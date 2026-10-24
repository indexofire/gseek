"""Real-tool tests for the M2 adapters (KMA and MUMmer4)."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

import gseek
from gseek.index import IndexStore

pytestmark = pytest.mark.integration


def _fixture_files(tmp_path: Path) -> tuple[Path, Path, str]:
    random.seed(20261023)
    sequence = "".join(random.choice("ACGT") for _ in range(500))
    target = tmp_path / "targets.fa"
    target.write_text(
        f">t1\n{sequence}\n>t2\n{'TTTT' + sequence[4:]}\n",
        encoding="utf-8",
    )
    query = tmp_path / "query.fa"
    query.write_text(f">q1\n{sequence[50:350]}\n", encoding="utf-8")
    return query, target, sequence


def _reverse_complement(sequence: str) -> str:
    return sequence.translate(str.maketrans("ACGT", "TGCA"))[::-1]


@pytest.mark.skipif(shutil.which("kma") is None, reason="KMA not installed")
def test_kma_assembly_and_index_reuse(tmp_path: Path) -> None:
    query, target, _ = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "kma.manifest.json"
    result = gseek.align(
        backend="kma",
        query=query,
        target=target,
        store=store,
        manifest=manifest,
    )
    assert result.records
    assert result.index is not None
    assert not result.index.cache_hit
    assert all(record.identity is not None for record in result.records)
    assert all(record.query_interval is None for record in result.records)
    assert gseek.verify(manifest) == ()

    second = gseek.align(backend="kma", query=query, target=target, store=store)
    assert second.index is not None
    assert second.index.cache_hit
    assert second.manifest is not None
    assert [command.phase for command in second.manifest.commands] == ["align"]


@pytest.mark.skipif(shutil.which("kma") is None, reason="KMA not installed")
def test_kma_fastq_depth(tmp_path: Path) -> None:
    _, target, sequence = _fixture_files(tmp_path)
    reads = tmp_path / "reads.fq"
    reads.write_text(
        f"@read1\n{sequence[50:350]}\n+\n{'I' * 300}\n",
        encoding="utf-8",
    )
    result = gseek.align(
        backend="kma",
        query=reads,
        target=target,
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records
    assert any(record.depth is not None and record.depth > 0 for record in result.records)


@pytest.mark.skipif(
    shutil.which("nucmer") is None or shutil.which("show-coords") is None,
    reason="MUMmer4 not installed",
)
def test_mummer_reverse_strand_and_no_index(tmp_path: Path) -> None:
    _, target, sequence = _fixture_files(tmp_path)
    reverse = tmp_path / "reverse.fa"
    reverse.write_text(f">q1\n{_reverse_complement(sequence[50:350])}\n", encoding="utf-8")
    result = gseek.align(
        backend="mummer",
        query=reverse,
        target=target,
    )
    assert result.records
    assert result.index is None
    assert any(record.strand == "-" for record in result.records)
    assert any(record.identity == 100.0 for record in result.records)
    assert result.manifest is not None
    assert [command.phase for command in result.manifest.commands] == ["align", "align"]


@pytest.mark.skipif(shutil.which("kma") is None, reason="KMA not installed")
def test_kma_paired_fastq(tmp_path: Path) -> None:
    _, target, sequence = _fixture_files(tmp_path)
    r1 = tmp_path / "r1.fq"
    r2 = tmp_path / "r2.fq"
    r1.write_text(f"@r1\n{sequence[50:200]}\n+\n{'I' * 150}\n", encoding="utf-8")
    r2.write_text(f"@r2\n{sequence[200:350]}\n+\n{'I' * 150}\n", encoding="utf-8")
    result = gseek.align(
        backend="kma",
        query=(r1, r2),
        target=target,
        paired=True,
        query_kind="fastq",
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records
    assert any(record.depth is not None and record.depth > 0 for record in result.records)


@pytest.mark.skipif(shutil.which("kma") is None, reason="KMA not installed")
def test_kma_index_params_end_to_end(tmp_path: Path) -> None:
    query, target, _ = _fixture_files(tmp_path)
    result = gseek.align(
        backend="kma",
        query=query,
        target=target,
        params={"k": 17},
        store=IndexStore(tmp_path / "cache-params"),
    )
    assert result.records
