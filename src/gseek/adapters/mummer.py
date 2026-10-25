"""MUMmer4/nucmer adapter: nucmer + show-coords, no persistent index."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    BaseAdapter,
    IndexPlan,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.base import iter_data_lines, result_lines
from gseek.capabilities import BackendSpec, Capabilities, ToolchainProbe, ToolRequirement
from gseek.errors import ParseError, ToolNotFoundError, ToolVersionError, UnsupportedFeatureError
from gseek.execution import CommandPlan, ToolProbe, ToolResolver
from gseek.io import merge_fasta
from gseek.model import AlignmentRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand

_ALLOWED_PARAMS = frozenset({"maxmatch", "min_cluster", "min_match", "threads"})
_DEFAULT_MIN_CLUSTER = 20
_DEFAULT_MIN_MATCH = 20


class MummerAdapter(BaseAdapter):
    """MUMmer 4.x nucmer alignment normalized through show-coords."""

    spec = BackendSpec(
        name="mummer",
        family="mummer",
        summary="MUMmer4 nucmer alignment (show-coords)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            paired=False,
            index_scope=IndexScope.NONE,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="nucmer",
                role="aligner",
                version_args=("--version",),
                version_pattern=r"(\d+)\.(\d+)\.(\d+)",
                min_version=(4, 0, 0),
                hint="install MUMmer 4.x (conda-forge/bioconda package `mummer4`)",
            ),
            ToolRequirement(
                binary="show-coords",
                role="parser",
                version_args=("-h",),
                version_pattern=None,
                min_version=None,
                optional=True,
                hint="install MUMmer 4.x (provides show-coords)",
            ),
        ),
        aliases=("nucmer", "mummer4"),
    )

    def probe(self, resolver: ToolResolver, *, strict: bool = False) -> ToolchainProbe:
        """Probe nucmer for version; verify show-coords exists separately."""
        nucmer_requirement = next(tool for tool in self.spec.tools if tool.binary == "nucmer")
        nucmer = resolver.probe(nucmer_requirement, strict=False)
        show_path = resolver.which("show-coords")
        show_coords = ToolProbe(
            name="show-coords",
            path=show_path,
            version=nucmer.version if show_path is not None else "",
            ok=show_path is not None,
            error="" if show_path is not None else "show-coords not found",
            role="parser",
        )
        probes = (nucmer, show_coords)
        failures = [
            f"{probe.name}: {probe.error or 'unavailable'}" for probe in probes if not probe.ok
        ]
        result = ToolchainProbe(
            backend=self.spec.name,
            tools=probes,
            ok=not failures,
            error="; ".join(failures),
        )
        if strict and not result.ok:
            # Classify by structure, not by sniffing the error text: a binary
            # that exists but probes badly is a version/tool problem, a binary
            # without a path is simply missing.
            if nucmer.path is not None and not nucmer.ok:
                raise ToolVersionError(
                    f"backend 'mummer' has an unusable nucmer version: {nucmer.error}",
                    context={"backend": self.spec.name, "binary": "nucmer"},
                )
            raise ToolNotFoundError(
                f"backend 'mummer' is unavailable: {result.error}",
                context={"backend": self.spec.name},
            )
        return result

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan | None:
        return None

    def plan_align(
        self,
        request: AlignRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        self.check_align_request(request)
        params = _validated_params(request.params)
        target_fasta = merge_fasta(request.target, ctx.path("targets.fasta"))
        prefix = ctx.path("nucmer")
        delta = prefix.with_suffix(".delta")

        nucmer_argv: list[str] = ["nucmer"]
        if bool(params.get("maxmatch", True)):
            nucmer_argv.append("--maxmatch")
        nucmer_argv.extend(
            [
                "-c",
                str(params.get("min_cluster", _DEFAULT_MIN_CLUSTER)),
                "-l",
                str(params.get("min_match", _DEFAULT_MIN_MATCH)),
            ]
        )
        if request.threads > 1:
            nucmer_argv.extend(["-t", str(request.threads)])
        nucmer_argv.extend(
            [
                "-p",
                str(prefix),
                str(target_fasta.resolve()),
                str(request.query[0].resolve()),
            ]
        )
        coords_argv = ("show-coords", "-rcl", "-T", str(delta))
        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=tuple(nucmer_argv),
                    label="nucmer align",
                    timeout_s=request.timeout_s,
                ),
                CommandPlan(
                    argv=coords_argv,
                    label="show-coords",
                    timeout_s=request.timeout_s,
                ),
            ),
            stream=True,
        )

    def parse_align(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[AlignmentRecord]:
        if not isinstance(ctx.request, AlignRequest):  # pragma: no cover
            raise ParseError("alignment parser received a non-alignment request")
        target_filter = ctx.request.target_filter
        path = str(ctx.plan.result_files[0]) if ctx.plan.result_files else None
        for line_number, line in iter_data_lines(result_lines(ctx, stream), skip_prefixes=()):
            # show-coords echoes the delta header (target/query paths, NUCMER,
            # [S1]...); only numeric data rows start with a digit.
            if not line[:1].isdigit():
                continue
            fields = line.split("\t")
            if len(fields) < 13:
                raise ParseError(
                    f"expected >= 13 show-coords fields, got {len(fields)}",
                    path=path,
                    line_number=line_number,
                    raw_line=line,
                )
            try:
                s1 = int(fields[0])
                e1 = int(fields[1])
                s2 = int(fields[2])
                e2 = int(fields[3])
                len1 = int(fields[4])
                identity = float(fields[6])
                lenr = int(fields[7])
                lenq = int(fields[8])
                covr = float(fields[9])
                covq = float(fields[10])
            except (TypeError, ValueError) as exc:
                raise ParseError(
                    f"invalid show-coords numeric field: {exc}",
                    path=path,
                    line_number=line_number,
                    raw_line=line,
                ) from exc

            target_id = fields[11].strip()
            query_id = fields[12].strip()
            if target_filter is not None and target_id not in target_filter:
                continue
            yield AlignmentRecord(
                query_id=query_id,
                target_id=target_id,
                query_length=lenq,
                target_length=lenr,
                query_start=min(s2, e2) - 1,
                query_end=max(s2, e2),
                target_start=min(s1, e1) - 1,
                target_end=max(s1, e1),
                strand=Strand.MINUS if s2 > e2 else Strand.PLUS,
                identity=identity,
                aligned_length=len1,
                target_coverage=covr / 100.0,
                query_coverage=covq / 100.0,
                tags={"identity_method": "mummer_coords"},
            )


def _validated_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported MUMmer parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


ADAPTER = MummerAdapter

__all__ = ["ADAPTER", "MummerAdapter"]
