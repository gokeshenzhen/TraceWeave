"""Bound compile/KDB ownership, using file bytes and mocked execution only.

Native and scheduler spies below establish call routing, cancellation and
generation ownership. They do not validate HDL semantics, NPI APIs, license
availability, real remote execution or public design_handle routing.
"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest
from pydantic import ValidationError

from config import NpiExecutionConfig
from src import connectivity_backend, kdb_anchor, npi_dynamic, npi_lsf, signal_driver, signal_load, verdi_npi_backend
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.dynamic_evidence import unsupported_step
from src.kdb_anchor import KdbBindingChanged, capture_kdb_anchor


OPS = ("driver", "loads", "path", "dynamic")
MODES = ("local", "lsf")
TOP = "compiled_top"
MAX_BYTES = 16 * 1024 * 1024


def _forbidden(*args, **kwargs):
    pytest.fail("bound control fixture reached parsing, probing or a real EDA process")


@pytest.fixture(autouse=True)
def no_real_native_or_scheduler(monkeypatch):
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", _forbidden)
    monkeypatch.setattr(verdi_npi_backend, "_silence_native_stdio", nullcontext)
    monkeypatch.setattr(verdi_npi_backend, "_install_shutdown_banner_silencer", lambda: None)
    monkeypatch.setattr(verdi_npi_backend, "_NPI_INITIALIZED_IDS", set())
    monkeypatch.setattr(verdi_npi_backend, "_NPI_ACTIVE_DESIGNS", {})
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)


def _fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "top.sv"
    source.write_text(f"module {TOP}(input logic a, output logic y); assign y = a; endmodule\n")
    maps = root / "chosen_kdb"
    maps.mkdir()
    (maps / "_libFileMap").write_bytes(b"lib_A")
    (maps / "_stnElabDB").write_bytes(b"db_A")
    metadata = {
        "simulator": "vcs", "compile_cwd": str(root),
        "compile_command": f"vcs -sverilog {source} -top {TOP}",
        "top_modules": [TOP], "primary_top": TOP,
        "files": {"user": [{"path": str(source), "type": "module", "category": "rtl"}]},
        "compile_evidence": {
            "schema_version": 1, "unit_order_source": "simulator_log",
            "ordered_compilation_units": [{"path": str(source), "role": "project"}],
            "source_phases": [{"source_log_index": 0, "options": ["+define+ORIGINAL"]}],
        },
    }
    return maps, metadata, str(root / "must_not_reparse.log")


def _mutate_map(root):
    path = root / "_libFileMap"
    old = path.stat()
    path.write_bytes(b"lib_B")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))


def _mutate_caller(metadata):
    metadata["top_modules"][0] = "unrelated_top"
    metadata["files"]["user"][0]["path"] = "/unrelated/source.sv"
    metadata["compile_evidence"]["source_phases"][0]["options"].append("+define+MUTATED")


def _result(op, backend="verdi_npi"):
    if op == "dynamic":
        return unsupported_step(f"{TOP}.q", backend, "mock_control_only")
    if op == "driver":
        return {"signal_path": f"{TOP}.q", "wave_path": "", "resolved_rtl_name": "q",
                "resolved_module": TOP, "driver_status": "not_found", "recursive": True, "backend": backend}
    if op == "loads":
        return {"signal_path": f"{TOP}.a", "resolved_rtl_name": "a", "resolved_module": TOP,
                "loads": [], "completeness": "exact", "stopped_at": "no_npi_loads", "backend": backend}
    return {"from_signal": f"{TOP}.a", "to_signal": f"{TOP}.y", "found": False,
            "hops": 0, "path": [], "expand_assigns": True, "unsupported_reason": "not_connected", "backend": backend}


class _Fallback:
    def __init__(self):
        self.calls, self.action, self.bound = [], None, None

    def bind_compile_context(self, metadata):
        self.bound = deepcopy(metadata)

    def _call(self, op):
        self.calls.append(op)
        if self.action:
            self.action()
        return _result(op, "static")

    def find_driver(self, *args, **kwargs):
        return self._call("driver")

    def find_loads(self, *args, **kwargs):
        return self._call("loads")

    def find_path(self, *args, **kwargs):
        return self._call("path")


class _NativeSpy:
    def __init__(self):
        self.loads, self.queries = [], []
        self.load_action = self.query_action = None
        self.load_rc = 1
        self.kdb_load_quality = "clean"

    def init(self, argv):
        return 1

    def load_design(self, argv):
        self.loads.append(list(argv))
        if self.load_action:
            self.load_action()
        return self.load_rc

    def query(self, op, **facts):
        self.queries.append((op, deepcopy(facts)))
        if self.query_action:
            self.query_action()
        return _result(op)

    def bind_kdb_anchor(self, anchor):
        self.anchor = anchor

    def _ensure_loaded(self, path, top):
        self.loads.append((path, top))
        if self.load_action:
            self.load_action()
        return self.load_rc == 1

    def _npi_find_driver(self, signal, wave, top, *, recursive):
        return self.query("driver", top=top, recursive=recursive)

    def _npi_find_loads(self, signal, metadata, path, top, include_expr, kind_filter):
        return self.query("loads", metadata=metadata, path=path, top=top,
                          include_expr=include_expr, kind_filter=kind_filter)

    def _npi_find_path(self, source, target, *, expand_assigns):
        return self.query("path", expand_assigns=expand_assigns)


class _Transport:
    def __init__(self):
        self.requests, self.action = [], None
        self.failure = None

    def execute(self, request):
        self.requests.append(request)
        if self.action:
            self.action()
        op = {"find_driver": "driver", "find_loads": "loads", "find_path": "path", "dynamic_step": "dynamic"}[request.operation]
        return npi_lsf.LsfExecutionResult(
            result=None if self.failure else _result(op), scheduler_status="completed",
            worker_status="failed" if self.failure else "completed", fallback_reason=self.failure,
        )


def _config(root):
    return NpiExecutionConfig(mode="lsf", queue="mock_only", bsub_bin="bsub", bkill_bin="bkill",
                              python_bin="python3", timeout_sec=30, staging_dir=root / "staging")


def _make(mode, tmp_path, monkeypatch, *, bound=True):
    maps, metadata, log = _fixture(tmp_path)
    fallback = _Fallback()
    if mode == "local":
        spy = _NativeSpy()
        monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", lambda: (spy, object()))
        backend = verdi_npi_backend.VerdiNpiBackend(fallback)
        for name in ("_npi_find_driver", "_npi_find_loads", "_npi_find_path"):
            monkeypatch.setattr(backend, name, getattr(spy, name))
        monkeypatch.setattr(npi_dynamic, "query_step", lambda backend, signal, **kwargs: spy.query("dynamic", **kwargs))
    else:
        spy = _Transport()
        backend = npi_lsf.LsfConnectivityBackend(_config(tmp_path), fallback=fallback, transport=spy)
    anchor = capture_kdb_anchor(str(maps))
    if bound:
        backend.bind_compile_context(metadata)
        backend.bind_kdb_anchor(anchor)
    return backend, spy, fallback, maps, metadata, anchor, log


def _no_parse_or_probe(monkeypatch):
    for module in (verdi_npi_backend, npi_lsf):
        monkeypatch.setattr(module, "parse_compile_log", _forbidden)
        monkeypatch.setattr(module, "probe_verdi_backend", _forbidden)


def _query(backend, op, log):
    if op == "driver":
        return backend.find_driver(f"{TOP}.q", "/mock_only/wave.vcd", log, recursive=True)
    if op == "loads":
        return backend.find_loads(f"{TOP}.a", log, include_expr=False, kind_filter=["rhs_expr"])
    if op == "path":
        return backend.find_path(f"{TOP}.a", f"{TOP}.y", log, expand_assigns=True)
    return backend.get_dynamic_step(f"{TOP}.q", log, include_dependency_candidates=True)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op", OPS)
def test_bound_queries_use_fixed_anchor_and_deep_copied_metadata_without_reparse(tmp_path, monkeypatch, mode, op):
    backend, spy, fallback, maps, metadata, anchor, log = _make(mode, tmp_path, monkeypatch)
    expected = deepcopy(metadata)
    _mutate_caller(metadata)
    _no_parse_or_probe(monkeypatch)
    result = _query(backend, op, log)
    assert result["backend"] == "verdi_npi"
    assert fallback.calls == [] and fallback.bound == expected
    if mode == "local":
        assert len(spy.loads) == 1 and spy.loads[0][-1] == TOP
        assert spy.queries[0][0] == op
        if op == "loads":
            assert spy.queries[0][1]["metadata"] == expected
    else:
        assert len(spy.requests) == 1
        request = spy.requests[0]
        assert request.kdb_path == anchor.path and request.top == TOP
        assert request.kdb_identity_sha256 == anchor.identity_sha256
        assert request.kdb_identity_max_bytes == anchor.max_bytes
        assert request.protocol_version == "5.0"
        field = {"driver": "recursive", "loads": "include_expr", "path": "expand_assigns", "dynamic": "include_dependency_candidates"}[op]
        assert getattr(request, field) is (False if op == "loads" else True)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["missing", "empty", "unrecognized", "source_only", "partial", "over_budget"])
def test_binding_rejects_non_elaborated_or_incomplete_anchors(tmp_path, monkeypatch, mode, kind):
    backend, _, _, maps, _, _, _ = _make(mode, tmp_path / "backend", monkeypatch)
    candidate = tmp_path / "candidate"
    if kind != "missing":
        candidate.mkdir()
    if kind in {"source_only", "partial", "over_budget"}:
        (candidate / "_libFileMap").write_bytes(b"library")
    if kind in {"partial", "over_budget"}:
        (candidate / "_stnElabDB").write_bytes(b"database")
    if kind == "unrecognized":
        (candidate / "payload").write_bytes(b"not a map")
    limit = 1 if kind == "partial" else MAX_BYTES + 1 if kind == "over_budget" else MAX_BYTES
    anchor = capture_kdb_anchor(str(candidate), max_bytes=limit)
    with pytest.raises(ValueError):
        backend.bind_kdb_anchor(anchor)


@pytest.mark.parametrize("mode", MODES)
def test_known_error_marker_candidate_may_be_bound_without_claiming_loadability(tmp_path, monkeypatch, mode):
    backend, _, _, maps, _, _, _ = _make(mode, tmp_path, monkeypatch)
    (maps / ".hasElabcomError").write_bytes(b"")
    anchor = capture_kdb_anchor(str(maps))
    assert anchor.identity_complete and anchor.artifact_status == "elaboration_error"
    backend.bind_kdb_anchor(anchor)
    assert anchor.receipt()["load_status"] == "not_checked"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op", OPS)
def test_prequery_generation_change_prevents_execution_and_fallback(tmp_path, monkeypatch, mode, op):
    backend, spy, fallback, maps, _, _, log = _make(mode, tmp_path, monkeypatch)
    _no_parse_or_probe(monkeypatch)
    _mutate_map(maps)
    with pytest.raises(KdbBindingChanged):
        _query(backend, op, log)
    assert fallback.calls == []
    assert (spy.loads if mode == "local" else spy.requests) == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("raises_after_change", [False, True])
def test_postquery_generation_change_discards_success_or_exception_without_fallback(tmp_path, monkeypatch, mode, op, raises_after_change):
    backend, spy, fallback, maps, _, _, log = _make(mode, tmp_path, monkeypatch)
    _no_parse_or_probe(monkeypatch)

    def change():
        _mutate_map(maps)
        if raises_after_change:
            raise ValueError("mock ordinary query failure")

    if mode == "local":
        spy.query_action = change
    else:
        spy.action = change
    with pytest.raises(KdbBindingChanged):
        _query(backend, op, log)
    assert fallback.calls == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("error", ["cancel", "timeout", "input_changed", "binding_changed"])
def test_control_errors_propagate_instead_of_falling_back(tmp_path, monkeypatch, mode, op, error):
    backend, spy, fallback, _, _, _, log = _make(mode, tmp_path, monkeypatch)
    exception = (OperationCancelled("mock cancellation") if error == "cancel" else KdbBindingChanged()
                 if error == "binding_changed" else CaptureAborted(error))

    def fail():
        raise exception

    if mode == "local":
        spy.query_action = fail
    else:
        spy.action = fail
    with pytest.raises(type(exception)):
        _query(backend, op, log)
    assert fallback.calls == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op", ("driver", "loads", "path"))
@pytest.mark.parametrize("failure", ["changed", "cancelled"])
def test_fallback_return_cannot_bypass_bound_identity_or_cancellation(tmp_path, monkeypatch, mode, op, failure):
    backend, spy, fallback, maps, _, _, log = _make(mode, tmp_path, monkeypatch)
    event = threading.Event()

    def ordinary_failure():
        raise ValueError("ordinary NPI query failure")

    if mode == "local":
        spy.query_action = ordinary_failure
    else:
        spy.failure = "npi_lsf_worker_failed"
    fallback.action = (lambda: _mutate_map(maps)) if failure == "changed" else event.set
    token = push_cancel_event(event)
    try:
        with pytest.raises(KdbBindingChanged if failure == "changed" else OperationCancelled):
            _query(backend, op, log)
        assert fallback.calls == [op]
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize("op", OPS)
def test_failed_local_load_still_checks_generation_before_any_fallback(tmp_path, monkeypatch, op):
    backend, spy, fallback, maps, _, _, log = _make("local", tmp_path, monkeypatch)
    spy.load_rc = 0
    spy.load_action = lambda: _mutate_map(maps)
    with pytest.raises(KdbBindingChanged):
        _query(backend, op, log)
    assert fallback.calls == [] and spy.queries == []


def test_local_load_reuse_uses_raw_generation_and_requires_explicit_new_binding(tmp_path, monkeypatch):
    backend, spy, _, maps, _, first, _ = _make("local", tmp_path, monkeypatch)
    assert backend._ensure_loaded(first.path, TOP)
    assert backend._ensure_loaded(first.path, TOP)
    assert len(spy.loads) == 1
    target = maps / "_libFileMap"
    old = target.stat()
    _mutate_map(maps)
    real_stat, real_fstat = os.stat, os.fstat

    def coarse(info):
        return old if (info.st_dev, info.st_ino) == (old.st_dev, old.st_ino) else info

    monkeypatch.setattr(kdb_anchor.os, "stat", lambda path, *args, **kwargs: coarse(real_stat(path, *args, **kwargs)))
    monkeypatch.setattr(kdb_anchor.os, "fstat", lambda fd: coarse(real_fstat(fd)))
    with pytest.raises(KdbBindingChanged):
        backend._ensure_loaded(first.path, TOP)
    assert len(spy.loads) == 1
    second = capture_kdb_anchor(first.path)
    assert second.identity_sha256 != first.identity_sha256
    backend.bind_kdb_anchor(second)
    assert backend._ensure_loaded(second.path, TOP)
    assert backend._ensure_loaded(second.path, TOP)
    assert len(spy.loads) == 2


def test_static_driver_and_loads_keep_bound_metadata_and_skip_log_parser(tmp_path, monkeypatch):
    _, metadata, log = _fixture(tmp_path)
    expected = deepcopy(metadata)
    backend = connectivity_backend.StaticConnectivityBackend()
    backend.bind_compile_context(metadata)
    _mutate_caller(metadata)
    received = []

    def inspect_call(**kwargs):
        received.append(deepcopy(kwargs["compile_result"]))
        return {}

    monkeypatch.setattr(connectivity_backend, "explain_signal_driver", inspect_call)
    monkeypatch.setattr(connectivity_backend, "find_signal_loads", inspect_call)
    backend.find_driver(f"{TOP}.y", "unused.vcd", log)
    backend.find_loads(f"{TOP}.a", log)
    assert received == [expected, expected]


def test_static_loads_real_assignment_oracle_accepts_explicit_compile_result(tmp_path, monkeypatch):
    _, metadata, log = _fixture(tmp_path)
    monkeypatch.setattr(signal_driver, "parse_compile_log", _forbidden)
    direct = signal_load.find_signal_loads(f"{TOP}.a", log, compile_result=metadata)
    backend = connectivity_backend.StaticConnectivityBackend()
    backend.bind_compile_context(metadata)
    wrapped = backend.find_loads(f"{TOP}.a", log)
    for result in (direct, wrapped):
        assert any(row["load_path"] == f"{TOP}.y" and row["kind"] == "rhs_expr" for row in result["loads"])
        assert result["completeness"] == "shallow_only"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op", OPS)
def test_unbound_legacy_backend_retains_automatic_parse_and_probe(tmp_path, monkeypatch, mode, op):
    backend, spy, fallback, maps, metadata, _, log = _make(mode, tmp_path, monkeypatch, bound=False)
    calls = []
    module = verdi_npi_backend if mode == "local" else npi_lsf

    def parse(path, simulator="auto"):
        calls.append(("parse", path))
        return deepcopy(metadata)

    def probe(context, **kwargs):
        assert context == metadata
        calls.append(("probe", kwargs["compile_log_path"]))
        return {"kdb_path": str(maps)}

    monkeypatch.setattr(module, "parse_compile_log", parse)
    monkeypatch.setattr(module, "probe_verdi_backend", probe)
    assert _query(backend, op, log)["backend"] == "verdi_npi"
    assert calls == [("parse", log), ("probe", log)]
    assert fallback.calls == []
    if mode == "lsf":
        assert spy.requests[0].kdb_identity_sha256 is None


REQUESTS = {"driver": npi_lsf.FindDriverWorkerRequest, "loads": npi_lsf.FindLoadsWorkerRequest,
            "path": npi_lsf.FindPathWorkerRequest, "dynamic": npi_lsf.DynamicStepWorkerRequest}


def _request(op, anchor, *, bound=True, **overrides):
    fields = {"kdb_path": anchor.path, "top": TOP, "protocol_version": "4.0"}
    fields.update({"from_signal": f"{TOP}.a", "to_signal": f"{TOP}.y", "expand_assigns": True}
                  if op == "path" else {"signal_path": f"{TOP}.q"})
    if bound:
        fields.update(kdb_identity_sha256=anchor.identity_sha256, kdb_identity_max_bytes=anchor.max_bytes)
    return REQUESTS[op](**{**fields, **overrides})


@pytest.mark.parametrize("op", OPS)
def test_protocol_4_roundtrip_carries_exact_bound_identity_and_budget(tmp_path, op):
    maps, _, _ = _fixture(tmp_path)
    anchor = capture_kdb_anchor(str(maps), max_bytes=128)
    request = _request(op, anchor)
    decoded = npi_lsf.parse_worker_request_bytes(json.dumps(request.model_dump()).encode())
    assert decoded.protocol_version == "4.0"
    assert decoded.kdb_identity_sha256 == anchor.identity_sha256
    assert decoded.kdb_identity_max_bytes == 128
    legacy = _request(op, anchor, bound=False)
    assert legacy.kdb_identity_sha256 is None and legacy.kdb_identity_max_bytes == MAX_BYTES


@pytest.mark.parametrize("invalid", [
    {"kdb_identity_sha256": "a" * 63}, {"kdb_identity_sha256": "g" * 64},
    {"kdb_identity_sha256": 1}, {"kdb_identity_max_bytes": 0},
    {"kdb_identity_max_bytes": MAX_BYTES + 1}, {"kdb_identity_max_bytes": True},
    {"kdb_identity_max_bytes": 1.0}, {"kdb_identity_max_bytes": "16"},
])
def test_protocol_rejects_invalid_identity_or_unbounded_budget(tmp_path, invalid):
    maps, _, _ = _fixture(tmp_path)
    anchor = capture_kdb_anchor(str(maps))
    with pytest.raises(ValidationError):
        _request("driver", anchor, **invalid)


def _worker_spy(monkeypatch):
    spy = _NativeSpy()
    monkeypatch.setattr(npi_dynamic, "query_step", lambda backend, signal, **kwargs: spy.query("dynamic", **kwargs))
    return spy


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("phase", ["before_load", "query", "query_exception"])
def test_worker_changed_identity_discards_facts_at_correct_stage(tmp_path, monkeypatch, op, phase):
    maps, _, _ = _fixture(tmp_path)
    anchor = capture_kdb_anchor(str(maps))
    request = _request(op, anchor)
    spy = _worker_spy(monkeypatch)
    if phase == "before_load":
        _mutate_map(maps)
    else:
        def change():
            _mutate_map(maps)
            if phase == "query_exception":
                raise ValueError("ordinary native query error")
        spy.query_action = change
    response = npi_lsf.execute_worker_request(request, backend=spy)
    assert isinstance(response, npi_lsf.WorkerError)
    assert response.error_code == "kdb_identity_changed"
    assert response.stage == ("load" if phase == "before_load" else "query")
    assert (spy.loads == []) is (phase == "before_load")


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("stage", ["load", "query"])
@pytest.mark.parametrize("failure", ["cancel", "timeout", "input_changed"])
def test_worker_preserves_cancel_and_serializes_fixed_capture_failures(tmp_path, monkeypatch, op, stage, failure):
    maps, _, _ = _fixture(tmp_path)
    anchor = capture_kdb_anchor(str(maps))
    request = _request(op, anchor)
    spy = _worker_spy(monkeypatch)

    def fail(*args, **kwargs):
        raise OperationCancelled("mock cancellation") if failure == "cancel" else CaptureAborted(failure)

    if stage == "load":
        monkeypatch.setattr(npi_lsf, "capture_kdb_anchor", fail)
    else:
        spy.query_action = fail
    if failure == "cancel":
        with pytest.raises(OperationCancelled):
            npi_lsf.execute_worker_request(request, backend=spy)
    else:
        response = npi_lsf.execute_worker_request(request, backend=spy)
        assert isinstance(response, npi_lsf.WorkerError)
        assert response.error_code == ("kdb_identity_timeout" if failure == "timeout" else "kdb_identity_changed")
        assert response.stage == stage


@pytest.mark.parametrize("op", OPS)
def test_legacy_worker_request_remains_usable_without_bound_identity(tmp_path, monkeypatch, op):
    maps, _, _ = _fixture(tmp_path)
    request = _request(op, capture_kdb_anchor(str(maps)), bound=False)
    monkeypatch.setattr(npi_lsf, "capture_kdb_anchor", _forbidden)
    spy = _worker_spy(monkeypatch)
    response = npi_lsf.execute_worker_request(request, backend=spy)
    assert isinstance(response, npi_lsf.WorkerSuccess)
    assert len(spy.loads) == 1 and len(spy.queries) == 1


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("reason", ["kdb_identity_changed", "kdb_identity_timeout"])
def test_parent_remote_identity_errors_cannot_fall_back_to_static(tmp_path, monkeypatch, op, reason):
    backend, transport, fallback, _, _, _, log = _make("lsf", tmp_path, monkeypatch)
    transport.failure = reason
    with pytest.raises(KdbBindingChanged if reason == "kdb_identity_changed" else CaptureAborted) as raised:
        _query(backend, op, log)
    if reason == "kdb_identity_timeout":
        assert raised.value.code == "timeout"
    assert fallback.calls == []


@pytest.mark.parametrize("reason", ["kdb_identity_changed", "kdb_identity_timeout"])
@pytest.mark.parametrize("stage", ["load", "query"])
def test_mock_scheduler_transport_preserves_worker_identity_error_code(tmp_path, monkeypatch, reason, stage):
    maps, _, _ = _fixture(tmp_path)
    request = _request("driver", capture_kdb_anchor(str(maps)))

    class FakePopen:
        returncode = 2

        def __init__(self, argv, **kwargs):
            assert kwargs["shell"] is False
            path = Path(argv[argv.index("--response") + 1])
            path.write_text(json.dumps({"protocol_version": "4.0", "status": "error", "error_code": reason, "stage": stage}))

        def poll(self):
            return 2

    monkeypatch.setattr(npi_lsf.subprocess, "Popen", FakePopen)
    outcome = npi_lsf.LsfNpiTransport(_config(tmp_path)).execute(request)
    assert outcome.result is None
    assert outcome.scheduler_status == "completed" and outcome.worker_status == "failed"
    assert outcome.fallback_reason == reason
