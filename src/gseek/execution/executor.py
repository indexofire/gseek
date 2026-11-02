"""The only place in gseek that starts subprocesses."""

from __future__ import annotations

import copy
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from gseek.errors import ExecutionError, InputError, ToolNotFoundError

from .command import CommandPlan, ProcessResult, _read_tail, build_env

_CAPTURE_CHUNK = 8192
_JOIN_TIMEOUT_S = 5.0


class _BoundedCapture:
    """Drain stdout/stderr pipes without allowing unbounded memory growth."""

    def __init__(
        self, process: subprocess.Popen[str], stdout_limit: int, stderr_limit: int
    ) -> None:
        self.process = process
        self.stdout_limit = max(0, stdout_limit)
        self.stderr_limit = max(0, stderr_limit)
        self.stdout_parts: list[str] = []
        self.stdout_size = 0
        self.stdout_truncated = False
        self.stderr_parts: deque[str] = deque()
        self.stderr_size = 0
        self.stderr_truncated = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self.process.stdout is not None:
            thread = threading.Thread(target=self._read_stdout, daemon=True)
            thread.start()
            self._threads.append(thread)
        if self.process.stderr is not None:
            thread = threading.Thread(target=self._read_stderr, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while chunk := self.process.stdout.read(_CAPTURE_CHUNK):
                remaining = self.stdout_limit - self.stdout_size
                if remaining > 0:
                    self.stdout_parts.append(chunk[:remaining])
                    self.stdout_size += min(len(chunk), remaining)
                if len(chunk) > max(0, remaining):
                    self.stdout_truncated = True
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            while chunk := self.process.stderr.read(_CAPTURE_CHUNK):
                self.stderr_parts.append(chunk)
                self.stderr_size += len(chunk)
                while self.stderr_size > self.stderr_limit and self.stderr_parts:
                    removed = self.stderr_parts.popleft()
                    self.stderr_size -= len(removed)
                    self.stderr_truncated = True
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=_JOIN_TIMEOUT_S)
        self._close_stream(self.process.stdout)
        self._close_stream(self.process.stderr)

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is not None:
            with suppress(OSError):
                stream.close()

    @property
    def stdout(self) -> str:
        return "".join(self.stdout_parts)

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_parts)


class _StdinWriter:
    """Feed ``stdin_text`` without blocking the main execution path."""

    def __init__(self, stream: Any, text: str) -> None:
        self.stream = stream
        self.text = text
        self._thread = threading.Thread(target=self._write, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _write(self) -> None:
        try:
            self.stream.write(self.text)
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            with suppress(OSError):
                self.stream.close()

    def join(self) -> None:
        self._thread.join(timeout=_JOIN_TIMEOUT_S)


class Executor:
    """Run :class:`CommandPlan` objects with consistent safety guarantees."""

    def __init__(
        self,
        *,
        base_env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        deterministic_env: bool = True,
        stderr_limit: int = 64_000,
        stdout_limit: int = 1_000_000,
        stdout_line_limit: int = 8_000_000,
        tool_paths: Mapping[str, Path | str] | None = None,
    ) -> None:
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.cwd = cwd
        self.deterministic_env = deterministic_env
        self.stderr_limit = stderr_limit
        self.stdout_limit = stdout_limit
        self.stdout_line_limit = stdout_line_limit
        self.tool_paths: dict[str, Path] = {
            str(name): Path(path) for name, path in (tool_paths or {}).items()
        }

    def with_tool_paths(self, tool_paths: Mapping[str, Path | str]) -> Executor:
        """Return a copy of this executor with resolved tool paths bound.

        Runner binds the paths returned by :class:`ToolResolver` here so that
        ``overrides``/``GSEEK_<BINARY>``/``extra_paths`` affect the actual
        command, not only version probing. Paths already bound on this
        executor (explicit user configuration) take precedence: they are the
        strongest intent and must not be shadowed by PATH discoveries.
        """
        clone = copy.copy(self)
        clone.tool_paths = {
            **{str(name): Path(path) for name, path in tool_paths.items()},
            **self.tool_paths,
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
    ) -> tuple[dict[str, str], Path | None, Path | None, Path | None]:
        env = build_env(
            base=self.base_env,
            overlay=plan.env,
            deterministic=self.deterministic_env,
        )
        # Do not slurp plan.stdin here: a large file would defeat the bounded
        # capture work below. It is opened lazily in run().
        return env, plan.stdin, plan.stdout, plan.stderr

    def _start_capture(self, process: subprocess.Popen[str]) -> _BoundedCapture:
        capture = _BoundedCapture(process, self.stdout_limit, self.stderr_limit)
        capture.start()
        return capture

    def run(self, plan: CommandPlan) -> ProcessResult:
        """Execute *plan*, capture stdout/stderr, and enforce the timeout."""
        plan = self._resolve_plan(plan)
        env, stdin_path, stdout_path, stderr_path = self._prepare(plan)
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)

        stdin_handle = None
        if stdin_path is not None:
            if not stdin_path.is_file() or stdin_path.is_symlink():
                raise InputError(f"stdin must be a regular file: {stdin_path}")
            try:
                stdin_handle = stdin_path.open("r", encoding="utf-8", newline="")
            except OSError as exc:
                raise InputError(f"cannot read stdin file {stdin_path}: {exc}") from exc
        stdout_handle = None
        stderr_handle = None
        try:
            stdout_handle = (
                stdout_path.open("w", encoding="utf-8") if stdout_path is not None else None
            )
            stderr_handle = (
                stderr_path.open("w", encoding="utf-8") if stderr_path is not None else None
            )
        except OSError as exc:
            self._close_quietly(stdin_handle, stdout_handle)
            raise InputError(f"cannot open command output: {exc}") from exc
        started = time.monotonic()
        timed_out = False
        capture: _BoundedCapture | None = None
        stdin_writer: _StdinWriter | None = None
        try:
            process = subprocess.Popen(
                plan.argv,
                stdin=(
                    stdin_handle
                    if stdin_handle is not None
                    else subprocess.PIPE
                    if plan.stdin_text is not None
                    else subprocess.DEVNULL
                ),
                stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
                stderr=stderr_handle if stderr_handle is not None else subprocess.PIPE,
                cwd=str(plan.cwd or self.cwd) if (plan.cwd or self.cwd) is not None else None,
                env=env,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self._close_quietly(stdin_handle, stdout_handle, stderr_handle)
            raise ToolNotFoundError(
                f"required binary not found: {plan.argv[0]}",
                context={"binary": plan.argv[0]},
            ) from exc
        except OSError as exc:
            self._close_quietly(stdin_handle, stdout_handle, stderr_handle)
            raise ExecutionError(
                f"could not start {plan.argv[0]}: {exc}",
                argv=plan.argv,
                context={"binary": plan.argv[0]},
            ) from exc

        try:
            if plan.stdin_text is not None and process.stdin is not None:
                stdin_writer = _StdinWriter(process.stdin, plan.stdin_text)
                stdin_writer.start()
            capture = self._start_capture(process)
            process.wait(timeout=plan.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            with suppress(subprocess.TimeoutExpired):  # pragma: no cover - defensive
                process.wait(timeout=5)
        except BaseException:
            # Any other failure (capture thread error, KeyboardInterrupt, ...)
            # must still reap the child: start_new_session would orphan it and
            # the unread pipes would keep it blocked forever.
            self._kill(process)
            with suppress(subprocess.TimeoutExpired):  # pragma: no cover - defensive
                process.wait(timeout=5)
            raise
        finally:
            if stdin_writer is not None:
                stdin_writer.join()
                self._close_quietly(process.stdin)
            if capture is not None:
                capture.join()
            self._close_quietly(stdin_handle, stdout_handle, stderr_handle)

        duration = time.monotonic() - started
        stdout_text = capture.stdout if capture is not None else ""
        stderr_text = capture.stderr if capture is not None else ""
        stdout_truncated = capture.stdout_truncated if capture is not None else False
        stderr_truncated = capture.stderr_truncated if capture is not None else False
        if stdout_handle is not None:
            stdout_text = ""
            stdout_truncated = False
        if stderr_handle is not None:
            stderr_text = _read_tail(stderr_path, self.stderr_limit) if stderr_path else ""
            stderr_truncated = bool(
                stderr_path
                and stderr_path.exists()
                and stderr_path.stat().st_size > self.stderr_limit
            )

        result = ProcessResult(
            plan=plan,
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout_text or "",
            stderr=(stderr_text or "")[-self.stderr_limit :],
            duration_s=duration,
            timed_out=timed_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
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
        if plan.stdin is not None or plan.stdin_text is not None:
            # Reject before _prepare() would slurp the whole stdin file.
            raise InputError("streaming commands cannot use stdin")
        env, _stdin_text, _stdout_path, _stderr_path = self._prepare(plan)
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
                encoding="utf-8",
                errors="replace",
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
            stdout_line_limit=self.stdout_line_limit,
        )
        try:
            if plan.stderr is not None:
                plan.stderr.parent.mkdir(parents=True, exist_ok=True)
            state.start()
            yield state
        finally:
            state.close()

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # Cleanup must never mask the failure that triggered it.
            with suppress(ProcessLookupError, PermissionError, OSError):  # pragma: no cover
                process.kill()

    @staticmethod
    def _close_quietly(*handles: Any) -> None:
        for handle in handles:
            if handle is not None:
                with suppress(OSError):  # pragma: no cover - defensive
                    handle.close()


# Grace period between stdout EOF and SIGKILL: a tool may close its output
# pipe while still finalizing side files.
_CLOSE_GRACE_S = 10.0


class StreamedProcess:
    """A running command whose stdout is consumed incrementally."""

    def __init__(
        self,
        *,
        plan: CommandPlan,
        process: subprocess.Popen[str],
        started: float,
        stderr_limit: int = 64_000,
        stdout_line_limit: int = 8_000_000,
    ) -> None:
        self.plan = plan
        self.process = process
        self.started = started
        self.stderr_limit = stderr_limit
        self.stdout_line_limit = stdout_line_limit
        self._stderr_chunks: deque[str] = deque()
        self._stderr_size = 0
        self.stderr_truncated = False
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._timer: threading.Timer | None = None
        self._timed_out = False
        self._closed = False
        self._started = False
        self.returncode: int | None = None
        self.stderr = ""

    def start(self) -> None:
        self._started = True
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
            while chunk := self.process.stderr.read(_CAPTURE_CHUNK):
                self._stderr_chunks.append(chunk)
                self._stderr_size += len(chunk)
                while self._stderr_size > self.stderr_limit and self._stderr_chunks:
                    removed = self._stderr_chunks.popleft()
                    self._stderr_size -= len(removed)
                    self.stderr_truncated = True
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass

    def iter_lines(self) -> Iterator[str]:
        """Yield stdout lines (without trailing newline)."""
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            if len(line) > self.stdout_line_limit:
                raise ExecutionError(
                    f"stdout line exceeds {self.stdout_line_limit} characters: {self.plan.argv[0]}",
                    argv=self.plan.argv,
                )
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
            stderr_truncated=self.stderr_truncated,
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
            # Stdout reached EOF but the child may still be finalizing (aux
            # files, fsync). Give it a grace period before killing so a clean
            # run is not reported as exit code -9. A pre-start failure has no
            # consumer at all: kill immediately.
            if self._started:
                try:
                    self.process.wait(timeout=_CLOSE_GRACE_S)
                except subprocess.TimeoutExpired:
                    Executor._kill(self.process)
            else:
                Executor._kill(self.process)
        self.returncode = self.process.wait()
        # The drain thread may never have started when close() runs from a
        # pre-start failure path; joining an unstarted thread would raise.
        if self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=5)
        self.stderr = "".join(self._stderr_chunks)
        if self.plan.stderr is not None:
            # Diagnostics only: an unwritable stderr path must not mask the
            # original failure (the in-memory copy is still returned).
            with suppress(OSError):
                self.plan.stderr.parent.mkdir(parents=True, exist_ok=True)
                self.plan.stderr.write_text(self.stderr, encoding="utf-8")

    def __enter__(self) -> StreamedProcess:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
