import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.compile_log_parser import detect_simulator, parse_compile_log


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _make_demo_tree():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    _write(root / "tb" / "top_tb.sv", "module top_tb; endmodule\n")
    _write(root / "tb" / "my_driver.sv", "class my_driver extends uvm_driver; endclass\n")
    _write(root / "dut" / "dut.sv", "module dut; endmodule\n")
    _write(root / "assertion" / "sva_top.sv", "module sva_top; endmodule\n")
    return tmp, root


class TestDetectSimulator:
    def test_detect_vcs(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("Chronologic VCS\n")
            path = f.name
        try:
            assert detect_simulator(path) == "vcs"
        finally:
            os.unlink(path)

    def test_detect_xcelium(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("xrun\n")
            path = f.name
        try:
            assert detect_simulator(path) == "xcelium"
        finally:
            os.unlink(path)

    def test_detect_vcs_banner_buried_past_line_20(self):
        fixture = Path(__file__).parent / "fixtures" / "uvm_demo_cc18_comp_head.log"
        assert detect_simulator(str(fixture)) == "vcs"


class TestParseCompileLog:
    def test_parse_vcs_compile_log(self):
        tmp, root = _make_demo_tree()
        try:
            log = root / "comp.log"
            log.write_text(
                f"""Command: vcs -f {root / 'dut' / 'filelist.f'} +incdir+{root / 'tb'} /tools/synopsys/vcs/etc/uvm.sv
Parsing design file '{root / 'tb' / 'top_tb.sv'}'
Parsing included file 'my_driver.sv'.
Back to file '{root / 'tb' / 'top_tb.sv'}'.
Parsing design file '{root / 'dut' / 'dut.sv'}'
Parsing design file '{root / 'assertion' / 'sva_top.sv'}'
Top Level Modules:
       top_tb
       dut
"""
            )
            result = parse_compile_log(str(log), "vcs")
            user_paths = {Path(item["path"]).name for item in result["files"]["user"]}
            assert {"top_tb.sv", "my_driver.sv", "dut.sv", "sva_top.sv"} <= user_paths
            assert result["files"]["filtered_count"] == 0
            top_tb = str((root / "tb" / "top_tb.sv").resolve())
            assert str((root / "tb" / "my_driver.sv").resolve()) in result["include_tree"][top_tb]
            assert "top_tb" in result["top_modules"]
        finally:
            tmp.cleanup()

    def test_parse_xcelium_compile_log(self):
        tmp, root = _make_demo_tree()
        try:
            log = root / "elab.log"
            log.write_text(
                f"""xrun
\t-f {root / 'dut' / 'filelist.f'}
\t\t{root / 'dut' / 'dut.sv'}
\t\t{root / 'tb' / 'top_tb.sv'}
\t-top top_tb
file: {root / 'dut' / 'dut.sv'}
\tmodule worklib.dut:sv
file: {root / 'tb' / 'top_tb.sv'}
\tinterface worklib.my_if:sv
\tmodule worklib.top_tb:sv
"""
            )
            result = parse_compile_log(str(log), "xcelium")
            user_paths = {Path(item["path"]).name for item in result["files"]["user"]}
            assert {"top_tb.sv", "dut.sv"} <= user_paths
            assert result["filelist_tree"]["filelist.f"] == []
            assert result["interfaces"] == ["my_if"]
            assert result["top_modules"] == ["top_tb"]
        finally:
            tmp.cleanup()

    def test_xcelium_command_stops_before_unindented_log_output(self, tmp_path):
        source = tmp_path / "top_tb.sv"
        _write(source, "module top_tb; endmodule\n")
        log = tmp_path / "elab.log"
        _write(
            log,
            "xrun(64): 26.03-s001-20260323: started\n"
            "xrun\n"
            "\t-elaborate\n"
            f"\t{source}\n"
            "\t-timescale 1ns/10ps\n"
            "\t-top top_tb\n"
            "DEFINE std ./STD\n"
            "|\n"
            "xrun: *W,DLCPTH: library warning\n"
            f"file: {source}\n"
            "\tmodule worklib.top_tb:sv\n",
        )

        result = parse_compile_log(str(log), "xcelium")

        assert result["compile_command"] == (
            f"xrun -elaborate {source} -timescale 1ns/10ps -top top_tb"
        )
        assert "DEFINE" not in result["compile_command"]
        assert "*W,DLCPTH" not in result["compile_command"]

    def test_xcelium_wrapper_command_anchors_relative_files_to_xrun_cwd(self, tmp_path):
        work = tmp_path / "fusesoc work"
        source = work / "src" / "top_earlgrey.sv"
        filelist = work / "design.scr"
        _write(source, "module top_earlgrey; endmodule\n")
        _write(filelist, "src/top_earlgrey.sv\n")
        log = tmp_path / "default" / "build.log"
        _write(
            log,
            "[make]: build\n"
            f"cd '{work}' && xrun -elaborate -f design.scr "
            "-top top_earlgrey -top tb\n"
            "TOOL: xrun(64) 26.03-s001\n"
            "file: src/top_earlgrey.sv\n"
            "\tmodule worklib.top_earlgrey:sv\n"
            "\tinterface worklib.chip_if:sv\n",
        )

        result = parse_compile_log(str(log), "xcelium")

        assert result["compile_command"] == (
            "xrun -elaborate -f design.scr -top top_earlgrey -top tb"
        )
        assert result["top_modules"] == ["top_earlgrey", "tb"]
        assert result["filelist_tree"] == {"design.scr": []}
        assert result["files"]["user"] == [
            {
                "path": str(source.resolve()),
                "type": "interface",
                "category": "other",
            }
        ]
        assert result["interfaces"] == ["chip_if"]

    def test_vcs_incremental_command_recovers_direct_sources_and_top(self, tmp_path):
        rtl = tmp_path / "rtl" / "deep uart.sv"
        tb = tmp_path / "tb" / "deep_x_tb.sv"
        _write(rtl, "module deep_uart; endmodule\n")
        _write(tb, "module uart_deep_x_tb; endmodule\n")
        log = tmp_path / "compile.log"
        log.write_text(
            "Chronologic VCS simulator\n"
            f"Command: vcs -full64 -sverilog \\\n  '{rtl}' \\\n  {tb} "
            "-top uart_deep_x_tb -o simv\n"
            "The design hasn't changed and need not be recompiled.\n"
        )

        result = parse_compile_log(str(log), "vcs")

        assert [Path(item["path"]).name for item in result["files"]["user"]] == [
            "deep uart.sv",
            "deep_x_tb.sv",
        ]
        assert result["top_modules"] == ["uart_deep_x_tb"]
        assert result["parse_warnings"] == []

    def test_vcs_incremental_command_expands_f_and_F_with_correct_bases(self, tmp_path):
        work = tmp_path / "work"
        lists = tmp_path / "lists"
        command_relative = work / "rtl" / "command_relative.sv"
        list_relative = lists / "rtl" / "list relative.sv"
        nested_source = lists / "nested" / "nested.sv"
        for path, module in (
            (command_relative, "command_relative"),
            (list_relative, "list_relative"),
            (nested_source, "nested"),
        ):
            _write(path, f"module {module}; endmodule\n")

        _write(lists / "plain.f", "rtl/command_relative.sv\n")
        _write(
            lists / "relative.f",
            "'rtl/list relative.sv'\n-F nested/nested.f\n",
        )
        _write(lists / "nested" / "nested.f", "nested.sv\n")
        log = work / "compile.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            "Command: vcs -f ../lists/plain.f -F ../lists/relative.f -top top\n"
            "The design hasn't changed and need not be recompiled.\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert [item["path"] for item in result["files"]["user"]] == [
            str(command_relative.resolve()),
            str(list_relative.resolve()),
            str(nested_source.resolve()),
        ]
        assert result["filelist_tree"] == {
            "plain.f": [],
            "relative.f": ["nested.f"],
            "nested.f": [],
        }

    def test_vcs_incremental_filelist_cycle_is_bounded(self, tmp_path):
        rtl = tmp_path / "rtl" / "dut.sv"
        _write(rtl, "module dut; endmodule\n")
        _write(tmp_path / "a.f", "-F b.f\nrtl/dut.sv\n")
        _write(tmp_path / "b.f", "-F a.f\n")
        log = tmp_path / "compile.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            "Command: vcs -F a.f -top dut\n"
            "The design hasn't changed and need not be recompiled.\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert [Path(item["path"]).name for item in result["files"]["user"]] == [
            "dut.sv"
        ]
        assert any("filelist cycle" in warning for warning in result["parse_warnings"])

    def test_vcs_incremental_command_reports_missing_sources_without_shell_execution(
        self, tmp_path
    ):
        marker = tmp_path / "must_not_exist"
        log = tmp_path / "compile.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            f"Command: vcs missing.sv '$(touch {marker}).sv' -top top\n"
            "The design hasn't changed and need not be recompiled.\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert result["files"]["user"] == []
        assert result["parse_warnings"]
        assert marker.exists() is False

    def test_vcs_parsing_records_remain_authoritative_when_command_has_sources(
        self, tmp_path
    ):
        parsed = tmp_path / "parsed.sv"
        command_only = tmp_path / "command_only.sv"
        _write(parsed, "module parsed; endmodule\n")
        _write(command_only, "module command_only; endmodule\n")
        log = tmp_path / "compile.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            f"Command: vcs {command_only} -top parsed\n"
            f"Parsing design file '{parsed}'\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert [item["path"] for item in result["files"]["user"]] == [
            str(parsed.resolve())
        ]
        assert result["top_modules"] == ["parsed"]
