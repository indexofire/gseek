"""Typed errors and the ``gseek.error/1`` JSON envelope.

The error model is intentionally close to ``gapit``'s: every failure carries a
stable ``code``, an exit code suitable for a CLI, a machine-readable context
mapping, and an optional actionable hint. Library callers can catch
:class:`GseekError` and inspect ``code`` without parsing messages.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

ERROR_SCHEMA = "gseek.error/1"


class GseekError(Exception):
    """Base class for all typed gseek failures."""

    code: str = "GSEEK_ERROR"
    exit_code: int = 1

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: Mapping[str, str] | None = None,
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        self.context: dict[str, str] = dict(context or {})
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        """Return the serialisable ``gseek.error/1`` envelope."""
        return {
            "schema": ERROR_SCHEMA,
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
            "hint": self.hint,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        if self.hint:
            return f"{self.message} ({self.hint})"
        return self.message


class UsageError(GseekError):
    """Invalid API/CLI usage."""

    code = "USAGE_ERROR"
    exit_code = 2


class ConfigError(GseekError):
    """Invalid or missing configuration."""

    code = "CONFIG_ERROR"
    exit_code = 2


class UnsupportedFeatureError(GseekError):
    """The requested capability is not supported by this backend."""

    code = "UNSUPPORTED_FEATURE"
    exit_code = 2


class ToolNotFoundError(GseekError):
    """A required external binary could not be resolved."""

    code = "TOOL_NOT_FOUND"
    exit_code = 3


class ToolVersionError(GseekError):
    """A tool exists but its version is unparseable or too old."""

    code = "TOOL_VERSION_ERROR"
    exit_code = 3


class ExecutionError(GseekError):
    """An external command failed or timed out."""

    code = "EXECUTION_FAILED"
    exit_code = 4

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        returncode: int | None = None,
        stderr: str = "",
        timed_out: bool = False,
        context: Mapping[str, str] | None = None,
        hint: str = "",
    ) -> None:
        ctx = dict(context or {})
        if argv:
            ctx.setdefault("argv", " ".join(str(part) for part in argv))
        if returncode is not None:
            ctx.setdefault("returncode", str(returncode))
        if timed_out:
            ctx.setdefault("timed_out", "true")
        super().__init__(message, context=ctx, hint=hint)
        self.argv = tuple(str(part) for part in argv)
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out


class ParseError(GseekError):
    """A tool output line could not be parsed."""

    code = "PARSE_ERROR"
    exit_code = 5

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        line_number: int | None = None,
        raw_line: str = "",
        context: Mapping[str, str] | None = None,
        hint: str = "",
    ) -> None:
        ctx = dict(context or {})
        if path is not None:
            ctx.setdefault("path", path)
        if line_number is not None:
            ctx.setdefault("line", str(line_number))
        if raw_line:
            ctx.setdefault("raw", raw_line[:200])
        super().__init__(message, context=ctx, hint=hint)
        self.path = path
        self.line_number = line_number
        self.raw_line = raw_line


class InputError(GseekError):
    """User-supplied input is missing, malformed, or unsupported."""

    code = "INPUT_ERROR"
    exit_code = 5


class DuplicateIdError(InputError):
    """Two input records share the same sequence identifier."""

    code = "DUPLICATE_SEQUENCE_ID"


class IndexBuildError(GseekError):
    """An index could not be built, validated, or published."""

    code = "INDEX_BUILD_FAILED"
    exit_code = 4


class IndexLockError(GseekError):
    """Timed out waiting for another process to finish building an index."""

    code = "INDEX_LOCK_TIMEOUT"
    exit_code = 4


class CacheError(GseekError):
    """The cache directory is unavailable, corrupt, or not writable."""

    code = "CACHE_ERROR"
    exit_code = 4


class ManifestError(GseekError):
    """A run/index manifest is missing, malformed, or fails verification."""

    code = "MANIFEST_ERROR"
    exit_code = 4


class ReplayMismatchError(GseekError):
    """A replay produced a different canonical result than the manifest."""

    code = "REPLAY_MISMATCH"
    exit_code = 6


def render_error(exc: BaseException) -> str:
    """Render any exception as a one-line ``gseek.error/1`` JSON envelope."""
    if isinstance(exc, GseekError):
        envelope = exc.to_dict()
    else:
        envelope = {
            "schema": ERROR_SCHEMA,
            "code": "UNEXPECTED",
            "message": f"{type(exc).__name__}: {exc}",
            "context": {},
            "hint": "",
        }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fail(
    message: str,
    *,
    code: str,
    exit_code: int = 1,
    context: Mapping[str, str] | None = None,
    hint: str = "",
) -> NoReturn:
    """Raise a plain :class:`GseekError` with an explicit code (helper)."""
    error = GseekError(message, code=code, context=context, hint=hint)
    error.exit_code = exit_code
    raise error


__all__ = [
    "ERROR_SCHEMA",
    "CacheError",
    "ConfigError",
    "DuplicateIdError",
    "ExecutionError",
    "GseekError",
    "IndexBuildError",
    "IndexLockError",
    "InputError",
    "ManifestError",
    "ParseError",
    "ReplayMismatchError",
    "ToolNotFoundError",
    "ToolVersionError",
    "UnsupportedFeatureError",
    "UsageError",
    "fail",
    "render_error",
]
