from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from gseek.adapter import (
    AlignPlan,
    AlignRequest,
    ParseContext,
    PlanContext,
)
from gseek.adapters.mummer import MummerAdapter
from gseek.errors import ParseError, UnsupportedFeatureError
from gseek.execution import ToolResolver
from gseek.types import IndexScope, InputKind, OutputKind, Strand

_HEADER = (
    "[S1]\t[E1]\t[S2]\t[E2]\t[LEN 1]\t[LEN 2]\t[% IDY]\t[LEN R]\t[LEN Q]\t[COV R]\t[COV Q]\t[TAGS]"
)


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.fa"
    path.write_text(">t1\nACGTACGT\n>t2\nTTTTGGGG\n", encoding="utf-8")
    return path


def _query(tmp_path: Path, name: str = "query.fa") -> Path:
    path = tmp_path / name
    path.write_text(">q1\nACGTACGT\n", encoding="utf-8")
    return path


def _plan() -> AlignPlan:
    return AlignPlan(commands=(), stream=True)


def _parse(
    lines: list[str],
    *,
    tmp_path: Path,
    target_filter: frozenset[str] | None = None,
) -> list:
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        target_filter=target_filter,
    )
    ctx = ParseContext(
        request=request,
        plan=_plan(),
        workspace=tmp_path,
    )
    return list(MummerAdapter().parse_align(ctx, iter(lines)))


def _row(
    s1: int = 51,
    e1: int = 350,
    s2: int = 1,
    e2: int = 300,
    len1: int = 300,
    len2: int = 300,
    identity: str = "100.00",
    lenr: int = 500,
    lenq: int = 300,
    covr: str = "60.00",
    covq: str = "100.00",
    tagr: str = "t1",
    tagq: str = "q1",
) -> str:
    return "\t".join(
        [
            str(s1),
            str(e1),
            str(s2),
            str(e2),
            str(len1),
            str(len2),
            identity,
            str(lenr),
            str(lenq),
            covr,
            covq,
            tagr,
            tagq,
        ]
    )


def test_spec_declares_mummer4_no_index() -> None:
    spec = MummerAdapter.spec
    assert spec.name == "mummer"
    assert "nucmer" in spec.aliases
    assert spec.capabilities.inputs == frozenset({InputKind.FASTA})
    assert spec.capabilities.outputs == frozenset({OutputKind.ALIGNMENT})
    assert spec.capabilities.index_scope is IndexScope.NONE
    assert spec.capabilities.paired is False
    tool = next(tool for tool in spec.tools if tool.binary == "nucmer")
    assert tool.min_version == (4, 0, 0)
    assert tool.version_args == ("--version",)


def test_plan_align_runs_nucmer_then_show_coords(tmp_path: Path) -> None:
    target = _targets(tmp_path)
    query = _query(tmp_path)
    request = AlignRequest(
        query=(query,),
        target=(target,),
        params={},
        threads=2,
    )
    plan = MummerAdapter().plan_align(
        request,
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    assert plan.stream is True
    assert len(plan.commands) == 2
    nucmer, coords = plan.commands
    assert nucmer.argv[0] == "nucmer"
    assert "--maxmatch" in nucmer.argv
    assert nucmer.argv[nucmer.argv.index("-c") + 1] == "20"
    assert nucmer.argv[nucmer.argv.index("-l") + 1] == "20"
    assert nucmer.argv[nucmer.argv.index("-t") + 1] == "2"
    prefix = Path(nucmer.argv[nucmer.argv.index("-p") + 1])
    merged_target = Path(nucmer.argv[-2])
    assert merged_target.exists()
    assert merged_target.name == "targets.fasta"
    assert nucmer.argv[-1] == str(query.resolve())
    assert coords.argv[0] == "show-coords"
    assert "-rcl" in coords.argv
    assert "-T" in coords.argv
    assert str(prefix) + ".delta" == coords.argv[-1]


def test_plan_align_overrides_cluster_and_match(tmp_path: Path) -> None:
    request = AlignRequest(
        query=(_query(tmp_path),),
        target=(_targets(tmp_path),),
        params={"maxmatch": False, "min_cluster": 50, "min_match": 30},
        threads=1,
    )
    plan = MummerAdapter().plan_align(
        request,
        None,
        PlanContext(workspace=tmp_path / "work"),
    )
    nucmer = plan.commands[0]
    assert "--maxmatch" not in nucmer.argv
    assert nucmer.argv[nucmer.argv.index("-c") + 1] == "50"
    assert nucmer.argv[nucmer.argv.index("-l") + 1] == "30"
    assert "-t" not in nucmer.argv


def test_plan_align_rejects_fastq_paired_and_unknown_params(tmp_path: Path) -> None:
    adapter = MummerAdapter()
    fastq = _query(tmp_path, "reads.fq")
    fastq.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    with pytest.raises(UnsupportedFeatureError):
        adapter.check_align_request(
            AlignRequest(
                query=(fastq,),
                target=(_targets(tmp_path),),
                query_kind=InputKind.FASTQ,
            )
        )
    with pytest.raises(UnsupportedFeatureError):
        adapter.check_align_request(
            AlignRequest(
                query=(fastq, fastq),
                target=(_targets(tmp_path),),
                query_kind=InputKind.FASTA,
                paired=True,
            )
        )
    with pytest.raises(UnsupportedFeatureError):
        adapter.plan_align(
            AlignRequest(
                query=(_query(tmp_path),),
                target=(_targets(tmp_path),),
                params={"bogus": True},
            ),
            None,
            PlanContext(workspace=tmp_path / "work"),
        )


def test_parse_forward_and_reverse_coords(tmp_path: Path) -> None:
    lines = [
        "/tmp/targets.fa /tmp/query.fa",
        "NUCMER",
        "",
        _HEADER,
        _row(),
        _row(
            s1=40,
            e1=250,
            s2=300,
            e2=1,
            len1=210,
            len2=300,
            identity="95.00",
            lenr=500,
            lenq=300,
            covr="42.00",
            covq="70.00",
            tagr="t2",
        ),
    ]
    records = _parse(lines, tmp_path=tmp_path)
    assert len(records) == 2

    forward = records[0]
    assert forward.query_id == "q1"
    assert forward.target_id == "t1"
    assert forward.strand is Strand.PLUS
    assert forward.target_start == 50
    assert forward.target_end == 350
    assert forward.query_start == 0
    assert forward.query_end == 300
    assert forward.identity == 100.0
    assert forward.aligned_length == 300
    assert forward.target_length == 500
    assert forward.query_length == 300
    assert forward.target_coverage == pytest.approx(0.6)
    assert forward.query_coverage == pytest.approx(1.0)
    assert forward.tags["identity_method"] == "mummer_coords"

    reverse = records[1]
    assert reverse.target_id == "t2"
    assert reverse.strand is Strand.MINUS
    assert reverse.target_start == 39
    assert reverse.target_end == 250
    assert reverse.query_start == 0
    assert reverse.query_end == 300
    assert reverse.identity == 95.0
    assert reverse.target_coverage == pytest.approx(0.42)
    assert reverse.query_coverage == pytest.approx(0.7)


def test_parse_filters_targets_and_skips_header(tmp_path: Path) -> None:
    lines = [_HEADER, _row(), _row(tagr="t2")]
    records = _parse(lines, tmp_path=tmp_path, target_filter=frozenset({"t2"}))
    assert [record.target_id for record in records] == ["t2"]


def test_parse_rejects_malformed_numeric_row(tmp_path: Path) -> None:
    lines = [_HEADER, "1\t2\t3\t4\t5\t6\tNOPE\t8\t9\t10\t11\tt\tq"]
    with pytest.raises(ParseError):
        _parse(lines, tmp_path=tmp_path)
    with pytest.raises(ParseError):
        _parse([_HEADER, "1\t2\t3"], tmp_path=tmp_path)


def test_plan_index_is_none(tmp_path: Path) -> None:
    from gseek.adapter import IndexRequest

    assert (
        MummerAdapter().plan_index(
            IndexRequest(target=(_targets(tmp_path),)),
            PlanContext(workspace=tmp_path),
        )
        is None
    )


class _FakeResolver:
    def __init__(
        self, *, nucmer_ok: bool = True, nucmer_error: str = "", show: bool = True
    ) -> None:
        self._nucmer_ok = nucmer_ok
        self._nucmer_error = nucmer_error
        self._show = show

    def probe(self, requirement, *, strict: bool = False):  # type: ignore[no-untyped-def]
        from gseek.execution import ToolProbe

        return ToolProbe(
            name="nucmer",
            path=Path("/bin/nucmer"),
            version="3.23" if not self._nucmer_ok else "4.0.1",
            ok=self._nucmer_ok,
            error=self._nucmer_error,
        )

    def which(self, binary: str):  # type: ignore[no-untyped-def]
        return Path("/bin/show-coords") if self._show else None


def test_probe_rejects_old_nucmer_as_version_error() -> None:
    from gseek.errors import ToolVersionError

    resolver = _FakeResolver(
        nucmer_ok=False,
        nucmer_error="nucmer 3.23 is older than 4.0.0",
    )
    with pytest.raises(ToolVersionError):
        MummerAdapter().probe(cast(ToolResolver, resolver), strict=True)


def test_probe_reports_missing_show_coords() -> None:
    from gseek.errors import ToolNotFoundError

    with pytest.raises(ToolNotFoundError):
        MummerAdapter().probe(cast(ToolResolver, _FakeResolver(show=False)), strict=True)
