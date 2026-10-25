# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.10.0] - 2026-10-25

### Fixed
- `skani dist` (no index) with multiple query files silently treated the extra
  queries as references; it now raises `UnsupportedFeatureError` (indexed
  `search` mode still accepts multiple queries).
- BLAST parsing no longer guesses the `sseq` column from the field count: an
  unescaped tab in `stitle` could leak title text into `target_aligned` when
  `sseq` was not requested. The layout now follows `params={"sseq": ...}`.
- `sourmash search`/`gather` with zero matches write a 0-byte CSV (exit 0);
  this is now parsed as an empty result instead of raising `ParseError`.
- `run_distance` now raises `ParseError` when a distance adapter yields
  non-`DistanceRecord` values instead of silently dropping them (matching the
  alignment branch).
- A corrupted index-cache manifest (non-iterable or absolute-path artifact
  patterns) no longer crashes `IndexStore.lookup` with
  `NotImplementedError`/`TypeError`; it degrades to a cache miss.
- `Executor.run` now kills and reaps the child when `communicate()` fails for
  a non-timeout reason (decode errors, interrupts) instead of leaking an
  orphaned process.
- Streaming commands whose child closes stdout early but keeps finalizing are
  no longer SIGKILLed into a bogus exit code -9; `close()` waits a grace
  period before killing.
- `AlignmentRecord`/`DistanceRecord` reject NaN/Infinity float fields with
  `InputError` at construction instead of crashing canonical-JSON hashing
  later.
- BWA supplementary records with hard-clipped CIGARs (`5H50M15H`) now report
  `query_length` and query coordinates in the original read's coordinate
  system (offset by hard clips, both strands).
- minimap2 PAF rows carrying an `SA` tag now set `is_supplementary` (mirroring
  bwa's SAM `0x800` semantics).
- `merge_fasta` raises typed `InputError` (invalid width / unknown duplicate
  policy) and `views.to_tsv` raises typed `UsageError` for unknown columns,
  instead of bare `ValueError`s.
- `Executor.run`/`stream` decode child output with explicit
  `encoding="utf-8", errors="replace"`: invalid UTF-8 from a tool no longer
  crashes the parent (and orphaned the child) under non-coerced locales.
- `stream()` rejects `stdin` plans before slurping the stdin file, and a
  failure between `Popen` and `start()` (for example an unwritable stderr
  parent) now reaps the child instead of orphaning it; the `_kill` fallback
  no longer leaks `PermissionError` from cleanup paths.
- Stderr files redirected to disk are read as a bounded tail instead of
  loading the whole file before truncating.
- `IndexStore.gc` now also collects stale `.staging-*` directories from
  crashed builds (dead pid in the name) and removes the `.lock` file of every
  index it deletes instead of letting them accumulate.
- `IndexStore.lock` rejects keys that would escape the adapter cache
  directory, matching `path_for`.
- `RunWorkspace.path`/`PlanContext.path` reject absolute or `..` parts that
  would silently escape the workspace.
- `run_alignment`/`run_distance` validate `manifest_path`/`record_manifest`
  up front: config errors fail before any probe, index build, or command.
- Failed runs now leave an audit trail: when a run fails mid-flight the
  manifest is still written with `status=failed`, a `RUN_FAILED` issue, and
  the commands that actually executed (including the failing one).
- The published index manifest now records the exact params/tool versions the
  cache key was hashed from, instead of raw request params.
- `verify` reports an unreadable sketch directory as a `*_UNREADABLE` issue
  instead of crashing.
- `mash screen` with multiple query files is rejected (rows cannot be
  attributed); with `external_reference`, `k`/`sketch_size` tags are marked
  `unknown` instead of recording unverified request values.
- BLAST task binaries are gated per request via the new
  `BaseAdapter.validate_tools` hook: a missing or outdated optional binary
  (e.g. `blastx`, or `blastx-fast` needing BLAST+ >= 2.11) fails fast with an
  actionable error instead of at subprocess spawn.
- MUMmer's strict probe classifies missing vs broken `nucmer` structurally
  (path present) instead of sniffing words like "version" in the error text.
- `sourmash`/`kma` parsers reject a supplied `stream` explicitly instead of
  silently ignoring it; BWA rejects `algorithm` values that name alignment
  algorithms (`aln`/`bwasw`/`mem`) — it selects the index construction method.
- `Interval`, `FileRef`, `InputRef`, and `Issue` raise typed `InputError`
  instead of bare `ValueError`; `CommandRecord.from_dict` rejects unknown
  phases with `ManifestError`.
- Record `tags` (NM/identity_method provenance) now participate in record
  equality, consistent with `stable_key`; hashing still excludes them.
- `to_jsonable` decodes stray `bytes` values instead of crashing canonical
  JSON.

### Changed
- Shared adapter helpers `result_lines`/`float_or_none` moved to
  `gseek.adapters.base`, replacing five and three duplicated private copies
  across adapters; `float_or_none` also drops non-finite tool values.
- `gseek.execution` is now a package (`command`/`executor`/`tools`/`workspace`
  modules) and `gseek.model.alignment` is split into
  `alignment_record` + `alignment`; all public import paths are unchanged.
- `ParseContext.request` is typed as `AlignRequest | DistanceRequest`;
  parsers narrow explicitly instead of `type: ignore`.
- `gseek.index.directory_size` renamed to `tree_size` to stop shadowing the
  strict `provenance.directory_size`.
- Removed the unused public helpers `gseek.errors.fail` and
  `gseek.adapters.base.reverse_complement` (no callers in gseek or consumers).


### Added
- BLAST records optionally request `sseq` (`params={"sseq": True}`) and expose
  it as `AlignmentRecord.target_aligned`; default output is unchanged.
- minimap2 accepts `-e` and `-U` alignment flags (needed by gmlst speed profiles).

### Fixed
- KMA index parameter validation accepts the alignment-only `mem_mode` flag.

## [0.9.1] - 2026-10-25

### Added

- `gseek.compat`: shared `legacy`/`gseek`/`shadow` switch with shadow
  comparison, logging, and legacy fallback. Consumers keep their own legacy
  implementations; the module only removes duplicated selector/shadow code.

## [0.9.0] - 2026-10-25

### Changed

- With `record_manifest=False`, gseek now skips query record counting. Counting
  millions of FASTQ records only served manifest provenance and made gapit reads
  screening materially slower than the legacy path.

## [0.8.1] - 2026-10-25

### Fixed

- minimap2 target materialization now also allows duplicate subject IDs. VFDB
  reads screening failed before this; the BLAST fix in 0.8.0 covered only the
  BLAST index path.

## [0.8.0] - 2026-10-25

### Fixed

- BLAST target materialization now allows duplicate subject IDs. VFDB contains
  16 repeated IDs with distinct sequences; legacy BLAST databases accept them,
  and rejecting them broke a real consumer (gapit).

## [0.7.0] - 2026-10-25

### Added

- BLAST `blastx-fast` task support (gapit uses this faster protein task).

## [0.6.0] - 2026-10-24

### Added

- `record_manifest=False` on `gseek.align()` / `gseek.distance()`: skip run
  manifest construction and canonical result hashing for latency-sensitive
  consumers that do not need provenance. Default remains `True`; specifying a
  manifest path with `record_manifest=False` is rejected.

## [0.5.0] - 2026-10-24

### Added

- `external_sha256` / `external_size` on `DistanceRequest` and
  `gseek.distance()`: callers can pass a pre-computed reference digest and
  skip re-hashing large external databases. Replay reuses the digest recorded
  in the manifest.
- mash fast path: external reference + FASTA query + default sketch parameters
  runs `mash dist <ref> <query.fa>` directly, avoiding an extra query sketch
  subprocess.

## [0.4.0] - 2026-10-24

### Added

- skani external database directories. `gseek.distance(backend="skani",
  external_reference=True)` accepts a pre-built `skani sketch -o db` directory
  and runs `skani search -d <db> <query>`. Directory references reuse the
  deterministic tree hash, manifest verify, and replay support.

## [0.3.1] - 2026-10-24

### Fixed

- Refresh the PyPI README metadata to reflect that 0.3.x is published and to
  install `gseek==0.3.1`. No package code or API changes.

## [0.3.0] - 2026-10-24

### Added

- Directory external references for sourmash rocksdb databases. `gseek.distance(
  ..., external_reference=True)` now accepts a directory as well as a single
  `.msh`/`.sig`/`.zip` file.
- Deterministic directory tree hashing (`sha256_tree`): sorted relative paths,
  entry type, and regular-file content. Manifest `verify` and `replay` handle
  directory references; symlinks inside the tree are rejected.

## [0.2.0] - 2026-10-24

### Added

- **First PyPI release.**
- `gseek.distance(..., external_reference=True)` for pre-built distance
  artifacts: mash `.msh` and sourmash `.sig`/`.zip` are consumed directly,
  without index construction or reference re-sketching. External artifacts are
  recorded as `InputKind.SKETCH`; replay re-passes the flag.
- mash `reads=True` params append `-m 2 -r` to the query sketch for
  error-filtered FASTQ distance.
- sourmash gather/search parse `query_name` per row and expose
  `reference_name`, `reference_md5`, and `f_unique_weighted` tags.
- `gseek distance --external-reference` exposes the pre-built-artifact path in
  the CLI.
- `gseek --error-format {json,text}`; JSON remains the default for automation,
  text is available for humans.
- User-facing docs: `docs/index.md`, `docs/quickstart.md`, `docs/cli.md`,
  `docs/errors.md`, plus a relative-link regression test.
- `AGENTS.md`: mandatory TDD workflow, architecture boundaries, MUMmer4 policy.
- `CONTRIBUTING.md`, `CHANGELOG.md`, `.editorconfig`, `.pre-commit-config.yaml`.
- ADR-0001: target MUMmer **4.x**, not MUMmer3.
- ADR-0002: rename from `gcore` to `gseek`.
- Project identity tests (`tests/test_project_identity.py`) written before the
  rename, covering import name, schemas, env vars, and CLI.
- **M1 adapters**:
  - BLAST+ `blastn` adapter with `makeblastdb` target indexes, outfmt-6
    streaming parser, reverse-strand coordinates, gap accounting, target
    filtering, and `index_policy=none` subject mode.
  - minimap2 adapter with explicit presets, PAF streaming parser, FASTA/FASTQ,
    paired reads, primary/secondary filtering, `cg`-based gap counts, and
    target indexes.
  - Built-in adapter registry (`gseek.backends()` now lists `blast` and
    `minimap2`).
  - Prefix index entries (e.g. BLAST `targets` backed by `targets.nhr`) in
    `IndexStore`.
- **M2 adapters**:
  - KMA adapter with `kma index` target databases, `.res` parsing (no
    coordinates), assembly/FASTQ/paired modes, depth, and template-level
    identity/coverage.
  - MUMmer4 `nucmer` + `show-coords -rcl -T` adapter, multi-step execution,
    reverse-strand coordinates, no persistent index, and explicit MUMmer4
    version gate.
  - Built-in registry now lists `blast`, `minimap2`, `kma`, and `mummer`.
- **M3 adapters**:
  - BWA-MEM adapter with `bwa index` target prefixes, SAM/CIGAR streaming,
    reverse-strand query intervals, soft-clip accounting, paired reads, and
    secondary/supplementary flags.
  - mash adapter with `mash sketch`, `mash dist`, and reads `screen` mode;
    MinHash distance/containment records.
  - sourmash adapter with DNA sketches, `sourmash search` CSV, and
    Jaccard/containment semantics.
  - CIGAR helper now tracks leading/trailing soft and hard clips.
- **M4 features**:
  - BLAST protein tasks: `blastp`, `blastx`, `tblastn`, `tblastx`, with
    protein/nucleotide `makeblastdb` selection and SEG defaults.
  - sourmash `gather` mode with `--threshold-bp` and gather CSV parsing.
  - skani ANI adapter with sketch/search databases, ANI and aligned-fraction
    records.
  - Safe `gseek.replay` / `gseek replay` with `result_hash` in run manifests;
    replay never executes recorded argv.
- Unit/contract tests for all eight adapters, plus real-tool integration tests
  for index reuse, manifest verification, FASTQ mapping, thread invariance,
  KMA paired reads, MUMmer reverse-strand hits, BWA paired reads, mash
  dist/screen, sourmash search/gather, skani ANI, and manifest replay.

### Changed

- **Renamed the project from `gcore` to `gseek`.** PyPI's `gcore` is already
  owned by the Gcore Python API. Distribution, import package, CLI, environment
  variables (`GSEEK_*`), schemas (`gseek.*`), and plugin entry point group are
  now all `gseek`.
- `GcoreError` → `GseekError`.

### Fixed

- `result_hash` is now computed over a canonical total order, so multi-record
  `replay --check` no longer depends on parser or tool output order.
- `index_policy=rebuild` now actually replaces the published index; the cache
  layer gained `publish(replace=...)` and returns `cache_hit=False` after a
  rebuild.
- Tool paths resolved by `ToolResolver` (`overrides`, `GSEEK_<BINARY>`,
  `extra_paths`, `GSEEK_PATH_PREPEND`) are bound into `Executor` and used for
  execution; manifest commands now record the absolute executable.
- Index-only parameters (KMA `-k`, BWA `-a/-b`, skani `-c/-m`) are accepted by
  the alignment/distance planners and no longer make end-to-end runs fail.
- Manifests record absolute input/target paths; `verify` resolves legacy
  relative paths against the manifest directory, matching `replay`.
- Corrupt `index.json` values (for example a JSON array) are treated as cache
  misses; `IndexStore(enabled=False)` rejects writes and Runner skips indexing.
- Probe caching now keys on resolved path/pattern/min_version/role, re-raises
  cached missing tools under `strict=True`, and does not raise for optional
  outdated tools. `BaseAdapter` distinguishes `ToolNotFoundError` from
  `ToolVersionError` and forwards `ToolRequirement.hint`.
- BLAST always passes `-task` where supported (not `tblastx`), so `blastn` is
  no longer silently megablast; protein tasks reject `-perc_identity`.
- minimap2 identity prefers `NM` (`paf_nm`) and falls back to `nmatch/alen`
  (`paf_matches`); records expose `identity_from_nm`, `identity_from_matches`,
  and `identity_nmatch_nm` parity views.
- JSON/TSV and ingestion fixes: deterministic set serialization, truthful
  JSONL footer status, coordinate-less records preserved by
  `deduplicate_intervals`, strict FASTQ record counting, streamed stderr
  capped during drain, and executor-based manifest environment capture.
- CI integration now installs `skani` and runs nightly in addition to manual
  dispatch.
- Added `external_reference=True` to `gseek.distance()`: mash/sourmash
  adapters can consume pre-built `.msh`/`.sig`/`.zip` artifacts without
  indexing or re-sketching references. External artifacts are recorded as
  `InputKind.SKETCH` and replay re-passes the flag.
- Added real-tool golden fixtures under `tests/fixtures/`, field/formula parity
  tests for gside/gapit/gmlst, and an 8-backend real replay matrix. Sourmash
  reference IDs now fall back from signature name to a genome-like filename to
  the unique `md5`, so references inside one combined `.sig` no longer collide.

## [0.1.0] - 2026-10-22

### Added

- Project foundation:
  - `gseek.model`: `AlignmentRecord`, `AlignmentResult`, `DistanceResult`,
    `RunManifest`, `IndexRef`, `CommandRecord`.
  - `gseek.execution`: deterministic subprocess execution, timeouts, streaming,
    stderr draining, tool resolution/version probing.
  - `gseek.index`: content-addressed index cache, file locks, staging + atomic
    publish, GC.
  - `gseek.provenance`: SHA-256, canonical JSON, recipe hash, manifest
    read/write/verify.
  - `gseek.adapter`, `gseek.registry`, `gseek.runner`: adapter protocol,
    registry, end-to-end orchestration.
  - `gseek.api` and `gseek` CLI: align/distance/backends/doctor/index/manifest.
- Documentation:
  - `docs/api.md`
  - `dev/development-plan.md`
  - `dev/research.md`
  - `dev/adapter-matrix.md`
  - `dev/compatibility.md`
- 250 unit/contract tests + 21 integration tests; 88% coverage; ruff + pyright clean.
- MIT license, pixi environment, GitHub Actions CI.

### Notes

- M1–M4 adapter features (including protein BLAST, gather, skani, replay)
  are implemented. Consumer migration/parity is still pending.
