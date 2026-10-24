from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gseek.capabilities import ToolRequirement
from gseek.errors import ExecutionError, ToolNotFoundError, ToolVersionError
from gseek.execution import (
    CommandPlan,
    Executor,
    ToolResolver,
    build_env,
    create_workspace,
    env_for_manifest,
)


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _fake_tool(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'fake-tool {version}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_probe_cache_respects_min_version(tmp_path: Path) -> None:
    tool = _fake_tool(tmp_path / "fake-tool", "1.0.0")
    resolver = ToolResolver(overrides={"fake-tool": tool})
    low = ToolRequirement(
        "fake-tool",
        version_args=("--version",),
        version_pattern=r"fake-tool\s+(\d+)\.(\d+)\.(\d+)",
        min_version=(0, 5, 0),
    )
    high = ToolRequirement(
        "fake-tool",
        version_args=("--version",),
        version_pattern=r"fake-tool\s+(\d+)\.(\d+)\.(\d+)",
        min_version=(2, 0, 0),
    )
    assert resolver.probe(low, strict=False).ok is True
    assert resolver.probe(high, strict=False).ok is False
    with pytest.raises(ToolVersionError):
        resolver.probe(high, strict=True)


def test_probe_cache_respects_resolved_path(tmp_path: Path) -> None:
    first = _fake_tool(tmp_path / "one" / "fake-tool", "1.0.0")
    second = _fake_tool(tmp_path / "two" / "fake-tool", "2.0.0")
    resolver = ToolResolver(overrides={"fake-tool": first})
    requirement = ToolRequirement(
        "fake-tool",
        version_args=("--version",),
        version_pattern=r"fake-tool\s+(\d+)\.(\d+)\.(\d+)",
    )
    assert resolver.probe(requirement, strict=False).version == "1.0.0"
    resolver.overrides["fake-tool"] = second
    assert resolver.probe(requirement, strict=False).version == "2.0.0"


def test_probe_strict_reraises_cached_missing_tool() -> None:
    requirement = ToolRequirement("definitely-not-a-real-binary-xyz")
    resolver = ToolResolver()
    assert resolver.probe(requirement, strict=False).ok is False
    with pytest.raises(ToolNotFoundError):
        resolver.probe(requirement, strict=True)


def test_probe_optional_outdated_does_not_raise(tmp_path: Path) -> None:
    tool = _fake_tool(tmp_path / "fake-tool", "1.0.0")
    resolver = ToolResolver(overrides={"fake-tool": tool})
    requirement = ToolRequirement(
        "fake-tool",
        version_args=("--version",),
        version_pattern=r"fake-tool\s+(\d+)\.(\d+)\.(\d+)",
        min_version=(2, 0, 0),
        optional=True,
    )
    probe = resolver.probe(requirement, strict=True)
    assert probe.ok is False


def test_stream_caps_stderr_memory(tmp_path: Path) -> None:
    executor = Executor(stderr_limit=100)
    plan = CommandPlan(argv=_python("import sys; sys.stderr.write('x' * 10000)"))
    with executor.stream(plan) as process:
        list(process.iter_lines())
        process.wait()
    assert len(process.stderr) <= 100


def test_executor_binds_tool_paths(tmp_path: Path) -> None:
    tool = tmp_path / "fake-tool"
    tool.write_text("#!/bin/sh\necho bound\n", encoding="utf-8")
    tool.chmod(0o755)

    executor = Executor(tool_paths={"fake-tool": tool})
    result = executor.run(CommandPlan(argv=("fake-tool",)))
    assert result.stdout.strip() == "bound"
    assert result.plan.argv[0] == str(tool)


def test_run_redirects_stdout_and_stderr(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    err = tmp_path / "err.txt"
    result = Executor().run(
        CommandPlan(
            argv=_python("import sys; print('out'); print('err', file=sys.stderr)"),
            stdout=out,
            stderr=err,
        )
    )
    assert result.stdout == ""
    assert result.stdout_path == out
    assert out.read_text(encoding="utf-8").strip() == "out"
    assert err.read_text(encoding="utf-8").strip() == "err"
    assert result.stderr_path == err


def test_run_passes_stdin_text_and_file(tmp_path: Path) -> None:
    code = "import sys; print(sys.stdin.read().strip())"
    text_result = Executor().run(CommandPlan(argv=_python(code), stdin_text="hello"))
    assert text_result.stdout.strip() == "hello"

    stdin_path = tmp_path / "stdin.txt"
    stdin_path.write_text("file-input\n", encoding="utf-8")
    file_result = Executor().run(CommandPlan(argv=_python(code), stdin=stdin_path))
    assert file_result.stdout.strip() == "file-input"


def test_run_missing_binary() -> None:
    with pytest.raises(ToolNotFoundError):
        Executor().run(CommandPlan(argv=("definitely-not-a-real-binary-xyz",)))


def test_stream_timeout() -> None:
    plan = CommandPlan(
        argv=_python("import time; time.sleep(5)"),
        timeout_s=0.2,
    )
    with pytest.raises(ExecutionError), Executor().stream(plan) as process:
        list(process.iter_lines())
        process.wait()


def test_command_plan_with_env_and_render() -> None:
    command = CommandPlan(argv=("echo", "hi"), label="echo").with_env(A="B")
    assert command.env == {"A": "B"}
    assert command.render() == "echo hi"


def test_build_env_and_manifest_subset(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("SECRET", "do-not-record")
    env = build_env(base={"PATH": "/bin"}, deterministic=True)
    assert env["LC_ALL"] == "C"
    assert env["TZ"] == "UTC"
    subset = env_for_manifest(env)
    assert subset["PATH"] == "/bin"
    assert "SECRET" not in subset


def test_resolver_override_and_which(tmp_path: Path) -> None:
    resolver = ToolResolver(overrides={"python": sys.executable})
    assert resolver.resolve("python") == Path(sys.executable).resolve()
    assert resolver.which("python") is not None
    assert resolver.which("definitely-not-a-real-binary-xyz") is None

    with pytest.raises(ToolNotFoundError):
        ToolResolver(overrides={"bad": tmp_path / "missing"}).resolve("bad")


def test_resolver_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GSEEK_PYTHON", sys.executable)
    resolver = ToolResolver()
    assert resolver.resolve("python") == Path(sys.executable).resolve()


def test_resolver_probe_optional_missing() -> None:
    probe = ToolResolver().probe(
        ToolRequirement("definitely-not-a-real-binary-xyz", optional=True),
        strict=False,
    )
    assert probe.ok is False


def test_resolver_probe_parse_failure() -> None:
    requirement = ToolRequirement(
        binary=sys.executable,
        version_args=("-c", "print('no version here')"),
        version_pattern=r"(\d+)\.(\d+)\.(\d+)",
    )
    with pytest.raises(ToolVersionError):
        ToolResolver().probe(requirement)


def test_workspace_respects_tmpdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GSEEK_TMPDIR", str(tmp_path / "tmp"))
    workspace = create_workspace(prefix="gseek-test-")
    try:
        assert workspace.root.parent == tmp_path / "tmp"
    finally:
        workspace.cleanup()
