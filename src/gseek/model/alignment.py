"""Alignment aggregates: stats, summaries, and the result model."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gseek.errors import InputError
from gseek.types import ALIGNMENT_SCHEMA, JsonValue, union_length

from .alignment_record import AlignmentRecord
from .common import InputRef, Issue

if TYPE_CHECKING:
    from .run import IndexRef, RunManifest


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
