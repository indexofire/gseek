from __future__ import annotations

import json

import pytest

from gseek.errors import UsageError
from gseek.model import AlignmentRecord, AlignmentResult, AlignmentStats, Issue
from gseek.views import (
    count_distinct_occurrences,
    deduplicate_intervals,
    to_jsonl,
    to_tsv,
)


def _result(records: tuple[AlignmentRecord, ...]) -> AlignmentResult:
    stats = AlignmentStats(
        record_count=len(records),
        query_count=1,
        target_count=1,
        queries_with_hits=1,
        targets_with_hits=1,
        aligned_bases=sum(record.aligned_bases for record in records),
        runtime_s=0.0,
    )
    return AlignmentResult(
        backend="fake",
        records=records,
        inputs=(),
        targets=(),
        params={},
        stats=stats,
    )


def test_deduplicate_intervals_keeps_first_and_ignores_strand() -> None:
    first = AlignmentRecord(
        query_id="q",
        target_id="t1",
        query_start=0,
        query_end=10,
        target_start=0,
        target_end=10,
    )
    duplicate = AlignmentRecord(
        query_id="q",
        target_id="t2",
        query_start=0,
        query_end=10,
        target_start=5,
        target_end=15,
    )
    other = AlignmentRecord(
        query_id="q",
        target_id="t1",
        query_start=20,
        query_end=30,
        target_start=20,
        target_end=30,
    )
    assert deduplicate_intervals((first, duplicate, other)) == (first, other)


def test_count_distinct_occurrences() -> None:
    first = AlignmentRecord(
        query_id="contig1",
        target_id="allele1",
        query_start=0,
        query_end=10,
        target_start=0,
        target_end=10,
    )
    overlapping = AlignmentRecord(
        query_id="contig1",
        target_id="allele1",
        query_start=0,
        query_end=10,
        target_start=2,
        target_end=12,
    )
    second_contig = AlignmentRecord(
        query_id="contig2",
        target_id="allele1",
        query_start=0,
        query_end=10,
        target_start=0,
        target_end=10,
    )
    assert (
        count_distinct_occurrences(
            (first, overlapping, second_contig), query_id="contig1", target_id="allele1"
        )
        == 1
    )
    assert (
        count_distinct_occurrences(
            (first, overlapping, second_contig), query_id="contig2", target_id="allele1"
        )
        == 1
    )


def test_to_tsv_rejects_unknown_columns() -> None:
    result = _result(())
    with pytest.raises(UsageError):
        to_tsv(result, columns=("query_id", "nope"))


def test_deduplicate_intervals_keeps_records_without_coordinates() -> None:
    first = AlignmentRecord(query_id="q", target_id="t1", identity=90.0)
    second = AlignmentRecord(query_id="q", target_id="t2", identity=80.0)
    assert deduplicate_intervals((first, second)) == (first, second)


def test_to_jsonl_footer_reflects_errors() -> None:
    result = _result(())
    result_with_errors = AlignmentResult(
        backend="fake",
        records=(),
        inputs=(),
        targets=(),
        params={},
        stats=result.stats,
        errors=(Issue(code="X", message="x", severity="error"),),
    )
    footer = json.loads(to_jsonl(result_with_errors).strip().splitlines()[-1])
    assert footer["status"] == "error"
