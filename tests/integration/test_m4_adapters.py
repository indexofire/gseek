"""Real-tool tests for the M4 adapters and protein BLAST modes."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

import gseek
from gseek.index import IndexStore

pytestmark = pytest.mark.integration

_PROTEIN = "MKTIIALSYIFCLVFADYKD"
_DNA = "ATGAAAACAATTATTGCTCTTTCTTATATTTTTTGTCTTGTTTTTGCTGATTATAAAGAT"


def _protein_files(tmp_path: Path) -> tuple[Path, Path]:
    protein = tmp_path / "protein.fa"
    protein.write_text(f">p1\n{_PROTEIN}\n", encoding="utf-8")
    dna = tmp_path / "dna.fa"
    dna.write_text(f">d1\n{_DNA}\n", encoding="utf-8")
    return protein, dna


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("blastp", "blastx", "tblastn", "tblastx")),
    reason="BLAST+ protein tools not installed",
)
@pytest.mark.parametrize(
    ("task", "query_name", "target_name"),
    [
        ("blastp", "protein.fa", "protein.fa"),
        ("blastx", "dna.fa", "protein.fa"),
        ("tblastn", "protein.fa", "dna.fa"),
        ("tblastx", "dna.fa", "dna.fa"),
    ],
)
def test_blast_protein_tasks(
    task: str,
    query_name: str,
    target_name: str,
    tmp_path: Path,
) -> None:
    protein, dna = _protein_files(tmp_path)
    files = {"protein.fa": protein, "dna.fa": dna}
    result = gseek.align(
        backend="blast",
        query=files[query_name],
        target=files[target_name],
        params={"task": task},
        store=IndexStore(tmp_path / f"cache-{task}"),
    )
    assert result.records
    assert any(record.identity == 100.0 for record in result.records)


@pytest.mark.skipif(shutil.which("sourmash") is None, reason="sourmash not installed")
def test_sourmash_gather(tmp_path: Path) -> None:
    random.seed(20261025)
    sequence = "".join(random.choice("ACGT") for _ in range(1200))
    target = tmp_path / "targets.fa"
    target.write_text(f">t1\n{sequence}\n", encoding="utf-8")
    query = tmp_path / "query.fa"
    query.write_text(f">q1\n{sequence[100:700]}\n", encoding="utf-8")
    store = IndexStore(tmp_path / "cache")
    result = gseek.distance(
        backend="sourmash",
        query=query,
        reference=target,
        params={"mode": "gather", "threshold_bp": 0, "scaled": 100, "k": 21},
        store=store,
    )
    assert result.records
    assert result.records[0].tags["metric"] == "sourmash_gather"
    assert result.records[0].containment is not None
    assert result.manifest is not None
    assert gseek.verify(result.manifest) == ()


@pytest.mark.skipif(shutil.which("skani") is None, reason="skani not installed")
def test_skani_ani_and_index_reuse(tmp_path: Path) -> None:
    random.seed(20261026)
    sequence = "".join(random.choice("ACGT") for _ in range(5000))
    target = tmp_path / "target.fa"
    target.write_text(f">target\n{sequence}\n", encoding="utf-8")
    query = tmp_path / "query.fa"
    query.write_text(f">query\n{sequence[100:4900]}\n", encoding="utf-8")
    store = IndexStore(tmp_path / "cache")
    result = gseek.distance(
        backend="skani",
        query=query,
        reference=target,
        store=store,
    )
    assert result.records
    assert result.records[0].ani is not None and result.records[0].ani >= 99.0
    assert result.records[0].aligned_fraction is not None
    assert result.index is not None
    assert not result.index.cache_hit

    second = gseek.distance(backend="skani", query=query, reference=target, store=store)
    assert second.index is not None
    assert second.index.cache_hit


@pytest.mark.skipif(shutil.which("blastn") is None, reason="BLAST+ not installed")
def test_replay_real_blast_manifest(tmp_path: Path) -> None:
    _, dna = _protein_files(tmp_path)
    query = target = dna
    manifest = tmp_path / "run.json"
    store = IndexStore(tmp_path / "cache")
    first = gseek.align(
        backend="blast",
        query=query,
        target=target,
        store=store,
        manifest=manifest,
    )
    assert first.records
    assert first.manifest is not None
    outcome = gseek.replay(manifest, check=True, store=store)
    assert outcome.matches is True
    assert outcome.result_hash == first.manifest.result_hash


@pytest.mark.skipif(shutil.which("skani") is None, reason="skani not installed")
def test_skani_index_params_end_to_end(tmp_path: Path) -> None:
    random.seed(20261027)
    sequence = "".join(random.choice("ACGT") for _ in range(5000))
    target = tmp_path / "target.fa"
    target.write_text(f">target\n{sequence}\n", encoding="utf-8")
    query = tmp_path / "query.fa"
    query.write_text(f">query\n{sequence[100:4900]}\n", encoding="utf-8")
    result = gseek.distance(
        backend="skani",
        query=query,
        reference=target,
        params={"compression_factor": 200},
        store=IndexStore(tmp_path / "cache-params"),
    )
    assert result.records
