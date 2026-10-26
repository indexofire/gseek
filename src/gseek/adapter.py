"""Adapter protocol: pure planning and parsing, no subprocesses.

An adapter turns a declarative request into commands plus a parser. The runner
owns execution, index publication, provenance, and result construction. This
split is what makes adapters testable without installing external tools.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from gseek.capabilities import BackendSpec, ToolchainProbe, ToolRequirement, supports
from gseek.errors import ToolNotFoundError, ToolVersionError, UnsupportedFeatureError
from gseek.execution import CommandPlan, ToolProbe, ToolResolver, checked_workspace_parts
from gseek.index import IndexPolicy
from gseek.io import detect_input_kind
from gseek.model import AlignmentRecord, IndexRef, InputRef, ToolRef
from gseek.provenance import semantic_params
from gseek.types import InputKind, JsonValue, Molecule, OutputKind


@dataclass(frozen=True, slots=True)
class AlignRequest:
    """One query (or read pair) against one target set."""

    query: tuple[Path, ...]
    target: tuple[Path, ...]
    query_kind: InputKind | None = None
    paired: bool = False
    params: Mapping[str, JsonValue] = field(default_factory=dict)
    threads: int = 1
    timeout_s: float | None = None
    index_policy: IndexPolicy = IndexPolicy.AUTO
    target_filter: frozenset[str] | None = None
    work_dir: Path | None = None
    keep_work: bool = False

    def __post_init__(self) -> None:
        if not self.query:
            raise UnsupportedFeatureError("at least one query file is required")
        if not self.target:
            raise UnsupportedFeatureError("at least one target file is required")
        if self.paired and len(self.query) != 2:
            raise UnsupportedFeatureError("paired mode requires exactly two query files")
        if not self.paired and len(self.query) != 1:
            raise UnsupportedFeatureError("single-end mode requires exactly one query file")
        if self.threads < 1:
            raise UnsupportedFeatureError(f"threads must be >= 1, got {self.threads}")
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise UnsupportedFeatureError(f"timeout_s must be positive, got {self.timeout_s}")

    @property
    def resolved_query_kind(self) -> InputKind:
        return self.query_kind or detect_input_kind(self.query[0])


@dataclass(frozen=True, slots=True)
class DistanceRequest:
    """One or more queries against a sketch/distance reference set."""

    query: tuple[Path, ...]
    reference: tuple[Path, ...]
    query_kind: InputKind | None = None
    params: Mapping[str, JsonValue] = field(default_factory=dict)
    threads: int = 1
    timeout_s: float | None = None
    index_policy: IndexPolicy = IndexPolicy.AUTO
    external_reference: bool = False
    external_sha256: str | None = None
    external_size: int | None = None
    work_dir: Path | None = None
    keep_work: bool = False

    def __post_init__(self) -> None:
        if not self.query:
            raise UnsupportedFeatureError("at least one query file is required")
        if not self.reference:
            raise UnsupportedFeatureError("at least one reference file is required")
        if self.threads < 1:
            raise UnsupportedFeatureError(f"threads must be >= 1, got {self.threads}")
        if self.external_reference and len(self.reference) != 1:
            raise UnsupportedFeatureError(
                "external_reference requires exactly one pre-built reference file"
            )
        if (self.external_sha256 is None) != (self.external_size is None):
            raise UnsupportedFeatureError(
                "external_sha256 and external_size must be supplied together"
            )
        if self.external_sha256 is not None and not self.external_reference:
            raise UnsupportedFeatureError(
                "external_sha256/external_size require external_reference=True"
            )


@dataclass(frozen=True, slots=True)
class IndexRequest:
    target: tuple[Path, ...]
    params: Mapping[str, JsonValue] = field(default_factory=dict)
    threads: int = 1
    force: bool = False


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Paths and immutable context available while planning commands."""

    workspace: Path
    index_dir: Path | None = None
    threads: int = 1
    query_refs: tuple[InputRef, ...] = ()
    target_refs: tuple[InputRef, ...] = ()

    def path(self, *parts: str) -> Path:
        target = self.workspace.joinpath(*checked_workspace_parts(self.workspace, parts))
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


@dataclass(frozen=True, slots=True)
class IndexPlan:
    """Commands that build an index plus the expected entry/artifacts."""

    commands: tuple[CommandPlan, ...]
    entry: str
    artifacts: tuple[str, ...] = ()
    params: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlignPlan:
    """Commands that perform one alignment plus how to parse their output."""

    commands: tuple[CommandPlan, ...]
    stream: bool = False
    result_files: tuple[Path, ...] = ()
    extra: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParseContext:
    """Everything a parser needs to normalize one result set."""

    # Alignment parsers receive an AlignRequest, distance parsers a
    # DistanceRequest; the union keeps the protocol honest without casts.
    request: AlignRequest | DistanceRequest
    plan: AlignPlan
    workspace: Path
    index: IndexRef | None = None
    tools: tuple[ToolRef, ...] = ()


@runtime_checkable
class AlignmentAdapter(Protocol):
    """A backend that produces :class:`AlignmentRecord` rows."""

    spec: BackendSpec

    def probe(
        self, resolver: ToolResolver, *, strict: bool = False, include_optional: bool = True
    ) -> ToolchainProbe: ...

    def semantic_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]: ...

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan | None: ...

    def plan_align(
        self,
        request: AlignRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan: ...

    def parse_align(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[AlignmentRecord]: ...


@runtime_checkable
class DistanceAdapter(Protocol):
    """A backend that produces distance/containment rows."""

    spec: BackendSpec

    def probe(
        self, resolver: ToolResolver, *, strict: bool = False, include_optional: bool = True
    ) -> ToolchainProbe: ...

    def semantic_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]: ...

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan | None: ...

    def plan_distance(
        self,
        request: DistanceRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan: ...

    def parse_distance(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[object]: ...


_LAZY_ERROR = "lazy: not probed (probed on demand per request)"


class BaseAdapter:
    """Shared validation/probing helpers for concrete adapters."""

    spec: BackendSpec

    def validate_tools(
        self,
        request: AlignRequest | DistanceRequest,
        probe: ToolchainProbe,
        resolver: ToolResolver | None = None,
    ) -> Mapping[str, Path] | None:
        """Raise when *request* needs a tool the *probe* shows unusable.

        The strict :meth:`probe` already gates non-optional tools; adapters
        whose requests pick among optional binaries (BLAST task binaries)
        override this to gate the right one with the right minimum version.
        The probe result is passed in so custom probe implementations and
        test doubles keep working. When the runner probes lazily
        (``include_optional=False``), the override may resolve the task
        binary on demand through *resolver* and return ``{binary: path}``
        bindings for the executor.
        """
        del request, probe, resolver
        return None

    def probe(
        self,
        resolver: ToolResolver,
        *,
        strict: bool = False,
        include_optional: bool = True,
    ) -> ToolchainProbe:
        """Resolve and version-check declared tools.

        ``include_optional=False`` replaces optional tools with lazy
        placeholders (no ``--version`` subprocess): the runner uses this so a
        request pays only for the binaries it can actually use, and
        ``validate_tools`` probes the task binary on demand. ``doctor`` keeps
        the default eager mode.
        """
        probes = []
        failed: list[tuple[ToolRequirement, ToolProbe]] = []
        for requirement in self.spec.tools:
            if not include_optional and requirement.optional:
                probes.append(
                    ToolProbe(
                        name=requirement.binary,
                        path=None,
                        version="",
                        ok=False,
                        error=_LAZY_ERROR,
                        role=requirement.role,
                    )
                )
                continue
            probe = resolver.probe(requirement, strict=False)
            probes.append(probe)
            if not probe.ok and not requirement.optional:
                failed.append((requirement, probe))
        failures = [f"{probe.name}: {probe.error or 'unavailable'}" for _, probe in failed]
        ok = not failures
        result = ToolchainProbe(
            backend=self.spec.name,
            tools=tuple(probes),
            ok=ok,
            error="; ".join(failures),
        )
        if strict and failed:
            missing = any(probe.path is None for _, probe in failed)
            error_type = ToolNotFoundError if missing else ToolVersionError
            hint = "; ".join(requirement.hint for requirement, _ in failed if requirement.hint)
            raise error_type(
                f"backend {self.spec.name!r} is unavailable: {result.error}",
                hint=hint,
                context={"backend": self.spec.name},
            )
        return result

    def semantic_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return semantic_params(params)

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan | None:
        if self.spec.capabilities.index_scope.value != "none":
            raise NotImplementedError(f"{self.spec.name} declares an index but does not plan one")
        return None

    def check_align_request(self, request: AlignRequest) -> InputKind:
        """Validate input kind/paired/preset against the capability declaration."""
        query_kind = request.resolved_query_kind
        if not supports(
            self.spec,
            input_kind=query_kind,
            output_kind=OutputKind.ALIGNMENT,
            paired=request.paired,
        ):
            raise UnsupportedFeatureError(
                f"backend {self.spec.name!r} does not support "
                f"input={query_kind.value}, paired={request.paired}",
                context={"backend": self.spec.name, "input": query_kind.value},
            )
        preset = request.params.get("preset")
        if (
            preset is not None
            and self.spec.capabilities.presets
            and str(preset) not in self.spec.capabilities.presets
        ):
            raise UnsupportedFeatureError(
                f"unknown preset {preset!r} for backend {self.spec.name!r}; "
                f"expected one of {self.spec.capabilities.presets}",
                context={"backend": self.spec.name, "preset": str(preset)},
            )
        return query_kind

    def describe(self) -> dict[str, JsonValue]:
        from gseek.provenance import to_jsonable

        return to_jsonable(self.spec)


def request_molecule(params: Mapping[str, JsonValue]) -> Molecule:
    raw = str(params.get("molecule", Molecule.DNA.value)).lower()
    try:
        return Molecule(raw)
    except ValueError as exc:
        raise UnsupportedFeatureError(
            f"unknown molecule {raw!r}",
            context={"molecule": raw},
        ) from exc


__all__ = [
    "AlignPlan",
    "AlignRequest",
    "AlignmentAdapter",
    "BaseAdapter",
    "DistanceAdapter",
    "DistanceRequest",
    "IndexPlan",
    "IndexRequest",
    "ParseContext",
    "PlanContext",
    "request_molecule",
]
