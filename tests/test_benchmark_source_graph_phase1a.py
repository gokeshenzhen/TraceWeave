from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import benchmark_source_graph_phase1a as benchmark
from src.connectivity_ir import CoverageGap, CoverageStatus, SignalSelection
from src.connectivity_query import QueryStatus


ROOT = Path(__file__).resolve().parents[1]


def _args(*extra: str):
    return benchmark.build_argument_parser().parse_args(list(extra))


def _measured_run(*, fingerprint: str = "a" * 64) -> dict:
    latency = {
        "wall_latency_ms": {"samples": [0.1, 0.2]},
        "cpu_latency_ms": {"samples": [0.08, 0.18]},
    }
    return {
        "schema_version": benchmark.SCHEMA_VERSION,
        "benchmark": benchmark.BENCHMARK_NAME,
        "workload": "hand_fixture",
        "status": "measured",
        "worker_process_wall_time_ms": 250.0,
        "process_rss_kib": {"peak": 64_000},
        "ir": {"fingerprint_sha256": fingerprint},
        "correctness": {"all_passed": True},
        "phases": {
            "frontend_build": {},
            "minimal_projection": {},
            "serialization": {},
            "query_index_build": {},
            "warm_queries": {
                "driver": deepcopy(latency),
                "load": deepcopy(latency),
            },
        },
    }


def test_fixture_specs_use_only_tracked_inputs_and_need_no_optional_frontend():
    specs = benchmark.build_workload_specs(
        _args("--workload", "deep_x_npi", "--workload", "hand_fixture")
    )

    assert [item["workload"] for item in specs] == [
        "deep_x_npi",
        "hand_fixture",
    ]
    for spec in specs:
        assert spec["focus_instance_paths"] == []
        assert spec["assignment_instance_paths"] == []
        assert len(spec["input_fingerprint_sha256"]) == 64
        assert spec["source_facts"]["missing_sources"] == []
        assert all(
            Path(path).resolve().is_relative_to(ROOT)
            for path in spec["source_facts"]["source_paths"]
        )


def test_opentitan_requires_explicit_manual_compile_artifact():
    with pytest.raises(benchmark.BenchmarkError, match="real-compile-log"):
        benchmark.build_workload_specs(_args("--workload", "opentitan_core"))


def test_opentitan_exclusions_make_runtime_gaps_explicit():
    spec = {
        "workload": "opentitan_core",
        "translation_receipt": {
            "unsupported_options": [
                {"impact": "external_runtime_not_modeled"},
                {"impact": "external_runtime_not_modeled"},
                {"impact": "requires_workload_review"},
            ]
        },
    }

    exclusions = benchmark._projection_exclusions(spec)
    codes = {item.code for item in exclusions}

    assert {
        "uvm_dynamic_connectivity_not_modeled",
        "dpi_runtime_not_modeled",
        "procedural_force_not_modeled",
        "bind_semantics_incomplete",
        "protected_payload_not_modeled",
        "compile_option_exclusion:external_runtime_not_modeled",
        "compile_option_exclusion:requires_workload_review",
    } <= codes
    assert all(item.impact.value == "inconclusive" for item in exclusions)


def test_worker_result_contract_requires_all_separate_measurement_phases():
    result = _measured_run()

    benchmark._validate_worker_result(result, "hand_fixture")
    del result["phases"]["minimal_projection"]

    with pytest.raises(benchmark.BenchmarkError, match="lacks measured phases"):
        benchmark._validate_worker_result(result, "hand_fixture")


def test_aggregate_uses_only_successful_warm_samples_and_checks_fingerprint():
    runs = [_measured_run(), _measured_run()]

    aggregate = benchmark._aggregate_runs(runs)
    gate = benchmark._workload_gate("hand_fixture", aggregate)

    assert aggregate["measured_run_count"] == 2
    assert aggregate["ir_fingerprint_stable"] is True
    assert aggregate["warm_queries"]["driver"]["wall_latency_ms"]["count"] == 4
    assert gate["passed"] is True

    runs[1]["ir"]["fingerprint_sha256"] = "b" * 64
    unstable = benchmark._aggregate_runs(runs)
    assert unstable["ir_fingerprint_stable"] is False
    assert benchmark._workload_gate("hand_fixture", unstable)["passed"] is False


def test_opentitan_gate_enforces_cold_and_memory_limits():
    aggregate = benchmark._aggregate_runs([_measured_run()])
    aggregate["cold_worker_wall_ms"]["p50"] = 15_001.0

    gate = benchmark._workload_gate("opentitan_core", aggregate)
    by_name = {item["name"]: item for item in gate["checks"]}

    assert gate["passed"] is False
    assert by_name["opentitan_cold_prepare_p50"]["passed"] is False
    assert by_name["opentitan_peak_rss_p50"]["passed"] is True


def test_percentiles_and_input_validation_are_deterministic():
    assert benchmark._percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert benchmark._sample_summary([3.0, 1.0, 2.0])["p95"] == 2.9

    args = _args("--cold-repeats", "0")
    with pytest.raises(benchmark.BenchmarkError, match="repeat counts"):
        benchmark.run_benchmark(args)


def test_query_receipt_bounds_repeated_global_coverage_gaps():
    gaps = tuple(
        CoverageGap(
            code=f"gap_{index}",
            message="global projection limitation",
            impact=CoverageStatus.INCONCLUSIVE,
            scopes=("*",),
        )
        for index in range(benchmark.MAX_RESULT_GAP_ITEMS + 5)
    )
    result = SimpleNamespace(
        operation="driver",
        signal=SignalSelection("sig", (0,), "top"),
        status=QueryStatus.INCONCLUSIVE,
        coverage_status=CoverageStatus.INCONCLUSIVE,
        matches=(),
        unresolved_boundaries=gaps,
        traversed_binding_edges=0,
        max_depth=64,
    )

    receipt = benchmark._compact_query_result(result)

    assert receipt["unresolved_boundary_count"] == len(gaps)
    assert len(receipt["unresolved_boundaries"]) == benchmark.MAX_RESULT_GAP_ITEMS
    assert receipt["unresolved_boundaries_truncated"] is True
