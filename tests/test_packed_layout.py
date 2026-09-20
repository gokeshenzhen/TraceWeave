"""Elaborated type evidence, independent of dump projection arithmetic."""
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from src.connectivity_ir import CoverageStatus, CoverageGap
from src.connectivity_query import ConnectivityQueryEngine
from src.packed_layout import packed_field_selections
from src.slang_connectivity_projector import SlangConnectivityProjector


def entry():
    import pyslang
    tree = pyslang.syntax.SyntaxTree.fromText("""
module leaf #(parameter W=3) ();
  typedef struct packed {logic valid; logic [W-1:0] data;} packet_t;
  packet_t bus;
endmodule
module top;
  if (1) begin : active
    leaf #(.W(3)) small_bus();
    leaf #(.W(6)) large_bus();
  end else begin : inactive
    leaf #(.W(11)) absent();
  end
endmodule
""")
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    ir = SlangConnectivityProjector(source_manager=tree.sourceManager).project(compilation.getRoot()).ir
    return NS(ir=ir, query_engine=ConnectivityQueryEngine(ir), ir_fingerprint_sha256="test-ir",
              build_key=NS(cross_request_reusable=True),
              artifact_identity=NS(scope=NS(projection_instance_paths=tuple(i.path for i in ir.instances)),
                                   source=NS(compile_inputs=NS(fingerprint="test-source"))))


def test_active_generate_parameter_layouts_and_inactive_branch():
    e = entry()
    small = packed_field_selections(e, "top.active.small_bus.bus", "dump.small[3:0]", ["valid", "data"])
    large = packed_field_selections(e, "top.active.large_bus.bus", "dump.large[6:0]", ["valid", "data"])
    assert small["fields"]["valid"]["bits"] == [3]
    assert small["fields"]["data"]["bits"] == [2, 1, 0]
    assert large["fields"]["valid"]["bits"] == [6]
    assert large["fields"]["data"]["bits"] == [5, 4, 3, 2, 1, 0]
    assert small["evidence"]["definition_id"] != large["evidence"]["definition_id"]
    with pytest.raises((KeyError, ValueError)):
        packed_field_selections(e, "top.inactive.absent.bus", "dump.absent[11:0]", ["data"])


@pytest.mark.parametrize("gap", ["identity", "port_only", "type_gap", "diagnostic"])
def test_missing_type_or_identity_evidence_requires_mapping(gap):
    e = entry()
    if gap == "identity":
        e.build_key.cross_request_reusable = False
    elif gap == "port_only":
        e.artifact_identity.scope.projection_instance_paths = ("top",)
    elif gap == "type_gap":
        e.ir = replace(e.ir, coverage=replace(e.ir.coverage, status=CoverageStatus.PARTIAL,
                            gaps=(CoverageGap(code="unmodeled_type", message="type unknown", impact=CoverageStatus.PARTIAL),)))
    else:
        e.ir = replace(e.ir, coverage=replace(e.ir.coverage, status=CoverageStatus.PARTIAL,
                            diagnostic_count=1, blocking_diagnostic_count=1))
    with pytest.raises(ValueError):
        packed_field_selections(e, "top.active.small_bus.bus", "dump.bus[3:0]", ["data"])
