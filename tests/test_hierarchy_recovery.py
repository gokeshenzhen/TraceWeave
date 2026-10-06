"""Expired handles recover one bounded current context, never another case."""
import asyncio
from pathlib import Path
import threading
import time

import pytest

import server
from src import cancellation
from src.divergence_context import resolve_context
from src.hierarchy_handles import HandleStore
from src.hierarchy_recovery import HierarchyRecoveryRuntime

REAL_CONNECTIVITY_ROUTE = server._route_public_connectivity


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    server.reset_session_state()
    monkeypatch.setattr(server, "_handle_store", HandleStore())
    monkeypatch.setattr(server, "_hierarchy_recovery_runtime", HierarchyRecoveryRuntime())
    monkeypatch.setattr(server, "_check_prerequisites", lambda *_: None)
    monkeypatch.setattr(server, "apply_npi_source_overlay", lambda *_: None)
    async def no_binding(*_):
        return None
    monkeypatch.setattr(server, "_bind_wave_design_root", no_binding)
    async def route(*, args, **_):
        assert args["_bound_context"].current()
        return dict(signal_path=args["signal_path"], wave_path=args["wave_path"],
                    resolved_rtl_name="q", driver_status="not_found"), {}
    monkeypatch.setattr(server, "_route_public_connectivity", route)
    yield
    server.reset_session_state()


def design(tmp_path, *, name="top", include=False):
    source = tmp_path / f"{name}.sv"
    source.write_text((f'`include "extra.svh"\n' if include else '') + f"module {name}; wire q; endmodule\n")
    if include:
        (tmp_path / "extra.svh").write_text("// include\n" * 20)
    log = tmp_path / f"{name}.log"
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog +incdir+{tmp_path} {source} -top {name}\nParsing design file '{source}'\nTop Level Modules:\n       {name}\n")
    return source, dict(compile_log=str(log), simulator="vcs", top_hint=name)


async def old_context(ctx):
    built = await server._dispatch("build_tb_hierarchy", ctx)
    bound, reason = resolve_context(ctx, server._handle_store)
    assert reason == "ready"
    server._handle_store.invalidate()
    return bound.context, built


async def query(ctx):
    return await server._dispatch("explain_signal_driver", dict(
        compile_log=ctx["compile_log"], simulator=ctx.get("simulator", "auto"),
        signal_path="top.q", wave_path="synthetic.vcd", compile_context=ctx))


@pytest.mark.anyio
async def test_expired_handle_rebuilds_and_returns_verified_identity(monkeypatch, tmp_path):
    _, raw = design(tmp_path)
    ctx, built = await old_context(raw)
    original = server.build_hierarchy
    calls = []
    def build(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(server, "build_hierarchy", build)
    state_before = dict(server._session_state)
    result = await query(ctx)
    assert result.hierarchy_recovery.status == "rebuilt"
    assert result.hierarchy_recovery.identity_basis == "validated_historical_source"
    assert result.hierarchy_recovery.hierarchy_handle == ctx["hierarchy_handle"]
    assert result.hierarchy_recovery.structural_scan_performed is False
    assert len(calls) == 1 and calls[0]["apply_source_overlay"] is False
    assert server._session_state == state_before
    assert built.handle_lifetime == "process"
    again = await query(ctx)
    assert again.hierarchy_recovery.status == "hit_existing" and len(calls) == 1


@pytest.mark.anyio
async def test_no_historical_source_token_establishes_current_context(tmp_path):
    _, ctx = design(tmp_path)
    result = await query(ctx)
    assert result.hierarchy_recovery.status == "rebuilt"
    assert result.hierarchy_recovery.identity_basis == "current_sources"
    assert result.hierarchy_recovery.compile_context["source_snapshot_sha256"]


@pytest.mark.anyio
async def test_source_change_cannot_be_hidden_by_recovery(tmp_path):
    source, raw = design(tmp_path)
    ctx, _ = await old_context(raw)
    source.write_text("module top; wire changed; endmodule\n")
    result = await query(ctx)
    assert result.error_code == "compile_context_changed"
    assert result.hierarchy_recovery.status == "blocked"
    assert len(server._handle_store) == 0


@pytest.mark.anyio
async def test_changed_logs_and_wrong_top_never_recover(monkeypatch, tmp_path):
    _, raw = design(tmp_path)
    ctx, _ = await old_context(raw)
    Path(ctx["compile_log"]).write_text("changed compile\n")
    def forbidden(*_, **__):
        raise AssertionError("must not rebuild changed identity")
    monkeypatch.setattr(server, "build_hierarchy", forbidden)
    r = await query(ctx)
    assert r.error_code == "compile_context_changed"
    assert not r.hierarchy_recovery.attempted


@pytest.mark.anyio
async def test_auto_simulator_old_handle_can_recover(tmp_path):
    _, raw = design(tmp_path)
    ctx, _ = await old_context(raw)
    r = await query({**ctx, "simulator":"auto"})
    assert r.hierarchy_recovery.status == "rebuilt"
    assert r.hierarchy_recovery.compile_context["simulator"] == "vcs"


@pytest.mark.anyio
async def test_recovery_bounds_include_files_before_publication(monkeypatch, tmp_path):
    from src.hierarchy_recovery import RecoveryLimits
    _, raw = design(tmp_path, include=True)
    monkeypatch.setattr(server, "get_hierarchy_recovery_limits", lambda: RecoveryLimits(max_source_files=1))
    r = await query(raw)
    assert r.error_code == "hierarchy_recovery_source_file_limit"
    assert len(server._handle_store) == 0


@pytest.mark.anyio
async def test_recovery_source_bytes_and_timeout_are_bounded(monkeypatch, tmp_path):
    from src.hierarchy_recovery import RecoveryLimits
    source, ctx = design(tmp_path, include=True)
    monkeypatch.setattr(server, "get_hierarchy_recovery_limits", lambda: RecoveryLimits(max_source_bytes=source.stat().st_size + 2))
    result = await query(ctx)
    assert result.error_code == "hierarchy_recovery_source_byte_limit"
    stopped = threading.Event()
    def slow(*_, **__):
        try:
            while True:
                cancellation.check_cancelled()
                time.sleep(.002)
        except cancellation.OperationCancelled:
            stopped.set()
            raise
    monkeypatch.setattr(server, "build_hierarchy", slow)
    monkeypatch.setattr(server, "get_hierarchy_recovery_limits", lambda: RecoveryLimits(timeout_sec=.03))
    result = await query(ctx)
    assert result.error_code == "hierarchy_recovery_timeout"
    assert await asyncio.to_thread(stopped.wait, 1)
    assert len(server._handle_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_all", [False, True])
async def test_shared_recovery_cancellation_and_single_build(monkeypatch, tmp_path, cancel_all):
    _, ctx = design(tmp_path)
    original = server.build_hierarchy
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def slow(*args, **kwargs):
        calls.append(1)
        started.set()
        try:
            while not release.wait(.002):
                cancellation.check_cancelled()
            return original(*args, **kwargs)
        except cancellation.OperationCancelled:
            stopped.set()
            raise
    monkeypatch.setattr(server, "build_hierarchy", slow)
    first = asyncio.create_task(query(ctx))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(query(ctx))
        await asyncio.sleep(.03)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not stopped.is_set()
        if cancel_all:
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert await asyncio.to_thread(stopped.wait, 1)
            assert len(server._handle_store) == 0
        else:
            release.set()
            r = await second
            assert r.hierarchy_recovery.status == "rebuilt"
            assert r.hierarchy_recovery.coalesced
            assert len(server._handle_store) == 1
        assert len(calls) == 1
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)


@pytest.mark.anyio
async def test_shared_flight_deadline_cannot_be_extended_by_later_waiter():
    runtime = HierarchyRecoveryRuntime()
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = []

    async def build():
        calls.append(1)
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    first = asyncio.create_task(runtime.run("same", build, timeout_sec=.1))
    await started.wait()
    second = asyncio.create_task(runtime.run("same", build, timeout_sec=5))
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=1)
    assert all(isinstance(outcome, TimeoutError) for outcome in outcomes)
    assert stopped.is_set() and len(calls) == 1
    assert not runtime._flights


@pytest.mark.anyio
async def test_recovery_admission_wait_is_inside_deadline():
    runtime = HierarchyRecoveryRuntime()
    started = asyncio.Event()
    calls = []

    async def build():
        calls.append(1)
        started.set()
        await asyncio.Future()

    first = asyncio.create_task(runtime.run("first", build, timeout_sec=5))
    try:
        await started.wait()
        with pytest.raises(TimeoutError):
            await runtime.run("queued", build, timeout_sec=.03)
        assert len(calls) == 1
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.anyio
async def test_other_case_handles_survive_recovery(tmp_path):
    _, a = design(tmp_path, name="case_a")
    _, b = design(tmp_path)
    ready = await server._dispatch("build_tb_hierarchy", a)
    stored = server._handle_store.resolve(ready.hierarchy_handle)
    r = await query(b)
    assert r.hierarchy_recovery.status == "rebuilt"
    assert server._handle_store.resolve(ready.hierarchy_handle) is stored


@pytest.mark.anyio
async def test_ambiguous_top_and_wrong_simulator_stop_before_build(monkeypatch, tmp_path):
    _, ctx = design(tmp_path)
    def forbidden(*_, **__):
        raise AssertionError("ambiguous contexts cannot build")
    monkeypatch.setattr(server, "build_hierarchy", forbidden)
    wrong_sim = await query({**ctx, "simulator":"xcelium"})
    assert wrong_sim.error_code == "compile_context_changed"
    wrong_top = await query({**ctx, "top_hint":"other"})
    assert wrong_top.error_code == "compile_context_changed"
    original = server._parse_merged_compile_context
    def ambiguous(**kwargs):
        parsed, sim = original(**kwargs)
        return {**parsed, "top_modules":["top","other"]}, sim
    monkeypatch.setattr(server, "_parse_merged_compile_context", ambiguous)
    r = await query({k:v for k,v in ctx.items() if k != "top_hint"})
    assert r.error_code == "compile_context_ambiguous"
    assert len(server._handle_store) == 0


@pytest.mark.anyio
async def test_supplement_order_change_and_missing_log_block(monkeypatch, tmp_path):
    from src.hierarchy_handles import compute_handle, compute_snapshot_fingerprint
    _, raw = design(tmp_path)
    supplements = [tmp_path / name for name in ("a.log", "b.log")]
    for path in supplements:
        path.write_text("Chronologic VCS simulator\n")
    logs = [str(p) for p in supplements]
    ctx = {**raw, "supplementary_compile_logs":logs,
           "hierarchy_handle":compute_handle(raw["compile_log"], "vcs", logs),
           "snapshot_sha256":compute_snapshot_fingerprint(raw["compile_log"], "vcs", logs)}
    def forbidden(*_, **__):
        raise AssertionError("must not rebuild identity mismatch")
    monkeypatch.setattr(server, "build_hierarchy", forbidden)
    r = await query({**ctx, "supplementary_compile_logs":list(reversed(logs))})
    assert r.error_code == "compile_context_changed" and not r.hierarchy_recovery.attempted
    missing = await query({**raw, "compile_log":str(tmp_path / "missing.log")})
    assert missing.error_code == "hierarchy_recovery_log_unavailable"


@pytest.mark.anyio
async def test_resolver_ambiguity_does_not_attempt_rebuild(monkeypatch, tmp_path):
    from src.hierarchy_handles import compute_handle, compute_snapshot_fingerprint
    from src.divergence_context import log_identity
    _, ctx = design(tmp_path)
    cr = server.parse_compile_log(ctx["compile_log"], "vcs")
    full = server.build_hierarchy(cr, apply_source_overlay=False)
    for sim in ("vcs", "xcelium"):
        candidate = {**full, "compile_result":{**cr,"simulator":sim},
            "_compile_log_identity_sha256":log_identity(ctx),
            "_hierarchy_snapshot_sha256":compute_snapshot_fingerprint(ctx["compile_log"],sim)}
        server._handle_store.register(compute_handle(ctx["compile_log"],sim), candidate)
    def forbidden(*_, **__):
        raise AssertionError("ambiguous resolver must not rebuild")
    monkeypatch.setattr(server, "build_hierarchy", forbidden)
    r = await query({**ctx,"simulator":"auto"})
    assert r.error_code == "compile_context_ambiguous"
    assert not r.hierarchy_recovery.attempted


@pytest.mark.anyio
async def test_failed_build_and_changed_during_build_never_publish(monkeypatch, tmp_path):
    source, ctx = design(tmp_path)
    original = server.build_hierarchy
    def broken(*_, **__):
        raise RuntimeError("private tool detail")
    monkeypatch.setattr(server, "build_hierarchy", broken)
    r = await query(ctx)
    assert r.error_code == "hierarchy_recovery_failed"
    assert "private tool detail" not in r.model_dump_json()
    def changed(*args, **kwargs):
        full = original(*args, **kwargs)
        source.write_text("module top; wire changed; endmodule\n")
        return full
    monkeypatch.setattr(server, "build_hierarchy", changed)
    r = await query(ctx)
    assert r.error_code == "compile_context_changed"
    assert len(server._handle_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("corrected,counts", [(False,(31,1,1)), (True,(32,0,0))])
async def test_real_isolated_worker_matches_recovered_and_no_handle_queries(monkeypatch, tmp_path, corrected, counts):
    import importlib.metadata
    import sys
    from config import SourceGraphExecutionConfig
    import src.connectivity_backend as backends
    from src.source_graph_runtime import SourceGraphRuntime, IsolatedSourceGraphProcessRunner
    from tests.test_source_graph_driver_processes import FIXTURE
    source = tmp_path / "bank.sv"
    text = FIXTURE.read_text()
    source.write_text(text.replace("S[6:9]", "S[5:8]") if corrected else text)
    log = tmp_path / "compile.log"
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top bank\nParsing design file '{source}'\nTop Level Modules:\n       bank\n")
    ctx = dict(compile_log=str(log),simulator="vcs",top_hint="bank")
    monkeypatch.setenv("TRACEWEAVE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "_route_public_connectivity", REAL_CONNECTIVITY_ROUTE)
    config = SourceGraphExecutionConfig(enabled=True, python_bin=sys.executable,
        frontend_version=importlib.metadata.version("pyslang"), timeout_sec=5)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda:config)
    runtime = SourceGraphRuntime(IsolatedSourceGraphProcessRunner(python_executable=sys.executable,
                                staging_directory=tmp_path))
    monkeypatch.setattr(server, "get_source_graph_runtime", lambda _:runtime)
    monkeypatch.setattr(server, "probe_verdi_backend", lambda *_, **__:dict(
        simulator="vcs", backend="static", parser_match="approximate", kdb_path=None,
        kdb_flow="none", kdb_validation_status="unavailable", kdb_hint=None))
    monkeypatch.setattr(backends, "select_backend", lambda _, *, fallback:fallback)
    args = dict(**ctx, signal_path="bank.S",wave_path="synthetic.vcd",recursive=True)
    recovered = await server._dispatch("explain_signal_driver", {**args,"compile_context":ctx})
    plain = await server._dispatch("explain_signal_driver", args)
    assert recovered.hierarchy_recovery.status == "rebuilt"
    assert recovered.backend == plain.backend == "source_graph"
    assert (recovered.resolved_bit_count, recovered.unresolved_bit_count, recovered.multi_driver_bit_count) == counts
    assert recovered.driver_chain == plain.driver_chain
    assert recovered.bit_provenance == plain.bit_provenance
    assert recovered.claim_semantics == plain.claim_semantics
    assert recovered.traversal.retained_evidence_count == 512
    assert plain.backend_status.source_graph.metrics.frontend_launch_count == 0
    for result in (recovered, plain):
        receipt = result.backend_status.source_graph
        assert receipt.single_artifact_provenance
        assert receipt.final_artifact_scope_match
        if not corrected:
            assert receipt.blocker.code == "frontier_expansion_stalled"
            assert not result.claim_semantics.negative_claim_allowed
