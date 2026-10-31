"""The normalized, lossless alignment record."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from gseek.errors import InputError
from gseek.types import (
    Interval,
    JsonValue,
    Strand,
)

if TYPE_CHECKING:
    pass


def _validate_optional_non_negative(name: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise InputError(f"{name} must be non-negative, got {value}")


def _validate_optional_finite(name: str, value: float | None) -> None:
    if value is not None and not math.isfinite(value):
        raise InputError(f"{name} must be finite, got {value}")


def _validate_optional_coverage(name: str, value: float | None) -> None:
    _validate_optional_finite(name, value)
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
    # tags carry identity provenance (NM, identity_method) and therefore
    # participate in equality; they stay out of __hash__ (mappings are
    # unhashable), which keeps the eq/hash contract intact.
    tags: Mapping[str, str] = field(default_factory=dict, compare=True, hash=False)
    # Cached canonical JSON payload (lazily built): stable_key ordering and
    # result hashing serialize each record exactly once. Never compared, and
    # ``init=False`` so dataclasses.replace() cannot copy a payload that still
    # describes the pre-replacement fields (align_many rewrites query ids).
    _payload_cache: str = field(default="", compare=False, repr=False, init=False)

    def __post_init__(self) -> None:
        if not self.query_id:
            raise InputError("query_id must not be empty")
        if not self.target_id:
            raise InputError("target_id must not be empty")
        _validate_optional_finite("identity", self.identity)
        if self.identity is not None and not 0.0 <= self.identity <= 100.0:
            raise InputError(f"identity must be in [0, 100], got {self.identity}")
        _validate_optional_non_negative("query_length", self.query_length)
        _validate_optional_non_negative("target_length", self.target_length)
        _validate_optional_non_negative("aligned_length", self.aligned_length)
        _validate_optional_non_negative("matches", self.matches)
        _validate_optional_non_negative("mismatches", self.mismatches)
        _validate_optional_non_negative("gap_count", self.gap_count)
        _validate_optional_non_negative("gap_openings", self.gap_openings)
        _validate_optional_finite("evalue", self.evalue)
        _validate_optional_finite("bit_score", self.bit_score)
        _validate_optional_finite("depth", self.depth)
        _validate_optional_finite("score", self.score)
        _validate_optional_coverage("query_coverage", self.query_coverage)
        _validate_optional_coverage("target_coverage", self.target_coverage)

        for start_name, end_name, length_name, start, end, length in (
            (
                "query_start",
                "query_end",
                "query_length",
                self.query_start,
                self.query_end,
                self.query_length,
            ),
            (
                "target_start",
                "target_end",
                "target_length",
                self.target_start,
                self.target_end,
                self.target_length,
            ),
        ):
            if (start is None) != (end is None):
                raise InputError(f"{start_name} and {end_name} must be provided together")
            if start is not None and end is not None:
                if start < 0:
                    raise InputError(f"{start_name} must be >= 0, got {start}")
                if end < start:
                    raise InputError(f"{end_name} ({end}) precedes {start_name} ({start})")
                # Out-of-bounds intervals would silently derive coverage > 1.
                if length is not None and end > length:
                    raise InputError(
                        f"{end_name} ({end}) exceeds {length_name} ({length})",
                        context={"query_id": self.query_id, "target_id": self.target_id},
                    )

        # Direct interval arithmetic: (end - start) is exactly
        # Interval(start, end).length — the coordinates were validated above,
        # so constructing Interval objects here would be redundant work.
        if self.query_coverage is None:
            query_start = self.query_start
            if query_start is not None and self.query_end is not None and self.query_length:
                object.__setattr__(
                    self,
                    "query_coverage",
                    (self.query_end - query_start) / self.query_length,
                )
        if self.target_coverage is None:
            target_start = self.target_start
            if target_start is not None and self.target_end is not None and self.target_length:
                object.__setattr__(
                    self,
                    "target_coverage",
                    (self.target_end - target_start) / self.target_length,
                )

        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))

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

    def canonical_payload(self) -> str:
        """Canonical JSON of this record (cached).

        Uses the exact parameters of :func:`gseek.provenance.canonical_json`
        so a streaming hash over payloads is byte-identical to hashing the
        serialized record list in one shot.
        """
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

    def stable_key(self) -> tuple[str, str, int, int, str, int, str]:
        """Total ordering key, including a canonical JSON tie-breaker.

        ``sort_key`` alone can tie for records whose coordinates and lengths
        match but whose identity or scores differ. Tool output order can vary
        with thread count, so the final JSON component makes the order (and
        therefore ``result_hash``) independent of parser order.
        """
        return (*self.sort_key(), self.canonical_payload())

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


__all__ = ["AlignmentRecord"]
