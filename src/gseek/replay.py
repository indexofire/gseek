"""Safe replay of a run manifest by re-planning through the adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gseek.adapter import AlignRequest, DistanceRequest
from gseek.errors import ManifestError, ReplayMismatchError
from gseek.execution import Executor, ToolResolver
from gseek.index import IndexPolicy, IndexStore
from gseek.model import AlignmentResult, DistanceResult, Issue, RunManifest
from gseek.provenance import (
    alignment_result_hash,
    distance_result_hash,
    read_manifest,
    verify_manifest,
)
from gseek.registry import get_backend
from gseek.types import InputKind, JsonValue, OutputKind


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Outcome of re-planning and re-running one manifest recipe."""

    manifest: RunManifest
    result: AlignmentResult | DistanceResult
    result_hash: str
    expected_hash: str | None
    matches: bool | None
    issues: tuple[Issue, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "backend": self.manifest.backend,
            "record_count": len(self.result.records),
            "result_hash": self.result_hash,
            "expected_hash": self.expected_hash,
            "matches": self.matches,
            "verify_issues": [issue.to_dict() for issue in self.issues],
        }


def replay(
    manifest: Path | str,
    *,
    check: bool = False,
    base_dir: Path | str | None = None,
    resolver: ToolResolver | None = None,
    executor: Executor | None = None,
    store: IndexStore | None = None,
) -> ReplayResult:
    """Re-run a manifest recipe and optionally verify its canonical result hash.

    Only ``backend``, ``params``, input paths, threads, and paired mode are
    used. Commands recorded in the manifest are never executed.
    """
    path = Path(manifest)
    document = read_manifest(path)
    root = Path(base_dir) if base_dir is not None else path.parent

    spec = get_backend(document.backend).spec
    kind = str(document.extra.get("kind", ""))
    if kind not in {"alignment", "distance"}:
        if OutputKind.ALIGNMENT in spec.capabilities.outputs:
            kind = "alignment"
        elif OutputKind.DISTANCE in spec.capabilities.outputs:
            kind = "distance"
        else:
            raise ManifestError(
                f"cannot infer replay kind for backend {document.backend!r}",
                context={"backend": document.backend},
            )

    query = tuple(_resolve(ref.path, root) for ref in document.inputs)
    targets = tuple(_resolve(ref.path, root) for ref in document.targets)
    if not query or not targets:
        raise ManifestError(
            "manifest has no inputs or targets",
            context={"backend": document.backend},
        )
    # Hash every input exactly once: the refs feed verification AND the
    # re-planned run, instead of verify and the runner each re-reading.
    from gseek.provenance import input_refs

    query_refs = input_refs(query, kind=document.inputs[0].kind)
    # Sketch references are binary artifacts: hash them, never count records.
    target_kind = document.targets[0].kind
    target_refs = input_refs(
        targets,
        kind=target_kind,
        compute_records=target_kind is not InputKind.SKETCH,
    )
    prefetched = {
        str(Path(ref.path).resolve()): (ref.size, ref.sha256) for ref in (*query_refs, *target_refs)
    }
    issues = verify_manifest(document, base_dir=root, prefetched=prefetched)
    params = dict(document.params)
    target_filter_raw = params.pop("target_filter", None)
    target_filter = (
        tuple(str(value) for value in target_filter_raw)
        if isinstance(target_filter_raw, list)
        else None
    )
    params.pop("paired", None)
    try:
        threads = int(str(document.extra.get("threads", 1)))
    except (TypeError, ValueError):
        threads = 1
    paired = bool(document.extra.get("paired", False))
    # The index policy changes the planned command sequence (subject vs db);
    # replaying under the default AUTO would fabricate result mismatches.
    index_policy = str(document.extra.get("index_policy", "auto"))
    query_kind = document.inputs[0].kind
    external_reference = bool(document.extra.get("external_reference", False)) or any(
        ref.kind is InputKind.SKETCH for ref in document.targets
    )

    if kind == "alignment":
        from gseek.runner import run_alignment

        result = run_alignment(
            AlignRequest(
                query=query,
                target=targets,
                query_kind=query_kind,
                paired=paired,
                params=params,
                threads=threads,
                index_policy=IndexPolicy(index_policy),
                target_filter=frozenset(target_filter) if target_filter else None,
            ),
            backend=document.backend,
            resolver=resolver,
            executor=executor,
            store=store,
            query_refs=query_refs,
            target_refs=target_refs,
        )
        actual = alignment_result_hash(result.records)
    else:
        from gseek.runner import run_distance

        result = run_distance(
            DistanceRequest(
                query=query,
                reference=targets,
                query_kind=query_kind,
                params=params,
                threads=threads,
                index_policy=IndexPolicy(index_policy),
                external_reference=external_reference,
                external_sha256=document.targets[0].sha256 if external_reference else None,
                external_size=document.targets[0].size if external_reference else None,
            ),
            backend=document.backend,
            resolver=resolver,
            executor=executor,
            store=store,
            query_refs=query_refs,
            target_refs=target_refs,
        )
        actual = distance_result_hash(result.records)

    expected = document.result_hash
    matches = None if expected is None else expected == actual
    if check and matches is False:
        raise ReplayMismatchError(
            f"replay result differs from manifest for backend {document.backend!r}",
            context={
                "backend": document.backend,
                "expected": expected or "",
                "actual": actual,
            },
        )
    return ReplayResult(
        manifest=document,
        result=result,
        result_hash=actual,
        expected_hash=expected,
        matches=matches,
        issues=issues,
    )


def _resolve(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


__all__ = ["ReplayResult", "replay"]
