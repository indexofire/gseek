"""NCBI BLAST+ adapter for nucleotide and protein search tasks."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    BaseAdapter,
    DistanceRequest,
    IndexPlan,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.base import finite_float, iter_data_lines, result_lines
from gseek.capabilities import BackendSpec, Capabilities, ToolchainProbe, ToolRequirement
from gseek.errors import ParseError, ToolNotFoundError, ToolVersionError, UnsupportedFeatureError
from gseek.execution import CommandPlan, version_at_least
from gseek.io import merge_fasta
from gseek.model import AlignmentRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, Molecule, OutputKind, Strand

_OUTFMT_FIELDS = (
    "qseqid",
    "sseqid",
    "pident",
    "length",
    "mismatch",
    "gapopen",
    "gaps",
    "qstart",
    "qend",
    "sstart",
    "send",
    "evalue",
    "bitscore",
    "qlen",
    "slen",
    "stitle",
)
_OUTFMT = "6 " + " ".join(_OUTFMT_FIELDS)


def _outfmt(include_sseq: bool) -> str:
    return _OUTFMT + (" sseq" if include_sseq else "")


_SUPPORTED_TASKS = frozenset(
    {
        "blastn",
        "megablast",
        "dc-megablast",
        "blastp",
        "blastx",
        "blastx-fast",
        "tblastn",
        "tblastx",
    }
)
_TASK_TO_BINARY = {
    "blastn": "blastn",
    "megablast": "blastn",
    "dc-megablast": "blastn",
    "blastp": "blastp",
    "blastx": "blastx",
    "blastx-fast": "blastx",
    "tblastn": "tblastn",
    "tblastx": "tblastx",
}
_TASK_TO_DBTYPE = {
    "blastn": "nucl",
    "megablast": "nucl",
    "dc-megablast": "nucl",
    "blastp": "prot",
    "blastx": "prot",
    "blastx-fast": "prot",
    "tblastn": "nucl",
    "tblastx": "nucl",
}
_PROTEIN_TASKS = frozenset({"blastp", "blastx", "blastx-fast", "tblastn", "tblastx"})
_INDEX_ARTIFACTS = {
    "nucl": ("targets.nhr", "targets.nin", "targets.nsq"),
    "prot": ("targets.phr", "targets.pin", "targets.psq"),
}
_ALLOWED_PARAMS = frozenset(
    {
        "task",
        "dbtype",
        "evalue",
        "max_targets",
        "max_target_seqs",
        "min_identity",
        "perc_identity",
        "dust",
        "seg",
        "culling_limit",
        "max_hsps",
        "qcov_hsp_perc",
        "strand",
        "word_size",
        "threads",
        "sseq",
    }
)
_DEFAULT_EVALUE = 1e-5
_DEFAULT_MAX_TARGETS = 500


class BlastAdapter(BaseAdapter):
    """blastn against a reusable makeblastdb nucleotide index."""

    spec = BackendSpec(
        name="blast",
        family="blast",
        summary="NCBI BLAST+ nucleotide/protein alignment",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            molecule=frozenset({Molecule.DNA, Molecule.PROTEIN}),
            paired=False,
            index_scope=IndexScope.TARGET,
            streaming=True,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="blastn",
                role="aligner",
                version_args=("-version",),
                version_pattern=r"blastn:\s*(\d+)\.(\d+)\.(\d+)",
                min_version=(2, 2, 30),
                hint="install BLAST+ (conda-forge/bioconda package `blast`)",
            ),
            ToolRequirement(
                binary="makeblastdb",
                role="indexer",
                version_args=("-version",),
                version_pattern=r"makeblastdb:\s*(\d+)\.(\d+)\.(\d+)",
                min_version=(2, 2, 30),
                hint="install BLAST+ (conda-forge/bioconda package `blast`)",
            ),
            *(
                ToolRequirement(
                    binary=binary,
                    role="aligner",
                    version_args=("-version",),
                    version_pattern=rf"{binary}:\s*(\d+)\.(\d+)\.(\d+)",
                    min_version=(2, 2, 30),
                    optional=True,
                    hint="install BLAST+ (conda-forge/bioconda package `blast`)",
                )
                for binary in ("blastp", "blastx", "tblastn", "tblastx")
            ),
        ),
        aliases=("blastn", "blast+"),
    )

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {"dbtype": _dbtype_for(params)}

    def validate_tools(
        self,
        request: AlignRequest | DistanceRequest,
        probe: ToolchainProbe,
    ) -> None:
        """Gate the task binary: optional spec tools bypass the strict probe."""
        assert isinstance(request, AlignRequest)
        task = _task_from(_validated_params(request.params))
        binary = _TASK_TO_BINARY[task]
        tool = next((entry for entry in probe.tools if entry.name == binary), None)
        if tool is None or tool.path is None:
            raise ToolNotFoundError(
                f"BLAST task {task!r} requires the {binary!r} binary",
                hint="install BLAST+ (conda-forge/bioconda package `blast`)",
                context={"backend": "blast", "task": task, "binary": binary},
            )
        if not tool.ok:
            raise ToolVersionError(
                f"BLAST task {task!r} requires a working {binary!r}: {tool.error}",
                hint="install BLAST+ (conda-forge/bioconda package `blast`)",
                context={"backend": "blast", "task": task, "binary": binary},
            )
        minimum = (2, 11, 0) if task.endswith("-fast") else (2, 2, 30)
        if not version_at_least(tool.version, minimum):
            raise ToolVersionError(
                f"BLAST task {task!r} needs {binary} >= {'.'.join(map(str, minimum))}, "
                f"found {tool.version or 'unknown'}",
                hint="upgrade BLAST+ to 2.11+ for -fast tasks",
                context={"backend": "blast", "task": task, "binary": binary},
            )

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        params = _validated_params(request.params)
        dbtype = _dbtype_for(params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        merged = merge_fasta(request.target, index_dir / "targets.fasta", on_duplicate="allow")
        prefix = index_dir / "targets"
        command = CommandPlan(
            argv=(
                "makeblastdb",
                "-in",
                str(merged),
                "-dbtype",
                dbtype,
                "-out",
                str(prefix),
                "-title",
                "gseek_targets",
            ),
            label="makeblastdb",
            timeout_s=None,
        )
        return IndexPlan(
            commands=(command,),
            entry="targets",
            artifacts=_INDEX_ARTIFACTS[dbtype],
            params={"dbtype": dbtype},
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
        task = _task_from(params)
        binary = _TASK_TO_BINARY[task]
        expected_dbtype = _TASK_TO_DBTYPE[task]
        if index is not None:
            index_dbtype = str(index.params.get("dbtype", expected_dbtype)).lower()
            if index_dbtype != expected_dbtype:
                raise UnsupportedFeatureError(
                    f"BLAST task {task!r} needs a {expected_dbtype} index, got {index_dbtype}",
                    context={"task": task, "dbtype": index_dbtype},
                )
            index_dir = (ctx.index_dir or ctx.workspace).resolve()
            db_path = index_dir / index.entry
            subject_args = ("-db", str(db_path))
        else:
            merged = merge_fasta(request.target, ctx.path("targets.fasta"), on_duplicate="allow")
            subject_args = ("-subject", str(merged.resolve()))

        argv: list[str] = [
            binary,
            "-query",
            str(request.query[0].resolve()),
            *subject_args,
            "-outfmt",
            _outfmt(bool(params.get("sseq"))),
            "-evalue",
            str(params.get("evalue", _DEFAULT_EVALUE)),
            "-max_target_seqs",
            str(params.get("max_targets", params.get("max_target_seqs", _DEFAULT_MAX_TARGETS))),
            "-num_threads",
            str(max(1, request.threads)),
        ]

        # Pass -task explicitly where supported: blastn defaults to megablast,
        # so omitting it makes task="blastn" and task="megablast"
        # indistinguishable. tblastx has no -task option.
        if task != "tblastx":
            argv.extend(["-task", task])

        if task in _PROTEIN_TASKS:
            seg = params.get("seg", False)
            if isinstance(seg, bool):
                if not seg:
                    argv.extend(["-seg", "no"])
            else:
                argv.extend(["-seg", str(seg)])
        else:
            dust = params.get("dust", False)
            if isinstance(dust, bool):
                if not dust:
                    argv.extend(["-dust", "no"])
            else:
                argv.extend(["-dust", str(dust)])

        min_identity_raw = params.get("min_identity", params.get("perc_identity", 0))
        try:
            min_identity = float(str(min_identity_raw))
        except (TypeError, ValueError) as exc:
            raise UnsupportedFeatureError(
                f"min_identity must be numeric, got {min_identity_raw!r}",
                context={"min_identity": str(min_identity_raw)},
            ) from exc
        if min_identity > 0:
            if task in _PROTEIN_TASKS:
                raise UnsupportedFeatureError(
                    f"min_identity is only supported for nucleotide BLAST tasks, not {task!r}",
                    context={"task": task, "min_identity": str(min_identity_raw)},
                )
            argv.extend(["-perc_identity", str(min_identity_raw)])
        for key, flag in (
            ("culling_limit", "-culling_limit"),
            ("max_hsps", "-max_hsps"),
            ("qcov_hsp_perc", "-qcov_hsp_perc"),
            ("strand", "-strand"),
            ("word_size", "-word_size"),
        ):
            if key in params and params[key] is not None:
                argv.extend([flag, str(params[key])])

        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=tuple(argv),
                    label=f"{binary} align",
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

        lines = result_lines(ctx, stream)
        path = str(ctx.plan.result_files[0]) if ctx.plan.result_files else None
        for line_number, line in iter_data_lines(lines, skip_prefixes=("#",)):
            fields = line.split("\t")
            if len(fields) < len(_OUTFMT_FIELDS) - 1:
                raise ParseError(
                    f"expected >= {len(_OUTFMT_FIELDS) - 1} BLAST fields, got {len(fields)}",
                    path=path,
                    line_number=line_number,
                    raw_line=line,
                )
            try:
                qseqid = fields[0]
                sseqid = fields[1]
                pident = finite_float(fields[2])
                length = int(fields[3])
                mismatch = int(fields[4])
                gapopen = int(fields[5])
                gaps = int(fields[6])
                qstart = int(fields[7])
                qend = int(fields[8])
                sstart = int(fields[9])
                send = int(fields[10])
                evalue = finite_float(fields[11])
                bitscore = finite_float(fields[12])
                qlen = int(fields[13])
                slen = int(fields[14])
                # The parser knows whether sseq was requested (the plan built
                # the outfmt from the same flag); trusting the column count
                # instead would let unescaped tabs in stitle leak title text
                # into target_aligned.
                if bool(ctx.request.params.get("sseq")):
                    stitle = "\t".join(fields[15:-1]).strip() or None
                    sseq = fields[-1].strip() or None
                else:
                    stitle = "\t".join(fields[15:]).strip() or None
                    sseq = None
            except (TypeError, ValueError) as exc:
                raise ParseError(
                    f"invalid BLAST numeric field: {exc}",
                    path=path,
                    line_number=line_number,
                    raw_line=line,
                ) from exc

            if target_filter is not None and sseqid not in target_filter:
                continue

            strand = Strand.MINUS if sstart > send else Strand.PLUS
            yield AlignmentRecord(
                query_id=qseqid,
                target_id=sseqid,
                query_length=qlen,
                target_length=slen,
                query_start=min(qstart, qend) - 1,
                query_end=max(qstart, qend),
                target_start=min(sstart, send) - 1,
                target_end=max(sstart, send),
                strand=strand,
                identity=pident,
                aligned_length=length,
                matches=max(0, length - mismatch - gaps),
                mismatches=mismatch,
                gap_count=gaps,
                gap_openings=gapopen,
                evalue=evalue,
                bit_score=bitscore,
                target_description=stitle,
                target_aligned=sseq,
                tags={"identity_method": "blast_pident"},
            )


def _validated_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported blast parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    _task_from(params)
    _dbtype_for(params)
    return {str(key): value for key, value in params.items()}


def _task_from(params: Mapping[str, JsonValue]) -> str:
    task = str(params.get("task", "blastn")).lower()
    if task not in _SUPPORTED_TASKS:
        raise UnsupportedFeatureError(
            f"unsupported BLAST task {task!r}; expected one of {sorted(_SUPPORTED_TASKS)}",
            context={"task": task},
        )
    return task


def _dbtype_for(params: Mapping[str, JsonValue]) -> str:
    task = _task_from(params)
    expected = _TASK_TO_DBTYPE[task]
    explicit = params.get("dbtype")
    if explicit is not None and str(explicit).lower() != expected:
        raise UnsupportedFeatureError(
            f"BLAST task {task!r} requires dbtype={expected!r}, got {str(explicit)!r}",
            context={"task": task, "dbtype": str(explicit)},
        )
    return expected


ADAPTER = BlastAdapter

__all__ = ["ADAPTER", "BlastAdapter"]
