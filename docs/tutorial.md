# gseek tutorial

**English** | [简体中文](https://indexofire.github.io/gseek/zh/tutorial/)

This tutorial walks from a first alignment to reproducible runs and custom
integration. It is written for Python users who already have a basic
bioinformatics environment but have not used gseek before.

## 1. What gseek is (and is not)

`gseek` is a Python library that:

- plans and runs external alignment/distance tools with an argv list (never a shell);
- normalizes their output into `AlignmentRecord` / `DistanceRecord`;
- manages content-addressed indexes;
- records reproducible run manifests and can replay them;
- provides a `legacy | gseek | shadow` switch for consumers.

It does **not** decide whether an allele is valid, whether a species call is
correct, or which phenotype a genome has. Those are consumer rules.

```text
your pipeline
    │  request (query, target, params)
    ▼
 gseek runner
    │  probe → index → plan → execute → parse
    ▼
 records + provenance (and optionally a manifest)
```

## 2. Install and check tools

Python **3.12+** is required:

```bash
pip install gseek==0.10.2
```

External tools are not Python dependencies. Install the ones you need:

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# or
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

Check the environment:

```bash
gseek --version
gseek backends
gseek doctor
```

`gseek doctor` exits 1 if a required tool is missing. You can also point to a
tool explicitly:

```bash
export GSEEK_BLASTN=/opt/bio/bin/blastn
```

## 3. The mental model

### Request

An alignment request has a query, one or more targets, an adapter, and
adapter-specific parameters. A distance request uses a reference instead of a
target.

### Result

An `AlignmentResult` contains:

- normalized records (coordinates are 0-based half-open);
- inputs/targets and their hashes;
- the adapter and tool versions;
- optional index and manifest references;
- aggregate statistics.

### Index

An index is keyed by the adapter version, semantic parameters, target content
hash, and tool version. `index="auto"` reuses a matching index or builds one.
`index="none"` skips a persistent index where the adapter supports subject mode.

### Manifest

A manifest captures what happened: inputs, tools, index, commands, parameters,
result hash, and an allowlisted environment. `verify` checks recorded files;
`replay` re-plans through the adapter and can compare the result hash.

## 4. First alignment

Suppose you have a sample assembly and a target FASTA:

```text
sample.fna
scheme/alleles.fna
```

```python
from pathlib import Path

import gseek

result = gseek.align(
    backend="minimap2",
    query=Path("sample.fna"),
    target=[Path("scheme/alleles.fna")],
    preset="asm5",
    threads=8,
    index="auto",
    manifest=Path("runs/sample.manifest.json"),
)

print("records:", len(result.records))
print("backend:", result.backend)
print("index key:", result.index.key if result.index else None)
```

What happens internally:

1. gseek detects FASTA input.
2. It probes `minimap2` and selects the adapter.
3. `index="auto"` resolves the content-addressed target index.
4. The adapter plans the command; the executor runs it.
5. The parser emits normalized `AlignmentRecord`s.
6. The result is sorted deterministically.
7. A manifest is written if requested.

## 5. Inspect records and views

```python
for hit in result.records[:5]:
    print(
        hit.query_id,
        hit.target_id,
        hit.identity,
        hit.alignment_length,
        hit.target_coverage,
        hit.strand,
    )

best = result.best_per_query_target()
primary = result.primary_only()
summary = result.summarize_by_target()
```

Important conventions:

- Coordinates are 0-based half-open on the forward sequence.
- `strand` is separate (`+` / `-`).
- `identity` is a percentage; coverage is a fraction.
- `tags` keeps tool-specific evidence such as `NM` or `identity_method`.

Views are explicit and never delete the original records. If you need a
different selection rule, filter `result.records` yourself.

## 6. FASTQ and paired reads

```python
result = gseek.align(
    backend="kma",
    query=(Path("sample_R1.fq"), Path("sample_R2.fq")),
    target=[Path("scheme/alleles.fna")],
    paired=True,
    threads=8,
    index="auto",
)
```

Only adapters that support FASTQ/paired input are accepted; otherwise gseek
raises `UnsupportedFeatureError` before running a tool. KMA reports depth but no
coordinates; do not assume alignment coordinates exist for every backend.

## 7. Index policies

```python
gseek.align(..., index="auto")     # reuse or build
gseek.align(..., index="reuse")    # require a cache hit
gseek.align(..., index="rebuild")  # invalidate and rebuild
gseek.align(..., index="none")     # no persistent index
```

Inspect the cache:

```bash
gseek index list
gseek index gc            # dry run
gseek index gc --apply    # delete unreferenced entries
```

Do not key indexes by mtime or by manually assembled paths; let gseek compute
the key from content and semantic parameters.

## 8. Distance and ANI

```python
distance = gseek.distance(
    backend="mash",
    query=Path("assembly.fna"),
    reference=Path("refs.msh"),
    external_reference=True,
    params={"max_distance": 1.0},
    manifest=Path("runs/mash.manifest.json"),
)

for row in distance.records:
    print(row.query_id, row.reference_id, row.distance, row.pvalue)
```

`external_reference=True` is for a prebuilt sketch/database (`.msh`,
sourmash `.sig`/rocksdb, skani database). Without it, gseek builds the
reference index/sketch from FASTA.

## 9. Manifests, verify, and replay

Write a manifest:

```python
gseek.align(..., manifest=Path("runs/sample.manifest.json"))
```

Verify it:

```bash
gseek verify runs/sample.manifest.json
```

Replay with a result-hash check:

```bash
gseek replay runs/sample.manifest.json --check --json
```

Replay does not execute stored argv. It asks the adapter to plan again from the
manifest's inputs/parameters, then compares the normalized result hash. A
mismatch means something changed (inputs, tool version, environment, or
adapter behavior); investigate rather than ignoring it.

Python equivalents:

```python
issues = gseek.verify("runs/sample.manifest.json")
replayed = gseek.replay("runs/sample.manifest.json", check=True)
```

## 10. Errors

Catch the base class and branch on stable codes:

```python
from gseek import GseekError

try:
    result = gseek.align(...)
except GseekError as exc:
    print(exc.code, exc.exit_code)
    print(exc.message)
    print(exc.context)
    print(exc.hint)
```

See the [error model](errors.md) for the full table. Do not parse exception
messages to decide whether to retry or install a tool.

## 11. CLI equivalent

The CLI is useful for pipelines and debugging:

```bash
gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --threads 8 \
  --index auto \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json
```

Results go to stdout (or `-o`); errors go to stderr as `gseek.error/1` JSON.
Exit codes: `0` success, `2` usage, `3` tool, `4` execution/index, `5`
input/parse, `6` replay mismatch. See the [CLI reference](cli.md).

## 12. Consumer compatibility (`legacy | gseek | shadow`)

If you maintain a legacy alignment path, use `gseek.compat.Engine`:

```python
from gseek.compat import Engine

switch = Engine.from_env("MYTOOL_ENGINE")

result = switch.run(
    legacy=lambda: run_legacy(request),
    gseek=lambda: run_with_gseek(request),
    compare=lambda old, new: old == new,
)
```

- `legacy` (default): only the existing implementation.
- `gseek`: only the new path; errors propagate.
- `shadow`: both paths, compare/log differences, return legacy; a gseek or
  comparator failure falls back to legacy.

The consumer owns thresholds, business rules, and the legacy implementation.

## 13. Production checklist

1. Pin gseek and external tool versions.
2. Always write a manifest for production runs.
3. Keep index/cache roots on fast local disk; use `gseek index gc` regularly.
4. Run `gseek doctor` in CI or at pipeline start.
5. Treat replay mismatches as reproducibility incidents.
6. Parallelize at the sample level; set `threads=` per tool deliberately.
7. Keep consumer business rules out of adapters.

## 14. Where to go next

- [Quickstart](quickstart.md) — minimal examples.
- [API reference](api.md) — every public function and model.
- [CLI reference](cli.md) — command and automation contract.
- [Error model](errors.md) — exception/exit-code handling.
- [Adapter matrix](https://github.com/indexofire/gseek/blob/main/dev/adapter-matrix.md) —
  backend-specific parameters and quirks.
- [Compatibility](https://github.com/indexofire/gseek/blob/main/dev/compatibility.md) —
  field mapping and shadow migration.
