from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.blast import BlastAdapter
from gseek.capabilities import Capabilities
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">arcC_1\nACGTACGT\n>arcC_2\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path) -> Path:
    path = tmp_path / "query.fa"
    path.write_text(">contig1\nACGTACGT\n", encoding="utf-8")
    return path


def _index() -> IndexRef:
    return IndexRef(
        key="a" * 64,
        adapter="blast",
        adapter_version="1",
        entry="targets",
        source_sha256="b" * 64,
    )


def _plan() -> AlignPlan:
    return AlignPlan(commands=(), stream=True)


def _parse(
    lines: list[str],
    *,
    params: Mapping[str, JsonValue] | None = None,
    target_filter: frozenset[str] | None = None,
    tmp_path: Path,
):
    query = _query(tmp_path)
    target = _targets(tmp_path)
    request = AlignRequest(
        query=(query,),
        target=(target,),
        params=params or {},
        target_filter=target_filter,
    )
    ctx = ParseContext(
        request=request,
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    return list(BlastAdapter().parse_align(ctx, iter(lines)))


def _row(
    qseqid: str = "contig1",
    sseqid: str = "arcC_1",
    pident: str = "100.000",
    length: str = "480",
    mismatch: str = "0",
    gapopen: str = "0",
    gaps: str = "0",
    qstart: str = "1",
    qend: str = "480",
    sstart: str = "100",
    send: str = "579",
    evalue: str = "0.0",
    bitscore: str = "900",
    qlen: str = "480",
    slen: str = "480",
    stitle: str = "allele one",
) -> str:
    return "\t".join(
        [
            qseqid,
            sseqid,
            pident,
            length,
            mismatch,
            gapopen,
            gaps,
            qstart,
            qend,
            sstart,
            send,
            evalue,
            bitscore,
            qlen,
            slen,
            stitle,
        ]
    )


def test_spec_declares_blastn_and_makeblastdb() -> None:
    spec = BlastAdapter.spec
    assert spec.name == "blast"
    assert "blastn" in spec.aliases
    caps = spec.capabilities
    assert isinstance(caps, Capabilities)
    assert caps.inputs == frozenset({InputKind.FASTA})
    assert caps.outputs == frozenset({OutputKind.ALIGNMENT})
    assert caps.index_scope is IndexScope.TARGET
    tools = {tool.binary: tool for tool in spec.tools}
    assert tools["blastn"].min_version == (2, 2, 30)
    assert tools["makeblastdb"].role == "indexer"
    assert tools["blastp"].optional is True
    assert tools["tblastx"].version_args == ("-version",)


def test_plan_index_merges_targets_and_builds_blast_db(tmp_path: Path) -> None:
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    ctx = PlanContext(workspace=index_dir, index_dir=index_dir)
    plan = BlastAdapter().plan_index(IndexRequest(target=(target,)), ctx)
    assert plan is not None
    assert plan.entry == "targets"
    assert "targets.nhr" in plan.artifacts
    assert len(plan.commands) == 1
    argv = plan.commands[0].argv
    assert argv[0] == "makeblastdb"
    assert "-dbtype" in argv and "nucl" in argv
    merged = Path(argv[argv.index("-in") + 1])
    assert merged.exists()
    assert "arcC_1" in merged.read_text(encoding="utf-8")
    assert Path(argv[argv.index("-out") + 1]).name == "targets"


def test_plan_align_uses_db_and_streams_blastn(tmp_path: Path) -> None:
    query = _query(tmp_path)
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    request = AlignRequest(
        query=(query,),
        target=(target,),
        params={"evalue": 1e-10},
        threads=4,
    )
    plan = BlastAdapter().plan_align(
        request,
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert plan.stream is True
    argv = plan.commands[0].argv
    assert argv[0] == "blastn"
    assert "-query" in argv
    assert "-db" in argv
    assert "-subject" not in argv
    assert argv[argv.index("-num_threads") + 1] == "4"
    assert argv[argv.index("-evalue") + 1] == "1e-10"
    assert "-dust" in argv and argv[argv.index("-dust") + 1] == "no"
    outfmt = argv[argv.index("-outfmt") + 1]
    for field in ("qseqid", "sseqid", "pident", "gaps", "stitle"):
        assert field in outfmt


def test_plan_align_without_index_uses_subject(tmp_path: Path) -> None:
    query = _query(tmp_path)
    target = _targets(tmp_path)
    request = AlignRequest(query=(query,), target=(target,))
    plan = BlastAdapter().plan_align(
        request,
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert "-subject" in argv
    merged = Path(argv[argv.index("-subject") + 1])
    assert merged.exists()
    assert "arcC_2" in merged.read_text(encoding="utf-8")


def test_parse_forward_reverse_and_gapped_rows(tmp_path: Path) -> None:
    lines = [
        _row(),
        _row(
            qseqid="contig2",
            sseqid="arcC_2",
            pident="99.792",
            mismatch="1",
            qstart="1",
            qend="480",
            sstart="979",
            send="500",
            stitle="allele two",
        ),
        _row(
            qseqid="contig3",
            sseqid="arcC_3",
            pident="95.000",
            length="100",
            mismatch="3",
            gapopen="1",
            gaps="2",
            qstart="1",
            qend="100",
            sstart="50",
            send="149",
            evalue="1e-5",
            bitscore="200",
            qlen="100",
            slen="100",
            stitle="gapped",
        ),
    ]
    records = _parse(lines, tmp_path=tmp_path)
    assert len(records) == 3

    forward = records[0]
    assert forward.query_id == "contig1"
    assert forward.target_id == "arcC_1"
    assert forward.strand is Strand.PLUS
    assert forward.query_start == 0
    assert forward.query_end == 480
    assert forward.target_start == 99
    assert forward.target_end == 579
    assert forward.identity == 100.0
    assert forward.matches == 480
    assert forward.mismatches == 0
    assert forward.gap_count == 0
    assert forward.gap_openings == 0
    assert forward.query_coverage == 1.0
    assert forward.target_coverage == 1.0
    assert forward.target_description == "allele one"
    assert forward.tags["identity_method"] == "blast_pident"

    reverse = records[1]
    assert reverse.strand is Strand.MINUS
    assert reverse.target_start == 499
    assert reverse.target_end == 979
    assert reverse.query_start == 0

    gapped = records[2]
    assert gapped.matches == 95
    assert gapped.mismatches == 3
    assert gapped.gap_count == 2
    assert gapped.ungapped_target_coverage == pytest.approx(0.98)


def test_parser_filters_target_ids(tmp_path: Path) -> None:
    lines = [
        _row(sseqid="arcC_1"),
        _row(qseqid="contig2", sseqid="arcC_2"),
    ]
    records = _parse(lines, target_filter=frozenset({"arcC_2"}), tmp_path=tmp_path)
    assert [record.target_id for record in records] == ["arcC_2"]


def test_parser_rejects_malformed_rows(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        _parse(["too\tfew\tfields"], tmp_path=tmp_path)


def test_unsupported_task_and_input_kind(tmp_path: Path) -> None:
    adapter = BlastAdapter()
    fastq = tmp_path / "reads.fq"
    fastq.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    with pytest.raises(UnsupportedFeatureError):
        adapter.check_align_request(
            AlignRequest(
                query=(fastq,),
                target=(_targets(tmp_path),),
                query_kind=InputKind.FASTQ,
            )
        )
    with pytest.raises(UnsupportedFeatureError):
        adapter.check_align_request(
            AlignRequest(
                query=(fastq, fastq),
                target=(_targets(tmp_path),),
                query_kind=InputKind.FASTA,
                paired=True,
            )
        )

    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"task": "blastz"},
    )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(request, None, PlanContext(workspace=tmp_path / "work"))


def test_index_and_semantic_params(tmp_path: Path) -> None:
    adapter = BlastAdapter()
    assert adapter.index_params({"threads": 8, "evalue": 1e-5}) == {"dbtype": "nucl"}
    assert adapter.index_params({"task": "blastp"}) == {"dbtype": "prot"}
    assert adapter.index_params({"task": "blastx"}) == {"dbtype": "prot"}
    assert adapter.index_params({"task": "tblastn"}) == {"dbtype": "nucl"}
    assert adapter.index_params({"task": "tblastx"}) == {"dbtype": "nucl"}
    semantic = adapter.semantic_params({"threads": 8, "evalue": 1e-5, "debug": True})
    assert semantic == {"evalue": 1e-5}


@pytest.mark.parametrize(
    ("task", "binary", "dbtype", "artifacts"),
    [
        ("blastp", "blastp", "prot", ("targets.phr", "targets.pin", "targets.psq")),
        ("blastx", "blastx", "prot", ("targets.phr", "targets.pin", "targets.psq")),
        ("tblastn", "tblastn", "nucl", ("targets.nhr", "targets.nin", "targets.nsq")),
        ("tblastx", "tblastx", "nucl", ("targets.nhr", "targets.nin", "targets.nsq")),
    ],
)
def test_protein_tasks_plan_binary_dbtype_and_artifacts(
    task: str,
    binary: str,
    dbtype: str,
    artifacts: tuple[str, ...],
    tmp_path: Path,
) -> None:
    adapter = BlastAdapter()
    index_plan = adapter.plan_index(
        IndexRequest(target=(_targets(tmp_path),), params={"task": task}),
        PlanContext(workspace=tmp_path / "index", index_dir=tmp_path / "index"),
    )
    assert index_plan is not None
    assert index_plan.artifacts == artifacts
    index_argv = index_plan.commands[0].argv
    assert index_argv[index_argv.index("-dbtype") + 1] == dbtype

    plan = adapter.plan_align(
        AlignRequest(
            query=(_query(tmp_path),),
            target=(_targets(tmp_path),),
            params={"task": task},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert argv[0] == binary
    if task == "tblastx":
        assert "-task" not in argv
    else:
        assert argv[argv.index("-task") + 1] == task
    if task in {"blastp", "blastx", "tblastn", "tblastx"}:
        assert "-seg" in argv
        assert argv[argv.index("-seg") + 1] == "no"
        assert "-dust" not in argv
    else:
        assert "-dust" in argv
        assert argv[argv.index("-dust") + 1] == "no"


def test_blastn_passes_explicit_task_flag(tmp_path: Path) -> None:
    plan = BlastAdapter().plan_align(
        AlignRequest(query=(_query(tmp_path),), target=(_targets(tmp_path),)),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert argv[0] == "blastn"
    assert argv[argv.index("-task") + 1] == "blastn"


def test_protein_tasks_reject_percent_identity_filter(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFeatureError):
        BlastAdapter().plan_align(
            AlignRequest(
                query=(_query(tmp_path),),
                target=(_targets(tmp_path),),
                params={"task": "blastp", "min_identity": 90},
            ),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )


def test_megablast_uses_blastn_with_task_flag(tmp_path: Path) -> None:
    plan = BlastAdapter().plan_align(
        AlignRequest(
            query=(_query(tmp_path),),
            target=(_targets(tmp_path),),
            params={"task": "megablast"},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert argv[0] == "blastn"
    assert argv[argv.index("-task") + 1] == "megablast"


def test_dbtype_must_match_task(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFeatureError):
        BlastAdapter().index_params({"task": "blastp", "dbtype": "nucl"})
    with pytest.raises(UnsupportedFeatureError):
        BlastAdapter().plan_index(
            IndexRequest(target=(_targets(tmp_path),), params={"dbtype": "prot"}),
            PlanContext(workspace=tmp_path / "index", index_dir=tmp_path / "index"),
        )


def test_plan_align_rejects_preset(tmp_path: Path) -> None:
    adapter = BlastAdapter()
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"preset": "asm5"},
    )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(request, None, PlanContext(workspace=tmp_path / "work"))
