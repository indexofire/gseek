from __future__ import annotations

import contextlib
from typing import Any

import pytest

from gseek.errors import InputError
from gseek.model import (
    AlignmentRecord,
    AlignmentResult,
    DistanceRecord,
    DistanceResult,
    DistanceStats,
    InputRef,
    build_alignment_stats,
)
from gseek.types import DISTANCE_SCHEMA, InputKind


def _record(
    *,
    query_id: str = "q1",
    target_id: str = "t1",
    identity: float = 90.0,
    aligned_length: int = 50,
    query_start: int = 10,
    query_end: int = 60,
    target_start: int = 20,
    target_end: int = 80,
    depth: float | None = None,
    **kwargs: Any,
) -> AlignmentRecord:
    return AlignmentRecord(
        query_id=query_id,
        target_id=target_id,
        query_length=100,
        target_length=200,
        query_start=query_start,
        query_end=query_end,
        target_start=target_start,
        target_end=target_end,
        identity=identity,
        aligned_length=aligned_length,
        gap_count=5,
        depth=depth,
        **kwargs,
    )


def test_record_derives_coverage_and_ungapped_coverage() -> None:
    record = _record()
    assert record.query_coverage == pytest.approx(0.5)
    assert record.target_coverage == pytest.approx(0.3)
    assert record.ungapped_query_coverage == pytest.approx(45 / 100)
    assert record.ungapped_target_coverage == pytest.approx(45 / 200)
    assert record.identity_fraction == pytest.approx(0.9)
    assert record.aligned_bases == 50


def test_record_can_carry_explicit_coverage_without_coordinates() -> None:
    record = AlignmentRecord(
        query_id="q",
        target_id="t",
        query_coverage=0.25,
        target_coverage=1.75,
        identity=99.0,
    )
    assert record.query_coverage == 0.25
    assert record.target_coverage == 1.75
    assert record.query_interval is None


def test_record_validation() -> None:
    with pytest.raises(InputError):
        AlignmentRecord(query_id="", target_id="t")
    with pytest.raises(InputError):
        AlignmentRecord(query_id="q", target_id="t", identity=101)
    with pytest.raises(InputError):
        AlignmentRecord(query_id="q", target_id="t", query_start=1)
    with pytest.raises(InputError):
        AlignmentRecord(query_id="q", target_id="t", query_start=5, query_end=4)


def _alignment_record(**extra: Any) -> AlignmentRecord:
    return AlignmentRecord(query_id="q", target_id="t", **extra)


def _distance_record(**extra: Any) -> DistanceRecord:
    return DistanceRecord(query_id="q", reference_id="r", **extra)


def test_record_rejects_non_finite_floats() -> None:
    # NaN compares False against everything and would crash canonical JSON
    # hashing far away from its origin; reject it at the model boundary.
    for field, value in (
        ("evalue", float("nan")),
        ("bit_score", float("inf")),
        ("identity", float("nan")),
        ("query_coverage", float("nan")),
        ("depth", float("inf")),
    ):
        with pytest.raises(InputError, match=field):
            _alignment_record(**{field: value})


def test_distance_record_rejects_non_finite_floats() -> None:
    for field, value in (
        ("distance", float("nan")),
        ("pvalue", float("inf")),
        ("containment", float("nan")),
        ("aligned_fraction", float("inf")),
        ("ani", float("nan")),
    ):
        with pytest.raises(InputError, match=field):
            _distance_record(**{field: value})


def test_result_sorts_records_and_picks_best_per_pair() -> None:
    low = _record(identity=80.0)
    high = _record(identity=99.0, target_start=30, target_end=90)
    other = _record(query_id="q2", identity=95.0)
    stats = build_alignment_stats(
        (low, high, other),
        query_count=2,
        target_count=1,
        runtime_s=0.1,
    )
    result = AlignmentResult(
        backend="fake",
        records=(low, high, other),
        inputs=(),
        targets=(),
        params={},
        stats=stats,
    )
    assert [record.query_id for record in result.records] == ["q1", "q1", "q2"]
    best = result.best_per_query_target()
    assert len(best) == 2
    assert best[0].identity == 99.0
    assert result.primary_only() == result.records


def test_summarize_by_target_breadth_and_weighted_identity() -> None:
    first = _record(identity=90.0, target_start=0, target_end=10, aligned_length=10)
    second = _record(identity=100.0, target_start=5, target_end=15, aligned_length=10)
    stats = build_alignment_stats((first, second), query_count=2, target_count=1, runtime_s=0)
    result = AlignmentResult(
        backend="fake",
        records=(first, second),
        inputs=(),
        targets=(),
        params={},
        stats=stats,
    )
    summary = result.summarize_by_target()[0]
    assert summary.target_length == 200
    assert summary.covered_bases == 15
    assert summary.breadth == pytest.approx(0.075)
    assert summary.weighted_identity == pytest.approx(95.0)


def test_summarize_by_query() -> None:
    record = _record(identity=88.0)
    stats = build_alignment_stats((record,), query_count=1, target_count=1, runtime_s=0)
    result = AlignmentResult(
        backend="fake",
        records=(record,),
        inputs=(),
        targets=(),
        params={},
        stats=stats,
    )
    summary = result.summarize_by_query()[0]
    assert summary.query_id == "q1"
    assert summary.target_count == 1
    assert summary.best_identity == 88.0


def test_alignment_record_json_round_trip() -> None:
    record = _record(target_description="target one", tags={"identity_method": "fake"})
    restored = AlignmentRecord.from_dict(record.to_dict())
    assert restored == record
    assert restored.target_description == "target one"


def test_distance_result_best_per_query_and_sorting() -> None:
    far = DistanceRecord(query_id="q1", reference_id="r1", distance=0.2)
    near = DistanceRecord(query_id="q1", reference_id="r2", distance=0.01)
    other = DistanceRecord(query_id="q2", reference_id="r1", distance=0.1)
    stats = DistanceStats(record_count=3, query_count=2, reference_count=2, runtime_s=0)
    result = DistanceResult(
        backend="mash",
        records=(far, other, near),
        inputs=(),
        references=(),
        params={},
        stats=stats,
    )
    assert result.schema == DISTANCE_SCHEMA
    assert [record.reference_id for record in result.records] == ["r1", "r2", "r1"]
    best = result.best_per_query()
    assert {record.query_id: record.reference_id for record in best} == {"q1": "r2", "q2": "r1"}


def test_alignment_result_order_is_total_for_tied_sort_keys() -> None:
    low = _record(identity=80.0)
    high = _record(identity=90.0)
    assert low.sort_key() == high.sort_key()

    stats = build_alignment_stats((low, high), query_count=1, target_count=1, runtime_s=0)
    forward = AlignmentResult(
        backend="fake",
        records=(low, high),
        inputs=(),
        targets=(),
        params={},
        stats=stats,
    )
    backward = AlignmentResult(
        backend="fake",
        records=(high, low),
        inputs=(),
        targets=(),
        params={},
        stats=stats,
    )
    assert [record.identity for record in forward.records] == [
        record.identity for record in backward.records
    ]


def test_distance_result_order_is_total_for_tied_keys() -> None:
    first = DistanceRecord(query_id="q", reference_id="r", distance=0.1, shared_hashes=1)
    second = DistanceRecord(query_id="q", reference_id="r", distance=0.1, shared_hashes=2)
    assert first.sort_key == second.sort_key
    stats = DistanceStats(record_count=2, query_count=1, reference_count=1, runtime_s=0)
    forward = DistanceResult(
        backend="mash",
        records=(first, second),
        inputs=(),
        references=(),
        params={},
        stats=stats,
    )
    backward = DistanceResult(
        backend="mash",
        records=(second, first),
        inputs=(),
        references=(),
        params={},
        stats=stats,
    )
    assert forward.records == backward.records


def test_input_ref_round_trip() -> None:
    reference = InputRef(
        path="/tmp/x.fa",
        size=10,
        sha256="a" * 64,
        kind=InputKind.FASTA,
        records=1,
        compressed=False,
    )
    assert InputRef.from_dict(reference.to_dict()) == reference


def test_tags_participate_in_record_equality() -> None:
    # tags carry identity provenance (NM, identity_method): records differing
    # only in tags are distinct observations, consistent with stable_key().
    base = {"query_id": "q", "target_id": "t", "identity": 99.0}
    plain = AlignmentRecord(**base)
    same = AlignmentRecord(**base)
    with_nm = AlignmentRecord(**base, tags={"NM": "3"})
    assert plain == same
    assert plain != with_nm
    # Hashing must stay usable (tags stay out of the hash; equal records
    # still hash equally, so the eq/hash contract holds).
    assert hash(plain) == hash(same)
    assert len({plain, same, with_nm}) == 2


def test_record_rejects_interval_beyond_length() -> None:
    # Out-of-bounds coordinates would silently derive coverage > 1.0 and
    # skew best-hit selection.
    with pytest.raises(InputError, match="query_end"):
        AlignmentRecord(query_id="q", target_id="t", query_start=0, query_end=600, query_length=480)
    with pytest.raises(InputError, match="target_end"):
        AlignmentRecord(query_id="q", target_id="t", target_start=0, target_end=9, target_length=8)
    # end == length is a legitimate full-length hit.
    AlignmentRecord(query_id="q", target_id="t", query_start=0, query_end=480, query_length=480)


def test_summarize_uses_max_length_on_conflict() -> None:
    # Same target_id with inconsistent lengths must not silently depend on
    # sort position; the largest observed length wins deterministically.
    first_sorted = AlignmentRecord(
        query_id="q",
        target_id="t",
        identity=90.0,
        query_start=0,
        query_end=10,
        query_length=100,
        target_start=0,
        target_end=10,
        target_length=100,
    )
    later = AlignmentRecord(
        query_id="q",
        target_id="t",
        identity=99.0,
        query_start=0,
        query_end=10,
        query_length=100,
        target_start=0,
        target_end=10,
        target_length=200,
    )
    stats = build_alignment_stats(
        (first_sorted, later), query_count=1, target_count=1, runtime_s=0.0
    )
    result = AlignmentResult(
        backend="fake", records=(first_sorted, later), inputs=(), targets=(), stats=stats, params={}
    )
    summary = result.summarize_by_target()[0]
    assert summary.target_length == 200, "must not depend on which record sorts first"


def test_tags_are_immutable_after_construction() -> None:
    record = AlignmentRecord(query_id="q", target_id="t", tags={"NM": "1"})
    original = dict(record.tags)
    with contextlib.suppress(TypeError):
        record.tags["NM"] = "999"  # type: ignore[index]
    assert dict(record.tags) == original
