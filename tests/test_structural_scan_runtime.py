import asyncio
from dataclasses import replace
import os
import threading

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


@pytest.mark.parametrize('present', [False, True])
def test_repeated_include_lookup_is_fresh_on_every_validation(tmp_path, monkeypatch, present):
    from src import design_identity
    early, late = tmp_path / 'early', tmp_path / 'late'
    early.mkdir(); late.mkdir()
    if present:
        (late / 'defs.svh').write_text('`define WIDTH 8\n')
    log, _, result = context(tmp_path, '`include "defs.svh"\n' * 8 + 'module top; endmodule\n')
    result['compile_command'] += f' +incdir+{early}+{late}'
    identity = DesignIdentityReader().capture(str(log), result)
    assert identity.complete and len(identity.includes) == 8
    original, calls = design_identity._resolve, []
    def resolve(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(design_identity, '_resolve', resolve)
    assert identity.current() and len(calls) == 1
    assert identity.current() and len(calls) == 2
    # The memo must retain neither a successful lookup nor an absent result
    # across validation passes, even when source bytes did not change.
    (early / 'defs.svh').write_text('`define WIDTH 16\n')
    assert not identity.current() and len(calls) == 3


def test_include_lookup_keeps_ordered_search_paths_distinct(tmp_path):
    from src import design_identity
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    (a / 'defs.svh').write_text('a')
    (b / 'defs.svh').write_text('b')
    log, _, result = context(tmp_path)
    identity = DesignIdentityReader().capture(str(log), result)
    ab, ba = (str(a), str(b)), (str(b), str(a))
    includes = tuple(('defs.svh', dirs, design_identity._resolve('defs.svh', dirs))
                     for dirs in [ab, ba, ab, ba])
    assert replace(identity, includes=includes).current()


@pytest.mark.parametrize('capacity', [0, 1])
def test_include_lookup_capacity_bypass_still_checks_every_choice(tmp_path, monkeypatch, capacity):
    from src import design_identity
    log, _, result = context(tmp_path)
    (tmp_path / 'known.svh').write_text('known')
    identity = DesignIdentityReader().capture(str(log), result)
    dirs = (str(tmp_path),)
    includes = (('known.svh', dirs, str(tmp_path / 'known.svh')),
                ('absent.svh', dirs, None)) * 3
    identity = replace(identity, includes=includes)
    original, calls = design_identity._resolve, []
    def resolve(name, directories):
        calls.append(name)
        return original(name, directories)
    monkeypatch.setattr(design_identity, '_resolve', resolve)
    monkeypatch.setattr(design_identity, '_INCLUDE_LOOKUP_MEMO_LIMIT', capacity)
    assert identity.current()
    assert calls.count('absent.svh') == 3  # never memoized at these capacities
    (tmp_path / 'absent.svh').write_text('new candidate')
    assert not identity.current()


def test_include_memo_hits_observe_cancellation_and_never_publish_a_memo(tmp_path, monkeypatch):
    from src import cancellation, design_identity
    log, _, result = context(tmp_path, '`include "absent.svh"\n' * 8 + 'module top; endmodule\n')
    identity = DesignIdentityReader().capture(str(log), result)
    original = design_identity._resolve
    event = threading.Event()
    def resolve(*args):
        actual = original(*args)
        event.set()
        return actual
    token = cancellation.push_cancel_event(event)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(design_identity, '_resolve', resolve)
            with pytest.raises(cancellation.OperationCancelled):
                identity.current()
    finally:
        cancellation.pop_cancel_event(token)
    assert identity.current()


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


def test_exact_identity_reuse_still_checks_bytes_and_include_shadowing(tmp_path, monkeypatch):
    early, late = tmp_path / 'early', tmp_path / 'late'
    early.mkdir(); late.mkdir()
    (late / 'defs.svh').write_text('`define WIDTH 8\n')
    log, source, result = context(tmp_path, '`include "defs.svh"\nmodule top; endmodule\n')
    result['compile_command'] += f' +incdir+{early}+{late}'
    reader = DesignIdentityReader()
    first = reader.capture(str(log), result)
    # A hit bypasses graph reconstruction; option replay and content hashing
    # remain mandatory to detect directory aliases and external path bindings.
    original = reader._read
    monkeypatch.setattr(reader, '_read', lambda *a: pytest.fail('rebuilt unchanged graph'))
    assert reader.capture(str(log), result) is first
    monkeypatch.setattr(reader, '_read', original)
    (early / 'defs.svh').write_text('`define WIDTH 16\n')
    second = reader.capture(str(log), result)
    assert second.digest != first.digest and second is not first
    from src.compile_session_snapshot import FileContentSnapshot
    monkeypatch.setattr(FileContentSnapshot, 'current', lambda _: True)
    source.write_text('`include "defs.svh"\nmodule top; wire a; endmodule\n')
    assert reader.capture(str(log), result).digest != second.digest


def test_identity_memo_is_bounded_and_never_retains_incomplete_capture(tmp_path):
    log, source, result = context(tmp_path)
    reader = DesignIdentityReader(max_entries=2)
    for i in range(8):
        reader.capture(str(log), {**result, 'compile_command': result['compile_command'] + f' +define+V={i}'})
    assert len(reader._identities) == 2
    assert reader._identity_bytes == sum(n for _, n in reader._identities.values())
    assert reader._identity_bytes <= reader._identity_max_bytes
    bypass = DesignIdentityReader(max_bytes=1)
    assert bypass.capture(str(log), result).complete and not bypass._identities
    source.unlink()
    incomplete = DesignIdentityReader()
    assert not incomplete.capture(str(log), result).complete and not incomplete._identities


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


@pytest.mark.anyio
async def test_identity_lease_survives_hierarchy_completion_on_scan_miss(tmp_path, monkeypatch):
    import server
    from src.compile_source_runtime import CompileSourceIndexRuntime, compile_source_index_key
    log, _, result = context(tmp_path)
    monkeypatch.setattr(server, "_parse_merged_compile_context", lambda **_: (result, "vcs"))
    monkeypatch.setattr(server, "_structural_scan_runtime", StructuralScanRuntime())
    runtime = CompileSourceIndexRuntime()
    monkeypatch.setattr(server, "_compile_source_index_runtime", runtime)
    monkeypatch.setenv("TRACEWEAVE_STRUCTURAL_SCAN_CACHE", "1")
    monkeypatch.setenv("TRACEWEAVE_COMPILE_SOURCE_INDEX", "1")
    config = server.get_compile_source_index_config()
    key, paths = compile_source_index_key(
        compile_snapshot_sha256=server.compute_snapshot_fingerprint(str(log), "vcs"),
        compile_result=result,
    )
    hierarchy_lease = await runtime.acquire(key=key, paths=paths,
        max_bytes=config.max_bytes, max_files=config.max_files)
    entered = asyncio.Event()
    resume = threading.Event()
    loop = asyncio.get_running_loop()
    reader = DesignIdentityReader()
    class GatedIdentityReader:
        def capture(self, *args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert resume.wait(5)
            return reader.capture(*args, **kwargs)
    monkeypatch.setattr(server, "_design_identity_reader", GatedIdentityReader())
    task = asyncio.create_task(server._dispatch("scan_structural_risks", {
        "compile_log": str(log), "simulator": "vcs",
    }))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Hierarchy finishes while scan still owns the shared source lease.
        await hierarchy_lease.release()
        resume.set()
        scan = await asyncio.wait_for(task, 5)
        assert scan.scan_metrics["rule_execution_count"] == 1
        assert runtime.metrics_snapshot()["compile_source_runtime_build_count"] == 1
        assert runtime.metrics_snapshot()["compile_source_runtime_active_session_count"] == 0
    finally:
        resume.set()
        await hierarchy_lease.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('change', ['environment', 'directory_alias'])
def test_identity_reuse_refreshes_external_include_search_paths(tmp_path, monkeypatch, change):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    (a / 'defs.svh').write_text('`define WIDTH 8\n')
    (b / 'defs.svh').write_text('`define WIDTH 16\n')
    log, source, result = context(tmp_path, '`include "defs.svh"\nmodule top; endmodule\n')
    alias = tmp_path / 'include'
    if change == 'environment':
        monkeypatch.setenv('TW_IDENTITY_INCLUDE', str(a))
        result['compile_command'] += ' +incdir+$TW_IDENTITY_INCLUDE'
    else:
        alias.symlink_to(a, target_is_directory=True)
        result['compile_command'] += f' +incdir+{alias}'
    reader = DesignIdentityReader()
    first = reader.capture(str(log), result)
    assert first.complete and reader.capture(str(log), result) is first
    if change == 'environment':
        monkeypatch.setenv('TW_IDENTITY_INCLUDE', str(b))
    else:
        alias.unlink();alias.symlink_to(b, target_is_directory=True)
    second = reader.capture(str(log), result)
    assert second.complete and second.digest != first.digest
    assert any(r.path == str(b / 'defs.svh') for _, r in second.records)
