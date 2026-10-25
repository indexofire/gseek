from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from conftest import FakeAdapter, FakeDistanceAdapter
from gseek.adapter import AlignPlan, AlignRequest, DistanceRequest, ParseContext, PlanContext
from gseek.api import replay
from gseek.capabilities import ToolchainProbe
from gseek.errors import ReplayMismatchError
from gseek.execution import CommandPlan, ToolProbe
from gseek.index import IndexPolicy, IndexStore
from gseek.model import AlignmentRecord, IndexRef
from gseek.registry import _reset_for_tests, register_adapter
from gseek.runner import run_alignment, run_distance


class OutOfOrderAdapter(FakeAdapter):
    """Emit records in reverse canonical order to exercise replay hashing."""

    def probe(self, resolver: object, *, strict: bool = False) -> ToolchainProbe:
        return ToolchainProbe(
            backend="fake",
            tools=(ToolProbe(name="fake-tool", path=Path(sys.executable), version="1.0"),),
            ok=True,
        )

    def plan_align(
        self, request: AlignRequest, index: IndexRef | None, ctx: PlanContext
    ) -> AlignPlan:
        return AlignPlan(commands=(CommandPlan(argv=(sys.executable, "-c", "pass")),), stream=True)

    def parse_align(
        self, ctx: ParseContext, stream: Iterable[str] | None = None
    ) -> Iterator[AlignmentRecord]:
        for _ in stream or ():
            pass
        for target_id in ("z", "a"):
            yield AlignmentRecord(
                query_id="q1",
                target_id=target_id,
                query_length=10,
                target_length=10,
                query_start=0,
                query_end=10,
                target_start=0,
                target_end=10,
                identity=90.0,
                aligned_length=10,
                tags={"identity_method": "fake"},
            )


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_manifest_records_result_hash(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.manifest is not None
    assert result.manifest.result_hash is not None
    assert len(result.manifest.result_hash) == 64
    assert result.manifest.extra["kind"] == "alignment"


def test_replay_alignment_matches(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeAdapter)
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    replay_result = replay(manifest_path, check=True, store=store)
    assert replay_result.matches is True
    assert replay_result.expected_hash == replay_result.result_hash
    assert replay_result.result.records


def test_replay_distance_matches(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeDistanceAdapter)
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "distance.json"
    run_distance(
        DistanceRequest(query=(query,), reference=(target,)),
        adapter=FakeDistanceAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    replay_result = replay(manifest_path, store=store)
    assert replay_result.matches is True
    assert replay_result.result.records


def test_replay_detects_tampered_result_hash(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeAdapter)
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["result_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReplayMismatchError):
        replay(manifest_path, check=True, store=store)


def test_replay_ignores_manifest_commands(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeAdapter)
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["commands"][0]["argv"] = ["rm", "-rf", str(tmp_path)]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    replay_result = replay(manifest_path, store=store)
    assert replay_result.matches is True
    assert tmp_path.exists()


def test_replay_cli_json_and_check(fasta_pair, tmp_path: Path, capsys, monkeypatch) -> None:
    from gseek.cli import main

    monkeypatch.setenv("GSEEK_CACHE_DIR", str(tmp_path / "cache-cli"))
    register_adapter(FakeAdapter)
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    assert main(["replay", str(manifest_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["matches"] is True
    assert payload["record_count"] == 1

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["result_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert main(["replay", str(manifest_path), "--check"]) == 6


def test_replay_multi_record_out_of_order_matches(fasta_pair, tmp_path: Path) -> None:
    register_adapter(OutOfOrderAdapter)
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    first = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=OutOfOrderAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    assert [record.target_id for record in first.records] == ["a", "z"]

    outcome = replay(manifest_path, check=True, store=store)
    assert outcome.matches is True
    assert outcome.expected_hash == outcome.result_hash


def test_replay_restores_index_policy(fasta_pair, tmp_path: Path) -> None:
    # A run with index='none' (subject mode) must replay in subject mode:
    # replaying under the default AUTO changes the command sequence and
    # fabricates result mismatches.
    import json as _json

    from conftest import FakeAdapter

    query, target = fasta_pair
    manifest_path = tmp_path / "run.json"
    run_alignment(
        AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
        adapter=FakeAdapter(),
        manifest_path=manifest_path,
    )
    document = _json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["extra"]["index_policy"] == "none"
