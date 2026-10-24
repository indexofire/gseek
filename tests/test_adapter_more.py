from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import FakeAdapter
from gseek.adapter import (
    AlignRequest,
    BaseAdapter,
    DistanceRequest,
    IndexRequest,
    PlanContext,
)
from gseek.capabilities import BackendSpec, Capabilities, ToolchainProbe, ToolRequirement
from gseek.errors import ToolNotFoundError, ToolVersionError, UnsupportedFeatureError
from gseek.execution import ToolResolver
from gseek.types import IndexScope, InputKind, OutputKind


def test_align_request_validation() -> None:
    with pytest.raises(UnsupportedFeatureError):
        AlignRequest(query=(), target=(Path("t.fa"),))
    with pytest.raises(UnsupportedFeatureError):
        AlignRequest(query=(Path("q.fa"),), target=())
    with pytest.raises(UnsupportedFeatureError):
        AlignRequest(query=(Path("q.fa"),), target=(Path("t.fa"),), paired=True)
    with pytest.raises(UnsupportedFeatureError):
        AlignRequest(
            query=(Path("r1.fa"), Path("r2.fa")),
            target=(Path("t.fa"),),
            paired=False,
        )
    with pytest.raises(UnsupportedFeatureError):
        AlignRequest(query=(Path("q.fa"),), target=(Path("t.fa"),), threads=0)
    with pytest.raises(UnsupportedFeatureError):
        AlignRequest(query=(Path("q.fa"),), target=(Path("t.fa"),), timeout_s=0)


def test_distance_request_validation() -> None:
    with pytest.raises(UnsupportedFeatureError):
        DistanceRequest(query=(), reference=(Path("r.fa"),))
    with pytest.raises(UnsupportedFeatureError):
        DistanceRequest(query=(Path("q.fa"),), reference=())
    with pytest.raises(UnsupportedFeatureError):
        DistanceRequest(query=(Path("q.fa"),), reference=(Path("r.fa"),), threads=0)


def test_plan_context_path(tmp_path: Path) -> None:
    ctx = PlanContext(workspace=tmp_path)
    path = ctx.path("nested", "x.txt")
    assert path.parent.exists()
    assert path.parent == tmp_path / "nested"


def test_base_adapter_describe() -> None:
    described = FakeAdapter().describe()
    assert described["name"] == "fake"


def test_base_adapter_probe_strict_failure() -> None:
    class Missing(BaseAdapter):
        spec = BackendSpec(
            name="missing",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
            ),
            tools=(ToolRequirement("definitely-not-a-real-binary-xyz"),),
        )

    probe = Missing().probe(ToolResolver(), strict=False)
    assert probe.ok is False
    with pytest.raises(ToolNotFoundError):
        Missing().probe(ToolResolver(), strict=True)


def test_base_adapter_strict_reports_version_error_and_hint() -> None:
    class Old(BaseAdapter):
        spec = BackendSpec(
            name="old",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
            ),
            tools=(
                ToolRequirement(
                    sys.executable,
                    version_args=("-c", "print('1.0.0')"),
                    version_pattern=r"(\d+)\.(\d+)\.(\d+)",
                    min_version=(2, 0, 0),
                    hint="upgrade the fake tool",
                ),
            ),
        )

    assert Old().probe(ToolResolver(), strict=False).ok is False
    with pytest.raises(ToolVersionError) as exc_info:
        Old().probe(ToolResolver(), strict=True)
    assert exc_info.value.hint == "upgrade the fake tool"


def test_check_align_request_rejects_bad_preset() -> None:
    class PresetAdapter(FakeAdapter):
        spec = BackendSpec(
            name="preset",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
                presets=("asm5",),
            ),
            tools=(ToolRequirement("x"),),
        )

    request = AlignRequest(
        query=(Path("q.fa"),),
        target=(Path("t.fa"),),
        query_kind=InputKind.FASTA,
        params={"preset": "asm20"},
    )
    with pytest.raises(UnsupportedFeatureError):
        PresetAdapter().check_align_request(request)


def test_base_adapter_plan_index_target_requires_override() -> None:
    class Indexed(BaseAdapter):
        spec = BackendSpec(
            name="indexed",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
                index_scope=IndexScope.TARGET,
            ),
            tools=(ToolRequirement("x"),),
        )

    with pytest.raises(NotImplementedError):
        Indexed.plan_index(Indexed(), IndexRequest(target=()), PlanContext(workspace=Path(".")))


def test_toolchain_probe_lookup() -> None:
    probe = ToolchainProbe(backend="fake", tools=(), ok=True)
    assert probe.installed
    assert probe.tool("missing") is None
