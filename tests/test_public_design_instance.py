"""Public first-tool controls, with real text capture and a handwritten provider.

Injected NPI labels establish routing/serialization only. No SDK, scheduler,
frontend, actual KDB semantics or license checkout is exercised by these tests.
The separate real stdio replay remains necessary acceptance evidence.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import traceback

import anyio
import jsonschema
import pytest

import config
import server
from src import connectivity_backend, design_backend_binding, kdb_builder
from src import structural_scanner, tb_hierarchy_builder, verdi_npi_backend
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.connectivity_ir import SourceLocation
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context_runtime import ContextUnavailable, DesignContextStore
from src.hierarchy_source_ir import HierarchyScope, ScopeLookup
from src.kdb_anchor import KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable


_public_artifact_dir = None
_public_call_index = 0


def forbidden(*args, **kwargs):
    pytest.fail("first-tool route entered build/scan/bootstrap/frontend/native/fallback/process work")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    global _public_artifact_dir, _public_call_index
    _public_artifact_dir = tmp_path / "public-control-evidence"
    _public_call_index = 0
    server.reset_session_state()
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *a, **k: None)
    for module, name in ((server, "build_hierarchy"), (server, "scan_structural_risks"),
                         (server, "build_bounded_connectivity_context"),
                         (server, "build_source_graph_plan"), (server, "build_source_graph_path_plan"),
                         (tb_hierarchy_builder, "build_hierarchy"),
                         (tb_hierarchy_builder, "scan_sv_file"),
                         (tb_hierarchy_builder, "build_component_tree"),
                         (structural_scanner, "scan_structural_risks"),
                         (verdi_npi_backend, "_import_pynpi"), (kdb_builder, "build_kdb"),
                         (subprocess, "Popen"), (subprocess, "run")):
        monkeypatch.setattr(module, name, forbidden)
    for name in ("find_driver", "find_loads", "find_path"):
        monkeypatch.setattr(connectivity_backend.StaticConnectivityBackend, name, forbidden)
    monkeypatch.setattr(server, "_get_parser", forbidden)
    try:
        yield
    finally:
        server.reset_session_state()
        _public_artifact_dir = None
        _public_call_index = 0


def project(root, *, candidate=True):
    source = root / "top.sv"
    source.write_text("module leaf; endmodule\nmodule top; leaf a(); leaf b(); leaf c(); endmodule\n")
    log = root / "compile.log"
    log.write_text("Chronologic VCS simulator\n"
                   f"Command: vcs -sverilog {source} -top top\n"
                   f"Parsing design file '{source}'\nTop Level Modules:\n       top\n")
    maps = root / "simv.daidir" / "kdb.elab++"
    if candidate:
        maps.mkdir(parents=True)
        (maps / "_libFileMap").write_bytes(b"map-A")
        (maps / "_stnElabDB").write_bytes(b"elab-A")
    return source, log, maps


async def public(name, **arguments):
    global _public_call_index
    directory = _public_artifact_dir
    if directory is None:
        # A real-source suite also imports prepare() without the control
        # fixture; its explicit compile input still supplies a private root.
        directory = Path(arguments["compile_log"]).parent / "public-control-evidence"
    _public_call_index += 1  # Assigned before await, including concurrent calls.
    call_dir = directory / f"{_public_call_index:04d}"
    call_dir.mkdir(parents=True, exist_ok=False)
    (call_dir / "request.json").write_text(json.dumps({"tool": name, "arguments": arguments}) + "\n")
    try:
        blocks = await server.call_tool(name, arguments)
    except BaseException as exc:
        (call_dir / "call_exception.json").write_text(json.dumps({
            "module": type(exc).__module__, "type": type(exc).__name__, "message": str(exc)}) + "\n")
        (call_dir / "call_exception.txt").write_text(traceback.format_exc())
        raise
    # Preserve every original block before shape checks or text decoding.
    # repr also survives a malformed/non-model response or serialization error.
    (call_dir / "response.repr.txt").write_text(repr(blocks))
    try:
        raw = [block.model_dump(mode="json") for block in blocks]
        (call_dir / "response.raw.json").write_text(json.dumps({"blocks": raw}) + "\n")
        for index, block in enumerate(raw):
            if isinstance(block.get("text"), str):
                (call_dir / f"block_{index:03d}.raw").write_bytes(block["text"].encode("utf-8"))
        assert len(blocks) == 1 and blocks[0].type == "text"
        value = json.loads(blocks[0].text)
        (call_dir / "decoded.json").write_text(json.dumps(value) + "\n")
        return value
    except BaseException as exc:
        (call_dir / "response_exception.json").write_text(json.dumps({
            "module": type(exc).__module__, "type": type(exc).__name__, "message": str(exc)}) + "\n")
        (call_dir / "response_exception.txt").write_text(traceback.format_exc())
        raise


async def prepare(log, **kwargs):
    payload = await public("prepare_design_context", compile_log=str(log), **kwargs)
    assert "error" not in payload, payload
    assert payload["kdb_status"] == "not_checked" and payload["semantic_status"] == "not_run"
    return payload["design_handle"]


class HandProvider:
    origin, artifact_identity, top = "verdi_npi", "control-artifact", "top"

    def __init__(self, controls):
        self.controls = controls
        self.facts = {"top": HierarchyScope("scope-top", "top", "top", "module", None,
                                           "def-top", "top")}
        for name in ("a", "b", "c"):
            self.facts[f"top.{name}"] = HierarchyScope(
                f"scope-{name}", f"top.{name}", name, "module", "top", "def-leaf", "leaf",
                SourceLocation("/tb/instantiations.sv", 23), SourceLocation("/rtl/leaf.sv", 4),
                (("WIDTH", "8"),))

    def lookup_scope(self, path, budget):
        budget.call()
        self.controls.lookups.append(path)
        return ScopeLookup(self.facts.get(path), True)

    @contextmanager
    def open_children(self, scope, budget):
        self.controls.opens.append(scope.path)
        budget.call()
        children = [self.facts[f"top.{name}"] for name in ("a", "b", "c")] if scope.path == "top" else []

        class Stream:
            complete, gaps = False, ()
            def __init__(self):
                self.position = 0
            def __iter__(self):
                return self
            def __next__(self):
                budget.call()
                if self.position == len(children):
                    self.complete = True
                    raise StopIteration
                fact = children[self.position]
                self.position += 1
                return fact
        try:
            yield Stream()
        finally:
            self.controls.closed.append(scope.path)


class Controls:
    def __init__(self):
        self.requests, self.selections, self.lookups, self.opens, self.closed = [], [], [], [], []
        self.before = self.after = self.selector_action = None
        self.transform = None
        self.thread = threading.get_ident()

    def select(self, status, *, fallback):
        self.selections.append(deepcopy(status))
        if self.selector_action:
            self.selector_action()
        controls = self

        class ControlBackend:
            name, uses_external_worker = "verdi_npi", False
            kdb_status = None
            def bind_compile_context(self, metadata):
                self.compile_result = deepcopy(metadata)
            def bind_kdb_anchor(self, anchor):
                self.anchor = anchor
            def query_source(self, request):
                assert threading.get_ident() == controls.thread, "local native dispatch must stay synchronous"
                assert request.context.compile_result() == self.compile_result
                assert request.operation == "find_tb_instance"
                controls.requests.append(request)
                if controls.before:
                    controls.before()
                value = request.execute(HandProvider(controls))
                value["artifact_identity"] = hashlib.sha256(
                    (self.anchor.identity_sha256 + "\0" + request.context.selected_top).encode()).hexdigest()
                self.kdb_status = {"load_quality": "clean"}
                value["context_identity_sha256"] = request.context.identity_sha256
                value["kdb_binding"] = dict(self.anchor.receipt(), load_status="loaded", load_quality="clean")
                if controls.after:
                    controls.after()
                if controls.transform:
                    controls.transform(value)
                return value
        return ControlBackend()


@pytest.fixture
def controls(monkeypatch):
    value = Controls()
    monkeypatch.setattr(connectivity_backend, "select_backend", value.select)
    return value


@pytest.mark.anyio
async def test_list_tools_exposes_exactly_one_context_selector_and_new_query_controls():
    schema = next(tool.inputSchema for tool in await server.list_tools() if tool.name == "find_tb_instance")
    for value in ({"design_handle": "dc_example", "path": "top.a"},
                  {"handle": "tbh_example", "module": "leaf"},
                  {"design_handle": "dc_example", "module": "leaf", "limit": 2,
                   "count_mode": "exact", "timeout_sec": 1.5}):
        jsonschema.validate(value, schema)
    for value in ({"path": "top.a"}, {"handle": "tbh_x", "design_handle": "dc_x", "path": "top.a"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(value, schema)


@pytest.mark.anyio
async def test_prepare_first_and_repeat_exact_query_without_legacy_tree(tmp_path, monkeypatch, controls):
    _, log, _ = project(tmp_path)
    captures = []
    capture_init = CompileInputCapture.__init__
    def initialize(capture, *args, **kwargs):
        capture_init(capture, *args, **kwargs)
        captures.append(capture)
    monkeypatch.setattr(CompileInputCapture, "__init__", initialize)
    original = design_backend_binding.probe_verdi_backend
    probes = []
    def probe(*args, **kwargs):
        probes.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", probe)
    handle = await prepare(log)
    assert not probes
    outputs = [await public("find_tb_instance", design_handle=handle, path="top.a") for _ in range(2)]
    for index, output in enumerate(outputs):
        server.schemas.SourceInstanceResult.model_validate(output)
        assert output["facts_origin"] == "verdi_npi" and output["total"] == 1
        assert output["hits"][0] == {
            "scope_id": "scope-a", "path": "top.a", "inst": "a", "kind": "module", "parent": "top",
            "definition_id": "def-leaf", "module": "leaf", "source_file": "/tb/instantiations.sv",
            "source_line": 23, "definition_file": "/rtl/leaf.sv", "definition_line": 4,
            "parameterization": {"WIDTH": "8"},
            "gaps": ["npi_definition_location_unbound", "npi_instance_location_unbound"]}
        receipt = output["backend_status"]["design_context"]
        assert receipt["design_handle"] == handle and receipt["binding_reused"] is bool(index)
        assert receipt["artifact"]["load_status"] == "loaded"
        assert receipt["artifact"]["source_association"] == "unverified"
        assert output["backend_status"]["actual_backend"] == "verdi_npi"
    assert len(probes) == 1 and controls.lookups == ["top.a", "top.a"]
    assert controls.opens == [] and controls.closed == []
    assert server._session_state["build_tb_hierarchy"] is None
    with server._design_context_store.lease(handle) as context:
        assert context.current()
    assert captures and all(capture.body_count == capture.resident_bytes == 0 for capture in captures)


@pytest.mark.anyio
async def test_exact_missing_target_is_a_complete_zero_not_provider_unavailable(tmp_path, controls):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    output = await public("find_tb_instance", design_handle=handle, path="top.absent")
    assert output["facts_origin"] == "verdi_npi" and output["hits"] == []
    assert output["total"] == output["count_lower_bound"] == 0
    assert output["count_status"] == "exact" and output["coverage_status"] == "complete"
    assert controls.lookups == ["top.absent"] and not controls.opens


@pytest.mark.anyio
async def test_public_top_keeps_explicit_null_identity_and_location_fields(tmp_path, controls):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    output = await public("find_tb_instance", design_handle=handle, path="top")
    hit = output["hits"][0]
    assert hit["parent"] is None
    for field in ("source_file", "source_line", "definition_file", "definition_line"):
        assert field in hit and hit[field] is None
    assert hit["definition_id"] == "def-top" and hit["module"] == "top"
    assert controls.opens == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode,total,lower,coverage", [("bounded", None, 2, "partial"), ("exact", 3, 3, "complete")])
async def test_module_inventory_separates_display_truncation_from_exact_count(tmp_path, controls, mode, total, lower, coverage):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    output = await public("find_tb_instance", design_handle=handle, module="leaf", limit=1, count_mode=mode)
    assert [hit["path"] for hit in output["hits"]] == ["top.a"]
    assert "total" in output and output["total"] == total
    assert output["count_lower_bound"] == lower and output["coverage_status"] == coverage
    assert output["output_truncated"] is True and output["truncated"] is True
    assert sorted(controls.closed) == sorted(controls.opens)


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [
    {}, {"path": ""}, {"path": 7}, {"path": "top.a", "module": "leaf"},
    {"path": "top.a", "limit": True}, {"path": "top.a", "limit": 0},
    {"path": "top.a", "limit": 5001}, {"path": "top.a", "count_mode": "guess"},
    {"path": "top.a", "timeout_sec": True}, {"path": "top.a", "timeout_sec": 0},
    {"path": "top.a", "timeout_sec": float("inf")}, {"path": "top.a", "timeout_sec": 301},
    {"path": "top.a", "unexpected": 1}, {"path": "top.a", "handle": "tbh_old"},
])
async def test_invalid_query_rejected_before_binding_or_native_selection(tmp_path, monkeypatch, controls, arguments):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    monkeypatch.setattr(design_backend_binding, "prepare_backend_binding", forbidden)
    output = await public("find_tb_instance", design_handle=handle, **arguments)
    assert "error" in output and "hits" not in output
    assert not controls.selections and not controls.requests


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["source", "absent", "native_failure", "ordinary_failure"])
async def test_disabled_source_fallback_is_honestly_unavailable(tmp_path, controls, reason, monkeypatch):
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH", "0")
    _, log, _ = project(tmp_path, candidate=reason != "absent")
    handle = await prepare(log, backend_preference="source" if reason == "source" else "auto")
    if reason in {"native_failure", "ordinary_failure"}:
        def fail():
            if reason == "native_failure":
                raise NpiHierarchyUnavailable("npi_load_failed")
            raise RuntimeError("ordinary control backend error")
        controls.before = fail
    output = await public("find_tb_instance", design_handle=handle, path="top.a")
    server.schemas.SourceQueryEnvelope.model_validate(output)
    assert output["error"] == "semantic_provider_unavailable"
    assert output["semantic_status"] == "unavailable" and output["coverage_status"] == "partial"
    assert output["facts_origin"] == "compile_metadata" and output["analysis_limits"] == []
    assert "hits" not in output and "total" not in output
    assert output["backend_status"].get("actual_backend") is None
    if reason in {"source", "absent"}:
        assert not controls.selections and not controls.requests


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["selector", "query", "final_validation"])
@pytest.mark.parametrize("kind,code", [("source", "design_context_changed"),
    ("timeout", "design_context_timeout"), ("kdb", "kdb_identity_changed"),
    ("policy", "design_context_execution_changed"), ("cancel", None)])
async def test_typed_fatal_never_falls_back_or_returns_instance_facts(tmp_path, monkeypatch, controls, stage, kind, code):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    error = {"source": CaptureAborted("input_changed"), "timeout": TimeoutError("native deadline"),
             "kdb": KdbBindingChanged(), "policy": ExecutionBindingChanged(), "cancel": OperationCancelled()}[kind]
    def fail():
        raise error
    if stage == "selector":
        controls.selector_action = fail
    elif stage == "query":
        controls.before = fail
    else:
        original = server._validate_design_binding
        async def validate(binding, **kwargs):
            await original(binding, **kwargs)
            if controls.requests:
                fail()
        monkeypatch.setattr(server, "_validate_design_binding", validate)
    if kind == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await public("find_tb_instance", design_handle=handle, path="top.a")
    else:
        output = await public("find_tb_instance", design_handle=handle, path="top.a")
        assert output["error_code"] == code and "hits" not in output


@pytest.mark.anyio
@pytest.mark.parametrize("change,code", [("source", "design_context_changed"), ("maps", "kdb_identity_changed"),
                                        ("policy", "design_context_execution_changed"), ("reset", "design_context_generation_changed")])
async def test_final_identity_validation_discards_control_positive(tmp_path, monkeypatch, controls, change, code):
    source, log, maps = project(tmp_path)
    handle = await prepare(log)
    def mutate():
        if change == "source":
            source.write_text("module changed; endmodule\n")
        elif change == "maps":
            (maps / "_libFileMap").write_bytes(b"new-map-content")
        elif change == "policy":
            monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
        else:
            server.reset_session_state()
    controls.after = mutate
    output = await public("find_tb_instance", design_handle=handle, path="top.a")
    assert output["error_code"] == code and "hits" not in output


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["context", "artifact", "origin", "duplicate"])
async def test_foreign_or_invalid_native_payload_cannot_become_a_success_or_fallback(tmp_path, controls, change):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    def corrupt(value):
        if change == "context":
            value["context_identity_sha256"] = "0" * 64
        elif change == "artifact":
            value["artifact_identity"] = "other-native-generation"
        elif change == "origin":
            value["facts_origin"] = "static_inference"
        else:
            value["hits"].append(deepcopy(value["hits"][0]))
            value["total"] = value["count_lower_bound"] = 2
    controls.transform = corrupt
    output = await public("find_tb_instance", design_handle=handle, path="top.a")
    assert "error" in output and "hits" not in output
    assert len(controls.requests) == 1 and len(controls.selections) == 1


@pytest.mark.anyio
async def test_pin_is_held_through_awaited_final_validation(tmp_path, monkeypatch, controls):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    entered, release = anyio.Event(), anyio.Event()
    original = server._validate_design_source
    async def validate(context, **kwargs):
        await original(context, **kwargs)
        if controls.requests:
            entered.set()
            await release.wait()
    monkeypatch.setattr(server, "_validate_design_source", validate)
    result = []
    async def query():
        result.append(await public("find_tb_instance", design_handle=handle, path="top.a"))
    async with anyio.create_task_group() as group:
        group.start_soon(query)
        with anyio.fail_after(5):
            await entered.wait()
        # Busy entries cannot be explicitly removed while publication awaits.
        assert server._design_context_store.remove(handle) is False
        release.set()
    assert result[0]["hits"][0]["path"] == "top.a"


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["top.a", "top.absent"])
async def test_late_global_validation_cannot_publish_exact_count_or_negative(tmp_path, monkeypatch, controls, target):
    _, log, _ = project(tmp_path)
    handle = await prepare(log)
    original = server._validate_design_binding
    async def validate(binding, **kwargs):
        await original(binding, **kwargs)
        if controls.requests:
            controls.requests[-1].budget.deadline = -1
    monkeypatch.setattr(server, "_validate_design_binding", validate)
    output = await public("find_tb_instance", design_handle=handle, path=target)
    assert output["coverage_status"] == "partial" and output["count_status"] == "lower_bound"
    assert "total" in output and output["total"] is None
    assert "timeout_sec" in output["analysis_limits"]
    assert len(output["hits"]) == (1 if target == "top.a" else 0)


@pytest.mark.anyio
async def test_original_legacy_handle_shape_survives_without_native_selection(controls):
    handle = "tbh_control"
    server._handle_store.register(handle, {"component_tree": {"top": {"a": {
        "class": "leaf", "source_file": "/rtl/leaf.sv", "source_line": 4, "children": {}}}}})
    output = await public("find_tb_instance", handle=handle, path="top.a")
    assert output == {"handle": handle, "hits": [{"path": "top.a", "module": "leaf", "parent": "top",
                      "source_file": "/rtl/leaf.sv", "source_line": 4}], "total": 1, "truncated": False}
    assert not controls.requests and not controls.selections


@pytest.mark.anyio
async def test_expired_handle_does_not_recover_a_full_tree(controls):
    output = await public("find_tb_instance", design_handle="dc_expired", path="top.a")
    assert output["error_code"] == "handle_expired" and "hits" not in output
    assert not controls.requests and not controls.selections
