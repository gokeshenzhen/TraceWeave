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

