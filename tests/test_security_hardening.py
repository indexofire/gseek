"""Security hardening regressions for cache, manifest, and ref handling."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import FakeAdapter
from gseek.adapter import AlignPlan, AlignRequest, PlanContext
from gseek.api import align, verify
from gseek.batch import align_many
from gseek.errors import (
    CacheError,
    ConfigError,
    ExecutionError,
    IndexBuildError,
    InputError,
    ManifestError,
    ReplayMismatchError,
)
from gseek.execution import CommandPlan, Executor
from gseek.index import IndexPolicy, IndexStore
from gseek.provenance import (
    input_ref,
    read_manifest,
    recipe_hash,
    sha256_tree,
    verify_manifest,
    write_manifest,
)
from gseek.registry import _reset_for_tests, register_adapter
from gseek.replay import replay
from gseek.runner import run_alignment
from gseek.types import InputKind, RunStatus


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _manifest_run(tmp_path: Path):
    query = tmp_path / "query.fa"
    target = tmp_path / "target.fa"
    query.write_text(">q1\nACGT\n", encoding="utf-8")
    target.write_text(">t1\nACGT\n", encoding="utf-8")
    register_adapter(FakeAdapter)
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    result = run_alignment(
        AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
        adapter=FakeAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    assert result.manifest is not None
    return result.manifest, manifest_path, store, query, target


def _tamper(path: Path, **changes: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_gc_does_not_follow_symlinked_adapter_dir(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "index").mkdir(parents=True)
    victim = tmp_path / "victim"
    (victim / "precious").mkdir(parents=True)
    (victim / "precious" / "data.txt").write_text("important", encoding="utf-8")
    os.symlink(victim, cache / "index" / "fake")

    store = IndexStore(cache)
    assert store.list_indexes() == ()
    assert store.gc(max_bytes=0, dry_run=False) == ()
    assert (victim / "precious").exists()


def test_gc_does_not_follow_symlinked_key_dir(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    adapter_root = store.adapter_root("fake")
    adapter_root.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data.txt").write_text("important", encoding="utf-8")
    os.symlink(victim, adapter_root / ("a" * 64))

    assert store.list_indexes() == ()
    assert store.gc(max_bytes=0, dry_run=False) == ()
    assert (victim / "data.txt").exists()


def test_lookup_and_staging_reject_symlinked_adapter_dir(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "index").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.symlink(victim, cache / "index" / "fake")

    store = IndexStore(cache)
    assert store.lookup("fake", "a" * 64) is None
    with pytest.raises(CacheError):
        store.new_staging("fake", "a" * 64)


def test_index_key_and_adapter_names_are_strict(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    with pytest.raises(CacheError):
        store.path_for("fake", ".")
    with pytest.raises(CacheError):
        store.path_for("fake", "not-a-sha256")
    with pytest.raises(CacheError):
        store.new_staging("fake", "g" * 64)
    with pytest.raises(CacheError):
        store.adapter_root(".")
    with pytest.raises(CacheError):
        store.adapter_root("..")


def test_publish_rejects_staging_outside_adapter_root(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.txt").write_text("payload", encoding="utf-8")

    with pytest.raises(IndexBuildError):
        store.publish(
            outside,
            adapter="fake",
            key="a" * 64,
            adapter_version="1",
            entry="index.txt",
            artifacts=("index.txt",),
            source_sha256="b" * 64,
            params={},
            tool_versions={},
        )


def _publish_small_index(tmp_path: Path) -> tuple[IndexStore, Path]:
    store = IndexStore(tmp_path / "cache")
    key = "a" * 64
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    store.publish(
        staging,
        adapter="fake",
        key=key,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="b" * 64,
        params={},
        tool_versions={},
    )
    return store, store.path_for("fake", key) / "index.txt"


def test_gc_skips_leased_index(tmp_path: Path) -> None:
    store, artifact = _publish_small_index(tmp_path)
    key = "a" * 64
    index_dir = store.path_for("fake", key)
    lease = store.lease("fake", key, interval_s=0.01)
    lease.start()
    try:
        time.sleep(0.03)
        assert store.gc(max_age_days=0.0, dry_run=False) == ()
        assert artifact.exists()
    finally:
        lease.close()
    assert store.gc(max_age_days=0.0, dry_run=False)
    assert not index_dir.exists()


def test_rebuild_refuses_leased_index(tmp_path: Path) -> None:
    store, artifact = _publish_small_index(tmp_path)
    key = "a" * 64
    lease = store.lease("fake", key)
    lease.start()
    try:
        staging = store.new_staging("fake", key)
        (staging / "index.txt").write_text("rebuilt", encoding="utf-8")
        with pytest.raises(IndexBuildError):
            store.publish(
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
    finally:
        lease.close()

    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("rebuilt", encoding="utf-8")
    store.publish(
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
    assert artifact.read_text(encoding="utf-8") == "rebuilt"


def test_publish_records_artifact_hashes(tmp_path: Path) -> None:
    from gseek.provenance import sha256_file

    store, artifact = _publish_small_index(tmp_path)
    document = json.loads((store.path_for("fake", "a" * 64) / "index.json").read_text())
    recorded = document["artifact_hashes"]["index.txt"]
    assert recorded["size"] == artifact.stat().st_size
    assert recorded["sha256"] == sha256_file(artifact)


def test_lookup_rejects_artifact_size_change(tmp_path: Path) -> None:
    store, artifact = _publish_small_index(tmp_path)
    artifact.write_text("payload-plus", encoding="utf-8")
    assert store.lookup("fake", "a" * 64) is None


def test_lookup_strict_mode_rejects_same_size_tamper(tmp_path: Path) -> None:
    store, artifact = _publish_small_index(tmp_path)
    artifact.write_text("PAYLOAD", encoding="utf-8")  # same length as "payload"
    assert store.lookup("fake", "a" * 64) is not None
    strict = IndexStore(store.root, verify_artifacts=True)
    assert strict.lookup("fake", "a" * 64) is None


def test_lookup_rejects_legacy_index_without_artifact_hashes(tmp_path: Path) -> None:
    store, _artifact = _publish_small_index(tmp_path)
    manifest = store.path_for("fake", "a" * 64) / "index.json"
    document = json.loads(manifest.read_text())
    del document["artifact_hashes"]
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert store.lookup("fake", "a" * 64) is None


def test_publish_rejects_symlinked_artifact(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    staging = store.new_staging("fake", "a" * 64)
    victim = tmp_path / "victim.txt"
    victim.write_text("important", encoding="utf-8")
    os.symlink(victim, staging / "index.txt")

    with pytest.raises(IndexBuildError):
        store.publish(
            staging,
            adapter="fake",
            key="a" * 64,
            adapter_version="1",
            entry="index.txt",
            artifacts=("index.txt",),
            source_sha256="b" * 64,
            params={},
            tool_versions={},
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_cache_directories_and_files_are_private(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    staging = store.new_staging("fake", "a" * 64)
    (staging / "index.txt").write_text("payload", encoding="utf-8")
    store.publish(
        staging,
        adapter="fake",
        key="a" * 64,
        adapter_version="1",
        entry="index.txt",
        artifacts=("index.txt",),
        source_sha256="b" * 64,
        params={},
        tool_versions={},
    )
    index_dir = store.path_for("fake", "a" * 64)
    assert store.index_root.stat().st_mode & 0o077 == 0
    assert store.adapter_root("fake").stat().st_mode & 0o077 == 0
    assert index_dir.stat().st_mode & 0o077 == 0
    assert (index_dir / "index.json").stat().st_mode & 0o077 == 0
    assert (index_dir / "index.txt").stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_manifest_write_is_private(tmp_path: Path) -> None:
    manifest, _, _, _, _ = _manifest_run(tmp_path)
    path = tmp_path / "private.json"
    write_manifest(path, manifest)
    assert path.stat().st_mode & 0o077 == 0


def test_verify_manifest_detects_recipe_hash_tamper(tmp_path: Path) -> None:
    manifest, _, _, _, _ = _manifest_run(tmp_path)
    tampered = dataclasses.replace(manifest, params={**manifest.params, "evalue": 1e-9})
    issues = verify_manifest(tampered)
    assert any(issue.code == "RECIPE_HASH_MISMATCH" for issue in issues)


def test_verify_manifest_rejects_empty_inputs_and_targets(tmp_path: Path) -> None:
    manifest, _, _, _, _ = _manifest_run(tmp_path)
    empty = dataclasses.replace(manifest, inputs=(), targets=())
    codes = {issue.code for issue in verify_manifest(empty)}
    assert "INPUTS_EMPTY" in codes
    assert "TARGETS_EMPTY" in codes


def test_verify_manifest_rejects_failed_run(tmp_path: Path) -> None:
    manifest, _, _, _, _ = _manifest_run(tmp_path)
    failed = dataclasses.replace(manifest, status=RunStatus.FAILED)
    codes = {issue.code for issue in verify_manifest(failed)}
    assert "RUN_STATUS_FAILED" in codes


def test_verify_manifest_strict_paths_rejects_escape(tmp_path: Path) -> None:
    _, manifest_path, _, _, _ = _manifest_run(tmp_path)
    issues = verify(manifest_path, base_dir=tmp_path / "elsewhere", strict_paths=True)
    assert any(issue.code.endswith("_PATH_OUTSIDE_BASE") for issue in issues)


def test_replay_strict_paths_rejects_escape(tmp_path: Path) -> None:
    _, manifest_path, store, _, _ = _manifest_run(tmp_path)
    with pytest.raises(ManifestError):
        replay(
            manifest_path,
            base_dir=tmp_path / "elsewhere",
            strict_paths=True,
            store=store,
        )


def test_replay_strict_paths_accepts_common_base(tmp_path: Path) -> None:
    _, manifest_path, store, _, _ = _manifest_run(tmp_path)
    outcome = replay(
        manifest_path,
        base_dir=tmp_path,
        strict_paths=True,
        check=True,
        store=store,
    )
    assert outcome.matches is True


def test_replay_check_requires_result_hash(tmp_path: Path) -> None:
    _, manifest_path, store, _, _ = _manifest_run(tmp_path)
    _tamper(manifest_path, result_hash=None)
    with pytest.raises(ManifestError):
        replay(manifest_path, check=True, store=store)


def test_replay_check_fails_on_input_hash_mismatch(tmp_path: Path) -> None:
    _, manifest_path, store, query, _ = _manifest_run(tmp_path)
    query.write_text(">q1\nTTTT\n", encoding="utf-8")
    with pytest.raises(ReplayMismatchError):
        replay(manifest_path, check=True, store=store)


def test_replay_check_fails_on_recipe_hash_mismatch(tmp_path: Path) -> None:
    manifest, manifest_path, store, _, _ = _manifest_run(tmp_path)
    tampered = dataclasses.replace(manifest, params={**manifest.params, "evalue": 1e-9})
    write_manifest(manifest_path, tampered)
    with pytest.raises(ReplayMismatchError):
        replay(manifest_path, check=True, store=store)


def test_replay_rejects_unknown_index_policy(tmp_path: Path) -> None:
    _, manifest_path, store, _, _ = _manifest_run(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["extra"]["index_policy"] = "bogus"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError):
        replay(manifest_path, store=store)


def test_replay_rejects_nul_in_manifest_path(tmp_path: Path) -> None:
    _, manifest_path, store, _, _ = _manifest_run(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["inputs"][0]["path"] = "bad\x00path.fa"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError):
        replay(manifest_path, store=store)


def test_cli_strict_paths_is_wired(tmp_path: Path, capsys) -> None:
    from gseek.cli import main

    _, manifest_path, _, _, _ = _manifest_run(tmp_path)
    code = main(
        [
            "verify",
            str(manifest_path),
            "--base-dir",
            str(tmp_path / "elsewhere"),
            "--strict-paths",
            "--json",
        ]
    )
    assert code == 4
    assert "PATH_OUTSIDE_BASE" in capsys.readouterr().out


def test_precomputed_target_refs_must_match_target_path(tmp_path: Path) -> None:
    query, target, other = _three_files(tmp_path)
    with pytest.raises(ConfigError):
        align(
            query=query,
            target=target,
            adapter=FakeAdapter(),
            target_refs=(input_ref(other, kind=InputKind.FASTA),),
        )


def test_align_many_target_refs_must_match_target_path(tmp_path: Path) -> None:
    query, target, other = _three_files(tmp_path)
    with pytest.raises(ConfigError):
        align_many(
            queries=[query],
            target=target,
            adapter=FakeAdapter(),
            target_refs=(input_ref(other, kind=InputKind.FASTA),),
        )


def _three_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    query = tmp_path / "query.fa"
    target = tmp_path / "target.fa"
    other = tmp_path / "other.fa"
    query.write_text(">q1\nACGT\n", encoding="utf-8")
    target.write_text(">t1\nACGT\n", encoding="utf-8")
    other.write_text(">t2\nTTTT\n", encoding="utf-8")
    return query, target, other


def test_expand_fasta_paths_skips_symlinked_files_in_directories(tmp_path: Path) -> None:
    from gseek.io import expand_fasta_paths

    target_dir = tmp_path / "targets"
    target_dir.mkdir()
    real = target_dir / "real.fa"
    real.write_text(">real\nACGT\n", encoding="utf-8")
    outside = tmp_path / "outside.fa"
    outside.write_text(">outside\nTTTT\n", encoding="utf-8")
    os.symlink(outside, target_dir / "link.fa")

    assert expand_fasta_paths([target_dir]) == (real,)


def test_gc_tolerates_malformed_staging_owner_json(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "cache")
    adapter = store.adapter_root("fake")
    adapter.mkdir(parents=True)
    staging = adapter / (".staging-" + "a" * 64 + "-999999999-deadbeef")
    staging.mkdir()
    (staging / ".owner.json").write_text("[]", encoding="utf-8")

    entries = store.gc(dry_run=True)
    assert any(entry.path == staging for entry in entries)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_run_manifest_rejects_non_finite_duration(tmp_path: Path, value: float) -> None:
    manifest, _, _, _, _ = _manifest_run(tmp_path)
    with pytest.raises(ManifestError):
        dataclasses.replace(manifest, duration_s=value)


def test_compressed_input_has_decompression_budget(tmp_path: Path) -> None:
    import gzip

    from gseek.errors import ParseError
    from gseek.io import open_text

    path = tmp_path / "bomb.fa.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b">q\n" + b"A" * 10000 + b"\n")
    with (
        pytest.raises(ParseError, match="decompressed input exceeds"),
        open_text(path, max_chars=100) as handle,
    ):
        handle.read()


def test_compressed_input_budget_env_override(tmp_path: Path, monkeypatch) -> None:
    import gzip

    from gseek.errors import ParseError
    from gseek.io import open_text

    path = tmp_path / "bomb.fa.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b">q\n" + b"A" * 10000 + b"\n")
    monkeypatch.setenv("GSEEK_MAX_DECOMPRESSED_BYTES", "100")
    with (
        pytest.raises(ParseError, match="decompressed input exceeds"),
        open_text(path) as handle,
    ):
        handle.read()


def test_executor_run_caps_captured_output() -> None:
    code = "import sys; sys.stdout.write('o' * 10000); sys.stderr.write('e' * 10000)"
    executor = Executor(stdout_limit=100, stderr_limit=100)
    result = executor.run(CommandPlan(argv=(sys.executable, "-c", code)))
    assert len(result.stdout) <= 100
    assert len(result.stderr) <= 100
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_executor_stream_rejects_oversized_line() -> None:
    code = "import sys; sys.stdout.write('x' * 10000)"
    executor = Executor(stdout_line_limit=100)
    with (
        pytest.raises(ExecutionError),
        executor.stream(CommandPlan(argv=(sys.executable, "-c", code))) as process,
    ):
        list(process.iter_lines())
        process.wait()


def test_read_manifest_rejects_oversized_file(tmp_path: Path) -> None:
    manifest, manifest_path, _, _, _ = _manifest_run(tmp_path)
    assert read_manifest(manifest_path).run_id == manifest.run_id
    with pytest.raises(ManifestError):
        read_manifest(manifest_path, max_bytes=16)


def test_sha256_tree_rejects_too_many_entries(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a").write_text("a", encoding="utf-8")
    (tree / "b").write_text("b", encoding="utf-8")
    with pytest.raises(Exception, match="entries"):
        sha256_tree(tree, max_entries=1)


def test_align_request_rejects_absurd_thread_count(tmp_path: Path) -> None:
    from gseek.adapter import AlignRequest

    query = tmp_path / "query.fa"
    target = tmp_path / "target.fa"
    query.write_text(">q\nACGT\n", encoding="utf-8")
    target.write_text(">t\nACGT\n", encoding="utf-8")
    with pytest.raises(Exception, match="threads"):
        AlignRequest(query=(query,), target=(target,), threads=1_000_000)


def _fake_tool(tmp_path: Path) -> Path:
    tool = tmp_path / "fake-tool"
    tool.write_text("#!/bin/sh\necho 'fake-tool 1.2.3'\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def test_tool_hash_is_opt_in_and_persistently_cached(tmp_path: Path, monkeypatch) -> None:
    from gseek.capabilities import ToolRequirement
    from gseek.execution import ToolResolver
    from gseek.provenance import sha256_file

    tool = _fake_tool(tmp_path)
    cache = tmp_path / "tool-hashes.json"
    requirement = ToolRequirement(
        "fake-tool",
        version_args=("--version",),
        version_pattern=r"fake-tool\s+(\d+)\.(\d+)\.(\d+)",
    )
    resolver = ToolResolver(
        overrides={"fake-tool": tool},
        hash_tools=True,
        hash_cache_path=cache,
    )
    probe = resolver.probe(requirement)
    digest = sha256_file(tool)
    assert probe.sha256 == digest
    assert probe.to_tool_ref().sha256 == digest

    import gseek.execution.tools as tools_module

    def fail_hash(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("persistent tool hash cache was not used")

    monkeypatch.setattr(tools_module, "sha256_file", fail_hash)
    second = ToolResolver(
        overrides={"fake-tool": tool},
        hash_tools=True,
        hash_cache_path=cache,
    )
    assert second.probe(requirement).sha256 == digest


def test_tool_hash_is_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    from gseek.capabilities import ToolRequirement
    from gseek.execution import ToolResolver

    monkeypatch.delenv("GSEEK_TOOL_HASH", raising=False)
    tool = _fake_tool(tmp_path)
    resolver = ToolResolver(overrides={"fake-tool": tool})
    probe = resolver.probe(
        ToolRequirement(
            "fake-tool",
            version_args=("--version",),
            version_pattern=r"fake-tool\s+(\d+)\.(\d+)\.(\d+)",
        )
    )
    assert probe.sha256 is None


def test_recipe_and_index_key_include_tool_hashes() -> None:
    from gseek.index import compute_index_key
    from gseek.model import ToolRef

    plain = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=(),
        targets=(),
        params={},
        tools=(ToolRef(name="tool", version="1"),),
    )
    hashed = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=(),
        targets=(),
        params={},
        tools=(ToolRef(name="tool", version="1", sha256="a" * 64),),
    )
    assert plain != hashed

    base_key = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(),
        params={},
        tool_versions={"tool": "1"},
    )
    hash_key = compute_index_key(
        adapter="fake",
        adapter_version="1",
        targets=(),
        params={},
        tool_versions={"tool": "1"},
        tool_hashes={"tool": "a" * 64},
    )
    assert base_key != hash_key


def test_tsv_escapes_control_characters() -> None:
    from gseek.views import _format_cell

    assert _format_cell("t\tX\nY") == "t\\tX\\nY"


def test_record_ids_reject_control_characters() -> None:
    from gseek.model import AlignmentRecord, DistanceRecord

    with pytest.raises(InputError):
        AlignmentRecord(query_id="q\tX", target_id="t")
    with pytest.raises(InputError):
        DistanceRecord(query_id="q", reference_id="r\nX")


def test_input_ref_rejects_control_path(tmp_path: Path) -> None:
    from gseek.errors import InputError
    from gseek.provenance import input_ref

    path = tmp_path / "bad\tname.fa"
    path.write_text(">q\nACGT\n", encoding="utf-8")
    with pytest.raises(InputError):
        input_ref(path)


def test_bwa_read_group_rejects_control_characters(tmp_path: Path) -> None:
    from gseek.adapters.bwa import BwaAdapter
    from gseek.errors import UnsupportedFeatureError
    from gseek.model import IndexRef

    query = tmp_path / "query.fa"
    target = tmp_path / "target.fa"
    query.write_text(">q\nACGT\n", encoding="utf-8")
    target.write_text(">t\nACGT\n", encoding="utf-8")
    adapter = BwaAdapter()
    request = AlignRequest(
        query=(query,),
        target=(target,),
        params={"read_group": "ID:x\n@SQ\tSN:fake"},
    )
    index = IndexRef(
        key="a" * 64,
        adapter="bwa",
        adapter_version="1",
        entry="targets",
        source_sha256="b" * 64,
    )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(request, index, PlanContext(workspace=tmp_path))


def test_verify_inputs_detects_mutation_during_run(tmp_path: Path) -> None:
    from gseek.runner import run_alignment

    class MutatingAdapter(FakeAdapter):
        def plan_align(self, request, index, ctx):  # type: ignore[no-untyped-def]
            del index, ctx
            script = (
                "import pathlib,sys; "
                "pathlib.Path(sys.argv[1]).write_text('>t1\\nTTTT\\n'); "
                "print('q1\\tt1\\t100.0\\t4\\t0\\t0\\t0\\t4\\t4\\t0\\t1e-5\\t100.0')"
            )
            return AlignPlan(
                commands=(
                    CommandPlan(
                        argv=(sys.executable, "-c", script, str(request.target[0])),
                        label="mutate target",
                    ),
                ),
                stream=True,
            )

    query, target, _other = _three_files(tmp_path)
    with pytest.raises(InputError, match="changed during the run"):
        run_alignment(
            AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
            adapter=MutatingAdapter(),
            store=IndexStore(tmp_path / "cache"),
            record_manifest=False,
            verify_inputs=True,
        )


def test_recipe_hash_recomputation_matches_real_manifest(tmp_path: Path) -> None:
    manifest, _, _, _, _ = _manifest_run(tmp_path)
    recomputed = recipe_hash(
        backend=manifest.backend,
        adapter_version=manifest.backend_version,
        inputs=manifest.inputs,
        targets=manifest.targets,
        params=manifest.params,
        tools=manifest.tools,
        index_key=manifest.index.key if manifest.index is not None else None,
        gseek_version_value=manifest.gseek_version,
    )
    assert recomputed == manifest.recipe_hash
