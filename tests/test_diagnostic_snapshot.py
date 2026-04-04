import pytest

import server
import src.schemas as schemas


def _make_sim_paths_result() -> schemas.SimPathsResult:
    return schemas.SimPathsResult.model_validate({
        "verif_root": "/tmp/verif",
        "case_name": "case0",
        "config_source": "auto",
        "config_root": None,
        "discovery_mode": "root_dir",
        "case_dir": "/tmp/verif/work/work_case0",
        "simulator": "xcelium",
        "fsdb_runtime": {"enabled": False},
        "compile_logs": [
            {
                "path": "/tmp/verif/work/elab.log",
                "size": 10,
                "mtime": "2026-03-23T00:00:00",
                "age_hours": 1.0,
                "phase": "elaborate",
                "is_mixed": False,
            }
        ],
        "sim_logs": [
            {
                "path": "/tmp/verif/work/work_case0/irun.log",
                "size": 10,
                "mtime": "2026-03-23T00:00:00",
                "age_hours": 1.0,
                "phase": "runtime",
                "is_mixed": False,
            },
            {
                "path": "/tmp/verif/work/work_case0/irun_rerun.log",
                "size": 10,
                "mtime": "2026-03-23T00:10:00",
                "age_hours": 0.8,
                "phase": "runtime",
                "is_mixed": False,
            }
        ],
        "wave_files": [
            {
                "path": "/tmp/verif/work/work_case0/top_tb.vcd",
                "size": 10,
                "mtime": "2026-03-23T00:00:00",
                "age_hours": 1.0,
                "format": "vcd",
            },
            {
                "path": "/tmp/verif/work/work_case0/top_tb_alt.vcd",
                "size": 10,
                "mtime": "2026-03-23T00:05:00",
                "age_hours": 0.9,
                "format": "vcd",
            }
        ],
        "available_cases": [],
        "hints": ["prefer vcd"],
        "next_required_step": None,
    })


def _make_hierarchy_result() -> schemas.BuildTbHierarchyResult:
    return schemas.BuildTbHierarchyResult.model_validate({
        "project": {"top_module": "top_tb", "source_root": "/tmp/src", "simulator": "xcelium"},
        "files": {
            "rtl": [{"name": "dut.sv", "path": "/tmp/src/dut.sv", "type": "module"}],
            "tb": [{"name": "top_tb.sv", "path": "/tmp/src/top_tb.sv", "type": "module"}],
        },
        "component_tree": {
            "top_tb": {
                "type": "module",
                "class": "top_tb",
                "src": "top_tb.sv",
                "role": "tb",
                "children": {
                    "dut": {
                        "type": "module",
                        "class": "dut",
                        "src": "dut.sv",
                        "role": "dut",
                    }
                },
            }
        },
        "class_hierarchy": [],
        "interfaces": [{"name": "bus_if", "src": "bus_if.sv", "bound_in": "top_tb"}],
        "compile_result": {},
    })


def _make_parse_result(total_errors: int = 3) -> schemas.ParseSimLogResult:
    return schemas.ParseSimLogResult.model_validate({
        "log_file": "/tmp/verif/work/work_case0/irun.log",
        "simulator": "xcelium",
        "schema_version": "2.0",
        "contract_version": "1.3",
        "failure_events_schema_version": "1.0",
        "parser_capabilities": ["mixed_log_detection"],
        "runtime_total_errors": total_errors,
        "runtime_fatal_count": 0,
        "runtime_error_count": total_errors,
        "unique_types": 1,
        "total_groups": 1,
        "truncated": False,
        "max_groups": 50,
        "first_error_line": 10,
        "groups": [
            {
                "signature": "UVM_ERROR [TOP]",
                "severity": "ERROR",
                "count": total_errors,
                "first_line": 10,
                "first_time_ps": 1000,
                "last_time_ps": 3000,
                "sample_event_id": "failure-1",
                "sample_message": "compare failed",
                "source_file": "/tmp/src/top_tb.sv",
                "source_line": 10,
                "instance_path": "uvm_test_top.env.scb",
                "group_index": 0,
            }
        ],
        "detail_level": "compact",
        "detail_hint": None,
        "auto_downgraded": False,
        "failure_events": [],
        "failure_events_total": total_errors,
        "failure_events_returned": 0,
        "failure_events_truncated": bool(total_errors),
        "previous_log_detected": False,
        "candidate_previous_logs": [],
        "suggested_followup_tool": None,
        "first_group_context": None,
        "problem_hints": {
            "has_x": False,
            "has_z": False,
            "first_error_time_ps": 1000 if total_errors else None,
            "error_pattern": "compare failed" if total_errors else None,
        },
        "auto_diff": None,
    })


def _make_recommend_result() -> schemas.RecommendNextStepsResult:
    return schemas.RecommendNextStepsResult.model_validate({
        "primary_failure_target": {"event_id": "failure-1", "time_ps": 1000},
        "recommended_signals": [{"path": "top_tb.dut.state", "role": "state"}],
        "recommended_instances": [{"instance_path": "top_tb.dut", "score": 10}],
        "suspected_failure_class": "data-path corruption",
        "recommendation_strategy": "role_rank_v2_structural",
        "failure_window_center_ps": 1000,
        "why": ["earliest failure"],
    })


def _make_scan_result() -> schemas.ScanStructuralRisksResult:
    return schemas.ScanStructuralRisksResult.model_validate({
        "scan_scope": "scope1",
        "files_scanned": 4,
        "total_risks": 3,
        "risks": [
            {
                "type": "slice_overlap",
                "file": "/tmp/src/dut.sv",
                "line": 12,
                "module": "dut",
                "risk_level": "high",
                "detail": "slice overlap",
                "evidence": [],
            },
            {
                "type": "multi_drive",
                "file": "/tmp/src/dut.sv",
                "line": 18,
                "module": "dut",
                "risk_level": "high",
                "detail": "multiple drivers",
                "evidence": [],
            },
            {
                "type": "narrow_condition_injection",
                "file": "/tmp/src/top_tb.sv",
                "line": 24,
                "module": "top_tb",
                "risk_level": "medium",
                "detail": "narrow condition",
                "evidence": [],
            },
        ],
        "categories_scanned": ["slice_overlap", "multi_drive", "narrow_condition_injection"],
        "skipped_files": [],
    })


def _prefill_all():
    sim = _make_sim_paths_result()
    hier = _make_hierarchy_result()
    log = _make_parse_result()
    scan = _make_scan_result()
    rec = _make_recommend_result()
    server._result_cache["get_sim_paths"] = sim
    server._result_cache["build_tb_hierarchy"] = hier
    server._result_cache["parse_sim_log"] = log
    server._result_cache["scan_structural_risks"] = scan
    server._result_cache["recommend_failure_debug_next_steps"] = rec
    server._result_provenance["get_sim_paths"] = {
        "verif_root": sim.verif_root,
        "case_dir": sim.case_dir,
        "simulator": sim.simulator,
        "compile_log": sim.compile_logs[0].path,
    }
    server._result_provenance["build_tb_hierarchy"] = {
        "compile_log": sim.compile_logs[0].path,
        "simulator": sim.simulator,
    }
    server._result_provenance["parse_sim_log"] = {
        "log_path": sim.sim_logs[0].path,
        "simulator": sim.simulator,
    }
    server._result_provenance["scan_structural_risks"] = {
        "compile_log": sim.compile_logs[0].path,
        "simulator": sim.simulator,
    }
    server._result_provenance["recommend_failure_debug_next_steps"] = {
        "log_path": sim.sim_logs[0].path,
        "wave_path": sim.wave_files[0].path,
        "simulator": sim.simulator,
        "compile_log": sim.compile_logs[0].path,
    }
    server._session_state["get_sim_paths"] = {
        "verif_root": sim.verif_root,
        "case_dir": sim.case_dir,
        "simulator": sim.simulator,
        "compile_log": sim.compile_logs[0].path,
    }
    server._session_state["build_tb_hierarchy"] = {
        "compile_log": sim.compile_logs[0].path,
        "simulator": sim.simulator,
    }


@pytest.fixture(autouse=True)
def _reset_session_state():
    server.reset_session_state()
    yield
    server.reset_session_state()


class TestDiagnosticSnapshot:
    def test_empty_session_returns_all_unavailable(self):
        result = server._handle_diagnostic_snapshot({})

        assert result.sim_paths.available is False
        assert result.hierarchy.available is False
        assert result.log_analysis.available is False
        assert result.structural_scan is None
        assert result.recommended_next.available is False
        assert len(result.missing_steps) == 1
        assert result.missing_steps[0]["tool"] == "get_sim_paths"

    def test_after_sim_paths_only(self):
        sim = _make_sim_paths_result()
        server._result_cache["get_sim_paths"] = sim
        server._result_provenance["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._session_state["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.sim_paths.available is True
        assert result.hierarchy.available is False
        assert result.log_analysis.available is False
        assert result.structural_scan is None
        assert result.recommended_next.available is False
        assert result.simulator == "xcelium"
        assert len(result.missing_steps) == 3
        assert {step["tool"] for step in result.missing_steps} == {
            "build_tb_hierarchy",
            "parse_sim_log",
            "recommend_failure_debug_next_steps",
        }
        assert result.log_analysis.suggested_call["arguments"]["log_path"] == sim.sim_logs[0].path
        assert result.recommended_next.suggested_call is None

    def test_all_available(self):
        _prefill_all()

        result = server._handle_diagnostic_snapshot({})

        assert result.sim_paths.available is True
        assert result.hierarchy.available is True
        assert result.log_analysis.available is True
        assert result.structural_scan is not None
        assert result.structural_scan.available is True
        assert result.structural_scan.summary == {
            "files_scanned": 4,
            "total_risks": 3,
            "high_risk_count": 2,
        }
        assert result.recommended_next.available is True
        assert result.total_errors == 3
        assert result.top_module == "top_tb"
        assert result.missing_steps is None

    def test_log_summary_reports_auto_diff_when_present(self):
        _prefill_all()
        server._result_cache["parse_sim_log"] = schemas.ParseSimLogResult.model_validate(
            {
                **_make_parse_result().model_dump(),
                "auto_diff": {
                    "base_summary": {"total_events": 3, "unique_groups": 1, "groups": {"UVM_ERROR [TOP]": 3}},
                    "new_summary": {"total_events": 2, "unique_groups": 1, "groups": {"UVM_ERROR [TOP]": 2}},
                    "problem_hints_comparison": {
                        "base": {"has_x": False, "has_z": False, "first_error_time_ps": 1000, "error_pattern": None},
                        "new": {"has_x": False, "has_z": False, "first_error_time_ps": 1000, "error_pattern": None},
                        "x_resolved": False,
                        "z_resolved": False,
                        "x_introduced": False,
                        "z_introduced": False,
                        "error_pattern_changed": False,
                        "error_pattern_transition": None,
                        "first_error_time_shift_ps": 0,
                        "first_error_time_direction": "unchanged",
                    },
                    "resolved_events": [{"event_id": "resolved-1"}],
                    "persistent_events": [],
                    "new_events": [{"event_id": "introduced-1"}, {"event_id": "introduced-2"}],
                    "comparison_notes": [],
                    "convergence_summary": "1 resolved, 2 new",
                },
            }
        )

        result = server._handle_diagnostic_snapshot({})

        assert result.log_analysis.summary["auto_diff_available"] is True
        assert result.log_analysis.summary["auto_diff_resolved_count"] == 1
        assert result.log_analysis.summary["auto_diff_introduced_count"] == 2

    def test_log_summary_reports_auto_diff_false_when_absent(self):
        _prefill_all()

        result = server._handle_diagnostic_snapshot({})

        assert result.log_analysis.summary["auto_diff_available"] is False

    def test_cascade_invalidation(self):
        _prefill_all()

        server._invalidate_downstream("get_sim_paths")

        assert server._result_cache["get_sim_paths"] is not None
        assert server._result_cache["build_tb_hierarchy"] is None
        assert server._result_cache["parse_sim_log"] is None
        assert server._result_cache["recommend_failure_debug_next_steps"] is None
        assert server._session_state["build_tb_hierarchy"] is None

    def test_cascade_from_parse_sim_log(self):
        _prefill_all()

        server._invalidate_downstream("parse_sim_log")

        assert server._result_cache["build_tb_hierarchy"] is not None
        assert server._result_cache["recommend_failure_debug_next_steps"] is None

    def test_clean_run_skips_recommend(self):
        server._result_cache["parse_sim_log"] = _make_parse_result(total_errors=0)
        server._result_provenance["parse_sim_log"] = {
            "log_path": "/tmp/verif/work/work_case0/irun.log",
            "simulator": "xcelium",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.log_analysis.available is True
        assert result.recommended_next.available is False
        assert result.recommended_next.suggested_call is None
        assert result.total_errors is None
        assert all(step["tool"] != "recommend_failure_debug_next_steps" for step in (result.missing_steps or []))

    def test_suggested_call_has_correct_arguments(self):
        sim = _make_sim_paths_result()
        server._result_cache["get_sim_paths"] = sim
        server._result_provenance["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._session_state["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        result = server._handle_diagnostic_snapshot({})

        assert result.hierarchy.suggested_call["arguments"]["compile_log"] == sim.compile_logs[0].path
        assert result.log_analysis.suggested_call["arguments"]["log_path"] == sim.sim_logs[0].path
        assert result.log_analysis.suggested_call["arguments"]["simulator"] == "xcelium"
        assert result.recommended_next.suggested_call is None

    def test_fresh_sections_have_stale_false(self):
        _prefill_all()

        result = server._handle_diagnostic_snapshot({})

        assert result.sim_paths.stale is False
        assert result.hierarchy.stale is False
        assert result.log_analysis.stale is False
        assert result.structural_scan is not None
        assert result.structural_scan.stale is False
        assert result.recommended_next.stale is False

    def test_schema_roundtrip(self):
        _prefill_all()
        result = server._handle_diagnostic_snapshot({})

        json_str = result.model_dump_json(exclude_none=True)
        reparsed = schemas.DiagnosticSnapshot.model_validate_json(json_str)

        assert reparsed == result

    def test_stale_sections_do_not_promote_quick_ref_or_block_missing_steps(self):
        _prefill_all()
        server._result_provenance["parse_sim_log"] = {
            "log_path": "/tmp/verif/work/work_other/irun.log",
            "simulator": "xcelium",
        }
        server._result_provenance["recommend_failure_debug_next_steps"] = {
            "log_path": "/tmp/verif/work/work_other/irun.log",
            "wave_path": "/tmp/verif/work/work_other/top_tb.vcd",
            "simulator": "xcelium",
            "compile_log": "/tmp/verif/work/other_elab.log",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.log_analysis.available is True
        assert result.log_analysis.stale is True
        assert result.recommended_next.available is True
        assert result.recommended_next.stale is True
        assert result.total_errors is None
        assert result.primary_failure_target is None
        assert {step["tool"] for step in result.missing_steps} >= {
            "parse_sim_log",
            "recommend_failure_debug_next_steps",
        }

    def test_no_anchor_does_not_promote_top_level_fields_from_old_cache(self):
        server._result_cache["parse_sim_log"] = _make_parse_result(total_errors=4)
        server._result_provenance["parse_sim_log"] = {
            "log_path": "/tmp/verif/work/work_case0/irun.log",
            "simulator": "xcelium",
        }
        server._result_cache["recommend_failure_debug_next_steps"] = _make_recommend_result()
        server._result_provenance["recommend_failure_debug_next_steps"] = {
            "log_path": "/tmp/verif/work/work_case0/irun.log",
            "wave_path": "/tmp/verif/work/work_case0/top_tb.vcd",
            "simulator": "xcelium",
            "compile_log": "/tmp/verif/work/elab.log",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.sim_paths.available is False
        assert result.log_analysis.available is True
        assert result.recommended_next.available is True
        assert result.total_errors is None
        assert result.primary_failure_target is None
        assert result.problem_hints is None

    def test_build_hierarchy_with_auto_input_is_not_stale_when_result_has_actual_simulator(self):
        sim = _make_sim_paths_result()
        hier = _make_hierarchy_result()
        server._result_cache["get_sim_paths"] = sim
        server._result_cache["build_tb_hierarchy"] = hier
        server._result_provenance["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._result_provenance["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": "xcelium",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.hierarchy.available is True
        assert result.hierarchy.stale is False

    def test_second_log_in_same_case_is_fresh(self):
        _prefill_all()
        server._result_cache["parse_sim_log"] = schemas.ParseSimLogResult.model_validate({
            **_make_parse_result().model_dump(),
            "log_file": "/tmp/verif/work/work_case0/irun_rerun.log",
        })
        server._result_provenance["parse_sim_log"] = {
            "log_path": "/tmp/verif/work/work_case0/irun_rerun.log",
            "simulator": "xcelium",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.log_analysis.available is True
        assert result.log_analysis.stale is False
        assert result.total_errors == 3

    def test_second_wave_in_same_case_is_fresh(self):
        _prefill_all()
        server._result_provenance["recommend_failure_debug_next_steps"] = {
            "log_path": "/tmp/verif/work/work_case0/irun_rerun.log",
            "wave_path": "/tmp/verif/work/work_case0/top_tb_alt.vcd",
            "simulator": "xcelium",
            "compile_log": "/tmp/verif/work/elab.log",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.recommended_next.available is True
        assert result.recommended_next.stale is False
        assert result.primary_failure_target is not None

    def test_log_under_case_dir_but_not_in_sim_log_set_is_stale_when_set_is_non_empty(self):
        _prefill_all()
        server._result_provenance["parse_sim_log"] = {
            "log_path": "/tmp/verif/work/work_case0/manual_debug.log",
            "simulator": "xcelium",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.log_analysis.available is True
        assert result.log_analysis.stale is True
        assert result.total_errors is None
        assert {step["tool"] for step in result.missing_steps} >= {"parse_sim_log"}

    def test_recommend_paths_under_case_dir_but_not_in_sets_are_stale_when_sets_are_non_empty(self):
        _prefill_all()
        server._result_provenance["recommend_failure_debug_next_steps"] = {
            "log_path": "/tmp/verif/work/work_case0/manual_debug.log",
            "wave_path": "/tmp/verif/work/work_case0/manual_debug.vcd",
            "simulator": "xcelium",
            "compile_log": "/tmp/verif/work/elab.log",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.recommended_next.available is True
        assert result.recommended_next.stale is True
        assert result.primary_failure_target is None
        assert {step["tool"] for step in result.missing_steps} >= {"recommend_failure_debug_next_steps"}

    def test_structural_scan_stale_when_compile_log_mismatches(self):
        _prefill_all()
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/other_elab.log",
            "simulator": "xcelium",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.structural_scan is not None
        assert result.structural_scan.available is True
        assert result.structural_scan.stale is True

    def test_snapshot_adds_missing_scan_with_failure_context(self):
        sim = _make_sim_paths_result()
        hier = _make_hierarchy_result()
        log = _make_parse_result(total_errors=2)
        server._result_cache["get_sim_paths"] = sim
        server._result_cache["build_tb_hierarchy"] = hier
        server._result_cache["parse_sim_log"] = log
        server._result_provenance["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._result_provenance["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }
        server._result_provenance["parse_sim_log"] = {
            "log_path": sim.sim_logs[0].path,
            "simulator": sim.simulator,
        }
        server._session_state["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._session_state["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.structural_scan is None
        assert result.missing_steps[0]["tool"] == "scan_structural_risks"
        assert result.missing_steps[0]["arguments"] == {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }
        assert result.missing_steps[0]["reason"] == "Structural scan is missing, so recommendation quality will be degraded."

    def test_snapshot_adds_missing_scan_without_failure_context(self):
        sim = _make_sim_paths_result()
        hier = _make_hierarchy_result()
        server._result_cache["get_sim_paths"] = sim
        server._result_cache["build_tb_hierarchy"] = hier
        server._result_provenance["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._result_provenance["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }
        server._session_state["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._session_state["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }

        result = server._handle_diagnostic_snapshot({})

        scan_step = next(step for step in result.missing_steps if step["tool"] == "scan_structural_risks")
        assert scan_step["reason"] == "Structural scan has not been run yet."

    def test_snapshot_keeps_workflow_order_when_problem_hints_do_not_match_priority_rule(self):
        sim = _make_sim_paths_result()
        hier = _make_hierarchy_result()
        log = schemas.ParseSimLogResult.model_validate(
            {
                **_make_parse_result(total_errors=2).model_dump(),
                "problem_hints": {
                    "has_x": False,
                    "has_z": False,
                    "first_error_time_ps": 1000,
                    "error_pattern": "compare failed",
                },
            }
        )
        server._result_cache["get_sim_paths"] = sim
        server._result_cache["build_tb_hierarchy"] = hier
        server._result_cache["parse_sim_log"] = log
        server._result_provenance["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._result_provenance["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }
        server._result_provenance["parse_sim_log"] = {
            "log_path": sim.sim_logs[0].path,
            "simulator": sim.simulator,
        }
        server._session_state["get_sim_paths"] = {
            "verif_root": sim.verif_root,
            "case_dir": sim.case_dir,
            "simulator": sim.simulator,
            "compile_log": sim.compile_logs[0].path,
        }
        server._session_state["build_tb_hierarchy"] = {
            "compile_log": sim.compile_logs[0].path,
            "simulator": sim.simulator,
        }

        result = server._handle_diagnostic_snapshot({})

        assert [step["tool"] for step in result.missing_steps] == [
            "scan_structural_risks",
            "recommend_failure_debug_next_steps",
        ]

    def test_snapshot_does_not_request_scan_when_hierarchy_is_stale(self):
        _prefill_all()
        server._result_cache["scan_structural_risks"] = None
        server._result_provenance["scan_structural_risks"] = None
        server._result_provenance["build_tb_hierarchy"] = {
            "compile_log": "/tmp/verif/work/other_elab.log",
            "simulator": "xcelium",
        }

        result = server._handle_diagnostic_snapshot({})

        assert result.hierarchy.stale is True
        assert all(step["tool"] != "scan_structural_risks" for step in result.missing_steps)
