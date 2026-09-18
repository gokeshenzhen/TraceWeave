from dataclasses import replace
from pathlib import Path

import pytest

from src.structural_semantics import ScanLimits, scan_semantic_session
from src.structural_propagation import propagate_session
from tests.test_structural_semantics import session


CATEGORIES = ["propagated_constant", "constant_control"]


def run(tmp_path, rtl, **kwargs):
    return scan_semantic_session(session(tmp_path, rtl), categories=CATEGORIES, **kwargs)


def signals(result):
    return {f["signal"]: f for f in result["facts"] if f["kind"] == "propagated_constant"}


def bits(result, name):
    fact = signals(result)[name]
    assert fact["state"] in {"fully_known", "four_state_constant"}
    return fact["constant_regions"][0]["bits"]


def test_three_levels_tie_mask_and_named_control(tmp_path):
    result = run(tmp_path, """
module leaf(input [7:0] a,input clk,output [7:0] b,output logic q);
  localparam [7:0] MAGIC=8'hA5;
  assign b=a & 8'h0f;
  always_ff @(posedge clk) if(a==MAGIC) q<=1;else q<=0;
endmodule
module wrap(input [7:0] a,input clk,output [7:0] b);
  leaf u(.a(a),.clk(clk),.b(b),.q());
endmodule
module top(input clk,output [7:0] out);
  wire [7:0] tie_value=8'hA5;
  wrap w(.a(tie_value),.clk(clk),.b(out));
endmodule
""")
    assert result["status"] == "complete", result["gaps"]
    assert bits(result, "top.out") == "00000101"
    assert bits(result, "top.w.u.a") == "10100101"
    assert "top.w.u.q" not in signals(result)
    assert result["propagation"]["conflict_bits"] == 0
    assert any(f["kind"] == "constant_control" and f["role"] == "if_guard" and f["value"] == "1"
               and f["enclosing_consumers"] == ["q"] for f in result["facts"])


def test_ascending_ranges_slices_concat_and_partial_unknown(tmp_path):
    result = run(tmp_path, """
module top(input [3:0] external,output [0:7] mixed, output [3:0] hi,lo,up,down);
  assign mixed={4'b10xz,external};
  assign hi=mixed[0:3];
  assign lo=mixed[4:7];
  assign up=mixed[0 +: 4];
  assign down=mixed[3 -: 4];
endmodule
""")
    assert result["status"] == "complete", result["gaps"]
    for name in ("hi", "up", "down"):
        assert bits(result, "top."+name) == "10xz"
    assert "top.lo" not in signals(result)
    mixed = signals(result)["top.mixed"]
    assert mixed["state"] == "partial_known"
    assert mixed["constant_regions"] == [{"lsb_offset": 4, "width": 4, "bits": "10xz"}]


def test_four_state_is_distinct_from_missing_driver_and_conflict(tmp_path):
    result = run(tmp_path, """
module leaf(input a,output y);assign y=a;endmodule
module top(output xz,xor_x,open_out,conflict_out,masked);
  wire xvalue=1'bx, zvalue=1'bz;
  wire conflict;
  assign conflict=1'b0;
  assign conflict=1'b1;
  assign conflict_out=conflict;
  assign xz=xvalue;
  assign xor_x=zvalue ^ 1'b0;
  assign masked=xvalue & 1'b0;
  leaf open_leaf(.a(),.y(open_out));
endmodule
""")
    assert bits(result, "top.xz") == "x"
    assert bits(result, "top.xor_x") == "x"
    assert bits(result, "top.masked") == "0"
    assert "top.open_out" not in signals(result)
    assert "top.conflict_out" not in signals(result)
    assert result["status"] == "partial"
    assert result["propagation"]["conflict_bits"] >= 2
    assert "propagation_multiple_drivers" in result["gaps"]


def test_signed_extension_two_state_cast_mux_and_arithmetic(tmp_path):
    result = run(tmp_path, """
module top(output signed [7:0] sign_ext,output [7:0] unsigned_ext, add, mul,
           output [3:0] mux, output eq, ceq,cast_bit);
  wire signed [3:0] s=4'b1101;
  wire [3:0] u=4'b1101;
  wire x=1'bx;
  assign sign_ext=s;
  assign unsigned_ext=u;
  assign add=s+8'sd5;
  assign mul=u*8'd2;
  assign mux=x ? 4'b1001 : 4'b1011;
  assign eq=x==1'bx;
  assign ceq=x===1'bx;
  assign cast_bit=bit'(x);
endmodule
""")
    assert bits(result, "top.sign_ext") == "11111101"
    assert bits(result, "top.unsigned_ext") == "00001101"
    assert bits(result, "top.add") == "00000010"
    assert bits(result, "top.mul") == "00011010"
    assert bits(result, "top.mux") == "10x1"
    assert bits(result, "top.eq") == "x"
    assert bits(result, "top.ceq") == "1"
    assert bits(result, "top.cast_bit") == "0"


def test_sequential_initial_and_unsupported_expression_are_boundaries(tmp_path):
    result = run(tmp_path, """
module top(input clk,rst,external,output logic q,output initialized,unknown_shift);
  logic initial_value=1'b0;
  initial q=1'b0;
  always @(posedge clk) if(rst)q<=0;else q<=external;
  assign initialized=initial_value;
  assign unknown_shift=external >> 1;
endmodule
""")
    assert not signals(result)
    assert "propagation_expression_unsupported" in result["gaps"]


def test_logical_equality_can_be_definitely_false_with_x_bits(tmp_path):
    result = run(tmp_path, """module top(output equal, unequal);
wire [1:0] a=2'bx0,b=2'bx1;
assign equal=a==b;assign unequal=a!=b;
initial $display("observation only");
endmodule""")
    assert bits(result, "top.equal") == "0"
    assert bits(result, "top.unequal") == "1"


def test_scoped_output_still_inventories_outside_hierarchical_writers(tmp_path):
    result = run(tmp_path, """
module child;wire y;assign y=0;endmodule
module other;assign top.c.y=1;endmodule
module top;child c();other b();endmodule
""", scope="top.c")
    assert "top.c.y" not in signals(result)
    assert "propagation_multiple_drivers" in result["gaps"]


def test_collapsed_net_port_conflict_cannot_leave_upstream_tie_proven(tmp_path):
    result = run(tmp_path, """
module child(input a);assign a=1'b1;endmodule
module top;wire a=1'b0;child c(.a(a));endmodule
""")
    assert not signals(result)
    assert "propagation_port_alias_conflict" in result["gaps"]


def test_special_net_resolution_is_an_explicit_boundary(tmp_path):
    result = run(tmp_path, "module top;tri0 pulled;wire out;assign pulled=1'bz;assign out=pulled;endmodule")
    assert not signals(result)
    assert "propagation_net_resolution_boundary" in result["gaps"]


def test_propagated_operators_match_slang_constant_oracle(tmp_path):
    # Compare runtime-shaped expressions over tied wires with Slang's own
    # constant evaluator, across actual four-state inputs. No simulation or
    # license is required, and the expected table is independent of this pass.
    values = ["00", "01", "10", "11", "x0", "0x", "xz", "zz"]
    operators = ["&", "|", "^", "~^", "==", "!=", "===", "!==", "&&", "||", "+", "-"]
    lines = ["module top;"]
    for index, value in enumerate(values):
        lines.append(f"wire [1:0] a{index}=2'b{value};")
    count = 0
    for op in operators:
        for i, a in enumerate(values):
            for j, b in enumerate(values):
                lines.append(f"wire [1:0] got{count},want{count};assign got{count}=a{i} {op} a{j};assign want{count}=2'b{a} {op} 2'b{b};")
                count += 1
    lines.append("endmodule")
    result = run(tmp_path, "\n".join(lines))
    assert result["status"] == "complete", result["gaps"]
    observed = signals(result)
    for i in range(count):
        assert observed[f"top.got{i}"]["constant_regions"] == observed[f"top.want{i}"]["constant_regions"], i


@pytest.mark.parametrize("rtl,gap", [
    ("module top;reg y;wire a=0;initial force y=0;endmodule", "propagation_force_release_boundary"),
    ("module top;wire a=0;reg b;task change; b=0;endtask initial change();endmodule", "propagation_call_effects_unmodeled"),
])
def test_unknown_global_write_effects_block_propagation(tmp_path, rtl, gap):
    result = run(tmp_path, rtl)
    assert not signals(result)
    assert gap in result["gaps"]
    assert result["propagation"]["inventory_complete"] is False


@pytest.mark.parametrize("change,gap", [
    ({"max_propagation_bits": 1}, "propagation_bit_limit"),
    ({"max_propagation_steps": 1}, "propagation_work_limit"),
    ({"max_ast_nodes": 1}, "propagation_ast_limit"),
    ({"max_instances": 1}, "propagation_instance_limit"),
])
def test_limits_do_not_publish_an_unsolved_prefix(tmp_path, change, gap):
    model = session(tmp_path, "module leaf(input a,output y);assign y=a;endmodule module top;wire y;leaf u(.a(0),.y(y));endmodule")
    result = propagate_session(model, scope=None, categories=CATEGORIES, limits=replace(ScanLimits(), **change))
    assert result["receipt"]["status"] == "partial"
    assert gap in result["receipt"]["gaps"]
    assert result["facts"] == []


def test_comb_loop_terminates_without_guessing_and_cancellation_propagates(tmp_path, monkeypatch):
    model = session(tmp_path, "module top;wire a,b,c;assign a=b;assign b=~a;assign c=1'b0;endmodule")
    result = propagate_session(model, scope=None, categories=CATEGORIES, limits=ScanLimits())
    assert [f["signal"] for f in result["facts"]] == ["top.c"]
    assert result["receipt"]["work_steps"] < 100
    from src.cancellation import OperationCancelled
    import src.structural_propagation as module
    def cancelled():
        raise OperationCancelled()
    monkeypatch.setattr(module, "check_cancelled", cancelled)
    with pytest.raises(OperationCancelled):
        propagate_session(model, scope=None, categories=CATEGORIES, limits=ScanLimits())


def test_many_instances_specializations_and_inactive_generate(tmp_path):
    result = run(tmp_path, """
module leaf #(parameter K=1)(input a,output y);
 if(K)assign y=a;else assign y=~a;
endmodule
module wrap(input a,output y);leaf u(.a(a),.y(y));endmodule
module top;
 for(genvar i=0;i<200;i++)begin:g wire y;wrap w(.a(0),.y(y));end
 wire other;
 leaf #(.K(0)) inverted(.a(0),.y(other));
endmodule
""")
    assert result["status"] == "complete", result["gaps"]
    assert bits(result, "top.g[199].w.u.y") == "0"
    assert bits(result, "top.other") == "1"
    assert result["propagation"]["instances"] == 402


@pytest.mark.anyio
async def test_public_deep_propagation_receipt_and_auto_reuse(tmp_path, monkeypatch):
    import server
    from src.structural_semantic_runtime import SemanticScanRuntime
    from src.design_identity import DesignIdentityReader
    from tests.test_structural_scan_runtime import context
    log, _, compile_result = context(tmp_path, "module top;wire a=0;wire b;assign b=~a;endmodule\n")
    python = Path(__file__).resolve().parents[1] / ".venv/bin/python"
    if not python.exists():
        pytest.skip("isolated Slang interpreter unavailable")
    monkeypatch.setattr(server, "_parse_merged_compile_context", lambda **_: (compile_result, "vcs"))
    monkeypatch.setattr(server, "_semantic_scan_runtime", SemanticScanRuntime())
    monkeypatch.setattr(server, "_design_identity_reader", DesignIdentityReader())
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", str(python))
    args = {"compile_log": str(log), "simulator": "vcs", "semantic_categories": CATEGORIES}
    deep = await server._dispatch("scan_structural_risks", {**args, "analysis_mode": "deep"})
    assert deep.semantic.propagation.inventory_complete
    assert deep.semantic.status == "complete", deep.semantic
    assert deep.semantic.counts["propagated_constant"] == 2
    warm = await server._dispatch("scan_structural_risks", args)
    assert warm.semantic.cache_disposition == "hit_memory"
    assert warm.semantic.metrics["frontend_build_count"] == 0
    assert warm.semantic.facts == deep.semantic.facts
