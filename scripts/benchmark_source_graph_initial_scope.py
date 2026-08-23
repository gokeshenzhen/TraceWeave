#!/usr/bin/env python3
"""Benchmark reactive versus bounded-adjacent Source Graph preparation.

Run each strategy in a fresh process over the same compile log and target. The
reactive strategy prepares the exact-ancestor artifact followed by the bounded
adjacent artifact; bounded-adjacent prepares only the selected larger artifact.
The final artifact is queried once to fingerprint behavioral equivalence.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
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

import server  # noqa: E402
from src.source_graph_adapter import (  # noqa: E402
    AdapterStatus,
    SourceGraphBuildPlan,
    build_source_graph_initial_plan,
    build_source_graph_plan,
)
from src.source_graph_backend import SourceGraphConnectivityBackend  # noqa: E402
from src.source_graph_contract import (  # noqa: E402
    QueryOperation,
    compute_source_graph_build_key,
)
from src.source_graph_runtime import (  # noqa: E402
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    SourceGraphPrepareOutcome,
    SourceGraphRuntime,
)


BENCHMARK_NAME = "source_graph_initial_scope_ab_v1"
_NPI_OVERLAY_ENV = "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"


@contextmanager
def _without_npi_hierarchy_overlay():
    """Disable the unrelated overlay only for an active benchmark run."""

    previous = os.environ.get(_NPI_OVERLAY_ENV)
    os.environ[_NPI_OVERLAY_ENV] = "off"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_NPI_OVERLAY_ENV, None)
        else:
            os.environ[_NPI_OVERLAY_ENV] = previous


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


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


def _plan_shape(plan: SourceGraphBuildPlan) -> dict[str, Any]:
    if plan.status is not AdapterStatus.READY or plan.request is None:
        blocker = plan.receipt.blocker
        return {
            "status": plan.status.value,
            "blocker": blocker.code if blocker is not None else None,
        }
    artifact = plan.request.artifact_identity
    projection = artifact.compile_projection
    return {
        "status": plan.status.value,
        "artifact_fingerprint_sha256": compute_source_graph_build_key(
            plan.request
        ).digest,
        "scope_kind": plan.receipt.scope_kind,
        "scope_instance_count": len(artifact.scope.projection_instance_paths),
        "compile_input_count": (
            len(projection.ordered_inputs)
            if projection is not None
            else len(artifact.source.compile_inputs.ordered_inputs)
        ),
        "coverage_objective_exclusion_count": len(
            artifact.scope.coverage_boundary.objective_exclusions
        ),
    }


def _prepare_summary(outcomes: Sequence[SourceGraphPrepareOutcome]) -> dict[str, Any]:
    peaks = [
        item.metrics.rss_peak_kib
        for item in outcomes
        if item.metrics.rss_peak_kib is not None
    ]
    return {
        "statuses": [item.status.value for item in outcomes],
        "actual_build_count": sum(
            item.metrics.actual_build_count for item in outcomes
        ),
        "frontend_launch_count": sum(
            item.metrics.frontend_launch_count for item in outcomes
        ),
        "aggregate_prepare_wall_ms": round(
            sum(item.metrics.total_wall_ms for item in outcomes), 3
        ),
        "aggregate_worker_build_wall_ms": round(
            sum(item.metrics.build_wall_ms for item in outcomes), 3
        ),
        "aggregate_parent_load_wall_ms": round(
            sum(item.metrics.load_wall_ms for item in outcomes), 3
        ),
        "max_worker_peak_rss_kib": max(peaks) if peaks else None,
        "final_ir_bytes": outcomes[-1].metrics.ir_bytes,
    }


def _query_final(
    *,
    outcome: SourceGraphPrepareOutcome,
    plan: SourceGraphBuildPlan,
    operation: QueryOperation,
    signal: str,
    compile_log: str,
    max_depth: int,
    recursive: bool,
) -> dict[str, Any]:
    if outcome.status is not PrepareStatus.READY or outcome.entry is None:
        return {"status": "not_run"}
    backend = SourceGraphConnectivityBackend(outcome.entry)
    backend.set_unprojected_instance_candidates(
        plan.unprojected_instance_candidates
    )
    if operation is QueryOperation.DRIVER:
        result = backend.find_driver(
            signal_path=signal,
            wave_path="",
            compile_log=compile_log,
            recursive=recursive,
            max_depth=max_depth,
        )
    else:
        result = backend.find_loads(
            signal_path=signal,
            compile_log=compile_log,
            max_depth=max_depth,
        )
    receipt = result.pop("_source_graph_query_receipt")
    public_bytes = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "status": receipt["status"],
        "coverage_status": receipt["coverage_status"],
        "match_count": receipt["match_count"],
        "visited_state_count": receipt["visited_state_count"],
        "inspected_edge_count": receipt["inspected_edge_count"],
        "query_truncated": receipt["query_truncated"],
        "expansion_frontier_count": len(receipt["expansion_frontiers"]),
        "public_result_sha256": hashlib.sha256(public_bytes).hexdigest(),
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    operation = QueryOperation(args.operation)
    hierarchy_started = time.perf_counter()
    # Hierarchy construction is part of the measured local pipeline, but
    # loading a licensed NPI design would mix an unrelated optional overlay
    # into this Source Graph benchmark. Keep that environment override scoped
    # to the call so importing this module never changes server behavior.
    with _without_npi_hierarchy_overlay():
        await server._dispatch(
            "build_tb_hierarchy",
            {"compile_log": args.compile_log, "simulator": args.simulator},
        )
    hierarchy_wall_ms = (time.perf_counter() - hierarchy_started) * 1000.0
    hierarchy, snapshot = server._resolve_hierarchy_context(
        args.compile_log,
        args.simulator,
    )
    compile_result = hierarchy.get("compile_result")
    if not isinstance(compile_result, dict):
        raise RuntimeError("hierarchy lacks compile_result")
    common = {
        "compile_log": args.compile_log,
        "compile_result": compile_result,
        "hierarchy_result": hierarchy,
        "hierarchy_snapshot_sha256": snapshot,
        "operation": operation,
        "signal_path": args.signal,
        "top_hint": args.top,
        "max_hops": args.max_depth,
        "frontend_version": args.frontend_version,
        "recursive": args.recursive if operation is QueryOperation.DRIVER else False,
        "include_expr": True,
        "kind_filter": (),
    }
    base_started = time.perf_counter()
    base = build_source_graph_plan(**common)
    base_plan_wall_ms = (time.perf_counter() - base_started) * 1000.0
    adjacent_started = time.perf_counter()
    adjacent = build_source_graph_initial_plan(
        **common,
        max_instances=args.frontier_max_instances,
    )
    adjacent_plan_wall_ms = (time.perf_counter() - adjacent_started) * 1000.0
    if base.request is None or adjacent.request is None:
        raise RuntimeError("benchmark plans must be ready")
    base_key = compute_source_graph_build_key(base.request).digest
    adjacent_key = compute_source_graph_build_key(adjacent.request).digest
    if base_key == adjacent_key:
        raise RuntimeError("bounded adjacent policy did not select a larger artifact")

    runtime = SourceGraphRuntime(
        IsolatedSourceGraphProcessRunner(python_executable=args.python)
    )
    selected_plans = (
        (base, adjacent)
        if args.strategy == "reactive-sequence"
        else (adjacent,)
    )
    prepare_started = time.perf_counter()
    outcomes = [
        await runtime.prepare(
            plan.request,
            timeout_seconds=args.timeout,
        )
        for plan in selected_plans
    ]
    measured_prepare_wall_ms = (time.perf_counter() - prepare_started) * 1000.0
    final = outcomes[-1]
    return {
        "benchmark": BENCHMARK_NAME,
        "strategy": args.strategy,
        "environment": {
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "workload": {
            "operation": operation.value,
            "simulator": args.simulator,
            "max_depth": args.max_depth,
            "recursive": (
                args.recursive if operation is QueryOperation.DRIVER else False
            ),
            "frontier_max_instances": args.frontier_max_instances,
        },
        "planning": {
            "hierarchy_wall_ms": round(hierarchy_wall_ms, 3),
            "base_plan_wall_ms": round(base_plan_wall_ms, 3),
            "bounded_adjacent_plan_wall_ms": round(adjacent_plan_wall_ms, 3),
            "base": _plan_shape(base),
            "bounded_adjacent": _plan_shape(adjacent),
        },
        "preparation": {
            **_prepare_summary(outcomes),
            "measured_prepare_wall_ms": round(measured_prepare_wall_ms, 3),
            "runtime_cache_entry_count": runtime.stats_snapshot()[
                "cache_entry_count"
            ],
            "runtime_cache_bytes": runtime.stats_snapshot()["cache_bytes"],
            "parent_process_peak_rss_kib": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
        },
        "query": _query_final(
            outcome=final,
            plan=adjacent,
            operation=operation,
            signal=args.signal,
            compile_log=args.compile_log,
            max_depth=args.max_depth,
            recursive=args.recursive,
        ),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", required=True)
    parser.add_argument("--signal", required=True)
    parser.add_argument("--simulator", choices=("auto", "vcs", "xcelium"), default="auto")
    parser.add_argument("--operation", choices=("driver", "loads"), default="driver")
    parser.add_argument("--top")
    parser.add_argument("--max-depth", type=_positive_int, default=20)
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--strategy",
        choices=("reactive-sequence", "bounded-adjacent"),
        required=True,
    )
    parser.add_argument("--frontier-max-instances", type=_positive_int, default=128)
    parser.add_argument("--frontend-version", default="11.0.0")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=_positive_float, default=240.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    print(json.dumps(asyncio.run(run_benchmark(args)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
