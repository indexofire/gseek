"""Backend capability declarations.

Adapters describe *what* they can do; the registry and runner use these
declarations to route requests without importing every adapter module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gseek.errors import ConfigError
from gseek.types import IndexScope, InputKind, JsonValue, Molecule, OutputKind

if TYPE_CHECKING:
    from gseek.execution import ToolProbe


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    """One external binary required by a backend."""

    binary: str
    role: str = "aligner"
    version_args: tuple[str, ...] = ("--version",)
    version_pattern: str | None = None
    min_version: tuple[int, ...] | None = None
    optional: bool = False
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.binary:
            raise ConfigError("tool binary name must not be empty")
        if self.min_version is not None and any(part < 0 for part in self.min_version):
            raise ConfigError("min_version components must be non-negative")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a backend can accept and produce."""

    inputs: frozenset[InputKind]
    outputs: frozenset[OutputKind]
    molecule: frozenset[Molecule] = frozenset({Molecule.DNA})
    paired: bool = False
    index_scope: IndexScope = IndexScope.NONE
    streaming: bool = False
    threads: bool = True
    presets: tuple[str, ...] = ()
    deterministic: bool = True

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ConfigError("capabilities.inputs must not be empty")
        if not self.outputs:
            raise ConfigError("capabilities.outputs must not be empty")
        if not self.molecule:
            raise ConfigError("capabilities.molecule must not be empty")


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Static metadata for one backend family."""

    name: str
    family: str
    summary: str
    capabilities: Capabilities
    tools: tuple[ToolRequirement, ...]
    aliases: tuple[str, ...] = ()
    api_version: str = "1"
    params_schema: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(f"invalid backend name: {self.name!r}")
        if not self.family:
            raise ConfigError("backend family must not be empty")
        if not self.tools:
            raise ConfigError(f"backend {self.name!r} must declare at least one tool")
        names = [tool.binary for tool in self.tools]
        if len(names) != len(set(names)):
            raise ConfigError(f"backend {self.name!r} declares duplicate tools: {names}")

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True, slots=True)
class ToolchainProbe:
    """The result of probing every tool required by a backend."""

    backend: str
    tools: tuple[ToolProbe, ...]
    ok: bool
    error: str = ""

    @property
    def installed(self) -> bool:
        return self.ok

    def tool(self, name: str) -> ToolProbe | None:
        return next((probe for probe in self.tools if probe.name == name), None)


def supports(
    spec: BackendSpec,
    *,
    input_kind: InputKind | None = None,
    output_kind: OutputKind | None = None,
    paired: bool | None = None,
    molecule: Molecule | None = None,
) -> bool:
    """Return True when *spec* satisfies every supplied constraint."""
    capabilities = spec.capabilities
    if input_kind is not None and input_kind not in capabilities.inputs:
        return False
    if output_kind is not None and output_kind not in capabilities.outputs:
        return False
    if paired is not None and paired and not capabilities.paired:
        return False
    return molecule is None or molecule in capabilities.molecule


__all__ = [
    "BackendSpec",
    "Capabilities",
    "ToolRequirement",
    "ToolchainProbe",
    "supports",
]
