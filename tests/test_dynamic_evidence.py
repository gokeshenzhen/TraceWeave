from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.connectivity_ir import ConnectivityIR
from src.connectivity_query import ConnectivityQueryEngine
from src.dynamic_evidence import Expr, TRUE, evaluate
from src.dynamic_observe import observe_step
from src.source_graph_backend import SourceGraphConnectivityBackend
from tests.test_divergence_compare import Parser


def signal(name, width=1):
    bits = tuple(range(width-1, -1, -1))
    return Expr("signal", width, signal=name, bits=bits, declared_bits=bits)


@pytest.mark.parametrize("op,values,expected", [
    ("and", {"a":"0","b":"x"}, "false"),
    ("or", {"a":"1","b":"z"}, "true"),
    ("and", {"a":"1","b":"x"}, "unknown"),
    ("or", {"a":"0","b":None}, "unsupported"),
])
def test_four_state_conditions(op, values, expected):
    result = evaluate(Expr(op, 1, (signal("a"), signal("b"))), lambda e: {"value": values[e.signal]})
    assert result.truth == expected
    assert len(result.dependencies) == 2


def test_inactive_mux_data_is_not_read_but_unknown_condition_keeps_both():
    expr = Expr("mux", 1, (signal("s"), signal("a"), signal("b")))
    for s, expected in (("1", ["s", "a"]), ("0", ["s", "b"]), ("x", ["s", "a", "b"])):
        calls=[]
        def sample(e):
            calls.append(e.signal)
            return {"value": s if e.signal == "s" else "1"}
        result = evaluate(expr, sample)
        assert calls == expected
        assert ("guard_unresolved" in result.gaps) == (s == "x")


def step(boundary="sequential"):
    return {"signal":"q", "bits":[0], "width":1, "boundary":boundary, "complete":True, "gaps":[],
            "clock":{"expression":asdict(signal("clk")), "edge":"posedge"},
            "branches":[{"id":"q_d", "order":0, "guard":asdict(signal("en")), "value":asdict(signal("d"))}]}


def test_sequential_uses_clock_and_strict_predecessor_not_last_q_change():
    p = Parser({"clk":[(0,"0"),(10,"1"),(15,"0"),(20,"1")],
                "en":[(0,"1")], "d":[(0,"0"),(17,"1")], "q":[(0,"0")]})
    r = observe_step(step(),get_parser=lambda _:p,wave="w",time=23,history_start=0)
    assert r["complete"] and r["trigger_time_ps"] == 20 and r["value"] == "1"
    data=next(d for d in r["dependencies"] if d["signal"] == "d")
    assert data["phase"] == "before" and data["predecessor_time_ps"] == 17


def test_enable_hold_uses_previous_q():
    p = Parser({"clk":[(0,"0"),(10,"1")], "en":[(0,"0")], "q":[(0,"1")]})
    r = observe_step(step(),get_parser=lambda _:p,wave="w",time=12,history_start=0)
    assert r["complete"] and r["action"] == "hold" and r["value"] == "1"
    assert {d["signal"] for d in r["dependencies"]} == {"en","q"}


def test_same_timestamp_tb_data_and_clock_cannot_prove_actual_sample():
    p = Parser({"clk":[(0,"0"),(10,"1")], "en":[(0,"1")], "d":[(0,"0"),(10,"1")]})
    r = observe_step(step(),get_parser=lambda _:p,wave="w",time=12,history_start=0)
    assert not r["complete"] and "sampling_order_unresolved" in r["gaps"]
    assert r["value"] == "0"  # A strict predecessor, explicitly NOT a proved sample.


def test_missing_trigger_is_history_boundary():
    p = Parser({"clk":[(0,"1")], "en":[(0,"1")], "d":[(0,"1")]})
    r = observe_step(step(),get_parser=lambda _:p,wave="w",time=12,history_start=5)
    assert "history_window_exhausted" in r["gaps"] and not r["dependencies"]


def project(source):
    slang = pytest.importorskip("pyslang")
    from src.slang_connectivity_projector import SlangConnectivityProjector
    tree = slang.syntax.SyntaxTree.fromText(source)
    compilation = slang.ast.Compilation(); compilation.addSyntaxTree(tree)
    projection = SlangConnectivityProjector(source_manager=tree.sourceManager).project(compilation.getRoot())
    # The worker roundtrip is part of this test; missing fields must not turn true.
    ir = ConnectivityIR.from_dict(projection.ir.to_dict())
    backend = SourceGraphConnectivityBackend(SimpleNamespace(ir=ir,query_engine=ConnectivityQueryEngine(ir)))
    return backend


def test_real_slang_nested_guard_polarity_and_normal_always_edge():
    backend = project('''module t(input logic clk,rst,en, input logic [7:0] d, output logic [7:0] q);
    always @(posedge clk) if(rst) q<=0; else if(en) q<=d; endmodule''')
    s = backend.get_dynamic_step("t.q")
    assert s["complete"] and s["clock"]["edge"] == "posedge"
    for rst,en,states in (("1","1",["true","false"]),("0","1",["false","true"]),("0","0",["false","false"])):
        values={"t.rst":rst,"t.en":en}
        assert [evaluate(Expr.from_dict(b["guard"]),lambda e:{"value":values[e.signal]}).truth
                for b in sorted(s["branches"],key=lambda b:b["order"])] == states


def test_real_slang_mux_concat_and_typed_constant_equality():
    backend = project('''module t(input logic [3:0] s,a,b, output wire [7:0] y);
      assign y=(s==4'd3)?{a,4'b0011}:{4'b0000,b}; endmodule''')
    s=backend.get_dynamic_step("t.y")
    assert s["complete"]
    values={"t.s":"0011","t.a":"1010"}
    e=evaluate(Expr.from_dict(s["branches"][0]["value"]),lambda e:{"value":values[e.signal]})
    assert e.value == "10100011" and not e.gaps


@pytest.mark.parametrize("body", [
    "always @(posedge clk or negedge rst) if(!rst) q<=0; else q<=d;",
    "always_comb begin q=d; q=q+1; end",
    "always_comb case(d) 0:q=0; default:q=1; endcase",
    "always @(posedge clk) q=d;",
])
def test_real_slang_unsupported_semantics_cannot_claim_complete(body):
    backend=project("module t(input logic clk,rst,d,output logic q);"+body+"endmodule")
    assert not backend.get_dynamic_step("t.q")["complete"]


def test_old_ir_does_not_acquire_dynamic_capability():
    with pytest.raises(ValueError, match="version"):
        ConnectivityIR.from_dict({"ir_version":"1.2"})
