"""TDD contract for the gseek rename.

These tests were intentionally written before the production rename. They
failed against the old `gcore` package, then passed once the package, schemas,
CLI, and environment variables were consistently renamed to `gseek`.
"""

from __future__ import annotations

from pathlib import Path


def test_package_imports_as_gseek() -> None:
    import gseek

    assert gseek.__version__ == "0.5.0"


def test_schema_namespaces_are_gseek() -> None:
    from gseek.types import (
        ALIGNMENT_SCHEMA,
        DISTANCE_SCHEMA,
        INDEX_SCHEMA,
        RUN_SCHEMA,
    )

    assert ALIGNMENT_SCHEMA == "gseek.alignment/1"
    assert DISTANCE_SCHEMA == "gseek.distance/1"
    assert RUN_SCHEMA == "gseek.run/1"
    assert INDEX_SCHEMA == "gseek.index/1"


def test_error_envelope_is_gseek() -> None:
    from gseek.errors import ERROR_SCHEMA, InputError

    assert ERROR_SCHEMA == "gseek.error/1"
    assert InputError("bad").to_dict()["schema"] == "gseek.error/1"


def test_cache_uses_gseek_environment(monkeypatch, tmp_path: Path) -> None:
    from gseek.index import default_cache_root

    monkeypatch.setenv("GSEEK_CACHE_DIR", str(tmp_path / "cache"))
    assert default_cache_root() == tmp_path / "cache"


def test_cli_is_gseek(capsys) -> None:
    from gseek.cli import main

    assert main(["version"]) == 0
    assert capsys.readouterr().out.startswith("gseek ")


def test_public_models_are_importable_from_gseek() -> None:
    from gseek import AlignmentRecord, AlignmentResult, RunManifest

    assert AlignmentRecord.__name__ == "AlignmentRecord"
    assert AlignmentResult.__name__ == "AlignmentResult"
    assert RunManifest.__name__ == "RunManifest"


def test_package_declares_pep561_typing_marker() -> None:
    import gseek

    package_dir = Path(gseek.__file__).resolve().parent
    assert (package_dir / "py.typed").is_file()


def test_pyproject_distribution_identity() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == "gseek"
    assert project["requires-python"] == ">=3.12"
    assert project["scripts"]["gseek"] == "gseek.cli:main"
    assert project["dependencies"] == []
