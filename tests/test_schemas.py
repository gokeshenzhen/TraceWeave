import json

import pytest
from pydantic import ValidationError

from src.schemas import (
    BackendStatus,
    ErrorContextResult,
    GetSignalsByCycleResult,
    ParseSimLogResult,
    ProblemHints,
    RecommendNextStepsResult,
    ScanStructuralRisksResult,
    SearchSignalsBatchResult,
    SignalTransitionsResult,
    SimPathsResult,
    TraceXSourceResult,
    WaveformSummaryResult,
)


def test_sim_paths_result_minimal():
    data = {
        "verif_root": "/tmp/verif",
        "config_source": "auto",
        "discovery_mode": "case_dir",
        "fsdb_runtime": {"enabled": False},
    }
    result = SimPathsResult.model_validate(data)
    assert result.verif_root == "/tmp/verif"
    assert result.compile_logs == []
    assert json.loads(result.model_dump_json(exclude_none=True))["verif_root"] == "/tmp/verif"


def test_sim_paths_result_rejects_extra_fields():
    data = {
        "verif_root": "/tmp/verif",
        "config_source": "auto",
        "discovery_mode": "case_dir",
        "fsdb_runtime": {"enabled": False},
        "unexpected_field": "boom",
    }
    with pytest.raises(ValidationError):
        SimPathsResult.model_validate(data)


def test_parse_sim_log_result_with_problem_hints():
    data = {
        "log_file": "/tmp/sim.log",
        "simulator": "vcs",
        "schema_version": "2.0",
        "contract_version": "1.3",
        "failure_events_schema_version": "1.0",
        "parser_capabilities": [],
        "runtime_total_errors": 3,
        "runtime_fatal_count": 0,
        "runtime_error_count": 3,
        "unique_types": 2,
        "total_groups": 2,
        "truncated": False,
        "max_groups": 50,
        "first_error_line": 10,
        "problem_hints": {"has_x": True, "first_error_time_ps": 1000},
    }
    result = ParseSimLogResult.model_validate(data)
    assert isinstance(result.problem_hints, ProblemHints)
    assert result.problem_hints.has_x is True


def test_parse_sim_log_result_with_first_group_context():
    data = {
        "log_file": "/tmp/sim.log",
        "simulator": "vcs",
        "schema_version": "2.0",
        "contract_version": "1.3",
        "failure_events_schema_version": "1.0",
        "parser_capabilities": [],
        "runtime_total_errors": 1,
        "runtime_fatal_count": 0,
        "runtime_error_count": 1,
        "unique_types": 1,
        "total_groups": 1,
        "truncated": False,
        "max_groups": 50,
        "first_error_line": 10,
        "first_group_context": {
            "log_file": "/tmp/sim.log",
            "center_line": 10,
            "start_line": 1,
            "end_line": 20,
            "context": "line1\nline2\nERROR at line 10",
        },
    }
    result = ParseSimLogResult.model_validate(data)
    assert isinstance(result.first_group_context, ErrorContextResult)
    assert result.first_group_context.center_line == 10


def test_waveform_summary_json_roundtrip():
    result = WaveformSummaryResult.model_validate(
        {
            "file": "/tmp/wave.vcd",
            "format": "VCD",
            "timescale_ps": 1,
            "simulation_duration_ps": 200,
            "simulation_duration_ns": 0.2,
            "total_signals": 4,
        }
    )
    assert json.loads(result.model_dump_json(exclude_none=True))["format"] == "VCD"


def test_search_signals_batch_accepts_single_result_hint():
    result = SearchSignalsBatchResult.model_validate(
        {
            "batch": [
                {
                    "keyword": "clk",
                    "total_matched": 0,
                    "results": [],
                    "hint": "Use the full signal path.",
                }
            ],
            "hint": "One entry per keyword.",
        }
    )

    assert result.batch[0].hint == "Use the full signal path."


def test_get_signals_by_cycle_result_roundtrip():
    result = GetSignalsByCycleResult.model_validate(
        {
            "clock_path": "top_tb.clk",
            "edge": "posedge",
            "sample_offset_ps": 1,
            "clock_period_ps": 1000,
            "total_edges_found": 3,
            "start_cycle": 0,
            "num_cycles_requested": 2,
            "effective_num_cycles": 2,
            "num_cycles_returned": 2,
            "capped": False,
            "truncated": False,
            "cycles": [
                {
                    "cycle": 0,
                    "time_ps": 500,
                    "time_ns": 0.5,
                    "signals": {
                        "top_tb.data": {"bin": "0001", "hex": "0x1", "dec": 1},
                    },
                }
            ],
            "signal_errors": {},
        }
    )
    payload = json.loads(result.model_dump_json(exclude_none=True))
    assert payload["cycles"][0]["signals"]["top_tb.data"]["dec"] == 1


def test_signal_transitions_result_keeps_predecessor_separate():
    result = SignalTransitionsResult.model_validate(
        {
            "signal": "top.clk",
            "start_ps": 10,
            "end_ps": 20,
            "transition_count": 1,
            "transitions": [
                {"time_ps": 20, "value": {"bin": "1", "dec": 1}},
            ],
            "predecessor": {
                "time_ps": 5,
                "value": {"bin": "0", "dec": 0},
            },
        }
    )

    assert result.predecessor["time_ps"] == 5
    assert all(item["time_ps"] >= result.start_ps for item in result.transitions)


def test_backend_status_accepts_optional_lsf_receipt():
    status = BackendStatus.model_validate(
        {
            "simulator": "vcs",
            "backend": "verdi_npi",
            "actual_backend": "static",
            "fallback_reason": "npi_lsf_worker_failed",
            "execution_mode": "lsf",
            "scheduler_status": "failed",
            "worker_status": "failed",
        }
    )
    assert status.execution_mode == "lsf"
    assert status.scheduler_status == "failed"


def test_trace_x_source_result_carries_backend_consistency_receipt():
    result = TraceXSourceResult.model_validate(
        {
            "start_signal": "top_tb.dut.out",
            "start_time_ps": 100,
            "trace_status": "driver_unresolved",
            "trace_depth": 1,
            "max_depth": 10,
            "backend_status": {
                "simulator": "vcs",
                "backend": "verdi_npi",
                "actual_backend": "static",
                "fallback_reason": "npi_lsf_timeout",
                "execution_mode": "lsf",
                "scheduler_status": "timed_out",
                "worker_status": "not_started",
            },
            "trace_restarted": True,
        }
    )

    assert result.backend_status.backend == "verdi_npi"
    assert result.backend_status.actual_backend == "static"
    assert result.trace_restarted is True


def test_structural_scan_result_carries_explicit_coverage_receipt():
    result = ScanStructuralRisksResult.model_validate(
        {
            "scan_scope": "scope1",
            "eligible_file_count": 0,
            "files_scanned": 0,
            "coverage_status": "zero_coverage",
            "coverage_warnings": ["ZERO COVERAGE"],
            "total_risks": 0,
        }
    )

    assert result.coverage_status == "zero_coverage"
    assert result.eligible_file_count == 0
    assert result.coverage_warnings == ["ZERO COVERAGE"]


def test_recommend_result_preserves_runtime_protocol_coverage_without_findings():
    result = RecommendNextStepsResult.model_validate(
        {
            "suspected_failure_class": "data-path corruption",
            "runtime_protocol_findings": [],
            "runtime_protocol_coverage": {
                "coverage_status": "zero_coverage",
                "coverage_warnings": ["not a protocol pass"],
                "discovered_count": 0,
                "interface_count": 0,
                "flagged_count": 0,
            },
        }
    )

    assert result.runtime_protocol_findings == []
    assert result.runtime_protocol_coverage["coverage_status"] == "zero_coverage"
