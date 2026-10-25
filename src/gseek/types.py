"""Shared enums, JSON aliases, intervals, and schema identifiers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gseek.errors import InputError

# ---------------------------------------------------------------------------
# Schema identifiers (independent from the package version)
# ---------------------------------------------------------------------------

ALIGNMENT_SCHEMA = "gseek.alignment/1"
DISTANCE_SCHEMA = "gseek.distance/1"
RUN_SCHEMA = "gseek.run/1"
INDEX_SCHEMA = "gseek.index/1"
INDEX_KEY_SCHEMA = "gseek.index-key/1"
RECIPE_SCHEMA = "gseek.recipe/1"

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Strand(StrEnum):
    """Orientation of the query relative to the target."""

    PLUS = "+"
    MINUS = "-"

    @property
    def reverse(self) -> bool:
        return self is Strand.MINUS


class InputKind(StrEnum):
    """Input format understood by the core."""

    FASTA = "fasta"
    FASTQ = "fastq"
    #: Pre-built distance artifact (mash .msh, sourmash .sig/.zip) or a
    #: sourmash rocksdb directory used as an external reference; the adapter
    #: consumes it instead of sketching FASTA.
    SKETCH = "sketch"


class OutputKind(StrEnum):
    """Kind of result produced by a backend."""

    ALIGNMENT = "alignment"
    DISTANCE = "distance"


class IndexScope(StrEnum):
    """Which side of a comparison a backend can pre-index."""

    NONE = "none"
    TARGET = "target"
    QUERY = "query"
    BOTH = "both"


class Molecule(StrEnum):
    """Molecule type supported by a backend."""

    DNA = "dna"
    RNA = "rna"
    PROTEIN = "protein"


class RunStatus(StrEnum):
    """Outcome of a run manifest."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# JSON aliases
# ---------------------------------------------------------------------------

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """A 0-based half-open interval ``[start, end)`` on a forward sequence.

    ``start <= end`` is required. A zero-length interval is allowed and is
    useful for tools that report point features; coverage calculations simply
    treat it as empty.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise InputError(f"interval start must be >= 0, got {self.start}")
        if self.end < self.start:
            raise InputError(f"interval end ({self.end}) precedes start ({self.start})")

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, position: int) -> bool:
        return self.start <= position < self.end

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: Interval) -> Interval | None:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return Interval(start, end) if start < end else None

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Interval:
        return cls(start=int(data["start"]), end=int(data["end"]))


def union_length(intervals: Iterable[Interval | tuple[int, int]]) -> int:
    """Return the total length of the union of half-open intervals.

    Identical arithmetic to counting positions whose per-base depth is > 0,
    without materializing a per-base array. Accepts :class:`Interval` objects
    or ``(start, end)`` tuples.
    """
    normalized: list[tuple[int, int]] = []
    for item in intervals:
        if isinstance(item, Interval):
            normalized.append((item.start, item.end))
        else:
            start, end = item
            start, end = int(start), int(end)
            if start < 0:
                raise InputError(f"interval start must be >= 0, got {start}")
            if end < start:
                raise InputError(f"interval end ({end}) precedes start ({start})")
            normalized.append((start, end))
    covered = 0
    reach = -1
    for start, end in sorted(normalized):
        if end <= reach:
            continue
        covered += end - max(start, reach)
        reach = end
    return covered


def merge_intervals(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Merge overlapping or adjacent intervals into a sorted tuple."""
    ordered = sorted(intervals)
    if not ordered:
        return ()
    merged: list[Interval] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = Interval(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return tuple(merged)


def covered_fraction(intervals: Iterable[Interval], length: int | None) -> float | None:
    """Return the union-covered fraction of *length*, or ``None`` if unknown."""
    if length is None or length <= 0:
        return None
    return union_length(intervals) / length


__all__ = [
    "ALIGNMENT_SCHEMA",
    "DISTANCE_SCHEMA",
    "INDEX_KEY_SCHEMA",
    "INDEX_SCHEMA",
    "RECIPE_SCHEMA",
    "RUN_SCHEMA",
    "IndexScope",
    "InputKind",
    "Interval",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "Molecule",
    "OutputKind",
    "RunStatus",
    "Strand",
    "covered_fraction",
    "merge_intervals",
    "union_length",
]
