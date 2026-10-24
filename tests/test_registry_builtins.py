from __future__ import annotations

import json

import pytest

import gseek
from gseek.cli import main
from gseek.registry import _reset_for_tests
from gseek.types import InputKind, Molecule, OutputKind


@pytest.fixture(autouse=True)
def builtin_registry():
    _reset_for_tests(load_builtins=True)
    yield
    _reset_for_tests()


def test_builtin_backends_are_registered() -> None:
    names = {spec.name for spec in gseek.backends()}
    assert names == {
        "blast",
        "minimap2",
        "kma",
        "mummer",
        "bwa",
        "mash",
        "sourmash",
        "skani",
    }

    assert gseek.get_backend("blastn").spec.name == "blast"
    assert gseek.get_backend("mm2").spec.name == "minimap2"
    assert gseek.get_backend("nucmer").spec.name == "mummer"


def test_capability_routing() -> None:
    fasta_alignment = {
        spec.name for spec in gseek.find(input=InputKind.FASTA, output=OutputKind.ALIGNMENT)
    }
    assert fasta_alignment == {"blast", "minimap2", "kma", "mummer", "bwa"}

    fastq_alignment = {
        spec.name for spec in gseek.find(input=InputKind.FASTQ, output=OutputKind.ALIGNMENT)
    }
    assert fastq_alignment == {"kma", "minimap2", "bwa"}

    paired = {
        spec.name
        for spec in gseek.find(
            input=InputKind.FASTQ,
            output=OutputKind.ALIGNMENT,
            paired=True,
        )
    }
    assert paired == {"kma", "minimap2", "bwa"}

    protein_alignment = {
        spec.name
        for spec in gseek.find(
            input=InputKind.FASTA,
            output=OutputKind.ALIGNMENT,
            molecule=Molecule.PROTEIN,
        )
    }
    assert protein_alignment == {"blast"}

    fasta_distance = {
        spec.name for spec in gseek.find(input=InputKind.FASTA, output=OutputKind.DISTANCE)
    }
    assert fasta_distance == {"mash", "sourmash", "skani"}

    assert (
        gseek.find(
            input=InputKind.FASTQ,
            output=OutputKind.DISTANCE,
            paired=True,
        )
        == ()
    )


def test_cli_lists_builtin_backends(capsys) -> None:
    assert main(["backends", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in payload} == {
        "blast",
        "minimap2",
        "kma",
        "mummer",
        "bwa",
        "mash",
        "sourmash",
        "skani",
    }
    assert all(item["family"] for item in payload)
