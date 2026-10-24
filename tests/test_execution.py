from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gseek.capabilities import ToolRequirement
from gseek.errors import ExecutionError, InputError, ToolNotFoundError, ToolVersionError
from gseek.execution import (
    CommandPlan,
    Executor,
    ToolResolver,
    create_workspace,
    version_at_least,
)


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_run_captures_stdout_and_stderr() -> None:
    executor = Executor()
    result = executor.run(
        CommandPlan(
            argv=_python("import sys; print('out'); print('err', file=sys.stderr)"),
            label="echo",
        )
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.ok


def test_run_raises_on_nonzero_exit() -> None:
    executor = Executor()
    with pytest.raises(ExecutionError) as exc_info:
        executor.run(CommandPlan(argv=_python("import sys; sys.exit(7)"), label="fail"))
    assert exc_info.value.returncode == 7
    assert exc_info.value.code == "EXECUTION_FAILED"


def test_run_enforces_timeout() -> None:
    executor = Executor()
    with pytest.raises(ExecutionError) as exc_info:
        executor.run(
            CommandPlan(
                argv=_python("import time; time.sleep(5)"),
                label="slow",
                timeout_s=0.2,
            )
        )
    assert exc_info.value.timed_out


def test_stream_yields_lines_and_captures_stderr() -> None:
    executor = Executor()
    plan = CommandPlan(
        argv=_python("import sys; [print(i) for i in range(3)]; print('warn', file=sys.stderr)"),
        label="stream",
    )
    with executor.stream(plan) as process:
        lines = list(process.iter_lines())
        result = process.wait()
    assert lines == ["0", "1", "2"]
    assert result.returncode == 0
    assert "warn" in result.stderr


def test_stream_raises_on_nonzero_exit() -> None:
    executor = Executor()
    plan = CommandPlan(argv=_python("import sys; print('x'); sys.exit(3)"), label="stream-fail")
    with pytest.raises(ExecutionError), executor.stream(plan) as process:
        list(process.iter_lines())
        process.wait()


def test_command_plan_validation() -> None:
    with pytest.raises(InputError):
        CommandPlan(argv=())
    with pytest.raises(InputError):
        CommandPlan(argv=("echo",), stdin=Path("x"), stdin_text="y")
    with pytest.raises(InputError):
        CommandPlan(argv=("echo",), timeout_s=0)


def test_tool_resolver_probes_version() -> None:
    resolver = ToolResolver()
    requirement = ToolRequirement(
        binary=sys.executable,
        version_args=("-c", "print('1.2.3')"),
        version_pattern=r"(\d+)\.(\d+)\.(\d+)",
        min_version=(1, 2, 0),
    )
    probe = resolver.probe(requirement)
    assert probe.ok
    assert probe.version == "1.2.3"
    assert probe.path is not None


def test_tool_resolver_rejects_old_version() -> None:
    resolver = ToolResolver()
    requirement = ToolRequirement(
        binary=sys.executable,
        version_args=("-c", "print('0.9.0')"),
        version_pattern=r"(\d+)\.(\d+)\.(\d+)",
        min_version=(1, 0, 0),
    )
    with pytest.raises(ToolVersionError):
        resolver.probe(requirement)


def test_tool_resolver_missing_binary() -> None:
    resolver = ToolResolver()
    with pytest.raises(ToolNotFoundError):
        resolver.resolve("definitely-not-a-real-binary-xyz")


def test_version_at_least() -> None:
    assert version_at_least("2.17.0", (2, 2, 30))
    assert version_at_least("2.30-r1287", (2, 24))
    assert not version_at_least("2.1.0", (2, 2, 30))
    assert version_at_least("1.0", (1, 0, 0))


def test_workspace_cleanup() -> None:
    workspace = create_workspace(prefix="gseek-test-")
    path = workspace.path("nested", "x.txt")
    path.write_text("x", encoding="utf-8")
    assert path.exists()
    workspace.cleanup()
    assert not workspace.root.exists()


def test_workspace_keep(tmp_path: Path) -> None:
    workspace = create_workspace(prefix="gseek-test-", parent=tmp_path, keep=True)
    try:
        assert workspace.root.parent == tmp_path
    finally:
        workspace.cleanup()
    assert workspace.root.exists()
    import shutil

    shutil.rmtree(workspace.root)
