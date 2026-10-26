from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeAdapter, FakeDistanceAdapter
from gseek.api import align, backends, distance, find, verify
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ConfigError
from gseek.index import IndexStore
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
