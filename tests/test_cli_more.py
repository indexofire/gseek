from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeAdapter, FakeDistanceAdapter
from gseek.adapter import BaseAdapter
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.cli import main
from gseek.index import IndexStore
from gseek.registry import _reset_for_tests, register_adapter
from gseek.types import InputKind, OutputKind


class MissingAdapter(BaseAdapter):
    spec = BackendSpec(
        name="missing",
        family="fake",
        summary="missing tools",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
        ),
        tools=(ToolRequirement("definitely-not-a-real-binary-xyz"),),
    )


@pytest.fixture(autouse=True)
def clean_registry(tmp_path: Path, monkeypatch):
    _reset_for_tests()
    monkeypatch.setenv("GSEEK_CACHE_DIR", str(tmp_path / "cache"))
    yield
    _reset_for_tests()


def test_backends_text(capsys) -> None:
    register_adapter(FakeAdapter)
    assert main(["backends"]) == 0
    assert "fake" in capsys.readouterr().out


def test_doctor_ok_and_missing(capsys) -> None:
    register_adapter(FakeAdapter)
    assert main(["doctor"]) == 0
    assert "fake" in capsys.readouterr().out

    register_adapter(MissingAdapter)
    assert main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    missing = next(item for item in report if item["backend"] == "missing")
    assert missing["ok"] is False


def test_index_list_and_gc(tmp_path: Path, capsys) -> None:
    store = IndexStore()
    key = "a" * 64
    staging = store.new_staging("fake", key)
    (staging / "index.txt").write_text("x", encoding="utf-8")
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
    assert main(["index", "list", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1

    assert main(["index", "gc", "--apply", "--max-bytes", "0", "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert len(removed) == 1
    assert not store.list_indexes()


def test_manifest_verify_json(fasta_pair, tmp_path: Path, capsys) -> None:
    query, target = fasta_pair
    manifest = tmp_path / "run.json"
    from gseek.api import align

    align(query=query, target=target, adapter=FakeAdapter(), manifest=manifest)
    assert main(["manifest", "verify", str(manifest), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"] == []
    assert main(["verify", str(manifest), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == []


def test_align_cli(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeAdapter)
    query, target = fasta_pair
    output = tmp_path / "out.json"
    code = main(
        [
            "align",
            "--backend",
            "fake",
            "--query",
            str(query),
            "--target",
            str(target),
            "--preset",
            "asm5",
            "--param",
            "evalue=1e-5",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "gseek.alignment/1"
    assert len(payload["records"]) == 1


def test_distance_cli(fasta_pair, tmp_path: Path) -> None:
    register_adapter(FakeDistanceAdapter)
    query, reference = fasta_pair
    output = tmp_path / "dist.json"
    code = main(
        [
            "distance",
            "--backend",
            "fake-distance",
            "--query",
            str(query),
            "--reference",
            str(reference),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "gseek.distance/1"


def test_distance_rejects_preset_option() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "distance",
                "--backend",
                "fake-distance",
                "--query",
                "q.fa",
                "--reference",
                "r.fa",
                "--preset",
                "asm5",
            ]
        )
    assert exc_info.value.code == 2


def test_invalid_param_and_backend(fasta_pair, capsys) -> None:
    query, target = fasta_pair
    code = main(
        [
            "align",
            "--backend",
            "fake",
            "--query",
            str(query),
            "--target",
            str(target),
            "--param",
            "bad",
        ]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().err)["code"] == "USAGE_ERROR"

    code = main(["align", "--backend", "unknown", "--query", str(query), "--target", str(target)])
    assert code == 2
    assert json.loads(capsys.readouterr().err)["code"] == "CONFIG_ERROR"


def test_python_dash_m_entry_point() -> None:
    # `python -m gseek` must work (covers gseek/__main__.py).

    completed = subprocess.run(
        [sys.executable, "-m", "gseek", "--version"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        timeout=60,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip().startswith("gseek ")


def test_parse_params_rejects_non_finite_numbers() -> None:
    # NaN/Infinity would crash canonical JSON mid-run with a bare ValueError.
    from gseek.cli import _parse_params
    from gseek.errors import UsageError

    with pytest.raises(UsageError, match="finite"):
        _parse_params(["x=NaN"])
    with pytest.raises(UsageError, match="finite"):
        _parse_params(["y=Infinity"])
    assert _parse_params(["z=1e5", "w=text"]) == {"z": 1e5, "w": "text"}
