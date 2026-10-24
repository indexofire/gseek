"""Small immutable value objects shared by alignment and distance results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from gseek.types import InputKind, JsonValue

IssueSeverity = Literal["warning", "error"]
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def is_sha256_hex(value: str) -> bool:
    """Return True for a 64-character hexadecimal SHA-256 digest."""
    return len(value) == 64 and all(char in _HEX_DIGITS for char in value)


@dataclass(frozen=True, slots=True)
class FileRef:
    """A file recorded in a manifest, with its content digest."""

    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if self.size < 0:
            raise ValueError(f"file size must be non-negative, got {self.size}")
        if not is_sha256_hex(self.sha256):
            raise ValueError(f"sha256 must be 64 hex characters, got {self.sha256!r}")

    @property
    def as_path(self) -> Path:
        return Path(self.path)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRef:
        return cls(path=str(data["path"]), size=int(data["size"]), sha256=str(data["sha256"]))


@dataclass(frozen=True, slots=True)
class InputRef(FileRef):
    """A sequence input file plus format metadata."""

    kind: InputKind = InputKind.FASTA
    records: int | None = None
    compressed: bool = False

    def __post_init__(self) -> None:
        # Explicit base call: @dataclass(slots=True) replaces the class object,
        # which breaks zero-argument super() on Python 3.12.
        FileRef.__post_init__(self)
        if self.records is not None and self.records < 0:
            raise ValueError(f"record count must be non-negative, got {self.records}")

    def to_dict(self) -> dict[str, JsonValue]:
        data = FileRef.to_dict(self)
        data.update(
            {
                "kind": self.kind.value,
                "records": self.records,
                "compressed": self.compressed,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputRef:
        return cls(
            path=str(data["path"]),
            size=int(data["size"]),
            sha256=str(data["sha256"]),
            kind=InputKind(str(data.get("kind", InputKind.FASTA.value))),
            records=int(data["records"]) if data.get("records") is not None else None,
            compressed=bool(data.get("compressed", False)),
        )


@dataclass(frozen=True, slots=True)
class ToolRef:
    """A resolved external tool: path, version, and optional binary digest."""

    name: str
    version: str
    path: str | None = None
    version_raw: str = ""
    role: str = "aligner"
    sha256: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "version": self.version,
            "path": self.path,
            "version_raw": self.version_raw,
            "role": self.role,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolRef:
        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            path=str(data["path"]) if data.get("path") is not None else None,
            version_raw=str(data.get("version_raw", "")),
            role=str(data.get("role", "aligner")),
            sha256=str(data["sha256"]) if data.get("sha256") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Issue:
    """A warning or error attached to a result or manifest."""

    code: str
    message: str
    severity: IssueSeverity = "warning"
    context: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in ("warning", "error"):
            raise ValueError(f"invalid issue severity: {self.severity!r}")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Issue:
        return cls(
            code=str(data["code"]),
            message=str(data["message"]),
            severity=str(data.get("severity", "warning")),  # type: ignore[arg-type]
            context={str(key): str(value) for key, value in dict(data.get("context", {})).items()},
        )


__all__ = ["FileRef", "InputRef", "Issue", "IssueSeverity", "ToolRef"]
