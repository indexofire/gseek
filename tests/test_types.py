from __future__ import annotations

import pytest

from gseek.types import (
    Interval,
    covered_fraction,
    merge_intervals,
    union_length,
)


def test_interval_basic_properties() -> None:
    interval = Interval(10, 20)
    assert interval.length == 10
    assert interval.contains(10)
    assert interval.contains(19)
    assert not interval.contains(20)
    assert interval.overlaps(Interval(19, 30))
    assert not interval.overlaps(Interval(20, 30))
    assert interval.intersection(Interval(15, 25)) == Interval(15, 20)


def test_interval_validation() -> None:
    with pytest.raises(ValueError):
        Interval(-1, 5)
    with pytest.raises(ValueError):
        Interval(5, 4)


def test_union_length_handles_overlap_adjacency_and_containment() -> None:
    assert union_length([]) == 0
    assert union_length([(1, 5), (3, 8), (7, 10)]) == 9
    assert union_length([(1, 5), (5, 8)]) == 7
    assert union_length([(1, 10), (3, 4), (2, 9)]) == 9
    assert union_length([Interval(1, 3), (2, 6)]) == 5


def test_union_length_rejects_invalid_tuples() -> None:
    with pytest.raises(ValueError):
        union_length([(-1, 5)])
    with pytest.raises(ValueError):
        union_length([(5, 4)])


def test_merge_intervals() -> None:
    assert merge_intervals([]) == ()
    assert merge_intervals([Interval(5, 8), Interval(1, 3), Interval(2, 6)]) == (Interval(1, 8),)
    assert merge_intervals([Interval(1, 2), Interval(3, 4)]) == (
        Interval(1, 2),
        Interval(3, 4),
    )


def test_covered_fraction() -> None:
    assert covered_fraction([Interval(0, 5)], 10) == 0.5
    assert covered_fraction([], 10) == 0.0
    assert covered_fraction([Interval(0, 5)], None) is None
    assert covered_fraction([Interval(0, 5)], 0) is None
