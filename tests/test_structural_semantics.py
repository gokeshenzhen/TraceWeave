from pathlib import Path

import pytest

from src.structural_semantics import ScanLimits, scan_semantic_session


def session(tmp_path, text):
    pytest.importorskip("pyslang")
    from src.structural_semantic_worker import build_session
    source = tmp_path / "design.sv"
    source.write_text(text)
    return build_session({"frontend_version": "11.0.0", "frontend_args": [str(source), "--top", "top"]})


def test_active_branches_partial_ties_open_inputs_and_named_consumers(tmp_path):
    model = session(tmp_path, """
module leaf #(parameter N=8)(input [N-1:0] d,input en,output y);
  localparam MAGIC=8'hA5;
  assign y = en && (d == MAGIC);
endmodule
module top(input [3:0] d,output y,z);
  leaf u(.d({4'b10xz,d}),.en(1'b0),.y(y));
  leaf #(.N(4)) v(.d(),.en(1'b1),.y(z));
  if(0)begin leaf inactive(.d(8'hff),.en(1'b0),.y()); end
endmodule
""")
    result = scan_semantic_session(model)
    assert result["status"] == "complete"
    assert result["instances_visited"] == 3
    partial = next(f for f in result["facts"] if f.get("port") == "d" and f["kind"] == "constant_connection")
    assert partial["constant_regions"] == [{"lsb_offset": 4, "width": 4, "bits": "10xz"}]
    assert any(f["kind"] == "open_input" and f["instance"] == "top.v" for f in result["facts"])
    compare = next(f for f in result["facts"] if f["kind"] == "constant_comparison")
    assert compare["constant_symbol"] == "MAGIC"
    assert compare["bits"] == "10100101"
    assert compare["enclosing_consumers"] == ["y"]
    assert "en &&" in compare["enclosing_expression"]
    assert compare["constant_source"]["line"] > 0


def test_repeated_instances_share_templates_but_specializations_do_not(tmp_path):
    model = session(tmp_path, """
module leaf #(parameter N=8)(input [N-1:0] d, output y);
  assign y = d == N'(165);
endmodule
module top;
  for(genvar i=0;i<100;i++) begin:g leaf u(.d(8'h00),.y()); end
  leaf #(.N(16)) wider(.d(16'h0000),.y());
endmodule
""")
    result = scan_semantic_session(model)
    assert result["template_count"] == 3
    assert result["instances_visited"] == 102
    assert result["counts"]["constant_comparison"] == 2
    assert result["ast_nodes_visited"] < 30
    scoped = scan_semantic_session(model, scope="top.g[99].u")
    assert len(scoped["instance_bindings"]) == 1
    assert scoped["instance_bindings"][0]["instance"] == "top.g[99].u"


@pytest.mark.parametrize("limit,gap", [(ScanLimits(max_instances=1), "instance_limit"),
                                      (ScanLimits(max_facts=0), "fact_limit"),
                                      (ScanLimits(max_ast_nodes=1), "ast_node_limit")])
def test_budget_prefix_is_explicitly_partial(tmp_path, limit, gap):
    model = session(tmp_path, "module leaf(input d);endmodule module top; wire y; assign y=1'b0; leaf u(.d(1'b0)); endmodule")
    result = scan_semantic_session(model, limits=limit)
    assert result["status"] == "partial"
    assert gap in result["gaps"]


def test_sequential_reset_and_initial_values_are_not_ties(tmp_path):
    model = session(tmp_path, "module top(input clk,rst,en,output reg q); initial q=0; always @(posedge clk) if(rst)q<=0;else q<=en; endmodule")
    result = scan_semantic_session(model)
    assert result["counts"].get("constant_connection", 0) == 0


def test_hierarchical_constant_context_keeps_separate_templates(tmp_path):
    model = session(tmp_path, """
module leaf(input [7:0] data, output y); assign y=data==top.MAGIC; endmodule
module top; localparam MAGIC=8'hA5; leaf a(.data(0),.y());leaf b(.data(0),.y()); endmodule
""")
    result = scan_semantic_session(model)
    assert result["template_count"] == 3


def test_templates_reused_across_different_parent_instances(tmp_path):
    model = session(tmp_path, """
module leaf(input [7:0] a,output y);assign y=a==8'hA5;endmodule
module wrap;leaf a(.a(0),.y());leaf b(.a(0),.y());endmodule
module top;for(genvar i=0;i<100;i++)begin:g wrap w();end endmodule
""")
    result = scan_semantic_session(model)
    assert result["template_count"] == 3
    assert result["instances_visited"] == 301
    assert result["counts"]["constant_comparison"] == 1


@pytest.mark.anyio
async def test_auto_has_no_cold_build_and_deep_is_license_free(tmp_path, monkeypatch):
    import server
    from src.structural_semantic_runtime import SemanticScanRuntime
    from src.design_identity import DesignIdentityReader
    from tests.test_structural_scan_runtime import context
    log, _, compile_result = context(tmp_path, "module top; wire y; assign y=1'b0; endmodule\n")
    python = Path(__file__).resolve().parents[1] / ".venv/bin/python"
    if not python.exists():
        pytest.skip("isolated Slang interpreter unavailable")
    monkeypatch.setattr(server, "_parse_merged_compile_context", lambda **_: (compile_result, "vcs"))
    monkeypatch.setattr(server, "_semantic_scan_runtime", SemanticScanRuntime())
    monkeypatch.setattr(server, "_design_identity_reader", DesignIdentityReader())
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", str(python))
    args = {"compile_log": str(log), "simulator": "vcs"}
    auto = await server._dispatch("scan_structural_risks", args)
    assert auto.semantic.status == "not_run"
    assert auto.semantic.gaps == ["semantic_cache_miss"]
    deep = await server._dispatch("scan_structural_risks", {**args, "analysis_mode": "deep"})
    assert deep.semantic.status == "complete", deep.semantic
    assert deep.semantic.counts["constant_connection"] == 1
    warm = await server._dispatch("scan_structural_risks", args)
    assert warm.semantic.cache_disposition == "hit_memory"
    assert warm.semantic.metrics["frontend_build_count"] == 0
    assert warm.semantic.facts == deep.semantic.facts


@pytest.mark.anyio
async def test_worker_timeout_and_missing_interpreter_are_explicit():
    import sys
    from src.structural_semantic_runtime import run_isolated_scan
    payload = {"timeout_sec": .001, "max_rss_mib": 512}
    result = await run_isolated_scan(payload, sys.executable)
    assert result["status"] == "partial"
    assert result["gaps"] == ["worker_timeout"]
    result = await run_isolated_scan(payload, "/nonexistent/traceweave-python")
    assert result["status"] == "unavailable"


def test_semantic_output_trim_keeps_counts_and_coverage():
    import server
    from src.schemas import SemanticScanResult
    model = SemanticScanResult(status="complete", total_facts=3,
                               facts=[{"kind": "open_input"}] * 3)
    trimmed = server._trim_semantic_scan(model, 1)
    assert trimmed["status"] == "complete"
    assert trimmed["total_facts"] == 3
    assert trimmed["output_truncated"]
    assert len(model.facts) == 3
