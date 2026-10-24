"""skani adapter: ANI search against sketched genomes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from gseek.adapter import (
    AlignPlan,
    BaseAdapter,
    DistanceRequest,
    IndexPlan,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.base import iter_data_lines
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement, supports
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import detect_input_kind
from gseek.model import DistanceRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind

_ALLOWED_INDEX_PARAMS = frozenset(
    {"compression_factor", "marker_compression", "threads", "timeout_s"}
)
_ALLOWED_DISTANCE_PARAMS = frozenset(
    {
        "min_af",
        "both_min_af",
        "num_results",
        "detailed",
        "ci",
        "short_header",
        "threads",
        "timeout_s",
        "compression_factor",
        "marker_compression",
    }
)
_FLAG_PARAMS = {
    "detailed": "--detailed",
    "ci": "--ci",
    "short_header": "--short-header",
}
_SKANI_HEADER = (
    "Ref_file\tQuery_file\tANI\tAlign_fraction_ref\tAlign_fraction_query\tRef_name\tQuery_name"
)


class SkaniAdapter(BaseAdapter):
    """skani ANI/Distance adapter with optional sketch databases."""

    spec = BackendSpec(
        name="skani",
        family="skani",
        summary="skani ANI search (fast genome ANI)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.DISTANCE}),
            paired=False,
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="skani",
                role="distance",
                version_args=("--version",),
                version_pattern=r"skani\s+(\d+)\.(\d+)\.(\d+)",
                min_version=(0, 2, 0),
                hint="install skani (conda-forge/bioconda package `skani`)",
            ),
        ),
    )

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: params[key]
            for key in ("compression_factor", "marker_compression")
            if key in params and params[key] is not None
        }

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        _validate_index_params(request.params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        database = index_dir / "db"
        argv: list[str] = ["skani", "sketch"]
        if "compression_factor" in request.params:
            argv.extend(["-c", str(request.params["compression_factor"])])
        if "marker_compression" in request.params:
            argv.extend(["-m", str(request.params["marker_compression"])])
        if request.threads > 1:
            argv.extend(["-t", str(request.threads)])
        argv.extend(["-o", str(database)])
        argv.extend(str(path.resolve()) for path in request.target)
        return IndexPlan(
            commands=(CommandPlan(argv=tuple(argv), label="skani sketch references"),),
            entry="db",
            artifacts=("db/index.db", "db/sketches.db", "db/markers.bin"),
            params=self.index_params(request.params),
        )

    def plan_distance(
        self,
        request: DistanceRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        _validate_request(request)
        if request.external_reference:
            raise UnsupportedFeatureError(
                "skani external database directories are not supported yet; "
                "point reference= at FASTA files or omit external_reference"
            )
        params = _validate_distance_params(request.params)
        argv: list[str] = ["skani"]
        if index is not None:
            index_dir = (ctx.index_dir or ctx.workspace).resolve()
            argv.extend(["search", "-d", str(index_dir / index.entry)])
            inputs = [str(path.resolve()) for path in request.query]
        else:
            argv.append("dist")
            inputs = [
                *(str(path.resolve()) for path in request.query),
                *(str(path.resolve()) for path in request.reference),
            ]
        argv.extend(["-t", str(max(1, request.threads))])
        for key, flag in _FLAG_PARAMS.items():
            if params.get(key):
                argv.append(flag)
        for key, flag in (
            ("min_af", "--min-af"),
            ("both_min_af", "--both-min-af"),
            ("num_results", "-n"),
        ):
            if key in params and params[key] is not None:
                argv.extend([flag, str(params[key])])
        argv.extend(inputs)
        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=tuple(argv),
                    label="skani search" if index is not None else "skani dist",
                    timeout_s=request.timeout_s,
                ),
            ),
            stream=True,
        )

    def parse_distance(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[DistanceRecord]:
        for line_number, line in iter_data_lines(_result_lines(ctx, stream), skip_prefixes=()):
            if line.startswith("Ref_file\t") or line == _SKANI_HEADER:
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                raise ParseError(
                    f"expected >= 7 skani fields, got {len(fields)}",
                    line_number=line_number,
                    raw_line=line,
                )
            try:
                ani = float(fields[2])
                fraction_ref = float(fields[3])
                fraction_query = float(fields[4])
            except (TypeError, ValueError) as exc:
                raise ParseError(
                    f"invalid skani numeric field: {exc}",
                    line_number=line_number,
                    raw_line=line,
                ) from exc
            reference_id = fields[5].strip() or Path(fields[0]).name
            query_id = fields[6].strip() or Path(fields[1]).name
            yield DistanceRecord(
                query_id=query_id,
                reference_id=reference_id,
                aligned_fraction=fraction_ref / 100.0,
                ani=ani,
                tags={
                    "metric": "skani_ani",
                    "align_fraction_query": fields[4],
                    "align_fraction_ref": fields[3],
                    "reference_file": fields[0],
                    "query_file": fields[1],
                    "query_fraction": str(fraction_query),
                },
            )


def _validate_request(request: DistanceRequest) -> None:
    query_kind = request.query_kind or detect_input_kind(request.query[0])
    if not supports(
        SkaniAdapter.spec,
        input_kind=query_kind,
        output_kind=OutputKind.DISTANCE,
    ):
        raise UnsupportedFeatureError(
            f"backend 'skani' does not support input={query_kind.value}",
            context={"backend": "skani", "input": query_kind.value},
        )


def _validate_index_params(params: Mapping[str, JsonValue]) -> None:
    unknown = set(params) - _ALLOWED_INDEX_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported skani index parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )


def _validate_distance_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_DISTANCE_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported skani parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


def _result_lines(
    ctx: ParseContext,
    stream: Iterable[str] | None,
) -> Iterator[str]:
    if stream is not None:
        yield from stream
        return
    for path in ctx.plan.result_files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            yield from handle


ADAPTER = SkaniAdapter

__all__ = ["ADAPTER", "SkaniAdapter"]
