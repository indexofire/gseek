from __future__ import annotations

from pathlib import Path

import pytest

from gseek.adapter import (
    AlignPlan,
    DistanceRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.sylph import SylphAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.types import IndexScope, InputKind, OutputKind


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "ref.fa"
    path.write_text(">ref_genome\nACGTACGT\n", encoding="utf-8")
    return path


def _reads(tmp_path: Path, name: str = "reads.fq") -> Path:
    path = tmp_path / name
    path.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    return path


def _plan(result: Path | None = None) -> AlignPlan:
    return AlignPlan(
        commands=(),
        stream=False,
        result_files=(result,) if result is not None else (),
    )


def _parse(result: Path, tmp_path: Path) -> list:
    request = DistanceRequest(
        query=(_reads(tmp_path),), reference=(_targets(tmp_path),), external_reference=True
    )
    ctx = ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=_plan(result),
        workspace=tmp_path,
    )
    return list(SylphAdapter().parse_distance(ctx))


# ---------------------------------------------------------------------------
# 1. BackendSpec 能力/工具
# ---------------------------------------------------------------------------


def test_spec_declares_sylph_capabilities() -> None:
    spec = SylphAdapter.spec
    assert spec.name == "sylph"
    assert spec.family == "minhash"
    assert spec.capabilities.inputs == frozenset({InputKind.FASTQ})
    assert spec.capabilities.outputs == frozenset({OutputKind.DISTANCE})
    assert spec.capabilities.index_scope is IndexScope.NONE
    assert spec.capabilities.streaming is False
    (requirement,) = spec.tools
    assert requirement.binary == "sylph"
    assert requirement.min_version == (1, 0, 0)
    assert not requirement.optional


# ---------------------------------------------------------------------------
# 2. 命令规划
# ---------------------------------------------------------------------------


def test_plan_distance_external_single_end(tmp_path: Path) -> None:
    db = tmp_path / "gtdb.syl2db.syldb"
    db.write_bytes(b"db")
    reads = _reads(tmp_path)
    plan = SylphAdapter().plan_distance(
        DistanceRequest(query=(reads,), reference=(db,), external_reference=True, threads=8),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert plan.stream is False
    sketch, profile = plan.commands
    argv = list(sketch.argv)
    assert list(argv[:2]) == ["sylph", "sketch"] and argv[argv.index("-t") + 1] == "8"
    assert argv[argv.index("-d") + 1].endswith("sketches")
    assert argv[argv.index("-d") - 1] == str(reads)
    profile_argv = list(profile.argv)
    assert list(profile_argv[:2]) == ["sylph", "profile"]
    assert Path(profile_argv[profile_argv.index("-d") + 1]) == db
    # Sketch output names are deterministic: input filename + .sylsp
    sketch_input = profile_argv[profile_argv.index("-o") - 1]
    assert sketch_input.endswith(f"sketches/{reads.name}.sylsp")
    (result_file,) = plan.result_files
    assert result_file.name == "profile.tsv"


def test_plan_distance_paired_uses_deterministic_paired_sketch(tmp_path: Path) -> None:
    db = tmp_path / "db.syldb"
    db.write_bytes(b"db")
    r1 = _reads(tmp_path, "sample_R1.fq")
    r2 = _reads(tmp_path, "sample_R2.fq")
    plan = SylphAdapter().plan_distance(
        DistanceRequest(query=(r1, r2), reference=(db,), external_reference=True),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    sketch, profile = plan.commands
    argv = list(sketch.argv)
    assert argv[argv.index("-1") + 1] == str(r1)
    assert argv[argv.index("-2") + 1] == str(r2)
    profile_argv = list(profile.argv)
    assert profile_argv[profile_argv.index("-o") - 1].endswith(f"{r1.name}.paired.sylsp")


def test_plan_distance_builds_reference_db_without_external(tmp_path: Path) -> None:
    reads = _reads(tmp_path)
    ref = _targets(tmp_path)
    plan = SylphAdapter().plan_distance(
        DistanceRequest(query=(reads,), reference=(ref,)),
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    build, _query_sketch, profile = plan.commands
    build_argv = list(build.argv)
    assert build_argv[build_argv.index("-g") + 1] == str(ref)
    # sylph appends .syldb to the -o prefix: the output name is predictable
    out_prefix = Path(build_argv[build_argv.index("-o") + 1])
    assert (out_prefix.parent / (out_prefix.name + ".syldb")) == Path(
        list(profile.argv)[list(profile.argv).index("-d") + 1]
    )


def test_plan_distance_rejects_fasta_query_and_missing_external(tmp_path: Path) -> None:
    ref = _targets(tmp_path)
    with pytest.raises(UnsupportedFeatureError, match="FASTQ"):
        SylphAdapter().plan_distance(
            DistanceRequest(query=(ref,), reference=(ref,)),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )
    missing = tmp_path / "nope.syldb"
    with pytest.raises(UnsupportedFeatureError, match="external"):
        SylphAdapter().plan_distance(
            DistanceRequest(
                query=(_reads(tmp_path),), reference=(missing,), external_reference=True
            ),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )


# ---------------------------------------------------------------------------
# 3. Parser golden tests (real sylph 1.0.0 output fixture)
# ---------------------------------------------------------------------------


def test_parse_real_profile_fixture(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "sylph" / "profile.tsv"
    record = _parse(fixture, tmp_path)[0]
    assert record.query_id == "reads.fq"
    assert record.reference_id == "ref"
    assert record.ani == pytest.approx(99.27)
    assert record.aligned_fraction == pytest.approx(3.410 / 100.0)
    assert record.containment == pytest.approx(183 / 230)
    tags = record.tags
    assert tags["metric"] == "sylph_ani"
    assert tags["genome_file"] == "ref.fa"
    assert tags["taxonomic_abundance"] == "100.0000"
    assert tags["containment_ind"] == "183/230"


def test_parse_rejects_malformed_rows(tmp_path: Path) -> None:
    bad = tmp_path / "profile.tsv"
    header = (
        "Sample_file\tGenome_file\tTaxonomic_abundance\tSequence_abundance\t"
        "Adjusted_ANI\tEff_cov\tContainment_ind"
    )
    bad.write_text(
        header + "\nreads.fq\tref.fa\t50\t50\tnan\t3.1\t1/2\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError) as exc_info:
        _parse(bad, tmp_path)
    assert exc_info.value.line_number == 2


def test_parse_empty_profile_yields_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "profile.tsv"
    empty.write_text("", encoding="utf-8")
    assert _parse(empty, tmp_path) == []


def test_parse_rejects_stream_input(tmp_path: Path) -> None:
    request = DistanceRequest(
        query=(_reads(tmp_path),), reference=(_targets(tmp_path),), external_reference=True
    )
    ctx = ParseContext(request=request, plan=_plan(), workspace=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedFeatureError, match="stream"):
        list(SylphAdapter().parse_distance(ctx, iter(["Sample_file"])))
