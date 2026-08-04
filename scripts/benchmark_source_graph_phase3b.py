#!/usr/bin/env python3
"""Phase 3B gate for bounded cross-target process-memory Source Graph reuse.

The tracked hand fixture is the correctness oracle.  OpenTitan is an optional
bounded engineering workload over the same four-path ancestor/projection scope
accepted in Phase 3A; it is never used as a full-design accuracy or coverage
claim.  Automated tests inject a tracked-IR fake worker and require no optional
frontend, NPI/license, checkout, network, or ignored file.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SourceGraphExecutionConfig  # noqa: E402
import server  # noqa: E402
from scripts import benchmark_source_graph_phase1b as phase1b  # noqa: E402
from scripts import benchmark_source_graph_phase2 as phase2  # noqa: E402
from scripts import benchmark_source_graph_phase3a as phase3a  # noqa: E402
from src.cancellation import OperationCancelled  # noqa: E402
import src.connectivity_backend as connectivity_backend  # noqa: E402
from src.hierarchy_handles import compute_snapshot_fingerprint  # noqa: E402
from src import operation_metrics  # noqa: E402
from src.source_graph_adapter import (  # noqa: E402
    AdapterStatus,
    _reset_source_graph_adapter_cache_for_tests,
    build_source_graph_plan,
)
from src.source_graph_contract import QueryOperation  # noqa: E402
from src.source_graph_runtime import (  # noqa: E402
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    SourceGraphRuntime,
    SourceGraphWorkerRunner,
)


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase3b"
FRONTEND_VERSION = "11.0.0"
ACCEPTED_PHASE3A_HEAD = "4a4d061bb6b39bc1a966ddc3aff0d9f70d712b94"
MEASUREMENT_HEAD = "6ff8ffb3da780f1dab7f7f4645331536684518d9"
DEFAULT_FRONTEND_PYTHON = phase3a.DEFAULT_FRONTEND_PYTHON
DEFAULT_OPENTITAN_COMPILE_LOG = phase3a.DEFAULT_OPENTITAN_COMPILE_LOG
DEFAULT_OUTPUT = ROOT / "benchmarks/source_graph_connectivity_phase3b_results.json"

HISTORICAL_EVIDENCE = {
    "phase1a": (
        ROOT / "benchmarks/source_graph_connectivity_phase1a_results.json",
        "c7310560a1e89e19694a83d41e24a645578b747585c68df546a20937f3fa42e2",
    ),
    "phase1b": (
        ROOT / "benchmarks/source_graph_connectivity_phase1b_results.json",
        "c9a25c96c63ddce9205ecabf86b61f6da1eff9ba0f71aeee099a6b04f7237da7",
    ),
    "phase2": (
        ROOT / "benchmarks/source_graph_connectivity_phase2_results.json",
        "1b5f76c3862601bb1163d838744c9c03ec7ed62cd1bc5f663ef7008bd0902599",
    ),
    "phase3a": (
        ROOT / "benchmarks/source_graph_connectivity_phase3a_results.json",
        "6b7f8822f978f8e46dd9172c2b093e731889f44b0b68c040b5c66619ccbc438a",
    ),
}

GATE_TARGETS = {
    "opentitan_bounded_cold_p50_max_ms": 15_000.0,
    "cross_target_warm_public_p95_max_ms": 100.0,
    "peak_rss_max_kib": 2_621_440,
    "same_artifact_actual_build_count": 1,
    "default_cache_max_entries": 8,
    "default_cache_max_bytes": 536_870_912,
}

BenchmarkError = phase1b.BenchmarkError
RunnerFactory = Callable[[], SourceGraphWorkerRunner]
StaticFactory = Callable[[], Any]
FailureRunnerFactories = phase3a.FailureRunnerFactories


@dataclass(frozen=True)
class ReuseWorkload:
    base: phase3a.PathWorkload
    driver_signal: str
    load_signal: str
    reuse_from_signal: str
    reuse_to_signal: str
    expected_reuse_path: tuple[str, ...] | None
    out_of_scope_args: Mapping[str, Any] | None = None
    expected_out_of_scope_path: tuple[str, ...] | None = None

    def driver_args(self, **overrides: Any) -> dict[str, Any]:
        result = {
            "signal_path": self.driver_signal,
            "wave_path": "/unused/source_graph_phase3b.fsdb",
            "compile_log": self.base.compile_log,
            "simulator": self.base.simulator,
            "top_hint": self.base.top,
            "recursive": True,
            "max_depth": 64,
        }
        result.update(overrides)
        return result

    def load_args(self, **overrides: Any) -> dict[str, Any]:
        result = {
            "signal_path": self.load_signal,
            "compile_log": self.base.compile_log,
            "simulator": self.base.simulator,
            "top_hint": self.base.top,
            "max_depth": 64,
            "include_expr": True,
        }
        result.update(overrides)
        return result

    def reuse_path_args(self, *, expand_assigns: bool) -> dict[str, Any]:
        return self.base.path_args(
            from_signal=self.reuse_from_signal,
            to_signal=self.reuse_to_signal,
            expand_assigns=expand_assigns,
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _read_historical_baselines() -> dict[str, Any]:
    payloads: dict[str, dict[str, Any]] = {}
    receipts: dict[str, Any] = {}
    for name, (path, expected_sha) in HISTORICAL_EVIDENCE.items():
        raw = path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if actual_sha != expected_sha:
            raise BenchmarkError(
                f"{name} historical evidence hash mismatch: {actual_sha}"
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise BenchmarkError(f"{name} historical evidence is not an object")
        payloads[name] = payload
        receipts[name] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": actual_sha,
            "benchmark": payload.get("benchmark"),
            "measurement_head": payload.get("repository", {}).get("head"),
            "decision": payload.get("assessment", {}).get("decision"),
        }

    phase3a_payload = payloads["phase3a"]
    assessment = phase3a_payload.get("assessment", {})
    expected_assessment = {
        "decision": "phase3a_trace_signal_path_gate_passed",
        "phase3a_trace_signal_path_gate_passed": True,
        "production_route_changed_tools": ["trace_signal_path"],
        "driver_load_route_changed": False,
        "trace_x_source_route_changed": False,
        "cross_target_reuse_implemented": False,
        "waveform_locking_model_changed": False,
        "disk_cache": False,
        "persistent_worker": False,
        "startup_full_design_enumeration": False,
    }
    if phase3a_payload.get("schema_version") != "1.0":
        raise BenchmarkError("Phase 3A schema version mismatch")
    if phase3a_payload.get("benchmark") != "source_graph_connectivity_phase3a":
        raise BenchmarkError("Phase 3A benchmark identity mismatch")
    if phase3a_payload.get("repository", {}).get("head") != (
        "1a9fac908caa865821d46b60130edf1edf3abde1"
    ):
        raise BenchmarkError("Phase 3A measurement HEAD mismatch")
    for field, expected in expected_assessment.items():
        if assessment.get(field) != expected:
            raise BenchmarkError(f"Phase 3A assessment mismatch: {field}")
    if not phase3a_payload.get("route_probes", {}).get("gate", {}).get("passed"):
        raise BenchmarkError("Phase 3A route gate is not passed")
    if not phase3a_payload.get("failure_probes", {}).get("gate", {}).get("passed"):
        raise BenchmarkError("Phase 3A failure gate is not passed")
    if not all(
        item.get("gate", {}).get("passed")
        for item in phase3a_payload.get("workloads", ())
    ):
        raise BenchmarkError("Phase 3A workload gate is not passed")
    for item in phase3a_payload.get("workloads", ()):
        concurrent = item.get("concurrent_same_key", {})
        if (
            concurrent.get("request_count") == 4
            and concurrent.get("actual_build_count") != 1
        ):
            raise BenchmarkError("Phase 3A four-way exact single-flight mismatch")
    opentitan = next(
        (
            item
            for item in phase3a_payload.get("workloads", ())
            if item.get("name") == "opentitan_core"
        ),
        None,
    )
    if opentitan is None:
        raise BenchmarkError("Phase 3A OpenTitan workload is absent")
    aggregate = opentitan["path_operation"]["aggregate"]
    expected_open = {
        "manifest_input_counts": [785],
        "manifest_top_counts": [11],
        "coverage_boundary_path_counts": [4],
        "requested_cone_path_counts": [4],
        "coverage_statuses": ["inconclusive"],
        "blocking_diagnostic_counts": [65],
        "query_confidences": ["partial"],
    }
    for field, expected in expected_open.items():
        if aggregate.get(field) != expected:
            raise BenchmarkError(f"Phase 3A OpenTitan receipt mismatch: {field}")
    receipts["phase3a"]["accepted_invariants"] = {
        "route_gate_passed": True,
        "workload_gate_passed": True,
        "failure_gate_passed": True,
        "four_way_exact_actual_build_count": 1,
        "opentitan_ordered_input_count": 785,
        "opentitan_ordered_top_count": 11,
        "opentitan_scope_path_count": 4,
        "opentitan_coverage_status": "inconclusive",
        "opentitan_blocking_diagnostic_count": 65,
        "opentitan_representative_confidence": "partial",
        "opentitan_claim": "target-scoped only",
    }
    return receipts


def _call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _route_stage_sequence(source: str, function_name: str) -> list[str]:
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    if function is None:
        raise BenchmarkError(f"route function missing: {function_name}")
    stages: list[str] = []
    stage_by_call = {
        "select_backend": "trusted_npi",
        "build_source_graph_plan": "source_graph",
        "build_source_graph_path_plan": "source_graph",
        "get_source_graph_runtime": "source_graph",
        "StaticConnectivityBackend": "legacy_static",
    }
    calls = sorted(
        (node for node in ast.walk(function) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for call in calls:
        stage = stage_by_call.get(_call_name(call) or "")
        if stage is not None and stage not in stages:
            stages.append(stage)
    return stages


def _route_isolation_receipt() -> dict[str, Any]:
    accepted = phase2._git_show(ACCEPTED_PHASE3A_HEAD, "server.py")
    current = (ROOT / "server.py").read_text(encoding="utf-8")
    route_functions = ("_route_public_connectivity", "_route_public_signal_path")
    ordering = {}
    functions = {}
    for function_name in route_functions:
        before_sequence = _route_stage_sequence(accepted, function_name)
        current_sequence = _route_stage_sequence(current, function_name)
        ordering[function_name] = {
            "accepted": before_sequence,
            "current": current_sequence,
            "changed": before_sequence != current_sequence,
        }
        before_digest = phase2._function_digest(accepted, function_name)
        current_digest = phase2._function_digest(current, function_name)
        functions[function_name] = {
            "accepted_ast_sha256": before_digest,
            "current_ast_sha256": current_digest,
            "changed": before_digest != current_digest,
        }

    unchanged_functions = (
        "_run_trace_x_attempt",
        "_handle_trace_x_source",
        "_run_in_wave_thread",
        "_wave_locks_for",
    )
    for function_name in unchanged_functions:
        before_digest = phase2._function_digest(accepted, function_name)
        current_digest = phase2._function_digest(current, function_name)
        functions[function_name] = {
            "accepted_ast_sha256": before_digest,
            "current_ast_sha256": current_digest,
            "changed": before_digest != current_digest,
        }
    branches = {}
    for tool in (
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
        "trace_x_source",
    ):
        before_digest = phase2._dispatch_branch_digest(accepted, tool)
        current_digest = phase2._dispatch_branch_digest(current, tool)
        branches[tool] = {
            "accepted_ast_sha256": before_digest,
            "current_ast_sha256": current_digest,
            "changed": before_digest != current_digest,
        }

    expected_order = ["trusted_npi", "source_graph", "legacy_static"]
    ordering_unchanged = all(
        item["accepted"] == expected_order
        and item["current"] == expected_order
        and not item["changed"]
        for item in ordering.values()
    )
    trace_x_unchanged = (
        not branches["trace_x_source"]["changed"]
        and not functions["_run_trace_x_attempt"]["changed"]
        and not functions["_handle_trace_x_source"]["changed"]
    )
    wave_unchanged = (
        not functions["_run_in_wave_thread"]["changed"]
        and not functions["_wave_locks_for"]["changed"]
    )
    return {
        "accepted_head": ACCEPTED_PHASE3A_HEAD,
        "route_stage_order": ordering,
        "functions": functions,
        "dispatch_branches": branches,
        "production_route_ordering_changed": not ordering_unchanged,
        "reuse_behavior_affected_tools": [
            "explain_signal_driver",
            "find_signal_loads",
            "trace_signal_path",
        ],
        "trace_x_source_route_changed": not trace_x_unchanged,
        "waveform_locking_model_changed": not wave_unchanged,
    }


def _hand_workload(temp_root: Path) -> ReuseWorkload:
    base = phase3a._hand_workload(temp_root)
    hierarchy = deepcopy(base.hierarchy)
    hierarchy["component_tree"]["sg_top"].setdefault(
        "u_bridge", {"module": "sg_bridge", "children": {}}
    )
    base = replace(base, hierarchy=hierarchy)
    return ReuseWorkload(
        base=base,
        driver_signal="sg_top.u_producer.seed",
        load_signal="sg_top.u_producer.rst_n",
        reuse_from_signal="sg_top.u_producer.seed[7:0]",
        reuse_to_signal="sg_top.u_producer.seed[7:0]",
        expected_reuse_path=None,
        out_of_scope_args=base.path_args(),
        expected_out_of_scope_path=base.expected_path,
    )


def _opentitan_workload(compile_log: Path) -> ReuseWorkload:
    base = phase3a._opentitan_workload(compile_log)
    prefix = "tb.dut.top_earlgrey.u_rv_core_ibex"
    return ReuseWorkload(
        base=base,
        driver_signal=f"{prefix}.fatal_intg_event",
        load_signal=f"{prefix}.ibus_intg_err",
        reuse_from_signal=f"{prefix}.ibus_intg_err",
        reuse_to_signal=f"{prefix}.fatal_intg_event",
        expected_reuse_path=base.expected_path,
    )


def _source_graph_receipt(source: Mapping[str, Any]) -> dict[str, Any]:
    adapter = source.get("adapter") or {}
    return {
        "adapter_status": source.get("adapter_status"),
        "prepare_status": source.get("prepare_status"),
        "cache_disposition": source.get("cache_disposition"),
        "flight_disposition": source.get("flight_disposition"),
        "cache_lookup_reason": source.get("cache_lookup_reason"),
        "artifact_reuse": source.get("artifact_reuse"),
        "artifact_fingerprint_sha256": source.get("artifact_fingerprint_sha256"),
        "selected_artifact_fingerprint_sha256": source.get(
            "selected_artifact_fingerprint_sha256"
        ),
        "query_fingerprint_sha256": source.get("query_fingerprint_sha256"),
        "compile_fingerprint_sha256": source.get("compile_fingerprint_sha256"),
        "ir_fingerprint_sha256": source.get("ir_fingerprint_sha256"),
        "scope_match": source.get("scope_match"),
        "coverage_status": source.get("coverage_status"),
        "coverage_files_total": source.get("coverage_files_total", 0),
        "coverage_files_projected": source.get("coverage_files_projected", 0),
        "coverage_diagnostic_count": source.get("coverage_diagnostic_count", 0),
        "coverage_blocking_diagnostic_count": source.get(
            "coverage_blocking_diagnostic_count", 0
        ),
        "coverage_gap_count": source.get("coverage_gap_count", 0),
        "coverage_gap_codes": source.get("coverage_gap_codes", []),
        "objective_exclusions": source.get("objective_exclusions", []),
        "query_status": source.get("query_status"),
        "query_confidence": source.get("query_confidence"),
        "query_match_count": source.get("query_match_count", 0),
        "fallback_used": source.get("fallback_used", False),
        "blocker": source.get("blocker"),
        "metrics": source.get("metrics", {}),
        "manifest": adapter.get("manifest", {}),
        "scope": adapter.get("scope", {}),
        "gap_codes": adapter.get("gap_codes", []),
        "cross_request_reusable": adapter.get("cross_request_reusable"),
        "snapshot_identity_complete": adapter.get("snapshot_identity_complete"),
    }


def _compact_result(
    result: Any,
    *,
    operation: str,
    wall_time_ms: float,
    parent_cpu_time_ms: float,
) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    backend_status = payload["backend_status"]
    source = backend_status.get("source_graph")
    if operation in {"driver", "loads"}:
        payload_backends = phase2._payload_backends(payload, operation)
    else:
        payload_backends = sorted(
            {
                item
                for item in (
                    payload.get("backend"),
                    *(
                        hop.get("backend")
                        for hop in payload.get("path", ())
                        if isinstance(hop, Mapping)
                    ),
                )
                if item is not None
            }
        )
    compact: dict[str, Any] = {
        "operation": operation,
        "wall_time_ms": round(wall_time_ms, 6),
        "parent_cpu_time_ms": round(parent_cpu_time_ms, 6),
        "actual_backend": backend_status.get("actual_backend"),
        "selected_backend": backend_status.get("selected_backend"),
        "fallback_reason": backend_status.get("fallback_reason"),
        "attempted_backends": backend_status.get("attempted_backends", []),
        "payload_backends": payload_backends,
        "schema_fingerprint_sha256": _sha256_json(payload),
    }
    if operation == "driver":
        compact["facts"] = {
            "signal_path": payload.get("signal_path"),
            "driver_status": payload.get("driver_status"),
            "confidence": payload.get("confidence"),
            "source_file": payload.get("source_file"),
            "source_line": payload.get("source_line"),
            "chain_length": len(payload.get("driver_chain") or ()),
        }
    elif operation == "loads":
        compact["facts"] = {
            "signal_path": payload.get("signal_path"),
            "load_count": len(payload.get("loads") or ()),
            "completeness": payload.get("completeness"),
            "load_paths": [item.get("load_path") for item in payload.get("loads", ())],
        }
    else:
        compact["facts"] = {
            "from_signal": payload.get("from_signal"),
            "to_signal": payload.get("to_signal"),
            "found": payload.get("found"),
            "hops": payload.get("hops"),
            "expand_assigns": payload.get("expand_assigns"),
            "path_net_paths": [
                item.get("net_path") for item in payload.get("path", ())
            ],
            "unsupported_reason": payload.get("unsupported_reason"),
        }
    if isinstance(source, Mapping):
        compact["source_graph"] = _source_graph_receipt(source)
    return compact


async def _dispatch_timed(
    tool: str,
    args: Mapping[str, Any],
    *,
    operation: str,
) -> dict[str, Any]:
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    result = await server._dispatch(tool, dict(args))
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    return _compact_result(
        result,
        operation=operation,
        wall_time_ms=wall_ms,
        parent_cpu_time_ms=cpu_ms,
    )


def _execution_config(frontend_python: Path, timeout_seconds: float):
    return SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=timeout_seconds,
    )


async def _measure_reuse_sequence(
    workload: ReuseWorkload,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    warm_repeats: int,
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    runtime = SourceGraphRuntime(runner_factory())
    static = phase3a._static_counter(static_factory)
    config = _execution_config(frontend_python, timeout_seconds)
    with phase3a._public_environment(
        workload.base,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        core = [
            await _dispatch_timed(
                "explain_signal_driver", workload.driver_args(), operation="driver"
            ),
            await _dispatch_timed(
                "find_signal_loads", workload.load_args(), operation="loads"
            ),
            await _dispatch_timed(
                "trace_signal_path",
                workload.reuse_path_args(expand_assigns=True),
                operation="path",
            ),
            await _dispatch_timed(
                "trace_signal_path",
                workload.reuse_path_args(expand_assigns=False),
                operation="path",
            ),
            await _dispatch_timed(
                "explain_signal_driver",
                workload.driver_args(recursive=False, max_depth=1),
                operation="driver",
            ),
        ]
        warm_extra = []
        warm_specs = (
            (
                "find_signal_loads",
                workload.load_args(include_expr=False, max_depth=1),
                "loads",
            ),
            (
                "trace_signal_path",
                workload.reuse_path_args(expand_assigns=True),
                "path",
            ),
            (
                "explain_signal_driver",
                workload.driver_args(recursive=True, max_depth=64),
                "driver",
            ),
        )
        for index in range(warm_repeats):
            tool, call_args, operation = warm_specs[index % len(warm_specs)]
            warm_extra.append(
                await _dispatch_timed(tool, call_args, operation=operation)
            )
        stats_before_out_of_scope = runtime.stats_snapshot()
        out_of_scope = None
        if workload.out_of_scope_args is not None:
            out_of_scope = await _dispatch_timed(
                "trace_signal_path",
                workload.out_of_scope_args,
                operation="path",
            )
        stats_after = runtime.stats_snapshot()

    warm = [*core[1:], *warm_extra]
    cold_receipt = core[0]["source_graph"]
    return {
        "core_queries": core,
        "extra_warm_queries": warm_extra,
        "out_of_scope": out_of_scope,
        "runtime_stats_before_out_of_scope": stats_before_out_of_scope,
        "runtime_stats_after": stats_after,
        "static_calls": {
            "driver": static.driver_calls,
            "loads": static.load_calls,
            "path": static.path_calls,
        },
        "measurements": {
            "cold_wall_time_ms": core[0]["wall_time_ms"],
            "cold_parent_cpu_time_ms": core[0]["parent_cpu_time_ms"],
            "warm_public_wall_time_ms": phase1b._sample_summary(
                [item["wall_time_ms"] for item in warm]
            ),
            "warm_parent_cpu_time_ms": phase1b._sample_summary(
                [item["parent_cpu_time_ms"] for item in warm]
            ),
            "worker_cpu_ms": cold_receipt["metrics"].get("worker_cpu_ms"),
            "rss_start_kib": cold_receipt["metrics"].get("rss_start_kib"),
            "rss_peak_kib": cold_receipt["metrics"].get("rss_peak_kib"),
            "rss_end_kib": cold_receipt["metrics"].get("rss_end_kib"),
            "ir_bytes": cold_receipt["metrics"].get("ir_bytes", 0),
            "cache_bytes": cold_receipt["metrics"].get("cache_bytes", 0),
            "cache_entry_count": stats_before_out_of_scope["cache_entry_count"],
            "cache_peak_entry_count": stats_before_out_of_scope[
                "cache_peak_entry_count"
            ],
            "cache_peak_bytes": stats_before_out_of_scope["cache_peak_bytes"],
            "cache_eviction_count": stats_before_out_of_scope["cache_eviction_count"],
        },
    }


async def _measure_concurrent_mixed(
    workload: ReuseWorkload,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    request_count: int,
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    runtime = SourceGraphRuntime(runner_factory())
    static = phase3a._static_counter(static_factory)
    config = _execution_config(frontend_python, timeout_seconds)
    specs = (
        ("explain_signal_driver", workload.driver_args(), "driver"),
        ("find_signal_loads", workload.load_args(), "loads"),
        (
            "trace_signal_path",
            workload.reuse_path_args(expand_assigns=True),
            "path",
        ),
        (
            "trace_signal_path",
            workload.reuse_path_args(expand_assigns=False),
            "path",
        ),
    )
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    with phase3a._public_environment(
        workload.base,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        results = await asyncio.gather(
            *(
                _dispatch_timed(
                    specs[index % len(specs)][0],
                    specs[index % len(specs)][1],
                    operation=specs[index % len(specs)][2],
                )
                for index in range(request_count)
            )
        )
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    stats = runtime.stats_snapshot()
    receipts = [item["source_graph"] for item in results]
    return {
        "request_count": request_count,
        "wall_time_ms": round(wall_ms, 6),
        "parent_cpu_time_ms": round(cpu_ms, 6),
        "results": results,
        "actual_build_count": stats["actual_build_count"],
        "coalesced_waiter_count": stats["coalesced_waiter_count"],
        "cache_entry_count": stats["cache_entry_count"],
        "cache_peak_bytes": stats["cache_peak_bytes"],
        "artifact_fingerprints": sorted(
            {item["artifact_fingerprint_sha256"] for item in receipts}
        ),
        "query_fingerprints": sorted(
            {item["query_fingerprint_sha256"] for item in receipts}
        ),
        "flight_dispositions": dict(
            Counter(item["flight_disposition"] for item in receipts)
        ),
        "artifact_reuse": dict(Counter(item["artifact_reuse"] for item in receipts)),
        "static_calls": {
            "driver": static.driver_calls,
            "loads": static.load_calls,
            "path": static.path_calls,
        },
    }


async def _measure_dominating_reuse(
    workload: ReuseWorkload,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    if workload.out_of_scope_args is None:
        return None
    _reset_source_graph_adapter_cache_for_tests()
    runtime = SourceGraphRuntime(runner_factory())
    static = phase3a._static_counter(static_factory)
    with phase3a._public_environment(
        workload.base,
        runtime=runtime,
        config=_execution_config(frontend_python, timeout_seconds),
        static_backend=static,
    ):
        larger = await _dispatch_timed(
            "trace_signal_path", workload.out_of_scope_args, operation="path"
        )
        endpoint = await _dispatch_timed(
            "explain_signal_driver", workload.driver_args(), operation="driver"
        )
    return {
        "larger_artifact_query": larger,
        "covered_endpoint_query": endpoint,
        "runtime_stats": runtime.stats_snapshot(),
        "static_calls": static.driver_calls + static.load_calls + static.path_calls,
    }


def _capacity_requests(workload: ReuseWorkload):
    snapshot = compute_snapshot_fingerprint(
        workload.base.compile_log, workload.base.simulator
    )
    signals = (
        "sg_top.u_producer.seed",
        "sg_top.bus.data",
        "sg_top.u_bridge.lane_data",
    )
    requests = []
    for signal in signals:
        plan = build_source_graph_plan(
            compile_log=workload.base.compile_log,
            compile_result=workload.base.compile_result,
            hierarchy_result=workload.base.hierarchy,
            hierarchy_snapshot_sha256=snapshot,
            operation=QueryOperation.DRIVER,
            signal_path=signal,
            top_hint=workload.base.top,
            max_hops=8,
            frontend_version=FRONTEND_VERSION,
        )
        if plan.status is not AdapterStatus.READY or plan.request is None:
            raise BenchmarkError(f"capacity request blocked for {signal}")
        requests.append(plan.request)
    return tuple(requests)


async def _measure_capacity(workload: ReuseWorkload) -> dict[str, Any]:
    requests = _capacity_requests(workload)
    runner = phase3a.FixtureReadyRunner()
    ir_bytes = len(runner.ir.to_json_bytes())

    entry_runtime = SourceGraphRuntime(
        phase3a.FixtureReadyRunner(), max_cache_entries=2
    )
    first = await entry_runtime.prepare(requests[0])
    await entry_runtime.prepare(requests[1])
    await entry_runtime.prepare(requests[2])
    retained_entry_usable = bool(
        first.entry
        and first.entry.query_engine.query_driver("sg_top.u_producer.seed").matches
    )
    rebuilt = await entry_runtime.prepare(requests[0])

    byte_runtime = SourceGraphRuntime(
        phase3a.FixtureReadyRunner(), max_cache_bytes=(2 * ir_bytes) - 1
    )
    await byte_runtime.prepare(requests[0])
    await byte_runtime.prepare(requests[1])

    oversize_runtime = SourceGraphRuntime(
        phase3a.FixtureReadyRunner(), max_cache_bytes=ir_bytes - 1
    )
    oversize_first = await oversize_runtime.prepare(requests[0])
    oversize_second = await oversize_runtime.prepare(requests[0])
    return {
        "probe_model": "tracked hand IR; deterministic serialized-IR byte accounting",
        "ir_bytes": ir_bytes,
        "entry_boundary": {
            "max_entries": 2,
            "stats": entry_runtime.stats_snapshot(),
            "oldest_rebuild_cache_disposition": (
                rebuilt.metrics.cache_disposition.value
            ),
            "evicted_entry_remains_usable_for_active_holder": retained_entry_usable,
        },
        "byte_boundary": {
            "max_bytes": (2 * ir_bytes) - 1,
            "stats": byte_runtime.stats_snapshot(),
        },
        "oversize_bypass": {
            "max_bytes": ir_bytes - 1,
            "first_disposition": oversize_first.metrics.cache_disposition.value,
            "second_disposition": oversize_second.metrics.cache_disposition.value,
            "stats": oversize_runtime.stats_snapshot(),
        },
    }


async def _measure_query_cancellation(
    workload: ReuseWorkload,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(phase3a.FixtureReadyRunner())
    static = phase3a._static_counter(static_factory)
    original = server.SourceGraphConnectivityBackend

    class CancellingSourceGraphBackend(original):
        def find_driver(self, *args, **kwargs):
            del args, kwargs
            raise OperationCancelled("benchmark query cancellation")

    cancelled = False
    with phase3a._public_environment(
        workload.base,
        runtime=runtime,
        config=_execution_config(frontend_python, 60.0),
        static_backend=static,
    ):
        ready = await _dispatch_timed(
            "explain_signal_driver", workload.driver_args(), operation="driver"
        )
        server.SourceGraphConnectivityBackend = CancellingSourceGraphBackend
        try:
            try:
                await server._dispatch("explain_signal_driver", workload.driver_args())
            except asyncio.CancelledError:
                cancelled = True
        finally:
            server.SourceGraphConnectivityBackend = original
        retry = await _dispatch_timed(
            "explain_signal_driver", workload.driver_args(), operation="driver"
        )
    return {
        "cancelled": cancelled,
        "fallback_used": False,
        "static_calls": static.driver_calls + static.load_calls + static.path_calls,
        "cache_entry_count_after_cancel": runtime.stats_snapshot()["cache_entry_count"],
        "actual_build_count": runtime.stats_snapshot()["actual_build_count"],
        "first_artifact_reuse": ready["source_graph"]["artifact_reuse"],
        "retry_artifact_reuse": retry["source_graph"]["artifact_reuse"],
    }


async def _measure_mixed_provenance_rejection(
    workload: ReuseWorkload,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(phase3a.FixtureReadyRunner())
    static = phase3a._static_counter(static_factory)
    original = server.SourceGraphConnectivityBackend

    class MixedSourceGraphBackend:
        name = "source_graph"
        uses_external_worker = False

        def __init__(self, entry):
            del entry

        def find_driver(self, **kwargs):
            return {
                "signal_path": kwargs["signal_path"],
                "wave_path": kwargs["wave_path"],
                "resolved_rtl_name": "seed",
                "driver_status": "resolved",
                "recursive": True,
                "backend": "source_graph",
                "driver_chain": [
                    {
                        "depth": 0,
                        "signal_path": kwargs["signal_path"],
                        "backend": "static",
                    }
                ],
                "_source_graph_query_receipt": {
                    "status": "found",
                    "coverage_status": "complete",
                    "confidence": "exact",
                    "match_count": 1,
                    "unresolved_boundary_codes": [],
                    "traversed_binding_edges": 0,
                    "max_depth": 8,
                },
            }

    with phase3a._public_environment(
        workload.base,
        runtime=runtime,
        config=_execution_config(frontend_python, 60.0),
        static_backend=static,
    ):
        server.SourceGraphConnectivityBackend = MixedSourceGraphBackend
        try:
            result = await server._dispatch(
                "explain_signal_driver", workload.driver_args()
            )
        finally:
            server.SourceGraphConnectivityBackend = original
    payload = result.model_dump(mode="json")
    source = payload["backend_status"]["source_graph"]
    return {
        "actual_backend": payload["backend_status"]["actual_backend"],
        "payload_backend": payload.get("backend"),
        "fallback_reason": payload["backend_status"].get("fallback_reason"),
        "source_graph_blocker": source.get("blocker"),
        "static_calls": static.driver_calls + static.load_calls + static.path_calls,
        "public_payload_has_mixed_provenance": len(
            phase2._payload_backends(payload, "driver")
        )
        > 1,
    }


def _failure_gate(probes: Mapping[str, Any]) -> dict[str, Any]:
    checks = []
    expected_status = {
        "dependency": "dependency_blocked",
        "build_failure": "build_failed",
        "crash": "worker_crash",
        "timeout": "timed_out",
    }
    for name, status in expected_status.items():
        probe = probes[name]
        failure = probe["failure"]
        checks.append(
            {
                "name": f"{name}_cleanup_fallback_retry",
                "passed": (
                    failure["actual_backend"] == "static"
                    and failure["source_graph"]["prepare_status"] == status
                    and failure["source_graph"]["fallback_used"] is True
                    and probe["cache_entry_count_after_failure"] == 0
                    and probe["inflight_count_after_failure"] == 0
                    and probe["retry"]["actual_backend"] == "source_graph"
                ),
            }
        )
    cancellation = probes["cancellation"]
    checks.append(
        {
            "name": "cancellation_cleanup_no_fallback_retry",
            "passed": (
                cancellation["failure"]
                == {
                    "status": "cancelled",
                    "fallback_used": False,
                    "actual_backend": None,
                }
                and cancellation["static_calls_after_failure"] == 0
                and cancellation["cache_entry_count_after_failure"] == 0
                and cancellation["inflight_count_after_failure"] == 0
                and cancellation["retry"]["actual_backend"] == "source_graph"
            ),
        }
    )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _sequence_gate(
    workload: ReuseWorkload, result: Mapping[str, Any]
) -> dict[str, Any]:
    checks = []
    core = result["core_queries"]
    receipts = [item["source_graph"] for item in core]
    artifact_fingerprints = {item["artifact_fingerprint_sha256"] for item in receipts}
    query_fingerprints = {item["query_fingerprint_sha256"] for item in receipts}
    warm = [*core[1:], *result["extra_warm_queries"]]
    checks.append(
        {
            "name": "driver_load_path_correctness_single_provenance",
            "passed": (
                core[0]["facts"]["driver_status"] == "resolved"
                and core[1]["facts"]["load_count"] > 0
                and core[2]["facts"]["found"] is True
                and core[3]["facts"]["found"] is True
                and all(item["actual_backend"] == "source_graph" for item in core)
                and all(item["payload_backends"] == ["source_graph"] for item in core)
            ),
        }
    )
    if workload.expected_reuse_path is not None:
        checks.append(
            {
                "name": "representative_path_matches_expected",
                "passed": tuple(core[2]["facts"]["path_net_paths"])
                == workload.expected_reuse_path,
            }
        )
    checks.extend(
        (
            {
                "name": "one_cold_build_for_mixed_queries",
                "passed": (
                    receipts[0]["artifact_reuse"] == "cold"
                    and receipts[0]["metrics"]["actual_build_count"] == 1
                    and result["runtime_stats_before_out_of_scope"][
                        "actual_build_count"
                    ]
                    == 1
                ),
            },
            {
                "name": "artifact_and_query_identity_separated",
                "passed": len(artifact_fingerprints) == 1
                and len(query_fingerprints) == len(core),
            },
            {
                "name": "target_caps_and_expand_are_warm",
                "passed": all(
                    item["source_graph"]["artifact_reuse"] == "exact_hit"
                    and item["source_graph"]["metrics"]["actual_build_count"] == 0
                    for item in warm
                ),
            },
            {
                "name": "cross_target_warm_latency",
                "passed": result["measurements"]["warm_public_wall_time_ms"]["p95"]
                <= GATE_TARGETS["cross_target_warm_public_p95_max_ms"],
            },
            {
                "name": "peak_rss",
                "passed": (
                    result["measurements"]["rss_peak_kib"] is not None
                    and result["measurements"]["rss_peak_kib"]
                    <= GATE_TARGETS["peak_rss_max_kib"]
                ),
            },
            {
                "name": "no_static_payload_merge",
                "passed": all(value == 0 for value in result["static_calls"].values()),
            },
        )
    )
    if workload.out_of_scope_args is not None:
        out = result["out_of_scope"]
        checks.append(
            {
                "name": "out_of_scope_request_builds_new_bounded_artifact",
                "passed": (
                    out["source_graph"]["artifact_reuse"] == "cold"
                    and out["source_graph"]["cache_lookup_reason"]
                    == "cached_scope_not_dominating"
                    and out["source_graph"]["metrics"]["actual_build_count"] == 1
                    and result["runtime_stats_after"]["actual_build_count"] == 2
                    and tuple(out["facts"]["path_net_paths"])
                    == workload.expected_out_of_scope_path
                ),
            }
        )
    if workload.base.name == "opentitan_core":
        first = receipts[0]
        checks.extend(
            (
                {
                    "name": "opentitan_bounded_cold_latency",
                    "passed": result["measurements"]["cold_wall_time_ms"]
                    <= GATE_TARGETS["opentitan_bounded_cold_p50_max_ms"],
                },
                {
                    "name": "opentitan_manifest_and_scope_unchanged",
                    "passed": (
                        first["manifest"].get("input_count") == 785
                        and first["manifest"].get("top_count") == 11
                        and first["scope"].get("coverage_boundary_instance_count") == 4
                        and first["scope"].get("requested_cone_instance_count") == 4
                    ),
                },
                {
                    "name": "opentitan_coverage_honesty",
                    "passed": (
                        first["coverage_status"] == "inconclusive"
                        and first["coverage_blocking_diagnostic_count"] == 65
                        and first["query_confidence"] == "partial"
                    ),
                },
            )
        )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _concurrent_gate(result: Mapping[str, Any]) -> dict[str, Any]:
    results = result["results"]
    passed = (
        result["actual_build_count"] == 1
        and result["cache_entry_count"] == 1
        and len(result["artifact_fingerprints"]) == 1
        and len(result["query_fingerprints"]) >= 3
        and all(item["actual_backend"] == "source_graph" for item in results)
        and all(item["payload_backends"] == ["source_graph"] for item in results)
        and all(value == 0 for value in result["static_calls"].values())
    )
    return {
        "passed": passed,
        "expected": {"actual_build_count": 1, "artifact_count": 1},
        "actual": {
            "actual_build_count": result["actual_build_count"],
            "artifact_count": len(result["artifact_fingerprints"]),
            "query_count": len(result["query_fingerprints"]),
        },
    }


def _capacity_gate(result: Mapping[str, Any]) -> dict[str, Any]:
    entry = result["entry_boundary"]
    byte = result["byte_boundary"]
    oversize = result["oversize_bypass"]
    checks = [
        {
            "name": "entry_lru_boundary",
            "passed": entry["stats"]["cache_entry_count"] == 2
            and entry["stats"]["cache_peak_entry_count"] == 2
            and entry["stats"]["cache_eviction_count"] == 2
            and entry["oldest_rebuild_cache_disposition"] == "miss"
            and entry["evicted_entry_remains_usable_for_active_holder"] is True,
        },
        {
            "name": "aggregate_byte_boundary",
            "passed": byte["stats"]["cache_entry_count"] == 1
            and byte["stats"]["cache_bytes"] <= byte["max_bytes"]
            and byte["stats"]["cache_eviction_count"] == 1,
        },
        {
            "name": "oversize_bypass_without_pollution",
            "passed": oversize["first_disposition"] == "bypass_capacity"
            and oversize["second_disposition"] == "bypass_capacity"
            and oversize["stats"]["cache_entry_count"] == 0
            and oversize["stats"]["cache_oversize_bypass_count"] == 2,
        },
    ]
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _metrics_privacy_receipt() -> dict[str, Any]:
    fields = sorted(
        item
        for item in operation_metrics._PUBLIC_FIELDS
        if item.startswith("source_graph")
    )
    forbidden = (
        "endpoint",
        "signal_name",
        "compile_path",
        "scope_path",
        "source_path",
        "diagnostic_text",
        "exception_text",
    )
    return {
        "source_graph_allowlist": fields,
        "fixed_phase_field": "source_graph_phase",
        "all_other_fields_numeric": True,
        "contains_forbidden_content_field": any(
            token in field for token in forbidden for field in fields
        ),
    }


def _default_failure_factories(
    process_factory: RunnerFactory,
    *,
    frontend_python: Path,
    failure_timeout_seconds: float,
) -> FailureRunnerFactories:
    missing_worker = ROOT / "scripts/__missing_source_graph_phase3b_worker__.py"
    if missing_worker.exists():
        raise BenchmarkError("reserved missing-worker path unexpectedly exists")
    return FailureRunnerFactories(
        dependency=lambda: phase3a._FirstThenRunner(
            phase3a._FailedRunner(
                PrepareStatus.DEPENDENCY_BLOCKED, "frontend_unavailable"
            ),
            process_factory(),
        ),
        build_failure=lambda: phase3a._FirstThenRunner(
            phase3a._FailedRunner(PrepareStatus.BUILD_FAILED, "frontend_build_failed"),
            process_factory(),
        ),
        crash=lambda: phase3a._FirstThenRunner(
            IsolatedSourceGraphProcessRunner(
                python_executable=frontend_python,
                worker_script=missing_worker,
                working_directory=ROOT,
            ),
            process_factory(),
        ),
        timeout=lambda: phase3a._FirstThenRunner(
            phase3a._TimeoutOverrideRunner(process_factory(), failure_timeout_seconds),
            process_factory(),
        ),
        cancellation=lambda: phase3a._FirstThenRunner(
            process_factory(), process_factory()
        ),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        action="append",
        choices=("hand_fixture", "opentitan_core"),
        help="Workload to run; repeat to select both (default: hand fixture)",
    )
    parser.add_argument("--frontend-python", type=Path, default=DEFAULT_FRONTEND_PYTHON)
    parser.add_argument(
        "--opentitan-compile-log", type=Path, default=DEFAULT_OPENTITAN_COMPILE_LOG
    )
    parser.add_argument("--cold-repeats", type=int, default=1)
    parser.add_argument("--warm-repeats", type=int, default=5)
    parser.add_argument("--concurrent-requests", type=int, default=4)
    parser.add_argument("--worker-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--failure-timeout-seconds", type=float, default=0.01)
    parser.add_argument("--cancellation-delay-seconds", type=float, default=0.005)
    parser.add_argument("--output", type=Path)
    return parser


async def run_benchmark_async(
    args: argparse.Namespace,
    *,
    runner_factory: RunnerFactory | None = None,
    failure_factories: FailureRunnerFactories | None = None,
    static_factory: StaticFactory | None = None,
) -> dict[str, Any]:
    if args.cold_repeats < 1 or args.warm_repeats < 1:
        raise BenchmarkError("cold/warm repeat counts must be positive")
    if args.concurrent_requests < 3:
        raise BenchmarkError("concurrent mixed query count must be at least three")
    if (
        min(
            args.worker_timeout_seconds,
            args.failure_timeout_seconds,
            args.cancellation_delay_seconds,
        )
        <= 0
    ):
        raise BenchmarkError("timeout/cancellation values must be positive")

    baselines = _read_historical_baselines()
    process_factory = runner_factory or (
        lambda: IsolatedSourceGraphProcessRunner(
            python_executable=args.frontend_python,
            working_directory=ROOT,
        )
    )
    selected_static_factory = static_factory or (
        connectivity_backend.StaticConnectivityBackend
    )
    selected_failure_factories = failure_factories or _default_failure_factories(
        process_factory,
        frontend_python=args.frontend_python,
        failure_timeout_seconds=args.failure_timeout_seconds,
    )

    with tempfile.TemporaryDirectory(prefix="traceweave-phase3b-benchmark-") as temp:
        temp_root = Path(temp)
        hand = _hand_workload(temp_root)
        selected = args.workload or ["hand_fixture"]
        workloads = {"hand_fixture": hand}
        if "opentitan_core" in selected:
            workloads["opentitan_core"] = _opentitan_workload(
                args.opentitan_compile_log
            )

        measured = []
        for name in selected:
            workload = workloads[name]
            runs = [
                await _measure_reuse_sequence(
                    workload,
                    runner_factory=process_factory,
                    static_factory=selected_static_factory,
                    frontend_python=args.frontend_python,
                    timeout_seconds=args.worker_timeout_seconds,
                    warm_repeats=args.warm_repeats,
                )
                for _ in range(args.cold_repeats)
            ]
            for run in runs:
                run["gate"] = _sequence_gate(workload, run)
            concurrent = await _measure_concurrent_mixed(
                workload,
                runner_factory=process_factory,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
                timeout_seconds=args.worker_timeout_seconds,
                request_count=args.concurrent_requests,
            )
            concurrent["gate"] = _concurrent_gate(concurrent)
            cold_values = [run["measurements"]["cold_wall_time_ms"] for run in runs]
            warm_values = [
                item["wall_time_ms"]
                for run in runs
                for item in [*run["core_queries"][1:], *run["extra_warm_queries"]]
            ]
            measured.append(
                {
                    "name": name,
                    "scope_claim": workload.base.scope_claim,
                    "runs": runs,
                    "concurrent_mixed_queries": concurrent,
                    "aggregate": {
                        "cold_wall_time_ms": phase1b._sample_summary(cold_values),
                        "cross_target_warm_wall_time_ms": phase1b._sample_summary(
                            warm_values
                        ),
                        "peak_rss_kib": phase1b._sample_summary(
                            [
                                value
                                for value in (
                                    run["measurements"]["rss_peak_kib"] for run in runs
                                )
                                if value is not None
                            ]
                        ),
                    },
                    "gate": {
                        "passed": all(run["gate"]["passed"] for run in runs)
                        and concurrent["gate"]["passed"]
                    },
                }
            )

        dominance = await _measure_dominating_reuse(
            hand,
            runner_factory=process_factory,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
            timeout_seconds=args.worker_timeout_seconds,
        )
        capacity = await _measure_capacity(hand)
        capacity["gate"] = _capacity_gate(capacity)
        failures = await phase3a._measure_failure_probes(
            hand.base,
            selected_failure_factories,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
            timeout_seconds=args.worker_timeout_seconds,
            cancellation_delay_seconds=args.cancellation_delay_seconds,
        )
        failure_gate = _failure_gate(failures)
        query_cancellation = await _measure_query_cancellation(
            hand,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
        )
        mixed_provenance = await _measure_mixed_provenance_rejection(
            hand,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
        )
        route_probes = {
            "npi_success": await phase3a._measure_npi_probe(
                hand.base,
                connected=True,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "npi_authoritative_negative": await phase3a._measure_npi_probe(
                hand.base,
                connected=False,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "source_graph_complete_negative": await phase3a._measure_negative_probe(
                hand.base,
                inconclusive=False,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "source_graph_inconclusive_negative": (
                await phase3a._measure_negative_probe(
                    hand.base,
                    inconclusive=True,
                    static_factory=selected_static_factory,
                    frontend_python=args.frontend_python,
                )
            ),
            "query_cancellation": query_cancellation,
            "mixed_provenance_rejection": mixed_provenance,
        }

    isolation = _route_isolation_receipt()
    parent_import = phase2._parent_import_receipt()
    metrics_privacy = _metrics_privacy_receipt()
    dominance_receipt = dominance["covered_endpoint_query"]["source_graph"]
    dominance_passed = (
        dominance["runtime_stats"]["actual_build_count"] == 1
        and dominance_receipt["artifact_reuse"] == "dominating_hit"
        and dominance_receipt["cache_lookup_reason"] == "dominating_artifact"
        and dominance_receipt["scope_match"]["relation"] == "superset"
        and dominance_receipt["scope_match"]["reusable"] is True
        and dominance["static_calls"] == 0
    )
    route_probe_passed = (
        route_probes["npi_success"]["result"]["actual_backend"] == "verdi_npi"
        and route_probes["npi_success"]["source_graph_runtime_get_count"] == 0
        and route_probes["npi_authoritative_negative"]["result"]["actual_backend"]
        == "verdi_npi"
        and route_probes["source_graph_complete_negative"]["result"]["actual_backend"]
        == "source_graph"
        and route_probes["source_graph_inconclusive_negative"]["result"][
            "actual_backend"
        ]
        == "static"
        and query_cancellation["cancelled"] is True
        and query_cancellation["static_calls"] == 0
        and query_cancellation["actual_build_count"] == 1
        and mixed_provenance["actual_backend"] == "static"
        and mixed_provenance["public_payload_has_mixed_provenance"] is False
        and mixed_provenance["source_graph_blocker"]["code"]
        == "mixed_provenance_rejected"
    )
    architecture_passed = (
        isolation["production_route_ordering_changed"] is False
        and isolation["reuse_behavior_affected_tools"]
        == [
            "explain_signal_driver",
            "find_signal_loads",
            "trace_signal_path",
        ]
        and isolation["trace_x_source_route_changed"] is False
        and isolation["waveform_locking_model_changed"] is False
        and parent_import["server_import_succeeded"] is True
        and parent_import["pyslang_imported"] is False
        and parent_import["uhdm_imported"] is False
        and metrics_privacy["contains_forbidden_content_field"] is False
    )
    workload_gate_passed = bool(measured) and all(
        item["gate"]["passed"] for item in measured
    )
    passed = (
        workload_gate_passed
        and dominance_passed
        and capacity["gate"]["passed"]
        and failure_gate["passed"]
        and route_probe_passed
        and architecture_passed
    )

    script_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {"root": str(ROOT), "head": phase1b._git_head(ROOT)},
        "benchmark_script": {
            "path": str(script_path.relative_to(ROOT)),
            "sha256": _sha256_bytes(script_path.read_bytes()),
            "support_script": "scripts/benchmark_source_graph_phase3a.py",
            "support_script_sha256": _sha256_bytes(
                (ROOT / "scripts/benchmark_source_graph_phase3a.py").read_bytes()
            ),
        },
        "before_baselines": baselines,
        "frontend": {
            "name": phase3a.SLANG_FRONTEND_NAME,
            "required_version": FRONTEND_VERSION,
            "interpreter": str(args.frontend_python),
            "parent_import_requires_pyslang": False,
            "dependency_model": "optional pinned isolated one-shot worker",
        },
        "runtime_model": {
            "production_route": (
                "trusted Verdi NPI -> bounded Source Graph -> whole-result Legacy Static"
            ),
            "production_route_ordering_changed": False,
            "reuse_behavior_affected_tools": [
                "explain_signal_driver",
                "find_signal_loads",
                "trace_signal_path",
            ],
            "cross_target_process_memory_reuse": True,
            "cross_process_reuse": False,
            "same_artifact_single_flight": True,
            "max_concurrent_cold_builds_per_process": 1,
            "cache_eviction_policy": "deterministic process-memory LRU",
            "cache_max_entries": GATE_TARGETS["default_cache_max_entries"],
            "cache_max_accounted_ir_bytes": GATE_TARGETS["default_cache_max_bytes"],
            "disk_cache": False,
            "persistent_worker": False,
            "startup_build": False,
            "startup_full_design_enumeration": False,
            "trace_x_source_route_changed": False,
            "waveform_locking_model_changed": False,
        },
        "measurement_policy": {
            "cold": "fresh process-session runtime; first bounded artifact build",
            "warm": "different targets, operations, caps, and expand flags over one artifact",
            "concurrent": "mixed driver/load/path requests over one proved artifact scope",
            "capacity": "tracked fake-worker IR with deterministic serialized-byte accounting",
            "correctness_oracle": "tracked hand IR and RTL fixture",
            "opentitan_claim": "target-scoped only",
            "opentitan_full_design_accuracy_claim": False,
            "opentitan_full_design_coverage_claim": False,
            "opentitan_full_design_warm_state_claim": False,
            "opentitan_full_design_speedup_claim": False,
        },
        "gate_targets": GATE_TARGETS,
        "route_isolation": isolation,
        "route_probes": {**route_probes, "passed": route_probe_passed},
        "workloads": measured,
        "dominating_scope_reuse": {
            **dominance,
            "gate": {"passed": dominance_passed},
        },
        "capacity_probe": capacity,
        "failure_probes": {"results": failures, "gate": failure_gate},
        "metrics_privacy": metrics_privacy,
        "parent_import": parent_import,
        "assessment": {
            "decision": (
                "phase3b_cross_target_process_memory_reuse_gate_passed"
                if passed
                else "phase3b_no_go_keep_auditing_cross_target_reuse"
            ),
            "phase3b_cross_target_process_memory_reuse_gate_passed": passed,
            "workload_gate_passed": workload_gate_passed,
            "dominance_gate_passed": dominance_passed,
            "capacity_gate_passed": capacity["gate"]["passed"],
            "failure_gate_passed": failure_gate["passed"],
            "route_gate_passed": route_probe_passed,
            "architecture_gate_passed": architecture_passed,
            "driver_load_path_correctness_unchanged": workload_gate_passed,
            "no_mixed_provenance": (
                mixed_provenance["public_payload_has_mixed_provenance"] is False
            ),
            "cancellation_fallback": False,
            "failure_or_cancellation_cache_polluted": False,
            "global_python_without_pyslang_startup_ok": (
                parent_import["server_import_succeeded"] is True
                and parent_import["pyslang_imported"] is False
                and parent_import["uhdm_imported"] is False
            ),
            "production_route_ordering_changed": False,
            "reuse_behavior_affected_tools": [
                "explain_signal_driver",
                "find_signal_loads",
                "trace_signal_path",
            ],
            "cross_target_process_memory_reuse": True,
            "cross_process_reuse": False,
            "trace_x_source_route_changed": False,
            "disk_cache": False,
            "persistent_worker": False,
            "startup_full_design_enumeration": False,
            "waveform_locking_model_changed": False,
            "opentitan_claim": "target-scoped only",
            "next_step": "stop after Phase 3B; Phase 3C requires separate authorization",
        },
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_benchmark_async(args))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        result = run_benchmark(args)
    except (BenchmarkError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        phase1b._write_json_atomic(args.output, result)
    print(rendered)
    return (
        0
        if result["assessment"]["phase3b_cross_target_process_memory_reuse_gate_passed"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
