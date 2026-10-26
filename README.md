# gseek

[![PyPI](https://img.shields.io/pypi/v/gseek.svg)](https://pypi.org/project/gseek/)
[![CI](https://github.com/indexofire/gseek/actions/workflows/ci.yml/badge.svg)](https://github.com/indexofire/gseek/actions)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

**English** | [简体中文](README.zh.md)

`gseek` is a zero-runtime-dependency Python library that puts a stable,
reproducible interface around DNA alignment and distance tools. It normalizes
requests and results for BLAST+, minimap2, MUMmer4, KMA, BWA, mash, sourmash,
and skani, with content-addressed indexes and replayable run manifests.

It is the shared alignment layer for [gside](https://github.com/indexofire/gside),
[gapit](https://github.com/indexofire/gapit), and
[gmlst](https://github.com/indexofire/gmlst). Adapters only plan commands and
normalize output; biological interpretation stays in the consumer.

```text
consumer (gside / gapit / gmlst)
              │
              ▼
      gseek.api: align / distance / backends
              │
              ▼
 runner: probe → index → plan → execute → parse → manifest
              │
              ▼
 adapters: blast · minimap2 · mummer · kma · bwa · mash · sourmash · skani
```

## Why gseek

Bioinformatics tools solve similar problems with incompatible conventions:

| Concern | Typical consumer code | gseek |
| --- | --- | --- |
| BLAST columns | different `outfmt` per project | one `AlignmentRecord` |
| Coordinates | 1-based inclusive or 0-based half-open | 0-based half-open + strand |
| Identity / coverage | tool-specific formulas | normalized values + source |
| Index reuse | mtime / file existence | content-addressed keys + locks |
| Reproducibility | partial version logging | `gseek.run/1` manifest + replay |

The library does not decide whether a hit is "good"; it preserves the evidence
and lets the consumer apply thresholds and business rules.

## Status

`0.10.1`; Python `>=3.12`; runtime dependencies: **none**.

| Backend | Input | Operations | Index |
| --- | --- | --- | --- |
| `blast` | FASTA (DNA/protein tasks) | alignment | target |
| `minimap2` | FASTA / FASTQ / paired | alignment | target |
| `kma` | FASTA / FASTQ / paired | alignment | target |
| `mummer` | FASTA | alignment (MUMmer 4) | none |
| `bwa` | FASTA / FASTQ / paired | alignment | target |
| `mash` | FASTA / FASTQ | distance / screen | sketch |
| `sourmash` | FASTA / FASTQ | distance / search / gather | sketch |
| `skani` | FASTA | ANI distance | sketch |

External tools are installed separately (conda, Bioconda, pixi, or system
packages); `gseek` discovers them at runtime and reports actionable errors when
one is missing or too old.

## Installation

```bash
pip install gseek==0.10.1
```

From source:

```bash
pip install -e ".[dev]"
pixi install
```

External tools:

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# or
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

The package itself has no third-party runtime dependencies.

## Python quickstart

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

print(result.stats.record_count)
for hit in result.records:
    print(hit.query_id, hit.target_id, hit.identity, hit.target_coverage)

best = result.best_per_query_target()
```

Inspect capabilities and tools:

```python
gseek.backends()
gseek.get_backend("blast").probe()
gseek.align(...)
gseek.distance(...)
gseek.replay(Path("run.manifest.json"), check=True)
gseek.verify(Path("run.manifest.json"))
```

### Distance and ANI

```python
result = gseek.distance(
    backend="mash",
    query=Path("assembly.fna"),
    reference=Path("refs.msh"),
    external_reference=True,
    params={"max_distance": 1.0},
    manifest=Path("runs/mash.manifest.json"),
)
```

`distance` supports mash, sourmash, and skani. `external_reference=True` means
the reference is already a sketch/database rather than FASTA to be indexed.

## Result model

`gseek.align()` returns an `AlignmentResult` containing normalized
`AlignmentRecord`s, the index/tool references used, and optional manifest data.

Key provenance fields:

- `query_id`, `target_id`
- `query_start` / `query_end`, `target_start` / `target_end` (0-based half-open)
- `strand`
- `identity`, `aligned_length`, `matches`, `gap_count`, `gap_openings`
- `target_coverage`, `query_coverage`
- `evalue`, `bit_score`, `mapq`, `depth`
- `tags` for tool-specific values (`NM`, `cs`, …)

`AlignmentResult` also provides explicit views such as
`best_per_query_target()`, `summarize_by_target()`, and
`iter_records()`. Views never discard the original records.

`gseek.distance()` returns a `DistanceResult`; distance records keep the query,
reference, distance, and p-value/ANI fields appropriate to the backend.

## CLI

The CLI is a thin wrapper over the public API: argv parsing, JSON output, and
exit-code mapping. It never executes a shell string.

```bash
gseek doctor
gseek backends --json

gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --threads 8 \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json

gseek verify runs/sample.manifest.json
gseek replay runs/sample.manifest.json --check --json
```

Results go to stdout (or `-o`), errors to stderr as `gseek.error/1` JSON by
default. Use `--error-format text` for humans. Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | success |
| `2` | usage/configuration error |
| `3` | missing or unsupported tool |
| `4` | execution/index/manifest error |
| `5` | input/parse error |
| `6` | replay hash mismatch |

## Reproducibility

- Indexes are content-addressed: adapter version, semantic parameters, target
  content hash, and tool version all participate in the key.
- `gseek.run/1` manifests record inputs, tool versions, planned commands, and a
  canonical recipe/result hash.
- `gseek verify` checks recorded files and hashes.
- `gseek replay` re-plans from the manifest and can compare the new
  `result_hash`; it does **not** re-execute arbitrary argv stored in JSON.

## Consumer compatibility

`gseek.compat` centralizes the `legacy | gseek | shadow` switch used by
consumers:

```python
from gseek.compat import Engine

switch = Engine.from_env("GSIDE_ENGINE")
result = switch.run(legacy_call, gseek_call, compare=compare)
```

- `legacy` (default): only the consumer's existing implementation.
- `gseek`: only the gseek path; failures propagate.
- `shadow`: run both, compare/log, and return the legacy result.

The consumer keeps its own business rules and legacy code; `gseek.compat`
only owns mode parsing, shadow execution, and fallback.

## Documentation

- [Documentation site](https://indexofire.github.io/gseek/)
- [Documentation index](docs/index.md)
- [Tutorial](docs/tutorial.md)
- [Quickstart](docs/quickstart.md)
- [CLI reference](docs/cli.md)
- [Error model](docs/errors.md)
- [API reference](docs/api.md)
- [中文 README](README.zh.md)
- [中文文档](docs/index.zh.md)

Development documents:

- [Development plan](dev/development-plan.md)
- [Adapter matrix](dev/adapter-matrix.md)
- [Compatibility and migration](dev/compatibility.md)
- [Testing and contribution rules](AGENTS.md)
- [Changelog](CHANGELOG.md)

## Development

The project enforces test-first development.

```bash
pixi install
pixi run test          # unit and contract tests
pixi run test-cov      # coverage gate
pixi run lint          # ruff check + format --check
pixi run typecheck     # pyright
pixi run check         # lint + typecheck + test
```

Integration tests need the external tools and are skipped when a binary is
absent. The test suite currently passes **350 tests** (36 skipped integration
cases without optional tools) and covers adapter planning/parsing, execution,
indexing, manifests, replay, and consumer parity.

## Design principles

1. Adapters are pure planners/parsers; only `execution.py` starts processes.
2. No information loss: merging is an explicit view.
3. Coordinates are 0-based half-open; strand is a separate field.
4. Identity/coverage carry their source and formula.
5. Indexes are content-addressed; no mtime decisions.
6. Deterministic defaults: stable ordering, absolute paths, `LC_ALL=C`, no shell.
7. Zero runtime dependencies; typed errors with stable codes.
8. Docs and schemas are contracts and evolve with the code.

## License

MIT. See [LICENSE](LICENSE).
