"""Minimal stdlib CLI: backends, doctor, index cache, manifest verify, align."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from gseek import __version__
from gseek.api import align, backends, distance, replay, verify
from gseek.errors import GseekError, UsageError, render_error
from gseek.execution import ToolResolver
from gseek.index import IndexPolicy, IndexStore
from gseek.provenance import read_manifest, to_jsonable
from gseek.registry import get_backend
from gseek.types import InputKind, JsonValue


class _HelpOnErrorHandler(argparse.ArgumentParser):
    """Print the full help on missing/invalid arguments instead of a bare error.

    Subcommands with missing required arguments land the user in the help
    text, not in an argparse usage error. Bare ``gseek`` is handled in
    :func:`main` (exit 0 with help).
    """

    def error(self, message: str) -> NoReturn:
        self.print_help(sys.stderr)
        sys.stderr.write(f"\ngseek: error: {message}\n")
        raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    parser = _HelpOnErrorHandler(prog="gseek", description="Unified DNA alignment infrastructure")
    parser.add_argument("--version", "-V", action="version", version=f"gseek {__version__}")
    parser.add_argument(
        "--error-format",
        choices=("json", "text"),
        default="json",
        help="error output format (default: json)",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_HelpOnErrorHandler
    )

    subparsers.add_parser("version", help="print the gseek version")

    backends_parser = subparsers.add_parser("backends", help="list registered backends")
    backends_parser.add_argument("--json", action="store_true", help="machine-readable output")

    doctor_parser = subparsers.add_parser("doctor", help="probe registered tools")
    doctor_parser.add_argument("--json", action="store_true", help="machine-readable output")

    index_parser = subparsers.add_parser("index", help="inspect or clean the index cache")
    index_sub = index_parser.add_subparsers(dest="index_command", required=True)
    index_list = index_sub.add_parser("list", help="list cached indexes")
    index_list.add_argument("--json", action="store_true")
    index_gc = index_sub.add_parser("gc", help="garbage-collect cached indexes")
    index_gc.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    index_gc.add_argument("--max-bytes", type=int, default=None)
    index_gc.add_argument("--max-age-days", type=float, default=None)
    index_gc.add_argument("--json", action="store_true")

    manifest_parser = subparsers.add_parser("manifest", help="inspect a run manifest")
    manifest_sub = manifest_parser.add_subparsers(dest="manifest_command", required=True)
    manifest_verify = manifest_sub.add_parser("verify", help="verify recorded file hashes")
    manifest_verify.add_argument("path", type=Path)
    manifest_verify.add_argument("--base-dir", "-d", type=Path, default=None)
    manifest_verify.add_argument("--json", action="store_true")

    # Convenience alias matching the plan's `gseek verify run.json`.
    verify_parser = subparsers.add_parser("verify", help="verify a run manifest")
    verify_parser.add_argument("path", type=Path)
    verify_parser.add_argument("--base-dir", type=Path, default=None)
    verify_parser.add_argument("--json", action="store_true")

    replay_parser = subparsers.add_parser("replay", help="re-run a manifest recipe")
    replay_parser.add_argument("path", type=Path)
    replay_parser.add_argument("--base-dir", type=Path, default=None)
    replay_parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="fail if the canonical result hash differs",
    )
    replay_parser.add_argument("--json", action="store_true")

    for name, help_text, needs_ref in (
        ("align", "align a query against targets", False),
        ("distance", "compare queries against references", True),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--backend", "-b", required=True)
        command.add_argument("--query", "-q", type=Path, action="append", required=True)
        command.add_argument(
            "--target" if not needs_ref else "--reference",
            "-t" if not needs_ref else "-r",
            dest="target",
            type=Path,
            action="append",
            required=True,
        )
        command.add_argument("--threads", "-j", type=int, default=1)
        if not needs_ref:
            command.add_argument("--preset", "-p", default=None)
        command.add_argument("--param", "-P", action="append", default=[], metavar="KEY=VALUE")
        command.add_argument(
            "--index", "-i", choices=[policy.value for policy in IndexPolicy], default="auto"
        )
        command.add_argument("--manifest", "-m", type=Path, default=None)
        command.add_argument("--output", "-o", type=Path, default=None)
        if not needs_ref:
            command.add_argument("--paired", action="store_true")
        else:
            command.add_argument(
                "--external-reference",
                "-e",
                action="store_true",
                help="treat --reference as a pre-built .msh/.sig/.zip artifact",
            )
        command.add_argument(
            "--query-kind", "-k", choices=[kind.value for kind in InputKind], default=None
        )
        command.add_argument("--keep-work", "-K", action="store_true")

    return parser


def _parse_params(items: Sequence[str]) -> dict[str, JsonValue]:
    params: dict[str, JsonValue] = {}
    for item in items:
        if "=" not in item:
            raise UsageError(f"--param must be KEY=VALUE, got {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise UsageError(f"--param has an empty key: {item!r}")
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        if isinstance(value, float) and not math.isfinite(value):
            # NaN/Infinity parse fine but crash canonical JSON mid-run with
            # a bare ValueError far from the user's --param.
            raise UsageError(
                f"--param {key} must be a finite number, got {raw}",
                context={"param": key},
            )
        params[key] = value
    return params


def _print_json(value: Any) -> None:
    print(json.dumps(to_jsonable(value), indent=2, ensure_ascii=False, sort_keys=False))


def _render_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """Render a Unicode box-drawing table (zero-dependency).

    ``rows`` maps column header → cell value; missing keys render as blanks.
    """
    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            cell = str(row.get(column, ""))
            widths[column] = max(widths[column], len(cell))

    def border(left: str, middle: str, right: str) -> str:
        segments = ["─" * (widths[column] + 2) for column in columns]
        return left + middle.join(segments) + right

    def cells(row: dict[str, object] | None) -> str:
        values = [str(row.get(column, "")) for column in columns] if row is not None else columns
        padded = [
            f" {value:<{widths[column]}} " for value, column in zip(values, columns, strict=False)
        ]
        return "│" + "│".join(padded) + "│"

    lines = [
        border("┌", "┬", "┐"),
        cells(None),
        border("├", "┼", "┤"),
        *(cells(row) for row in rows),
        border("└", "┴", "┘"),
    ]
    return "\n".join(lines)


def _print_error(exc: BaseException, error_format: str) -> None:
    if error_format != "text":
        print(render_error(exc), file=sys.stderr)
        return
    if isinstance(exc, GseekError):
        print(f"gseek: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return
    print(f"gseek: {type(exc).__name__}: {exc}", file=sys.stderr)


def _cmd_backends(args: argparse.Namespace) -> int:
    specs = backends()
    if args.json:
        _print_json(
            [
                {
                    "name": spec.name,
                    "family": spec.family,
                    "summary": spec.summary,
                    "aliases": list(spec.aliases),
                    "capabilities": to_jsonable(spec.capabilities),
                    "tools": to_jsonable(spec.tools),
                }
                for spec in specs
            ]
        )
        return 0
    if not specs:
        print("no backends registered (see dev/development-plan.md)")
        return 0
    rows = []
    for spec in specs:
        caps = spec.capabilities
        inputs = "/".join(sorted(kind.value for kind in caps.inputs))
        outputs = "/".join(sorted(kind.value for kind in caps.outputs))
        rows.append(
            {
                "Backend": spec.name,
                "Family": spec.family,
                "Input": inputs,
                "Output": outputs,
                "Summary": spec.summary,
            }
        )
    print(_render_table(["Backend", "Family", "Input", "Output", "Summary"], rows))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    specs = backends()
    resolver = ToolResolver()
    report: list[dict[str, Any]] = []
    failed = False
    for spec in specs:
        adapter = get_backend(spec.name)
        probe = adapter.probe(resolver, strict=False)
        record = {
            "backend": spec.name,
            "ok": probe.ok,
            "error": probe.error,
            "tools": [
                {
                    "name": tool.name,
                    "path": str(tool.path) if tool.path is not None else None,
                    "version": tool.version,
                    "ok": tool.ok,
                    "error": tool.error,
                }
                for tool in probe.tools
            ],
        }
        report.append(record)
        failed = failed or not probe.ok
    if args.json:
        _print_json(report)
    elif not report:
        print("no backends registered (nothing to probe)")
    else:
        rows = []
        for record in report:
            status = "ok" if record["ok"] else "MISSING"
            rows.append(
                {
                    "Backend": record["backend"],
                    "Status": status,
                    "Tool": "",
                    "Version": "",
                    "Path": "",
                }
            )
            for tool in record["tools"]:
                rows.append(
                    {
                        "Backend": "",
                        "Status": "",
                        "Tool": tool["name"],
                        "Version": tool["version"] or "-",
                        "Path": tool["path"] or "-",
                    }
                )
        print(_render_table(["Backend", "Status", "Tool", "Version", "Path"], rows))
    return 1 if failed else 0


def _cmd_index(args: argparse.Namespace) -> int:
    store = IndexStore()
    if args.index_command == "list":
        entries = store.list_indexes()
        if args.json:
            _print_json(entries)
        elif not entries:
            print(f"no cached indexes under {store.index_root}")
        else:
            rows = [
                {
                    "Adapter": entry.adapter,
                    "Key": entry.key[:16] + "…",
                    "Size": f"{entry.size_bytes:,} B",
                    "Created": entry.created_at or "-",
                }
                for entry in entries
            ]
            print(_render_table(["Adapter", "Key", "Size", "Created"], rows))
        return 0

    removed = store.gc(
        max_bytes=args.max_bytes,
        max_age_days=args.max_age_days,
        dry_run=not args.apply,
    )
    if args.json:
        _print_json(removed)
    elif not removed:
        print("nothing to collect")
    else:
        verb = "removed" if args.apply else "would remove"
        for entry in removed:
            print(f"{verb}: {entry.adapter}/{entry.key} ({entry.size_bytes} B)")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    manifest = read_manifest(args.path)
    base_dir = args.base_dir if args.base_dir is not None else args.path.resolve().parent
    # Pass the parsed document: verify() must not read and parse the file again.
    issues = verify(manifest, base_dir=base_dir)
    if args.json:
        _print_json({"manifest": manifest.to_dict(), "issues": issues})
    else:
        print(f"manifest: {manifest.run_id}")
        print(f"backend:  {manifest.backend}")
        print(f"recipe:   {manifest.recipe_hash}")
        if issues:
            for issue in issues:
                print(f"{issue.severity.upper()}: {issue.code}: {issue.message}")
        else:
            print("all recorded file hashes match")
    return 4 if issues else 0


def _cmd_replay(args: argparse.Namespace) -> int:
    outcome = replay(args.path, check=args.check, base_dir=args.base_dir)
    if args.json:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    print(
        f"{outcome.manifest.backend} replay records={len(outcome.result.records)} "
        f"matches={outcome.matches}"
    )
    return 0


def _cmd_run(args: argparse.Namespace, *, distance_mode: bool) -> int:
    params = _parse_params(args.param)
    preset = getattr(args, "preset", None)
    if preset:
        params["preset"] = preset
    common = {
        "backend": args.backend,
        "query": tuple(args.query),
        "params": params,
        "threads": args.threads,
        "index": IndexPolicy(args.index),
        "keep_work": args.keep_work,
        "manifest": args.manifest,
        "query_kind": InputKind(args.query_kind) if args.query_kind else None,
    }
    if distance_mode:
        result = distance(
            reference=tuple(args.target),
            external_reference=args.external_reference,
            **common,
        )
    else:
        result = align(target=tuple(args.target), paired=args.paired, **common)
    document = result.to_dict(include_manifest=False)
    if args.output is not None:
        # Stream to the file instead of building the whole indented document
        # in memory (million-record results would triple peak RSS).
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return 0
    json.dump(document, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    resolved = list(sys.argv[1:] if argv is None else argv)
    if not resolved:
        # Bare invocation: print help instead of a "required" error.
        parser.print_help()
        return 0
    args = parser.parse_args(resolved)
    try:
        if args.command == "backends":
            return _cmd_backends(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "index":
            return _cmd_index(args)
        if args.command in ("manifest", "verify"):
            return _cmd_manifest(args)
        if args.command == "replay":
            return _cmd_replay(args)
        if args.command == "align":
            return _cmd_run(args, distance_mode=False)
        if args.command == "distance":
            return _cmd_run(args, distance_mode=True)
        raise UsageError(f"unknown command: {args.command}")
    except GseekError as exc:
        _print_error(exc, args.error_format)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        _print_error(exc, args.error_format)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
