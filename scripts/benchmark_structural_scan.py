#!/usr/bin/env python3
"""Benchmark whole-compile structural scanning on a real project.

The report intentionally omits paths and finding text. It retains a hash of
the full path-bearing result so the same workload can prove behavior equality.
"""

from __future__ import annotations

import argparse
import builtins
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.compile_log_parser import detect_simulator  # noqa: E402
from src.hdl_suffixes import TEXT_SCAN_HDL_SUFFIXES  # noqa: E402
from src.operation_metrics import read_process_rss_kib  # noqa: E402
from src.structural_scanner import (  # noqa: E402
    ALL_CATEGORIES,
    scan_structural_risks,
)


BENCHMARK_NAME = "structural_scan_real_v1"
_SOURCE_SUFFIXES = tuple(sorted(TEXT_SCAN_HDL_SUFFIXES))


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


def _result_oracle(result: dict[str, Any]) -> tuple[str, int]:
    encoded = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    simulator = args.simulator
    if simulator == "auto":
        detected = detect_simulator(args.compile_log)
        simulator = detected if detected in {"vcs", "xcelium"} else "auto"
    source_open_count = 0
    logical_source_bytes_read = 0
    real_open = builtins.open

    def counted_open(path, *open_args, **open_kwargs):
        nonlocal source_open_count, logical_source_bytes_read
        rendered = os.fspath(path)
        if rendered.lower().endswith(_SOURCE_SUFFIXES):
            source_open_count += 1
            try:
                logical_source_bytes_read += os.stat(rendered).st_size
            except OSError:
                pass
        return real_open(path, *open_args, **open_kwargs)

    rss_start_kib = read_process_rss_kib()
    started = time.perf_counter()
    builtins.open = counted_open
    try:
        result = scan_structural_risks(
            args.compile_log,
            simulator,
            categories=args.category or None,
        )
    finally:
        builtins.open = real_open
    wall_ms = (time.perf_counter() - started) * 1000.0
    result_sha256, result_bytes = _result_oracle(result)
    risk_counts = Counter(item["type"] for item in result["risks"])
    return {
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "workload": {
            "simulator": simulator,
            "categories": list(result["categories_scanned"]),
        },
        "measurement": {
            "wall_ms": round(wall_ms, 3),
            "rss_start_kib": rss_start_kib,
            "rss_end_kib": read_process_rss_kib(),
            "process_peak_rss_kib": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
            "source_open_count": source_open_count,
            "logical_source_bytes_read": logical_source_bytes_read,
            "result_bytes": result_bytes,
        },
        "structural_oracle": {
            "result_sha256": result_sha256,
            "coverage_status": result["coverage_status"],
            "eligible_file_count": result["eligible_file_count"],
            "files_scanned": result["files_scanned"],
            "total_risks": result["total_risks"],
            "risk_counts": dict(sorted(risk_counts.items())),
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", required=True)
    parser.add_argument(
        "--simulator",
        choices=("auto", "vcs", "xcelium"),
        default="auto",
    )
    parser.add_argument(
        "--category",
        action="append",
        choices=ALL_CATEGORIES,
        default=[],
        help="Scan one category; repeat for multiple. Defaults to all.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    print(json.dumps(run_benchmark(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
