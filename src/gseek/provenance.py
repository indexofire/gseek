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
from gseek.types import RECIPE_SCHEMA, InputKind, JsonValue, RunStatus
from gseek.version import __version__

_CHUNK_SIZE = 1024 * 1024
# Concurrent file-hash workers (hashlib releases the GIL on big updates).
_HASH_WORKERS = min(8, (os.cpu_count() or 2))
_DEFAULT_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_TREE_ENTRIES = 1_000_000

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


def validate_path_text(path: Path) -> str:
    """Reject paths containing control characters that can forge tool output."""
    text = str(path)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise InputError(
            "path contains control characters",
            context={"path": repr(text)},
        )
    return text


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


def _tree_entries(root: Path, *, max_entries: int | None = None) -> Iterable[tuple[Path, str, str]]:
    """Yield regular files/directories in deterministic relative-path order.

    Symlinks are rejected: following them would make a manifest depend on
    files outside the external reference and complicate reproducibility.
    """
    entries: list[Path] = []
    try:
        for path in root.rglob("*"):
            entries.append(path)
            if max_entries is not None and len(entries) > max_entries:
                raise InputError(
                    f"directory tree has too many entries (limit {max_entries})",
                    context={"path": str(root), "limit": str(max_entries)},
                )
        entries.sort(key=lambda path: path.relative_to(root).as_posix())
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


def sha256_tree(path: Path, *, max_entries: int | None = None) -> str:
    """Hash a directory tree deterministically (relative paths + file bytes).

    The tree hash is not a hash of a tar/zip; it is gseek's stable directory
    identity. Entries are sorted by POSIX relative path and include type, path,
    and regular-file content. Symlinks are rejected.
    """
    return _tree_digest_and_size(
        path, max_entries=_DEFAULT_MAX_TREE_ENTRIES if max_entries is None else max_entries
    )[0]


def directory_size(path: Path, *, max_entries: int | None = None) -> int:
    """Total size of regular files in a directory tree (symlinks rejected).

    Stat-only: callers that only need a size (external-reference validation)
    must not pay for a full tree hash.
    """
    root = path.resolve()
    if not root.is_dir():
        raise InputError(f"directory not found: {path}", context={"path": str(path)})
    total = 0
    for child, _relative, kind in _tree_entries(
        root, max_entries=_DEFAULT_MAX_TREE_ENTRIES if max_entries is None else max_entries
    ):
        if kind == "F":
            try:
                total += child.stat().st_size
            except OSError as exc:
                raise InputError(
                    f"cannot stat directory entry: {child}: {exc}",
                    context={"path": str(child)},
                ) from exc
    return total


def _tree_digest_and_size(path: Path, *, max_entries: int | None = None) -> tuple[str, int]:
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
    for child, relative, kind in _tree_entries(
        root, max_entries=_DEFAULT_MAX_TREE_ENTRIES if max_entries is None else max_entries
    ):
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
    validate_path_text(path)
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
    validate_path_text(path)
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
            {
                "name": tool.name,
                "version": tool.version,
                **({"sha256": tool.sha256} if tool.sha256 is not None else {}),
            }
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
    # Manifests contain absolute paths, command argv, and stderr tails; keep
    # them private instead of inheriting a permissive process umask.
    atomic_write_text(path, text, mode=0o600)
    return path


def read_manifest(path: Path, *, max_bytes: int | None = None) -> RunManifest:
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}", context={"path": str(path)})
    limit = _DEFAULT_MAX_MANIFEST_BYTES if max_bytes is None else max(0, max_bytes)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestError(
            f"cannot stat manifest {path}: {exc}", context={"path": str(path)}
        ) from exc
    if size > limit:
        raise ManifestError(
            f"manifest is too large: {size} bytes (limit {limit})",
            context={"path": str(path), "size": str(size), "limit": str(limit)},
        )
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
    strict_paths: bool = False,
) -> tuple[Issue, ...]:
    """Check that recorded files still match their size and SHA-256.

    Tool versions are not re-probed here because that requires a resolver; the
    CLI's ``verify`` command can add that check later. ``strict_paths`` confines
    every recorded path (after resolving symlinks) to ``base_dir`` (or the
    current directory when ``base_dir`` is omitted).

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
            try:
                validate_path_text(path)
            except InputError:
                issues.append(
                    Issue(
                        code=f"{label.upper()}_PATH_INVALID",
                        message=f"{label} path contains control characters: {path!r}",
                        severity="error",
                        context={"path": repr(str(path))},
                    )
                )
                continue
            if strict_paths:
                try:
                    root = (base_dir or Path.cwd()).resolve()
                    resolved = path.resolve()
                except (OSError, RuntimeError, ValueError):
                    issues.append(
                        Issue(
                            code=f"{label.upper()}_PATH_UNRESOLVABLE",
                            message=f"{label} path cannot be resolved: {path}",
                            severity="error",
                            context={"path": str(path)},
                        )
                    )
                    continue
                if not resolved.is_relative_to(root):
                    issues.append(
                        Issue(
                            code=f"{label.upper()}_PATH_OUTSIDE_BASE",
                            message=f"{label} path escapes the base directory: {path}",
                            severity="error",
                            context={"path": str(path), "base_dir": str(root)},
                        )
                    )
                    continue
                path = resolved
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

    # Structural and recipe checks: a manifest that only happens to point at
    # intact files is not necessarily a complete or internally consistent run.
    if not manifest.inputs:
        issues.append(
            Issue(
                code="INPUTS_EMPTY",
                message="manifest records no inputs",
                severity="error",
                context={"backend": manifest.backend},
            )
        )
    if not manifest.targets:
        issues.append(
            Issue(
                code="TARGETS_EMPTY",
                message="manifest records no targets",
                severity="error",
                context={"backend": manifest.backend},
            )
        )
    if manifest.status is RunStatus.FAILED:
        issues.append(
            Issue(
                code="RUN_STATUS_FAILED",
                message="manifest records a failed run",
                severity="error",
                context={"backend": manifest.backend},
            )
        )
    try:
        expected_recipe = recipe_hash(
            backend=manifest.backend,
            adapter_version=manifest.backend_version,
            inputs=manifest.inputs,
            targets=manifest.targets,
            params=manifest.params,
            tools=manifest.tools,
            index_key=manifest.index.key if manifest.index is not None else None,
            gseek_version_value=manifest.gseek_version,
        )
    except (InputError, TypeError, ValueError) as exc:
        issues.append(
            Issue(
                code="RECIPE_HASH_UNVERIFIABLE",
                message=f"manifest recipe cannot be recomputed: {exc}",
                severity="error",
                context={"backend": manifest.backend},
            )
        )
    else:
        if expected_recipe != manifest.recipe_hash:
            issues.append(
                Issue(
                    code="RECIPE_HASH_MISMATCH",
                    message="manifest recipe hash does not match its recorded fields",
                    severity="error",
                    context={
                        "expected": manifest.recipe_hash,
                        "actual": expected_recipe,
                    },
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
    "validate_path_text",
    "verify_manifest",
    "write_manifest",
]
