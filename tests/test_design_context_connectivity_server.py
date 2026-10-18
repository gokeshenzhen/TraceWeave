"""Public fixed-context routing with real text/maps and control-only backends.

No native, scheduler or semantic frontend runs. Spy success/load labels test
serialization and routing only; they do not establish NPI/LSF correctness, KDB
source association, complete source planning, performance or D1 completion.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import threading

import anyio
import pytest

import config
import server
from src import connectivity_backend, design_backend_binding, kdb_anchor, kdb_builder
from src import structural_scanner, tb_hierarchy_builder, verdi_npi_backend, wave_design_binding
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context import DesignContext
from src.design_context_runtime import ContextUnavailable, DesignContextStore, prepare_design_context
from src.kdb_anchor import KdbBindingChanged


TOOLS = ("explain_signal_driver", "find_signal_loads", "trace_signal_path")


def _forbidden(*args, **kwargs):
    pytest.fail("fixed-context fixture reached hierarchy, scan, frontend, native or process work")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_server(monkeypatch, tmp_path):
    server.reset_session_state()
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.delenv("TRACEWEAVE_NPI_WORKER", raising=False)
    monkeypatch.setattr(config, "NPI_ALLOW_DEGRADED_KDB", True)
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    for module, name in ((server, "build_hierarchy"), (server, "scan_structural_risks"),
                         (tb_hierarchy_builder, "build_hierarchy"), (structural_scanner, "scan_structural_risks"),
                         (verdi_npi_backend, "_import_pynpi"), (kdb_builder, "build_kdb"),
                         (subprocess, "Popen"), (subprocess, "run"),
                         (server, "build_source_graph_plan"), (server, "build_source_graph_path_plan")):
        monkeypatch.setattr(module, name, _forbidden)
    source_config = replace(server.get_source_graph_execution_config(), enabled=False, error_code=None)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda: source_config)
    # Driver/load bounded fallback must never start a whole-source Static scan.
    monkeypatch.setattr(connectivity_backend.StaticConnectivityBackend, "find_driver", _forbidden)
    monkeypatch.setattr(connectivity_backend.StaticConnectivityBackend, "find_loads", _forbidden)
    yield
    server.reset_session_state()


def _project(root, *, simulator="vcs", candidate=True):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "top.sv"
    source.write_text("module top(input logic a, output logic y); assign y=a; endmodule\n")
    log = root / "compile.log"
    if simulator == "verilator":
        text = f"make: Entering directory '{root}'\nverilator --cc {source} --top-module top\n"
    else:
        text = (f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top\n"
                f"Parsing design file '{source}'\nTop Level Modules:\n       top\n")
    log.write_text(text)
    path = root / "simv.daidir" / "kdb.elab++"
    if candidate:
        path.mkdir(parents=True)
        (path / "_libFileMap").write_bytes(b"lib_A")
        (path / "_stnElabDB").write_bytes(b"elab_A")
    return source, log, path


async def _public(name, arguments):
    blocks = await server.call_tool(name, arguments)
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


async def _prepare(log, **kwargs):
    result = await _public("prepare_design_context", {"compile_log": str(log), **kwargs})
    assert "error_code" not in result, result
    return result["design_handle"], result


def _arguments(tool, handle=None, **extra):
    args = ({"from_signal": "top.a", "to_signal": "top.y"} if tool == "trace_signal_path"
            else {"signal_path": "top.y"})
    if tool == "explain_signal_driver":
        args.update(wave_path="/control_only/unused.vcd", output_format="full")
    if handle is not None:
        args["design_handle"] = handle
    return {**args, **extra}


class ControlNpi:
    name = "verdi_npi"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self, owner):
        self.owner = owner
        self.compile_result = self.anchor = None

    @property
    def kdb_status(self):
        return None if self.owner.failure_reason else {"load_quality": "clean"}

    def bind_compile_context(self, metadata):
        self.compile_result = deepcopy(metadata)
        self.owner.binds.append(("compile", deepcopy(metadata)))

    def bind_kdb_anchor(self, anchor):
        self.anchor = anchor
        self.owner.binds.append(("anchor", anchor))

    def _query(self, operation, kwargs):
        self.owner.calls.append((operation, deepcopy(kwargs), self.compile_result, self.anchor))
        if self.owner.action:
            self.owner.action()
        if operation == "driver":
            result = {"signal_path": kwargs["signal_path"], "wave_path": kwargs["wave_path"],
                      "resolved_rtl_name": "y", "resolved_module": "top", "driver_status": "resolved",
                      "driver_kind": "assign", "source_file": "top.sv", "source_line": 1,
                      "expression_summary": "control positive driver", "recursive": False, "backend": self.name}
        elif operation == "loads":
            result = {"signal_path": kwargs["signal_path"], "resolved_rtl_name": "y", "resolved_module": "top",
                      "loads": [], "completeness": "exact", "stopped_at": "no_npi_loads", "backend": self.name}
        else:
            result = {"from_signal": kwargs["from_signal"], "to_signal": kwargs["to_signal"],
                      "found": True, "hops": 1, "path": [
                          {"index": 0, "net_path": kwargs["from_signal"], "is_endpoint": True, "backend": self.name},
                          {"index": 1, "net_path": kwargs["to_signal"], "is_endpoint": True, "backend": self.name}],
                      "expand_assigns": kwargs.get("expand_assigns", False), "backend": self.name}
        if self.owner.failure_reason:
            result["_npi_fallback_reason"] = self.owner.failure_reason
        return result

    def find_driver(self, **kwargs):
        return self._query("driver", kwargs)

    def find_loads(self, **kwargs):
        return self._query("loads", kwargs)

    def find_path(self, **kwargs):
        return self._query("path", kwargs)


class BackendControls:
    def __init__(self):
        self.selections, self.calls, self.binds = [], [], []
        self.action = self.selector_action = self.failure_reason = None
        self.external = False

    def select(self, status, *, fallback):
        self.selections.append(deepcopy(status))
        if self.selector_action:
            self.selector_action()
        backend = ControlNpi(self)
        backend.uses_external_worker = self.external
        return backend


@pytest.fixture
def backend_controls(monkeypatch):
    controls = BackendControls()
    monkeypatch.setattr(connectivity_backend, "select_backend", controls.select)
    return controls


async def _event(event):
    with anyio.fail_after(5):
        while not event.is_set():
            await anyio.sleep(0.001)


@pytest.mark.anyio
async def test_three_tool_schemas_allow_explicit_handle_without_compile_log():
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in TOOLS:
        schema = tools[name].inputSchema
        assert schema["properties"]["design_handle"]["minLength"] == 1
        assert "compile_log" not in schema["required"]
        assert {"required": ["design_handle"]} in schema["anyOf"]
        assert {"required": ["compile_log"]} in schema["anyOf"]


@pytest.mark.anyio
async def test_public_prepare_declares_candidate_and_recovery_path_without_probe(tmp_path, monkeypatch):
    _, log, candidate = _project(tmp_path)
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", _forbidden)
    monkeypatch.setattr(design_backend_binding, "capture_kdb_anchor", _forbidden)
    handle, receipt = await _prepare(log, kdb_path=str(candidate))
    assert handle.startswith("dc_") and receipt["kdb_status"] == "not_checked"
    assert receipt["semantic_status"] == "not_run"
    assert receipt["recovery_context"]["kdb_path"] == str(candidate)
    with server._design_context_store.pin(handle) as entry:
        assert entry.binding is None and entry.context.requested_kdb_path == str(candidate)


@pytest.mark.anyio
@pytest.mark.parametrize("tool", TOOLS)
async def test_handle_query_has_no_hierarchy_prerequisite_and_receipts_are_honest(tmp_path, monkeypatch, backend_controls, tool):
    _, log, candidate = _project(tmp_path)
    handle, prepared = await _prepare(log)
    original = server.prepare_backend_binding
    discoveries = []
    def observe(context):
        discoveries.append(threading.get_ident())
        return original(context)
    monkeypatch.setattr(server, "prepare_backend_binding", observe)
    first = await _public(tool, _arguments(tool, handle))
    second = await _public(tool, _arguments(tool, handle))
    assert first["backend"] == second["backend"] == "verdi_npi"
    assert len(discoveries) == 1 and len(backend_controls.calls) == 2
    assert all(call[2]["top_modules"] == ["top"] and call[3].path == str(candidate) for call in backend_controls.calls)
    for index, result in enumerate((first, second)):
        receipt = result["backend_status"]["design_context"]
        assert receipt["design_handle"] == handle
        assert receipt["context_identity_sha256"] == prepared["context_identity_sha256"]
        assert receipt["binding_reused"] is bool(index)
        assert receipt["source_snapshot_status"] == receipt["include_resolution_status"] == "complete"
        assert receipt["artifact"]["source_association"] == "unverified"
        assert receipt["artifact"]["identity_basis"] == "bounded_elaboration_maps"
        assert receipt["artifact"]["load_status"] == "loaded"  # Control backend receipt only.
        metrics = receipt["metrics"]
        assert metrics["source_validation_ms"] > 0 and metrics["binding_validation_ms"] > 0
        assert metrics["final_validation_ms"] > 0 and metrics["total_ms"] >= metrics["query_ms"]
        if index:
            for field in ("discovery_ms", "discovery_read_bytes", "discovery_file_count", "discovery_entry_count", "anchor_ms", "binding_ms"):
                assert metrics[field] == 0
    assert server._session_state["get_sim_paths"] is server._session_state["build_tb_hierarchy"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("policy", ["source", "route"])
@pytest.mark.parametrize("tool", TOOLS)
async def test_source_policy_skips_npi_and_keeps_fallback_coverage_explicit(tmp_path, monkeypatch, backend_controls, policy, tool):
    _, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log, backend_preference="source" if policy == "source" else "auto")
    if policy == "route":
        monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", _forbidden)
    monkeypatch.setattr(design_backend_binding, "capture_kdb_anchor", _forbidden)
    result = await _public(tool, _arguments(tool, handle))
    assert not backend_controls.selections and not backend_controls.calls
    receipt = result["backend_status"]["design_context"]
    assert receipt["selection"] == "source_policy" and receipt.get("artifact") is None
    assert next(a for a in result["backend_status"]["attempted_backends"] if a["backend"] == "verdi_npi")["status"] == "skipped"
    if tool == "trace_signal_path":
        assert result["found"] is False and result["unsupported_reason"] == "static_backend_no_path_api"
    else:
        assert result["backend"] == "source_graph"
        assert result["claim_semantics"]["negative_claim_allowed"] is False
        assert result["backend_status"]["source_graph"]["bootstrap_context"]
        assert result["backend_status"]["source_graph"]["fallback_used"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["explain_signal_driver", "find_signal_loads"])
async def test_source_fallback_uses_shared_prepared_source_metadata(tmp_path, monkeypatch, tool):
    _, log, _ = _project(tmp_path, candidate=False)
    handle, _ = await _prepare(log, backend_preference="source")
    cfg = replace(server.get_source_graph_execution_config(), enabled=True, error_code=None)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda: cfg)
    monkeypatch.setattr(server, "get_compile_source_index_config", lambda: replace(config.get_compile_source_index_config(), enabled=False))
    from src import prepared_source_connectivity
    from src.source_graph_adapter import _blocked_plan
    calls = []
    def prepared_plan(**kwargs):
        calls.append(kwargs)
        return _blocked_plan(code="compile_context_unavailable", stage="compile_manifest")
    monkeypatch.setattr(prepared_source_connectivity, "build_prepared_source_plan", prepared_plan)
    monkeypatch.setattr(server, "build_bounded_connectivity_context", _forbidden)
    monkeypatch.setattr(server, "_resolve_hierarchy_context", _forbidden)
    monkeypatch.setattr(server, "_parse_merged_compile_context", _forbidden)
    result = await _public(tool, _arguments(tool, handle))
    assert len(calls) == 1 and calls[0]["context"].compile_result()["top_modules"] == ["top"]
    assert result["backend"] == "source_graph" and result["claim_semantics"]["negative_claim_allowed"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["npi_load_failed", "npi_lsf_npi_unavailable", "npi_query_failed"])
async def test_availability_failures_disable_later_npi_attempts_only_for_this_handle(tmp_path, backend_controls, reason):
    _, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log)
    backend_controls.failure_reason = reason
    first = await _public("find_signal_loads", _arguments("find_signal_loads", handle))
    second = await _public("find_signal_loads", _arguments("find_signal_loads", handle))
    remembered = reason != "npi_query_failed"
    assert len(backend_controls.selections) == (1 if remembered else 2)
    assert len(backend_controls.calls) == (1 if remembered else 2)
    for index, result in enumerate((first, second)):
        receipt = result["backend_status"]["design_context"]
        assert receipt.get("previous_npi_failure") == (reason if remembered and index else None)
        assert receipt["artifact"]["source_association"] == "unverified"
    if remembered:
        assert second["backend_status"]["design_context"]["artifact"]["load_status"] == "failed"
        assert next(a for a in second["backend_status"]["attempted_backends"] if a["backend"] == "verdi_npi")["status"] == "skipped"


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["selector", "query"])
@pytest.mark.parametrize("failure,code", [("input_changed", "design_context_changed"), ("timeout", "design_context_timeout"),
                                        ("kdb", "kdb_identity_changed"), ("execution", "design_context_execution_changed"), ("cancel", None)])
async def test_identity_abort_timeout_and_cancel_never_advance_fallback(tmp_path, monkeypatch, backend_controls, stage, failure, code):
    _, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log)
    errors = {"input_changed": CaptureAborted("input_changed"), "timeout": CaptureAborted("timeout"),
              "kdb": KdbBindingChanged(), "execution": ExecutionBindingChanged(), "cancel": OperationCancelled()}
    def fail():
        raise errors[failure]
    if stage == "selector":
        backend_controls.selector_action = fail
    else:
        backend_controls.action = fail
    # Pure configuration capture is allowed; typed failures must not execute source fallback.
    monkeypatch.setattr(server, "_query_prepared_source", _forbidden)  # Pure config capture is allowed; no source work after typed fatal.
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await _public("find_signal_loads", _arguments("find_signal_loads", handle))
    else:
        result = await _public("find_signal_loads", _arguments("find_signal_loads", handle))
        assert result["error_code"] == code and "loads" not in result


@pytest.mark.anyio
@pytest.mark.parametrize("change,code", [("source", "design_context_changed"), ("maps", "kdb_identity_changed"),
                                       ("environment", "design_context_execution_changed")])
async def test_post_query_change_discards_positive_control_facts(tmp_path, monkeypatch, backend_controls, change, code):
    source, log, candidate = _project(tmp_path)
    handle, _ = await _prepare(log)
    def mutate():
        if change == "source":
            source.write_text("module changed; endmodule\n")
        elif change == "maps":
            path = candidate / "_libFileMap"
            before = path.stat()
            path.write_bytes(b"lib_B")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        else:
            monkeypatch.setenv("LM_LICENSE_FILE", "control_changed_after_query")
    backend_controls.action = mutate
    result = await _public("find_signal_loads", _arguments("find_signal_loads", handle))
    assert result["error_code"] == code and "loads" not in result
    assert len(backend_controls.calls) == 1


@pytest.mark.anyio
async def test_changed_source_before_query_never_discovers_or_selects_backend(tmp_path, monkeypatch, backend_controls):
    source, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log)
    source.write_text("module replacement; endmodule\n")
    monkeypatch.setattr(server, "prepare_backend_binding", _forbidden)
    result = await _public("find_signal_loads", _arguments("find_signal_loads", handle))
    assert result["error_code"] == "design_context_changed"
    assert not backend_controls.selections


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [{"compile_log": "/different.log"}, {"simulator": "xcelium"}, {"top_hint": "different"},
                                  {"backend_preference": "source"}, {"compile_context": {}}, {"kdb_path": "/new"},
                                  {"supplementary_compile_logs": ["/different.log"]}])
async def test_handle_conflicts_have_fixed_error_before_discovery(tmp_path, monkeypatch, extra):
    _, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log)
    monkeypatch.setattr(server, "prepare_backend_binding", _forbidden)
    result = await _public("find_signal_loads", _arguments("find_signal_loads", handle, **extra))
    assert result["error_code"] == "design_context_arguments_mismatch"


@pytest.mark.anyio
@pytest.mark.parametrize("handle", ["", None, True, 12])
async def test_invalid_handle_has_fixed_error(handle):
    result = await _public("find_signal_loads", _arguments("find_signal_loads", design_handle=handle))
    assert result["error_code"] == "invalid_design_handle"


@pytest.mark.anyio
async def test_unknown_expired_unsupported_and_reserved_handles_are_explicit(tmp_path):
    _, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log)
    unknown = await _public("find_signal_loads", _arguments("find_signal_loads", "dc_unknown"))
    assert unknown["error_code"] == "handle_expired"
    # Hierarchy queries now support prepared handles; waveform-only queries
    # still reject them before opening the waveform or selecting a backend.
    unsupported = await _public("get_waveform_summary", {
        "design_handle": handle, "wave_path": "/control_only/unused.vcd",
    })
    assert unsupported["error_code"] == "design_handle_tool_unsupported"
    reserved = await _public("find_signal_loads", {"_prepared_design": {}})
    assert reserved["error_code"] == "invalid_design_context_arguments"
    server.reset_session_state()
    expired = await _public("find_signal_loads", _arguments("find_signal_loads", handle))
    assert expired["error_code"] == "handle_expired"


@pytest.mark.anyio
async def test_two_queries_share_one_worker_discovery_and_publish_on_event_loop(tmp_path, monkeypatch, backend_controls):
    _, log, _ = _project(tmp_path)
    _, other_log, _ = _project(tmp_path / "other")
    other = prepare_design_context(str(other_log), simulator="vcs")
    store = DesignContextStore(max_entries=1)
    monkeypatch.setattr(server, "_design_context_store", store)
    handle, _ = await _prepare(log)
    entered, release = threading.Event(), threading.Event()
    original = server.prepare_backend_binding
    worker_ids, publications = [], []
    main_thread = threading.get_ident()
    def blocked(context):
        worker_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(context)
    original_publish = store.publish_binding
    def publish(*args):
        publications.append(threading.get_ident())
        return original_publish(*args)
    monkeypatch.setattr(server, "prepare_backend_binding", blocked)
    monkeypatch.setattr(store, "publish_binding", publish)
    first = asyncio.create_task(_public("find_signal_loads", _arguments("find_signal_loads", handle)))
    second = None
    try:
        await _event(entered)
        second = asyncio.create_task(_public("find_signal_loads", _arguments("find_signal_loads", handle)))
        await anyio.sleep(0)
        with store.pin(handle) as entry:
            assert entry.leases >= 3  # Two requests plus this inspection pin.
            with pytest.raises(ContextUnavailable) as raised:
                store.register(other)
            assert raised.value.code == "context_capacity"
            assert store.remove(handle) is False
        release.set()
        results = await asyncio.gather(first, second)
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    assert len(worker_ids) == 1 and worker_ids[0] != main_thread
    assert publications == [main_thread]
    assert sorted(result["backend_status"]["design_context"]["binding_reused"] for result in results) == [False, True]
    assert store.remove(handle)


@pytest.mark.anyio
@pytest.mark.parametrize("termination", ["cancel", "reset", "timeout"])
async def test_abandoned_binding_worker_never_publishes_late(tmp_path, monkeypatch, backend_controls, termination):
    _, log, _ = _project(tmp_path)
    handle, _ = await _prepare(log)
    store = server._design_context_store
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = server.prepare_backend_binding
    publications = []
    def blocked(context):
        entered.set()
        assert release.wait(5)
        try:
            return original(context)
        finally:
            finished.set()
    original_publish = store.publish_binding
    def publish(*args):
        publications.append(1)
        return original_publish(*args)
    monkeypatch.setattr(server, "prepare_backend_binding", blocked)
    monkeypatch.setattr(store, "publish_binding", publish)
    if termination == "timeout":
        fail_after = anyio.fail_after
        monkeypatch.setattr(server.anyio, "fail_after", lambda seconds, *args, **kwargs: fail_after(min(seconds, 0.5), *args, **kwargs))
    task = asyncio.create_task(_public("find_signal_loads", _arguments("find_signal_loads", handle)))
    try:
        await _event(entered)
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif termination == "reset":
            server.reset_session_state()
            release.set()
            result = await task
            assert result["error_code"] == "design_context_generation_changed"
        else:
            result = await task
            assert result["error_code"] == "design_context_timeout"
    finally:
        release.set()
        await _event(finished)
        await asyncio.gather(task, return_exceptions=True)
    assert not publications and not backend_controls.calls
    with store.pin(handle) as entry:
        assert entry.binding is None
    assert store.remove(handle)


@pytest.mark.anyio
async def test_pin_covers_awaited_query_and_releases_on_cancellation(tmp_path, monkeypatch, backend_controls):
    _, log, _ = _project(tmp_path)
    store = DesignContextStore(max_entries=1)
    monkeypatch.setattr(server, "_design_context_store", store)
    handle, _ = await _prepare(log)
    _, other_log, _ = _project(tmp_path / "other")
    other = prepare_design_context(str(other_log), simulator="vcs")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    backend_controls.external = True  # Scheduling control, not an LSF claim.
    def blocked_query():
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()
    backend_controls.action = blocked_query
    task = asyncio.create_task(_public("find_signal_loads", _arguments("find_signal_loads", handle)))
    try:
        await _event(entered)
        assert store.remove(handle) is False
        with pytest.raises(ContextUnavailable) as raised:
            store.register(other)
        assert raised.value.code == "context_capacity"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _event(finished)
        await asyncio.gather(task, return_exceptions=True)
    assert store.remove(handle)


@pytest.mark.anyio
async def test_verilator_top_binding_uses_context_copy_and_validates_outside_wave_lock(tmp_path, monkeypatch, backend_controls):
    _, log, candidate = _project(tmp_path, simulator="verilator")
    handle, _ = await _prepare(log, simulator="verilator", kdb_path=str(candidate))
    wave = tmp_path / "wave.vcd"
    wave.write_text("control-only metadata fixture\n")
    wave_lock = server._wave_locks_for([str(wave)])[0]
    main_thread = threading.get_ident()
    observations = {"source": [], "binding": [], "root": []}
    original_source = DesignContext.current
    original_binding = design_backend_binding.DesignBackendBinding.require_current
    original_root = wave_design_binding.bind_root
    def source_current(self, *args, **kwargs):
        assert threading.get_ident() != main_thread and not wave_lock.locked()
        observations["source"].append(1)
        return original_source(self, *args, **kwargs)
    def binding_current(self, *args, **kwargs):
        assert threading.get_ident() != main_thread and not wave_lock.locked()
        observations["binding"].append(1)
        return original_binding(self, *args, **kwargs)
    class MetadataParser:
        file_path = str(wave)
        def get_signal_declaration(self, path):
            assert wave_lock.locked()
            return {"path": path, "width": 1}
    def bind_root(parser, path, compile_result, **kwargs):
        assert wave_lock.locked() and compile_result["top_modules"] == ["top"]
        observations["root"].append(deepcopy(compile_result))
        return original_root(parser, path, compile_result, **kwargs)
    monkeypatch.setattr(DesignContext, "current", source_current)
    monkeypatch.setattr(design_backend_binding.DesignBackendBinding, "require_current", binding_current)
    monkeypatch.setattr(wave_design_binding, "bind_root", bind_root)
    monkeypatch.setattr(server, "_get_parser", lambda path: MetadataParser())
    monkeypatch.setattr(server, "_resolve_hierarchy_context", _forbidden)
    result = await _public("find_signal_loads", _arguments("find_signal_loads", handle, signal_path="TOP.top.y", wave_path=str(wave)))
    assert result["signal_path"] == "TOP.top.y"
    assert result["wave_design_binding"]["source_signal"] == "top.y"
    assert backend_controls.calls[0][1]["signal_path"] == "top.y"
    assert observations["source"] and observations["binding"] and len(observations["root"]) == 1


@pytest.mark.anyio
async def test_legacy_arguments_keep_existing_route_without_implicit_design_handle(tmp_path, monkeypatch):
    _, log, _ = _project(tmp_path)
    # A prepared handle must not silently become session-wide active state.
    await _prepare(log)
    calls = []
    async def legacy_route(*, operation, args, simulator):
        assert "_prepared_design" not in args and "design_handle" not in args
        calls.append(args["compile_log"])
        result = {"signal_path": args["signal_path"], "resolved_rtl_name": "y", "loads": [],
                  "completeness": "shallow_only", "backend": "static"}
        return result, {"simulator": "vcs", "backend": "static", "actual_backend": "static"}
    monkeypatch.setattr(server, "_check_prerequisites", lambda *args: None)
    monkeypatch.setattr(server, "_route_public_connectivity", legacy_route)
    monkeypatch.setattr(server, "prepare_backend_binding", _forbidden)
    result = await _public("find_signal_loads", _arguments("find_signal_loads", compile_log=str(log)))
    assert calls == [str(log)] and result["backend"] == "static"
    assert result["backend_status"].get("design_context") is None
