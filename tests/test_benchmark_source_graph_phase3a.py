from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_source_graph_phase3a as benchmark
import src.connectivity_backend as connectivity_backend
from src.source_graph_runtime import PrepareStatus


ROOT = Path(__file__).resolve().parents[1]
PHASE3A_EVIDENCE = ROOT / "benchmarks/source_graph_connectivity_phase3a_results.json"
PHASE3A_EVIDENCE_SHA256 = (
    "6b7f8822f978f8e46dd9172c2b093e731889f44b0b68c040b5c66619ccbc438a"
)


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def _ready():
    return benchmark.FixtureReadyRunner(delay_seconds=0.02)


def _failure_factories():
    def failed_then_ready(status: PrepareStatus, code: str):
        return benchmark._FirstThenRunner(
            benchmark._FailedRunner(status, code),
            _ready(),
        )

    return benchmark.FailureRunnerFactories(
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
        cancellation=lambda: benchmark._FirstThenRunner(
            benchmark.FixtureReadyRunner(delay_seconds=0.05),
            _ready(),
        ),
    )


def test_phase2_baseline_guard_accepts_only_frozen_evidence():
    receipt = benchmark._read_phase2_baseline(benchmark.DEFAULT_PHASE2_EVIDENCE)

    assert receipt["sha256"] == benchmark.PHASE2_EVIDENCE_SHA256
    assert receipt["measurement_head"] == ("8b40dc2b7b597f152fe5d0a152d3afe71c8ff05e")
    assert receipt["decision"] == "phase2_public_driver_load_gate_passed"
    assert receipt["production_route_changed_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
    ]
    assert receipt["trace_signal_path_route_changed"] is False
    assert receipt["opentitan_reference"] == {
        "ordered_input_count": 785,
        "ordered_top_count": 11,
        "coverage_boundary_path_count": 4,
        "requested_cone_path_count": 1,
        "coverage_status": "inconclusive",
        "blocking_diagnostic_count": 65,
        "representative_confidence": "partial",
    }


def test_phase2_baseline_guard_rejects_tampering(tmp_path):
    copied = tmp_path / "phase2.json"
    payload = json.loads(benchmark.DEFAULT_PHASE2_EVIDENCE.read_text())
    payload["assessment"]["trace_signal_path_route_changed"] = True
    copied.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="hash mismatch"):
        benchmark._read_phase2_baseline(copied)


def test_frozen_phase3a_ast_probe_detects_phase3b_without_x_or_wave_lock_changes():
    receipt = benchmark._route_isolation_receipt()

    assert receipt["accepted_head"] == benchmark.ACCEPTED_PHASE2_HEAD
    assert receipt["production_route_changed_tools"] == ["trace_signal_path"]
    assert receipt["phase3a_isolated"] is False
    assert receipt["functions"]["_route_public_connectivity"]["changed"] is True
    assert receipt["dispatch_branches"]["trace_x_source"]["changed"] is False
    for function_name in (
        "_run_trace_x_attempt",
        "_handle_trace_x_source",
        "_run_in_wave_thread",
        "_wave_locks_for",
    ):
        assert receipt["functions"][function_name]["changed"] is False


@pytest.mark.anyio
async def test_frozen_phase3a_executable_gate_detects_phase3b_reuse():
    result = await benchmark.run_benchmark_async(
        _args(
            "--workload",
            "hand_fixture",
            "--cold-repeats",
            "1",
            "--warm-repeats",
            "2",
            "--concurrent-requests",
            "3",
            "--cancellation-delay-seconds",
            "0.005",
        ),
        runner_factory=_ready,
        failure_factories=_failure_factories(),
        static_factory=connectivity_backend.StaticConnectivityBackend,
    )

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase3a"
    assessment = result["assessment"]
    assert assessment["decision"] == "phase3a_no_go_keep_auditing_trace_signal_path"
    assert assessment["phase3a_trace_signal_path_gate_passed"] is False
    assert assessment["production_route_changed_tools"] == ["trace_signal_path"]
    assert assessment["driver_load_route_changed"] is False
    assert assessment["trace_x_source_route_changed"] is False
    assert assessment["cross_target_reuse_implemented"] is False

    routes = result["route_probes"]
    assert routes["gate"]["passed"] is False
    assert {
        check["name"] for check in routes["gate"]["checks"] if not check["passed"]
    } == {
        "target_specific_identity_no_cross_target_reuse",
        "phase3a_ast_isolation",
    }
    assert routes["npi_success"]["result"]["actual_backend"] == "verdi_npi"
    assert (
        routes["source_graph_complete_not_connected"]["result"]["actual_backend"]
        == "source_graph"
    )
    assert (
        routes["source_graph_inconclusive_to_static"]["result"]["actual_backend"]
        == "static"
    )

    identity = routes["target_specific_identity"]
    keys = identity["build_keys"]
    assert keys["first"] == keys["repeated"]
    assert len({keys["first"], keys["changed_pair"], keys["changed_expand"]}) == 1
    assert identity["runtime_stats"]["actual_build_count"] == 1
    assert identity["cross_target_reuse_implemented"] is False

    assert result["workloads"][0]["gate"]["passed"] is True
    assert result["failure_probes"]["gate"]["passed"] is True
    for probe in result["failure_probes"]["results"].values():
        assert probe["cache_entry_count_after_failure"] == 0
        assert probe["inflight_count_after_failure"] == 0
        assert probe["retry"]["actual_backend"] == "source_graph"


def test_parent_import_does_not_load_optional_frontends():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import scripts.benchmark_source_graph_phase3a; "
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


def test_tracked_phase3a_evidence_passes_path_production_gate():
    raw = PHASE3A_EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PHASE3A_EVIDENCE_SHA256
    result = json.loads(raw)

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase3a"
    assert result["repository"]["head"] == benchmark.MEASUREMENT_HEAD
    script = ROOT / result["benchmark_script"]["path"]
    assert (
        hashlib.sha256(script.read_bytes()).hexdigest()
        == result["benchmark_script"]["sha256"]
    )
    assert result["before_baselines"]["phase1a"]["sha256"] == (
        benchmark.PHASE1A_EVIDENCE_SHA256
    )
    assert result["before_baselines"]["phase1b"]["sha256"] == (
        benchmark.PHASE1B_EVIDENCE_SHA256
    )
    assert result["before_baselines"]["phase2"]["sha256"] == (
        benchmark.PHASE2_EVIDENCE_SHA256
    )

    assessment = result["assessment"]
    assert assessment["decision"] == "phase3a_trace_signal_path_gate_passed"
    assert assessment["phase3a_trace_signal_path_gate_passed"] is True
    assert assessment["production_route_changed_tools"] == ["trace_signal_path"]
    assert assessment["driver_load_route_changed"] is False
    assert assessment["trace_x_source_route_changed"] is False
    assert assessment["cross_target_reuse_implemented"] is False
    assert assessment["waveform_locking_model_changed"] is False
    assert assessment["disk_cache"] is False
    assert assessment["persistent_worker"] is False
    assert assessment["startup_full_design_enumeration"] is False
    assert assessment["opentitan_claim"] == (
        "target-scoped only; no full-design path claim"
    )

    runtime = result["runtime_model"]
    assert runtime["different_endpoint_pair_may_be_cold"] is True
    assert runtime["cross_target_reuse_implemented"] is False
    assert runtime["disk_cache"] is False
    assert runtime["persistent_worker"] is False
    assert runtime["startup_build"] is False
    assert runtime["startup_full_design_enumeration"] is False
    assert runtime["same_key_single_flight"] is True

    routes = result["route_probes"]
    assert routes["gate"]["passed"] is True
    assert routes["scope_isolation"]["production_route_changed_tools"] == [
        "trace_signal_path"
    ]
    assert routes["scope_isolation"]["phase3a_isolated"] is True
    assert routes["npi_success"]["result"]["actual_backend"] == "verdi_npi"
    assert routes["npi_success"]["source_graph_runtime_get_count"] == 0
    assert (
        routes["source_graph_complete_not_connected"]["result"]["actual_backend"]
        == "source_graph"
    )
    assert (
        routes["source_graph_inconclusive_to_static"]["result"]["actual_backend"]
        == "static"
    )
    identity = routes["target_specific_identity"]
    assert identity["runtime_stats"]["actual_build_count"] == 3
    assert identity["cross_target_reuse_implemented"] is False

    workloads = {item["name"]: item for item in result["workloads"]}
    assert set(workloads) == {"hand_fixture", "opentitan_core"}
    for workload in workloads.values():
        assert workload["gate"]["passed"] is True
        aggregate = workload["path_operation"]["aggregate"]
        assert aggregate["actual_backends"] == ["source_graph"]
        assert aggregate["found_values"] == [True]
        assert aggregate["payload_backend_sets"] == [["source_graph"]]
        assert aggregate["actual_build_counts"] == [1]
        assert aggregate["warm_wall_time_ms"]["p95"] <= 100.0
        assert aggregate["peak_rss_kib"]["max"] <= 2_621_440
        assert workload["concurrent_same_key"]["actual_build_count"] == 1
        for field in (
            "build_fingerprints",
            "compile_fingerprints",
            "ir_fingerprints",
            "endpoint_pair_fingerprints",
        ):
            assert all(len(value) == 64 for value in aggregate[field])

    hand = workloads["hand_fixture"]
    assert hand["path_operation"]["aggregate"]["path_net_path_sets"] == [
        hand["expected_path"]
    ]
    opentitan = workloads["opentitan_core"]
    assert "target-scoped only" in result["assessment"]["opentitan_claim"]
    aggregate = opentitan["path_operation"]["aggregate"]
    assert aggregate["cold_wall_time_ms"]["p50"] <= 15_000.0
    assert aggregate["manifest_input_counts"] == [785]
    assert aggregate["manifest_top_counts"] == [11]
    assert aggregate["coverage_boundary_path_counts"] == [4]
    assert aggregate["requested_cone_path_counts"] == [4]
    assert aggregate["coverage_statuses"] == ["inconclusive"]
    assert aggregate["blocking_diagnostic_counts"] == [65]
    assert aggregate["query_confidences"] == ["partial"]

    failures = result["failure_probes"]
    assert failures["gate"]["passed"] is True
    for kind, probe in failures["results"].items():
        assert probe["cache_entry_count_after_failure"] == 0
        assert probe["inflight_count_after_failure"] == 0
        assert probe["retry"]["actual_backend"] == "source_graph"
        if kind == "cancellation":
            assert probe["failure"]["fallback_used"] is False
            assert probe["static_calls_after_failure"] == 0
        else:
            assert probe["failure"]["actual_backend"] == "static"
            assert probe["failure"]["source_graph"]["fallback_used"] is True
