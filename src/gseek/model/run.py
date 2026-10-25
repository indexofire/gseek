"""Run provenance: commands, index references, and the ``gseek.run/1`` manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast

from gseek.errors import ManifestError
from gseek.types import RUN_SCHEMA, JsonValue, RunStatus

from .common import FileRef, InputRef, Issue, ToolRef, is_sha256_hex

CommandPhase = Literal["index", "align", "distance"]


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexRef:
    """A resolved index (or a no-index reference to the target files)."""

    key: str
    adapter: str
    adapter_version: str
    entry: str
    source_sha256: str
    artifacts: tuple[str, ...] = ()
    params: dict[str, JsonValue] = field(default_factory=dict)
    created_at: str | None = None
    cache_hit: bool = False
    tool_versions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "key": self.key,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "entry": self.entry,
            "source_sha256": self.source_sha256,
            "artifacts": list(self.artifacts),
            "params": dict(self.params),
            "created_at": self.created_at,
            "cache_hit": self.cache_hit,
            "tool_versions": dict(self.tool_versions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndexRef:
        return cls(
            key=str(data["key"]),
            adapter=str(data["adapter"]),
            adapter_version=str(data["adapter_version"]),
            entry=str(data["entry"]),
            source_sha256=str(data["source_sha256"]),
            artifacts=tuple(str(item) for item in data.get("artifacts", ())),
            params=dict(data.get("params", {})),
            created_at=str(data["created_at"]) if data.get("created_at") is not None else None,
            cache_hit=bool(data.get("cache_hit", False)),
            tool_versions={
                str(key): str(value) for key, value in dict(data.get("tool_versions", {})).items()
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandRecord:
    """One executed command, kept for audit and replay."""

    phase: CommandPhase
    label: str
    argv: tuple[str, ...]
    cwd: str | None
    exit_code: int
    duration_s: float
    timed_out: bool = False
    stderr_tail: str = ""
    stdout_path: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in ("index", "align", "distance"):
            raise ManifestError(f"invalid command phase: {self.phase!r}")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "phase": self.phase,
            "label": self.label,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "stderr_tail": self.stderr_tail,
            "stdout_path": self.stdout_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandRecord:
        phase = str(data["phase"])
        if phase not in ("index", "align", "distance"):
            raise ManifestError(
                f"invalid command phase: {phase!r}",
                context={"phase": phase},
            )
        return cls(
            phase=cast(CommandPhase, phase),
            label=str(data["label"]),
            argv=tuple(str(item) for item in data.get("argv", ())),
            cwd=str(data["cwd"]) if data.get("cwd") is not None else None,
            exit_code=int(data["exit_code"]),
            duration_s=float(data["duration_s"]),
            timed_out=bool(data.get("timed_out", False)),
            stderr_tail=str(data.get("stderr_tail", "")),
            stdout_path=str(data["stdout_path"]) if data.get("stdout_path") is not None else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunManifest:
    """The reproducible record of one alignment/distance run."""

    run_id: str
    recipe_hash: str
    created_at: str
    gseek_version: str
    python_version: str
    platform: str
    backend: str
    backend_version: str
    tools: tuple[ToolRef, ...]
    inputs: tuple[InputRef, ...]
    targets: tuple[InputRef, ...]
    params: dict[str, JsonValue]
    commands: tuple[CommandRecord, ...]
    result_hash: str | None = None
    outputs: tuple[FileRef, ...] = ()
    index: IndexRef | None = None
    hostname: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    status: RunStatus = RunStatus.OK
    duration_s: float = 0.0
    warnings: tuple[Issue, ...] = ()
    errors: tuple[Issue, ...] = ()
    extra: dict[str, JsonValue] = field(default_factory=dict)
    schema: str = RUN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RUN_SCHEMA:
            raise ManifestError(f"unsupported run schema: {self.schema!r}")
        if not self.run_id:
            raise ManifestError("run_id must not be empty")
        if not is_sha256_hex(self.recipe_hash):
            raise ManifestError(
                f"recipe_hash must be a 64-char sha256 hex digest, got {self.recipe_hash!r}"
            )
        if self.duration_s < 0:
            raise ManifestError("duration_s must be non-negative")
        if self.result_hash is not None and not is_sha256_hex(self.result_hash):
            raise ManifestError(
                f"result_hash must be a 64-char sha256 hex digest, got {self.result_hash!r}"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "recipe_hash": self.recipe_hash,
            "created_at": self.created_at,
            "gseek_version": self.gseek_version,
            "python_version": self.python_version,
            "platform": self.platform,
            "hostname": self.hostname,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "tools": [tool.to_dict() for tool in self.tools],
            "inputs": [item.to_dict() for item in self.inputs],
            "targets": [item.to_dict() for item in self.targets],
            "index": self.index.to_dict() if self.index is not None else None,
            "params": dict(self.params),
            "commands": [command.to_dict() for command in self.commands],
            "result_hash": self.result_hash,
            "outputs": [item.to_dict() for item in self.outputs],
            "env": dict(self.env),
            "status": self.status.value,
            "duration_s": self.duration_s,
            "warnings": [issue.to_dict() for issue in self.warnings],
            "errors": [issue.to_dict() for issue in self.errors],
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunManifest:
        schema = str(data.get("schema", ""))
        if schema != RUN_SCHEMA:
            raise ManifestError(f"unsupported run schema: {schema!r}")
        try:
            return cls(
                schema=schema,
                run_id=str(data["run_id"]),
                recipe_hash=str(data["recipe_hash"]),
                created_at=str(data["created_at"]),
                gseek_version=str(data["gseek_version"]),
                python_version=str(data["python_version"]),
                platform=str(data["platform"]),
                hostname=str(data["hostname"]) if data.get("hostname") is not None else None,
                backend=str(data["backend"]),
                backend_version=str(data["backend_version"]),
                tools=tuple(ToolRef.from_dict(item) for item in data.get("tools", ())),
                inputs=tuple(InputRef.from_dict(item) for item in data.get("inputs", ())),
                targets=tuple(InputRef.from_dict(item) for item in data.get("targets", ())),
                index=IndexRef.from_dict(data["index"]) if data.get("index") is not None else None,
                params=dict(data.get("params", {})),
                commands=tuple(CommandRecord.from_dict(item) for item in data.get("commands", ())),
                result_hash=(
                    str(data["result_hash"]) if data.get("result_hash") is not None else None
                ),
                outputs=tuple(FileRef.from_dict(item) for item in data.get("outputs", ())),
                env={str(key): str(value) for key, value in dict(data.get("env", {})).items()},
                status=RunStatus(str(data.get("status", RunStatus.OK.value))),
                duration_s=float(data.get("duration_s", 0.0)),
                warnings=tuple(Issue.from_dict(item) for item in data.get("warnings", ())),
                errors=tuple(Issue.from_dict(item) for item in data.get("errors", ())),
                extra=dict(data.get("extra", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"malformed run manifest: {exc}") from exc


__all__ = ["CommandPhase", "CommandRecord", "IndexRef", "RunManifest"]
