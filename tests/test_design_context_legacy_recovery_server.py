"""Old driver-action recovery into independent contexts, with control NPI.

Text parsing, bounded historical verification, KDB map capture, publication and
public serialization are real. Probe/selection and query facts are controls;
passing this file does not establish SDK, license, LSF or D1 acceptance.
"""
from __future__ import annotations

import asyncio
import builtins
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import anyio
import pytest

import config
import server
from src import cancellation, connectivity_backend, design_backend_binding, design_context_runtime
from src import sv_preprocessor, tb_hierarchy_builder, verdi_npi_backend
from src.compile_input_capture import CaptureAborted
from src.compile_session_snapshot import CompileSessionSnapshotBuilder
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context_runtime import ContextUnavailable, DesignContextStore
from src.divergence_context import log_identity, resolve_context
from src.hierarchy_handles import HandleStore, compute_handle, compute_snapshot_fingerprint
from src.hierarchy_recovery import HierarchyRecoveryRuntime, RecoveryLimits
from src.kdb_anchor import KdbBindingChanged


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _forbidden(*args, **kwargs):
    pytest.fail("healthy recovery entered full hierarchy, scan, frontend, overlay or native work")


def _no_full(monkeypatch):
    for module, name in ((server, "build_hierarchy"), (tb_hierarchy_builder, "build_hierarchy"),
            (tb_hierarchy_builder, "_scan_user_files"), (tb_hierarchy_builder, "build_component_tree"),
            (tb_hierarchy_builder, "scan_sv_file"), (tb_hierarchy_builder, "scan_preprocessed_sv"),
            (sv_preprocessor.SystemVerilogPreprocessor, "preprocess"),
            (sv_preprocessor, "_expand_hierarchy_macro_line")):
        monkeypatch.setattr(module, name, _forbidden)


class ControlNpi:
    name, execution_mode, uses_external_worker = "verdi_npi", "local", False

    def __init__(self, controls):
        self.controls, self.loaded = controls, False
        self.metadata = self.anchor = None

    def bind_compile_context(self, metadata):
        self.metadata = deepcopy(metadata)

    def bind_kdb_anchor(self, anchor):
        self.anchor = anchor

    @property
    def kdb_status(self):
        return {"load_quality": "clean"} if self.loaded else None

    def find_driver(self, **kwargs):
        owner = self.controls
        owner.queries.append((deepcopy(kwargs), self.metadata, self.anchor))
        assert not server._FSDB_WAVE_LOCK.locked()
        if owner.query_action:
            owner.query_action()
        self.loaded = owner.reason != "npi_load_failed"
        result = dict(signal_path=kwargs["signal_path"], wave_path=kwargs["wave_path"],
            resolved_rtl_name="y", resolved_module="top", driver_status="resolved", driver_kind="assign",
            source_file=self.metadata["files"]["user"][0]["path"], source_line=1,
            expression_summary="a", recursive=False, backend=self.name)
        if owner.reason:
            result["_npi_fallback_reason"] = owner.reason
        return result


class Controls:
    def __init__(self):
        self.probes, self.prepares, self.historical, self.publishes, self.queries = [], [], [], [], []
        self.query_action = self.selector_action = self.reason = None
        self.external = False

    def probe(self, compile_result, compile_log_path):
        self.probes.append((deepcopy(compile_result), compile_log_path))
        candidate = Path(compile_log_path).parent / "simv.daidir/kdb.elab++"
        return dict(simulator="vcs", kdb_path=str(candidate) if candidate.is_dir() else None,
                    kdb_flow="vcs_two_step" if candidate.is_dir() else "none",
                    kdb_validation_status="usable" if candidate.is_dir() else "unavailable")

    def select(self, status, *, fallback):
        if self.selector_action:
            self.selector_action()
        result = ControlNpi(self)
        result.uses_external_worker = self.external
        return result


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    server.reset_session_state()
    monkeypatch.setattr(server, "_handle_store", HandleStore())
    monkeypatch.setattr(server, "_hierarchy_recovery_runtime", HierarchyRecoveryRuntime())
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "apply_npi_source_overlay", _forbidden)
    monkeypatch.setattr(server, "scan_structural_risks", _forbidden)
    monkeypatch.setattr(server, "_get_parser", _forbidden)
    monkeypatch.setattr(server, "_wave_locks_for", _forbidden)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    source_config = replace(server.get_source_graph_execution_config(), enabled=False, error_code=None)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda: source_config)
    async def no_wave_binding(*args, **kwargs):
        return None
    monkeypatch.setattr(server, "_bind_wave_design_root", no_wave_binding)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pynpi", "pyslang"}:
            _forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    yield
    server.reset_session_state()


@pytest.fixture
def controls(monkeypatch):
    owner = Controls()
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", owner.probe)
    monkeypatch.setattr(connectivity_backend, "select_backend", owner.select)
    original_prepare = design_context_runtime.prepare_design_context
    def prepared(*args, **kwargs):
        owner.prepares.append(args[0])
        return original_prepare(*args, **kwargs)
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", prepared)
    original_verify = server.verify_legacy_source_observations
    def historical(context, token, **kwargs):
        owner.historical.append(token)
        return original_verify(context, token, **kwargs)
    monkeypatch.setattr(server, "verify_legacy_source_observations", historical)
    original_publish = DesignContextStore.publish
    def publish(self, ticket, **kwargs):
        owner.publishes.append((threading.get_ident(), ticket, kwargs))
        return original_publish(self, ticket, **kwargs)
    monkeypatch.setattr(DesignContextStore, "publish", publish)
    return owner


def _project(root, *, candidate=True, include=True):
    root.mkdir(parents=True, exist_ok=True)
    source, log, header = root / "top.sv", root / "compile.log", root / "defs.svh"
    source.write_text(('`include "defs.svh"\n' if include else "") +
        "module top(input logic a, output wire y); assign y=a; endmodule\n")
    if include:
        header.write_text("// historical header\n")
    log.write_text("Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n       top\n")
    kdb = root / "simv.daidir/kdb.elab++"
    if candidate:
        kdb.mkdir(parents=True)
        (kdb / "_libFileMap").write_bytes(b"lib_A")
        (kdb / "_stnElabDB").write_bytes(b"elab_A")
    return SimpleNamespace(source=source, log=log, header=header, kdb=kdb,
        raw=dict(compile_log=str(log), simulator="vcs", top_hint="top", supplementary_compile_logs=[]))


def _old_token(project):
    # H is explicit and intentionally excludes the compile log.
    builder = CompileSessionSnapshotBuilder()
    builder.read_text(str(project.source))
    if project.header.exists():
        builder.read_text(str(project.header))
    return {**project.raw, "hierarchy_handle": compute_handle(str(project.log), "vcs"),
        "snapshot_sha256": compute_snapshot_fingerprint(str(project.log), "vcs"),
        "compile_log_identity_sha256": log_identity(project.raw),
        "source_snapshot_sha256": builder.finish().content_fingerprint_sha256}


async def _query(ctx, **extra):
    blocks = await server.call_tool("explain_signal_driver", dict(compile_log=ctx["compile_log"],
        simulator=ctx.get("simulator", "auto"), compile_context=ctx, signal_path="top.y",
        wave_path="/control_only/not_read.vcd", recursive=False, output_format="full", **extra))
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


def _error(result, code):
    assert result["error_code"] == code
    assert "driver_status" not in result


@pytest.mark.anyio
async def test_real_old_build_token_recovers_without_another_tree_and_reuses_design(tmp_path, monkeypatch, controls):
    project = _project(tmp_path)
    # This explicit baseline is outside the guarded recovery/query sequence.
    metadata = server.parse_compile_log(str(project.log), "vcs")
    full = server.build_hierarchy(metadata, apply_source_overlay=False)
    assert {record.path for record in full["_compile_session_snapshot"].records} == {str(project.source), str(project.header)}
    manual = _old_token(project)
    assert full["_compile_session_snapshot"].content_fingerprint_sha256 == manual["source_snapshot_sha256"]
    full["_hierarchy_snapshot_sha256"] = manual["snapshot_sha256"]
    full["_compile_log_identity_sha256"] = manual["compile_log_identity_sha256"]
    server._handle_store.register(manual["hierarchy_handle"], full)
    bound, reason = resolve_context(project.raw, server._handle_store)
    assert reason == "ready"
    token = deepcopy(bound.context)
    server._handle_store.invalidate()
    _no_full(monkeypatch)
    state = deepcopy(server._session_state)
    loop_thread = threading.get_ident()
    first = await _query(token)
    second = await _query(token)
    for index, result in enumerate((first, second)):
        server.schemas.ExplainDriverResult.model_validate(result)
        receipt = result["hierarchy_recovery"]
        assert receipt["status"] == ("prepared_design" if index == 0 else "hit_design_context")
        assert receipt["hierarchy_available"] is False and receipt.get("hierarchy_handle") is None
        assert receipt["identity_basis"] == "validated_historical_source"
        assert receipt["compile_context"] == token
        assert result["backend_status"]["actual_backend"] == "verdi_npi"
        assert result["backend_status"]["design_context"]["binding_reused"] is True
        assert result["backend_status"]["design_context"]["artifact"]["source_association"] == "unverified"
    assert first["hierarchy_recovery"]["design_handle"] == second["hierarchy_recovery"]["design_handle"]
    assert len(controls.prepares) == len(controls.probes) == len(controls.historical) == len(controls.publishes) == 1
    assert controls.publishes[0][0] == loop_thread
    assert controls.historical == [token["source_snapshot_sha256"]]
    assert controls.publishes[0][2]["binding"] is not None
    assert first["hierarchy_recovery"]["preparation_metrics"]["legacy_source_files_read"] == 2
    assert second["hierarchy_recovery"]["preparation_metrics"] == {}
    assert server._session_state == state and len(server._handle_store) == 0
    assert token == bound.context


@pytest.mark.anyio
async def test_existing_complete_handle_stays_on_read_only_legacy_hit(tmp_path, monkeypatch, controls):
    project = _project(tmp_path)
    token = _old_token(project)
    full = server.build_hierarchy(server.parse_compile_log(str(project.log), "vcs"), apply_source_overlay=False)
    full.update(_hierarchy_snapshot_sha256=token["snapshot_sha256"], _compile_log_identity_sha256=token["compile_log_identity_sha256"])
    server._handle_store.register(token["hierarchy_handle"], full)
    _no_full(monkeypatch)
    async def legacy_route(*, args, **kwargs):
        assert args["_bound_context"].hierarchy is full
        return dict(signal_path="top.y", wave_path=args["wave_path"], resolved_rtl_name="y", driver_status="not_found"), {}
    monkeypatch.setattr(server, "_route_public_connectivity", legacy_route)
    result = await _query(token)
    assert result["hierarchy_recovery"]["status"] == "hit_existing"
    assert result["hierarchy_recovery"]["hierarchy_available"] is True
    assert controls.prepares == controls.publishes == controls.historical == []


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source_raw", "source_stat", "log", "top", "source_token", "snapshot", "handle", "log_token"])
async def test_historical_changes_or_wrong_tokens_do_not_publish_or_query(tmp_path, monkeypatch, controls, change):
    project = _project(tmp_path)
    token = _old_token(project)
    if change == "source_raw":
        project.header.write_text("// historical altered\n")
    elif change == "source_stat":
        previous = project.header.stat()
        os.utime(project.header, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    elif change == "log":
        previous = project.log.stat()
        # Historical log tokens contain stat, not archived raw bytes. An
        # equal-size rewrite + restored mtime can also retain ctime within one
        # filesystem tick. Change size while leaving the handle's mtime fixed
        # to exercise the full log-stat token deterministically.
        project.log.write_text(project.log.read_text() + "\n# updated compile record\n")
        os.utime(project.log, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert project.log.stat().st_size != previous.st_size
    elif change == "top":
        token["top_hint"] = "other"
    else:
        field = {"source_token": "source_snapshot_sha256", "snapshot": "snapshot_sha256",
                 "handle": "hierarchy_handle", "log_token": "compile_log_identity_sha256"}[change]
        token[field] = "tbh_wrong" if change == "handle" else "0" * 64
    _no_full(monkeypatch)
    _error(await _query(token), "compile_context_changed")
    assert controls.publishes == controls.queries == []
    assert len(server._handle_store) == 0


@pytest.mark.anyio
async def test_token_without_historical_source_establishes_only_current_sources(tmp_path, monkeypatch, controls):
    project = _project(tmp_path)
    _no_full(monkeypatch)
    result = await _query(project.raw)
    receipt = result["hierarchy_recovery"]
    assert receipt["status"] == "prepared_design" and receipt["identity_basis"] == "current_sources"
    assert "source_snapshot_sha256" not in receipt["compile_context"]
    assert controls.historical == []


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["npi_load_failed", "npi_query_failed"])
async def test_post_candidate_failure_uses_bound_fallback_without_legacy_rebuild(tmp_path, monkeypatch, controls, reason):
    project = _project(tmp_path)
    token = _old_token(project)
    controls.reason = reason
    _no_full(monkeypatch)
    result = await _query(token)
    assert result["hierarchy_recovery"]["status"] == "prepared_design"
    assert result["hierarchy_recovery"]["hierarchy_available"] is False
    assert result["backend_status"]["actual_backend"] == "source_graph"
    assert result["claim_semantics"]["negative_claim_allowed"] is False
    assert len(controls.prepares) == len(controls.probes) == len(controls.publishes) == 1
    assert len(server._handle_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["probe", "selector", "query"])
@pytest.mark.parametrize("failure,code", [("changed", "compile_context_changed"), ("timeout", "hierarchy_recovery_timeout"),
                                        ("kdb", "kdb_identity_changed"), ("policy", "design_context_execution_changed")])
async def test_typed_failures_never_enter_source_compatibility(tmp_path, monkeypatch, controls, stage, failure, code):
    project = _project(tmp_path)
    _no_full(monkeypatch)
    error = {"changed": CaptureAborted("input_changed"), "timeout": CaptureAborted("timeout"),
             "kdb": KdbBindingChanged(), "policy": ExecutionBindingChanged()}[failure]
    def fail(*args, **kwargs):
        raise error
    if stage == "probe":
        monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", fail)
    else:
        setattr(controls, stage + "_action", fail)
        # The already-published independent query wrapper owns these errors.
        code = {"changed": "design_context_changed", "timeout": "design_context_timeout"}.get(failure, code)
    _error(await _query(_old_token(project)), code)
    if stage == "probe":
        assert controls.publishes == []
    assert len(server._handle_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["no_candidate", "source_policy"])
async def test_explicit_source_compatibility_retains_one_old_build(tmp_path, monkeypatch, controls, kind):
    project = _project(tmp_path, candidate=kind != "no_candidate")
    if kind == "source_policy":
        monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    original = server.build_hierarchy
    builds = []
    def build(*args, **kwargs):
        builds.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(server, "build_hierarchy", build)
    async def legacy_route(*, args, **kwargs):
        assert args["_bound_context"].current()
        return dict(signal_path="top.y", wave_path=args["wave_path"], resolved_rtl_name="y", driver_status="not_found"), {}
    monkeypatch.setattr(server, "_route_public_connectivity", legacy_route)
    result = await _query(_old_token(project))
    assert result["hierarchy_recovery"]["status"] == "rebuilt"
    assert result["hierarchy_recovery"]["hierarchy_available"] is True
    assert len(builds) == 1 and builds[0]["apply_source_overlay"] is False
    assert controls.publishes == []
    if kind == "source_policy":
        assert controls.probes == controls.queries == []


def _barrier(started, release, stopped):
    started.set()
    try:
        while not release.wait(0.005):
            cancellation.check_cancelled()
        cancellation.check_cancelled()
    finally:
        stopped.set()


async def _joined():
    # Scheduling coordination only; no sharing key is used as an oracle.
    with anyio.fail_after(2):
        while not any(f.waiters >= 2 for f in server._hierarchy_recovery_runtime._flights.values()):
            await anyio.sleep(0.001)


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", ["none", "one", "all"])
async def test_exact_live_recovery_shares_work_and_waiter_cancellation(tmp_path, monkeypatch, controls, cancel):
    project = _project(tmp_path)
    token = _old_token(project)
    _no_full(monkeypatch)
    original = design_context_runtime.prepare_design_context
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        _barrier(started, release, stopped)
        return result
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", delayed)
    first, second = asyncio.create_task(_query(token)), None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        second = asyncio.create_task(_query(token))
        await _joined()
        if cancel in {"one", "all"}:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not stopped.is_set()
        if cancel == "all":
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert await asyncio.to_thread(stopped.wait, 1)
            assert controls.publishes == []
        else:
            release.set()
            result = await second
            assert result["hierarchy_recovery"]["coalesced"] is True
            if cancel == "none":
                other = await first
                assert other["hierarchy_recovery"]["design_handle"] == result["hierarchy_recovery"]["design_handle"]
            assert len(controls.publishes) == len(controls.probes) == 1
        assert len(controls.prepares) == 1
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("interruption", ["reset", "cancel", "timeout"])
async def test_registered_worker_ticket_cannot_publish_after_abandonment(tmp_path, monkeypatch, controls, interruption):
    project = _project(tmp_path)
    token = _old_token(project)
    _no_full(monkeypatch)
    original = DesignContextStore.prepare_registration
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def delayed(self, context, **kwargs):
        ticket = original(self, context, **kwargs)
        _barrier(started, release, stopped)
        return ticket
    monkeypatch.setattr(DesignContextStore, "prepare_registration", delayed)
    if interruption == "timeout":
        monkeypatch.setattr(server, "get_hierarchy_recovery_limits", lambda: RecoveryLimits(timeout_sec=0.2))
    task = asyncio.create_task(_query(token))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if interruption == "reset":
            server.reset_session_state()
            release.set()
            _error(await task, "design_context_generation_changed")
        elif interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            _error(await task, "hierarchy_recovery_timeout")
        assert await asyncio.to_thread(stopped.wait, 1)
        assert controls.publishes == controls.queries == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_binding_capacity_failure_is_atomic_and_preserves_existing_entry(tmp_path, monkeypatch, controls):
    project = _project(tmp_path / "target")
    other = _project(tmp_path / "other", include=False)
    store = server._design_context_store
    previous = store.register(design_context_runtime.prepare_design_context(str(other.log)))
    controls.publishes.clear()
    original = DesignContextStore.prepare_registration
    def only_context_fits(self, context, **kwargs):
        ticket = original(self, context, **kwargs)
        self.max_bytes = ticket.size
        return ticket
    monkeypatch.setattr(DesignContextStore, "prepare_registration", only_context_fits)
    _no_full(monkeypatch)
    _error(await _query(_old_token(project)), "context_capacity")
    assert len(controls.publishes) == 1  # An attempted transaction, never an entry.
    _, ticket, keywords = controls.publishes[0]
    assert store.find_recovery(keywords["recovery_key"]) is None
    with store.pin(previous) as entry:
        assert entry.context.ordered_compile_logs == (str(other.log),)
    with pytest.raises(ContextUnavailable) as raised:
        with store.pin(ticket.handle):
            pytest.fail("failed transaction leaked a context")
    assert raised.value.code == "handle_expired"
    assert controls.queries == [] and len(server._handle_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("change,code", [("source", "design_context_changed"), ("candidate", "kdb_identity_changed"),
                                       ("policy", "design_context_execution_changed")])
async def test_reused_alias_never_borrows_old_source_candidate_or_policy(tmp_path, monkeypatch, controls, change, code):
    project = _project(tmp_path)
    token = _old_token(project)
    _no_full(monkeypatch)
    first = await _query(token)
    assert first["hierarchy_recovery"]["status"] == "prepared_design"
    if change == "source":
        project.header.write_text("// newer header\n")
    elif change == "candidate":
        path = project.kdb / "_libFileMap"
        before = path.stat()
        path.write_bytes(b"lib_B")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    _error(await _query(token), code)
    assert len(controls.prepares) == len(controls.probes) == 1
    assert len(controls.queries) == 1


@pytest.mark.anyio
async def test_recovery_alias_is_pinned_for_query_and_disappears_on_entry_eviction(tmp_path, monkeypatch, controls):
    project = _project(tmp_path / "target")
    other = _project(tmp_path / "other", include=False)
    token = _old_token(project)
    store = DesignContextStore(max_entries=1)
    monkeypatch.setattr(server, "_design_context_store", store)
    _no_full(monkeypatch)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    controls.external = True  # Control-only async scheduling, no external worker/LSF.
    controls.query_action = lambda: _barrier(started, release, stopped)
    task = asyncio.create_task(_query(token))

    async def prepare_other():
        blocks = await server.call_tool("prepare_design_context", dict(compile_log=str(other.log), simulator="vcs"))
        return json.loads(blocks[0].text)

    try:
        assert await asyncio.to_thread(started.wait, 1)
        _, ticket, keywords = controls.publishes[0]
        key = keywords["recovery_key"]
        assert store.find_recovery(key) == ticket.handle
        _error(await prepare_other(), "context_capacity")
        assert store.find_recovery(key) == ticket.handle
        release.set()
        first = await task
        controls.query_action = None
        fresh = await prepare_other()
        assert fresh["design_handle"] != ticket.handle
        assert store.find_recovery(key) is None
        with pytest.raises(ContextUnavailable) as raised:
            with store.pin(ticket.handle):
                pytest.fail("evicted recovery entry remained available")
        assert raised.value.code == "handle_expired"
        repeated = await _query(token)
        assert repeated["hierarchy_recovery"]["status"] == "prepared_design"
        assert repeated["hierarchy_recovery"]["design_handle"] != first["hierarchy_recovery"]["design_handle"]
        assert len(controls.probes) == 2
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
