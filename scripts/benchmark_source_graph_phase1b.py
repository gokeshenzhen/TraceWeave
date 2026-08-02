#!/usr/bin/env python3
"""Benchmark the internal Phase 1B scoped/on-demand Source Graph runtime.

This harness is intentionally separate from the Phase 0A/0B/1A evidence.  It
uses the internal build contract and process-isolated runtime directly; it does
not register a backend, alter production routing, or expose a public schema.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Protocol, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_source_graph_phase1a as phase1a  # noqa: E402
from src.connectivity_ir import CoverageStatus  # noqa: E402
from src.connectivity_query import QueryStatus  # noqa: E402
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME  # noqa: E402
from src.source_graph_contract import (  # noqa: E402
    BoundaryMode,
    CompileInputManifest,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    RequestedCone,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphIdentity,
    compute_source_graph_build_key,
)
from src.source_graph_runtime import (  # noqa: E402
    CacheDisposition,
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    SourceGraphPrepareOutcome,
    SourceGraphRuntime,
    SourceGraphWorkerRunner,
)


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase1b"
FRONTEND_VERSION = "11.0.0"
DEFAULT_FRONTEND_PYTHON = Path("/tmp/traceweave-phase0b-pyslang-11.0.0/bin/python")
DEFAULT_PHASE1A_EVIDENCE = (
    ROOT / "benchmarks" / "source_graph_connectivity_phase1a_results.json"
)
PHASE1A_EVIDENCE_SHA256 = (
    "c7310560a1e89e19694a83d41e24a645578b747585c68df546a20937f3fa42e2"
)
WORKLOAD_NAMES = phase1a.WORKLOAD_NAMES
GATE_TARGETS = {
    "opentitan_cold_prepare_p50_max_ms": 15_000.0,
    "warm_prepare_query_p95_max_ms": 100.0,
    "peak_rss_max_kib": 2_621_440,
    "same_key_actual_build_count": 1,
}
REQUIRED_OPENTITAN_EXCLUSIONS = (
    "bind_semantics_incomplete",
    "dpi_runtime_not_modeled",
    "procedural_force_not_modeled",
    "protected_payload_not_modeled",
    "uvm_dynamic_connectivity_not_modeled",
)


class BenchmarkError(RuntimeError):
    """A benchmark input or internal measurement contract is invalid."""


class RunnerFactory(Protocol):
    def __call__(self) -> SourceGraphWorkerRunner: ...


@dataclass(frozen=True)
class FailureRunnerFactories:
    crash: RunnerFactory
    timeout: RunnerFactory
    cancellation: RunnerFactory


class _FirstThenRunner:
    """Use one runner for the failure attempt and another for its retry."""

    def __init__(
        self,
        first: SourceGraphWorkerRunner,
        then: SourceGraphWorkerRunner,
    ) -> None:
        self._first = first
        self._then = then
        self._calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        self._calls += 1
        runner = self._first if self._calls == 1 else self._then
        return await runner.run(
            request,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        action="append",
        choices=WORKLOAD_NAMES,
        help="Workload to run; repeat to select multiple (default: tracked fixtures)",
    )
    parser.add_argument(
        "--frontend-python",
        type=Path,
        default=DEFAULT_FRONTEND_PYTHON,
        help="Pinned CPython 3.11 interpreter containing pyslang 11.0.0",
    )
    parser.add_argument("--cold-repeats", type=int, default=3)
    parser.add_argument("--warm-repeats", type=int, default=100)
    parser.add_argument("--concurrent-requests", type=int, default=4)
    parser.add_argument("--worker-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--failure-timeout-seconds", type=float, default=0.001)
    parser.add_argument("--cancellation-delay-seconds", type=float, default=0.01)
    parser.add_argument("--real-compile-log", type=Path)
    parser.add_argument(
        "--real-simulator", choices=("auto", "vcs", "xcelium"), default="auto"
    )
    parser.add_argument("--real-waveform", type=Path)
    parser.add_argument("--real-env", action="append", default=[])
    parser.add_argument(
        "--opentitan-oracle",
        type=Path,
        default=phase1a.DEFAULT_OPENTITAN_ORACLE,
    )
    parser.add_argument(
        "--phase1a-evidence",
        type=Path,
        default=DEFAULT_PHASE1A_EVIDENCE,
    )
    parser.add_argument("--output", type=Path)
    return parser


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        value = ordered[lower]
    else:
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
    return round(value, 6)


def _sample_summary(values: Sequence[float]) -> dict[str, Any]:
    samples = [float(value) for value in values]
    return {
        "count": len(samples),
        "min": round(min(samples), 6) if samples else None,
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "max": round(max(samples), 6) if samples else None,
    }


def _read_phase1a_baseline(path: Path) -> dict[str, Any]:
    actual_hash = _sha256_file(path)
    if actual_hash != PHASE1A_EVIDENCE_SHA256:
        raise BenchmarkError(
            "Phase 1A evidence hash mismatch; stop instead of regenerating baseline"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": "1.0",
        "benchmark": "source_graph_connectivity_phase1a",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise BenchmarkError("Phase 1A evidence schema or benchmark mismatch")
    if payload.get("assessment", {}).get("decision") != (
        "go_for_production_integration_review"
    ):
        raise BenchmarkError("Phase 1A evidence assessment is no longer accepted")
    if payload.get("assessment", {}).get("production_route_changed") is not False:
        raise BenchmarkError("Phase 1A evidence unexpectedly changed production route")
    return {
        "path": str(path),
        "sha256": actual_hash,
        "measurement_head": payload.get("repository", {}).get("head"),
        "assessment": payload.get("assessment", {}).get("decision"),
        "production_route_changed": False,
        "opentitan_reference": next(
            (
                {
                    "cold_prepare_p50_ms": item["aggregate"]["cold_worker_wall_ms"][
                        "p50"
                    ],
                    "driver_p95_ms": item["aggregate"]["warm_queries"]["driver"][
                        "wall_latency_ms"
                    ]["p95"],
                    "load_p95_ms": item["aggregate"]["warm_queries"]["load"][
                        "wall_latency_ms"
                    ]["p95"],
                    "peak_rss_p50_kib": item["aggregate"]["peak_rss_kib"]["p50"],
                    "coverage_claim": "focused_scoped_inconclusive",
                }
                for item in payload.get("workloads", ())
                if item.get("name") == "opentitan_core"
            ),
            None,
        ),
    }


def _translated_manifest_parts(
    spec: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    arguments = tuple(str(item) for item in spec["frontend_args"])
    declared_sources = {
        str(path) for path in spec["source_facts"].get("source_paths", ())
    }
    ordered_inputs: list[str] = []
    ordered_options: list[str] = []
    ordered_tops: list[str] = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in declared_sources:
            ordered_inputs.append(item)
        elif item == "--top":
            if index + 1 >= len(arguments):
                raise BenchmarkError("translated --top is missing its value")
            ordered_tops.append(arguments[index + 1])
            index += 1
        elif item.startswith("--top="):
            ordered_tops.append(item.split("=", 1)[1])
        else:
            ordered_options.append(item)
        index += 1

    if set(ordered_inputs) != declared_sources or len(ordered_inputs) != len(
        declared_sources
    ):
        raise BenchmarkError(
            "translated source order cannot be reconciled with source facts"
        )
    if not ordered_tops or str(spec["top"]) not in ordered_tops:
        raise BenchmarkError("translated ordered tops omit the selected scope top")
    return tuple(ordered_inputs), tuple(ordered_options), tuple(ordered_tops)


def _compile_input_fingerprint(
    ordered_inputs: Sequence[str],
    ordered_options: Sequence[str],
    ordered_tops: Sequence[str],
) -> str | None:
    records: list[dict[str, Any]] = []
    for item in ordered_inputs:
        path = Path(item)
        if not path.is_file():
            return None
        records.append(
            {
                "path": item,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return _sha256_json(
        {
            "ordered_inputs": records,
            "ordered_options": list(ordered_options),
            "ordered_tops": list(ordered_tops),
        }
    )


def _scope_for_spec(spec: Mapping[str, Any]) -> SourceGraphBuildScope:
    name = str(spec["workload"])
    paths = tuple(str(path) for path in spec["expected_instance_paths"])
    if name == "deep_x_npi":
        target_signal = "uart_deep_x_tb.apb_prdata[7:0]"
        ancestors = ("uart_deep_x_tb",)
        cone_paths = paths
    elif name == "hand_fixture":
        target_signal = "sg_top.bus.data[15:8]"
        ancestors = ("sg_top", "sg_top.bus")
        cone_paths = paths
    elif name == "opentitan_core":
        target_signal = "tb.dut.top_earlgrey.u_rv_core_ibex.fatal_intg_event"
        ancestors = phase1a.OPENTITAN_FOCUS_PATHS[:4]
        cone_paths = phase1a.OPENTITAN_ASSIGNMENT_PATHS
    else:
        raise BenchmarkError(f"unsupported workload scope: {name}")
    exclusions = REQUIRED_OPENTITAN_EXCLUSIONS if name == "opentitan_core" else ()
    return SourceGraphBuildScope(
        design=f"phase1b_{name}",
        top=str(spec["top"]),
        target=ConnectivityTarget(
            operation=QueryOperation.DRIVER,
            signal_path=target_signal,
        ),
        hierarchy_ancestors=tuple(ancestors),
        requested_cone=RequestedCone(
            operation=QueryOperation.DRIVER,
            max_hops=64,
            instance_paths=tuple(cone_paths),
            cross_instance_boundaries=True,
            stop_at_sequential=True,
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=paths,
            objective_exclusions=exclusions,
        ),
    )


def build_request(spec: Mapping[str, Any]) -> SourceGraphBuildRequest:
    inputs, options, tops = _translated_manifest_parts(spec)
    fingerprint = _compile_input_fingerprint(inputs, options, tops)
    missing = tuple(spec["source_facts"].get("missing_sources", ()))
    return SourceGraphBuildRequest(
        identity=SourceGraphIdentity(
            compile_inputs=CompileInputManifest(
                fingerprint=fingerprint,
                ordered_inputs=inputs,
                ordered_options=options,
                ordered_tops=tops,
                inputs_complete=fingerprint is not None and not missing,
                options_complete=True,
                tops_complete=True,
            ),
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version=FRONTEND_VERSION,
        ),
        scope=_scope_for_spec(spec),
    )


def build_workload_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    # The accepted Phase 1A helper owns compile-log translation and manual
    # correctness oracles.  Calling it here does not import optional pyslang or
    # execute a frontend build.
    phase1a_args = argparse.Namespace(
        workload=args.workload,
        query_repeats=args.warm_repeats,
        real_compile_log=args.real_compile_log,
        real_simulator=args.real_simulator,
        real_waveform=args.real_waveform,
        real_env=args.real_env,
        opentitan_oracle=args.opentitan_oracle,
    )
    return phase1a.build_workload_specs(phase1a_args)


def _compact_coverage(outcome: SourceGraphPrepareOutcome) -> dict[str, Any] | None:
    if outcome.entry is None:
        return None
    coverage = outcome.entry.ir.coverage
    return {
        "status": coverage.status.value,
        "files_total": coverage.files_total,
        "files_projected": coverage.files_projected,
        "diagnostic_count": coverage.diagnostic_count,
        "blocking_diagnostic_count": coverage.blocking_diagnostic_count,
        "gap_count": len(coverage.gaps),
        "by_code": dict(sorted(Counter(gap.code for gap in coverage.gaps).items())),
        "scope_receipt": outcome.entry.scope_receipt.to_dict(),
    }


def _query_summary(query_results: Sequence[tuple[dict[str, Any], Any]]) -> list[dict]:
    return [
        {
            "name": query["name"],
            "operation": query["operation"],
            "status": result.status.value,
            "coverage_status": result.coverage_status.value,
            "match_confidences": sorted(
                {match.confidence.value for match in result.matches}
            ),
            "match_count": len(result.matches),
        }
        for query, result in query_results
    ]


async def _measure_warm_prepare_queries(
    runtime: SourceGraphRuntime,
    request: SourceGraphBuildRequest,
    query_results: Sequence[tuple[dict[str, Any], Any]],
    repeats: int,
) -> tuple[dict[str, Any], dict[str, dict[str, list[float]]]]:
    internal: dict[str, dict[str, list[float]]] = {}
    public: dict[str, Any] = {}
    for operation in ("driver", "load"):
        successful = [
            query
            for query, result in query_results
            if query["operation"] == operation and result.status is QueryStatus.FOUND
        ]
        wall_samples: list[float] = []
        cpu_samples: list[float] = []
        cache_dispositions: Counter[str] = Counter()
        actual_build_count = 0
        for _ in range(repeats):
            for query in successful:
                wall_started = time.perf_counter_ns()
                cpu_started = time.process_time_ns()
                prepared = await runtime.prepare(request)
                if prepared.status is not PrepareStatus.READY or prepared.entry is None:
                    raise BenchmarkError(
                        f"warm {operation} prepare returned {prepared.status.value}"
                    )
                current = (
                    prepared.entry.query_engine.query_driver(query["signal"])
                    if operation == "driver"
                    else prepared.entry.query_engine.query_loads(query["signal"])
                )
                cpu_samples.append((time.process_time_ns() - cpu_started) / 1_000_000)
                wall_samples.append((time.perf_counter_ns() - wall_started) / 1_000_000)
                if current.status is not QueryStatus.FOUND:
                    raise BenchmarkError(
                        f"warm {operation} query stopped returning found"
                    )
                cache_dispositions[prepared.metrics.cache_disposition.value] += 1
                actual_build_count += prepared.metrics.actual_build_count
        internal[operation] = {"wall": wall_samples, "cpu": cpu_samples}
        public[operation] = {
            "successful_query_count": len(successful),
            "repeat_count": repeats,
            "wall_latency_ms": _sample_summary(wall_samples),
            "cpu_latency_ms": _sample_summary(cpu_samples),
            "cache_dispositions": dict(sorted(cache_dispositions.items())),
            "actual_build_count": actual_build_count,
            "sample_policy": (
                "combined in-memory prepare plus successful driver/load query"
            ),
        }
    return public, internal


async def _measure_cold_run(
    spec: Mapping[str, Any],
    request: SourceGraphBuildRequest,
    runner: SourceGraphWorkerRunner,
    *,
    timeout_seconds: float,
    warm_repeats: int,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(runner)
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    outcome = await runtime.prepare(request, timeout_seconds=timeout_seconds)
    parent_wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    parent_cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    result: dict[str, Any] = {
        "status": outcome.status.value,
        "fallback_used": outcome.fallback_used,
        "prepare": outcome.to_receipt(),
        "parent_observed": {
            "wall_time_ms": round(parent_wall_ms, 6),
            "cpu_time_ms": round(parent_cpu_ms, 6),
        },
        "runtime_stats": runtime.stats_snapshot(),
    }
    if outcome.status is not PrepareStatus.READY or outcome.entry is None:
        return result

    correctness, query_results = phase1a._run_correctness(
        outcome.entry.ir,
        outcome.entry.query_engine,
        spec,
    )
    warm, warm_samples = await _measure_warm_prepare_queries(
        runtime,
        request,
        query_results,
        warm_repeats,
    )
    entry = outcome.entry
    result.update(
        {
            "correctness": correctness,
            "queries": _query_summary(query_results),
            "coverage": _compact_coverage(outcome),
            "ir": {
                "version": entry.ir.ir_version,
                "stats": entry.ir.stats(),
                "fingerprint_sha256": entry.ir_fingerprint_sha256,
                "ir_bytes": entry.ir_bytes,
                "cache_bytes": entry.cache_bytes,
            },
            "warm_prepare_queries": warm,
            "runtime_stats_after_warm": runtime.stats_snapshot(),
            "_warm_samples": warm_samples,
        }
    )
    return result


async def _measure_concurrent_same_key(
    request: SourceGraphBuildRequest,
    runner: SourceGraphWorkerRunner,
    *,
    request_count: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(runner)
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    outcomes = await asyncio.gather(
        *(
            runtime.prepare(request, timeout_seconds=timeout_seconds)
            for _ in range(request_count)
        )
    )
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    stats = runtime.stats_snapshot()
    return {
        "request_count": request_count,
        "status_counts": dict(Counter(item.status.value for item in outcomes)),
        "cache_dispositions": dict(
            Counter(item.metrics.cache_disposition.value for item in outcomes)
        ),
        "flight_dispositions": dict(
            Counter(item.metrics.flight_disposition.value for item in outcomes)
        ),
        "actual_build_count": stats["actual_build_count"],
        "coalesced_waiter_count": stats["coalesced_waiter_count"],
        "cache_entry_count": stats["cache_entry_count"],
        "fallback_used": any(item.fallback_used for item in outcomes),
        "fingerprints": sorted(
            {
                item.entry.ir_fingerprint_sha256
                for item in outcomes
                if item.entry is not None
            }
        ),
        "coverage_statuses": sorted(
            {
                item.coverage_status.value
                for item in outcomes
                if item.coverage_status is not None
            }
        ),
        "wall_time_ms": round(wall_ms, 6),
        "cpu_time_ms": round(cpu_ms, 6),
        "runtime_stats": stats,
    }


async def _measure_failure_case(
    request: SourceGraphBuildRequest,
    runner: SourceGraphWorkerRunner,
    *,
    kind: str,
    worker_timeout_seconds: float,
    failure_timeout_seconds: float,
    cancellation_delay_seconds: float,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(runner)
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    if kind == "timeout":
        failed = await runtime.prepare(
            request,
            timeout_seconds=failure_timeout_seconds,
        )
    elif kind == "cancellation":
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            runtime.prepare(
                request,
                timeout_seconds=worker_timeout_seconds,
                cancel_event=cancel_event,
            )
        )
        await asyncio.sleep(cancellation_delay_seconds)
        cancel_event.set()
        failed = await task
    elif kind == "crash":
        failed = await runtime.prepare(
            request,
            timeout_seconds=worker_timeout_seconds,
        )
    else:
        raise BenchmarkError(f"unsupported failure probe: {kind}")
    failure_stats = runtime.stats_snapshot()
    retry = await runtime.prepare(
        request,
        timeout_seconds=worker_timeout_seconds,
    )
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    return {
        "kind": kind,
        "failure": failed.to_receipt(),
        "cache_entry_count_after_failure": failure_stats["cache_entry_count"],
        "inflight_count_after_failure": failure_stats["inflight_count"],
        "retry": retry.to_receipt(),
        "cache_entry_count_after_retry": runtime.stats_snapshot()["cache_entry_count"],
        "wall_time_ms": round(wall_ms, 6),
        "cpu_time_ms": round(cpu_ms, 6),
    }


async def _measure_failure_probes(
    request: SourceGraphBuildRequest,
    factories: FailureRunnerFactories,
    *,
    worker_timeout_seconds: float,
    failure_timeout_seconds: float,
    cancellation_delay_seconds: float,
) -> dict[str, Any]:
    results = {}
    for kind, factory in (
        ("crash", factories.crash),
        ("timeout", factories.timeout),
        ("cancellation", factories.cancellation),
    ):
        results[kind] = await _measure_failure_case(
            request,
            factory(),
            kind=kind,
            worker_timeout_seconds=worker_timeout_seconds,
            failure_timeout_seconds=failure_timeout_seconds,
            cancellation_delay_seconds=cancellation_delay_seconds,
        )
    return results


def _aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ready = [run for run in runs if run.get("status") == PrepareStatus.READY.value]
    fingerprints = [run["ir"]["fingerprint_sha256"] for run in ready]
    aggregate: dict[str, Any] = {
        "requested_run_count": len(runs),
        "ready_run_count": len(ready),
        "status_counts": dict(Counter(str(run.get("status")) for run in runs)),
        "correctness_all_runs": bool(ready)
        and all(run["correctness"]["all_passed"] for run in ready),
        "ir_fingerprint_stable": bool(fingerprints) and len(set(fingerprints)) == 1,
        "ir_fingerprints": sorted(set(fingerprints)),
        "cold_prepare_wall_ms": _sample_summary(
            [run["parent_observed"]["wall_time_ms"] for run in ready]
        ),
        "worker_cpu_ms": _sample_summary(
            [
                run["prepare"]["metrics"]["worker_cpu_ms"]
                for run in ready
                if "worker_cpu_ms" in run["prepare"]["metrics"]
            ]
        ),
        "peak_rss_kib": _sample_summary(
            [
                run["prepare"]["metrics"]["rss_peak_kib"]
                for run in ready
                if "rss_peak_kib" in run["prepare"]["metrics"]
            ]
        ),
        "ir_bytes": _sample_summary([run["ir"]["ir_bytes"] for run in ready]),
        "cache_bytes": _sample_summary([run["ir"]["cache_bytes"] for run in ready]),
        "coverage_statuses": sorted({run["coverage"]["status"] for run in ready}),
        "blocking_diagnostic_counts": sorted(
            {run["coverage"]["blocking_diagnostic_count"] for run in ready}
        ),
        "coverage_gap_codes": sorted(
            {code for run in ready for code in run["coverage"]["by_code"]}
        ),
        "cold_cache_dispositions": dict(
            Counter(run["prepare"]["metrics"]["cache_disposition"] for run in ready)
        ),
        "cold_actual_build_counts": sorted(
            {run["prepare"]["metrics"]["actual_build_count"] for run in ready}
        ),
    }
    warm: dict[str, Any] = {}
    for operation in ("driver", "load"):
        walls = [
            sample
            for run in ready
            for sample in run["_warm_samples"][operation]["wall"]
        ]
        cpus = [
            sample for run in ready for sample in run["_warm_samples"][operation]["cpu"]
        ]
        warm[operation] = {
            "wall_latency_ms": _sample_summary(walls),
            "cpu_latency_ms": _sample_summary(cpus),
            "all_memory_hits": bool(ready)
            and all(
                set(run["warm_prepare_queries"][operation]["cache_dispositions"])
                == {CacheDisposition.HIT_EXACT.value}
                and run["warm_prepare_queries"][operation]["actual_build_count"] == 0
                for run in ready
            ),
            "sample_policy": "combined in-memory prepare plus successful query",
        }
    aggregate["warm_prepare_queries"] = warm
    aggregate["representative_query_confidences"] = sorted(
        {
            confidence
            for run in ready
            for query in run["queries"]
            for confidence in query["match_confidences"]
        }
    )
    return aggregate


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    expected: Any,
    actual: Any,
) -> None:
    checks.append(
        {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}
    )


def _workload_gate(
    name: str,
    aggregate: Mapping[str, Any],
    concurrent: Mapping[str, Any],
    cold_repeats: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "all_cold_runs_ready",
        aggregate["ready_run_count"] == cold_repeats,
        expected=cold_repeats,
        actual=aggregate["ready_run_count"],
    )
    _check(
        checks,
        "correctness_all_runs",
        bool(aggregate["correctness_all_runs"]),
        expected=True,
        actual=aggregate["correctness_all_runs"],
    )
    _check(
        checks,
        "stable_ir_fingerprint",
        bool(aggregate["ir_fingerprint_stable"]),
        expected=True,
        actual=aggregate["ir_fingerprint_stable"],
    )
    _check(
        checks,
        "cold_prepare_is_fresh_miss",
        aggregate["cold_cache_dispositions"]
        == {CacheDisposition.MISS.value: cold_repeats}
        and aggregate["cold_actual_build_counts"] == [1],
        expected={
            "cache_dispositions": {CacheDisposition.MISS.value: cold_repeats},
            "actual_build_counts": [1],
        },
        actual={
            "cache_dispositions": aggregate["cold_cache_dispositions"],
            "actual_build_counts": aggregate["cold_actual_build_counts"],
        },
    )
    for operation in ("driver", "load"):
        warm = aggregate["warm_prepare_queries"][operation]
        p95 = warm["wall_latency_ms"]["p95"]
        _check(
            checks,
            f"warm_prepare_{operation}_p95",
            p95 is not None and p95 <= GATE_TARGETS["warm_prepare_query_p95_max_ms"],
            expected=f"<={GATE_TARGETS['warm_prepare_query_p95_max_ms']}ms",
            actual=p95,
        )
        _check(
            checks,
            f"warm_{operation}_all_memory_hits",
            bool(warm["all_memory_hits"]),
            expected=True,
            actual=warm["all_memory_hits"],
        )
    _check(
        checks,
        "same_key_concurrent_actual_build_count",
        concurrent["actual_build_count"] == GATE_TARGETS["same_key_actual_build_count"],
        expected=GATE_TARGETS["same_key_actual_build_count"],
        actual=concurrent["actual_build_count"],
    )
    _check(
        checks,
        "same_key_concurrent_all_ready",
        concurrent["status_counts"]
        == {PrepareStatus.READY.value: concurrent["request_count"]},
        expected={PrepareStatus.READY.value: concurrent["request_count"]},
        actual=concurrent["status_counts"],
    )
    _check(
        checks,
        "same_key_concurrent_waiters_coalesced",
        concurrent["coalesced_waiter_count"] == concurrent["request_count"] - 1,
        expected=concurrent["request_count"] - 1,
        actual=concurrent["coalesced_waiter_count"],
    )
    if name == "opentitan_core":
        cold_p50 = aggregate["cold_prepare_wall_ms"]["p50"]
        peak_rss = aggregate["peak_rss_kib"]["max"]
        _check(
            checks,
            "opentitan_cold_prepare_p50",
            cold_p50 is not None
            and cold_p50 <= GATE_TARGETS["opentitan_cold_prepare_p50_max_ms"],
            expected=f"<={GATE_TARGETS['opentitan_cold_prepare_p50_max_ms']}ms",
            actual=cold_p50,
        )
        _check(
            checks,
            "opentitan_peak_rss",
            peak_rss is not None and peak_rss <= GATE_TARGETS["peak_rss_max_kib"],
            expected=f"<={GATE_TARGETS['peak_rss_max_kib']}KiB",
            actual=peak_rss,
        )
        _check(
            checks,
            "opentitan_coverage_inconclusive",
            aggregate["coverage_statuses"] == [CoverageStatus.INCONCLUSIVE.value],
            expected=[CoverageStatus.INCONCLUSIVE.value],
            actual=aggregate["coverage_statuses"],
        )
        _check(
            checks,
            "opentitan_blocking_diagnostics_preserved",
            aggregate["blocking_diagnostic_counts"] == [65],
            expected=[65],
            actual=aggregate["blocking_diagnostic_counts"],
        )
        _check(
            checks,
            "opentitan_query_confidence_partial",
            aggregate["representative_query_confidences"] == ["partial"],
            expected=["partial"],
            actual=aggregate["representative_query_confidences"],
        )
        actual_exclusions = sorted(
            set(REQUIRED_OPENTITAN_EXCLUSIONS) & set(aggregate["coverage_gap_codes"])
        )
        _check(
            checks,
            "opentitan_objective_exclusions_explicit",
            actual_exclusions == list(REQUIRED_OPENTITAN_EXCLUSIONS),
            expected=list(REQUIRED_OPENTITAN_EXCLUSIONS),
            actual=actual_exclusions,
        )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _failure_gate(probes: Mapping[str, Any]) -> dict[str, Any]:
    expected_status = {
        "crash": PrepareStatus.WORKER_CRASH.value,
        "timeout": PrepareStatus.TIMED_OUT.value,
        "cancellation": PrepareStatus.CANCELLED.value,
    }
    checks: list[dict[str, Any]] = []
    for kind, expected in expected_status.items():
        probe = probes[kind]
        actual = probe["failure"]["status"]
        _check(
            checks,
            f"{kind}_structured_status",
            actual == expected,
            expected=expected,
            actual=actual,
        )
        _check(
            checks,
            f"{kind}_no_cache_pollution",
            probe["cache_entry_count_after_failure"] == 0
            and probe["inflight_count_after_failure"] == 0,
            expected={"cache_entry_count": 0, "inflight_count": 0},
            actual={
                "cache_entry_count": probe["cache_entry_count_after_failure"],
                "inflight_count": probe["inflight_count_after_failure"],
            },
        )
        _check(
            checks,
            f"{kind}_no_fallback",
            probe["failure"]["fallback_used"] is False,
            expected=False,
            actual=probe["failure"]["fallback_used"],
        )
        _check(
            checks,
            f"{kind}_safe_retry",
            probe["retry"]["status"] == PrepareStatus.READY.value
            and probe["cache_entry_count_after_retry"] == 1,
            expected={"status": PrepareStatus.READY.value, "cache_entry_count": 1},
            actual={
                "status": probe["retry"]["status"],
                "cache_entry_count": probe["cache_entry_count_after_retry"],
            },
        )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _public_run(run: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if not key.startswith("_")}


async def run_benchmark_async(
    args: argparse.Namespace,
    *,
    runner_factory: RunnerFactory | None = None,
    failure_factories: FailureRunnerFactories | None = None,
) -> dict[str, Any]:
    if args.cold_repeats < 1 or args.warm_repeats < 1:
        raise BenchmarkError("cold and warm repeat counts must be positive")
    if args.concurrent_requests < 2:
        raise BenchmarkError("concurrent request count must be at least two")
    if (
        min(
            args.worker_timeout_seconds,
            args.failure_timeout_seconds,
            args.cancellation_delay_seconds,
        )
        <= 0
    ):
        raise BenchmarkError("timeout and cancellation values must be positive")

    baseline = _read_phase1a_baseline(args.phase1a_evidence)
    specs = build_workload_specs(args)
    process_factory: RunnerFactory = runner_factory or (
        lambda: IsolatedSourceGraphProcessRunner(
            python_executable=args.frontend_python,
            working_directory=ROOT,
        )
    )
    workloads: list[dict[str, Any]] = []
    requests: dict[str, SourceGraphBuildRequest] = {}
    for spec in specs:
        request = build_request(spec)
        name = str(spec["workload"])
        requests[name] = request
        runs = [
            await _measure_cold_run(
                spec,
                request,
                process_factory(),
                timeout_seconds=args.worker_timeout_seconds,
                warm_repeats=args.warm_repeats,
            )
            for _ in range(args.cold_repeats)
        ]
        aggregate = _aggregate_runs(runs)
        concurrent = await _measure_concurrent_same_key(
            request,
            process_factory(),
            request_count=args.concurrent_requests,
            timeout_seconds=args.worker_timeout_seconds,
        )
        gate = _workload_gate(name, aggregate, concurrent, args.cold_repeats)
        build_key = compute_source_graph_build_key(request)
        workloads.append(
            {
                "name": name,
                "phase1a_input_fingerprint_sha256": spec["input_fingerprint_sha256"],
                "compile_input_fingerprint_sha256": request.identity.compile_inputs.fingerprint,
                "source_facts": {
                    key: value
                    for key, value in spec["source_facts"].items()
                    if key != "source_paths"
                },
                "build_key": build_key.to_dict(),
                "scope": request.scope.to_dict(),
                "cold_runs": [_public_run(run) for run in runs],
                "aggregate": aggregate,
                "concurrent_same_key": concurrent,
                "gate": gate,
            }
        )

    failure_spec = next(
        (spec for spec in specs if spec["workload"] == "hand_fixture"),
        None,
    )
    if failure_spec is None:
        helper_args = argparse.Namespace(**vars(args))
        helper_args.workload = ["hand_fixture"]
        failure_spec = build_workload_specs(helper_args)[0]
    failure_request = requests.get("hand_fixture") or build_request(failure_spec)

    if failure_factories is None:
        missing_worker = ROOT / "scripts" / "__missing_source_graph_worker__.py"
        if missing_worker.exists():
            raise BenchmarkError("reserved missing-worker path unexpectedly exists")

        def crash_factory() -> SourceGraphWorkerRunner:
            return _FirstThenRunner(
                IsolatedSourceGraphProcessRunner(
                    python_executable=args.frontend_python,
                    worker_script=missing_worker,
                    working_directory=ROOT,
                ),
                process_factory(),
            )

        failure_factories = FailureRunnerFactories(
            crash=crash_factory,
            timeout=process_factory,
            cancellation=process_factory,
        )
    failure_probes = await _measure_failure_probes(
        failure_request,
        failure_factories,
        worker_timeout_seconds=args.worker_timeout_seconds,
        failure_timeout_seconds=args.failure_timeout_seconds,
        cancellation_delay_seconds=args.cancellation_delay_seconds,
    )
    failure_gate = _failure_gate(failure_probes)
    all_workload_gates = all(item["gate"]["passed"] for item in workloads)
    passed = bool(workloads) and all_workload_gates and failure_gate["passed"]
    decision = (
        "phase1b_internal_gate_passed_await_production_integration_approval"
        if passed
        else "phase1b_no_go_or_incomplete_keep_npi_legacy_route"
    )
    script_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {"root": str(ROOT), "head": _git_head(ROOT)},
        "benchmark_script": {
            "path": str(script_path.relative_to(ROOT)),
            "sha256": _sha256_file(script_path),
        },
        "phase1a_before_baseline": baseline,
        "frontend": {
            "name": SLANG_FRONTEND_NAME,
            "required_version": FRONTEND_VERSION,
            "interpreter": str(args.frontend_python),
            "dependency_model": "optional pinned isolated environment",
            "parent_import_requires_pyslang": False,
        },
        "runtime_model": {
            "cache": "process-session memory only",
            "disk_cache": False,
            "persistent_worker": False,
            "max_concurrent_cold_builds_per_process": 1,
            "same_key_single_flight": True,
            "fallback_used": False,
            "public_backend_registered": False,
            "production_route": "NPI success -> Verdi NPI, otherwise Legacy Static",
        },
        "measurement_policy": {
            "cold": "fresh SourceGraphRuntime and isolated worker per repeat",
            "warm": "same-session exact memory hit plus successful query",
            "concurrent": "fresh runtime with simultaneous identical requests",
            "failure_probes": "structured failure, empty cache, then same-runtime retry",
            "scope_claim": (
                "OpenTitan uses the same five-path/two-assignment focused DUT/core "
                "scope as Phase 1A; no full-design accuracy or speedup claim"
            ),
        },
        "gate_targets": GATE_TARGETS,
        "workloads": workloads,
        "failure_probes": {
            "workload": "hand_fixture",
            "results": failure_probes,
            "gate": failure_gate,
        },
        "assessment": {
            "decision": decision,
            "phase1b_internal_gate_passed": passed,
            "all_workload_gates_passed": all_workload_gates,
            "failure_gate_passed": failure_gate["passed"],
            "production_route_changed": False,
            "public_production_integration_performed": False,
            "coverage_claim": "scoped_only_partial_or_inconclusive_preserved",
            "next_step": (
                "stop and await explicit public production integration approval"
                if passed
                else "retain NPI -> Legacy Static and inspect machine-readable blockers"
            ),
        },
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_benchmark_async(args))


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        result = run_benchmark(args)
    except (BenchmarkError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        _write_json_atomic(args.output, result)
    else:
        print(rendered)
    return 0 if result["assessment"]["phase1b_internal_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
