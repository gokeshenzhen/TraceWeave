"""
test_server.py
覆盖：MCP dispatch 层的关键参数透传和 sim path 发现结果
"""

import os
import tempfile
from pathlib import Path

import pytest
from unittest.mock import patch

import server


LOG_SAMPLE = """\
Booting simulation
module_a ERROR unique issue a @ 1 ns
module_b ERROR unique issue b @ 2 ns
module_c ERROR unique issue c @ 3 ns
"""


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
class TestDispatchParseSimLog:
    async def test_forwards_max_groups(self):
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
            assert result["detail_level"] == "compact"
            assert result["failure_events_total"] == 2
            assert result["failure_events_returned"] == 2
            assert result["failure_events_truncated"] is False
            assert result["failure_events"][0]["time_parse_status"] == "exact"
            assert result["failure_events"][0]["log_phase"] == "runtime"
        finally:
            Path(log_path).unlink()

    async def test_max_groups_limits_failure_events_to_summary_groups(self):
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

    async def test_diff_sim_failure_results(self):
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
        finally:
            Path(base_path).unlink()
            Path(new_path).unlink()


@pytest.mark.anyio
class TestNewAnalyzerTools:
    async def test_recommend_failure_debug_next_steps(self, tmp_path):
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

    async def test_recommend_failure_debug_next_steps_without_top_hint(self, tmp_path):
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

    async def test_explain_signal_driver(self, tmp_path):
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
        with patch("server._dispatch", side_effect=RuntimeError("FSDB 解析不可用：runtime missing")):
            result = await server.call_tool("search_signals", {"wave_path": "/tmp/a.fsdb", "keyword": "sig"})

        payload = result[0].text
        assert "fsdb_runtime_unavailable" in payload
        assert "prefer_vcd_waveforms" in payload


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
