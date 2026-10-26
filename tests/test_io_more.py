from __future__ import annotations

import bz2
from pathlib import Path

import pytest

from gseek.errors import InputError, ParseError
from gseek.io import (
    atomic_write_bytes,
    count_records,
    detect_input_kind,
    expand_fasta_paths,
    is_compressed,
    iter_fastq,
    merge_fasta,
    strip_compression_suffix,
)
from gseek.types import InputKind


def test_bz2_input(tmp_path: Path) -> None:
    path = tmp_path / "x.fa.bz2"
    with bz2.open(path, "wt", encoding="utf-8") as handle:
        handle.write(">x\nACGT\n")
    assert detect_input_kind(path) is InputKind.FASTA
    assert count_records(path) == 1


def test_compression_helpers() -> None:
    assert is_compressed(Path("x.fa.gz"))
    assert strip_compression_suffix(Path("x.fa.gz")) == Path("x.fa")
    assert strip_compression_suffix(Path("x.fa")) == Path("x.fa")


def test_detect_input_kind_errors(tmp_path: Path) -> None:
    empty = tmp_path / "empty.data"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(InputError):
        detect_input_kind(empty)

    unknown = tmp_path / "unknown.data"
    unknown.write_text("ACGT\n", encoding="utf-8")
    with pytest.raises(InputError):
        detect_input_kind(unknown)
    with pytest.raises(InputError):
        detect_input_kind(unknown, probe_content=False)


def test_fastq_errors(tmp_path: Path) -> None:
    empty_header = tmp_path / "a.fq"
    empty_header.write_text("@\nACGT\n+\nIIII\n", encoding="utf-8")
    with pytest.raises(ParseError):
        list(iter_fastq(empty_header))

    truncated = tmp_path / "b.fq"
    truncated.write_text("@r\nACGT\n+\n", encoding="utf-8")
    with pytest.raises(ParseError):
        list(iter_fastq(truncated))

    bad_plus = tmp_path / "c.fq"
    bad_plus.write_text("@r\nACGT\n-\nIIII\n", encoding="utf-8")
    with pytest.raises(ParseError):
        list(iter_fastq(bad_plus))


def test_count_fastq(tmp_path: Path) -> None:
    path = tmp_path / "x.fq"
    path.write_text("@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nJJJJ\n", encoding="utf-8")
    assert count_records(path) == 2


def test_count_fastq_rejects_truncated_record(tmp_path: Path) -> None:
    path = tmp_path / "x.fq"
    path.write_text("@r1\nACGT\n+\n", encoding="utf-8")
    with pytest.raises(ParseError):
        count_records(path)


def test_merge_fasta_rejects_unknown_duplicate_policy(tmp_path: Path) -> None:
    source = tmp_path / "x.fa"
    source.write_text(">x\nACGT\n", encoding="utf-8")
    with pytest.raises(InputError):
        merge_fasta([source], tmp_path / "merged.fa", on_duplicate="keep")  # type: ignore[arg-type]


def test_merge_fasta_rejects_invalid_width(tmp_path: Path) -> None:
    source = tmp_path / "x.fa"
    source.write_text(">x\nACGT\n", encoding="utf-8")
    with pytest.raises(InputError, match="width"):
        merge_fasta([source], tmp_path / "merged.fa", width=0)


def test_merge_fasta_width_and_uppercase(tmp_path: Path) -> None:
    source = tmp_path / "x.fa"
    source.write_text(">x\nacgtacgt\n", encoding="utf-8")
    dest = tmp_path / "merged.fa"
    merge_fasta([source], dest, width=4, uppercase=True)
    assert dest.read_text(encoding="utf-8") == ">x\nACGT\nACGT\n"


def test_merge_fasta_errors(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        merge_fasta([], tmp_path / "out.fa")
    with pytest.raises(InputError):
        merge_fasta([tmp_path / "missing.fa"], tmp_path / "out.fa")

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(InputError):
        expand_fasta_paths([empty_dir])


def test_atomic_write_bytes(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    atomic_write_bytes(path, b"\x00\x01")
    assert path.read_bytes() == b"\x00\x01"


def test_truncated_gzip_raises_typed_parse_error(tmp_path: Path) -> None:
    # A truncated .gz must surface as ParseError with the path, not a bare
    # EOFError from gzip internals.
    import gzip as _gzip

    whole = tmp_path / "ok.fa.gz"
    with _gzip.open(whole, "wb") as handle:
        handle.write(b">q1\nACGTACGT\n" * 100)
    truncated = tmp_path / "bad.fa.gz"
    truncated.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])

    from gseek.io import iter_fasta

    with pytest.raises(ParseError, match=r"truncat|UTF-8|corrupt"):
        list(iter_fasta(truncated))


def test_expand_fasta_paths_sort_param_is_honored(tmp_path: Path) -> None:
    from gseek.io import expand_fasta_paths

    b = tmp_path / "b.fa"
    b.write_text(">b\nA\n", encoding="utf-8")
    a = tmp_path / "a.fa"
    a.write_text(">a\nA\n", encoding="utf-8")
    assert expand_fasta_paths([b, a]) == (a, b)
    assert expand_fasta_paths([b, a], sort=False) == (b, a), "user order must survive"


def test_atomic_write_permissions_follow_umask(tmp_path: Path) -> None:
    import os

    from gseek.io import atomic_write_text

    destination = tmp_path / "out.json"
    atomic_write_text(destination, "{}\n")
    mode = destination.stat().st_mode & 0o777
    0o666 & ~os.umask(0o022) if False else None  # computed below
    import os as _os

    mask = _os.umask(0)
    _os.umask(mask)
    assert mode == 0o666 & ~mask, f"got {oct(mode)}, expected user-umask default"


def test_merge_fasta_prefix_survives_same_stem_sources(tmp_path: Path) -> None:
    # same-stem sources in different dirs must not spuriously collide under prefix mode
    from gseek.io import iter_fasta

    d1 = tmp_path / "d1"
    d1.mkdir()
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d1 / "ref.fa").write_text(">x\nACGT\n", encoding="utf-8")
    (d2 / "ref.fa").write_text(">x\nTTTT\n", encoding="utf-8")
    merged = merge_fasta(
        [d1 / "ref.fa", d2 / "ref.fa"], tmp_path / "merged.fa", on_duplicate="prefix"
    )
    ids = [record.sequence_id for record in iter_fasta(merged)]
    assert len(ids) == 2 and len(set(ids)) == 2, ids
