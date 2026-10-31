from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeAdapter
from gseek.api import iter_align
from gseek.index import IndexStore


def test_iter_align_yields_records(fasta_pair, tmp_path: Path) -> None:
    """Given a fake alignment backend, When iter_align runs, Then records are
    yielded without constructing an AlignmentResult (no manifest/stats)."""
    query, target = fasta_pair
    records = list(
        iter_align(
            query=query,
            target=target,
            adapter=FakeAdapter(),
            store=IndexStore(tmp_path / "cache"),
        )
    )
    assert [record.query_id for record in records] == ["q1"]
    assert [record.target_id for record in records] == ["t1"]


def test_iter_align_skips_hashes_when_no_index_is_needed(
    fasta_pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given iter_align with index='none', When it runs, Then neither query
    nor target files are hashed for unused provenance."""
    query, target = fasta_pair

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("iter_align must not prepare refs")

    monkeypatch.setattr("gseek.runner._prepare_queries", fail)
    monkeypatch.setattr("gseek.runner._prepare_targets", fail)
    records = list(
        iter_align(
            query=query,
            target=target,
            adapter=FakeAdapter(),
            store=IndexStore(tmp_path / "cache"),
            index="none",
        )
    )
    assert [record.query_id for record in records] == ["q1"]


def test_iter_align_prepare_refs_can_be_requested(fasta_pair, tmp_path: Path) -> None:
    """Given prepare_refs=True, When iter_align runs, Then the eager-ref path
    still yields records (custom adapters can rely on PlanContext refs)."""
    query, target = fasta_pair
    records = list(
        iter_align(
            query=query,
            target=target,
            adapter=FakeAdapter(),
            store=IndexStore(tmp_path / "cache"),
            index="none",
            prepare_refs=True,
        )
    )
    assert [record.query_id for record in records] == ["q1"]


def test_iter_align_accepts_precomputed_refs(fasta_pair, tmp_path: Path) -> None:
    """Given precomputed refs, When iter_align runs, Then it passes them to
    the runner instead of re-hashing the query/target."""
    from gseek.provenance import input_ref
    from gseek.types import InputKind

    query, target = fasta_pair
    query_ref = input_ref(query, kind=InputKind.FASTA, compute_records=False)
    target_ref = input_ref(target, kind=InputKind.FASTA, compute_records=False)
    records = list(
        iter_align(
            query=query,
            target=target,
            adapter=FakeAdapter(),
            store=IndexStore(tmp_path / "cache"),
            query_refs=(query_ref,),
            target_refs=(target_ref,),
        )
    )
    assert len(records) == 1
