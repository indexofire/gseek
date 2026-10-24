from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from gseek.errors import DuplicateIdError, InputError, ParseError
from gseek.io import (
    atomic_write_text,
    count_records,
    detect_input_kind,
    iter_fasta,
    iter_fastq,
    merge_fasta,
    read_fasta,
)
from gseek.types import InputKind


def test_iter_fasta_preserves_description_and_case(tmp_path: Path) -> None:
    path = tmp_path / "x.fa"
    path.write_text(">seq1 description here\nacgt\n\nACGT\n>seq2\nTTTT\n", encoding="utf-8")
    records = read_fasta(path)
    assert [record.sequence_id for record in records] == ["seq1", "seq2"]
    assert records[0].description == "description here"
    assert records[0].sequence == "acgtACGT"
    assert records[1].sequence == "TTTT"


def test_iter_fasta_rejects_sequence_before_header(tmp_path: Path) -> None:
    path = tmp_path / "bad.fa"
    path.write_text("ACGT\n", encoding="utf-8")
    with pytest.raises(ParseError):
        list(iter_fasta(path))


def test_iter_fastq(tmp_path: Path) -> None:
    path = tmp_path / "x.fq"
    path.write_text("@r1 desc\nACGT\n+\nIIII\n@r2\nTTTT\n+\nJJJJ\n", encoding="utf-8")
    records = tuple(iter_fastq(path))
    assert len(records) == 2
    assert records[0].sequence_id == "r1"
    assert records[0].description == "desc"
    assert records[1].sequence == "TTTT"


def test_iter_fastq_rejects_length_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "bad.fq"
    path.write_text("@r1\nACGT\n+\nIII\n", encoding="utf-8")
    with pytest.raises(ParseError):
        list(iter_fastq(path))


def test_detect_input_kind_by_suffix_and_content(tmp_path: Path) -> None:
    fasta = tmp_path / "x.fa"
    fasta.write_text(">x\nACGT\n", encoding="utf-8")
    assert detect_input_kind(fasta) is InputKind.FASTA

    fastq = tmp_path / "x.fq"
    fastq.write_text("@x\nACGT\n+\nIIII\n", encoding="utf-8")
    assert detect_input_kind(fastq) is InputKind.FASTQ

    unknown = tmp_path / "x.data"
    unknown.write_text(">x\nACGT\n", encoding="utf-8")
    assert detect_input_kind(unknown) is InputKind.FASTA

    with pytest.raises(InputError):
        detect_input_kind(tmp_path / "missing.fa")


def test_gzip_fasta(tmp_path: Path) -> None:
    path = tmp_path / "x.fa.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(">x\nACGT\n")
    assert read_fasta(path)[0].sequence == "ACGT"
    assert count_records(path) == 1


def test_merge_fasta_is_deterministic_and_counts(tmp_path: Path) -> None:
    first = tmp_path / "a.fa"
    first.write_text(">a\nAAAA\n", encoding="utf-8")
    second = tmp_path / "b.fa"
    second.write_text(">b\nCCCC\n", encoding="utf-8")
    dest = tmp_path / "merged.fa"

    merge_fasta([second, first], dest)
    first_bytes = dest.read_bytes()
    assert first_bytes == b">a\nAAAA\n>b\nCCCC\n"
    assert count_records(dest) == 2

    merge_fasta([first, second], dest)
    assert dest.read_bytes() == first_bytes


def test_merge_fasta_duplicate_policies(tmp_path: Path) -> None:
    first = tmp_path / "a.fa"
    first.write_text(">same\nAAAA\n", encoding="utf-8")
    second = tmp_path / "b.fa"
    second.write_text(">same\nCCCC\n", encoding="utf-8")
    dest = tmp_path / "merged.fa"

    with pytest.raises(DuplicateIdError):
        merge_fasta([first, second], dest)

    merge_fasta([first, second], dest, on_duplicate="first")
    assert read_fasta(dest)[0].sequence == "AAAA"

    merge_fasta([first, second], dest, on_duplicate="prefix")
    ids = [record.sequence_id for record in read_fasta(dest)]
    assert "same" in ids
    assert any(sequence_id.startswith("b|") for sequence_id in ids)


def test_merge_fasta_expands_directory(tmp_path: Path) -> None:
    directory = tmp_path / "targets"
    directory.mkdir()
    (directory / "a.fa").write_text(">a\nAAAA\n", encoding="utf-8")
    (directory / "b.fna").write_text(">b\nCCCC\n", encoding="utf-8")
    (directory / "ignore.txt").write_text("not fasta\n", encoding="utf-8")
    dest = tmp_path / "merged.fa"
    merge_fasta([directory], dest)
    assert count_records(dest) == 2


def test_atomic_write_text(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "x.txt"
    atomic_write_text(path, "hello\n")
    assert path.read_text(encoding="utf-8") == "hello\n"
