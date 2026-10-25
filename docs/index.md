# gseek documentation

**English** | [简体中文](https://indexofire.github.io/gseek/zh/)

`gseek` is a zero-runtime-dependency Python library for reproducible DNA
alignment and distance workflows. It wraps BLAST+, minimap2, MUMmer4, KMA, BWA,
mash, sourmash, and skani behind one request/result model, with content-addressed
indexes and replayable run manifests.

## Start here

| I want to… | Read |
| --- | --- |
| Install and run a first alignment | [Quickstart](quickstart.md) |
| Follow a complete walkthrough | [Tutorial](tutorial.md) |
| Use the Python API | [API reference](api.md) |
| Script/automate the CLI | [CLI reference](cli.md) |
| Handle failures by code | [Error model](errors.md) |
| Understand reproducibility | [API: manifests and replay](api.md#7-reproducibility-and-manifests) |
| Add a custom backend | [API: adapter protocol](api.md#4-adapter-api) |
| Migrate a consumer safely | [Compatibility](https://github.com/indexofire/gseek/blob/main/dev/compatibility.md) |

## Core idea

```text
consumers: gside / gapit / gmlst / your pipeline
                  │
                  ▼
        gseek.align / gseek.distance
                  │
                  ▼
  probe → index → plan → execute → parse → manifest
                  │
                  ▼
      normalized records + provenance
```

Adapters are pure planners and parsers. Only `gseek.execution` starts
processes. Business decisions such as allele calling, species thresholds, or
phenotype rules belong in the consumer.

## Install

```bash
pip install gseek==0.9.2
```

External tools are not bundled:

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
```

Check the environment:

```bash
gseek doctor
gseek backends
```

## First Python call

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

for hit in result.records:
    print(hit.query_id, hit.target_id, hit.identity)
```

## First CLI call

```bash
gseek align \
  --backend minimap2 \
  --query sample.fna \
  --target scheme/alleles.fna \
  --preset asm5 \
  --manifest runs/sample.manifest.json \
  -o runs/sample.json
```

## Documentation map

- [Quickstart](quickstart.md) — install, first run, verify/replay.
- [API reference](api.md) — public functions, result models, adapters,
  execution, indexing, manifests, schemas.
- [CLI reference](cli.md) — command matrix, output, exit codes.
- [Error model](errors.md) — exception hierarchy and JSON envelope.

Development material lives outside the user docs:

- [Development plan](https://github.com/indexofire/gseek/blob/main/dev/development-plan.md)
- [Adapter matrix](https://github.com/indexofire/gseek/blob/main/dev/adapter-matrix.md)
- [Compatibility and migration](https://github.com/indexofire/gseek/blob/main/dev/compatibility.md)
- [AGENTS.md](https://github.com/indexofire/gseek/blob/main/AGENTS.md) — mandatory TDD and architecture rules.
