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

PyPI release 0.10.3, a locally built wheel, or a source checkout:

```bash
pip install gseek==0.10.3

# or install the locally built wheel
uv pip install dist/gseek-0.10.3-py3-none-any.whl

# or run from a source checkout
pip install -e ".[dev]"
export PYTHONPATH=/path/to/gseek/src
```

Verify:

```bash
gseek --version          # gseek 0.10.3
gseek backends           # capability matrix, 8 backends
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
    external_reference=True,   # prebuilt .msh, not FASTA
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

`sourmash` also accepts a **rocksdb directory** as an external reference:

```bash
gseek distance \
  --backend sourmash \
  --query genome.fna \
  --reference refs.rocksdb \
  --external-reference \
  --param k=31 \
  --param scaled=1000
```

`sourmash` and `skani` follow the same pattern; see the
[API reference](api.md#24-gseekdistance) and [CLI reference](cli.md#distance).

## 6. Verify and replay

```bash
gseek verify runs/sample.manifest.json
gseek replay runs/sample.manifest.json --check --json
```

- `verify` checks the files and hashes recorded in the manifest; a mismatch
  exits `4`.
- `replay` re-plans from the manifest and can compare the new `result_hash`; a
  mismatch exits `6`.
- Replay never executes arbitrary argv strings stored in JSON; it asks the
  adapter to plan again.

## 7. Troubleshooting

Errors are one-line `gseek.error/1` JSON on stderr by default:

```json
{"schema":"gseek.error/1","code":"TOOL_NOT_FOUND","message":"...","context":{},"hint":"..."}
```

For a human-readable message:

```bash
gseek --error-format text align --backend blast --query q.fna --target t.fna
```

Library callers catch `gseek.GseekError` and read `code`, `context`, `hint`,
and `exit_code`. See the [error model](errors.md).

## 8. Next

- [CLI reference](cli.md) — JSON output, exit codes, automation conventions.
- [API reference](api.md) — result models, adapter protocol, indexes, schemas.
- [Adapter matrix](https://github.com/indexofire/gseek/blob/main/dev/adapter-matrix.md)
  — tool parameters and version floors.
- [Compatibility](https://github.com/indexofire/gseek/blob/main/dev/compatibility.md)
  — field differences for gside, gapit, and gmlst.
