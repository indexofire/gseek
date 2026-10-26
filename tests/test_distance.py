from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeDistanceAdapter
from gseek.adapter import DistanceRequest
from gseek.provenance import verify_manifest
from gseek.runner import run_distance


def test_run_distance_end_to_end(fasta_pair, tmp_path: Path) -> None:
    query, reference = fasta_pair
    result = run_distance(
        DistanceRequest(query=(query,), reference=(reference,), threads=2),
        adapter=FakeDistanceAdapter(),
    )
    assert result.backend == "fake-distance"
    assert len(result.records) == 1
    record = result.records[0]
    assert record.query_id == "q1"
    assert record.reference_id == "r1"
    assert record.distance == 0.1
    assert record.shared_hashes == 10
    assert result.index is None
    assert result.manifest is not None
    assert [command.phase for command in result.manifest.commands] == ["distance"]
    assert verify_manifest(result.manifest) == ()


def test_run_distance_empty(fasta_pair) -> None:
    query, reference = fasta_pair
    result = run_distance(
        DistanceRequest(query=(query,), reference=(reference,), params={"no_hits": True}),
        adapter=FakeDistanceAdapter(),
    )
    assert result.records == ()
    assert result.stats.record_count == 0


def test_run_distance_rejects_non_distance_records(fasta_pair, tmp_path: Path) -> None:
    # Mirrors the alignment branch: an adapter yielding anything but
    # DistanceRecord values must fail loudly instead of silently dropping rows.
    from gseek.errors import ParseError

    class RogueAdapter(FakeDistanceAdapter):
        def parse_distance(self, ctx, stream=None):  # type: ignore[no-untyped-def]
            if stream is not None:
                for _ in stream:
                    pass
            yield "not-a-record"

    query, reference = fasta_pair
    with pytest.raises(ParseError, match="non-DistanceRecord"):
        run_distance(
            DistanceRequest(query=(query,), reference=(reference,)),
            adapter=RogueAdapter(),
        )


def test_external_reference_with_provided_sha_never_hashes(
    fasta_pair, tmp_path, monkeypatch
) -> None:
    # With caller-provided external_sha256/external_size, gseek only does a
    # stat-level size check and never re-reads file content.
    import gseek.provenance as provenance

    query, reference = fasta_pair
    digest = provenance.sha256_file(reference)  # test fixture; not counted

    calls: list[str] = []
    monkeypatch.setattr(
        provenance,
        "sha256_file",
        lambda path, **kw: (calls.append(str(path)), digest)[1],
    )
    real_tree = provenance._tree_digest_and_size

    def counting_tree(path):  # type: ignore[no-untyped-def]
        calls.append(f"tree:{path}")
        return real_tree(path)

    monkeypatch.setattr(provenance, "_tree_digest_and_size", counting_tree)

    from conftest import FakeDistanceAdapter
    from gseek.adapter import DistanceRequest
    from gseek.runner import run_distance

    run_distance(
        DistanceRequest(
            query=(query,),
            reference=(reference,),
            external_reference=True,
            external_sha256=digest,
            external_size=reference.stat().st_size,
        ),
        adapter=FakeDistanceAdapter(),
    )
    assert calls == [], f"provided sha must not trigger content hashing: {calls}"
