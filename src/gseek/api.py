"""Convenience public API for alignment, distance, and registry queries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from gseek.adapter import AlignmentAdapter, AlignRequest, DistanceAdapter, DistanceRequest
from gseek.capabilities import BackendSpec
from gseek.errors import ConfigError, InputError
from gseek.execution import Executor, ToolResolver
from gseek.index import IndexPolicy, IndexStore
from gseek.model import AlignmentResult, DistanceResult, Issue, RunManifest
from gseek.provenance import read_manifest, verify_manifest
from gseek.registry import (
    backend_specs,
    find_backends,
    get_backend,
    register_adapter,
)
from gseek.replay import ReplayResult
from gseek.replay import replay as _replay
from gseek.runner import run_alignment, run_distance
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

    Returns:
        An :class:`~gseek.AlignmentResult` with normalized records, stats,
        references, and the optional manifest.

    Raises:
        GseekError: Base class for typed failures such as missing tools,
            unsupported capabilities, parse errors, or index failures.
    """
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
    request = AlignRequest(
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
    return run_alignment(
        request,
        backend=backend,
        adapter=adapter,
        resolver=resolver,
        executor=executor,
        store=store,
        manifest_path=Path(manifest) if manifest is not None else None,
        record_manifest=record_manifest,
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
