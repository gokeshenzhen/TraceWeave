#!/usr/bin/env python3
"""Benchmark parallel hierarchy/structural compile-source sharing.

Each measured trial runs in a fresh Python process.  Reports omit source and
compile-log paths while retaining semantic hashes so enabled/disabled trials
can prove that the optimization did not change either consumer's result.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BENCHMARK_NAME = "compile_source_index_parallel_v1"
_SOURCE_SUFFIXES = (".sv", ".svh", ".v", ".vh")
_HIERARCHY_ORACLE_FIELDS = (
    "project",
    "files",
    "component_tree",
    "class_hierarchy",
    "interfaces",
    "compile_result",
    "_scan_results",
)


def _sha256_json(value: Any) -> tuple[str, int]:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _git_head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _git_dirty() -> bool | None:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout) if completed.returncode == 0 else None


def _index_metrics(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if key.startswith("compile_source_index_")
    }


def _source_count_and_bytes(compile_result: Mapping[str, Any]) -> tuple[int, int]:
    files = compile_result.get("files")
    user_files = files.get("user", ()) if isinstance(files, Mapping) else ()
    paths = tuple(
        dict.fromkeys(
            os.path.realpath(str(item.get("path")))
            for item in user_files
            if isinstance(item, Mapping) and item.get("path")
        )
    )
    total_bytes = 0
    for path in paths:
        try:
            total_bytes += os.stat(path).st_size
        except OSError:
            pass
    return len(paths), total_bytes


async def _timed_dispatch(server, name: str, arguments: dict[str, Any]):
    started = time.perf_counter()
    result = await server._dispatch(name, arguments)
    return result, (time.perf_counter() - started) * 1000.0


def run_trial(args: argparse.Namespace) -> dict[str, Any]:
    """Run one in-process trial; the CLI wraps this in a fresh child."""

    environment_names = (
        "TRACEWEAVE_COMPILE_SOURCE_INDEX",
        "TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES",
        "TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES",
        "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY",
    )
    saved_environment = {name: os.environ.get(name) for name in environment_names}
    enabled = args.trial_index == "enabled"
    os.environ["TRACEWEAVE_COMPILE_SOURCE_INDEX"] = "1" if enabled else "0"
    os.environ["TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES"] = str(args.max_bytes)
    os.environ["TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES"] = str(args.max_files)
    os.environ["TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"] = "off"

    import server
    from src.compile_source_runtime import CompileSourceIndexRuntime
    from src.operation_metrics import read_process_rss_kib

    server.reset_session_state()
    runtime = CompileSourceIndexRuntime()
    previous_runtime = server._compile_source_index_runtime
    server._compile_source_index_runtime = runtime
    captured_scan: dict[str, Any] = {}
    real_scan = server.scan_structural_risks

    def capture_scan(*scan_args, **scan_kwargs):
        result = real_scan(*scan_args, **scan_kwargs)
        captured_scan["result"] = result
        return result

    server.scan_structural_risks = capture_scan
    source_open_count = 0
    logical_source_bytes = 0
    counter_lock = threading.Lock()
    real_open = builtins.open

    def counted_open(path, *open_args, **open_kwargs):
        nonlocal source_open_count, logical_source_bytes
        try:
            rendered = os.fspath(path)
        except TypeError:
            rendered = ""
        if isinstance(rendered, str) and rendered.lower().endswith(_SOURCE_SUFFIXES):
            try:
                source_bytes = os.stat(rendered).st_size
            except OSError:
                source_bytes = 0
            with counter_lock:
                source_open_count += 1
                logical_source_bytes += source_bytes
        return real_open(path, *open_args, **open_kwargs)

    rss_start_kib = read_process_rss_kib()
    started = time.perf_counter()
    builtins.open = counted_open
    try:
        async def run_pair():
            return await asyncio.gather(
                _timed_dispatch(
                    server,
                    "build_tb_hierarchy",
                    {
                        "compile_log": str(args.compile_log),
                        "simulator": args.simulator,
                    },
                ),
                _timed_dispatch(
                    server,
                    "scan_structural_risks",
                    {
                        "compile_log": str(args.compile_log),
                        "simulator": args.simulator,
                    },
                ),
            )

        (hierarchy, hierarchy_wall_ms), (scan, scan_wall_ms) = asyncio.run(
            run_pair()
        )
    finally:
        builtins.open = real_open
        server.scan_structural_risks = real_scan
    parallel_wall_ms = (time.perf_counter() - started) * 1000.0
    rss_end_kib = read_process_rss_kib()
    peak_rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    full_hierarchy = server._handle_store.resolve(hierarchy.hierarchy_handle)
    if full_hierarchy is None:
        raise RuntimeError("hierarchy handle did not resolve inside benchmark child")
    hierarchy_payload = {
        key: full_hierarchy.get(key) for key in _HIERARCHY_ORACLE_FIELDS
    }
    hierarchy_sha, hierarchy_bytes = _sha256_json(hierarchy_payload)
    raw_scan = dict(captured_scan.get("result") or {})
    raw_scan.pop("scan_metrics", None)
    scan_sha, scan_bytes = _sha256_json(raw_scan)
    compile_result = full_hierarchy.get("compile_result") or {}
    source_count, source_bytes = _source_count_and_bytes(compile_result)
    hierarchy_metrics = dict(hierarchy.build_metrics)
    scan_metrics = dict(scan.scan_metrics)

    report = {
        "benchmark": BENCHMARK_NAME,
        "trial_index": args.trial_index,
        "workload": {
            "simulator": hierarchy.project.get("simulator"),
            "source_file_count": source_count,
            "source_bytes": source_bytes,
            "parallel_consumers": [
                "build_tb_hierarchy",
                "scan_structural_risks",
            ],
            "npi_source_overlay": False,
        },
        "measurement": {
            "parallel_wall_ms": round(parallel_wall_ms, 3),
            "hierarchy_public_wall_ms": round(hierarchy_wall_ms, 3),
            "structural_public_wall_ms": round(scan_wall_ms, 3),
            "source_open_count": source_open_count,
            "logical_source_bytes": logical_source_bytes,
            "rss_start_kib": rss_start_kib,
            "peak_rss_kib": peak_rss_kib,
            "rss_end_kib": rss_end_kib,
        },
        "source_index": {
            "hierarchy": _index_metrics(hierarchy_metrics),
            "structural": _index_metrics(scan_metrics),
            "runtime": runtime.metrics_snapshot(),
        },
        "behavior_oracle": {
            "hierarchy_sha256": hierarchy_sha,
            "hierarchy_result_bytes": hierarchy_bytes,
            "structural_sha256": scan_sha,
            "structural_result_bytes": scan_bytes,
            "structural_total_risks": raw_scan.get("total_risks"),
            "structural_by_category": dict(
                sorted(
                    Counter(
                        str(item.get("type"))
                        for item in raw_scan.get("risks", ())
                        if isinstance(item, Mapping)
                    ).items()
                )
            ),
        },
    }
    server.reset_session_state()
    server._compile_source_index_runtime = previous_runtime
    for name, previous in saved_environment.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
    return report


def _median(runs: Sequence[Mapping[str, Any]], field: str) -> float:
    return round(
        statistics.median(float(run["measurement"][field]) for run in runs),
        3,
    )


def aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_mode = {
        mode: [run for run in runs if run["trial_index"] == mode]
        for mode in ("disabled", "enabled")
    }
    aggregates: dict[str, Any] = {}
    for mode, selected in by_mode.items():
        if not selected:
            continue
        aggregates[mode] = {
            "run_count": len(selected),
            "parallel_wall_median_ms": _median(selected, "parallel_wall_ms"),
            "hierarchy_wall_median_ms": _median(
                selected, "hierarchy_public_wall_ms"
            ),
            "structural_wall_median_ms": _median(
                selected, "structural_public_wall_ms"
            ),
            "source_open_median": _median(selected, "source_open_count"),
            "logical_source_bytes_median": _median(
                selected, "logical_source_bytes"
            ),
            "peak_rss_median_kib": _median(selected, "peak_rss_kib"),
            "hierarchy_oracles": sorted(
                {run["behavior_oracle"]["hierarchy_sha256"] for run in selected}
            ),
            "structural_oracles": sorted(
                {run["behavior_oracle"]["structural_sha256"] for run in selected}
            ),
        }
    comparison = None
    if set(aggregates) == {"disabled", "enabled"}:
        before = aggregates["disabled"]
        after = aggregates["enabled"]

        def reduction(field: str) -> float | None:
            baseline = float(before[field])
            if baseline <= 0:
                return None
            return round((baseline - float(after[field])) * 100.0 / baseline, 3)

        comparison = {
            "parallel_wall_reduction_percent": reduction(
                "parallel_wall_median_ms"
            ),
            "source_open_reduction_percent": reduction("source_open_median"),
            "logical_source_bytes_reduction_percent": reduction(
                "logical_source_bytes_median"
            ),
            "peak_rss_change_kib": round(
                float(after["peak_rss_median_kib"])
                - float(before["peak_rss_median_kib"]),
                3,
            ),
            "behavior_equal": (
                before["hierarchy_oracles"] == after["hierarchy_oracles"]
                and before["structural_oracles"] == after["structural_oracles"]
            ),
        }
    return {"by_mode": aggregates, "comparison": comparison}


def _child_command(args: argparse.Namespace, mode: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--compile-log",
        str(args.compile_log),
        "--simulator",
        args.simulator,
        "--max-bytes",
        str(args.max_bytes),
        "--max-files",
        str(args.max_files),
        "--trial-index",
        mode,
        "--child",
    ]


def _run_child(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    completed = subprocess.run(
        _child_command(args, mode),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"benchmark child failed for {mode}: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument("--simulator", choices=("auto", "vcs", "xcelium"), default="auto")
    parser.add_argument("--index", choices=("compare", "disabled", "enabled"), default="compare")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--max-files", type=int, default=32_768)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--trial-index",
        choices=("disabled", "enabled"),
        default="enabled",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.repeats < 1 or args.max_bytes < 1 or args.max_files < 1:
        raise SystemExit("repeats/max-bytes/max-files must be positive")
    if args.child:
        print(json.dumps(run_trial(args), sort_keys=True, separators=(",", ":")))
        return 0

    requested_modes = (
        ("disabled", "enabled")
        if args.index == "compare"
        else (args.index,)
    )
    runs = []
    for repeat in range(args.repeats):
        modes = requested_modes if repeat % 2 == 0 else tuple(reversed(requested_modes))
        for mode in modes:
            runs.append(_run_child(args, mode))
    report = {
        "benchmark": BENCHMARK_NAME,
        "schema_version": 1,
        "git": {"head": _git_head(), "dirty": _git_dirty()},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or None,
            "filesystem_cache": "uncontrolled; trial order alternates",
        },
        "conditions": {
            "repeats_per_mode": args.repeats,
            "index_max_bytes": args.max_bytes,
            "index_max_files": args.max_files,
            "fresh_process_per_trial": True,
        },
        "runs": runs,
        "aggregate": aggregate_runs(runs),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
