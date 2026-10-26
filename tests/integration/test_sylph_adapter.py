"""Real-sylph integration: sketch reads + profile → normalized records."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gseek.adapter import DistanceRequest, PlanContext
from gseek.adapters.sylph import SylphAdapter
from gseek.api import distance
from gseek.runner import run_distance

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sylph_env(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    binary = shutil.which("sylph")
    if binary is None:
        pytest.skip("sylph not installed")
    work = tmp_path_factory.mktemp("sylph")
    import random

    random.seed(9)

    def genome(n: int) -> str:
        return "".join(random.choice("ACGT") for _ in range(n))

    reference = work / "ref.fa"
    sequence = genome(50_000)
    reference.write_text(f">ref_genome\n{sequence}\n", encoding="utf-8")
    reads = work / "reads.fq"
    with reads.open("w", encoding="utf-8") as handle:
        for i in range(2_000):
            segment = sequence[i * 20 : i * 20 + 100]
            handle.write(f"@r{i}\n{segment}\n+\n{'I' * len(segment)}\n")
    subprocess.run(
        [binary, "sketch", "-g", str(reference), "-o", str(work / "db")],
        check=True,
        capture_output=True,
    )
    return reads, reference, work


def test_sylph_end_to_end(sylph_env: tuple[Path, Path, Path]) -> None:
    reads, _reference, work = sylph_env
    database = work / "db.syldb"
    result = run_distance(
        DistanceRequest(query=(reads,), reference=(database,), external_reference=True),
        backend="sylph",
    )
    assert result.backend == "sylph"
    assert result.stats.record_count == 1
    record = result.records[0]
    assert record.query_id == "reads.fq"
    assert record.ani is not None and record.ani > 95.0
    assert record.tags["metric"] == "sylph_ani"


def test_sylph_api_and_determinism(sylph_env: tuple[Path, Path, Path]) -> None:
    reads, _reference, work = sylph_env
    database = work / "db.syldb"
    first = distance(query=reads, reference=database, backend="sylph", external_reference=True)
    second = distance(query=reads, reference=database, backend="sylph", external_reference=True)
    from gseek.provenance import distance_result_hash

    assert distance_result_hash(first.records) == distance_result_hash(second.records)


def test_sylph_plan_smoke(sylph_env: tuple[Path, Path, Path]) -> None:
    reads, _reference, work = sylph_env
    adapter = SylphAdapter()
    plan = adapter.plan_distance(
        DistanceRequest(query=(reads,), reference=(work / "db.syldb",), external_reference=True),
        None,
        PlanContext(workspace=work / "plan"),
    )
    assert [command.argv[1] for command in plan.commands] == ["sketch", "profile"]
