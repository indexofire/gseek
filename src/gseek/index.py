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
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from gseek.errors import CacheError, IndexBuildError, IndexLockError, InputError
from gseek.model import IndexRef, InputRef
from gseek.provenance import (
    semantic_params,
    sha256_file,
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
_LEASE_FILE = ".inuse"
_LEASE_PREFIX = ".inuse-"
_OWNER_FILE = ".owner.json"
_LEASE_FRESH_S = 3600.0
_LEASE_REFRESH_S = 300.0
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


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
    inode: int | None = None
    mtime_ns: int | None = None


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
    tool_hashes: Mapping[str, str] | None = None,
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
    if tool_hashes:
        payload["tool_hashes"] = {name: tool_hashes[name] for name in sorted(tool_hashes)}
    return sha256_json(payload)


def _lock_timeout_from_env() -> float:
    """Lock timeout in seconds, overridable via GSEEK_INDEX_LOCK_TIMEOUT."""
    raw = os.environ.get("GSEEK_INDEX_LOCK_TIMEOUT", "")
    if raw:
        with suppress(ValueError):
            value = float(raw)
            if value > 0:
                return value
    return _DEFAULT_LOCK_TIMEOUT_S


def _valid_key(key: str) -> bool:
    """Require the 64-char SHA-256 shape used by :func:`compute_index_key`.

    A permissive key (``"."`` in particular) would make ``path_for`` resolve to
    the adapter directory itself, so a publish could delete the whole cache.
    """
    return len(key) == 64 and all(char in _HEX_DIGITS for char in key)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def _valid_artifact_path(value: str) -> bool:
    """Validate an exact relative artifact path stored in ``index.json``."""
    if not value or value in {".", ".."}:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _verify_artifacts_from_env() -> bool:
    raw = os.environ.get("GSEEK_INDEX_VERIFY", "").strip().lower()
    return raw in {"1", "true", "yes", "on", "full"}


def _resolved_within(path: Path, root: Path) -> bool:
    """True when *path* resolves inside *root* without following an escape."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _harden_dir(path: Path) -> None:
    """Best-effort private mode for a cache directory (never follow a link)."""
    if path.is_symlink():
        return
    with suppress(OSError):  # pragma: no cover - read-only/exotic filesystem
        os.chmod(path, 0o700)


def _harden_tree(path: Path) -> None:
    """Best-effort private modes for a freshly built index tree."""
    if path.is_symlink():
        return
    for root, _directories, files in os.walk(path):
        _harden_dir(Path(root))
        for name in files:
            child = Path(root) / name
            if child.is_symlink():
                continue
            with suppress(OSError):  # pragma: no cover - read-only/exotic filesystem
                os.chmod(child, 0o600)


def _lease_is_fresh(index_dir: Path) -> bool:
    """True when any in-use lease was touched recently."""
    try:
        leases = tuple(index_dir.glob(f"{_LEASE_FILE}*"))
    except OSError:
        return False
    for lease in leases:
        try:
            age = time.time() - lease.stat().st_mtime
        except OSError:
            continue
        if age < _LEASE_FRESH_S:
            return True
    return False


def _same_entry_identity(entry: IndexGcEntry) -> bool:
    """True when a listed index is still the same directory at delete time."""
    if entry.inode is None:
        return True
    try:
        stat_result = entry.path.stat()
    except OSError:
        return False
    return stat_result.st_ino == entry.inode and stat_result.st_mtime_ns == entry.mtime_ns


def _process_identity() -> dict[str, str]:
    """Identity of the current process strong enough to survive pid reuse."""
    identity: dict[str, str] = {"pid": str(os.getpid())}
    with suppress(OSError):  # pragma: no cover - non-Linux
        identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        stat = Path(f"/proc/{os.getpid()}/stat").read_text()
        # comm may contain spaces/parens: fields resume after the last ')'.
        fields = stat[stat.rfind(")") + 1 :].split()
        identity["starttime"] = fields[19]  # field 22 overall
    except (OSError, IndexError):  # pragma: no cover - non-Linux
        pass
    return identity


def _staging_owner_alive(staging: Path) -> bool:
    """Return True when the staging directory's creating process still runs.

    Prefers the recorded boot_id/pid/starttime triple (immune to pid reuse);
    directories from older versions fall back to the pid embedded in the name.
    """
    owner_file = staging / _OWNER_FILE
    if owner_file.is_file():
        try:
            owner = json.loads(owner_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False  # unreadable identity: treat as garbage
        if not isinstance(owner, dict):
            return False  # malformed identity: treat as garbage
        recorded_boot = str(owner.get("boot_id", ""))
        current_boot = _process_identity().get("boot_id", "")
        if recorded_boot and current_boot and recorded_boot != current_boot:
            return False  # different boot: the pid cannot be the same process
        pid = str(owner.get("pid", ""))
        recorded_start = str(owner.get("starttime", ""))
        if not pid.isdigit():
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except (OSError, OverflowError):
            return True  # exists but is not ours; assume alive
        if recorded_start:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
                fields = stat[stat.rfind(")") + 1 :].split()
                if fields[19] != recorded_start:
                    return False  # pid reused by a different process
            except (OSError, IndexError):  # pragma: no cover - process raced out
                return False
        return True

    parts = staging.name.split("-")  # legacy: .staging-{key}-{pid}-{uuid}
    if len(parts) < 4:
        return False
    try:
        pid = int(parts[-2])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True  # exists but is not ours; assume alive
    return True


def tree_size(path: Path) -> int:
    """Total regular-file size under *path* (tolerant; cf. provenance.directory_size,
    which is strict and raises on unreadable trees)."""
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


def _valid_artifact_pattern(pattern: str) -> bool:
    """Reject artifact glob patterns that escape the index directory.

    Wildcards are allowed (patterns are matched with ``Path.glob``); absolute
    patterns crash ``Path.glob`` and ``..`` components could match outside the
    cache, so both are cache misses instead.
    """
    if not pattern or pattern.startswith(("/", "\\")):
        return False
    return ".." not in Path(pattern).parts


def _entry_exists(directory: Path, entry: str) -> bool:
    """Return True for a file/dir entry or a prefix with artifacts."""
    if (directory / entry).exists():
        return True
    return any(directory.glob(f"{entry}.*"))


class IndexLease:
    """A keep-alive lease for one index used by a running command."""

    def __init__(
        self,
        store: IndexStore,
        adapter: str,
        key: str,
        *,
        interval_s: float = _LEASE_REFRESH_S,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.key = key
        self.interval_s = max(0.001, interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lease_path: Path | None = None

    def _touch(self) -> None:
        if self._lease_path is None:
            return
        with suppress(OSError):
            self._lease_path.touch()

    def start(self) -> None:
        index_dir = self.store.path_for(self.adapter, self.key)
        self._lease_path = index_dir / f"{_LEASE_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._touch()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._touch()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._lease_path is not None:
            with suppress(OSError):
                self._lease_path.unlink()

    def __enter__(self) -> IndexLease:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class IndexStore:
    """A filesystem cache for adapter indexes."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        enabled: bool = True,
        verify_artifacts: bool | None = None,
    ) -> None:
        self.root = (root or default_cache_root()).expanduser()
        self.enabled = enabled
        # Metadata (size) is checked on every hit; full hashing is opt-in so a
        # cache hit never re-reads a multi-gigabyte index by default.
        self.verify_artifacts = (
            _verify_artifacts_from_env() if verify_artifacts is None else verify_artifacts
        )
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
            self.index_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise CacheError(f"cannot create cache directory {self.index_root}: {exc}") from exc
        if self.index_root.is_symlink() or not _resolved_within(self.index_root, self.root):
            raise CacheError(f"cache index directory escapes the cache root: {self.index_root}")
        _harden_dir(self.index_root)
        self._root_ready = True

    def adapter_root(self, adapter: str) -> Path:
        if (
            not adapter
            or adapter in {".", ".."}
            or "/" in adapter
            or "\\" in adapter
            or ".." in adapter
        ):
            raise CacheError(f"invalid adapter name for cache path: {adapter!r}")
        return self.index_root / adapter

    def _ensure_adapter_root(self, adapter: str) -> Path:
        root = self.adapter_root(adapter)
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise CacheError(f"cannot create cache directory {root}: {exc}") from exc
        if root.is_symlink() or not _resolved_within(root, self.index_root):
            raise CacheError(f"cache adapter directory escapes the cache root: {root}")
        _harden_dir(root)
        return root

    def path_for(self, adapter: str, key: str) -> Path:
        if not _valid_key(key):
            raise CacheError(f"invalid index key: {key!r}")
        return self.adapter_root(adapter) / key

    def lookup(self, adapter: str, key: str) -> IndexRef | None:
        """Return a valid cached index, or ``None`` if missing/corrupt."""
        if not self.enabled or self.index_root.is_symlink():
            return None
        final = self.path_for(adapter, key)
        if final.is_symlink() or not _resolved_within(final, self.index_root):
            return None
        manifest_path = final / "index.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
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
        raw_artifacts = data.get("artifacts", ())
        if isinstance(raw_artifacts, (str, bytes)) or not isinstance(raw_artifacts, (list, tuple)):
            return None
        if any(
            not isinstance(item, str) or not _valid_artifact_pattern(item) for item in raw_artifacts
        ):
            return None
        artifacts = tuple(raw_artifacts)
        artifact_hashes = data.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict):
            # Legacy or unsealed cache entries cannot be integrity-checked.
            return None
        entry_path = final / entry
        if (
            not _valid_entry(entry)
            or entry_path.is_symlink()
            or not _resolved_within(entry_path, final)
            or not _entry_exists(final, entry)
        ):
            return None
        used_artifacts: dict[str, Path] = {}
        if entry_path.is_file():
            used_artifacts[entry_path.relative_to(final).as_posix()] = entry_path
        try:
            for pattern in artifacts:
                matches = tuple(final.glob(pattern))
                if not matches:
                    return None
                for match in matches:
                    if match.is_symlink() or not _resolved_within(match, final):
                        return None
                    if match.is_file():
                        used_artifacts[match.relative_to(final).as_posix()] = match
        except (NotImplementedError, ValueError, OSError):  # pragma: no cover - defensive
            return None
        for relative, artifact_path in used_artifacts.items():
            record = artifact_hashes.get(relative)
            if not _valid_artifact_path(relative) or not isinstance(record, dict):
                return None
            size = record.get("size")
            digest = record.get("sha256")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or not _valid_sha256(digest)
            ):
                return None
            try:
                if artifact_path.stat().st_size != size:
                    return None
                if self.verify_artifacts and sha256_file(artifact_path) != digest:
                    return None
            except (InputError, OSError, ValueError):
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
        if not _valid_key(key):
            raise CacheError(f"invalid index key: {key!r}")
        root = self._ensure_adapter_root(adapter)
        staging = root / f".staging-{key}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=False, exist_ok=False, mode=0o700)
        _harden_dir(staging)
        # Owner identity: pid reuse would otherwise keep garbage staging alive
        # forever (same pid, different process). boot_id+starttime disambiguate.
        with suppress(OSError):  # pragma: no cover - defensive
            (staging / _OWNER_FILE).write_text(json.dumps(_process_identity()), encoding="utf-8")
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
        root = self._ensure_adapter_root(adapter)
        if (
            staging.is_symlink()
            or not staging.is_dir()
            or staging.parent.resolve() != root.resolve()
            or not _resolved_within(staging, root)
        ):
            raise IndexBuildError(
                f"staging directory is not a private build under {root}: {staging}",
                context={"adapter": adapter, "staging": str(staging)},
            )
        _harden_tree(staging)
        if not _valid_entry(entry):
            raise IndexBuildError(
                f"invalid index entry name: {entry!r}",
                context={"entry": entry},
            )
        entry_path = staging / entry
        if entry_path.is_symlink() or not _resolved_within(entry_path, staging):
            raise IndexBuildError(
                f"index entry escapes the staging directory: {entry_path}",
                context={"entry": entry, "staging": str(staging)},
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
        artifact_paths: dict[str, Path] = {}
        if entry_path.is_file():
            artifact_paths[entry_path.relative_to(staging).as_posix()] = entry_path
        for pattern in artifacts:
            for match in staging.glob(pattern):
                if match.is_symlink() or not _resolved_within(match, staging):
                    raise IndexBuildError(
                        f"index artifact escapes the staging directory: {match}",
                        context={"artifact": str(match), "staging": str(staging)},
                    )
                if match.is_file():
                    artifact_paths[match.relative_to(staging).as_posix()] = match
        artifact_hashes: dict[str, dict[str, int | str]] = {}
        for relative in sorted(artifact_paths):
            path = artifact_paths[relative]
            try:
                digest = sha256_file(path)
                size = path.stat().st_size
            except (InputError, OSError) as exc:
                raise IndexBuildError(
                    f"cannot hash index artifact {path}: {exc}",
                    context={"artifact": str(path), "staging": str(staging)},
                ) from exc
            artifact_hashes[relative] = {"size": size, "sha256": digest}

        timestamp = created_at or utc_now_iso()
        document = {
            "schema": INDEX_SCHEMA,
            "key": key,
            "adapter": adapter,
            "adapter_version": adapter_version,
            "entry": entry,
            "artifacts": list(artifacts),
            "artifact_hashes": artifact_hashes,
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
        with suppress(OSError):
            os.chmod(manifest_path, 0o600)

        final = self.path_for(adapter, key)
        if final.is_symlink() or not _resolved_within(final, self.index_root):
            raise IndexBuildError(
                f"index path escapes the cache root: {final}",
                context={"adapter": adapter, "key": key},
            )
        if final.exists():
            if not replace:
                existing = self.lookup(adapter, key)
                if existing is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                    return existing
            if replace and _lease_is_fresh(final):
                shutil.rmtree(staging, ignore_errors=True)
                raise IndexBuildError(
                    f"index {key} is currently in use; retry the rebuild later",
                    context={"adapter": adapter, "key": key},
                )
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
        self,
        adapter: str,
        key: str,
        *,
        timeout_s: float | None = None,
    ) -> Iterator[None]:
        """Acquire an exclusive cross-process lock for one index key.

        The default timeout comes from ``GSEEK_INDEX_LOCK_TIMEOUT`` (seconds)
        or 300s: large references can legitimately index for longer, and the
        waiter should keep waiting rather than fail.
        """
        self._require_enabled()
        self._ensure_root()
        if not _valid_key(key):
            raise CacheError(f"invalid index key: {key!r}")
        if timeout_s is None:
            timeout_s = _lock_timeout_from_env()
        root = self._ensure_adapter_root(adapter)
        lock_path = root / f"{key}.lock"
        if fcntl is None:  # pragma: no cover - Windows fallback
            yield
            return
        deadline = time.monotonic() + timeout_s
        while True:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            os.close(descriptor)
                            raise IndexLockError(
                                f"timed out waiting for index lock: {lock_path}",
                                context={"adapter": adapter, "key": key},
                            ) from None
                        time.sleep(_LOCK_POLL_S)
                # A GC between open and flock may have unlinked this lock file:
                # our fd would then guard an orphan inode while a new locker
                # opens a fresh one and both "hold" the lock. Re-validate.
                try:
                    same_inode = os.fstat(descriptor).st_ino == os.stat(lock_path).st_ino
                except OSError:
                    same_inode = False
                if same_inode:
                    break
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                raise
            with suppress(OSError):
                os.close(descriptor)
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                with suppress(OSError):
                    os.close(descriptor)

    def list_indexes(self) -> tuple[IndexGcEntry, ...]:
        if self.index_root.is_symlink() or not self.index_root.is_dir():
            return ()
        entries: list[IndexGcEntry] = []
        for adapter_dir in sorted(self.index_root.iterdir()):
            if (
                adapter_dir.is_symlink()
                or not adapter_dir.is_dir()
                or adapter_dir.name.startswith(".")
                or not _resolved_within(adapter_dir, self.index_root)
            ):
                continue
            for index_dir in sorted(adapter_dir.iterdir()):
                if (
                    index_dir.is_symlink()
                    or not index_dir.is_dir()
                    or index_dir.name.startswith(".")
                    or not _resolved_within(index_dir, adapter_dir)
                ):
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
                inode: int | None = None
                mtime_ns: int | None = None
                try:
                    stat_result = index_dir.stat()
                except OSError:
                    pass
                else:
                    inode = stat_result.st_ino
                    mtime_ns = stat_result.st_mtime_ns
                entries.append(
                    IndexGcEntry(
                        key=index_dir.name,
                        adapter=adapter_dir.name,
                        path=index_dir,
                        size_bytes=tree_size(index_dir),
                        created_at=created_at,
                        inode=inode,
                        mtime_ns=mtime_ns,
                    )
                )
        entries.sort(key=lambda entry: (entry.created_at or "", entry.adapter, entry.key))
        return tuple(entries)

    def _stale_staging_entries(self) -> list[IndexGcEntry]:
        """Leftover ``.staging-*`` directories from crashed builds.

        A staging directory is transient by construction; its name embeds the
        builder's pid, so anything owned by a dead process is pure garbage
        that neither ``list_indexes`` nor ``gc`` would otherwise reclaim.
        """
        stale: list[IndexGcEntry] = []
        if self.index_root.is_symlink() or not self.index_root.is_dir():
            return stale
        for adapter_dir in sorted(self.index_root.iterdir()):
            if (
                adapter_dir.is_symlink()
                or not adapter_dir.is_dir()
                or not _resolved_within(adapter_dir, self.index_root)
            ):
                continue
            for child in sorted(adapter_dir.iterdir()):
                if (
                    child.is_symlink()
                    or not child.is_dir()
                    or not child.name.startswith(".staging-")
                    or not _resolved_within(child, adapter_dir)
                ):
                    continue
                if _staging_owner_alive(child):
                    continue
                stale.append(
                    IndexGcEntry(
                        key=child.name,
                        adapter=adapter_dir.name,
                        path=child,
                        size_bytes=tree_size(child),
                        created_at=None,
                    )
                )
        return stale

    def mark_in_use(self, adapter: str, key: str) -> None:
        """Refresh the in-use lease for one index.

        GC skips leased indexes: an external tool may be reading the index
        right now, and deleting it mid-run fails the pipeline. The lease is a
        dotfile inside the index directory whose mtime is the heartbeat.
        """
        index_dir = self.path_for(adapter, key)
        with suppress(OSError):
            (index_dir / _LEASE_FILE).touch()

    def lease(
        self,
        adapter: str,
        key: str,
        *,
        interval_s: float = _LEASE_REFRESH_S,
    ) -> IndexLease:
        """Return a keep-alive lease for an index used by a running command."""
        return IndexLease(self, adapter, key, interval_s=interval_s)

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
            if _lease_is_fresh(entry.path):
                continue  # a run is using this index right now
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
        # Staging leftovers are garbage independent of the budget policy.
        selected.extend(self._stale_staging_entries())
        if dry_run:
            return tuple(selected)
        removed: list[IndexGcEntry] = []
        for entry in selected:
            if entry.path.is_symlink() or not _resolved_within(entry.path, self.index_root):
                continue
            if entry.key.startswith(".staging-"):
                # Garbage from a crashed build (dead pid): re-check the owner
                # after listing, then drop.
                if _staging_owner_alive(entry.path):
                    continue
                shutil.rmtree(entry.path, ignore_errors=True)
                if not entry.path.exists():
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
                continue
            # Best-effort: never delete an index while another process holds
            # its build lock. Skip it and let a later GC retry.
            try:
                with self.lock(entry.adapter, entry.key, timeout_s=0.1):
                    # A rebuild may have replaced this directory while we were
                    # waiting for the lock; never delete the new generation.
                    if _lease_is_fresh(entry.path) or not _same_entry_identity(entry):
                        continue
                    shutil.rmtree(entry.path, ignore_errors=True)
            except (IndexLockError, CacheError):
                continue
            if entry.path.exists():
                continue
            # The flock lives on the inode; the leftover lock file would
            # otherwise accumulate one entry per GC'd index forever.
            with suppress(OSError):
                (self.adapter_root(entry.adapter) / f"{entry.key}.lock").unlink()
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
    "tree_size",
]
