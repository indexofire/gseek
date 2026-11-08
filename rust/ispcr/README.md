# `gseek-ispcr-fast` — optional Rust accelerator for `gseek.ispcr`

`gseek.ispcr` is pure Python by design (zero runtime dependencies). At the
default 7-30 primer pairs that means one `bytes.find` pass per primer seed: 250 ms
per genome for 7 pairs, 1.1 s for 30 — and because the scan holds the GIL,
`--jobs` threads give no speedup.

This crate replaces the scan with **one pass per contig**:

1. build a q-gram index of the contig (q = 8) — a bucketed position list built in
   a single sweep (~7 MB for a 1.7 Mb assembly);
2. look each primer's seeds up in the index (O(1) per seed);
3. verify every candidate with a banded unit-cost edit distance, which also
   gives the substitution/insertion/deletion mix and the subject span.

Measured on 1.6-1.7 Mb *C. jejuni* assemblies (single thread, 8 genomes):

| pairs | pure Python | Rust scanner | speedup |
|---|---|---|---|
| 7 | 263 ms/genome | **20 ms/genome** | 13× |
| 30 | 1130 ms/genome | **22 ms/genome** | 51× |

## Build

```console
cd rust/ispcr
maturin build --release           # wheel in target/wheels/
# or, into the active interpreter:
maturin develop --release
```

`maturin` builds the extension as `gseek._ispcr_fast`; `gseek.ispcr` imports it
when it is present and silently falls back to the Python engine otherwise, so a
plain `pip install gseek` keeps working with identical results.

Set `PYO3_PYTHON=<interpreter>` when the build environment's Python is not the
one you want to link against.

## Contract

`SiteScanner(haystack).sites(primer, max_mismatches, max_edits)` returns
`(position, substitutions, span, insertions, deletions)` tuples — exactly the
site records the Python engine produces. `tests/test_ispcr.py` pins that parity
(and skips when the accelerator is not installed), so the accelerator stays a
performance knob and never a behaviour change.

## Why q = 8

Every primer seed in the Python engine is at most 8 nt (`_MIN_SEED` = 6, seeds are
`len // (mismatches + 1)`). Indexing 8-mers therefore lets the accelerator serve
both the substitution pass and the 6 nt seeds of the indel pass from one index,
and 4^8 = 65 536 buckets keeps the head table small enough for L2.
