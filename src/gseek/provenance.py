"""Hashing, canonical JSON, recipes, and run-manifest persistence."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import socket
import uuid
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from gseek.errors import InputError, ManifestError
from gseek.io import atomic_write_text, count_records, detect_input_kind, is_compressed
from gseek.model import (
    AlignmentRecord,
    DistanceRecord,
    FileRef,
    InputRef,
    Issue,
    RunManifest,
    ToolRef,
)
from gseek.types import RECIPE_SCHEMA, InputKind, JsonValue
from gseek.version import __version__

_CHUNK_SIZE = 1024 * 1024
# Concurrent file-hash workers (hashlib releases the GIL on big updates).
_HASH_WORKERS = min(8, (os.cpu_count() or 2))

# Parameters that describe how a job is executed rather than what it computes.
# They are excluded from recipe_hash but preserved in the manifest.
VOLATILE_PARAM_KEYS = frozenset(
    {
        "threads",
        "num_threads",
        "timeout_s",
        "timeout",
        "keep_work",
        "work_dir",
        "cache_dir",
        "verbose",
        "quiet",
        "debug",
        "manifest",
        "manifest_path",
        "output",
        "output_path",
    }
)


# ---------------------------------------------------------------------------
# Hashing and canonical JSON
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, *, chunk_size: int = _CHUNK_SIZE) -> str:
    """Hash a file without loading it into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise InputError(
            f"cannot read file for hashing: {path}: {exc}", context={"path": str(path)}
        ) from exc
    return digest.hexdigest()


def hash_and_count(
    path: Path, kind: InputKind, *, chunk_size: int = _CHUNK_SIZE
) -> tuple[str, int]:
    """Hash a file and count its records in one pass when possible.

    For uncompressed FASTA the header count is accumulated from the same byte
    chunks fed to the digest (``b"\\n>"`` plus a leading ``b">"``), halving
    the full-file scans ``input_ref`` performs on every run. Compressed files
    and FASTQ fall back to the separate passes (FASTQ counting must validate
    the four-line record structure).
    """
    if kind is not InputKind.FASTA or is_compressed(path):
        return sha256_file(path, chunk_size=chunk_size), count_records(path, kind)

    digest = hashlib.sha256()
    headers = 0
    previous_byte = b""
    try:
        with path.open("rb") as handle:
            first = True
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
                if first and chunk[:1] == b">":
                    headers += 1
                first = False
                # A header line starts at a record boundary: the byte before
                # '>' must be a newline. Keep one byte across chunk edges so a
                # boundary-spanning "\n>" is not missed or double-counted.
                if previous_byte == b"\n" and chunk[:1] == b">":
                    headers += 1
                headers += chunk.count(b"\n>")
                previous_byte = chunk[-1:]
    except OSError as exc:
        raise InputError(
            f"cannot read file for hashing: {path}: {exc}", context={"path": str(path)}
        ) from exc
    return digest.hexdigest(), headers


def _tree_entries(root: Path) -> Iterable[tuple[Path, str, str]]:
    """Yield regular files/directories in deterministic relative-path order.

    Symlinks are rejected: following them would make a manifest depend on
    files outside the external reference and complicate reproducibility.
    """
    try:
        entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    except OSError as exc:
        raise InputError(
            f"cannot read directory for hashing: {root}: {exc}", context={"path": str(root)}
        ) from exc
    for path in entries:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise InputError(
                "external reference directory contains a symlink",
                context={"path": str(path)},
                hint="use a real directory or a single .sig/.zip file",
            )
        if path.is_dir():
            yield path, relative, "D"
        elif path.is_file():
            yield path, relative, "F"
        else:
            raise InputError(
                "external reference directory contains an unsupported entry",
                context={"path": str(path)},
            )


def sha256_tree(path: Path) -> str:
    """Hash a directory tree deterministically (relative paths + file bytes).

    The tree hash is not a hash of a tar/zip; it is gseek's stable directory
    identity. Entries are sorted by POSIX relative path and include type, path,
    and regular-file content. Symlinks are rejected.
    """
    return _tree_digest_and_size(path)[0]


def directory_size(path: Path) -> int:
    """Total size of regular files in a directory tree (symlinks rejected).

    Stat-only: callers that only need a size (external-reference validation)
    must not pay for a full tree hash.
    """
    root = path.resolve()
    if not root.is_dir():
        raise InputError(f"directory not found: {path}", context={"path": str(path)})
    total = 0
    for child, _relative, kind in _tree_entries(root):
        if kind == "F":
            try:
                total += child.stat().st_size
            except OSError as exc:
                raise InputError(
                    f"cannot stat directory entry: {child}: {exc}",
                    context={"path": str(child)},
                ) from exc
    return total


def _tree_digest_and_size(path: Path) -> tuple[str, int]:
    """One traversal producing both the tree digest and its total size.

    ``input_ref`` and ``verify_manifest`` need both; computing them with two
    separate ``rglob`` walks doubled the stat traffic on large sketch
    databases (thousands of SST files).
    """
    root = path.resolve()
    if not root.is_dir():
        raise InputError(f"directory not found: {path}", context={"path": str(path)})
    digest = hashlib.sha256(b"gseek.tree/1\0")
    total = 0
    for child, relative, kind in _tree_entries(root):
        encoded = relative.encode("utf-8")
        digest.update(kind.encode("ascii") + b"\0")
        digest.update(encoded + b"\0")
        if kind == "F":
            try:
                size = child.stat().st_size
            except OSError as exc:
                raise InputError(
                    f"cannot stat directory entry: {child}: {exc}",
                    context={"path": str(child)},
                ) from exc
            total += size
            digest.update(bytes.fromhex(sha256_file(child)))
    return digest.hexdigest(), total


def to_jsonable(value: Any) -> Any:
    """Best-effort conversion of dataclasses/Path/Enum/tuples to JSON values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        # Sets have no defined iteration order; sort by a stable representation
        # so canonical JSON and content hashes do not depend on PYTHONHASHSEED.
        # Objects whose repr embeds an address would silently make the hash
        # process-dependent: reject them instead.
        for item in value:
            if not isinstance(item, (str, int, float, bool, bytes, type(None), Enum)):
                raise InputError(
                    "sets passed to canonical JSON must contain only stable scalars"
                    " (str/int/float/bool/bytes/None), "
                    f"got {type(item).__name__}",
                    context={"element_type": type(item).__name__},
                )
        ordered = sorted(value, key=lambda item: (type(item).__name__, repr(item)))
        return [to_jsonable(item) for item in ordered]
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        # Raw bytes (rare in params) decode lossily instead of crashing
        # canonical JSON with a bare TypeError.
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_json(value: Any) -> str:
    """Stable JSON encoding used for all content hashes."""
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


# ---------------------------------------------------------------------------
# Time, host, version
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:  # pragma: no cover - platform-dependent
        return None


def platform_label() -> str:
    return f"{platform.system()}-{platform.release()}-{platform.machine()}"


def gseek_version() -> str:
    return __version__


def new_run_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# File/input references
# ---------------------------------------------------------------------------


def file_ref(path: Path) -> FileRef:
    resolved = path.resolve()
    if not resolved.is_file():
        raise InputError(f"file not found: {path}", context={"path": str(path)})
    return FileRef(
        path=str(resolved),
        size=resolved.stat().st_size,
        sha256=sha256_file(resolved),
    )


def input_ref(
    path: Path,
    *,
    kind: InputKind | None = None,
    compute_records: bool = True,
) -> InputRef:
    resolved = path.resolve()
    if resolved.is_dir():
        if kind is not InputKind.SKETCH:
            raise InputError(
                "directory inputs are only supported for sketch external references",
                context={"path": str(path)},
            )
        digest, size = _tree_digest_and_size(resolved)
        return InputRef(
            path=str(resolved),
            size=size,
            sha256=digest,
            kind=InputKind.SKETCH,
            records=None,
            compressed=False,
        )
    if not resolved.is_file():
        raise InputError(f"input file not found: {path}", context={"path": str(path)})
    resolved_kind = kind or detect_input_kind(resolved)
    if compute_records:
        digest, records = hash_and_count(resolved, resolved_kind)
    else:
        digest, records = sha256_file(resolved), None
    return InputRef(
        path=str(resolved),
        size=resolved.stat().st_size,
        sha256=digest,
        kind=resolved_kind,
        records=records,
        compressed=is_compressed(resolved),
    )


def input_refs(
    paths: Iterable[Path],
    *,
    kind: InputKind | None = None,
    compute_records: bool = True,
) -> tuple[InputRef, ...]:
    """Build :class:`InputRef` values for every path, in input order.

    Two or more files are hashed concurrently: ``hashlib`` releases the GIL
    on large updates, so multi-reference runs scale with cores instead of
    walking every file serially.
    """
    materialized = tuple(paths)
    if len(materialized) > 1:
        workers = min(len(materialized), _HASH_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return tuple(
                pool.map(
                    lambda path: input_ref(path, kind=kind, compute_records=compute_records),
                    materialized,
                )
            )
    return tuple(
        input_ref(path, kind=kind, compute_records=compute_records) for path in materialized
    )


def tool_refs(tools: Iterable[ToolRef]) -> tuple[ToolRef, ...]:
    return tuple(sorted(tools, key=lambda tool: tool.name))


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def semantic_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Drop execution-only parameters and unset values for recipe hashing."""
    return {
        str(key): value
        for key, value in params.items()
        if key not in VOLATILE_PARAM_KEYS and value is not None
    }


def recipe_hash(
    *,
    backend: str,
    adapter_version: str,
    inputs: Sequence[InputRef],
    targets: Sequence[InputRef],
    params: Mapping[str, JsonValue],
    tools: Sequence[ToolRef],
    index_key: str | None = None,
    gseek_version_value: str | None = None,
) -> str:
    """Hash the semantic identity of a run (no timestamps/host/paths)."""
    payload = {
        "schema": RECIPE_SCHEMA,
        "gseek_version": gseek_version_value or gseek_version(),
        "backend": backend,
        "adapter_version": adapter_version,
        "params": semantic_params(params),
        "inputs": [
            {"sha256": item.sha256, "kind": item.kind.value, "records": item.records}
            for item in inputs
        ],
        "targets": [
            {"sha256": item.sha256, "kind": item.kind.value, "records": item.records}
            for item in targets
        ],
        "index_key": index_key,
        "tools": [
            {"name": tool.name, "version": tool.version}
            for tool in sorted(tools, key=lambda item: item.name)
        ],
    }
    return sha256_json(payload)


def result_hash(records: Sequence[AlignmentRecord]) -> str:
    """Canonical hash of normalized alignment records (order-independent).

    Streams one cached canonical payload per record instead of materializing
    the full JSON document: byte-identical to hashing the serialized list in
    one shot (``json.dumps`` of a list is ``"[" + ",".join(items) + "]"``),
    with O(1) extra memory.
    """
    digest = hashlib.sha256()
    digest.update(b"[")
    for position, record in enumerate(sorted(records, key=AlignmentRecord.stable_key)):
        if position:
            digest.update(b",")
        digest.update(record.canonical_payload().encode("utf-8"))
    digest.update(b"]")
    return digest.hexdigest()


def distance_result_hash(records: Sequence[DistanceRecord]) -> str:
    """Canonical hash of normalized distance records (order-independent)."""
    digest = hashlib.sha256()
    digest.update(b"[")
    for position, record in enumerate(sorted(records, key=DistanceRecord.stable_key)):
        if position:
            digest.update(b",")
        digest.update(record.canonical_payload().encode("utf-8"))
    digest.update(b"]")
    return digest.hexdigest()


def alignment_result_hash(records: Sequence[AlignmentRecord]) -> str:
    """Canonical hash of normalized alignment records."""
    return result_hash(records)


# ---------------------------------------------------------------------------
# Manifest persistence and verification
# ---------------------------------------------------------------------------


def write_manifest(path: Path, manifest: RunManifest) -> Path:
    """Atomically write a pretty-printed ``gseek.run/1`` manifest."""
    text = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    atomic_write_text(path, text)
    return path


def read_manifest(path: Path) -> RunManifest:
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}", context={"path": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(
            f"cannot read manifest {path}: {exc}", context={"path": str(path)}
        ) from exc
    if not isinstance(data, dict):
        raise ManifestError(
            f"manifest root must be a JSON object: {path}", context={"path": str(path)}
        )
    return RunManifest.from_dict(data)


def _resolve_ref_path(ref_path: str, base_dir: Path | None) -> Path:
    path = Path(ref_path)
    if path.is_absolute() or base_dir is None:
        return path
    return base_dir / path


def verify_manifest(
    manifest: RunManifest,
    *,
    base_dir: Path | None = None,
    prefetched: Mapping[str, tuple[int, str]] | None = None,
) -> tuple[Issue, ...]:
    """Check that recorded files still match their size and SHA-256.

    Tool versions are not re-probed here because that requires a resolver; the
    CLI's ``verify`` command can add that check later.

    ``prefetched`` maps a resolved path to a freshly computed ``(size,
    sha256)``. When present the values are used instead of re-reading the
    file, letting callers that just hashed the inputs (replay) verify without
    doubling the I/O; the comparison against the recorded values is unchanged.
    """
    issues: list[Issue] = []
    groups = (
        ("input", manifest.inputs),
        ("target", manifest.targets),
        ("output", manifest.outputs),
    )
    for label, refs in groups:
        for ref in refs:
            path = _resolve_ref_path(ref.path, base_dir)
            known = prefetched.get(str(path)) if prefetched is not None else None
            if known is not None:
                size, digest = known
            elif path.is_dir():
                if not isinstance(ref, InputRef) or ref.kind is not InputKind.SKETCH:
                    issues.append(
                        Issue(
                            code=f"{label.upper()}_UNEXPECTED_DIRECTORY",
                            message=f"{label} points at a directory: {path}",
                            severity="error",
                            context={"path": str(path)},
                        )
                    )
                    continue
                try:
                    digest, size = _tree_digest_and_size(path)
                except (InputError, OSError) as exc:
                    # verify's job is to report damage, not to die on it.
                    issues.append(
                        Issue(
                            code=f"{label.upper()}_UNREADABLE",
                            message=f"{label} directory cannot be read: {path}: {exc}",
                            severity="error",
                            context={"path": str(path)},
                        )
                    )
                    continue
            elif path.is_file():
                size = path.stat().st_size
                digest = sha256_file(path)
            else:
                issues.append(
                    Issue(
                        code=f"{label.upper()}_MISSING",
                        message=f"{label} file is missing: {path}",
                        severity="error",
                        context={"path": str(path)},
                    )
                )
                continue
            if size != ref.size:
                issues.append(
                    Issue(
                        code=f"{label.upper()}_SIZE_MISMATCH",
                        message=f"{label} size changed: {path} ({ref.size} -> {size})",
                        severity="error",
                        context={"path": str(path)},
                    )
                )
                continue
            if digest != ref.sha256:
                issues.append(
                    Issue(
                        code=f"{label.upper()}_HASH_MISMATCH",
                        message=f"{label} content changed: {path}",
                        severity="error",
                        context={"path": str(path), "expected": ref.sha256, "actual": digest},
                    )
                )
    return tuple(issues)


__all__ = [
    "VOLATILE_PARAM_KEYS",
    "alignment_result_hash",
    "canonical_json",
    "directory_size",
    "distance_result_hash",
    "file_ref",
    "gseek_version",
    "hostname",
    "input_ref",
    "input_refs",
    "new_run_id",
    "platform_label",
    "read_manifest",
    "recipe_hash",
    "result_hash",
    "semantic_params",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "sha256_text",
    "sha256_tree",
    "to_jsonable",
    "tool_refs",
    "utc_now_iso",
    "verify_manifest",
    "write_manifest",
]
