import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.compile_log_parser import parse_compile_log
from src.tb_hierarchy_builder import build_hierarchy, scan_sv_file


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
