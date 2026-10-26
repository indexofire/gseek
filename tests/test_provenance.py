from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from gseek.model import AlignmentRecord, DistanceRecord, RunManifest, ToolRef
from gseek.provenance import (
    alignment_result_hash,
    canonical_json,
    distance_result_hash,
    input_ref,
    read_manifest,
    recipe_hash,
    result_hash,
    semantic_params,
    sha256_bytes,
    sha256_file,
    sha256_text,
    sha256_tree,
    to_jsonable,
    utc_now_iso,
    verify_manifest,
    write_manifest,
)
from gseek.types import RUN_SCHEMA, InputKind


def _manifest(tmp_path: Path) -> RunManifest:
    query = tmp_path / "query.fa"
    query.write_text(">q\nACGT\n", encoding="utf-8")
    target = tmp_path / "target.fa"
    target.write_text(">t\nACGT\n", encoding="utf-8")
    query_ref = input_ref(query)
    target_ref = input_ref(target)
    tools = (ToolRef(name="fake-tool", version="1.0.0"),)
    digest = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=(query_ref,),
        targets=(target_ref,),
        params={"evalue": 1e-5},
        tools=tools,
    )
    return RunManifest(
        run_id="run-1",
        recipe_hash=digest,
        created_at=utc_now_iso(),
        gseek_version="0.1.0",
        python_version="3.12.0",
        platform="Linux-test",
        hostname="host",
        backend="fake",
        backend_version="1",
        tools=tools,
        inputs=(query_ref,),
        targets=(target_ref,),
        params={"evalue": 1e-5},
        commands=(),
        env={"LC_ALL": "C"},
    )


def test_hashes_and_canonical_json() -> None:
    assert sha256_text("abc") == sha256_bytes(b"abc")
    assert canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'
    assert sha256_file(Path(__file__)) == sha256_file(Path(__file__))


def test_to_jsonable_handles_paths_and_enums(tmp_path: Path) -> None:
    assert to_jsonable(tmp_path / "x") == str(tmp_path / "x")
    assert to_jsonable({"x": (1, 2)}) == {"x": [1, 2]}


def test_to_jsonable_sorts_sets_for_deterministic_json() -> None:
    assert to_jsonable({"b", "a"}) == ["a", "b"]
    assert to_jsonable(frozenset({"c", "a", "b"})) == ["a", "b", "c"]


def test_semantic_params_drops_volatile_keys() -> None:
    assert semantic_params({"threads": 8, "evalue": 1e-5, "debug": True, "missing": None}) == {
        "evalue": 1e-5
    }


def test_recipe_hash_ignores_threads_and_changes_with_semantics(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    base = manifest.recipe_hash
    threaded = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=manifest.inputs,
        targets=manifest.targets,
        params={"evalue": 1e-5, "threads": 64},
        tools=manifest.tools,
    )
    changed = recipe_hash(
        backend="fake",
        adapter_version="1",
        inputs=manifest.inputs,
        targets=manifest.targets,
        params={"evalue": 1e-10},
        tools=manifest.tools,
    )
    assert threaded == base
    assert changed != base


def test_manifest_write_read_round_trip(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "run.json"
    write_manifest(path, manifest)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == RUN_SCHEMA
    restored = read_manifest(path)
    assert restored == manifest
    assert verify_manifest(restored) == ()


def test_verify_manifest_detects_tampering(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    Path(manifest.inputs[0].path).write_text(">q\nTTTT\n", encoding="utf-8")
    issues = verify_manifest(manifest)
    assert issues
    assert issues[0].severity == "error"


def test_result_hash_is_stable() -> None:
    record = AlignmentRecord(query_id="q", target_id="t", identity=99.0)
    assert result_hash((record,)) == result_hash((record,))
    assert result_hash((record,)) != result_hash(
        (AlignmentRecord(query_id="q", target_id="t", identity=98.0),)
    )


def test_alignment_result_hash_is_independent_of_record_order() -> None:
    first = AlignmentRecord(query_id="q", target_id="a", identity=90.0)
    second = AlignmentRecord(query_id="q", target_id="b", identity=80.0)
    assert alignment_result_hash((first, second)) == alignment_result_hash((second, first))
    assert result_hash((first, second)) == result_hash((second, first))


def test_distance_result_hash_is_independent_of_record_order() -> None:
    first = DistanceRecord(query_id="q", reference_id="a", distance=0.1)
    second = DistanceRecord(query_id="q", reference_id="b", distance=0.2)
    assert distance_result_hash((first, second)) == distance_result_hash((second, first))


def test_sha256_tree_is_deterministic_and_content_addressed(tmp_path: Path) -> None:
    tree = tmp_path / "db"
    (tree / "sub").mkdir(parents=True)
    (tree / "b.bin").write_bytes(b"b")
    (tree / "sub" / "a.bin").write_bytes(b"a")
    first = sha256_tree(tree)

    # Creation order must not matter.
    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "sub" / "a.bin").write_bytes(b"a")
    (other / "b.bin").write_bytes(b"b")
    assert sha256_tree(other) == first

    (tree / "sub" / "a.bin").write_bytes(b"changed")
    assert sha256_tree(tree) != first


def test_input_ref_accepts_sketch_directory(tmp_path: Path) -> None:
    tree = tmp_path / "rocksdb"
    tree.mkdir()
    (tree / "CURRENT").write_text("MANIFEST-1\n", encoding="utf-8")
    (tree / "000001.sst").write_bytes(b"data")
    ref = input_ref(tree, kind=InputKind.SKETCH, compute_records=False)
    assert ref.kind is InputKind.SKETCH
    assert ref.path == str(tree.resolve())
    assert ref.size == len("MANIFEST-1\n") + 4
    assert ref.records is None
    assert ref.sha256 == sha256_tree(tree)


def test_input_ref_rejects_non_sketch_directory(tmp_path: Path) -> None:
    tree = tmp_path / "db"
    tree.mkdir()
    with pytest.raises(Exception, match="sketch external references"):
        input_ref(tree, kind=InputKind.FASTA)


def test_verify_manifest_detects_directory_tampering(tmp_path: Path) -> None:
    tree = tmp_path / "rocksdb"
    tree.mkdir()
    (tree / "000001.sst").write_bytes(b"data")
    manifest = dataclasses.replace(
        _manifest(tmp_path),
        targets=(input_ref(tree, kind=InputKind.SKETCH, compute_records=False),),
    )
    assert verify_manifest(manifest) == ()
    (tree / "000001.sst").write_bytes(b"DATA")
    issues = verify_manifest(manifest)
    assert issues
    assert issues[0].code == "TARGET_HASH_MISMATCH"


def test_verify_reports_unreadable_sketch_directory(tmp_path: Path) -> None:
    # A sketch directory that cannot be hashed (symlink inside) must be
    # reported as an Issue: verify's whole job is reporting damage.
    sketch_dir = tmp_path / "refs"
    sketch_dir.mkdir()
    (sketch_dir / "ok.sig").write_text("x", encoding="utf-8")
    (sketch_dir / "bad.sig").symlink_to(tmp_path / "elsewhere")

    base = _manifest(tmp_path)
    manual_ref = dataclasses.replace(
        base.targets[0],
        path=str(sketch_dir.resolve()),
        size=1,
        sha256="0" * 64,
        kind=InputKind.SKETCH,
    )
    damaged = dataclasses.replace(base, targets=(manual_ref,))
    issues = verify_manifest(damaged)
    assert any(issue.code == "TARGET_UNREADABLE" and issue.severity == "error" for issue in issues)


def test_to_jsonable_decodes_bytes() -> None:
    # params carrying raw bytes must not crash canonical JSON with a bare
    # TypeError; decode lossily instead.
    assert to_jsonable(b"nucleotide: \xff") == "nucleotide: �"


def test_command_record_from_dict_rejects_bad_phase() -> None:
    from gseek.errors import ManifestError
    from gseek.model import CommandRecord

    with pytest.raises(ManifestError):
        CommandRecord.from_dict(
            {
                "phase": "teardown",
                "label": "x",
                "argv": ("a",),
                "cwd": None,
                "exit_code": 0,
                "duration_s": 0.0,
            }
        )


def test_result_hash_streaming_matches_bulk() -> None:
    # The streaming hash must be byte-identical to the previous bulk
    # implementation: replay --check against pre-0.11 manifests depends on it.
    import hashlib

    from gseek.model import AlignmentRecord as AR

    def bulk_hash(records):
        ordered = sorted(records, key=AR.stable_key)
        return hashlib.sha256(
            __import__("gseek.provenance", fromlist=["canonical_json"])
            .canonical_json([r.to_dict() for r in ordered])
            .encode("utf-8")
        ).hexdigest()

    records = [
        AR(
            query_id=f"q{i % 7}",
            target_id=f"t{i % 3}",
            identity=90.0 + i % 10,
            query_start=i % 50,
            query_end=(i % 50) + 10,
            query_length=100,
            target_start=0,
            target_end=10,
            target_length=200,
            tags={"NM": str(i % 5), "café": "naïve"},
        )
        for i in range(500)
    ]
    assert alignment_result_hash(records) == bulk_hash(records)


def test_to_jsonable_rejects_unstable_set_elements() -> None:
    # Objects with default reprs carry memory addresses: their presence would
    # silently make recipe_hash process-dependent.
    from gseek.errors import InputError

    class Unstable:
        pass

    with pytest.raises(InputError):
        to_jsonable({Unstable()})
    # Stable order: by type name, then repr.
    assert to_jsonable({"b", 1, None}) == [None, 1, "b"]


def test_hash_and_count_matches_separate_passes(tmp_path: Path) -> None:
    # Single pass must match sha256_file + count_records exactly, including
    # edges: leading '>', CRLF, no trailing newline, '>' mid-line.
    from gseek.io import count_records
    from gseek.provenance import hash_and_count, sha256_file
    from gseek.types import InputKind

    cases = {
        "plain.fa": b">a\nACGT\n>b\nTGCA\n",
        "leading.fa": b">first\nACGT\n>second\nAAAA\n",
        "crlf.fa": b">a\r\nACGT\r\n>b\r\nTGCA\r\n",
        "no-trailing.fa": b">a\nACGT\n>b\nTGCA",
        "mid-gt.fa": b">a\nAC>GT\n>b\nTT\n",
        "empty-seq.fa": b">a\n>b\nACGT\n",
        "blank-lines.fa": b"\n>a\nACGT\n\n\n>b\nTT\n",
    }
    for name, payload in cases.items():
        target = tmp_path / name
        target.write_bytes(payload)
        digest, records = hash_and_count(target, InputKind.FASTA)
        assert digest == sha256_file(target), name
        assert records == count_records(target, InputKind.FASTA), (
            f"{name}: {records} != {count_records(target, InputKind.FASTA)}"
        )
        # Compressed inputs keep the two-pass fallback; equally correct.
        import gzip

        gz = tmp_path / (name + ".gz")
        gz.write_bytes(gzip.compress(payload))
        digest_gz, records_gz = hash_and_count(gz, InputKind.FASTA)
        assert digest_gz == sha256_file(gz) and records_gz == records


def test_manifest_dict_fields_are_copied(tmp_path: Path) -> None:
    # Aliasing the caller's dicts would let post-construction mutation drift
    # recipe_hash away from the recorded manifest.
    base = _manifest(tmp_path)
    shared_params = {"evalue": 1e-5}
    shared_extra = {"kind": "alignment"}
    manifest = dataclasses.replace(base, params=shared_params, extra=shared_extra)
    shared_params["evalue"] = 1.0
    shared_extra["kind"] = "distance"
    assert manifest.params["evalue"] == 1e-5
    assert manifest.extra["kind"] == "alignment"


def test_input_refs_parallel_matches_serial_and_preserves_order(tmp_path: Path) -> None:
    files = []
    for index in range(6):
        target = tmp_path / f"ref{index}.fa"
        target.write_text(f">s{index}\n{'ACGT' * (100 + index)}\n", encoding="utf-8")
        files.append(target)
    from gseek.provenance import input_ref, input_refs

    parallel = input_refs(files)
    serial = tuple(input_ref(path) for path in files)
    assert parallel == serial
    assert [ref.path for ref in parallel] == [str(path) for path in files]


def test_directory_size_does_not_hash_files(tmp_path: Path) -> None:
    # directory_size is the stat-only API: external-reference size checks
    # must not pay a full tree hash (a regression that cost +40ms per call).
    from gseek import provenance

    target = tmp_path / "refs"
    target.mkdir()
    (target / "a.bin").write_bytes(b"x" * 1000)
    calls: list[str] = []
    real_hash = provenance.sha256_file

    def counting(path, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(path))
        return real_hash(path, **kwargs)

    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setattr(provenance, "sha256_file", counting)
    try:
        assert provenance.directory_size(target) == 1000
    finally:
        monkeypatch_holder.undo()
    assert calls == [], f"directory_size must not hash, hashed: {calls}"
    # digest+size helper DOES hash (single traversal for both).
    digest, size = provenance._tree_digest_and_size(target)
    assert size == 1000 and len(digest) == 64
