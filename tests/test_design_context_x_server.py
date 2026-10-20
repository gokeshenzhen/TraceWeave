"""Independent design_handle X control tests with hand-written VCD/HDL facts.

No hierarchy, Slang, native NPI, LSF or simulator supplies an oracle. Backend
spies establish routing/provenance and typed facts only; they are not real X
query acceptance, KDB source-association evidence or D1 completion.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import asdict, replace
import json
import os
import subprocess
import threading
import time

import anyio
import pytest

import config
import server
from src import connectivity_backend, design_backend_binding, divergence_context, divergence_routing
from src import kdb_builder, npi_dynamic, npi_lsf, structural_scanner, tb_hierarchy_builder, verdi_npi_backend, x_history
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context import DesignContext
from src.design_context_runtime import DesignContextStore
from src.dynamic_evidence import Expr, unsupported_step
from src.kdb_anchor import KdbBindingChanged, capture_kdb_anchor
from src.prepared_trace_context import PreparedTraceContext
from src.vcd_parser import VCDParser


MODES = ("snapshot", "history")
RTL = """module top(input logic clk,rst,en,d,output logic q,output wire y);
always_ff @(posedge clk) if(rst) q<=1'b0; else if(en) q<=d;
assign y=q;
endmodule
"""


def _forbidden(*args, **kwargs):
    pytest.fail("independent X request reached legacy hierarchy, real frontend, native or process work")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
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
                         (server, "_resolve_hierarchy_context"), (x_history, "resolve_context"),
                         (divergence_context, "resolve_context"), (server, "build_source_graph_trace_plan"),
                         (server, "get_source_graph_runtime")):
        monkeypatch.setattr(module, name, _forbidden)
    # This routing-control suite deliberately disables the optional frontend.
    # Real prepared source traces are exercised in test_prepared_source_trace.
    source_cfg = replace(server.get_source_graph_execution_config(), enabled=False, error_code=None)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda: source_cfg)
    monkeypatch.setattr(divergence_routing, "get_source_graph_execution_config", lambda: source_cfg)
    yield
    server.reset_session_state()


def _wave(path, *, width=1, wrapper=False, changes=None, alias=False):
    rows = {0: dict(clk="0", rst="0", en="1", d="0", q="0", y="0"),
            5: dict(clk="1"), 10: dict(clk="0"), 12: dict(d="x"),
            15: dict(clk="1", q="x", y="x"), 18: dict(d="1"),
            20: dict(clk="0", en="0"), 25: dict(clk="1"),
            30: dict(clk="0"), 35: dict(clk="1"), 40: dict(clk="0")}
    for at, fields in (changes or {}).items():
        rows.setdefault(at, {}).update(fields)
    symbols = dict(clk="!", rst="@", en="#", d="$", q="%", y="%" if alias else "&")
    text = "$timescale 1ps $end\n" + ("$scope module TOP $end\n" if wrapper else "") + "$scope module top $end\n"
    for name, symbol in symbols.items():
        vector = width > 1 and name in {"d", "q", "y"}
        text += f"$var wire {width if vector else 1} {symbol} {name}{f' [{width - 1}:0]' if vector else ''} $end\n"
    text += "$upscope $end\n" + ("$upscope $end\n" if wrapper else "") + "$enddefinitions $end\n"
    for at, values in sorted(rows.items()):
        text += f"#{at}\n"
        for name, value in values.items():
            if width > 1 and name in {"d", "q", "y"}:
                value = {"0": "0" * width, "1": "1" * width, "x": "10xz"}[value]
                text += f"b{value} {symbols[name]}\n"
            else:
                text += f"{value}{symbols[name]}\n"
    path.write_text(text)


async def _public(name, arguments):
    blocks = await server.call_tool(name, arguments)
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


async def _prepare(root, *, preference="auto", width=1, wrapper=False, alias=False, changes=None):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "design.sv"
    source.write_text(RTL if width == 1 else RTL.replace(
        "en,d,output logic q,output wire y",
        "en,input logic [3:0] d,output logic [3:0] q,output wire [3:0] y"))
    log = root / "compile.log"
    simulator = "verilator" if wrapper else "vcs"
    log.write_text((f"make: Entering directory '{root}'\nverilator --cc {source} --top-module top\n" if wrapper else
                    f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top\n"
                    f"Parsing design file '{source}'\nTop Level Modules:\n       top\n"))
    candidate = root / "selected_kdb"
    candidate.mkdir()
    (candidate / "_libFileMap").write_bytes(b"lib_A")
    (candidate / "_stnElabDB").write_bytes(b"elab_A")
    wave = root / "wave.vcd"
    _wave(wave, width=width, wrapper=wrapper, alias=alias, changes=changes)
    receipt = await _public("prepare_design_context", {"compile_log": str(log), "simulator": simulator,
                            "backend_preference": preference, "kdb_path": str(candidate)})
    assert "design_handle" in receipt, receipt
    return receipt["design_handle"], source, log, candidate, wave


def _arguments(handle, wave, mode, **extra):
    result = dict(design_handle=handle, wave_path=str(wave), signal_path="top.q", time_ps=36, mode=mode)
    if mode == "history":
        result["history_start_ps"] = 0
    return {**result, **extra}


def _ref(name, width=1):
    return Expr("signal", width, signal="top." + name, bits=tuple(range(width - 1, -1, -1)),
                declared_bits=tuple(range(width - 1, -1, -1)))


def _step(signal, *, width=1):
    leaf = signal.rsplit(".", 1)[-1]
    base = dict(version="2.0", backend="verdi_npi", signal=signal, bits=list(range(width - 1, -1, -1)),
                width=width, complete=True, gaps=[], clock=None, branches=[],
                sources=[{"file": "npi_control.sv", "line": 1}])
    if leaf == "q":
        base.update(boundary="sequential", state=asdict(_ref("q", width)),
                    clock=dict(expression=asdict(_ref("clk")), edge="posedge"), branches=[
                        dict(id="q_reset", order=0, guard=asdict(_ref("rst")),
                             value=asdict(Expr("const", width, value="0" * width))),
                        dict(id="q_data", order=1,
                             guard=asdict(Expr("and", 1, (Expr("not", 1, (_ref("rst"),)), _ref("en")))),
                             value=asdict(_ref("d", width))),
                    ])
    elif leaf == "y":
        base.update(boundary="combinational", branches=[dict(id="y_q", order=0,
                    guard=asdict(Expr("const", 1, value="1")), value=asdict(_ref("q", width)))])
    else:
        base.update(boundary="input")
    return base


class ControlBackend:
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self, controls, name):
        self.controls, self.name = controls, name
        self.compile_result = self.anchor = None
        self.loaded = False
        self.uses_external_worker = controls.external

    @property
    def kdb_status(self):
        return {"load_quality": "clean"} if self.name == "verdi_npi" and self.loaded else None

    def bind_compile_context(self, metadata):
        self.compile_result = deepcopy(metadata)

    def bind_kdb_anchor(self, anchor):
        self.anchor = anchor

    def _enter(self, operation, signal):
        assert self.compile_result is not None, "every route must use the bound compile copy"
        if self.name == "verdi_npi":
            assert self.anchor is not None
            self.loaded = True
        call = (self.name, operation, signal, deepcopy(self.compile_result), self.anchor)
        self.controls.calls.append(call)
        if self.controls.action:
            self.controls.action(self, operation, signal)

    def find_driver(self, **kwargs):
        signal = kwargs["signal_path"]
        self._enter("driver", signal)
        if self.name == "verdi_npi" and signal == self.controls.failure_signal and self.controls.failure:
            self.loaded = self.controls.failure not in {"npi_load_failed", "npi_lsf_npi_unavailable"}
            return dict(signal_path=signal, resolved_rtl_name=signal.rsplit(".", 1)[-1],
                        driver_status="deferred", upstream_signals=[], backend="source_graph_deferred",
                        _connectivity_fallback_deferred=True, _npi_fallback_reason=self.controls.failure)
        leaf = signal.rsplit(".", 1)[-1]
        upstream = ["top.q"] if leaf == "y" else ["top.d"] if leaf == "q" else []
        return dict(signal_path=signal, wave_path=kwargs["wave_path"], resolved_rtl_name=leaf,
                    driver_status="resolved" if upstream else "testbench_driven", driver_kind="assign",
                    source_file=f"{self.name}_control.sv", source_line=1, upstream_signals=upstream,
                    recursive=kwargs.get("recursive", False), backend=self.name)

    def get_dynamic_step(self, signal_path, **kwargs):
        self._enter("dynamic", signal_path)
        if signal_path == self.controls.failure_signal and self.controls.failure:
            self.loaded = self.controls.failure not in {"npi_load_failed", "npi_lsf_npi_unavailable"}
            return unsupported_step(signal_path, self.name, self.controls.failure)
        step = _step(signal_path, width=self.controls.width)
        if self.controls.step_transform:
            step = self.controls.step_transform(step)
        return step


class Controls:
    def __init__(self):
        self.calls, self.selections, self.static_instances = [], [], []
        self.action = self.selector_action = self.failure = self.step_transform = None
        self.failure_signal = "top.q"
        self.width, self.external = 1, False

    def select(self, status, *, fallback):
        self.selections.append(deepcopy(status))
        if self.selector_action:
            self.selector_action()
        return ControlBackend(self, "verdi_npi")

    def static(self):
        backend = ControlBackend(self, "static")
        self.static_instances.append(backend)
        return backend


@pytest.fixture
def controls(monkeypatch):
    controls = Controls()
    monkeypatch.setattr(connectivity_backend, "select_backend", controls.select)
    monkeypatch.setattr(divergence_routing, "select_backend", controls.select)
    monkeypatch.setattr(connectivity_backend, "StaticConnectivityBackend", controls.static)
    monkeypatch.setattr(divergence_routing, "StaticConnectivityBackend", controls.static)
    return controls


async def _trace(handle, wave, mode, **kwargs):
    return await _public("trace_x_source", _arguments(handle, wave, mode, **kwargs))


def _history(result):
    assert result["mode"] == "history" and "error_code" not in result, result
    return result["history"]


def _npi_calls(controls):
    return [call for call in controls.calls if call[0] == "verdi_npi"]


@pytest.mark.anyio
async def test_prepared_trace_view_has_identity_and_source_snapshot_without_a_hierarchy(tmp_path):
    handle, source, _, _, _ = await _prepare(tmp_path)
    with server._design_context_store.pin(handle) as entry:
        view = PreparedTraceContext(prepared=entry, design_handle=handle)
        assert view.hierarchy is None and view.snapshot == entry.context.identity_sha256
        assert view.context["context_identity_sha256"] == entry.context.identity_sha256
        assert view.context["source_snapshot_sha256"] == entry.context.snapshot.content_fingerprint_sha256
        metadata = view.compile_result
        metadata["top_modules"].append("caller_mutation")
        assert view.compile_result["top_modules"] == ["top"]
        assert view.current()
        source.write_text("module changed; endmodule\n")
        assert not view.current()


@pytest.mark.anyio
async def test_public_trace_schema_accepts_handle_and_retains_history_inputs():
    tool = next(t for t in await server.list_tools() if t.name == "trace_x_source")
    schema = tool.inputSchema
    assert "compile_log" not in schema["required"]
    assert {"required": ["design_handle"]} in schema["anyOf"]
    assert {"required": ["compile_log"]} in schema["anyOf"]
    assert schema["properties"]["mode"]["default"] == "snapshot"
    assert schema["properties"]["phase"]["enum"] == ["before", "after"]
    assert any(row.get("then", {}).get("required") == ["history_start_ps"] for row in schema["allOf"])


@pytest.mark.anyio
async def test_same_handle_snapshot_and_history_use_one_binding_and_no_old_tree(tmp_path, controls, monkeypatch):
    handle, _, _, candidate, wave = await _prepare(tmp_path)
    # Any attempted legacy fallback/lookup fails, even if old session state exists.
    server._session_state["build_tb_hierarchy"] = {"compile_log": "/unrelated/old.log", "simulator": "xcelium"}
    original = server.prepare_backend_binding
    discoveries = []
    def observed(context, **kwargs):
        discoveries.append(context.identity_sha256)
        return original(context, **kwargs)
    monkeypatch.setattr(server, "prepare_backend_binding", observed)
    snapshot = await _trace(handle, wave, "snapshot", signal_path="top.y")
    history = await _trace(handle, wave, "history")
    assert snapshot["backend_status"]["actual_backend"] == "verdi_npi"
    assert all(n["source_file"] == "verdi_npi_control.sv" for n in snapshot["propagation_chain"])
    assert _history(history)["context"]["backend_status"]["actual_backend"] == "verdi_npi"
    assert len(discoveries) == 1 and not controls.static_instances
    for index, result in enumerate((snapshot, history)):
        receipt = result["backend_status"]["design_context"]
        assert receipt["design_handle"] == handle and receipt["binding_reused"] is bool(index)
        assert receipt["artifact"]["source_association"] == "unverified"
        assert receipt["artifact"]["load_status"] == "loaded"
    assert all(call[3]["top_modules"] == ["top"] and call[4].path == str(candidate) for call in _npi_calls(controls))


@pytest.mark.anyio
async def test_history_retains_predecessor_injection_and_two_holds(tmp_path, controls):
    handle, _, _, _, wave = await _prepare(tmp_path)
    result = _history(await _trace(handle, wave, "history"))
    nodes = result["nodes"]
    assert [(n["signal"], n["time_ps"], n["phase"]) for n in nodes] == [
        ("top.q", 36, "after"), ("top.q", 35, "before"),
        ("top.q", 25, "before"), ("top.d", 15, "before")]
    assert [n["relation"] for n in nodes[:3]] == ["hold", "hold", "register_sample"]
    assert [n["observation"]["trigger_time_ps"] for n in nodes[:3]] == [35, 25, 15]
    assert nodes[0]["interval"]["active_interval_start_time_fs"] == 15000
    assert nodes[-1]["interval"]["active_interval_start_time_fs"] == 12000
    assert nodes[-1]["value"] == "x" and result["operation_metrics"]["observation_cache_hits"] > 0
    assert any(d["signal"] == "top.en" and d["value"] == "0" for d in nodes[0]["checked_dependencies"])
    assert any(d["signal"] == "top.d" and d["value"] == "x" for d in nodes[2]["checked_dependencies"])
    assert not result["coverage"]["true_origin_proven"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", ["clean", "missing_signal", "missing_wave"])
async def test_no_native_query_never_reports_a_loaded_candidate(tmp_path, controls, mode, case):
    handle, _, _, _, wave = await _prepare(tmp_path)
    kwargs = {"time_ps": 1} if case == "clean" else {"signal_path": "top.absent"} if case == "missing_signal" else {}
    if case == "missing_wave":
        wave = wave.with_name("absent.vcd")
    result = await _trace(handle, wave, mode, **kwargs)
    assert not _npi_calls(controls)
    status = result.get("backend_status", {})
    if status.get("design_context", {}).get("artifact"):
        assert status["design_context"]["artifact"]["load_status"] == "not_checked"
    assert status.get("kdb_load_status") not in {"loaded", "failed"}
    if case == "clean":
        assert (result["trace_status"] if mode == "snapshot" else _history(result)["status"]) == "signal_is_clean"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("policy", ["source", "route"])
async def test_source_policy_uses_bound_static_and_honest_disabled_source(tmp_path, controls, monkeypatch, mode, policy):
    handle, _, _, _, wave = await _prepare(tmp_path, preference="source" if policy == "source" else "auto")
    if policy == "route":
        monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", _forbidden)
    monkeypatch.setattr(design_backend_binding, "capture_kdb_anchor", _forbidden)
    result = await _trace(handle, wave, mode)
    assert not controls.selections and not _npi_calls(controls)
    assert "source_graph_disabled" in json.dumps(result)
    assert result["backend_status"]["actual_backend"] == "static"
    assert controls.static_instances and all(b.compile_result["top_modules"] == ["top"] for b in controls.static_instances)
    assert result["backend_status"]["design_context"]["selection"] == "source_policy"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_mid_trace_fallback_restarts_from_root_and_discards_npi_evidence(tmp_path, controls, mode):
    handle, _, _, _, wave = await _prepare(tmp_path)
    controls.failure = "npi_operator_semantics_unresolved" if mode == "history" else "npi_query_failed"
    result = await _trace(handle, wave, mode, signal_path="top.y")
    assert [c[2] for c in _npi_calls(controls)] == ["top.y", "top.q"]
    static_calls = [c for c in controls.calls if c[0] == "static"]
    assert static_calls and static_calls[0][2] == "top.y"
    assert "verdi_npi_control.sv" not in json.dumps(result)
    assert "npi_control.sv" not in json.dumps(result)
    assert result["backend_status"]["actual_backend"] == "static"
    assert result["backend_status"]["whole_trace_restart_count"] >= 1
    if mode == "history":
        graph = _history(result)
        assert all(n["structure"]["backend"] == "static" for n in graph["nodes"])
        assert graph["operation_metrics"]["backend_query_count"] >= 3
        assert graph["operation_metrics"]["restart_count"] >= 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("reason", ["npi_load_failed", "npi_lsf_npi_unavailable", "npi_operator_semantics_unresolved"])
async def test_only_native_availability_failure_is_remembered_across_x_requests(tmp_path, controls, mode, reason):
    handle, _, _, _, wave = await _prepare(tmp_path)
    controls.failure = reason
    first = await _trace(handle, wave, mode)
    selected = len(controls.selections)
    second = await _trace(handle, wave, mode)
    remembered = reason in {"npi_load_failed", "npi_lsf_npi_unavailable"}
    assert len(controls.selections) == selected + (0 if remembered else 1)
    assert first["backend_status"]["design_context"].get("previous_npi_failure") is None
    assert second["backend_status"]["design_context"].get("previous_npi_failure") == (reason if remembered else None)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("stage", ["selector", "query"])
@pytest.mark.parametrize("failure,code", [("cancel", None), ("input_changed", "design_context_changed"),
                                        ("timeout", "design_context_timeout"), ("kdb", "kdb_identity_changed"),
                                        ("execution", "design_context_execution_changed")])
async def test_typed_abort_never_falls_back_or_returns_a_partial_graph(tmp_path, controls, mode, stage, failure, code):
    handle, _, _, _, wave = await _prepare(tmp_path)
    error = {"cancel": OperationCancelled(), "input_changed": CaptureAborted("input_changed"),
             "timeout": CaptureAborted("timeout"), "kdb": KdbBindingChanged(), "execution": ExecutionBindingChanged()}[failure]
    def fail(*args):
        raise error
    if stage == "selector":
        controls.selector_action = fail
    else:
        controls.action = fail
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await _trace(handle, wave, mode)
    else:
        result = await _trace(handle, wave, mode)
        assert result["error_code"] == code and "history" not in result and "propagation_chain" not in result
    assert not controls.static_instances


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("change", ["source", "maps", "policy", "wave", "parser"])
async def test_mid_query_identity_change_discards_all_old_evidence(tmp_path, controls, monkeypatch, mode, change):
    handle, source, _, candidate, wave = await _prepare(tmp_path)
    changed = []
    def mutate(backend, operation, signal):
        if changed:
            return
        changed.append(True)
        if change == "source":
            source.write_text("module replacement; endmodule\n")
        elif change == "maps":
            path = candidate / "_libFileMap"
            before = path.stat()
            path.write_bytes(b"lib_B")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == "policy":
            monkeypatch.setenv("LM_LICENSE_FILE", "changed_control_policy")
        elif change == "wave":
            wave.write_text(wave.read_text() + "\n")
        else:
            server._get_parser(str(wave))._scope_epoch = object()
    controls.action = mutate
    result = await _trace(handle, wave, mode)
    if "error_code" in result:
        assert result["error_code"] in {"design_context_changed", "kdb_identity_changed", "design_context_execution_changed", "waveform_changed", "parser_generation_changed"}
    else:
        assert mode == "history"
        graph = _history(result)
        assert graph["status"] == "inconclusive"
        assert not graph["nodes"] and not graph["edges"] and not graph["candidates"]
        assert any("changed" in row["reason"] for row in graph["frontier"])
    assert not controls.static_instances


@pytest.mark.anyio
async def test_history_bits_phase_and_alias_keep_distinct_coordinate_evidence(tmp_path, controls):
    controls.width = 4
    handle, _, _, _, wave = await _prepare(tmp_path, width=4, alias=True)
    selected = _history(await _trace(handle, wave, "history", signal_path="top.q[3:0]", signal_bits=[0, 2]))
    assert [(n["value"], n["bits"]) for n in selected["nodes"]] == [("z0", [0, 2])] * 4
    clean = _history(await _trace(handle, wave, "history", signal_path="top.q[3:0]", signal_bits=[3, 2]))
    assert clean["status"] == "signal_is_clean" and clean["operation_metrics"]["backend_query_count"] == 0
    before = _history(await _trace(handle, wave, "history", signal_path="top.q[3:0]", time_ps=15, phase="before"))
    after = _history(await _trace(handle, wave, "history", signal_path="top.q[3:0]", time_ps=15, phase="after"))
    assert before["status"] == "signal_is_clean" and after["nodes"][0]["value"] == "10xz"
    alias = _history(await _trace(handle, wave, "history", signal_path="top.y[3:0]", signal_bits=[0, 2]))
    assert [n["signal"] for n in alias["nodes"][:2]] == ["top.y[3:0]", "top.q[3:0]"]


@pytest.mark.anyio
@pytest.mark.parametrize("limit,value", [("max_nodes", 1), ("max_depth", 0), ("max_events", 1), ("max_read_bytes", 1024)])
async def test_history_cumulative_budget_is_partial_without_negative_claims(tmp_path, controls, limit, value):
    handle, _, _, _, wave = await _prepare(tmp_path)
    result = _history(await _trace(handle, wave, "history", **{limit: value}))
    assert result["status"] in {"partial", "inconclusive"} and result["coverage"]["truncated"]
    assert any(row.get("budget") == limit for row in result["frontier"])
    assert result["coverage"]["true_origin_proven"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["missing_window", "reverse_window", "phase", "bits", "snapshot_history_flags", "unknown_mode"])
async def test_history_public_input_rejects_invalid_controls_before_backend_selection(tmp_path, controls, invalid):
    handle, _, _, _, wave = await _prepare(tmp_path)
    args = _arguments(handle, wave, "history")
    if invalid == "missing_window":
        args.pop("history_start_ps")
    elif invalid == "reverse_window":
        args["history_start_ps"] = 40
    elif invalid == "phase":
        args["phase"] = "during"
    elif invalid == "bits":
        args["signal_bits"] = ["0"]
    else:
        args["mode"] = "snapshot" if invalid == "snapshot_history_flags" else "unrecognized"
    result = await _public("trace_x_source", args)
    # Ordinary input validation retains the existing public error shape;
    # fixed design-identity codes belong to the typed context failure paths.
    expected_error = {
        "missing_window": "history_start_ps",
        "reverse_window": "history_start_ps <= time_ps",
        "phase": "phase",
        "bits": "signal_bits",
        "snapshot_history_flags": "history parameters require mode=history",
        "unknown_mode": "mode must be snapshot or history",
    }[invalid]
    assert expected_error in result.get("error", "") and "history" not in result
    assert not _npi_calls(controls) and not controls.selections


@pytest.mark.anyio
async def test_history_async_controls_preserve_typed_gap_without_invented_assignment(tmp_path, controls):
    handle, _, _, _, wave = await _prepare(tmp_path, changes={12: dict(rst="1", q="x", y="x")})
    def async_step(step):
        if step["signal"] == "top.q":
            step.update(complete=False, branches=[], gaps=["temporal_context_unavailable", "async_control_value_unmodeled"],
                        async_controls=[dict(expression=asdict(_ref("rst")), kind="reset", active_value="1", assertion_edge="posedge")])
        return step
    controls.step_transform = async_step
    result = _history(await _trace(handle, wave, "history", time_ps=13))
    assert not controls.static_instances
    assert "typed_async_control_observations" in result["coverage"]["checks"]
    node = result["nodes"][0]
    assert node["async_observation"]["assignment_value_status"] == "unmodeled"
    control = node["async_observation"]["controls"][0]
    assert control["edge_at_unknown_onset"] is True and control["last_observed_edge_ps"] == 12
    assert "async_control_value_unmodeled" in node["structure"]["gaps"]
    actions = next(row["next_actions"] for row in result["candidates"] if row["kind"] == "async_control_onset_candidate")
    assert len(actions) == 2 and {a["arguments"]["signal_path"] for a in actions} == {"top.q", "top.rst"}
    assert all(a["tool"] == "explain_signal_driver" and a["arguments"]["design_handle"] == handle
               and "compile_log" not in a["arguments"] for a in actions)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_top_wrapper_mapping_uses_independent_compile_metadata(tmp_path, controls, mode):
    handle, _, _, _, wave = await _prepare(tmp_path, wrapper=True)
    result = await _trace(handle, wave, mode, signal_path="TOP.top.q")
    assert result["start_signal"] == "TOP.top.q"
    assert _npi_calls(controls) and all(c[2].startswith("top.") for c in _npi_calls(controls))
    if mode == "history":
        nodes = _history(result)["nodes"]
        assert [(n["signal"], n["time_ps"]) for n in nodes] == [
            ("TOP.top.q", 36), ("TOP.top.q", 35), ("TOP.top.q", 25), ("TOP.top.d", 15)]
    else:
        assert result["wave_design_binding"]["source_signal"] == "top.q"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_backend_and_identity_work_are_outside_wave_locks(tmp_path, controls, monkeypatch, mode):
    handle, _, _, _, wave = await _prepare(tmp_path)
    lock = server._wave_locks_for([str(wave)])[0]
    original_source, original_binding = DesignContext.current, design_backend_binding.DesignBackendBinding.require_current
    reads = []
    def source_current(self, *args, **kwargs):
        assert not lock.locked()
        return original_source(self, *args, **kwargs)
    def binding_current(self, *args, **kwargs):
        assert not lock.locked()
        return original_binding(self, *args, **kwargs)
    controls.action = lambda *args: pytest.fail("backend held waveform lock") if lock.locked() else None
    original_value = VCDParser.get_value_at_time
    original_declaration = VCDParser.get_signal_declaration
    def sampled(self, *args, **kwargs):
        assert lock.locked()
        reads.append(1)
        return original_value(self, *args, **kwargs)
    def declared(self, *args, **kwargs):
        assert lock.locked()
        reads.append(1)
        return original_declaration(self, *args, **kwargs)
    monkeypatch.setattr(DesignContext, "current", source_current)
    monkeypatch.setattr(design_backend_binding.DesignBackendBinding, "require_current", binding_current)
    monkeypatch.setattr(VCDParser, "get_value_at_time", sampled)
    monkeypatch.setattr(VCDParser, "get_signal_declaration", declared)
    result = await _trace(handle, wave, mode)
    assert "error_code" not in result
    assert reads


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_cancelled_awaited_x_query_releases_pin_without_fallback(tmp_path, controls, monkeypatch, mode):
    monkeypatch.setattr(server, "_design_context_store", DesignContextStore(max_entries=1))
    handle, _, _, _, wave = await _prepare(tmp_path)
    store = server._design_context_store
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    controls.external = True  # Execution-placement control only, not real LSF.
    def blocked(*args):
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()
    controls.action = blocked
    task = asyncio.create_task(_trace(handle, wave, mode))
    try:
        with anyio.fail_after(5):
            while not entered.is_set():
                await anyio.sleep(0.001)
        assert store.remove(handle) is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        with anyio.fail_after(5):
            while not finished.is_set():
                await anyio.sleep(0.001)
        await asyncio.gather(task, return_exceptions=True)
    assert not controls.static_instances and store.remove(handle)


@pytest.mark.anyio
async def test_history_prelude_timeout_is_not_reset_when_graph_starts(tmp_path, controls, monkeypatch):
    handle, _, _, _, wave = await _prepare(tmp_path)
    original = server.prepare_backend_binding
    finished = threading.Event()
    def delayed(context, **kwargs):
        try:
            time.sleep(0.15)
            return original(context, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(server, "prepare_backend_binding", delayed)
    result = await _trace(handle, wave, "history", timeout_sec=0.03)
    with anyio.fail_after(5):
        while not finished.is_set():
            await anyio.sleep(0.001)
    assert result["error_code"] == "design_context_timeout"
    assert not _npi_calls(controls) and not controls.static_instances
    with server._design_context_store.pin(handle) as entry:
        assert entry.binding is None


@pytest.mark.anyio
async def test_reset_during_history_query_cannot_return_old_graph_or_note_failure(tmp_path, controls):
    handle, _, _, _, wave = await _prepare(tmp_path)
    old_store = server._design_context_store
    entered, release = threading.Event(), threading.Event()
    controls.external = True
    def blocked(*args):
        entered.set()
        assert release.wait(5)
    controls.action = blocked
    task = asyncio.create_task(_trace(handle, wave, "history"))
    try:
        with anyio.fail_after(5):
            while not entered.is_set():
                await anyio.sleep(0.001)
        assert old_store.remove(handle) is False
        server.reset_session_state()
        release.set()
        result = await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert result["error_code"] == "design_context_generation_changed" and "history" not in result
    with old_store.pin(handle) as entry:
        assert entry.npi_failure is None
    assert not controls.static_instances and old_store.remove(handle)


@pytest.mark.anyio
async def test_history_graph_timeout_retains_original_partial_budget_contract(tmp_path, controls):
    handle, _, _, _, wave = await _prepare(tmp_path)
    # Warm the binding so this is graph work, not the prelude timeout case.
    await _trace(handle, wave, "history", time_ps=1)
    controls.action = lambda *args: time.sleep(0.08)
    result = _history(await _trace(handle, wave, "history", timeout_sec=0.04))
    assert any(row.get("budget") == "timeout_sec" for row in result["frontier"])
    assert result["coverage"]["truncated"] and not result["coverage"]["true_origin_proven"]
    assert not controls.static_instances


@pytest.mark.anyio
@pytest.mark.parametrize("placement", ["local", "lsf"])
@pytest.mark.parametrize("failure", ["target", "load"])
async def test_actual_dynamic_backend_clears_old_load_status_but_keeps_successful_partial_load(
    tmp_path, monkeypatch, placement, failure,
):
    """Exercise real backend methods; only execution boundaries are simulated."""
    handle, _, log, candidate, _ = await _prepare(tmp_path)
    with server._design_context_store.pin(handle) as entry:
        metadata = entry.context.compile_result()
    anchor = capture_kdb_anchor(str(candidate))
    complete = _step("top.q")
    partial = unsupported_step("top.q", "verdi_npi", "npi_operator_semantics_unresolved")
    transport_calls, load_calls = [], []
    if placement == "local":
        backend = verdi_npi_backend.VerdiNpiBackend()
        state = {"load": True, "step": complete}
        def load(path, top):
            load_calls.append((path, top))
            return state["load"]
        monkeypatch.setattr(backend, "_ensure_loaded", load)
        monkeypatch.setattr(npi_dynamic, "query_step", lambda *args, **kwargs: deepcopy(state["step"]))
        monkeypatch.setattr(verdi_npi_backend, "_silence_native_stdio", nullcontext)
    else:
        outcomes = [npi_lsf.LsfExecutionResult(complete, "completed", "completed", kdb_load_quality="clean")]
        if failure == "load":
            outcomes.append(npi_lsf.LsfExecutionResult(None, "completed", "npi_unavailable", "npi_lsf_npi_unavailable"))
        outcomes.append(npi_lsf.LsfExecutionResult(partial, "completed", "completed", kdb_load_quality="clean"))
        class Transport:
            def execute(self, request):
                transport_calls.append(request)
                return outcomes.pop(0)
        cfg = config.NpiExecutionConfig(mode="lsf", queue="control_only", bsub_bin="forbidden_bsub",
            bkill_bin="forbidden_bkill", python_bin="python3", timeout_sec=30, staging_dir=tmp_path / "unused_stage")
        backend = npi_lsf.LsfConnectivityBackend(cfg, transport=Transport())
    backend.bind_compile_context(metadata)
    backend.bind_kdb_anchor(anchor)
    first = backend.get_dynamic_step("top.q", str(log))
    assert first["complete"] and backend.kdb_status["load_quality"] == "clean"
    if failure == "target":
        backend.bind_compile_context({**metadata, "top_modules": [], "primary_top": None})
    elif placement == "local":
        state["load"] = False
    failed = backend.get_dynamic_step("top.q", str(log))
    assert not failed["complete"] and backend.kdb_status is None
    if placement == "lsf" and failure == "load":
        assert failed["_npi_execution_status"]["worker_status"] == "npi_unavailable"
    backend.bind_compile_context(metadata)
    if placement == "local":
        state.update(load=True, step=partial)
    result = backend.get_dynamic_step("top.q", str(log))
    assert not result["complete"] and result["gaps"] == ["npi_operator_semantics_unresolved"]
    assert backend.kdb_status["load_quality"] == "clean"
    assert len(load_calls if placement == "local" else transport_calls) == (2 if failure == "target" else 3)
