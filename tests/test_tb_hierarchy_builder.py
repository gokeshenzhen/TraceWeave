import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.compile_log_parser import parse_compile_log
from src.tb_hierarchy_builder import build_hierarchy


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _make_project():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    _write(root / "dut" / "dut.sv", "module dut; endmodule\n")
    _write(root / "tb" / "my_if.sv", "interface my_if; endinterface\n")
    _write(
        root / "tb" / "top_tb.sv",
        """
interface bus_if; endinterface
module top_tb;
  dut dut_i();
endmodule
""",
    )
    _write(
        root / "tb" / "my_agent.sv",
        """
class my_driver extends uvm_driver; endclass
class my_monitor extends uvm_monitor; endclass
class my_agent extends uvm_agent;
  function void build_phase(uvm_phase phase);
    drv = my_driver::type_id::create("drv", this);
    mon = my_monitor::type_id::create("mon", this);
  endfunction
endclass
""",
    )
    _write(
        root / "tb" / "my_env.sv",
        """
class my_env extends uvm_env;
  virtual my_if vif;
  function void build_phase(uvm_phase phase);
    agt = my_agent::type_id::create("agt", this);
  endfunction
endclass
""",
    )
    _write(
        root / "tb" / "base_test.sv",
        "class base_test extends uvm_test; endclass\n",
    )
    _write(
        root / "tb" / "my_case0.sv",
        """
class my_case0 extends base_test;
  function void build_phase(uvm_phase phase);
    env = my_env::type_id::create("env", this);
  endfunction
endclass
""",
    )
    return tmp, root


class TestBuildHierarchy:
    def test_full_pipeline(self):
        tmp, root = _make_project()
        try:
            log = root / "comp.log"
            log.write_text(
                f"""Command: vcs -f {root / 'dut' / 'filelist.f'} +incdir+{root / 'tb'}
Parsing design file '{root / 'tb' / 'my_if.sv'}'
Parsing design file '{root / 'tb' / 'my_agent.sv'}'
Parsing design file '{root / 'tb' / 'my_env.sv'}'
Parsing design file '{root / 'tb' / 'base_test.sv'}'
Parsing design file '{root / 'tb' / 'my_case0.sv'}'
Parsing design file '{root / 'tb' / 'top_tb.sv'}'
Parsing design file '{root / 'dut' / 'dut.sv'}'
Top Level Modules:
       top_tb
"""
            )
            compile_result = parse_compile_log(str(log), "vcs")
            hierarchy = build_hierarchy(compile_result)

            assert hierarchy["project"]["top_module"] == "top_tb"
            assert "my_case0 -> base_test -> uvm_test" in hierarchy["class_hierarchy"]
            assert hierarchy["component_tree"]["top_tb"]["dut_i"]["class"] == "dut"
            assert hierarchy["component_tree"]["uvm_test_top"]["env"]["class"] == "my_env"
            assert hierarchy["component_tree"]["uvm_test_top"]["env"]["children"]["agt"]["class"] == "my_agent"

            tb_files = {item["name"] for item in hierarchy["files"]["tb"]}
            assert {"top_tb.sv", "my_agent.sv", "my_env.sv", "base_test.sv", "my_case0.sv"} <= tb_files

            interfaces = {item["name"]: item for item in hierarchy["interfaces"]}
            assert "my_if" in interfaces
        finally:
            tmp.cleanup()
