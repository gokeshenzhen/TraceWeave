#!/usr/bin/env python3
"""Run a privacy-safe Source Graph disk-cache soak on an arbitrary SoC.

The workload is supplied as an external JSON query list so this harness does
not encode a DVSim/FuseSoC/Bazel layout or project-specific hierarchy.  Each
sample launches a fresh TraceWeave process, anchors a telemetry session with
``get_sim_paths``, recovers hierarchy while scanning structural risks, and
issues the public driver/load/path calls through ``server.call_tool``.  The
disk cache and local telemetry are opt-in for the isolated cache root only.

Raw telemetry and cache artifacts remain under that private root.  The JSON
written by this script contains aggregate numeric/status receipts only: no
cache/source/wave path, signal, scope, artifact fingerprint, or exception text.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_source_graph_soc import (  # noqa: E402
    DEFAULT_FRONTEND_PYTHON,
    _distribution,
    _git_head,
    _write_json_atomic,
)
from src.usage_telemetry import aggregate, load_records  # noqa: E402


SCHEMA_VERSION = "1.0"
SOAK_NAME = "source_graph_soc_p4_operational_soak"
QUERY_TO_OPERATION = {
    "explain_signal_driver": "driver",
    "find_signal_loads": "loads",
    "trace_signal_path": "path",
}
REQUIRED_QUERY_KEYS = {
    "explain_signal_driver": {"signal_path"},
    "find_signal_loads": {"signal_path"},
    "trace_signal_path": {"from_signal", "to_signal"},
}
INJECTED_QUERY_KEYS = {"compile_log", "wave_path", "simulator", "top_hint"}
FORBIDDEN_EVIDENCE_KEYS = {
    "cache_root",
    "compile_log",
    "wave_path",
    "sim_log",
    "verif_root",
    "signal_path",
    "from_signal",
    "to_signal",
    "scope",
    "fingerprint",
    "digest",
    "exception",
    "message",
}


class SoakInputError(ValueError):
    """Raised when a soak would be ambiguous or non-reproducible."""


def _absolute_without_resolving_final_symlink(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _load_query_spec(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoakInputError("query spec is unreadable or invalid JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise SoakInputError("query spec must be a non-empty JSON list")
    queries: list[dict[str, Any]] = []
    operations: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise SoakInputError(f"query {index} must be an object")
        tool = item.get("tool")
        arguments = item.get("arguments")
        if tool not in QUERY_TO_OPERATION:
            raise SoakInputError(f"query {index} uses an unsupported tool")
        if not isinstance(arguments, Mapping):
            raise SoakInputError(f"query {index} arguments must be an object")
        missing = REQUIRED_QUERY_KEYS[tool] - arguments.keys()
        if missing:
            raise SoakInputError(f"query {index} lacks required arguments")
        if INJECTED_QUERY_KEYS & arguments.keys():
            raise SoakInputError(f"query {index} overrides harness-owned arguments")
        queries.append({"tool": tool, "arguments": dict(arguments)})
        operations.add(QUERY_TO_OPERATION[tool])
    if operations != {"driver", "loads", "path"}:
        raise SoakInputError("query spec must cover driver, loads, and path")
    return queries


def _parse_call_tool_result(content: Sequence[Any]) -> dict[str, Any]:
    for item in content:
        if getattr(item, "type", None) == "text":
            try:
                payload = json.loads(item.text)
            except (AttributeError, json.JSONDecodeError) as exc:
                raise SoakInputError("public tool returned invalid JSON") from exc
            if isinstance(payload, dict):
                return payload
    raise SoakInputError("public tool returned no JSON text payload")


async def _call_public(server_module: Any, tool: str, arguments: dict) -> dict:
    return _parse_call_tool_result(await server_module.call_tool(tool, arguments))


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
    return {
        "wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "coverage_status": result.get("coverage_status"),
        "eligible_file_count": int(result.get("eligible_file_count") or 0),
        "files_scanned": int(result.get("files_scanned") or 0),
        "total_risks": int(result.get("total_risks") or 0),
    }


def _compact_query(
    index: int,
    tool: str,
    payload: Mapping[str, Any],
    wall_ms: float,
) -> dict[str, Any]:
    backend = payload.get("backend_status") or {}
    source = backend.get("source_graph") or {}
    metrics = source.get("metrics") or {}
    operation = QUERY_TO_OPERATION[tool]
    positive = (
        payload.get("driver_status") == "resolved"
        if operation == "driver"
        else bool(payload.get("loads"))
        if operation == "loads"
        else payload.get("found") is True
    )
    return {
        "ordinal": index,
        "tool": tool,
        "operation": operation,
        "wall_ms": round(wall_ms, 3),
        "actual_backend": backend.get("actual_backend"),
        "single_backend_provenance": backend.get("single_backend_provenance"),
        "kdb_validation_status": backend.get("kdb_validation_status"),
        "positive_fact": positive,
        "prepare_status": source.get("prepare_status"),
        "cache_tier": source.get("cache_tier"),
        "disk_validation_outcome": source.get("disk_validation_outcome"),
        "artifact_reuse": source.get("artifact_reuse"),
        "coverage_status": source.get("coverage_status"),
        "query_status": source.get("query_status"),
        "query_match_count": int(source.get("query_match_count") or 0),
        "metrics": {
            key: metrics.get(key)
            for key in (
                "adapter_wall_ms",
                "prepare_total_wall_ms",
                "build_wall_ms",
                "load_wall_ms",
                "query_wall_ms",
                "actual_build_count",
                "frontend_launch_count",
                "rss_peak_kib",
                "ir_bytes",
                "cache_bytes",
                "cache_entry_count",
                "cache_peak_entry_count",
                "cache_peak_bytes",
                "cache_eviction_count",
                "disk_lookup_wall_ms",
                "disk_read_wall_ms",
                "disk_validate_wall_ms",
                "disk_publish_wall_ms",
                "disk_write_wall_ms",
                "disk_hit_count",
                "disk_miss_count",
                "disk_corrupt_count",
                "disk_build_skip_count",
                "disk_bytes_read",
                "disk_bytes_written",
                "disk_entry_count",
                "disk_bytes",
                "disk_eviction_count",
            )
        },
    }


async def _run_child_async(args: argparse.Namespace) -> dict[str, Any]:
    # The child environment is fixed by the parent before this import.
    import server  # noqa: PLC0415
    from src import usage_telemetry  # noqa: PLC0415

    queries = _load_query_spec(args.query_spec)
    sim_paths = await _call_public(
        server,
        "get_sim_paths",
        {
            "verif_root": str(args.verif_root),
            "compile_log": str(args.compile_log),
            "sim_log": str(args.sim_log),
            "wave_file": str(args.wave_path),
        },
    )
    if sim_paths.get("simulator") not in {args.simulator, None}:
        raise SoakInputError("discovered simulator does not match requested simulator")

    scan_task = asyncio.create_task(
        asyncio.to_thread(
            _run_structural_scan,
            server,
            str(args.compile_log),
            args.simulator,
        )
    )
    await asyncio.sleep(0)
    hierarchy_started = time.perf_counter()
    hierarchy = await _call_public(
        server,
        "build_tb_hierarchy",
        {"compile_log": str(args.compile_log), "simulator": args.simulator},
    )
    hierarchy_wall_ms = (time.perf_counter() - hierarchy_started) * 1000.0
    structural = await scan_task

    common = {
        "compile_log": str(args.compile_log),
        "simulator": args.simulator,
        "top_hint": args.top_hint,
    }
    compact_queries: list[dict[str, Any]] = []
    for index, query in enumerate(queries):
        tool = query["tool"]
        arguments = {**common, **query["arguments"]}
        if tool == "explain_signal_driver":
            arguments["wave_path"] = str(args.wave_path)
        started = time.perf_counter()
        payload = await _call_public(server, tool, arguments)
        compact_queries.append(
            _compact_query(
                index,
                tool,
                payload,
                (time.perf_counter() - started) * 1000.0,
            )
        )

    return {
        "session_anchored": bool(usage_telemetry.current_session_id()),
        "discovery_mode": sim_paths.get("discovery_mode"),
        "hierarchy_wall_ms": round(hierarchy_wall_ms, 3),
        "hierarchy_stats": hierarchy.get("stats", {}),
        "structural_scan": structural,
        "queries": compact_queries,
    }


def _child_main(args: argparse.Namespace) -> int:
    print(json.dumps(asyncio.run(_run_child_async(args)), sort_keys=True))
    return 0


def _child_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-run",
        "--verif-root",
        str(args.verif_root),
        "--compile-log",
        str(args.compile_log),
        "--sim-log",
        str(args.sim_log),
        "--wave-path",
        str(args.wave_path),
        "--simulator",
        args.simulator,
        "--top-hint",
        args.top_hint,
        "--query-spec",
        str(args.query_spec),
        "--cache-root",
        str(args.cache_root),
        "--frontend-python",
        str(args.frontend_python),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--disk-max-entries",
        str(args.disk_max_entries),
        "--disk-max-bytes",
        str(args.disk_max_bytes),
    ]
    return command


def _spawn_session(args: argparse.Namespace) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "TRACEWEAVE_CACHE_DIR": str(args.cache_root),
            "TRACEWEAVE_SOURCE_GRAPH": "1",
            "TRACEWEAVE_SOURCE_GRAPH_PYTHON": str(args.frontend_python),
            "TRACEWEAVE_SOURCE_GRAPH_FRONTEND_VERSION": "11.0.0",
            "TRACEWEAVE_SOURCE_GRAPH_TIMEOUT": str(args.worker_timeout_seconds),
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE": "1",
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES": str(
                args.disk_max_entries
            ),
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES": str(args.disk_max_bytes),
            "TRACEWEAVE_TELEMETRY": "1",
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
        raise SoakInputError(
            f"fresh soak process failed ({completed.returncode}): {detail}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SoakInputError("fresh soak process returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SoakInputError("fresh soak process returned a non-object")
    return payload


def _sum_metric(queries: Sequence[Mapping[str, Any]], field: str) -> int:
    return sum(int(query.get("metrics", {}).get(field) or 0) for query in queries)


def _session_summary(index: int, run: Mapping[str, Any]) -> dict[str, Any]:
    queries = list(run.get("queries", []))
    hit_count = _sum_metric(queries, "disk_hit_count")
    miss_count = _sum_metric(queries, "disk_miss_count")
    return {
        "ordinal": index,
        "session_anchored": run.get("session_anchored") is True,
        "query_count": len(queries),
        "source_graph_query_count": sum(
            query.get("actual_backend") == "source_graph" for query in queries
        ),
        "positive_fact_count": sum(
            query.get("positive_fact") is True for query in queries
        ),
        "cache_tier_counts": {
            tier: sum(query.get("cache_tier") == tier for query in queries)
            for tier in ("build", "disk", "memory")
        },
        "disk_lookup_outcome_count": hit_count + miss_count,
        "disk_hit_count": hit_count,
        "disk_miss_count": miss_count,
        "disk_corrupt_count": _sum_metric(queries, "disk_corrupt_count"),
        "disk_build_skip_count": _sum_metric(queries, "disk_build_skip_count"),
        "actual_build_count": _sum_metric(queries, "actual_build_count"),
        "frontend_launch_count": _sum_metric(queries, "frontend_launch_count"),
        "disk_eviction_count": _sum_metric(queries, "disk_eviction_count"),
        "cache_entry_count_max": max(
            (
                int(query.get("metrics", {}).get("disk_entry_count") or 0)
                for query in queries
            ),
            default=0,
        ),
        "cache_bytes_max": max(
            (int(query.get("metrics", {}).get("disk_bytes") or 0) for query in queries),
            default=0,
        ),
        "hierarchy_stats": run.get("hierarchy_stats", {}),
        "structural_scan": run.get("structural_scan", {}),
    }


def _telemetry_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    source = report.get("source_graph") or {}
    return {
        "total_records": int(report.get("total_records") or 0),
        "total_sessions": int(report.get("total_sessions") or 0),
        "calls_with_metrics": int(source.get("calls_with_metrics") or 0),
        "sessions_with_metrics": int(source.get("sessions_with_metrics") or 0),
        "cache_tiers": source.get("cache_tiers", {}),
        "disk": source.get("disk", {}),
        "execution": source.get("execution", {}),
        "validation_outcomes": source.get("validation_outcomes", {}),
        "by_tool": source.get("by_tool", {}),
    }


def _contains_forbidden_evidence_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            # Telemetry's by-tool aggregate uses the fixed public MCP tool name
            # as a map key.  ``trace_signal_path`` is an operation label, not a
            # raw signal identity; its child still receives the full audit.
            public_tool_label = normalized in QUERY_TO_OPERATION
            if not public_tool_label and any(
                token in normalized for token in FORBIDDEN_EVIDENCE_KEYS
            ):
                return True
            if _contains_forbidden_evidence_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_evidence_key(item) for item in value)
    return False


def _build_result(
    args: argparse.Namespace,
    runs: Sequence[Mapping[str, Any]],
    telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    queries = [query for run in runs for query in run.get("queries", [])]
    sessions = [_session_summary(index, run) for index, run in enumerate(runs)]
    hits = _sum_metric(queries, "disk_hit_count")
    misses = _sum_metric(queries, "disk_miss_count")
    corrupt = _sum_metric(queries, "disk_corrupt_count")
    operations = sorted({query.get("operation") for query in queries})
    verified_hits = [query for query in queries if query.get("cache_tier") == "disk"]
    memory_hits = [query for query in queries if query.get("cache_tier") == "memory"]
    telemetry_summary = _telemetry_summary(telemetry)
    hierarchy_stable = (
        len(
            {json.dumps(run.get("hierarchy_stats", {}), sort_keys=True) for run in runs}
        )
        == 1
    )
    structural_complete = all(
        run.get("structural_scan", {}).get("coverage_status") == "complete"
        for run in runs
    )
    aggregate_result = {
        "fresh_process_count": len(runs),
        "query_count": len(queries),
        "source_graph_query_count": sum(
            query.get("actual_backend") == "source_graph" for query in queries
        ),
        "positive_fact_count": sum(
            query.get("positive_fact") is True for query in queries
        ),
        "operations_covered": operations,
        "coverage_status_counts": {
            status: sum(query.get("coverage_status") == status for query in queries)
            for status in ("complete", "partial", "inconclusive")
        },
        "query_status_counts": {
            status: sum(query.get("query_status") == status for query in queries)
            for status in ("found", "not_connected", "inconclusive")
        },
        "single_backend_provenance_count": sum(
            query.get("single_backend_provenance") is True for query in queries
        ),
        "kdb_validation_status_counts": {
            status: sum(
                query.get("kdb_validation_status") == status for query in queries
            )
            for status in ("usable", "elaboration_error", "unavailable")
        },
        "cache_tier_counts": {
            tier: sum(query.get("cache_tier") == tier for query in queries)
            for tier in ("build", "disk", "memory")
        },
        "disk_lookup_outcome_count": hits + misses,
        "disk_hit_count": hits,
        "disk_miss_count": misses,
        "disk_corrupt_count": corrupt,
        "disk_build_skip_count": _sum_metric(queries, "disk_build_skip_count"),
        "actual_build_count": _sum_metric(queries, "actual_build_count"),
        "frontend_launch_count": _sum_metric(queries, "frontend_launch_count"),
        "disk_eviction_count": _sum_metric(queries, "disk_eviction_count"),
        "query_wall_ms_by_tier": {
            tier: _distribution(
                [
                    query.get("wall_ms")
                    for query in queries
                    if query.get("cache_tier") == tier
                ]
            )
            for tier in ("build", "disk", "memory")
        },
        "disk_lookup_wall_ms": _distribution(
            [
                query.get("metrics", {}).get("disk_lookup_wall_ms")
                for query in queries
                if query.get("cache_tier") in {"build", "disk"}
            ]
        ),
        "disk_read_wall_ms": _distribution(
            [
                query.get("metrics", {}).get("disk_read_wall_ms")
                for query in verified_hits
            ]
        ),
        "hierarchy_stats_stable": hierarchy_stable,
        "structural_scan_complete": structural_complete,
        "verified_disk_hits_skip_build": all(
            int(query.get("metrics", {}).get("actual_build_count") or 0) == 0
            and int(query.get("metrics", {}).get("frontend_launch_count") or 0) == 0
            and int(query.get("metrics", {}).get("disk_build_skip_count") or 0) >= 1
            for query in verified_hits
        ),
        "memory_hits_skip_disk": all(
            int(query.get("metrics", {}).get("disk_hit_count") or 0) == 0
            and int(query.get("metrics", {}).get("disk_miss_count") or 0) == 0
            and float(query.get("metrics", {}).get("disk_lookup_wall_ms") or 0.0) == 0.0
            for query in memory_hits
        ),
    }
    build_median = aggregate_result["query_wall_ms_by_tier"]["build"]["median"]
    disk_median = aggregate_result["query_wall_ms_by_tier"]["disk"]["median"]
    aggregate_result["disk_vs_build_median_reduction_percent"] = (
        round((build_median - disk_median) / build_median * 100.0, 3)
        if build_median and disk_median is not None
        else None
    )
    telemetry_matches = (
        telemetry_summary["calls_with_metrics"] == len(queries)
        and telemetry_summary["sessions_with_metrics"] >= args.sessions
        and int(telemetry_summary.get("disk", {}).get("hit_count") or 0) == hits
        and int(telemetry_summary.get("disk", {}).get("miss_count") or 0) == misses
        and int(telemetry_summary.get("disk", {}).get("corrupt_count") or 0) == corrupt
    )
    passed = (
        len(runs) >= 5
        and hits + misses >= 20
        and hits > 0
        and misses > 0
        and corrupt == 0
        and operations == ["driver", "loads", "path"]
        and aggregate_result["source_graph_query_count"] == len(queries)
        and aggregate_result["positive_fact_count"] == len(queries)
        and all(query.get("single_backend_provenance") is True for query in queries)
        and aggregate_result["verified_disk_hits_skip_build"]
        and aggregate_result["memory_hits_skip_disk"]
        and aggregate_result["disk_eviction_count"] == 0
        and hierarchy_stable
        and structural_complete
        and telemetry_matches
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "soak": SOAK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_head": _git_head(ROOT),
        "measurement_policy": {
            "fresh_process_per_session": True,
            "requested_sessions": args.sessions,
            "production_backend_order_preserved": True,
            "disk_cache_opt_in": True,
            "telemetry_opt_in": True,
            "disk_cache_default_changed": False,
            "persistent_worker": False,
            "startup_cache_scan": False,
            "raw_telemetry_committed": False,
            "raw_cache_artifacts_committed": False,
            "disk_max_entries": args.disk_max_entries,
            "disk_max_bytes": args.disk_max_bytes,
        },
        "session_aggregates": sessions,
        "aggregate": aggregate_result,
        "telemetry": telemetry_summary,
        "assessment": {
            "decision": (
                "p4_operational_soak_passed_keep_opt_in"
                if passed
                else "p4_operational_soak_not_met"
            ),
            "passed": passed,
            "telemetry_cross_check_passed": telemetry_matches,
            "privacy_safe_aggregate_only": True,
            "default_on_authorized": False,
            "persistent_worker_authorized": False,
        },
    }
    if _contains_forbidden_evidence_key(result):
        raise SoakInputError("aggregate evidence contains a forbidden identity key")
    return result


def _validate_args(args: argparse.Namespace) -> None:
    for attribute in (
        "verif_root",
        "compile_log",
        "sim_log",
        "wave_path",
        "query_spec",
    ):
        value = getattr(args, attribute).expanduser().resolve()
        setattr(args, attribute, value)
    args.frontend_python = _absolute_without_resolving_final_symlink(
        args.frontend_python
    )
    args.cache_root = _absolute_without_resolving_final_symlink(args.cache_root)
    if not args.verif_root.is_dir():
        raise SoakInputError("verification root is not a directory")
    for label, path in (
        ("compile log", args.compile_log),
        ("simulation log", args.sim_log),
        ("waveform", args.wave_path),
        ("query spec", args.query_spec),
        ("frontend Python", args.frontend_python),
    ):
        if not path.is_file():
            raise SoakInputError(f"{label} is not a readable file")
    if not os.access(args.frontend_python, os.X_OK):
        raise SoakInputError("frontend Python is not executable")
    if args.sessions < 1:
        raise SoakInputError("sessions must be positive")
    if min(args.worker_timeout_seconds, args.process_timeout_seconds) <= 0:
        raise SoakInputError("timeouts must be positive")
    if args.disk_max_entries < 1 or args.disk_max_bytes < 1:
        raise SoakInputError("disk cache bounds must be positive")
    if not args.cache_root.is_absolute() or ".." in args.cache_root.parts:
        raise SoakInputError("cache root must be an absolute normalized path")
    _load_query_spec(args.query_spec)


def _prepare_cache_root(args: argparse.Namespace) -> None:
    args.cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        args.cache_root.chmod(0o700)
    except OSError:
        pass
    if not args.resume and any(args.cache_root.iterdir()):
        raise SoakInputError("cache root is not empty; use a new root or --resume")


def run_soak(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    _prepare_cache_root(args)
    runs: list[dict[str, Any]] = []
    for index in range(args.sessions):
        print(
            f"source-graph soak session {index + 1}/{args.sessions}",
            file=sys.stderr,
            flush=True,
        )
        runs.append(_spawn_session(args))
    telemetry_path = args.cache_root / "telemetry/usage.jsonl"
    report = aggregate(load_records(str(telemetry_path)))
    return _build_result(args, runs, report)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verif-root", type=Path, required=True)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument("--sim-log", type=Path, required=True)
    parser.add_argument("--wave-path", type=Path, required=True)
    parser.add_argument("--simulator", choices=("vcs", "xcelium"), required=True)
    parser.add_argument("--top-hint", required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--frontend-python", type=Path, default=DEFAULT_FRONTEND_PYTHON)
    parser.add_argument("--worker-timeout-seconds", type=float, default=240.0)
    parser.add_argument("--process-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--disk-max-entries", type=int, default=8)
    parser.add_argument("--disk-max-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-run", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        _validate_args(args)
        if args.child_run:
            return _child_main(args)
        result = run_soak(args)
    except SoakInputError as exc:
        print(f"soak input error: {exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        _write_json_atomic(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["assessment"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
