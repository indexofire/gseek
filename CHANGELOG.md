# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.10.3] - 2026-10-31

### Added
- `gseek.iter_align(...)` yields normalized alignment records as the
  adapter parses them, without materializing an `AlignmentResult`, stats, or
  manifest. Closing the iterator releases the child process/workspace; this is
  the low-memory path for large PAF/BLAST result sets. It does not hash query
  files and hashes targets only when a content-addressed index needs a key;
  `prepare_refs=True` restores eager ref preparation for custom adapters.
- `gseek.align_many(queries=[...], target=...)` merges single-end FASTA query
  files into one backend invocation and restores the original query ids. This
  amortizes target hashing, index lookup/loading, workspace setup, and process
  startup across many small queries. It intentionally does not write a
  manifest because the merged query file is temporary. `annotate_batch=True`
  tags every record with its input `batch_index` for per-file consumers.
  `query_refs` supplies precomputed input refs; `compute_inputs=False` skips
  query-file hashing entirely; `sort_records=False` keeps parser order for
  consumers that immediately re-sort or aggregate.
- `gseek.align(..., query_refs=..., target_refs=...)` accepts precomputed
  `InputRef` tuples, so consumers that run many queries against one target
  hash the database once instead of on every call. The public API now exposes
  the runner's precomputed-ref hook.
- BLAST `params={"db": "<prefix>"}` consumes a consumer-owned BLAST database
  with `index="none"`, skipping gseek's index build/cache and running blastn
  directly against the existing `-db` prefix. Passing both `db` and a gseek
  index is a typed `UnsupportedFeatureError`.

### Changed
- `AlignmentResult`/`DistanceResult` canonical ordering now sorts by the cheap
  structural key first and serializes canonical JSON only for tied rows. A
  100k-record result no longer builds a payload string per record, and
  `AlignmentResult(sort_records=False)` and `align(..., sort_records=False)`
  give consumers a parser-order opt-out. `iter_align` also skips query hashing
  and hashes the target only when a content-addressed index needs its key.
- Docs site: top-tab navigation with page content on the left and the
  right-hand TOC, matching gmlst/gside; Getting Started, Usage, and Reference
  now have bilingual landing pages that link their child pages. Chinese tab
  labels are translated.
- Docs content: English and Chinese pages now share the same section
  structure, API reference, quickstart, CLI, and error-model content.
- Docs homepage: new hero, feature cards, backend matrix, install and first-call
  examples, and ecosystem links.
- Docs diagrams: architecture, run pipeline, and request flow are rendered from
  the Graphviz sources in `dev/diagrams/` instead of ASCII art.

### Fixed
- `AlignmentRecord`/`DistanceRecord` canonical payload caches are now
  `init=False`, so `dataclasses.replace()` (used by `align_many` to restore
  query ids and add `batch_index`) cannot retain a payload describing the old
  fields in `stable_key()`/`result_hash()`.
- CLI: removed the redundant ``version`` subcommand (``--version``/``-V``
  is the only way to print the version).
- CLI: ``backends``, ``doctor``, and ``index list`` render bordered Unicode
  tables (``┌─┬─┐ / ├─┼─┤ / └─┴─┘``) for visual consistency with
  gmlst/gside/gapit output; ``--json`` output is unchanged.
- CLI: bare ``gseek`` now prints help and exits 0 instead of a bare
  argparse error; subcommands with missing required arguments print the
  full help before the error line.
- CLI: ``-V`` is a short alias for ``--version``.
- CLI: commonly used flags now have short forms: ``-b`` (backend),
  ``-q`` (query), ``-t`` (target), ``-r`` (reference), ``-j`` (threads),
  ``-p`` (preset), ``-P`` (param), ``-i`` (index), ``-m`` (manifest),
  ``-k`` (query-kind), ``-K`` (keep-work), ``-e``
  (external-reference), ``-c`` (check), ``-d`` (base-dir).

## [0.10.2] - 2026-10-26

### Added
- New `sylph` backend (v1.x): `sylph sketch` (reads) + `sylph profile`
  against a pre-built `.syldb`/`.syl2db` reference (external_reference) or a
  gseek-planned reference sketch. Records normalize into `DistanceRecord`
  (ANI, coverage, containment from `Containment_ind`), with abundance columns
  preserved as tags. Deterministic sketch output names (`<file>.sylsp`,
  `<R1>.paired.sylsp`) make the two/three-command plan fully static.
- ADR 0003: backend existence is decided by record-model fit. sylph joins
  the sketch family; kraken2/bracken stay consumer-side until a second
  composition-semantics tool justifies a `gseek.profile/1` family.

## [0.10.1] - 2026-10-26

### Added
- `AlignmentResult.from_dict` / `DistanceResult.from_dict` /
  `AlignmentStats.from_dict` / `DistanceStats.from_dict`: the
  `gseek.alignment/1` and `gseek.distance/1` documents written by the CLI can
  now be read back by the library itself (schema-checked, record-validated),
  closing the round-trip gap; `hash_and_count` and `placement_counts` are also
  public now.

### Fixed
- `merge_fasta(on_duplicate="prefix")` disambiguates same-stem sources
  (`dir1/ref.fa` + `dir2/ref.fa`) with deterministic `_2` suffixes instead of
  a spurious DuplicateIdError.
- Staging directories record a boot_id/pid/starttime owner identity, so pid
  reuse can no longer keep crashed-build garbage alive forever.
- `align(preset=..., params={"preset": ...})` with conflicting values raises
  `ConfigError` instead of silently overriding; unknown index policies raise
  `ConfigError` instead of a bare `ValueError`.
- `Executor.with_tool_paths` no longer shadows tool paths the caller bound
  explicitly on their own executor.
- RunManifest params/env/extra and result params are copied on construction:
  mutating the caller's dicts no longer drifts recorded provenance.
- `gseek manifest verify` parses the manifest file once instead of twice.

### Changed
- BLAST probing is lazy in runs: the runner probes only the required
  tools (blastn/makeblastdb); task binaries are probed on demand by
  `validate_tools` (which now receives the resolver and may return
  `{binary: path}` executor bindings). `gseek doctor` keeps probing the
  full suite. Fresh-process marker calls through gside drop from ~1.7x
  to ~1.27x of legacy.
- `BaseAdapter.probe` accepts `include_optional=False`; custom probe
  overrides should accept the new keyword.
- mash rejects same-basename reference files at plan time with an
  actionable `InputError` (their rows would be indistinguishable once
  sketched; external pre-built sketches are exempt).
- Documented the intentional cross-backend policies in
  dev/adapter-matrix.md: duplicate target-ID policy per backend (§0.6,
  with a hint in `DuplicateIdError`) and `query_id` derivation semantics
  (§0.7, per-sequence defline vs per-file stem/name).
- Performance (round 2): minimap2 selects tag fields in one pass (no full
  tag dict per row); sourmash drops per-row `Path()` construction and hoists
  the query fallback name; `to_tsv` reads record attributes directly instead
  of building the 30-key dict per row; `to_jsonl` merges the row type into
  the fresh `to_dict()` result; `input_refs` hashes multiple files
  concurrently (GIL-releasing hashlib updates); `best_per_query_target`
  caches the incumbent quality key; `iter_data_lines` fast-paths a single
  skip prefix; coverage derivation avoids redundant Interval construction;
  `AlignmentResult`/`DistanceResult` skip re-sorting already-canonical input
  (keys computed once). `iter_data_lines` documents the single-result-file
  line-number contract.
- Fixed a regression introduced by the single-traversal change:
  `directory_size` had been reimplemented on top of `_tree_digest_and_size`,
  so size-only callers (external-reference validation, gside's bridge)
  silently paid a full tree hash (+40ms per call on a 127MB sketch panel).
  It is stat-only again; `_tree_digest_and_size` remains the single-pass
  path for callers that need both digest and size, and a regression test
  asserts `directory_size` never hashes file contents.
- Performance: `input_ref` hashes and counts uncompressed FASTA in one pass
  (`hash_and_count`; ~45% faster on large references, exact same outputs);
  replay hashes every input exactly once (verify shares the freshly computed
  refs via `verify_manifest(prefetched=...)` and the runner accepts
  precomputed `query_refs`/`target_refs`); `parse_cigar` is single-pass with
  no per-operation list (long-CIGAR records ~27% faster, identical stats);
  sketch directories are traversed once for both digest and size.

- mash screen no longer truncates reference IDs at `/`: the screen column is
  a sketch defline, not a path (`Path().name` turned
  `"...plasmid/pO157"` into `"pO157"`).
- Replay now restores the recorded `index_policy` (a `--index none` run
  replays in subject mode); manifests record it in `extra`.
- `IndexStore.lock` re-validates the lock file inode after acquiring the
  flock, closing the GC-unlink race where a waiter on an orphaned inode and a
  new opener could both "hold" the lock.
- GC no longer deletes indexes with a fresh in-use lease (`mark_in_use`
  heartbeat; runner refreshes it for every run) — concurrent pipelines cannot
  lose an index out from under an external tool.
- The index-build lock timeout is configurable via
  `GSEEK_INDEX_LOCK_TIMEOUT` (default remains 300s).
- Truncated/corrupt gzip/xz inputs raise `ParseError` with the path instead
  of a bare `EOFError`/`LZMAError`; non-UTF-8 input likewise.
- Records reject intervals beyond the declared sequence length
  (`query_end > query_length`) instead of silently deriving coverage > 1.0.
- Adapter parsers reject non-finite floats (`inf`/`nan`) with a contextual
  `ParseError` (path/line) instead of an unlocalized model `InputError`;
  KMA's `int(float(...))` overflow is caught too.
- A parse failure mid-stream now still audits the streamed command (and any
  leading commands) in the failed-run manifest.
- `gseek --param x=NaN` fails fast with `UsageError` instead of a bare
  `ValueError` from canonical JSON deep in the run.
- Atomic writes fsync the parent directory after rename and apply the user's
  umask (mkstemp's 0600 leaked onto published files); merge_fasta suppresses
  secondary close failures so ENOSPC cleanup cannot mask the real error.
- `expand_fasta_paths(sort=False)` actually preserves caller order now;
  merge_fasta no longer re-sorts/re-resolves an already-expanded list.
- `Interval.from_dict` rejects floats/bools/missing keys with `ParseError`
  instead of silently truncating or raising `KeyError`.
- `to_jsonable` rejects set elements whose `repr` embeds a memory address
  (they would silently make recipe_hash process-dependent).
- summarize_by_target/query use the largest observed length when a group has
  conflicting lengths, instead of depending on sort position.
- `AlignmentRecord.from_dict` no longer coerces `is_primary: "false"` to
  `True` (string bools rejected); record `tags` are immutable
  (`MappingProxyType`) after construction.
- bwa parsing skips blank lines (consistent with every other adapter) and
  filters secondary/filtered-target rows before CIGAR and tags parsing.

### Changed
- Performance: the default `ToolResolver` is a process-level singleton
  (~240ms of per-call re-probing eliminated for the BLAST suite); record
  canonical JSON payloads are cached, making repeated sorts and result
  hashing ~10x cheaper; `result_hash` streams per-record payloads with
  byte-identical output; targets skip record counting when
  `record_manifest=False`; the CLI streams JSON output instead of building
  the full indented document in memory; mash hoists per-row `Path` work out
  of parse loops.
- `AlignmentRecord`/`DistanceRecord` expose `canonical_payload()` (cached
  canonical JSON used by stable_key and result hashing).

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

## [0.9.2] - 2026-10-25

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
