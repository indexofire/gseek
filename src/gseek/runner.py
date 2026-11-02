"""Plan → execute → parse → provenance orchestration."""

from __future__ import annotations

import platform
import shutil
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from gseek.adapter import (
    AlignmentAdapter,
    AlignRequest,
    DistanceAdapter,
    DistanceRequest,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.capabilities import BackendSpec, ToolchainProbe
from gseek.errors import (
    ConfigError,
    ExecutionError,
    GseekError,
    IndexBuildError,
    InputError,
    ParseError,
    UnsupportedFeatureError,
)
from gseek.execution import (
    CommandPlan,
    Executor,
    ProcessResult,
    StreamedProcess,
    ToolProbe,
    ToolResolver,
    create_workspace,
    env_for_manifest,
)
from gseek.index import IndexLease, IndexPolicy, IndexStore, compute_index_key
from gseek.io import expand_fasta_paths
from gseek.model import (
    AlignmentRecord,
    AlignmentResult,
    CommandPhase,
    CommandRecord,
    DistanceRecord,
    DistanceResult,
    DistanceStats,
    IndexRef,
    InputRef,
    Issue,
    RunManifest,
    build_alignment_stats,
)
from gseek.provenance import (
    alignment_result_hash,
    directory_size,
    distance_result_hash,
    gseek_version,
    hostname,
    input_refs,
    new_run_id,
    platform_label,
    recipe_hash,
    sha256_json,
    utc_now_iso,
    validate_path_text,
    write_manifest,
)
from gseek.registry import find_backends, get_backend
from gseek.types import InputKind, JsonValue, OutputKind, RunStatus


def _validate_tools_hook(
    adapter: AlignmentAdapter | DistanceAdapter,
    request: AlignRequest | DistanceRequest,
    probe: ToolchainProbe,
    resolver: ToolResolver,
) -> Mapping[str, Path] | None:
    """Call the optional per-request tool gate (see BaseAdapter.validate_tools).

    Adapters running under lazy probing may return ``{binary: path}``
    bindings for binaries they resolved on demand; the caller merges them
    into the executor so ``GSEEK_<BINARY>``-style resolution still applies.
    """
    validate = getattr(adapter, "validate_tools", None)
    if not callable(validate):
        return None
    bindings = validate(request, probe, resolver)
    return bindings if isinstance(bindings, Mapping) else None


def _checked_records[RecordT: (AlignmentRecord, DistanceRecord)](
    records: tuple[Any, ...],
    record_cls: type[RecordT],
    backend: str,
) -> tuple[RecordT, ...]:
    """Filter to *record_cls* and fail loudly on any foreign value.

    Adapters are trusted planners but their parsers are the least typed edge:
    silently dropping a non-record value would lose data without a trace.
    """
    normalized = tuple(record for record in records if isinstance(record, record_cls))
    if len(normalized) != len(records):
        kind = "alignment" if record_cls is AlignmentRecord else "distance"
        raise ParseError(
            f"{kind} adapter {backend!r} returned non-{record_cls.__name__} values",
            context={"backend": backend},
        )
    return cast(tuple[RecordT, ...], normalized)


def verify_refs_unchanged(refs: Sequence[InputRef], *, role: str) -> None:
    """Re-hash refs after a run and fail if an input changed underneath it."""
    if not refs:
        return
    current = input_refs(
        tuple(Path(ref.path) for ref in refs),
        kind=refs[0].kind,
        compute_records=False,
    )
    for before, after in zip(refs, current, strict=True):
        if before.path != after.path or before.size != after.size or before.sha256 != after.sha256:
            raise InputError(
                f"{role} input changed during the run: {before.path}",
                context={
                    "role": role,
                    "path": before.path,
                    "expected": before.sha256,
                    "actual": after.sha256,
                },
            )


def validate_ref_paths(
    refs: Sequence[InputRef],
    paths: Sequence[Path],
    *,
    role: str,
    allow_empty: bool = False,
) -> None:
    """Require precomputed refs to describe exactly the requested paths.

    ``target_refs`` are used as the concrete target list, so accepting a ref
    for a different file would silently redirect the run (and poison its index
    key/provenance). The check is a resolve comparison only; it never re-hashes.
    """
    if allow_empty and not refs:
        return
    if len(refs) != len(paths):
        raise ConfigError(
            f"{role}_refs has {len(refs)} ref(s), but {role} has {len(paths)} path(s)",
            context={"role": role, "refs": str(len(refs)), "paths": str(len(paths))},
        )
    for ref, path in zip(refs, paths, strict=True):
        try:
            validate_path_text(Path(ref.path))
            validate_path_text(Path(path))
            ref_path = Path(ref.path).resolve()
            requested = Path(path).resolve()
        except (InputError, OSError, RuntimeError, ValueError) as exc:
            raise ConfigError(
                f"{role}_refs contains an invalid path: {ref.path!r}",
                context={"role": role, "ref": ref.path},
            ) from exc
        if ref_path != requested:
            raise ConfigError(
                f"{role}_refs path {ref_path} does not match {role} path {requested}",
                context={"role": role, "ref": str(ref_path), "path": str(requested)},
            )


def resolve_adapter(
    *,
    backend: str | None,
    adapter: AlignmentAdapter | DistanceAdapter | None,
    request: AlignRequest | DistanceRequest,
    output_kind: OutputKind,
) -> AlignmentAdapter | DistanceAdapter:
    """Select an adapter explicitly, or require an unambiguous capability match."""
    if backend is not None and adapter is not None:
        raise ConfigError("pass either backend= or adapter=, not both")
    if adapter is not None:
        return adapter
    if backend is not None:
        return cast(AlignmentAdapter | DistanceAdapter, get_backend(backend))

    query_kind = request.query[0]
    from gseek.io import detect_input_kind

    kind = detect_input_kind(query_kind)
    paired = isinstance(request, AlignRequest) and request.paired
    candidates = find_backends(input=kind, output=output_kind, paired=paired)
    if not candidates:
        raise UnsupportedFeatureError(
            f"no registered backend supports input={kind.value}, "
            f"output={output_kind.value}, paired={paired}",
            context={"input": kind.value, "output": output_kind.value},
        )
    if len(candidates) > 1:
        names = ", ".join(spec.name for spec in candidates)
        raise ConfigError(
            f"multiple backends match ({names}); pass backend= explicitly",
            context={"candidates": names},
        )
    candidate = next(iter(candidates))
    return cast(AlignmentAdapter | DistanceAdapter, get_backend(candidate.name))


def _command_record(
    result: ProcessResult,
    *,
    phase: CommandPhase,
) -> CommandRecord:
    return CommandRecord(
        phase=phase,
        label=result.plan.label or result.plan.argv[0],
        argv=result.plan.argv,
        cwd=str(result.plan.cwd) if result.plan.cwd is not None else None,
        exit_code=result.returncode,
        duration_s=result.duration_s,
        timed_out=result.timed_out,
        stderr_tail=result.stderr_tail,
        stdout_path=str(result.stdout_path) if result.stdout_path is not None else None,
    )


def _run_commands(
    commands: Sequence[CommandPlan],
    executor: Executor,
    *,
    phase: CommandPhase,
) -> tuple[CommandRecord, ...]:
    records: list[CommandRecord] = []
    for command in commands:
        try:
            result = executor.run(command)
        except ExecutionError as exc:
            # Audit the failing command itself and surface every record run so
            # far: the run-level failure manifest must show what executed.
            exc.command_records = (  # type: ignore[attr-defined]
                *records,
                _command_record_from_error(command, exc, phase=phase),
            )
            raise
        records.append(_command_record(result, phase=phase))
    return tuple(records)


def _execution_like(
    exc: GseekError,
    process: StreamedProcess | None,
) -> ExecutionError:
    """Wrap a non-execution failure with the streamed process's outcome."""
    wrapper = ExecutionError(
        f"{exc.code}: {exc.message}",
        context={"cause": exc.code},
    )
    wrapper.argv = ()
    wrapper.returncode = process.returncode if process is not None else None
    wrapper.stderr = (process.stderr if process is not None else "")[-64_000:]
    wrapper.timed_out = False
    return wrapper


def _command_record_from_error(
    command: CommandPlan,
    exc: ExecutionError,
    *,
    phase: CommandPhase,
) -> CommandRecord:
    return CommandRecord(
        phase=phase,
        label=command.label or command.argv[0],
        argv=command.argv,
        cwd=str(command.cwd) if command.cwd is not None else None,
        exit_code=exc.returncode if exc.returncode is not None else -1,
        duration_s=0.0,
        timed_out=exc.timed_out,
        stderr_tail=exc.stderr,
        stdout_path=str(command.stdout) if command.stdout is not None else None,
    )


def _effective_params(
    adapter: AlignmentAdapter | DistanceAdapter,
    request: AlignRequest | DistanceRequest,
) -> dict[str, JsonValue]:
    params = dict(adapter.semantic_params(request.params))
    if isinstance(request, AlignRequest) and request.target_filter:
        target_filter: list[JsonValue] = []
        target_filter.extend(sorted(request.target_filter))
        params["target_filter"] = target_filter
    return params


def _adapter_identity(spec: BackendSpec) -> str:
    """Adapter implementation identity used in index keys and manifests.

    ``api_version`` alone is static, so a gseek release that changes index
    planning would otherwise reuse stale caches. The package version is part of
    the implementation identity; upgrades invalidate indexes.
    """
    return f"{spec.api_version}+gseek.{gseek_version()}"


def _index_key(
    spec: BackendSpec,
    adapter: AlignmentAdapter | DistanceAdapter,
    request: AlignRequest | DistanceRequest,
    target_refs: tuple[InputRef, ...],
    tools: tuple[ToolProbe, ...],
) -> tuple[str, dict[str, JsonValue], dict[str, str]]:
    """Return the cache key *and* the exact identity inputs it was built from.

    ``publish`` must record the same params/tool versions the key hashed;
    otherwise the archived provenance misdescribes the cache entry.
    """
    tool_versions = {tool.name: tool.version for tool in tools}
    tool_hashes = {tool.name: tool.sha256 for tool in tools if tool.sha256 is not None}
    index_params_fn = getattr(adapter, "index_params", None)
    if callable(index_params_fn):
        index_params = dict(cast(Mapping[str, JsonValue], index_params_fn(request.params)))
    else:
        index_params = dict(adapter.semantic_params(request.params))
    key = compute_index_key(
        adapter=spec.name,
        adapter_version=_adapter_identity(spec),
        targets=target_refs,
        params=index_params,
        tool_versions=tool_versions,
        tool_hashes=tool_hashes or None,
    )
    return key, index_params, tool_versions


def _ensure_index(
    *,
    adapter: AlignmentAdapter | DistanceAdapter,
    request: AlignRequest | DistanceRequest,
    target_refs: tuple[InputRef, ...],
    tools: tuple[ToolProbe, ...],
    probe: ToolchainProbe,
    executor: Executor,
    store: IndexStore,
    policy: IndexPolicy,
) -> tuple[IndexRef | None, tuple[CommandRecord, ...], IndexLease | None]:
    spec = adapter.spec
    if (
        not store.enabled
        or spec.capabilities.index_scope.value == "none"
        or policy is IndexPolicy.NONE
        or (isinstance(request, DistanceRequest) and request.external_reference)
    ):
        return None, (), None

    key, index_params, tool_versions = _index_key(spec, adapter, request, target_refs, tools)
    with store.lock(spec.name, key):
        existing = store.lookup(spec.name, key)
        if existing is not None and policy is not IndexPolicy.REBUILD:
            lease = store.lease(spec.name, existing.key)
            lease.start()
            return existing, (), lease
        if policy is IndexPolicy.REUSE and existing is None:
            raise IndexBuildError(
                f"index {key} is not cached and index_policy=reuse",
                context={"backend": spec.name, "key": key},
            )

        staging = store.new_staging(spec.name, key)
        try:
            tuple(tool.to_tool_ref() for tool in probe.tools if tool.path is not None)
            ctx = PlanContext(
                workspace=staging,
                index_dir=staging,
                threads=request.threads,
                target_refs=target_refs,
            )
            target_paths = (
                request.target if isinstance(request, AlignRequest) else request.reference
            )
            index_request = IndexRequest(
                target=tuple(target_paths),
                params=request.params,
                threads=request.threads,
                force=policy is IndexPolicy.REBUILD,
            )
            plan = adapter.plan_index(index_request, ctx)
            if plan is None:
                shutil.rmtree(staging, ignore_errors=True)
                return None, (), None
            records = _run_commands(plan.commands, executor, phase="index")
            source_sha256 = sha256_json(
                [
                    {"sha256": ref.sha256, "kind": ref.kind.value, "size": ref.size}
                    for ref in target_refs
                ]
            )
            published = store.publish(
                staging,
                adapter=spec.name,
                key=key,
                adapter_version=_adapter_identity(spec),
                entry=plan.entry,
                artifacts=plan.artifacts,
                source_sha256=source_sha256,
                params=plan.params or index_params,
                tool_versions=tool_versions,
                replace=policy is IndexPolicy.REBUILD,
            )
            lease = store.lease(spec.name, published.key)
            lease.start()
            return published, records, lease
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def _build_manifest(
    *,
    spec: BackendSpec,
    request: AlignRequest | DistanceRequest,
    query_refs: tuple[InputRef, ...],
    target_refs: tuple[InputRef, ...],
    params: dict[str, JsonValue],
    tools: tuple[ToolProbe, ...],
    index: IndexRef | None,
    commands: tuple[CommandRecord, ...],
    started: float,
    env: Mapping[str, str] | None = None,
    result_hash_value: str | None = None,
    warnings: tuple[Issue, ...] = (),
    status: RunStatus = RunStatus.OK,
    errors: tuple[Issue, ...] = (),
) -> RunManifest:
    tool_refs = tuple(tool.to_tool_ref() for tool in tools)
    digest = recipe_hash(
        backend=spec.name,
        adapter_version=spec.api_version,
        inputs=query_refs,
        targets=target_refs,
        params=params,
        tools=tool_refs,
        index_key=index.key if index is not None else None,
    )
    return RunManifest(
        run_id=new_run_id(),
        recipe_hash=digest,
        created_at=utc_now_iso(),
        gseek_version=gseek_version(),
        python_version=platform.python_version(),
        platform=platform_label(),
        hostname=hostname(),
        backend=spec.name,
        backend_version=spec.api_version,
        tools=tool_refs,
        inputs=query_refs,
        targets=target_refs,
        index=index,
        params=params,
        commands=commands,
        result_hash=result_hash_value,
        env=env_for_manifest(env),
        status=status,
        errors=errors,
        duration_s=time.monotonic() - started,
        warnings=warnings,
        extra={
            "kind": "alignment" if isinstance(request, AlignRequest) else "distance",
            "threads": request.threads,
            "paired": isinstance(request, AlignRequest) and request.paired,
            "index_policy": request.index_policy.value,
            "external_reference": (
                isinstance(request, DistanceRequest) and request.external_reference
            ),
        },
    )


_SHARED_RESOLVER: ToolResolver | None = None


def _default_resolver() -> ToolResolver:
    """Process-level default resolver.

    Probing spawns one subprocess per tool (~40ms each): creating a fresh
    resolver per API call re-paid that cost on every invocation (~240ms for
    the BLAST suite). Tool versions do not change within a process, so the
    instance cache is safe to share.
    """
    global _SHARED_RESOLVER
    if _SHARED_RESOLVER is None:
        _SHARED_RESOLVER = ToolResolver()
    return _SHARED_RESOLVER


def _prepare_targets(
    request: AlignRequest | DistanceRequest,
    *,
    compute_records: bool = True,
) -> tuple[tuple[Path, ...], tuple[InputRef, ...]]:
    raw = request.target if isinstance(request, AlignRequest) else request.reference
    if isinstance(request, DistanceRequest) and request.external_reference:
        files = tuple(path.resolve() for path in raw)
        if request.external_sha256 is not None and request.external_size is not None:
            refs = tuple(
                _external_ref(path, request.external_sha256, request.external_size)
                for path in files
            )
            return files, refs
        return files, input_refs(files, kind=InputKind.SKETCH, compute_records=False)
    files = expand_fasta_paths(raw)
    return files, input_refs(files, compute_records=compute_records)


def _external_ref(path: Path, sha256: str, size: int) -> InputRef:
    if path.is_dir():
        actual_size = directory_size(path)
    elif path.is_file():
        actual_size = path.stat().st_size
    else:
        raise InputError(f"external reference not found: {path}", context={"path": str(path)})
    if actual_size != size:
        raise InputError(
            f"external reference size mismatch: expected {size}, got {actual_size}",
            context={"path": str(path), "expected": str(size), "actual": str(actual_size)},
        )
    return InputRef(
        path=str(path),
        size=size,
        sha256=sha256,
        kind=InputKind.SKETCH,
        records=None,
        compressed=False,
    )


def _prepare_queries(
    request: AlignRequest | DistanceRequest, *, compute_records: bool = True
) -> tuple[InputRef, ...]:
    kind = request.query_kind
    return input_refs(request.query, kind=kind, compute_records=compute_records)


def _prepare_manifest_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.suffix.lower() != ".json":
        raise InputError(f"manifest path must end with .json: {path}", context={"path": str(path)})
    return path


def run_alignment(
    request: AlignRequest,
    *,
    backend: str | None = None,
    adapter: AlignmentAdapter | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
    manifest_path: Path | None = None,
    record_manifest: bool = True,
    query_refs: tuple[InputRef, ...] | None = None,
    target_refs: tuple[InputRef, ...] | None = None,
    sort_records: bool = True,
    validate_query_refs: bool = True,
    verify_inputs: bool = False,
) -> AlignmentResult:
    """Run one alignment request and return normalized records plus provenance.

    ``query_refs``/``target_refs`` accept precomputed input references (from
    :func:`gseek.provenance.input_refs`) so callers that already hashed the
    files — replay, verify flows — do not pay the full-file scan again.
    ``sort_records=False`` keeps parser order for consumers that immediately
    re-sort or aggregate the rows. ``validate_query_refs=False`` is reserved for
    batched runs whose request query is an internal merged file while the refs
    describe the original per-file inputs. ``verify_inputs=True`` re-hashes the
    refs after the tool finishes and raises if an input changed mid-run; it is
    off by default because it doubles input I/O.
    """
    started = time.monotonic()
    # Config errors fail fast: before any probe, index build, or command runs.
    destination = _prepare_manifest_path(manifest_path)
    if destination is not None and not record_manifest:
        raise ConfigError("manifest_path requires record_manifest=True")
    selected = resolve_adapter(
        backend=backend,
        adapter=adapter,
        request=request,
        output_kind=OutputKind.ALIGNMENT,
    )
    if not isinstance(selected, AlignmentAdapter):
        raise UnsupportedFeatureError("selected adapter does not implement alignment")
    align_adapter: AlignmentAdapter = selected
    spec = align_adapter.spec

    resolver = resolver or _default_resolver()
    executor = executor or Executor()
    store = store or IndexStore()
    probe = align_adapter.probe(resolver, strict=True, include_optional=False)
    tools = probe.tools
    executor = executor.with_tool_paths(
        {tool.name: tool.path for tool in tools if tool.path is not None}
    )

    if query_refs is not None and validate_query_refs:
        validate_ref_paths(query_refs, request.query, role="query")
    if target_refs is not None:
        validate_ref_paths(target_refs, request.target, role="target")
    if query_refs is None:
        query_refs = _prepare_queries(request, compute_records=record_manifest)
    if target_refs is None:
        target_files, target_refs = _prepare_targets(request, compute_records=record_manifest)
    else:
        target_files = tuple(Path(ref.path) for ref in target_refs)
    # Adapters plan indices from the concrete target files, not the user's
    # original directory arguments.
    effective_request = AlignRequest(
        query=request.query,
        target=target_files,
        query_kind=request.query_kind,
        paired=request.paired,
        params=request.params,
        threads=request.threads,
        timeout_s=request.timeout_s,
        index_policy=request.index_policy,
        target_filter=request.target_filter,
        work_dir=request.work_dir,
        keep_work=request.keep_work,
    )
    bindings = _validate_tools_hook(align_adapter, effective_request, probe, resolver)
    if bindings:
        executor = executor.with_tool_paths(bindings)
    params = _effective_params(align_adapter, effective_request)

    index, index_commands, lease = _ensure_index(
        adapter=align_adapter,
        request=effective_request,
        target_refs=target_refs,
        tools=tools,
        probe=probe,
        executor=executor,
        store=store,
        policy=request.index_policy,
    )
    align_commands: tuple[CommandRecord, ...] = ()
    try:
        workspace = create_workspace(
            prefix=f"gseek-{spec.name}-",
            parent=request.work_dir,
            keep=request.keep_work,
        )
    except BaseException:
        if lease is not None:
            lease.close()
        raise
    try:
        index_dir = store.path_for(spec.name, index.key) if index is not None else None
        ctx = PlanContext(
            workspace=workspace.root,
            index_dir=index_dir,
            threads=request.threads,
            query_refs=query_refs,
            target_refs=target_refs,
        )
        plan = align_adapter.plan_align(effective_request, index, ctx)
        parse_ctx = ParseContext(
            request=effective_request,
            plan=plan,
            workspace=workspace.root,
            index=index,
            tools=tuple(tool.to_tool_ref() for tool in tools),
        )

        align_started = time.monotonic()
        records: tuple[Any, ...]
        if plan.stream:
            if not plan.commands:
                raise ConfigError("streaming alignment plan has no commands")
            leading = _run_commands(plan.commands[:-1], executor, phase="align")
            align_commands = leading
            last = plan.commands[-1]
            streamed: StreamedProcess | None = None
            try:
                with executor.stream(last) as process:
                    streamed = process
                    parsed = tuple(align_adapter.parse_align(parse_ctx, process.iter_lines()))
                    result = process.wait()
            except GseekError as exc:
                # Audit whatever ran: ExecutionError carries structured fields;
                # parse failures fall back to the (closed) process state.
                if not getattr(exc, "command_records", None):
                    exc.command_records = (  # type: ignore[attr-defined]
                        _command_record_from_error(
                            last,
                            exc
                            if isinstance(exc, ExecutionError)
                            else _execution_like(exc, streamed),
                            phase="align",
                        ),
                    )
                align_commands = (*leading, *getattr(exc, "command_records", ()))
                raise
            align_commands = (*leading, _command_record(result, phase="align"))
            records = parsed
        else:
            align_commands = _run_commands(plan.commands, executor, phase="align")
            records = tuple(align_adapter.parse_align(parse_ctx))

        normalized = _checked_records(records, AlignmentRecord, spec.name)
        if verify_inputs:
            verify_refs_unchanged(query_refs, role="query")
            verify_refs_unchanged(target_refs, role="target")
        runtime_s = time.monotonic() - align_started
        stats = build_alignment_stats(
            normalized,
            query_count=len(query_refs),
            target_count=len(target_refs),
            runtime_s=runtime_s,
        )
        commands = (*index_commands, *align_commands)
        manifest = None
        if record_manifest:
            manifest = _build_manifest(
                spec=spec,
                request=effective_request,
                query_refs=query_refs,
                target_refs=target_refs,
                params=params,
                tools=tools,
                index=index,
                commands=commands,
                started=started,
                env=executor.base_env,
                result_hash_value=alignment_result_hash(normalized),
            )
        result = AlignmentResult(
            backend=spec.name,
            records=normalized,
            inputs=query_refs,
            targets=target_refs,
            params=params,
            stats=stats,
            index=index,
            manifest=manifest,
            sort_records=sort_records,
        )
        if destination is not None:
            assert manifest is not None
            write_manifest(destination, manifest)
        return result
    except GseekError as exc:
        # A failed run still leaves an audit trail: the commands that already
        # ran, the failure itself, and a FAILED status for verify/replay.
        if record_manifest:
            failed_manifest = _build_manifest(
                spec=spec,
                request=effective_request,
                query_refs=query_refs,
                target_refs=target_refs,
                params=params,
                tools=tools,
                index=index,
                commands=(
                    *index_commands,
                    *align_commands,
                    *getattr(exc, "command_records", ()),
                ),
                started=started,
                env=executor.base_env,
                status=RunStatus.FAILED,
                errors=(
                    Issue(
                        code="RUN_FAILED",
                        message=str(exc),
                        severity="error",
                        context={"error": exc.code, "backend": spec.name},
                    ),
                ),
            )
            if destination is not None:
                write_manifest(destination, failed_manifest)
        raise
    finally:
        if lease is not None:
            lease.close()
        workspace.cleanup()


def _index_will_run(spec: BackendSpec, request: AlignRequest, store: IndexStore) -> bool:
    """Whether the request needs a content-addressed target index key."""
    return (
        store.enabled
        and spec.capabilities.index_scope.value != "none"
        and request.index_policy is not IndexPolicy.NONE
    )


def iter_alignment(
    request: AlignRequest,
    *,
    backend: str | None = None,
    adapter: AlignmentAdapter | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
    query_refs: tuple[InputRef, ...] | None = None,
    target_refs: tuple[InputRef, ...] | None = None,
    prepare_refs: bool = False,
    verify_inputs: bool = False,
) -> Iterator[AlignmentRecord]:
    """Yield normalized alignment records as they are parsed.

    This is the low-memory sibling of :func:`run_alignment`: no
    ``AlignmentResult``, stats, or manifest is built, and records are
    validated one at a time. The workspace and any streamed child are closed
    when the iterator is exhausted or closed early. Callers that need
    provenance should use :func:`run_alignment` instead.

    ``prepare_refs=False`` (the default) avoids hashing the query entirely and
    hashes the target only when a content-addressed index needs its key. An
    adapter that consumes ``PlanContext.query_refs``/``target_refs`` should
    opt in with ``prepare_refs=True`` or pass explicit refs.
    """
    selected = resolve_adapter(
        backend=backend,
        adapter=adapter,
        request=request,
        output_kind=OutputKind.ALIGNMENT,
    )
    if not isinstance(selected, AlignmentAdapter):
        raise UnsupportedFeatureError("selected adapter does not implement alignment")
    align_adapter: AlignmentAdapter = selected
    spec = align_adapter.spec

    resolver = resolver or _default_resolver()
    executor = executor or Executor()
    store = store or IndexStore()
    probe = align_adapter.probe(resolver, strict=True, include_optional=False)
    tools = probe.tools
    executor = executor.with_tool_paths(
        {tool.name: tool.path for tool in tools if tool.path is not None}
    )

    if query_refs:
        validate_ref_paths(query_refs, request.query, role="query", allow_empty=True)
    if target_refs:
        validate_ref_paths(target_refs, request.target, role="target", allow_empty=True)
    if query_refs is None:
        query_refs = (
            _prepare_queries(request, compute_records=False)
            if prepare_refs or verify_inputs
            else ()
        )
    if target_refs is None:
        if prepare_refs or verify_inputs or _index_will_run(spec, request, store):
            target_files, target_refs = _prepare_targets(request, compute_records=False)
        else:
            target_files = expand_fasta_paths(request.target)
            target_refs = ()
    else:
        target_files = tuple(Path(ref.path) for ref in target_refs)
    effective_request = AlignRequest(
        query=request.query,
        target=target_files,
        query_kind=request.query_kind,
        paired=request.paired,
        params=request.params,
        threads=request.threads,
        timeout_s=request.timeout_s,
        index_policy=request.index_policy,
        target_filter=request.target_filter,
        work_dir=request.work_dir,
        keep_work=request.keep_work,
    )
    bindings = _validate_tools_hook(align_adapter, effective_request, probe, resolver)
    if bindings:
        executor = executor.with_tool_paths(bindings)

    index, _index_commands, lease = _ensure_index(
        adapter=align_adapter,
        request=effective_request,
        target_refs=target_refs,
        tools=tools,
        probe=probe,
        executor=executor,
        store=store,
        policy=request.index_policy,
    )
    try:
        workspace = create_workspace(
            prefix=f"gseek-{spec.name}-",
            parent=request.work_dir,
            keep=request.keep_work,
        )
    except BaseException:
        if lease is not None:
            lease.close()
        raise
    try:
        index_dir = store.path_for(spec.name, index.key) if index is not None else None
        ctx = PlanContext(
            workspace=workspace.root,
            index_dir=index_dir,
            threads=request.threads,
            query_refs=query_refs,
            target_refs=target_refs,
        )
        plan = align_adapter.plan_align(effective_request, index, ctx)
        parse_ctx = ParseContext(
            request=effective_request,
            plan=plan,
            workspace=workspace.root,
            index=index,
            tools=tuple(tool.to_tool_ref() for tool in tools),
        )

        def checked(records: Iterator[object]) -> Iterator[AlignmentRecord]:
            for record in records:
                if not isinstance(record, AlignmentRecord):
                    raise ParseError(
                        f"alignment adapter {spec.name!r} returned a non-AlignmentRecord value",
                        context={"backend": spec.name},
                    )
                yield record

        if plan.stream:
            if not plan.commands:
                raise ConfigError("streaming alignment plan has no commands")
            _run_commands(plan.commands[:-1], executor, phase="align")
            last = plan.commands[-1]
            with executor.stream(last) as process:
                yield from checked(align_adapter.parse_align(parse_ctx, process.iter_lines()))
                process.wait()
        else:
            _run_commands(plan.commands, executor, phase="align")
            yield from checked(align_adapter.parse_align(parse_ctx))
        if verify_inputs:
            if not query_refs or not target_refs:
                raise ConfigError(
                    "iter_align(verify_inputs=True) requires prepared query and target refs",
                    context={"backend": spec.name},
                )
            verify_refs_unchanged(query_refs, role="query")
            verify_refs_unchanged(target_refs, role="target")
    finally:
        if lease is not None:
            lease.close()
        workspace.cleanup()


def run_distance(
    request: DistanceRequest,
    *,
    backend: str | None = None,
    adapter: DistanceAdapter | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
    manifest_path: Path | None = None,
    record_manifest: bool = True,
    query_refs: tuple[InputRef, ...] | None = None,
    target_refs: tuple[InputRef, ...] | None = None,
    verify_inputs: bool = False,
) -> DistanceResult:
    """Run one MinHash/distance request.

    ``query_refs``/``target_refs`` accept precomputed input references so
    callers that already hashed the files do not pay the scan again.
    ``verify_inputs=True`` re-hashes them after the command and rejects a run
    whose input changed mid-flight.
    """
    started = time.monotonic()
    # Config errors fail fast: before any probe, index build, or command runs.
    destination = _prepare_manifest_path(manifest_path)
    if destination is not None and not record_manifest:
        raise ConfigError("manifest_path requires record_manifest=True")
    selected = resolve_adapter(
        backend=backend,
        adapter=adapter,
        request=request,
        output_kind=OutputKind.DISTANCE,
    )
    if not isinstance(selected, DistanceAdapter):
        raise UnsupportedFeatureError("selected adapter does not implement distance")
    distance_adapter: DistanceAdapter = selected
    spec = distance_adapter.spec

    resolver = resolver or _default_resolver()
    executor = executor or Executor()
    store = store or IndexStore()
    probe = distance_adapter.probe(resolver, strict=True, include_optional=False)
    tools = probe.tools
    executor = executor.with_tool_paths(
        {tool.name: tool.path for tool in tools if tool.path is not None}
    )

    if query_refs is not None:
        validate_ref_paths(query_refs, request.query, role="query")
    if target_refs is not None:
        validate_ref_paths(target_refs, request.reference, role="target")
    if query_refs is None:
        query_refs = _prepare_queries(request, compute_records=record_manifest)
    if target_refs is None:
        target_files, target_refs = _prepare_targets(request, compute_records=record_manifest)
    else:
        target_files = tuple(Path(ref.path) for ref in target_refs)
    effective_request = DistanceRequest(
        query=request.query,
        reference=target_files,
        query_kind=request.query_kind,
        params=request.params,
        threads=request.threads,
        timeout_s=request.timeout_s,
        index_policy=request.index_policy,
        external_reference=request.external_reference,
        external_sha256=request.external_sha256,
        external_size=request.external_size,
        work_dir=request.work_dir,
        keep_work=request.keep_work,
    )
    bindings = _validate_tools_hook(distance_adapter, effective_request, probe, resolver)
    if bindings:
        executor = executor.with_tool_paths(bindings)
    params = _effective_params(distance_adapter, effective_request)

    index, index_commands, lease = _ensure_index(
        adapter=distance_adapter,
        request=effective_request,
        target_refs=target_refs,
        tools=tools,
        probe=probe,
        executor=executor,
        store=store,
        policy=request.index_policy,
    )
    distance_commands: tuple[CommandRecord, ...] = ()
    try:
        workspace = create_workspace(
            prefix=f"gseek-{spec.name}-",
            parent=request.work_dir,
            keep=request.keep_work,
        )
    except BaseException:
        if lease is not None:
            lease.close()
        raise
    try:
        index_dir = store.path_for(spec.name, index.key) if index is not None else None
        ctx = PlanContext(
            workspace=workspace.root,
            index_dir=index_dir,
            threads=request.threads,
            query_refs=query_refs,
            target_refs=target_refs,
        )
        plan = distance_adapter.plan_distance(effective_request, index, ctx)
        parse_ctx = ParseContext(
            request=effective_request,
            plan=plan,
            workspace=workspace.root,
            index=index,
            tools=tuple(tool.to_tool_ref() for tool in tools),
        )

        align_started = time.monotonic()
        records: tuple[Any, ...]
        if plan.stream:
            if not plan.commands:
                raise ConfigError("streaming distance plan has no commands")
            leading = _run_commands(plan.commands[:-1], executor, phase="distance")
            distance_commands = leading
            last = plan.commands[-1]
            streamed: StreamedProcess | None = None
            try:
                with executor.stream(last) as process:
                    streamed = process
                    parsed = tuple(distance_adapter.parse_distance(parse_ctx, process.iter_lines()))
                    result = process.wait()
            except GseekError as exc:
                if not getattr(exc, "command_records", None):
                    exc.command_records = (  # type: ignore[attr-defined]
                        _command_record_from_error(
                            last,
                            (
                                exc
                                if isinstance(exc, ExecutionError)
                                else _execution_like(exc, streamed)
                            ),
                            phase="distance",
                        ),
                    )
                distance_commands = (*leading, *getattr(exc, "command_records", ()))
                raise
            distance_commands = (*leading, _command_record(result, phase="distance"))
            records = parsed
        else:
            distance_commands = _run_commands(plan.commands, executor, phase="distance")
            records = tuple(distance_adapter.parse_distance(parse_ctx))

        normalized = _checked_records(records, DistanceRecord, spec.name)
        if verify_inputs:
            verify_refs_unchanged(query_refs, role="query")
            verify_refs_unchanged(target_refs, role="target")
        runtime_s = time.monotonic() - align_started
        stats = DistanceStats(
            record_count=len(normalized),
            query_count=len({record.query_id for record in normalized}),
            reference_count=len({record.reference_id for record in normalized}),
            runtime_s=runtime_s,
        )
        commands = (*index_commands, *distance_commands)
        manifest = None
        if record_manifest:
            manifest = _build_manifest(
                spec=spec,
                request=effective_request,
                query_refs=query_refs,
                target_refs=target_refs,
                params=params,
                tools=tools,
                index=index,
                commands=commands,
                started=started,
                env=executor.base_env,
                result_hash_value=distance_result_hash(normalized),
            )
        result = DistanceResult(
            backend=spec.name,
            records=normalized,
            inputs=query_refs,
            references=target_refs,
            params=params,
            stats=stats,
            index=index,
            manifest=manifest,
        )
        if destination is not None:
            assert manifest is not None
            write_manifest(destination, manifest)
        return result
    except GseekError as exc:
        if record_manifest:
            failed_manifest = _build_manifest(
                spec=spec,
                request=effective_request,
                query_refs=query_refs,
                target_refs=target_refs,
                params=params,
                tools=tools,
                index=index,
                commands=(
                    *index_commands,
                    *distance_commands,
                    *getattr(exc, "command_records", ()),
                ),
                started=started,
                env=executor.base_env,
                status=RunStatus.FAILED,
                errors=(
                    Issue(
                        code="RUN_FAILED",
                        message=str(exc),
                        severity="error",
                        context={"error": exc.code, "backend": spec.name},
                    ),
                ),
            )
            if destination is not None:
                write_manifest(destination, failed_manifest)
        raise
    finally:
        if lease is not None:
            lease.close()
        workspace.cleanup()


__all__ = ["resolve_adapter", "run_alignment", "run_distance"]
