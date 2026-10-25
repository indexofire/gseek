"""sourmash adapter: DNA sketches and sourmash search CSV."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import cast

from gseek.adapter import (
    AlignPlan,
    BaseAdapter,
    DistanceRequest,
    IndexPlan,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement, supports
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import detect_input_kind
from gseek.model import DistanceRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind

_DEFAULT_K = 31
_DEFAULT_SCALED = 1000
_DEFAULT_THRESHOLD = 0.0
_DEFAULT_THRESHOLD_BP = 50.0
_ALLOWED_PARAMS = frozenset(
    {
        "k",
        "scaled",
        "threshold",
        "containment",
        "num_results",
        "mode",
        "threshold_bp",
        "threads",
    }
)


class SourmashAdapter(BaseAdapter):
    """sourmash sketch/search adapter for Jaccard or containment distances."""

    spec = BackendSpec(
        name="sourmash",
        family="minhash",
        summary="sourmash MinHash search (Jaccard/containment)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA, InputKind.FASTQ}),
            outputs=frozenset({OutputKind.DISTANCE}),
            paired=False,
            index_scope=IndexScope.TARGET,
            streaming=False,
            threads=False,
        ),
        tools=(
            ToolRequirement(
                binary="sourmash",
                role="distance",
                version_args=("--version",),
                version_pattern=r"sourmash\s+(\d+)\.(\d+)\.(\d+)",
                min_version=(4, 0, 0),
                hint="install sourmash (conda-forge/bioconda package `sourmash`)",
            ),
        ),
    )

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            "k": params.get("k", _DEFAULT_K),
            "scaled": params.get("scaled", _DEFAULT_SCALED),
        }

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        _validate_params(request.params)
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        index_dir.mkdir(parents=True, exist_ok=True)
        signature = index_dir / "targets.sig"
        argv = _sketch_argv(request.target, signature, request.params)
        return IndexPlan(
            commands=(CommandPlan(argv=tuple(argv), label="sourmash sketch references"),),
            entry="targets.sig",
            artifacts=("targets.sig",),
            params=self.index_params(request.params),
        )

    def plan_distance(
        self,
        request: DistanceRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        _validate_request(request)
        if len(request.query) != 1:
            raise UnsupportedFeatureError(
                "sourmash search/gather accepts exactly one query file per run",
                context={"query_count": str(len(request.query))},
            )
        params = _validate_params(request.params)
        mode = str(params.get("mode", "search")).lower()
        if mode not in {"search", "gather"}:
            raise UnsupportedFeatureError(
                f"unknown sourmash mode {mode!r}; expected 'search' or 'gather'",
                context={"mode": mode},
            )
        index_dir = (ctx.index_dir or ctx.workspace).resolve()
        query_signature = ctx.path("query.sig")
        matches = ctx.path("matches.csv")
        commands: list[CommandPlan] = [
            CommandPlan(
                argv=tuple(_sketch_argv(request.query, query_signature, params)),
                label="sourmash sketch queries",
                timeout_s=request.timeout_s,
            )
        ]

        if request.external_reference:
            reference_signature = request.reference[0]
        elif index is not None:
            reference_signature = index_dir / index.entry
        else:
            reference_signature = ctx.path("references.sig")
            commands.append(
                CommandPlan(
                    argv=tuple(_sketch_argv(request.reference, reference_signature, params)),
                    label="sourmash sketch references",
                    timeout_s=request.timeout_s,
                )
            )

        if mode == "gather":
            gather_argv = [
                "sourmash",
                "gather",
                str(query_signature),
                str(reference_signature),
                "--threshold-bp",
                str(params.get("threshold_bp", _DEFAULT_THRESHOLD_BP)),
                "-o",
                str(matches),
            ]
            commands.append(
                CommandPlan(
                    argv=tuple(gather_argv),
                    label="sourmash gather",
                    timeout_s=request.timeout_s,
                )
            )
            return AlignPlan(
                commands=tuple(commands),
                stream=False,
                result_files=(matches,),
            )

        search_argv: list[str] = [
            "sourmash",
            "search",
            str(query_signature),
            str(reference_signature),
            "--threshold",
            str(params.get("threshold", _DEFAULT_THRESHOLD)),
        ]
        if params.get("containment"):
            search_argv.append("--containment")
        if "num_results" in params:
            search_argv.extend(["-n", str(params["num_results"])])
        search_argv.extend(["-o", str(matches)])
        commands.append(
            CommandPlan(
                argv=tuple(search_argv),
                label="sourmash search",
                timeout_s=request.timeout_s,
            )
        )
        return AlignPlan(
            commands=tuple(commands),
            stream=False,
            result_files=(matches,),
        )

    def parse_distance(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[DistanceRecord]:
        if stream is not None:
            raise UnsupportedFeatureError(
                "sourmash parses its CSV result file; streaming input is not supported",
                context={"backend": "sourmash"},
            )
        path = ctx.plan.result_files[0] if ctx.plan.result_files else None
        request = cast(DistanceRequest, ctx.request)
        mode = str(request.params.get("mode", "search")).lower()
        if path is None or not path.is_file():
            if mode == "gather":
                return
            raise ParseError(
                "sourmash search CSV not found",
                path=str(path) if path is not None else None,
            )
        if path.stat().st_size == 0:
            # `sourmash search`/`gather` write a 0-byte CSV (exit 0) when
            # nothing passes the threshold: a legitimate empty result.
            return
        if mode == "gather":
            yield from _parse_gather(path, request)
            return
        containment_mode = bool(request.params.get("containment", False))
        k = str(request.params.get("k", _DEFAULT_K))
        scaled = str(request.params.get("scaled", _DEFAULT_SCALED))

        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ParseError("sourmash CSV has no header", path=str(path))
            score_column = (
                "similarity"
                if "similarity" in reader.fieldnames
                else "containment"
                if "containment" in reader.fieldnames
                else None
            )
            if score_column is None:
                raise ParseError(
                    "sourmash CSV lacks similarity/containment column",
                    path=str(path),
                )
            references = {"filename", "name", "md5"} & set(reader.fieldnames)
            queries = {"query_filename", "query_name", "query_md5"} & set(reader.fieldnames)
            if not references or not queries:
                raise ParseError(
                    "sourmash CSV lacks reference/query identifier columns",
                    path=str(path),
                )
            for line_number, row in enumerate(reader, start=2):
                if not any(value for value in row.values() if value):
                    continue
                raw_reference = _first_value(row, ("filename", "name", "md5"))
                raw_query = _first_value(row, ("query_filename", "query_name", "query_md5"))
                if not raw_reference or not raw_query:
                    raise ParseError(
                        "sourmash row has no reference/query identifier",
                        path=str(path),
                        line_number=line_number,
                    )
                reference_id = _reference_id(row)
                query_id = _query_id(row, request)
                try:
                    score = float(row.get(score_column) or "")
                except (TypeError, ValueError) as exc:
                    raise ParseError(
                        f"invalid sourmash similarity: {exc}",
                        path=str(path),
                        line_number=line_number,
                    ) from exc
                if not 0.0 <= score <= 1.0:
                    raise ParseError(
                        f"sourmash score out of range: {score}",
                        path=str(path),
                        line_number=line_number,
                    )
                ani = _optional_float(row.get("ani"))
                if ani is not None and 0.0 <= ani <= 1.0:
                    ani *= 100.0
                yield DistanceRecord(
                    query_id=query_id,
                    reference_id=reference_id,
                    distance=max(0.0, min(1.0, 1.0 - score)),
                    containment=score if containment_mode else None,
                    jaccard=None if containment_mode else score,
                    ani=ani,
                    tags={
                        "metric": (
                            "sourmash_containment" if containment_mode else "sourmash_similarity"
                        ),
                        "k": k,
                        "scaled": scaled,
                        "reference_signature": raw_reference,
                        "query_signature": raw_query,
                        **_identifier_tags(row),
                    },
                )


def _parse_gather(path: Path, request: DistanceRequest) -> Iterator[DistanceRecord]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ParseError("sourmash gather CSV has no header", path=str(path))
        if "f_orig_query" not in reader.fieldnames:
            raise ParseError(
                "sourmash gather CSV lacks f_orig_query",
                path=str(path),
            )
        for line_number, row in enumerate(reader, start=2):
            if not any(value for value in row.values() if value):
                continue
            raw_reference = _first_value(row, ("filename", "name", "md5"))
            raw_query = _first_value(row, ("query_filename", "query_name", "query_md5"))
            if not raw_reference or not raw_query:
                raise ParseError(
                    "sourmash gather row has no reference/query identifier",
                    path=str(path),
                    line_number=line_number,
                )
            try:
                containment = float(row.get("f_orig_query") or "")
            except (TypeError, ValueError) as exc:
                raise ParseError(
                    f"invalid sourmash gather f_orig_query: {exc}",
                    path=str(path),
                    line_number=line_number,
                ) from exc
            reference_id = _reference_id(row)
            ani = _optional_float(row.get("average_containment_ani"))
            if ani is not None and 0.0 <= ani <= 1.0:
                ani *= 100.0
            tags = {
                "metric": "sourmash_gather",
                "reference_signature": raw_reference,
                "query_signature": raw_query,
                **_identifier_tags(row),
            }
            for key in (
                "f_match",
                "f_unique_weighted",
                "intersect_bp",
                "gather_result_rank",
                "remaining_bp",
                "ksize",
                "scaled",
            ):
                if row.get(key):
                    tags[key] = row[key]
            yield DistanceRecord(
                query_id=_query_id(row, request),
                reference_id=reference_id,
                containment=containment,
                ani=ani,
                tags=tags,
            )


def _query_id(row: Mapping[str, str], request: DistanceRequest) -> str:
    name = (row.get("query_name") or "").strip()
    if name:
        return name
    first = request.query[0] if request.query else None
    return first.name if first is not None else ""


def _identifier_tags(row: Mapping[str, str]) -> dict[str, str]:
    """Keep the raw signature identifiers for consumers that need names."""
    tags: dict[str, str] = {}
    for source, target in (
        ("name", "reference_name"),
        ("md5", "reference_md5"),
        ("filename", "reference_filename"),
    ):
        value = (row.get(source) or "").strip()
        if value:
            tags[target] = value
    return tags


def _sketch_argv(
    inputs: tuple[Path, ...],
    signature: Path,
    params: Mapping[str, JsonValue],
) -> list[str]:
    k = params.get("k", _DEFAULT_K)
    scaled = params.get("scaled", _DEFAULT_SCALED)
    argv = [
        "sourmash",
        "sketch",
        "dna",
        "-p",
        f"k={k},scaled={scaled}",
        "-o",
        str(signature),
    ]
    argv.extend(str(path.resolve()) for path in inputs)
    return argv


def _first_value(row: Mapping[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


_SIGNATURE_SUFFIXES = frozenset({".sig", ".zip", ".sbt"})


def _reference_id(row: Mapping[str, str]) -> str:
    """Pick a stable, unique reference ID from a sourmash CSV row.

    ``filename`` is the signature file, not the genome; a gseek-built
    ``targets.sig`` contains every reference, so using its basename would make
    all rows collide. Prefer an explicit signature ``name``, then a genome-like
    filename, then the unique ``md5``.
    """
    name = (row.get("name") or "").strip()
    if name:
        return name
    filename = (row.get("filename") or "").strip()
    md5 = (row.get("md5") or "").strip()
    if filename and Path(filename).suffix.lower() not in _SIGNATURE_SUFFIXES:
        return Path(filename).name
    if md5:
        return md5
    return Path(filename).name if filename else ""


def _optional_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _validate_request(request: DistanceRequest) -> None:
    for path in request.query:
        query_kind = request.query_kind or detect_input_kind(path)
        if not supports(
            SourmashAdapter.spec,
            input_kind=query_kind,
            output_kind=OutputKind.DISTANCE,
        ):
            raise UnsupportedFeatureError(
                f"backend 'sourmash' does not support input={query_kind.value}",
                context={"backend": "sourmash", "input": query_kind.value},
            )


def _validate_params(params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise UnsupportedFeatureError(
            f"unsupported sourmash parameters: {sorted(unknown)}",
            context={"params": ", ".join(sorted(unknown))},
        )
    return {str(key): value for key, value in params.items()}


ADAPTER = SourmashAdapter

__all__ = ["ADAPTER", "SourmashAdapter"]
