"""Mash adapter: MinHash sketches and mash dist."""

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
from gseek.adapters.base import finite_float, iter_data_lines, result_lines
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement, supports
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import detect_input_kind
from gseek.model import DistanceRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind

_DEFAULT_K = 21
_DEFAULT_SKETCH_SIZE = 1000
_DEFAULT_SEED = 42
_DEFAULT_MAX_DISTANCE = 0.1
_ALLOWED_PARAMS = frozenset(
    {"k", "sketch_size", "seed", "max_distance", "mode", "threads", "reads"}
)


class MashAdapter(BaseAdapter):
    """Mash sketch/dist adapter for genome and read containment/distance."""

    spec = BackendSpec(
        name="mash",
        family="minhash",
        summary="Mash MinHash distance (mash dist)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA, InputKind.FASTQ}),
            outputs=frozenset({OutputKind.DISTANCE}),
            paired=False,
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="mash",
                role="distance",
                version_args=("--version",),
                version_pattern=r"(\d+)\.(\d+)",
                min_version=(2, 0),
                hint="install Mash (conda-forge/bioconda package `mash`)",
            ),
        ),
    )

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            "k": params.get("k", _DEFAULT_K),
            "sketch_size": params.get("sketch_size", _DEFAULT_SKETCH_SIZE),
            "seed": params.get("seed", _DEFAULT_SEED),
        }

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        _validate_params(request.params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        prefix = index_dir / "targets"
        argv = _sketch_argv(
            request.target,
            prefix,
            request.params,
            threads=request.threads,
        )
        return IndexPlan(
            commands=(CommandPlan(argv=tuple(argv), label="mash sketch references"),),
            entry="targets.msh",
            artifacts=("targets.msh",),
            params=self.index_params(request.params),
        )

    def plan_distance(
        self,
        request: DistanceRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        _validate_request(request)
        params = _validate_params(request.params)
        mode = str(params.get("mode", "dist")).lower()
        if mode not in {"dist", "screen"}:
            raise UnsupportedFeatureError(
                f"unknown mash mode {mode!r}; expected 'dist' or 'screen'",
                context={"mode": mode},
            )
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        commands: list[CommandPlan] = []
        if request.external_reference:
            reference_sketch = request.reference[0]
        elif index is not None:
            reference_sketch = index_dir / index.entry
        else:
            reference_prefix = ctx.path("references")
            commands.append(
                CommandPlan(
                    argv=tuple(
                        _sketch_argv(
                            request.reference,
                            reference_prefix,
                            params,
                            threads=request.threads,
                        )
                    ),
                    label="mash sketch references",
                    timeout_s=request.timeout_s,
                )
            )
            reference_sketch = reference_prefix.with_suffix(".msh")

        if mode == "screen":
            if len(request.query) != 1:
                # Screen output rows carry no query-file column; attributing
                # them to query[0] would silently mislabel every record.
                raise UnsupportedFeatureError(
                    "mash screen requires a single query file (pool reads first)",
                    context={"query_count": str(len(request.query))},
                )
            screen_argv = [
                "mash",
                "screen",
                "-p",
                str(max(1, request.threads)),
                str(reference_sketch),
                *[str(path.resolve()) for path in request.query],
            ]
            commands.append(
                CommandPlan(
                    argv=tuple(screen_argv),
                    label="mash screen",
                    timeout_s=request.timeout_s,
                )
            )
            return AlignPlan(commands=tuple(commands), stream=True)

        query_kind = request.query_kind or detect_input_kind(request.query[0])
        direct_query = (
            request.external_reference
            and len(request.query) == 1
            and query_kind is InputKind.FASTA
            and mode == "dist"
            and not any(key in params for key in ("k", "sketch_size", "seed", "reads"))
        )
        if direct_query:
            query_argument = str(request.query[0].resolve())
        else:
            query_prefix = ctx.path("query")
            commands.insert(
                0,
                CommandPlan(
                    argv=tuple(
                        _sketch_argv(
                            request.query,
                            query_prefix,
                            params,
                            threads=request.threads,
                            reads=bool(params.get("reads")),
                        )
                    ),
                    label="mash sketch queries",
                    timeout_s=request.timeout_s,
                ),
            )
            query_argument = str(query_prefix.with_suffix(".msh"))
        dist_argv = [
            "mash",
            "dist",
            "-p",
            str(max(1, request.threads)),
            "-d",
            str(params.get("max_distance", _DEFAULT_MAX_DISTANCE)),
            str(reference_sketch),
            query_argument,
        ]
        commands.append(
            CommandPlan(
                argv=tuple(dist_argv),
                label="mash dist",
                timeout_s=request.timeout_s,
            )
        )
        return AlignPlan(commands=tuple(commands), stream=True)

    def parse_distance(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[DistanceRecord]:
        request = ctx.request
        if not isinstance(request, DistanceRequest):  # pragma: no cover
            raise ParseError("distance parser received a non-distance request")
        if request.external_reference:
            # The pre-built reference sketch may use different parameters than
            # the request; recording request values would misdescribe the
            # comparison actually performed.
            k = "unknown"
            sketch_size = "unknown"
        else:
            k = str(request.params.get("k", _DEFAULT_K))
            sketch_size = str(request.params.get("sketch_size", _DEFAULT_SKETCH_SIZE))
        mode = str(request.params.get("mode", "dist")).lower()
        query_stem = request.query[0].stem
        for line_number, line in iter_data_lines(result_lines(ctx, stream), skip_prefixes=()):
            fields = line.split("\t")
            if len(fields) < 5:
                raise ParseError(
                    f"expected >= 5 mash fields, got {len(fields)}",
                    line_number=line_number,
                    raw_line=line,
                )
            if mode == "screen":
                try:
                    identity = finite_float(fields[0])
                    shared_raw, _, total_raw = fields[1].partition("/")
                    shared = int(shared_raw)
                    total = int(total_raw)
                    pvalue = finite_float(fields[3])
                except (TypeError, ValueError) as exc:
                    raise ParseError(
                        f"invalid mash field: {exc}",
                        line_number=line_number,
                        raw_line=line,
                    ) from exc
                yield DistanceRecord(
                    query_id=query_stem,
                    # Screen column 5 is the sketch *defline*, not a path:
                    # Path().name would truncate descriptions containing '/'.
                    reference_id=fields[4].strip(),
                    distance=max(0.0, 1.0 - identity),
                    pvalue=pvalue,
                    shared_hashes=shared,
                    total_hashes=total,
                    containment=identity,
                    tags={
                        "metric": "mash_screen",
                        "median_multiplicity": fields[2],
                        "reference_name": fields[4],
                        "k": k,
                        "sketch_size": sketch_size,
                    },
                )
                continue
            try:
                distance = finite_float(fields[2])
                pvalue = finite_float(fields[3])
                shared_raw, _, total_raw = fields[4].partition("/")
                shared = int(shared_raw)
                total = int(total_raw)
            except (TypeError, ValueError) as exc:
                raise ParseError(
                    f"invalid mash field: {exc}",
                    line_number=line_number,
                    raw_line=line,
                ) from exc
            yield DistanceRecord(
                query_id=_display_id(fields[1]),
                reference_id=_display_id(fields[0]),
                distance=distance,
                pvalue=pvalue,
                shared_hashes=shared,
                total_hashes=total,
                tags={
                    "metric": "mash_distance",
                    "reference_path": fields[0],
                    "query_path": fields[1],
                    "k": k,
                    "sketch_size": sketch_size,
                },
            )


def _display_id(value: str) -> str:
    # rsplit is ~25x cheaper than Path().name and this runs per dist row.
    return value.rsplit("/", 1)[-1] if value else value


def _sketch_argv(
    inputs: Iterable[Path],
    prefix: Path,
    params: Mapping[str, JsonValue],
    *,
    threads: int,
    reads: bool = False,
) -> list[str]:
    argv = [
        "mash",
        "sketch",
    ]
    if reads:
        argv.extend(["-m", "2", "-r"])
    argv.extend(
        [
            "-k",
            str(params.get("k", _DEFAULT_K)),
            "-s",
            str(params.get("sketch_size", _DEFAULT_SKETCH_SIZE)),
            "-S",
            str(params.get("seed", _DEFAULT_SEED)),
        ]
    )
    if threads > 1:
        argv.extend(["-p", str(threads)])
    argv.extend(["-o", str(prefix)])
    argv.extend(str(path.resolve()) for path in inputs)
    return argv


def _validate_request(request: DistanceRequest) -> None:
    query_kind = request.query_kind or detect_input_kind(request.query[0])
    if not supports(
        MashAdapter.spec,
        input_kind=query_kind,
        output_kind=OutputKind.DISTANCE,
    ):
        raise UnsupportedFeatureError(
            f"backend 'mash' does not support input={query_kind.value}",
            context={"backend": "mash", "input": query_kind.value},
        )


def _validate_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported mash parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


ADAPTER = MashAdapter

__all__ = ["ADAPTER", "MashAdapter"]
