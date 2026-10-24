from __future__ import annotations

import pytest

from conftest import FakeAdapter
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ConfigError
from gseek.registry import (
    _reset_for_tests,
    backend_specs,
    find_backends,
    get_backend,
    iter_adapters,
    register_adapter,
    unregister_adapter,
)
from gseek.types import InputKind, OutputKind


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_unknown_backend() -> None:
    with pytest.raises(ConfigError):
        get_backend("nope")


def test_find_backends_no_match() -> None:
    register_adapter(FakeAdapter)
    assert find_backends(input=InputKind.FASTQ) == ()


def test_iter_adapters() -> None:
    register_adapter(FakeAdapter)
    assert [adapter.spec.name for adapter in iter_adapters()] == ["fake"]


def test_invalid_adapter_registration() -> None:
    class Invalid:
        pass

    with pytest.raises(ConfigError):
        register_adapter(Invalid)  # type: ignore[arg-type]


def test_constructor_error_becomes_config_error() -> None:
    class NeedsArg(FakeAdapter):
        spec = BackendSpec(
            name="needs-arg",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
            ),
            tools=(ToolRequirement("x"),),
        )

        def __init__(self, required: str) -> None:
            self.required = required

    register_adapter(NeedsArg)
    with pytest.raises(ConfigError):
        get_backend("needs-arg")


def test_unregister_unknown() -> None:
    with pytest.raises(ConfigError):
        unregister_adapter("nope")


def test_backend_specs_deduplicates_aliases() -> None:
    class Aliased(FakeAdapter):
        spec = BackendSpec(
            name="aliased",
            family="fake",
            summary="",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
            ),
            tools=(ToolRequirement("x"),),
            aliases=("alias",),
        )

    register_adapter(Aliased)
    assert len(backend_specs()) == 1
