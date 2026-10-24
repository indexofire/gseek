from __future__ import annotations

import json
from pathlib import Path

from gseek.index import IndexPolicy, IndexStore, compute_index_key, default_cache_root
from gseek.model import InputRef
from gseek.types import InputKind


def _ref(sha: str = "a" * 64, *, kind: InputKind = InputKind.FASTA) -> InputRef:
    return InputRef(path="/tmp/x.fa", size=10, sha256=sha, kind=kind, records=1)


def test_compute_index_key_is_content_addressed() -> None:
    base = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(_ref(),),
        params={"preset": "asm5"},
        tool_versions={"minimap2": "2.30"},
    )
    same = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(_ref(),),
        params={"preset": "asm5", "threads": 16},
        tool_versions={"minimap2": "2.30"},
    )
    changed_param = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(_ref(),),
        params={"preset": "asm20"},
        tool_versions={"minimap2": "2.30"},
    )
    changed_tool = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(_ref(),),
        params={"preset": "asm5"},
        tool_versions={"minimap2": "2.31"},
    )
    changed_content = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(_ref("b" * 64),),
        params={"preset": "asm5"},
        tool_versions={"minimap2": "2.30"},
    )
    assert same == base
    assert changed_param != base
    assert changed_tool != base
    assert changed_content != base


def test_publish_and_lookup(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "a" * 64
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    ref = store.publish(
        staging,
        adapter="fake",
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="b" * 64,
        params={"x": 1},
        tool_versions={"fake": "1.0"},
    )
    assert not ref.cache_hit
    found = store.lookup("fake", key)
    assert found is not None
    assert found.cache_hit
    assert found.entry == "index.txt"
    assert (store.path_for("fake", key) / "index.txt").read_text(encoding="utf-8") == "payload"


def test_lookup_ignores_corrupt_index(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "c" * 64
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    store.publish(
        staging,
        adapter="fake",
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="d" * 64,
        params={},
        tool_versions={},
    )
    index_json = store.path_for("fake", key) / "index.json"
    data = json.loads(index_json.read_text(encoding="utf-8"))
    data["key"] = "e" * 64
    index_json.write_text(json.dumps(data), encoding="utf-8")
    assert store.lookup("fake", key) is None


def test_gc_dry_run_and_apply(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    for sha in ("1", "2"):
        key = sha * 64
        staging = store.new_staging("fake", key)
        (staging / "index.txt").write_text("payload", encoding="utf-8")
        store.publish(
            staging,
            adapter="fake",
            key=key,
            adapter_version="1",
            entry="index.txt",
            artifacts=("index.txt",),
            source_sha256="f" * 64,
            params={},
            tool_versions={},
        )
    entries = store.list_indexes()
    assert len(entries) == 2
    planned = store.gc(max_bytes=0, dry_run=True)
    assert len(planned) == 2
    assert len(store.list_indexes()) == 2
    removed = store.gc(max_bytes=0, dry_run=False)
    assert len(removed) == 2
    assert store.list_indexes() == ()


def test_lock_context_creates_lock_file(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "9" * 64
    with store.lock("fake", key):
        lock_file = store.adapter_root("fake") / f"{key}.lock"
        assert lock_file.exists()


def test_default_cache_root_respects_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GSEEK_CACHE_DIR", str(tmp_path / "custom"))
    assert default_cache_root() == tmp_path / "custom"


def test_index_policy_values() -> None:
    assert {policy.value for policy in IndexPolicy} == {"auto", "reuse", "rebuild", "none"}


def test_publish_accepts_prefix_entry_with_artifacts(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "f" * 64
    staging = store.new_staging("blast", key)
    for suffix in (".nhr", ".nin", ".nsq"):
        (staging / f"targets{suffix}").write_text("payload", encoding="utf-8")
    ref = store.publish(
        staging,
        adapter="blast",
        key=key,
        adapter_version="1",
        entry="targets",
        artifacts=("targets.nhr", "targets.nin", "targets.nsq"),
        source_sha256="a" * 64,
        params={},
        tool_versions={},
    )
    assert ref.entry == "targets"
    found = store.lookup("blast", key)
    assert found is not None
    assert found.entry == "targets"
