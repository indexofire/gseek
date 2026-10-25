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
from gseek.adapters.mash import MashAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, OutputKind


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
        key="3" * 64,
        adapter="mash",
        adapter_version="1",
        entry="targets.msh",
        source_sha256="4" * 64,
    )


def _plan(stream: bool = True, result: Path | None = None) -> AlignPlan:
    return AlignPlan(commands=(), stream=stream, result_files=(result,) if result else ())


def _parse(
    lines: list[str],
    *,
    tmp_path: Path,
) -> list:
    request = DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    return list(MashAdapter().parse_distance(ctx, iter(lines)))


def test_spec_declares_mash_capabilities() -> None:
    spec = MashAdapter.spec
    assert spec.name == "mash"
    assert spec.capabilities.inputs == frozenset({InputKind.FASTA, InputKind.FASTQ})
    assert spec.capabilities.outputs == frozenset({OutputKind.DISTANCE})
    assert spec.capabilities.index_scope is IndexScope.TARGET
    tool = next(tool for tool in spec.tools if tool.binary == "mash")
    assert tool.version_args == ("--version",)
    assert tool.min_version == (2, 0)


def test_plan_index_builds_msh(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    plan = MashAdapter().plan_index(
        IndexRequest(
            target=(_targets(tmp_path),),
            params={"k": 21, "sketch_size": 100},
        ),
        PlanContext(workspace=index_dir, index_dir=index_dir),
    )
    assert plan is not None
    assert plan.entry == "targets.msh"
    assert plan.artifacts == ("targets.msh",)
    argv = plan.commands[0].argv
    assert argv[:2] == ("mash", "sketch")
    assert argv[argv.index("-k") + 1] == "21"
    assert argv[argv.index("-s") + 1] == "100"
    assert argv[argv.index("-o") + 1].endswith("targets")


def test_plan_distance_with_index(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    work = tmp_path / "work"
    plan = MashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"k": 21, "sketch_size": 100, "max_distance": 0.2},
        ),
        _index(),
        PlanContext(workspace=work, index_dir=index_dir),
    )
    assert plan.stream is True
    assert len(plan.commands) == 2
    query_sketch, dist = plan.commands
    assert query_sketch.argv[:2] == ("mash", "sketch")
    assert query_sketch.argv[query_sketch.argv.index("-o") + 1].endswith("query")
    assert dist.argv[:2] == ("mash", "dist")
    assert dist.argv[dist.argv.index("-d") + 1] == "0.2"
    assert dist.argv[-2] == str(index_dir / "targets.msh")
    assert dist.argv[-1].endswith("query.msh")


def test_plan_distance_without_index_builds_reference_sketch(tmp_path: Path) -> None:
    plan = MashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"k": 21, "sketch_size": 100},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert len(plan.commands) == 3
    assert plan.commands[0].argv[:2] == ("mash", "sketch")
    assert plan.commands[1].argv[:2] == ("mash", "sketch")
    assert plan.commands[2].argv[:2] == ("mash", "dist")


def test_plan_distance_rejects_unknown_params(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFeatureError):
        MashAdapter().plan_distance(
            DistanceRequest(
                query=(_query(tmp_path),),
                reference=(_targets(tmp_path),),
                params={"bogus": True},
            ),
            _index(),
            PlanContext(workspace=tmp_path / "work", index_dir=tmp_path),
        )


def test_parse_mash_dist(tmp_path: Path) -> None:
    records = _parse(
        ["targets.fa\tquery.fa\t0.0199514\t0\t49/100"],
        tmp_path=tmp_path,
    )
    assert len(records) == 1
    record = records[0]
    assert record.reference_id == "targets.fa"
    assert record.query_id == "query.fa"
    assert record.distance == pytest.approx(0.0199514)
    assert record.pvalue == 0.0
    assert record.shared_hashes == 49
    assert record.total_hashes == 100
    assert record.tags["metric"] == "mash_distance"
    assert record.tags["k"] == "21"
    assert record.tags["sketch_size"] == "1000"


def test_parse_mash_errors(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        _parse(["targets.fa\tquery.fa"], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse(["targets.fa\tquery.fa\tNOPE\t0\t49/100"], tmp_path=tmp_path)


def test_index_and_semantic_params() -> None:
    adapter = MashAdapter()
    assert adapter.index_params({"k": 21, "sketch_size": 100, "seed": 7, "threads": 4}) == {
        "k": 21,
        "sketch_size": 100,
        "seed": 7,
    }
    assert adapter.semantic_params({"max_distance": 0.2, "threads": 4}) == {"max_distance": 0.2}


def test_plan_distance_screen_mode(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    plan = MashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(_targets(tmp_path),),
            params={"k": 21, "sketch_size": 100, "mode": "screen"},
        ),
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert len(plan.commands) == 1
    argv = plan.commands[0].argv
    assert argv[:2] == ("mash", "screen")
    assert argv[-1].endswith("query.fa")


def test_parse_mash_screen(tmp_path: Path) -> None:
    result_path = tmp_path / "screen.tsv"
    result_path.write_text(
        "0.939555\t27/100\t1\t1.16083e-256\ttargets.fa\n",
        encoding="utf-8",
    )
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(_targets(tmp_path),),
        params={"mode": "screen"},
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    records = list(MashAdapter().parse_distance(ctx, iter(result_path.read_text().splitlines())))
    assert len(records) == 1
    record = records[0]
    assert record.reference_id == "targets.fa"
    assert record.query_id == "query"
    assert record.containment == pytest.approx(0.939555)
    assert record.distance == pytest.approx(1.0 - 0.939555)
    assert record.shared_hashes == 27
    assert record.total_hashes == 100
    assert record.pvalue == pytest.approx(1.16083e-256)
    assert record.tags["metric"] == "mash_screen"
    assert record.tags["median_multiplicity"] == "1"


def test_plan_distance_external_reference_skips_reference_sketch(tmp_path: Path) -> None:
    reference = tmp_path / "refs.msh"
    plan = MashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(reference,),
            external_reference=True,
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert len(plan.commands) == 1
    assert plan.commands[0].argv[:2] == ("mash", "dist")
    assert plan.commands[0].argv[-2] == str(reference)
    assert plan.commands[0].argv[-1] == str(_query(tmp_path).resolve())


def test_plan_distance_reads_uses_error_filtering_flags(tmp_path: Path) -> None:
    reference = tmp_path / "refs.msh"
    plan = MashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(reference,),
            external_reference=True,
            params={"reads": True, "k": 21, "sketch_size": 1000},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    sketch = plan.commands[0]
    assert sketch.argv[:2] == ("mash", "sketch")
    assert "-m" in sketch.argv and sketch.argv[sketch.argv.index("-m") + 1] == "2"
    assert "-r" in sketch.argv


def test_plan_distance_external_nondefault_sketch_uses_query_sketch(tmp_path: Path) -> None:
    reference = tmp_path / "refs.msh"
    plan = MashAdapter().plan_distance(
        DistanceRequest(
            query=(_query(tmp_path),),
            reference=(reference,),
            external_reference=True,
            params={"sketch_size": 100},
        ),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert [command.argv[:2] for command in plan.commands] == [
        ("mash", "sketch"),
        ("mash", "dist"),
    ]


def test_plan_screen_rejects_multiple_query_files(tmp_path: Path) -> None:
    # mash screen output rows carry no query-file column: attributing them all
    # to query[0] would be wrong. Require one pooled query file per run.
    queries = [_query(tmp_path, f"q{i}.fa") for i in range(2)]
    request = DistanceRequest(
        query=tuple(queries),
        reference=(_targets(tmp_path),),
        params={"mode": "screen"},
    )
    with pytest.raises(UnsupportedFeatureError, match="single query"):
        MashAdapter().plan_distance(request, None, PlanContext(workspace=tmp_path / "work"))


def test_external_reference_tags_mark_unknown_sketch_params(tmp_path: Path) -> None:
    # A pre-built .msh may use different k/sketch_size than the request;
    # recording the request values would misdescribe the comparison.
    result = tmp_path / "dist.tsv"
    result.write_text(
        "ref.msh\tquery.fa\t0.05\t1.0\t100/1000\n",
        encoding="utf-8",
    )
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(tmp_path / "external.msh",),
        params={"mode": "dist", "k": 21, "sketch_size": 1000},
        external_reference=True,
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result=result),
        workspace=tmp_path,
    )
    record = next(iter(MashAdapter().parse_distance(ctx)))
    assert record.tags["k"] == "unknown"
    assert record.tags["sketch_size"] == "unknown"


def test_screen_preserves_defline_with_slash(tmp_path: Path) -> None:
    # mash screen col 5 is the sketch defline, not a path: Path().name would
    # turn "…plasmid/pO157" into "pO157", silently corrupting reference_id.
    result = tmp_path / "screen.tsv"
    result.write_text(
        "0.99\t10/1000\t5\t1e-20\tGCF_000195955.2 E.coli plasmid/pO157\n",
        encoding="utf-8",
    )
    request = DistanceRequest(
        query=(_query(tmp_path),),
        reference=(_targets(tmp_path),),
        params={"mode": "screen"},
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(stream=True, result=result),
        workspace=tmp_path,
    )
    record = next(iter(MashAdapter().parse_distance(ctx)))
    assert record.reference_id == "GCF_000195955.2 E.coli plasmid/pO157"


def test_non_finite_fields_raise_parse_error_with_context(tmp_path: Path) -> None:
    # "inf"/"nan" bypass the adapter's ValueError branch and would explode at
    # the model layer as an InputError with no path/line context.
    result = tmp_path / "dist.tsv"
    result.write_text("ref.msh\tquery.fa\tinf\t1.0\t100/1000\n", encoding="utf-8")
    request = DistanceRequest(query=(_query(tmp_path),), reference=(_targets(tmp_path),))
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(stream=True, result=result),
        workspace=tmp_path,
    )
    with pytest.raises(ParseError) as exc_info:
        list(MashAdapter().parse_distance(ctx))
    assert exc_info.value.line_number == 1
