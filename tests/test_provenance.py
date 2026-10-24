from __future__ import annotations

import json
from pathlib import Path

from gseek.model import AlignmentRecord, DistanceRecord, RunManifest, ToolRef
from gseek.provenance import (
    alignment_result_hash,
    canonical_json,
    distance_result_hash,
    input_ref,
    read_manifest,
    recipe_hash,
    result_hash,
    semantic_params,
    sha256_bytes,
    sha256_file,
    sha256_text,
    to_jsonable,
    utc_now_iso,
    verify_manifest,
    write_manifest,
)
from gseek.types import RUN_SCHEMA


def _manifest(tmp_path: Path) -> RunManifest:
    query = tmp_path / "query.fa"
    query.write_text(">q\nACGT\n", encoding="utf-8")
    target = tmp_path / "target.fa"
    target.write_text(">t\nACGT\n", encoding="utf-8")
    query_ref = input_ref(query)
    target_ref = input_ref(target)
    tools = (ToolRef(name="fake-tool", version="1.0.0"),)
    digest = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=(query_ref,),
        targets=(target_ref,),
        params={"evalue": 1e-5},
        tools=tools,
    )
    return RunManifest(
        run_id="run-1",
        recipe_hash=digest,
        created_at=utc_now_iso(),
        gseek_version="0.1.0",
        python_version="3.12.0",
        platform="Linux-test",
        hostname="host",
        backend="fake",
        backend_version="1",
        tools=tools,
        inputs=(query_ref,),
        targets=(target_ref,),
        params={"evalue": 1e-5},
        commands=(),
        env={"LC_ALL": "C"},
    )


def test_hashes_and_canonical_json() -> None:
    assert sha256_text("abc") == sha256_bytes(b"abc")
    assert canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'
    assert sha256_file(Path(__file__)) == sha256_file(Path(__file__))


def test_to_jsonable_handles_paths_and_enums(tmp_path: Path) -> None:
    assert to_jsonable(tmp_path / "x") == str(tmp_path / "x")
    assert to_jsonable({"x": (1, 2)}) == {"x": [1, 2]}


def test_to_jsonable_sorts_sets_for_deterministic_json() -> None:
    assert to_jsonable({"b", "a"}) == ["a", "b"]
    assert to_jsonable(frozenset({"c", "a", "b"})) == ["a", "b", "c"]


def test_semantic_params_drops_volatile_keys() -> None:
    assert semantic_params({"threads": 8, "evalue": 1e-5, "debug": True, "missing": None}) == {
        "evalue": 1e-5
    }


def test_recipe_hash_ignores_threads_and_changes_with_semantics(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    base = manifest.recipe_hash
    threaded = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=manifest.inputs,
        targets=manifest.targets,
        params={"evalue": 1e-5, "threads": 64},
        tools=manifest.tools,
    )
    changed = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=manifest.inputs,
        targets=manifest.targets,
        params={"evalue": 1e-10},
        tools=manifest.tools,
    )
    assert threaded == base
    assert changed != base


def test_manifest_write_read_round_trip(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "run.json"
    write_manifest(path, manifest)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == RUN_SCHEMA
    restored = read_manifest(path)
    assert restored == manifest
    assert verify_manifest(restored) == ()


def test_verify_manifest_detects_tampering(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    Path(manifest.inputs[0].path).write_text(">q\nTTTT\n", encoding="utf-8")
    issues = verify_manifest(manifest)
    assert issues
    assert issues[0].severity == "error"


def test_result_hash_is_stable() -> None:
    record = AlignmentRecord(query_id="q", target_id="t", identity=99.0)
    assert result_hash((record,)) == result_hash((record,))
    assert result_hash((record,)) != result_hash(
        (AlignmentRecord(query_id="q", target_id="t", identity=98.0),)
    )


def test_alignment_result_hash_is_independent_of_record_order() -> None:
    first = AlignmentRecord(query_id="q", target_id="a", identity=90.0)
    second = AlignmentRecord(query_id="q", target_id="b", identity=80.0)
    assert alignment_result_hash((first, second)) == alignment_result_hash((second, first))
    assert result_hash((first, second)) == result_hash((second, first))


def test_distance_result_hash_is_independent_of_record_order() -> None:
    first = DistanceRecord(query_id="q", reference_id="a", distance=0.1)
    second = DistanceRecord(query_id="q", reference_id="b", distance=0.2)
    assert distance_result_hash((first, second)) == distance_result_hash((second, first))
