"""A/B actions stay bound even when a shared primary log has two supplements."""
import pytest

import server
from src.compile_session_snapshot import CompileSessionSnapshotBuilder
from src.divergence_context import resolve_context, driver_action
from src.hierarchy_handles import HandleStore, compute_handle, compute_snapshot_fingerprint


@pytest.fixture
def anyio_backend():
    return "asyncio"


def register(store, primary, supplement, source):
    ctx = {"compile_log": str(primary), "simulator": "vcs",
           "supplementary_compile_logs": [str(supplement)]}
    snapshot = compute_snapshot_fingerprint(str(primary), "vcs", [str(supplement)])
    builder = CompileSessionSnapshotBuilder()
    for path in (primary, supplement, source):
        builder.read_text(str(path))
    hierarchy = {"compile_result": {"files": {"user": [{"path": str(source)}]}},
                 "_hierarchy_snapshot_sha256": snapshot,
                 "_compile_session_snapshot": builder.finish()}
    store.register(compute_handle(str(primary), "vcs", [str(supplement)]), hierarchy)
    return ctx, hierarchy


@pytest.fixture
def contexts(tmp_path):
    store = HandleStore()
    files = [tmp_path / n for n in ("primary.log", "a.log", "b.log", "a.sv", "b.sv")]
    for p in files:
        p.write_text(p.name)
    primary, a, b, sa, sb = files
    ca, ha = register(store, primary, a, sa)
    cb, hb = register(store, primary, b, sb)
    return store, ca, cb, ha, hb, sa


def test_same_primary_different_supplements_remain_exact(contexts):
    store, ca, cb, ha, hb, _ = contexts
    for ctx, expected in ((cb, hb), (ca, ha), (cb, hb), (ca, ha)):
        bound, reason = resolve_context(ctx, store)
        assert reason == "ready" and bound.hierarchy is expected
    assert resolve_context({"compile_log": ca["compile_log"]}, store)[0] is None


def test_stale_source_and_stale_explicit_identity_block(contexts):
    store, ca, cb, _, _, sa = contexts
    bound, _ = resolve_context(ca, store)
    assert resolve_context({**ca, "hierarchy_handle": "tbh_wrong"}, store)[0] is None
    sa.write_text("changed source")
    assert not bound.current()
    assert resolve_context(ca, store)[1] == "compile_context_changed"
    assert resolve_context(cb, store)[0] is not None


def test_action_has_exact_tokens_and_valid_prerequisite_shapes(contexts):
    store, ca, _, _, _, _ = contexts
    result = {"wave_path_a": "a.vcd", "signal_a": "top.q", "first_divergence_time_ps": 10}
    bound, _ = resolve_context(ca, store)
    ready = driver_action(result=result, side="a", context=ca, bound=bound)
    assert ready["status"] == "ready"
    assert ready["arguments"]["compile_context"]["snapshot_sha256"] == bound.snapshot
    missing = driver_action(result=result, side="a", context=None)
    assert missing["status"] == "needs_context"
    retry = driver_action(result=result, side="a", context=ca)
    assert len(retry["prerequisite_calls"]) == 3
    assert all("supplementary_compile_logs" not in c["arguments"]
               for c in retry["prerequisite_calls"] if c["tool"] == "scan_structural_risks")


@pytest.mark.anyio
async def test_public_ready_actions_execute_with_frozen_context(contexts, monkeypatch):
    store, ca, cb, ha, hb, _ = contexts
    monkeypatch.setattr(server, "_handle_store", store)
    seen = []
    async def route(operation, args, simulator):
        hierarchy, snapshot, _, _ = await server._resolve_connectivity_hierarchy_context(
            args=args, simulator=simulator)
        seen.append(hierarchy)
        return {"signal_path": args["signal_path"], "wave_path": args["wave_path"],
                "resolved_rtl_name": "q", "driver_status": "not_found"}, {}
    monkeypatch.setattr(server, "_route_public_connectivity", route)
    for ctx in (cb, ca, cb, ca):
        result = {"wave_path_a": "a.vcd", "signal_a": "top.q", "first_divergence_time_ps": 10}
        bound, _ = resolve_context(ctx, store)
        action = driver_action(result=result, side="a", context=ctx, bound=bound)
        await server._dispatch(action["tool"], action["arguments"])
    assert seen == [hb, ha, hb, ha]


@pytest.mark.anyio
async def test_public_tool_schemas_accept_actions():
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "compile_context" in tools["explain_signal_driver"].inputSchema["properties"]
    assert {"context_a", "context_b"} <= tools["diff_first_divergence"].inputSchema["properties"].keys()
