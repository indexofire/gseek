"""Content-addressed index cache with cross-process locking.

Index keys are built from target content hashes, semantic parameters, adapter
version, tool versions, and the index format version — never from mtimes. A
build happens in a private staging directory and is published with a directory
rename; ``index.json`` is written before the rename so a visible index is
always complete.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from gseek.errors import CacheError, IndexBuildError, IndexLockError
from gseek.model import IndexRef, InputRef
from gseek.provenance import (
    semantic_params,
    sha256_json,
    utc_now_iso,
)
from gseek.types import INDEX_KEY_SCHEMA, INDEX_SCHEMA, JsonValue

try:  # pragma: no cover - platform-specific
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_DEFAULT_LOCK_TIMEOUT_S = 300.0
_LOCK_POLL_S = 0.1


class IndexPolicy(StrEnum):
    """How an alignment request should use the index cache."""

    AUTO = "auto"
    REUSE = "reuse"
    REBUILD = "rebuild"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class IndexGcEntry:
    key: str
    adapter: str
    path: Path
    size_bytes: int
    created_at: str | None
    removed: bool = False


def default_cache_root() -> Path:
    """Resolve the cache root: ``GSEEK_CACHE_DIR`` > XDG > ``~/.cache/gseek``."""
    configured = os.environ.get("GSEEK_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "gseek"
    return Path.home() / ".cache" / "gseek"


def compute_index_key(
    *,
    adapter: str,
    adapter_version: str,
    targets: Sequence[InputRef],
    params: Mapping[str, JsonValue],
    tool_versions: Mapping[str, str],
    format_version: str = "1",
) -> str:
    """Compute the deterministic key for an index build."""
    payload = {
        "schema": INDEX_KEY_SCHEMA,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "format_version": format_version,
        "params": semantic_params(params),
        "targets": [
            {"sha256": item.sha256, "kind": item.kind.value, "size": item.size} for item in targets
        ],
        "tools": {name: tool_versions[name] for name in sorted(tool_versions)},
    }
    return sha256_json(payload)


def directory_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:  # pragma: no cover - race
                continue
    return total


def _valid_entry(entry: str) -> bool:
    """Reject absolute/escaping/globbing entry names from index manifests."""
    if not entry or entry in {".", ".."}:
        return False
    path = Path(entry)
    if path.is_absolute() or ".." in path.parts:
        return False
    return not any(char in entry for char in "*?[")


def _entry_exists(directory: Path, entry: str) -> bool:
    """Return True for a file/dir entry or a prefix with artifacts."""
    if (directory / entry).exists():
        return True
    return any(directory.glob(f"{entry}.*"))


class IndexStore:
    """A filesystem cache for adapter indexes."""

    def __init__(self, root: Path | None = None, *, enabled: bool = True) -> None:
        self.root = (root or default_cache_root()).expanduser()
        self.enabled = enabled
        self._root_ready = False

    @property
    def index_root(self) -> Path:
        return self.root / "index"

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise CacheError("index cache is disabled (enabled=False)")

    def _ensure_root(self) -> None:
        if not self.enabled:
            return
        if self._root_ready:
            return
        try:
            self.index_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CacheError(f"cannot create cache directory {self.index_root}: {exc}") from exc
        self._root_ready = True

    def adapter_root(self, adapter: str) -> Path:
        if not adapter or "/" in adapter or "\\" in adapter or ".." in adapter:
            raise CacheError(f"invalid adapter name for cache path: {adapter!r}")
        return self.index_root / adapter

    def path_for(self, adapter: str, key: str) -> Path:
        if not key or "/" in key or "\\" in key or ".." in key:
            raise CacheError(f"invalid index key: {key!r}")
        return self.adapter_root(adapter) / key

    def lookup(self, adapter: str, key: str) -> IndexRef | None:
        """Return a valid cached index, or ``None`` if missing/corrupt."""
        if not self.enabled:
            return None
        final = self.path_for(adapter, key)
        manifest_path = final / "index.json"
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if (
            data.get("schema") != INDEX_SCHEMA
            or data.get("key") != key
            or data.get("adapter") != adapter
        ):
            return None
        entry = str(data.get("entry", ""))
        artifacts = tuple(str(item) for item in data.get("artifacts", ()))
        if not _valid_entry(entry) or not _entry_exists(final, entry):
            return None
        for pattern in artifacts:
            if not any(final.glob(pattern)):
                return None
        try:
            return IndexRef(
                key=key,
                adapter=adapter,
                adapter_version=str(data["adapter_version"]),
                entry=entry,
                source_sha256=str(data["source_sha256"]),
                artifacts=artifacts,
                params=dict(data.get("params", {})),
                created_at=str(data["created_at"]) if data.get("created_at") is not None else None,
                cache_hit=True,
                tool_versions={
                    str(k): str(v) for k, v in dict(data.get("tool_versions", {})).items()
                },
            )
        except (KeyError, TypeError, ValueError):
            return None

    def new_staging(self, adapter: str, key: str) -> Path:
        self._require_enabled()
        self._ensure_root()
        root = self.adapter_root(adapter)
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".staging-{key}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def publish(
        self,
        staging: Path,
        *,
        adapter: str,
        key: str,
        adapter_version: str,
        entry: str,
        artifacts: Sequence[str],
        source_sha256: str,
        params: Mapping[str, JsonValue],
        tool_versions: Mapping[str, str],
        created_at: str | None = None,
        replace: bool = False,
    ) -> IndexRef:
        """Validate *staging*, write ``index.json``, and atomically publish it.

        With ``replace=False`` an already-published, valid index wins and the
        staging directory is discarded. ``replace=True`` is used by
        ``IndexPolicy.REBUILD`` to publish the newly built artifacts even when
        a valid index already exists.
        """
        self._require_enabled()
        self._ensure_root()
        if not staging.is_dir():
            raise IndexBuildError(f"staging directory does not exist: {staging}")
        if not _valid_entry(entry):
            raise IndexBuildError(
                f"invalid index entry name: {entry!r}",
                context={"entry": entry},
            )
        if not _entry_exists(staging, entry):
            raise IndexBuildError(
                f"index entry {entry!r} was not produced under {staging}",
                context={"entry": entry, "staging": str(staging)},
            )
        missing = [pattern for pattern in artifacts if not any(staging.glob(pattern))]
        if missing:
            raise IndexBuildError(
                f"index artifacts missing after build: {missing}",
                context={"artifacts": ", ".join(missing)},
            )

        timestamp = created_at or utc_now_iso()
        document = {
            "schema": INDEX_SCHEMA,
            "key": key,
            "adapter": adapter,
            "adapter_version": adapter_version,
            "entry": entry,
            "artifacts": list(artifacts),
            "source_sha256": source_sha256,
            "params": dict(params),
            "tool_versions": dict(tool_versions),
            "created_at": timestamp,
        }
        manifest_path = staging / "index.json"
        with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        final = self.path_for(adapter, key)
        if final.exists():
            if not replace:
                existing = self.lookup(adapter, key)
                if existing is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                    return existing
            try:
                if final.is_dir() and not final.is_symlink():
                    shutil.rmtree(final)
                else:
                    final.unlink()
            except OSError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise IndexBuildError(
                    f"could not replace existing index {key}: {exc}",
                    context={"adapter": adapter, "key": key},
                ) from exc

        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, final)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise IndexBuildError(f"could not publish index {key}: {exc}") from exc

        return IndexRef(
            key=key,
            adapter=adapter,
            adapter_version=adapter_version,
            entry=entry,
            source_sha256=source_sha256,
            artifacts=tuple(artifacts),
            params=dict(params),
            created_at=timestamp,
            cache_hit=False,
            tool_versions=dict(tool_versions),
        )

    @contextmanager
    def lock(
        self, adapter: str, key: str, *, timeout_s: float = _DEFAULT_LOCK_TIMEOUT_S
    ) -> Iterator[None]:
        """Acquire an exclusive cross-process lock for one index key."""
        self._require_enabled()
        self._ensure_root()
        root = self.adapter_root(adapter)
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / f"{key}.lock"
        if fcntl is None:  # pragma: no cover - Windows fallback
            yield
            return
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise IndexLockError(
                            f"timed out waiting for index lock: {lock_path}",
                            context={"adapter": adapter, "key": key},
                        ) from None
                    time.sleep(_LOCK_POLL_S)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def list_indexes(self) -> tuple[IndexGcEntry, ...]:
        if not self.index_root.is_dir():
            return ()
        entries: list[IndexGcEntry] = []
        for adapter_dir in sorted(self.index_root.iterdir()):
            if not adapter_dir.is_dir() or adapter_dir.name.startswith("."):
                continue
            for index_dir in sorted(adapter_dir.iterdir()):
                if not index_dir.is_dir() or index_dir.name.startswith("."):
                    continue
                manifest_path = index_dir / "index.json"
                created_at: str | None = None
                if manifest_path.is_file():
                    try:
                        data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        data = None
                    if isinstance(data, dict) and data.get("created_at") is not None:
                        created_at = str(data["created_at"])
                entries.append(
                    IndexGcEntry(
                        key=index_dir.name,
                        adapter=adapter_dir.name,
                        path=index_dir,
                        size_bytes=directory_size(index_dir),
                        created_at=created_at,
                    )
                )
        entries.sort(key=lambda entry: (entry.created_at or "", entry.adapter, entry.key))
        return tuple(entries)

    def gc(
        self,
        *,
        max_bytes: int | None = None,
        max_age_days: float | None = None,
        dry_run: bool = True,
    ) -> tuple[IndexGcEntry, ...]:
        """Remove old/large indexes (oldest first); returns affected entries."""
        if not self.enabled:
            return ()
        now = time.time()
        entries = list(self.list_indexes())
        total = sum(entry.size_bytes for entry in entries)
        selected: list[IndexGcEntry] = []
        for entry in entries:
            too_old = False
            if max_age_days is not None and entry.created_at:
                try:
                    from datetime import datetime

                    created = datetime.fromisoformat(
                        entry.created_at.replace("Z", "+00:00")
                    ).timestamp()
                    too_old = (now - created) > max_age_days * 86400
                except ValueError:
                    too_old = False
            too_large = max_bytes is not None and total > max_bytes
            if too_old or too_large:
                selected.append(entry)
                total -= entry.size_bytes
        if dry_run:
            return tuple(selected)
        removed: list[IndexGcEntry] = []
        for entry in selected:
            # Best-effort: never delete an index while another process holds
            # its build lock. Skip it and let a later GC retry.
            try:
                with self.lock(entry.adapter, entry.key, timeout_s=0.1):
                    shutil.rmtree(entry.path, ignore_errors=True)
            except IndexLockError:
                continue
            if entry.path.exists():
                continue
            removed.append(
                IndexGcEntry(
                    key=entry.key,
                    adapter=entry.adapter,
                    path=entry.path,
                    size_bytes=entry.size_bytes,
                    created_at=entry.created_at,
                    removed=True,
                )
            )
        return tuple(removed)


__all__ = [
    "IndexGcEntry",
    "IndexPolicy",
    "IndexStore",
    "compute_index_key",
    "default_cache_root",
    "directory_size",
]
