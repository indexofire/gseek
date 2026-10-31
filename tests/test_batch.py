from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from conftest import FakeAdapter
from gseek.adapter import AlignPlan, AlignRequest, ParseContext, PlanContext
from gseek.batch import align_many
from gseek.errors import ConfigError
from gseek.execution import CommandPlan
from gseek.index import IndexStore
from gseek.model import AlignmentRecord, IndexRef
from gseek.provenance import result_hash


class BatchFakeAdapter(FakeAdapter):
    """Emit one record per FASTA record in the (merged) query file."""

    def plan_align(
        self, request: AlignRequest, index: IndexRef | None, ctx: PlanContext
    ) -> AlignPlan:
        del index, ctx
        script = (
            "import sys\n"
            "for line in open(sys.argv[1], encoding='utf-8'):\n"
            "    if line.startswith('>'):\n"
            "        q = line[1:].split()[0]\n"
            "        print(q + '\\tt1\\t100.0\\t4\\t0\\t0\\t0\\t4\\t4\\t0\\t1e-5\\t100.0')\n"
        )
        return AlignPlan(
            commands=(
                CommandPlan(
                    argv=(sys.executable, "-c", script, str(request.query[0])),
                    label="batch fake align",
                ),
            ),
            stream=True,
        )

    def parse_align(
        self, ctx: ParseContext, stream: Iterable[str] | None = None
    ) -> Iterator[AlignmentRecord]:
        return super().parse_align(ctx, stream)


def test_align_many_merges_queries_and_restores_ids(fasta_pair, tmp_path: Path) -> None:
    """Given two FASTA queries, When align_many runs, Then one adapter
    invocation covers both and the returned records carry the original query
    ids (not the internal batch prefixes)."""
    query, target = fasta_pair
    second = tmp_path / "second.fa"
    second.write_text(">q2\nACGT\n", encoding="utf-8")
    result = align_many(
        queries=[query, second],
        target=target,
        adapter=BatchFakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        annotate_batch=True,
    )
    assert [record.query_id for record in result.records] == ["q1", "q2"]
    assert [record.tags["batch_index"] for record in result.records] == ["0", "1"]
    assert [Path(ref.path).name for ref in result.inputs] == ["query.fa", "second.fa"]


def test_align_many_result_hash_uses_restored_records(fasta_pair, tmp_path: Path) -> None:
    """Given annotate_batch=True, When result_hash is computed, Then it must
    describe the restored query ids/tags, not the internal batch prefixes."""
    query, target = fasta_pair
    second = tmp_path / "second.fa"
    second.write_text(">q2\nACGT\n", encoding="utf-8")
    result = align_many(
        queries=[query, second],
        target=target,
        adapter=BatchFakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        annotate_batch=True,
    )
    reparsed = tuple(AlignmentRecord.from_dict(record.to_dict()) for record in result.records)
    assert result_hash(result.records) == result_hash(reparsed)


def test_align_many_sort_records_controls_final_order(fasta_pair, tmp_path: Path) -> None:
    """Given reverse-ordered input files, When sort_records is toggled, Then
    the final AlignmentResult either canonicalizes or keeps parser order."""
    query, target = fasta_pair
    second = tmp_path / "second.fa"
    second.write_text(">q2\nACGT\n", encoding="utf-8")
    canonical = align_many(
        queries=[second, query],
        target=target,
        adapter=BatchFakeAdapter(),
        store=IndexStore(tmp_path / "canonical"),
        sort_records=True,
    )
    parser_order = align_many(
        queries=[second, query],
        target=target,
        adapter=BatchFakeAdapter(),
        store=IndexStore(tmp_path / "parser"),
        sort_records=False,
    )
    assert [record.query_id for record in canonical.records] == ["q1", "q2"]
    assert [record.query_id for record in parser_order.records] == ["q2", "q1"]


def test_align_many_can_skip_query_input_hashing(
    fasta_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given compute_inputs=False, When align_many runs, Then original query
    files are not hashed; only the records and per-file stats survive."""
    query, target = fasta_pair
    second = tmp_path / "second.fa"
    second.write_text(">q2\nACGT\n", encoding="utf-8")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("align_many must not hash query inputs")

    monkeypatch.setattr("gseek.batch.input_refs", fail)
    result = align_many(
        queries=[query, second],
        target=target,
        adapter=BatchFakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        compute_inputs=False,
        sort_records=False,
    )
    assert [record.query_id for record in result.records] == ["q1", "q2"]
    assert result.inputs == ()
    assert result.stats.query_count == 2


def test_align_many_rejects_empty_and_fastq_queries(fasta_pair, tmp_path: Path) -> None:
    """Given no queries or a FASTQ query, When align_many runs, Then fail with
    a typed ConfigError before launching the aligner."""
    _query, target = fasta_pair
    with pytest.raises(ConfigError, match="at least one query"):
        align_many(queries=[], target=target, adapter=BatchFakeAdapter())
    fastq = tmp_path / "reads.fq"
    fastq.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="FASTA"):
        align_many(queries=[fastq], target=target, adapter=BatchFakeAdapter())
