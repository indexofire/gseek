"""Real-tool tests for the M3 adapters (BWA, mash, sourmash)."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

import gseek
from gseek.index import IndexStore

pytestmark = pytest.mark.integration


def _fixture_files(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    random.seed(20261024)
    sequence = "".join(random.choice("ACGT") for _ in range(600))
    target = tmp_path / "targets.fa"
    target.write_text(
        f">t1\n{sequence}\n>t2\n{'TTTT' + sequence[4:]}\n",
        encoding="utf-8",
    )
    query = tmp_path / "query.fa"
    query.write_text(f">q1\n{sequence[100:400]}\n", encoding="utf-8")
    reads = tmp_path / "reads.fq"
    reads.write_text(
        f"@read1\n{sequence[100:400]}\n+\n{'I' * 300}\n",
        encoding="utf-8",
    )
    return query, target, reads, sequence


@pytest.mark.skipif(shutil.which("bwa") is None, reason="BWA not installed")
def test_bwa_assembly_and_index_reuse(tmp_path: Path) -> None:
    query, target, _, _ = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "bwa.manifest.json"
    result = gseek.align(
        backend="bwa",
        query=query,
        target=target,
        store=store,
        manifest=manifest,
    )
    assert result.records
    assert result.index is not None
    assert not result.index.cache_hit
    assert any(record.identity == 100.0 for record in result.records)
    assert any(record.strand == "+" for record in result.records)
    assert gseek.verify(manifest) == ()

    second = gseek.align(backend="bwa", query=query, target=target, store=store)
    assert second.index is not None
    assert second.index.cache_hit
    assert second.manifest is not None
    assert [command.phase for command in second.manifest.commands] == ["align"]


@pytest.mark.skipif(shutil.which("bwa") is None, reason="BWA not installed")
def test_bwa_paired_fastq(tmp_path: Path) -> None:
    _, target, _, sequence = _fixture_files(tmp_path)
    r1 = tmp_path / "r1.fq"
    r2 = tmp_path / "r2.fq"
    r1.write_text(f"@pair1\n{sequence[100:250]}\n+\n{'I' * 150}\n", encoding="utf-8")
    r2.write_text(f"@pair1\n{sequence[250:400]}\n+\n{'I' * 150}\n", encoding="utf-8")
    result = gseek.align(
        backend="bwa",
        query=(r1, r2),
        target=target,
        paired=True,
        query_kind="fastq",
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records


@pytest.mark.skipif(shutil.which("mash") is None, reason="mash not installed")
def test_mash_distance_and_index_reuse(tmp_path: Path) -> None:
    query, target, _, _ = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "mash.manifest.json"
    result = gseek.distance(
        backend="mash",
        query=query,
        reference=target,
        store=store,
        manifest=manifest,
    )
    assert result.records
    assert result.index is not None
    assert not result.index.cache_hit
    assert all(record.distance is not None and record.distance >= 0 for record in result.records)
    assert result.records[0].tags["metric"] == "mash_distance"
    assert gseek.verify(manifest) == ()

    second = gseek.distance(backend="mash", query=query, reference=target, store=store)
    assert second.index is not None
    assert second.index.cache_hit


@pytest.mark.skipif(shutil.which("sourmash") is None, reason="sourmash not installed")
def test_sourmash_distance_and_index_reuse(tmp_path: Path) -> None:
    query, target, _, _ = _fixture_files(tmp_path)
    store = IndexStore(tmp_path / "cache")
    result = gseek.distance(
        backend="sourmash",
        query=query,
        reference=target,
        store=store,
    )
    assert result.records
    assert result.index is not None
    assert not result.index.cache_hit
    assert result.records[0].jaccard is not None
    assert result.records[0].distance == pytest.approx(1.0 - result.records[0].jaccard)

    second = gseek.distance(backend="sourmash", query=query, reference=target, store=store)
    assert second.index is not None
    assert second.index.cache_hit


@pytest.mark.skipif(shutil.which("mash") is None, reason="mash not installed")
def test_mash_screen_containment(tmp_path: Path) -> None:
    _, target, reads, _ = _fixture_files(tmp_path)
    result = gseek.distance(
        backend="mash",
        query=reads,
        reference=target,
        params={"mode": "screen"},
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records
    assert result.records[0].containment is not None
    assert result.records[0].tags["metric"] == "mash_screen"


@pytest.mark.skipif(shutil.which("sourmash") is None, reason="sourmash not installed")
def test_sourmash_containment_mode(tmp_path: Path) -> None:
    query, target, _, _ = _fixture_files(tmp_path)
    result = gseek.distance(
        backend="sourmash",
        query=query,
        reference=target,
        params={"containment": True},
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records
    assert result.records[0].containment is not None
    assert result.records[0].jaccard is None
    assert result.records[0].tags["metric"] == "sourmash_containment"


@pytest.mark.skipif(shutil.which("bwa") is None, reason="BWA not installed")
def test_bwa_index_params_end_to_end(tmp_path: Path) -> None:
    query, target, _, _ = _fixture_files(tmp_path)
    result = gseek.align(
        backend="bwa",
        query=query,
        target=target,
        params={"algorithm": "bwtsw"},
        store=IndexStore(tmp_path / "cache-params"),
    )
    assert result.records
