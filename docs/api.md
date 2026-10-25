# gseek API reference

**English** | [简体中文](https://indexofire.github.io/gseek/zh/api/)

> Version: 0.10.0  
> Python: >=3.12  
> Source: `src/gseek/`  
> New users: read [Quickstart](quickstart.md) first.

## 0. Status

M0 foundation and M1–M4 adapters are implemented.

| Backend | Input | Index | Operations |
| --- | --- | --- | --- |
| `blast` | FASTA | target | alignment (DNA/protein tasks) |
| `minimap2` | FASTA / FASTQ / paired | target | alignment |
| `kma` | FASTA / FASTQ / paired | target | alignment |
| `mummer` | FASTA | none | alignment (MUMmer 4) |
| `bwa` | FASTA / FASTQ / paired | target | alignment |
| `mash` | FASTA / FASTQ | sketch | distance / screen |
| `sourmash` | FASTA / FASTQ | sketch | search / gather |
| `skani` | FASTA | sketch | ANI distance |

All symbols below are importable from the top-level package:

```python
import gseek

gseek.__version__        # "0.10.0"
gseek.ALIGNMENT_SCHEMA   # "gseek.alignment/1"
gseek.DISTANCE_SCHEMA    # "gseek.distance/1"
gseek.RUN_SCHEMA         # "gseek.run/1"
gseek.INDEX_SCHEMA       # "gseek.index/1"
```

## 1. Core concepts

### 1.1 One run

```text
validate request
  → select adapter
  → probe tools
  → resolve/build index
  → plan command(s)
  → execute
  → parse/normalize
  → sort records
  → optional manifest
```

Adapters plan argv and parse output. `gseek.execution` is the only layer that
starts subprocesses. The runner owns orchestration and provenance.

### 1.2 Coordinates

All coordinates are **0-based half-open on the forward sequence**. Orientation
is stored separately in `strand` (`+` / `-`). Adapters must not emit
1-based-inclusive coordinates.

### 1.3 Identity and coverage

`identity` is a percentage in `[0, 100]`. Coverage is a fraction in `[0, 1]`.
Keep the source in `tags` (for example `identity_method`), especially when a
tool reports identity by a non-obvious formula.

### 1.4 Lossless records

Merging is an explicit view (`best_per_query_target`, `primary_only`,
`best_per_query`), not a mutation of `records`. Consumers can always inspect the
original rows.

### 1.5 Stable order

`AlignmentResult` and `DistanceResult` sort records deterministically by their
stable key. Do not rely on tool output order.

## 2. Public API

### 2.1 `gseek.align`

```python
gseek.align(
    *,
    query: Path | str | Sequence[Path | str],
    target: Path | str | Sequence[Path | str],
    backend: str | None = None,
    adapter: AlignmentAdapter | None = None,
    query_kind: InputKind | str | None = None,
    paired: bool = False,
    params: Mapping[str, JsonValue] | None = None,
    preset: str | None = None,
    threads: int = 1,
    timeout_s: float | None = None,
    index: IndexPolicy | str = IndexPolicy.AUTO,
    target_filter: Iterable[str] | None = None,
    work_dir: Path | str | None = None,
    keep_work: bool = False,
    manifest: Path | str | None = None,
    record_manifest: bool = True,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> AlignmentResult
```

| Argument | Meaning |
| --- | --- |
| `query` | one file, or two files when `paired=True` |
| `target` | one or more target FASTA files |
| `backend` | registered adapter name; mutually exclusive with `adapter` |
| `adapter` | explicit adapter instance (plugins/tests) |
| `query_kind` | `"fasta"` / `"fastq"` override; auto-detected by default |
| `paired` | treat two query paths as mates |
| `params` | adapter-specific parameters |
| `preset` | shorthand for `params["preset"]` |
| `threads` | tool threads; recorded, not part of recipe hash |
| `timeout_s` | per-command timeout |
| `index` | `auto` / `reuse` / `rebuild` / `none` |
| `target_filter` | optional target IDs passed to the adapter |
| `work_dir` | parent for temporary workspaces |
| `keep_work` | keep workspaces for debugging |
| `manifest` | write a `gseek.run/1` manifest |
| `record_manifest` | `False` skips manifest/result hashing for low-latency consumers |
| `resolver` / `executor` / `store` | inject custom infrastructure |

If `backend` and `adapter` are both omitted, gseek filters registered adapters
by input/output/paired capabilities. Exactly one candidate must remain; zero or
multiple candidates raise `ConfigError` instead of guessing.

Common errors: `ConfigError`, `UnsupportedFeatureError`, `ToolNotFoundError`,
`ToolVersionError`, `ExecutionError`, `ParseError`, `IndexBuildError`.

Backend notes:

| Backend | Notes |
| --- | --- |
| `blast` | DNA/protein tasks; FASTA; no `preset`; `index=none` uses `-subject` |
| `minimap2` | requires a preset; FASTA/FASTQ; paired; primary-only by default |
| `kma` | requires a target index; no coordinates; depth only for FASTQ |
| `mummer` | FASTA only; MUMmer 4.x; no persistent index |
| `bwa` | requires a target index; SAM/CIGAR; secondary skipped by default |
| `mash` | default `dist`; `params={"mode":"screen"}` for read screening |
| `sourmash` | one query file per call; `containment` toggles semantics |
| `skani` | FASTA only; sketch database; returns ANI + aligned fraction |

### 2.2 `gseek.distance`

```python
gseek.distance(
    *,
    query: Path | str | Sequence[Path | str],
    reference: Path | str | Sequence[Path | str],
    backend: str | None = None,
    adapter: DistanceAdapter | None = None,
    query_kind: InputKind | str | None = None,
    params: Mapping[str, JsonValue] | None = None,
    threads: int = 1,
    timeout_s: float | None = None,
    index: IndexPolicy | str = IndexPolicy.AUTO,
    external_reference: bool = False,
    external_sha256: str | None = None,
    external_size: int | None = None,
    work_dir: Path | str | None = None,
    keep_work: bool = False,
    manifest: Path | str | None = None,
    record_manifest: bool = True,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> DistanceResult
```

Used by mash, sourmash, and skani. There are no alignment coordinates.

`external_reference=True` means `reference` is already a built artifact rather
than FASTA: `.msh`, `.sig`/`.zip`, a sourmash rocksdb directory, or a skani
database directory. In that case gseek skips reference indexing and hashes the
artifact (directories use deterministic tree hashing; symlinks are rejected).

### 2.3 Registry queries

```python
gseek.backends() -> tuple[BackendSpec, ...]
gseek.find(
    *,
    input: InputKind | str | None = None,
    output: OutputKind | str | None = None,
    paired: bool | None = None,
    molecule: Molecule | str | None = None,
) -> tuple[BackendSpec, ...]
gseek.find_backends(...)  # alias
```

`BackendSpec.capabilities` describes `inputs`, `outputs`, `molecule`, `paired`,
`index_scope`, `streaming`, `threads`, `presets`, and `deterministic`.

### 2.4 Get/register an adapter

```python
adapter = gseek.get_backend("minimap2")
result = gseek.align(
    adapter=adapter,
    query="sample.fna",
    target="alleles.fna",
    preset="asm5",
)

gseek.register_adapter(MyAdapter)
gseek.register_adapter(MyAdapter, replace=True)
```

`register_adapter` accepts a class or instance; duplicate names raise
`ConfigError` unless `replace=True`. Plugins may also use the
`gseek.adapters` entry-point group.

### 2.5 Verify a manifest

```python
issues = gseek.verify("runs/sample.manifest.json")
for issue in issues:
    print(issue.severity, issue.code, issue.message)
```

`verify` checks recorded file sizes/SHA-256. It does not re-probe tools or rerun.

### 2.6 Replay

```python
from pathlib import Path
import gseek

result = gseek.replay(Path("runs/sample.manifest.json"), check=True)
print(result.matches, result.issues)
```

Replay re-plans through the adapter from the manifest; it never executes stored
argv. `check=True` compares the new result hash and raises/reports a mismatch.

### 2.7 Consumer switch: `gseek.compat`

```python
from gseek.compat import Engine

engine = Engine.from_env("GAPIT_ENGINE")  # legacy (default) | gseek | shadow

result = engine.run(
    legacy=run_legacy,
    gseek=run_gseek,
    compare=compare_results,  # optional; default ==
)
```

- `legacy`: only legacy; no gseek adapter import.
- `gseek`: only gseek; exceptions propagate.
- `shadow`: run both, compare, return legacy; gseek/comparator failures log and
  fall back to legacy.

Consumers materialize streaming iterators before passing them to `run`.

## 3. Result models

### 3.1 `AlignmentRecord`

Key fields:

```text
query_id, target_id
query_length, target_length
query_start, query_end, target_start, target_end
strand
identity, aligned_length, matches, mismatches,
gap_count, gap_openings
evalue, bit_score, mapq, depth, score
query_coverage, target_coverage
query_aligned, target_aligned, target_description
is_primary, is_supplementary
tags
```

`AlignmentRecord.stable_key` defines deterministic ordering.

### 3.2 `AlignmentResult`

```text
backend, records, inputs, targets, params, stats
index (IndexRef | None), manifest (RunManifest | None)
warnings, errors, schema
```

Views:

- `primary_only()` — primary, non-supplementary records.
- `best_per_query_target()` — best row per query/target by
  `(identity, aligned_length, query_coverage, target_coverage)`; ties keep the
  first row.
- `summarize_by_target()` — aggregate counts.
- Iteration/`len()` operate on normalized, sorted records.

### 3.3 `DistanceRecord` / `DistanceResult`

Fields: `query_id`, `reference_id`, `distance`, `pvalue`, `shared_hashes`,
`total_hashes`, `containment`, `jaccard`, `ani`, `aligned_fraction`, `tags`.

`DistanceResult` contains `backend`, `records`, `inputs`, `references`,
`params`, `stats`, `index`, `manifest`, `warnings`, `errors`, `schema`; it
offers `best_per_query()`.

### 3.4 Provenance references

- `InputRef`: path, kind (`fasta`/`fastq`/`sketch`), SHA-256, size.
- `ToolRef`: binary, path, version.
- `IndexRef`: key, adapter/version, entry, source hash, artifacts, params,
  cache-hit flag, tool versions.
- `CommandRecord`: argv, cwd, exit code, duration, stdout/stderr hashes.
- `RunManifest`: schema, run ID, timestamps, inputs, tools, index, commands,
  params, result hash, environment allowlist, issues.

## 4. Adapter API

### 4.1 Protocol

An adapter implements:

```python
class MyAdapter:
    spec: BackendSpec

    def probe(self, resolver, *, strict=True): ...
    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan: ...
    def plan_align(self, request: AlignRequest, index: IndexRef | None,
                   ctx: PlanContext) -> AlignPlan: ...
    def parse_align(self, ctx: ParseContext, lines: Iterable[str]) -> Iterable[AlignmentRecord]: ...
```

Distance adapters use `DistanceRequest` / `DistancePlan` / `DistanceRecord`.
Adapters must not call `subprocess` directly.

### 4.2 Requests and contexts

`AlignRequest` carries query/target paths, detected kinds, paired flag, params,
threads, timeout, index policy, target filter, workspace policy. `PlanContext`
carries workspace, index directory, and environment.

### 4.3 Capabilities

Declare inputs, outputs, molecule, paired support, index scope, streaming,
threads, presets, and determinism in `BackendSpec` / `Capabilities`. The runner
uses these for automatic adapter selection and validation.

### 4.4 Minimal adapter

```python
from gseek.adapter import BaseAdapter
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.execution import CommandPlan

class MyAdapter(BaseAdapter):
    spec = BackendSpec(
        name="myalign",
        family="myalign",
        summary="example aligner",
        capabilities=Capabilities(...),
        tools=(ToolRequirement(binary="myalign", role="aligner"),),
    )

    def plan_align(self, request, index, ctx):
        return AlignPlan(commands=(CommandPlan(argv=("myalign", ...)),))
```

Use `@pytest.mark.integration` and skip when the binary is absent. Adapter tests
must cover command planning, golden parsing (forward/reverse/gapped/empty),
coordinate normalization, identity/coverage sources, index keys, and errors.

### 4.5 Plugin entry points

Register third-party adapters under the `gseek.adapters` entry-point group; the
registry imports them lazily.

## 5. Execution layer

### 5.1 `CommandPlan`

A command plan is an argv tuple plus label, cwd, environment overrides, stdin,
output capture mode, and timeout. No shell strings.

### 5.2 `Executor`

Responsibilities:

- resolve binaries and run with an argv list;
- set `LC_ALL=C`, deterministic environment, absolute paths;
- enforce timeouts, drain stderr, support streaming;
- return exit code/stdout/stderr without interpreting tool semantics.

### 5.3 `ToolResolver`

Discovers binaries from PATH and explicit overrides, probes versions, caches
results per process, and raises typed errors for missing/old tools.

### 5.4 Environment variables

| Variable | Meaning |
| --- | --- |
| `GSEEK_CACHE_DIR` | index cache root |
| `GSEEK_<BINARY>` | explicit tool path, e.g. `GSEEK_BLASTN` |
| `GSEEK_*_TIMEOUT` | per-tool timeout overrides where supported |
| `LC_ALL` | forced to `C` for tool execution |

Secrets are never written to manifests; only allowlisted environment values are
recorded.

### 5.5 Workspace

`RunWorkspace` allocates a temporary directory per run, supports cleanup, and
can be retained with `keep_work=True` for debugging. All output paths must stay
inside the workspace or index staging directory.

## 6. Index layer

### 6.1 Policies

`IndexPolicy`: `auto`, `reuse`, `rebuild`, `none`.

- `auto`: reuse a matching cache entry or build one.
- `reuse`: require a cache hit.
- `rebuild`: invalidate and rebuild.
- `none`: no persistent index (where the adapter supports subject mode).

### 6.2 Content-addressed key

The key includes adapter name/version, semantic parameters, target content
SHA-256, and tool version. `threads`, `timeout`, and other non-semantic values
are excluded. There are no mtime decisions.

### 6.3 Cache layout and GC

Indexes are stored under the cache root by key with a lock file, staging
directory, and atomic publish. `IndexStore` supports lookup, build, purge, and
garbage collection. `gseek index gc` is a dry run unless `--apply` is passed.

## 7. Reproducibility and manifests

### 7.1 Hashing

- `sha256_file`, `sha256_tree` (deterministic tree hash; symlinks rejected)
- `canonical_json` / `sha256_json`
- `result_hash` over sorted normalized records

### 7.2 `recipe_hash`

The recipe hash captures the semantic operation: adapter, semantic params,
inputs, index key, and tool versions. It is stable across hosts where the same
inputs/tools are used.

### 7.3 Manifest

`gseek.run/1` records:

```json
{
  "schema": "gseek.run/1",
  "run_id": "...",
  "created_at": "...",
  "inputs": [],
  "tools": [],
  "index": null,
  "commands": [],
  "params": {},
  "result_hash": "...",
  "environment": {},
  "issues": []
}
```

`write_manifest`/`read_manifest` round-trip the model. `verify_manifest` checks
recorded file hashes. `replay` re-plans and can compare `result_hash`.

## 8. I/O and views

- `gseek.io`: FASTA/FASTQ readers, line normalization, `merge_fasta`.
- `gseek.views`: explicit record views used by consumers.
- No adapter writes outside its workspace.

## 9. Errors

See [Error model](errors.md) for the full table. All typed failures derive from
`GseekError` with `code`, `message`, `context`, `hint`, and `exit_code`.

## 10. CLI

See [CLI reference](cli.md). The CLI is a thin API wrapper; its JSON output and
exit codes are part of the automation contract.

## 11. Schemas

The stable schema strings are:

```text
gseek.alignment/1
gseek.distance/1
gseek.run/1
gseek.index/1
gseek.error/1
```

Example alignment record:

```json
{
  "query_id": "contig1",
  "target_id": "allele_1",
  "query_start": 100,
  "query_end": 580,
  "target_start": 0,
  "target_end": 480,
  "strand": "+",
  "identity": 99.79,
  "aligned_length": 480,
  "target_coverage": 1.0,
  "query_coverage": 1.0,
  "tags": {"identity_method": "blast_pident"}
}
```

Schemas are versioned; breaking changes require a new schema string.

## 12. Stability and limits

- `0.10.0` is the current release; pin it in production. Public API may still change before 1.0.
- Pin `gseek==0.10.0` in production and keep a legacy path via
  `gseek.compat` while migrating.
- Backend coverage differs by tool; see the matrix at the top.
- Manifest replay requires the same adapter/tool versions; environment changes
  can legitimately change results and are reported as replay mismatches.
- Return to the [documentation index](index.md) or [Chinese API reference](https://indexofire.github.io/gseek/zh/api/).
