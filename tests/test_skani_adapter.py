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
from gseek.adapters.skani import SkaniAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, OutputKind

_HEADER = (
    "Ref_file\tQuery_file\tANI\tAlign_fraction_ref\tAlign_fraction_query\tRef_name\tQuery_name"
)


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">a\nACGTACGT\n>b\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path) -> Path:
    path = tmp_path / "query.fa"
    path.write_text(">q\nACGTACGT\n", encoding="utf-8")
    return path


def _index() -> IndexRef:
    return IndexRef(
        key="7" * 64,
        adapter="skani",
        adapter_version="1",
        entry="db",
        source_sha256="8" * 64,
    )


def _plan() -> AlignPlan:
    return AlignPlan(commands=(), stream=True)


def _parse(lines: list[str], *, tmp_path: Path) -> list:
    request = DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    return list(SkaniAdapter().parse_distance(ctx, iter(lines)))


def test_spec_declares_skani_capabilities() -> None:
    spec = SkaniAdapter.spec
    assert spec.name == "skani"
    assert spec.capabilities.inputs == frozenset({InputKind.FASTA})
    assert spec.capabilities.outputs == frozenset({OutputKind.DISTANCE})
    assert spec.capabilities.index_scope is IndexScope.TARGET
    tool = next(tool for tool in spec.tools if tool.binary == "skani")
    assert tool.version_args == ("--version",)
    assert tool.min_version == (0, 2, 0)


def test_plan_index_sketches_database(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    plan = SkaniAdapter().plan_index(
        IndexRequest(
            target=(_targets(tmp_path),),
            params={"compression_factor": 200},
        ),
        PlanContext(workspace=index_dir, index_dir=index_dir),
    )
    assert plan is not None
    assert plan.entry == "db"
    assert plan.artifacts == ("db/index.db", "db/sketches.db", "db/markers.bin")
    argv = plan.commands[0].argv
    assert argv[:2] == ("skani", "sketch")
    assert argv[argv.index("-c") + 1] == "200"
    assert argv[argv.index("-o") + 1].endswith("db")


def test_plan_distance_with_index(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    plan = SkaniAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"min_af": 5, "num_results": 3},
        ),
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert plan.stream is True
    argv = plan.commands[0].argv
    assert argv[:2] == ("skani", "search")
    assert argv[argv.index("-d") + 1] == str(index_dir / "db")
    assert argv[argv.index("--min-af") + 1] == "5"
    assert argv[argv.index("-n") + 1] == "3"
    assert argv[-1] == str(_query(tmp_path))


def test_plan_distance_accepts_index_only_params(tmp_path: Path) -> None:
    plan = SkaniAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"compression_factor": 200, "marker_compression": 5},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert "-c" not in argv
    assert "-m" not in argv


def test_plan_distance_without_index(tmp_path: Path) -> None:
    plan = SkaniAdapter().plan_distance(
        DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),)),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert argv[:2] == ("skani", "dist")
    assert argv[-2] == str(_query(tmp_path))
    assert argv[-1] == str(_targets(tmp_path))


def test_plan_distance_rejects_fastq(tmp_path: Path) -> None:
    reads = tmp_path / "reads.fq"
    reads.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    with pytest.raises(UnsupportedFeatureError):
        SkaniAdapter().plan_distance(
            DistanceRequest(query=(reads,), reference=(_targets(tmp_path),)),
            _index(),
            PlanContext(workspace=tmp_path / "work", index_dir=tmp_path),
        )


def test_parse_skani_dist(tmp_path: Path) -> None:
    records = _parse(
        [
            _HEADER,
            "a.fa\tq.fa\t99.50\t98.00\t97.00\ta\tq",
        ],
        tmp_path=tmp_path,
    )
    assert len(records) == 1
    record = records[0]
    assert record.reference_id == "a"
    assert record.query_id == "q"
    assert record.ani == pytest.approx(99.5)
    assert record.aligned_fraction == pytest.approx(0.98)
    assert record.distance is None
    assert record.tags["metric"] == "skani_ani"
    assert record.tags["align_fraction_query"] == "97.00"


def test_parse_skani_fallback_names_and_errors(tmp_path: Path) -> None:
    records = _parse(
        [_HEADER, "a.fa\tq.fa\t100.0\t99.0\t98.0\t\t"],
        tmp_path=tmp_path,
    )
    assert records[0].reference_id == "a.fa"
    assert records[0].query_id == "q.fa"
    with pytest.raises(ParseError):
        _parse([_HEADER, "a.fa\tq.fa"], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse([_HEADER, "a.fa\tq.fa\tNOPE\t1\t1\ta\tq"], tmp_path=tmp_path)


def test_index_and_semantic_params() -> None:
    adapter = SkaniAdapter()
    assert adapter.index_params({"compression_factor": 200, "threads": 4}) == {
        "compression_factor": 200
    }
    assert adapter.semantic_params({"min_af": 5, "threads": 4}) == {"min_af": 5}


def test_plan_distance_with_external_database_directory(tmp_path: Path) -> None:
    database = tmp_path / "panel.sketch"
    database.mkdir()
    plan = SkaniAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(database,),
            external_reference=True,
            threads=2,
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert argv[:4] == ("skani", "search", "-d", str(database.resolve()))
    assert argv[argv.index("-t") + 1] == "2"
    assert argv[-1] == str(_query(tmp_path))
