"""Sylph ANI profiling adapter (sketch reads + profile against a reference DB).

Sylph (v1.x) sketches FASTQ reads and profiles them against a pre-built
genome database, emitting per-reference ANI/coverage. Records normalize into
``DistanceRecord`` exactly like the other sketch-family backends
(mash/sourmash/skani); threshold→confidence labelling stays with consumers
(see dev/decisions/0003-backend-scope-rule.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from gseek.adapter import (
    AlignPlan,
    BaseAdapter,
    DistanceRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.base import finite_float, result_lines
from gseek.capabilities import BackendSpec, Capabilities, ToolRequirement
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import CommandPlan
from gseek.io import detect_input_kind
from gseek.model import DistanceRecord, IndexRef
from gseek.types import IndexScope, InputKind, OutputKind


class SylphAdapter(BaseAdapter):
    """``sylph sketch`` + ``sylph profile`` → normalized ANI records."""

    spec = BackendSpec(
        name="sylph",
        family="minhash",
        summary="Sylph ANI metagenomic profiling (FASTQ reads vs genome DB)",
        capabilities=Capabilities(
            inputs=frozenset({InputKind.FASTQ}),
            outputs=frozenset({OutputKind.DISTANCE}),
            paired=True,
            index_scope=IndexScope.NONE,
            streaming=False,
            threads=True,
        ),
        tools=(
            ToolRequirement(
                binary="sylph",
                role="aligner",
                version_args=("--version",),
                version_pattern=r"sylph\s+(\d+)\.(\d+)\.(\d+)",
                min_version=(1, 0, 0),
                hint="install sylph (conda-forge/bioconda package `sylph`)",
            ),
        ),
        aliases=("sylph_ani",),
    )

    def plan_distance(
        self,
        request: DistanceRequest,
        index: IndexRef | None,
        ctx: PlanContext,
    ) -> AlignPlan:
        query_kind = request.query_kind or detect_input_kind(request.query[0])
        if query_kind is not InputKind.FASTQ:
            raise UnsupportedFeatureError(
                "sylph profiles FASTQ reads only",
                context={"input": query_kind.value},
            )
        if len(request.query) not in (1, 2):
            raise UnsupportedFeatureError(
                "sylph accepts one read file or a paired (R1, R2) tuple",
                context={"query_count": str(len(request.query))},
            )

        commands: list[CommandPlan] = []
        if request.external_reference:
            database = request.reference[0]
            if not database.is_file():
                raise UnsupportedFeatureError(
                    "sylph external_reference must point at a .syldb/.syl2db file",
                    context={"reference": str(database)},
                    hint="build one with `sylph sketch -g genomes.fa -o prefix`",
                )
        else:
            # sylph appends ".syldb" to the -o prefix: the plan can rely on it.
            prefix = ctx.path("targets")
            database = prefix.with_name(prefix.name + ".syldb")
            argv = [
                "sylph",
                "sketch",
                "-g",
                *[str(p.resolve()) for p in request.reference],
                "-o",
                str(prefix),
            ]
            commands.append(
                CommandPlan(
                    argv=tuple(argv), label="sylph sketch references", timeout_s=request.timeout_s
                )
            )

        sketch_dir = ctx.path("sketches")
        sketch_argv = ["sylph", "sketch", "-t", str(max(1, request.threads))]
        if len(request.query) == 2:
            sketch_argv += [
                "-1",
                str(request.query[0].resolve()),
                "-2",
                str(request.query[1].resolve()),
            ]
            # v1 names paired read sketches "<R1 filename>.paired.sylsp".
            sketch_output = sketch_dir / f"{request.query[0].name}.paired.sylsp"
        else:
            sketch_argv += [str(request.query[0].resolve())]
            sketch_output = sketch_dir / f"{request.query[0].name}.sylsp"
        sketch_argv += ["-d", str(sketch_dir)]
        commands.append(
            CommandPlan(
                argv=tuple(sketch_argv), label="sylph sketch reads", timeout_s=request.timeout_s
            )
        )

        profile = ctx.path("profile.tsv")
        commands.append(
            CommandPlan(
                argv=(
                    "sylph",
                    "profile",
                    "-d",
                    str(database),
                    str(sketch_output),
                    "-o",
                    str(profile),
                ),
                label="sylph profile",
                timeout_s=request.timeout_s,
            )
        )
        return AlignPlan(commands=tuple(commands), stream=False, result_files=(profile,))

    def parse_distance(
        self,
        ctx: ParseContext,
        stream: Iterable[str] | None = None,
    ) -> Iterator[DistanceRecord]:
        if stream is not None:
            raise UnsupportedFeatureError(
                "sylph parses its profile TSV result file; streaming input is not supported",
                context={"backend": "sylph"},
            )
        path = ctx.plan.result_files[0] if ctx.plan.result_files else None
        if path is None or not path.is_file():
            raise ParseError(
                "sylph profile TSV not found",
                path=str(path) if path is not None else None,
            )
        header: dict[str, int] | None = None
        for line_number, line in enumerate(result_lines(ctx, None), start=1):
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            fields = line.split("\t")
            if header is None:
                header = {name.strip(): position for position, name in enumerate(fields)}
                for required in ("Genome_file", "Adjusted_ANI"):
                    if required not in header:
                        raise ParseError(
                            f"sylph profile lacks the {required!r} column",
                            path=str(path),
                            line_number=line_number,
                        )
                continue
            try:
                genome_file = fields[header["Genome_file"]]
                ani = finite_float(fields[header["Adjusted_ANI"]])
                eff_cov_raw = fields[header["Eff_cov"]] if "Eff_cov" in header else ""
                coverage = finite_float(eff_cov_raw) / 100.0 if eff_cov_raw else None
                containment: float | None = None
                if "Containment_ind" in header:
                    shared_raw, _, total_raw = fields[header["Containment_ind"]].partition("/")
                    if total_raw:
                        containment = int(shared_raw) / int(total_raw)
            except (IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise ParseError(
                    f"invalid sylph profile row: {exc}",
                    path=str(path),
                    line_number=line_number,
                    raw_line=line,
                ) from exc
            tags = {
                "metric": "sylph_ani",
                "genome_file": genome_file,
            }
            for column in (
                "Taxonomic_abundance",
                "Sequence_abundance",
                "Containment_ind",
                "Naive_ANI",
                "Median_cov",
            ):
                if column in header and header[column] < len(fields):
                    value = fields[header[column]].strip()
                    if value:
                        tags[column.lower()] = value
            yield DistanceRecord(
                query_id=Path(
                    fields[header["Sample_file"]].strip()
                    if "Sample_file" in header and header["Sample_file"] < len(fields)
                    else ctx.request.query[0].name
                ).name,
                reference_id=Path(genome_file).stem,
                ani=ani,
                aligned_fraction=coverage,
                containment=containment,
                tags=tags,
            )


ADAPTER = SylphAdapter

__all__ = ["ADAPTER", "SylphAdapter"]
