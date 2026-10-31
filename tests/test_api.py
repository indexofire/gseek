from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeAdapter, FakeDistanceAdapter
from gseek.api import align, backends, distance, find, verify
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ConfigError
from gseek.index import IndexStore
from gseek.provenance import input_ref
from gseek.registry import _reset_for_tests, register_adapter
from gseek.types import IndexScope, InputKind, OutputKind


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_align_api_with_adapter(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    result = align(
        query=query,
        target=target,
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        preset="asm5",
    )
    assert result.params["preset"] == "asm5"
    assert len(result.records) == 1


def test_align_api_with_registered_backend(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeAdapter)
    query, target = fasta_pair
    result = align(
        backend="fake",
        query=query,
        target=target,
        store=IndexStore(tmp_path / "cache"),
    )
    assert result.backend == "fake"
    auto = align(
        query=query,
        target=target,
        store=IndexStore(tmp_path / "cache"),
    )
    assert auto.backend == "fake"


def test_align_api_can_skip_canonical_sort(fasta_pair, tmp_path: Path) -> None:
    """Given sort_records=False, When align is called, Then the result keeps
    parser order and records the opt-out."""
    query, target = fasta_pair
    result = align(
        query=query,
        target=target,
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        sort_records=False,
    )
    assert result.sort_records is False


def test_align_api_accepts_precomputed_input_refs(fasta_pair, tmp_path: Path) -> None:
    """Given refs already hashed by the consumer, When align runs, Then the
    result reuses them instead of re-scanning the target/query files. This is
    the API hook for callers that run many queries against one database."""
    query, target = fasta_pair
    query_ref = input_ref(query, kind=InputKind.FASTA, compute_records=False)
    target_ref = input_ref(target, kind=InputKind.FASTA, compute_records=False)
    result = align(
        query=query,
        target=target,
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        query_refs=(query_ref,),
        target_refs=(target_ref,),
    )
    assert result.inputs == (query_ref,)
    assert result.targets == (target_ref,)


def test_align_api_rejects_ref_count_mismatch(fasta_pair) -> None:
    """Given a ref tuple that does not match the request's path count, When
    align runs, Then fail before probing or indexing."""
    query, target = fasta_pair
    with pytest.raises(ConfigError, match="target_refs"):
        align(
            query=query,
            target=target,
            adapter=FakeAdapter(),
            target_refs=(),
        )


def test_align_api_rejects_backend_and_adapter(fasta_pair) -> None:
    query, target = fasta_pair
    with pytest.raises(ConfigError):
        align(query=query, target=target, backend="fake", adapter=FakeAdapter())


def test_auto_selection_requires_unambiguous_backend(fasta_pair, tmp_path: Path) -> None:
    class SecondFake(FakeAdapter):
        spec = BackendSpec(
            name="second-fake",
            family="fake",
            summary="second",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
                index_scope=IndexScope.NONE,
            ),
            tools=(ToolRequirement("second-tool"),),
        )

    register_adapter(FakeAdapter)
    register_adapter(SecondFake)
    query, target = fasta_pair
    with pytest.raises(ConfigError):
        align(query=query, target=target, store=IndexStore(tmp_path / "cache"))


def test_distance_api(fasta_pair) -> None:
    query, reference = fasta_pair
    result = distance(
        query=query,
        reference=reference,
        adapter=FakeDistanceAdapter(),
    )
    assert result.backend == "fake-distance"
    assert result.records[0].reference_id == "r1"


def test_backends_and_find() -> None:
    register_adapter(FakeAdapter)
    register_adapter(FakeDistanceAdapter)
    assert {spec.name for spec in backends()} == {"fake", "fake-distance"}
    assert [spec.name for spec in find(input="fasta", output="alignment")] == ["fake"]
    assert [spec.name for spec in find(input="fasta", output="distance")] == ["fake-distance"]


def test_verify_api(fasta_pair, tmp_path: Path) -> None:
    query, target = fasta_pair
    manifest_path = tmp_path / "run.json"
    align(
        query=query,
        target=target,
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        manifest=manifest_path,
    )
    assert verify(manifest_path) == ()


def test_preset_conflict_is_rejected(fasta_pair, tmp_path) -> None:
    # preset 与 params["preset"] 同传但不同值时静默覆盖是配置错误
    from gseek.errors import ConfigError

    query, target = fasta_pair
    with pytest.raises(ConfigError, match="preset"):
        align(
            query=query,
            target=target,
            backend="blast",
            params={"preset": "sr"},
            preset="asm5",
            index="none",
            work_dir=tmp_path,
        )


def test_invalid_index_policy_is_typed_error(fasta_pair, tmp_path) -> None:
    from gseek.errors import ConfigError

    query, target = fasta_pair
    with pytest.raises(ConfigError, match="index"):
        align(query=query, target=target, backend="blast", index="whenever", work_dir=tmp_path)


def test_invalid_query_kind_is_typed_error(fasta_pair, tmp_path) -> None:
    query, target = fasta_pair
    with pytest.raises(ConfigError, match="query kind"):
        align(query=query, target=target, backend="blast", query_kind="solid", work_dir=tmp_path)
