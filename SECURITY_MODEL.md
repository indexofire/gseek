# gseek security model

This document is gseek's threat model. Security scans and reviewers start from it. It was written from the code (as of 0.10.5). Until the maintainers say otherwise, **every attacker below is in scope** and nothing is ruled out or accepted as risk. Edit sections 5, 7 and 8 as those decisions are made.

## 1. System overview and trust boundaries

gseek is a zero-runtime-dependency Python library (`src/gseek`) and CLI (`gseek`). It wraps external DNA alignment and distance tools (BLAST+, minimap2, MUMmer4, KMA, BWA, mash, sourmash, skani, sylph) and in-silico PCR (`ispcr`, with an optional Rust accelerator in `rust/ispcr`). It runs inside the caller's process, under the caller's OS account. Nothing is deployed and there is no network listener. Its consumers are gside, gspec, gmlst, and through them bacmap, which has a Web/MCP console.

Trust boundaries:
- **Caller → gseek API/CLI**: request params, input paths, manifest paths, primer tables, output paths.
- **Files → parsers**: sequence files (FASTA/FASTQ, gzip/xz), primer tables (TSV/JSON), run manifests (JSON), and the external tools' output files (TSV/PAF/SAM/CSV/coords), which gseek parses.
- **gseek → subprocess**: argv built by adapters, environment, and the resolved binary path (`execution/`).
- **gseek ↔ filesystem cache**: the index cache under `GSEEK_CACHE_DIR` / `$XDG_CACHE_HOME/gseek` / `~/.cache/gseek`, plus run workspaces under `GSEEK_TMPDIR` or the system temp. These may be shared with other local users on a multi-user host (HPC).
- **Environment → tool resolution**: `GSEEK_<BINARY>`, `GSEEK_PATH_PREPEND`, `PATH`, `GSEEK_MAX_DECOMPRESSED_BYTES`, `GSEEK_INDEX_*`, `GSEEK_ENGINE`-style switches (`compat.py`).

## 2. Components

| Component | Code | Notes |
| --- | --- | --- |
| CLI | `src/gseek/cli.py`, `__main__.py` | argparse front end; writes `--output` files |
| API & Runner | `api.py`, `runner.py`, `batch.py`, `adapter.py`, `registry.py`, `capabilities.py`, `views.py`, `compat.py`, `types.py`, `errors.py` | orchestration: probe → index → plan → execute → parse → manifest; registry loads `gseek.adapters` entry points |
| Adapters | `src/gseek/adapters/*` | build argv from request params; parse tool output |
| Execution | `src/gseek/execution/*` | the only subprocess layer (no shell), tool resolution, environment, workspaces, bounded capture, timeouts |
| Index cache | `index.py` | content-addressed index store, locks/leases, staging/publish, gc, symlink checks, 0700/0600 modes |
| Provenance & Replay | `provenance.py`, `replay.py`, `model/run.py` | manifest read/write/verify, hashing, replay re-planning, `--strict-paths` |
| Input parsing | `io.py`, `model/*` | FASTA/FASTQ/decompression with a size cap (64x / `GSEEK_MAX_DECOMPRESSED_BYTES`) |
| ispcr | `ispcr.py`, `rust/ispcr` | primer table parsing, site scanning, optional native accelerator |

## 3. Entry points and who can reach them

- Python API (`gseek.api.align/distance/replay/verify/backends`, `ispcr`, `batch.align_many`): any code that imports gseek, including consumer services that may pass through data from their own users.
- CLI subcommands: `align`, `distance`, `replay`, `verify`/`manifest verify`, `index list|gc`, `ispcr`, `doctor`, `backends`: whoever runs the CLI.
- Indirect: files on disk (inputs, manifests, cache entries) that another party may have produced or can modify.

## 4. Assets and sensitivity

- Integrity of the caller's account: no arbitrary command execution, and no file writes or deletes outside the workspace, cache or requested output.
- Integrity of results: alignment/distance records and `result_hash` that consumers (gspec/gmlst/bacmap) treat as evidence. A poisoned cache or a parsing bug can silently change downstream calls.
- Confidentiality of files readable by the caller: replay/verify read and hash paths named in a manifest. Manifests record absolute paths, argv, stderr tails and allowlisted env vars.
- Genomic data, which may include pathogen isolates in consumer deployments: moderate sensitivity.
- Availability: long or huge runs, decompression bombs, lock starvation.

## 5. Attacker profiles (all in scope by default)

1. **Supplier of untrusted input files**: crafted FASTA/FASTQ/gzip or primer tables, reaching gseek through a consumer service or a downloaded dataset.
2. **Supplier of a crafted run manifest** passed to `replay`/`verify`: path traversal, reading or hashing arbitrary files, forcing unexpected tool execution or params.
3. **Untrusted request params** passed by a consumer: argv/flag injection into tool commands, output paths escaping the workspace.
4. **Co-tenant local user** on a shared host: cache/temp dir races, symlink attacks, index poisoning in a shared `GSEEK_CACHE_DIR`.
5. **Environment controller**: anyone who sets `GSEEK_<BINARY>`, `GSEEK_PATH_PREPEND`, `PATH`. Usually equivalent to code execution already.
6. **Malicious tool output**, from a compromised or buggy external binary or a tampered index: parser robustness.

## 6. Existing controls

- No `shell=True`; argv is passed as a list (`execution/executor.py`, AGENTS.md section 9).
- Replay never executes recorded argv; it re-plans through the adapter (`replay.py`). `--strict-paths` confines paths to `base_dir`; `validate_path_text`.
- Workspace path checks: `checked_workspace_parts` rejects absolute paths and `..` (`execution/workspace.py`).
- Index cache: strict key/adapter-name validation, symlink checks on lookup/staging/gc, `_resolved_within`, private modes, atomic publish via `os.replace`, leases and locks (`index.py`). Regression tests are in `tests/test_security_hardening.py`.
- Manifests are written 0600 and size-capped on read (`provenance.py`). The manifest env is an allowlist (`execution/command.py`).
- Bounded stdout/stderr capture and timeouts (`execution/executor.py`), and a decompression cap (`io.py`).

## 7. Out of scope / accepted risk

None stated yet.

## 8. Open questions

- Do consumer services pass untrusted users' files, params or manifests into gseek, or is it only used by trusted users on their own data?
- Which inputs can be untrusted: sequence files, manifests, primer tables?
- Are environment-variable controllers, co-tenant local users, DoS, or bugs inside the external tools out of scope?
- No infrastructure repository was used, because gseek is a library. If a consumer deployment (e.g. bacmap) matters, its threat model is in the `bacmap threat model` profile.
