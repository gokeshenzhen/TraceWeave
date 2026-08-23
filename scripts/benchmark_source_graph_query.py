#!/usr/bin/env python3
"""Reproduce bounded versus full high-fanout Source Graph query costs.

Each invocation runs one mode in one process so peak RSS remains attributable.
``bounded`` uses the production driver/load budgets. ``full`` raises only the
edge and match limits enough to materialize the complete synthetic fanout; it
is the reproducible counterfactual for the formerly unbounded behavior.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.connectivity_ir import (  # noqa: E402
    AssignmentFact,
    BitRange,
    BoundaryKind,
    ConnectivityIR,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    DependencyFact,
    EdgeKind,
    InstanceDecl,
    SignalDecl,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
    SymbolKind,
)
from src.connectivity_query import ConnectivityQueryEngine  # noqa: E402
from src.source_graph_contract import (  # noqa: E402
    DEFAULT_QUERY_EDGE_LIMIT,
    DEFAULT_QUERY_FRONTIER_LIMIT,
    DEFAULT_QUERY_MATCH_LIMIT,
    DEFAULT_QUERY_STATE_LIMIT,
)


BENCHMARK_NAME = "source_graph_query_fanout_v1"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _current_rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


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


def _distribution(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def build_fanout_ir(fanout: int) -> ConnectivityIR:
    location = SourceLocation(file="synthetic/high_fanout.sv", line=1)
    scalar = BitRange.scalar()
    assignments = tuple(
        AssignmentFact(
            assignment_id=f"fanout-{index:08d}",
            kind=EdgeKind.CONTINUOUS_ASSIGN,
            target=SignalSelection.template("sink", scalar),
            dependencies=(
                DependencyFact(
                    source=SignalSelection.template("source", scalar),
                    target=SignalSelection.template("sink", scalar),
                ),
            ),
            boundary=BoundaryKind.COMBINATIONAL,
            evidence=SourceEvidence(
                construct="synthetic_fanout",
                location=SourceLocation(
                    file=location.file,
                    line=index + 2,
                ),
                frontend="synthetic_benchmark",
                frontend_version="1",
            ),
        )
        for index in range(fanout)
    )
    definition = DefinitionTemplate(
        definition_id="fanout_top",
        name="fanout_top",
        kind=DefinitionKind.MODULE,
        location=location,
        signals=(
            SignalDecl("source", SymbolKind.NET, scalar, location),
            SignalDecl("sink", SymbolKind.NET, scalar, location),
        ),
        assignments=assignments,
    )
    return ConnectivityIR(
        frontend_name="synthetic_benchmark",
        frontend_version="1",
        definitions=(definition,),
        instances=(
            InstanceDecl(
                "fanout_top",
                "fanout_top",
                "fanout_top",
                None,
                location,
            ),
        ),
        bindings=(),
        coverage=CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=1,
            files_projected=1,
        ),
        top_instances=("fanout_top",),
    )


def run_benchmark(*, fanout: int, repeats: int, mode: str) -> dict[str, Any]:
    if fanout < 1 or repeats < 1:
        raise ValueError("fanout and repeats must be positive")
    if mode not in {"bounded", "full"}:
        raise ValueError("mode must be bounded or full")

    rss_start = _current_rss_kib()
    started = time.perf_counter()
    ir = build_fanout_ir(fanout)
    ir_build_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    engine = ConnectivityQueryEngine(ir)
    index_build_ms = (time.perf_counter() - started) * 1000.0
    rss_before_queries = _current_rss_kib()

    query_ms: list[float] = []
    serialization_ms: list[float] = []
    result_fingerprints: list[str] = []
    result_summary: dict[str, Any] | None = None
    json_bytes = 0
    limits: dict[str, int] = {}
    if mode == "full":
        limits = {"edge_limit": fanout + 1, "match_limit": fanout + 1}

    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        result = engine.query_loads("fanout_top.source", **limits)
        query_ms.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter()
        encoded = json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        serialization_ms.append((time.perf_counter() - started) * 1000.0)
        json_bytes = len(encoded)
        result_fingerprints.append(hashlib.sha256(encoded).hexdigest())
        result_summary = {
            "status": result.status.value,
            "coverage_status": result.coverage_status.value,
            "match_count": len(result.matches),
            "visited_state_count": result.visited_state_count,
            "inspected_edge_count": result.inspected_edge_count,
            "query_truncated": result.truncated,
            "state_truncated": result.state_truncated,
            "edge_truncated": result.edge_truncated,
            "match_truncated": result.match_truncated,
            "frontier_truncated": result.frontier_truncated,
            "coverage_gap_codes": [
                gap.code for gap in result.unresolved_boundaries
            ],
        }
        del encoded, result

    assert result_summary is not None
    rss_after_queries = _current_rss_kib()
    return {
        "benchmark": BENCHMARK_NAME,
        "mode": mode,
        "workload": {
            "fanout": fanout,
            "repeats": repeats,
            "signal_width": 1,
            "operation": "loads",
        },
        "environment": {
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "limits": {
            "state_limit": DEFAULT_QUERY_STATE_LIMIT,
            "edge_limit": (
                fanout + 1 if mode == "full" else DEFAULT_QUERY_EDGE_LIMIT
            ),
            "match_limit": (
                fanout + 1 if mode == "full" else DEFAULT_QUERY_MATCH_LIMIT
            ),
            "frontier_limit": DEFAULT_QUERY_FRONTIER_LIMIT,
        },
        "timings_ms": {
            "ir_build": round(ir_build_ms, 3),
            "index_build": round(index_build_ms, 3),
            "query": _distribution(query_ms),
            "serialization": _distribution(serialization_ms),
        },
        "memory_kib": {
            "rss_start": rss_start,
            "rss_before_queries": rss_before_queries,
            "rss_after_queries": rss_after_queries,
            "process_peak": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "result": {
            **result_summary,
            "json_bytes": json_bytes,
            "stable_across_repeats": len(set(result_fingerprints)) == 1,
            "fingerprint_sha256": result_fingerprints[0],
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fanout", type=_positive_int, default=50_000)
    parser.add_argument("--repeats", type=_positive_int, default=3)
    parser.add_argument("--mode", choices=("bounded", "full"), default="bounded")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    print(
        json.dumps(
            run_benchmark(
                fanout=args.fanout,
                repeats=args.repeats,
                mode=args.mode,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
