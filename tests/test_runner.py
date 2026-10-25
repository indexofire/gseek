from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from conftest import FakeAdapter, FakeDistanceAdapter
from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    DistanceRequest,
    IndexPlan,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.api import verify
from gseek.capabilities import ToolchainProbe
from gseek.errors import ConfigError, ExecutionError, IndexBuildError, InputError, ParseError
from gseek.execution import CommandPlan, Executor, ToolProbe
from gseek.index import IndexPolicy, IndexStore
from gseek.model import AlignmentRecord, IndexRef
from gseek.provenance import read_manifest, verify_manifest
from gseek.runner import run_alignment, run_distance
from gseek.types import InputKind, JsonValue
from gseek.views import to_jsonl, to_tsv


class ChangingIndexAdapter(FakeAdapter):
    """Write a different index payload on every build."""

    builds = 0

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        type(self).builds += 1
        entry = ctx.index_dir or ctx.workspace
        script = (
            "import pathlib, sys; "
            "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
        )
        command = CommandPlan(
            argv=(
                sys.executable,
                "-c",
                script,
                str(entry / "index.txt"),
                f"build-{type(self).builds}",
            )
        )
        return IndexPlan(
            commands=(command,),
            entry="index.txt",
            artifacts=("index.txt",),
        )


class BareToolAdapter(FakeAdapter):
    """Plan a bare tool name while reporting an absolute probe path."""

    def __init__(self, tool: Path) -> None:
        self.tool = tool

    def probe(self, resolver: object, *, strict: bool = False) -> ToolchainProbe:
        return ToolchainProbe(
            backend="fake",
            tools=(ToolProbe(name="fake-tool", path=self.tool, version="1.0"),),
            ok=True,
        )

    def plan_align(
        self, request: AlignRequest, index: IndexRef | None, ctx: PlanContext
    ) -> AlignPlan:
        return AlignPlan(commands=(CommandPlan(argv=("fake-tool",)),), stream=True)

    def parse_align(
        self, ctx: ParseContext, stream: Iterable[str] | None = None
    ) -> Iterator[AlignmentRecord]:
        for _ in stream or ():
            pass
        yield AlignmentRecord(
            query_id="q1",
            target_id="t1",
            query_length=4,
            target_length=4,
            query_start=0,
            query_end=4,
            target_start=0,
            target_end=4,
            identity=100.0,
            aligned_length=4,
            tags={"identity_method": "fake"},
        )


def _request(query: Path, target: Path, **params: JsonValue) -> AlignRequest:
    return AlignRequest(
        query=(query,),
        target=(target,),
        params=params,
        threads=2,
        index_policy=IndexPolicy.AUTO,
    )


def test_run_alignment_end_to_end(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    manifest_path = tmp_path / "run.json"
    result = run_alignment(
        _request(query, target, evalue=1e-5),
        adapter=FakeAdapter(),
        store=store,
        manifest_path=manifest_path,
    )
    assert result.backend == "fake"
    assert len(result.records) == 1
    record = result.records[0]
    assert record.query_id == "q1"
    assert record.target_id == "t1"
    assert record.query_coverage == pytest.approx(1.0)
    assert result.stats.query_count == 1
    assert result.stats.target_count == 1
    assert result.index is not None
    assert result.index.cache_hit is False
    assert result.manifest is not None
    assert [command.phase for command in result.manifest.commands] == ["index", "align"]
    assert manifest_path.is_file()
    assert verify_manifest(read_manifest(manifest_path)) == ()


def test_second_run_reuses_index(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    first = run_alignment(_request(query, target), adapter=FakeAdapter(), store=store)
    second = run_alignment(_request(query, target), adapter=FakeAdapter(), store=store)
    assert first.index is not None and second.index is not None
    assert second.index.cache_hit is True
    assert second.manifest is not None
    assert [command.phase for command in second.manifest.commands] == ["align"]


def test_index_policy_none_skips_index(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    request = AlignRequest(
        query=(query,),
        target=(target,),
        index_policy=IndexPolicy.NONE,
    )
    result = run_alignment(request, adapter=FakeAdapter(), store=store)
    assert result.index is None
    assert result.manifest is not None
    assert [command.phase for command in result.manifest.commands] == ["align"]


def test_disabled_store_skips_index(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache", enabled=False),
    )
    assert result.index is None
    assert result.manifest is not None
    assert [command.phase for command in result.manifest.commands] == ["align"]


def test_index_policy_reuse_without_cache_fails(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    request = AlignRequest(
        query=(query,),
        target=(target,),
        index_policy=IndexPolicy.REUSE,
    )
    with pytest.raises(IndexBuildError):
        run_alignment(request, adapter=FakeAdapter(), store=IndexStore(tmp_path / "cache"))


def test_rebuild_policy_rebuilds_index(fasta_pair, tmp_path: Path) -> None:
    ChangingIndexAdapter.builds = 0
    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    first = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=ChangingIndexAdapter(),
        store=store,
    )
    assert first.index is not None
    index_path = store.path_for("fake", first.index.key) / "index.txt"
    assert index_path.read_text(encoding="utf-8") == "build-1"

    second = run_alignment(
        AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.REBUILD),
        adapter=ChangingIndexAdapter(),
        store=store,
    )
    assert second.index is not None
    assert second.index.cache_hit is False
    assert index_path.read_text(encoding="utf-8") == "build-2"
    assert second.manifest is not None
    assert [command.phase for command in second.manifest.commands] == ["index", "align"]


def test_executor_binds_probed_tool_path(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    tool = tmp_path / "fake-tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)

    result = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=BareToolAdapter(tool),
        store=IndexStore(tmp_path / "cache"),
    )
    assert len(result.records) == 1
    assert result.manifest is not None
    align_command = result.manifest.commands[-1]
    assert align_command.argv[0] == str(tool)


def test_non_streaming_adapter(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = run_alignment(
        _request(query, target),
        adapter=FakeAdapter(stream=False),
        store=IndexStore(tmp_path / "cache"),
    )
    assert len(result.records) == 1


def test_empty_result(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = run_alignment(
        _request(query, target, no_hits=True),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.records == ()
    assert result.stats.record_count == 0


def test_manifest_env_uses_executor_base_env(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    executor = Executor(base_env={"PATH": "/custom/bin", "LC_ALL": "C"})
    result = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        executor=executor,
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.manifest is not None
    assert result.manifest.env["PATH"] == "/custom/bin"


def test_index_key_tracks_package_version(fasta_pair, tmp_path: Path, monkeypatch) -> None:
    import gseek.runner as runner_module

    query, target = fasta_pair
    store = IndexStore(tmp_path / "cache")
    first = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=store,
    )
    monkeypatch.setattr(runner_module, "gseek_version", lambda: "9.9.9")
    second = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=store,
    )
    assert first.index is not None and second.index is not None
    assert first.index.key != second.index.key
    assert second.index.adapter_version.endswith("+gseek.9.9.9")


def test_manifest_records_absolute_paths_and_verify_from_elsewhere(
    fasta_pair, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest_path = tmp_path / "run.json"
    result = run_alignment(
        AlignRequest(query=(Path("query.fa"),), target=(Path("target.fa"),)),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        manifest_path=manifest_path,
    )
    assert result.manifest is not None
    assert all(Path(ref.path).is_absolute() for ref in result.manifest.inputs)
    assert all(Path(ref.path).is_absolute() for ref in result.manifest.targets)

    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert verify(manifest_path) == ()


def test_result_serialization_and_views(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = run_alignment(
        _request(query, target),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
    )
    payload = result.to_dict()
    json.dumps(payload)
    jsonl = to_jsonl(result)
    lines = jsonl.strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["type"] == "header"
    assert json.loads(lines[1])["query_id"] == "q1"
    tsv = to_tsv(result)
    assert tsv.splitlines()[0].startswith("query_id\ttarget_id")


def test_external_reference_skips_index_and_records_sketch_kind(fasta_pair, tmp_path: Path) -> None:
    query, _target = fasta_pair
    external = tmp_path / "refs.msh"
    external.write_bytes(b"fake-msh")
    result = run_distance(
        DistanceRequest(
            query=(query,),
            reference=(external,),
            external_reference=True,
        ),
        adapter=FakeDistanceAdapter(),
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.index is None
    assert result.references[0].kind is InputKind.SKETCH
    assert result.manifest is not None
    assert result.manifest.extra["external_reference"] is True


def test_external_reference_accepts_sketch_directory(fasta_pair, tmp_path: Path) -> None:
    query, _target = fasta_pair
    reference = tmp_path / "rocksdb"
    reference.mkdir()
    (reference / "CURRENT").write_text("MANIFEST-1\n", encoding="utf-8")
    result = run_distance(
        DistanceRequest(
            query=(query,),
            reference=(reference,),
            external_reference=True,
        ),
        adapter=FakeDistanceAdapter(),
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.references[0].path == str(reference.resolve())
    assert result.references[0].kind is InputKind.SKETCH
    assert result.index is None


def test_external_reference_digest_skips_hashing(fasta_pair, tmp_path: Path, monkeypatch) -> None:
    query, _target = fasta_pair
    reference = tmp_path / "refs.msh"
    reference.write_bytes(b"fake-msh")
    calls = []
    import gseek.provenance as provenance

    monkeypatch.setattr(
        provenance,
        "sha256_file",
        lambda path, **kwargs: calls.append(path) or "0" * 64,
    )
    result = run_distance(
        DistanceRequest(
            query=(query,),
            reference=(reference,),
            external_reference=True,
            external_sha256="a" * 64,
            external_size=8,
        ),
        adapter=FakeDistanceAdapter(),
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.references[0].sha256 == "a" * 64
    assert reference not in calls


def test_distance_record_manifest_false_skips_result_hash(
    fasta_pair, tmp_path: Path, monkeypatch
) -> None:
    query, target = fasta_pair
    import gseek.runner as runner

    monkeypatch.setattr(
        runner,
        "distance_result_hash",
        lambda records: (_ for _ in ()).throw(AssertionError("hash should be skipped")),
    )
    result = run_distance(
        DistanceRequest(query=(query,), reference=(target,)),
        adapter=FakeDistanceAdapter(),
        store=IndexStore(tmp_path / "cache"),
        record_manifest=False,
    )
    assert result.manifest is None


def test_record_manifest_false_skips_query_record_counts(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        record_manifest=False,
    )
    assert result.inputs[0].records is None


def test_bad_manifest_path_fails_before_any_command(fasta_pair, tmp_path: Path) -> None:
    # Config errors must fail fast: no probe, no index build, no alignment.
    from conftest import FakeAdapter

    calls: list[str] = []

    class SpyAdapter(FakeAdapter):
        def probe(self, resolver, *, strict: bool = False):  # type: ignore[no-untyped-def]
            calls.append("probe")
            return super().probe(resolver, strict=strict)

        def plan_align(self, request, index, ctx):  # type: ignore[no-untyped-def]
            calls.append("plan_align")
            return super().plan_align(request, index, ctx)

    query, target = fasta_pair
    with pytest.raises(InputError, match="manifest path"):
        run_alignment(
            AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
            adapter=SpyAdapter(),
            manifest_path=tmp_path / "manifest.txt",
        )
    assert calls == []


def test_manifest_path_requires_record_manifest_fails_fast(fasta_pair, tmp_path: Path) -> None:
    from conftest import FakeAdapter

    query, target = fasta_pair
    with pytest.raises(ConfigError, match="record_manifest"):
        run_alignment(
            AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
            adapter=FakeAdapter(),
            manifest_path=tmp_path / "manifest.json",
            record_manifest=False,
        )


def test_failed_run_still_writes_failed_manifest(fasta_pair, tmp_path: Path) -> None:
    # A crashed run must leave an audit trail: commands that ran + FAILED.
    from conftest import FakeAdapter

    class CrashingAdapter(FakeAdapter):
        def plan_align(self, request, index, ctx):  # type: ignore[no-untyped-def]
            from gseek.execution import CommandPlan

            command = CommandPlan(
                argv=(sys.executable, "-c", "import sys; sys.exit(7)"),
                label="doomed",
            )
            return AlignPlan(commands=(command,), stream=True)

    query, target = fasta_pair
    destination = tmp_path / "failed.json"
    with pytest.raises(ExecutionError):
        run_alignment(
            AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
            adapter=CrashingAdapter(),
            manifest_path=destination,
        )
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["errors"]
    assert document["commands"], "executed commands must be audited"
    assert any(command["exit_code"] == 7 for command in document["commands"])


def test_required_tools_gate_missing_optional_binary(fasta_pair, tmp_path: Path) -> None:
    # Optional spec tools bypass the strict probe; a request that actually
    # needs one must fail fast with an actionable error, not at subprocess
    # spawn (or worse, run an outdated binary).
    from conftest import FakeAdapter
    from gseek.errors import ToolNotFoundError

    class NeedsMissingTool(FakeAdapter):
        def validate_tools(self, request, probe):  # type: ignore[no-untyped-def]
            raise ToolNotFoundError(
                "backend 'fake' requires 'gseek-nonexistent-tool-x'",
                hint="install gseek-nonexistent-tool-x",
                context={"backend": "fake", "binary": "gseek-nonexistent-tool-x"},
            )

    query, target = fasta_pair
    with pytest.raises(ToolNotFoundError) as exc_info:
        run_alignment(
            AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
            adapter=NeedsMissingTool(),
        )
    assert "gseek-nonexistent-tool-x" in str(exc_info.value)
    assert exc_info.value.hint


def test_default_resolver_is_shared_between_calls(fasta_pair) -> None:
    # Re-probing every tool on every call costs ~240ms for the blast suite;
    # the default resolver must be a process-level singleton.
    from gseek import runner as runner_module

    first = runner_module._default_resolver()
    second = runner_module._default_resolver()
    assert first is second


def test_targets_skip_record_counting_when_manifest_off(fasta_pair, tmp_path: Path) -> None:
    # record_manifest=False must skip counting for targets too, not just
    # queries: counting is a full text scan that never feeds the index key.
    query, target = fasta_pair
    result = run_alignment(
        AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
        adapter=FakeAdapter(),
        record_manifest=False,
    )
    assert all(ref.records is None for ref in result.targets)


def test_parse_failure_manifest_audits_streamed_command(fasta_pair, tmp_path: Path) -> None:
    # A parser blowing up mid-stream must still audit the command that ran
    # (and any leading commands), not just ExecutionErrors.
    from conftest import FakeAdapter

    class BadParseAdapter(FakeAdapter):
        def parse_align(self, ctx, stream=None):  # type: ignore[no-untyped-def]
            for _ in stream or ():
                pass
            raise ParseError("boom", line_number=1)

    query, target = fasta_pair
    destination = tmp_path / "parse-failed.json"
    with pytest.raises(ParseError):
        run_alignment(
            AlignRequest(query=(query,), target=(target,), index_policy=IndexPolicy.NONE),
            adapter=BadParseAdapter(),
            manifest_path=destination,
        )
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["status"] == "failed"
    assert document["commands"], "streamed command must be audited on parse failure"
