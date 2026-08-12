#!/usr/bin/env python3
"""Benchmark the public Source Graph route on an arbitrary SoC build.

Unlike the frozen phase benchmarks, this harness does not assume DVSim,
FuseSoC, Bazel, or a particular checkout layout.  Callers provide the compile
log, waveform, top, and three representative connectivity endpoints.  Every
sample runs in a fresh TraceWeave process with the disk cache disabled.  The
sample performs hierarchy recovery while a structural scan runs concurrently,
then measures one cold driver query and process-memory driver/load/path hits.

The normal production backend order is preserved.  A usable Verdi KDB may win
before Source Graph; that is reported as an unavailable Source Graph sample,
not silently bypassed by benchmark-only routing.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_soc_p3"
DEFAULT_FRONTEND_PYTHON = ROOT / ".venv/bin/python"


class BenchmarkInputError(ValueError):
    """Raised when a benchmark workload cannot be reproduced safely."""


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported result type: {type(value).__name__}")


def _current_rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _git_head(path: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(result, 3)


def _distribution(values: Sequence[float | int | None]) -> dict[str, Any]:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return {"n": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "n": len(numeric),
        "min": round(min(numeric), 3),
        "median": round(statistics.median(numeric), 3),
        "p95": _percentile(numeric, 0.95),
        "max": round(max(numeric), 3),
    }


def _query_facts(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if operation == "driver":
        return {
            "driver_status": payload.get("driver_status"),
            "driver_kind": payload.get("driver_kind"),
            "source_file": payload.get("source_file"),
            "source_line": payload.get("source_line"),
            "upstream_signals": payload.get("upstream_signals", []),
            "driver_chain": payload.get("driver_chain"),
        }
    if operation == "loads":
        return {
            "completeness": payload.get("completeness"),
            "loads": payload.get("loads", []),
        }
    if operation == "path":
        return {
            "found": payload.get("found"),
            "hops": payload.get("hops"),
            "path": payload.get("path", []),
        }
    raise ValueError(f"unknown operation: {operation}")


def _compact_query(
    operation: str,
    phase: str,
    result: Any,
    wall_ms: float,
) -> dict[str, Any]:
    payload = _model_dict(result)
    backend = payload.get("backend_status") or {}
    source_graph = backend.get("source_graph") or {}
    metrics = source_graph.get("metrics") or {}
    facts = _query_facts(operation, payload)
    cache_tier = source_graph.get("cache_tier")
    artifact_reuse = source_graph.get("artifact_reuse")
    effective_cache_tier = cache_tier
    if effective_cache_tier is None and source_graph.get("prepare_status") == "ready":
        if artifact_reuse in {"exact_hit", "dominating_hit"}:
            effective_cache_tier = "memory"
        elif int(metrics.get("actual_build_count") or 0) > 0:
            effective_cache_tier = "build"
    return {
        "operation": operation,
        "phase": phase,
        "wall_ms": round(wall_ms, 3),
        "actual_backend": backend.get("actual_backend"),
        "selected_backend": backend.get("selected_backend"),
        "attempted_backends": backend.get("attempted_backends", []),
        "fallback_reason": backend.get("fallback_reason"),
        "kdb_flow": backend.get("kdb_flow"),
        "kdb_validation_status": backend.get("kdb_validation_status"),
        "single_backend_provenance": backend.get("single_backend_provenance"),
        "source_graph": {
            "prepare_status": source_graph.get("prepare_status"),
            "cache_disposition": source_graph.get("cache_disposition"),
            "cache_tier": cache_tier,
            "effective_cache_tier": effective_cache_tier,
            "artifact_reuse": artifact_reuse,
            "coverage_status": source_graph.get("coverage_status"),
            "coverage_files_total": source_graph.get("coverage_files_total"),
            "coverage_files_projected": source_graph.get("coverage_files_projected"),
            "coverage_gap_codes": source_graph.get("coverage_gap_codes", []),
            "blocker": source_graph.get("blocker"),
            "adapter": source_graph.get("adapter"),
            "query_status": source_graph.get("query_status"),
            "query_confidence": source_graph.get("query_confidence"),
            "query_match_count": source_graph.get("query_match_count"),
            "artifact_fingerprint_sha256": source_graph.get(
                "selected_artifact_fingerprint_sha256"
            ),
            "compile_fingerprint_sha256": source_graph.get(
                "compile_fingerprint_sha256"
            ),
            "ir_fingerprint_sha256": source_graph.get("ir_fingerprint_sha256"),
            "metrics": metrics,
        },
        "fact_count": (
            len(facts.get("loads", []))
            if operation == "loads"
            else len(facts.get("path", []))
            if operation == "path"
            else len(facts.get("driver_chain") or []) or 1
        ),
        "facts_sha256": _sha256_json(facts),
        "facts": facts,
    }


async def _timed_dispatch(
    server_module: Any,
    operation: str,
    phase: str,
    tool: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    result = await server_module._dispatch(tool, dict(arguments))
    return _compact_query(
        operation,
        phase,
        result,
        (time.perf_counter() - started) * 1000.0,
    )


def _run_structural_scan(
    server_module: Any, compile_log: str, simulator: str
) -> dict[str, Any]:
    started = time.perf_counter()
    result = server_module.scan_structural_risks(
        compile_log=compile_log,
        simulator=simulator,
        scan_scope="scope1",
        categories=None,
    )
    payload = _model_dict(result)
    return {
        "wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "coverage_status": payload.get("coverage_status"),
        "files_total": payload.get("eligible_file_count"),
        "files_scanned": payload.get("files_scanned"),
        "files_skipped": len(payload.get("skipped_files", [])),
        "total_risks": payload.get("total_risks"),
    }


async def _run_child_async(args: argparse.Namespace) -> dict[str, Any]:
    # Import after the parent has fixed the child environment.  This keeps the
    # server interpreter and the optional frontend interpreter independent.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import server  # noqa: PLC0415

    compile_log = str(args.compile_log)
    wave_path = str(args.wave_path)
    common = {
        "compile_log": compile_log,
        "simulator": args.simulator,
    }
    if args.top_hint:
        common["top_hint"] = args.top_hint

    rss_start = _current_rss_kib()
    self_peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_peak_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

    # scan_structural_risks is a synchronous source scan.  Submit it first to a
    # worker thread and yield once so it overlaps hierarchy recovery, matching
    # the documented default workflow on the same compile log.
    preparation_started = time.perf_counter()
    scan_task = asyncio.create_task(
        asyncio.to_thread(
            _run_structural_scan,
            server,
            compile_log,
            args.simulator,
        )
    )
    await asyncio.sleep(0)
    hierarchy_started = time.perf_counter()
    hierarchy_result = await server._dispatch(
        "build_tb_hierarchy",
        {"compile_log": compile_log, "simulator": args.simulator},
    )
    hierarchy_wall_ms = (time.perf_counter() - hierarchy_started) * 1000.0
    structural = await scan_task
    preparation_wall_ms = (time.perf_counter() - preparation_started) * 1000.0
    hierarchy = _model_dict(hierarchy_result)

    driver_args = {
        **common,
        "signal_path": args.driver_signal,
        "wave_path": wave_path,
        "recursive": True,
        "max_depth": args.driver_max_depth,
    }
    load_args = {
        **common,
        "signal_path": args.load_signal,
        "max_depth": 1,
        "include_expr": True,
    }
    path_args = {
        **common,
        "from_signal": args.from_signal,
        "to_signal": args.to_signal,
        "expand_assigns": True,
    }

    queries = [
        await _timed_dispatch(
            server,
            "driver",
            "cold",
            "explain_signal_driver",
            driver_args,
        )
    ]
    for _ in range(args.warm_repeats):
        queries.append(
            await _timed_dispatch(
                server,
                "driver",
                "memory",
                "explain_signal_driver",
                driver_args,
            )
        )
    queries.extend(
        [
            await _timed_dispatch(
                server,
                "loads",
                "memory",
                "find_signal_loads",
                load_args,
            ),
            await _timed_dispatch(
                server,
                "path",
                "memory",
                "trace_signal_path",
                path_args,
            ),
        ]
    )

    return {
        "process_id": os.getpid(),
        "repository_head": _git_head(ROOT),
        "preparation": {
            "parallel": True,
            "wall_ms": round(preparation_wall_ms, 3),
            "hierarchy_wall_ms": round(hierarchy_wall_ms, 3),
            "hierarchy": {
                "stats": hierarchy.get("stats", {}),
                "project": hierarchy.get("project", {}),
                "hierarchy_handle_present": bool(hierarchy.get("hierarchy_handle")),
            },
            "structural_scan": structural,
        },
        "queries": queries,
        "process_memory": {
            "rss_start_kib": rss_start,
            "rss_end_kib": _current_rss_kib(),
            "rss_peak_kib_before": self_peak_before,
            "rss_peak_kib_after": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "frontend_children_peak_kib_before": child_peak_before,
            "frontend_children_peak_kib_after": resource.getrusage(
                resource.RUSAGE_CHILDREN
            ).ru_maxrss,
        },
    }


def _child_main(args: argparse.Namespace) -> int:
    print(json.dumps(asyncio.run(_run_child_async(args)), sort_keys=True))
    return 0


def _child_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-run",
        "--compile-log",
        str(args.compile_log),
        "--wave-path",
        str(args.wave_path),
        "--simulator",
        args.simulator,
        "--driver-signal",
        args.driver_signal,
        "--load-signal",
        args.load_signal,
        "--from-signal",
        args.from_signal,
        "--to-signal",
        args.to_signal,
        "--driver-max-depth",
        str(args.driver_max_depth),
        "--warm-repeats",
        str(args.warm_repeats),
        "--frontend-python",
        str(args.frontend_python),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
    ]
    if args.top_hint:
        command.extend(["--top-hint", args.top_hint])
    return command


def _run_fresh_process(args: argparse.Namespace) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "TRACEWEAVE_SOURCE_GRAPH": "1",
            "TRACEWEAVE_SOURCE_GRAPH_PYTHON": str(args.frontend_python),
            "TRACEWEAVE_SOURCE_GRAPH_FRONTEND_VERSION": "11.0.0",
            "TRACEWEAVE_SOURCE_GRAPH_TIMEOUT": str(args.worker_timeout_seconds),
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE": "0",
            "TRACEWEAVE_TELEMETRY": "0",
            "TRACEWEAVE_AUTO_KDB": "0",
        }
    )
    completed = subprocess.run(
        _child_command(args),
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=args.process_timeout_seconds,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        detail = stderr[-1] if stderr else "no stderr"
        raise BenchmarkInputError(
            f"fresh benchmark process failed ({completed.returncode}): {detail}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkInputError(
            "fresh benchmark process returned invalid JSON"
        ) from exc


def _operation_runs(runs: Sequence[Mapping[str, Any]], operation: str) -> list[dict]:
    return [
        query
        for run in runs
        for query in run.get("queries", [])
        if query.get("operation") == operation
    ]


def _aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    queries = [query for run in runs for query in run.get("queries", [])]
    cold = [query for query in queries if query.get("phase") == "cold"]
    memory = [query for query in queries if query.get("phase") == "memory"]
    cold_metrics = [query.get("source_graph", {}).get("metrics", {}) for query in cold]
    fact_hashes: dict[str, list[str]] = {}
    for operation in ("driver", "loads", "path"):
        fact_hashes[operation] = sorted(
            {query["facts_sha256"] for query in _operation_runs(runs, operation)}
        )
    source_queries = [
        query for query in queries if query.get("actual_backend") == "source_graph"
    ]
    memory_source_queries = [
        query for query in memory if query.get("actual_backend") == "source_graph"
    ]
    disk_activity = sum(
        int(metrics.get(field) or 0)
        for metrics in cold_metrics
        for field in ("disk_hit_count", "disk_miss_count", "disk_corrupt_count")
    )
    return {
        "fresh_process_count": len(runs),
        "query_count": len(queries),
        "source_graph_query_count": len(source_queries),
        "cold_driver_wall_ms": _distribution([query.get("wall_ms") for query in cold]),
        "cold_prepare_wall_ms": _distribution(
            [metrics.get("prepare_total_wall_ms") for metrics in cold_metrics]
        ),
        "cold_build_wall_ms": _distribution(
            [metrics.get("build_wall_ms") for metrics in cold_metrics]
        ),
        "cold_worker_cpu_ms": _distribution(
            [metrics.get("worker_cpu_ms") for metrics in cold_metrics]
        ),
        "cold_worker_rss_peak_kib": _distribution(
            [metrics.get("rss_peak_kib") for metrics in cold_metrics]
        ),
        "memory_query_wall_ms": _distribution(
            [query.get("wall_ms") for query in memory_source_queries]
        ),
        "by_operation_wall_ms": {
            operation: _distribution(
                [query.get("wall_ms") for query in _operation_runs(runs, operation)]
            )
            for operation in ("driver", "loads", "path")
        },
        "preparation_wall_ms": _distribution(
            [run.get("preparation", {}).get("wall_ms") for run in runs]
        ),
        "parent_rss_peak_kib": _distribution(
            [run.get("process_memory", {}).get("rss_peak_kib_after") for run in runs]
        ),
        "cache_tier_counts": {
            tier: sum(
                query.get("source_graph", {}).get("effective_cache_tier") == tier
                for query in source_queries
            )
            for tier in ("build", "memory", "disk")
        },
        "actual_build_count": sum(
            int(metrics.get("actual_build_count") or 0) for metrics in cold_metrics
        ),
        "frontend_launch_count": sum(
            int(metrics.get("frontend_launch_count") or 0) for metrics in cold_metrics
        ),
        "disk_activity_count": disk_activity,
        "stable_fact_hashes": fact_hashes,
        "stable_facts": all(len(values) == 1 for values in fact_hashes.values()),
        "all_queries_source_graph": len(source_queries) == len(queries),
        "all_queries_single_backend": all(
            query.get("single_backend_provenance") is True for query in queries
        ),
        "all_memory_queries_hit_memory": all(
            query.get("source_graph", {}).get("effective_cache_tier") == "memory"
            for query in memory_source_queries
        )
        and len(memory_source_queries) == len(memory),
        "disk_cache_inactive": disk_activity == 0
        and all(
            query.get("source_graph", {}).get("cache_tier") != "disk"
            for query in source_queries
        ),
    }


def _validate_args(args: argparse.Namespace) -> None:
    args.compile_log = args.compile_log.expanduser().resolve()
    args.wave_path = args.wave_path.expanduser().resolve()
    # Do not resolve the final symlink: ``venv/bin/python`` commonly points at
    # the system binary, but launching through that symlink is what activates
    # the virtual environment's sys.prefix and site-packages.
    args.frontend_python = Path(
        os.path.abspath(os.fspath(args.frontend_python.expanduser()))
    )
    for label, path in (
        ("compile log", args.compile_log),
        ("waveform", args.wave_path),
        ("frontend Python", args.frontend_python),
    ):
        if not path.is_file():
            raise BenchmarkInputError(f"{label} is not a readable file: {path}")
    if not os.access(args.frontend_python, os.X_OK):
        raise BenchmarkInputError(
            f"frontend Python is not executable: {args.frontend_python}"
        )
    if args.repeats < 1 or args.warm_repeats < 1:
        raise BenchmarkInputError("repeat counts must be positive")
    if args.driver_max_depth < 1:
        raise BenchmarkInputError("driver max depth must be positive")
    if min(args.worker_timeout_seconds, args.process_timeout_seconds) <= 0:
        raise BenchmarkInputError("timeouts must be positive")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    runs = [_run_fresh_process(args) for _ in range(args.repeats)]
    aggregate = _aggregate_runs(runs)
    hierarchy_stats = [
        run.get("preparation", {}).get("hierarchy", {}).get("stats", {}) for run in runs
    ]
    hierarchy_stable = len({_sha256_json(stats) for stats in hierarchy_stats}) == 1
    structural_complete = all(
        run.get("preparation", {}).get("structural_scan", {}).get("coverage_status")
        == "complete"
        for run in runs
    )
    passed = (
        aggregate["all_queries_source_graph"]
        and aggregate["all_queries_single_backend"]
        and aggregate["all_memory_queries_hit_memory"]
        and aggregate["disk_cache_inactive"]
        and aggregate["stable_facts"]
        and hierarchy_stable
        and structural_complete
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {"root": str(ROOT), "head": _git_head(ROOT)},
        "workload": {
            "compile_log": str(args.compile_log),
            "wave_path": str(args.wave_path),
            "simulator": args.simulator,
            "top_hint": args.top_hint,
            "driver_signal": args.driver_signal,
            "load_signal": args.load_signal,
            "from_signal": args.from_signal,
            "to_signal": args.to_signal,
        },
        "measurement_policy": {
            "fresh_process_per_sample": True,
            "fresh_process_count": args.repeats,
            "warm_driver_repeats_per_process": args.warm_repeats,
            "hierarchy_and_structural_scan_overlap": True,
            "production_backend_order_preserved": True,
            "disk_cache_enabled": False,
            "telemetry_enabled": False,
            "auto_kdb_build_enabled": False,
            "full_design_enumeration": False,
        },
        "frontend": {
            "python": str(args.frontend_python),
            "version": "11.0.0",
            "worker_timeout_seconds": args.worker_timeout_seconds,
        },
        "runs": runs,
        "aggregate": aggregate,
        "assessment": {
            "decision": (
                "p3_source_graph_soc_baseline_recorded"
                if passed
                else "p3_source_graph_soc_baseline_not_met"
            ),
            "passed": passed,
            "hierarchy_stats_stable": hierarchy_stable,
            "structural_scan_complete": structural_complete,
            "source_graph_coverage_claim": "preserved_from_each_query_receipt",
            "performance_improvement_claimed": False,
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument("--wave-path", type=Path, required=True)
    parser.add_argument(
        "--simulator", choices=("auto", "vcs", "xcelium"), default="auto"
    )
    parser.add_argument("--top-hint")
    parser.add_argument("--driver-signal", required=True)
    parser.add_argument("--load-signal", required=True)
    parser.add_argument("--from-signal", required=True)
    parser.add_argument("--to-signal", required=True)
    parser.add_argument("--driver-max-depth", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warm-repeats", type=int, default=3)
    parser.add_argument("--frontend-python", type=Path, default=DEFAULT_FRONTEND_PYTHON)
    parser.add_argument("--worker-timeout-seconds", type=float, default=240.0)
    parser.add_argument("--process-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-run", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        _validate_args(args)
        if args.child_run:
            return _child_main(args)
        result = run_benchmark(args)
    except BenchmarkInputError as exc:
        print(f"benchmark input error: {exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        _write_json_atomic(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["assessment"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
