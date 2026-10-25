from __future__ import annotations

import json
from pathlib import Path

import pytest

from gseek.errors import CacheError, IndexBuildError
from gseek.index import IndexStore, default_cache_root, tree_size


def _publish(store: IndexStore, key: str, adapter: str = "fake") -> None:
    staging = store.new_staging(adapter, key)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    store.publish(
        staging,
        adapter=adapter,
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="b" * 64,
        params={},
        tool_versions={},
    )


def test_disabled_store_does_nothing(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache", enabled=False)
    assert store.lookup("fake", "a" * 64) is None
    assert store.list_indexes() == ()
    assert store.gc() == ()


def test_invalid_names(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    with pytest.raises(CacheError):
        store.adapter_root("../escape")
    with pytest.raises(CacheError):
        store.path_for("fake", "../escape")


def test_publish_missing_entry(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    staging = store.new_staging("fake", "a" * 64)
    with pytest.raises(IndexBuildError):
        store.publish(
            staging,
            adapter="fake",
            key="a" * 64,
            adapter_version="1",
            entry="missing.txt",
            artifacts=(),
            source_sha256="b" * 64,
            params={},
            tool_versions={},
        )


def test_publish_missing_artifact(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    staging = store.new_staging("fake", "a" * 64)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    with pytest.raises(IndexBuildError):
        store.publish(
            staging,
            adapter="fake",
            key="a" * 64,
            adapter_version="1",
            entry="index.txt",
            artifacts=("missing.*",),
            source_sha256="b" * 64,
            params={},
            tool_versions={},
        )


def test_publish_is_idempotent(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "a" * 64
    _publish(store, key)
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("different", encoding="utf-8")
    ref = store.publish(
        staging,
        adapter="fake",
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="c" * 64,
        params={},
        tool_versions={},
    )
    assert ref.cache_hit is True
    assert not staging.exists()
    assert (store.path_for("fake", key) / "index.txt").read_text(encoding="utf-8") == "payload"


def test_non_object_manifest_is_a_cache_miss(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "b" * 64
    entry = store.path_for("fake", key)
    entry.mkdir(parents=True)
    (entry / "index.json").write_text("[]", encoding="utf-8")
    assert store.lookup("fake", key) is None
    listed = store.list_indexes()
    assert len(listed) == 1
    assert listed[0].created_at is None


def test_lookup_rejects_entry_path_traversal(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "d" * 64
    entry = store.path_for("fake", key)
    entry.mkdir(parents=True)
    outside = store.index_root / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    document = {
        "schema": "gseek.index/1",
        "key": key,
        "adapter": "fake",
        "adapter_version": "1",
        "entry": "../../outside.txt",
        "artifacts": [],
        "source_sha256": "a" * 64,
        "params": {},
        "tool_versions": {},
        "created_at": "2026-01-01T00:00:00Z",
    }
    (entry / "index.json").write_text(json.dumps(document), encoding="utf-8")
    assert store.lookup("fake", key) is None


def test_disabled_store_rejects_writes(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache", enabled=False)
    with pytest.raises(CacheError):
        store.new_staging("fake", "a" * 64)
    with pytest.raises(CacheError), store.lock("fake", "a" * 64):
        pass


def test_publish_replace_publishes_new_payload(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "a" * 64
    _publish(store, key)
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("rebuilt", encoding="utf-8")
    ref = store.publish(
        staging,
        adapter="fake",
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="c" * 64,
        params={},
        tool_versions={},
        replace=True,
    )
    assert ref.cache_hit is False
    assert (store.path_for("fake", key) / "index.txt").read_text(encoding="utf-8") == "rebuilt"


def test_tree_size_and_dot_dirs(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    _publish(store, "a" * 64)
    assert tree_size(store.path_for("fake", "a" * 64)) > 0
    dot = store.adapter_root("fake") / ".staging-ignored"
    dot.mkdir()
    assert len(store.list_indexes()) == 1


def test_gc_does_not_report_failed_deletion(tmp_path: Path, monkeypatch) -> None:
    store = IndexStore(tmp_path / "cache")
    _publish(store, "a" * 64)
    monkeypatch.setattr("gseek.index.shutil.rmtree", lambda *args, **kwargs: None)
    assert store.gc(max_bytes=0, dry_run=False) == ()
    assert store.list_indexes()


def test_default_cache_root_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GSEEK_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_root() == tmp_path / "xdg" / "gseek"


def test_gc_cleans_stale_staging_directories(tmp_path: Path) -> None:
    # Staging dirs left by crashed builds (dead pid in the name) are garbage;
    # live builds (current pid) must never be collected.
    import os

    store = IndexStore(tmp_path / "cache")
    adapter_dir = store.index_root / "fake"
    adapter_dir.mkdir(parents=True)
    dead = adapter_dir / f".staging-{'a' * 64}-999999999-deadbeef"
    dead.mkdir()
    (dead / "index.bin").write_text("payload", encoding="utf-8")
    alive = adapter_dir / f".staging-{'b' * 64}-{os.getpid()}-0000"
    alive.mkdir()

    listed = store.gc(dry_run=True)
    assert any(entry.path == dead for entry in listed)
    assert not any(entry.path == alive for entry in listed)
    assert dead.exists() and alive.exists()

    removed = store.gc(dry_run=False)
    assert any(entry.path == dead and entry.removed for entry in removed)
    assert not dead.exists()
    assert alive.exists()


def test_gc_cleans_malformed_staging_directories(tmp_path: Path) -> None:
    # Unparsable pid => treat as stale (a live build always embeds its pid).
    store = IndexStore(tmp_path / "cache")
    adapter_dir = store.index_root / "fake"
    adapter_dir.mkdir(parents=True)
    junk = adapter_dir / ".staging-not-a-valid-name"
    junk.mkdir()
    removed = store.gc(dry_run=False)
    assert any(entry.path == junk and entry.removed for entry in removed)
    assert not junk.exists()


def test_lock_rejects_escaping_keys(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    with pytest.raises(CacheError), store.lock("fake", "../escape"):
        pass


def test_gc_removes_lock_files_after_deletion(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    key = "3" * 64
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    store.publish(
        staging,
        adapter="fake",
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="7" * 64,
        params={},
        tool_versions={},
        created_at="2020-01-01T00:00:00",
    )
    lock_file = store.index_root / "fake" / f"{key}.lock"
    removed = store.gc(max_age_days=0.0, dry_run=False)
    assert removed
    assert not (store.index_root / "fake" / key).exists()
    assert not lock_file.exists(), "stale .lock file must not accumulate"
