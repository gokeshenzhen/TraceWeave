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
    TraceSignalPathResult,
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
    assert (
        json.loads(result.model_dump_json(exclude_none=True))["verif_root"]
        == "/tmp/verif"
    )


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


def test_backend_status_keeps_legacy_payload_backward_compatible():
    status = BackendStatus.model_validate(
        {
            "simulator": "vcs",
            "backend": "static",
            "actual_backend": "static",
        }
    )

    assert status.backend == "static"
    assert status.selected_backend is None
    assert status.attempted_backend is None
    assert status.attempted_backends == []
    assert status.source_graph is None
    assert status.connectivity_route == "auto"
    assert status.connectivity_route_error is None


def test_backend_status_validates_additive_source_graph_route_receipt():
    status = BackendStatus.model_validate(
        {
            "simulator": "xcelium",
            "backend": "source_graph",
            "selected_backend": "source_graph",
            "attempted_backend": "source_graph",
            "actual_backend": "source_graph",
            "connectivity_route": "source_graph",
            "attempted_backends": [
                {
                    "backend": "verdi_npi",
                    "status": "skipped",
                    "reason": "npi_skipped_by_policy",
                },
                {
                    "backend": "source_graph",
                    "status": "success",
                    "coverage_status": "partial",
                },
            ],
            "source_graph": {
                "adapter_status": "ready",
                "prepare_status": "ready",
                "cache_disposition": "miss",
                "flight_disposition": "builder",
                "coverage_status": "partial",
                "coverage_files_total": 785,
                "coverage_files_projected": 4,
                "coverage_diagnostic_count": 29416,
                "coverage_blocking_diagnostic_count": 65,
                "coverage_gap_count": 388,
                "coverage_gap_codes": ["protected_payload"],
                "query_status": "found",
                "query_confidence": "partial",
                "query_match_count": 1,
                "build_key_sha256": "b" * 64,
                "artifact_fingerprint_sha256": "b" * 64,
                "selected_artifact_fingerprint_sha256": "d" * 64,
                "query_fingerprint_sha256": "e" * 64,
                "artifact_reuse": "dominating_hit",
                "cache_lookup_reason": "dominating_artifact",
                "scope_match": {
                    "relation": "superset",
                    "reusable": True,
                    "complete_for_request": False,
                    "reason": "coverage_preserved_partial",
                },
                "compile_fingerprint_sha256": "c" * 64,
                "ir_fingerprint_sha256": "a" * 64,
                "metrics": {
                    "prepare_total_wall_ms": 5.0,
                    "actual_build_count": 1,
                    "ir_bytes": 123,
                    "cache_bytes": 123,
                    "cache_entry_count": 2,
                    "cache_peak_entry_count": 3,
                    "cache_peak_bytes": 456,
                    "cache_eviction_count": 1,
                },
            },
        }
    )

    assert status.actual_backend == "source_graph"
    assert status.connectivity_route == "source_graph"
    assert status.attempted_backends[0].status == "skipped"
    assert status.source_graph.query_confidence == "partial"
    assert status.source_graph.coverage_files_total == 785
    assert status.source_graph.coverage_files_projected == 4
    assert status.source_graph.coverage_diagnostic_count == 29416
    assert status.source_graph.coverage_blocking_diagnostic_count == 65
    assert status.source_graph.coverage_gap_count == 388
    assert status.source_graph.build_key_sha256 == "b" * 64
    assert status.source_graph.artifact_reuse == "dominating_hit"
    assert status.source_graph.cache_lookup_reason == "dominating_artifact"
    assert status.source_graph.scope_match.relation == "superset"
    assert status.source_graph.metrics.cache_eviction_count == 1
    assert status.source_graph.metrics.actual_build_count == 1
    serialized = status.model_dump(mode="json")["source_graph"]
    assert "cache_tier" not in serialized
    assert "disk_validation_outcome" not in serialized
    assert "frontend_launch_count" not in serialized["metrics"]
    assert all(not key.startswith("disk_") for key in serialized["metrics"])


def test_backend_status_validates_additive_exact_disk_cache_receipt():
    status = BackendStatus.model_validate(
        {
            "simulator": "xcelium",
            "backend": "source_graph",
            "actual_backend": "source_graph",
            "source_graph": {
                "adapter_status": "ready",
                "prepare_status": "ready",
                "cache_disposition": "miss",
                "cache_tier": "disk",
                "disk_validation_outcome": "hit",
                "artifact_reuse": "disk_exact_hit",
                "cache_lookup_reason": "no_cached_artifact",
                "metrics": {
                    "actual_build_count": 0,
                    "frontend_launch_count": 0,
                    "disk_lookup_wall_ms": 3.25,
                    "disk_read_wall_ms": 1.5,
                    "disk_validate_wall_ms": 1.75,
                    "disk_hit_count": 1,
                    "disk_build_skip_count": 1,
                    "disk_bytes_read": 8192,
                    "disk_entry_count": 1,
                    "disk_bytes": 9000,
                },
            },
        }
    )

    receipt = status.source_graph
    assert receipt.cache_disposition == "miss"
    assert receipt.cache_tier == "disk"
    assert receipt.disk_validation_outcome == "hit"
    assert receipt.artifact_reuse == "disk_exact_hit"
    assert receipt.metrics.actual_build_count == 0
    assert receipt.metrics.frontend_launch_count == 0
    assert receipt.metrics.disk_build_skip_count == 1
    assert receipt.metrics.disk_bytes_read == 8192

    with pytest.raises(ValidationError):
        BackendStatus.model_validate(
            {
                "simulator": "xcelium",
                "backend": "source_graph",
                "source_graph": {
                    "adapter_status": "ready",
                    "cache_tier": "/private/cache",
                },
            }
        )


def test_trace_signal_path_validates_additive_source_graph_path_evidence():
    result = TraceSignalPathResult.model_validate(
        {
            "from_signal": "top.u_src.out[7:0]",
            "to_signal": "top.u_dst.in[7:0]",
            "found": True,
            "hops": 1,
            "expand_assigns": True,
            "backend": "source_graph",
            "path": [
                {
                    "index": 0,
                    "net_path": "top.u_src.out[7:0]",
                    "scope_inst": "top.u_src",
                    "source_info_origin": "source_graph",
                    "backend": "source_graph",
                    "is_endpoint": True,
                },
                {
                    "index": 1,
                    "net_path": "top.u_dst.in[7:0]",
                    "scope_inst": "top.u_dst",
                    "source_file": "/rtl/top.sv",
                    "source_line": 17,
                    "source_info_origin": "source_graph",
                    "backend": "source_graph",
                    "edge_kind": "continuous_assign",
                    "edge_id": "edge-1",
                    "edge_source_path": "/rtl/top.sv",
                    "exact_bit_mapping": True,
                    "is_endpoint": True,
                },
            ],
            "backend_status": {
                "backend": "source_graph",
                "actual_backend": "source_graph",
                "source_graph": {
                    "adapter_status": "ready",
                    "query_status": "found",
                    "query_confidence": "partial",
                    "path_edge_count": 1,
                    "traversed_edge_count": 3,
                    "visited_state_count": 4,
                    "traversal_limit": 4096,
                    "output_limit": 256,
                    "endpoint_alias_equivalent": False,
                    "expand_assigns": True,
                },
            },
        }
    )

    assert result.backend == "source_graph"
    assert result.path[1].edge_kind == "continuous_assign"
    assert result.path[1].exact_bit_mapping is True
    assert result.backend_status.source_graph.path_edge_count == 1
    assert result.backend_status.source_graph.traversal_limit == 4096


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
                "whole_trace_restart_count": 2,
                "whole_trace_restart_reasons": [
                    "npi_internal_fallback",
                    "source_graph_to_static",
                ],
                "single_backend_provenance": True,
                "fallback_reason": "npi_lsf_timeout",
                "execution_mode": "lsf",
                "scheduler_status": "timed_out",
                "worker_status": "not_started",
                "source_graph": {
                    "adapter_status": "ready",
                    "query_count": 2,
                    "attempted_query_count": 3,
                    "query_fingerprints_sha256": ["a" * 64, "b" * 64],
                    "query_statuses": ["found", "not_connected"],
                    "coverage_statuses": ["partial", "complete"],
                    "positive_query_count": 1,
                    "complete_negative_query_count": 1,
                    "artifact_attempt_count": 2,
                    "scope_expansion_count": 1,
                    "attempted_artifact_fingerprints_sha256": [
                        "c" * 64,
                        "d" * 64,
                    ],
                    "final_artifact_fingerprint_sha256": "d" * 64,
                    "single_artifact_provenance": True,
                    "final_artifact_scope_match": True,
                    "fallback_used": True,
                },
            },
            "trace_restarted": True,
        }
    )

    assert result.backend_status.backend == "verdi_npi"
    assert result.backend_status.actual_backend == "static"
    assert result.backend_status.whole_trace_restart_count == 2
    assert result.backend_status.single_backend_provenance is True
    assert result.backend_status.source_graph.query_count == 2
    assert result.backend_status.source_graph.artifact_attempt_count == 2
    assert result.backend_status.source_graph.single_artifact_provenance is True
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
