from __future__ import annotations

import re
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
from gseek.adapters.minimap2 import Minimap2Adapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.model import IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">arcC_1\nACGTACGT\n>arcC_2\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path, name: str = "query.fa") -> Path:
    path = tmp_path / name
    path.write_text(">contig1\nACGTACGT\n", encoding="utf-8")
    return path


def _index() -> IndexRef:
    return IndexRef(
        key="c" * 64,
        adapter="minimap2",
        adapter_version="1",
        entry="targets.mmi",
        source_sha256="d" * 64,
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
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"preset": "asm5", "secondary": "no", **(params or {})},
        target_filter=target_filter,
    )
    ctx = ParseContext(
        request=request,
        plan=_plan(),
        workspace=tmp_path,
        index=_index(),
    )
    return list(Minimap2Adapter().parse_align(ctx, iter(lines)))


def _row(
    qname: str = "contig1",
    qlen: str = "5000",
    qstart: str = "1000",
    qend: str = "1480",
    strand: str = "+",
    tname: str = "arcC_1",
    tlen: str = "480",
    tstart: str = "0",
    tend: str = "480",
    nmatch: str = "480",
    alen: str = "480",
    mapq: str = "60",
    tags: tuple[str, ...] = (),
) -> str:
    base = [
        qname,
        qlen,
        qstart,
        qend,
        strand,
        tname,
        tlen,
        tstart,
        tend,
        nmatch,
        alen,
        mapq,
    ]
    return "\t".join([*base, *tags])


def test_spec_declares_presets_and_paired_reads() -> None:
    spec = Minimap2Adapter.spec
    assert spec.name == "minimap2"
    assert spec.capabilities.inputs == frozenset({InputKind.FASTA, InputKind.FASTQ})
    assert spec.capabilities.outputs == frozenset({OutputKind.ALIGNMENT})
    assert spec.capabilities.index_scope is IndexScope.TARGET
    assert spec.capabilities.paired is True
    assert {"asm5", "asm20", "sr", "map-ont"} <= set(spec.capabilities.presets)
    tool = next(tool for tool in spec.tools if tool.binary == "minimap2")
    assert tool.min_version == (2, 24)
    assert tool.version_pattern is not None
    match = re.search(tool.version_pattern, "2.30-r1287")
    assert match is not None
    assert match.group(1) == "2.30-r1287"


def test_plan_index_uses_preset(tmp_path: Path) -> None:
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    ctx = PlanContext(workspace=index_dir, index_dir=index_dir)
    plan = Minimap2Adapter().plan_index(
        IndexRequest(target=(target,), params={"preset": "asm5"}),
        ctx,
    )
    assert plan is not None
    assert plan.entry == "targets.mmi"
    assert plan.artifacts == ("targets.mmi",)
    argv = plan.commands[0].argv
    assert argv[0] == "minimap2"
    assert argv[argv.index("-x") + 1] == "asm5"
    assert argv[argv.index("-d") + 1].endswith("targets.mmi")
    assert Path(argv[-1]).exists()


def test_plan_index_requires_preset(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFeatureError):
        Minimap2Adapter().plan_index(
            IndexRequest(target=(_targets(tmp_path),)),
            PlanContext(workspace=tmp_path / "index", index_dir=tmp_path / "index"),
        )


def test_plan_align_streams_paf_and_passes_threads(tmp_path: Path) -> None:
    query = _query(tmp_path)
    target = _targets(tmp_path)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    request = AlignRequest(
        query=(query,),
        target=(target,),
        params={"preset": "asm5"},
        threads=3,
    )
    plan = Minimap2Adapter().plan_align(
        request,
        _index(),
        PlanContext(workspace=tmp_path / "work", index_dir=index_dir),
    )
    assert plan.stream is True
    argv = plan.commands[0].argv
    assert argv[0] == "minimap2"
    assert argv[argv.index("-x") + 1] == "asm5"
    assert argv[argv.index("-t") + 1] == "3"
    assert "--secondary=no" in argv
    assert str(index_dir / "targets.mmi") in argv
    assert str(query) in argv


def test_plan_align_paired_and_cigar_flag(tmp_path: Path) -> None:
    r1 = _query(tmp_path, "r1.fq")
    r2 = _query(tmp_path, "r2.fq")
    target = _targets(tmp_path)
    request = AlignRequest(
        query=(r1, r2),
        target=(target,),
        query_kind=InputKind.FASTQ,
        paired=True,
        params={"preset": "sr", "cigar": True},
    )
    plan = Minimap2Adapter().plan_align(
        request,
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    argv = plan.commands[0].argv
    assert "-c" in argv
    assert argv[-2:] == (str(r1), str(r2))
    subject = Path(argv[-3])
    assert subject.exists()
    assert "--secondary=no" in argv


def test_plan_align_requires_preset_and_rejects_unknown_params(tmp_path: Path) -> None:
    adapter = Minimap2Adapter()
    query = _query(tmp_path)
    target = _targets(tmp_path)
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(
            AlignRequest(query=(query,), target=(target,)),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(
            AlignRequest(
                query=(query,),
                target=(target,),
                params={"preset": "asm5", "bogus": True},
            ),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )


def test_parse_primary_and_secondary_rows(tmp_path: Path) -> None:
    lines = [
        _row(tags=("tp:A:P", "NM:i:0", "AS:i:900")),
        _row(
            qname="contig2",
            qstart="0",
            qend="480",
            strand="-",
            tname="arcC_2",
            tstart="100",
            tend="580",
            nmatch="470",
            mapq="0",
            tags=("tp:A:S", "NM:i:10", "cg:Z:100M2I3D5M"),
        ),
    ]
    records = _parse(lines, tmp_path=tmp_path)
    assert len(records) == 1
    primary = records[0]
    assert primary.query_id == "contig1"
    assert primary.target_id == "arcC_1"
    assert primary.strand is Strand.PLUS
    assert primary.query_start == 1000
    assert primary.query_end == 1480
    assert primary.target_start == 0
    assert primary.target_end == 480
    assert primary.identity == 100.0
    assert primary.matches == 480
    assert primary.mapq == 60
    assert primary.query_coverage == pytest.approx(0.096)
    assert primary.target_coverage == 1.0
    assert primary.tags["identity_method"] == "paf_nm"
    assert primary.tags["NM"] == "0"
    assert primary.tags["AS"] == "900"
    assert primary.is_primary is True

    all_records = _parse(lines, params={"secondary": "all"}, tmp_path=tmp_path)
    assert len(all_records) == 2
    secondary = all_records[1]
    assert secondary.strand is Strand.MINUS
    assert secondary.is_primary is False
    assert secondary.target_start == 100
    assert secondary.target_end == 580
    assert secondary.identity == pytest.approx(470 / 480 * 100)
    assert secondary.gap_count == 5
    assert secondary.gap_openings == 2
    assert secondary.tags["cg"] == "100M2I3D5M"


def test_parse_prefers_nm_identity_and_exposes_parity_views(tmp_path: Path) -> None:
    lines = [
        _row(
            nmatch="470",
            tags=("tp:A:P", "NM:i:20"),
        )
    ]
    record = _parse(lines, tmp_path=tmp_path)[0]
    assert record.tags["identity_method"] == "paf_nm"
    assert record.identity == pytest.approx((480 - 20) / 480 * 100)
    assert record.identity_from_nm == pytest.approx((480 - 20) / 480 * 100)
    assert record.identity_from_matches == pytest.approx(470 / 480 * 100)
    assert record.identity_nmatch_nm == pytest.approx(470 / (470 + 20) * 100)


def test_parse_falls_back_to_nmatch_without_nm(tmp_path: Path) -> None:
    lines = [_row(nmatch="470", tags=("tp:A:P",))]
    record = _parse(lines, tmp_path=tmp_path)[0]
    assert record.tags["identity_method"] == "paf_matches"
    assert record.identity == pytest.approx(470 / 480 * 100)
    assert record.identity_from_nm is None


def test_parse_filters_target_ids(tmp_path: Path) -> None:
    lines = [
        _row(tags=("tp:A:P",)),
        _row(qname="contig2", tname="arcC_2", tags=("tp:A:P",)),
    ]
    records = _parse(lines, target_filter=frozenset({"arcC_2"}), tmp_path=tmp_path)
    assert [record.target_id for record in records] == ["arcC_2"]


def test_parse_rejects_malformed_rows(tmp_path: Path) -> None:
    with pytest.raises(ParseError):
        _parse(["too\tfew\tfields"], tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse([_row(strand="?")], tmp_path=tmp_path)


def test_index_and_semantic_params() -> None:
    adapter = Minimap2Adapter()
    assert adapter.index_params({"preset": "asm5", "k": 19, "threads": 4, "evalue": 1}) == {
        "preset": "asm5",
        "k": 19,
    }
    assert adapter.semantic_params({"preset": "asm5", "threads": 4, "debug": True}) == {
        "preset": "asm5"
    }


def test_plan_align_passes_speed_profile_flags(tmp_path: Path) -> None:
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"preset": "asm5", "secondary": "no", "w": 15, "e": 1000, "U": "50,1000"},
    )
    plan = Minimap2Adapter().plan_align(request, None, PlanContext(workspace=tmp_path / "work"))
    argv = plan.commands[0].argv
    assert argv[argv.index("-w") + 1] == "15"
    assert argv[argv.index("-e") + 1] == "1000"
    assert argv[argv.index("-U") + 1] == "50,1000"
