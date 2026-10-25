"""Run workspaces under GSEEK_TMPDIR, a parent, or the system temp."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gseek.errors import InputError


def checked_workspace_parts(root: Path, parts: tuple[str, ...]) -> tuple[str, ...]:
    """Reject parts that would silently escape the workspace root."""
    for part in parts:
        candidate = Path(part)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise InputError(
                f"workspace path escapes the workspace: {part!r}",
                context={"workspace": str(root), "part": part},
            )
    return parts


@dataclass
class RunWorkspace:
    """A temporary directory for one run's intermediate files."""

    root: Path
    keep: bool = False

    def path(self, *parts: str) -> Path:
        target = self.root.joinpath(*checked_workspace_parts(self.root, parts))
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
