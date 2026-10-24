"""Deterministic external-process execution, tool discovery, and workspaces.

This module is the only place in gseek that starts subprocesses. Adapters plan
commands; :class:`Executor` runs them with absolute paths, no shell, a C
locale, process-group timeouts, and drained stderr pipes.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gseek.errors import ExecutionError, InputError, ToolNotFoundError, ToolVersionError

if TYPE_CHECKING:
    from gseek.capabilities import ToolRequirement
    from gseek.model import ToolRef

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:[.\-](\d+))?")


# ---------------------------------------------------------------------------
# Command plan
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """Run :class:`CommandPlan` objects with consistent safety guarantees."""

    def __init__(
        self,
        *,
        base_env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        deterministic_env: bool = True,
        stderr_limit: int = 64_000,
        tool_paths: Mapping[str, Path | str] | None = None,
    ) -> None:
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.cwd = cwd
        self.deterministic_env = deterministic_env
        self.stderr_limit = stderr_limit
        self.tool_paths: dict[str, Path] = {
            str(name): Path(path) for name, path in (tool_paths or {}).items()
        }

    def with_tool_paths(self, tool_paths: Mapping[str, Path | str]) -> Executor:
        """Return a copy of this executor with resolved tool paths bound.

        Runner binds the paths returned by :class:`ToolResolver` here so that
        ``overrides``/``GSEEK_<BINARY>``/``extra_paths`` affect the actual
        command, not only version probing.
        """
        clone = copy.copy(self)
        clone.tool_paths = {
            **self.tool_paths,
            **{str(name): Path(path) for name, path in tool_paths.items()},
        }
        return clone

    def _resolve_plan(self, plan: CommandPlan) -> CommandPlan:
        if not self.tool_paths:
            return plan
        executable = plan.argv[0]
        resolved = self.tool_paths.get(executable) or self.tool_paths.get(Path(executable).name)
        if resolved is None or str(resolved) == executable:
            return plan
        return replace(plan, argv=(str(resolved), *plan.argv[1:]))

    def _prepare(
        self, plan: CommandPlan
    ) -> tuple[dict[str, str], str | None, Path | None, Path | None]:
        env = build_env(
            base=self.base_env,
            overlay=plan.env,
            deterministic=self.deterministic_env,
        )
        stdin_text: str | None = None
        if plan.stdin is not None:
            try:
                stdin_text = plan.stdin.read_text(encoding="utf-8")
            except OSError as exc:
                raise InputError(f"cannot read stdin file {plan.stdin}: {exc}") from exc
        elif plan.stdin_text is not None:
            stdin_text = plan.stdin_text
        return env, stdin_text, plan.stdout, plan.stderr

    def run(self, plan: CommandPlan) -> ProcessResult:
        """Execute *plan*, capture stdout/stderr, and enforce the timeout."""
        plan = self._resolve_plan(plan)
        env, stdin_text, stdout_path, stderr_path = self._prepare(plan)
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)

        stdout_handle = stdout_path.open("w", encoding="utf-8") if stdout_path is not None else None
        stderr_handle = stderr_path.open("w", encoding="utf-8") if stderr_path is not None else None
        started = time.monotonic()
        timed_out = False
        try:
            process = subprocess.Popen(
                plan.argv,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
                stderr=stderr_handle if stderr_handle is not None else subprocess.PIPE,
                cwd=str(plan.cwd or self.cwd) if (plan.cwd or self.cwd) is not None else None,
                env=env,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self._close_quietly(stdout_handle, stderr_handle)
            raise ToolNotFoundError(
                f"required binary not found: {plan.argv[0]}",
                context={"binary": plan.argv[0]},
            ) from exc
        except OSError as exc:
            self._close_quietly(stdout_handle, stderr_handle)
            raise ExecutionError(
                f"could not start {plan.argv[0]}: {exc}",
                argv=plan.argv,
                context={"binary": plan.argv[0]},
            ) from exc

        try:
            stdout_text, stderr_text = process.communicate(input=stdin_text, timeout=plan.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            try:
                stdout_text, stderr_text = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                stdout_text, stderr_text = "", ""
        finally:
            self._close_quietly(stdout_handle, stderr_handle)

        duration = time.monotonic() - started
        if stdout_handle is not None:
            stdout_text = ""
        if stderr_handle is not None:
            stderr_text = (
                stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path else ""
            )
            stderr_text = stderr_text[-self.stderr_limit :]

        result = ProcessResult(
            plan=plan,
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout_text or "",
            stderr=(stderr_text or "")[-self.stderr_limit :],
            duration_s=duration,
            timed_out=timed_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        if timed_out:
            raise ExecutionError(
                f"command timed out after {plan.timeout_s}s: {plan.argv[0]}",
                argv=plan.argv,
                returncode=result.returncode,
                stderr=result.stderr_tail,
                timed_out=True,
            )
        if result.returncode not in plan.expected_codes:
            raise ExecutionError(
                f"command failed with exit code {result.returncode}: {plan.argv[0]}",
                argv=plan.argv,
                returncode=result.returncode,
                stderr=result.stderr_tail,
            )
        return result

    @contextmanager
    def stream(self, plan: CommandPlan) -> Iterator[StreamedProcess]:
        """Run *plan* and expose stdout as a line iterator.

        stderr is drained by a background thread so the child cannot block on a
        full pipe. A watchdog timer kills the process group on timeout. The
        context manager always reaps the child, including early consumer exit.
        """
        plan = self._resolve_plan(plan)
        env, _stdin_text, _stdout_path, _stderr_path = self._prepare(plan)
        if plan.stdin is not None or plan.stdin_text is not None:
            raise InputError("streaming commands cannot use stdin")
        try:
            process = subprocess.Popen(
                plan.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(plan.cwd or self.cwd) if (plan.cwd or self.cwd) is not None else None,
                env=env,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ToolNotFoundError(
                f"required binary not found: {plan.argv[0]}",
                context={"binary": plan.argv[0]},
            ) from exc
        except OSError as exc:
            raise ExecutionError(
                f"could not start {plan.argv[0]}: {exc}",
                argv=plan.argv,
                context={"binary": plan.argv[0]},
            ) from exc

        state = StreamedProcess(
            plan=plan,
            process=process,
            started=time.monotonic(),
            stderr_limit=self.stderr_limit,
        )
        if plan.stderr is not None:
            plan.stderr.parent.mkdir(parents=True, exist_ok=True)
        state.start()
        try:
            yield state
        finally:
            state.close()

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            with suppress(ProcessLookupError):  # pragma: no cover - already gone
                process.kill()

    @staticmethod
    def _close_quietly(*handles: Any) -> None:
        for handle in handles:
            if handle is not None:
                with suppress(OSError):  # pragma: no cover - defensive
                    handle.close()


class StreamedProcess:
    """A running command whose stdout is consumed incrementally."""

    def __init__(
        self,
        *,
        plan: CommandPlan,
        process: subprocess.Popen[str],
        started: float,
        stderr_limit: int = 64_000,
    ) -> None:
        self.plan = plan
        self.process = process
        self.started = started
        self.stderr_limit = stderr_limit
        self._stderr_chunks: deque[str] = deque()
        self._stderr_size = 0
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._timer: threading.Timer | None = None
        self._timed_out = False
        self._closed = False
        self.returncode: int | None = None
        self.stderr = ""

    def start(self) -> None:
        self._stderr_thread.start()
        if self.plan.timeout_s is not None:

            def on_timeout() -> None:
                self._timed_out = True
                Executor._kill(self.process)

            self._timer = threading.Timer(self.plan.timeout_s, on_timeout)
            self._timer.daemon = True
            self._timer.start()

    def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        try:
            for line in self.process.stderr:
                self._stderr_chunks.append(line)
                self._stderr_size += len(line)
                while self._stderr_size > self.stderr_limit and self._stderr_chunks:
                    removed = self._stderr_chunks.popleft()
                    self._stderr_size -= len(removed)
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass

    def iter_lines(self) -> Iterator[str]:
        """Yield stdout lines (without trailing newline)."""
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            yield line.rstrip("\n")

    def wait(self) -> ProcessResult:
        """Reap the process and return its result, raising on failure."""
        if not self._closed:
            self.close()
        duration = time.monotonic() - self.started
        result = ProcessResult(
            plan=self.plan,
            returncode=self.returncode if self.returncode is not None else -1,
            stdout="",
            stderr=self.stderr,
            duration_s=duration,
            timed_out=self._timed_out,
            stderr_path=self.plan.stderr,
        )
        if self._timed_out:
            raise ExecutionError(
                f"command timed out after {self.plan.timeout_s}s: {self.plan.argv[0]}",
                argv=self.plan.argv,
                returncode=result.returncode,
                stderr=result.stderr_tail,
                timed_out=True,
            )
        if result.returncode not in self.plan.expected_codes:
            raise ExecutionError(
                f"command failed with exit code {result.returncode}: {self.plan.argv[0]}",
                argv=self.plan.argv,
                returncode=result.returncode,
                stderr=result.stderr_tail,
            )
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        if self.process.stdout is not None:
            with suppress(OSError):  # pragma: no cover - defensive
                self.process.stdout.close()
        if self.process.poll() is None:
            Executor._kill(self.process)
        self.returncode = self.process.wait()
        self._stderr_thread.join(timeout=5)
        self.stderr = "".join(self._stderr_chunks)
        if self.plan.stderr is not None:
            self.plan.stderr.parent.mkdir(parents=True, exist_ok=True)
            self.plan.stderr.write_text(self.stderr, encoding="utf-8")

    def __enter__(self) -> StreamedProcess:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Tool discovery and version probing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


@dataclass
class RunWorkspace:
    """A temporary directory for one run's intermediate files."""

    root: Path
    keep: bool = False

    def path(self, *parts: str) -> Path:
        target = self.root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def cleanup(self) -> None:
        if self.keep:
            return
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> RunWorkspace:
        return self

    def __exit__(self, *args: object) -> None:
        self.cleanup()


def create_workspace(
    *,
    prefix: str = "gseek-",
    parent: Path | None = None,
    keep: bool = False,
) -> RunWorkspace:
    """Create a workspace under ``GSEEK_TMPDIR``, *parent*, or the system temp."""
    root_parent = parent
    if root_parent is None:
        configured = os.environ.get("GSEEK_TMPDIR")
        root_parent = Path(configured) if configured else None
    if root_parent is not None:
        root_parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root_parent) if root_parent else None))
    return RunWorkspace(root=root, keep=keep)


__all__ = [
    "CommandPlan",
    "Executor",
    "ProcessResult",
    "RunWorkspace",
    "StreamedProcess",
    "ToolProbe",
    "ToolResolver",
    "build_env",
    "create_workspace",
    "env_for_manifest",
    "version_at_least",
]
