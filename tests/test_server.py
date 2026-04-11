"""
test_server.py
覆盖：MCP dispatch 层的关键参数透传和 sim path 发现结果
"""

import json
import os
import tempfile
from pathlib import Path

import pytest
from unittest.mock import patch

import server
from config import DEFAULT_EXTRA_TRANSITIONS
from src.schemas import ToolErrorResult


@pytest.fixture(autouse=True)
def _reset_session_state():
    """每个测试前后重置 session state。"""
    server.reset_session_state()
    yield
    server.reset_session_state()


def _prefill_get_sim_paths_state(**overrides):
    """预填 get_sim_paths state 以绕过门禁。"""
    state = {
        "verif_root": "/tmp/verif",
        "case_dir": "/tmp/verif/work/work_case0",
        "simulator": "vcs",
        "compile_log": "/tmp/verif/work/elab.log",
    }
    state.update(overrides)
    server._session_state["get_sim_paths"] = state


def _prefill_build_tb_hierarchy_state(**overrides):
    """预填 build_tb_hierarchy state 以绕过门禁。"""
    state = {
        "compile_log": "/tmp/verif/work/elab.log",
        "simulator": "vcs",
    }
    state.update(overrides)
    server._session_state["build_tb_hierarchy"] = state


LOG_SAMPLE = """\
Booting simulation
module_a ERROR unique issue a @ 1 ns
module_b ERROR unique issue b @ 2 ns
module_c ERROR unique issue c @ 3 ns
"""


class TestScanRequiredNextCallHelpers:
    def test_build_scan_required_next_call_returns_none_when_compile_log_missing(self):
        assert server._build_scan_required_next_call(None, "vcs") is None

    def test_build_scan_required_next_call_returns_none_when_simulator_missing(self):
        assert server._build_scan_required_next_call("/tmp/elab.log", None) is None


@pytest.mark.anyio
class TestDispatchGetSimPaths:
    async def test_returns_discovery_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            verif_root = Path(tmpdir)
            work_dir = verif_root / "work"
            case_dir = work_dir / "work_case0"

            case_dir.mkdir(parents=True)

            elab_log = work_dir / "elab.log"
            sim_log = case_dir / "irun.log"
            wave_file = case_dir / "top_tb.fsdb"

            elab_log.write_text("xrun\nxmelab\n")
            sim_log.write_text("sim ok\n")
            wave_file.write_text("0" * 2048)

            result = await server._dispatch(
                "get_sim_paths",
                {"verif_root": str(work_dir), "case_name": "case0"},
            )

            assert result["verif_root"] == str(work_dir.resolve())
            assert result["case_name"] == "case0"
            assert result["config_source"] == "auto"
            assert "config_root" in result
            assert result["discovery_mode"] == "root_dir"
            assert result["case_dir"] == str(case_dir.resolve())
            assert result["simulator"] == "xcelium"
            assert result["compile_logs"][0]["path"] == str(elab_log.resolve())
            assert result["compile_logs"][0]["phase"] == "elaborate"
            assert result["sim_logs"][0]["path"] == str(sim_log.resolve())
            assert result["wave_files"][0]["path"] == str(wave_file.resolve())
            assert result["wave_files"][0]["format"] == "fsdb"
            assert result["available_cases"] == []
            assert "fsdb_runtime" in result


@pytest.mark.anyio
class TestStructuralScannerToolContract:
    async def test_tool_schema_allows_default_simulator(self):
        tools = await server.list_tools()
        scan_tool = next(tool for tool in tools if tool.name == "scan_structural_risks")

        assert scan_tool.inputSchema["required"] == ["compile_log"]
        assert scan_tool.inputSchema["properties"]["simulator"]["default"] == "auto"

    async def test_dispatch_uses_auto_simulator_default(self):
        with patch.object(server, "scan_structural_risks", return_value={
            "scan_scope": "scope1",
            "files_scanned": 1,
            "total_risks": 0,
            "risks": [],
            "categories_scanned": ["slice_overlap"],
            "skipped_files": [],
        }) as scan_mock:
            result = await server._dispatch(
                "scan_structural_risks",
                {
                    "compile_log": "/tmp/elab.log",
                    "categories": ["slice_overlap"],
                },
            )

        scan_mock.assert_called_once_with(
            compile_log="/tmp/elab.log",
            simulator="auto",
            scan_scope="scope1",
            categories=["slice_overlap"],
        )
        assert result["scan_scope"] == "scope1"
        assert result["files_scanned"] == 1

    async def test_build_tb_hierarchy_returns_required_next_call_when_scan_missing(self):
        with patch.object(server, "parse_compile_log", return_value={"files": []}), patch.object(
            server,
            "build_hierarchy",
            return_value={
                "project": {"top_module": "top_tb", "simulator": "vcs"},
                "files": {},
                "component_tree": {},
                "class_hierarchy": [],
                "interfaces": [],
                "compile_result": {},
            },
        ):
            result = await server._dispatch(
                "build_tb_hierarchy",
                {"compile_log": "/tmp/elab.log", "simulator": "vcs"},
            )

        assert result["required_next_call"] == {
            "tool": "scan_structural_risks",
            "arguments": {"compile_log": "/tmp/elab.log", "simulator": "vcs"},
        }
        assert result["suggested_next"]["tool"] == "scan_structural_risks"
        assert result["suggested_next"]["arguments"] == result["required_next_call"]["arguments"]

    async def test_build_tb_hierarchy_clears_required_next_call_when_scan_already_cached(self):
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {
                "scan_scope": "scope1",
                "files_scanned": 1,
                "total_risks": 0,
                "risks": [],
                "categories_scanned": ["slice_overlap"],
                "skipped_files": [],
            }
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/elab.log",
            "simulator": "vcs",
        }
        with patch.object(server, "parse_compile_log", return_value={"files": []}), patch.object(
            server,
            "build_hierarchy",
            return_value={
                "project": {"top_module": "top_tb", "simulator": "vcs"},
                "files": {},
                "component_tree": {},
                "class_hierarchy": [],
                "interfaces": [],
                "compile_result": {},
            },
        ):
            result = await server._dispatch(
                "build_tb_hierarchy",
                {"compile_log": "/tmp/elab.log", "simulator": "vcs"},
            )

        assert result["required_next_call"] is None
        assert result["suggested_next"] is None

    async def test_cycle_query_tool_schema_and_dispatch(self):
        tools = await server.list_tools()
        cycle_tool = next(tool for tool in tools if tool.name == "get_signals_by_cycle")

        assert cycle_tool.inputSchema["required"] == ["wave_path", "clock_path", "signal_paths"]
        assert cycle_tool.inputSchema["properties"]["edge"]["default"] == "posedge"
        assert cycle_tool.inputSchema["properties"]["sample_offset_ps"]["minimum"] == 0
        assert cycle_tool.inputSchema["properties"]["num_cycles"]["minimum"] == 0

        fixture = Path(__file__).parent / "fixtures" / "cycle_test.vcd"
        result = await server._dispatch(
            "get_signals_by_cycle",
            {
                "wave_path": str(fixture),
                "clock_path": "top_tb.clk",
                "signal_paths": ["top_tb.data"],
                "num_cycles": 2,
            },
        )

        assert result["num_cycles_requested"] == 2
        assert result["effective_num_cycles"] == 2
        assert result["capped"] is False
        assert result["num_cycles_returned"] == 2
        assert [cycle["signals"]["top_tb.data"]["dec"] for cycle in result["cycles"]] == [1, 2]

    async def test_cycle_query_dispatch_reports_capped_request(self):
        fixture = Path(__file__).parent / "fixtures" / "cycle_test.vcd"
        result = await server._dispatch(
            "get_signals_by_cycle",
            {
                "wave_path": str(fixture),
                "clock_path": "top_tb.clk",
                "signal_paths": ["top_tb.data"],
                "num_cycles": 999,
            },
        )

        assert result["num_cycles_requested"] == 999
        assert result["effective_num_cycles"] == server.MAX_CYCLES_PER_QUERY
        assert result["capped"] is True
        assert result["num_cycles_returned"] == 3


@pytest.mark.anyio
class TestDispatchParseSimLog:
    async def test_first_parse_omits_auto_diff(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("module_a ERROR unique issue a @ 1 ns\n")
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            assert result.auto_diff is None
        finally:
            Path(log_path).unlink()

    async def test_second_parse_same_log_returns_auto_diff_when_file_changed(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("module_a ERROR unique issue a @ 1 ns\n")
            log_path = Path(handle.name)

        try:
            first = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": str(log_path),
                    "simulator": "vcs",
                },
            )
            assert first.auto_diff is None

            stat = log_path.stat()
            log_path.write_text("module_b ERROR unique issue b @ 2 ns\n")
            os.utime(log_path, (stat.st_atime, stat.st_mtime + 1))

            second = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": str(log_path),
                    "simulator": "vcs",
                },
            )

            assert second["auto_diff"]["base_summary"]["total_events"] == 1
            assert second["auto_diff"]["new_summary"]["total_events"] == 1
            assert len(second["auto_diff"]["resolved_events"]) == 1
            assert len(second["auto_diff"]["new_events"]) == 1
            assert second["auto_diff"]["persistent_events"] == []
        finally:
            log_path.unlink()

    async def test_second_parse_same_log_without_change_omits_auto_diff(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("module_a ERROR unique issue a @ 1 ns\n")
            log_path = handle.name

        try:
            await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            second = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            assert second.auto_diff is None
        finally:
            Path(log_path).unlink()

    async def test_second_parse_different_log_omits_auto_diff(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as first_handle:
            first_handle.write("module_a ERROR unique issue a @ 1 ns\n")
            first_log = first_handle.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as second_handle:
            second_handle.write("module_b ERROR unique issue b @ 2 ns\n")
            second_log = second_handle.name

        try:
            await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": first_log,
                    "simulator": "vcs",
                },
            )

            second = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": second_log,
                    "simulator": "vcs",
                },
            )

            assert second.auto_diff is None
        finally:
            Path(first_log).unlink()
            Path(second_log).unlink()

    async def test_second_parse_same_log_with_different_simulator_omits_auto_diff(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("module_a ERROR unique issue a @ 1 ns\n")
            log_path = handle.name

        try:
            await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            stat = os.stat(log_path)
            Path(log_path).write_text("module_b ERROR unique issue b @ 2 ns\n")
            os.utime(log_path, (stat.st_atime, stat.st_mtime + 1))

            second = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "xcelium",
                },
            )

            assert second.auto_diff is None
        finally:
            Path(log_path).unlink()

    async def test_auto_diff_uses_untruncated_failure_events(self):
        _prefill_get_sim_paths_state()
        repeated_before = "\n".join(
            [
                "UVM_ERROR /tmp/top_tb.sv(10) @ 1 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 2 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 3 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 4 ns: reporter [TOP] repeated issue",
            ]
        ) + "\n"
        repeated_after = "\n".join(
            [
                "UVM_ERROR /tmp/top_tb.sv(10) @ 1 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 2 ns: reporter [TOP] repeated issue",
            ]
        ) + "\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(repeated_before)
            log_path = Path(handle.name)

        try:
            first = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": str(log_path),
                    "simulator": "vcs",
                    "detail_level": "compact",
                    "max_events_per_group": 1,
                },
            )
            assert first["failure_events_returned"] == 1

            stat = log_path.stat()
            log_path.write_text(repeated_after)
            os.utime(log_path, (stat.st_atime, stat.st_mtime + 1))

            second = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": str(log_path),
                    "simulator": "vcs",
                    "detail_level": "compact",
                    "max_events_per_group": 1,
                },
            )

            assert second["failure_events_returned"] == 1
            assert second["auto_diff"]["base_summary"]["total_events"] == 4
            assert second["auto_diff"]["new_summary"]["total_events"] == 2
            assert len(second["auto_diff"]["resolved_events"]) == 2
            assert len(second["auto_diff"]["persistent_events"]) == 2
        finally:
            log_path.unlink()

    async def test_forwards_max_groups(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(LOG_SAMPLE)
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                    "max_groups": 2,
                    "detail_level": "full",
                },
            )

            assert result["schema_version"] == "2.0"
            assert result["runtime_total_errors"] == 3
            assert result["total_groups"] == 3
            assert result["truncated"] is True
            assert result["max_groups"] == 2
            assert len(result["groups"]) == 2
            assert result["groups"][0]["group_index"] == 0
            assert len(result["failure_events"]) == 2
            assert result["detail_level"] == "full"
            assert result["failure_events_total"] == 2
            assert result["failure_events_returned"] == 2
            assert result["failure_events_truncated"] is False
            assert result["failure_events"][0]["time_parse_status"] == "exact"
            assert result["failure_events"][0]["log_phase"] == "runtime"
            assert result["problem_hints"]["has_x"] is False
            assert result["problem_hints"]["has_z"] is False
            assert result["problem_hints"]["first_error_time_ps"] == 1000
        finally:
            Path(log_path).unlink()

    async def test_max_groups_limits_failure_events_to_summary_groups(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(LOG_SAMPLE)
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                    "max_groups": 2,
                    "detail_level": "compact",
                    "max_events_per_group": 3,
                },
            )

            allowed = {group["signature"] for group in result["groups"]}
            assert len(allowed) == 2
            assert all(event["group_signature"] in allowed for event in result["failure_events"])
            assert result["failure_events_total"] == 2
        finally:
            Path(log_path).unlink()

    async def test_summary_detail_level_skips_failure_events(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(LOG_SAMPLE)
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                    "detail_level": "summary",
                },
            )

            assert result["failure_events"] == []
            assert result["failure_events_total"] == 3
            assert result["failure_events_returned"] == 0
            assert result["failure_events_truncated"] is True
            assert "detail_hint" in result
        finally:
            Path(log_path).unlink()

    async def test_compact_detail_level_limits_events_per_group(self):
        _prefill_get_sim_paths_state()
        repeated_log = "\n".join(
            [
                "UVM_ERROR /tmp/top_tb.sv(10) @ 1 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 2 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 3 ns: reporter [TOP] repeated issue",
                "UVM_ERROR /tmp/top_tb.sv(10) @ 4 ns: reporter [TOP] repeated issue",
            ]
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(repeated_log)
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                    "detail_level": "compact",
                    "max_events_per_group": 2,
                },
            )

            assert result["failure_events_total"] == 4
            assert result["failure_events_returned"] == 2
            assert result["failure_events_truncated"] is True
        finally:
            Path(log_path).unlink()

    async def test_problem_hints_detects_z_heuristically(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("module_z ERROR output is z @ 4 ns\n")
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            assert result["problem_hints"]["has_z"] is True
            assert result["problem_hints"]["error_pattern"] == "zprop"
        finally:
            Path(log_path).unlink()

    async def test_groups_include_xprop_priority_when_x_present(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(
                "\n".join(
                    [
                        "UVM_ERROR /path/top_tb.sv(10) @ 10 ns: reporter [SCB] expected=0x12 actual=0xXX",
                        "UVM_ERROR /path/top_tb.sv(20) @ 20 ns: reporter [CHK] expected=0x12 actual=0x34",
                    ]
                )
                + "\n"
            )
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            priorities = {group["signature"]: group["xprop_priority"] for group in result["groups"]}
            assert priorities["UVM_ERROR [SCB]"] == "high"
            assert priorities["UVM_ERROR [CHK]"] == "normal"
        finally:
            Path(log_path).unlink()

    async def test_summary_detail_level_still_returns_first_group_context(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(
                "".join(
                    ["info line\n"] * 3
                    + ["module_a ERROR summary path issue @ 100 ns\n"]
                    + ["info after\n"] * 3
                )
            )
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                    "detail_level": "summary",
                },
            )

            assert result["failure_events"] == []
            assert result["first_group_context"] is not None
            assert result["first_group_context"]["center_line"] == 4
            assert "ERROR" in result["first_group_context"]["context"]
        finally:
            Path(log_path).unlink()

    async def test_returns_first_group_context(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(
                "".join(
                    ["info line\n"] * 5
                    + ["module_a ERROR some issue @ 100 ns\n"]
                    + ["info after\n"] * 5
                )
            )
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            assert "first_group_context" in result
            ctx = result["first_group_context"]
            assert ctx is not None
            assert ctx["center_line"] == 6
            assert "ERROR" in ctx["context"]
        finally:
            Path(log_path).unlink()

    async def test_no_errors_returns_no_first_group_context(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("info: all good\ninfo: simulation passed\n")
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            assert result.get("first_group_context") is None
        finally:
            Path(log_path).unlink()

    async def test_problem_hints_detects_x_annotation(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("module_x ERROR xprop detected on bus @ 4 ns\n")
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            assert result["problem_hints"]["has_x"] is True
            assert result["problem_hints"]["has_z"] is False
            assert result["problem_hints"]["error_pattern"] == "xprop"
        finally:
            Path(log_path).unlink()

    async def test_diff_sim_failure_results(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as base:
            base.write("module_a ERROR unique issue a @ 1 ns\n")
            base_path = base.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as new:
            new.write("module_b ERROR unique issue b @ 2 ns\n")
            new_path = new.name
        try:
            result = await server._dispatch(
                "diff_sim_failure_results",
                {
                    "base_log_path": base_path,
                    "new_log_path": new_path,
                    "simulator": "vcs",
                },
            )
            assert len(result["resolved_events"]) == 1
            assert len(result["new_events"]) == 1
            assert "problem_hints_comparison" in result
            assert "convergence_summary" in result
        finally:
            Path(base_path).unlink()
            Path(new_path).unlink()


@pytest.mark.anyio
class TestNewAnalyzerTools:
    async def test_recommend_failure_debug_next_steps(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sva_top.sv", 66: top_tb.sva_top_inst.apREQ: started at 10ps failed at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module dut $end
$var wire 1 ! req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )
        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
                "top_hint": "top_tb",
            },
        )
        assert result["primary_failure_target"]["group_signature"] == "ASSERTION_FAIL: apREQ"
        assert result["recommended_signals"][0]["path"] == "top_tb.dut.req"
        assert result["workflow_incomplete"] is True
        assert result["degraded_reason"] == "missing_structural_scan"
        assert result["next_iteration_hint"]["tool"] == "diff_sim_failure_results"
        assert result["required_next_call"] == {
            "tool": "scan_structural_risks",
            "arguments": {
                "compile_log": "/tmp/verif/work/elab.log",
                "simulator": "vcs",
            },
        }
        assert result["missing_inputs"] == []

    async def test_recommend_failure_debug_next_steps_consumes_scan_cache(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sva_top.sv", 66: top_tb.sva_top_inst.apREQ: started at 10ps failed at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module dut $end
$var wire 1 ! req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )
        server._result_cache["parse_sim_log"] = server.schemas.ParseSimLogResult.model_validate(
            {
                "log_file": str(log_path),
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
                "first_error_line": 1,
                "problem_hints": {"has_x": True, "has_z": False, "error_pattern": "xprop"},
            }
        )
        server._result_provenance["parse_sim_log"] = {
            "log_path": str(log_path),
            "simulator": "vcs",
        }
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {
                "scan_scope": "scope1",
                "files_scanned": 1,
                "total_risks": 1,
                "risks": [
                    {
                        "type": "slice_overlap",
                        "file": "/tmp/dut.sv",
                        "line": 8,
                        "module": "sva_top_inst",
                        "risk_level": "high",
                        "detail": "slice overlap",
                        "evidence": [],
                    }
                ],
                "categories_scanned": ["slice_overlap"],
                "skipped_files": [],
            }
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/elab.log",
            "simulator": "vcs",
        }

        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
                "top_hint": "top_tb",
            },
        )

        assert result["correlated_structural_risks"][0]["risk_type"] == "slice_overlap"
        assert result["correlated_structural_risks"][0]["relevance_score"] == 17
        assert result["workflow_incomplete"] is False
        assert result["degraded_reason"] is None
        assert result["required_next_call"] is None

    async def test_recommend_failure_debug_next_steps_accepts_scan_cache_with_auto_simulator(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sva_top.sv", 66: top_tb.sva_top_inst.apREQ: started at 10ps failed at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module dut $end
$var wire 1 ! req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )
        server._result_cache["parse_sim_log"] = server.schemas.ParseSimLogResult.model_validate(
            {
                "log_file": str(log_path),
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
                "first_error_line": 1,
                "problem_hints": {"has_x": True, "has_z": False, "error_pattern": "xprop"},
            }
        )
        server._result_provenance["parse_sim_log"] = {
            "log_path": str(log_path),
            "simulator": "vcs",
        }
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {
                "scan_scope": "scope1",
                "files_scanned": 1,
                "total_risks": 1,
                "risks": [
                    {
                        "type": "slice_overlap",
                        "file": "/tmp/dut.sv",
                        "line": 8,
                        "module": "sva_top_inst",
                        "risk_level": "high",
                        "detail": "slice overlap",
                        "evidence": [],
                    }
                ],
                "categories_scanned": ["slice_overlap"],
                "skipped_files": [],
            }
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/elab.log",
            "simulator": "auto",
        }

        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
                "top_hint": "top_tb",
            },
        )

        assert result["correlated_structural_risks"][0]["risk_type"] == "slice_overlap"
        assert result["correlated_structural_risks"][0]["relevance_score"] == 17
        assert result["workflow_incomplete"] is False
        assert result["required_next_call"] is None

    async def test_recommend_failure_debug_next_steps_ignores_incompatible_cached_inputs(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        stale_log_path = tmp_path / "stale.log"
        log_path.write_text(
            '"/path/sva_top.sv", 66: top_tb.sva_top_inst.apREQ: started at 10ps failed at 20ps\n'
        )
        stale_log_path.write_text("module_a ERROR stale issue @ 1 ns\n")
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module dut $end
$var wire 1 ! req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )
        server._result_cache["parse_sim_log"] = server.schemas.ParseSimLogResult.model_validate(
            {
                "log_file": str(stale_log_path),
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
                "first_error_line": 1,
                "problem_hints": {"has_x": True, "has_z": False, "error_pattern": "xprop"},
            }
        )
        server._result_provenance["parse_sim_log"] = {
            "log_path": str(stale_log_path),
            "simulator": "vcs",
        }
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {
                "scan_scope": "scope1",
                "files_scanned": 1,
                "total_risks": 1,
                "risks": [
                    {
                        "type": "slice_overlap",
                        "file": "/tmp/dut.sv",
                        "line": 8,
                        "module": "sva_top_inst",
                        "risk_level": "high",
                        "detail": "slice overlap",
                        "evidence": [],
                    }
                ],
                "categories_scanned": ["slice_overlap"],
                "skipped_files": [],
            }
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": str(tmp_path / "stale_elab.log"),
            "simulator": "vcs",
        }

        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
                "top_hint": "top_tb",
            },
        )

        assert result["correlated_structural_risks"] == []
        assert result["workflow_incomplete"] is True
        assert result["degraded_reason"] == "missing_structural_scan"
        assert result["required_next_call"] == {
            "tool": "scan_structural_risks",
            "arguments": {
                "compile_log": "/tmp/verif/work/elab.log",
                "simulator": "vcs",
            },
        }
        assert result["missing_inputs"] == []

    async def test_recommend_failure_debug_next_steps_without_top_hint(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sva_top.sv", 66: top_tb.sva_top_inst.apREQ: started at 10ps failed at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module dut $end
$var wire 1 ! req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )
        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
            },
        )
        assert result["primary_failure_target"]["group_signature"] == "ASSERTION_FAIL: apREQ"
        assert result["recommended_signals"][0]["path"] == "top_tb.dut.req"

    async def test_recommend_failure_debug_next_steps_clean_run_is_not_degraded(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "clean.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text("simulation completed cleanly\n")
        wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n#0\n")
        server._result_cache["parse_sim_log"] = server.schemas.ParseSimLogResult.model_validate(
            {
                "log_file": str(log_path),
                "simulator": "vcs",
                "schema_version": "2.0",
                "contract_version": "1.3",
                "failure_events_schema_version": "1.0",
                "parser_capabilities": [],
                "runtime_total_errors": 0,
                "runtime_fatal_count": 0,
                "runtime_error_count": 0,
                "unique_types": 0,
                "total_groups": 0,
                "truncated": False,
                "max_groups": 50,
                "first_error_line": 0,
                "problem_hints": {"has_x": False, "has_z": False, "error_pattern": None},
            }
        )
        server._result_provenance["parse_sim_log"] = {
            "log_path": str(log_path),
            "simulator": "vcs",
        }

        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
            },
        )

        assert result["suspected_failure_class"] == "no_failure_detected"
        assert result["workflow_incomplete"] is False
        assert result["degraded_reason"] is None
        assert result["required_next_call"] is None

    async def test_recommend_failure_debug_next_steps_keeps_required_next_call_null_when_compile_log_unavailable(self, tmp_path):
        _prefill_get_sim_paths_state(compile_log=None)
        _prefill_build_tb_hierarchy_state(compile_log=None)
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sva_top.sv", 66: top_tb.sva_top_inst.apREQ: started at 10ps failed at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module dut $end
$var wire 1 ! req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )

        result = await server._dispatch(
            "recommend_failure_debug_next_steps",
            {
                "log_path": str(log_path),
                "wave_path": str(wave_path),
                "simulator": "vcs",
                "top_hint": "top_tb",
            },
        )

        assert result["workflow_incomplete"] is True
        assert result["degraded_reason"] == "missing_structural_scan"
        assert result["required_next_call"] is None

    async def test_analyze_failures_inserts_step0_when_scan_missing(self):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()

        class _FakeAnalyzer:
            def __init__(self, *args, **kwargs):
                pass

            def analyze(self, **kwargs):
                return {
                    "summary": {},
                    "focused_group": None,
                    "focused_event": None,
                    "log_context": None,
                    "wave_context": None,
                    "remaining_groups": 0,
                    "analysis_guide": {"step1": "one", "step2": "two"},
                    "problem_hints": None,
                }

        with patch.object(server, "WaveformAnalyzer", _FakeAnalyzer), patch.object(
            server, "_get_parser", return_value=object()
        ):
            result = await server._dispatch(
                "analyze_failures",
                {
                    "log_path": "/tmp/run.log",
                    "wave_path": "/tmp/wave.vcd",
                    "simulator": "vcs",
                    "signal_paths": ["top_tb.sig"],
                },
            )

        assert list(result["analysis_guide"].keys())[:2] == ["step0", "step1"]
        assert result["analysis_guide"]["step0"] == (
            "scan_structural_risks has not been run, so this analysis does not include structural risk correlation."
        )

    async def test_analyze_failures_skips_step0_when_scan_is_compatible(self):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {
                "scan_scope": "scope1",
                "files_scanned": 1,
                "total_risks": 0,
                "risks": [],
                "categories_scanned": ["slice_overlap"],
                "skipped_files": [],
            }
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/elab.log",
            "simulator": "vcs",
        }

        class _FakeAnalyzer:
            def __init__(self, *args, **kwargs):
                pass

            def analyze(self, **kwargs):
                return {
                    "summary": {},
                    "focused_group": None,
                    "focused_event": None,
                    "log_context": None,
                    "wave_context": None,
                    "remaining_groups": 0,
                    "analysis_guide": {"step1": "one"},
                    "problem_hints": None,
                }

        with patch.object(server, "WaveformAnalyzer", _FakeAnalyzer), patch.object(
            server, "_get_parser", return_value=object()
        ):
            result = await server._dispatch(
                "analyze_failures",
                {
                    "log_path": "/tmp/run.log",
                    "wave_path": "/tmp/wave.vcd",
                    "simulator": "vcs",
                    "signal_paths": ["top_tb.sig"],
                },
            )

        assert "step0" not in result["analysis_guide"]

    async def test_analyze_failures_keeps_step0_when_scan_is_incompatible(self):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {
                "scan_scope": "scope1",
                "files_scanned": 1,
                "total_risks": 0,
                "risks": [],
                "categories_scanned": ["slice_overlap"],
                "skipped_files": [],
            }
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/other_elab.log",
            "simulator": "vcs",
        }

        class _FakeAnalyzer:
            def __init__(self, *args, **kwargs):
                pass

            def analyze(self, **kwargs):
                return {
                    "summary": {},
                    "focused_group": None,
                    "focused_event": None,
                    "log_context": None,
                    "wave_context": None,
                    "remaining_groups": 0,
                    "analysis_guide": {"step1": "one"},
                    "problem_hints": None,
                }

        with patch.object(server, "WaveformAnalyzer", _FakeAnalyzer), patch.object(
            server, "_get_parser", return_value=object()
        ):
            result = await server._dispatch(
                "analyze_failures",
                {
                    "log_path": "/tmp/run.log",
                    "wave_path": "/tmp/wave.vcd",
                    "simulator": "vcs",
                    "signal_paths": ["top_tb.sig"],
                },
            )

        assert list(result["analysis_guide"].keys())[0] == "step0"

    async def test_explain_signal_driver(self, tmp_path):
        _prefill_build_tb_hierarchy_state()
        rtl = tmp_path / "dut.sv"
        compile_log = tmp_path / "compile.log"
        wave_path = tmp_path / "wave.vcd"
        rtl.write_text(
            """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign K_sub = a ^ b;
  output logic K_sub;
endmodule
"""
        )
        compile_log.write_text(
            f"""\
Chronologic VCS simulator
Parsing design file '{rtl}'
Top Level Modules:
    top_tb
"""
        )
        wave_path.write_text("$date\n$end\n")

        result = await server._dispatch(
            "explain_signal_driver",
            {
                "signal_path": "top_tb.u0.K_sub",
                "wave_path": str(wave_path),
                "compile_log": str(compile_log),
                "top_hint": "top_tb",
            },
        )

        assert result["driver_status"] == "resolved"
        assert result["driver_kind"] == "assign"
        assert result["resolved_rtl_name"] == "K_sub"
        assert str(rtl) == result["source_file"]

    async def test_explain_signal_driver_instance_ports(self, tmp_path):
        _prefill_build_tb_hierarchy_state()
        rtl = tmp_path / "dut.sv"
        compile_log = tmp_path / "compile.log"
        wave_path = tmp_path / "wave.vcd"
        rtl.write_text(
            """\
module top_tb;
  dut u0();
endmodule

module leaf(output logic [3:0] dout);
endmodule

module dut;
  logic [7:0] S;
  leaf u_a(.dout(S[3:0]));
  leaf u_b(.dout(S[7:4]));
endmodule
"""
        )
        compile_log.write_text(
            f"""\
Chronologic VCS simulator
Parsing design file '{rtl}'
Top Level Modules:
    top_tb
"""
        )
        wave_path.write_text("$date\n$end\n")

        result = await server._dispatch(
            "explain_signal_driver",
            {
                "signal_path": "top_tb.u0.S",
                "wave_path": str(wave_path),
                "compile_log": str(compile_log),
                "top_hint": "top_tb",
            },
        )

        assert result["driver_status"] == "resolved"
        assert result["driver_kind"] == "instance_ports"
        assert len(result["instance_port_connections"]) == 2

    async def test_trace_x_source(self, tmp_path):
        _prefill_build_tb_hierarchy_state()
        rtl = tmp_path / "dut.sv"
        compile_log = tmp_path / "compile.log"
        wave_path = tmp_path / "wave.vcd"
        rtl.write_text(
            """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic x_sig;
  logic out_sig;
  assign out_sig = x_sig;
endmodule
"""
        )
        compile_log.write_text(
            f"""\
Chronologic VCS simulator
Parsing design file '{rtl}'
Top Level Modules:
    top_tb
"""
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module u0 $end
$var wire 1 ! x_sig $end
$var wire 1 " out_sig $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
x!
x"
"""
        )

        result = await server._dispatch(
            "trace_x_source",
            {
                "signal_path": "top_tb.u0.out_sig",
                "wave_path": str(wave_path),
                "compile_log": str(compile_log),
                "time_ps": 0,
                "top_hint": "top_tb",
            },
        )

        assert result["trace_status"] == "driver_unresolved"
        assert len(result["propagation_chain"]) == 2
        assert result["propagation_chain"][0]["signal_path"] == "top_tb.u0.out_sig"
        assert result["propagation_chain"][1]["signal_path"] == "top_tb.u0.x_sig"

    async def test_trace_x_source_signal_not_in_waveform(self, tmp_path):
        _prefill_build_tb_hierarchy_state()
        rtl = tmp_path / "dut.sv"
        compile_log = tmp_path / "compile.log"
        wave_path = tmp_path / "wave.vcd"
        rtl.write_text(
            """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic only_sig;
endmodule
"""
        )
        compile_log.write_text(
            f"""\
Chronologic VCS simulator
Parsing design file '{rtl}'
Top Level Modules:
    top_tb
"""
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$scope module u0 $end
$var wire 1 ! different_sig $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
"""
        )

        result = await server._dispatch(
            "trace_x_source",
            {
                "signal_path": "top_tb.u0.only_sig",
                "wave_path": str(wave_path),
                "compile_log": str(compile_log),
                "time_ps": 0,
                "top_hint": "top_tb",
            },
        )

        assert result["trace_status"] == "signal_not_in_waveform"
        assert result["propagation_chain"][0]["trace_stop_reason"] == "signal_not_in_waveform"


@pytest.mark.anyio
class TestCallToolErrors:
    async def test_fsdb_runtime_error_is_structured(self):
        with patch("server._dispatch", side_effect=RuntimeError("FSDB parsing unavailable: runtime missing")):
            result = await server.call_tool("search_signals", {"wave_path": "/tmp/a.fsdb", "keyword": "sig"})

        payload = result[0].text
        assert "fsdb_runtime_unavailable" in payload
        assert "prefer_vcd_waveforms" in payload

    async def test_generic_error_is_serialized_through_tool_error_result(self):
        with patch("server._dispatch", side_effect=ValueError("boom")):
            result = await server.call_tool("search_signals", {"wave_path": "/tmp/a.vcd", "keyword": "sig"})

        payload = json.loads(result[0].text)
        parsed = ToolErrorResult.model_validate(payload)
        assert parsed.error == "boom"


@pytest.mark.anyio
class TestPrerequisiteGating:
    async def test_parse_sim_log_blocked_without_get_sim_paths(self, tmp_path):
        log_path = tmp_path / "run.log"
        log_path.write_text("module_a ERROR issue @ 1 ns\n")
        result = await server._dispatch(
            "parse_sim_log",
            {"log_path": str(log_path), "simulator": "vcs"},
        )
        assert result["ok"] is False
        assert result["error_code"] == "missing_prerequisite"
        assert result["missing_step"] == "get_sim_paths"
        assert result["required_before"] == "parse_sim_log"
        assert result["suggested_call"]["tool"] == "get_sim_paths"

    async def test_parse_sim_log_passes_after_get_sim_paths(self, tmp_path):
        work_dir = tmp_path / "work"
        case_dir = work_dir / "work_case0"
        case_dir.mkdir(parents=True)
        elab_log = work_dir / "elab.log"
        sim_log = case_dir / "irun.log"
        elab_log.write_text("xrun\nxmelab\n")
        sim_log.write_text("module_a ERROR issue @ 1 ns\n")

        await server._dispatch(
            "get_sim_paths",
            {"verif_root": str(work_dir), "case_name": "case0"},
        )

        result = await server._dispatch(
            "parse_sim_log",
            {"log_path": str(sim_log), "simulator": "xcelium"},
        )
        assert "schema_version" in result
        assert result.get("ok") is not False

    async def test_analyze_failures_blocked_without_build_tb_hierarchy(self, tmp_path):
        _prefill_get_sim_paths_state()
        result = await server._dispatch(
            "analyze_failures",
            {
                "log_path": "/tmp/run.log",
                "wave_path": "/tmp/wave.vcd",
                "signal_paths": ["top.sig"],
                "simulator": "vcs",
            },
        )
        assert result["ok"] is False
        assert result["error_code"] == "missing_prerequisite"
        assert result["missing_step"] == "build_tb_hierarchy"
        assert result["required_before"] == "analyze_failures"

    async def test_search_signals_no_gate(self, tmp_path):
        wave_path = tmp_path / "wave.vcd"
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
"""
        )
        result = await server._dispatch(
            "search_signals",
            {"wave_path": str(wave_path), "keyword": "clk"},
        )
        assert result.get("ok") is not False
        assert any("clk" in m["path"] for m in result["results"])

    async def test_get_sim_paths_clears_build_tb_hierarchy_state(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        assert server._session_state["build_tb_hierarchy"] is not None

        work_dir = tmp_path / "work"
        case_dir = work_dir / "work_case0"
        case_dir.mkdir(parents=True)
        elab_log = work_dir / "elab.log"
        sim_log = case_dir / "irun.log"
        elab_log.write_text("xrun\nxmelab\n")
        sim_log.write_text("sim ok\n")

        await server._dispatch(
            "get_sim_paths",
            {"verif_root": str(work_dir), "case_name": "case0"},
        )
        assert server._session_state["build_tb_hierarchy"] is None
        assert server._session_state["get_sim_paths"] is not None

    async def test_suggested_call_includes_compile_log(self):
        _prefill_get_sim_paths_state(
            compile_log="/my/elab.log",
            simulator="xcelium",
        )
        result = await server._dispatch(
            "analyze_failures",
            {
                "log_path": "/tmp/run.log",
                "wave_path": "/tmp/wave.vcd",
                "signal_paths": ["top.sig"],
                "simulator": "vcs",
            },
        )
        assert result["ok"] is False
        suggested = result["suggested_call"]
        assert suggested["tool"] == "build_tb_hierarchy"
        assert suggested["arguments"]["compile_log"] == "/my/elab.log"
        assert suggested["arguments"]["simulator"] == "xcelium"


class TestWaveCacheInvalidation:
    def test_get_parser_invalidates_when_file_changes(self, monkeypatch, tmp_path):
        created = []

        class FakeParser:
            def __init__(self, file_path):
                self.file_path = file_path
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        wave = tmp_path / "wave.vcd"
        wave.write_text("$date\n$end\n")
        server._parser_cache.clear()
        monkeypatch.setattr(server, "VCDParser", FakeParser)

        first = server._get_parser(str(wave))
        os.utime(wave, None)
        wave.write_text("$date\n$end\n#1\n0!\n")
        second = server._get_parser(str(wave))

        assert first is not second
        assert created[0].closed is True
        assert created[1].closed is False


class _FakeParserForGuards:
    """Minimal parser double for get_signals_around_time guard tests."""

    def __init__(self, clock_period_ps=None, sim_end_ps=6_300_000_000):
        self._clock_period_ps = clock_period_ps
        self._sim_end_ps = sim_end_ps
        self.called_with = None

    def search_signals(self, keyword, max_results=20):
        if self._clock_period_ps is None:
            return {"results": []}
        return {"results": [{"path": "top_tb.clk", "width": 1}]}

    def get_transitions(self, signal_path, start_ps, end_ps):
        if self._clock_period_ps is None:
            return {"transitions": []}
        half_period = self._clock_period_ps // 2
        transitions = []
        for index in range(20):
            transitions.append(
                {"time_ps": index * half_period, "value": {"dec": index % 2}}
            )
        return {"transitions": transitions}

    def get_summary(self):
        return {"simulation_duration_ps": self._sim_end_ps}

    def get_signals_around_time(self, signal_paths, center_ps, window_ps, extra):
        self.called_with = (signal_paths, center_ps, window_ps, extra)
        return {
            "center_time_ps": center_ps,
            "center_time_ns": center_ps / 1000,
            "window_ps": window_ps,
            "extra_transitions": extra,
            "signals": {},
            "truncated": False,
        }


class _FakeParserMultiSig:
    """Flexible parser double for clock auto-detect tests."""

    def __init__(self, sig_map, sim_end_ps=6_300_000_000):
        self._sigs = sig_map
        self._sim_end_ps = sim_end_ps
        self.called_with = None

    def search_signals(self, keyword, max_results=20):
        keyword = keyword.lower()
        results = [
            {"path": path, "width": width}
            for path, (width, _, _) in self._sigs.items()
            if keyword in path.lower()
        ]
        return {"results": results[:max_results]}

    def get_transitions(self, signal_path, start_ps, end_ps):
        _, period_ps, edge_count = self._sigs[signal_path]
        if period_ps is None or edge_count < 2:
            return {"transitions": []}

        transitions = []
        half_period = period_ps // 2
        for index in range(2 * edge_count):
            transitions.append(
                {"time_ps": index * half_period, "value": {"dec": index % 2}}
            )
        return {"transitions": transitions}

    def get_summary(self):
        return {"simulation_duration_ps": self._sim_end_ps}

    def get_signals_around_time(self, signal_paths, center_ps, window_ps, extra):
        self.called_with = (signal_paths, center_ps, window_ps, extra)
        return {
            "center_time_ps": center_ps,
            "center_time_ns": center_ps / 1000,
            "window_ps": window_ps,
            "extra_transitions": extra,
            "signals": {},
            "truncated": False,
        }


class _FakeParserClockDetectFailure(_FakeParserForGuards):
    """Parser double that forces clock auto-detect failure with a real exception."""

    def search_signals(self, keyword, max_results=20):
        raise RuntimeError(f"signal index unavailable for {keyword}")


@pytest.mark.anyio
class TestDispatchGetSignalsAroundTimeGuards:
    async def test_rejects_window_past_cycle_cap(self, monkeypatch):
        fake = _FakeParserForGuards(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError) as excinfo:
            await server._dispatch(
                "get_signals_around_time",
                {
                    "wave_path": "/tmp/a.fsdb",
                    "signal_paths": ["top_tb.dut.sig"],
                    "center_time_ps": 1_000_000,
                    "window_ps": 4_000_000_000,
                },
            )

        msg = str(excinfo.value)
        assert "20000 clock cycles" in msg
        assert "MAX_WAVE_WINDOW_CYCLES" in msg
        assert "get_signals_by_cycle" in msg
        assert fake.called_with is None

    async def test_rejects_center_past_sim_end(self, monkeypatch):
        fake = _FakeParserForGuards(
            clock_period_ps=200_000, sim_end_ps=6_300_000_000
        )
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError) as excinfo:
            await server._dispatch(
                "get_signals_around_time",
                {
                    "wave_path": "/tmp/a.fsdb",
                    "signal_paths": ["top_tb.dut.sig"],
                    "center_time_ps": 7_500_000_000,
                    "window_ps": 2_000,
                },
            )

        msg = str(excinfo.value)
        assert "past the recorded waveform end" in msg
        assert "ns->ps" in msg or "ns-ps" in msg
        assert fake.called_with is None

    async def test_rejects_center_one_ps_past_sim_end(self, monkeypatch):
        fake = _FakeParserForGuards(
            clock_period_ps=200_000, sim_end_ps=6_300_000_000
        )
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError) as excinfo:
            await server._dispatch(
                "get_signals_around_time",
                {
                    "wave_path": "/tmp/a.fsdb",
                    "signal_paths": ["top_tb.dut.sig"],
                    "center_time_ps": 6_300_000_001,
                    "window_ps": 500_000,
                },
            )

        msg = str(excinfo.value)
        assert "past the recorded waveform end" in msg
        assert fake.called_with is None

    async def test_happy_path_still_works(self, monkeypatch):
        fake = _FakeParserForGuards(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        await server._dispatch(
            "get_signals_around_time",
            {
                "wave_path": "/tmp/a.fsdb",
                "signal_paths": ["top_tb.dut.sig"],
                "center_time_ps": 1_000_000,
                "window_ps": 2_000,
            },
        )

        assert fake.called_with == (
            ["top_tb.dut.sig"],
            1_000_000,
            2_000,
            DEFAULT_EXTRA_TRANSITIONS,
        )

    @pytest.mark.parametrize(
        "window_ps,should_raise",
        [
            (256 * 200_000, False),
            (257 * 200_000, True),
        ],
    )
    async def test_cycle_cap_boundary_inclusive(
        self, monkeypatch, window_ps, should_raise
    ):
        fake = _FakeParserForGuards(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        args = {
            "wave_path": "/tmp/a.fsdb",
            "signal_paths": ["top_tb.dut.sig"],
            "center_time_ps": 1_000_000,
            "window_ps": window_ps,
        }

        if should_raise:
            with pytest.raises(ValueError):
                await server._dispatch("get_signals_around_time", args)
            assert fake.called_with is None
        else:
            await server._dispatch("get_signals_around_time", args)
            assert fake.called_with is not None

    async def test_detects_clock_named_clock(self, monkeypatch):
        fake = _FakeParserMultiSig(
            {
                "top_tb.sys_clock": (1, 200_000, 100),
                "top_tb.data": (8, None, 0),
            }
        )
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError) as excinfo:
            await server._dispatch(
                "get_signals_around_time",
                {
                    "wave_path": "/tmp/a.fsdb",
                    "signal_paths": ["top_tb.data"],
                    "center_time_ps": 1_000_000,
                    "window_ps": 257 * 200_000,
                },
            )

        msg = str(excinfo.value)
        assert "257 clock cycles" in msg
        assert "top_tb.sys_clock" in msg
        assert "FALLBACK_WAVE_WINDOW_PS" not in msg

    async def test_prefers_high_edge_density_over_gated_signal(self, monkeypatch):
        fake = _FakeParserMultiSig(
            {
                "top_tb.clk_gate": (1, 1_000_000, 2),
                "top_tb.clk": (1, 200_000, 1000),
                "top_tb.data": (8, None, 0),
            }
        )
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError) as excinfo:
            await server._dispatch(
                "get_signals_around_time",
                {
                    "wave_path": "/tmp/a.fsdb",
                    "signal_paths": ["top_tb.data"],
                    "center_time_ps": 1_000_000,
                    "window_ps": 257 * 200_000,
                },
            )

        msg = str(excinfo.value)
        assert "top_tb.clk" in msg
        assert "top_tb.clk_gate" not in msg
        assert "clock_period_ps=200000" in msg

    async def test_fallback_error_surfaces_clock_detect_failure_reason(self, monkeypatch):
        fake = _FakeParserClockDetectFailure(clock_period_ps=None)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError) as excinfo:
            await server._dispatch(
                "get_signals_around_time",
                {
                    "wave_path": "/tmp/a.fsdb",
                    "signal_paths": ["top_tb.data"],
                    "center_time_ps": 1_000_000,
                    "window_ps": 60_000_000,
                },
            )

        msg = str(excinfo.value)
        assert "FALLBACK_WAVE_WINDOW_PS" in msg
        assert "detection error:" in msg
        assert "RuntimeError: signal index unavailable for clk" in msg
        assert fake.called_with is None
