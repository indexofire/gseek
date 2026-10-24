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
    register_adapter,
    registered_names,
    unregister_adapter,
)
from gseek.types import IndexScope, InputKind, OutputKind


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_register_get_and_find() -> None:
    register_adapter(FakeAdapter)
    assert "fake" in registered_names()
    backend = get_backend("fake")
    assert isinstance(backend, FakeAdapter)
    specs = find_backends(input=InputKind.FASTA, output=OutputKind.ALIGNMENT)
    assert [spec.name for spec in specs] == ["fake"]
    assert backend_specs()[0].name == "fake"


def test_duplicate_registration_conflicts() -> None:
    register_adapter(FakeAdapter)

    class OtherFake(FakeAdapter):
        spec = BackendSpec(
            name="fake",
            family="fake",
            summary="other",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
                index_scope=IndexScope.NONE,
            ),
            tools=(ToolRequirement("other-tool"),),
        )

    with pytest.raises(ConfigError):
        register_adapter(OtherFake)


def test_unregister_removes_aliases() -> None:
    class Aliased(FakeAdapter):
        spec = BackendSpec(
            name="aliased",
            family="fake",
            summary="aliased",
            capabilities=Capabilities(
                inputs=frozenset({InputKind.FASTA}),
                outputs=frozenset({OutputKind.ALIGNMENT}),
                index_scope=IndexScope.NONE,
            ),
            tools=(ToolRequirement("other-tool"),),
            aliases=("alias1", "alias2"),
        )

    register_adapter(Aliased)
    assert {"aliased", "alias1", "alias2"} <= set(registered_names())
    unregister_adapter("aliased")
    assert "aliased" not in registered_names()
    assert "alias1" not in registered_names()
