"""Consumer parity against captured real-tool output.

The expected formulas come from the consumer implementations:

- gside BLAST: identity=pident, coverage=aligned_length/qlen*100.
- gside PAF: identity=100*nmatch/alen, breadth from target spans.
- gapit BLAST: 15-column outfmt with sstrand/stitle and 1-based coordinates.
- gapit PAF: identity=100*(alen-NM)/alen.
- gmlst BLAST: query=allele, subject=contig, coverage=alen/qlen.
- gmlst PAF: identity=100*nmatch/(nmatch+NM), coverage=(qend-qstart)/qlen.
- gmlst KMA: identity=Template_Identity, coverage=Template_Coverage/100.
- gmlst MUMmer: identity=[% IDY], coverage=COVQ/100; gmlst hardcodes "+".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gseek.adapter import AlignPlan, AlignRequest, DistanceRequest, ParseContext
from gseek.adapters.blast import BlastAdapter
from gseek.adapters.bwa import BwaAdapter
from gseek.adapters.kma import KmaAdapter
from gseek.adapters.mash import MashAdapter
from gseek.adapters.minimap2 import Minimap2Adapter
from gseek.adapters.mummer import MummerAdapter
from gseek.adapters.skani import SkaniAdapter
from gseek.adapters.sourmash import SourmashAdapter
from gseek.index import IndexPolicy
from gseek.types import InputKind, JsonValue, Strand

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _align_context(request: AlignRequest, plan: AlignPlan, workspace: Path) -> ParseContext:
    return ParseContext(request=request, plan=plan, workspace=workspace)


def _distance_context(request: DistanceRequest, plan: AlignPlan, workspace: Path) -> ParseContext:
    return ParseContext(
        request=request,  # type: ignore[arg-type]
        plan=plan,
        workspace=workspace,
    )


def _align_request(*, params: dict[str, JsonValue] | None = None) -> AlignRequest:
    return AlignRequest(
        query=(Path("query.fa"),),
        target=(Path("targets.fa"),),
        query_kind=InputKind.FASTA,
        params=params or {},
        index_policy=IndexPolicy.NONE,
    )


def test_blast_fixture_parity_with_gside_and_gapit(tmp_path: Path) -> None:
    lines = (_FIXTURES / "blast" / "blastn_nucl.tsv").read_text(encoding="utf-8").splitlines()
    ctx = _align_context(
        _align_request(),
        AlignPlan(commands=(), stream=True),
        tmp_path,
    )
    records = list(BlastAdapter().parse_align(ctx, iter(lines)))

    assert len(records) == 2
    forward, reverse = records
    assert (forward.query_id, forward.target_id) == ("q1", "t1")
    assert (reverse.query_id, reverse.target_id) == ("q2", "t2")
    assert forward.strand is Strand.PLUS
    assert reverse.strand is Strand.MINUS
    assert forward.identity == pytest.approx(100.0)
    assert forward.aligned_length == 300
    assert forward.query_length == 300
    assert forward.target_length == 600
    assert (forward.query_start, forward.query_end) == (0, 300)
    assert (forward.target_start, forward.target_end) == (100, 400)
    assert forward.target_description == "t1"

    # gside parity: coverage is aligned_length / length, not string length.
    assert 100.0 * forward.aligned_length / forward.query_length == pytest.approx(100.0)
    assert 100.0 * forward.aligned_length / forward.target_length == pytest.approx(50.0)
    # gapit parity: 1-based inclusive BLAST coordinates become 0-based half-open.
    assert (reverse.target_start, reverse.target_end) == (100, 400)
    assert reverse.strand is Strand.MINUS


def test_minimap2_fixture_parity_with_gapit_and_gmlst(tmp_path: Path) -> None:
    lines = (_FIXTURES / "minimap2" / "align.paf").read_text(encoding="utf-8").splitlines()
    ctx = _align_context(
        _align_request(params={"preset": "asm5"}),
        AlignPlan(commands=(), stream=True),
        tmp_path,
    )
    records = list(Minimap2Adapter().parse_align(ctx, iter(lines)))

    assert len(records) == 2
    forward, reverse = records
    assert (forward.query_id, forward.target_id) == ("q1", "t1")
    assert (reverse.query_id, reverse.target_id) == ("q2", "t2")
    assert reverse.strand is Strand.MINUS
    assert forward.aligned_length == 300
    assert forward.matches == 300
    assert forward.mapq == 60
    assert forward.target_coverage == pytest.approx(0.5)
    assert forward.query_coverage == pytest.approx(1.0)

    # gapit parity: NM-first identity.
    assert forward.identity_method == "paf_nm"
    assert forward.identity == pytest.approx(forward.identity_from_nm or 0.0)
    assert forward.identity_from_matches == pytest.approx(100.0)
    # gmlst parity: nmatch / (nmatch + NM).
    assert forward.identity_nmatch_nm == pytest.approx(100.0)
    assert forward.tags["cg"] == "300M"


def test_mummer_fixture_parity_with_gmlst_and_strand_fix(tmp_path: Path) -> None:
    lines = (_FIXTURES / "mummer" / "coords.txt").read_text(encoding="utf-8").splitlines()
    ctx = _align_context(
        _align_request(),
        AlignPlan(commands=(), stream=True),
        tmp_path,
    )
    records = list(MummerAdapter().parse_align(ctx, iter(lines)))

    assert len(records) == 2
    forward, reverse = records
    assert (forward.query_id, forward.target_id) == ("q1", "t1")
    assert forward.identity == pytest.approx(100.0)
    assert forward.aligned_length == 300
    assert forward.target_coverage == pytest.approx(0.5)
    assert forward.query_coverage == pytest.approx(1.0)
    assert (forward.target_start, forward.target_end) == (100, 400)
    assert (forward.query_start, forward.query_end) == (0, 300)

    # gmlst derived coverage from COV Q / 100.
    assert forward.query_coverage == pytest.approx(100.0 / 100.0)
    # Intentional divergence: gmlst hardcodes "+"; gseek restores S2 > E2.
    assert reverse.strand is Strand.MINUS


def test_kma_fixture_parity_with_gmlst(tmp_path: Path) -> None:
    fixture = _FIXTURES / "kma" / "align.res"
    request = _align_request()
    plan = AlignPlan(commands=(), stream=False, result_files=(fixture,))
    ctx = _align_context(request, plan, tmp_path)
    records = list(KmaAdapter().parse_align(ctx))

    assert len(records) == 2
    for record, target in zip(records, ("t1", "t2"), strict=True):
        assert record.query_id == "query"
        assert record.target_id == target
        assert record.target_length == 600
        assert record.identity == pytest.approx(50.0)
        assert record.target_coverage == pytest.approx(0.5)
        assert record.query_coverage == pytest.approx(2.0)
        assert record.depth is None
        assert record.query_interval is None
        assert record.target_interval is None
        assert record.tags["identity_method"] == "kma_template"
        assert record.tags["query_identity"] == "100.00"


def test_bwa_fixture_coordinates_and_identity(tmp_path: Path) -> None:
    lines = (_FIXTURES / "bwa" / "align.sam").read_text(encoding="utf-8").splitlines()
    request = AlignRequest(
        query=(Path("query.fa"),),
        target=(Path("targets.fa"),),
        query_kind=InputKind.FASTA,
    )
    ctx = _align_context(request, AlignPlan(commands=(), stream=True), tmp_path)
    records = list(BwaAdapter().parse_align(ctx, iter(lines)))

    assert len(records) == 2
    forward, reverse = records
    assert (forward.query_id, forward.target_id) == ("q1", "t1")
    assert forward.target_length == 600
    assert forward.query_length == 300
    assert (forward.query_start, forward.query_end) == (0, 300)
    assert (forward.target_start, forward.target_end) == (100, 400)
    assert forward.identity == pytest.approx(100.0)
    assert forward.mapq == 60
    assert forward.tags["NM"] == "0"
    assert reverse.strand is Strand.MINUS


def test_mash_dist_and_screen_fixtures(tmp_path: Path) -> None:
    dist_lines = (_FIXTURES / "mash" / "dist.tsv").read_text(encoding="utf-8").splitlines()
    request = DistanceRequest(
        query=(Path("query.fa"),),
        reference=(Path("target1.fa"), Path("target2.fa")),
    )
    ctx = _distance_context(request, AlignPlan(commands=(), stream=True), tmp_path)
    records = list(MashAdapter().parse_distance(ctx, iter(dist_lines)))
    assert len(records) == 2
    assert [record.reference_id for record in records] == ["target1.fa", "target2.fa"]
    assert records[0].query_id == "query.fa"
    assert records[0].distance == pytest.approx(0.0338498)
    assert records[0].shared_hashes == 280
    assert records[0].total_hashes == 860
    assert records[0].tags["metric"] == "mash_distance"

    screen_lines = (_FIXTURES / "mash" / "screen.tsv").read_text(encoding="utf-8").splitlines()
    screen_request = DistanceRequest(
        query=(Path("reads.fq"),),
        reference=(Path("target1.fa"), Path("target2.fa")),
        params={"mode": "screen"},
    )
    screen_ctx = _distance_context(screen_request, AlignPlan(commands=(), stream=True), tmp_path)
    screened = list(MashAdapter().parse_distance(screen_ctx, iter(screen_lines)))
    assert len(screened) == 2
    assert screened[0].query_id == "reads"
    assert screened[0].containment == pytest.approx(0.931263)
    assert screened[0].distance == pytest.approx(1.0 - 0.931263)
    assert screened[0].tags["metric"] == "mash_screen"


def test_sourmash_search_and_gather_fixtures(tmp_path: Path) -> None:
    search = _FIXTURES / "sourmash" / "search.csv"
    request = DistanceRequest(
        query=(Path("query.fa"),),
        reference=(Path("target1.fa"), Path("target2.fa")),
        params={"k": 21, "scaled": 10},
    )
    search_plan = AlignPlan(commands=(), stream=False, result_files=(search,))
    records = list(
        SourmashAdapter().parse_distance(_distance_context(request, search_plan, tmp_path))
    )
    assert len(records) == 2
    assert {record.reference_id for record in records} == {
        "3a00f8ea860bf8b8af7c1669573ed0fc",
        "3f913b40076613bce00ebd8290eab4af",
    }
    assert {record.query_id for record in records} == {"query.fa"}
    assert records[0].jaccard is not None
    assert records[0].distance == pytest.approx(1.0 - (records[0].jaccard or 0.0))
    assert all(record.tags["metric"] == "sourmash_similarity" for record in records)
    assert all(record.ani is None for record in records)

    gather = _FIXTURES / "sourmash" / "gather.csv"
    gather_request = DistanceRequest(
        query=(Path("query.fa"),),
        reference=(Path("target1.fa"), Path("target2.fa")),
        params={"mode": "gather", "k": 21, "scaled": 10},
    )
    gather_plan = AlignPlan(commands=(), stream=False, result_files=(gather,))
    gathered = list(
        SourmashAdapter().parse_distance(_distance_context(gather_request, gather_plan, tmp_path))
    )
    assert len(gathered) == 2
    assert {record.reference_id for record in gathered} == {
        "3a00f8ea860bf8b8af7c1669573ed0fc",
        "3f913b40076613bce00ebd8290eab4af",
    }
    containments = sorted(
        record.containment for record in gathered if record.containment is not None
    )
    assert containments == pytest.approx([0.3770491803278688, 0.6229508196721312])
    assert all(record.tags["metric"] == "sourmash_gather" for record in gathered)


def test_skani_fixture_ani_and_aligned_fraction(tmp_path: Path) -> None:
    lines = (_FIXTURES / "skani" / "search.tsv").read_text(encoding="utf-8").splitlines()
    request = DistanceRequest(
        query=(Path("skani_query.fa"),),
        reference=(Path("skani_target.fa"),),
    )
    ctx = _distance_context(request, AlignPlan(commands=(), stream=True), tmp_path)
    records = list(SkaniAdapter().parse_distance(ctx, iter(lines)))

    assert len(records) == 1
    record = records[0]
    assert (record.query_id, record.reference_id) == ("skani_q", "skani_t")
    assert record.ani == pytest.approx(100.0)
    assert record.aligned_fraction == pytest.approx(0.9876)
    assert record.tags["metric"] == "skani_ani"
    assert record.tags["align_fraction_ref"] == "98.76"
