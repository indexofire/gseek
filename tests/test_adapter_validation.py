from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeAdapter
from gseek.adapter import AlignRequest, BaseAdapter, IndexRequest, PlanContext, request_molecule
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement, supports
from gseek.errors import ConfigError, UnsupportedFeatureError
from gseek.types import IndexScope, InputKind, Molecule, OutputKind


def test_check_align_request_rejects_paired_fasta() -> None:
    adapter = FakeAdapter()
    request = AlignRequest(
        query=(Path("r1.fa"), Path("r2.fa")),
        target=(Path("t.fa"),),
        query_kind=InputKind.FASTA,
        paired=True,
    )
    with pytest.raises(UnsupportedFeatureError):
        adapter.check_align_request(request)


def test_capabilities_require_inputs_and_outputs() -> None:
    with pytest.raises(ConfigError):
        Capabilities(inputs=frozenset(), outputs=frozenset({OutputKind.ALIGNMENT}))
    with pytest.raises(ConfigError):
        Capabilities(inputs=frozenset({InputKind.FASTA}), outputs=frozenset())


def test_backend_spec_validation() -> None:
    capabilities = Capabilities(
        inputs=frozenset({InputKind.FASTA}),
        outputs=frozenset({OutputKind.ALIGNMENT}),
    )
    with pytest.raises(ConfigError):
        BackendSpec(name="bad name", family="x", summary="", capabilities=capabilities, tools=())
    with pytest.raises(ConfigError):
        BackendSpec(
            name="ok",
            family="x",
            summary="",
            capabilities=capabilities,
            tools=(),
        )
    with pytest.raises(ConfigError):
        BackendSpec(
            name="ok",
            family="x",
            summary="",
            capabilities=capabilities,
            tools=(ToolRequirement("a"), ToolRequirement("a")),
        )


def test_supports() -> None:
    spec = FakeAdapter.spec
    assert supports(spec, input_kind=InputKind.FASTA, output_kind=OutputKind.ALIGNMENT)
    assert not supports(spec, input_kind=InputKind.FASTQ)
    assert not supports(spec, paired=True)
    assert supports(spec, molecule=Molecule.DNA)


def test_base_adapter_plan_index_for_no_index_backend() -> None:
    class NoIndex(BaseAdapter):
        spec = BackendSpec(
            name="no-index",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
                index_scope=IndexScope.NONE,
            ),
            tools=(ToolRequirement("x"),),
        )

    adapter = NoIndex()
    assert adapter.plan_index(IndexRequest(target=()), PlanContext(workspace=Path("."))) is None


def test_request_molecule() -> None:
    assert request_molecule({}) is Molecule.DNA
    assert request_molecule({"molecule": "protein"}) is Molecule.PROTEIN
    with pytest.raises(UnsupportedFeatureError):
        request_molecule({"molecule": "lipid"})
