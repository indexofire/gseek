"""Binary resolution and version probing."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gseek.errors import ExecutionError, InputError, ToolNotFoundError, ToolVersionError
from gseek.io import atomic_write_text
from gseek.provenance import sha256_file

if TYPE_CHECKING:
    from gseek.capabilities import ToolRequirement
    from gseek.model import ToolRef

from .command import CommandPlan
from .executor import Executor

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:[.\-](\d+))?")
_HASH_ENV = "GSEEK_TOOL_HASH"


def _hash_tools_from_env() -> bool:
    return os.environ.get(_HASH_ENV, "").strip().lower() in {"1", "true", "yes", "on", "full"}


def _default_hash_cache_path() -> Path:
    from gseek.index import default_cache_root

    return default_cache_root() / "tool-hashes.json"


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


@dataclass(frozen=True, slots=True)
class ToolProbe:
    """The outcome of resolving and version-checking one tool."""

    name: str
    path: Path | None
    version: str
    version_raw: str = ""
    ok: bool = True
    error: str = ""
    role: str = "aligner"
    sha256: str | None = None

    def to_tool_ref(self) -> ToolRef:
        from gseek.model import ToolRef

        return ToolRef(
            name=self.name,
            version=self.version or "unknown",
            path=str(self.path) if self.path is not None else None,
            version_raw=self.version_raw,
            role=self.role,
            sha256=self.sha256,
        )


class ToolResolver:
    """Resolve binaries and probe versions, with optional explicit overrides."""

    def __init__(
        self,
        *,
        overrides: Mapping[str, Path | str] | None = None,
        extra_paths: Sequence[Path | str] = (),
        env: Mapping[str, str] | None = None,
        executor: Executor | None = None,
        hash_tools: bool | None = None,
        hash_cache_path: Path | None = None,
    ) -> None:
        self.overrides = {key: Path(value) for key, value in (overrides or {}).items()}
        self.env = dict(os.environ if env is None else env)
        self.hash_tools = _hash_tools_from_env() if hash_tools is None else hash_tools
        self.hash_cache_path = (
            (hash_cache_path or _default_hash_cache_path()) if self.hash_tools else None
        )
        self._hash_cache: dict[str, dict[str, int | str]] | None = None
        path_parts = [str(Path(part)) for part in extra_paths if str(part)]
        prepend = self.env.get("GSEEK_PATH_PREPEND", "")
        if prepend:
            path_parts.extend(part for part in prepend.split(os.pathsep) if part)
        base_path = self.env.get("PATH", "")
        if base_path:
            path_parts.append(base_path)
        self.search_path = os.pathsep.join(path_parts)
        self.executor = executor or Executor(base_env=self.env)
        self._cache: dict[tuple[object, ...], ToolProbe] = {}

    def resolve(self, binary: str) -> Path:
        """Return an absolute binary path or raise :class:`ToolNotFoundError`."""
        explicit = self.overrides.get(binary)
        if explicit is not None:
            if not explicit.is_file():
                raise ToolNotFoundError(
                    f"configured binary does not exist: {explicit}",
                    context={"binary": binary, "path": str(explicit)},
                )
            return explicit.resolve()

        env_override = self.env.get(f"GSEEK_{binary.upper().replace('-', '_')}")
        if env_override:
            path = Path(env_override)
            if not path.is_file():
                raise ToolNotFoundError(
                    f"GSEEK_{binary.upper().replace('-', '_')} points to a missing file: {path}",
                    context={"binary": binary, "path": str(path)},
                )
            return path.resolve()

        found = shutil.which(binary, path=self.search_path)
        if not found:
            raise ToolNotFoundError(
                f"required binary not found on PATH: {binary}",
                hint=(
                    "install it via pixi/conda (e.g. `pixi add blast minimap2 mummer4 kma bwa mash`) "
                    f"or set GSEEK_{binary.upper().replace('-', '_')}"
                ),
                context={"binary": binary},
            )
        return Path(found).resolve()

    def which(self, binary: str) -> Path | None:
        try:
            return self.resolve(binary)
        except ToolNotFoundError:
            return None

    def _load_hash_cache(self) -> dict[str, dict[str, int | str]]:
        if self._hash_cache is not None:
            return self._hash_cache
        self._hash_cache = {}
        if self.hash_cache_path is None:
            return self._hash_cache
        try:
            data = json.loads(self.hash_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._hash_cache
        binaries = data.get("binaries") if isinstance(data, dict) else None
        if not isinstance(binaries, dict):
            return self._hash_cache
        for path, record in binaries.items():
            if not isinstance(path, str) or not isinstance(record, dict):
                continue
            size = record.get("size")
            mtime_ns = record.get("mtime_ns")
            inode = record.get("inode")
            digest = record.get("sha256")
            if (
                isinstance(size, int)
                and not isinstance(size, bool)
                and isinstance(mtime_ns, int)
                and not isinstance(mtime_ns, bool)
                and isinstance(inode, int)
                and not isinstance(inode, bool)
                and isinstance(digest, str)
                and _is_sha256(digest)
            ):
                self._hash_cache[path] = {
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "inode": inode,
                    "sha256": digest,
                }
        return self._hash_cache

    def _save_hash_cache(self) -> None:
        if self.hash_cache_path is None or self._hash_cache is None:
            return
        payload = {"schema": "gseek.tool-hashes/1", "binaries": self._hash_cache}
        try:
            self.hash_cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_text(
                self.hash_cache_path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                mode=0o600,
            )
        except OSError:  # pragma: no cover - read-only cache
            pass

    def _binary_sha256(self, path: Path) -> str | None:
        if not self.hash_tools:
            return None
        try:
            stat_result = path.stat()
        except OSError:
            return None
        resolved = path.resolve()
        key = str(resolved)
        cache = self._load_hash_cache()
        record = cache.get(key)
        if record is not None:
            cached_digest = record.get("sha256")
            if (
                record.get("size") == stat_result.st_size
                and record.get("mtime_ns") == stat_result.st_mtime_ns
                and record.get("inode") == stat_result.st_ino
                and isinstance(cached_digest, str)
                and _is_sha256(cached_digest)
            ):
                return cached_digest
        try:
            digest = sha256_file(resolved)
        except (InputError, OSError, ValueError):
            return None
        cache[key] = {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "inode": stat_result.st_ino,
            "sha256": digest,
        }
        self._save_hash_cache()
        return digest

    def probe(self, requirement: ToolRequirement, *, strict: bool = True) -> ToolProbe:
        """Resolve *requirement* and parse its version output."""
        try:
            path = self.resolve(requirement.binary)
        except ToolNotFoundError as exc:
            key = self._probe_key(requirement, None)
            cached = self._cache.get(key)
            if cached is not None:
                if strict and not requirement.optional:
                    raise ToolNotFoundError(
                        cached.error,
                        hint=requirement.hint,
                        context={"binary": requirement.binary},
                    ) from None
                return cached
            probe = ToolProbe(
                name=requirement.binary,
                path=None,
                version="",
                ok=False,
                error=exc.message,
                role=requirement.role,
            )
            self._cache[key] = probe
            if strict and not requirement.optional:
                raise ToolNotFoundError(
                    probe.error,
                    hint=requirement.hint,
                    context={"binary": requirement.binary},
                ) from exc
            return probe

        key = self._probe_key(requirement, path)
        cached = self._cache.get(key)
        if cached is not None:
            if not cached.ok and strict and not requirement.optional:
                raise ToolVersionError(
                    cached.error,
                    hint=requirement.hint,
                    context={"binary": requirement.binary, "path": str(path)},
                )
            return cached

        sha256 = self._binary_sha256(path)
        plan = CommandPlan(
            argv=(str(path), *requirement.version_args),
            label=f"{requirement.binary} --version",
            expected_codes=frozenset(range(0, 256)),
            timeout_s=30.0,
        )
        try:
            result = self.executor.run(plan)
        except ExecutionError as exc:
            probe = ToolProbe(
                name=requirement.binary,
                path=path,
                version="",
                ok=False,
                error=str(exc),
                role=requirement.role,
                sha256=sha256,
            )
            self._cache[key] = probe
            if strict and not requirement.optional:
                raise ToolVersionError(
                    f"could not determine version of {requirement.binary}: {exc}",
                    hint=requirement.hint,
                    context={"binary": requirement.binary, "path": str(path)},
                ) from exc
            return probe

        raw = (result.stdout + "\n" + result.stderr).strip()
        version = _parse_version(raw, requirement.version_pattern)
        if version is None:
            probe = ToolProbe(
                name=requirement.binary,
                path=path,
                version="",
                version_raw=raw[:500],
                ok=False,
                error=f"could not parse version from output: {raw[:200]!r}",
                role=requirement.role,
                sha256=sha256,
            )
            self._cache[key] = probe
            if strict and not requirement.optional:
                raise ToolVersionError(
                    f"could not parse {requirement.binary} version from output: {raw[:200]!r}",
                    hint=requirement.hint,
                    context={"binary": requirement.binary, "path": str(path)},
                )
            return probe

        if requirement.min_version is not None and not version_at_least(
            version, requirement.min_version
        ):
            probe = ToolProbe(
                name=requirement.binary,
                path=path,
                version=version,
                version_raw=raw[:500],
                ok=False,
                error=(
                    f"{requirement.binary} {version} is older than "
                    f"{'.'.join(str(part) for part in requirement.min_version)}"
                ),
                role=requirement.role,
                sha256=sha256,
            )
            self._cache[key] = probe
            if strict and not requirement.optional:
                raise ToolVersionError(
                    probe.error,
                    hint=requirement.hint,
                    context={"binary": requirement.binary, "path": str(path)},
                )
            return probe

        probe = ToolProbe(
            name=requirement.binary,
            path=path,
            version=version,
            version_raw=raw[:500],
            role=requirement.role,
            sha256=sha256,
        )
        self._cache[key] = probe
        return probe

    @staticmethod
    def _probe_key(
        requirement: ToolRequirement, path: Path | None
    ) -> tuple[str, str, tuple[str, ...], str, tuple[int, ...], str]:
        return (
            requirement.binary,
            str(path) if path is not None else "",
            tuple(requirement.version_args),
            requirement.version_pattern or "",
            tuple(requirement.min_version or ()),
            requirement.role,
        )


def _parse_version(raw: str, pattern: str | None) -> str | None:
    if pattern:
        match = re.search(pattern, raw)
        if match is None:
            return None
        groups = [group for group in match.groups() if group is not None]
        return ".".join(groups) if groups else match.group(0)
    match = _VERSION_RE.search(raw)
    if match is None:
        return None
    return ".".join(part for part in match.groups() if part is not None)


def version_at_least(current: str, minimum: tuple[int, ...]) -> bool:
    """Compare dotted versions, padding the shorter tuple with zeros."""
    current_parts = tuple(int(part) for part in re.findall(r"\d+", current))
    width = max(len(current_parts), len(minimum))
    padded_current = current_parts + (0,) * (width - len(current_parts))
    padded_minimum = minimum + (0,) * (width - len(minimum))
    return padded_current >= padded_minimum
