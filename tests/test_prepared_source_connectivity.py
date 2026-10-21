"""Real Slang/public prepared queries; hand-written HDL oracles, no licensed build."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback

import pytest

import server
from src import prepared_source_connectivity as prepared_source
from src import source_graph_production, source_graph_runtime, tb_hierarchy_builder
from src.source_graph_contract import SemanticScopeSearch, compare_source_graph_artifact_scopes

FIXTURES = Path(__file__).parent / "fixtures" / "prepared_source"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    import pyslang  # Required qualification; an unavailable frontend fails.
    server.reset_session_state()
    source_graph_production._reset_source_graph_runtime_for_tests()
    for name, value in {"TRACEWEAVE_NPI_EXECUTION": "local", "TRACEWEAVE_CONNECTIVITY_ROUTE": "auto",
                        "TRACEWEAVE_AUTO_KDB": "0", "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE": "0",
                        "TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION": "0"}.items():
        monkeypatch.setenv(name, value)
    def forbidden(*a, **kw):
        raise AssertionError("prepared query attempted a lexical hierarchy or licensed build")
    monkeypatch.setattr(server, "build_hierarchy", forbidden)
    monkeypatch.setattr(tb_hierarchy_builder, "build_hierarchy", forbidden)
    monkeypatch.setattr(server, "build_bounded_connectivity_context", forbidden)
    monkeypatch.setattr(server, "_resolve_hierarchy_context", forbidden)
    from src import kdb_builder
    monkeypatch.setattr(kdb_builder, "build_kdb", forbidden)
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *a, **kw: None)
    # Capture actual worker ingress before runtime decoding. Native frontend
    # objects are never inspected, synthesized or rewritten by this wrapper.
    evidence_root = tmp_path / "cache_load_evidence"
    capture_failed = threading.Event()
    original_load = source_graph_runtime.SourceGraphRuntime._load_cache_entry

    def capture_load(runtime, request, key, worker, **kwargs):
        directory = None
        inputs_saved = False
        def capture_error():
            capture_failed.set()
            if directory is not None:
                try: (directory / "capture_error.txt").write_text(traceback.format_exc())
                except BaseException: pass
        try:
            evidence_root.mkdir(exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="load-", dir=evidence_root))
            if worker.ir_json_bytes is not None:
                (directory / "ir.raw.json").write_bytes(worker.ir_json_bytes)
            (directory / "request.json").write_text(json.dumps(request.to_dict()) + "\n")
            (directory / "key.json").write_text(json.dumps(key.to_dict()) + "\n")
            (directory / "worker.json").write_text(json.dumps({
                "status": worker.status.value, "ir_fingerprint_sha256": worker.ir_fingerprint_sha256,
                "ir_is_none": worker.ir_json_bytes is None,
                "scope_receipt": worker.scope_receipt.to_dict() if worker.scope_receipt is not None else None,
                "metrics": worker.metrics.to_dict(), "projection_receipt": worker.projection_receipt,
                "fallback_used": worker.fallback_used,
                "blocker": worker.blocker.to_dict() if worker.blocker is not None else None}) + "\n")
            inputs_saved = True
        except BaseException:
            capture_error()

        def finish(outcome, error=None, original_traceback=None):
            try:
                if directory is None: return
                (directory / "result.json").write_text(json.dumps({"outcome": outcome}) + "\n")
                if error is not None:
                    (directory / "exception.json").write_text(json.dumps({
                        "module": type(error).__module__, "type": type(error).__name__, "message": str(error)}) + "\n")
                    (directory / "exception.txt").write_text(original_traceback)
                if inputs_saved:
                    files = {path.name: {"bytes": path.stat().st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in directory.iterdir() if path.is_file()}
                    (directory / "manifest.json").write_text(json.dumps({"outcome": outcome, "files": files}) + "\n")
            except BaseException:
                capture_error()
        try:
            result = original_load(runtime, request, key, worker, **kwargs)
        except BaseException as exc:
            finish("raised", exc, traceback.format_exc())
            raise
        finish("returned")
        return result

    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, "_load_cache_entry", capture_load)
    yield
    server.reset_session_state()
    source_graph_production._reset_source_graph_runtime_for_tests()
    # Evidence errors fail the test independently of the original product
    # return/exception; incomplete capture cannot silently qualify a result.
    assert not capture_failed.is_set(), "worker ingress evidence capture failed"
    for directory in evidence_root.iterdir() if evidence_root.exists() else ():
        manifest = json.loads((directory / "manifest.json").read_text())
        files = manifest["files"]
        assert {"request.json", "key.json", "worker.json", "result.json"} <= files.keys()
        worker = json.loads((directory / "worker.json").read_text())
        assert ("ir.raw.json" in files) is not worker["ir_is_none"]
        request = json.loads((directory / "request.json").read_text())
        assert request["contract_version"] and request["artifact_identity"] and request["scope"]
        assert worker["scope_receipt"] is not None and worker["ir_fingerprint_sha256"]
        assert json.loads((directory / "result.json").read_text())["outcome"] == manifest["outcome"]
        if manifest["outcome"] == "raised":
            assert {"exception.json", "exception.txt"} <= files.keys()
            assert json.loads((directory / "exception.json").read_text())["type"]
            assert (directory / "exception.txt").read_text()
        else:
            assert manifest["outcome"] == "returned"
        assert not (directory / "capture_error.txt").exists()
        assert set(files) == {path.name for path in directory.iterdir() if path.is_file()} - {"manifest.json"}
        for name, expected in files.items():
            raw = (directory / name).read_bytes()
            assert len(raw) == expected["bytes"] and hashlib.sha256(raw).hexdigest() == expected["sha256"]


async def public(tmp, name, args):
    # Raw evidence precedes decoding and assertions, including invalid JSON.
    with (tmp / "public.raw.jsonl").open("a") as out:
        out.write(json.dumps({"phase": "request", "tool": name, "arguments": args}) + "\n")
    try:
        blocks = await server.call_tool(name, args)
    except BaseException as exc:
        with (tmp / "public.raw.jsonl").open("a") as out:
            out.write(json.dumps({"phase": "exception", "tool": name,
                "module": type(exc).__module__, "type": type(exc).__name__, "message": str(exc),
                "traceback": traceback.format_exc()}) + "\n")
        raise
    with (tmp / "public.raw.jsonl").open("a") as out:
        out.write(json.dumps({"phase": "response_repr", "tool": name, "response": repr(blocks)}) + "\n")
    try:
        with (tmp / "public.raw.jsonl").open("a") as out:
            out.write(json.dumps({"phase": "response", "tool": name,
                                  "blocks": [block.model_dump(mode="json") for block in blocks]}) + "\n")
        value = json.loads(blocks[0].text)
    except BaseException as exc:
        with (tmp / "public.raw.jsonl").open("a") as out:
            out.write(json.dumps({"phase": "decode_exception", "tool": name,
                "module": type(exc).__module__, "type": type(exc).__name__, "message": str(exc),
                "traceback": traceback.format_exc()}) + "\n")
        raise
    trace = tmp / "public.jsonl"
    with trace.open("a") as out:
        out.write(json.dumps({"tool": name, "arguments": args, "response": value}) + "\n")
    return value


def project(tmp, stem="a", top="p4_s9_a_tb"):
    tmp.mkdir(exist_ok=True)
    source = tmp / (stem + ".sv")
    source.write_bytes((FIXTURES / source.name).read_bytes())
    log = tmp / "compile.log"
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top {top}\n"
                   f"Parsing design file '{source}'\nTop Level Modules:\n       {top}\n")
    return source, log


async def prepare(tmp, log, policy="auto", top=None):
    value = await public(tmp, "prepare_design_context", {
        "compile_log": str(log), "backend_preference": policy,
        **({"top_hint": top} if top else {})})
    assert "error_code" not in value, value
    return value["design_handle"]


async def path(tmp, handle, src, dst, **kwargs):
    return await public(tmp, "trace_signal_path", {"design_handle": handle,
        "from_signal": src, "to_signal": dst, "expand_assigns": True, "timeout_sec": 60, **kwargs})


def source_positive(value):
    assert value.get("backend") == "source_graph", value
    assert value.get("backend_status", {}).get("actual_backend") == "source_graph", value
    assert value["backend_status"]["single_backend_provenance"] is True
    assert value["backend_status"]["source_graph"]["single_artifact_provenance"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("policy", ["auto", "source"])
@pytest.mark.parametrize("old_global", [False, True])
async def test_g1_same_scope_three_public_queries(tmp_path, policy, old_global):
    _, log = project(tmp_path)
    if old_global:
        server._result_cache["build_tb_hierarchy"] = {"component_tree": {"p4_s9_a_tb": {"dut": {"class": "wrong_same_name", "file": "/wrong/a.sv", "children": {}}}}, "compile_result": {"top_modules": ["p4_s9_a_tb"], "files": {"user": [{"path": "/wrong/a.sv"}]}}}
    h = await prepare(tmp_path, log, policy)
    assert not source_graph_production.source_graph_runtime_created()
    p = await path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")
    source_positive(p)
    assert p["found"] and p["path"][0]["net_path"] == "p4_s9_a_tb.dut.q1[7:0]"
    assert p["path"][-1]["net_path"] == "p4_s9_a_tb.dut.result[7:0]"
    assert p["path"][-1]["source_line"] == 9
    d = await public(tmp_path, "explain_signal_driver", {"design_handle": h,
        "signal_path": "p4_s9_a_tb.dut.result", "wave_path": "/unused.vcd", "output_format": "full", "timeout_sec": 60})
    source_positive(d)
    assert d["source_line"] == 9 and d["driver_kind"] == "continuous_assign", d
    l = await public(tmp_path, "find_signal_loads", {"design_handle": h,
        "signal_path": "p4_s9_a_tb.dut.q1", "timeout_sec": 60})
    source_positive(l)
    assert any(row.get("kind") == "rhs_expr" and row.get("source_line") == 9 for row in l["loads"]), l
    repeat = await path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")
    assert repeat["found"]
    assert repeat["backend_status"]["source_graph"]["effective_timeout_sec"] <= 60


@pytest.mark.anyio
async def test_g1_cross_and_required_middle_instance(tmp_path):
    _, log = project(tmp_path, "chain", "chain_top")
    h = await prepare(tmp_path, log)
    cross = await path(tmp_path, h, "chain_top.src.y[3:0]", "chain_top.mid.a[3:0]")
    source_positive(cross)
    assert cross["found"]
    assert [r["net_path"] for r in cross["path"]] == ["chain_top.src.y[3:0]", "chain_top.left[3:0]", "chain_top.mid.a[3:0]"]
    result = await path(tmp_path, h, "chain_top.src.y[3:0]", "chain_top.dst.y[7:4]")
    source_positive(result)
    assert result["found"], result
    assert [r["net_path"] for r in result["path"]] == ["chain_top.src.y[3:0]", "chain_top.left[3:0]", "chain_top.mid.a[3:0]",
        "chain_top.mid.y[7:4]", "chain_top.right[7:4]", "chain_top.dst.a[7:4]", "chain_top.dst.y[7:4]"]
    assert all(r["exact_bit_mapping"] for r in result["path"][1:])
    sg = result["backend_status"]["source_graph"]
    assert sg["scope_expansion_count"] >= 1 and len(set(sg["attempted_artifact_fingerprints_sha256"])) >= 2
    assert sg["semantic_instances_charged"] <= 64 and sg["semantic_members_charged"] <= 4096


@pytest.mark.anyio
async def test_g1_generate_specialization_and_packed_field(tmp_path):
    _, log = project(tmp_path, "packed", "top")
    h = await prepare(tmp_path, log)
    for width, bits, physical in [(16, "15:8", "19:12"), (24, "23:16", "27:20")]:
        r = await path(tmp_path, h, f"top.rsp.data[{bits}]", f"top.g{width}.u_lane.data_o[{bits}]")
        source_positive(r)
        assert r["found"], r
        assert r["path"][0]["net_path"] == f"top.rsp[{physical}]"
        assert r["path"][-1]["net_path"] == f"top.g{width}.u_lane.data_o[{bits}]"
        assert r["path"][-1]["source_line"] == 13
        assert all(row["exact_bit_mapping"] for row in r["path"][1:])


@pytest.mark.anyio
async def test_g1_controlled_builder_failure_keeps_handle(tmp_path, monkeypatch):
    from src import kdb_builder
    _, log = project(tmp_path)
    h = await prepare(tmp_path, log)
    def fail_builder(*a, **kw):
        raise RuntimeError("controlled builder failure; no executable launched")
    monkeypatch.setattr(kdb_builder, "build_kdb", fail_builder)
    response = await public(tmp_path, "build_kdb", {"compile_log": str(log), "simulator": "vcs"})
    assert 'controlled builder failure' in response.get('error', ''), response
    r = await path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")
    assert r["found"] and r["backend_status"]["design_context"]["design_handle"] == h


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source", "same_size_mtime", "options", "top", "order", "include"])
async def test_g3_input_change_rejects_old_context(tmp_path, change):
    source, log = project(tmp_path)
    extra = tmp_path / "other.sv"
    extra.write_text("module other(input logic a, output logic y); assign y=a; endmodule\n")
    header = tmp_path / "values.svh"
    header.write_text("`define V 1\n")
    source.write_text('`include "values.svh"\n' + source.read_text())
    evidence = f"Parsing design file '{source}'\nParsing design file '{extra}'\n"
    log.write_text(log.read_text().replace(f"{source} -top", f"+incdir+{tmp_path} {source} {extra} -top")
                   .replace(f"Parsing design file '{source}'\n", evidence))
    def save_inputs(name):
        records = {}
        for file in (source, extra, header, log):
            stat = file.stat()
            records[str(file)] = {"sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns,
                "inode": stat.st_ino}
        (tmp_path / name).write_text(json.dumps(records, indent=2))
    save_inputs("inputs.before.json")
    h = await prepare(tmp_path, log)
    if change in {"source", "same_size_mtime"}:
        before = source.stat()
        source.write_text(source.read_text().replace("q1 ^ mask", "q1 | mask"))
        if change == "same_size_mtime": os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "include": header.write_text("`define V 2\n")
    elif change == "options": log.write_text(log.read_text().replace("-sverilog", "-sverilog +define+ALT"))
    elif change == "top": log.write_text(log.read_text().replace("p4_s9_a_tb", "other"))
    else:
        log.write_text(log.read_text().replace(f"{source} {extra}", f"{extra} {source}")
                       .replace(evidence, f"Parsing design file '{extra}'\nParsing design file '{source}'\n"))
    save_inputs("inputs.after.json")
    r = await path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")
    assert r.get("error_code") == "design_context_changed", r
    assert not source_graph_production.source_graph_runtime_created()
    fresh = await prepare(tmp_path, log)
    assert fresh != h
    result = await path(tmp_path, fresh, *( ("other.a", "other.y") if change == "top"
                        else ("p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")))
    assert result["found"], result
    source_positive(result)
    endpoint = result["path"][-1]
    assert Path(endpoint["source_file"]).resolve() == (extra if change == "top" else source).resolve()
    assert endpoint["source_line"] == (1 if change == "top" else 10)


@pytest.mark.anyio
async def test_g3_same_named_designs_do_not_share_answers(tmp_path):
    a, alog = project(tmp_path / "a")
    b, blog = project(tmp_path / "b")
    b.write_text(b.read_text().replace("q1 ^ mask", "mask"))
    ha, hb = await prepare(tmp_path, alog), await prepare(tmp_path, blog)
    good = await path(tmp_path, ha, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")
    bad = await path(tmp_path, hb, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result")
    assert good["found"] and not bad["found"]
    assert good["backend_status"]["source_graph"]["compile_fingerprint_sha256"] != bad["backend_status"]["source_graph"]["compile_fingerprint_sha256"]


@pytest.mark.anyio
async def test_g3_deadline_stops_before_next_stage(tmp_path, monkeypatch):
    _, log = project(tmp_path)
    h = await prepare(tmp_path, log)
    original = prepared_source.build_prepared_source_plan
    calls = []
    def slow(**kw):
        calls.append(1)
        time.sleep(.08)
        return original(**kw)
    monkeypatch.setattr(prepared_source, "build_prepared_source_plan", slow)
    r = await path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result", timeout_sec=.04)
    assert r.get("error_code") == "design_context_timeout", r
    assert not source_graph_production.source_graph_runtime_created()
    await asyncio.sleep(.1)  # Wait for the owned planning thread to observe cancellation.


@pytest.mark.anyio
async def test_g3_same_artifact_waiter_cancel_preserves_survivor(tmp_path, monkeypatch):
    _, log = project(tmp_path)
    h = await prepare(tmp_path, log)
    original = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def held(self, *a, **kw):
        calls.append(1); entered.set()
        await release.wait()
        return await original(self, *a, **kw)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, "run", held)
    one = asyncio.create_task(path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result"))
    await asyncio.wait_for(entered.wait(), 5)
    two = asyncio.create_task(path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result"))
    async def coalesced():
        while True:
            runtime = source_graph_production._PROCESS_SESSION._runtime
            if runtime is not None and any(f.waiter_count >= 2 for f in runtime._inflight.values()):
                return
            await asyncio.sleep(.005)
    await asyncio.wait_for(coalesced(), 5)
    one.cancel()
    with pytest.raises(asyncio.CancelledError): await one
    release.set()
    r = await asyncio.wait_for(two, 60)
    assert r["found"] and len(calls) == 1
    assert r["backend_status"]["source_graph"]["flight_disposition"] == "coalesced"


@pytest.mark.anyio
async def test_g3_last_waiter_cancels_real_worker(tmp_path, monkeypatch):
    _, log = project(tmp_path)
    h = await prepare(tmp_path, log)
    original = asyncio.create_subprocess_exec
    launched = asyncio.Event()
    processes = []
    async def observed(*a, **kw):
        proc = await original(*a, **kw)
        if any("source_graph_worker" in str(item) for item in a):
            processes.append(proc); launched.set()
            (tmp_path / "real_worker.json").write_text(json.dumps({"pid": proc.pid,
                "stat": Path(f"/proc/{proc.pid}/stat").read_text()}))
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed)
    task = asyncio.create_task(path(tmp_path, h, "p4_s9_a_tb.dut.q1", "p4_s9_a_tb.dut.result"))
    await asyncio.wait_for(launched.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    for proc in processes:
        await asyncio.wait_for(proc.wait(), 5)
        assert not Path(f"/proc/{proc.pid}").exists()
    assert processes


def test_g3_expansion_scope_key_and_coverage_are_exact():
    from tests.test_source_graph_runtime import _request
    request = _request()
    scope = request.artifact_identity.scope
    one = replace(scope, semantic_search=SemanticScopeSearch())
    two = replace(scope, semantic_search=SemanticScopeSearch((scope.top,), 1))
    assert compare_source_graph_artifact_scopes(one, two).value == "unproven"
    assert compare_source_graph_artifact_scopes(two, one).value == "unproven"
    assert compare_source_graph_artifact_scopes(two, type(two).from_dict(two.to_dict())).value == "exact"


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['worker', 'query', 'expansion'])
@pytest.mark.parametrize('stop', ['timeout', 'config', 'kdb'])
async def test_g3_phase_barriers_preserve_deadline_and_binding(tmp_path, monkeypatch, phase, stop):
    """Controlled phase barriers around real artifacts, never counterfeit facts."""
    from src.design_backend_binding import ExecutionBindingChanged
    from src.kdb_anchor import KdbBindingChanged
    _, log = project(tmp_path, 'chain', 'chain_top')
    h = await prepare(tmp_path, log)
    endpoints = ('chain_top.src.y[3:0]', 'chain_top.dst.y[7:4]')
    warm = await path(tmp_path, h, *endpoints)
    assert warm['found']
    entered, release = asyncio.Event(), asyncio.Event()
    trace = []
    changed = False
    cfg = server.get_source_graph_execution_config()
    original_binding = server._validate_design_binding
    async def binding(*a, **kw):
        if changed and stop == 'kdb': raise KdbBindingChanged()
        return await original_binding(*a, **kw)
    monkeypatch.setattr(server, '_validate_design_binding', binding)
    monkeypatch.setattr(server, 'get_source_graph_execution_config',
        lambda: replace(cfg, timeout_sec=cfg.timeout_sec + 1) if changed and stop == 'config' else cfg)
    original_prepare = source_graph_runtime.SourceGraphRuntime.prepare
    async def runtime(self, request, **kw):
        depth = request.artifact_identity.scope.semantic_search.depth
        trace.append(('prepare', depth, kw['timeout_seconds']))
        if phase == 'worker' or (phase == 'expansion' and depth > 0):
            entered.set(); await release.wait()
        return await original_prepare(self, request, **kw)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', runtime)
    original_query = server._call_signal_path_backend
    async def query(*a, **kw):
        trace.append(('query',))
        if phase == 'query': entered.set(); await release.wait()
        return await original_query(*a, **kw)
    monkeypatch.setattr(server, '_call_signal_path_backend', query)
    from src.connectivity_backend import StaticConnectivityBackend
    def forbidden(*a, **kw): raise AssertionError('typed stop advanced to Static')
    monkeypatch.setattr(StaticConnectivityBackend, 'find_path', forbidden)
    timeout = .8 if stop == 'timeout' else 60
    task = asyncio.create_task(path(tmp_path, h, *endpoints, timeout_sec=timeout))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        size = len(trace)
        if stop == 'timeout':
            result = await asyncio.wait_for(task, 2)
            assert result.get('error_code') == 'design_context_timeout', result
        else:
            changed = True; release.set()
            result = await asyncio.wait_for(task, 5)
            assert result.get('error_code') == ('design_context_execution_changed' if stop == 'config' else 'kdb_identity_changed'), result
        assert len(trace) == size, trace
        (tmp_path / 'phase_trace.json').write_text(json.dumps({'phase': phase, 'stop': stop, 'trace': trace, 'response': result}))
    finally:
        release.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('work', ['prefix', 'instance', 'member', 'edge'])
async def test_g3_cumulative_work_stops_without_static(tmp_path, monkeypatch, work):
    from src.connectivity_backend import StaticConnectivityBackend
    def forbidden(*a, **kw): raise AssertionError('budget stop advanced to Static')
    monkeypatch.setattr(StaticConnectivityBackend, 'find_path', forbidden)
    _, log = project(tmp_path, 'chain', 'chain_top')
    h = await prepare(tmp_path, log)
    endpoints = ('chain_top.src.y[3:0]', 'chain_top.dst.y[7:4]')
    calls = []
    original = source_graph_runtime.SourceGraphRuntime.prepare
    async def record(self, request, **kw):
        calls.append(request.artifact_identity.scope.semantic_search.to_dict())
        return await original(self, request, **kw)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', record)
    if work == 'prefix':
        endpoints = ('chain_top.' + '.'.join(['x'] * 65) + '.y', endpoints[1])
    elif work in ('instance', 'member'):
        original_account = prepared_source.SourceQueryBudget.account_projection
        def charged(self, entry):
            original_account(self, entry)
            # Control: a preceding projection has consumed the remaining work.
            if work == 'instance': self.instances = 0
            else: self.members = 0
        monkeypatch.setattr(prepared_source.SourceQueryBudget, 'account_projection', charged)
    else:
        original_account = prepared_source.SourceQueryBudget.account_query
        def charged(self, query):
            original_account(self, query)
            self.edges = 0  # Controlled cumulative exhaustion after real first query.
        monkeypatch.setattr(prepared_source.SourceQueryBudget, 'account_query', charged)
    result = await path(tmp_path, h, *endpoints)
    assert len(calls) == (0 if work == 'prefix' else 1), calls
    if work == 'prefix': assert result.get('error_code') == 'design_context_source_scope_limit', result
    else:
        assert not result['found'], result
        assert result['backend_status']['source_graph']['blocker']['code'] == 'source_query_budget_exceeded'
        assert result['claim_semantics']['negative_claim_allowed'] is False
    (tmp_path / 'budget_trace.json').write_text(json.dumps({'control': work, 'requests': calls, 'response': result}))


@pytest.mark.anyio
@pytest.mark.parametrize('incomplete', [False, True])
async def test_g3_prepared_scope_reuse_and_consumed_handoff(tmp_path, monkeypatch, incomplete):
    """Real worker data; incomplete manifest marker is an explicit control."""
    from src.source_graph_contract import compute_source_graph_artifact_key
    _, log = project(tmp_path, 'packed', 'top')
    h = await prepare(tmp_path, log)
    original_plan = prepared_source.build_prepared_source_plan
    if incomplete:
        def marked(**kwargs):
            plan = original_plan(**kwargs)
            request = plan.request
            source = replace(request.identity, compile_inputs=replace(
                request.identity.compile_inputs, options_complete=False))
            artifact = replace(request.artifact_identity, source=source)
            request = replace(request, identity=source, artifact=artifact)
            key = compute_source_graph_artifact_key(artifact)
            return replace(plan, request=request, receipt=replace(plan.receipt,
                manifest_complete=False, cross_request_reusable=False,
                artifact_fingerprint_sha256=key.digest))
        monkeypatch.setattr(prepared_source, 'build_prepared_source_plan', marked)
    calls = []
    original = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    async def record(self, request, **kw):
        calls.append({'scope': request.scope.to_dict(), 'key': compute_source_graph_artifact_key(request.artifact_identity).digest})
        return await original(self, request, **kw)
    # The runner receives the query-neutral artifact through the normal request contract.
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', record)
    receipts = []
    for width, bits in [(16, '15:8'), (16, '15:8'), (16, '15:8'), (24, '23:16')]:
        result = await path(tmp_path, h, f'top.rsp.data[{bits}]', f'top.g{width}.u_lane.data_o[{bits}]')
        assert result['found'], result
        receipts.append(result['backend_status']['source_graph'])
    assert len(calls) == (3 if incomplete else 2), calls
    assert calls[0]['key'] != calls[-1]['key']
    assert receipts[-1]['metrics']['actual_build_count'] == 1
    if incomplete:
        assert all(r['cache_disposition'] == 'bypass_incomplete_key' for r in receipts)
        assert receipts[1]['metrics']['actual_build_count'] == 0
        assert receipts[2]['metrics']['actual_build_count'] == 1
        runtime = source_graph_production._PROCESS_SESSION._runtime
        assert runtime.stats_snapshot()['cache_entry_count'] == 0
    else:
        assert receipts[1]['metrics']['actual_build_count'] == receipts[2]['metrics']['actual_build_count'] == 0
    (tmp_path / 'reuse_trace.json').write_text(json.dumps({'control_incomplete': incomplete, 'workers': calls, 'receipts': receipts}))


@pytest.mark.anyio
@pytest.mark.parametrize('limit', ['max_instances', 'max_members'])
async def test_g3_real_projection_limit_is_typed_stop(tmp_path, monkeypatch, limit):
    from src.connectivity_backend import StaticConnectivityBackend
    _, log = project(tmp_path)
    h = await prepare(tmp_path, log)
    original = prepared_source.build_prepared_source_plan
    def bounded(**kw):
        kw['search'] = SemanticScopeSearch(**{limit: 1})
        return original(**kw)
    monkeypatch.setattr(prepared_source, 'build_prepared_source_plan', bounded)
    def forbidden(*a, **kw): raise AssertionError('real projection limit fell back')
    monkeypatch.setattr(StaticConnectivityBackend, 'find_path', forbidden)
    result = await path(tmp_path, h, 'p4_s9_a_tb.dut.q1', 'p4_s9_a_tb.dut.result')
    assert result.get('error_code') == 'design_context_source_scope_limit', result
