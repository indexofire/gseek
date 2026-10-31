"""Multi-query alignment batching.

``align_many`` merges FASTA query files into one internal query, runs the
selected backend once, and restores the original query ids. It amortizes
target hashing, index loading, workspace setup, and aligner process startup
across many small queries. It intentionally does not build a manifest: the
internal merged query file is temporary and cannot be replayed.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from gseek.adapter import AlignmentAdapter
from gseek.api import _align_request, _precomputed_refs
from gseek.errors import ConfigError, ParseError
from gseek.execution import Executor, ToolResolver
from gseek.index import IndexPolicy, IndexStore
from gseek.io import detect_input_kind, iter_fasta
from gseek.model import AlignmentRecord, AlignmentResult, InputRef
from gseek.provenance import input_refs
from gseek.runner import run_alignment
from gseek.types import InputKind, JsonValue


def _paths(queries: Sequence[Path | str]) -> tuple[Path, ...]:
    paths = tuple(Path(query) for query in queries)
    if not paths:
        raise ConfigError("align_many requires at least one query")
    for path in paths:
        if detect_input_kind(path) is not InputKind.FASTA:
            raise ConfigError(
                "align_many supports FASTA query files only"
                " (paired/FASTQ batching needs lane-aware aggregation)",
                context={"query": str(path)},
            )
    return paths


def _rewrite_query_ids(
    records: Sequence[AlignmentRecord],
    nonce: str,
    *,
    query_count: int,
    annotate_batch: bool,
) -> tuple[AlignmentRecord, ...]:
    """Strip the internal ``<nonce><index>:`` prefix from every record."""
    restored: list[AlignmentRecord] = []
    for record in records:
        if not record.query_id.startswith(nonce):
            raise ParseError(
                "batched alignment record lost its internal query prefix",
                context={"query_id": record.query_id},
            )
        body = record.query_id[len(nonce) :]
        index_text, separator, original = body.partition(":")
        if not separator or not original or not index_text.isdigit():
            raise ParseError("malformed batched query id", context={"query_id": record.query_id})
        if int(index_text) >= query_count:
            raise ParseError(
                "batched query id refers to an unknown input",
                context={"query_id": record.query_id},
            )
        if annotate_batch:
            tags = {**record.tags, "batch_index": index_text}
            restored.append(replace(record, query_id=original, tags=tags))
        else:
            restored.append(replace(record, query_id=original))
    return tuple(restored)


def align_many(
    *,
    queries: Sequence[Path | str],
    target: Path | str | Sequence[Path | str],
    backend: str | None = None,
    adapter: AlignmentAdapter | None = None,
    params: Mapping[str, JsonValue] | None = None,
    preset: str | None = None,
    threads: int = 1,
    timeout_s: float | None = None,
    index: IndexPolicy | str = IndexPolicy.AUTO,
    target_filter: Sequence[str] | None = None,
    work_dir: Path | str | None = None,
    keep_work: bool = False,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
    target_refs: Sequence[InputRef] | None = None,
    query_refs: Sequence[InputRef] | None = None,
    compute_inputs: bool = True,
    sort_records: bool = True,
    annotate_batch: bool = False,
) -> AlignmentResult:
    """Align several FASTA query files in one backend invocation.

    Query files are merged into a temporary FASTA with opaque internal ids,
    and the returned records are rewritten back to the original ids. All
    queries must be single-end FASTA; FASTQ/paired batching is deliberately
    not covered by this API. The result has no manifest because the merged
    query file is temporary.

    Args mirror :func:`gseek.align` where applicable. ``target_refs`` may be
    supplied to reuse a precomputed target digest across batches;
    ``query_refs`` does the same for the original query files. Set
    ``compute_inputs=False`` to skip hashing query files entirely (the result
    then has empty ``inputs``). ``sort_records=False`` keeps parser order for
    consumers that re-sort or aggregate rows immediately. ``annotate_batch=True``
    adds a ``batch_index`` tag to every record so a consumer (gapit) can split
    the merged result back into its input files.
    """
    paths = _paths(queries)
    prepared_query_refs = _precomputed_refs(query_refs, paths, role="query")
    if prepared_query_refs is None:
        prepared_query_refs = (
            input_refs(paths, kind=InputKind.FASTA, compute_records=False) if compute_inputs else ()
        )
    nonce = f"gseekb{uuid.uuid4().hex}:"
    with tempfile.TemporaryDirectory(prefix="gseek-batch-") as tmp:
        merged = Path(tmp) / "queries.fa"
        with merged.open("w", encoding="utf-8", newline="\n") as handle:
            for query_index, path in enumerate(paths):
                prefix = f"{nonce}{query_index}:"
                for record in iter_fasta(path):
                    handle.write(f">{prefix}{record.sequence_id}\n{record.sequence}\n")
        request = _align_request(
            query=merged,
            target=target,
            query_kind=InputKind.FASTA,
            paired=False,
            params=params,
            preset=preset,
            threads=threads,
            timeout_s=timeout_s,
            index=index,
            target_filter=target_filter,
            work_dir=work_dir,
            keep_work=keep_work,
        )
        result = run_alignment(
            request,
            backend=backend,
            adapter=adapter,
            resolver=resolver,
            executor=executor,
            store=store,
            record_manifest=False,
            query_refs=prepared_query_refs,
            target_refs=tuple(target_refs) if target_refs is not None else None,
            sort_records=False,
        )
        records = _rewrite_query_ids(
            result.records, nonce, query_count=len(paths), annotate_batch=annotate_batch
        )
        stats = result.stats
        if not prepared_query_refs:
            stats = replace(stats, query_count=len(paths))
        return AlignmentResult(
            backend=result.backend,
            records=records,
            inputs=result.inputs,
            targets=result.targets,
            params=result.params,
            stats=stats,
            index=result.index,
            sort_records=sort_records,
        )


__all__ = ["align_many"]
