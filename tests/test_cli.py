from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeAdapter
from gseek.adapter import AlignRequest
from gseek.cli import main
from gseek.index import IndexStore
from gseek.registry import _reset_for_tests
from gseek.runner import run_alignment


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_version(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert "gseek" in capsys.readouterr().out


def test_backends_empty(capsys) -> None:
    assert main(["backends", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_doctor_empty(capsys) -> None:
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_index_list_empty(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GSEEK_CACHE_DIR", str(tmp_path / "cache"))
    assert main(["index", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_manifest_verify(fasta_pair, tmp_path: Path, capsys) -> None:
    query, target = fasta_pair
    manifest_path = tmp_path / "run.json"
    run_alignment(
        AlignRequest(query=(query,), target=(target,)),
        adapter=FakeAdapter(),
        store=IndexStore(tmp_path / "cache"),
        manifest_path=manifest_path,
    )
    assert main(["manifest", "verify", str(manifest_path)]) == 0
    assert "all recorded file hashes match" in capsys.readouterr().out

    query.write_text(">q1\nTTTT\n", encoding="utf-8")
    assert main(["manifest", "verify", str(manifest_path)]) == 4
    assert "HASH_MISMATCH" in capsys.readouterr().out


def _bad_param_argv() -> list[str]:
    return [
        "align",
        "--backend",
        "blast",
        "--query",
        "q.fa",
        "--target",
        "t.fa",
        "--param",
        "bad",
    ]


def test_cli_error_defaults_to_json(capsys) -> None:
    assert main(_bad_param_argv()) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["schema"] == "gseek.error/1"
    assert payload["code"] == "USAGE_ERROR"


def test_cli_error_format_text(capsys) -> None:
    assert main(["--error-format", "text", *_bad_param_argv()]) == 2
    err = capsys.readouterr().err
    assert "KEY=VALUE" in err
    assert "hint:" in err or "gseek:" in err


def test_cli_error_format_json_is_explicit(capsys) -> None:
    assert main(["--error-format", "json", *_bad_param_argv()]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "USAGE_ERROR"


def test_distance_external_reference_flag(monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    import gseek.cli as cli

    captured = {}

    def fake_distance(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda **_: {"schema": "gseek.distance/1"})

    monkeypatch.setattr(cli, "distance", fake_distance)
    rc = cli.main(
        [
            "distance",
            "--backend",
            "mash",
            "--query",
            "q.fna",
            "--reference",
            "refs.msh",
            "--external-reference",
        ]
    )
    assert rc == 0
    assert captured["external_reference"] is True
    assert json.loads(capsys.readouterr().out)["schema"] == "gseek.distance/1"
