"""Explicit reductions and serializers over the lossless record model."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from gseek.errors import UsageError
from gseek.model import AlignmentRecord, AlignmentResult, DistanceResult
from gseek.types import JsonValue

DEFAULT_TSV_COLUMNS = (
    "query_id",
    "target_id",
    "identity",
    "aligned_length",
    "query_coverage",
    "target_coverage",
    "strand",
    "query_start",
    "query_end",
    "target_start",
    "target_end",
    "evalue",
    "bit_score",
    "mapq",
    "depth",
)


def to_jsonl(result: AlignmentResult | DistanceResult) -> str:
    """Serialize a result as a header line, record lines, and a footer line."""
    header = {
        "schema": result.schema,
        "type": "header",
        "backend": result.backend,
        "params": result.params,
        "stats": result.stats.to_dict(),
    }
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    for record in result.records:
        payload = {"type": "record", **record.to_dict()}
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    status = "error" if result.errors else "warning" if result.warnings else "ok"
    footer = {
        "type": "footer",
        "record_count": len(result.records),
        "status": status,
    }
    lines.append(json.dumps(footer, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


def to_tsv(
    result: AlignmentResult,
    *,
    columns: Sequence[str] = DEFAULT_TSV_COLUMNS,
) -> str:
    """Serialize alignment records as header + rows with selected columns."""
    unknown = [column for column in columns if column not in DEFAULT_TSV_COLUMNS]
    if unknown:
        raise UsageError(
            f"unknown TSV columns: {unknown}; expected only {list(DEFAULT_TSV_COLUMNS)}"
        )
    lines = ["\t".join(columns)]
    for record in result.records:
        payload = record.to_dict()
        lines.append("\t".join(_format_cell(payload.get(column)) for column in columns))
    return "\n".join(lines) + "\n"


def _format_cell(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def deduplicate_intervals(records: Iterable[AlignmentRecord]) -> tuple[AlignmentRecord, ...]:
    """Keep the first record per ``(query_id, query_start, query_end)``.

    The key deliberately ignores strand and target, matching gapit's
    abricate-compatible dedup rule. The key is claimed before any downstream
    coverage filter, so callers should deduplicate first and filter second.

    Records without coordinates (for example KMA template hits) are kept
    as-is: ``(query_id, None, None)`` would otherwise collapse every hit for a
    query into a single record.
    """
    seen: set[tuple[str, int | None, int | None]] = set()
    kept: list[AlignmentRecord] = []
    for record in records:
        if record.query_start is None or record.query_end is None:
            kept.append(record)
            continue
        key = (record.query_id, record.query_start, record.query_end)
        if key in seen:
            continue
        seen.add(key)
        kept.append(record)
    return tuple(kept)


def count_distinct_occurrences(
    records: Iterable[AlignmentRecord],
    *,
    query_id: str,
    target_id: str,
) -> int:
    """Count distinct genomic placements of one target hit.

    Placements are keyed by ``(query_id, query_start, query_end)`` so multiple
    HSPs on the same contig interval count once, while the same allele on two
    contigs counts twice. This mirrors gmlst's ``count_same_copy`` semantics
    after the query/target direction flip.
    """
    placements = {
        (record.query_id, record.query_start, record.query_end)
        for record in records
        if record.query_id == query_id and record.target_id == target_id
    }
    return len(placements)


__all__ = [
    "DEFAULT_TSV_COLUMNS",
    "count_distinct_occurrences",
    "deduplicate_intervals",
    "to_jsonl",
    "to_tsv",
]
