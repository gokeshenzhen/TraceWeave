from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from scripts import benchmark_source_graph_phase3d as benchmark


ROOT = Path(__file__).resolve().parents[1]
PHASE3D_EVIDENCE = ROOT / "benchmarks/source_graph_connectivity_phase3d_results.json"
PHASE3D_EVIDENCE_SHA256 = (
    "7200c6a79b763ec261663acd322748de025623197cf10d1635263a5e8b391d5b"
)


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def test_historical_evidence_guard_preserves_phase1a_through_phase3c():
    receipts = benchmark._read_historical_evidence()

    assert {name: item["sha256"] for name, item in receipts.items()} == {
        name: values[1] for name, values in benchmark.HISTORICAL_EVIDENCE.items()
    }
    assert receipts["phase3c"] == {
        "path": "benchmarks/source_graph_connectivity_phase3c_results.json",
        "sha256": "8b77588206e9108edf7cb47e56979332e8f8205be873462911d30efabf3b19cf",
        "schema_version": "1.0",
        "benchmark": "source_graph_connectivity_phase3c",
        "measurement_head": "d10ad7fbdc2581318c3f3e448112b6415943fa63",
        "decision": "phase3c_trace_x_source_graph_integration_gate_passed",
    }


def test_fake_phase3d_gate_uses_distinct_processes_and_stops_before_closure():
    result = benchmark.run_benchmark(
        _args(
            "--repeats",
            "1",
            "--memory-repeats",
            "2",
            "--skip-correctness-suite",
        )
    )

    assert result["schema_version"] == "1.0"
    assert result["benchmark"] == "source_graph_connectivity_phase3d"
    assessment = result["assessment"]
    assert assessment["decision"] == (
        "phase3d_correctness_mvp_passed_performance_gate_unavailable"
    )
    assert assessment["phase3d_correctness_mvp_passed"] is True
    assert assessment["phase3d_bounded_disk_cache_gate_passed"] is False
    assert assessment["representative_performance_gate_available"] is False
    assert assessment["disk_cache_opt_in"] is True
    assert assessment["disk_cache_default_enabled"] is False
    assert assessment["persistent_worker"] is False
    assert assessment["phase3e_started"] is False

    workload = result["workloads"][0]
    assert workload["name"] == "hand_fixture"
    assert workload["gate"]["passed"] is True
    pair = workload["pairs"][0]
    cold = pair["cold_miss_process"]
    disk = pair["disk_hit_process"]
    assert cold["process_id"] != disk["process_id"]
    assert cold["primary"]["source_graph"]["cache_tier"] == "build"
    assert cold["primary"]["source_graph"]["disk_validation_outcome"] == ("not_found")
    assert cold["frontend"]["actual_build_count"] == 1
    assert disk["primary"]["source_graph"]["cache_tier"] == "disk"
    assert disk["primary"]["source_graph"]["artifact_reuse"] == "disk_exact_hit"
    assert disk["frontend"]["actual_build_count"] == 0
    assert disk["frontend"]["launch_count"] == 0
    assert disk["adapter_full_content_validation"] is True
    assert pair["cold_disk_equivalence"]["passed"] is True
    assert all(
        item["source_graph"]["cache_tier"] == "memory"
        and item["source_graph"]["metrics"]["disk_lookup_wall_ms"] == 0
        for item in disk["memory_hits"]
    )
    assert workload["aggregate"]["cross_process_exact_hit_rate"] == 1.0
    assert result["corruption_recovery"]["gate"]["passed"] is True
    assert result["x_trace_correctness"]["gate"]["passed"] is True
    assert result["architecture"]["waveform_locking_model_changed"] is False
    assert result["architecture"]["startup_cache_scan"] is False


def test_cli_writes_fake_phase3d_result_atomically(tmp_path):
    output = tmp_path / "phase3d.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/benchmark_source_graph_phase3d.py"),
            "--repeats",
            "1",
            "--memory-repeats",
            "1",
            "--skip-correctness-suite",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["assessment"]["phase3d_correctness_mvp_passed"] is True
    assert payload["assessment"]["representative_performance_gate_available"] is (False)
    assert list(tmp_path.glob(".phase3d.json.*.tmp")) == []


def test_tracked_phase3d_evidence_is_immutable_and_passes_all_gates():
    raw = PHASE3D_EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PHASE3D_EVIDENCE_SHA256
    payload = json.loads(raw)

    assert payload["schema_version"] == "1.0"
    assert payload["benchmark"] == "source_graph_connectivity_phase3d"
    assert payload["repository"]["head"] == benchmark.MEASUREMENT_HEAD
    script = ROOT / payload["benchmark_script"]["path"]
    assert (
        hashlib.sha256(script.read_bytes()).hexdigest()
        == payload["benchmark_script"]["sha256"]
    )
    assert {
        name: item["sha256"] for name, item in payload["historical_evidence"].items()
    } == {name: values[1] for name, values in benchmark.HISTORICAL_EVIDENCE.items()}
    assert (
        payload["pre_implementation_baseline"]["hand_fixture"][
            "session_local_raw_sha256"
        ]
        == "662084a3486314a814a685454f2129694ecc0dfbf69f7b3278c00dccd86c64df"
    )
    assert (
        payload["pre_implementation_baseline"]["opentitan_core"][
            "session_local_raw_sha256"
        ]
        == "4290fc2910b5c4ee1ec2eba0ed210e3612688e07d3eb58e88cbfdb9cfd3a3853"
    )

    assessment = payload["assessment"]
    assert assessment["decision"] == "phase3d_bounded_disk_cache_gate_passed"
    assert assessment["phase3d_bounded_disk_cache_gate_passed"] is True
    assert assessment["phase3d_correctness_mvp_passed"] is True
    assert assessment["representative_performance_gate_available"] is True
    assert assessment["representative_performance_gate_passed"] is True
    assert assessment["disk_cache_opt_in"] is True
    assert assessment["disk_cache_default_enabled"] is False
    assert assessment["memory_cache_first"] is True
    assert assessment["verified_disk_hit_frontend_build_count"] == 0
    assert assessment["verified_disk_hit_frontend_launch_count"] == 0
    assert assessment["no_mixed_backend_or_artifact_provenance"] is True
    assert assessment["cancellation_fallback"] is False
    assert assessment["waveform_locking_model_changed"] is False
    assert assessment["persistent_worker"] is False
    assert assessment["startup_full_design_enumeration"] is False
    assert assessment["startup_cache_scan"] is False
    assert assessment["sqlite_or_global_database"] is False
    assert assessment["phase3e_started"] is False
    assert assessment["default_on_authorized"] is False

    assert payload["correctness_suite"]["gate"]["passed"] is True
    assert payload["correctness_suite"]["passed_count"] >= 170
    assert payload["corruption_recovery"]["gate"]["passed"] is True
    assert payload["x_trace_correctness"]["gate"]["passed"] is True
    assert payload["architecture"]["wave_lock_gate_passed"] is True
    assert payload["architecture"]["global_python_without_pyslang_import"] is True

    workloads = {item["name"]: item for item in payload["workloads"]}
    assert set(workloads) == {"hand_fixture", "opentitan_core"}
    for workload in workloads.values():
        assert workload["run_count"] == 3
        assert workload["gate"]["passed"] is True
        assert workload["aggregate"]["cross_process_exact_hit_count"] == 3
        assert workload["aggregate"]["cross_process_exact_hit_rate"] == 1.0
        for pair in workload["pairs"]:
            cold = pair["cold_miss_process"]
            disk = pair["disk_hit_process"]
            assert cold["process_id"] != disk["process_id"]
            assert cold["frontend"]["actual_build_count"] == 1
            assert disk["frontend"]["actual_build_count"] == 0
            assert disk["frontend"]["launch_count"] == 0
            assert disk["frontend"]["child_rss_peak_kib"] is None
            assert disk["adapter_full_content_validation"] is True
            assert pair["cold_disk_equivalence"]["passed"] is True
            assert all(
                item["source_graph"]["metrics"]["disk_lookup_wall_ms"] == 0
                for item in disk["memory_hits"]
            )
            source_receipt = json.dumps(disk["primary"]["source_graph"], sort_keys=True)
            assert "cache_root" not in source_receipt
            assert "namespace_root" not in source_receipt

    hand_gate = payload["performance_gates"]["hand_fixture"]
    assert hand_gate["passed"] is True
    assert hand_gate["small_workload_speedup_claim"] is False
    open_gate = payload["performance_gates"]["opentitan_core"]
    assert open_gate["status"] == "measured"
    assert open_gate["passed"] is True
    assert open_gate["disk_hit_p95_ms"] <= 1_500.0
    assert open_gate["cold_to_disk_p95_reduction_percent"] >= 75.0
    assert open_gate["disk_hit_frontend_child_peak_rss_kib"]["count"] == 0
    assert payload["opentitan_x_trace"] == {
        "status": "unavailable",
        "synthetic_evidence_generated": False,
        "bounded_measurement_performed": False,
    }
