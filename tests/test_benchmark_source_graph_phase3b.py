from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_source_graph_phase3a as phase3a
from scripts import benchmark_source_graph_phase3b as benchmark
import src.connectivity_backend as connectivity_backend
from src.source_graph_runtime import PrepareStatus


ROOT = Path(__file__).resolve().parents[1]
PHASE3B_EVIDENCE = ROOT / "benchmarks/source_graph_connectivity_phase3b_results.json"
PHASE3B_EVIDENCE_SHA256 = (
    "32a57dba8a1c554188da79f9bab0982fc0f547e0b19f9a6a789f438340113e81"
)


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def _ready():
    return phase3a.FixtureReadyRunner(delay_seconds=0.02)


def _failure_factories():
    def failed_then_ready(status: PrepareStatus, code: str):
        return phase3a._FirstThenRunner(
            phase3a._FailedRunner(status, code),
            _ready(),
        )

    return phase3a.FailureRunnerFactories(
        dependency=lambda: failed_then_ready(
            PrepareStatus.DEPENDENCY_BLOCKED, "frontend_unavailable"
        ),
        build_failure=lambda: failed_then_ready(
            PrepareStatus.BUILD_FAILED, "frontend_build_failed"
        ),
        crash=lambda: failed_then_ready(
            PrepareStatus.WORKER_CRASH, "worker_exit_failure"
        ),
        timeout=lambda: failed_then_ready(PrepareStatus.TIMED_OUT, "worker_timeout"),
        cancellation=lambda: phase3a._FirstThenRunner(
            phase3a.FixtureReadyRunner(delay_seconds=0.05),
            _ready(),
        ),
    )


def test_historical_baseline_guard_accepts_all_frozen_evidence():
    receipt = benchmark._read_historical_baselines()

    assert {name: item["sha256"] for name, item in receipt.items()} == {
        name: expected for name, (_, expected) in benchmark.HISTORICAL_EVIDENCE.items()
    }
    assert receipt["phase3a"]["measurement_head"] == (
        "1a9fac908caa865821d46b60130edf1edf3abde1"
    )
    accepted = receipt["phase3a"]["accepted_invariants"]
    assert accepted["route_gate_passed"] is True
    assert accepted["workload_gate_passed"] is True
    assert accepted["failure_gate_passed"] is True
    assert accepted["four_way_exact_actual_build_count"] == 1
    assert accepted["opentitan_ordered_input_count"] == 785
    assert accepted["opentitan_ordered_top_count"] == 11
    assert accepted["opentitan_scope_path_count"] == 4
    assert accepted["opentitan_coverage_status"] == "inconclusive"
    assert accepted["opentitan_blocking_diagnostic_count"] == 65
    assert accepted["opentitan_representative_confidence"] == "partial"
    assert accepted["opentitan_claim"] == "target-scoped only"


def test_historical_baseline_guard_rejects_phase3a_tampering(tmp_path, monkeypatch):
    original, expected = benchmark.HISTORICAL_EVIDENCE["phase3a"]
    copied = tmp_path / "phase3a.json"
    payload = json.loads(original.read_text())
    payload["assessment"]["cross_target_reuse_implemented"] = True
    copied.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setitem(
        benchmark.HISTORICAL_EVIDENCE,
        "phase3a",
        (copied, expected),
    )

    with pytest.raises(benchmark.BenchmarkError, match="hash mismatch"):
        benchmark._read_historical_baselines()


def test_route_isolation_detects_phase3c_while_preserving_order_and_wave_locks():
    receipt = benchmark._route_isolation_receipt()

    assert receipt["accepted_head"] == benchmark.ACCEPTED_PHASE3A_HEAD
    assert receipt["production_route_ordering_changed"] is False
    assert receipt["reuse_behavior_affected_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
    ]
    assert receipt["trace_x_source_route_changed"] is True
    assert receipt["waveform_locking_model_changed"] is False
    for route in receipt["route_stage_order"].values():
        assert route["accepted"] == ["trusted_npi", "source_graph", "legacy_static"]
        assert route["current"] == route["accepted"]
    assert receipt["functions"]["_route_public_connectivity"]["changed"] is True
    assert receipt["functions"]["_route_public_signal_path"]["changed"] is True
    assert receipt["functions"]["_run_trace_x_attempt"]["changed"] is True
    assert receipt["functions"]["_handle_trace_x_source"]["changed"] is True


@pytest.mark.anyio
async def test_fake_phase3b_gate_covers_reuse_dominance_capacity_and_failures(
    monkeypatch,
):
    # Phase 3B's executable gate is a historical behavior test. Freeze its
    # isolation receipt at the Phase 3B boundary; the separate test above must
    # detect the authorized Phase 3C X-trace route change in current sources.
    frozen_isolation = benchmark._route_isolation_receipt()
    frozen_isolation["trace_x_source_route_changed"] = False
    for function_name in ("_run_trace_x_attempt", "_handle_trace_x_source"):
        function = frozen_isolation["functions"][function_name]
        function["current_ast_sha256"] = function["accepted_ast_sha256"]
        function["changed"] = False
    monkeypatch.setattr(
        benchmark,
        "_route_isolation_receipt",
        lambda: frozen_isolation,
    )
    result = await benchmark.run_benchmark_async(
        _args(
            "--workload",
            "hand_fixture",
            "--cold-repeats",
            "1",
            "--warm-repeats",
            "2",
            "--concurrent-requests",
            "4",
            "--cancellation-delay-seconds",
            "0.005",
        ),
        runner_factory=_ready,
        failure_factories=_failure_factories(),
        static_factory=connectivity_backend.StaticConnectivityBackend,
    )

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase3b"
    assessment = result["assessment"]
    assert assessment["decision"] == (
        "phase3b_cross_target_process_memory_reuse_gate_passed"
    )
    assert assessment["phase3b_cross_target_process_memory_reuse_gate_passed"] is True
    assert assessment["production_route_ordering_changed"] is False
    assert assessment["reuse_behavior_affected_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
    ]
    assert assessment["cross_target_process_memory_reuse"] is True
    assert assessment["cross_process_reuse"] is False
    assert assessment["trace_x_source_route_changed"] is False
    assert assessment["waveform_locking_model_changed"] is False
    assert assessment["driver_load_path_correctness_unchanged"] is True
    assert assessment["no_mixed_provenance"] is True
    assert assessment["cancellation_fallback"] is False
    assert assessment["failure_or_cancellation_cache_polluted"] is False
    assert assessment["global_python_without_pyslang_startup_ok"] is True

    workload = result["workloads"][0]
    assert workload["gate"]["passed"] is True
    run = workload["runs"][0]
    receipts = [item["source_graph"] for item in run["core_queries"]]
    assert run["runtime_stats_before_out_of_scope"]["actual_build_count"] == 1
    assert len({item["artifact_fingerprint_sha256"] for item in receipts}) == 1
    assert len({item["query_fingerprint_sha256"] for item in receipts}) == 5
    assert [item["artifact_reuse"] for item in receipts] == [
        "cold",
        "exact_hit",
        "exact_hit",
        "exact_hit",
        "exact_hit",
    ]
    assert run["out_of_scope"]["source_graph"]["cache_lookup_reason"] == (
        "cached_scope_not_dominating"
    )
    assert run["runtime_stats_after"]["actual_build_count"] == 2
    concurrent = workload["concurrent_mixed_queries"]
    assert concurrent["gate"]["passed"] is True
    assert concurrent["actual_build_count"] == 1
    assert len(concurrent["artifact_fingerprints"]) == 1
    assert len(concurrent["query_fingerprints"]) == 4

    dominance = result["dominating_scope_reuse"]
    assert dominance["gate"]["passed"] is True
    selected = dominance["covered_endpoint_query"]["source_graph"]
    assert selected["artifact_reuse"] == "dominating_hit"
    assert selected["scope_match"]["relation"] == "superset"
    assert result["capacity_probe"]["gate"]["passed"] is True
    assert result["failure_probes"]["gate"]["passed"] is True
    assert result["route_probes"]["query_cancellation"]["static_calls"] == 0
    assert (
        result["route_probes"]["mixed_provenance_rejection"][
            "public_payload_has_mixed_provenance"
        ]
        is False
    )
    assert result["metrics_privacy"]["contains_forbidden_content_field"] is False


def test_parent_import_does_not_load_optional_frontends_or_start_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import scripts.benchmark_source_graph_phase3b; "
                "from src.source_graph_production import "
                "source_graph_runtime_created; "
                "print(int('pyslang' in sys.modules), "
                "int('uhdm' in sys.modules), int(source_graph_runtime_created()))"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.stdout.strip() == "0 0 0"


def test_tracked_phase3b_evidence_passes_cross_target_reuse_gate():
    raw = PHASE3B_EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PHASE3B_EVIDENCE_SHA256
    result = json.loads(raw)

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase3b"
    assert result["repository"]["head"] == benchmark.MEASUREMENT_HEAD
    script = ROOT / result["benchmark_script"]["path"]
    assert (
        hashlib.sha256(script.read_bytes()).hexdigest()
        == (result["benchmark_script"]["sha256"])
    )
    for name, (_, expected) in benchmark.HISTORICAL_EVIDENCE.items():
        path = ROOT / result["before_baselines"][name]["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        assert result["before_baselines"][name]["sha256"] == expected

    assessment = result["assessment"]
    assert assessment["decision"] == (
        "phase3b_cross_target_process_memory_reuse_gate_passed"
    )
    assert assessment["phase3b_cross_target_process_memory_reuse_gate_passed"] is True
    assert assessment["workload_gate_passed"] is True
    assert assessment["dominance_gate_passed"] is True
    assert assessment["capacity_gate_passed"] is True
    assert assessment["failure_gate_passed"] is True
    assert assessment["route_gate_passed"] is True
    assert assessment["architecture_gate_passed"] is True
    assert assessment["production_route_ordering_changed"] is False
    assert assessment["reuse_behavior_affected_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
    ]
    assert assessment["cross_target_process_memory_reuse"] is True
    assert assessment["cross_process_reuse"] is False
    assert assessment["trace_x_source_route_changed"] is False
    assert assessment["disk_cache"] is False
    assert assessment["persistent_worker"] is False
    assert assessment["startup_full_design_enumeration"] is False
    assert assessment["waveform_locking_model_changed"] is False
    assert assessment["opentitan_claim"] == "target-scoped only"
    assert assessment["driver_load_path_correctness_unchanged"] is True
    assert assessment["no_mixed_provenance"] is True
    assert assessment["cancellation_fallback"] is False
    assert assessment["failure_or_cancellation_cache_polluted"] is False
    assert assessment["global_python_without_pyslang_startup_ok"] is True

    runtime = result["runtime_model"]
    assert runtime["production_route_ordering_changed"] is False
    assert runtime["cross_target_process_memory_reuse"] is True
    assert runtime["cross_process_reuse"] is False
    assert runtime["cache_max_entries"] == 8
    assert runtime["cache_max_accounted_ir_bytes"] == 512 * 1024 * 1024
    assert runtime["disk_cache"] is False
    assert runtime["persistent_worker"] is False
    assert runtime["startup_build"] is False
    assert runtime["startup_full_design_enumeration"] is False

    workloads = {item["name"]: item for item in result["workloads"]}
    assert set(workloads) == {"hand_fixture", "opentitan_core"}
    for workload in workloads.values():
        assert workload["gate"]["passed"] is True
        assert workload["concurrent_mixed_queries"]["actual_build_count"] == 1
        assert len(workload["concurrent_mixed_queries"]["artifact_fingerprints"]) == 1
    hand_run = workloads["hand_fixture"]["runs"][0]
    assert hand_run["runtime_stats_before_out_of_scope"]["actual_build_count"] == 1
    assert hand_run["runtime_stats_after"]["actual_build_count"] == 2

    opentitan = workloads["opentitan_core"]
    assert opentitan["aggregate"]["cold_wall_time_ms"]["p50"] <= 15_000
    assert opentitan["aggregate"]["cross_target_warm_wall_time_ms"]["p95"] <= 100
    assert opentitan["aggregate"]["peak_rss_kib"]["max"] <= 2_621_440
    for run in opentitan["runs"]:
        first = run["core_queries"][0]["source_graph"]
        assert first["manifest"]["input_count"] == 785
        assert first["manifest"]["top_count"] == 11
        assert first["scope"]["coverage_boundary_instance_count"] == 4
        assert first["scope"]["requested_cone_instance_count"] == 4
        assert first["coverage_status"] == "inconclusive"
        assert first["coverage_blocking_diagnostic_count"] == 65
        assert first["query_confidence"] == "partial"
        assert run["runtime_stats_before_out_of_scope"]["actual_build_count"] == 1

    policy = result["measurement_policy"]
    assert policy["opentitan_claim"] == "target-scoped only"
    assert policy["opentitan_full_design_accuracy_claim"] is False
    assert policy["opentitan_full_design_coverage_claim"] is False
    assert policy["opentitan_full_design_warm_state_claim"] is False
    assert policy["opentitan_full_design_speedup_claim"] is False
    assert result["dominating_scope_reuse"]["gate"]["passed"] is True
    assert result["capacity_probe"]["gate"]["passed"] is True
    assert result["failure_probes"]["gate"]["passed"] is True
    assert result["route_probes"]["passed"] is True
    assert result["metrics_privacy"]["contains_forbidden_content_field"] is False
