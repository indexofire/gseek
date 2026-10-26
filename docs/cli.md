# CLI reference

**English** | [简体中文](https://indexofire.github.io/gseek/zh/cli/)

The CLI is a thin wrapper over the public API: argument parsing, JSON output,
and exit-code mapping. It never executes a shell string.

## Global options

```bash
gseek --version
gseek --error-format {json,text} <command> ...
```

| Option | Meaning |
| --- | --- |
| `--version` | print `gseek 0.10.1` |
| `--error-format json` | default; one-line `gseek.error/1` JSON on stderr |
| `--error-format text` | human-readable `gseek: message` plus `hint:` |

`--error-format` must appear before the subcommand.

## Commands

| Command | Purpose |
| --- | --- |
| `gseek backends [--json]` | capability matrix and adapter metadata |
| `gseek doctor [--json]` | probe external tools; exit 1 when something is missing |
| `gseek index list [--json]` | list content-addressed index entries |
| `gseek index gc [--apply] [--json]` | garbage-collect indexes; dry-run by default |
| `gseek manifest verify PATH [--json]` | verify hashes recorded in a manifest |
| `gseek verify PATH [--json]` | alias for the above |
| `gseek replay PATH [--check] [--json]` | re-plan and replay; never executes old argv |
| `gseek align ...` | align FASTA/FASTQ to target(s) |
| `gseek distance ...` | mash/sourmash/skani distance or ANI |

## align

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

Key options:

- `--query` / `--target` are repeatable; with `--paired`, pass two FASTQ
  queries.
- `--param KEY=VALUE` passes adapter parameters. Values are parsed as JSON
  first, then fall back to strings.
- `--index auto|reuse|rebuild|none`.
- `--manifest` writes `gseek.run/1`; `-o` writes result JSON (stdout by
  default).

## distance

```bash
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0 \
  --manifest runs/mash.manifest.json
```

`--external-reference` means the reference is already a sketch/database (for
example a `.msh` file or a sourmash rocksdb directory) rather than FASTA to be
indexed. Omit it when gseek should build the reference itself.

## Machine-readable output

- Results: stdout, one JSON document; `-o` writes a file instead.
- Errors: stderr, `gseek.error/1` JSON by default.
- `--json` on informational commands (`backends`, `doctor`, `index list`,
  `index gc`, `verify`, `replay`) emits a JSON document instead of text.
- Progress and notices never contaminate stdout.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | generic/unclassified `GseekError` (for example `doctor` with a missing tool) |
| `2` | usage or configuration error |
| `3` | missing tool or unsupported tool version |
| `4` | execution, index, cache, or manifest error |
| `5` | input or parse error |
| `6` | replay `result_hash` mismatch |

## Automation notes

- Prefer explicit `--manifest` paths in pipelines; they are the reproducibility
  boundary.
- `gseek replay --check` is safe for untrusted manifests: it re-plans through
  adapters instead of executing recorded argv.
- `gseek index gc` is a dry run unless `--apply` is passed.
- `--error-format json` is stable for machine parsing; do not parse
  human-readable text.
