"""Real-tool replay matrix across every built-in backend.

Each case produces at least two records, writes a manifest, verifies the
recorded file hashes, and replays with ``check=True``. This is the end-to-end
guard for canonical ``result_hash`` ordering and tool-path binding.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest

import gseek
from gseek.index import IndexStore
from gseek.provenance import alignment_result_hash, distance_result_hash
from gseek.types import JsonValue

pytestmark = pytest.mark.integration

_CASES = (
    ("blast", "align", {}, ("blastn", "makeblastdb")),
    ("minimap2", "align", {"preset": "asm5"}, ("minimap2",)),
    ("mummer", "align", {}, ("nucmer", "show-coords")),
    ("kma", "align", {}, ("kma",)),
    ("bwa", "align", {}, ("bwa",)),
    ("mash", "distance", {"max_distance": 1.0}, ("mash",)),
    ("sourmash", "distance", {"k": 21, "scaled": 10}, ("sourmash",)),
    ("skani", "distance", {}, ("skani",)),
)


@pytest.fixture(scope="module")
def replay_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("replay-dataset")
    random.seed(20261030)
    first = "".join(random.choice("ACGT") for _ in range(5000))
    second = "".join(random.choice("ACGT") for _ in range(5000))
    complement = str.maketrans("ACGT", "TGCA")

    def reverse_complement(sequence: str) -> str:
        return sequence.translate(complement)[::-1]

    (root / "target1.fa").write_text(f">t1\n{first}\n", encoding="utf-8")
    (root / "target2.fa").write_text(f">t2\n{second}\n", encoding="utf-8")
    (root / "query.fa").write_text(
        f">q1\n{first[100:2100]}\n>q2\n{reverse_complement(second[100:2100])}\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize(
    ("backend", "kind", "params", "requirements"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_replay_matrix(
    backend: str,
    kind: str,
    params: dict[str, JsonValue],
    requirements: tuple[str, ...],
    replay_dataset: Path,
    tmp_path: Path,
) -> None:
    missing = [tool for tool in requirements if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")

    query = replay_dataset / "query.fa"
    targets = (replay_dataset / "target1.fa", replay_dataset / "target2.fa")
    store = IndexStore(tmp_path / "cache")
    manifest = tmp_path / "run.json"

    if kind == "align":
        result = gseek.align(
            backend=backend,
            query=query,
            target=targets,
            params=params,
            store=store,
            manifest=manifest,
        )
        expected_hash = alignment_result_hash(result.records)
    else:
        result = gseek.distance(
            backend=backend,
            query=query,
            reference=targets,
            params=params,
            store=store,
            manifest=manifest,
        )
        expected_hash = distance_result_hash(result.records)

    assert len(result.records) >= 2
    assert result.manifest is not None
    assert result.manifest.result_hash == expected_hash
    assert gseek.verify(manifest) == ()

    outcome = gseek.replay(manifest, check=True, store=store)
    assert outcome.matches is True
    assert outcome.expected_hash == outcome.result_hash
