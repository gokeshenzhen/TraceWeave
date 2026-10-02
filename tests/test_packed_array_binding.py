"""Typed packed-array coordinates, independent of waveform name spelling."""
import pytest

from src.connectivity_ir import ConnectivityIR
from src.connectivity_query import ConnectivityQueryEngine, QueryStatus
from src.slang_connectivity_projector import SlangConnectivityProjector


def project(text):
    slang = pytest.importorskip("pyslang")
    tree = slang.syntax.SyntaxTree.fromText(text)
    compilation = slang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    root = compilation.getRoot()
    assert not any(d.isError() for d in compilation.getAllDiagnostics())
    return SlangConnectivityProjector(source_manager=tree.sourceManager).project(root).ir


@pytest.mark.parametrize("bounds,index,expected", [
    ("[5:3]", 4, (9, 8, 7, 6)),
    ("[3:5]", 3, (14, 13, 12, 11)),
    ("[-1:-3]", -3, (4, 3, 2, 1)),
])
def test_struct_array_field_driver_and_roundtrip(bounds, index, expected):
    ir = project(f"""
      typedef struct packed {{logic [3:0] data; logic gnt;}} rsp_t;
      module top(input rsp_t src, output logic [3:0] data);
        rsp_t {bounds} rsp;
        assign rsp[{index}] = src;
        assign data = rsp[{index}].data;
      endmodule
    """)
    ir = ConnectivityIR.from_json_bytes(ir.to_json_bytes())
    engine = ConnectivityQueryEngine(ir)
    field = engine.resolve_signal(f"top.rsp[{index}].data[3:0]")
    assert field.symbol == "rsp" and field.bits == expected
    element = engine.resolve_signal(f"top.rsp[{index}]")
    assert element.width == 5
    driver = engine.query_driver(f"top.rsp[{index}].data[3:0]", max_depth=8)
    assert driver.status is QueryStatus.FOUND
    assert driver.matches[0].target.bits == element.bits
    assert driver.matches[0].dependencies[0].source.symbol == "src"
    # Assignment expression mapping uses the same elaborated field offsets.
    data_driver = engine.query_driver("top.data", max_depth=8)
    assert data_driver.matches[0].dependencies[0].source.bits == expected
    with pytest.raises(ValueError):
        engine.resolve_signal(f"top.rsp[{index}].data[4]")


def test_nested_packed_arrays_and_union_aliases():
    ir = project("""
      typedef union packed {logic [7:0] word; logic [0:7] asc;} elem_t;
      module top;
        elem_t [1:0][2:3] a;
        assign a[0][2].word = 8'h12;
      endmodule
    """)
    e = ConnectivityQueryEngine(ir)
    assert e.resolve_signal("top.a[0][2].word[7:4]").bits == (15, 14, 13, 12)
    assert e.resolve_signal("top.a[0][2].asc[0:3]").bits == (15, 14, 13, 12)
    assert e.resolve_signal("top.a[0][2]").width == 8
    assert e.query_driver("top.a[0][2].word", max_depth=8).status is QueryStatus.FOUND


def test_alias_budget_is_explicit_and_unpacked_arrays_remain_unmodeled(monkeypatch):
    import src.slang_connectivity_projector as projector
    monkeypatch.setattr(projector, "MAX_PACKED_ALIASES", 3)
    ir = project("""
      typedef struct packed {logic [3:0] data; logic gnt;} elem_t;
      module top;
        elem_t [7:0] a;
        elem_t b[7:0];
      endmodule
    """)
    definition = ir.definitions[0]
    assert len(definition.packed_members) == 3
    gaps = {g.code for g in ir.coverage.gaps}
    assert "packed_members_budget_exceeded" in gaps
    assert "array_connectivity_unmodeled" in gaps
    assert not any(m.name.startswith("b[") for m in definition.packed_members)
