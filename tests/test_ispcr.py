"""in-silico PCR (isPCR) — primer pairs in, amplicons out.

The engine is a pure-stdlib seed-and-verify search so that any consumer
(gspec, gside, scripts) can ask "does this pair amplify, and what is the
product size" without adding an exonerate/ipcress dependency.

Behaviour pinned here:

- a pair amplifies when the forward primer matches the forward strand and the
  reverse primer's reverse complement matches the forward strand *downstream*
  of it, with the product inside the pair's size window;
- mismatches are allowed on both primers, counted per primer, never silently;
- both strands are searched: a pair whose primers sit on the minus strand
  amplifies as well (coordinates are reported on the forward strand);
- products spanning contigs do not exist (one contig per amplicon);
- IUPAC ambiguity codes in a primer match any base; an ambiguity code in the
  *target* never matches (it is not a concrete base);
- zero-amplification files report zero amplicons instead of failing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from gseek.errors import InputError, UsageError
from gseek.ispcr import (
    ISPCR_SCHEMA,
    PRIMERS_SCHEMA,
    Amplicon,
    PrimerPair,
    ispcr,
    positive_pairs,
    read_primers,
)

FWD = "ACGTACGTACGTACGTACGT"  # 20 nt
REV = "TTTTGGGGCCCCAAAATTTT"  # 20 nt


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _genome(tmp_path: Path, sequence: str, name: str = "ctg1") -> Path:
    return _write(tmp_path / "genome.fna", f">{name}\n{sequence}\n")


def _rc(sequence: str) -> str:
    table = str.maketrans("ACGTacgt", "TGCAtgca")
    return sequence.translate(table)[::-1]


def test_amplicon_is_found_with_expected_coordinates_and_size(tmp_path: Path) -> None:
    spacer = "A" * 200
    genome = _genome(tmp_path, "C" * 50 + FWD + spacer + _rc(REV) + "G" * 50)
    result = ispcr(genome, [PrimerPair("p1", FWD, REV, min_size=100, max_size=400)])
    assert result.schema == ISPCR_SCHEMA
    assert len(result.amplicons) == 1
    amplicon = result.amplicons[0]
    assert (amplicon.pair, amplicon.contig) == ("p1", "ctg1")
    assert amplicon.start == 50
    assert amplicon.size == len(FWD) + len(spacer) + len(REV)
    assert amplicon.end == 50 + amplicon.size
    assert amplicon.forward_mismatches == 0
    assert amplicon.reverse_mismatches == 0


def test_product_outside_the_size_window_is_not_reported(tmp_path: Path) -> None:
    genome = _genome(tmp_path, FWD + "A" * 500 + _rc(REV))
    assert not ispcr(genome, [PrimerPair("p", FWD, REV, min_size=100, max_size=200)]).amplicons
    assert ispcr(genome, [PrimerPair("p", FWD, REV, min_size=500, max_size=600)]).amplicons


def test_mismatches_are_allowed_and_counted(tmp_path: Path) -> None:
    """One mismatch per primer still amplifies and is reported per primer."""
    fwd_mut = "A" + FWD[1:]  # first base differs from the target's ACGT...
    target_fwd = "T" + FWD[1:]  # target carries the mutated base
    genome = _genome(tmp_path, target_fwd + "A" * 100 + _rc("G" + REV[1:]))
    result = ispcr(
        genome, [PrimerPair("p", fwd_mut, REV, min_size=100, max_size=300)], max_mismatches=1
    )
    assert len(result.amplicons) == 1
    assert result.amplicons[0].forward_mismatches == 1
    assert result.amplicons[0].reverse_mismatches == 1


def test_too_many_mismatches_does_not_amplify(tmp_path: Path) -> None:
    target_fwd = "TTTT" + FWD[4:]
    genome = _genome(tmp_path, target_fwd + "A" * 100 + _rc(REV))
    assert not ispcr(
        genome, [PrimerPair("p", FWD, REV, min_size=100, max_size=300)], max_mismatches=2
    ).amplicons


def test_pair_on_the_minus_strand_amplifies(tmp_path: Path) -> None:
    """A pair whose sites sit on the minus strand still yields a product, with
    coordinates reported on the forward strand."""
    forward_sequence = FWD + "A" * 150 + _rc(REV)
    genome = _genome(tmp_path, _rc(forward_sequence))
    result = ispcr(genome, [PrimerPair("p", FWD, REV, min_size=150, max_size=300)])
    assert len(result.amplicons) == 1
    assert result.amplicons[0].size == len(forward_sequence)


def test_iupac_ambiguity_in_a_primer_matches(tmp_path: Path) -> None:
    genome = _genome(tmp_path, "ACGTAAGTACGTACGTACGT" + "A" * 120 + _rc(REV))
    degenerate = PrimerPair("p", "ACGTARGTACGTACGTACGT", REV, min_size=100, max_size=300)
    assert len(ispcr(genome, [degenerate]).amplicons) == 1


def test_ambiguity_in_the_target_is_a_mismatch_not_a_match(tmp_path: Path) -> None:
    """A target ambiguity code is not a base the primer can pair with, so it
    counts against the mismatch budget instead of matching."""
    genome = _genome(tmp_path, "ACGTARGTACGTACGTACGT" + "A" * 120 + _rc(REV))
    pair = PrimerPair("p", "ACGTACGTACGTACGTACGT", REV, min_size=100, max_size=300)
    assert not ispcr(genome, [pair], max_mismatches=0).amplicons
    lenient = ispcr(genome, [pair], max_mismatches=1)
    assert lenient.amplicons[0].forward_mismatches == 1


def test_multiple_pairs_and_multiple_products_are_all_reported(tmp_path: Path) -> None:
    sequence = FWD + "A" * 100 + _rc(REV) + "C" * 100 + FWD + "A" * 100 + _rc(REV)
    genome = _genome(tmp_path, sequence)
    result = ispcr(
        genome,
        [
            PrimerPair("p1", FWD, REV, min_size=100, max_size=300),
            PrimerPair("p2", FWD, FWD, min_size=10, max_size=10_000),
        ],
    )
    assert {amplicon.pair for amplicon in result.amplicons} == {"p1", "p2"}
    assert [amplicon.pair for amplicon in result.amplicons].count("p1") == 2
    assert positive_pairs(result) == frozenset({"p1", "p2"})


def test_multiple_contigs_are_not_joined(tmp_path: Path) -> None:
    genome = _write(
        tmp_path / "two.fna",
        f">a\n{FWD}{'A' * 100}\n>b\n{_rc(REV)}{'A' * 100}\n",
    )
    assert not ispcr(genome, [PrimerPair("p", FWD, REV, min_size=10, max_size=10_000)]).amplicons


# ---------------------------------------------------------------- indels
#: Long primers (30 nt) keep the indel cases unambiguous: a Hamming alignment
#: across a mid-primer deletion would need ~15 substitutions, so only the
#: gapped pass can find these sites.
FWD30 = "ACGTACGTACGTACGTACGTACGTACGTAC"
REV30 = "TTTTGGGGCCCCAAAATTTTGGGGCCCCAA"


def _pair() -> PrimerPair:
    return PrimerPair("p", FWD30, REV30, min_size=100, max_size=400)


def test_one_base_deletion_in_the_forward_primer_site_amplifies(tmp_path: Path) -> None:
    mutated = FWD30[:15] + FWD30[16:]  # one base deleted mid-primer
    genome = _genome(tmp_path, mutated + "A" * 150 + _rc(REV30))
    assert not ispcr(genome, [_pair()]).amplicons
    result = ispcr(genome, [_pair()], max_edits=1)
    assert len(result.amplicons) == 1
    amplicon = result.amplicons[0]
    assert amplicon.forward_edits == {"substitutions": 0, "insertions": 0, "deletions": 1}
    assert amplicon.forward_span == len(FWD30) - 1
    assert amplicon.size == len(mutated) + 150 + len(REV30)


def test_one_base_insertion_in_the_reverse_primer_site_amplifies(tmp_path: Path) -> None:
    reverse_site = _rc(REV30)
    mutated = reverse_site[:15] + "A" + reverse_site[15:]
    genome = _genome(tmp_path, FWD30 + "A" * 150 + mutated)
    assert not ispcr(genome, [_pair()]).amplicons
    result = ispcr(genome, [_pair()], max_edits=1)
    assert len(result.amplicons) == 1
    assert result.amplicons[0].reverse_edits == {
        "substitutions": 0,
        "insertions": 1,
        "deletions": 0,
    }
    assert result.amplicons[0].reverse_span == len(REV30) + 1


def test_indel_plus_substitution_within_budget(tmp_path: Path) -> None:
    mutated = "T" + FWD30[1:15] + FWD30[16:]  # one substitution + one deletion
    genome = _genome(tmp_path, mutated + "A" * 150 + _rc(REV30))
    result = ispcr(genome, [_pair()], max_edits=2)
    assert len(result.amplicons) == 1
    edits = result.amplicons[0].forward_edits
    assert sum(edits.values()) == 2  # within the budget, mix as the DP finds it
    assert edits["deletions"] >= 1


def test_indels_beyond_the_budget_do_not_amplify(tmp_path: Path) -> None:
    mutated = FWD30[:14] + FWD30[16:]  # two bases deleted
    genome = _genome(tmp_path, mutated + "A" * 150 + _rc(REV30))
    pair = _pair()
    assert not ispcr(genome, [pair], max_edits=1).amplicons
    assert len(ispcr(genome, [pair], max_edits=2).amplicons) == 1


def test_max_edits_zero_keeps_the_substitution_only_behaviour(tmp_path: Path) -> None:
    mutated = FWD30[:15] + FWD30[16:]
    genome = _genome(tmp_path, mutated + "A" * 150 + _rc(REV30))
    assert ispcr(genome, [_pair()], max_edits=0).amplicons == ()


def test_primer_pair_may_override_max_edits(tmp_path: Path) -> None:
    mutated = FWD30[:15] + FWD30[16:]
    genome = _genome(tmp_path, mutated + "A" * 150 + _rc(REV30))
    lenient = PrimerPair("p", FWD30, REV30, min_size=100, max_size=400, max_edits=1)
    assert len(ispcr(genome, [lenient]).amplicons) == 1


def test_missing_input_file_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        ispcr(tmp_path / "missing.fna", [PrimerPair("p", FWD, REV)])


def test_primers_tsv_and_json_round_trip(tmp_path: Path) -> None:
    tsv = _write(
        tmp_path / "primers.tsv",
        "name\tforward\treverse\tmin_size\tmax_size\tmax_mismatches\np1\tACGT\tTGCA\t70\t120\t1\n",
    )
    pairs = read_primers(tsv)
    assert pairs == (PrimerPair("p1", "ACGT", "TGCA", 70, 120, 1),)

    document = {
        "schema": PRIMERS_SCHEMA,
        "primers": [
            {"name": "p1", "forward": "ACGT", "reverse": "TGCA"},
            {"name": "p2", "forward": "AAAA", "reverse": "CCCC", "min_size": 50},
        ],
    }
    json_path = _write(tmp_path / "primers.json", json.dumps(document))
    parsed = read_primers(json_path)
    assert [pair.name for pair in parsed] == ["p1", "p2"]
    assert parsed[1].min_size == 50


def test_duplicate_primer_names_are_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "dup.tsv", "name\tforward\treverse\np\tACGT\tTGCA\np\tACGT\tTGCA\n")
    with pytest.raises(UsageError):
        read_primers(path)


def test_invalid_primer_bases_are_rejected() -> None:
    with pytest.raises(UsageError):
        PrimerPair("p", "ACGTX", "TGCA")
    with pytest.raises(UsageError):
        PrimerPair("p", "ACGT", "")


def test_accelerator_matches_the_python_engine_when_installed(tmp_path: Path) -> None:
    """The optional Rust scanner is a performance knob, never a behaviour change."""
    import importlib

    module = importlib.import_module("gseek.ispcr")
    assert hasattr(module, "ispcr_backend")
    if module.ispcr_backend() != "rust":
        pytest.skip("optional Rust accelerator not installed")
    primers = [
        PrimerPair("p1", FWD, REV, min_size=100, max_size=400),
        PrimerPair("p2", FWD30, REV30, min_size=100, max_size=400),
    ]
    genomes = [
        _write(
            tmp_path / f"g{i}.fna",
            f">ctg1\n{'A' * i}{FWD}{'C' * (120 + i)}{_rc(REV)}{'G' * i}\n"
            f">ctg2\n{FWD30[:15]}{FWD30[16:]}{'A' * 130}{_rc(REV30)}\n",
        )
        for i in range(3)
    ]

    mutable = cast(Any, module)

    def collect(force_python: bool) -> dict[str, set[tuple[object, ...]]]:
        saved = mutable._FAST_MODULE
        if force_python:
            mutable._FAST_MODULE = False
        try:
            return {
                genome.name: {
                    (
                        amplicon.pair,
                        amplicon.contig,
                        amplicon.start,
                        amplicon.end,
                        amplicon.size,
                        amplicon.forward_mismatches,
                        amplicon.reverse_mismatches,
                        tuple(sorted(amplicon.forward_edits.items())),
                    )
                    for amplicon in module.ispcr(genome, primers, max_edits=1).amplicons
                }
                for genome in genomes
            }
        finally:
            mutable._FAST_MODULE = saved

    accelerated = collect(force_python=False)
    plain = collect(force_python=True)
    assert accelerated == plain


def test_result_serializes_to_the_documented_schema(tmp_path: Path) -> None:
    genome = _genome(tmp_path, FWD + "A" * 100 + _rc(REV))
    document = ispcr(genome, [PrimerPair("p", FWD, REV, min_size=100, max_size=300)]).to_document()
    assert document["schema"] == ISPCR_SCHEMA
    assert document["params"]["max_mismatches"] == 2
    assert document["files"][0]["file"].endswith("genome.fna")
    amplicon = document["files"][0]["amplicons"][0]
    assert amplicon["pair"] == "p"
    assert amplicon["size"] == 140
    assert document["summary"] == {"pairs_tested": 1, "pairs_positive": 1}


def test_empty_primer_list_is_a_usage_error(tmp_path: Path) -> None:
    genome = _genome(tmp_path, FWD)
    with pytest.raises(UsageError):
        ispcr(genome, [])


def test_amplicon_type_declares_its_fields() -> None:
    fields = set(Amplicon.__dataclass_fields__)
    assert {"pair", "contig", "start", "end", "size", "forward_mismatches"} <= fields
