"""Convenience public API for alignment, distance, and registry queries."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path

from gseek.adapter import AlignmentAdapter, AlignRequest, DistanceAdapter, DistanceRequest
from gseek.capabilities import BackendSpec
from gseek.errors import ConfigError, InputError
from gseek.execution import Executor, ToolResolver
from gseek.index import IndexPolicy, IndexStore
from gseek.model import (
    AlignmentRecord,
    AlignmentResult,
    DistanceResult,
    InputRef,
    Issue,
    RunManifest,
)
from gseek.provenance import read_manifest, verify_manifest
from gseek.registry import (
    backend_specs,
    find_backends,
    get_backend,
    register_adapter,
)
from gseek.replay import ReplayResult
from gseek.replay import replay as _replay
from gseek.runner import iter_alignment, run_alignment, run_distance
from gseek.types import InputKind, JsonValue, Molecule, OutputKind


def _as_paths(value: Path | str | Sequence[Path | str]) -> tuple[Path, ...]:
    if isinstance(value, (str, Path)):
        return (Path(value),)
    paths = tuple(Path(item) for item in value)
    if not paths:
        raise InputError("at least one path is required")
    return paths


def _coerce_query_kind(query_kind: InputKind | str | None) -> InputKind | None:
    if not isinstance(query_kind, str):
        return query_kind
    try:
        return InputKind(query_kind)
    except ValueError as exc:
        raise ConfigError(
            f"unknown query kind {query_kind!r}; expected one of "
            f"{[kind.value for kind in InputKind]}",
            context={"query_kind": query_kind},
        ) from exc


def _precomputed_refs(
    refs: Sequence[InputRef] | None,
    paths: tuple[Path, ...],
    *,
    role: str,
) -> tuple[InputRef, ...] | None:
    """Validate a precomputed ref tuple against the request's path count."""
    if refs is None:
        return None
    materialized = tuple(refs)
    if len(materialized) != len(paths):
        raise ConfigError(
            f"{role}_refs has {len(materialized)} ref(s), but {role} has {len(paths)} path(s)",
            context={"role": role, "refs": str(len(materialized)), "paths": str(len(paths))},
        )
    return materialized


def _align_request(
    *,
    query: Path | str | Sequence[Path | str],
    target: Path | str | Sequence[Path | str],
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
) -> AlignRequest:
    """Build and validate the shared alignment request for align/iter_align."""
    merged_params: dict[str, JsonValue] = dict(params or {})
    if preset is not None:
        existing = merged_params.get("preset")
        if existing is not None and str(existing) != str(preset):
            raise ConfigError(
                f"preset={preset!r} conflicts with params['preset']={existing!r}",
                context={"preset": str(preset), "params_preset": str(existing)},
            )
        merged_params["preset"] = preset
    try:
        policy = IndexPolicy(index)
    except ValueError as exc:
        raise ConfigError(
            f"unknown index policy {index!r}; expected one of "
            f"{[policy.value for policy in IndexPolicy]}",
            context={"index": str(index)},
        ) from exc
    return AlignRequest(
        query=_as_paths(query),
        target=_as_paths(target),
        query_kind=_coerce_query_kind(query_kind),
        paired=paired,
        params=merged_params,
        threads=threads,
        timeout_s=timeout_s,
        index_policy=policy,
        target_filter=frozenset(target_filter) if target_filter is not None else None,
        work_dir=Path(work_dir) if work_dir is not None else None,
        keep_work=keep_work,
    )


def align(
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
    query_refs: Sequence[InputRef] | None = None,
    target_refs: Sequence[InputRef] | None = None,
    sort_records: bool = True,
) -> AlignmentResult:
    """Align one query (or read pair) against a target set.

    This is the main entry point for consumers such as gside/gapit/gmlst.
    The adapter is chosen explicitly via ``backend`` or unambiguously from the
    request capabilities.

    Args:
        query: One FASTA/FASTQ path, or a sequence of paths. Two FASTQ paths
            with ``paired=True`` are treated as mates.
        target: One or more target FASTA paths.
        backend: Registered backend name, for example ``"blast"`` or
            ``"minimap2"``. Mutually exclusive with ``adapter``.
        adapter: Explicit adapter instance, useful for plugins and tests.
        query_kind: Override input detection (``fasta``/``fastq``).
        paired: Treat two query paths as paired-end reads.
        params: Adapter-specific parameters. See the adapter matrix.
        preset: Shorthand for ``params["preset"]``.
        threads: Tool worker threads (``>= 1``).
        timeout_s: Per-command timeout; ``None`` uses the adapter default.
        index: ``auto`` reuses or builds a content-addressed target index,
            ``reuse`` requires a cache hit, ``rebuild`` replaces it, and
            ``none`` runs in subject mode where supported.
        target_filter: Optional target IDs to keep after parsing.
        work_dir: Parent directory for temporary run workspaces.
        keep_work: Keep the workspace for debugging.
        manifest: Write a ``gseek.run/1`` manifest for ``verify``/``replay``.
        resolver: Custom tool resolver.
        executor: Custom subprocess executor.
        store: Custom index store.
        query_refs: Precomputed input references for ``query`` (for example
            from :func:`gseek.provenance.input_refs`). Avoids re-hashing the
            query; the tuple length must match ``query``.
        target_refs: Precomputed input references for ``target``. Callers
            that run many queries against one database can hash the target
            once and pass the same refs for every call.
        sort_records: Apply the default canonical ordering. Pass ``False``
            when the consumer immediately re-sorts or aggregates the rows.

    Returns:
        An :class:`~gseek.AlignmentResult` with normalized records, stats,
        references, and the optional manifest.

    Raises:
        GseekError: Base class for typed failures such as missing tools,
            unsupported capabilities, parse errors, or index failures.
    """
    request = _align_request(
        query=query,
        target=target,
        query_kind=query_kind,
        paired=paired,
        params=params,
        preset=preset,
        threads=threads,
        timeout_s=timeout_s,
        index=index,
        target_filter=target_filter,
        work_dir=work_dir,
        keep_work=keep_work,
    )
    return run_alignment(
        request,
        backend=backend,
        adapter=adapter,
        resolver=resolver,
        executor=executor,
        store=store,
        manifest_path=Path(manifest) if manifest is not None else None,
        record_manifest=record_manifest,
        query_refs=_precomputed_refs(query_refs, request.query, role="query"),
        target_refs=_precomputed_refs(target_refs, request.target, role="target"),
        sort_records=sort_records,
    )


def iter_align(
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
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
    query_refs: Sequence[InputRef] | None = None,
    target_refs: Sequence[InputRef] | None = None,
    prepare_refs: bool = False,
) -> Iterator[AlignmentRecord]:
    """Yield alignment records as they are parsed (no materialized result).

    This is the low-memory sibling of :func:`align`: it does not build an
    ``AlignmentResult``, stats, or a manifest, so it has no ``manifest`` /
    ``record_manifest`` parameters. Close the iterator early to release the
    child process and workspace.

    Args match :func:`align` for the shared alignment inputs; ``query_refs`` /
    ``target_refs`` are precomputed :class:`~gseek.model.InputRef` tuples.
    By default queries are not hashed and the target is hashed only when a
    content-addressed index needs its key. Pass ``prepare_refs=True`` when a
    custom adapter consumes ``PlanContext`` refs.
    """
    request = _align_request(
        query=query,
        target=target,
        query_kind=query_kind,
        paired=paired,
        params=params,
        preset=preset,
        threads=threads,
        timeout_s=timeout_s,
        index=index,
        target_filter=target_filter,
        work_dir=work_dir,
        keep_work=keep_work,
    )
    return iter_alignment(
        request,
        backend=backend,
        adapter=adapter,
        resolver=resolver,
        executor=executor,
        store=store,
        query_refs=_precomputed_refs(query_refs, request.query, role="query"),
        target_refs=_precomputed_refs(target_refs, request.target, role="target"),
        prepare_refs=prepare_refs,
    )


def distance(
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
) -> DistanceResult:
    """Compare queries against sketch/distance references.

    Args:
        query: One or more FASTA/FASTQ query paths.
        reference: Reference FASTA paths, or one pre-built distance artifact
            when ``external_reference=True``.
        backend: Registered backend name (``mash``, ``sourmash``, ``skani``).
        adapter: Explicit distance adapter instance.
        query_kind: Override input detection.
        params: Adapter-specific parameters (for example mash ``k`` or
            sourmash ``scaled``/``mode``).
        threads: Tool worker threads.
        timeout_s: Per-command timeout; ``None`` uses the adapter default.
        index: Content-addressed index policy for FASTA references.
        external_reference: Treat ``reference`` as a pre-built ``.msh``,
            ``.sig``, or ``.zip`` artifact, or as a sourmash rocksdb / skani
            database directory. No index is built or referenced.
        external_sha256: Optional pre-computed SHA-256 for the external
            reference. Requires ``external_size``; skips re-hashing.
        external_size: Size in bytes of the external reference (regular-file
            bytes for a directory tree). Validated against the path.
        work_dir: Parent directory for temporary run workspaces.
        keep_work: Keep the workspace for debugging.
        manifest: Write a ``gseek.run/1`` manifest for ``verify``/``replay``.
        resolver: Custom tool resolver.
        executor: Custom subprocess executor.
        store: Custom index store.

    Returns:
        A :class:`~gseek.DistanceResult` with normalized distance records.

    Raises:
        GseekError: Base class for typed failures such as missing tools,
            unsupported capabilities, parse errors, or index failures.
    """
    try:
        policy = IndexPolicy(index)
    except ValueError as exc:
        raise ConfigError(
            f"unknown index policy {index!r}; expected one of "
            f"{[policy.value for policy in IndexPolicy]}",
            context={"index": str(index)},
        ) from exc
    request = DistanceRequest(
        query=_as_paths(query),
        reference=_as_paths(reference),
        query_kind=_coerce_query_kind(query_kind),
        params=dict(params or {}),
        threads=threads,
        timeout_s=timeout_s,
        index_policy=policy,
        external_reference=external_reference,
        external_sha256=external_sha256,
        external_size=external_size,
        work_dir=Path(work_dir) if work_dir is not None else None,
        keep_work=keep_work,
    )
    return run_distance(
        request,
        backend=backend,
        adapter=adapter,
        resolver=resolver,
        executor=executor,
        store=store,
        manifest_path=Path(manifest) if manifest is not None else None,
        record_manifest=record_manifest,
    )


def backends() -> tuple[BackendSpec, ...]:
    """Return all registered backend specs."""
    return backend_specs()


def find(
    *,
    input: InputKind | str | None = None,
    output: OutputKind | str | None = None,
    paired: bool | None = None,
    molecule: Molecule | str | None = None,
) -> tuple[BackendSpec, ...]:
    """Find registered backends matching capability constraints."""
    return find_backends(input=input, output=output, paired=paired, molecule=molecule)


def verify(
    manifest: Path | str | RunManifest,
    *,
    base_dir: Path | str | None = None,
) -> tuple[Issue, ...]:
    """Verify the file hashes recorded in a run manifest.

    When ``manifest`` is a path and ``base_dir`` is omitted, relative paths in
    a legacy manifest are resolved against the manifest's directory, matching
    :func:`gseek.replay`. New manifests record absolute input paths.
    """
    if isinstance(manifest, RunManifest):
        document = manifest
        resolved_base = Path(base_dir) if base_dir is not None else None
    else:
        path = Path(manifest)
        document = read_manifest(path)
        resolved_base = Path(base_dir) if base_dir is not None else path.resolve().parent
    return verify_manifest(document, base_dir=resolved_base)


def replay(
    manifest: Path | str,
    *,
    check: bool = False,
    base_dir: Path | str | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> ReplayResult:
    """Re-run a manifest recipe by re-planning through its adapter."""
    return _replay(
        manifest,
        check=check,
        base_dir=base_dir,
        resolver=resolver,
        executor=executor,
        store=store,
    )


__all__ = [
    "align",
    "backends",
    "distance",
    "find",
    "find_backends",
    "get_backend",
    "register_adapter",
    "replay",
    "verify",
]
