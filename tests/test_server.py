"""
test_server.py
覆盖：MCP dispatch 层的关键参数透传和 sim path 发现结果
"""

import json
import os
import tempfile
import types
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


def _prefill_sweep_handshakes_cache(
    wave_path: str,
    interfaces=None,
    *,
    coverage_status: str = "complete",
    coverage_warnings=None,
    suggested_next_actions=None,
):
    """预填一个与 wave_path 兼容的 sweep_handshakes 缓存 + provenance。"""
    if interfaces is None and coverage_status == "complete":
        interfaces = [
            {
                "kind": "valid_ready",
                "scope": "top_tb.if0",
                "clock": "top_tb.clk",
                "valid": "top_tb.if0.valid",
                "ready": "top_tb.if0.ready",
                "flags": [],
            }
        ]
    server._result_cache["sweep_handshakes"] = server.schemas.HandshakeSweepResult.model_validate(
        {
            "wave_path": wave_path,
            "discovered_count": len(interfaces or []),
            "interface_count": len(interfaces or []),
            "flagged_count": sum(1 for iface in (interfaces or []) if iface.get("flags")),
            "coverage_status": coverage_status,
            "coverage_warnings": coverage_warnings or [],
            "suggested_next_actions": suggested_next_actions or [],
            "interfaces": interfaces or [],
        }
    )
    server._result_provenance["sweep_handshakes"] = {"wave_path": wave_path, "scope": None}


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


@pytest.mark.anyio
class TestProtocolBundleToolContract:
    async def test_tool_schema_requires_protocol(self):
        tools = await server.list_tools()
        tool = next(tool for tool in tools if tool.name == "suggest_protocol_bundles")

        assert tool.inputSchema["required"] == ["wave_path", "protocol"]
        assert tool.inputSchema["properties"]["protocol"]["enum"] == ["ahb", "apb"]

    async def test_dispatch_validates_protocol_bundle_result(self):
        with patch.object(server, "suggest_protocol_bundles", return_value={
            "wave_path": "/tmp/w.fsdb",
            "protocol": "ahb",
            "scope": None,
            "candidate_count": 1,
            "candidates": [{
                "protocol": "ahb",
                "scope": "tb",
                "direction_tag": "unknown",
                "direction_basis": "no mechanical side marker found",
                "direction_confidence": "unknown",
                "clock": "tb.hclk",
                "reset": None,
                "valid_htrans": "tb.htrans",
                "htrans_rule": "active",
                "psel": None,
                "penable": None,
                "ready": "tb.hready",
                "payload": ["tb.haddr"],
                "inspect_handshake_args": {
                    "clock": "tb.hclk",
                    "valid_htrans": "tb.htrans",
                    "htrans_rule": "active",
                    "ready": "tb.hready",
                    "payload": ["tb.haddr"],
                },
                "confidence": "high",
                "rationale": "AHB htrans/ready pair in scope tb",
                "needs": [],
                "warnings": [],
            }],
            "reason": None,
        }) as suggest_mock:
            result = await server._dispatch(
                "suggest_protocol_bundles",
                {"wave_path": "/tmp/w.fsdb", "protocol": "ahb"},
            )

        assert result["candidates"][0]["valid_htrans"] == "tb.htrans"
        suggest_mock.assert_called_once()

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

    async def test_cycle_query_schema_exposes_time_axes_and_rejects_unknown(self):
        tools = await server.list_tools()
        cycle_tool = next(tool for tool in tools if tool.name == "get_signals_by_cycle")

        assert cycle_tool.inputSchema["additionalProperties"] is False
        assert "start_time_ps" in cycle_tool.inputSchema["properties"]
        assert "end_time_ps" in cycle_tool.inputSchema["properties"]

    async def test_cycle_query_dispatch_resolves_start_time(self):
        fixture = Path(__file__).parent / "fixtures" / "cycle_test.vcd"
        result = await server._dispatch(
            "get_signals_by_cycle",
            {
                "wave_path": str(fixture),
                "clock_path": "top_tb.clk",
                "signal_paths": ["top_tb.data"],
                "start_time_ps": 1000,
                "num_cycles": 2,
            },
        )

        assert result["resolved_from_time"] is True
        assert result["requested_start_time_ps"] == 1000
        assert result["start_cycle"] == 1
        assert [cycle["time_ps"] for cycle in result["cycles"]] == [1500, 2500]

    async def test_cycle_query_dispatch_resolves_time_window(self):
        fixture = Path(__file__).parent / "fixtures" / "cycle_test.vcd"
        result = await server._dispatch(
            "get_signals_by_cycle",
            {
                "wave_path": str(fixture),
                "clock_path": "top_tb.clk",
                "signal_paths": ["top_tb.data"],
                "start_time_ps": 500,
                "end_time_ps": 1500,
            },
        )

        assert result["num_cycles_returned"] == 2
        assert result["requested_end_time_ps"] == 1500
        assert [cycle["time_ps"] for cycle in result["cycles"]] == [500, 1500]

    async def test_cycle_query_dispatch_rejects_mixed_start_axis(self):
        fixture = Path(__file__).parent / "fixtures" / "cycle_test.vcd"
        with pytest.raises(ValueError, match="start_time_ps and start_cycle"):
            await server._dispatch(
                "get_signals_by_cycle",
                {
                    "wave_path": str(fixture),
                    "clock_path": "top_tb.clk",
                    "signal_paths": ["top_tb.data"],
                    "start_time_ps": 500,
                    "start_cycle": 1,
                },
            )

    async def test_cycle_query_dispatch_rejects_mixed_count_axis(self):
        fixture = Path(__file__).parent / "fixtures" / "cycle_test.vcd"
        with pytest.raises(ValueError, match="end_time_ps and num_cycles"):
            await server._dispatch(
                "get_signals_by_cycle",
                {
                    "wave_path": str(fixture),
                    "clock_path": "top_tb.clk",
                    "signal_paths": ["top_tb.data"],
                    "end_time_ps": 1500,
                    "num_cycles": 2,
                },
            )


class _FakeFsdbParser:
    """Mimics the FSDB backend's signal naming: a bus's canonical path carries a
    [msb:lsb] suffix, so a bare bus name fails get_signal_width and must be
    resolved via search_signals (the P4 papercut). Scalars resolve as-is."""

    def __init__(self, table):
        self._table = dict(table)  # canonical path -> width

    def get_signal_width(self, path):
        if path in self._table:
            return self._table[path]
        raise KeyError(f"Signal not found: '{path}'")

    def search_signals(self, keyword, max_results=100):
        leaf = keyword.rsplit(".", 1)[-1]
        results = [
            {"path": p, "name": p.split(".")[-1], "width": w}
            for p, w in self._table.items()
            if leaf in p.split(".")[-1]
        ]
        return {"results": results[:max_results]}

    def get_value_at_time(self, path, time_ps):
        if path not in self._table:
            raise KeyError(f"Signal not found: '{path}'. Use search_signals first.")
        return {
            "signal": path,
            "time_ps": time_ps,
            "time_ns": time_ps / 1000,
            "value": {"bin": "1", "hex": "0x1", "dec": 1},
        }


_FAKE_TABLE = {
    "tb.u.HADDRM[31:0]": 32,
    "tb.u.HWRITEM": 1,
    "tb.u.HRESP[1:0]": 2,
}


class TestBareSignalNameResolution:
    def test_resolve_signal_list_autocompletes_bus_keeps_scalar(self):
        parser = _FakeFsdbParser(_FAKE_TABLE)
        resolved, aliases = server._resolve_signal_list(
            parser, ["tb.u.HADDRM", "tb.u.HWRITEM"]
        )
        assert resolved == ["tb.u.HADDRM[31:0]", "tb.u.HWRITEM"]
        assert aliases == {"tb.u.HADDRM": "tb.u.HADDRM[31:0]"}

    def test_resolve_signal_list_leaves_already_sliced_and_missing(self):
        parser = _FakeFsdbParser(_FAKE_TABLE)
        resolved, aliases = server._resolve_signal_list(
            parser, ["tb.u.HADDRM[31:0]", "tb.u.NOPE"]
        )
        assert resolved == ["tb.u.HADDRM[31:0]", "tb.u.NOPE"]
        assert aliases == {}

    def test_suggest_signal_paths_for_bare_bus(self):
        parser = _FakeFsdbParser(_FAKE_TABLE)
        assert server._suggest_signal_paths(parser, "tb.u.HRESP") == ["tb.u.HRESP[1:0]"]

    def test_suggest_signal_paths_empty_for_unknown_and_sliced(self):
        parser = _FakeFsdbParser(_FAKE_TABLE)
        assert server._suggest_signal_paths(parser, "tb.u.NOPE") == []
        assert server._suggest_signal_paths(parser, "tb.u.HADDRM[31:0]") == []


@pytest.mark.anyio
class TestBareSignalNameDispatch:
    async def test_get_signal_at_time_resolves_bare_bus(self, monkeypatch):
        parser = _FakeFsdbParser(_FAKE_TABLE)
        monkeypatch.setattr(server, "_get_parser", lambda _p: parser)
        result = await server._dispatch(
            "get_signal_at_time",
            {"wave_path": "x.fsdb", "signal_path": "tb.u.HADDRM", "time_ps": 1000},
        )
        assert result["signal"] == "tb.u.HADDRM[31:0]"
        assert result["resolved_from"] == "tb.u.HADDRM"

    async def test_get_signal_at_time_miss_appends_did_you_mean(self, monkeypatch):
        # tb.x.HADDRM: scope tb.x is absent so the bare name does not auto-resolve
        # (no same-scope [..] candidate), but the leaf HADDRM yields a cross-scope
        # suggestion.
        parser = _FakeFsdbParser(_FAKE_TABLE)
        monkeypatch.setattr(server, "_get_parser", lambda _p: parser)
        with pytest.raises(KeyError, match="did_you_mean.*tb.u.HADDRM"):
            await server._dispatch(
                "get_signal_at_time",
                {"wave_path": "x.fsdb", "signal_path": "tb.x.HADDRM", "time_ps": 1000},
            )


_SCOREBOARD_MISMATCH_LINE = (
    "UVM_ERROR /tmp/top_tb.sv(129) @ 1429.000 ns: uvm_test_top.env.scb "
    "[SCOREBOARD] expected=0x5a, actual=0x58 txn_id=84\n"
)


@pytest.mark.anyio
class TestParseSimLogSweepNextStep:
    async def test_protocol_symptom_emits_ready_to_run_sweep_call(self, tmp_path):
        _prefill_get_sim_paths_state()
        wave = tmp_path / "wave.fsdb"
        wave.write_text("")
        log = tmp_path / "run.log"
        log.write_text(_SCOREBOARD_MISMATCH_LINE)
        server._result_cache["get_sim_paths"] = types.SimpleNamespace(
            wave_files=[types.SimpleNamespace(path=str(wave))]
        )
        try:
            res = await server._dispatch(
                "parse_sim_log", {"log_path": str(log), "simulator": "vcs"}
            )
            assert res.protocol_symptom_hint is not None
            step = res.protocol_symptom_next_step
            assert step is not None, "sweep call not surfaced despite symptom + wave"
            assert step.tool == "sweep_handshakes"
            assert step.arguments["wave_path"] == str(wave)
        finally:
            server._result_cache.pop("get_sim_paths", None)

    async def test_no_sweep_call_without_a_waveform(self, tmp_path):
        _prefill_get_sim_paths_state()
        log = tmp_path / "run.log"
        log.write_text(_SCOREBOARD_MISMATCH_LINE)
        server._result_cache["get_sim_paths"] = types.SimpleNamespace(wave_files=[])
        try:
            res = await server._dispatch(
                "parse_sim_log", {"log_path": str(log), "simulator": "vcs"}
            )
            assert res.protocol_symptom_hint is not None  # symptom still reported
            assert res.protocol_symptom_next_step is None  # but no runnable call
        finally:
            server._result_cache.pop("get_sim_paths", None)

    async def test_no_sweep_call_without_protocol_symptom(self, tmp_path):
        _prefill_get_sim_paths_state()
        wave = tmp_path / "wave.fsdb"
        wave.write_text("")
        log = tmp_path / "run.log"
        log.write_text("module_a ERROR something went wrong @ 1 ns\n")
        server._result_cache["get_sim_paths"] = types.SimpleNamespace(
            wave_files=[types.SimpleNamespace(path=str(wave))]
        )
        try:
            res = await server._dispatch(
                "parse_sim_log", {"log_path": str(log), "simulator": "vcs"}
            )
            assert res.protocol_symptom_hint is None
            assert res.protocol_symptom_next_step is None
        finally:
            server._result_cache.pop("get_sim_paths", None)


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

            assert first.log_snapshot_id is not None
            assert second.log_snapshot_id is not None
            assert second.previous_log_snapshot_id == first.log_snapshot_id
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

    async def test_diff_same_overwritten_log_uses_previous_snapshot(self):
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

            stat = log_path.stat()
            log_path.write_text("module_b ERROR unique issue b @ 2 ns\n")
            os.utime(log_path, (stat.st_atime, stat.st_mtime + 1))

            result = await server._dispatch(
                "diff_sim_failure_results",
                {
                    "new_log_path": str(log_path),
                    "simulator": "vcs",
                },
            )

            assert result["diff_source"] == "auto_previous_snapshot"
            assert result["base_snapshot_id"] == first.log_snapshot_id
            assert result["new_snapshot_id"] is not None
            assert len(result["resolved_events"]) == 1
            assert len(result["new_events"]) == 1
        finally:
            log_path.unlink()

    async def test_diff_same_overwritten_log_preserves_baseline_when_stat_signature_matches(self):
        _prefill_get_sim_paths_state()
        before_text = "module_a ERROR same length a @ 1 ns\n"
        after_text = "module_b ERROR same length b @ 2 ns\n"
        assert len(before_text) == len(after_text)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(before_text)
            log_path = Path(handle.name)

        try:
            first = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": str(log_path),
                    "simulator": "vcs",
                },
            )

            stat = log_path.stat()
            log_path.write_text(after_text)
            os.utime(log_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

            result = await server._dispatch(
                "diff_sim_failure_results",
                {
                    "new_log_path": str(log_path),
                    "simulator": "vcs",
                },
            )

            assert result["diff_source"] == "auto_previous_snapshot"
            assert result["base_snapshot_id"] == first.log_snapshot_id
            assert result["new_snapshot_id"] != first.log_snapshot_id
            assert result["base_summary"]["groups"] == {"ERROR: module_a ERROR same length a @ 1 ns": 1}
            assert result["new_summary"]["groups"] == {"ERROR: module_b ERROR same length b @ 2 ns": 1}
        finally:
            log_path.unlink()

    async def test_diff_can_compare_explicit_snapshot_ids_after_same_path_rerun(self):
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

            result = await server._dispatch(
                "diff_sim_failure_results",
                {
                    "base_snapshot_id": first.log_snapshot_id,
                    "new_snapshot_id": second.log_snapshot_id,
                    "simulator": "vcs",
                },
            )

            assert result["diff_source"] == "snapshots"
            assert result["base_snapshot_id"] == first.log_snapshot_id
            assert result["new_snapshot_id"] == second.log_snapshot_id
            assert len(result["resolved_events"]) == 1
            assert len(result["new_events"]) == 1
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

    async def test_summary_first_group_context_uses_small_window(self):
        _prefill_get_sim_paths_state()
        lines = [f"info before {idx}\n" for idx in range(35)]
        lines.append("module_a ERROR bounded context issue @ 100 ns\n")
        lines.extend(f"info after {idx}\n" for idx in range(35))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write("".join(lines))
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                },
            )

            ctx = result["first_group_context"]
            assert ctx is not None
            assert ctx["end_line"] - ctx["start_line"] + 1 <= 25
            assert "module_a ERROR bounded context issue" in ctx["context"]
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

    async def test_full_detail_slims_returned_failure_events_only(self):
        _prefill_get_sim_paths_state()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as handle:
            handle.write(
                "UVM_ERROR /tmp/top_tb.sv(10) @ 1 ns: reporter [TOP] repeated issue\n"
                "UVM_ERROR /tmp/top_tb.sv(10) @ 2 ns: reporter [TOP] repeated issue\n"
            )
            log_path = handle.name

        try:
            result = await server._dispatch(
                "parse_sim_log",
                {
                    "log_path": log_path,
                    "simulator": "vcs",
                    "detail_level": "full",
                },
            )

            event = result["failure_events"][0]
            assert "log_path" not in event
            assert "field_provenance" in event
            structured_fields = event.get("structured_fields") or {}
            assert structured_fields.get("reporter") != event["instance_path"]

            original = server._result_provenance["parse_sim_log"]["all_failure_events"][0]
            assert original["log_path"] == log_path
            assert original["structured_fields"]["reporter"] == original["instance_path"]
            assert original["structured_fields"]["tag"] == "TOP"
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
        _prefill_sweep_handshakes_cache(str(wave_path))

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
        _prefill_sweep_handshakes_cache(str(wave_path))

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

    async def test_recommend_consumes_sweep_cache_into_runtime_protocol_findings(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sb.sv", 66: top_tb.sb.route: started at 10ps failed at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$var wire 1 ! req $end
$upscope $end
$enddefinitions $end
#0
0!
#20
1!
"""
        )
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {"scan_scope": "scope1", "files_scanned": 1, "total_risks": 0, "risks": [],
             "categories_scanned": ["magic_condition"], "skipped_files": []}
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/elab.log",
            "simulator": "vcs",
        }
        _prefill_sweep_handshakes_cache(
            str(wave_path),
            interfaces=[
                {
                    "kind": "ahb",
                    "scope": "top_tb.m_if0",
                    "clock": "top_tb.HCLK",
                    "valid": "top_tb.m_if0.HTRANS",
                    "ready": "top_tb.m_if0.HREADY",
                    "flags": ["premature_valid_deassertion"],
                    "valid_deassert_violations": 1,
                    "max_stall_cycles": 4,
                    "attribution": {
                        "violating_side": "valid_driver",
                        "exonerated_side": "ready_driver",
                        "basis": "protocol_valid_hold_obligation",
                    },
                },
            ],
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

        findings = result["runtime_protocol_findings"]
        assert findings, "sweep cache should populate runtime_protocol_findings"
        top = findings[0]
        assert top["kind"] == "ahb"
        assert "HTRANS" in top["valid"]
        assert top["flags"] == ["premature_valid_deassertion"]
        assert top["attribution"]["violating_side"] == "valid_driver"
        assert "valid" in top["relevance_reason"].lower()
        # sweep cache present ⇒ workflow not flagged incomplete for a missing sweep
        assert result["degraded_reason"] is None

    async def test_recommend_requires_sweep_on_protocol_symptom_when_missing(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text(
            '"/path/sb.sv", 66: top_tb.sb: READ-BACK mismatch at 20ps\n'
        )
        wave_path.write_text(
            """\
$timescale 1ps $end
$scope module top_tb $end
$var wire 1 ! req $end
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
                "problem_hints": {"has_x": False, "has_z": False, "error_pattern": "mismatch"},
            }
        )
        server._result_provenance["parse_sim_log"] = {
            "log_path": str(log_path),
            "simulator": "vcs",
        }
        # scan cache present, sweep cache absent ⇒ on a protocol/mismatch symptom
        # the sweep takes priority over the (already-satisfied) structural scan.
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {"scan_scope": "scope1", "files_scanned": 1, "total_risks": 0, "risks": [],
             "categories_scanned": ["magic_condition"], "skipped_files": []}
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

        assert result["workflow_incomplete"] is True
        assert result["degraded_reason"] == "missing_handshake_sweep"
        assert result["required_next_call"]["tool"] == "sweep_handshakes"
        assert result["required_next_call"]["arguments"]["wave_path"] == str(wave_path)

    async def test_recommend_treats_zero_coverage_sweep_as_incomplete(self, tmp_path):
        _prefill_get_sim_paths_state()
        _prefill_build_tb_hierarchy_state()
        log_path = tmp_path / "run.log"
        wave_path = tmp_path / "wave.vcd"
        log_path.write_text('"/path/sb.sv", 66: top_tb.sb: READ-BACK mismatch at 20ps\n')
        wave_path.write_text("$timescale 1ps $end\n$enddefinitions $end\n")
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
                "problem_hints": {"has_x": False, "has_z": False, "error_pattern": "mismatch"},
            }
        )
        server._result_provenance["parse_sim_log"] = {
            "log_path": str(log_path),
            "simulator": "vcs",
        }
        server._result_cache["scan_structural_risks"] = server.schemas.ScanStructuralRisksResult.model_validate(
            {"scan_scope": "scope1", "files_scanned": 1, "total_risks": 0, "risks": [],
             "categories_scanned": ["magic_condition"], "skipped_files": []}
        )
        server._result_provenance["scan_structural_risks"] = {
            "compile_log": "/tmp/verif/work/elab.log",
            "simulator": "vcs",
        }
        _prefill_sweep_handshakes_cache(
            str(wave_path),
            interfaces=[],
            coverage_status="zero_coverage",
            coverage_warnings=["ZERO COVERAGE: no protocol interfaces checked"],
            suggested_next_actions=[
                {
                    "tool": "sweep_handshakes",
                    "arguments": {"wave_path": str(wave_path)},
                    "reason": "Retry without scope.",
                }
            ],
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
        assert result["degraded_reason"] == "incomplete_handshake_sweep"
        assert result["required_next_call"]["tool"] == "sweep_handshakes"
        assert result["required_next_call"]["arguments"]["wave_path"] == str(wave_path)

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

    async def test_telemetry_receives_error_code_on_exception(self):
        seen = {}

        def spy(tool, args, **kw):
            seen.update(kw, tool=tool)

        with patch("server._dispatch", side_effect=ValueError("boom")), \
             patch.object(server.usage_telemetry, "record_call", spy):
            await server.call_tool("search_signals", {"wave_path": "/tmp/a.vcd", "keyword": "sig"})

        # Exception class name is the fallback classification code so failure
        # telemetry is analyzable without guessing from result byte sizes.
        assert seen["error_code"] == "ValueError"
        assert seen["ok"] is False

    async def test_telemetry_error_code_none_on_success(self):
        seen = {}

        def spy(tool, args, **kw):
            seen.update(kw, tool=tool)

        with patch("server._dispatch", return_value={"keyword": "sig", "total_matched": 0, "results": []}), \
             patch.object(server.usage_telemetry, "record_call", spy):
            await server.call_tool("search_signals", {"wave_path": "/tmp/a.vcd", "keyword": "sig"})

        assert seen["error_code"] is None
        assert seen["ok"] is True


@pytest.mark.anyio
class TestSignalTransitionsCap:
    """get_signal_transitions caps the returned list at the dispatch layer
    (telemetry showed a single uncapped call returning 8.9MB); internal
    callers of parser.get_transitions() still see the full list."""

    _FIXTURE = Path(__file__).parent / "fixtures" / "cycle_test.vcd"

    async def test_schema_exposes_max_transitions_default(self):
        tools = await server.list_tools()
        tool = next(t for t in tools if t.name == "get_signal_transitions")
        assert tool.inputSchema["properties"]["max_transitions"]["default"] == \
            server.TRANSITIONS_MAX_RETURNED

    async def test_under_cap_is_untouched(self):
        result = await server._dispatch(
            "get_signal_transitions",
            {"wave_path": str(self._FIXTURE), "signal_path": "top_tb.clk"},
        )
        assert result.truncated is False
        assert result.hint is None
        assert result.transition_count == len(result.transitions)

    async def test_explicit_cap_truncates_keeps_earliest_and_hints(self):
        full = await server._dispatch(
            "get_signal_transitions",
            {"wave_path": str(self._FIXTURE), "signal_path": "top_tb.clk"},
        )
        assert full.transition_count > 2  # fixture precondition

        capped = await server._dispatch(
            "get_signal_transitions",
            {"wave_path": str(self._FIXTURE), "signal_path": "top_tb.clk",
             "max_transitions": 2},
        )
        assert capped.truncated is True
        assert len(capped.transitions) == 2
        # transition_count stays the TOTAL found, not the returned length.
        assert capped.transition_count == full.transition_count
        assert capped.transitions == full.transitions[:2]
        assert "max_transitions" in capped.hint

    async def test_rejects_nonpositive_cap(self):
        with pytest.raises(ValueError, match="max_transitions"):
            await server._dispatch(
                "get_signal_transitions",
                {"wave_path": str(self._FIXTURE), "signal_path": "top_tb.clk",
                 "max_transitions": 0},
            )


@pytest.mark.anyio
class TestSearchSignalsBatch:
    """keyword accepts a list to batch several lookups in one call — the fix
    for the consecutive keyword-groping chains telemetry surfaced (334/524
    search calls arrived in runs of >=4). A single string keeps the exact
    single-search result shape."""

    _FIXTURE = Path(__file__).parent / "fixtures" / "cycle_test.vcd"

    async def test_list_keyword_returns_one_entry_per_keyword_in_order(self):
        result = await server._dispatch(
            "search_signals",
            {"wave_path": str(self._FIXTURE),
             "keyword": ["clk", "data", "no_such_signal_xyz"]},
        )
        assert [e.keyword for e in result.batch] == ["clk", "data", "no_such_signal_xyz"]
        assert any("clk" in m["path"] for m in result.batch[0].results)
        assert any("data" in m["path"] for m in result.batch[1].results)
        # A zero-match keyword is a fact entry, never an error.
        assert result.batch[2].total_matched == 0
        assert result.batch[2].results == []

    async def test_single_string_keeps_single_search_shape(self):
        result = await server._dispatch(
            "search_signals",
            {"wave_path": str(self._FIXTURE), "keyword": "clk"},
        )
        assert isinstance(result, server.schemas.SearchSignalsResult)
        assert result.keyword == "clk"

    async def test_empty_list_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            await server._dispatch(
                "search_signals",
                {"wave_path": str(self._FIXTURE), "keyword": []},
            )

    async def test_oversized_list_rejected(self):
        too_many = [f"kw{i}" for i in range(server.SIGNAL_SEARCH_MAX_KEYWORDS + 1)]
        with pytest.raises(ValueError, match="max"):
            await server._dispatch(
                "search_signals",
                {"wave_path": str(self._FIXTURE), "keyword": too_many},
            )

    async def test_fsdb_batch_reuses_one_cached_index(self, monkeypatch):
        created = []

        class _FakeIndex:
            def __init__(self, path):
                created.append(self)
                self.searched = []

            def search(self, kw, max_r):
                self.searched.append(kw)
                return {"keyword": kw, "total_matched": 0, "results": []}

        monkeypatch.setattr(server, "FSDBSignalIndex", _FakeIndex)
        monkeypatch.setattr(server, "_get_wave_signature", lambda p: ("sig",))
        monkeypatch.setattr(server, "_fsdb_index_cache", {})

        result = await server._dispatch(
            "search_signals",
            {"wave_path": "/tmp/x.fsdb", "keyword": ["a", "b"]},
        )
        assert len(created) == 1  # one index build serves the whole batch
        assert created[0].searched == ["a", "b"]
        assert [e.keyword for e in result.batch] == ["a", "b"]


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


class _FakeParserValuesOnly(_FakeParserForGuards):
    """Parser double returning rich per-signal payloads for values_only tests.

    top_tb.dut.data carries a dip-and-return around the centre (1 -> 0 -> 1) so
    annotate_center_transients fires; the annotation must survive stripping."""

    def get_signals_around_time(self, signal_paths, center_ps, window_ps, extra):
        self.called_with = (signal_paths, center_ps, window_ps, extra)

        def _v(bit):
            return {"bin": bit, "hex": bit, "dec": int(bit)}

        def _tr(t, bit):
            return {"time_ps": t, "time_ns": t / 1000, "value": _v(bit)}

        return {
            "center_time_ps": center_ps,
            "center_time_ns": center_ps / 1000,
            "window_ps": window_ps,
            "extra_transitions": extra,
            "signals": {
                "top_tb.dut.data": {
                    "value_at_center": _v("0"),
                    "transitions_in_window": [
                        _tr(center_ps - 2000, "1"),
                        _tr(center_ps - 500, "0"),
                        _tr(center_ps + 500, "1"),
                    ],
                    "pre_window_transitions": [_tr(0, "0")],
                },
                "top_tb.dut.quiet": {
                    "value_at_center": _v("1"),
                    "transitions_in_window": [],
                    "pre_window_transitions": [],
                },
                "top_tb.dut.broken": {"error": "signal_not_found"},
            },
            "truncated": False,
        }


@pytest.mark.anyio
class TestDispatchGetSignalsAroundTimeValuesOnly:
    _ARGS = {
        "wave_path": "/tmp/a.fsdb",
        "signal_paths": ["top_tb.dut.data", "top_tb.dut.quiet", "top_tb.dut.broken"],
        "center_time_ps": 1_000_000,
        "window_ps": 4_000,
    }

    async def test_values_only_strips_transition_lists(self, monkeypatch):
        fake = _FakeParserValuesOnly(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        result = await server._dispatch(
            "get_signals_around_time",
            {**self._ARGS, "return_mode": "values_only"},
        )

        assert result.return_mode == "values_only"
        data = result.signals["top_tb.dut.data"]
        assert "transitions_in_window" not in data
        assert "pre_window_transitions" not in data
        assert data["value_at_center"]["bin"] == "0"
        assert data["window_transition_count"] == 3
        quiet = result.signals["top_tb.dut.quiet"]
        assert quiet["window_transition_count"] == 0
        # error entries pass through untouched
        assert result.signals["top_tb.dut.broken"] == {"error": "signal_not_found"}

    async def test_values_only_keeps_transient_annotation(self, monkeypatch):
        """annotate_center_transients runs BEFORE stripping: the dip-and-return
        flag must survive even though the transitions it was derived from are gone."""
        fake = _FakeParserValuesOnly(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        result = await server._dispatch(
            "get_signals_around_time",
            {**self._ARGS, "return_mode": "values_only"},
        )

        data = result.signals["top_tb.dut.data"]
        assert data["center_transient"] is True
        assert data["center_settles_to"]["bin"] == "1"
        assert result.transient_note is not None

    async def test_full_mode_is_default_and_keeps_transitions(self, monkeypatch):
        fake = _FakeParserValuesOnly(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        result = await server._dispatch("get_signals_around_time", dict(self._ARGS))

        assert result.return_mode == "full"
        data = result.signals["top_tb.dut.data"]
        assert len(data["transitions_in_window"]) == 3
        assert len(data["pre_window_transitions"]) == 1

    async def test_unknown_return_mode_rejected_before_parser_call(self, monkeypatch):
        fake = _FakeParserValuesOnly(clock_period_ps=200_000)
        monkeypatch.setattr(server, "_get_parser", lambda wave_path: fake)

        with pytest.raises(ValueError, match="return_mode"):
            await server._dispatch(
                "get_signals_around_time",
                {**self._ARGS, "return_mode": "compact"},
            )
        assert fake.called_with is None


# Two signals identical until #20 (20_000 ps), then b stays high while a falls.
_DIVERGE_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 # b $end
$upscope $end
$enddefinitions $end
#0
0!
0#
#10000
1!
1#
#20000
0!
1#
#30000
0!
0#
"""


@pytest.mark.anyio
class TestCursorAndDiffDispatch:
    """Auto-debug v2: diff_first_divergence registers a cursor that
    downstream time-taking tools can dereference via '@name'."""

    async def test_diff_registers_cursor_consumed_by_get_signal_at_time(self, tmp_path):
        wave = tmp_path / "diverge.vcd"
        wave.write_text(_DIVERGE_VCD)

        diff = await server._dispatch(
            "diff_first_divergence",
            {
                "wave_path_a": str(wave),
                "signal_a": "top.a",
                "wave_path_b": str(wave),
                "signal_b": "top.b",
            },
        )
        assert diff["diverged"] is True
        assert diff["first_divergence_time_ps"] == 20_000
        cursor_name = diff["cursor"]["name"]

        # cursor_list surfaces it
        listed = await server._dispatch("cursor_list", {})
        assert any(c["name"] == cursor_name for c in listed["cursors"])

        # get_signal_at_time accepts '@cursor' in place of an integer
        at = await server._dispatch(
            "get_signal_at_time",
            {
                "wave_path": str(wave),
                "signal_path": "top.a",
                "time_ps": f"@{cursor_name}",
            },
        )
        assert at["time_ps"] == 20_000
        assert at["value"]["bin"] == "0"

    async def test_unit_literal_time_input(self, tmp_path):
        wave = tmp_path / "diverge.vcd"
        wave.write_text(_DIVERGE_VCD)

        at = await server._dispatch(
            "get_signal_at_time",
            {
                "wave_path": str(wave),
                "signal_path": "top.b",
                "time_ps": "20ns",
            },
        )
        assert at["time_ps"] == 20_000
        assert at["value"]["bin"] == "1"

    async def test_period_dispatch_registers_offbeat_cursor(self, tmp_path):
        # 1ps timescale; posedges at 10,20,35,45 → off-beat ends at 35.
        wave = tmp_path / "jitter.vcd"
        wave.write_text(
            "$timescale 1ps $end\n"
            "$scope module top $end\n$var wire 1 ! clk $end\n$upscope $end\n"
            "$enddefinitions $end\n"
            "#0\n0!\n#10\n1!\n#13\n0!\n#20\n1!\n#23\n0!\n#35\n1!\n#38\n0!\n#45\n1!\n"
        )
        result = await server._dispatch(
            "period",
            {"wave_path": str(wave), "signal": "top.clk", "edge": "posedge"},
        )
        assert result["period_ps"] == 10
        assert result["first_off_beat_time_ps"] == 35
        cursor_name = result["cursor"]["name"]

        # Off-beat cursor is dereferenceable by downstream time inputs.
        at = await server._dispatch(
            "get_signal_at_time",
            {"wave_path": str(wave), "signal_path": "top.clk", "time_ps": f"@{cursor_name}"},
        )
        assert at["time_ps"] == 35

    async def test_cursor_set_and_delete_roundtrip(self):
        created = await server._dispatch(
            "cursor_set", {"name": "anchor", "time_ps": 5000, "note": "n"}
        )
        assert created["cursor"]["name"] == "anchor"
        assert created["cursor"]["time_ps"] == 5000

        deleted = await server._dispatch("cursor_delete", {"name": "anchor"})
        assert deleted["deleted"] is True
        again = await server._dispatch("cursor_delete", {"name": "anchor"})
        assert again["deleted"] is False

    async def test_unknown_cursor_reference_raises(self, tmp_path):
        wave = tmp_path / "diverge.vcd"
        wave.write_text(_DIVERGE_VCD)
        with pytest.raises(Exception):
            await server._dispatch(
                "get_signal_at_time",
                {
                    "wave_path": str(wave),
                    "signal_path": "top.a",
                    "time_ps": "@nope",
                },
            )
