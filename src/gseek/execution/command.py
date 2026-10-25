"""Command plans, process results, and deterministic environments."""

from __future__ import annotations

import io
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from gseek.errors import InputError


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """A fully specified external command (never interpreted by a shell)."""

    argv: tuple[str, ...]
    label: str = ""
    stdin: Path | None = None
    stdin_text: str | None = None
    stdout: Path | None = None
    stderr: Path | None = None
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float | None = None
    expected_codes: frozenset[int] = frozenset({0})

    def __post_init__(self) -> None:
        if not self.argv:
            raise InputError("command argv must not be empty")
        if not all(isinstance(part, str) for part in self.argv):
            raise InputError("command argv must contain only strings")
        if any("\x00" in part for part in self.argv):
            raise InputError("command argv must not contain NUL bytes")
        if self.stdin is not None and self.stdin_text is not None:
            raise InputError("stdin and stdin_text are mutually exclusive")
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise InputError(f"timeout_s must be positive, got {self.timeout_s}")

    def render(self) -> str:
        return " ".join(self.argv)

    def with_env(self, **values: str) -> CommandPlan:
        merged = dict(self.env)
        merged.update(values)
        return CommandPlan(
            argv=self.argv,
            label=self.label,
            stdin=self.stdin,
            stdin_text=self.stdin_text,
            stdout=self.stdout,
            stderr=self.stderr,
            cwd=self.cwd,
            env=merged,
            timeout_s=self.timeout_s,
            expected_codes=self.expected_codes,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """The result of one command execution."""

    plan: CommandPlan
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    stdout_path: Path | None = None
    stderr_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.returncode in self.plan.expected_codes and not self.timed_out

    @property
    def stderr_tail(self) -> str:
        return self.stderr[-8192:]


# Variables that can influence dynamic linking/tool discovery. They are kept
# in the manifest allowlist; secrets are not.
_ENV_ALLOWLIST = (
    "PATH",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "CONDA_PREFIX",
    "VIRTUAL_ENV",
    "TMPDIR",
    "GSEEK_CACHE_DIR",
    "GSEEK_TMPDIR",
    "GSEEK_PATH_PREPEND",
)


def _read_tail(path: Path, limit: int) -> str:
    """Read at most *limit* bytes from the end of *path*.

    Stderr files can grow to gigabytes; reading them whole before truncating
    would risk OOM for a diagnostic tail.
    """
    with path.open("rb") as handle:
        size = handle.seek(0, io.SEEK_END)
        handle.seek(max(0, size - limit))
        data = handle.read(limit)
    return data.decode("utf-8", errors="replace")


def build_env(
    *,
    base: Mapping[str, str] | None = None,
    overlay: Mapping[str, str] | None = None,
    deterministic: bool = True,
) -> dict[str, str]:
    """Build a child environment with optional deterministic locale/timezone."""
    env = dict(os.environ if base is None else base)
    if deterministic:
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["TZ"] = "UTC"
    if overlay:
        env.update(overlay)
    return env


def env_for_manifest(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the non-secret, reproducible subset of an environment."""
    source = os.environ if env is None else env
    return {key: source[key] for key in _ENV_ALLOWLIST if key in source}
