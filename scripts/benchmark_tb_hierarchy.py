#!/usr/bin/env python3
"""Benchmark full compile-log hierarchy construction on a real project.

The default path is compile-log-only and never loads an NPI design. The output
omits source paths while retaining phase timings, bounded preprocessor counters,
RSS, hierarchy counts, and a deterministic hash of the path-bearing structural
payload for same-workload before/after equivalence checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.compile_log_parser import (  # noqa: E402
    detect_simulator,
    merge_compile_results,
    parse_compile_log,
)
from src.operation_metrics import read_process_rss_kib  # noqa: E402
from src.tb_hierarchy_builder import build_hierarchy, compute_stats  # noqa: E402


BENCHMARK_NAME = "tb_hierarchy_real_v1"
_STRUCTURAL_FIELDS = (
    "project",
    "files",
    "component_tree",
    "class_hierarchy",
    "interfaces",
    "compile_result",
    "_scan_results",
)


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


def _parse_context(
    *,
    compile_log: str,
    simulator: str,
    supplementary_compile_logs: Sequence[str],
) -> tuple[dict[str, Any], str]:
    primary = parse_compile_log(compile_log, simulator)
    context_simulator = str(primary.get("simulator") or simulator)
    if not supplementary_compile_logs:
        return primary, context_simulator
    supplements = []
    for path in supplementary_compile_logs:
        detected = detect_simulator(path)
        parse_simulator = (
            detected if detected in {"vcs", "xcelium"} else context_simulator
        )
        supplements.append(parse_compile_log(path, parse_simulator))
    return (
        merge_compile_results(
            primary,
            supplements,
            primary_log=compile_log,
            supplementary_logs=list(supplementary_compile_logs),
        ),
        context_simulator,
    )


def _structural_sha256(result: Mapping[str, Any]) -> str:
    payload = {field: result[field] for field in _STRUCTURAL_FIELDS}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    rss_start_kib = read_process_rss_kib()
    parse_started = time.perf_counter()
    compile_result, context_simulator = _parse_context(
        compile_log=args.compile_log,
        simulator=args.simulator,
        supplementary_compile_logs=args.supplementary_compile_log,
    )
    parse_wall_ms = (time.perf_counter() - parse_started) * 1000.0
    build_started = time.perf_counter()
    hierarchy = build_hierarchy(
        compile_result,
        args.compile_log,
        apply_source_overlay=args.npi_source_overlay,
    )
    build_wall_ms = (time.perf_counter() - build_started) * 1000.0
    metrics = hierarchy["build_metrics"]
    preprocessor_metrics = {
        key: value
        for key, value in metrics.items()
        if key.startswith("preprocessor_")
    }
    return {
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "workload": {
            "simulator": context_simulator,
            "compile_log_count": 1 + len(args.supplementary_compile_log),
            "npi_source_overlay": args.npi_source_overlay,
        },
        "measurement": {
            "parse_wall_ms": round(parse_wall_ms, 3),
            "build_wall_ms": round(build_wall_ms, 3),
            "rss_start_kib": rss_start_kib,
            "rss_end_kib": read_process_rss_kib(),
            "process_peak_rss_kib": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
        },
        "build_metrics": metrics,
        "preprocessor_metrics": preprocessor_metrics,
        "hierarchy_oracle": {
            **compute_stats(hierarchy),
            "structural_sha256": _structural_sha256(hierarchy),
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", required=True)
    parser.add_argument(
        "--supplementary-compile-log",
        action="append",
        default=[],
        help="Additional compile/elaboration log in build order; repeatable.",
    )
    parser.add_argument(
        "--simulator",
        choices=("auto", "vcs", "xcelium"),
        default="auto",
    )
    parser.add_argument(
        "--npi-source-overlay",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Include the optional licensed NPI file/line overlay.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    print(json.dumps(run_benchmark(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
