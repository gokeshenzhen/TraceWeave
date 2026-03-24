import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import schemas
from src.signal_driver import (
    _find_input_port,
    _find_output_port,
    explain_signal_driver,
)


def _mock_compile(monkeypatch, files, top_module="top_tb"):
    def fake_parse_compile_log(log_path, simulator="auto"):
        return {
            "top_modules": [top_module],
            "files": {
                "user": [{"path": str(path), "type": "module", "category": "rtl"} for path in files],
            },
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)


def test_single_hop_backward_compat(monkeypatch, tmp_path):
    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign K_sub = a ^ b;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.K_sub",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
    )

    assert result["driver_status"] == "resolved"
    assert result["driver_kind"] == "assign"
    assert result["stopped_at"] is None
    assert result["recursive"] is False
    assert result["driver_chain"] is None


def test_single_hop_stopped_at_port(monkeypatch, tmp_path):
    rtl = tmp_path / "dut.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module leaf(output logic [3:0] dout);
endmodule

module dut;
  logic [7:0] s;
  leaf u_a(.dout(s[3:0]));
  leaf u_b(.dout(s[7:4]));
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.s",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
    )

    assert result["driver_kind"] == "instance_ports"
    assert result["stopped_at"] == "port_boundary"


def test_single_hop_input_port_primary_input(monkeypatch, tmp_path):
    rtl = tmp_path / "top_tb.sv"
    rtl.write_text(
        """\
module top_tb(input logic clk);
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.clk",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
    )

    assert result["driver_kind"] == "input_port"
    assert result["stopped_at"] == "primary_input"


def test_recursive_upward_traversal_to_register(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  core u0();
endmodule

module core;
  logic clk;
  logic data_in;
  logic some_reg;
  logic result;
  alu u_alu(.a(data_in), .b(data_in), .result(result));
  always_ff @(posedge clk) begin
    data_in <= some_reg;
  end
endmodule

module alu;
  input logic a, b;
  output logic result;
  logic sum;
  assign sum = a + b;
  assign result = sum;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.u_alu.result",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=5,
    )

    assert result["recursive"] is True
    assert result["stopped_at"] == "trace_boundary"
    assert [hop["driver_kind"] for hop in result["driver_chain"]] == ["assign", "assign"]
    assert result["driver_chain"][1]["branch_candidates"] == ["a", "b"]
    assert "ambiguous_rhs_not_traced" in result["driver_chain"][1]["expression_summary"]
    schemas.ExplainDriverResult.model_validate(result)


def test_recursive_stops_at_primary_input(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb(input logic ext_in);
  core u0(.data_in(ext_in));
endmodule

module core(input logic data_in);
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.data_in",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "primary_input"
    assert [hop["signal_path"] for hop in result["driver_chain"]] == [
        "top_tb.u0.data_in",
        "top_tb.ext_in",
    ]
    assert result["driver_chain"][-1]["driver_kind"] == "input_port"


def test_recursive_stops_at_register_boundary(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  core u0();
endmodule

module core;
  logic clk;
  logic data_in;
  logic some_reg;
  alu u_alu(.a(data_in), .result());
  always_ff @(posedge clk) begin
    data_in <= some_reg;
  end
endmodule

module alu;
  input logic a;
  output logic result;
  assign result = a;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.u_alu.result",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=5,
    )

    assert result["stopped_at"] == "register_boundary"
    assert [hop["driver_kind"] for hop in result["driver_chain"]] == [
        "assign", "input_port", "always_ff",
    ]
    assert result["driver_chain"][-1]["stopped_at"] == "register_boundary"


def test_recursive_stops_at_max_depth(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b, c;
  assign a = b;
  assign b = c;
  assign c = 1'b0;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.a",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=1,
    )

    assert result["stopped_at"] == "max_depth"
    assert len(result["driver_chain"]) == 2
    assert result["driver_chain"][-1]["stopped_at"] == "max_depth"


def test_recursive_stops_at_unresolved(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign a = b;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.a",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "unresolved"
    assert result["driver_chain"][-1]["signal_path"] == "top_tb.u0.b"
    assert result["driver_chain"][-1]["stopped_at"] == "unresolved"


def test_hierarchical_rhs_stops_at_trace_boundary(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module leaf(output logic y);
endmodule

module dut;
  logic x;
  leaf u_leaf();
  assign x = u_leaf.y;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.x",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "trace_boundary"
    assert len(result["driver_chain"]) == 1
    assert result["driver_chain"][0]["signal_path"] == "top_tb.u0.x"
    assert "hierarchical_rhs_not_traced" in result["driver_chain"][0]["expression_summary"]
    assert "top_tb.u0.u_leaf" not in str(result["driver_chain"])


def test_uppercase_signal_names_supported(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb(input logic DATA_IN);
  logic OUT;
  assign OUT = DATA_IN;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    top_result = explain_signal_driver(
        signal_path="top_tb.DATA_IN",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
    )
    out_result = explain_signal_driver(
        signal_path="top_tb.OUT",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=3,
    )

    assert top_result["driver_kind"] == "input_port"
    assert out_result["upstream_signals"] == ["DATA_IN"]
    assert out_result["stopped_at"] == "primary_input"


def test_recursive_cycle_detected(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b;
  assign a = b;
  assign b = a;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.a",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=5,
    )

    assert result["stopped_at"] == "cycle_detected"
    assert result["driver_chain"][-1]["signal_path"] == "top_tb.u0.b"
    assert result["driver_chain"][-1]["stopped_at"] == "cycle_detected"
    assert "cycle_detected" in result["driver_chain"][-1]["expression_summary"]


def test_input_port_stops_at_port_boundary_when_parent_not_traversable(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  core u0(.data_in(ext_in));
  logic ext_in;
endmodule

module core(input logic data_in);
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    monkeypatch.setattr("src.signal_driver._traverse_upward", lambda *args, **kwargs: None)

    result = explain_signal_driver(
        signal_path="top_tb.u0.data_in",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "port_boundary"
    assert len(result["driver_chain"]) == 1
    assert result["driver_chain"][0]["driver_kind"] == "input_port"
    assert result["driver_chain"][0]["stopped_at"] == "port_boundary"


def test_max_depth_distinct_from_cycle_detected(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b, c;
  assign a = b;
  assign b = c;
  assign c = b;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    max_depth_result = explain_signal_driver(
        signal_path="top_tb.u0.a",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=1,
    )
    cycle_result = explain_signal_driver(
        signal_path="top_tb.u0.a",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=5,
    )

    assert max_depth_result["stopped_at"] == "max_depth"
    assert cycle_result["stopped_at"] == "cycle_detected"


def test_ambiguous_rhs_stops_at_trace_boundary(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module leaf(output logic y);
endmodule

module dut;
  logic sel, b, x;
  leaf u1();
  assign x = sel ? u1.y : b;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.x",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "trace_boundary"
    assert "ambiguous_rhs_not_traced" in result["driver_chain"][0]["expression_summary"]


def test_multiple_local_refs_stop_at_trace_boundary(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b, x;
  assign x = a + b;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.x",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "trace_boundary"
    assert len(result["driver_chain"]) == 1
    assert result["driver_chain"][0]["branch_candidates"] == ["a", "b"]
    assert "ambiguous_rhs_not_traced" in result["driver_chain"][0]["expression_summary"]


def test_local_ternary_stops_at_trace_boundary(monkeypatch, tmp_path):
    rtl = tmp_path / "design.sv"
    rtl.write_text(
        """\
module top_tb;
  dut u0();
endmodule

module dut;
  logic a, b, c, x;
  assign x = a ? b : c;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])

    result = explain_signal_driver(
        signal_path="top_tb.u0.x",
        wave_path=str(tmp_path / "wave.vcd"),
        compile_log=str(tmp_path / "compile.log"),
        top_hint="top_tb",
        recursive=True,
        max_depth=4,
    )

    assert result["stopped_at"] == "trace_boundary"
    assert result["driver_chain"][0]["branch_candidates"] == ["a", "b", "c"]
    assert "ambiguous_rhs_not_traced" in result["driver_chain"][0]["expression_summary"]


def test_port_decl_comma_separated():
    scan = {
        "source_text": """\
module dut(
  input logic [7:0] a, b,
  output logic [7:0] x, y
);
endmodule
""",
    }

    assert _find_input_port(scan, "a") == {"source_line": 2}
    assert _find_input_port(scan, "b") == {"source_line": 2}
    assert _find_output_port(scan, "x") == {"source_line": 3}
    assert _find_output_port(scan, "y") == {"source_line": 3}
