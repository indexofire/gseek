"""Alignment aggregates: stats, summaries, and the result model."""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from gseek.errors import InputError, ParseError
from gseek.types import ALIGNMENT_SCHEMA, JsonValue, union_length

from .alignment_record import AlignmentRecord
from .common import InputRef, Issue
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlignmentStats:
        try:
            return cls(
                record_count=int(data["record_count"]),
                query_count=int(data["query_count"]),
                target_count=int(data["target_count"]),
                queries_with_hits=int(data["queries_with_hits"]),
                targets_with_hits=int(data["targets_with_hits"]),
                aligned_bases=int(data["aligned_bases"]),
                runtime_s=float(data["runtime_s"]),
                truncated=bool(data.get("truncated", False)),
            )
        except KeyError as exc:
            raise ParseError(
                f"alignment stats missing the {exc.args[0]!r} field",
                context={"field": str(exc.args[0])},
            ) from exc


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


def _canonical_records(
    records: tuple[AlignmentRecord, ...],
) -> tuple[AlignmentRecord, ...]:
    """Order records by the full stable key, serializing only tied rows.

    ``stable_key()`` appends a canonical JSON payload so equal cheap sort keys
    still have a deterministic order. Building that payload for every row is
    expensive and unnecessary: equal cheap keys are rare in real output. Sort
    by ``sort_key()`` first, then serialize only within equal-key runs.
    """
    if len(records) < 2:
        return records
    keys = [record.sort_key() for record in records]
    ordered = list(records)
    if any(left > right for left, right in itertools.pairwise(keys)):
        order = sorted(range(len(records)), key=keys.__getitem__)
        ordered = [records[index] for index in order]
        keys = [keys[index] for index in order]
    # Stable sort by payload within each equal-cheap-key run. This is exactly
    # the lexicographic order of (sort_key, canonical_payload).
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and keys[end] == keys[start]:
            end += 1
        if end - start > 1:
            ordered[start:end] = sorted(
                ordered[start:end], key=lambda record: record.canonical_payload()
            )
        start = end
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """A normalized alignment result plus provenance and aggregate views.

    ``sort_records=True`` (the default) applies gseek's canonical ordering.
    Consumers that immediately re-sort or aggregate rows — such as gapit's
    batch path — may pass ``sort_records=False`` and keep parser order.
    """

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
    sort_records: bool = field(default=True, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.schema != ALIGNMENT_SCHEMA:
            raise InputError(f"unsupported alignment schema: {self.schema!r}")
        records = (
            _canonical_records(tuple(self.records)) if self.sort_records else tuple(self.records)
        )
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "params", dict(self.params))

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
        # Cache the incumbent's key: recomputing it per challenger was 2k-1
        # key builds for a group of size k.
        best: dict[tuple[str, str], tuple[AlignmentRecord, tuple[float, int, float, float]]] = {}
        for record in self.records:
            key = (record.query_id, record.target_id)
            existing = best.get(key)
            challenger_key = record.quality_key()
            if existing is None or challenger_key > existing[1]:
                best[key] = (record, challenger_key)
        return tuple(record for record, _ in best.values())

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
            # Conflicting lengths for one ID must not silently depend on which
            # record happens to sort first: the largest observed wins.
            lengths = [row.target_length for row in rows if row.target_length is not None]
            target_length = max(lengths) if lengths else None
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
            # See summarize_by_target: largest observed length, not first-sorted.
            lengths = [row.query_length for row in rows if row.query_length is not None]
            query_length = max(lengths) if lengths else None
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlignmentResult:
        """Rebuild a result from its ``gseek.alignment/1`` document.

        Complements :meth:`to_dict` so consumers can read back what the CLI
        and ``to_jsonl`` write, with full record-level validation.
        """
        schema = data.get("schema")
        if schema != ALIGNMENT_SCHEMA:
            raise ParseError(
                f"unsupported alignment schema: {schema!r}; expected {ALIGNMENT_SCHEMA!r}",
                context={"schema": str(schema)},
            )
        try:
            return cls(
                backend=str(data["backend"]),
                records=tuple(AlignmentRecord.from_dict(item) for item in data.get("records", ())),
                inputs=tuple(InputRef.from_dict(item) for item in data.get("inputs", ())),
                targets=tuple(InputRef.from_dict(item) for item in data.get("targets", ())),
                params=dict(data.get("params", {})),
                stats=AlignmentStats.from_dict(data["stats"]),
                index=IndexRef.from_dict(data["index"]) if data.get("index") is not None else None,
                manifest=(
                    RunManifest.from_dict(data["manifest"])
                    if data.get("manifest") is not None
                    else None
                ),
                warnings=tuple(Issue.from_dict(item) for item in data.get("warnings", ())),
                errors=tuple(Issue.from_dict(item) for item in data.get("errors", ())),
            )
        except KeyError as exc:
            raise ParseError(
                f"alignment result missing the {exc.args[0]!r} field",
                context={"field": str(exc.args[0])},
            ) from exc


__all__ = [
    "AlignmentRecord",
    "AlignmentResult",
    "AlignmentStats",
    "QuerySummary",
    "TargetSummary",
    "build_alignment_stats",
]
