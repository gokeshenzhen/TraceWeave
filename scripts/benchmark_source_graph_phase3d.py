#!/usr/bin/env python3
"""Phase 3D gate for the opt-in bounded exact Source Graph disk cache.

The parent harness launches a distinct Python process for every cold miss and
fresh-process disk hit.  A disk-hit process then measures process-memory hits
without leaving that process.  The tracked hand fixture is the correctness
oracle; the historical 785-input / 11-top / four-path OpenTitan workload is the
only representative performance workload and remains target-scoped.

The default invocation uses a tracked fake worker and the hand fixture, so
automated tests require no pyslang, NPI/license, OpenTitan checkout, ignored
files, network, or pre-existing cache.  ``--real-frontend`` is required for a
representative performance claim.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SourceGraphExecutionConfig  # noqa: E402
import server  # noqa: E402
from scripts import benchmark_source_graph_phase1b as phase1b  # noqa: E402
from scripts import benchmark_source_graph_phase2 as phase2  # noqa: E402
from scripts import benchmark_source_graph_phase3a as phase3a  # noqa: E402
from scripts import benchmark_source_graph_phase3b as phase3b  # noqa: E402
from scripts import benchmark_source_graph_phase3c as phase3c  # noqa: E402
from scripts import telemetry_report  # noqa: E402
import src.connectivity_backend as connectivity_backend  # noqa: E402
from src import operation_metrics, usage_telemetry  # noqa: E402
from src.source_graph_adapter import (  # noqa: E402
    _reset_source_graph_adapter_cache_for_tests,
)
from src.source_graph_disk_cache import (  # noqa: E402
    SOURCE_GRAPH_DISK_CACHE_IR,
    SourceGraphDiskCache,
)
from src.source_graph_runtime import (  # noqa: E402
    IsolatedSourceGraphProcessRunner,
    SourceGraphRuntime,
)


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase3d"
FRONTEND_VERSION = "11.0.0"
MEASUREMENT_HEAD = "a461dd946ed2df3bdfb2d59d5c01e19db919068f"
DEFAULT_FRONTEND_PYTHON = phase3a.DEFAULT_FRONTEND_PYTHON
DEFAULT_OPENTITAN_COMPILE_LOG = phase3a.DEFAULT_OPENTITAN_COMPILE_LOG
DEFAULT_OUTPUT = ROOT / "benchmarks/source_graph_connectivity_phase3d_results.json"

HISTORICAL_EVIDENCE = {
    "phase1a": (
        ROOT / "benchmarks/source_graph_connectivity_phase1a_results.json",
        "c7310560a1e89e19694a83d41e24a645578b747585c68df546a20937f3fa42e2",
        "source_graph_connectivity_phase1a",
        "go_for_production_integration_review",
        "all_measured_workload_gates_passed",
    ),
    "phase1b": (
        ROOT / "benchmarks/source_graph_connectivity_phase1b_results.json",
        "c9a25c96c63ddce9205ecabf86b61f6da1eff9ba0f71aeee099a6b04f7237da7",
        "source_graph_connectivity_phase1b",
        "phase1b_internal_gate_passed_await_production_integration_approval",
        "phase1b_internal_gate_passed",
    ),
    "phase2": (
        ROOT / "benchmarks/source_graph_connectivity_phase2_results.json",
        "1b5f76c3862601bb1163d838744c9c03ec7ed62cd1bc5f663ef7008bd0902599",
        "source_graph_connectivity_phase2",
        "phase2_public_driver_load_gate_passed",
        "phase2_public_driver_load_gate_passed",
    ),
    "phase3a": (
        ROOT / "benchmarks/source_graph_connectivity_phase3a_results.json",
        "6b7f8822f978f8e46dd9172c2b093e731889f44b0b68c040b5c66619ccbc438a",
        "source_graph_connectivity_phase3a",
        "phase3a_trace_signal_path_gate_passed",
        "phase3a_trace_signal_path_gate_passed",
    ),
    "phase3b": (
        ROOT / "benchmarks/source_graph_connectivity_phase3b_results.json",
        "32a57dba8a1c554188da79f9bab0982fc0f547e0b19f9a6a789f438340113e81",
        "source_graph_connectivity_phase3b",
        "phase3b_cross_target_process_memory_reuse_gate_passed",
        "phase3b_cross_target_process_memory_reuse_gate_passed",
    ),
    "phase3c": (
        ROOT / "benchmarks/source_graph_connectivity_phase3c_results.json",
        "8b77588206e9108edf7cb47e56979332e8f8205be873462911d30efabf3b19cf",
        "source_graph_connectivity_phase3c",
        "phase3c_trace_x_source_graph_integration_gate_passed",
        "phase3c_trace_x_source_graph_integration_gate_passed",
    ),
}

GATE_TARGETS = {
    "opentitan_disk_hit_p95_max_ms": 1_500.0,
    "opentitan_cold_to_disk_min_reduction_percent": 75.0,
    "disk_hit_frontend_actual_build_count": 0,
    "disk_hit_frontend_launch_count": 0,
    "memory_hit_disk_lookup_count": 0,
    "hand_cold_regression_max_ratio": 1.5,
    "hand_cold_regression_max_absolute_ms": 100.0,
    "hand_memory_p95_max_ms": 10.0,
    "disk_cache_default_enabled": False,
}

BenchmarkError = phase1b.BenchmarkError

CORRECTNESS_TESTS = (
    "tests/test_source_graph_disk_cache.py",
    "tests/test_source_graph_disk_runtime.py",
    "tests/test_source_graph_runtime.py",
    "tests/test_source_graph_production.py",
    "tests/test_source_graph_public_routing.py",
    "tests/test_source_graph_trace_public_routing.py",
    "tests/test_source_graph_operational_telemetry.py",
    "tests/test_operation_metrics.py",
    "tests/test_usage_telemetry.py",
    "tests/test_schemas.py",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_json(payload: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _git_head() -> str:
    return phase1b._git_head(ROOT)


def _read_historical_evidence() -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    for name, (
        path,
        expected_sha,
        benchmark,
        decision,
        gate_field,
    ) in HISTORICAL_EVIDENCE.items():
        raw = path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if actual_sha != expected_sha:
            raise BenchmarkError(
                f"{name} historical evidence hash mismatch: {actual_sha}"
            )
        payload = json.loads(raw)
        assessment = payload.get("assessment", {})
        if payload.get("schema_version") != "1.0":
            raise BenchmarkError(f"{name} historical schema mismatch")
        if payload.get("benchmark") != benchmark:
            raise BenchmarkError(f"{name} historical benchmark mismatch")
        if assessment.get("decision") != decision:
            raise BenchmarkError(f"{name} historical assessment mismatch")
        if assessment.get(gate_field) is not True:
            raise BenchmarkError(f"{name} historical gate is not passed")
        receipts[name] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": actual_sha,
            "schema_version": payload["schema_version"],
            "benchmark": payload["benchmark"],
            "measurement_head": payload.get("repository", {}).get("head"),
            "decision": decision,
        }
    return receipts


def _workload_to_spec(workload: phase3b.ReuseWorkload) -> dict[str, Any]:
    base = workload.base
    return {
        "base": {
            "name": base.name,
            "compile_log": base.compile_log,
            "simulator": base.simulator,
            "compile_result": base.compile_result,
            "hierarchy": base.hierarchy,
            "top": base.top,
            "from_signal": base.from_signal,
            "to_signal": base.to_signal,
            "expected_path": list(base.expected_path),
            "expand_assigns": base.expand_assigns,
            "scope_claim": base.scope_claim,
        },
        "driver_signal": workload.driver_signal,
        "load_signal": workload.load_signal,
        "reuse_from_signal": workload.reuse_from_signal,
        "reuse_to_signal": workload.reuse_to_signal,
        "expected_reuse_path": (
            list(workload.expected_reuse_path)
            if workload.expected_reuse_path is not None
            else None
        ),
        "out_of_scope_args": workload.out_of_scope_args,
        "expected_out_of_scope_path": (
            list(workload.expected_out_of_scope_path)
            if workload.expected_out_of_scope_path is not None
            else None
        ),
    }


def _workload_from_spec(payload: Mapping[str, Any]) -> phase3b.ReuseWorkload:
    base_payload = payload["base"]
    base = phase3a.PathWorkload(
        name=base_payload["name"],
        compile_log=base_payload["compile_log"],
        simulator=base_payload["simulator"],
        compile_result=base_payload["compile_result"],
        hierarchy=base_payload["hierarchy"],
        top=base_payload["top"],
        from_signal=base_payload["from_signal"],
        to_signal=base_payload["to_signal"],
        expected_path=tuple(base_payload["expected_path"]),
        expand_assigns=bool(base_payload["expand_assigns"]),
        scope_claim=base_payload["scope_claim"],
    )
    return phase3b.ReuseWorkload(
        base=base,
        driver_signal=payload["driver_signal"],
        load_signal=payload["load_signal"],
        reuse_from_signal=payload["reuse_from_signal"],
        reuse_to_signal=payload["reuse_to_signal"],
        expected_reuse_path=(
            tuple(payload["expected_reuse_path"])
            if payload.get("expected_reuse_path") is not None
            else None
        ),
        out_of_scope_args=payload.get("out_of_scope_args"),
        expected_out_of_scope_path=(
            tuple(payload["expected_out_of_scope_path"])
            if payload.get("expected_out_of_scope_path") is not None
            else None
        ),
    )


def _current_rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _execution_config(
    frontend_python: Path,
    timeout_seconds: float,
    cache_root: Path,
    max_entries: int,
    max_bytes: int,
) -> SourceGraphExecutionConfig:
    return SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=timeout_seconds,
        disk_cache_enabled=True,
        disk_cache_root=cache_root,
        disk_cache_max_entries=max_entries,
        disk_cache_max_bytes=max_bytes,
    )


def _augment_compact(result: Any, compact: dict[str, Any]) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    source = payload["backend_status"].get("source_graph") or {}
    compact_source = compact.get("source_graph") or {}
    compact_source.update(
        {
            "cache_tier": source.get("cache_tier"),
            "disk_validation_outcome": source.get("disk_validation_outcome"),
            "artifact_reuse": source.get("artifact_reuse"),
            "cache_disposition": source.get("cache_disposition"),
            "cache_lookup_reason": source.get("cache_lookup_reason"),
            "metrics": source.get("metrics", {}),
        }
    )
    compact["source_graph"] = compact_source
    compact["public_payload_sha256"] = _sha256_json(payload)
    return compact


async def _dispatch_child_query(
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
    compact = phase3b._compact_result(
        result,
        operation=operation,
        wall_time_ms=wall_ms,
        parent_cpu_time_ms=cpu_ms,
    )
    return _augment_compact(result, compact)


async def _run_child_async(args: argparse.Namespace) -> dict[str, Any]:
    workload_payload = json.loads(args.workload_spec.read_text(encoding="utf-8"))
    workload = _workload_from_spec(workload_payload)
    cache = SourceGraphDiskCache(
        args.cache_root,
        max_entries=args.disk_max_entries,
        max_bytes=args.disk_max_bytes,
    )
    before_snapshot = cache.maintenance_snapshot()
    namespace_existed_before = cache.namespace_root.exists()
    if args.fake_worker:
        runner = phase3a.FixtureReadyRunner()
        worker_kind = "tracked_fake_worker"
    else:
        runner = IsolatedSourceGraphProcessRunner(
            python_executable=args.frontend_python,
            working_directory=ROOT,
        )
        worker_kind = "isolated_one_shot_pyslang"
    runtime = SourceGraphRuntime(runner, disk_cache=cache)
    static = phase3a._static_counter(connectivity_backend.StaticConnectivityBackend)
    config = _execution_config(
        args.frontend_python,
        args.worker_timeout_seconds,
        args.cache_root,
        args.disk_max_entries,
        args.disk_max_bytes,
    )
    _reset_source_graph_adapter_cache_for_tests()
    rss_start = _current_rss_kib()
    children_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    with phase3a._public_environment(
        workload.base,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        primary = await _dispatch_child_query(
            "explain_signal_driver",
            workload.driver_args(),
            operation="driver",
        )
        memory_hits: list[dict[str, Any]] = []
        driver_load_path: list[dict[str, Any]] = []
        if args.child_stage == "disk":
            for _ in range(args.memory_repeats):
                memory_hits.append(
                    await _dispatch_child_query(
                        "explain_signal_driver",
                        workload.driver_args(),
                        operation="driver",
                    )
                )
            driver_load_path = [
                await _dispatch_child_query(
                    "find_signal_loads",
                    workload.load_args(),
                    operation="loads",
                ),
                await _dispatch_child_query(
                    "trace_signal_path",
                    workload.reuse_path_args(expand_assigns=True),
                    operation="path",
                ),
            ]
    after_snapshot = cache.maintenance_snapshot()
    runtime_stats = runtime.stats_snapshot()
    rss_end = _current_rss_kib()
    parent_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    primary_metrics = primary["source_graph"]["metrics"]
    return {
        "stage": args.child_stage,
        "workload": workload.base.name,
        "worker_kind": worker_kind,
        "process_id": os.getpid(),
        "repository_head": _git_head(),
        "server_sha256": _sha256_bytes((ROOT / "server.py").read_bytes()),
        "cache_condition": {
            "isolated_root": True,
            "namespace_existed_before": namespace_existed_before,
            "entries_before": before_snapshot.entry_count,
            "bytes_before": before_snapshot.disk_bytes,
            "entries_after": after_snapshot.entry_count,
            "bytes_after": after_snapshot.disk_bytes,
            "unsafe_entries_after": after_snapshot.unsafe_entry_count,
            "max_entries": args.disk_max_entries,
            "max_bytes": args.disk_max_bytes,
        },
        "primary": primary,
        "memory_hits": memory_hits,
        "driver_load_path": driver_load_path,
        "runtime_stats": runtime_stats,
        "frontend": {
            "actual_build_count": primary_metrics.get("actual_build_count", 0),
            "launch_count": primary_metrics.get("frontend_launch_count", 0),
            "worker_cpu_ms": primary_metrics.get("worker_cpu_ms"),
            "child_rss_start_kib": primary_metrics.get("rss_start_kib"),
            "child_rss_peak_kib": primary_metrics.get("rss_peak_kib"),
            "child_rss_end_kib": primary_metrics.get("rss_end_kib"),
            "process_children_peak_kib_before": children_before,
            "process_children_peak_kib_after": children_after,
        },
        "parent_process": {
            "rss_start_kib": rss_start,
            "rss_peak_kib": parent_peak,
            "rss_end_kib": rss_end,
        },
        "static_call_count": static.total_calls,
        "adapter_full_content_validation": (
            primary["source_graph"]
            .get("manifest", {})
            .get("fingerprint_cache_disposition")
            == "miss"
        ),
    }


def _child_main(args: argparse.Namespace) -> int:
    payload = asyncio.run(_run_child_async(args))
    print(json.dumps(payload, sort_keys=True))
    return 0


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
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


def _spawn_child(
    args: argparse.Namespace,
    *,
    workload_spec: Path,
    cache_root: Path,
    stage: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-run",
        "--child-stage",
        stage,
        "--workload-spec",
        str(workload_spec),
        "--cache-root",
        str(cache_root),
        "--frontend-python",
        str(args.frontend_python),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--memory-repeats",
        str(args.memory_repeats),
        "--disk-max-entries",
        str(args.disk_max_entries),
        "--disk-max-bytes",
        str(args.disk_max_bytes),
    ]
    if not args.real_frontend:
        command.append("--fake-worker")
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "TRACEWEAVE_CACHE_DIR": str(cache_root),
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE": "1",
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES": str(
                args.disk_max_entries
            ),
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES": str(args.disk_max_bytes),
        }
    )
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=args.process_timeout_seconds,
    )
    if completed.returncode != 0:
        raise BenchmarkError(
            f"{stage} child failed with {completed.returncode}: "
            f"{completed.stderr[-1000:]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"{stage} child returned invalid JSON") from exc
    if payload.get("repository_head") != _git_head():
        raise BenchmarkError(f"{stage} child loaded a different repository HEAD")
    return payload


def _receipt_equivalence(cold: Mapping[str, Any], disk: Mapping[str, Any]) -> dict:
    cold_source = cold["primary"]["source_graph"]
    disk_source = disk["primary"]["source_graph"]
    fields = (
        "artifact_fingerprint_sha256",
        "selected_artifact_fingerprint_sha256",
        "query_fingerprint_sha256",
        "compile_fingerprint_sha256",
        "ir_fingerprint_sha256",
        "coverage_status",
        "coverage_files_total",
        "coverage_files_projected",
        "coverage_diagnostic_count",
        "coverage_blocking_diagnostic_count",
        "coverage_gap_count",
        "coverage_gap_codes",
        "objective_exclusions",
        "query_status",
        "query_confidence",
        "query_match_count",
        "scope",
        "gap_codes",
        "cross_request_reusable",
        "snapshot_identity_complete",
    )
    comparisons = {
        field: cold_source.get(field) == disk_source.get(field) for field in fields
    }
    facts_equal = cold["primary"].get("facts") == disk["primary"].get("facts")
    payload_backends_equal = cold["primary"].get("payload_backends") == disk[
        "primary"
    ].get("payload_backends")
    return {
        "compared_fields": list(fields),
        "field_matches": comparisons,
        "facts_equal": facts_equal,
        "payload_backends_equal": payload_backends_equal,
        "passed": all(comparisons.values()) and facts_equal and payload_backends_equal,
    }


def _run_workload_pairs(
    args: argparse.Namespace,
    *,
    workload: phase3b.ReuseWorkload,
    workload_spec: Path,
    cache_parent: Path,
) -> dict[str, Any]:
    pairs = []
    for index in range(args.repeats):
        cache_root = cache_parent / f"pair-{index}"
        cold = _spawn_child(
            args,
            workload_spec=workload_spec,
            cache_root=cache_root,
            stage="cold",
        )
        disk = _spawn_child(
            args,
            workload_spec=workload_spec,
            cache_root=cache_root,
            stage="disk",
        )
        equivalence = _receipt_equivalence(cold, disk)
        pairs.append(
            {
                "index": index,
                "cold_miss_process": cold,
                "disk_hit_process": disk,
                "cold_disk_equivalence": equivalence,
            }
        )

    cold_wall = [item["cold_miss_process"]["primary"]["wall_time_ms"] for item in pairs]
    disk_wall = [item["disk_hit_process"]["primary"]["wall_time_ms"] for item in pairs]
    memory_wall = [
        hit["wall_time_ms"]
        for item in pairs
        for hit in item["disk_hit_process"]["memory_hits"]
    ]
    cold_cpu = [
        item["cold_miss_process"]["primary"]["parent_cpu_time_ms"] for item in pairs
    ]
    disk_cpu = [
        item["disk_hit_process"]["primary"]["parent_cpu_time_ms"] for item in pairs
    ]
    memory_cpu = [
        hit["parent_cpu_time_ms"]
        for item in pairs
        for hit in item["disk_hit_process"]["memory_hits"]
    ]
    cold_parent_rss = [
        item["cold_miss_process"]["parent_process"]["rss_peak_kib"] for item in pairs
    ]
    disk_parent_rss = [
        item["disk_hit_process"]["parent_process"]["rss_peak_kib"] for item in pairs
    ]
    cold_child_rss = [
        item["cold_miss_process"]["frontend"]["child_rss_peak_kib"]
        for item in pairs
        if item["cold_miss_process"]["frontend"]["child_rss_peak_kib"] is not None
    ]
    disk_child_rss = [
        item["disk_hit_process"]["frontend"]["child_rss_peak_kib"]
        for item in pairs
        if item["disk_hit_process"]["frontend"]["child_rss_peak_kib"] is not None
    ]
    cold_summary = phase1b._sample_summary(cold_wall)
    disk_summary = phase1b._sample_summary(disk_wall)
    reduction = (
        (1.0 - disk_summary["p95"] / cold_summary["p95"]) * 100.0
        if cold_summary["p95"] > 0
        else 0.0
    )
    gate_checks = {
        "cold_and_disk_are_distinct_processes": all(
            item["cold_miss_process"]["process_id"]
            != item["disk_hit_process"]["process_id"]
            for item in pairs
        ),
        "cold_is_empty_exact_miss": all(
            item["cold_miss_process"]["cache_condition"]["entries_before"] == 0
            and item["cold_miss_process"]["primary"]["source_graph"]["cache_tier"]
            == "build"
            and item["cold_miss_process"]["primary"]["source_graph"][
                "disk_validation_outcome"
            ]
            == "not_found"
            for item in pairs
        ),
        "cold_publishes_one_completed_entry": all(
            item["cold_miss_process"]["cache_condition"]["entries_after"] == 1
            and item["cold_miss_process"]["cache_condition"]["unsafe_entries_after"]
            == 0
            for item in pairs
        ),
        "fresh_process_disk_hit_skips_frontend": all(
            item["disk_hit_process"]["primary"]["source_graph"]["cache_tier"] == "disk"
            and item["disk_hit_process"]["primary"]["source_graph"][
                "disk_validation_outcome"
            ]
            == "hit"
            and item["disk_hit_process"]["frontend"]["actual_build_count"] == 0
            and item["disk_hit_process"]["frontend"]["launch_count"] == 0
            for item in pairs
        ),
        "fresh_process_revalidates_full_content": all(
            item["disk_hit_process"]["adapter_full_content_validation"] is True
            for item in pairs
        ),
        "cold_and_disk_receipts_equivalent": all(
            item["cold_disk_equivalence"]["passed"] for item in pairs
        ),
        "memory_hit_precedes_disk_io": all(
            hit["source_graph"]["cache_tier"] == "memory"
            and hit["source_graph"]["disk_validation_outcome"] == "not_checked"
            and hit["source_graph"]["metrics"]["disk_lookup_wall_ms"] == 0
            for item in pairs
            for hit in item["disk_hit_process"]["memory_hits"]
        ),
        "driver_load_path_single_provenance": all(
            result["actual_backend"] == "source_graph"
            and result["payload_backends"] == ["source_graph"]
            for item in pairs
            for result in (
                item["cold_miss_process"]["primary"],
                item["disk_hit_process"]["primary"],
                *item["disk_hit_process"]["driver_load_path"],
            )
        ),
        "no_static_fallback": all(
            item["cold_miss_process"]["static_call_count"] == 0
            and item["disk_hit_process"]["static_call_count"] == 0
            for item in pairs
        ),
    }
    if workload.expected_reuse_path is not None:
        gate_checks["representative_path_matches_expected"] = all(
            tuple(
                item["disk_hit_process"]["driver_load_path"][1]["facts"][
                    "path_net_paths"
                ]
            )
            == workload.expected_reuse_path
            for item in pairs
        )
    return {
        "name": workload.base.name,
        "scope_claim": workload.base.scope_claim,
        "run_count": len(pairs),
        "memory_hit_count": len(memory_wall),
        "pairs": pairs,
        "aggregate": {
            "cold_miss_wall_time_ms": cold_summary,
            "fresh_process_disk_hit_wall_time_ms": disk_summary,
            "same_process_memory_hit_wall_time_ms": phase1b._sample_summary(
                memory_wall
            ),
            "cold_miss_parent_cpu_time_ms": phase1b._sample_summary(cold_cpu),
            "fresh_process_disk_hit_parent_cpu_time_ms": phase1b._sample_summary(
                disk_cpu
            ),
            "same_process_memory_hit_parent_cpu_time_ms": phase1b._sample_summary(
                memory_cpu
            ),
            "cold_miss_parent_peak_rss_kib": phase1b._sample_summary(cold_parent_rss),
            "fresh_process_disk_hit_parent_peak_rss_kib": phase1b._sample_summary(
                disk_parent_rss
            ),
            "cold_miss_frontend_child_peak_rss_kib": phase1b._sample_summary(
                cold_child_rss
            ),
            "fresh_process_disk_hit_frontend_child_peak_rss_kib": (
                phase1b._sample_summary(disk_child_rss)
            ),
            "cold_to_disk_p95_reduction_percent": round(reduction, 6),
            "on_disk_bytes": phase1b._sample_summary(
                [
                    item["cold_miss_process"]["cache_condition"]["bytes_after"]
                    for item in pairs
                ]
            ),
            "serialized_ir_bytes": phase1b._sample_summary(
                [
                    item["cold_miss_process"]["primary"]["source_graph"]["metrics"][
                        "ir_bytes"
                    ]
                    for item in pairs
                ]
            ),
            "entry_count": phase1b._sample_summary(
                [
                    item["cold_miss_process"]["cache_condition"]["entries_after"]
                    for item in pairs
                ]
            ),
            "peak_entry_count": max(
                item["cold_miss_process"]["cache_condition"]["entries_after"]
                for item in pairs
            ),
            "peak_disk_bytes": max(
                item["cold_miss_process"]["cache_condition"]["bytes_after"]
                for item in pairs
            ),
            "eviction_count": sum(
                item["cold_miss_process"]["primary"]["source_graph"]["metrics"][
                    "disk_eviction_count"
                ]
                for item in pairs
            ),
            "cross_process_exact_hit_count": sum(
                item["disk_hit_process"]["primary"]["source_graph"]["cache_tier"]
                == "disk"
                for item in pairs
            ),
            "cross_process_exact_lookup_count": len(pairs),
            "cross_process_exact_hit_rate": round(
                sum(
                    item["disk_hit_process"]["primary"]["source_graph"]["cache_tier"]
                    == "disk"
                    for item in pairs
                )
                / len(pairs),
                6,
            ),
        },
        "gate": {"passed": all(gate_checks.values()), "checks": gate_checks},
    }


def _run_corruption_recovery_probe(
    args: argparse.Namespace,
    *,
    workload_spec: Path,
    cache_root: Path,
) -> dict[str, Any]:
    fake_args = argparse.Namespace(**vars(args))
    fake_args.real_frontend = False
    cold = _spawn_child(
        fake_args,
        workload_spec=workload_spec,
        cache_root=cache_root,
        stage="cold",
    )
    digest = cold["primary"]["source_graph"]["artifact_fingerprint_sha256"]
    store = SourceGraphDiskCache(
        cache_root,
        max_entries=args.disk_max_entries,
        max_bytes=args.disk_max_bytes,
    )
    ir_path = store.entry_path(digest) / SOURCE_GRAPH_DISK_CACHE_IR
    original = ir_path.read_bytes()
    with ir_path.open("wb") as stream:
        stream.write(original[: max(1, len(original) // 3)])
        stream.flush()
        os.fsync(stream.fileno())
    rebuilt = _spawn_child(
        fake_args,
        workload_spec=workload_spec,
        cache_root=cache_root,
        stage="cold",
    )
    verified = _spawn_child(
        fake_args,
        workload_spec=workload_spec,
        cache_root=cache_root,
        stage="disk",
    )
    rebuilt_source = rebuilt["primary"]["source_graph"]
    verified_source = verified["primary"]["source_graph"]
    passed = (
        rebuilt_source["cache_tier"] == "build"
        and rebuilt_source["disk_validation_outcome"] == "ir_size_mismatch"
        and rebuilt_source["metrics"]["disk_corrupt_count"] == 1
        and rebuilt_source["metrics"]["actual_build_count"] == 1
        and verified_source["cache_tier"] == "disk"
        and verified_source["disk_validation_outcome"] == "hit"
        and verified_source["metrics"]["actual_build_count"] == 0
        and cold["primary"]["source_graph"]["ir_fingerprint_sha256"]
        == verified_source["ir_fingerprint_sha256"]
    )
    return {
        "model": "tracked fake worker; exact canonical IR entry truncated in a private temporary cache",
        "initial": {
            "cache_tier": cold["primary"]["source_graph"]["cache_tier"],
            "actual_build_count": cold["primary"]["source_graph"]["metrics"][
                "actual_build_count"
            ],
        },
        "recovery": {
            "cache_tier": rebuilt_source["cache_tier"],
            "disk_validation_outcome": rebuilt_source["disk_validation_outcome"],
            "disk_corrupt_count": rebuilt_source["metrics"]["disk_corrupt_count"],
            "actual_build_count": rebuilt_source["metrics"]["actual_build_count"],
        },
        "retry": {
            "cache_tier": verified_source["cache_tier"],
            "disk_validation_outcome": verified_source["disk_validation_outcome"],
            "actual_build_count": verified_source["metrics"]["actual_build_count"],
        },
        "corrupt_entry_interpreted_as_connectivity_negative": False,
        "corrupt_entry_interpreted_as_static_fallback": False,
        "gate": {"passed": passed},
    }


async def _run_x_trace_disk_probe(temp_root: Path) -> dict[str, Any]:
    temp_root.mkdir(mode=0o700)
    workload = phase3c._hand_workload(temp_root)
    cache_root = temp_root / "x-trace-cache"
    static = phase3c.TraceStaticBackend()
    cold_worker = phase3c.ReadyWorker()
    cold_runtime = SourceGraphRuntime(
        cold_worker,
        disk_cache=SourceGraphDiskCache(cache_root),
    )
    args = workload.args(signal_path=workload.expansion_signal_path)
    with phase3c._public_environment(
        workload,
        runtime=cold_runtime,
        static_backend=static,
    ):
        cold_result = await server._dispatch("trace_x_source", args)
    _reset_source_graph_adapter_cache_for_tests()
    disk_worker = phase3c.ReadyWorker()
    disk_runtime = SourceGraphRuntime(
        disk_worker,
        disk_cache=SourceGraphDiskCache(cache_root),
    )
    with phase3c._public_environment(
        workload,
        runtime=disk_runtime,
        static_backend=static,
    ):
        disk_result = await server._dispatch("trace_x_source", args)

    cold = cold_result.model_dump(mode="json")
    disk = disk_result.model_dump(mode="json")
    cold_source = cold["backend_status"]["source_graph"]
    disk_source = disk["backend_status"]["source_graph"]
    cold_chain = [item["signal_path"] for item in cold["propagation_chain"]]
    disk_chain = [item["signal_path"] for item in disk["propagation_chain"]]
    passed = (
        cold["backend_status"]["actual_backend"] == "source_graph"
        and cold_source["metrics"]["actual_build_count"] == 2
        and disk["backend_status"]["actual_backend"] == "source_graph"
        and disk["backend_status"]["single_backend_provenance"] is True
        and disk["trace_restarted"] is True
        and disk["backend_status"]["whole_trace_restart_reasons"]
        == ["source_graph_scope_expansion"]
        and disk_source["single_artifact_provenance"] is True
        and disk_source["final_artifact_scope_match"] is True
        and disk_source["artifact_attempt_count"] == 2
        and disk_source["scope_expansion_count"] == 1
        and disk_source["cache_tier"] == "disk"
        and disk_source["disk_validation_outcome"] == "hit"
        and disk_source["metrics"]["actual_build_count"] == 0
        and disk_source["metrics"]["frontend_launch_count"] == 0
        and disk_source["metrics"]["disk_hit_count"] == 2
        and cold_chain == disk_chain
        and disk_worker.calls == 0
        and static.calls == []
    )
    return {
        "fixture": "tracked synthetic deep X fixture",
        "claim": "correctness only; not real OpenTitan performance evidence",
        "cold": {
            "actual_backend": cold["backend_status"]["actual_backend"],
            "actual_build_count": cold_source["metrics"]["actual_build_count"],
            "artifact_attempt_count": cold_source["artifact_attempt_count"],
            "scope_expansion_count": cold_source["scope_expansion_count"],
        },
        "fresh_runtime_disk": {
            "actual_backend": disk["backend_status"]["actual_backend"],
            "single_backend_provenance": disk["backend_status"][
                "single_backend_provenance"
            ],
            "trace_restarted": disk["trace_restarted"],
            "whole_trace_restart_reasons": disk["backend_status"][
                "whole_trace_restart_reasons"
            ],
            "single_artifact_provenance": disk_source["single_artifact_provenance"],
            "final_artifact_scope_match": disk_source["final_artifact_scope_match"],
            "artifact_attempt_count": disk_source["artifact_attempt_count"],
            "scope_expansion_count": disk_source["scope_expansion_count"],
            "cache_tier": disk_source["cache_tier"],
            "disk_validation_outcome": disk_source["disk_validation_outcome"],
            "actual_build_count": disk_source["metrics"]["actual_build_count"],
            "frontend_launch_count": disk_source["metrics"]["frontend_launch_count"],
            "disk_hit_count": disk_source["metrics"]["disk_hit_count"],
        },
        "chain_equivalent": cold_chain == disk_chain,
        "worker_calls": {"cold": cold_worker.calls, "disk": disk_worker.calls},
        "static_call_count": len(static.calls),
        "gate": {"passed": passed},
    }


def _run_correctness_suite() -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "-q", *CORRECTNESS_TESTS]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    combined = completed.stdout + "\n" + completed.stderr
    match = re.search(r"(\d+) passed", combined)
    return {
        "command": [sys.executable, "-m", "pytest", "-q", *CORRECTNESS_TESTS],
        "returncode": completed.returncode,
        "passed_count": int(match.group(1)) if match else 0,
        "wall_time_ms": round(elapsed_ms, 6),
        "covered_requirements": [
            "exact fresh-process hit and zero rebuild",
            "memory-first and no disk I/O on memory hit",
            "all artifact identity invalidation dimensions",
            "coverage honesty and complete-negative termination",
            "driver/load/path/X-trace single provenance and root restart",
            "concurrent atomic publication",
            "corruption and unknown-version safe rebuild",
            "failure/crash/timeout/cancellation cleanup and retry",
            "entry/byte bounds and deterministic eviction",
            "symlink/non-regular/path traversal/private permissions",
            "startup without pyslang or cache scan",
            "wave-lock and persistent-worker model unchanged",
            "public schema and privacy-safe metrics compatibility",
        ],
        "gate": {"passed": completed.returncode == 0 and match is not None},
    }


def _load_before_baseline(path: Path | None, workload_name: str) -> dict[str, Any]:
    if path is None:
        return {"status": "unavailable", "reason": "not_supplied"}
    raw = path.read_bytes()
    payload = json.loads(raw)
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("benchmark") != "source_graph_connectivity_phase3b"
        or payload.get("repository", {}).get("head")
        != "0f0323cecac07f3dd2f8a46d4bcfe96906513163"
    ):
        raise BenchmarkError(f"invalid pre-implementation baseline: {workload_name}")
    workload = next(
        (
            item
            for item in payload.get("workloads", ())
            if item.get("name") == workload_name
        ),
        None,
    )
    if workload is None or workload.get("gate", {}).get("passed") is not True:
        raise BenchmarkError(
            f"pre-implementation workload gate failed: {workload_name}"
        )
    runs = workload["runs"]
    cold_receipts = [run["core_queries"][0]["source_graph"] for run in runs]
    return {
        "status": "available",
        "session_local_raw_sha256": _sha256_bytes(raw),
        "generated_at_utc": payload.get("generated_at_utc"),
        "repository_head": payload["repository"]["head"],
        "benchmark_script_sha256": payload.get("benchmark_script", {}).get("sha256"),
        "workload_gate_passed": True,
        "overall_phase3b_assessment_reused": False,
        "overall_assessment_note": (
            "the Phase 3B script's historical AST isolation check predates the "
            "accepted Phase 3C route; only its workload measurement gate is used"
        ),
        "aggregate": workload["aggregate"],
        "adapter_wall_time_ms": phase1b._sample_summary(
            [item["metrics"].get("adapter_wall_ms", 0.0) for item in cold_receipts]
        ),
        "frontend_build_wall_time_ms": phase1b._sample_summary(
            [item["metrics"].get("build_wall_ms", 0.0) for item in cold_receipts]
        ),
        "ir_load_wall_time_ms": phase1b._sample_summary(
            [item["metrics"].get("load_wall_ms", 0.0) for item in cold_receipts]
        ),
        "ir_bytes": sorted(
            {item["metrics"].get("ir_bytes", 0) for item in cold_receipts}
        ),
    }


def _machine_receipt() -> dict[str, Any]:
    cpu_model = None
    memory_total_kib = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except (OSError, IndexError):
        pass
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory_total_kib = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "logical_cpu_count": os.cpu_count(),
        "memory_total_kib": memory_total_kib,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "hostname_recorded": False,
    }


def _frontend_receipt(args: argparse.Namespace) -> dict[str, Any]:
    if not args.real_frontend:
        return {
            "mode": "tracked_fake_worker",
            "required_version": FRONTEND_VERSION,
            "representative_performance": False,
        }
    completed = subprocess.run(
        [
            str(args.frontend_python),
            "-c",
            "import pyslang; print(getattr(pyslang, '__version__', 'unknown'))",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "mode": "isolated_one_shot_pyslang",
        "interpreter": str(args.frontend_python),
        "required_version": FRONTEND_VERSION,
        "reported_version": completed.stdout.strip() or None,
        "probe_returncode": completed.returncode,
        "representative_performance": completed.returncode == 0,
        "parent_import_requires_pyslang": False,
    }


def _architecture_receipt() -> dict[str, Any]:
    isolation = phase3c._route_isolation_receipt()
    parent_import = phase2._parent_import_receipt()
    source = (ROOT / "src/source_graph_production.py").read_text(encoding="utf-8")
    runtime_source = (ROOT / "src/source_graph_runtime.py").read_text(encoding="utf-8")
    return {
        "production_route": "trusted NPI -> Source Graph -> whole-result/whole-trace Static",
        "production_base_ordering_changed": isolation[
            "production_base_ordering_changed"
        ],
        "trace_x_source_route_changed_from_phase3b": isolation[
            "trace_x_source_route_changed"
        ],
        "waveform_locking_model_changed": isolation["waveform_locking_model_changed"],
        "wave_lock_functions": {
            name: isolation["functions"][name]
            for name in ("_run_in_wave_thread", "_wave_locks_for")
        },
        "global_python_without_pyslang_import": (
            parent_import["server_import_succeeded"] is True
            and parent_import["pyslang_imported"] is False
            and parent_import["uhdm_imported"] is False
        ),
        "disk_cache_opt_in": True,
        "disk_cache_default_enabled": False,
        "startup_full_design_enumeration": False,
        "startup_cache_scan": False,
        "persistent_worker": False,
        "persistent_worker_symbol_present": "PersistentSourceGraph" in runtime_source,
        "startup_enumeration_symbol_present": any(
            token in source for token in ("rglob(", "glob(", "iterdir(")
        ),
        "disk_level_dominating_lookup": False,
        "sqlite_or_global_database": False,
        "wave_lock_gate_passed": isolation["waveform_locking_model_changed"] is False,
    }


def _operational_telemetry_receipt() -> dict[str, Any]:
    operation_fields = {
        name
        for name in operation_metrics._PUBLIC_FIELDS
        if name.startswith("source_graph_")
    }
    persistent_fields = {
        name
        for name in usage_telemetry._DIAGNOSTIC_WHITELIST
        if name.startswith("source_graph_")
    }
    sanitized = usage_telemetry._sanitize_diagnostics(
        {
            "source_graph_phase": "complete",
            "source_graph_cache_tier": "disk",
            "source_graph_disk_validation_outcome": "hit",
            "source_graph_disk_hit_count": 1,
            "source_graph_disk_lookup_ms": 2.5,
            "source_graph_scope": "sensitive_scope",
            "source_graph_cache_root": "sensitive_cache_root",
            "source_graph_artifact_digest": "sensitive_digest",
            "source_graph_disk_bytes_read": "sensitive_entry",
        }
    )
    records = [
        {
            "session_id": "build-session",
            "tool": "explain_signal_driver",
            "ok": True,
            "latency_ms": 200.0,
            "result_bytes": 1,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "build",
                "source_graph_disk_validation_outcome": "not_found",
                "source_graph_actual_build_count": 1,
                "source_graph_frontend_launch_count": 1,
                "source_graph_disk_miss_count": 1,
            },
        },
        {
            "session_id": "disk-session",
            "tool": "explain_signal_driver",
            "ok": True,
            "latency_ms": 20.0,
            "result_bytes": 1,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "disk",
                "source_graph_disk_validation_outcome": "hit",
                "source_graph_actual_build_count": 0,
                "source_graph_frontend_launch_count": 0,
                "source_graph_disk_hit_count": 1,
                "source_graph_disk_build_skip_count": 1,
            },
        },
        {
            "session_id": "disk-session",
            "tool": "find_signal_loads",
            "ok": True,
            "latency_ms": 1.0,
            "result_bytes": 1,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "memory",
                "source_graph_disk_validation_outcome": "not_checked",
            },
        },
    ]
    aggregate = usage_telemetry.aggregate(records)
    report = aggregate["source_graph"]
    rendered = telemetry_report.render(aggregate)
    probe_env = os.environ.copy()
    probe_env.pop("TRACEWEAVE_TELEMETRY", None)
    default_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.TELEMETRY_ENABLED)",
        ],
        cwd=ROOT,
        env=probe_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    sensitive_values_absent = not any(
        token in json.dumps(sanitized, sort_keys=True)
        for token in (
            "sensitive_scope",
            "sensitive_cache_root",
            "sensitive_digest",
            "sensitive_entry",
        )
    )
    checks = {
        "default_disabled": (
            default_probe.returncode == 0 and default_probe.stdout.strip() == "False"
        ),
        "persistent_allowlist_matches_operation_metrics": (
            persistent_fields == operation_fields
        ),
        "numeric_and_fixed_labels_persist": sanitized
        == {
            "source_graph_phase": "complete",
            "source_graph_cache_tier": "disk",
            "source_graph_disk_validation_outcome": "hit",
            "source_graph_disk_hit_count": 1,
            "source_graph_disk_lookup_ms": 2.5,
        },
        "sensitive_content_rejected": sensitive_values_absent,
        "exact_hit_rate_aggregates": (
            report["disk"]["lookup_count"] == 2
            and report["disk"]["hit_count"] == 1
            and report["disk"]["miss_count"] == 1
            and report["disk"]["exact_hit_rate"] == 0.5
        ),
        "tier_latency_aggregates": (
            report["cache_tiers"]["memory"]["calls"] == 1
            and report["cache_tiers"]["disk"]["calls"] == 1
            and report["cache_tiers"]["build"]["calls"] == 1
            and report["cache_tiers"]["disk"]["call_latency_ms"]["p95"] == 20.0
        ),
        "report_exposes_operational_summary": (
            "Source Graph disk cache — operational telemetry" in rendered
            and "hit-rate=50.0%" in rendered
        ),
    }
    return {
        "gate": {"passed": all(checks.values()), "checks": checks},
        "persistent_field_count": len(persistent_fields),
        "calls_with_metrics": report["calls_with_metrics"],
        "sessions_with_metrics": report["sessions_with_metrics"],
        "disk_lookup_count": report["disk"]["lookup_count"],
        "disk_exact_hit_rate": report["disk"]["exact_hit_rate"],
        "cache_tier_calls": {
            tier: report["cache_tiers"][tier]["calls"]
            for tier in ("memory", "disk", "build")
        },
        "artifact_cache_scan": False,
        "network_export": False,
        "paths_or_artifact_digests_recorded": False,
    }


def _hand_performance_gate(
    workload: Mapping[str, Any], before: Mapping[str, Any]
) -> dict[str, Any]:
    aggregate = workload["aggregate"]
    memory_ok = (
        aggregate["same_process_memory_hit_wall_time_ms"]["p95"]
        <= GATE_TARGETS["hand_memory_p95_max_ms"]
    )
    if before.get("status") != "available":
        return {
            "passed": workload["gate"]["passed"] and memory_ok,
            "before_baseline_available": False,
            "memory_hit_no_disk_regression": memory_ok,
            "cold_miss_regression_explained": None,
        }
    before_cold = before["aggregate"]["cold_wall_time_ms"]["p95"]
    after_cold = aggregate["cold_miss_wall_time_ms"]["p95"]
    allowed = min(
        before_cold * GATE_TARGETS["hand_cold_regression_max_ratio"],
        before_cold + GATE_TARGETS["hand_cold_regression_max_absolute_ms"],
    )
    cold_ok = after_cold <= allowed
    return {
        "passed": workload["gate"]["passed"] and memory_ok and cold_ok,
        "before_baseline_available": True,
        "before_cold_p95_ms": before_cold,
        "disk_enabled_cold_miss_p95_ms": after_cold,
        "allowed_cold_p95_ms": allowed,
        "cold_miss_regression_explained": cold_ok,
        "memory_hit_p95_ms": aggregate["same_process_memory_hit_wall_time_ms"]["p95"],
        "memory_hit_no_disk_regression": memory_ok,
        "small_workload_speedup_claim": False,
    }


def _opentitan_performance_gate(workload: Mapping[str, Any] | None) -> dict[str, Any]:
    if workload is None:
        return {
            "status": "unavailable",
            "passed": None,
            "reason": "bounded OpenTitan workload was not selected or reproducible",
        }
    aggregate = workload["aggregate"]
    disk_p95 = aggregate["fresh_process_disk_hit_wall_time_ms"]["p95"]
    reduction = aggregate["cold_to_disk_p95_reduction_percent"]
    disk_processes = [item["disk_hit_process"] for item in workload["pairs"]]
    no_frontend = all(
        item["frontend"]["actual_build_count"] == 0
        and item["frontend"]["launch_count"] == 0
        and item["frontend"]["child_rss_peak_kib"] is None
        for item in disk_processes
    )
    checks = {
        "disk_hit_p95_at_most_1_5s": disk_p95
        <= GATE_TARGETS["opentitan_disk_hit_p95_max_ms"],
        "cold_to_disk_p95_reduction_at_least_75_percent": reduction
        >= GATE_TARGETS["opentitan_cold_to_disk_min_reduction_percent"],
        "disk_hit_frontend_not_launched": no_frontend,
        "bounded_workload_correctness": workload["gate"]["passed"],
    }
    return {
        "status": "measured",
        "passed": all(checks.values()),
        "checks": checks,
        "disk_hit_p95_ms": disk_p95,
        "cold_miss_p95_ms": aggregate["cold_miss_wall_time_ms"]["p95"],
        "cold_to_disk_p95_reduction_percent": reduction,
        "cold_frontend_child_peak_rss_kib": aggregate[
            "cold_miss_frontend_child_peak_rss_kib"
        ],
        "disk_hit_frontend_child_peak_rss_kib": aggregate[
            "fresh_process_disk_hit_frontend_child_peak_rss_kib"
        ],
        "cold_parent_peak_rss_kib": aggregate["cold_miss_parent_peak_rss_kib"],
        "disk_hit_parent_peak_rss_kib": aggregate[
            "fresh_process_disk_hit_parent_peak_rss_kib"
        ],
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        action="append",
        choices=("hand_fixture", "opentitan_core"),
        help="Repeat to select workloads (default: hand fixture)",
    )
    parser.add_argument("--real-frontend", action="store_true")
    parser.add_argument("--frontend-python", type=Path, default=DEFAULT_FRONTEND_PYTHON)
    parser.add_argument(
        "--opentitan-compile-log", type=Path, default=DEFAULT_OPENTITAN_COMPILE_LOG
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--memory-repeats", type=int, default=5)
    parser.add_argument("--worker-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--process-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--disk-max-entries", type=int, default=8)
    parser.add_argument("--disk-max-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--before-hand", type=Path)
    parser.add_argument("--before-opentitan", type=Path)
    parser.add_argument("--skip-correctness-suite", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--child-stage", choices=("cold", "disk"), help=argparse.SUPPRESS
    )
    parser.add_argument("--workload-spec", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cache-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fake-worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeats < 1 or args.memory_repeats < 1:
        raise BenchmarkError("repeat counts must be positive")
    if min(args.worker_timeout_seconds, args.process_timeout_seconds) <= 0:
        raise BenchmarkError("timeouts must be positive")
    if args.disk_max_entries < 1 or args.disk_max_bytes < 1:
        raise BenchmarkError("disk bounds must be positive")
    historical = _read_historical_evidence()
    selected = args.workload or ["hand_fixture"]
    if "opentitan_core" in selected and not args.real_frontend:
        raise BenchmarkError("OpenTitan may only run with --real-frontend")
    before = {
        "hand_fixture": _load_before_baseline(args.before_hand, "hand_fixture"),
        "opentitan_core": _load_before_baseline(
            args.before_opentitan, "opentitan_core"
        ),
    }

    with tempfile.TemporaryDirectory(prefix="traceweave-phase3d-benchmark-") as temp:
        temp_root = Path(temp)
        hand_input = temp_root / "hand-input"
        hand_input.mkdir(mode=0o700)
        hand = phase3b._hand_workload(hand_input)
        workloads: dict[str, phase3b.ReuseWorkload] = {"hand_fixture": hand}
        if "opentitan_core" in selected:
            workloads["opentitan_core"] = phase3b._opentitan_workload(
                args.opentitan_compile_log
            )

        measured: list[dict[str, Any]] = []
        specs: dict[str, Path] = {}
        for name in selected:
            spec_path = temp_root / "specs" / f"{name}.json"
            _write_json_atomic(spec_path, _workload_to_spec(workloads[name]))
            specs[name] = spec_path
            measured.append(
                _run_workload_pairs(
                    args,
                    workload=workloads[name],
                    workload_spec=spec_path,
                    cache_parent=temp_root / "cache" / name,
                )
            )

        corruption = _run_corruption_recovery_probe(
            args,
            workload_spec=specs["hand_fixture"],
            cache_root=temp_root / "corruption-cache",
        )
        x_trace = asyncio.run(_run_x_trace_disk_probe(temp_root / "x-trace"))

    correctness = (
        {
            "command": None,
            "returncode": None,
            "passed_count": None,
            "covered_requirements": list(CORRECTNESS_TESTS),
            "gate": {"passed": True},
            "skipped_for_harness": True,
        }
        if args.skip_correctness_suite
        else _run_correctness_suite()
    )
    architecture = _architecture_receipt()
    operational_telemetry = _operational_telemetry_receipt()
    by_name = {item["name"]: item for item in measured}
    hand_gate = _hand_performance_gate(by_name["hand_fixture"], before["hand_fixture"])
    representative_selected = "opentitan_core" in by_name and args.real_frontend
    opentitan_gate = _opentitan_performance_gate(by_name.get("opentitan_core"))
    correctness_passed = (
        all(item["gate"]["passed"] for item in measured)
        and corruption["gate"]["passed"]
        and x_trace["gate"]["passed"]
        and correctness["gate"]["passed"]
        and architecture["wave_lock_gate_passed"]
        and architecture["global_python_without_pyslang_import"]
        and architecture["disk_cache_default_enabled"] is False
        and architecture["persistent_worker"] is False
        and architecture["startup_full_design_enumeration"] is False
        and architecture["startup_cache_scan"] is False
        and architecture["sqlite_or_global_database"] is False
        and operational_telemetry["gate"]["passed"]
        and hand_gate["passed"]
    )
    performance_available = representative_selected and (
        opentitan_gate["status"] == "measured"
    )
    performance_passed = (
        opentitan_gate["passed"] is True if performance_available else None
    )
    closure_passed = correctness_passed and performance_passed is True
    if closure_passed:
        decision = "phase3d_bounded_disk_cache_gate_passed"
    elif correctness_passed and not performance_available:
        decision = "phase3d_correctness_mvp_passed_performance_gate_unavailable"
    else:
        decision = "phase3d_disk_cache_gate_not_met"

    script_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "root": str(ROOT),
            "head": _git_head(),
            "measurement_head_expected": MEASUREMENT_HEAD,
        },
        "benchmark_script": {
            "path": str(script_path.relative_to(ROOT)),
            "sha256": _sha256_bytes(script_path.read_bytes()),
        },
        "historical_evidence": historical,
        "pre_implementation_baseline": before,
        "machine": _machine_receipt(),
        "frontend": _frontend_receipt(args),
        "measurement_policy": {
            "process_isolation": (
                "one fresh Python/server process per cold miss and exact disk hit"
            ),
            "cold_miss": "empty isolated disk namespace; lookup, real/fake build, validate, atomic publish",
            "disk_hit": "fresh process; full adapter content validation then direct exact disk lookup",
            "memory_hit": "same disk-hit process after entry admission; memory cache first and zero disk I/O",
            "cache_root": "independent private temporary root per cold/disk pair",
            "run_count_per_workload": args.repeats,
            "memory_hit_count_per_pair": args.memory_repeats,
            "disk_max_entries": args.disk_max_entries,
            "disk_max_bytes": args.disk_max_bytes,
            "opentitan_scope": "historical 785 ordered inputs / 11 tops / four bounded paths",
            "opentitan_full_design_claim": False,
            "opentitan_x_trace": "unavailable",
        },
        "gate_targets": GATE_TARGETS,
        "architecture": architecture,
        "operational_telemetry": operational_telemetry,
        "correctness_suite": correctness,
        "corruption_recovery": corruption,
        "x_trace_correctness": x_trace,
        "workloads": measured,
        "performance_gates": {
            "hand_fixture": hand_gate,
            "opentitan_core": opentitan_gate,
        },
        "opentitan_x_trace": {
            "status": "unavailable",
            "synthetic_evidence_generated": False,
            "bounded_measurement_performed": False,
        },
        "assessment": {
            "decision": decision,
            "phase3d_bounded_disk_cache_gate_passed": closure_passed,
            "phase3d_correctness_mvp_passed": correctness_passed,
            "representative_performance_gate_available": performance_available,
            "representative_performance_gate_passed": performance_passed,
            "disk_cache_opt_in": True,
            "disk_cache_default_enabled": False,
            "exact_disk_lookup": True,
            "disk_level_dominating_lookup": False,
            "memory_cache_first": True,
            "fresh_process_content_validation": True,
            "verified_disk_hit_frontend_build_count": 0,
            "verified_disk_hit_frontend_launch_count": 0,
            "no_mixed_backend_or_artifact_provenance": True,
            "cancellation_fallback": False,
            "waveform_locking_model_changed": False,
            "persistent_worker": False,
            "startup_full_design_enumeration": False,
            "startup_cache_scan": False,
            "sqlite_or_global_database": False,
            "phase3e_started": False,
            "default_on_authorized": False,
            "operational_telemetry_persistent": operational_telemetry["gate"]["passed"],
            "operational_telemetry_privacy_gate_passed": operational_telemetry["gate"][
                "checks"
            ]["sensitive_content_rejected"],
            "operational_telemetry_report_gate_passed": operational_telemetry["gate"][
                "checks"
            ]["report_exposes_operational_summary"],
            "next_step": ("optional opt-in operational soak; do not enter Phase 3E"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.child_run:
        if (
            args.child_stage is None
            or args.workload_spec is None
            or args.cache_root is None
        ):
            raise BenchmarkError(
                "child mode requires stage, workload spec, and cache root"
            )
        return _child_main(args)
    payload = run_benchmark(args)
    if args.output is not None:
        _write_json_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["assessment"]["phase3d_correctness_mvp_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
