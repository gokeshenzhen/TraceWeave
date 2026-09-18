import asyncio
import os

import pytest

from src.design_identity import DesignIdentityReader
from src.structural_scan_runtime import StructuralScanRuntime


def context(tmp_path, text="module top; endmodule\n"):
    source = tmp_path / "top.sv"
    source.write_text(text)
    log = tmp_path / "compile.log"
    log.write_text(f"vcs -sverilog {source} -top top\n")
    result = {"simulator": "vcs", "compile_cwd": str(tmp_path),
              "compile_command": log.read_text(), "top_modules": ["top"],
              "files": {"user": [{"path": str(source)}]}}
    return log, source, result


def test_content_not_mtime_and_configuration_drive_identity(tmp_path):
    log, source, result = context(tmp_path)
    reader = DesignIdentityReader()
    first = reader.capture(str(log), result)
    assert first.complete
    assert reader.capture(str(log), result).digest == first.digest
    st = source.stat()
    source.write_text("module top; wire xx; endmodule\n")
    os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
    second = reader.capture(str(log), result)
    assert second.complete and second.digest != first.digest
    assert not first.current()
    changed = {**result, "compile_command": result["compile_command"] + " +define+MODE=1"}
    assert reader.capture(str(log), changed).digest != second.digest


def test_include_content_and_earlier_shadow_candidate_invalidate(tmp_path):
    early, late = tmp_path / "early", tmp_path / "late"
    early.mkdir(); late.mkdir()
    (late / "defs.svh").write_text("`define WIDTH 8\n")
    log, source, result = context(tmp_path, '`include "defs.svh"\nmodule top; endmodule\n')
    result["compile_command"] += f" +incdir+{early}+{late}"
    reader = DesignIdentityReader()
    first = reader.capture(str(log), result)
    assert first.complete
    (early / "defs.svh").write_text("`define WIDTH 16\n")
    assert not first.current()
    second = reader.capture(str(log), result)
    assert second.complete and second.digest != first.digest
    (early / "defs.svh").write_text("`define WIDTH 32\n")
    assert not second.current()


@pytest.mark.parametrize("text", ['`include `DYNAMIC\n', '`include "absent.svh"\n'])
def test_include_gaps_do_not_prove_semantic_identity(tmp_path, text):
    log, _, result = context(tmp_path, text)
    identity = DesignIdentityReader().capture(str(log), result)
    assert identity.complete  # exact bodies consumed by the lexical scanner
    assert not identity.include_resolution_complete


def test_missing_scanned_source_has_incomplete_identity(tmp_path):
    log, source, result = context(tmp_path)
    source.unlink()
    assert not DesignIdentityReader().capture(str(log), result).complete


def test_coarsened_stat_identity_cannot_hide_content_change(tmp_path, monkeypatch):
    from src.compile_session_snapshot import FileContentSnapshot
    log, source, result = context(tmp_path, "module top; wire a; endmodule\n")
    reader = DesignIdentityReader()
    first = reader.capture(str(log), result)
    monkeypatch.setattr(FileContentSnapshot, "current", lambda _: True)
    source.write_text("module top; wire b; endmodule\n")
    assert not first.current()
    assert reader.capture(str(log), result).digest != first.digest


@pytest.mark.anyio
async def test_hit_returns_copy_and_rules_are_not_reexecuted(tmp_path):
    log, source, context_ = context(tmp_path)
    reader = DesignIdentityReader()
    runtime = StructuralScanRuntime()
    calls = 0
    async def build():
        nonlocal calls
        calls += 1
        return {"risks": [{"detail": "all evidence"}], "coverage_status": "complete"}
    identity = reader.capture(str(log), context_)
    first, disposition = await runtime.run(identity, {}, build)
    first["risks"].clear()
    second, disposition = await runtime.run(identity, {}, build)
    assert second["risks"] and disposition == "hit_memory" and calls == 1
    await runtime.run(identity, {"categories": []}, build)
    assert calls == 2
    source.write_text("changed")
    await runtime.run(reader.capture(str(log), context_), {}, build)
    assert calls == 3


@pytest.mark.anyio
async def test_shared_build_survives_one_cancel_and_final_cancel_stops(tmp_path):
    log, _, result = context(tmp_path)
    identity = DesignIdentityReader().capture(str(log), result)
    runtime = StructuralScanRuntime()
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    async def build():
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"risks": []}
    a = asyncio.create_task(runtime.run(identity, {}, build))
    await entered.wait()
    b = asyncio.create_task(runtime.run(identity, {}, build))
    for _ in range(1000):
        if next(iter(runtime._flights.values())).waiters == 2:
            break
        await asyncio.sleep(.001)
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    assert not cancelled.is_set()
    release.set()
    _, disposition = await b
    assert calls == 1 and disposition == "coalesced"
    entered.clear(); release.clear()
    c = asyncio.create_task(runtime.run(identity, {"other": True}, build))
    await entered.wait()
    c.cancel()
    with pytest.raises(asyncio.CancelledError):
        await c
    await asyncio.wait_for(cancelled.wait(), 1)
    assert not runtime._flights


@pytest.mark.anyio
async def test_changed_during_build_and_capacity_bypass(tmp_path):
    log, source, result = context(tmp_path)
    identity = DesignIdentityReader().capture(str(log), result)
    runtime = StructuralScanRuntime(max_bytes=100)
    async def build():
        source.write_text("changed")
        return {"coverage_status": "complete", "risks": []}
    output, _ = await runtime.run(identity, {}, build)
    assert output["coverage_status"] == "degraded"
    assert runtime.metrics()["result_cache_bytes"] == 0
    current = DesignIdentityReader().capture(str(log), result)
    async def large():
        return {"evidence": "x" * 1000}
    output, _ = await runtime.run(current, {}, large)
    assert len(output["evidence"]) == 1000
    assert runtime.metrics()["result_cache_bytes"] == 0


@pytest.mark.anyio
async def test_dispatch_hit_skips_scan_and_source_preload(tmp_path, monkeypatch):
    import server
    from src.compile_source_index import CompileSourceIndex
    log, _, result = context(tmp_path)
    monkeypatch.setattr(server, "_parse_merged_compile_context", lambda **_: (result, "vcs"))
    monkeypatch.setattr(server, "_structural_scan_runtime", StructuralScanRuntime())
    monkeypatch.setattr(server, "_design_identity_reader", DesignIdentityReader())
    monkeypatch.setenv("TRACEWEAVE_STRUCTURAL_SCAN_CACHE", "1")
    args = {"compile_log": str(log), "simulator": "vcs"}
    first = await server._dispatch("scan_structural_risks", args)
    def unexpected(*args, **kwargs):
        raise AssertionError("warm hit must not execute rules or preload source text")
    monkeypatch.setattr(server, "scan_structural_risks", unexpected)
    monkeypatch.setattr(CompileSourceIndex, "preload", unexpected)
    second = await server._dispatch("scan_structural_risks", args)
    assert second.risks == first.risks
    assert second.scan_metrics["result_cache_disposition"] == "hit_memory"
    assert second.scan_metrics["rule_execution_count"] == 0
