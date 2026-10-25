"""Deterministic external-process execution, tool discovery, and workspaces.

This package is the only place in gseek that starts subprocesses. Adapters plan
commands; :class:`Executor` runs them with absolute paths, no shell, a C
locale, process-group timeouts, and drained stderr pipes.
"""

from .command import CommandPlan, ProcessResult, build_env, env_for_manifest
from .executor import Executor, StreamedProcess
from .tools import ToolProbe, ToolResolver, version_at_least
from .workspace import RunWorkspace, checked_workspace_parts, create_workspace

__all__ = [
    "CommandPlan",
    "Executor",
    "ProcessResult",
    "RunWorkspace",
    "StreamedProcess",
    "ToolProbe",
    "ToolResolver",
    "build_env",
    "checked_workspace_parts",
    "create_workspace",
    "env_for_manifest",
    "version_at_least",
]
