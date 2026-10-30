# Error model

**English** | [简体中文](https://indexofire.github.io/gseek/zh/errors/)

`gseek` exposes typed exceptions plus stable process exit codes. Library callers
should catch `gseek.GseekError` and read its fields instead of parsing strings.

## Exception hierarchy

All known errors inherit from `GseekError`:

```python
from gseek import GseekError

try:
    result = gseek.align(...)
except GseekError as exc:
    print(exc.code)        # stable code, e.g. TOOL_NOT_FOUND
    print(exc.message)     # human-readable description
    print(exc.context)     # dict[str, str], e.g. backend/path
    print(exc.hint)        # actionable hint, possibly empty
    print(exc.exit_code)   # CLI exit code
```

| Exception | `code` | Exit | Typical cause |
| --- | --- | --- | --- |
| `GseekError` | `GSEEK_ERROR` (overridable) | 1 | base/unclassified |
| `UsageError` | `USAGE_ERROR` | 2 | invalid argument combination or backend |
| `ConfigError` | `CONFIG_ERROR` | 2 | missing environment/configuration |
| `UnsupportedFeatureError` | `UNSUPPORTED_FEATURE` | 2 | adapter does not support input/parameter |
| `ToolNotFoundError` | `TOOL_NOT_FOUND` | 3 | tool not on PATH/override path |
| `ToolVersionError` | `TOOL_VERSION_ERROR` | 3 | tool too old or version unparsable |
| `ExecutionError` | `EXECUTION_FAILED` | 4 | subprocess non-zero/timeout |
| `IndexBuildError` | `INDEX_BUILD_FAILED` | 4 | index construction failed |
| `IndexLockError` | `INDEX_LOCK_TIMEOUT` | 4 | concurrent index lock timeout |
| `CacheError` | `CACHE_ERROR` | 4 | corrupt cache or write while disabled |
| `ManifestError` | `MANIFEST_ERROR` | 4 | manifest read/validation failure |
| `ParseError` | `PARSE_ERROR` | 5 | tool output cannot be parsed |
| `InputError` | `INPUT_ERROR` | 5 | input file/format error |
| `DuplicateIdError` | `DUPLICATE_SEQUENCE_ID` | 5 | duplicate FASTA/FASTQ ID |
| `ReplayMismatchError` | `REPLAY_MISMATCH` | 6 | `result_hash` differs on replay |

Subclasses of `InputError` keep their own `code` but use exit code `5`.

## JSON envelope

`GseekError.to_dict()` and the CLI's default stderr use `gseek.error/1`:

```json
{
  "schema": "gseek.error/1",
  "code": "TOOL_NOT_FOUND",
  "message": "required tool 'blastn' was not found",
  "context": {"binary": "blastn"},
  "hint": "install BLAST+ or set GSEEK_BLASTN"
}
```

The CLI writes one JSON line by default. For humans:

```bash
gseek --error-format text align ...
# gseek: --param must be KEY=VALUE, got 'bad'
# hint: ...
```

## Common handling

### Missing tool

```python
except ToolNotFoundError as exc:
    raise SystemExit(f"install {exc.context.get('binary', 'the tool')}")
```

Run `gseek doctor` first, or set `GSEEK_<BINARY>` to an explicit path.

### Old tool version

`ToolVersionError.context` contains `required` / `found`. Do not silently
downgrade; upgrade the tool or choose a compatible adapter.

### Input/parse errors

`ParseError` carries `path` and `line_number`; `InputError` carries the input
path. Keep the failing file and the tool version; do not retry blindly.

### Replay mismatch

`ReplayMismatchError` means the replayed canonical `result_hash` differs from
the manifest. Check, in order:

1. whether an external tool version changed;
2. whether the reference or input file hash changed;
3. whether a different index state was used;
4. whether the manifest came from another gseek version.

A replay mismatch is a reproducibility signal, not a transient failure.

## What not to do

- Do not branch on error message text; use `code` or the exception type.
- Do not swallow `GseekError` and return an empty result; callers cannot tell
  "no hits" from "failed".
- Do not wrap `KeyboardInterrupt` in `GseekError`; the CLI returns `130`.
- Do not parse the `text` error format in automation; use the default JSON.
