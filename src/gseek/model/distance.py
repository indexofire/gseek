"""Distance results for MinHash/sketch backends (mash, sourmash)."""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from gseek.errors import InputError, ParseError
from gseek.types import DISTANCE_SCHEMA, JsonValue

from .common import InputRef, Issue
from .run import IndexRef, RunManifest


@dataclass(frozen=True, slots=True)
class DistanceRecord:
    """One normalized distance/containment observation.

    MinHash distances are not alignments: there are no coordinates, and the
    meaning of ``distance`` depends on the backend. ``tags`` must record the
    metric and the sketch parameters (``k``, ``sketch_size``/``scaled``,
    ``seed``) so a consumer can interpret it without guessing.
    """

    query_id: str
    reference_id: str
    distance: float | None = None
    pvalue: float | None = None
    shared_hashes: int | None = None
    total_hashes: int | None = None
    containment: float | None = None
    jaccard: float | None = None
    ani: float | None = None
    aligned_fraction: float | None = None
    # See AlignmentRecord.tags: provenance participates in equality, not hash.
    tags: Mapping[str, str] = field(default_factory=dict, compare=True, hash=False)
    # Cached canonical JSON payload (see AlignmentRecord.canonical_payload).
    # init=False keeps dataclasses.replace() from carrying a stale payload.
    _payload_cache: str = field(default="", compare=False, repr=False, init=False)

    def __post_init__(self) -> None:
        if not self.query_id or not self.reference_id:
            raise InputError("distance record IDs must not be empty")
        for name, value in (
            ("distance", self.distance),
            ("pvalue", self.pvalue),
            ("containment", self.containment),
            ("jaccard", self.jaccard),
            ("aligned_fraction", self.aligned_fraction),
        ):
            if value is not None and not math.isfinite(value):
                raise InputError(f"{name} must be finite, got {value}")
            if value is not None and value < 0:
                raise InputError(f"{name} must be non-negative, got {value}")
        if self.ani is not None and not math.isfinite(self.ani):
            raise InputError(f"ani must be finite, got {self.ani}")
        if self.ani is not None and not 0.0 <= self.ani <= 100.0:
            raise InputError(f"ani must be in [0, 100], got {self.ani}")
        if self.shared_hashes is not None and self.shared_hashes < 0:
            raise InputError("shared_hashes must be non-negative")
        if self.total_hashes is not None and self.total_hashes < 0:
            raise InputError("total_hashes must be non-negative")
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))

    @property
    def metric(self) -> str | None:
        return self.tags.get("metric")

    @property
    def sort_key(self) -> tuple[str, str, float]:
        return (
            self.query_id,
            self.reference_id,
            self.distance if self.distance is not None else float("inf"),
        )

    def canonical_payload(self) -> str:
        """Canonical JSON of this record (cached)."""
        payload = self._payload_cache
        if not payload:
            payload = json.dumps(
                self.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            object.__setattr__(self, "_payload_cache", payload)
        return payload

    def stable_key(self) -> tuple[str, str, float, str]:
        """Total ordering key with a canonical JSON tie-breaker."""
        return (*self.sort_key, self.canonical_payload())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "query_id": self.query_id,
            "reference_id": self.reference_id,
            "distance": self.distance,
            "pvalue": self.pvalue,
            "shared_hashes": self.shared_hashes,
            "total_hashes": self.total_hashes,
            "containment": self.containment,
            "jaccard": self.jaccard,
            "ani": self.ani,
            "aligned_fraction": self.aligned_fraction,
            "tags": dict(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DistanceRecord:
        return cls(
            query_id=str(data["query_id"]),
            reference_id=str(data["reference_id"]),
            distance=float(data["distance"]) if data.get("distance") is not None else None,
            pvalue=float(data["pvalue"]) if data.get("pvalue") is not None else None,
            shared_hashes=int(data["shared_hashes"])
            if data.get("shared_hashes") is not None
            else None,
            total_hashes=int(data["total_hashes"])
            if data.get("total_hashes") is not None
            else None,
            containment=float(data["containment"]) if data.get("containment") is not None else None,
            jaccard=float(data["jaccard"]) if data.get("jaccard") is not None else None,
            ani=float(data["ani"]) if data.get("ani") is not None else None,
            aligned_fraction=float(data["aligned_fraction"])
            if data.get("aligned_fraction") is not None
            else None,
            tags={str(key): str(value) for key, value in dict(data.get("tags", {})).items()},
        )


@dataclass(frozen=True, slots=True)
class DistanceStats:
    record_count: int
    query_count: int
    reference_count: int
    runtime_s: float

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "record_count": self.record_count,
            "query_count": self.query_count,
            "reference_count": self.reference_count,
            "runtime_s": self.runtime_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DistanceStats:
        try:
            return cls(
                record_count=int(data["record_count"]),
                query_count=int(data["query_count"]),
                reference_count=int(data["reference_count"]),
                runtime_s=float(data["runtime_s"]),
            )
        except KeyError as exc:
            raise ParseError(
                f"distance stats missing the {exc.args[0]!r} field",
                context={"field": str(exc.args[0])},
            ) from exc


def _canonical_records(
    records: tuple[DistanceRecord, ...],
) -> tuple[DistanceRecord, ...]:
    """Distance analogue of ``AlignmentResult``'s lazy canonical ordering."""
    if len(records) < 2:
        return records
    keys = [record.sort_key for record in records]
    ordered = list(records)
    if any(left > right for left, right in itertools.pairwise(keys)):
        order = sorted(range(len(records)), key=keys.__getitem__)
        ordered = [records[index] for index in order]
        keys = [keys[index] for index in order]
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
class DistanceResult:
    backend: str
    records: tuple[DistanceRecord, ...]
    inputs: tuple[InputRef, ...]
    references: tuple[InputRef, ...]
    params: dict[str, JsonValue]
    stats: DistanceStats
    index: IndexRef | None = None
    manifest: RunManifest | None = None
    warnings: tuple[Issue, ...] = ()
    errors: tuple[Issue, ...] = ()
    schema: str = DISTANCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DISTANCE_SCHEMA:
            raise InputError(f"unsupported distance schema: {self.schema!r}")
        object.__setattr__(self, "records", _canonical_records(tuple(self.records)))
        object.__setattr__(self, "params", dict(self.params))

    def __iter__(self) -> Iterator[DistanceRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def best_per_query(self) -> tuple[DistanceRecord, ...]:
        """Best (lowest distance) record per query, first-seen by sorted order."""
        best: dict[str, DistanceRecord] = {}
        for record in self.records:
            existing = best.get(record.query_id)
            if existing is None:
                best[record.query_id] = record
                continue
            current_key = (
                record.distance if record.distance is not None else float("inf"),
                -(record.shared_hashes or 0),
            )
            existing_key = (
                existing.distance if existing.distance is not None else float("inf"),
                -(existing.shared_hashes or 0),
            )
            if current_key < existing_key:
                best[record.query_id] = record
        return tuple(best.values())

    def to_dict(self, *, include_manifest: bool = False) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "schema": self.schema,
            "backend": self.backend,
            "params": dict(self.params),
            "stats": self.stats.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
            "references": [item.to_dict() for item in self.references],
            "index": self.index.to_dict() if self.index is not None else None,
            "records": [record.to_dict() for record in self.records],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "errors": [issue.to_dict() for issue in self.errors],
        }
        if include_manifest and self.manifest is not None:
            data["manifest"] = self.manifest.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DistanceResult:
        """Rebuild a result from its ``gseek.distance/1`` document."""
        schema = data.get("schema")
        if schema != DISTANCE_SCHEMA:
            raise ParseError(
                f"unsupported distance schema: {schema!r}; expected {DISTANCE_SCHEMA!r}",
                context={"schema": str(schema)},
            )
        try:
            return cls(
                backend=str(data["backend"]),
                records=tuple(DistanceRecord.from_dict(item) for item in data.get("records", ())),
                inputs=tuple(InputRef.from_dict(item) for item in data.get("inputs", ())),
                references=tuple(InputRef.from_dict(item) for item in data.get("references", ())),
                params=dict(data.get("params", {})),
                stats=DistanceStats.from_dict(data["stats"]),
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
                f"distance result missing the {exc.args[0]!r} field",
                context={"field": str(exc.args[0])},
            ) from exc


__all__ = ["DistanceRecord", "DistanceResult", "DistanceStats"]
