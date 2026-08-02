from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_source_graph_phase1b as benchmark
from src.connectivity_ir import CoverageStatus
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME
from src.source_graph_contract import SourceGraphScopeReceipt
from src.source_graph_runtime import (
    PrepareStatus,
    WorkerBuildResult,
    WorkerResourceMetrics,
)
from tests.connectivity_ir_fixtures import build_hand_ir


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "benchmarks/source_graph_connectivity_phase1b_results.json"
EVIDENCE_SHA256 = "c9a25c96c63ddce9205ecabf86b61f6da1eff9ba0f71aeee099a6b04f7237da7"


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def _fake_ir():
    return replace(
        build_hand_ir(),
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version=benchmark.FRONTEND_VERSION,
    )


class ReadyRunner:
    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.count = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if cancel_event.is_set():
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="fake_worker",
            )
        ir = _fake_ir()
        return WorkerBuildResult.ready(
            ir,
            SourceGraphScopeReceipt(
                scope=request.scope,
                coverage_status=CoverageStatus.COMPLETE,
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


class FailureThenReadyRunner(ReadyRunner):
    def __init__(self, status: PrepareStatus):
        super().__init__()
        self.status = status

    async def run(self, request, *, timeout_seconds, cancel_event):
        if self.count:
            return await super().run(
                request,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        self.count += 1
        if self.status is PrepareStatus.CANCELLED:
            await cancel_event.wait()
        return WorkerBuildResult.failed(
            self.status,
            code={
                PrepareStatus.WORKER_CRASH: "worker_exit_failure",
                PrepareStatus.TIMED_OUT: "worker_timeout",
                PrepareStatus.CANCELLED: "request_cancelled",
            }[self.status],
            stage="fake_worker",
            metrics=WorkerResourceMetrics(cancel_to_exit_ms=0.5),
        )


def _fake_correctness(_ir, engine, _spec):
    queries = [
        {
            "name": "fixture_driver",
            "operation": "driver",
            "signal": "sg_top.bus.data[15:8]",
        },
        {
            "name": "fixture_load",
            "operation": "load",
            "signal": "sg_top.bus.data[15:8]",
        },
    ]
    results = [
        (query, engine.query_driver(query["signal"]))
        if query["operation"] == "driver"
        else (query, engine.query_loads(query["signal"]))
        for query in queries
    ]
    assert all(result.status.value == "found" for _, result in results)
    return {
        "all_passed": True,
        "passed_count": 2,
        "check_count": 2,
        "checks": [],
    }, results


def test_fixture_requests_use_tracked_inputs_and_explicit_finite_scopes():
    specs = benchmark.build_workload_specs(_args())

    assert [spec["workload"] for spec in specs] == [
        "deep_x_npi",
        "hand_fixture",
    ]
    for spec in specs:
        request = benchmark.build_request(spec)
        manifest = request.identity.compile_inputs
        key = benchmark.compute_source_graph_build_key(request)

        assert manifest.complete is True
        assert key.cross_request_reusable is True
        assert request.scope.coverage_boundary.explicit is True
        assert request.scope.coverage_boundary.instance_paths
        assert all(
            "*" not in path for path in request.scope.coverage_boundary.instance_paths
        )
        assert all(Path(path).is_file() for path in manifest.ordered_inputs)
        assert all(Path(path).is_relative_to(ROOT) for path in manifest.ordered_inputs)


def test_translation_preserves_source_option_and_multiple_top_order():
    source = str(ROOT / "tests/fixtures/source_graph_frontend/hand_connectivity.sv")
    spec = {
        "frontend_args": [
            "--compat",
            "vcs",
            source,
            "--top",
            "bind_top",
            "--top",
            "sg_top",
            "+define+AFTER_TOP=1",
        ],
        "source_facts": {"source_paths": [source]},
        "top": "sg_top",
    }

    inputs, options, tops = benchmark._translated_manifest_parts(spec)

    assert inputs == (source,)
    assert options == ("--compat", "vcs", "+define+AFTER_TOP=1")
    assert tops == ("bind_top", "sg_top")


def test_compile_fingerprint_changes_with_order_options_and_source_content(tmp_path):
    first = tmp_path / "first.sv"
    second = tmp_path / "second.sv"
    first.write_text("module first; endmodule\n")
    second.write_text("module second; endmodule\n")

    baseline = benchmark._compile_input_fingerprint(
        (str(first), str(second)), ("--std", "1800-2017"), ("top",)
    )

    assert baseline is not None
    assert baseline != benchmark._compile_input_fingerprint(
        (str(second), str(first)), ("--std", "1800-2017"), ("top",)
    )
    assert baseline != benchmark._compile_input_fingerprint(
        (str(first), str(second)), ("--std", "1800-2023"), ("top",)
    )
    second.write_text("module second; wire changed; endmodule\n")
    assert baseline != benchmark._compile_input_fingerprint(
        (str(first), str(second)), ("--std", "1800-2017"), ("top",)
    )
    assert (
        benchmark._compile_input_fingerprint(
            (str(tmp_path / "missing.sv"),), (), ("top",)
        )
        is None
    )


@pytest.mark.anyio
async def test_fake_worker_measures_cold_warm_and_same_key_single_flight(monkeypatch):
    monkeypatch.setattr(benchmark.phase1a, "_run_correctness", _fake_correctness)
    spec = benchmark.build_workload_specs(_args("--workload", "hand_fixture"))[0]
    request = benchmark.build_request(spec)

    run = await benchmark._measure_cold_run(
        spec,
        request,
        ReadyRunner(),
        timeout_seconds=2.0,
        warm_repeats=3,
    )
    concurrent = await benchmark._measure_concurrent_same_key(
        request,
        ReadyRunner(delay=0.02),
        request_count=4,
        timeout_seconds=2.0,
    )
    aggregate = benchmark._aggregate_runs([run])
    gate = benchmark._workload_gate("hand_fixture", aggregate, concurrent, 1)

    assert run["status"] == "ready"
    assert run["prepare"]["metrics"]["cache_disposition"] == "miss"
    assert run["prepare"]["metrics"]["actual_build_count"] == 1
    assert run["warm_prepare_queries"]["driver"]["cache_dispositions"] == {
        "hit_exact": 3
    }
    assert run["warm_prepare_queries"]["load"]["cache_dispositions"] == {"hit_exact": 3}
    assert concurrent["actual_build_count"] == 1
    assert concurrent["coalesced_waiter_count"] == 3
    assert concurrent["cache_entry_count"] == 1
    assert gate["passed"] is True


@pytest.mark.anyio
async def test_failure_probes_are_structured_do_not_cache_and_retry():
    spec = benchmark.build_workload_specs(_args("--workload", "hand_fixture"))[0]
    request = benchmark.build_request(spec)
    factories = benchmark.FailureRunnerFactories(
        crash=lambda: FailureThenReadyRunner(PrepareStatus.WORKER_CRASH),
        timeout=lambda: FailureThenReadyRunner(PrepareStatus.TIMED_OUT),
        cancellation=lambda: FailureThenReadyRunner(PrepareStatus.CANCELLED),
    )

    probes = await benchmark._measure_failure_probes(
        request,
        factories,
        worker_timeout_seconds=2.0,
        failure_timeout_seconds=0.001,
        cancellation_delay_seconds=0.001,
    )
    gate = benchmark._failure_gate(probes)

    assert gate["passed"] is True
    assert {kind: item["failure"]["status"] for kind, item in probes.items()} == {
        "crash": "worker_crash",
        "timeout": "timed_out",
        "cancellation": "cancelled",
    }
    assert all(item["cache_entry_count_after_failure"] == 0 for item in probes.values())
    assert all(item["retry"]["status"] == "ready" for item in probes.values())
    assert all(item["failure"]["fallback_used"] is False for item in probes.values())


def test_opentitan_gate_enforces_scope_coverage_latency_memory_and_exclusions():
    aggregate = {
        "ready_run_count": 3,
        "correctness_all_runs": True,
        "ir_fingerprint_stable": True,
        "cold_cache_dispositions": {"miss": 3},
        "cold_actual_build_counts": [1],
        "warm_prepare_queries": {
            operation: {
                "wall_latency_ms": {"p95": 1.0},
                "all_memory_hits": True,
            }
            for operation in ("driver", "load")
        },
        "cold_prepare_wall_ms": {"p50": 5_000.0},
        "peak_rss_kib": {"max": 1_400_000.0},
        "coverage_statuses": ["inconclusive"],
        "blocking_diagnostic_counts": [65],
        "representative_query_confidences": ["partial"],
        "coverage_gap_codes": list(benchmark.REQUIRED_OPENTITAN_EXCLUSIONS),
    }
    concurrent = {
        "actual_build_count": 1,
        "status_counts": {"ready": 4},
        "request_count": 4,
        "coalesced_waiter_count": 3,
    }

    assert (
        benchmark._workload_gate("opentitan_core", aggregate, concurrent, 3)["passed"]
        is True
    )

    aggregate["coverage_statuses"] = ["complete"]
    aggregate["cold_prepare_wall_ms"]["p50"] = 15_001.0
    failed = benchmark._workload_gate("opentitan_core", aggregate, concurrent, 3)
    by_name = {item["name"]: item for item in failed["checks"]}
    assert by_name["opentitan_coverage_inconclusive"]["passed"] is False
    assert by_name["opentitan_cold_prepare_p50"]["passed"] is False


@pytest.mark.anyio
async def test_fake_end_to_end_result_keeps_internal_boundary_and_old_route(
    monkeypatch,
):
    monkeypatch.setattr(benchmark.phase1a, "_run_correctness", _fake_correctness)
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
    factories = benchmark.FailureRunnerFactories(
        crash=lambda: FailureThenReadyRunner(PrepareStatus.WORKER_CRASH),
        timeout=lambda: FailureThenReadyRunner(PrepareStatus.TIMED_OUT),
        cancellation=lambda: FailureThenReadyRunner(PrepareStatus.CANCELLED),
    )

    result = await benchmark.run_benchmark_async(
        args,
        runner_factory=lambda: ReadyRunner(delay=0.01),
        failure_factories=factories,
    )

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase1b"
    assert result["assessment"]["phase1b_internal_gate_passed"] is True
    assert result["assessment"]["production_route_changed"] is False
    assert result["assessment"]["public_production_integration_performed"] is False
    assert result["runtime_model"]["disk_cache"] is False
    assert result["runtime_model"]["persistent_worker"] is False
    assert result["runtime_model"]["public_backend_registered"] is False
    assert result["failure_probes"]["gate"]["passed"] is True


def test_parent_benchmark_import_does_not_import_optional_pyslang():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import scripts.benchmark_source_graph_phase1b; "
                "print('yes' if 'pyslang' in sys.modules else 'no')"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.stdout.strip() == "no"


def test_phase1a_baseline_guard_rejects_tampering(tmp_path):
    copied = tmp_path / "phase1a.json"
    payload = json.loads(benchmark.DEFAULT_PHASE1A_EVIDENCE.read_text())
    payload["assessment"]["production_route_changed"] = True
    copied.write_text(json.dumps(payload))

    with pytest.raises(benchmark.BenchmarkError, match="hash mismatch"):
        benchmark._read_phase1a_baseline(copied)


def test_tracked_phase1b_evidence_is_complete_and_keeps_coverage_honest():
    assert benchmark._sha256_file(EVIDENCE) == EVIDENCE_SHA256
    payload = json.loads(EVIDENCE.read_text())

    assert payload["schema_version"] == "1.0"
    assert payload["benchmark"] == "source_graph_connectivity_phase1b"
    assert payload["repository"]["head"] == ("b1ad0da31f27cf9120fb22faff6e0a7c69101f40")
    assert payload["phase1a_before_baseline"]["sha256"] == (
        benchmark.PHASE1A_EVIDENCE_SHA256
    )
    assert payload["assessment"] == {
        "all_workload_gates_passed": True,
        "coverage_claim": "scoped_only_partial_or_inconclusive_preserved",
        "decision": (
            "phase1b_internal_gate_passed_await_production_integration_approval"
        ),
        "failure_gate_passed": True,
        "next_step": "stop and await explicit public production integration approval",
        "phase1b_internal_gate_passed": True,
        "production_route_changed": False,
        "public_production_integration_performed": False,
    }
    assert payload["runtime_model"]["production_route"] == (
        "NPI success -> Verdi NPI, otherwise Legacy Static"
    )
    assert payload["runtime_model"]["public_backend_registered"] is False

    workloads = {item["name"]: item for item in payload["workloads"]}
    assert set(workloads) == {"deep_x_npi", "hand_fixture", "opentitan_core"}
    assert all(item["gate"]["passed"] for item in workloads.values())
    assert all(
        item["build_key"]["cross_request_reusable"] for item in workloads.values()
    )
    assert all(
        item["build_key"]["incomplete_reasons"] == [] for item in workloads.values()
    )
    assert all(
        item["concurrent_same_key"]["actual_build_count"] == 1
        for item in workloads.values()
    )

    opentitan = workloads["opentitan_core"]
    aggregate = opentitan["aggregate"]
    assert aggregate["cold_prepare_wall_ms"]["p50"] <= 15_000.0
    assert (
        aggregate["warm_prepare_queries"]["driver"]["wall_latency_ms"]["p95"] <= 100.0
    )
    assert aggregate["warm_prepare_queries"]["load"]["wall_latency_ms"]["p95"] <= 100.0
    assert aggregate["peak_rss_kib"]["max"] <= 2_621_440
    assert aggregate["coverage_statuses"] == ["inconclusive"]
    assert aggregate["blocking_diagnostic_counts"] == [65]
    assert aggregate["representative_query_confidences"] == ["partial"]
    assert set(benchmark.REQUIRED_OPENTITAN_EXCLUSIONS) <= set(
        aggregate["coverage_gap_codes"]
    )
    assert len(opentitan["scope"]["coverage_boundary"]["instance_paths"]) == 5
    assert len(opentitan["scope"]["requested_cone"]["instance_paths"]) == 2
    assert all(
        run["coverage"]["status"] == "inconclusive"
        and run["coverage"]["blocking_diagnostic_count"] == 65
        and all(query["match_confidences"] == ["partial"] for query in run["queries"])
        for run in opentitan["cold_runs"]
    )

    probes = payload["failure_probes"]
    assert probes["gate"]["passed"] is True
    assert {
        kind: result["failure"]["status"] for kind, result in probes["results"].items()
    } == {
        "cancellation": "cancelled",
        "crash": "worker_crash",
        "timeout": "timed_out",
    }
    assert all(
        result["cache_entry_count_after_failure"] == 0
        and result["failure"]["fallback_used"] is False
        and result["retry"]["status"] == "ready"
        for result in probes["results"].values()
    )
