"""Unified, lossless alignment records and aggregate result model."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gseek.errors import InputError
from gseek.types import (
    ALIGNMENT_SCHEMA,
    Interval,
    JsonValue,
    Strand,
    union_length,
)

from .common import InputRef, Issue

if TYPE_CHECKING:
    from .run import IndexRef, RunManifest


def _validate_optional_non_negative(name: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise InputError(f"{name} must be non-negative, got {value}")


def _validate_optional_coverage(name: str, value: float | None) -> None:
    if value is not None and value < 0:
        raise InputError(f"{name} must be non-negative, got {value}")


@dataclass(frozen=True, slots=True)
class AlignmentRecord:
    """One normalized alignment row.

    Coordinates are always 0-based half-open on the forward sequence; the
    orientation of the query relative to the target is kept in ``strand``.
    Adapters must fill ``query_coverage`` / ``target_coverage`` when the tool
    reports coverage without coordinates (for example KMA); otherwise they are
    derived from the intervals in :meth:`__post_init__`.
    """

    query_id: str
    target_id: str
    query_length: int | None = None
    target_length: int | None = None
    query_start: int | None = None
    query_end: int | None = None
    target_start: int | None = None
    target_end: int | None = None
    strand: Strand = Strand.PLUS
    identity: float | None = None
    aligned_length: int | None = None
    matches: int | None = None
    mismatches: int | None = None
    gap_count: int | None = None
    gap_openings: int | None = None
    evalue: float | None = None
    bit_score: float | None = None
    mapq: int | None = None
    depth: float | None = None
    score: float | None = None
    query_coverage: float | None = None
    target_coverage: float | None = None
    query_aligned: str | None = None
    target_aligned: str | None = None
    target_description: str | None = None
    is_primary: bool = True
    is_supplementary: bool = False
    tags: Mapping[str, str] = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self) -> None:
        if not self.query_id:
            raise InputError("query_id must not be empty")
        if not self.target_id:
            raise InputError("target_id must not be empty")
        if self.identity is not None and not 0.0 <= self.identity <= 100.0:
            raise InputError(f"identity must be in [0, 100], got {self.identity}")
        _validate_optional_non_negative("query_length", self.query_length)
        _validate_optional_non_negative("target_length", self.target_length)
        _validate_optional_non_negative("aligned_length", self.aligned_length)
        _validate_optional_non_negative("matches", self.matches)
        _validate_optional_non_negative("mismatches", self.mismatches)
        _validate_optional_non_negative("gap_count", self.gap_count)
        _validate_optional_non_negative("gap_openings", self.gap_openings)
        _validate_optional_coverage("query_coverage", self.query_coverage)
        _validate_optional_coverage("target_coverage", self.target_coverage)

        for start_name, end_name, start, end in (
            ("query_start", "query_end", self.query_start, self.query_end),
            ("target_start", "target_end", self.target_start, self.target_end),
        ):
            if (start is None) != (end is None):
                raise InputError(f"{start_name} and {end_name} must be provided together")
            if start is not None and end is not None:
                if start < 0:
                    raise InputError(f"{start_name} must be >= 0, got {start}")
                if end < start:
                    raise InputError(f"{end_name} ({end}) precedes {start_name} ({start})")

        if self.query_coverage is None and self.query_interval is not None and self.query_length:
            object.__setattr__(
                self,
                "query_coverage",
                self.query_interval.length / self.query_length,
            )
        if self.target_coverage is None and self.target_interval is not None and self.target_length:
            object.__setattr__(
                self,
                "target_coverage",
                self.target_interval.length / self.target_length,
            )

        object.__setattr__(self, "tags", dict(self.tags))

    # ------------------------------------------------------------------
    # Derived coordinates and metrics
    # ------------------------------------------------------------------

    @property
    def query_interval(self) -> Interval | None:
        if self.query_start is None or self.query_end is None:
            return None
        return Interval(self.query_start, self.query_end)

    @property
    def target_interval(self) -> Interval | None:
        if self.target_start is None or self.target_end is None:
            return None
        return Interval(self.target_start, self.target_end)

    @property
    def is_reverse(self) -> bool:
        return self.strand is Strand.MINUS

    @property
    def identity_fraction(self) -> float | None:
        return None if self.identity is None else self.identity / 100.0

    @property
    def identity_method(self) -> str | None:
        return self.tags.get("identity_method")

    @property
    def identity_from_nm(self) -> float | None:
        """Identity from ``(aligned_length - NM) / aligned_length``, if available."""
        nm = self.tags.get("NM")
        if nm is None or not self.aligned_length:
            return None
        try:
            edits = int(nm)
        except (TypeError, ValueError):
            return None
        if edits < 0:
            return None
        return max(0.0, min(100.0, 100.0 * (self.aligned_length - edits) / self.aligned_length))

    @property
    def identity_from_matches(self) -> float | None:
        """Identity from ``matches / aligned_length``, if available."""
        if self.matches is None or not self.aligned_length:
            return None
        return max(0.0, min(100.0, 100.0 * self.matches / self.aligned_length))

    @property
    def identity_nmatch_nm(self) -> float | None:
        """gmlst-compatible ``nmatch / (nmatch + NM)`` identity, if available."""
        nm = self.tags.get("NM")
        if self.matches is None or nm is None:
            return None
        try:
            edits = int(nm)
        except (TypeError, ValueError):
            return None
        denominator = self.matches + edits
        if edits < 0 or denominator <= 0:
            return None
        return 100.0 * self.matches / denominator

    @property
    def span_query_coverage(self) -> float | None:
        interval = self.query_interval
        if interval is None or not self.query_length:
            return None
        return interval.length / self.query_length

    @property
    def span_target_coverage(self) -> float | None:
        interval = self.target_interval
        if interval is None or not self.target_length:
            return None
        return interval.length / self.target_length

    @property
    def ungapped_query_coverage(self) -> float | None:
        if self.aligned_length is None or self.gap_count is None or not self.query_length:
            return None
        return max(0, self.aligned_length - self.gap_count) / self.query_length

    @property
    def ungapped_target_coverage(self) -> float | None:
        if self.aligned_length is None or self.gap_count is None or not self.target_length:
            return None
        return max(0, self.aligned_length - self.gap_count) / self.target_length

    @property
    def aligned_bases(self) -> int:
        if self.aligned_length is not None:
            return self.aligned_length
        interval = self.query_interval
        return interval.length if interval is not None else 0

    def sort_key(self) -> tuple[str, str, int, int, str, int]:
        """Stable canonical ordering key used by :class:`AlignmentResult`."""
        return (
            self.query_id,
            self.target_id,
            self.query_start if self.query_start is not None else -1,
            self.target_start if self.target_start is not None else -1,
            self.strand.value,
            -(self.aligned_length or 0),
        )

    def stable_key(self) -> tuple[str, str, int, int, str, int, str]:
        """Total ordering key, including a canonical JSON tie-breaker.

        ``sort_key`` alone can tie for records whose coordinates and lengths
        match but whose identity or scores differ. Tool output order can vary
        with thread count, so the final JSON component makes the order (and
        therefore ``result_hash``) independent of parser order.
        """
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return (*self.sort_key(), payload)

    def quality_key(self) -> tuple[float, int, float, float]:
        """Key used by best-hit views: identity, length, then coverage."""
        return (
            self.identity if self.identity is not None else -1.0,
            self.aligned_length if self.aligned_length is not None else 0,
            self.query_coverage if self.query_coverage is not None else 0.0,
            self.target_coverage if self.target_coverage is not None else 0.0,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "query_id": self.query_id,
            "target_id": self.target_id,
            "query_length": self.query_length,
            "target_length": self.target_length,
            "query_start": self.query_start,
            "query_end": self.query_end,
            "target_start": self.target_start,
            "target_end": self.target_end,
            "strand": self.strand.value,
            "identity": self.identity,
            "aligned_length": self.aligned_length,
            "matches": self.matches,
            "mismatches": self.mismatches,
            "gap_count": self.gap_count,
            "gap_openings": self.gap_openings,
            "evalue": self.evalue,
            "bit_score": self.bit_score,
            "mapq": self.mapq,
            "depth": self.depth,
            "score": self.score,
            "query_coverage": self.query_coverage,
            "target_coverage": self.target_coverage,
            "ungapped_query_coverage": self.ungapped_query_coverage,
            "ungapped_target_coverage": self.ungapped_target_coverage,
            "query_aligned": self.query_aligned,
            "target_aligned": self.target_aligned,
            "target_description": self.target_description,
            "is_primary": self.is_primary,
            "is_supplementary": self.is_supplementary,
            "tags": dict(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlignmentRecord:
        return cls(
            query_id=str(data["query_id"]),
            target_id=str(data["target_id"]),
            query_length=int(data["query_length"])
            if data.get("query_length") is not None
            else None,
            target_length=int(data["target_length"])
            if data.get("target_length") is not None
            else None,
            query_start=int(data["query_start"]) if data.get("query_start") is not None else None,
            query_end=int(data["query_end"]) if data.get("query_end") is not None else None,
            target_start=int(data["target_start"])
            if data.get("target_start") is not None
            else None,
            target_end=int(data["target_end"]) if data.get("target_end") is not None else None,
            strand=Strand(str(data.get("strand", "+"))),
            identity=float(data["identity"]) if data.get("identity") is not None else None,
            aligned_length=int(data["aligned_length"])
            if data.get("aligned_length") is not None
            else None,
            matches=int(data["matches"]) if data.get("matches") is not None else None,
            mismatches=int(data["mismatches"]) if data.get("mismatches") is not None else None,
            gap_count=int(data["gap_count"]) if data.get("gap_count") is not None else None,
            gap_openings=int(data["gap_openings"])
            if data.get("gap_openings") is not None
            else None,
            evalue=float(data["evalue"]) if data.get("evalue") is not None else None,
            bit_score=float(data["bit_score"]) if data.get("bit_score") is not None else None,
            mapq=int(data["mapq"]) if data.get("mapq") is not None else None,
            depth=float(data["depth"]) if data.get("depth") is not None else None,
            score=float(data["score"]) if data.get("score") is not None else None,
            query_coverage=float(data["query_coverage"])
            if data.get("query_coverage") is not None
            else None,
            target_coverage=float(data["target_coverage"])
            if data.get("target_coverage") is not None
            else None,
            query_aligned=str(data["query_aligned"])
            if data.get("query_aligned") is not None
            else None,
            target_aligned=str(data["target_aligned"])
            if data.get("target_aligned") is not None
            else None,
            target_description=str(data["target_description"])
            if data.get("target_description") is not None
            else None,
            is_primary=bool(data.get("is_primary", True)),
            is_supplementary=bool(data.get("is_supplementary", False)),
            tags={str(key): str(value) for key, value in dict(data.get("tags", {})).items()},
        )


@dataclass(frozen=True, slots=True)
class AlignmentStats:
    record_count: int
    query_count: int
    target_count: int
    queries_with_hits: int
    targets_with_hits: int
    aligned_bases: int
    runtime_s: float
    truncated: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "record_count": self.record_count,
            "query_count": self.query_count,
            "target_count": self.target_count,
            "queries_with_hits": self.queries_with_hits,
            "targets_with_hits": self.targets_with_hits,
            "aligned_bases": self.aligned_bases,
            "runtime_s": self.runtime_s,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class TargetSummary:
    target_id: str
    target_length: int | None
    record_count: int
    query_count: int
    covered_bases: int
    breadth: float | None
    mean_identity: float | None
    weighted_identity: float | None
    mean_depth: float | None
    best_record: AlignmentRecord | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "target_id": self.target_id,
            "target_length": self.target_length,
            "record_count": self.record_count,
            "query_count": self.query_count,
            "covered_bases": self.covered_bases,
            "breadth": self.breadth,
            "mean_identity": self.mean_identity,
            "weighted_identity": self.weighted_identity,
            "mean_depth": self.mean_depth,
            "best_record": self.best_record.to_dict() if self.best_record is not None else None,
        }


@dataclass(frozen=True, slots=True)
class QuerySummary:
    query_id: str
    query_length: int | None
    record_count: int
    target_count: int
    best_identity: float | None
    best_target_id: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "query_id": self.query_id,
            "query_length": self.query_length,
            "record_count": self.record_count,
            "target_count": self.target_count,
            "best_identity": self.best_identity,
            "best_target_id": self.best_target_id,
        }


def build_alignment_stats(
    records: tuple[AlignmentRecord, ...],
    *,
    query_count: int,
    target_count: int,
    runtime_s: float,
    truncated: bool = False,
) -> AlignmentStats:
    """Compute stats from already-normalized records."""
    return AlignmentStats(
        record_count=len(records),
        query_count=query_count,
        target_count=target_count,
        queries_with_hits=len({record.query_id for record in records}),
        targets_with_hits=len({record.target_id for record in records}),
        aligned_bases=sum(record.aligned_bases for record in records),
        runtime_s=runtime_s,
        truncated=truncated,
    )


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """A normalized alignment result plus provenance and aggregate views."""

    backend: str
    records: tuple[AlignmentRecord, ...]
    inputs: tuple[InputRef, ...]
    targets: tuple[InputRef, ...]
    params: dict[str, JsonValue]
    stats: AlignmentStats
    index: IndexRef | None = None
    manifest: RunManifest | None = None
    warnings: tuple[Issue, ...] = ()
    errors: tuple[Issue, ...] = ()
    schema: str = ALIGNMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ALIGNMENT_SCHEMA:
            raise InputError(f"unsupported alignment schema: {self.schema!r}")
        object.__setattr__(
            self, "records", tuple(sorted(self.records, key=AlignmentRecord.stable_key))
        )

    def __iter__(self) -> Iterator[AlignmentRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def primary_only(self) -> tuple[AlignmentRecord, ...]:
        """Return records that are primary and not supplementary."""
        return tuple(
            record for record in self.records if record.is_primary and not record.is_supplementary
        )

    def best_per_query_target(self) -> tuple[AlignmentRecord, ...]:
        """Best row per ``(query_id, target_id)``, first-seen order.

        The quality key is ``(identity, aligned_length, query_coverage,
        target_coverage)``. Full ties keep the first row, matching the
        historical gmlst ``select_best_matches`` behaviour.
        """
        best: dict[tuple[str, str], AlignmentRecord] = {}
        for record in self.records:
            key = (record.query_id, record.target_id)
            existing = best.get(key)
            if existing is None or record.quality_key() > existing.quality_key():
                best[key] = record
        return tuple(best.values())

    def summarize_by_target(self) -> tuple[TargetSummary, ...]:
        """Aggregate per-target breadth, weighted identity, and depth.

        Breadth is the union of target intervals divided by the target length,
        using 0-based half-open intervals. Identity and depth are weighted by
        aligned length (or 1 when the length is unknown).
        """
        grouped: dict[str, list[AlignmentRecord]] = defaultdict(list)
        for record in self.records:
            grouped[record.target_id].append(record)

        summaries: list[TargetSummary] = []
        for target_id, rows in grouped.items():
            target_length = next(
                (row.target_length for row in rows if row.target_length is not None),
                None,
            )
            intervals = [row.target_interval for row in rows if row.target_interval is not None]
            covered = union_length(intervals)
            breadth = covered / target_length if target_length else None

            identity_pairs: list[tuple[float, int]] = []
            depth_pairs: list[tuple[float, int]] = []
            for row in rows:
                weight = max(1, row.aligned_length or 0)
                if row.identity is not None:
                    identity_pairs.append((row.identity, weight))
                if row.depth is not None:
                    depth_pairs.append((row.depth, weight))

            identity_weight = sum(weight for _, weight in identity_pairs)
            weighted_identity = (
                sum(identity * weight for identity, weight in identity_pairs) / identity_weight
                if identity_weight
                else None
            )
            mean_identity = (
                sum(identity for identity, _ in identity_pairs) / len(identity_pairs)
                if identity_pairs
                else None
            )
            depth_weight = sum(weight for _, weight in depth_pairs)
            mean_depth = (
                sum(depth * weight for depth, weight in depth_pairs) / depth_weight
                if depth_weight
                else None
            )
            best_record = max(rows, key=AlignmentRecord.quality_key) if rows else None
            summaries.append(
                TargetSummary(
                    target_id=target_id,
                    target_length=target_length,
                    record_count=len(rows),
                    query_count=len({row.query_id for row in rows}),
                    covered_bases=covered,
                    breadth=breadth,
                    mean_identity=mean_identity,
                    weighted_identity=weighted_identity,
                    mean_depth=mean_depth,
                    best_record=best_record,
                )
            )
        summaries.sort(key=lambda item: item.target_id)
        return tuple(summaries)

    def summarize_by_query(self) -> tuple[QuerySummary, ...]:
        """Aggregate per-query hit counts and best identity."""
        grouped: dict[str, list[AlignmentRecord]] = defaultdict(list)
        for record in self.records:
            grouped[record.query_id].append(record)

        summaries: list[QuerySummary] = []
        for query_id, rows in grouped.items():
            query_length = next(
                (row.query_length for row in rows if row.query_length is not None),
                None,
            )
            best = max(rows, key=AlignmentRecord.quality_key) if rows else None
            summaries.append(
                QuerySummary(
                    query_id=query_id,
                    query_length=query_length,
                    record_count=len(rows),
                    target_count=len({row.target_id for row in rows}),
                    best_identity=best.identity if best is not None else None,
                    best_target_id=best.target_id if best is not None else None,
                )
            )
        summaries.sort(key=lambda item: item.query_id)
        return tuple(summaries)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self, *, include_manifest: bool = False) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "schema": self.schema,
            "backend": self.backend,
            "params": dict(self.params),
            "stats": self.stats.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
            "targets": [item.to_dict() for item in self.targets],
            "index": self.index.to_dict() if self.index is not None else None,
            "records": [record.to_dict() for record in self.records],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "errors": [issue.to_dict() for issue in self.errors],
        }
        if include_manifest and self.manifest is not None:
            data["manifest"] = self.manifest.to_dict()
        return data


__all__ = [
    "AlignmentRecord",
    "AlignmentResult",
    "AlignmentStats",
    "QuerySummary",
    "TargetSummary",
    "build_alignment_stats",
]
