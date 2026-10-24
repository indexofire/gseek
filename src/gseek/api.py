"""Convenience public API for alignment, distance, and registry queries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from gseek.adapter import AlignmentAdapter, AlignRequest, DistanceAdapter, DistanceRequest
from gseek.capabilities import BackendSpec
from gseek.errors import InputError
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
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> AlignmentResult:
    """Align one query (or read pair) against a target set.

    This is the main entry point for gside/gapit/gmlst. The adapter is chosen
    explicitly via ``backend`` or unambiguously by capabilities.
    """
    merged_params: dict[str, JsonValue] = dict(params or {})
    if preset is not None:
        merged_params["preset"] = preset
    request = AlignRequest(
        query=_as_paths(query),
        target=_as_paths(target),
        query_kind=InputKind(query_kind) if isinstance(query_kind, str) else query_kind,
        paired=paired,
        params=merged_params,
        threads=threads,
        timeout_s=timeout_s,
        index_policy=IndexPolicy(index),
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
    work_dir: Path | str | None = None,
    keep_work: bool = False,
    manifest: Path | str | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> DistanceResult:
    """Compare queries against sketch/distance references."""
    request = DistanceRequest(
        query=_as_paths(query),
        reference=_as_paths(reference),
        query_kind=InputKind(query_kind) if isinstance(query_kind, str) else query_kind,
        params=dict(params or {}),
        threads=threads,
        timeout_s=timeout_s,
        index_policy=IndexPolicy(index),
        external_reference=external_reference,
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
