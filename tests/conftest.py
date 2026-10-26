from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import pytest

from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    BaseAdapter,
    DistanceRequest,
    IndexPlan,
    IndexRequest,
    ParseContext,
    PlanContext,
)
from gseek.capabilities import BackendSpec, Capabilities, ToolchainProbe, ToolRequirement
from gseek.execution import CommandPlan, ToolProbe, ToolResolver
from gseek.model import AlignmentRecord, DistanceRecord, IndexRef
from gseek.types import IndexScope, InputKind, JsonValue, OutputKind, Strand

_PRINT_HIT = "print('q1\\tt1\\t100.0\\t4\\t0\\t0\\t0\\t4\\t4\\t0\\t1e-5\\t100.0')"


class FakeAdapter(BaseAdapter):
    """A test adapter that plans real Python commands and parses their output."""

    spec = BackendSpec(
        name="fake",
        family="fake",
        summary="test-only backend",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.ALIGNMENT}),
            index_scope=IndexScope.TARGET,
            streaming=True,
        ),
        tools=(ToolRequirement("fake-tool", "aligner", ("--version",)),),
    )

    def __init__(self, *, stream: bool = True) -> None:
        self.stream = stream

    def probe(
        self, resolver: ToolResolver, *, strict: bool = False, include_optional: bool = True
    ) -> ToolchainProbe:
        del include_optional
        return ToolchainProbe(
            backend=self.spec.name,
            tools=(
                ToolProbe(
                    name="fake-tool",
                    path=Path(sys.executable),
                    version="1.0.0",
                    role="aligner",
                ),
            ),
            ok=True,
        )

    def index_params(self, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return {}

    def plan_index(self, request: IndexRequest, ctx: PlanContext) -> IndexPlan:
        entry = ctx.index_dir or ctx.workspace
        script = (
            "import pathlib, sys; "
            "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
        )
        target = entry / "index.txt"
        command = CommandPlan(
            argv=(sys.executable, "-c", script, str(target), "fake-index"),
            label="fake index",
        )
        return IndexPlan(commands=(command,), entry="index.txt", artifacts=("index.txt",))

    def plan_align(
        self,
        request: AlignRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        script = "pass" if request.params.get("no_hits") else _PRINT_HIT
        if self.stream:
            command = CommandPlan(argv=(sys.executable, "-c", script), label="fake align")
            return AlignPlan(commands=(command,), stream=True)
        output = ctx.path("hits.tsv")
        command = CommandPlan(
            argv=(sys.executable, "-c", script),
            label="fake align",
            stdout=output,
        )
        return AlignPlan(commands=(command,), stream=False, result_files=(output,))

    def parse_align(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[AlignmentRecord]:
        lines: Iterator[str]
        if stream is not None:
            lines = iter(stream)
        else:
            lines = iter(ctx.plan.result_files[0].read_text(encoding="utf-8").splitlines())
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 12:
                continue
            yield AlignmentRecord(
                query_id=fields[0],
                target_id=fields[1],
                query_length=4,
                target_length=4,
                query_start=0,
                query_end=4,
                target_start=0,
                target_end=4,
                strand=Strand.MINUS if ctx.request.params.get("reverse") else Strand.PLUS,
                identity=float(fields[2]),
                aligned_length=int(fields[3]),
                matches=int(fields[4]),
                mismatches=int(fields[5]),
                gap_count=int(fields[6]),
                evalue=float(fields[8]),
                bit_score=float(fields[9]),
                tags={"identity_method": "fake"},
            )


_PRINT_DISTANCE = "print('q1\\tr1\\t0.1\\t1e-5\\t10/100')"


class FakeDistanceAdapter(BaseAdapter):
    """A test-only MinHash-style distance backend."""

    spec = BackendSpec(
        name="fake-distance",
        family="fake",
        summary="test-only distance backend",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTA}),
            outputs=frozenset({OutputKind.DISTANCE}),
            index_scope=IndexScope.NONE,
            streaming=True,
        ),
        tools=(ToolRequirement("fake-distance-tool", "aligner", ("--version",)),),
    )

    def probe(
        self, resolver: ToolResolver, *, strict: bool = False, include_optional: bool = True
    ) -> ToolchainProbe:
        del include_optional
        return ToolchainProbe(
            backend=self.spec.name,
            tools=(
                ToolProbe(
                    name="fake-distance-tool",
                    path=Path(sys.executable),
                    version="2.0.0",
                    role="aligner",
                ),
            ),
            ok=True,
        )

    def plan_distance(
        self,
        request: DistanceRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        script = "pass" if request.params.get("no_hits") else _PRINT_DISTANCE
        return AlignPlan(
            commands=(CommandPlan(argv=(sys.executable, "-c", script), label="fake distance"),),
            stream=True,
        )

    def parse_distance(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[DistanceRecord]:
        if stream is None:
            return
        for line in stream:
            line = str(line).strip()
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) < 5:
                continue
            shared, total = fields[4].split("/", 1)
            yield DistanceRecord(
                query_id=fields[0],
                reference_id=fields[1],
                distance=float(fields[2]),
                pvalue=float(fields[3]),
                shared_hashes=int(shared),
                total_hashes=int(total),
                tags={"metric": "fake_distance"},
            )


@pytest.fixture
def fake_adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def fake_distance_adapter() -> FakeDistanceAdapter:
    return FakeDistanceAdapter()


@pytest.fixture
def fasta_pair(tmp_path: Path) -> tuple[Path, Path]:
    query = tmp_path / "query.fa"
    query.write_text(">q1\nACGT\n", encoding="utf-8")
    target = tmp_path / "target.fa"
    target.write_text(">t1\nACGT\n", encoding="utf-8")
    return query, target
