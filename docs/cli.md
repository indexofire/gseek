# CLI reference

**English** | [简体中文](https://indexofire.github.io/gseek/zh/cli/)

The CLI is a thin wrapper over the public API: argument parsing, JSON output,
and exit-code mapping. It never executes a shell string. It is designed for CI,
Snakemake/Nextflow, and interactive debugging. The full option matrix is in the
[API reference §10](api.md#10-cli).

## Global options

```bash
gseek --version
gseek --error-format {json,text} <command> ...
```

| Option | Meaning |
| --- | --- |
| `--version` | print `gseek 0.10.3` |
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
# FASTA reference: gseek builds or reuses an index
gseek distance --backend sourmash --query genome.fna --reference refs/ -o out.json

# prebuilt mash sketch
gseek distance \
  --backend mash \
  --query assembly.fna \
  --reference refs.msh \
  --external-reference \
  --param max_distance=1.0
```

`--external-reference` means the reference is already a sketch or database — a
single `.msh` / `.sig` / `.zip`, a sourmash rocksdb directory, or a skani
database directory — rather than FASTA to be indexed. gseek does not re-sketch
it; directories are recorded by a deterministic tree hash.

## Machine-readable output

- Results: stdout, one JSON document; `-o` writes a file instead.
- Errors: stderr, `gseek.error/1` JSON by default.
- `--json` on informational commands (`backends`, `doctor`, `index list`,
  `index gc`, `verify`, `replay`) emits a JSON document instead of text.
- Progress and notices never contaminate stdout.

Examples:

```bash
gseek backends --json | jq '.[].name'
gseek doctor --json | jq '.[] | select(.ok == false)'
```

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
| `130` | interrupted by the user |

## Automation notes

1. Do not parse human-readable output; use `--json` or the `align`/`distance`
   JSON document.
2. On failure, read the process exit code first, then parse the
   `gseek.error/1` JSON on stderr.
3. `gseek replay --check` is safe for untrusted manifests: it re-plans through
   adapters instead of executing recorded argv.
4. Concurrent calls share index file locks; run `gseek index gc --apply` when
   no build is in flight.
5. External tools must be discoverable via `GSEEK_<BINARY>`,
   `GSEEK_PATH_PREPEND`, or `PATH`; run `gseek doctor` first.
