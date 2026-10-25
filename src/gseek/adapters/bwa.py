"""BWA-MEM adapter: bwa index + bwa mem, SAM output."""

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
from gseek.adapters.base import float_or_none, parse_cigar, parse_sam_tags
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import merge_fasta
from gseek.model import AlignmentRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand

_INDEX_ARTIFACTS = (
    "targets.amb",
    "targets.ann",
    "targets.bwt",
    "targets.pac",
    "targets.sa",
)
_ALLOWED_INDEX_PARAMS = frozenset({"algorithm", "block_size", "threads", "timeout_s"})
_ALLOWED_ALIGN_PARAMS = frozenset(
    {
        "algorithm",
        "block_size",
        "threads",
        "timeout_s",
        "secondary",
        "mark_shorter",
        "soft_clip_supp",
        "all",
        "read_group",
        "min_score",
        "k",
        "w",
        "A",
        "B",
        "O",
        "E",
        "L",
        "c",
        "D",
        "W",
        "m",
        "S",
        "P",
        "x",
        "H",
        "j",
        "five",
        "quiet",
        "batch_bases",
        "verbosity",
    }
)
_FLAG_PARAMS = {
    "mark_shorter": "-M",
    "soft_clip_supp": "-Y",
    "all": "-a",
    "S": "-S",
    "P": "-P",
    "j": "-j",
    "five": "-5",
    "quiet": "-q",
}
_VALUE_PARAMS = {
    "min_score": "-T",
    "k": "-k",
    "w": "-w",
    "A": "-A",
    "B": "-B",
    "O": "-O",
    "E": "-E",
    "L": "-L",
    "c": "-c",
    "D": "-D",
    "W": "-W",
    "m": "-m",
    "x": "-x",
    "H": "-H",
    "batch_bases": "-K",
    "verbosity": "-v",
}


class BwaAdapter(BaseAdapter):
    """BWA-MEM alignment with target indexes and SAM streaming."""

    spec = BackendSpec(
        name="bwa",
        family="bwa",
        summary="BWA-MEM alignment (SAM output)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA, InputKind.FASTQ}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            paired=True,
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="bwa",
                role="aligner",
                version_args=(),
                version_pattern=r"Version:\s*(\d+\.\d+\.\d+(?:-r\d+)?)",
                min_version=(0, 7, 17),
                hint="install BWA (conda-forge/bioconda package `bwa`)",
            ),
        ),
    )

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: params[key]
            for key in ("algorithm", "block_size")
            if key in params and params[key] is not None
        }

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        _validate_index_params(request.params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        merged = merge_fasta(request.target, index_dir / "targets.fasta")
        prefix = index_dir / "targets"
        argv: list[str] = ["bwa", "index"]
        if "algorithm" in request.params:
            argv.extend(["-a", str(request.params["algorithm"])])
        if "block_size" in request.params:
            argv.extend(["-b", str(request.params["block_size"])])
        argv.extend(["-p", str(prefix), str(merged)])
        return IndexPlan(
            commands=(CommandPlan(argv=tuple(argv), label="bwa index"),),
            entry="targets",
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
        algorithm = str(params.get("algorithm", "")).lower()
        if algorithm in {"aln", "bwasw", "mem"}:
            raise UnsupportedFeatureError(
                f"algorithm={algorithm!r} selects a bwa *index* construction "
                "method (bwa index -a); gseek only performs 'bwa mem' "
                "alignments",
                context={"algorithm": algorithm},
                hint="remove params['algorithm'] or pass an index algorithm like 'is'",
            )
        secondary = str(params.get("secondary", "no")).lower()
        if secondary not in {"no", "all"}:
            raise UnsupportedFeatureError(
                f"BWA secondary must be 'no' or 'all', got {secondary!r}",
                context={"secondary": secondary},
            )
        if index is None:
            raise UnsupportedFeatureError(
                "BWA requires a target index (index_policy='none' is not supported)",
                hint="use index='auto' or build the bwa index first",
            )

        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        argv: list[str] = [
            "bwa",
            "mem",
            "-t",
            str(max(1, request.threads)),
        ]
        for key, flag in _FLAG_PARAMS.items():
            if params.get(key):
                argv.append(flag)
        if "read_group" in params:
            argv.extend(["-R", str(params["read_group"])])
        for key, flag in _VALUE_PARAMS.items():
            if key in params and params[key] is not None:
                argv.extend([flag, str(params[key])])
        argv.append(str(index_dir / index.entry))
        argv.extend(str(path.resolve()) for path in request.query)
        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=tuple(argv),
                    label="bwa mem",
                    timeout_s=request.timeout_s,
                ),
            ),
            stream=True,
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
        secondary_mode = str(ctx.request.params.get("secondary", "no")).lower()
        target_filter = ctx.request.target_filter
        target_lengths: dict[str, int] = {}

        for line_number, line in _sam_lines(ctx, stream):
            if line.startswith("@"):
                _record_sq_header(line, target_lengths)
                continue
            fields = line.split("\t")
            if len(fields) < 11:
                raise ParseError(
                    f"expected >= 11 SAM fields, got {len(fields)}",
                    line_number=line_number,
                    raw_line=line,
                )
            try:
                flag = int(fields[1])
            except ValueError as exc:
                raise ParseError(
                    f"invalid SAM FLAG: {exc}",
                    line_number=line_number,
                    raw_line=line,
                ) from exc
            if flag & 0x4:
                continue
            target_id = fields[2]
            if target_id == "*":
                continue
            # Filter before the expensive CIGAR/tags work: secondary rows and
            # filtered targets would be discarded right after parsing anyway.
            if secondary_mode == "no" and (flag & 0x100):
                continue
            if target_filter is not None and target_id not in target_filter:
                continue
            try:
                pos = int(fields[3])
                mapq = int(fields[4])
            except ValueError as exc:
                raise ParseError(
                    f"invalid SAM POS/MAPQ: {exc}",
                    line_number=line_number,
                    raw_line=line,
                ) from exc
            cigar = fields[5]
            if cigar == "*":
                raise ParseError(
                    "mapped SAM row has no CIGAR",
                    line_number=line_number,
                    raw_line=line,
                )
            try:
                stats = parse_cigar(cigar)
            except ParseError as exc:
                raise ParseError(
                    f"invalid SAM CIGAR: {exc}",
                    line_number=line_number,
                    raw_line=line,
                ) from exc

            seq = fields[9]
            hard_clipped = stats.leading_hard_clip + stats.trailing_hard_clip
            query_length = len(seq) + hard_clipped if seq != "*" else None
            tags = parse_sam_tags(fields[11:])
            is_primary = not bool(flag & 0x100)
            is_supplementary = bool(flag & 0x800)

            strand = Strand.MINUS if flag & 0x10 else Strand.PLUS
            query_start: int | None = None
            query_end: int | None = None
            if query_length is not None:
                # Hard clips drop bases from SEQ: coordinates must be offset
                # back into the original read's coordinate system.
                if strand is Strand.MINUS:
                    query_start = stats.trailing_hard_clip + stats.trailing_soft_clip
                    query_end = query_length - (stats.leading_soft_clip + stats.leading_hard_clip)
                else:
                    query_start = stats.leading_hard_clip + stats.leading_soft_clip
                    query_end = query_length - (stats.trailing_soft_clip + stats.trailing_hard_clip)
                query_start = max(0, min(query_start, query_length))
                query_end = max(query_start, min(query_end, query_length))

            target_start = pos - 1 if pos > 0 else None
            target_end = (
                target_start + stats.reference_consumed if target_start is not None else None
            )
            identity = _identity_from_nm(stats.aligned_columns, tags.get("NM"))
            score = float_or_none(tags.get("AS"))
            record_tags = {"identity_method": "sam_nm"}
            for key in ("NM", "AS", "XS", "MD", "SA", "RG"):
                if key in tags:
                    record_tags[key] = tags[key]
            yield AlignmentRecord(
                query_id=fields[0],
                target_id=target_id,
                query_length=query_length,
                target_length=target_lengths.get(target_id),
                query_start=query_start,
                query_end=query_end,
                target_start=target_start,
                target_end=target_end,
                strand=strand,
                identity=identity,
                aligned_length=stats.aligned_columns,
                matches=stats.matches or None,
                gap_count=stats.gap_count,
                gap_openings=stats.gap_openings,
                mapq=mapq,
                score=score,
                is_primary=is_primary,
                is_supplementary=is_supplementary,
                tags=record_tags,
            )


def _record_sq_header(line: str, target_lengths: dict[str, int]) -> None:
    if not line.startswith("@SQ\t"):
        return
    fields = line.split("\t")
    name = next((field[3:] for field in fields if field.startswith("SN:")), None)
    length_raw = next((field[3:] for field in fields if field.startswith("LN:")), None)
    if name is None or length_raw is None:
        return
    try:
        target_lengths[name] = int(length_raw)
    except ValueError:
        return


def _identity_from_nm(aligned_columns: int, nm_raw: str | None) -> float | None:
    if aligned_columns <= 0 or nm_raw is None:
        return None
    try:
        edits = int(nm_raw)
    except ValueError:
        return None
    matches = max(0, aligned_columns - edits)
    return 100.0 * matches / aligned_columns


def _validate_index_params(params: Mapping[str, JsonValue]) -> None:
    unknown = set(params) - _ALLOWED_INDEX_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported BWA index parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )


def _validate_align_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_ALIGN_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported BWA parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


def _sam_lines(
    ctx: ParseContext,
    stream: Iterable[str] | None,
) -> Iterator[tuple[int, str]]:
    if stream is not None:
        for line_number, raw in enumerate(stream, start=1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            yield line_number, line
        return
    for path in ctx.plan.result_files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                yield line_number, line


ADAPTER = BwaAdapter

__all__ = ["ADAPTER", "BwaAdapter"]
