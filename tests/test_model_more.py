from __future__ import annotations

import pytest

from gseek.errors import InputError, ManifestError
from gseek.model import (
    AlignmentResult,
    AlignmentStats,
    CommandRecord,
    DistanceRecord,
    DistanceResult,
    DistanceStats,
    FileRef,
    IndexRef,
    InputRef,
    Issue,
    RunManifest,
    ToolRef,
)
from gseek.types import InputKind, RunStatus


def test_file_ref_validation() -> None:
    with pytest.raises(ValueError):
        FileRef(path="x", size=-1, sha256="a" * 64)
    with pytest.raises(ValueError):
        FileRef(path="x", size=0, sha256="short")
    with pytest.raises(ValueError):
        FileRef(path="x", size=0, sha256="z" * 64)
    assert FileRef(path="x", size=0, sha256="a" * 64).as_path.name == "x"


def test_command_record_rejects_unknown_phase() -> None:
    with pytest.raises(ManifestError):
        CommandRecord(
            phase="bogus",  # type: ignore[arg-type]
            label="x",
            argv=("x",),
            cwd=None,
            exit_code=0,
            duration_s=0.0,
        )


def test_input_ref_validation_and_round_trip() -> None:
    with pytest.raises(ValueError):
        InputRef(path="x", size=0, sha256="a" * 64, records=-1)
    ref = InputRef(path="x", size=0, sha256="a" * 64, kind=InputKind.FASTQ)
    assert InputRef.from_dict(ref.to_dict()) == ref


def test_issue_validation() -> None:
    with pytest.raises(ValueError):
        Issue(code="X", message="x", severity="fatal")  # type: ignore[arg-type]
    issue = Issue(code="X", message="x", severity="error", context={"a": "b"})
    assert Issue.from_dict(issue.to_dict()) == issue


def test_distance_record_validation_and_round_trip() -> None:
    with pytest.raises(InputError):
        DistanceRecord(query_id="", reference_id="r")
    with pytest.raises(InputError):
        DistanceRecord(query_id="q", reference_id="r", distance=-1)
    with pytest.raises(InputError):
        DistanceRecord(query_id="q", reference_id="r", ani=101)
    record = DistanceRecord(
        query_id="q",
        reference_id="r",
        distance=0.1,
        shared_hashes=10,
        total_hashes=100,
        tags={"metric": "mash"},
    )
    assert DistanceRecord.from_dict(record.to_dict()) == record
    assert record.metric == "mash"


def test_distance_result_rejects_bad_schema() -> None:
    with pytest.raises(InputError):
        DistanceResult(
            backend="x",
            records=(),
            inputs=(),
            references=(),
            params={},
            stats=DistanceStats(0, 0, 0, 0),
            schema="bad",
        )


def test_alignment_result_rejects_bad_schema() -> None:
    with pytest.raises(InputError):
        AlignmentResult(
            backend="x",
            records=(),
            inputs=(),
            targets=(),
            params={},
            stats=AlignmentStats(0, 0, 0, 0, 0, 0, 0),
            schema="bad",
        )


def test_index_ref_and_command_record_round_trip() -> None:
    index = IndexRef(
        key="a" * 64,
        adapter="fake",
        adapter_version="1",
        entry="index.txt",
        source_sha256="b" * 64,
        artifacts=("index.txt",),
        params={"x": 1},
        created_at="2026-01-01T00:00:00Z",
        cache_hit=True,
        tool_versions={"tool": "1.0"},
    )
    assert IndexRef.from_dict(index.to_dict()) == index

    command = CommandRecord(
        phase="align",
        label="align",
        argv=("tool", "arg"),
        cwd="/tmp",
        exit_code=0,
        duration_s=1.0,
        stderr_tail="",
        stdout_path="/tmp/out",
    )
    assert CommandRecord.from_dict(command.to_dict()) == command


def test_run_manifest_validation() -> None:
    with pytest.raises(ManifestError):
        RunManifest(
            run_id="",
            recipe_hash="a" * 64,
            created_at="now",
            gseek_version="0.1.0",
            python_version="3.12",
            platform="linux",
            backend="fake",
            backend_version="1",
            tools=(),
            inputs=(),
            targets=(),
            params={},
            commands=(),
        )
    with pytest.raises(ManifestError):
        RunManifest(
            run_id="x",
            recipe_hash="short",
            created_at="now",
            gseek_version="0.1.0",
            python_version="3.12",
            platform="linux",
            backend="fake",
            backend_version="1",
            tools=(),
            inputs=(),
            targets=(),
            params={},
            commands=(),
        )
    with pytest.raises(ManifestError):
        RunManifest(
            run_id="x",
            recipe_hash="a" * 64,
            created_at="now",
            gseek_version="0.1.0",
            python_version="3.12",
            platform="linux",
            backend="fake",
            backend_version="1",
            tools=(),
            inputs=(),
            targets=(),
            params={},
            commands=(),
            schema="bad",
        )
    with pytest.raises(ManifestError):
        RunManifest.from_dict({"schema": "bad"})


def test_run_manifest_defaults_and_status() -> None:
    manifest = RunManifest(
        run_id="x",
        recipe_hash="a" * 64,
        created_at="now",
        gseek_version="0.1.0",
        python_version="3.12",
        platform="linux",
        backend="fake",
        backend_version="1",
        tools=(ToolRef(name="tool", version="1"),),
        inputs=(),
        targets=(),
        params={},
        commands=(),
        status=RunStatus.PARTIAL,
    )
    assert manifest.to_dict()["status"] == "partial"
