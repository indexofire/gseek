"""KMA adapter: indexed template mapping for assemblies and reads."""

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
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import merge_fasta
from gseek.model import AlignmentRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind

_INDEX_FLAGS = {
    "k": "-k",
    "m": "-m",
    "hc": "-hc",
    "orf": "-c",
    "cds": "-C",
    "min_length": "-ML",
}
_ALLOWED_ALIGN_PARAMS = frozenset({"mem_mode", "threads", "timeout_s", *_INDEX_FLAGS})
_INDEX_ARTIFACTS = (
    "kma_db.name",
    "kma_db.length.b",
    "kma_db.seq.b",
    "kma_db.comp.b",
)
_REQUIRED_COLUMNS = frozenset(
    {
        "Template",
        "Score",
        "Template_length",
        "Template_Identity",
        "Template_Coverage",
        "Query_Identity",
        "Query_Coverage",
        "Depth",
    }
)


class KmaAdapter(BaseAdapter):
    """KMA template DB mapping; results are template-level with no coordinates."""

    spec = BackendSpec(
        name="kma",
        family="kma",
        summary="KMA template mapping for assemblies and reads",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA, InputKind.FASTQ}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            paired=True,
            index_scope=IndexScope.TARGET,
            streaming=False,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="kma",
                role="aligner",
                version_args=("-v",),
                version_pattern=r"KMA-(\d+)\.(\d+)\.(\d+)",
                min_version=(1, 4, 0),
                hint="install KMA (conda-forge/bioconda package `kma`)",
            ),
        ),
    )

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: params[key] for key in _INDEX_FLAGS if key in params and params[key] is not None
        }

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        _validate_index_params(request.params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        merged = merge_fasta(request.target, index_dir / "targets.fasta")
        prefix = index_dir / "kma_db"
        argv: list[str] = ["kma", "index", "-i", str(merged), "-o", str(prefix)]
        for key, flag in _INDEX_FLAGS.items():
            value = request.params.get(key)
            if value is None:
                continue
            if isinstance(value, bool):
                if value:
                    argv.append(flag)
            else:
                argv.extend([flag, str(value)])
        return IndexPlan(
            commands=(CommandPlan(argv=tuple(argv), label="kma index"),),
            entry="kma_db",
            artifacts=_INDEX_ARTIFACTS,
            params=self.index_params(request.params),
        )

    # ------------------------------------------------------------------
    # Alignment
    # ------------------------------------------------------------------

    def plan_align(
        self,
        request: AlignRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        self.check_align_request(request)
        params = _validate_align_params(request.params)
        query_kind = request.resolved_query_kind
        if request.paired and query_kind is not InputKind.FASTQ:
            raise UnsupportedFeatureError(
                "KMA paired mode requires FASTQ inputs",
                context={"input": query_kind.value},
            )
        if index is None:
            raise UnsupportedFeatureError(
                "KMA requires a target index (index_policy='none' is not supported)",
                hint="use index='auto' or build the kma_db index first",
            )

        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        out_prefix = ctx.path("out")
        result_file = out_prefix.with_suffix(".res")
        argv: list[str] = [
            "kma",
            "-t_db",
            str(index_dir / index.entry),
            "-o",
            str(out_prefix),
            "-t",
            str(max(1, request.threads)),
        ]
        if request.paired:
            argv.extend(["-ipe", str(request.query[0].resolve()), str(request.query[1].resolve())])
        else:
            argv.extend(["-i", str(request.query[0].resolve())])
        if query_kind is InputKind.FASTA:
            argv.append("-asm")
        else:
            argv.append("-ill")
            if params.get("mem_mode"):
                argv.append("-mem_mode")

        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=tuple(argv),
                    label="kma align",
                    timeout_s=request.timeout_s,
                ),
            ),
            stream=False,
            result_files=(result_file,),
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_align(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[AlignmentRecord]:
        if not isinstance(ctx.request, AlignRequest):  # pragma: no cover
            raise ParseError("alignment parser received a non-alignment request")
        if stream is not None:
            raise UnsupportedFeatureError(
                "KMA parses its .res result file; streaming input is not supported",
                context={"backend": "kma"},
            )
        path = ctx.plan.result_files[0] if ctx.plan.result_files else None
        if path is None or not path.is_file():
            raise ParseError(
                "KMA result file not found",
                path=str(path) if path is not None else None,
            )
        header: list[str] | None = None
        query_id = ctx.request.query[0].stem
        target_filter = ctx.request.target_filter
        keep_depth = ctx.request.resolved_query_kind is InputKind.FASTQ

        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("#"):
                    header = [column.strip() for column in line.lstrip("#").split("\t")]
                    continue
                if header is None:
                    raise ParseError(
                        "KMA data row before header",
                        path=str(path),
                        line_number=line_number,
                        raw_line=line,
                    )

                fields = [field.strip() for field in line.split("\t")]
                if len(fields) < len(header):
                    raise ParseError(
                        f"expected >= {len(header)} KMA columns, got {len(fields)}",
                        path=str(path),
                        line_number=line_number,
                        raw_line=line,
                    )
                row = dict(zip(header, fields, strict=False))
                missing = _REQUIRED_COLUMNS - row.keys()
                if missing:
                    raise ParseError(
                        f"KMA result missing columns: {sorted(missing)}",
                        path=str(path),
                        line_number=line_number,
                        raw_line=line,
                    )
                try:
                    template = row["Template"]
                    if target_filter is not None and template not in target_filter:
                        continue
                    target_length = int(float(row["Template_length"]))
                    identity = float(row["Template_Identity"])
                    target_coverage = float(row["Template_Coverage"]) / 100.0
                    query_coverage = float(row["Query_Coverage"]) / 100.0
                    score = float(row["Score"])
                    depth = float(row["Depth"]) if keep_depth else None
                except (TypeError, ValueError) as exc:
                    raise ParseError(
                        f"invalid KMA numeric field: {exc}",
                        path=str(path),
                        line_number=line_number,
                        raw_line=line,
                    ) from exc

                tags = {
                    "identity_method": "kma_template",
                    "query_identity": row["Query_Identity"],
                    "query_coverage_percent": row["Query_Coverage"],
                }
                for key in ("Expected", "q_value", "p_value"):
                    if key in row:
                        tags[key] = row[key]
                yield AlignmentRecord(
                    query_id=query_id,
                    target_id=template,
                    target_length=target_length,
                    identity=identity,
                    aligned_length=None,
                    score=score,
                    depth=depth,
                    target_coverage=target_coverage,
                    query_coverage=query_coverage,
                    tags=tags,
                )


def _validate_index_params(params: Mapping[str, JsonValue]) -> None:
    unknown = set(params) - set(_INDEX_FLAGS) - {"threads", "timeout_s", "mem_mode"}
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported KMA index parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )


def _validate_align_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_ALIGN_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported KMA parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


ADAPTER = KmaAdapter

__all__ = ["ADAPTER", "KmaAdapter"]
