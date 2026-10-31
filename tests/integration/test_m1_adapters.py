"""Real-tool tests for the M1 adapters.

Run with:
    PATH=/path/to/blast/minimap2/bin:$PATH PYTHONPATH=src pytest -m integration
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

import gseek
from gseek.index import IndexStore
from gseek.provenance import input_ref
from gseek.types import InputKind

pytestmark = pytest.mark.integration


def _fixture_files(tmp_path: Path) -> tuple[Path, Path]:
    random.seed(20261022)
    sequence = "".join(random.choice("ACGT") for _ in range(800))
    target = tmp_path / "targets.fa"
    target.write_text(
        f">arcC_1\n{sequence}\n>arcC_2\n{'TTTT' + sequence[4:]}\n",
        encoding="utf-8",
    )
    query = tmp_path / "query.fa"
    query.write_text(f">contig1\n{sequence[100:500]}\n", encoding="utf-8")
    return query, target


@pytest.mark.skipif(
    shutil.which("blastn") is None or shutil.which("makeblastdb") is None,
    reason="BLAST+ not installed",
)
def test_blast_end_to_end_and_index_reuse(tmp_path: Path) -> None:
    query, target = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "blast.manifest.json"
    result = gseek.align(
        backend="blast",
        query=query,
        target=target,
        store=store,
        manifest=manifest,
    )
    assert result.records
    assert any(record.identity == 100.0 for record in result.records)
    assert result.index is not None
    assert not result.index.cache_hit
    assert gseek.verify(manifest) == ()

    second = gseek.align(
        backend="blast",
        query=query,
        target=target,
        store=store,
    )
    assert second.index is not None
    assert second.index.cache_hit
    assert second.manifest is not None
    assert [command.phase for command in second.manifest.commands] == ["align"]


@pytest.mark.skipif(
    shutil.which("makeblastdb") is None or shutil.which("blastn") is None,
    reason="BLAST+ not installed",
)
def test_blast_external_db_with_precomputed_refs(tmp_path: Path) -> None:
    """Given a consumer-owned BLAST database, When align uses params['db']
    with index='none' and precomputed refs, Then blastn runs without building
    a gseek index and returns the same hits."""
    query, target = _fixture_files(tmp_path)
    db_prefix = tmp_path / "owned" / "sequences"
    db_prefix.parent.mkdir()
    subprocess.run(
        ["makeblastdb", "-in", str(target), "-dbtype", "nucl", "-out", str(db_prefix)],
        check=True,
        capture_output=True,
        text=True,
    )
    query_ref = input_ref(query, kind=InputKind.FASTA, compute_records=False)
    target_ref = input_ref(target, kind=InputKind.FASTA, compute_records=False)
    result = gseek.align(
        backend="blast",
        query=query,
        target=target,
        params={"db": str(db_prefix)},
        index="none",
        query_refs=(query_ref,),
        target_refs=(target_ref,),
    )
    assert result.records
    assert result.index is None
    assert result.inputs == (query_ref,)
    assert result.targets == (target_ref,)


@pytest.mark.skipif(
    shutil.which("blastn") is None or shutil.which("makeblastdb") is None,
    reason="BLAST+ not installed",
)
def test_align_many_runs_one_blast_and_restores_query_ids(tmp_path: Path) -> None:
    """Given two FASTA queries, When align_many runs against one target, Then
    both query ids come back unchanged in one batched alignment result."""
    query, target = _fixture_files(tmp_path)
    second = tmp_path / "query2.fa"
    second.write_text(
        ">contig2\n" + query.read_text().splitlines()[1][:300] + "\n", encoding="utf-8"
    )
    result = gseek.align_many(
        backend="blast",
        queries=[query, second],
        target=target,
    )
    assert {record.query_id for record in result.records} == {"contig1", "contig2"}
    assert [Path(ref.path).name for ref in result.inputs] == ["query.fa", "query2.fa"]


@pytest.mark.skipif(shutil.which("minimap2") is None, reason="minimap2 not installed")
def test_minimap2_end_to_end_and_index_reuse(tmp_path: Path) -> None:
    query, target = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    result = gseek.align(
        backend="minimap2",
        query=query,
        target=target,
        preset="asm5",
        store=store,
    )
    assert result.records
    assert result.index is not None
    assert not result.index.cache_hit
    assert any(record.identity is not None and record.identity >= 99.0 for record in result.records)

    second = gseek.align(
        backend="minimap2",
        query=query,
        target=target,
        preset="asm5",
        store=store,
    )
    assert second.index is not None
    assert second.index.cache_hit


@pytest.mark.skipif(shutil.which("minimap2") is None, reason="minimap2 not installed")
def test_minimap2_short_read_fastq(tmp_path: Path) -> None:
    query, target = _fixture_files(tmp_path)
    sequence = query.read_text(encoding="utf-8").splitlines()[1]
    reads = tmp_path / "reads.fq"
    reads.write_text(
        f"@read1\n{sequence}\n+\n{'I' * len(sequence)}\n",
        encoding="utf-8",
    )
    result = gseek.align(
        backend="minimap2",
        query=reads,
        target=target,
        preset="sr",
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records
    assert result.records[0].query_id == "read1"


@pytest.mark.skipif(shutil.which("minimap2") is None, reason="minimap2 not installed")
def test_minimap2_thread_count_keeps_canonical_records(tmp_path: Path) -> None:
    query, target = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    one = gseek.align(
        backend="minimap2",
        query=query,
        target=target,
        preset="asm5",
        threads=1,
        store=store,
    )
    many = gseek.align(
        backend="minimap2",
        query=query,
        target=target,
        preset="asm5",
        threads=3,
        store=store,
    )
    assert gseek.result_hash(one.records) == gseek.result_hash(many.records)
