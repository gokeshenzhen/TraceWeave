import json

import pytest
from pydantic import ValidationError

from src.schemas import (
    ErrorContextResult,
    GetSignalsByCycleResult,
    ParseSimLogResult,
    ProblemHints,
    SimPathsResult,
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
