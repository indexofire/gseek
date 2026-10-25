"""minimap2 adapter (PAF output, FASTA/FASTQ, optional paired reads)."""

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
from gseek.adapters.base import iter_data_lines, parse_cigar, parse_tag_fields
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import merge_fasta
from gseek.model import AlignmentRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand

_PRESETS = ("sr", "map-ont", "map-hifi", "asm5", "asm10", "asm20")
_INDEX_FLAGS = {"k": "-k", "w": "-w", "H": "-H", "I": "-I"}
_ALIGN_FLAGS = {
    "k": "-k",
    "w": "-w",
    "f": "-f",
    "g": "-g",
    "G": "-G",
    "A": "-A",
    "B": "-B",
    "O": "-O",
    "E": "-E",
    "z": "-z",
    "s": "-s",
    "N": "-N",
    "p": "-p",
    "m": "-m",
    "n": "-n",
    "r": "-r",
    "e": "-e",
    "U": "-U",
    "K": "-K",
    "I": "-I",
    "H": "-H",
}
_ALLOWED_PARAMS = frozenset(
    {
        "preset",
        "secondary",
        "cigar",
        "cs",
        "md",
        "eqx",
        "threads",
        *_ALIGN_FLAGS,
    }
)


class Minimap2Adapter(BaseAdapter):
    """minimap2 PAF alignment with explicit presets."""

    spec = BackendSpec(
        name="minimap2",
        family="minimap2",
        summary="minimap2 PAF alignment (short/long reads and assemblies)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA, InputKind.FASTQ}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            paired=True,
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
            presets=_PRESETS,
        ),
        tools=(
            ToolRequirement(
                binary="minimap2",
                role="aligner",
                version_args=("--version",),
                version_pattern=r"(\d+\.\d+(?:-r\d+)?)",
                min_version=(2, 24),
                hint="install minimap2 (conda-forge/bioconda package `minimap2`)",
            ),
        ),
        aliases=("mm2",),
    )

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        preset = _require_preset(params)
        result: dict[str, JsonValue] = {"preset": preset}
        for key in _INDEX_FLAGS:
            if key in params and params[key] is not None:
                result[key] = params[key]
        return result

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        params = _validated_params(request.params)
        preset = _require_preset(params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        merged = merge_fasta(request.target, index_dir / "targets.fasta", on_duplicate="allow")
        index_path = index_dir / "targets.mmi"
        argv: list[str] = ["minimap2", "-x", preset]
        for key, flag in _INDEX_FLAGS.items():
            if key in params and params[key] is not None:
                argv.extend([flag, str(params[key])])
        argv.extend(["-d", str(index_path), str(merged)])
        return IndexPlan(
            commands=(CommandPlan(argv=tuple(argv), label="minimap2 index"),),
            entry="targets.mmi",
            artifacts=("targets.mmi",),
            params=self.index_params(params),
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
        params = _validated_params(request.params)
        preset = _require_preset(params)
        secondary = str(params.get("secondary", "no")).lower()
        if secondary not in {"no", "all"}:
            raise UnsupportedFeatureError(
                f"minimap2 secondary must be 'no' or 'all', got {secondary!r}",
                context={"secondary": secondary},
            )

        if index is not None:
            index_dir = (ctx.index_dir or ctx.workspace).resolve()
            subject = str(index_dir / index.entry)
        else:
            subject = str(
                merge_fasta(
                    request.target,
                    ctx.path("targets.fasta"),
                    on_duplicate="allow",
                ).resolve()
            )

        argv: list[str] = [
            "minimap2",
            "-x",
            preset,
            "-t",
            str(max(1, request.threads)),
        ]
        if secondary == "no":
            argv.append("--secondary=no")
        if params.get("cigar"):
            argv.append("-c")
        if params.get("cs"):
            cs = params["cs"]
            argv.append("--cs" if cs is True else f"--cs={cs}")
        if params.get("md"):
            argv.append("--MD")
        if params.get("eqx"):
            argv.append("--eqx")
        for key, flag in _ALIGN_FLAGS.items():
            if key in params and params[key] is not None:
                argv.extend([flag, str(params[key])])
        argv.append(subject)
        argv.extend(str(path.resolve()) for path in request.query)
        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=tuple(argv),
                    label="minimap2 align",
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
        secondary_mode = str(ctx.request.params.get("secondary", "no")).lower()
        target_filter = ctx.request.target_filter
        path = str(ctx.plan.result_files[0]) if ctx.plan.result_files else None
        for line_number, line in iter_data_lines(_result_lines(ctx, stream), skip_prefixes=("#",)):
            fields = line.split("\t")
            if len(fields) < 12:
                raise ParseError(
                    f"expected >= 12 PAF fields, got {len(fields)}",
                    path=path,
                    line_number=line_number,
                    raw_line=line,
                )
            try:
                qname = fields[0]
                qlen = int(fields[1])
                qstart = int(fields[2])
                qend = int(fields[3])
                strand_value = fields[4]
                if strand_value not in {"+", "-"}:
                    raise ValueError(f"invalid PAF strand {strand_value!r}")
                strand = Strand(strand_value)
                tname = fields[5]
                tlen = int(fields[6])
                tstart = int(fields[7])
                tend = int(fields[8])
                nmatch = int(fields[9])
                alen = int(fields[10])
                mapq = int(fields[11])
            except (TypeError, ValueError) as exc:
                raise ParseError(
                    f"invalid PAF row: {exc}",
                    path=path,
                    line_number=line_number,
                    raw_line=line,
                ) from exc

            tags = parse_tag_fields(fields[12:])
            is_primary = tags.get("tp", "P") == "P"
            if secondary_mode == "no" and not is_primary:
                continue
            if target_filter is not None and tname not in target_filter:
                continue

            gap_count: int | None = None
            gap_openings: int | None = None
            cigar = tags.get("cg")
            if cigar:
                try:
                    stats = parse_cigar(cigar)
                except ParseError as exc:
                    raise ParseError(
                        f"invalid PAF cg tag: {exc}",
                        path=path,
                        line_number=line_number,
                        raw_line=line,
                    ) from exc
                gap_count = stats.gap_count
                gap_openings = stats.gap_openings

            identity: float | None = None
            identity_method = "paf_matches"
            if alen > 0:
                nm_raw = tags.get("NM")
                if nm_raw is None:
                    identity = 100.0 * nmatch / alen
                else:
                    try:
                        edits = int(nm_raw)
                    except ValueError as exc:
                        raise ParseError(
                            f"invalid PAF NM tag: {exc}",
                            path=path,
                            line_number=line_number,
                            raw_line=line,
                        ) from exc
                    if edits < 0:
                        raise ParseError(
                            f"PAF NM tag must be non-negative, got {edits}",
                            path=path,
                            line_number=line_number,
                            raw_line=line,
                        )
                    identity = 100.0 * max(0, alen - edits) / alen
                    identity_method = "paf_nm"
            score = _float_or_none(tags.get("AS"))
            record_tags = {"identity_method": identity_method}
            for key in ("tp", "NM", "AS", "ms", "cg", "cs", "de", "rl"):
                if key in tags:
                    record_tags[key] = tags[key]
            yield AlignmentRecord(
                query_id=qname,
                target_id=tname,
                query_length=qlen,
                target_length=tlen,
                query_start=qstart,
                query_end=qend,
                target_start=tstart,
                target_end=tend,
                strand=strand,
                identity=identity,
                aligned_length=alen,
                matches=nmatch,
                gap_count=gap_count,
                gap_openings=gap_openings,
                mapq=mapq,
                score=score,
                is_primary=is_primary,
                tags=record_tags,
            )


def _require_preset(params: Mapping[str, JsonValue]) -> str:
    raw = params.get("preset")
    if raw is None:
        raise UnsupportedFeatureError(
            "minimap2 requires params['preset'] (e.g. 'sr', 'map-ont', 'asm5', 'asm20')",
            context={"backend": "minimap2"},
        )
    preset = str(raw)
    if preset not in _PRESETS:
        raise UnsupportedFeatureError(
            f"unknown minimap2 preset {preset!r}; expected one of {_PRESETS}",
            context={"preset": preset},
        )
    return preset


def _validated_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported minimap2 parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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


ADAPTER = Minimap2Adapter

__all__ = ["ADAPTER", "Minimap2Adapter"]
