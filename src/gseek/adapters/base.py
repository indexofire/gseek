"""Small parsing helpers shared by concrete adapters.

No adapter-specific policy lives here: just tab/tag splitting, CIGAR arithmetic,
and nucleotide helpers. These helpers are intentionally pure and
unit-testable without external tools.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gseek.errors import ParseError

if TYPE_CHECKING:
    from gseek.adapter import ParseContext

_CIGAR_RE = re.compile(r"(\d+)([MIDNSHP=X])")
_COMPLEMENT = str.maketrans("ACGTUNacgtun", "TGCAANtgcaan")


def iter_data_lines(
    stream: Iterable[str],
    *,
    skip_prefixes: tuple[str, ...] = ("#",),
    start_line: int = 1,
) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, stripped_line)`` for non-empty data lines."""
    for offset, raw in enumerate(stream):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if any(line.startswith(prefix) for prefix in skip_prefixes):
            continue
        yield start_line + offset, line


def split_tab(
    line: str,
    *,
    path: str | None = None,
    line_number: int | None = None,
    min_fields: int = 1,
) -> list[str]:
    """Split a tab-delimited line and enforce a minimum field count."""
    fields = line.split("\t")
    if len(fields) < min_fields:
        raise ParseError(
            f"expected at least {min_fields} tab-delimited fields, got {len(fields)}",
            path=path,
            line_number=line_number,
            raw_line=line,
        )
    return fields


def parse_tag_fields(fields: Iterable[str]) -> dict[str, str]:
    """Parse ``KEY:TYPE:VALUE`` tags (PAF/SAM) into a plain mapping.

    The type character is dropped; the value is preserved verbatim. Malformed
    tags without at least two colons are skipped rather than failing the whole
    record, matching the tolerant behaviour of the consumers.
    """
    tags: dict[str, str] = {}
    for raw in fields:
        key, separator, rest = raw.partition(":")
        if not separator:
            continue
        _type, separator, value = rest.partition(":")
        if not separator:
            continue
        tags[key] = value
    return tags


def parse_sam_tags(fields: Iterable[str]) -> dict[str, str]:
    """Parse SAM optional fields (``NM:i:3`` → ``{"NM": "3"}``)."""
    return parse_tag_fields(fields)


@dataclass(frozen=True, slots=True)
class CigarStats:
    """Consumption and gap counts derived from a SAM CIGAR string."""

    query_consumed: int
    reference_consumed: int
    aligned_columns: int
    insertions: int
    deletions: int
    soft_clips: int
    hard_clips: int
    matches: int
    mismatches: int
    gap_runs: int = 0
    leading_soft_clip: int = 0
    trailing_soft_clip: int = 0
    leading_hard_clip: int = 0
    trailing_hard_clip: int = 0

    @property
    def gap_count(self) -> int:
        return self.insertions + self.deletions

    @property
    def gap_openings(self) -> int:
        """Number of indel runs (each contiguous I/D run counts once)."""
        return self.gap_runs


def parse_cigar(cigar: str) -> CigarStats:
    """Parse a SAM CIGAR string into consumption/gap statistics.

    ``M``, ``=`` and ``X`` consume both query and reference; ``I``/``S``
    consume query; ``D``/``N`` consume reference; ``H``/``P`` consume neither.
    """
    if not cigar or cigar == "*":
        return CigarStats(0, 0, 0, 0, 0, 0, 0, 0, 0)
    query_consumed = 0
    reference_consumed = 0
    aligned_columns = 0
    insertions = 0
    deletions = 0
    soft_clips = 0
    hard_clips = 0
    matches = 0
    mismatches = 0
    gap_runs = 0
    operations: list[tuple[str, int]] = []
    position = 0
    for match in _CIGAR_RE.finditer(cigar):
        if match.start() != position:
            raise ParseError(f"invalid CIGAR string: {cigar!r}")
        position = match.end()
        length = int(match.group(1))
        operation = match.group(2)
        operations.append((operation, length))
        if operation in ("M", "=", "X"):
            query_consumed += length
            reference_consumed += length
            aligned_columns += length
            if operation == "=":
                matches += length
            elif operation == "X":
                mismatches += length
        elif operation in ("I", "S"):
            query_consumed += length
            if operation == "I":
                insertions += length
                gap_runs += 1
            else:
                soft_clips += length
        elif operation in ("D", "N"):
            reference_consumed += length
            if operation == "D":
                deletions += length
                gap_runs += 1
        elif operation == "H":
            hard_clips += length
        elif operation == "P":
            continue
        else:  # pragma: no cover - regex restricts operations
            raise ParseError(f"unknown CIGAR operation {operation!r} in {cigar!r}")
    if position != len(cigar):
        raise ParseError(f"invalid CIGAR string: {cigar!r}")
    leading_soft = 0
    leading_hard = 0
    for operation, length in operations:
        if operation == "S":
            leading_soft += length
        elif operation == "H":
            leading_hard += length
        else:
            break
    trailing_soft = 0
    trailing_hard = 0
    for operation, length in reversed(operations):
        if operation == "S":
            trailing_soft += length
        elif operation == "H":
            trailing_hard += length
        else:
            break
    return CigarStats(
        query_consumed=query_consumed,
        reference_consumed=reference_consumed,
        aligned_columns=aligned_columns,
        insertions=insertions,
        deletions=deletions,
        soft_clips=soft_clips,
        hard_clips=hard_clips,
        matches=matches,
        mismatches=mismatches,
        gap_runs=gap_runs,
        leading_soft_clip=leading_soft,
        trailing_soft_clip=trailing_soft,
        leading_hard_clip=leading_hard,
        trailing_hard_clip=trailing_hard,
    )


__all__ = [
    "CigarStats",
    "finite_float",
    "float_or_none",
    "iter_data_lines",
    "parse_cigar",
    "parse_sam_tags",
    "parse_tag_fields",
    "result_lines",
    "split_tab",
]


def result_lines(
    ctx: ParseContext,
    stream: Iterable[str] | None,
) -> Iterator[str]:
    """Yield tool output lines from *stream* when streaming, else result files."""
    if stream is not None:
        yield from stream
        return
    for path in ctx.plan.result_files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            yield from handle


def finite_float(value: str) -> float:
    """Parse a float and reject non-finite values (``inf``/``nan``).

    Raising ``ValueError`` keeps the callers' existing
    ``except (TypeError, ValueError)`` mapping to a contextful
    :class:`~gseek.errors.ParseError` instead of an unlocalized model error.
    """
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite value: {value!r}")
    return parsed


def float_or_none(value: str | None) -> float | None:
    """Parse an optional tool-provided float; absent/unparseable becomes None.

    Non-finite values (``nan``/``inf``) also become ``None``: the normalized
    record model rejects them, and a pathological tool field must not crash an
    otherwise complete parse.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None
