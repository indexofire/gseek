"""Distance results for MinHash/sketch backends (mash, sourmash)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gseek.errors import InputError
from gseek.types import DISTANCE_SCHEMA, JsonValue

from .common import InputRef, Issue

if TYPE_CHECKING:
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
        object.__setattr__(self, "tags", dict(self.tags))

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

    def stable_key(self) -> tuple[str, str, float, str]:
        """Total ordering key with a canonical JSON tie-breaker."""
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return (*self.sort_key, payload)

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
        object.__setattr__(
            self, "records", tuple(sorted(self.records, key=DistanceRecord.stable_key))
        )

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


__all__ = ["DistanceRecord", "DistanceResult", "DistanceStats"]
