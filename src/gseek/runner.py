"""Plan → execute → parse → provenance orchestration."""

from __future__ import annotations

import platform
import shutil
import time
from collections.abc import Mapping, Sequence
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
    IndexBuildError,
    InputError,
    ParseError,
    UnsupportedFeatureError,
)
from gseek.execution import (
    CommandPlan,
    Executor,
    ProcessResult,
    ToolResolver,
    create_workspace,
    env_for_manifest,
)
from gseek.index import IndexPolicy, IndexStore, compute_index_key
from gseek.io import expand_fasta_paths
from gseek.model import (
    AlignmentRecord,
    AlignmentResult,
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
    write_manifest,
)
from gseek.registry import find_backends, get_backend
from gseek.types import InputKind, JsonValue, OutputKind, RunStatus

_CommandPhase = str


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
    phase: _CommandPhase,
) -> CommandRecord:
    return CommandRecord(
        phase=phase,  # type: ignore[arg-type]
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
    phase: _CommandPhase,
) -> tuple[CommandRecord, ...]:
    records: list[CommandRecord] = []
    for command in commands:
        result = executor.run(command)
        records.append(_command_record(result, phase=phase))
    return tuple(records)


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
    tools: tuple[Any, ...],
) -> str:
    tool_versions = {tool.name: tool.version for tool in tools}
    index_params_fn = getattr(adapter, "index_params", None)
    if callable(index_params_fn):
        index_params = cast(Mapping[str, JsonValue], index_params_fn(request.params))
    else:
        index_params = adapter.semantic_params(request.params)
    return compute_index_key(
        adapter=spec.name,
        adapter_version=_adapter_identity(spec),
        targets=target_refs,
        params=index_params,
        tool_versions=tool_versions,
    )


def _ensure_index(
    *,
    adapter: AlignmentAdapter | DistanceAdapter,
    request: AlignRequest | DistanceRequest,
    target_refs: tuple[InputRef, ...],
    tools: tuple[Any, ...],
    probe: ToolchainProbe,
    executor: Executor,
    store: IndexStore,
    policy: IndexPolicy,
) -> tuple[IndexRef | None, tuple[CommandRecord, ...]]:
    spec = adapter.spec
    if (
        not store.enabled
        or spec.capabilities.index_scope.value == "none"
        or policy is IndexPolicy.NONE
        or (isinstance(request, DistanceRequest) and request.external_reference)
    ):
        return None, ()

    key = _index_key(spec, adapter, request, target_refs, tools)
    with store.lock(spec.name, key):
        existing = store.lookup(spec.name, key)
        if existing is not None and policy is not IndexPolicy.REBUILD:
            return existing, ()
        if policy is IndexPolicy.REUSE and existing is None:
            raise IndexBuildError(
                f"index {key} is not cached and index_policy=reuse",
                context={"backend": spec.name, "key": key},
            )

        staging = store.new_staging(spec.name, key)
        try:
            index_refs = tuple(tool.to_tool_ref() for tool in probe.tools if tool.path is not None)
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
                return None, ()
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
                params=plan.params or request.params,
                tool_versions={tool.name: tool.version for tool in index_refs},
                replace=policy is IndexPolicy.REBUILD,
            )
            return published, records
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
    tools: tuple[Any, ...],
    index: IndexRef | None,
    commands: tuple[CommandRecord, ...],
    started: float,
    env: Mapping[str, str] | None = None,
    result_hash_value: str | None = None,
    warnings: tuple[Issue, ...] = (),
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
        status=RunStatus.OK,
        duration_s=time.monotonic() - started,
        warnings=warnings,
        extra={
            "kind": "alignment" if isinstance(request, AlignRequest) else "distance",
            "threads": request.threads,
            "paired": isinstance(request, AlignRequest) and request.paired,
            "external_reference": (
                isinstance(request, DistanceRequest) and request.external_reference
            ),
        },
    )


def _prepare_targets(
    request: AlignRequest | DistanceRequest,
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
    return files, input_refs(files, compute_records=True)


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
) -> AlignmentResult:
    """Run one alignment request and return normalized records plus provenance."""
    started = time.monotonic()
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

    resolver = resolver or ToolResolver()
    executor = executor or Executor()
    store = store or IndexStore()
    probe = align_adapter.probe(resolver, strict=True)
    tools = probe.tools
    executor = executor.with_tool_paths(
        {tool.name: tool.path for tool in tools if tool.path is not None}
    )

    query_refs = _prepare_queries(request, compute_records=record_manifest)
    target_files, target_refs = _prepare_targets(request)
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
    params = _effective_params(align_adapter, effective_request)

    index, index_commands = _ensure_index(
        adapter=align_adapter,
        request=effective_request,
        target_refs=target_refs,
        tools=tools,
        probe=probe,
        executor=executor,
        store=store,
        policy=request.index_policy,
    )

    workspace = create_workspace(
        prefix=f"gseek-{spec.name}-",
        parent=request.work_dir,
        keep=request.keep_work,
    )
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
        align_commands: tuple[CommandRecord, ...]
        if plan.stream:
            if not plan.commands:
                raise ConfigError("streaming alignment plan has no commands")
            leading = _run_commands(plan.commands[:-1], executor, phase="align")
            last = plan.commands[-1]
            with executor.stream(last) as process:
                parsed = tuple(align_adapter.parse_align(parse_ctx, process.iter_lines()))
                result = process.wait()
            align_commands = (*leading, _command_record(result, phase="align"))
            records = parsed
        else:
            align_commands = _run_commands(plan.commands, executor, phase="align")
            records = tuple(align_adapter.parse_align(parse_ctx))

        normalized = tuple(record for record in records if isinstance(record, AlignmentRecord))
        if len(normalized) != len(records):
            raise ParseError(
                f"alignment adapter {spec.name!r} returned non-AlignmentRecord values",
                context={"backend": spec.name},
            )
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
        )
        destination = _prepare_manifest_path(manifest_path)
        if destination is not None and not record_manifest:
            raise ConfigError("manifest_path requires record_manifest=True")
        if destination is not None:
            assert manifest is not None
            write_manifest(destination, manifest)
        return result
    finally:
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
) -> DistanceResult:
    """Run one MinHash/distance request."""
    started = time.monotonic()
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

    resolver = resolver or ToolResolver()
    executor = executor or Executor()
    store = store or IndexStore()
    probe = distance_adapter.probe(resolver, strict=True)
    tools = probe.tools
    executor = executor.with_tool_paths(
        {tool.name: tool.path for tool in tools if tool.path is not None}
    )

    query_refs = _prepare_queries(request, compute_records=record_manifest)
    target_files, target_refs = _prepare_targets(request)
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
    params = _effective_params(distance_adapter, effective_request)

    index, index_commands = _ensure_index(
        adapter=distance_adapter,
        request=effective_request,
        target_refs=target_refs,
        tools=tools,
        probe=probe,
        executor=executor,
        store=store,
        policy=request.index_policy,
    )

    workspace = create_workspace(
        prefix=f"gseek-{spec.name}-",
        parent=request.work_dir,
        keep=request.keep_work,
    )
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
            request=effective_request,  # type: ignore[arg-type]
            plan=plan,
            workspace=workspace.root,
            index=index,
            tools=tuple(tool.to_tool_ref() for tool in tools),
        )

        align_started = time.monotonic()
        distance_commands: tuple[CommandRecord, ...]
        records: tuple[Any, ...]
        if plan.stream:
            if not plan.commands:
                raise ConfigError("streaming distance plan has no commands")
            leading = _run_commands(plan.commands[:-1], executor, phase="distance")
            with executor.stream(plan.commands[-1]) as process:
                parsed = tuple(distance_adapter.parse_distance(parse_ctx, process.iter_lines()))
                result = process.wait()
            distance_commands = (*leading, _command_record(result, phase="distance"))
            records = parsed
        else:
            distance_commands = _run_commands(plan.commands, executor, phase="distance")
            records = tuple(distance_adapter.parse_distance(parse_ctx))

        normalized = tuple(record for record in records if isinstance(record, DistanceRecord))
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
        destination = _prepare_manifest_path(manifest_path)
        if destination is not None and not record_manifest:
            raise ConfigError("manifest_path requires record_manifest=True")
        if destination is not None:
            assert manifest is not None
            write_manifest(destination, manifest)
        return result
    finally:
        workspace.cleanup()


__all__ = ["resolve_adapter", "run_alignment", "run_distance"]
