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
from gseek.adapters.bwa import BwaAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand

_SEQ17 = "ACGTACGTACGTACGTA"
_SEQ15 = "ACGTACGTACGTACG"
_SEQ10 = "ACGTACGTAC"


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">t1\nACGTACGT\n>t2\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path, name: str = "query.fa") -> Path:
    path = tmp_path / name
    path.write_text(">q1\nACGTACGT\n", encoding="utf-8")
    return path


def _index() -> IndexRef:
    return IndexRef(
        key="1" * 64,
        adapter="bwa",
        adapter_version="1",
        entry="targets",
        source_sha256="2" * 64,
    )


def _plan() -> AlignPlan:
    return AlignPlan(commands=(), stream=True)


def _parse(
    lines: list[str],
    *,
    params: Mapping[str, JsonValue] | None = None,
    target_filter: frozenset[str] | None = None,
    tmp_path: Path,
) -> list:
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params=params or {},
        target_filter=target_filter,
    )
    ctx = ParseContext(
        request=request,
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    return list(BwaAdapter().parse_align(ctx, iter(lines)))


def _sam(
    qname: str = "q1",
    flag: int = 0,
    rname: str = "t1",
    pos: int = 101,
    mapq: int = 60,
    cigar: str = "10M2I3D5M",
    seq: str = _SEQ17,
    tags: tuple[str, ...] = (),
) -> str:
    return "\t".join(
        [
            qname,
            str(flag),
            rname,
            str(pos),
            str(mapq),
            cigar,
            "*",
            "0",
            "0",
            seq,
            "*",
            *tags,
        ]
    )


def test_spec_declares_bwa_capabilities() -> None:
    spec = BwaAdapter.spec
    assert spec.name == "bwa"
    assert spec.capabilities.inputs == frozenset({InputKind.FASTA, InputKind.FASTQ})
    assert spec.capabilities.outputs == frozenset({OutputKind.ALIGNMENT})
    assert spec.capabilities.index_scope is IndexScope.TARGET
    assert spec.capabilities.paired is True
    tool = next(tool for tool in spec.tools if tool.binary == "bwa")
    assert tool.version_pattern == r"Version:\s*(\d+\.\d+\.\d+(?:-r\d+)?)"
    assert tool.min_version == (0, 7, 17)


def test_plan_index_builds_bwa_prefix(tmp_path: Path) -> None:
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    plan = BwaAdapter().plan_index(
        IndexRequest(target=(target,), params={"algorithm": "bwtsw"}),
        PlanContext(workspace=index_dir, index_dir=index_dir),
    )
    assert plan is not None
    assert plan.entry == "targets"
    assert plan.artifacts == (
        "targets.amb",
        "targets.ann",
        "targets.bwt",
        "targets.pac",
        "targets.sa",
    )
    argv = plan.commands[0].argv
    assert argv[0] == "bwa"
    assert argv[1] == "index"
    assert argv[argv.index("-a") + 1] == "bwtsw"
    merged = Path(argv[argv.index("-p") + 1])
    assert merged.name == "targets"
    assert "-p" in argv


def test_plan_align_streams_sam(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"mark_shorter": True, "read_group": "@RG\\tID:x\\tSM:s"},
        threads=3,
    )
    plan = BwaAdapter().plan_align(
        request,
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert plan.stream is True
    argv = plan.commands[0].argv
    assert argv[:3] == ("bwa", "mem", "-t")
    assert argv[argv.index("-t") + 1] == "3"
    assert "-M" in argv
    assert "-R" in argv
    assert str(index_dir / "targets") in argv
    assert str(_query(tmp_path)) in argv


def test_plan_align_accepts_index_only_params(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    plan = BwaAdapter().plan_align(
        AlignRequest(
            query=(_query(tmp_path),),
            target=(_targets(tmp_path),),
            params={"algorithm": "bwtsw", "block_size": 500000},
        ),
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    argv = plan.commands[0].argv
    assert "-a" not in argv
    assert "-b" not in argv


def test_plan_align_paired(tmp_path: Path) -> None:
    r1 = _query(tmp_path, "r1.fq")
    r2 = _query(tmp_path, "r2.fq")
    plan = BwaAdapter().plan_align(
        AlignRequest(
            query=(r1, r2),
            target=(_targets(tmp_path),),
            query_kind=InputKind.FASTQ,
            paired=True,
        ),
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=tmp_path / "index"),
    )
    assert plan.commands[0].argv[-2:] == (str(r1), str(r2))


def test_plan_align_requires_index(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFeatureError):
        BwaAdapter().plan_align(
            AlignRequest(query=(_query(tmp_path),), target=(_targets(tmp_path),)),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )


def test_parse_sam_hard_clipped_supplementary_uses_read_coordinates(tmp_path: Path) -> None:
    # bwa mem supplementary records carry hard-clipped CIGARs: SEQ no longer
    # spans the original read, so coordinates must be offset by the clipped
    # lengths to stay in the read's own coordinate system.
    lines = [
        "@SQ\tSN:t1\tLN:600",
        _sam(flag=2048, cigar="5H50M15H", seq="A" * 50, tags=("NM:i:0",)),
        _sam(qname="q2", flag=2064, cigar="5H50M15H", seq="A" * 50, tags=("NM:i:0",)),
    ]
    records = _parse(lines, tmp_path=tmp_path)

    plus = records[0]
    assert plus.strand is Strand.PLUS
    assert plus.query_length == 70
    assert plus.query_start == 5
    assert plus.query_end == 55

    minus = records[1]
    assert minus.strand is Strand.MINUS
    assert minus.query_length == 70
    assert minus.query_start == 15
    assert minus.query_end == 65


def test_parse_sam_forward_reverse_and_supplementary(tmp_path: Path) -> None:
    lines = [
        "@HD\tVN:1.5\tSO:unsorted",
        "@SQ\tSN:t1\tLN:600",
        "@SQ\tSN:t2\tLN:600",
        _sam(tags=("NM:i:5", "AS:i:80")),
        _sam(
            qname="q2",
            flag=16,
            rname="t2",
            pos=50,
            mapq=30,
            cigar="5S10M",
            seq=_SEQ15,
            tags=("NM:i:2",),
        ),
        _sam(qname="q3", flag=256, pos=200, mapq=0, cigar="10M", seq=_SEQ10, tags=("NM:i:1",)),
        _sam(qname="q4", flag=2048, pos=300, mapq=20, cigar="10M", seq=_SEQ10, tags=("NM:i:0",)),
        _sam(qname="q5", flag=4, rname="*", pos=0, mapq=0, cigar="*", seq="*"),
    ]
    records = _parse(lines, tmp_path=tmp_path)
    assert len(records) == 3

    forward = records[0]
    assert forward.query_id == "q1"
    assert forward.target_id == "t1"
    assert forward.strand is Strand.PLUS
    assert forward.query_length == 17
    assert forward.target_length == 600
    assert forward.query_start == 0
    assert forward.query_end == 17
    assert forward.target_start == 100
    assert forward.target_end == 118
    assert forward.aligned_length == 15
    assert forward.gap_count == 5
    assert forward.gap_openings == 2
    assert forward.identity == pytest.approx(100 * 10 / 15)
    assert forward.mapq == 60
    assert forward.score == 80.0
    assert forward.target_coverage == pytest.approx(18 / 600)
    assert forward.tags["identity_method"] == "sam_nm"
    assert forward.tags["NM"] == "5"

    reverse = records[1]
    assert reverse.strand is Strand.MINUS
    assert reverse.query_start == 0
    assert reverse.query_end == 10
    assert reverse.target_start == 49
    assert reverse.target_end == 59
    assert reverse.identity == pytest.approx(80.0)
    assert reverse.query_coverage == pytest.approx(10 / 15)

    supplementary = records[2]
    assert supplementary.query_id == "q4"
    assert supplementary.is_supplementary is True
    assert supplementary.is_primary is True


def test_parse_sam_secondary_all_and_target_filter(tmp_path: Path) -> None:
    lines = [
        "@SQ\tSN:t1\tLN:600",
        "@SQ\tSN:t2\tLN:600",
        _sam(tags=("NM:i:0",)),
        _sam(qname="q2", flag=256, rname="t2", pos=50, cigar="10M", seq=_SEQ10, tags=("NM:i:1",)),
    ]
    assert len(_parse(lines, tmp_path=tmp_path)) == 1
    both = _parse(lines, params={"secondary": "all"}, tmp_path=tmp_path)
    assert len(both) == 2
    filtered = _parse(
        lines,
        params={"secondary": "all"},
        target_filter=frozenset({"t2"}),
        tmp_path=tmp_path,
    )
    assert [record.target_id for record in filtered] == ["t2"]


def test_parse_sam_errors(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        _parse(["@SQ\tSN:t1\tLN:600", "q1\t0\tt1\t1"], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse(
            ["@SQ\tSN:t1\tLN:600", _sam(cigar="10Q")],
            tmp_path=tmp_path,
        )


def test_index_and_semantic_params() -> None:
    adapter = BwaAdapter()
    assert adapter.index_params({"algorithm": "bwtsw", "threads": 4}) == {"algorithm": "bwtsw"}
    assert adapter.semantic_params({"mark_shorter": True, "threads": 4}) == {"mark_shorter": True}


def test_plan_align_rejects_alignment_algorithm_trap(tmp_path: Path) -> None:
    # `algorithm` is the bwa *index* construction switch (bwa index -a);
    # passing "aln"/"bwasw"/"mem" silently changed nothing in the alignment.
    query = _query(tmp_path)
    target = _targets(tmp_path)
    for bogus in ("aln", "bwasw", "mem"):
        request = AlignRequest(query=(query,), target=(target,), params={"algorithm": bogus})
        with pytest.raises(UnsupportedFeatureError, match="bwa mem"):
            BwaAdapter().plan_align(request, _index(), PlanContext(workspace=tmp_path / "work"))
