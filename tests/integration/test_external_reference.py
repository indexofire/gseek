"""Real-tool external-reference tests (pre-built mash/sourmash sketches)."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

import gseek
from gseek.index import IndexStore
from gseek.types import InputKind

pytestmark = pytest.mark.integration


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    random.seed(20261031)
    sequence = "".join(random.choice("ACGT") for _ in range(5000))
    target = tmp_path / "target.fa"
    target.write_text(f">target\n{sequence}\n", encoding="utf-8")
    query = tmp_path / "query.fa"
    query.write_text(f">query\n{sequence[100:4900]}\n", encoding="utf-8")
    return query, target


@pytest.mark.skipif(shutil.which("mash") is None, reason="mash not installed")
def test_mash_external_sketch_and_replay(tmp_path: Path) -> None:
    query, target = _fixture(tmp_path)
    sketch = tmp_path / "refs"
    subprocess.run(
        ["mash", "sketch", "-k", "21", "-s", "1000", "-S", "42", "-o", str(sketch), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    reference = sketch.with_suffix(".msh")
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "mash.json"

    result = gseek.distance(
        backend="mash",
        query=query,
        reference=reference,
        external_reference=True,
        params={"k": 21, "sketch_size": 1000, "max_distance": 1.0},
        store=store,
        manifest=manifest,
    )
    assert result.records
    assert result.index is None
    assert result.references[0].kind is InputKind.SKETCH
    assert gseek.verify(manifest) == ()

    outcome = gseek.replay(manifest, check=True, store=store)
    assert outcome.matches is True


@pytest.mark.skipif(shutil.which("sourmash") is None, reason="sourmash not installed")
def test_sourmash_external_signature_and_replay(tmp_path: Path) -> None:
    query, target = _fixture(tmp_path)
    reference = tmp_path / "refs.sig"
    subprocess.run(
        [
            "sourmash",
            "sketch",
            "dna",
            "-p",
            "k=21,scaled=10",
            "-o",
            str(reference),
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "sourmash.json"

    result = gseek.distance(
        backend="sourmash",
        query=query,
        reference=reference,
        external_reference=True,
        params={"k": 21, "scaled": 10},
        store=store,
        manifest=manifest,
    )
    assert result.records
    assert result.index is None
    assert result.references[0].kind is InputKind.SKETCH
    assert gseek.verify(manifest) == ()

    outcome = gseek.replay(manifest, check=True, store=store)
    assert outcome.matches is True
