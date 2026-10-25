"""Dependency-free FASTA/FASTQ I/O and deterministic target materialization."""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from gseek.errors import DuplicateIdError, InputError, ParseError
from gseek.types import InputKind

_FASTA_SUFFIXES = frozenset({".fa", ".fasta", ".fna", ".ffn", ".frn", ".fas", ".fsa", ".faa"})
_FASTQ_SUFFIXES = frozenset({".fq", ".fastq"})
_COMPRESSION_SUFFIXES = frozenset({".gz", ".bz2", ".xz"})
_DuplicatePolicy = Literal["error", "first", "prefix", "allow"]


@dataclass(frozen=True, slots=True)
class SequenceRecord:
    """One FASTA/FASTQ record.

    ``sequence_id`` is the first whitespace-delimited token of the header;
    ``description`` is the remainder (possibly empty).
    """

    sequence_id: str
    sequence: str
    description: str = ""

    def __post_init__(self) -> None:
        if not self.sequence_id:
            raise InputError("sequence ID must not be empty")
        if any(char.isspace() for char in self.sequence_id):
            raise InputError(f"sequence ID must not contain whitespace: {self.sequence_id!r}")

    @property
    def header(self) -> str:
        return f"{self.sequence_id} {self.description}".strip()


def strip_compression_suffix(path: Path) -> Path:
    """Return *path* without a trailing ``.gz``/``.bz2``/``.xz`` suffix."""
    if path.suffix.lower() in _COMPRESSION_SUFFIXES:
        return path.with_suffix("")
    return path


def is_compressed(path: Path) -> bool:
    return path.suffix.lower() in _COMPRESSION_SUFFIXES


@contextmanager
def open_text(path: Path, *, encoding: str = "utf-8") -> Iterator[IO[str]]:
    """Open a possibly compressed text file for reading."""
    suffix = path.suffix.lower()
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding=encoding) as handle:
            yield handle
    elif suffix == ".bz2":
        with bz2.open(path, "rt", encoding=encoding) as handle:
            yield handle
    elif suffix == ".xz":
        with lzma.open(path, "rt", encoding=encoding) as handle:
            yield handle
    else:
        with path.open("rt", encoding=encoding) as handle:
            yield handle


def detect_input_kind(path: Path, *, probe_content: bool = True) -> InputKind:
    """Detect FASTA/FASTQ by suffix, falling back to the first content byte.

    Raises :class:`InputError` when the file is missing, empty, or neither
    format.
    """
    if not path.is_file():
        raise InputError(f"input file not found or unreadable: {path}", context={"path": str(path)})

    base = strip_compression_suffix(path)
    suffix = base.suffix.lower()
    if suffix in _FASTA_SUFFIXES:
        return InputKind.FASTA
    if suffix in _FASTQ_SUFFIXES:
        return InputKind.FASTQ
    if not probe_content:
        raise InputError(
            f"cannot determine sequence format from suffix: {path}",
            hint="use .fa/.fasta/.fna for FASTA or .fq/.fastq for FASTQ",
            context={"path": str(path)},
        )

    try:
        with open_text(path) as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith(">"):
                    return InputKind.FASTA
                if stripped.startswith("@"):
                    return InputKind.FASTQ
                break
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(
            f"cannot read input file {path}: {exc}", context={"path": str(path)}
        ) from exc

    raise InputError(
        f"cannot determine sequence format (expected '>' or '@'): {path}",
        context={"path": str(path)},
    )


def iter_fasta(path: Path) -> Iterator[SequenceRecord]:
    """Yield FASTA records, preserving sequence case and order."""
    current_id: str | None = None
    current_description = ""
    chunks: list[str] = []

    with open_text(path) as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            if line.startswith(">"):
                if current_id is not None:
                    yield SequenceRecord(current_id, "".join(chunks), current_description)
                header = line[1:].strip()
                if not header:
                    raise ParseError(
                        f"empty FASTA header in {path}",
                        path=str(path),
                        line_number=line_number,
                    )
                parts = header.split(maxsplit=1)
                current_id = parts[0]
                current_description = parts[1].strip() if len(parts) > 1 else ""
                chunks = []
                continue
            if current_id is None:
                raise ParseError(
                    f"FASTA sequence before first header in {path}",
                    path=str(path),
                    line_number=line_number,
                    raw_line=line,
                )
            chunks.append(line.strip())

    if current_id is not None:
        yield SequenceRecord(current_id, "".join(chunks), current_description)


def read_fasta(path: Path) -> tuple[SequenceRecord, ...]:
    """Materialize all FASTA records from *path*."""
    return tuple(iter_fasta(path))


def iter_fastq(path: Path) -> Iterator[SequenceRecord]:
    """Yield four-line FASTQ records.

    Multi-line FASTQ is intentionally not supported: every supported aligner
    consumes standard four-line records, and strict parsing catches corrupt
    files early. Gzip/bzip2/xz inputs are transparently decompressed.
    """
    with open_text(path) as handle:
        line_number = 0
        while True:
            header = handle.readline()
            if not header:
                return
            line_number += 1
            header = header.rstrip("\r\n")
            if not header:
                continue
            if not header.startswith("@"):
                raise ParseError(
                    f"FASTQ record does not start with '@' in {path}",
                    path=str(path),
                    line_number=line_number,
                    raw_line=header,
                )
            sequence_line = handle.readline()
            plus_line = handle.readline()
            quality_line = handle.readline()
            line_number += 3
            if not sequence_line or not plus_line or not quality_line:
                raise ParseError(
                    f"truncated FASTQ record in {path}",
                    path=str(path),
                    line_number=line_number,
                )
            sequence = sequence_line.rstrip("\r\n")
            plus = plus_line.rstrip("\r\n")
            quality = quality_line.rstrip("\r\n")
            if not plus.startswith("+"):
                raise ParseError(
                    f"FASTQ plus line missing in {path}",
                    path=str(path),
                    line_number=line_number - 1,
                    raw_line=plus,
                )
            if len(sequence) != len(quality):
                raise ParseError(
                    f"FASTQ sequence/quality length mismatch in {path} "
                    f"({len(sequence)} != {len(quality)})",
                    path=str(path),
                    line_number=line_number,
                )
            header_body = header[1:].strip()
            if not header_body:
                raise ParseError(
                    f"empty FASTQ header in {path}",
                    path=str(path),
                    line_number=line_number - 3,
                )
            parts = header_body.split(maxsplit=1)
            yield SequenceRecord(
                sequence_id=parts[0],
                sequence=sequence,
                description=parts[1].strip() if len(parts) > 1 else "",
            )


def count_records(path: Path, kind: InputKind | None = None) -> int:
    """Count FASTA headers or validate/count FASTQ records.

    FASTQ counting parses four-line records, so truncated or malformed input
    fails early with :class:`ParseError` instead of producing a misleading
    manifest record count.
    """
    kind = kind or detect_input_kind(path)
    if kind is InputKind.FASTA:
        count = 0
        with open_text(path) as handle:
            for line in handle:
                if line.startswith(">"):
                    count += 1
        return count
    return sum(1 for _ in iter_fastq(path))


def expand_fasta_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Expand files/directories into a deterministic FASTA file list."""
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                child
                for child in path.iterdir()
                if child.is_file()
                and strip_compression_suffix(child).suffix.lower() in _FASTA_SUFFIXES
            )
            if not candidates:
                raise InputError(
                    f"no FASTA files found in directory: {path}", context={"path": str(path)}
                )
            expanded.extend(candidates)
        elif path.is_file():
            expanded.append(path)
        else:
            raise InputError(f"input path not found: {path}", context={"path": str(path)})
    if not expanded:
        raise InputError("no target FASTA files supplied")
    return tuple(sorted(expanded, key=lambda item: str(item.resolve())))


def _write_fasta_record(
    handle: IO[str],
    record: SequenceRecord,
    *,
    width: int,
    uppercase: bool,
) -> None:
    handle.write(f">{record.header}\n")
    sequence = record.sequence.upper() if uppercase else record.sequence
    for start in range(0, len(sequence), width):
        handle.write(sequence[start : start + width] + "\n")
    if not sequence:
        handle.write("\n")


def _atomic_text_writer(path: Path) -> tuple[IO[str], Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
    return handle, Path(temporary_name)


def merge_fasta(
    paths: Sequence[Path],
    dest: Path,
    *,
    width: int = 60,
    uppercase: bool = False,
    on_duplicate: _DuplicatePolicy = "error",
    sort_files: bool = True,
) -> Path:
    """Merge FASTA files into a deterministic, atomically published *dest*.

    IDs are preserved verbatim. Duplicate IDs across files are an error by
    default; ``on_duplicate="first"`` keeps the first occurrence,
    ``"prefix"`` rewrites later collisions as ``<file-stem>|<id>``, and
    ``"allow"`` keeps every record (BLAST accepts duplicate subject IDs).

    This is the canonical target materialization used before indexing. Index
    keys hash the original target files; merging is deterministic for a given
    input set, and adapter/gseek versions are part of the key.
    """
    if width < 1:
        raise ValueError("width must be >= 1")
    if on_duplicate not in ("error", "first", "prefix", "allow"):
        raise ValueError(f"unknown on_duplicate policy: {on_duplicate!r}")
    expanded = expand_fasta_paths(paths)
    if sort_files:
        expanded = tuple(sorted(expanded, key=lambda item: str(item.resolve())))

    dest.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = _atomic_text_writer(dest)
    seen: dict[str, Path] = {}
    try:
        for source in expanded:
            for record in iter_fasta(source):
                sequence_id = record.sequence_id
                if sequence_id in seen:
                    if on_duplicate == "error":
                        raise DuplicateIdError(
                            f"duplicate sequence ID {sequence_id!r} in {source} "
                            f"(first seen in {seen[sequence_id]})",
                            context={
                                "sequence_id": sequence_id,
                                "first": str(seen[sequence_id]),
                                "duplicate": str(source),
                            },
                        )
                    if on_duplicate == "first":
                        continue
                    if on_duplicate == "allow":
                        seen.setdefault(sequence_id, source)
                        _write_fasta_record(
                            handle,
                            SequenceRecord(sequence_id, record.sequence, record.description),
                            width=width,
                            uppercase=uppercase,
                        )
                        continue
                    sequence_id = f"{source.stem}|{sequence_id}"
                    if sequence_id in seen:
                        raise DuplicateIdError(
                            f"duplicate sequence ID after prefixing: {sequence_id!r}",
                            context={"sequence_id": sequence_id},
                        )
                seen[sequence_id] = source
                _write_fasta_record(
                    handle,
                    SequenceRecord(sequence_id, record.sequence, record.description),
                    width=width,
                    uppercase=uppercase,
                )
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        handle.close()
        os.replace(temporary, dest)
    return dest


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write *text* to *path* (same-directory temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "SequenceRecord",
    "atomic_write_bytes",
    "atomic_write_text",
    "count_records",
    "detect_input_kind",
    "expand_fasta_paths",
    "is_compressed",
    "iter_fasta",
    "iter_fastq",
    "merge_fasta",
    "open_text",
    "read_fasta",
    "strip_compression_suffix",
]
