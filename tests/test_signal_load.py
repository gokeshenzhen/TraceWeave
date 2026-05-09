import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import schemas
from src.signal_load import find_signal_loads


def _mock_compile(monkeypatch, files, top_module="top_tb"):
    def fake_parse_compile_log(log_path, simulator="auto"):
        return {
            "top_modules": [top_module],
            "files": {
                "user": [
                    {"path": str(path), "type": "module", "category": "rtl"}
                    for path in files
                ],
            },
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)


def test_module_input_load(monkeypatch, tmp_path):
    rtl = tmp_path / "top_tb.sv"
    rtl.write_text(
        """\
module top_tb;
  reg clk;
  dut u_dut(.clk(clk));
endmodule

module dut(input clk);
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.clk", compile_log="x")
    kinds = [ld["kind"] for ld in r["loads"]]
    assert "module_input" in kinds
    hit = next(ld for ld in r["loads"] if ld["kind"] == "module_input")
    assert hit["load_path"] == "top_tb.u_dut.clk"
    assert hit["confidence"] == "approximate"
    assert hit["backend"] == "static"
    assert r["completeness"] == "shallow_only"


def test_rhs_expr_load_in_assign(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic a, b, c;
  assign c = a & b;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.a", compile_log="x")
    rhs = [ld for ld in r["loads"] if ld["kind"] == "rhs_expr"]
    assert any(ld["load_path"] == "top_tb.u0.c" for ld in rhs)
    assert all(ld["backend"] == "static" for ld in rhs)


def test_rhs_expr_load_in_always_block(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic clk, en;
  reg out;
  always @(posedge clk) begin
    out <= en;
  end
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.en", compile_log="x")
    rhs = [ld for ld in r["loads"] if ld["kind"] == "rhs_expr"]
    assert any(ld["load_path"] == "top_tb.u0.out" for ld in rhs)


def test_always_sensitivity_load(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic clk;
  reg q;
  always @(posedge clk) begin
    q <= 1'b0;
  end
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.clk", compile_log="x")
    sens = [ld for ld in r["loads"] if ld["kind"] == "always_sensitivity"]
    assert sens, f"expected at least one sensitivity hit, got {r['loads']}"
    assert "always" in sens[0]["expr"]


def test_top_selection_picks_matching_signal_root(monkeypatch, tmp_path):
    """Compile logs may list multiple top modules (UVM helpers + real
    testbench). Resolution must pick the top whose name matches the
    signal_path root, not blindly take ``top_modules[0]``."""

    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module uvm_recording_helper;
endmodule

module dut_top;
  child u0();
endmodule

module child;
  logic a, b;
  assign b = a;
endmodule
"""
    )

    def fake_parse_compile_log(log_path, simulator="auto"):
        return {
            "top_modules": ["uvm_recording_helper", "dut_top"],
            "files": {
                "user": [
                    {"path": str(rtl), "type": "module", "category": "rtl"},
                ],
            },
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)
    r = find_signal_loads(signal_path="dut_top.u0.a", compile_log="x")
    assert r["resolved_module"] == "child"
    assert any(ld["load_path"] == "dut_top.u0.b" for ld in r["loads"])


def test_signal_path_too_short(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text("module top_tb; endmodule\n")
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="orphan", compile_log="x")
    assert r["stopped_at"] == "signal_path_unresolved"
    assert r["loads"] == []


def test_output_port_loads_in_parent_scope(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m(output q);
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.q", compile_log="x")
    assert r["stopped_at"] == "output_port_loads_in_parent_scope"
    assert r["loads"] == []


def test_dedup_same_line_multiple_uses(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic a;
  logic b;
  assign b = a + a;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.a", compile_log="x")
    rhs_to_b = [
        ld for ld in r["loads"]
        if ld["kind"] == "rhs_expr" and ld["load_path"] == "top_tb.u0.b"
    ]
    assert len(rhs_to_b) == 1


def test_no_static_load_found_marker(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic dangling;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.dangling", compile_log="x")
    assert r["loads"] == []
    assert r["stopped_at"] == "no_static_load_found"


def test_kind_filter(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic clk, en;
  reg out;
  always @(posedge clk) begin
    out <= en;
  end
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(
        signal_path="top_tb.u0.clk",
        compile_log="x",
        kind_filter=["always_sensitivity"],
    )
    assert all(ld["kind"] == "always_sensitivity" for ld in r["loads"])
    assert r["loads"], "kind_filter should still return sensitivity hits"


def test_schema_validation_round_trip(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic a, b;
  assign b = a;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    r = find_signal_loads(signal_path="top_tb.u0.a", compile_log="x")
    r["backend_status"] = {
        "simulator": "vcs",
        "backend": "static",
        "parser_match": "approximate",
        "kdb_path": None,
        "kdb_flow": "none",
        "kdb_hint": None,
    }
    model = schemas.FindSignalLoadsResult.model_validate(r)
    assert model.completeness == "shallow_only"
    assert model.loads
    assert model.backend_status.backend == "static"
