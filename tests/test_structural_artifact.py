from dataclasses import replace
import asyncio
from pathlib import Path

import pytest

from src.source_graph_runtime import SourceGraphRuntime
from tests.test_source_graph_runtime import _request, _ready_result


class NoBuild:
    async def run(self, *args, **kwargs):
        raise AssertionError("a shared artifact must not launch another frontend")


@pytest.mark.anyio
async def test_scan_artifact_enters_normal_exact_cache_and_cannot_cross_design():
    runtime = SourceGraphRuntime(NoBuild())
    request = _request()
    assert await runtime.lookup_prepared(request) is None
    assert await runtime.publish_prepared(request, _ready_result(request))
    outcome = await runtime.prepare(request, timeout_seconds=1)
    assert outcome.entry is not None
    assert runtime.stats_snapshot()["actual_build_count"] == 0
    assert await runtime.lookup_prepared(_request(label="another_design")) is None


@pytest.mark.anyio
async def test_incomplete_corrupt_or_oversized_artifact_cannot_publish():
    runtime = SourceGraphRuntime(NoBuild())
    incomplete = _request(complete=False)
    assert not await runtime.publish_prepared(incomplete, _ready_result(incomplete))
    request = _request()
    with pytest.raises(ValueError, match="fingerprint"):
        await runtime.publish_prepared(request, replace(_ready_result(request), ir_fingerprint_sha256="0"*64))
    tiny = SourceGraphRuntime(NoBuild(), max_cache_bytes=1)
    assert not await tiny.publish_prepared(request, _ready_result(request))
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_deep_scan_shares_one_frontend_with_followup_query(tmp_path, monkeypatch):
    import server
    import src.source_graph_production as production
    from src.structural_semantic_runtime import SemanticScanRuntime
    from src.design_identity import DesignIdentityReader
    python = Path(__file__).resolve().parents[1] / ".venv/bin/python"
    if not python.exists():
        pytest.skip("isolated Slang interpreter unavailable")
    source = tmp_path / "top.sv"
    source.write_text("""module leaf(input en, output out);
assign out = en;
endmodule
module top(output out);
leaf u(.en(1'b0),.out(out));
endmodule
""")
    log = tmp_path / "build.log"
    log.write_text(f"Command: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n top\n")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", str(python))
    monkeypatch.setenv("TRACEWEAVE_AUTO_KDB", "0")
    monkeypatch.setenv("TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY", "off")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setattr(server, "_semantic_scan_runtime", SemanticScanRuntime())
    monkeypatch.setattr(server, "_design_identity_reader", DesignIdentityReader())
    monkeypatch.setattr(production, "_PROCESS_SESSION", production.SourceGraphRuntimeSession())
    args = {"compile_log": str(log), "simulator": "vcs"}
    await server._dispatch("build_tb_hierarchy", args)
    hierarchy, snapshot = server._resolve_hierarchy_context(str(log), "vcs")
    assert hierarchy is not None
    plan = server.build_source_graph_plan(compile_log=str(log), compile_result=hierarchy["compile_result"],
        hierarchy_result=hierarchy, hierarchy_snapshot_sha256=snapshot,
        operation="driver", signal_path="top.u.__structural_scan_anchor__", top_hint=None,
        max_hops=10, frontend_version="11.0.0", max_instances=64, allow_adjacent=False)
    assert plan.request is not None, plan.receipt
    assert server.compute_source_graph_build_key(plan.request).cross_request_reusable, plan.receipt
    deep = await server._dispatch("scan_structural_risks", {**args, "analysis_mode": "deep", "semantic_scope": "top.u"})
    assert deep.semantic.query_artifact_status == "published", deep.semantic
    runtime = production.get_source_graph_runtime(server.get_source_graph_execution_config())
    assert runtime.stats_snapshot()["cache_entry_count"] == 1
    runtime._worker_runner = NoBuild()
    queried = await server._dispatch("explain_signal_driver", {
        **args, "signal_path": "top.u.en", "wave_path": str(tmp_path / "unused.vcd"),
    })
    assert queried.backend_status.actual_backend == "source_graph"
    assert queried.backend_status.source_graph.cache_disposition in {"hit_exact", "hit_scope_superset"}
    request, _ = await server._structural_query_context(str(log), "vcs", "top.u", "auto", server.get_source_graph_execution_config())
    outcome = await runtime.prepare(request, timeout_seconds=1)
    driver = outcome.entry.query_engine.query_driver("top.u.en")
    assert driver.matches
    assert runtime.stats_snapshot()["actual_build_count"] == 0
    # Discard semantic result cache to exercise query -> scan reuse independently.
    monkeypatch.setattr(server, "_semantic_scan_runtime", SemanticScanRuntime())
    auto = await server._dispatch("scan_structural_risks", {**args, "semantic_scope": "top.u"})
    assert auto.semantic.cache_disposition == "hit_query_artifact"
    assert auto.semantic.status == "partial"
    assert auto.semantic.facts[0]["bits"] == "0"
    assert auto.semantic.metrics["frontend_build_count"] == 0
    # Changed source evidence cannot reuse the old query IR, even when size and
    # the requested scope stay the same.
    source.write_text(source.read_text().replace("1'b0", "1'b1"))
    changed = await server._dispatch("scan_structural_risks", {**args, "semantic_scope": "top.u"})
    assert changed.semantic.status == "not_run"


@pytest.mark.anyio
async def test_scan_admission_timeout_and_cancellation_preserve_query_owner():
    from src.structural_semantic_runtime import run_isolated_scan
    from src.source_graph_runtime import _PROCESS_COLD_BUILD_LOCK
    assert _PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
    try:
        result = await run_isolated_scan({"timeout_sec": .02}, "/must/not/launch")
        assert result["gaps"] == ["frontend_admission_timeout"]
        assert _PROCESS_COLD_BUILD_LOCK.locked()
        task = asyncio.create_task(run_isolated_scan({"timeout_sec": 10}, "/must/not/launch"))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _PROCESS_COLD_BUILD_LOCK.locked()
    finally:
        _PROCESS_COLD_BUILD_LOCK.release()
