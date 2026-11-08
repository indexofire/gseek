//! Optional in-silico PCR site scanner for `gseek.ispcr`.
//!
//! The pure-Python engine scans the genome once per primer seed with
//! `bytes.find`; at 30 primer pairs that is 360 full passes over an assembly
//! and it holds the GIL, so threads cannot help. This crate replaces that with
//! one pass per contig:
//!
//! 1. build a q-gram index of the contig (`q = 8`) — a bucketed position list,
//!    built in a single sweep, ~7 MB for a 1.7 Mb genome;
//! 2. for every primer, look its seeds up in the index (O(1) per seed) and
//!    verify each candidate position with a banded bit-parallel edit distance.
//!
//! The Python API stays the contract: `gseek.ispcr` imports this module when it
//! is installed and falls back to the pure-Python engine otherwise, so the core
//! wheel keeps its zero-runtime-dependency promise.
//!
//! Coordinates and semantics are identical to the Python engine: subject
//! intervals are 0-based half-open, mismatches are counted per primer, seeds are
//! exact, and an inline `max_edits` budget tolerates insertions/deletions.

use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Seed length for the q-gram index; every primer seed is padded/split to it.
const Q: usize = 8;
const BUCKETS: usize = 1 << (2 * Q);

#[inline]
fn encode_base(base: u8) -> Option<u32> {
    match base {
        b'A' => Some(0),
        b'C' => Some(1),
        b'G' => Some(2),
        b'T' => Some(3),
        _ => None,
    }
}

/// 2-bit packed q-mer at `start`, or None when the window has a non-ACGT base.
#[inline]
fn encode_qmer(haystack: &[u8], start: usize) -> Option<u32> {
    let mut code = 0u32;
    for offset in 0..Q {
        code = (code << 2) | encode_base(haystack[start + offset])?;
    }
    Some(code)
}

/// One swept q-gram index: bucket heads plus a linked list of positions.
struct QGramIndex {
    heads: Vec<i32>,
    next: Vec<i32>,
}

impl QGramIndex {
    fn build(haystack: &[u8]) -> Self {
        let mut heads = vec![-1i32; BUCKETS];
        let mut next = vec![-1i32; haystack.len().saturating_sub(Q - 1)];
        if haystack.len() >= Q {
            for start in 0..=haystack.len() - Q {
                if let Some(code) = encode_qmer(haystack, start) {
                    next[start] = heads[code as usize];
                    heads[code as usize] = start as i32;
                }
            }
        }
        QGramIndex { heads, next }
    }

    /// Visit every index position whose q-mer equals `code`.
    fn for_each<F: FnMut(usize)>(&self, code: u32, mut visit: F) {
        let mut position = self.heads[code as usize];
        while position >= 0 {
            visit(position as usize);
            position = self.next[position as usize];
        }
    }
}

/// Banded, unit-cost edit distance between a primer and a subject window.
/// Returns `(distance, substitutions, insertions, deletions)`.
fn edit_distance(primer: &[u8], target: &[u8], budget: usize) -> Option<(usize, usize, usize, usize)> {
    let n = primer.len();
    let m = target.len();
    if n.abs_diff(m) > budget {
        return None;
    }
    // Full DP with a one-row band; primer sites are short, so this is cheap and
    // keeps the operation mix exact.
    let mut previous: Vec<usize> = (0..=m).collect();
    let mut current = vec![0usize; m + 1];
    let mut ops: Vec<Vec<u8>> = vec![vec![0u8; m + 1]; n + 1];
    for column in 0..=m {
        ops[0][column] = 2; // insertion
    }
    for row in 1..=n {
        ops[row][0] = 3; // deletion
    }
    for row in 1..=n {
        current[0] = row;
        for column in 1..=m {
            let substitution =
                previous[column - 1] + usize::from(primer[row - 1] != target[column - 1]);
            let deletion = previous[column] + 1;
            let insertion = current[column - 1] + 1;
            let best = substitution.min(deletion).min(insertion);
            current[column] = best;
            ops[row][column] = if best == substitution {
                if primer[row - 1] == target[column - 1] {
                    0
                } else {
                    1
                }
            } else if best == deletion {
                3
            } else {
                2
            };
        }
        std::mem::swap(&mut previous, &mut current);
    }
    let distance = previous[m];
    if distance > budget {
        return None;
    }
    let (mut substitutions, mut insertions, mut deletions) = (0usize, 0usize, 0usize);
    let (mut row, mut column) = (n, m);
    while row > 0 || column > 0 {
        match ops[row][column] {
            1 => {
                substitutions += 1;
                row -= 1;
                column -= 1;
            }
            0 => {
                row -= 1;
                column -= 1;
            }
            3 => {
                deletions += 1;
                row -= 1;
            }
            _ => {
                insertions += 1;
                column -= 1;
            }
        }
    }
    Some((distance, substitutions, insertions, deletions))
}

/// Best `(distance, span, subs, ins, dels)` for a primer anchored at `start`.
fn best_span(
    primer: &[u8],
    haystack: &[u8],
    start: usize,
    budget: usize,
) -> Option<(usize, usize, usize, usize, usize)> {
    let length = primer.len();
    let low = length.saturating_sub(budget).max(1);
    let high = length + budget;
    let mut best: Option<(usize, usize, usize, usize, usize, usize)> = None;
    for span in low..=high {
        if start + span > haystack.len() {
            continue;
        }
        let Some((distance, subs, ins, dels)) =
            edit_distance(primer, &haystack[start..start + span], budget)
        else {
            continue;
        };
        let shift = span.abs_diff(length);
        let candidate = (distance, shift, span, subs, ins, dels);
        match best {
            None => best = Some(candidate),
            Some(current) if (distance, shift) < (current.0, current.1) => best = Some(candidate),
            _ => {}
        }
    }
    best.map(|(distance, _, span, subs, ins, dels)| (distance, span, subs, ins, dels))
}

/// A primer site search against one contig (the q-gram index lives here).
#[pyclass]
struct SiteScanner {
    haystack: Vec<u8>,
    index: QGramIndex,
}

#[pymethods]
impl SiteScanner {
    #[new]
    fn new(haystack: &[u8]) -> Self {
        SiteScanner {
            haystack: haystack.to_vec(),
            index: QGramIndex::build(haystack),
        }
    }

    /// Sites of `primer` in the contig.
    ///
    /// Returns a list of `(position, substitutions, span, insertions, deletions)`.
    /// `max_mismatches` bounds the substitution-only pass; when it finds nothing
    /// and `max_edits > 0`, the gapped pass runs with `max_edits` as the budget.
    #[pyo3(signature = (primer, max_mismatches, max_edits))]
    fn sites(
        &self,
        primer: &[u8],
        max_mismatches: usize,
        max_edits: usize,
    ) -> Vec<(usize, usize, usize, usize, usize)> {
        let length = primer.len();
        if length == 0 || self.haystack.len() < length {
            return Vec::new();
        }
        let hamming = self.hamming_sites(primer, max_mismatches);
        if !hamming.is_empty() || max_edits == 0 {
            return hamming;
        }
        self.gapped_sites(primer, max_edits)
    }

    fn hamming_sites(&self, primer: &[u8], max_mismatches: usize) -> Vec<(usize, usize, usize, usize, usize)> {
        let length = primer.len();
        if length < Q {
            return Vec::new();
        }
        let seeds = length / Q; // disjoint seeds, at least one survives `seeds-1` mismatches
        let seed_count = seeds.max(1);
        let mut candidates: Vec<usize> = Vec::new();
        for seed_index in 0..seed_count {
            let offset = seed_index * (length - Q) / seed_count.max(1);
            let Some(code) = encode_qmer(primer, offset) else {
                continue;
            };
            let span = self.haystack.len() - length;
            self.index.for_each(code, |position| {
                if position >= offset && position - offset <= span {
                    candidates.push(position - offset);
                }
            });
        }
        candidates.sort_unstable();
        candidates.dedup();
        let mut found: Vec<(usize, usize, usize, usize, usize)> = Vec::new();
        for start in candidates {
            let mut substitutions = 0usize;
            let mut over = false;
            for offset in 0..length {
                if primer[offset] != self.haystack[start + offset] {
                    substitutions += 1;
                    if substitutions > max_mismatches {
                        over = true;
                        break;
                    }
                }
            }
            if !over {
                found.push((start, substitutions, length, 0, 0));
            }
        }
        found
    }

    fn gapped_sites(&self, primer: &[u8], max_edits: usize) -> Vec<(usize, usize, usize, usize, usize)> {
        let length = primer.len();
        let span_limit = self.haystack.len().saturating_sub(length.saturating_sub(max_edits));
        if span_limit == 0 {
            return Vec::new();
        }
        let mut candidates: Vec<usize> = Vec::new();
        let seed_length = Q.min(length);
        let mut offset = 0usize;
        while offset + seed_length <= length {
            if let Some(code) = encode_qmer(primer, offset) {
                self.index.for_each(code, |position| {
                    for shift in 0..=(2 * max_edits) {
                        if let Some(start) = (position + shift).checked_sub(offset) {
                            if start < span_limit {
                                candidates.push(start);
                            }
                        }
                    }
                });
            }
            offset += seed_length;
        }
        candidates.sort_unstable();
        candidates.dedup();
        let mut found: Vec<(usize, usize, usize, usize, usize)> = Vec::new();
        for start in candidates {
            if let Some((_, span, subs, ins, dels)) = best_span(primer, &self.haystack, start, max_edits)
            {
                found.push((start, subs, span, ins, dels));
            }
        }
        // One site per cluster: a deletion lets the primer align at neighbouring
        // offsets; keep the fewest-edits site and drop overlapping ones.
        found.sort_by_key(|site| (site.1 + site.3 + site.4, site.0));
        let mut kept: Vec<(usize, usize, usize, usize, usize)> = Vec::new();
        for site in found {
            let overlaps = kept
                .iter()
                .any(|other| site.0 < other.0 + other.2 && other.0 < site.0 + site.2);
            if !overlaps {
                kept.push(site);
            }
        }
        kept.sort_by_key(|site| site.0);
        kept
    }

    /// 2-bit reverse complement, so callers can scan both strands with the same
    /// primer patterns.
    #[staticmethod]
    fn reverse_complement<'py>(py: Python<'py>, sequence: &[u8]) -> Bound<'py, PyBytes> {
        let complement: Vec<u8> = sequence
            .iter()
            .rev()
            .map(|base| match base {
                b'A' => b'T',
                b'T' => b'A',
                b'C' => b'G',
                b'G' => b'C',
                other => *other,
            })
            .collect();
        PyBytes::new(py, &complement)
    }
}

#[pymodule]
fn _ispcr_fast(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SiteScanner>()?;
    module.add("__doc__", "Optional Rust accelerator for gseek.ispcr")?;
    Ok(())
}
