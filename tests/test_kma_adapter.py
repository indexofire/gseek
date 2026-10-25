from __future__ import annotations

from pathlib import Path

import pytest

from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.kma import KmaAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, OutputKind

_HEADER = (
    "#Template\tScore\tExpected\tTemplate_length\tTemplate_Identity\t"
    "Template_Coverage\tQuery_Identity\tQuery_Coverage\tDepth\tq_value\tp_value"
)
_ROW = "t1\t     306\t       0\t     500\t   60.00\t   60.00\t  100.00\t  166.67\t    0.60\t  306.00\t1.0e-26"


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">t1\nACGTACGT\n>t2\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path, name: str = "query.fa") -> Path:
    path = tmp_path / name
    path.write_text(">q1\nACGT\n", encoding="utf-8")
    return path


def _index() -> IndexRef:
    return IndexRef(
        key="e" * 64,
        adapter="kma",
        adapter_version="1",
        entry="kma_db",
        source_sha256="f" * 64,
    )


def _plan(tmp_path: Path, name: str = "out.res") -> AlignPlan:
    return AlignPlan(commands=(), stream=False, result_files=(tmp_path / name,))


def _parse(
    rows: list[str],
    *,
    tmp_path: Path,
    query_kind: InputKind = InputKind.FASTA,
    target_filter: frozenset[str] | None = None,
    name: str = "out.res",
) -> list:
    result_path = tmp_path / name
    result_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        query_kind=query_kind,
        target_filter=target_filter,
    )
    ctx = ParseContext(
        request=request,
        plan=_plan(tmp_path, name),
        workspace=tmp_path,
        index=_index(),
    )
    return list(KmaAdapter().parse_align(ctx))


def test_spec_declares_kma_capabilities() -> None:
    spec = KmaAdapter.spec
    assert spec.name == "kma"
    caps = spec.capabilities
    assert caps.inputs == frozenset({InputKind.FASTA, InputKind.FASTQ})
    assert caps.outputs == frozenset({OutputKind.ALIGNMENT})
    assert caps.index_scope is IndexScope.TARGET
    assert caps.paired is True
    tool = next(tool for tool in spec.tools if tool.binary == "kma")
    assert tool.version_args == ("-v",)
    assert tool.min_version == (1, 4, 0)


def test_plan_index_builds_kma_prefix(tmp_path: Path) -> None:
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    plan = KmaAdapter().plan_index(
        IndexRequest(target=(target,), params={"k": 16}),
        PlanContext(workspace=index_dir, index_dir=index_dir),
    )
    assert plan is not None
    assert plan.entry == "kma_db"
    assert plan.artifacts == (
        "kma_db.name",
        "kma_db.length.b",
        "kma_db.seq.b",
        "kma_db.comp.b",
    )
    argv = plan.commands[0].argv
    assert argv[0] == "kma"
    assert argv[1] == "index"
    assert argv[argv.index("-k") + 1] == "16"
    merged = Path(argv[argv.index("-i") + 1])
    assert merged.exists()
    assert Path(argv[argv.index("-o") + 1]).name == "kma_db"


def test_plan_align_assembly(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"mem_mode": False},
    )
    plan = KmaAdapter().plan_align(
        request,
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert plan.stream is False
    assert len(plan.result_files) == 1
    assert plan.result_files[0].name == "out.res"
    argv = plan.commands[0].argv
    assert argv[0] == "kma"
    assert "-t_db" in argv
    assert "-asm" in argv
    assert "-ill" not in argv
    assert "-ipe" not in argv
    assert "-mem_mode" not in argv
    assert argv[argv.index("-i") + 1] == str(_query(tmp_path))


def test_plan_align_accepts_index_only_params(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    request = AlignRequest(
        query=(_query(tmp_path, "query.fq"),),
        target=(_targets(tmp_path),),
        query_kind=InputKind.FASTQ,
        params={"k": 17, "m": 16, "hc": True, "mem_mode": True},
    )
    plan = KmaAdapter().plan_align(
        request,
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    argv = plan.commands[0].argv
    assert "-k" not in argv
    assert "-m" not in argv
    assert "-hc" not in argv
    assert "-ill" in argv
    assert "-mem_mode" in argv


def test_plan_align_paired_fastq(tmp_path: Path) -> None:
    r1 = _query(tmp_path, "r1.fq")
    r2 = _query(tmp_path, "r2.fq")
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    request = AlignRequest(
        query=(r1, r2),
        target=(_targets(tmp_path),),
        query_kind=InputKind.FASTQ,
        paired=True,
        params={"mem_mode": True},
    )
    plan = KmaAdapter().plan_align(
        request,
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    argv = plan.commands[0].argv
    assert "-ipe" in argv
    assert argv[argv.index("-ipe") + 1] == str(r1)
    assert argv[argv.index("-ipe") + 2] == str(r2)
    assert "-ill" in argv
    assert "-mem_mode" in argv


def test_plan_align_requires_index_and_rejects_paired_fasta(tmp_path: Path) -> None:
    adapter = KmaAdapter()
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        query_kind=InputKind.FASTA,
    )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(request, None, PlanContext(workspace=tmp_path / "work"))

    paired = AlignRequest(
        query=(_query(tmp_path, "r1.fa"), _query(tmp_path, "r2.fa")),
        target=(_targets(tmp_path),),
        query_kind=InputKind.FASTA,
        paired=True,
    )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(
            paired,
            _index(),
            PlanContext(workspace=tmp_path / "work", index_dir=tmp_path / "index"),
        )


def test_parse_res_assembly_row(tmp_path: Path) -> None:
    records = _parse([_HEADER, _ROW], tmp_path=tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record.target_id == "t1"
    assert record.query_id == "query"
    assert record.identity == 60.0
    assert record.target_length == 500
    assert record.target_coverage == pytest.approx(0.6)
    assert record.query_coverage == pytest.approx(1.6667)
    assert record.depth is None
    assert record.score == 306.0
    assert record.query_interval is None
    assert record.target_interval is None
    assert record.tags["identity_method"] == "kma_template"
    assert record.tags["q_value"] == "306.00"
    assert record.tags["p_value"] == "1.0e-26"


def test_parse_res_fastq_keeps_depth(tmp_path: Path) -> None:
    records = _parse([_HEADER, _ROW], tmp_path=tmp_path, query_kind=InputKind.FASTQ)
    assert records[0].depth == pytest.approx(0.60)


def test_parse_res_target_filter(tmp_path: Path) -> None:
    rows = [
        _HEADER,
        _ROW,
        _ROW.replace("t1", "t2", 1),
    ]
    records = _parse(rows, tmp_path=tmp_path, target_filter=frozenset({"t2"}))
    assert [record.target_id for record in records] == ["t2"]


def test_parse_res_errors(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        _parse([_ROW], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse([_HEADER, "t1\tonly-one-field"], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse([_HEADER, "t1\t1\t0\tnope\t60\t60\t100\t100\t0\t1\t1"], tmp_path=tmp_path)

    request = AlignRequest(query=(_query(tmp_path),), target=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,
        plan=_plan(tmp_path, "missing.res"),
        workspace=tmp_path,
        index=_index(),
    )
    with pytest.raises(ParseError):
        list(KmaAdapter().parse_align(ctx))


def test_index_and_semantic_params() -> None:
    adapter = KmaAdapter()
    assert adapter.index_params({"k": 16, "m": 16, "hc": True, "threads": 4}) == {
        "k": 16,
        "m": 16,
        "hc": True,
    }
    assert adapter.semantic_params({"mem_mode": True, "threads": 4, "debug": True}) == {
        "mem_mode": True
    }


def test_index_accepts_align_only_mem_mode(tmp_path: Path) -> None:
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    plan = KmaAdapter().plan_index(
        IndexRequest(target=(target,), params={"mem_mode": True}),
        PlanContext(workspace=index_dir, index_dir=index_dir),
    )
    assert plan is not None
    assert plan.entry == "kma_db"
