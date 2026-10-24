from __future__ import annotations

import itertools

from hypothesis import given
from hypothesis import strategies as st

from gseek.types import Interval, merge_intervals, union_length

_Pairs = st.lists(st.tuples(st.integers(0, 80), st.integers(0, 80)), max_size=20)


@given(_Pairs)
def test_union_length_matches_bruteforce(pairs: list[tuple[int, int]]) -> None:
    intervals = [(min(start, end), max(start, end)) for start, end in pairs]
    covered = {position for start, end in intervals for position in range(start, end)}
    assert union_length(intervals) == len(covered)


@given(_Pairs)
def test_merge_intervals_preserves_coverage_and_order(
    pairs: list[tuple[int, int]],
) -> None:
    intervals = [Interval(min(start, end), max(start, end)) for start, end in pairs]
    merged = merge_intervals(intervals)
    assert union_length(merged) == union_length(intervals)
    for previous, current in itertools.pairwise(merged):
        assert previous.end < current.start
