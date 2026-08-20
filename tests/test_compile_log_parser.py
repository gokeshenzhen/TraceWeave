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
    _write(
        root / "tb" / "my_driver.sv", "class my_driver extends uvm_driver; endclass\n"
    )
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

    def test_detect_vcs_dvsim_wrapper_after_long_setup_preamble(self, tmp_path):
        log = tmp_path / "build.log"
        log.write_text(
            "".join(f"setup line {index}\n" for index in range(450))
            + f"cd {tmp_path} && vcs -full64 -sverilog -top tb\n"
        )

        assert detect_simulator(str(log)) == "vcs"


class TestParseCompileLog:
    def test_parse_vcs_compile_log(self):
        tmp, root = _make_demo_tree()
        try:
            log = root / "comp.log"
            log.write_text(
                f"""Command: vcs -f {root / "dut" / "filelist.f"} +incdir+{root / "tb"} /tools/synopsys/vcs/etc/uvm.sv
Parsing design file '{root / "tb" / "top_tb.sv"}'
Parsing included file 'my_driver.sv'.
Back to file '{root / "tb" / "top_tb.sv"}'.
Parsing design file '{root / "dut" / "dut.sv"}'
Parsing design file '{root / "assertion" / "sva_top.sv"}'
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
            assert (
                str((root / "tb" / "my_driver.sv").resolve())
                in result["include_tree"][top_tb]
            )
            assert "top_tb" in result["top_modules"]
        finally:
            tmp.cleanup()

    def test_parse_xcelium_compile_log(self):
        tmp, root = _make_demo_tree()
        try:
            log = root / "elab.log"
            log.write_text(
                f"""xrun
\t-f {root / "dut" / "filelist.f"}
\t\t{root / "dut" / "dut.sv"}
\t\t{root / "tb" / "top_tb.sv"}
\t-top top_tb
file: {root / "dut" / "dut.sv"}
\tmodule worklib.dut:sv
file: {root / "tb" / "top_tb.sv"}
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
            assert result["compile_replay_command"] == (
                f"xrun -f {root / 'dut' / 'filelist.f'} -top top_tb"
            )
        finally:
            tmp.cleanup()

    def test_vcs_compile_evidence_separates_units_includes_and_tool_roles(
        self, tmp_path
    ):
        first = tmp_path / "rtl" / "z_pkg.sv"
        second = tmp_path / "rtl" / "a_top.sv"
        include = tmp_path / "include" / "defs.svh"
        for path, text in (
            (first, "package z_pkg; endpackage\n"),
            (second, "module a_top; endmodule\n"),
            (include, "`define WIDTH 8\n"),
        ):
            _write(path, text)
        uvm_pkg = "/tools/synopsys/vcs/etc/uvm-1.2/uvm_pkg.sv"
        recorder = (
            "/tools/synopsys/vcs/etc/uvm-1.2/vcs/uvm_custom_install_vcs_recorder.sv"
        )
        log = tmp_path / "compile.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            f"Command: vcs {first} {second} -top a_top\n"
            f"Parsing design file '{uvm_pkg}'\n"
            f"Parsing design file '{recorder}'\n"
            f"Parsing design file '{first}'\n"
            f"Parsing included file '{include}'.\n"
            f"Back to file '{first}'.\n"
            f"Parsing design file '{second}'\n",
        )

        result = parse_compile_log(str(log), "vcs")
        evidence = result["compile_evidence"]

        assert evidence["unit_order_source"] == "simulator_log"
        assert [item["path"] for item in evidence["ordered_compilation_units"]] == [
            str(Path(uvm_pkg).resolve()),
            str(Path(recorder).resolve()),
            str(first.resolve()),
            str(second.resolve()),
        ]
        assert [item["role"] for item in evidence["ordered_compilation_units"]] == [
            "simulator_library",
            "simulator_instrumentation",
            "project",
            "project",
        ]
        assert evidence["ordered_includes"] == [
            {"parent": str(first.resolve()), "path": str(include.resolve())}
        ]
        assert str(include.resolve()) not in {
            item["path"] for item in evidence["ordered_compilation_units"]
        }

    def test_xcelium_compile_evidence_flattens_nested_filelists_without_refeed(
        self, tmp_path
    ):
        outer = tmp_path / "lists" / "outer.f"
        nested = tmp_path / "lists" / "nested.f"
        z_pkg = tmp_path / "rtl" / "z_pkg.sv"
        a_top = tmp_path / "rtl" / "a_top.sv"
        include_dir = tmp_path / "include"
        for path, text in (
            (outer, "unused after xrun expansion\n"),
            (nested, "unused after xrun expansion\n"),
            (z_pkg, "package z_pkg; endpackage\n"),
            (a_top, "module a_top; endmodule\n"),
        ):
            _write(path, text)
        include_dir.mkdir()
        log = tmp_path / "elab.log"
        _write(
            log,
            "xrun\n"
            "\t-sv\n"
            f"\t-f {outer}\n"
            f"\t\t+incdir+{include_dir}\n"
            f"\t\t{z_pkg}\n"
            f"\t\t-f {nested}\n"
            f"\t\t\t{a_top}\n"
            "\t-top a_top\n"
            f"file: {z_pkg}\n"
            "\tpackage worklib.z_pkg:sv\n"
            f"file: {a_top}\n"
            "\tmodule worklib.a_top:sv\n",
        )

        result = parse_compile_log(str(log), "xcelium")
        evidence = result["compile_evidence"]

        assert evidence["unit_order_source"] == "simulator_log"
        assert [item["path"] for item in evidence["ordered_compilation_units"]] == [
            str(z_pkg.resolve()),
            str(a_top.resolve()),
        ]
        assert " -f " not in f" {evidence['expanded_replay_command']} "
        assert f"+incdir+{include_dir}" in evidence["expanded_replay_command"]
        assert [item["path"] for item in evidence["filelists"]] == [
            str(outer.resolve()),
            str(nested.resolve()),
        ]
        assert evidence["filelists"][1]["parent"] == str(outer.resolve())
        assert evidence["filelists"][1]["depth"] == 2

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
        assert result["compile_replay_command"] == result["compile_command"]
        assert result["compile_cwd"] == str(tmp_path)
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
            "-top top_earlgrey -top tb -snapshot tb\n"
            "TOOL: xrun(64) 26.03-s001\n"
            "file: src/top_earlgrey.sv\n"
            "\tmodule worklib.top_earlgrey:sv\n"
            "\tinterface worklib.chip_if:sv\n",
        )

        result = parse_compile_log(str(log), "xcelium")

        assert result["compile_command"] == (
            "xrun -elaborate -f design.scr -top top_earlgrey -top tb -snapshot tb"
        )
        assert result["compile_replay_command"] == result["compile_command"]
        assert result["compile_cwd"] == str(work.resolve())
        assert result["top_modules"] == ["tb", "top_earlgrey"]
        assert result["filelist_tree"] == {"design.scr": []}
        assert result["files"]["user"] == [
            {
                "path": str(source.resolve()),
                "type": "interface",
                "category": "other",
            }
        ]
        assert result["interfaces"] == ["chip_if"]

    def test_vcs_wrapper_command_anchors_relative_files_to_vcs_cwd(self, tmp_path):
        work = tmp_path / "fusesoc work"
        source = work / "src" / "dut.sv"
        include = work / "src" / "includes" / "common.svh"
        filelist = work / "design.scr"
        _write(source, 'module dut; `include "common.svh" endmodule\n')
        _write(include, "localparam int CommonValue = 1;\n")
        _write(
            filelist,
            "+incdir+src/includes\nsrc/dut.sv\n",
        )
        log = tmp_path / "default" / "build.log"
        _write(
            log,
            "[make]: build\n"
            f"cd '{work}' && /tools/synopsys/vcs/bin/vcs "
            "-f design.scr -top helper_bind -top tb\n"
            "Chronologic VCS simulator\n"
            "Parsing design file 'src/dut.sv'\n"
            "Parsing included file 'common.svh'.\n"
            "Back to file 'src/dut.sv'.\n"
            "Top Level Modules:\n"
            "       uvm_custom_install_verdi_recording\n"
            "       helper_bind\n"
            "       tb\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert result["compile_command"] == (
            "vcs -f design.scr -top helper_bind -top tb"
        )
        assert result["compile_cwd"] == str(work.resolve())
        assert result["primary_top"] == "tb"
        assert result["top_modules"] == ["tb", "helper_bind"]
        assert result["reported_top_modules"] == [
            "uvm_custom_install_verdi_recording",
            "helper_bind",
            "tb",
        ]
        assert result["files"]["user"] == [
            {
                "path": str(source.resolve()),
                "type": "module",
                "category": "rtl",
            },
            {
                "path": str(include.resolve()),
                "type": "unknown",
                "category": "other",
            },
        ]
        assert result["include_tree"] == {
            str(source.resolve()): [str(include.resolve())]
        }
        assert result["filelist_tree"] == {"design.scr": []}
        assert result["parse_warnings"] == []

    def test_vcs_multi_top_prefers_single_conventional_tb_name(self, tmp_path):
        source = tmp_path / "chip_tb.sv"
        _write(source, "module chip_tb; endmodule\n")
        log = tmp_path / "build.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            f"Command: vcs {source} -top reset_bind -top chip_tb\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert result["primary_top"] == "chip_tb"
        assert result["top_modules"] == ["chip_tb", "reset_bind"]

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
        assert result["compile_cwd"] == str(tmp_path)
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

    def test_vcs_incremental_filelist_ignores_c_style_comments(self, tmp_path):
        live_library = tmp_path / "live_mem.v"
        top = tmp_path / "top.sv"
        old_line_library = tmp_path / "old_line_mem.v"
        old_block_library = tmp_path / "old_block_mem.v"
        for path in (live_library, top, old_line_library, old_block_library):
            _write(path, "module placeholder; endmodule\n")
        _write(
            tmp_path / "design.f",
            "// -v old_line_mem.v\n"
            "/* retired library:\n"
            "-v old_block_mem.v\n"
            "*/\n"
            "+define+DOC_URL='http://intranet/spec'\n"
            "+define+TEXT='a/*literal*/b'\n"
            "-v live_mem.v \\\n"
            "top.sv // active top\n",
        )
        log = tmp_path / "compile.log"
        _write(
            log,
            "Chronologic VCS simulator\n"
            "Command: vcs -F design.f -top top\n"
            "The design hasn't changed and need not be recompiled.\n",
        )

        result = parse_compile_log(str(log), "vcs")

        assert [item["path"] for item in result["files"]["user"]] == [
            str(live_library.resolve()),
            str(top.resolve()),
        ]
        assert result["parse_warnings"] == []

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
