from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_source_graph_phase3c as benchmark


ROOT = Path(__file__).resolve().parents[1]
PHASE3C_EVIDENCE = ROOT / "benchmarks/source_graph_connectivity_phase3c_results.json"
PHASE3C_EVIDENCE_SHA256 = (
    "8b77588206e9108edf7cb47e56979332e8f8205be873462911d30efabf3b19cf"
)


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def test_historical_baseline_guard_accepts_all_frozen_evidence():
    receipt = benchmark._read_historical_baselines()

    assert {name: item["sha256"] for name, item in receipt.items()} == {
        name: expected for name, (_, expected) in benchmark.HISTORICAL_EVIDENCE.items()
    }
    phase3b = receipt["phase3b"]
    assert phase3b["measurement_head"] == ("6ff8ffb3da780f1dab7f7f4645331536684518d9")
    accepted = phase3b["accepted_invariants"]
    assert accepted["same_artifact_mixed_driver_load_path_actual_build_count"] == 1
    assert accepted["opentitan_ordered_input_count"] == 785
    assert accepted["opentitan_ordered_top_count"] == 11
    assert accepted["opentitan_scope_path_count"] == 4
    assert accepted["opentitan_coverage_status"] == "inconclusive"
    assert accepted["opentitan_blocking_diagnostic_count"] == 65
    assert accepted["opentitan_representative_confidence"] == "partial"
    assert accepted["opentitan_claim"] == "target-scoped only"


def test_historical_baseline_guard_rejects_phase3b_tampering(tmp_path, monkeypatch):
    original, expected = benchmark.HISTORICAL_EVIDENCE["phase3b"]
    copied = tmp_path / "phase3b.json"
    payload = json.loads(original.read_text(encoding="utf-8"))
    payload["assessment"]["trace_x_source_route_changed"] = True
    copied.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setitem(
        benchmark.HISTORICAL_EVIDENCE,
        "phase3b",
        (copied, expected),
    )

    with pytest.raises(benchmark.BenchmarkError, match="hash mismatch"):
        benchmark._read_historical_baselines()


def test_historical_phase3c_guard_detects_authorized_public_route_evolution():
    receipt = benchmark._route_isolation_receipt()

    assert receipt["accepted_head"] == benchmark.ACCEPTED_TRACKED_HEAD
    assert receipt["trace_x_source_route_order"] == [
        "trusted_npi",
        "source_graph",
        "legacy_static",
    ]
    assert receipt["base_route_stage_order"] == {
        "_route_public_connectivity": [
            "trusted_npi",
            "source_graph",
            "legacy_static",
        ],
        "_route_public_signal_path": [
            "trusted_npi",
            "source_graph",
            "legacy_static",
        ],
    }
    # Phase 3C froze the whole public-connectivity function body. A later,
    # explicitly authorized Source Graph frontier enhancement changes that
    # body while preserving backend order and the waveform locking model.
    assert receipt["production_base_ordering_changed"] is True
    assert receipt["trace_x_source_route_changed"] is True
    assert receipt["waveform_locking_model_changed"] is False
    assert receipt["phase3b_reuse_behavior_affected_tools"] == [
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
    ]
    assert receipt["phase3c_new_process_memory_consumer"] == "trace_x_source"
    assert receipt["functions"]["_route_public_connectivity"]["changed"] is True
    assert receipt["functions"]["_route_public_signal_path"]["changed"] is False
    for name in ("_run_in_wave_thread", "_wave_locks_for"):
        assert receipt["functions"][name]["changed"] is False


@pytest.mark.anyio
async def test_fake_phase3c_gate_covers_trace_routing_reuse_and_cleanup():
    result = await benchmark.run_benchmark_async(_args("--warm-repeats", "2"))

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase3c"
    assessment = result["assessment"]
    assert assessment["decision"] == (
        "phase3c_no_go_keep_auditing_trace_x_source_integration"
    )
    assert not assessment["phase3c_trace_x_source_graph_integration_gate_passed"]
    assert not assessment["architecture_gate_passed"]
    assert result["route_isolation"]["production_base_ordering_changed"] is True
    assert assessment["trace_x_source_route_changed"] is True
    assert assessment["production_base_ordering_changed"] is False
    assert assessment["waveform_locking_model_changed"] is False
    assert assessment["source_graph_x_trace_process_memory_artifact_reuse"] is True
    assert assessment["same_artifact_multi_node_actual_build_count"] == 1
    assert assessment["cross_process_reuse"] is False
    assert assessment["disk_cache"] is False
    assert assessment["persistent_worker"] is False
    assert assessment["startup_full_design_enumeration"] is False
    assert assessment["no_mixed_backend_or_artifact_provenance"] is True
    assert assessment["cancellation_fallback"] is False
    assert assessment["failure_or_cancellation_cache_polluted"] is False
    assert assessment["phase3b_driver_load_path_correctness_and_reuse_unchanged"]

    core = result["core_x_trace"]
    assert core["gate"]["passed"] is True
    assert core["cold"]["source_graph"]["query_count"] == 2
    assert core["cold"]["source_graph"]["metrics"]["actual_build_count"] == 1
    assert core["worker_calls"] == 1
    assert core["warm_wall_time_ms"]["p95"] <= 100.0
    assert all(
        item["source_graph"]["artifact_reuse"] == "exact_hit" for item in core["warm"]
    )

    expansion = result["scope_expansion"]
    assert expansion["gate"]["passed"] is True
    source = expansion["expanded_trace"]["source_graph"]
    assert source["artifact_attempt_count"] == 2
    assert source["scope_expansion_count"] == 1
    assert source["single_artifact_provenance"] is True
    assert source["final_artifact_scope_match"] is True
    assert expansion["unrelated_sibling_or_full_hierarchy_enumerated"] is False

    routes = result["route_probes"]
    assert routes["gate"]["passed"] is True
    assert routes["trusted_npi_terminal"]["actual_backend"] == "verdi_npi"
    assert routes["npi_to_source_graph_restart"]["actual_backend"] == "source_graph"
    assert routes["source_graph_to_static_restart"]["actual_backend"] == "static"
    assert routes["no_mixed_provenance"] is True

    assert result["failure_probes"]["gate"]["passed"] is True
    assert set(result["failure_probes"]["results"]) == {
        "dependency_blocked",
        "build_failed",
        "worker_crash",
        "timed_out",
    }
    assert result["cancellation_probe"]["gate"]["passed"] is True
    assert result["query_cancellation_probe"]["gate"]["passed"] is True
    assert result["concurrency_probe"]["gate"]["passed"] is True
    assert result["different_artifact_admission_probe"]["gate"]["passed"]
    assert result["capacity_probe"]["gate"]["passed"] is True
    # The historical probe required every dotted suffix to be rejected as an
    # unproved instance. Packed-member support now defers that suffix to exact
    # IR declaration lookup, so this superseded Phase 3C sub-gate is expected
    # to report false along with the already expected overall no-go.
    assert result["scope_blocker_probes"]["gate"]["passed"] is False
    assert result["driver_load_path_compatibility"]["gate"]["passed"] is True
    assert result["opentitan_x_trace"]["status"] == "unavailable"
    assert result["opentitan_x_trace"]["bounded_measurement_performed"] is False


def test_tracked_phase3c_evidence_is_immutable_and_passes_all_gates():
    raw = PHASE3C_EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PHASE3C_EVIDENCE_SHA256
    payload = json.loads(raw)

    assert payload["schema_version"] == "1.0"
    assert payload["benchmark"] == "source_graph_connectivity_phase3c"
    assert payload["repository"]["head"] == benchmark.MEASUREMENT_HEAD
    script = ROOT / payload["benchmark_script"]["path"]
    assert (
        hashlib.sha256(script.read_bytes()).hexdigest()
        == (payload["benchmark_script"]["sha256"])
    )
    assessment = payload["assessment"]
    assert assessment["decision"] == (
        "phase3c_trace_x_source_graph_integration_gate_passed"
    )
    assert assessment["phase3c_trace_x_source_graph_integration_gate_passed"]
    assert assessment["production_base_ordering"] == (
        "trusted NPI -> Source Graph -> whole-result Static"
    )
    assert assessment["trace_x_source_route_changed"] is True
    assert assessment["phase3b_driver_load_path_correctness_and_reuse_unchanged"]
    assert assessment["source_graph_x_trace_process_memory_artifact_reuse"] is True
    assert assessment["cross_process_reuse"] is False
    assert assessment["waveform_locking_model_changed"] is False
    assert assessment["disk_cache"] is False
    assert assessment["persistent_worker"] is False
    assert assessment["startup_full_design_enumeration"] is False
    assert payload["core_x_trace"]["gate"]["passed"] is True
    assert payload["scope_expansion"]["gate"]["passed"] is True
    assert payload["route_probes"]["gate"]["passed"] is True
    assert payload["failure_probes"]["gate"]["passed"] is True
    assert payload["cancellation_probe"]["gate"]["passed"] is True
    assert payload["query_cancellation_probe"]["gate"]["passed"] is True
    assert payload["concurrency_probe"]["gate"]["passed"] is True
    assert payload["different_artifact_admission_probe"]["gate"]["passed"]
    assert payload["capacity_probe"]["gate"]["passed"] is True
    assert payload["scope_blocker_probes"]["gate"]["passed"] is True
    assert payload["driver_load_path_compatibility"]["gate"]["passed"] is True
    assert payload["opentitan_x_trace"]["status"] == "unavailable"

    assert {
        name: item["sha256"] for name, item in payload["before_baselines"].items()
    } == {
        name: expected for name, (_, expected) in benchmark.HISTORICAL_EVIDENCE.items()
    }


def test_cli_writes_atomic_phase3c_result(tmp_path):
    output = tmp_path / "phase3c.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/benchmark_source_graph_phase3c.py"),
            "--warm-repeats",
            "2",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # This historical gate correctly returns no-go after the later authorized
    # public-connectivity route enhancement; it must still emit atomic evidence.
    assert completed.returncode == 1, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert not payload["assessment"][
        "phase3c_trace_x_source_graph_integration_gate_passed"
    ]
    assert payload["route_isolation"]["production_base_ordering_changed"] is True
    assert list(tmp_path.glob(".phase3c.json.*.tmp")) == []
