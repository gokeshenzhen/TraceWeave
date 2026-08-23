import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import tb_hierarchy_builder
from src.compile_log_parser import parse_compile_log
from src.tb_hierarchy_builder import build_hierarchy, scan_sv_file, scan_sv_text


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
    def test_interface_binding_preserves_compile_order_and_first_match(
        self, tmp_path
    ):
        before = tmp_path / "before_definition.sv"
        comment_if = tmp_path / "comment_if.sv"
        direct_if = tmp_path / "direct_if.sv"
        earlier_only_if = tmp_path / "earlier_only_if.sv"
        first_user = tmp_path / "first_user.sv"
        later_user = tmp_path / "later_user.sv"
        _write(
            before,
            "module before_definition; earlier_only_if ignored(); endmodule\n",
        )
        _write(comment_if, "interface comment_if; endinterface\n")
        _write(direct_if, "interface direct_if; endinterface\n")
        _write(
            earlier_only_if,
            "interface earlier_only_if; endinterface\n",
        )
        _write(
            first_user,
            "// comment_if remains a historical raw-text reference\n"
            "module first_user; direct_if active(); endmodule\n",
        )
        _write(
            later_user,
            "module later_user;\n"
            "  comment_if later_comment();\n"
            "  direct_if later_direct();\n"
            "endmodule\n",
        )
        files = [
            before,
            comment_if,
            direct_if,
            earlier_only_if,
            first_user,
            later_user,
        ]
        compile_result = {
            "files": {
                "user": [
                    {"path": str(path), "category": "rtl", "type": "module"}
                    for path in files
                ]
            },
            "primary_top": "first_user",
            "top_modules": ["first_user"],
            "interfaces": [],
            "simulator": "vcs",
            "compile_command": "vcs -sverilog "
            + " ".join(str(path) for path in files),
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)
        interfaces = {item["name"]: item for item in hierarchy["interfaces"]}

        assert interfaces["comment_if"]["bound_in"] == "first_user.sv"
        assert interfaces["direct_if"]["bound_in"] == "first_user.sv"
        assert interfaces["earlier_only_if"]["bound_in"] == ""

    def test_interface_reference_scan_is_bounded_by_name_chunks(
        self, monkeypatch
    ):
        interface_names = [f"bus_if_{index:04d}" for index in range(600)]
        source_text = " ".join(interface_names)
        compile_calls = 0
        original = tb_hierarchy_builder.re.compile

        def counted(pattern: str, *args, **kwargs):
            nonlocal compile_calls
            compile_calls += 1
            return original(pattern, *args, **kwargs)

        monkeypatch.setattr(tb_hierarchy_builder.re, "compile", counted)

        found = tb_hierarchy_builder._find_interface_references(
            source_text,
            interface_names,
        )

        assert found == set(interface_names)
        assert compile_calls == 3

    @pytest.mark.parametrize("simulator", ["vcs", "xcelium"])
    def test_module_body_include_fragment_builds_hierarchy_for_both_simulators(
        self, tmp_path, simulator
    ):
        wrapper = tmp_path / "rtl" / "wrapper.sv"
        top = tmp_path / "rtl" / "top.sv"
        include_dir = tmp_path / "include"
        active_fragment = include_dir / f"{simulator}_connect.svh"
        nested_fragment = include_dir / "nested_connect.svh"
        _write(
            wrapper,
            "module wrapper; leaf u_leaf(); endmodule\n"
            "module leaf; logic value; endmodule\n"
            "module helper; endmodule\n",
        )
        _write(
            top,
            "module tb_top;\n"
            "`ifdef VCS\n"
            "  `include \"vcs_connect.svh\"\n"
            "`elsif XCELIUM\n"
            "  `include \"xcelium_connect.svh\"\n"
            "`endif\n"
            "  helper u_helper();\n"
            "endmodule\n",
        )
        _write(active_fragment, '`include "nested_connect.svh"\n')
        _write(nested_fragment, "wrapper u_dut();\n")

        log = tmp_path / ("comp.log" if simulator == "vcs" else "elab.log")
        if simulator == "vcs":
            _write(
                log,
                "Chronologic VCS simulator\n"
                f"Command: vcs -sverilog +define+VCS +incdir+{include_dir} "
                f"{wrapper} {top} -top tb_top\n"
                f"Parsing design file '{wrapper}'\n"
                f"Parsing design file '{top}'\n"
                f"Parsing included file '{active_fragment}'.\n"
                f"Parsing included file '{nested_fragment}'.\n"
                f"Back to file '{active_fragment}'.\n"
                f"Back to file '{top}'.\n"
                "Top Level Modules:\n"
                "       tb_top\n",
            )
        else:
            filelist = tmp_path / "run.f"
            _write(
                filelist,
                "+define+XCELIUM\n"
                f"-incdir {include_dir}\n"
                f"{wrapper}\n"
                f"{top}\n",
            )
            _write(
                log,
                "xrun\n"
                "\t-sv\n"
                f"\t-f {filelist}\n"
                "\t\t+define+XCELIUM\n"
                f"\t\t-incdir {include_dir}\n"
                f"\t\t{wrapper}\n"
                f"\t\t{top}\n"
                "\t-top tb_top\n"
                f"file: {wrapper}\n"
                "\tmodule worklib.wrapper:sv\n"
                "\tmodule worklib.leaf:sv\n"
                "\tmodule worklib.helper:sv\n"
                f"file: {top}\n"
                "\tmodule worklib.tb_top:sv\n",
            )

        compile_result = parse_compile_log(str(log), simulator)
        if simulator == "xcelium":
            # A normal elab.log has compilation-unit evidence but no include
            # parent/child records. The hierarchy builder must not depend on
            # xcelium.d/.incl* sidecars.
            assert compile_result["include_tree"] == {}
            assert compile_result["compile_evidence"]["ordered_includes"] == []

        hierarchy = build_hierarchy(
            compile_result,
            str(log),
            apply_source_overlay=False,
        )
        tree = hierarchy["component_tree"]["tb_top"]
        assert set(tree) == {"u_dut", "u_helper"}
        assert tree["u_dut"]["class"] == "wrapper"
        assert set(tree["u_dut"]["children"]) == {"u_leaf"}
        assert tree["u_dut"]["children"]["u_leaf"]["class"] == "leaf"
        assert "children" not in tree["u_helper"]
        assert hierarchy["build_metrics"]["include_resolution_issue_count"] == 0
        enriched = hierarchy["compile_result"]
        assert enriched["include_tree"][str(top.resolve())] == [
            str(active_fragment.resolve())
        ]
        assert enriched["include_tree"][str(active_fragment.resolve())] == [
            str(nested_fragment.resolve())
        ]

    def test_incomplete_late_include_keeps_only_proved_structural_nodes(
        self, tmp_path
    ):
        top = tmp_path / "top.sv"
        definitions = tmp_path / "definitions.sv"
        connections = tmp_path / "connect.svh"
        _write(
            top,
            "module tb_top;\n"
            '  `include "connect.svh"\n'
            '  `include "missing.svh"\n'
            "endmodule\n",
        )
        _write(
            connections,
            "`ifdef FPGA_BUILD\n"
            "  fpga_stub u_fpga_stub();\n"
            "`else\n"
            "  dut u_dut();\n"
            "  bus_if bus();\n"
            "  unknown_helper u_false_positive();\n"
            "`endif\n",
        )
        # Definitions intentionally follow the top in compile order.  Tree
        # admission must use the global scanned definition set, not only the
        # definitions seen before a candidate.
        _write(
            definitions,
            "module dut; endmodule\n"
            "module fpga_stub; endmodule\n"
            "interface bus_if; endinterface\n",
        )
        sources = [top, definitions]
        compile_result = {
            "files": {
                "user": [
                    {
                        "path": str(path),
                        "category": "rtl",
                        "type": "module",
                    }
                    for path in sources
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": "vcs",
            "compile_command": (
                f"vcs -sverilog +incdir+{tmp_path} "
                + " ".join(str(path) for path in sources)
                + " -top tb_top"
            ),
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(
            compile_result,
            apply_source_overlay=False,
        )

        tree = hierarchy["component_tree"]["tb_top"]
        assert set(tree) == {"u_dut", "bus"}
        assert tree["u_dut"]["class"] == "dut"
        assert tree["bus"]["type"] == "interface"
        assert "u_fpga_stub" not in tree
        assert "u_false_positive" not in tree
        assert hierarchy["build_metrics"]["include_context_complete"] is False
        assert hierarchy["build_metrics"][
            "include_resolution_issue_categories"
        ] == ["include_path_unresolved"]
        top_scan = next(
            scan
            for scan in hierarchy["_scan_results"]
            if scan["path"] == str(top)
        )
        assert top_scan["hierarchy_evidence_status"] == "positive_local"
        # Keep raw lexical candidates for diagnostics even though the proof
        # boundary filters the unknown type from the component tree.
        assert {
            item["instance_name"]
            for item in top_scan["module_instance_map"]["tb_top"]
        } == {"u_dut", "bus", "u_false_positive"}

    def test_incomplete_early_include_does_not_trust_later_instances(
        self, tmp_path
    ):
        top = tmp_path / "top.sv"
        definition = tmp_path / "dut.sv"
        connections = tmp_path / "connect.svh"
        _write(
            top,
            "module tb_top;\n"
            '  `include "missing.svh"\n'
            '  `include "connect.svh"\n'
            "endmodule\n",
        )
        _write(connections, "dut u_dut();\n")
        _write(definition, "module dut; endmodule\n")
        sources = [top, definition]
        compile_result = {
            "files": {
                "user": [
                    {
                        "path": str(path),
                        "category": "rtl",
                        "type": "module",
                    }
                    for path in sources
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": "vcs",
            "compile_command": (
                f"vcs -sverilog +incdir+{tmp_path} "
                + " ".join(str(path) for path in sources)
                + " -top tb_top"
            ),
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(
            compile_result,
            apply_source_overlay=False,
        )

        assert hierarchy["component_tree"] == {}
        top_scan = next(
            scan
            for scan in hierarchy["_scan_results"]
            if scan["path"] == str(top)
        )
        assert top_scan["module_instance_map"].get("tb_top", []) == []
        assert top_scan["hierarchy_evidence_status"] == "positive_local"

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

    def test_build_hierarchy_reads_each_source_once(self):
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
            open_counts = Counter()
            real_open = open

            def counting_open(path, *args, **kwargs):
                if str(path).endswith((".sv", ".svh", ".v", ".vh")):
                    open_counts[str(Path(path).resolve())] += 1
                return real_open(path, *args, **kwargs)

            with patch("builtins.open", side_effect=counting_open):
                build_hierarchy(compile_result)

            source_paths = [entry["path"] for entry in compile_result["files"]["user"]]
            assert source_paths
            for path in source_paths:
                assert open_counts[path] == 1
        finally:
            tmp.cleanup()

    def test_full_hierarchy_retains_compact_scan_metadata_not_source_text(self):
        tmp, root = _make_project()
        try:
            log = root / "comp.log"
            log.write_text(
                f"Parsing design file '{root / 'tb' / 'my_if.sv'}'\n"
                f"Parsing design file '{root / 'tb' / 'my_env.sv'}'\n"
                f"Parsing design file '{root / 'tb' / 'top_tb.sv'}'\n"
                "Top Level Modules:\n"
                "       top_tb\n"
            )
            hierarchy = build_hierarchy(parse_compile_log(str(log), "vcs"))

            scans = hierarchy["_scan_results"]
            assert scans
            assert all("source_text" not in scan for scan in scans)
            assert all("has_uvm_import" in scan for scan in scans)
            metrics = hierarchy["build_metrics"]
            assert metrics["source_text_bytes_retained"] == 0
            assert metrics["source_bytes_scanned"] > 0
            assert metrics["source_file_count_scanned"] == len(scans)
        finally:
            tmp.cleanup()

    def test_component_tree_marks_roles_and_filters_pseudo_nodes(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            _write(root / "dut" / "checker.sv", "module checker; endmodule\n")
            _write(
                root / "tb" / "top_tb.sv",
                """
module dut; endmodule
module top_tb;
  dut dut_i();
  checker checker_i();
  if (1) begin end
endmodu1e
""".replace("endmodu1e", "endmodule"),
            )
            log = root / "comp.log"
            log.write_text(
                f"""Parsing design file '{root / 'tb' / 'top_tb.sv'}'
Parsing design file '{root / 'dut' / 'checker.sv'}'
Top Level Modules:
       top_tb
"""
            )
            hierarchy = build_hierarchy(parse_compile_log(str(log), "vcs"))
            top = hierarchy["component_tree"]["top_tb"]
            assert top["dut_i"]["role"] == "dut"
            assert top["checker_i"]["role"] == "helper"
            assert "if" not in top
        finally:
            tmp.cleanup()

    def test_parameterized_multiline_instances_build_nested_hierarchy(self, tmp_path):
        source = tmp_path / "soc.sv"
        _write(
            source,
            """
module rv_core;
endmodule

module soc;
  rv_core #(
    .Width(32)
  ) u_core [2] (
    .clk_i(clk_i)
  );

  always_comb begin
    unique case (state_q)
      1'b0: state_d = 1'b1;
      default: state_d = state_q;
    endcase
  end
endmodule

module tb;
  soc #(
    .Enable(1'b1)
  ) dut (
    .clk_i(clk)
  );
endmodule
""",
        )
        log = tmp_path / "build.log"
        _write(
            log,
            f"Parsing design file '{source}'\n"
            "Top Level Modules:\n"
            "       helper_bind\n"
            "       tb\n",
        )
        compile_result = parse_compile_log(str(log), "vcs")
        compile_result["primary_top"] = "tb"

        scanned = scan_sv_file(str(source))
        hierarchy = build_hierarchy(compile_result)

        assert scanned["module_instances"] == [
            {"module_name": "rv_core", "instance_name": "u_core"},
            {"module_name": "soc", "instance_name": "dut"},
        ]
        assert hierarchy["project"]["top_module"] == "tb"
        dut = hierarchy["component_tree"]["tb"]["dut"]
        assert dut["class"] == "soc"
        assert dut["children"]["u_core"]["class"] == "rv_core"
        assert "case" not in dut["children"]

    def test_common_soc_instance_forms_and_declaration_false_positives(self, tmp_path):
        source = tmp_path / "soc_top.sv"
        _write(
            source,
            """
interface fabric_if #(parameter int Width = 32);
endinterface

module leaf(input logic clk_i);
endmodule

module soc_top(input logic clk_i);
  fabric_if #(.Width(64)) fabric();

  generate
    if (1) begin : gen_enabled
      leaf u_leaf0 (.clk_i(clk_i)),
           u_leaf1 [2] (.clk_i(clk_i));
    end
  endgenerate

  function automatic int helper(input int value);
    return value;
  endfunction
  task automatic tick();
  endtask
  property p_known;
    !$isunknown(clk_i);
  endproperty
  sequence s_tick;
    ##1 clk_i;
  endsequence
  covergroup cg_soc @(posedge clk_i);
  endgroup
  always_comb begin
    priority case (clk_i)
      default: ;
    endcase
  end
endmodule
""",
        )

        scanned = scan_sv_file(str(source))

        assert scanned["module_instances"] == [
            {"module_name": "fabric_if", "instance_name": "fabric"},
            {"module_name": "leaf", "instance_name": "u_leaf0"},
            {"module_name": "leaf", "instance_name": "u_leaf1"},
        ]

    @pytest.mark.parametrize(
        "statement",
        [
            "status = uvm_hdl_read(path, data);",
            "status <= uvm_hdl_deposit(path, data);",
            "status = uvm_hdl_force(path, data);",
            "status = uvm_hdl_release(path);",
            "status = uvm_hdl_check_path(path);",
            "data = uvm_hdl_data_t'(value);",
            "status = helper_func(data);",
            "status <= helper_func(data);",
            "status = helper_func(data) + 1;",
            "status = flag ? helper_func(data) : 0;",
            "status = randomize();",
            "status = $urandom();",
            'status = $value$plusargs("VALUE=%d", data);',
            "mailbox mb = new();",
            "semaphore sem = new(1);",
            'uvm_event ev = new("ev");',
            'uvm_object obj = new("obj");',
            "uvm_hdl_path paths = new();",
            'obj = new("obj");',
            "data = new[16];",
            'uvm_config_db#(virtual vif_t)::set(null, "*", "vif", vif);',
            (
                "status = uvm_config_db#(virtual vif_t)::get("
                'this, "", "vif", vif);'
            ),
            'obj = my_item::type_id::create("obj");',
            "proc = process::self();",
            "status = std::randomize(data);",
            "status = obj.randomize();",
            "void'(uvm_hdl_read(path, data));",
            "if (!uvm_hdl_read(path, data)) status = 0;",
            "assert (obj.randomize());",
            '`uvm_info("ID", "fake_mod fake_inst();", UVM_LOW)',
            'path = "fake_mod fake_inst(.*);";',
            "uvm_component component;",
            "virtual vif_t vif;",
            "-> done_event;",
            "coverage.sample();",
            "queue.push_back(data);",
            "data = queue.pop_front();",
            "phase.raise_objection(this);",
            "seq.start(seqr);",
        ],
    )
    def test_assignment_calls_casts_and_constructors_are_not_instances(
        self, statement
    ):
        scanned = scan_sv_text(
            "uvm_shapes.sv",
            f"""
module real_mod; endmodule
module audit_top;
  real_mod u_real();
  initial begin
    {statement}
  end
endmodule
""",
            retain_source_text=False,
        )

        assert scanned["module_instance_map"]["audit_top"] == [
            {"module_name": "real_mod", "instance_name": "u_real"}
        ]

    @pytest.mark.parametrize(
        ("definition", "invocation", "expected_instance"),
        [
            (
                "`define MAKE_LEAF leaf u_from_object(.*);",
                "`MAKE_LEAF",
                "u_from_object",
            ),
            (
                "`define MAKE_LEAF(NAME) leaf NAME(.*);",
                "`MAKE_LEAF(u_from_function)",
                "u_from_function",
            ),
        ],
    )
    def test_simple_macro_generated_instance_is_recovered(
        self, tmp_path, definition, invocation, expected_instance
    ):
        source = tmp_path / "macro_top.sv"
        _write(
            source,
            f"""{definition}
module leaf(input logic value); endmodule
module tb_top;
  logic value;
  {invocation}
endmodule
""",
        )
        compile_result = {
            "files": {
                "user": [
                    {
                        "path": str(source),
                        "category": "tb",
                        "type": "module",
                    }
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": "xcelium",
            "compile_command": f"xrun -sv {source} -top tb_top",
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)

        child = hierarchy["component_tree"]["tb_top"][expected_instance]
        assert child["class"] == "leaf"

    @pytest.mark.parametrize(
        ("simulator", "simulator_define", "expected_instance"),
        [
            ("vcs", "VCS", "u_vcs"),
            ("xcelium", "XCELIUM", "u_xcelium"),
        ],
    )
    def test_macro_instance_respects_simulator_conditional_branch(
        self, tmp_path, simulator, simulator_define, expected_instance
    ):
        source = tmp_path / "conditional_macro_top.sv"
        _write(
            source,
            "`define MAKE_VCS leaf u_vcs(.*);\n"
            "`define MAKE_XCELIUM leaf u_xcelium(.*);\n"
            "module leaf(input logic value); endmodule\n"
            "module tb_top;\n"
            "  logic value;\n"
            "`ifdef VCS\n"
            "  `MAKE_VCS\n"
            "`elsif XCELIUM\n"
            "  `MAKE_XCELIUM\n"
            "`endif\n"
            "endmodule\n",
        )
        compile_result = {
            "files": {
                "user": [
                    {"path": str(source), "category": "tb", "type": "module"}
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": simulator,
            "compile_command": (
                f"{'vcs' if simulator == 'vcs' else 'xrun'} -sv "
                f"+define+{simulator_define} {source} -top tb_top"
            ),
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)

        assert set(hierarchy["component_tree"]["tb_top"]) == {
            expected_instance
        }
        assert hierarchy["build_metrics"]["include_resolution_issue_count"] == 0

    def test_ifndef_else_undef_and_undefineall_select_only_active_instances(
        self, tmp_path
    ):
        source = tmp_path / "remaining_directives_top.sv"
        _write(
            source,
            "`define LOCAL_TEMP\n"
            "`undef LOCAL_TEMP\n"
            "module leaf; endmodule\n"
            "module tb_top;\n"
            "`ifndef LOCAL_TEMP\n"
            "  leaf u_ifndef();\n"
            "`else\n"
            "  leaf u_inactive_else();\n"
            "`endif\n"
            "`define LOCAL_AGAIN\n"
            "`undefineall\n"
            "`ifdef LOCAL_AGAIN\n"
            "  leaf u_inactive_ifdef();\n"
            "`else\n"
            "  leaf u_else();\n"
            "`endif\n"
            "endmodule\n",
        )
        compile_result = {
            "files": {
                "user": [
                    {"path": str(source), "category": "tb", "type": "module"}
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": "xcelium",
            "compile_command": f"xrun -sv {source} -top tb_top",
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)

        assert set(hierarchy["component_tree"]["tb_top"]) == {
            "u_ifndef",
            "u_else",
        }
        assert hierarchy["build_metrics"]["include_resolution_issue_count"] == 0

    def test_multiline_macro_generated_parameterized_instance_is_recovered(
        self, tmp_path
    ):
        source = tmp_path / "multiline_macro_top.sv"
        _write(
            source,
            "`define MAKE_LEAF(NAME) \\\n"
            "  leaf #(.WIDTH(1)) NAME(.*);\n"
            "module leaf #(parameter int WIDTH = 1)(input logic value); endmodule\n"
            "module tb_top;\n"
            "  logic value;\n"
            "  `MAKE_LEAF(u_multiline)\n"
            "endmodule\n",
        )
        compile_result = {
            "files": {
                "user": [
                    {"path": str(source), "category": "tb", "type": "module"}
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": "xcelium",
            "compile_command": f"xrun -sv {source} -top tb_top",
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)

        child = hierarchy["component_tree"]["tb_top"]["u_multiline"]
        assert child["class"] == "leaf"
        assert hierarchy["build_metrics"]["include_resolution_issue_count"] == 0

    @pytest.mark.parametrize(
        ("macro_source", "expected_issue_count"),
        [
            ("`define UNUSED(NAME) ghost NAME();\n", 0),
            ("`define UNUSED(NAME) \\\n  ghost NAME();\n", 0),
            (
                "`define MAKE_TWO ghost u_bad(); ghost u_also();\n"
                "  `MAKE_TWO\n",
                1,
            ),
            (
                "`define uvm_object_utils(NAME) ghost NAME();\n"
                "  `uvm_object_utils(u_bad)\n",
                0,
            ),
        ],
    )
    def test_uninvoked_compound_and_uvm_macros_do_not_fabricate_hierarchy(
        self, tmp_path, macro_source, expected_issue_count
    ):
        source = tmp_path / "macro_safety_top.sv"
        _write(
            source,
            f"{macro_source}"
            "module ghost; endmodule\n"
            "module real_mod; endmodule\n"
            "module tb_top;\n"
            "  real_mod u_real();\n"
            "endmodule\n",
        )
        compile_result = {
            "files": {
                "user": [
                    {"path": str(source), "category": "tb", "type": "module"}
                ]
            },
            "primary_top": "tb_top",
            "top_modules": ["tb_top"],
            "interfaces": [],
            "simulator": "xcelium",
            "compile_command": f"xrun -sv {source} -top tb_top",
            "compile_cwd": str(tmp_path),
        }

        hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)

        assert set(hierarchy["component_tree"]["tb_top"]) == {"u_real"}
        assert (
            hierarchy["build_metrics"]["include_resolution_issue_count"]
            == expected_issue_count
        )


# ---------------------------------------------------------------------------
# B2: source provenance on component_tree nodes
# ---------------------------------------------------------------------------


class TestB2NodeProvenance:
    """Each compile-log-derived node carries source_file/origin so callers
    can later distinguish from NPI-annotated nodes without inspecting the
    backend separately."""

    def test_compile_log_baseline_tags_every_node(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            top_path = root / "tb" / "top_tb.sv"
            _write(
                top_path,
                """
module dut;
  inner inner_i();
endmodule
module inner; endmodule
module top_tb;
  dut dut_i();
endmodule
""",
            )
            log = root / "comp.log"
            log.write_text(
                f"""Parsing design file '{top_path}'
Top Level Modules:
       top_tb
"""
            )
            hierarchy = build_hierarchy(parse_compile_log(str(log), "vcs"))
            top = hierarchy["component_tree"]["top_tb"]
            assert top["dut_i"]["source_info_origin"] == "compile_log"
            assert top["dut_i"]["source_file"] == str(top_path)
            # source_line stays None in the baseline — NPI is what
            # provides instance-level lines.
            assert top["dut_i"]["source_line"] is None
            # Recursive descent: the grandchild inherits provenance too.
            inner = top["dut_i"]["children"]["inner_i"]
            assert inner["source_info_origin"] == "compile_log"
        finally:
            tmp.cleanup()

    def test_npi_annotation_overwrites_source_file_and_line(self, monkeypatch):
        """When a KDB exists and NPI returns a (file, line) for a node's
        full instance path, the annotation pass overwrites both fields
        and flips origin to "npi"."""
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            top_path = root / "tb" / "top_tb.sv"
            _write(top_path, "module dut; endmodule\nmodule top_tb;\n  dut dut_i();\nendmodule\n")
            log = root / "comp.log"
            log.write_text(
                f"""Parsing design file '{top_path}'
Top Level Modules:
       top_tb
"""
            )
            # Force KDB discovery: probe key only needs kdb_flow != "none".
            monkeypatch.setattr(
                "src.verdi_backend.probe_verdi_backend",
                lambda compile_result, compile_log_path=None: {
                    "kdb_flow": "vcs_two_step",
                    "kdb_path": "/fake/kdb",
                    "actual_backend": "verdi_npi",
                    "backend": "verdi_npi",
                    "simulator": "vcs",
                },
            )

            class _FakeNpiBackend:
                name = "verdi_npi"

                def collect_instance_src_map(self, compile_log, simulator):
                    return {"top_tb.dut_i": ("/elaborated/dut.sv", 137)}

            monkeypatch.setattr(
                "src.connectivity_backend.select_backend",
                lambda status: _FakeNpiBackend(),
            )

            hierarchy = build_hierarchy(
                parse_compile_log(str(log), "vcs"),
                compile_log_path=str(log),
            )
            node = hierarchy["component_tree"]["top_tb"]["dut_i"]
            assert node["source_file"] == "/elaborated/dut.sv"
            assert node["source_line"] == 137
            assert node["source_info_origin"] == "npi"
            assert hierarchy["project"]["source_info_overlay"] == "npi"
            assert hierarchy["project"]["source_info_overlay_reason"] is None
        finally:
            tmp.cleanup()

    def test_degraded_npi_annotation_is_reported_as_partial(self, monkeypatch):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            top_path = root / "top_tb.sv"
            _write(
                top_path,
                "module dut; endmodule\nmodule top_tb; dut dut_i(); endmodule\n",
            )
            log = root / "comp.log"
            log.write_text(
                f"Parsing design file '{top_path}'\nTop Level Modules:\n       top_tb\n"
            )
            monkeypatch.setattr(
                "src.verdi_backend.probe_verdi_backend",
                lambda compile_result, compile_log_path=None: {
                    "kdb_flow": "vcs_two_step",
                    "kdb_path": "/fake/kdb",
                    "kdb_validation_status": "elaboration_error",
                },
            )

            class _DegradedNpiBackend:
                name = "verdi_npi"
                kdb_load_quality = "degraded"

                def collect_instance_src_map(self, compile_log, simulator):
                    return {"top_tb.dut_i": ("/partial/dut.sv", 23)}

            monkeypatch.setattr(
                "src.connectivity_backend.select_backend",
                lambda status: _DegradedNpiBackend(),
            )

            hierarchy = build_hierarchy(
                parse_compile_log(str(log), "vcs"),
                compile_log_path=str(log),
            )

            node = hierarchy["component_tree"]["top_tb"]["dut_i"]
            assert node["source_file"] == "/partial/dut.sv"
            assert node["source_info_origin"] == "npi"
            assert hierarchy["project"]["source_info_overlay"] == "npi_partial"
            assert hierarchy["project"]["source_info_overlay_reason"] == (
                "npi_degraded_kdb"
            )
        finally:
            tmp.cleanup()

    def test_explicit_source_graph_route_skips_npi_annotation(self, monkeypatch):
        """A pure Source Graph run must not probe or construct NPI during its
        hierarchy prerequisite, even when the compile log has a usable KDB."""
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            top_path = root / "tb" / "top_tb.sv"
            _write(top_path, "module dut; endmodule\nmodule top_tb;\n  dut dut_i();\nendmodule\n")
            log = root / "comp.log"
            log.write_text(
                f"""Parsing design file '{top_path}'
Top Level Modules:
       top_tb
"""
            )
            monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")

            def _must_not_probe(*args, **kwargs):
                del args, kwargs
                raise AssertionError("Source Graph route must not probe NPI hierarchy overlay")

            monkeypatch.setattr("src.verdi_backend.probe_verdi_backend", _must_not_probe)

            hierarchy = build_hierarchy(
                parse_compile_log(str(log), "vcs"),
                compile_log_path=str(log),
            )
            node = hierarchy["component_tree"]["top_tb"]["dut_i"]
            assert node["source_info_origin"] == "compile_log"
            assert hierarchy["project"]["source_info_overlay"] == "compile_log"
            assert hierarchy["project"]["source_info_overlay_reason"] == (
                "npi_skipped_by_policy"
            )
        finally:
            tmp.cleanup()

    def test_npi_failure_leaves_compile_log_baseline_intact(self, monkeypatch):
        """If the NPI walk raises, every node must keep its compile_log
        origin — annotation is strictly additive."""
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            top_path = root / "tb" / "top_tb.sv"
            _write(top_path, "module dut; endmodule\nmodule top_tb;\n  dut dut_i();\nendmodule\n")
            log = root / "comp.log"
            log.write_text(
                f"""Parsing design file '{top_path}'
Top Level Modules:
       top_tb
"""
            )
            monkeypatch.setattr(
                "src.verdi_backend.probe_verdi_backend",
                lambda compile_result, compile_log_path=None: {
                    "kdb_flow": "vcs_two_step",
                    "kdb_path": "/fake/kdb",
                    "actual_backend": "verdi_npi",
                    "backend": "verdi_npi",
                    "simulator": "vcs",
                },
            )

            class _ExplodingBackend:
                name = "verdi_npi"

                def collect_instance_src_map(self, compile_log, simulator):
                    raise RuntimeError("npi broke")

            monkeypatch.setattr(
                "src.connectivity_backend.select_backend",
                lambda status: _ExplodingBackend(),
            )

            hierarchy = build_hierarchy(
                parse_compile_log(str(log), "vcs"),
                compile_log_path=str(log),
            )
            node = hierarchy["component_tree"]["top_tb"]["dut_i"]
            assert node["source_info_origin"] == "compile_log"
            assert node["source_file"] == str(top_path)
            assert node["source_line"] is None
        finally:
            tmp.cleanup()

    def test_no_kdb_skips_npi_pass(self, monkeypatch):
        """When kdb_flow is 'none', select_backend is not even called."""
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            top_path = root / "tb" / "top_tb.sv"
            _write(top_path, "module dut; endmodule\nmodule top_tb;\n  dut dut_i();\nendmodule\n")
            log = root / "comp.log"
            log.write_text(
                f"""Parsing design file '{top_path}'
Top Level Modules:
       top_tb
"""
            )
            monkeypatch.setattr(
                "src.verdi_backend.probe_verdi_backend",
                lambda compile_result, compile_log_path=None: {"kdb_flow": "none"},
            )
            called = {"n": 0}

            def _boom(status):
                called["n"] += 1
                raise AssertionError("select_backend must not be invoked when no KDB")

            monkeypatch.setattr("src.connectivity_backend.select_backend", _boom)

            hierarchy = build_hierarchy(
                parse_compile_log(str(log), "vcs"),
                compile_log_path=str(log),
            )
            assert called["n"] == 0
            node = hierarchy["component_tree"]["top_tb"]["dut_i"]
            assert node["source_info_origin"] == "compile_log"
        finally:
            tmp.cleanup()
