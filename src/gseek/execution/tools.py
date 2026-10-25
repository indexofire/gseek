"""Binary resolution and version probing."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gseek.errors import ExecutionError, ToolNotFoundError, ToolVersionError

if TYPE_CHECKING:
    from gseek.capabilities import ToolRequirement
    from gseek.model import ToolRef

from .command import CommandPlan
from .executor import Executor

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:[.\-](\d+))?")


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

    def to_tool_ref(self) -> ToolRef:
        from gseek.model import ToolRef

        return ToolRef(
            name=self.name,
            version=self.version or "unknown",
            path=str(self.path) if self.path is not None else None,
            version_raw=self.version_raw,
            role=self.role,
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
    ) -> None:
        self.overrides = {key: Path(value) for key, value in (overrides or {}).items()}
        self.env = dict(os.environ if env is None else env)
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
