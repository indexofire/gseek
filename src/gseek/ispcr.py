"""in-silico PCR: primer pairs in, amplicons out.

Every other gseek capability adapts an external aligner; isPCR is arithmetic on
sequences, so it runs inline with the standard library only (gseek's zero
dependency rule) and needs no tool, no index and no subprocess.

Why it lives in gseek: PCR confirmation is the layer that settles what an
alignment cannot. Two references may be equally good alignments, and the
published assay distinguishes them by a band of a given size. Making that a
first-class gseek capability means consumers (gspec pipelines, gside, scripts)
can express "primer pair X amplified a product of N bp" in the same result
vocabulary as their alignments, instead of shelling out to exonerate/ipcress.

Model
-----
A pair amplifies when its forward primer matches one strand and the reverse
primer's reverse complement matches the same strand downstream of it, with the
product inside the pair's size window. Both orientations are searched, so a
product may lie on either strand; reported coordinates are always on the
forward strand and 0-based half-open (gseek's coordinate contract).

Matching is seed-and-verify: the primer is split into ``mismatches + 1`` seeds
that are searched with ``bytes.find`` (C speed) and every seed hit is verified
against the full primer. Primers may carry IUPAC ambiguity codes, which match
the concrete bases of their expansion; an ambiguity code in the *target* never
matches, because it is not a base the primer can pair with.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from gseek.errors import InputError, UsageError
from gseek.io import iter_fasta
from gseek.version import __version__

ISPCR_SCHEMA: Final = "gseek.ispcr/1"
PRIMERS_SCHEMA: Final = "gseek.primers/1"

DEFAULT_MAX_MISMATCHES: Final = 2
#: Default edit budget for the gapped pass; 0 keeps substitution-only matching.
DEFAULT_MAX_EDITS: Final = 0
DEFAULT_MIN_SIZE: Final = 20
DEFAULT_MAX_SIZE: Final = 20_000
#: Shortest seed worth searching; below this a seed hit is noise in a genome.
_MIN_SEED: Final = 6
#: Bound on seed hits verified per seed — a low-complexity target can put a
#: 6-mer everywhere, and an unbounded scan would turn a 1 s run into minutes.
_MAX_SEED_HITS: Final = 4096
#: Cached optional accelerator module; False once probed and unavailable.
_FAST_MODULE: Any = None

_COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVNacgtryswkmbdhvn", "TGCAYRSWMKVHDBNtgcayrswmkvhdbn")
_IUPAC: Final[dict[str, str]] = {
    "A": "A",
    "C": "C",
    "G": "G",
    "T": "T",
    "R": "AG",
    "Y": "CT",
    "S": "CG",
    "W": "AT",
    "K": "GT",
    "M": "AC",
    "B": "CGT",
    "D": "AGT",
    "H": "ACT",
    "V": "ACG",
    "N": "ACGT",
}


def reverse_complement(sequence: str) -> str:
    """Reverse complement of a DNA sequence (IUPAC-aware, case preserved)."""
    return sequence.translate(_COMPLEMENT)[::-1]


def _validate_primer(sequence: str, *, field_name: str, name: str) -> str:
    if not sequence:
        raise UsageError(
            f"primer pair {name!r}: {field_name} primer is empty",
            context={"pair": name, "field": field_name},
        )
    upper = sequence.upper()
    bad = sorted({base for base in upper if base not in _IUPAC})
    if bad:
        raise UsageError(
            f"primer pair {name!r}: {field_name} primer has invalid base(s) {', '.join(bad)}",
            context={"pair": name, "field": field_name, "bases": ", ".join(bad)},
        )
    return upper


@dataclass(frozen=True, slots=True)
class PrimerPair:
    """One primer pair and the product window that counts as amplification."""

    name: str
    forward: str
    reverse: str
    min_size: int = DEFAULT_MIN_SIZE
    max_size: int = DEFAULT_MAX_SIZE
    max_mismatches: int | None = None
    #: Total edits (substitutions + insertions + deletions) tolerated in EACH
    #: primer site when the gapped pass runs; None = the run default
    #: (``ispcr(max_edits=...)``, itself 0 = substitution-only).
    max_edits: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise UsageError("primer pair name must not be empty")
        forward = _validate_primer(self.forward, field_name="forward", name=self.name)
        reverse = _validate_primer(self.reverse, field_name="reverse", name=self.name)
        object.__setattr__(self, "forward", forward)
        object.__setattr__(self, "reverse", reverse)
        if self.min_size < 0 or self.max_size < 0:
            raise UsageError(
                f"primer pair {self.name!r}: product sizes must not be negative",
                context={"pair": self.name},
            )
        if self.min_size > self.max_size:
            raise UsageError(
                f"primer pair {self.name!r}: min_size {self.min_size} exceeds"
                f" max_size {self.max_size}",
                context={"pair": self.name},
            )
        if self.max_mismatches is not None and self.max_mismatches < 0:
            raise UsageError(
                f"primer pair {self.name!r}: max_mismatches must not be negative",
                context={"pair": self.name},
            )
        if self.max_edits is not None and self.max_edits < 0:
            raise UsageError(
                f"primer pair {self.name!r}: max_edits must not be negative",
                context={"pair": self.name},
            )


@dataclass(frozen=True, slots=True)
class Amplicon:
    """One primer site pair: a product of ``size`` bp on ``contig``.

    ``start``/``end`` are the product's 0-based half-open span on the forward
    strand; ``forward_*``/``reverse_*`` are each primer's site mapped onto that
    strand (so a minus-strand product still has ascending coordinates).
    """

    pair: str
    contig: str
    start: int
    end: int
    size: int
    forward_start: int
    forward_end: int
    reverse_start: int
    reverse_end: int
    forward_mismatches: int
    reverse_mismatches: int
    #: Edits per primer site (``substitutions`` + ``insertions`` + ``deletions``
    #: = the edit distance); substitutions-only matching leaves insertions and
    #: deletions at 0.
    forward_edits: dict[str, int] = field(default_factory=dict)
    reverse_edits: dict[str, int] = field(default_factory=dict)
    #: Bases the primer site consumed on the subject (indels shift this by the
    #: indel count, which is what makes the product size move).
    forward_span: int = 0
    reverse_span: int = 0

    def to_document(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "contig": self.contig,
            "start": self.start,
            "end": self.end,
            "size": self.size,
            "forward_start": self.forward_start,
            "forward_end": self.forward_end,
            "reverse_start": self.reverse_start,
            "reverse_end": self.reverse_end,
            "forward_mismatches": self.forward_mismatches,
            "reverse_mismatches": self.reverse_mismatches,
            "forward_edits": self.forward_edits,
            "reverse_edits": self.reverse_edits,
            "forward_span": self.forward_span,
            "reverse_span": self.reverse_span,
        }


@dataclass(frozen=True, slots=True)
class IspcrResult:
    """One query file's in-silico PCR outcome (``gseek.ispcr/1``)."""

    query: str
    primers: tuple[PrimerPair, ...]
    amplicons: tuple[Amplicon, ...]
    max_mismatches: int = DEFAULT_MAX_MISMATCHES
    max_edits: int = DEFAULT_MAX_EDITS
    schema: str = ISPCR_SCHEMA

    @property
    def pairs(self) -> frozenset[str]:
        """Names of the pairs that produced at least one amplicon."""
        return positive_pairs(self)

    def to_document(self) -> dict[str, Any]:
        """The ``gseek.ispcr/1`` document (JSON-ready, schema first)."""
        positive = self.pairs
        return {
            "schema": self.schema,
            "tool": {"name": "gseek", "version": __version__},
            "params": {
                "max_mismatches": self.max_mismatches,
                "max_edits": self.max_edits,
                "min_size": min((pair.min_size for pair in self.primers), default=None),
                "max_size": max((pair.max_size for pair in self.primers), default=None),
            },
            "primers": [
                {
                    "name": pair.name,
                    "forward": pair.forward,
                    "reverse": pair.reverse,
                    "min_size": pair.min_size,
                    "max_size": pair.max_size,
                    "max_mismatches": (
                        self.max_mismatches if pair.max_mismatches is None else pair.max_mismatches
                    ),
                }
                for pair in self.primers
            ],
            "files": [
                {
                    "file": self.query,
                    "amplicons": [amplicon.to_document() for amplicon in self.amplicons],
                }
            ],
            "summary": {
                "pairs_tested": len(self.primers),
                "pairs_positive": len(positive),
            },
        }


def positive_pairs(result: IspcrResult) -> frozenset[str]:
    """The pairs with at least one amplicon — the PCR-positive set."""
    return frozenset(amplicon.pair for amplicon in result.amplicons)


def _seed_offsets(primer: str, mismatches: int) -> list[tuple[int, str]]:
    """Unambiguous ``(offset, seed)`` pairs covering ``primer``.

    ``mismatches + 1`` seeds guarantee that at least one seed is exact for any
    tolerated site, so verification only runs on real candidates.
    """
    count = min(mismatches + 1, max(1, len(primer) // _MIN_SEED))
    width = max(_MIN_SEED, len(primer) // count)
    offsets: list[tuple[int, str]] = []
    for index in range(count):
        start = index * (len(primer) - width) // max(1, count - 1) if count > 1 else 0
        seed = primer[start : start + width]
        if all(base in "ACGT" for base in seed):
            offsets.append((start, seed))
    return offsets


def _edit_breakdown(primer: str, target: str) -> tuple[int, dict[str, int]]:
    """Unit-cost edit distance and its operation mix for ``primer`` vs ``target``.

    "Deletion" is a subject base the primer has and the genome lacks;
    "insertion" is an extra genome base inside the primer site. The primer is
    short (<= ~30 nt) and the window is primer length +/- the budget, so a plain
    banded DP is fast enough and keeps the traceback honest.
    """
    n, m = len(primer), len(target)
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for row in range(1, n + 1):
        cost[row][0] = row
    for column in range(1, m + 1):
        cost[0][column] = column
    for row in range(1, n + 1):
        previous, current = cost[row - 1], cost[row]
        base = primer[row - 1]
        for column in range(1, m + 1):
            current[column] = min(
                previous[column - 1] + (0 if base == target[column - 1] else 1),
                previous[column] + 1,
                current[column - 1] + 1,
            )
    edits = {"substitutions": 0, "insertions": 0, "deletions": 0}
    row, column = n, m
    while row > 0 or column > 0:
        if (
            row > 0
            and column > 0
            and cost[row][column]
            == cost[row - 1][column - 1] + (0 if primer[row - 1] == target[column - 1] else 1)
        ):
            if primer[row - 1] != target[column - 1]:
                edits["substitutions"] += 1
            row, column = row - 1, column - 1
        elif row > 0 and cost[row][column] == cost[row - 1][column] + 1:
            edits["deletions"] += 1
            row -= 1
        else:
            edits["insertions"] += 1
            column -= 1
    return cost[n][m], edits


def _gapped_spans(
    primer: str, haystack: bytes, start: int, max_edits: int
) -> tuple[int, int, dict[str, int]] | None:
    """Best ``(distance, span, mix)`` for ``primer`` anchored at ``start``.

    The primer may consume ``len(primer) +/- max_edits`` subject bases, so the
    product size moves with the indel — which is exactly what a gel would show.
    """
    length = len(primer)
    best: tuple[int, int, dict[str, int]] | None = None
    for span in range(max(1, length - max_edits), length + max_edits + 1):
        if start + span > len(haystack):
            continue
        # The break-down compares characters: decode the (short) window rather
        # than teaching the DP bytes-vs-str semantics.
        distance, mix = _edit_breakdown(primer, haystack[start : start + span].decode())
        if distance > max_edits:
            continue
        if best is None or (distance, abs(span - length)) < (best[0], abs(best[1] - length)):
            best = (distance, span, mix)
    return best


def _sites_gapped(
    haystack: bytes, primer: str, max_edits: int
) -> list[tuple[int, int, int, dict[str, int]]]:
    """Indel-tolerant primer sites: 6 nt seeds, shifted candidates, gapped verify."""
    length = len(primer)
    span_limit = len(haystack) - max(1, length - max_edits)
    if span_limit < 0:
        return []
    seed_length = 6
    candidates: set[int] = set()
    for offset in range(0, max(1, length - seed_length + 1), seed_length):
        seed = primer[offset : offset + seed_length]
        if len(seed) < seed_length or any(base not in "ACGT" for base in seed):
            continue
        needle = seed.encode()
        position = haystack.find(needle)
        verified = 0
        while position >= 0 and verified < _MAX_SEED_HITS:
            for shift in range(-max_edits, max_edits + 1):
                start = position - offset + shift
                if 0 <= start <= span_limit:
                    candidates.add(start)
            position = haystack.find(needle, position + 1)
            verified += 1
    found: list[tuple[int, int, int, dict[str, int]]] = []
    for start in sorted(candidates):
        best = _gapped_spans(primer, haystack, start, max_edits)
        if best is None:
            continue
        _, span, mix = best
        found.append((start, mix["substitutions"], span, mix))
    # One site per cluster: a deletion lets the primer align at several offsets,
    # and reporting all of them would multiply the products. Keep the fewest-
    # edits site, then drop anything overlapping it.
    found.sort(key=lambda site: (sum(site[3].values()), site[0]))
    kept: list[tuple[int, int, int, dict[str, int]]] = []
    for site in found:
        if any(site[0] < other[0] + other[2] and other[0] < site[0] + site[2] for other in kept):
            continue
        kept.append(site)
    return sorted(kept)


def _fast_scanner(haystack: bytes) -> Any | None:
    """The optional Rust scanner for one contig, or None for the pure-Python path.

    The accelerator is a separate optional wheel (``gseek[ispcr-fast]``); when it
    is absent — the default install — the pure-Python engine runs and the results
    are identical.
    """
    global _FAST_MODULE
    if ispcr_backend() != "rust":
        return None
    scanner = getattr(_FAST_MODULE, "SiteScanner", None)
    if scanner is None:  # pragma: no cover - broken install
        _FAST_MODULE = False
        return None
    return scanner(haystack)


def ispcr_backend() -> str:
    """``"rust"`` when the optional accelerator is importable, else ``"python"``."""
    global _FAST_MODULE
    if _FAST_MODULE is None:
        try:  # pragma: no cover - depends on the optional wheel
            from gseek import _ispcr_fast  # type: ignore[attr-defined]

            _FAST_MODULE = _ispcr_fast
        except ImportError:  # pragma: no cover - default install
            _FAST_MODULE = False
    return "rust" if _FAST_MODULE else "python"


def _all_sites(
    haystack: bytes,
    primer: str,
    mismatches: int,
    max_edits: int,
    scanner: Any | None = None,
) -> list[tuple[int, int, int, dict[str, int]]]:
    """Every primer site: the cheap substitution scan first, indels only if it misses."""
    if scanner is not None:
        return [
            (
                position,
                substitutions,
                span,
                {
                    "substitutions": substitutions,
                    "insertions": insertions,
                    "deletions": deletions,
                },
            )
            for position, substitutions, span, insertions, deletions in scanner.sites(
                primer.encode(), mismatches, max_edits
            )
        ]
    hamming = _sites(haystack, primer, mismatches)
    if hamming or max_edits <= 0:
        return [
            (position, subs, len(primer), {"substitutions": subs, "insertions": 0, "deletions": 0})
            for position, subs in hamming
        ]
    return _sites_gapped(haystack, primer, max_edits)


def _matches_at(haystack: bytes, position: int, primer: str, limit: int) -> int | None:
    """Mismatch count of ``primer`` at ``position`` (None when over ``limit``)."""
    mismatches = 0
    for index, base in enumerate(primer):
        allowed = _IUPAC[base]
        if haystack[position + index] in allowed.encode():
            continue
        mismatches += 1
        if mismatches > limit:
            return None
    return mismatches


def _sites(haystack: bytes, primer: str, mismatches: int) -> list[tuple[int, int]]:
    """``(position, mismatches)`` for every primer site on ``haystack``."""
    length = len(primer)
    span = len(haystack) - length
    if span < 0:
        return []
    limit = min(mismatches, length)
    seeds = _seed_offsets(primer, limit)
    best: dict[int, int] = {}
    if not seeds:
        # Fully degenerate primer: no exact seed exists, so scan the haystack.
        for position in range(span + 1):
            count = _matches_at(haystack, position, primer, limit)
            if count is not None:
                best[position] = count
        return sorted(best.items())
    for offset, seed in seeds:
        needle = seed.encode()
        position = haystack.find(needle)
        verified = 0
        while position >= 0 and verified < _MAX_SEED_HITS:
            start = position - offset
            if 0 <= start <= span:
                count = _matches_at(haystack, start, primer, limit)
                if count is not None:
                    previous = best.get(start)
                    if previous is None or count < previous:
                        best[start] = count
            position = haystack.find(needle, position + 1)
            verified += 1
    return sorted(best.items())


def _pair_amplicons(
    contig: str,
    plus: bytes,
    pair: PrimerPair,
    mismatches: int,
    max_edits: int,
    *,
    order: int,
    scanner: Any | None = None,
) -> list[tuple[int, int, int, Amplicon]]:
    """Every amplicon this pair makes on one contig, in both orientations.

    Site records are ``(start, substitutions, span, edit mix)``; the product
    size uses each site's SPAN, so an indel in a primer site shifts the product
    exactly as it would on a gel.
    """
    forward_plus = _all_sites(plus, pair.forward, mismatches, max_edits, scanner)
    reverse_minus = _all_sites(
        plus, reverse_complement(pair.reverse), mismatches, max_edits, scanner
    )
    reverse_plus = _all_sites(plus, pair.reverse, mismatches, max_edits, scanner)
    forward_minus = _all_sites(
        plus, reverse_complement(pair.forward), mismatches, max_edits, scanner
    )

    def amplicon(
        forward: tuple[int, int, int, dict[str, int]],
        reverse: tuple[int, int, int, dict[str, int]],
        *,
        start: int,
        end: int,
    ) -> Amplicon:
        return Amplicon(
            pair=pair.name,
            contig=contig,
            start=start,
            end=end,
            size=end - start,
            forward_start=forward[0],
            forward_end=forward[0] + forward[2],
            reverse_start=reverse[0],
            reverse_end=reverse[0] + reverse[2],
            forward_mismatches=forward[1],
            reverse_mismatches=reverse[1],
            forward_edits=dict(forward[3]),
            reverse_edits=dict(reverse[3]),
            forward_span=forward[2],
            reverse_span=reverse[2],
        )

    found: list[tuple[int, int, int, Amplicon]] = []
    for forward in forward_plus:
        for reverse in reverse_minus:
            if reverse[0] < forward[0] + forward[2]:
                continue
            start = forward[0]
            end = reverse[0] + reverse[2]
            found.append((order, start, end, amplicon(forward, reverse, start=start, end=end)))
    for reverse in reverse_plus:
        for forward in forward_minus:
            if forward[0] < reverse[0] + reverse[2]:
                continue
            start = reverse[0]
            end = forward[0] + forward[2]
            found.append((order, start, end, amplicon(forward, reverse, start=start, end=end)))
    return found


def _total_edits(amplicon: Amplicon) -> int:
    """Edits across both primer sites (substitutions + insertions + deletions)."""
    return sum(amplicon.forward_edits.values()) + sum(amplicon.reverse_edits.values())


def _validate_mismatches(max_mismatches: int) -> int:
    if max_mismatches < 0:
        raise UsageError(
            f"max_mismatches must not be negative: {max_mismatches}",
            context={"max_mismatches": str(max_mismatches)},
        )
    return max_mismatches


def ispcr(
    query: Path,
    primers: list[PrimerPair] | tuple[PrimerPair, ...],
    *,
    max_mismatches: int = DEFAULT_MAX_MISMATCHES,
    min_size: int | None = None,
    max_size: int | None = None,
    max_edits: int = DEFAULT_MAX_EDITS,
) -> IspcrResult:
    """Run in-silico PCR of ``primers`` against the assembly ``query``.

    ``max_mismatches`` is the run default for SUBSTITUTIONS; a pair may override
    it. ``max_edits`` enables the indel-tolerant pass (edits = substitutions +
    insertion + deletions per primer site) and is only paid when the cheap
    substitution scan finds nothing; 0 keeps substitution-only matching.
    ``min_size``/``max_size`` override every pair's window when given (a
    run-wide filter on top of the per-pair window: the product must satisfy
    both).
    """
    pairs = tuple(primers)
    if not pairs:
        raise UsageError("no primer pairs given")
    names = [pair.name for pair in pairs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise UsageError(
            f"duplicate primer pair name(s): {', '.join(duplicates)}",
            context={"pairs": ", ".join(duplicates)},
        )
    default_mismatches = _validate_mismatches(max_mismatches)
    if max_edits < 0:
        raise UsageError(
            f"max_edits must not be negative: {max_edits}", context={"max_edits": str(max_edits)}
        )
    if min_size is not None and min_size < 0:
        raise UsageError(f"min_size must not be negative: {min_size}")
    if max_size is not None and max_size < 0:
        raise UsageError(f"max_size must not be negative: {max_size}")
    if min_size is not None and max_size is not None and min_size > max_size:
        raise UsageError(f"min_size {min_size} exceeds max_size {max_size}")
    if not Path(query).is_file():
        raise InputError(
            f"query file not found or unreadable: {query}",
            code="INPUT_NOT_FOUND",
            context={"file": str(query)},
        )

    candidates: list[tuple[int, int, int, Amplicon]] = []
    for record in iter_fasta(Path(query)):
        plus = record.sequence.upper().encode()
        scanner = _fast_scanner(plus) if _FAST_MODULE is not False else None
        for order, pair in enumerate(pairs):
            mismatches = default_mismatches if pair.max_mismatches is None else pair.max_mismatches
            edits = max_edits if pair.max_edits is None else pair.max_edits
            low = pair.min_size if min_size is None else max(pair.min_size, min_size)
            high = pair.max_size if max_size is None else min(pair.max_size, max_size)
            if low > high:
                continue
            for order_, start, end, amplicon in _pair_amplicons(
                record.sequence_id, plus, pair, mismatches, edits, order=order, scanner=scanner
            ):
                if low <= amplicon.size <= high:
                    candidates.append((order_, start, end, amplicon))

    # One product per site pair: dedupe on (pair, contig, start, end) keeping
    # the fewest mismatches, then order by primer declaration, contig, start.
    best: dict[tuple[str, str, int, int], Amplicon] = {}
    for _, _, _, amplicon in candidates:
        key = (amplicon.pair, amplicon.contig, amplicon.start, amplicon.end)
        previous = best.get(key)
        if previous is None or (_total_edits(amplicon) < _total_edits(previous)):
            best[key] = amplicon
    rank = {pair.name: index for index, pair in enumerate(pairs)}
    ordered = sorted(
        best.values(),
        key=lambda amplicon: (
            rank[amplicon.pair],
            amplicon.contig,
            amplicon.start,
            amplicon.end,
        ),
    )
    return IspcrResult(
        query=str(query),
        primers=pairs,
        amplicons=tuple(ordered),
        max_mismatches=default_mismatches,
        max_edits=max_edits,
    )


_PRIMER_COLUMNS: Final = ("name", "forward", "reverse", "min_size", "max_size", "max_mismatches")


def _primer_from_mapping(row: dict[str, Any], *, source: str) -> PrimerPair:
    missing = [key for key in ("name", "forward", "reverse") if not row.get(key)]
    if missing:
        raise UsageError(
            f"primer entry in {source} lacks {', '.join(missing)}",
            context={"file": source, "missing": ", ".join(missing)},
        )
    max_mismatches = row.get("max_mismatches")
    return PrimerPair(
        name=str(row["name"]),
        forward=str(row["forward"]),
        reverse=str(row["reverse"]),
        min_size=int(row.get("min_size") or DEFAULT_MIN_SIZE),
        max_size=int(row.get("max_size") or DEFAULT_MAX_SIZE),
        max_mismatches=None if max_mismatches in (None, "") else int(max_mismatches),
    )


def read_primers(path: Path) -> tuple[PrimerPair, ...]:
    """Read primer pairs from a TSV table or a ``gseek.primers/1`` JSON file.

    The TSV header must carry ``name``, ``forward`` and ``reverse``; optional
    columns are ``min_size``, ``max_size`` and ``max_mismatches``. The JSON form
    is ``{"schema": "gseek.primers/1", "primers": [...]}``.
    """
    if not Path(path).is_file():
        raise InputError(
            f"primer file not found or unreadable: {path}",
            code="INPUT_NOT_FOUND",
            context={"file": str(path)},
        )
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        payload: Any = json.loads(text)
        if not isinstance(payload, dict):
            raise UsageError(f"primer file {path} must be a JSON object")
        schema = payload.get("schema", PRIMERS_SCHEMA)
        if schema != PRIMERS_SCHEMA:
            raise UsageError(
                f"primer file {path} declares schema {schema!r}, expected {PRIMERS_SCHEMA!r}",
                context={"file": str(path), "schema": str(schema)},
            )
        entries = payload.get("primers")
        if not isinstance(entries, list) or not entries:
            raise UsageError(f"primer file {path} carries no primers")
        pairs = tuple(_primer_from_mapping(dict(entry), source=str(path)) for entry in entries)
    else:
        rows = list(csv.DictReader(text.splitlines(), delimiter="\t"))
        if not rows:
            raise UsageError(f"primer file {path} carries no data rows")
        header = {name.strip().lower() for name in (rows[0].keys() if rows else ())}
        missing = [column for column in ("name", "forward", "reverse") if column not in header]
        if missing:
            raise UsageError(
                f"primer table {path} lacks column(s): {', '.join(missing)}",
                context={"file": str(path), "missing": ", ".join(missing)},
            )
        pairs = tuple(
            _primer_from_mapping({k.lower(): v for k, v in row.items() if k}, source=str(path))
            for row in rows
        )
    names = [pair.name for pair in pairs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise UsageError(
            f"primer file {path} repeats pair name(s): {', '.join(duplicates)}",
            context={"file": str(path), "pairs": ", ".join(duplicates)},
        )
    return pairs


@dataclass(frozen=True, slots=True)
class IspcrBatch:
    """Several queries screened against one primer set (convenience view)."""

    primers: tuple[PrimerPair, ...]
    results: tuple[IspcrResult, ...] = field(default_factory=tuple)

    def positive_by_file(self) -> dict[str, frozenset[str]]:
        return {result.query: result.pairs for result in self.results}

    def to_document(self) -> dict[str, Any]:
        files = [
            {
                "file": result.query,
                "amplicons": [amplicon.to_document() for amplicon in result.amplicons],
            }
            for result in self.results
        ]
        positive = sum(len(positive_pairs(result)) for result in self.results)
        return {
            "schema": ISPCR_SCHEMA,
            "tool": {"name": "gseek", "version": __version__},
            "params": {"max_mismatches": DEFAULT_MAX_MISMATCHES},
            "primers": [
                {"name": pair.name, "forward": pair.forward, "reverse": pair.reverse}
                for pair in self.primers
            ],
            "files": files,
            "summary": {"pairs_tested": len(self.primers), "pairs_positive": positive},
        }


def ispcr_many(
    queries: list[Path] | tuple[Path, ...],
    primers: list[PrimerPair] | tuple[PrimerPair, ...],
    *,
    max_mismatches: int = DEFAULT_MAX_MISMATCHES,
) -> IspcrBatch:
    """Run one primer set against several assemblies."""
    pairs = tuple(primers)
    results = tuple(ispcr(Path(query), pairs, max_mismatches=max_mismatches) for query in queries)
    return IspcrBatch(primers=pairs, results=results)


__all__ = [
    "DEFAULT_MAX_MISMATCHES",
    "DEFAULT_MAX_SIZE",
    "DEFAULT_MIN_SIZE",
    "ISPCR_SCHEMA",
    "PRIMERS_SCHEMA",
    "Amplicon",
    "IspcrBatch",
    "IspcrResult",
    "PrimerPair",
    "ispcr",
    "ispcr_backend",
    "ispcr_many",
    "positive_pairs",
    "read_primers",
    "reverse_complement",
]
