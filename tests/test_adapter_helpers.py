from __future__ import annotations

import pytest

from gseek.adapters.base import (
    iter_data_lines,
    parse_cigar,
    parse_sam_tags,
    parse_tag_fields,
    reverse_complement,
    split_tab,
)
from gseek.errors import ParseError


def test_iter_data_lines_strips_crlf_and_skips_comments() -> None:
    lines = ["# header\r\n", "\r\n", "q1\tt1\r\n"]
    assert list(iter_data_lines(lines)) == [(3, "q1\tt1")]


def test_parse_cigar_consumption_and_gaps() -> None:
    stats = parse_cigar("10M2I3D5M")
    assert stats.query_consumed == 17
    assert stats.reference_consumed == 18
    assert stats.aligned_columns == 15
    assert stats.insertions == 2
    assert stats.deletions == 3
    assert stats.gap_count == 5
    assert stats.gap_runs == 2


def test_parse_cigar_with_clipping() -> None:
    stats = parse_cigar("5S10M2H")
    assert stats.query_consumed == 15
    assert stats.reference_consumed == 10
    assert stats.soft_clips == 5
    assert stats.hard_clips == 2


def test_parse_cigar_rejects_invalid() -> None:
    with pytest.raises(ParseError):
        parse_cigar("10Mfoo")
    with pytest.raises(ParseError):
        parse_cigar("M10")


def test_parse_tags() -> None:
    assert parse_tag_fields(["NM:i:3", "tp:A:P", "bad", "x:y"]) == {
        "NM": "3",
        "tp": "P",
    }
    assert parse_sam_tags(["AS:i:100", "MD:Z:10"]) == {"AS": "100", "MD": "10"}


def test_reverse_complement() -> None:
    assert reverse_complement("ACGTN") == "NACGT"
    assert reverse_complement("acgt") == "acgt"
    assert reverse_complement("acgt", preserve_case=False) == "ACGT"


def test_split_tab_enforces_min_fields() -> None:
    assert split_tab("a\tb\tc", min_fields=2) == ["a", "b", "c"]
    with pytest.raises(ParseError):
        split_tab("a\tb", min_fields=3)


def test_parse_cigar_tracks_leading_and_trailing_clips() -> None:
    stats = parse_cigar("2H3S10M4S1H")
    assert stats.leading_hard_clip == 2
    assert stats.leading_soft_clip == 3
    assert stats.trailing_soft_clip == 4
    assert stats.trailing_hard_clip == 1
    assert stats.soft_clips == 7
    assert stats.hard_clips == 3
