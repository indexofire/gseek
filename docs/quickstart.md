# Quickstart

**English** | [简体中文](https://indexofire.github.io/gseek/zh/quickstart/)

Goal: run one alignment and produce a manifest that can be verified and replayed.

## 1. Requirements

- Python **>=3.12**
- External tools installed separately:

```bash
pixi add blast minimap2 mummer4 kma bwa mash sourmash skani
# or
conda install -c bioconda blast minimap2 mummer4 kma bwa mash sourmash skani
```

`gseek` itself has no third-party runtime dependencies.

## 2. Install

```bash
pip install gseek==0.9.2
```

From source:

```bash
pip install -e ".[dev]"
# or inject the source tree
export PYTHONPATH=/path/to/gseek/src
```

Verify:

```bash
gseek --version          # gseek 0.9.2
gseek backends           # capability matrix
gseek doctor             # external tool probe; exits 1 if a tool is missing
```

## 3. First CLI alignment

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

What the flags mean:

- `--index auto`: reuse a matching content-addressed index, or build one.
- `--manifest`: write `gseek.run/1` with input hashes, tool versions, planned
  commands, and result hashes.
- `-o`: write result JSON; without it, JSON goes to stdout.

Exit codes: `0` success, `2` usage, `3` missing tool, `4` execution/index, `5`
input/parse, `6` replay mismatch.

Inspect the result:

```bash
python -m json.tool runs/sample.json | head -40
```

## 4. First Python alignment

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

## 5. Distance and ANI

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

```bash
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0 \
  --manifest runs/mash.manifest.json
```

`sourmash` and `skani` follow the same pattern; see the
[API reference](api.md#22-gseekdistance) and [CLI reference](cli.md#distance).

## 6. Verify and replay

```bash
gseek verify runs/sample.manifest.json
gseek replay runs/sample.manifest.json --check --json
```

- `verify` checks the files and hashes recorded in the manifest.
- `replay` re-plans from the manifest and can compare the new `result_hash`.
- Replay never executes arbitrary argv strings stored in JSON; it asks the
  adapter to plan again.

## Next

- [API reference](api.md) — complete signatures and result models.
- [CLI reference](cli.md) — all commands and automation conventions.
- [Error model](errors.md) — typed errors and exit codes.
