from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_source_graph_phase2 as benchmark
from src.connectivity_ir import CoverageGap, CoverageReport, CoverageStatus
from src.source_graph_contract import SourceGraphScopeReceipt
from src.source_graph_runtime import (
    PrepareStatus,
    WorkerBuildResult,
    WorkerResourceMetrics,
)
from tests.connectivity_ir_fixtures import build_hand_ir


ROOT = Path(__file__).resolve().parents[1]
PHASE2_EVIDENCE = ROOT / "benchmarks/source_graph_connectivity_phase2_results.json"
PHASE2_EVIDENCE_SHA256 = (
    "1b5f76c3862601bb1163d838744c9c03ec7ed62cd1bc5f663ef7008bd0902599"
)


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def _fake_ir():
    return replace(
        build_hand_ir(),
        frontend_name=benchmark.SLANG_FRONTEND_NAME,
        frontend_version=benchmark.FRONTEND_VERSION,
    )


class ReadyRunner:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if cancel_event.is_set():
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="fake_worker",
            )
        ir = _fake_ir()
        exclusions = request.scope.coverage_boundary.objective_exclusions
        coverage_status = (
            CoverageStatus.INCONCLUSIVE if exclusions else CoverageStatus.COMPLETE
        )
        if exclusions:
            ir = replace(
                ir,
                coverage=CoverageReport(
                    status=coverage_status,
                    files_total=2,
                    files_projected=2,
                    gaps=tuple(
                        CoverageGap(
                            code=code,
                            message=f"tracked objective exclusion: {code}",
                            impact=CoverageStatus.INCONCLUSIVE,
                            constructs=(code,),
                            scopes=("*",),
                        )
                        for code in exclusions
                    ),
                ),
            )
        return WorkerBuildResult.ready(
            ir,
            SourceGraphScopeReceipt(
                scope=request.scope,
                coverage_status=coverage_status,
                gap_codes=tuple(exclusions),
            ),
            metrics=WorkerResourceMetrics(
                wall_time_ms=2.0,
                cpu_time_ms=1.0,
                rss_start_kib=100,
                rss_peak_kib=150,
                rss_end_kib=120,
                ir_bytes=len(ir.to_json_bytes()),
            ),
        )


class FailureThenReadyRunner:
    def __init__(self, status: PrepareStatus):
        self.status = status
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.calls += 1
        if self.calls > 1:
            return await ReadyRunner().run(
                request,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        if self.status is PrepareStatus.CANCELLED:
            await cancel_event.wait()
        return WorkerBuildResult.failed(
            self.status,
            code={
                PrepareStatus.BUILD_FAILED: "frontend_build_failed",
                PrepareStatus.WORKER_CRASH: "worker_exit_failure",
                PrepareStatus.TIMED_OUT: "worker_timeout",
                PrepareStatus.CANCELLED: "request_cancelled",
            }[self.status],
            stage="fake_worker",
            metrics=WorkerResourceMetrics(cancel_to_exit_ms=0.5),
        )


class StaticBackend:
    name = "static"
    uses_external_worker = False

    def find_driver(self, **kwargs):
        return {
            "signal_path": kwargs["signal_path"],
            "wave_path": kwargs["wave_path"],
            "resolved_rtl_name": "lane_data",
            "resolved_module": "sg_top",
            "resolved_instance_path": "sg_top",
            "driver_status": "partial",
            "driver_kind": "unknown",
            "source_file": None,
            "source_line": None,
            "expression_summary": None,
            "upstream_signals": [],
            "confidence": "low",
            "recursive": kwargs["recursive"],
            "driver_chain": None,
            "chain_summary": None,
            "backend": "static",
        }

    def find_loads(self, **kwargs):
        return {
            "signal_path": kwargs["signal_path"],
            "resolved_rtl_name": "data[7:0]",
            "resolved_module": "sg_bus",
            "resolved_instance_path": "sg_top.bus",
            "loads": [],
            "completeness": "shallow_only",
            "stopped_at": "no_static_load_found",
            "unsupported_reason": None,
            "backend": "static",
        }


def _failure_factories():
    return benchmark.FailureRunnerFactories(
        build_failure=lambda: FailureThenReadyRunner(PrepareStatus.BUILD_FAILED),
        crash=lambda: FailureThenReadyRunner(PrepareStatus.WORKER_CRASH),
        timeout=lambda: FailureThenReadyRunner(PrepareStatus.TIMED_OUT),
        cancellation=lambda: FailureThenReadyRunner(PrepareStatus.CANCELLED),
    )


def test_historical_baseline_guard_accepts_only_pinned_phase1_evidence():
    receipt = benchmark._read_historical_baselines(
        benchmark.DEFAULT_PHASE1A_EVIDENCE,
        benchmark.DEFAULT_PHASE1B_EVIDENCE,
    )

    assert receipt["phase1a"]["sha256"] == benchmark.PHASE1A_EVIDENCE_SHA256
    assert receipt["phase1b"]["sha256"] == benchmark.PHASE1B_EVIDENCE_SHA256
    assert receipt["phase1b"]["opentitan_reference_scope"] == {
        "coverage_boundary_path_count": 5,
        "requested_assignment_cone_path_count": 2,
        "coverage_status": "inconclusive",
        "blocking_diagnostic_count": 65,
        "representative_query_confidence": "partial",
        "objective_exclusions": sorted(benchmark.REQUIRED_PHASE1B_OPENTITAN_EXCLUSIONS),
    }


def test_historical_baseline_guard_rejects_tampering(tmp_path):
    copied = tmp_path / "phase1b.json"
    payload = json.loads(benchmark.DEFAULT_PHASE1B_EVIDENCE.read_text())
    payload["assessment"]["production_route_changed"] = True
    copied.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="hash mismatch"):
        benchmark._read_historical_baselines(
            benchmark.DEFAULT_PHASE1A_EVIDENCE,
            copied,
        )


def test_ast_route_receipt_proves_only_driver_load_and_not_wave_lock_changed():
    receipt = benchmark._route_isolation_receipt()

    assert receipt["driver_load_route_changed_only"] is True
    branches = receipt["dispatch_branches"]
    assert branches["explain_signal_driver"]["changed"] is True
    assert branches["find_signal_loads"]["changed"] is True
    assert branches["trace_signal_path"]["changed"] is False
    assert branches["trace_x_source"]["changed"] is False
    assert all(
        item["changed"] is False
        for item in receipt["waveform_locking_functions"].values()
    )


@pytest.mark.anyio
async def test_fake_public_phase2_benchmark_covers_routes_performance_and_failures():
    args = _args(
        "--workload",
        "hand_fixture",
        "--cold-repeats",
        "1",
        "--warm-repeats",
        "2",
        "--concurrent-requests",
        "3",
    )

    result = await benchmark.run_benchmark_async(
        args,
        runner_factory=lambda: ReadyRunner(delay=0.01),
        failure_factories=_failure_factories(),
        static_factory=StaticBackend,
    )

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase2"
    assert result["assessment"]["phase2_public_driver_load_gate_passed"] is True
    assert result["assessment"]["production_route_changed"] is True
    assert result["assessment"]["production_route_changed_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
    ]
    assert result["assessment"]["trace_signal_path_route_changed"] is False
    assert result["assessment"]["trace_x_source_route_changed"] is False
    assert result["runtime_model"]["disk_cache"] is False
    assert result["runtime_model"]["persistent_worker"] is False

    npi = result["route_probes"]["npi_success"]
    assert npi["driver"]["actual_backend"] == "verdi_npi"
    assert npi["loads"]["actual_backend"] == "verdi_npi"
    assert npi["source_graph_runtime_get_count"] == 0
    assert npi["static_calls"] == 0

    fallback = result["route_probes"]["source_graph_dependency_to_legacy_static"][
        "result"
    ]
    assert fallback["actual_backend"] == "static"
    assert fallback["source_graph"]["prepare_status"] == "dependency_blocked"
    assert fallback["source_graph"]["fallback_used"] is True

    boundary = result["route_probes"]["construct_coverage_boundary"]["result"]
    assert boundary["actual_backend"] == "source_graph"
    assert boundary["payload_backends"] == ["source_graph"]
    assert boundary["source_graph"]["coverage_status"] == "inconclusive"
    assert boundary["source_graph"]["query_confidence"] == "partial"
    assert benchmark.REQUIRED_PRODUCTION_MARKER_EXCLUSIONS <= set(
        boundary["source_graph"]["objective_exclusions"]
    )

    workload = result["workloads"][0]
    assert workload["gate"]["passed"] is True
    assert workload["operations"]["driver"]["aggregate"]["actual_backends"] == [
        "source_graph"
    ]
    assert workload["operations"]["loads"]["aggregate"]["actual_backends"] == [
        "source_graph"
    ]
    assert workload["concurrent_same_key"]["actual_build_count"] == 1
    assert workload["concurrent_same_key"]["coalesced_waiter_count"] == 2

    failures = result["failure_probes"]
    assert failures["gate"]["passed"] is True
    assert failures["results"]["build_failure"]["failure"]["actual_backend"] == (
        "static"
    )
    assert failures["results"]["crash"]["failure"]["actual_backend"] == "static"
    assert failures["results"]["timeout"]["failure"]["actual_backend"] == "static"
    assert failures["results"]["cancellation"]["failure"]["fallback_used"] is False
    assert all(
        item["cache_entry_count_after_failure"] == 0
        and item["retry"]["actual_backend"] == "source_graph"
        for item in failures["results"].values()
    )


def test_parent_import_does_not_load_optional_frontends():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import scripts.benchmark_source_graph_phase2; "
                "print(int('pyslang' in sys.modules), int('uhdm' in sys.modules))"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.stdout.strip() == "0 0"


def test_tracked_phase2_evidence_passes_public_production_gate():
    raw = PHASE2_EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PHASE2_EVIDENCE_SHA256
    result = json.loads(raw)

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase2"
    assert result["repository"]["head"] == ("8b40dc2b7b597f152fe5d0a152d3afe71c8ff05e")
    assert (
        result["benchmark_script"]["sha256"]
        == hashlib.sha256(
            (ROOT / result["benchmark_script"]["path"]).read_bytes()
        ).hexdigest()
    )
    assert result["before_baselines"]["phase1a"]["sha256"] == (
        benchmark.PHASE1A_EVIDENCE_SHA256
    )
    assert result["before_baselines"]["phase1b"]["sha256"] == (
        benchmark.PHASE1B_EVIDENCE_SHA256
    )

    assessment = result["assessment"]
    assert assessment["decision"] == "phase2_public_driver_load_gate_passed"
    assert assessment["phase2_public_driver_load_gate_passed"] is True
    assert assessment["production_route_changed_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
    ]
    assert assessment["trace_signal_path_route_changed"] is False
    assert assessment["trace_x_source_route_changed"] is False
    assert assessment["waveform_locking_model_changed"] is False

    runtime = result["runtime_model"]
    assert runtime["disk_cache"] is False
    assert runtime["persistent_worker"] is False
    assert runtime["startup_build"] is False
    assert runtime["same_key_single_flight"] is True
    assert runtime["max_concurrent_cold_builds_per_process"] == 1

    routes = result["route_probes"]
    assert routes["gate"]["passed"] is True
    npi = routes["npi_success"]
    assert {npi[operation]["actual_backend"] for operation in ("driver", "loads")} == {
        "verdi_npi"
    }
    assert npi["source_graph_runtime_get_count"] == 0
    assert npi["static_calls"] == 0
    fallback = routes["source_graph_dependency_to_legacy_static"]["result"]
    assert fallback["selected_backend"] == "source_graph"
    assert fallback["actual_backend"] == "static"
    assert fallback["fallback_reason"] == "source_graph_dependency_blocked"
    assert fallback["payload_backends"] == ["static"]
    boundary = routes["construct_coverage_boundary"]["result"]
    assert boundary["actual_backend"] == "source_graph"
    assert boundary["source_graph"]["coverage_status"] == "inconclusive"
    assert boundary["source_graph"]["query_confidence"] == "partial"
    assert benchmark.REQUIRED_PRODUCTION_MARKER_EXCLUSIONS <= set(
        boundary["source_graph"]["objective_exclusions"]
    )

    workloads = {item["name"]: item for item in result["workloads"]}
    assert set(workloads) == {"hand_fixture", "opentitan_core"}
    for workload in workloads.values():
        assert workload["gate"]["passed"] is True
        assert workload["concurrent_same_key"]["actual_build_count"] == 1
        assert workload["concurrent_same_key"]["actual_backends"] == {"source_graph": 4}
        assert workload["concurrent_same_key"]["static_calls"] == 0
        for operation in ("driver", "loads"):
            aggregate = workload["operations"][operation]["aggregate"]
            assert aggregate["actual_backends"] == ["source_graph"]
            assert aggregate["payload_backend_sets"] == [["source_graph"]]
            assert aggregate["actual_build_counts"] == [1]
            assert all(len(value) == 64 for value in aggregate["build_fingerprints"])
            assert all(len(value) == 64 for value in aggregate["compile_fingerprints"])
            assert all(len(value) == 64 for value in aggregate["ir_fingerprints"])

    opentitan = workloads["opentitan_core"]
    assert "not a full-design accuracy or speedup claim" in opentitan["scope_claim"]
    for operation in ("driver", "loads"):
        aggregate = opentitan["operations"][operation]["aggregate"]
        assert aggregate["cold_wall_time_ms"]["p50"] <= 15_000.0
        assert aggregate["warm_wall_time_ms"]["p95"] <= 100.0
        assert aggregate["peak_rss_kib"]["max"] <= 2_621_440
        assert aggregate["manifest_input_counts"] == [785]
        assert aggregate["manifest_top_counts"] == [11]
        assert aggregate["coverage_boundary_path_counts"] == [4]
        assert aggregate["requested_cone_path_counts"] == [1]
        assert aggregate["coverage_statuses"] == ["inconclusive"]
        assert aggregate["blocking_diagnostic_counts"] == [65]
        assert aggregate["query_confidences"] == ["partial"]

    failures = result["failure_probes"]
    assert failures["gate"]["passed"] is True
    assert set(failures["results"]) == {
        "build_failure",
        "cancellation",
        "crash",
        "timeout",
    }
    for kind, probe in failures["results"].items():
        assert probe["cache_entry_count_after_failure"] == 0
        assert probe["inflight_count_after_failure"] == 0
        assert probe["retry"]["actual_backend"] == "source_graph"
        assert probe["cache_entry_count_after_retry"] == 1
        if kind == "cancellation":
            assert probe["failure"]["fallback_used"] is False
            assert probe["static_calls_after_failure"] == 0
        else:
            assert probe["failure"]["actual_backend"] == "static"
            assert probe["failure"]["source_graph"]["fallback_used"] is True
