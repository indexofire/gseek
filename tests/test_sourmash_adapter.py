from __future__ import annotations

from pathlib import Path

import pytest

from gseek.adapter import (
    AlignPlan,
    DistanceRequest,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.sourmash import SourmashAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, OutputKind

_CSV_HEADER = "similarity,md5,filename,name,query_filename,query_name,query_md5,ani"
_CSV_ROW = "0.3333333333333333,9d9e,ref.sig,,query.fa,,682434c8,"


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">t1\nACGTACGT\n>t2\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path) -> Path:
    path = tmp_path / "query.fa"
    path.write_text(">q1\nACGTACGT\n", encoding="utf-8")
    return path


def _index() -> IndexRef:
    return IndexRef(
        key="5" * 64,
        adapter="sourmash",
        adapter_version="1",
        entry="targets.sig",
        source_sha256="6" * 64,
    )


def _plan(stream: bool = False, result: Path | None = None) -> AlignPlan:
    return AlignPlan(
        commands=(),
        stream=stream,
        result_files=(result,) if result is not None else (),
    )


def _parse(
    lines: list[str],
    *,
    tmp_path: Path,
    containment: bool = False,
) -> list:
    result = tmp_path / "matches.csv"
    result.write_text("\n".join(lines) + "\n", encoding="utf-8")
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(_targets(tmp_path),),
        params={"containment": containment},
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=result),
        workspace=tmp_path,
        index=_index(),
    )
    return list(SourmashAdapter().parse_distance(ctx))


def test_parse_empty_csv_yields_no_records(tmp_path: Path) -> None:
    # `sourmash search` writes a 0-byte CSV (exit 0) when nothing matches the
    # threshold: that is a legitimate empty result, not a parse failure.
    result = tmp_path / "matches.csv"
    result.write_text("", encoding="utf-8")
    request = DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=result),
        workspace=tmp_path,
        index=_index(),
    )
    assert list(SourmashAdapter().parse_distance(ctx)) == []


def test_parse_empty_gather_csv_yields_no_records(tmp_path: Path) -> None:
    result = tmp_path / "matches.csv"
    result.write_text("", encoding="utf-8")
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(_targets(tmp_path),),
        params={"mode": "gather"},
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=result),
        workspace=tmp_path,
        index=_index(),
    )
    assert list(SourmashAdapter().parse_distance(ctx)) == []


def test_spec_declares_sourmash_capabilities() -> None:
    spec = SourmashAdapter.spec
    assert spec.name == "sourmash"
    assert spec.capabilities.inputs == frozenset({InputKind.FASTA, InputKind.FASTQ})
    assert spec.capabilities.outputs == frozenset({OutputKind.DISTANCE})
    assert spec.capabilities.index_scope is IndexScope.TARGET
    tool = next(tool for tool in spec.tools if tool.binary == "sourmash")
    assert tool.version_args == ("--version",)
    assert tool.min_version == (4, 0, 0)


def test_plan_index_builds_signature(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    plan = SourmashAdapter().plan_index(
        IndexRequest(
            target=(_targets(tmp_path),),
            params={"k": 21, "scaled": 100},
        ),
        PlanContext(workspace=index_dir, index_dir=index_dir),
    )
    assert plan is not None
    assert plan.entry == "targets.sig"
    assert plan.artifacts == ("targets.sig",)
    argv = plan.commands[0].argv
    assert argv[:3] == ("sourmash", "sketch", "dna")
    assert argv[argv.index("-p") + 1] == "k=21,scaled=100"
    assert argv[argv.index("-o") + 1].endswith("targets.sig")


def test_plan_distance_with_index(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    plan = SourmashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"k": 21, "scaled": 100, "threshold": 0.2},
        ),
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert plan.stream is False
    assert len(plan.commands) == 2
    sketch, search = plan.commands
    assert sketch.argv[:3] == ("sourmash", "sketch", "dna")
    assert search.argv[:2] == ("sourmash", "search")
    assert search.argv[search.argv.index("--threshold") + 1] == "0.2"
    assert search.argv[search.argv.index("-o") + 1].endswith("matches.csv")
    assert str(index_dir / "targets.sig") in search.argv


def test_plan_distance_without_index_builds_reference_signature(tmp_path: Path) -> None:
    plan = SourmashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"k": 21, "scaled": 100},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert len(plan.commands) == 3
    assert plan.commands[0].argv[:3] == ("sourmash", "sketch", "dna")
    assert plan.commands[1].argv[:3] == ("sourmash", "sketch", "dna")
    assert plan.commands[2].argv[:2] == ("sourmash", "search")


def test_plan_distance_rejects_multiple_queries(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFeatureError):
        SourmashAdapter().plan_distance(
            DistanceRequest(
                query=(_query(tmp_path), _query(tmp_path)),
                reference=(_targets(tmp_path),),
            ),
            _index(),
            PlanContext(workspace=tmp_path / "work", index_dir=tmp_path),
        )


def test_parse_search_csv(tmp_path: Path) -> None:
    records = _parse([_CSV_HEADER, _CSV_ROW], tmp_path=tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record.reference_id == "9d9e"
    assert record.query_id == "query.fa"
    assert record.tags["reference_signature"] == "ref.sig"
    assert record.distance == pytest.approx(1.0 - 0.3333333333333333)
    assert record.jaccard == pytest.approx(0.3333333333333333)
    assert record.containment is None
    assert record.tags["metric"] == "sourmash_similarity"
    assert record.tags["k"] == "31"
    assert record.tags["scaled"] == "1000"


def test_parse_containment_csv(tmp_path: Path) -> None:
    records = _parse(
        [_CSV_HEADER, _CSV_ROW],
        tmp_path=tmp_path,
        containment=True,
    )
    record = records[0]
    assert record.containment == pytest.approx(0.3333333333333333)
    assert record.jaccard is None
    assert record.distance == pytest.approx(1.0 - 0.3333333333333333)


def test_parse_errors(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        _parse(["similarity,md5"], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse([_CSV_HEADER, "NOPE,md5,ref.sig,,query.fa,,md5,"], tmp_path=tmp_path)

    request = DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=tmp_path / "missing.csv"),
        workspace=tmp_path,
        index=_index(),
    )
    with pytest.raises(ParseError):
        list(SourmashAdapter().parse_distance(ctx))


def test_index_and_semantic_params() -> None:
    adapter = SourmashAdapter()
    assert adapter.index_params({"k": 21, "scaled": 100, "threads": 4}) == {
        "k": 21,
        "scaled": 100,
    }
    assert adapter.semantic_params({"threshold": 0.2, "threads": 4}) == {"threshold": 0.2}


def test_plan_distance_gather_mode(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    plan = SourmashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"k": 21, "scaled": 100, "mode": "gather", "threshold_bp": 0},
        ),
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert len(plan.commands) == 2
    gather = plan.commands[1]
    assert gather.argv[:2] == ("sourmash", "gather")
    assert gather.argv[gather.argv.index("--threshold-bp") + 1] == "0"
    assert gather.argv[gather.argv.index("-o") + 1].endswith("matches.csv")


_GATHER_HEADER = (
    "intersect_bp,f_orig_query,f_match,f_unique_to_query,filename,name,md5,"
    "gather_result_rank,remaining_bp,query_filename,query_name,query_md5,"
    "ksize,moltype,scaled,average_containment_ani"
)
_GATHER_ROW = "700,1.0,0.5833,1.0,ref.sig,,6e22,0,0,q.fa,,609e,21,DNA,100,0.98"


def test_parse_search_uses_md5_for_combined_signature(tmp_path: Path) -> None:
    rows = [
        _CSV_HEADER,
        "0.5,aaaa,refs.sig,,query.fa,,682434c8,",
        "0.4,bbbb,refs.sig,,query.fa,,682434c8,",
    ]
    records = _parse(rows, tmp_path=tmp_path)
    assert {record.reference_id for record in records} == {"aaaa", "bbbb"}


def test_parse_gather_csv(tmp_path: Path) -> None:
    result = tmp_path / "matches.csv"
    result.write_text(_GATHER_HEADER + "\n" + _GATHER_ROW + "\n", encoding="utf-8")
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(_targets(tmp_path),),
        params={"mode": "gather"},
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=result),
        workspace=tmp_path,
        index=_index(),
    )
    records = list(SourmashAdapter().parse_distance(ctx))
    assert len(records) == 1
    record = records[0]
    assert record.reference_id == "6e22"
    assert record.query_id == "query.fa"
    assert record.containment == pytest.approx(1.0)
    assert record.distance is None
    assert record.tags["metric"] == "sourmash_gather"
    assert record.tags["f_match"] == "0.5833"
    assert record.tags["intersect_bp"] == "700"
    assert record.tags["gather_result_rank"] == "0"
    assert record.ani == pytest.approx(98.0)


def test_plan_distance_external_reference_skips_reference_sketch(tmp_path: Path) -> None:
    reference = tmp_path / "gtdb.zip"
    plan = SourmashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(reference,),
            external_reference=True,
            params={"mode": "gather", "k": 21, "scaled": 100},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert len(plan.commands) == 2
    assert plan.commands[0].argv[:2] == ("sourmash", "sketch")
    gather = plan.commands[1]
    assert gather.argv[:2] == ("sourmash", "gather")
    assert str(reference) in gather.argv


def test_parse_gather_preserves_name_and_unique_fraction(tmp_path: Path) -> None:
    header = (
        "f_orig_query,f_unique_weighted,name,filename,md5,"
        "query_filename,query_name,query_md5,average_containment_ani"
    )
    row = "0.5,0.25,GTDB species,refs.sig,abc123,q.fa,,def456,0.97"
    result = tmp_path / "matches.csv"
    result.write_text(header + "\n" + row + "\n", encoding="utf-8")
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(_targets(tmp_path),),
        params={"mode": "gather"},
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=result),
        workspace=tmp_path,
        index=_index(),
    )
    (record,) = list(SourmashAdapter().parse_distance(ctx))
    assert record.tags["reference_name"] == "GTDB species"
    assert record.tags["f_unique_weighted"] == "0.25"
    assert record.tags["reference_md5"] == "abc123"


def test_parse_rejects_stream_input_explicitly(tmp_path: Path) -> None:
    # The protocol accepts `stream`, but this adapter only parses CSV result
    # files; silently ignoring a supplied stream would hide caller bugs.
    request = DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    with pytest.raises(UnsupportedFeatureError, match="stream"):
        list(SourmashAdapter().parse_distance(ctx, iter(["similarity,md5"])))
