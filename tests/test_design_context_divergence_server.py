"""Dual design_handle traces with independent VCD and typed control oracles.

No SDK, Slang, full hierarchy, scheduler or simulation is an oracle here. Real
text/map capture and public serialization establish ownership/identity facts;
backend controls establish routing only, not EDA or D1 acceptance.
"""
from __future__ import annotations

import asyncio
import builtins
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import anyio
import pytest

import config
import server
from src import connectivity_backend, design_backend_binding, divergence_design_context, divergence_routing, divergence_trace
from src import kdb_anchor, sv_preprocessor, tb_hierarchy_builder, verdi_npi_backend
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.compile_session_snapshot import CompileSessionSnapshotBuilder
from src.cursor_store import CursorStore
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context import DesignContext
from src.design_context_runtime import ContextUnavailable, DesignContextStore
from src.divergence_context import log_identity, resolve_context
from src.dynamic_evidence import Expr, unsupported_step
from src.hierarchy_handles import HandleStore, compute_handle, compute_snapshot_fingerprint
from src.kdb_anchor import KdbAnchor, KdbBindingChanged


REAL_RESOLVE = divergence_trace.resolve_context
RTL = "module top(input logic clk,en,input logic [7:0] d,output logic [7:0] q); always_ff @(posedge clk) if(en) q<=d; endmodule\n"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _forbidden(*args, **kwargs):
    pytest.fail("independent divergence reached a full tree, frontend, native API or subprocess")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    server.reset_session_state()
    monkeypatch.setattr(server, "_handle_store", HandleStore())
    monkeypatch.setattr(server, "_cursor_store", CursorStore())
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    for module, name in ((server, "build_hierarchy"), (server, "scan_structural_risks"),
            (server, "apply_npi_source_overlay"), (tb_hierarchy_builder, "build_hierarchy"),
            (tb_hierarchy_builder, "_scan_user_files"), (tb_hierarchy_builder, "scan_sv_file"),
            (tb_hierarchy_builder, "build_component_tree"), (sv_preprocessor.SystemVerilogPreprocessor, "preprocess"),
            (sv_preprocessor, "_expand_hierarchy_macro_line"), (divergence_trace, "resolve_context"),
            (server, "get_source_graph_runtime"), (server, "build_source_graph_trace_plan"),
            (verdi_npi_backend, "_import_pynpi"), (subprocess, "Popen"), (subprocess, "run")):
        monkeypatch.setattr(module, name, _forbidden)
    source_config = replace(server.get_source_graph_execution_config(), enabled=False, error_code=None)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda: source_config)
    monkeypatch.setattr(divergence_routing, "get_source_graph_execution_config", lambda: source_config)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pynpi", "pyslang"}:
            _forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    captures = []
    original_capture = CompileInputCapture.__init__
    def captured(self, *args, **kwargs):
        original_capture(self, *args, **kwargs)
        captures.append(self)
    monkeypatch.setattr(CompileInputCapture, "__init__", captured)
    yield
    assert all(capture.body_count == 0 and capture.resident_bytes == 0 for capture in captures)
    server.reset_session_state()


def _ref(name, width=1):
    return Expr("signal", width, signal="top." + name, bits=tuple(range(width - 1, -1, -1)),
                declared_bits=tuple(range(width - 1, -1, -1)))


def _step(signal, backend):
    result = dict(version="2.0", backend=backend, signal=signal, width=8, bits=list(range(7, -1, -1)),
                  complete=True, gaps=[], clock=None, branches=[], sources=[dict(file=backend + "_control.sv", line=1)])
    if signal == "top.q":
        result.update(boundary="sequential", state=asdict(_ref("q", 8)),
            clock=dict(expression=asdict(_ref("clk")), edge="posedge"),
            branches=[dict(id="q_d", order=0, guard=asdict(_ref("en")), value=asdict(_ref("d", 8)))])
    else:
        result.update(boundary="input")
    return result


class ControlBackend:
    execution_mode, uses_external_worker = "local", False

    def __init__(self, controls, name):
        self.controls, self.name, self.loaded = controls, name, False
        self.metadata = self.anchor = None
        self.uses_external_worker = controls.external

    def bind_compile_context(self, metadata):
        self.metadata = deepcopy(metadata)

    def bind_kdb_anchor(self, anchor):
        self.anchor = anchor

    @property
    def kdb_status(self):
        return {"load_quality": "clean"} if self.name == "verdi_npi" and self.loaded else None

    def get_dynamic_step(self, signal_path, **kwargs):
        self.controls.calls.append((self.name, signal_path, deepcopy(self.metadata), self.anchor))
        assert not server._FSDB_WAVE_LOCK.locked()
        assert all(not lock.locked() for lock in server._wave_locks_for(self.controls.waves))
        if self.controls.query_action:
            self.controls.query_action(self, signal_path)
        if self.controls.reason and signal_path == "top.q":
            self.loaded = self.controls.reason != "npi_load_failed"
            return unsupported_step(signal_path, self.name, self.controls.reason)
        self.loaded = True
        return _step(signal_path, self.name)

    def find_driver(self, **kwargs):
        self.controls.calls.append(("static", kwargs["signal_path"], deepcopy(self.metadata), None))
        return dict(signal_path=kwargs["signal_path"], wave_path=kwargs["wave_path"], resolved_rtl_name="q",
                    driver_status="partial", driver_kind="unknown", backend="static", recursive=False)


class Controls:
    def __init__(self):
        self.calls, self.selections, self.bindings, self.waves = [], [], [], []
        self.query_action = self.selector_action = self.binding_action = self.reason = None
        self.external = False

    def select(self, status, *, fallback):
        self.selections.append(deepcopy(status))
        if self.selector_action:
            self.selector_action()
        return ControlBackend(self, "verdi_npi")

    def static(self):
        return ControlBackend(self, "static")


@pytest.fixture
def controls(monkeypatch):
    owner = Controls()
    monkeypatch.setattr(connectivity_backend, "select_backend", owner.select)
    monkeypatch.setattr(divergence_routing, "select_backend", owner.select)
    monkeypatch.setattr(divergence_routing, "StaticConnectivityBackend", owner.static)
    original = server.prepare_backend_binding
    def binding(context, **kwargs):
        owner.bindings.append((context.identity_sha256, dict(kwargs)))
        if owner.binding_action:
            owner.binding_action(context)
        return original(context, **kwargs)
    monkeypatch.setattr(server, "prepare_backend_binding", binding)
    return owner


def _wave(path, *, different=True):
    data = "01101010" if different else "01010101"
    path.write_text("$timescale 1ps $end\n$scope module top $end\n"
        "$var wire 1 ! clk $end\n$var wire 1 @ en $end\n$var wire 8 # d $end\n$var wire 8 $ q $end\n"
        "$upscope $end\n$enddefinitions $end\n#0\n0!\n1@\nb01010101 #\nb00000000 $\n"
        f"#3\nb{data} #\n#5\n1!\nb{data} $\n#10\n0!\n#15\n1!\n#20\n0!\n")


async def _public(tool, arguments):
    blocks = await server.call_tool(tool, arguments)
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


async def _design(root, *, source=False, supplement=False):
    root.mkdir(parents=True, exist_ok=True)
    rtl, log, candidate = root / "top.sv", root / "compile.log", root / "selected_kdb"
    rtl.write_text(RTL)
    log.write_text("Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {rtl} -top top\nParsing design file '{rtl}'\nTop Level Modules:\n       top\n")
    candidate.mkdir()
    (candidate / "_libFileMap").write_bytes(b"lib_A")
    (candidate / "_stnElabDB").write_bytes(b"elab_A")
    extra = []
    if supplement:
        package = root / "support.sv"
        package.write_text("package support; endpackage\n")
        phase = root / "support.log"
        phase.write_text(f"Chronologic VCS\nCommand: vlogan -sverilog {package}\nParsing design file '{package}'\n")
        extra.append(str(phase))
    result = await _public("prepare_design_context", dict(compile_log=str(log), simulator="vcs", top_hint="top",
        supplementary_compile_logs=extra, backend_preference="source" if source else "auto", kdb_path=str(candidate)))
    assert "design_handle" in result, result
    return SimpleNamespace(handle=result["design_handle"], source=rtl, log=log, candidate=candidate, supplements=extra,
                           identity=result["context_identity_sha256"])


async def _case(root, controls, *, same=True, source=False, equal=False, supplement=False):
    a = await _design(root / "a", source=source, supplement=supplement)
    b = a if same else await _design(root / "b", source=source, supplement=supplement)
    wa, wb = root / "a.vcd", root / "b.vcd"
    _wave(wa, different=False)
    _wave(wb, different=not equal)
    controls.waves = [str(wa), str(wb)]
    args = dict(side_a=dict(design_handle=a.handle, wave_path=str(wa), signal_path="top.q"),
                side_b=dict(design_handle=b.handle, wave_path=str(wb), signal_path="top.q"),
                start_time_ps=0, end_time_ps=20)
    if not same:
        args["signal_pairs"] = [dict(a="top.d", b="top.d"), dict(a="top.en", b="top.en")]
    return a, b, args


async def _trace(arguments):
    return await _public("trace_divergence", arguments)


def _discarded(result, code):
    assert result["status"] == "inconclusive", result
    assert result["comparison"] == {}
    assert result["nodes"] == result["edges"] == result["findings"] == []
    assert result.get("cursor") is None
    assert code in result["coverage"]["gaps"]
    assert all(row["node_id"] is None for row in result["frontier"])


@pytest.mark.anyio
@pytest.mark.parametrize("same", [True, False])
async def test_handwritten_register_sampling_oracle_and_per_side_load_receipts(tmp_path, controls, same):
    a, b, args = await _case(tmp_path, controls, same=same)
    result = await _trace(args)
    server.schemas.TraceDivergenceResult.model_validate(result)
    assert result["comparison"]["first_divergence_time_ps"] == 5
    assert len(result["nodes"]) == 2 and len(result["edges"]) == 1
    root, data = result["nodes"]
    assert root["a"]["observation"]["trigger_time_ps"] == root["b"]["observation"]["trigger_time_ps"] == 5
    assert data["a"]["signal"] == data["b"]["signal"] == "top.d"
    assert data["a"]["phase"] == data["b"]["phase"] == "before"
    assert data["a"]["value"] == "01010101" and data["b"]["value"] == "01101010"
    assert len(controls.bindings) == (1 if same else 2)
    assert result["operation_metrics"]["design_context_count"] == (1 if same else 2)
    for label, design in (("a", a), ("b", b)):
        row = result["contexts"][label]
        assert row["design_context"]["design_handle"] == design.handle
        assert row["design_context"]["artifact"]["load_status"] == "not_checked"
        assert row["backend_status"]["kdb_load_status"] == "loaded"
        assert row["backend_status"]["actual_backend"] == "verdi_npi"
        assert row["design_context"]["artifact"]["source_association"] == "unverified"
    if same:
        assert result["contexts"]["b"]["design_context"]["metrics_owner_side"] == "a"
        assert "preparation_metrics" not in result["contexts"]["b"]["design_context"]
        assert "final_validation_ms" not in result["contexts"]["b"]["design_context"]
    actions = [row for row in result["next_actions"] if row["tool"] == "explain_signal_driver"]
    assert len(actions) == 2
    assert [row["arguments"]["design_handle"] for row in actions] == [a.handle, b.handle]
    assert all(row["status"] == "ready" and "compile_context" not in row["arguments"] for row in actions)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["event", "clock"])
async def test_no_difference_does_not_claim_native_load(tmp_path, controls, mode):
    _, _, args = await _case(tmp_path, controls, equal=True)
    if mode == "clock":
        args["comparison"] = dict(mode="clock", clock_a="top.clk", clock_b="top.clk", sample_offset_ps=1)
    result = await _trace(args)
    assert result["status"] == "no_difference" and result["nodes"] == [] and result.get("cursor") is None
    assert controls.calls == controls.selections == []
    assert len(controls.bindings) == 1
    assert result["operation_metrics"]["backend_query_count"] == 0
    for row in result["contexts"].values():
        assert row["design_context"]["artifact"]["load_status"] == "not_checked"
        assert row.get("backend_status", {}).get("kdb_load_status") != "loaded"


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['initial', 'empty', 'wrong_declaration', 'operator'])
async def test_unmodeled_initial_leaf_retains_native_chain_only_with_source_evidence(
        tmp_path, monkeypatch, controls, case):
    _, _, args = await _case(tmp_path, controls)
    original = ControlBackend.get_dynamic_step
    def query(self, signal_path, **kwargs):
        result = original(self, signal_path, **kwargs)
        if self.name == 'verdi_npi' and signal_path in {'top.d', 'top.d[7:0]'}:
            result.update(boundary='unsupported', complete=False, branches=[],
                gaps=['npi_operator_semantics_unresolved' if case == 'operator'
                      else 'dynamic_evidence_unavailable'], state=asdict(_ref('d', 8)),
                sources=[] if case == 'empty' else [dict(kind='initial', cell='top:2:Init.Q',
                    location='/design.sv:2', declaration='top.other' if case == 'wrong_declaration' else 'top.d')])
        return result
    monkeypatch.setattr(ControlBackend, 'get_dynamic_step', query)
    result = await _trace(args)
    assert result['status'] == 'partial'
    assert not any(f['kind'] == 'local_logic_candidate' for f in result['findings'])
    if case == 'initial':
        assert len(result['nodes']) == 2 and len(result['edges']) == 1
        leaf = result['nodes'][1]
        assert leaf['status'] == 'frontier'
        assert {f['reason'] for f in result['frontier']} == {'dynamic_evidence_unavailable'}
        assert {f['side'] for f in result['frontier']} == {'a', 'b'}
        assert result['coverage']['restart_reasons'] == []
        assert not any(call[0] == 'static' for call in controls.calls)
        for label in ('a', 'b'):
            assert leaf[label]['structure']['backend'] == 'verdi_npi'
            assert not leaf[label]['structure']['complete']
            assert leaf[label]['structure']['sources'][0]['kind'] == 'initial'
            assert result['contexts'][label]['backend_status']['actual_backend'] == 'verdi_npi'
    else:
        assert 'npi_dynamic_fallback' in result['coverage']['restart_reasons']
        assert any(call[0] == 'static' for call in controls.calls)


@pytest.mark.anyio
async def test_omitted_supplements_and_explicit_matching_metadata_are_distinct(tmp_path, controls):
    a, _, args = await _case(tmp_path, controls, supplement=True)
    assert (await _trace(args))["comparison"]["first_divergence_time_ps"] == 5
    args["side_a"].update(compile_log=str(a.log), simulator="vcs", top_hint="top", supplementary_compile_logs=a.supplements)
    assert (await _trace(args))["comparison"]["first_divergence_time_ps"] == 5
    args["side_a"]["supplementary_compile_logs"] = []
    before = len(controls.calls)
    _discarded(await _trace(args), "design_context_arguments_mismatch")
    assert len(controls.calls) == before


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["hierarchy_handle", "snapshot_sha256", "source_snapshot_sha256", "compile_log_identity_sha256"])
async def test_new_handle_and_old_identity_tokens_are_rejected_before_work(tmp_path, controls, field):
    _, _, args = await _case(tmp_path, controls)
    args["side_a"][field] = "tbh_old" if field == "hierarchy_handle" else "a" * 64
    result = await _trace(args)
    assert "error" in result
    assert controls.bindings == controls.calls == []
    assert len(server._cursor_store) == 0


@pytest.mark.anyio
async def test_expired_handle_suggests_explicit_prepare_and_never_a_tree(tmp_path, controls):
    a, _, args = await _case(tmp_path, controls)
    assert server._design_context_store.remove(a.handle)
    args["side_a"]["compile_log"] = str(a.log)
    result = await _trace(args)
    _discarded(result, "handle_expired")
    assert controls.calls == controls.bindings == []
    prerequisites = [call for action in result["next_actions"] for call in action["prerequisite_calls"]]
    assert [call["tool"] for call in prerequisites] == ["prepare_design_context", "scan_structural_risks"]
    assert all(call["arguments"]["compile_log"] == str(a.log) for call in prerequisites)
    assert all(call["depends_on"] == [] and call["parallel_group"] == "compile_a" for call in prerequisites)
    assert result["next_actions"][0]["evidence"]["preparation_identity"] == "current_inputs_only"


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["binding", "selector", "query"])
@pytest.mark.parametrize("failure,code", [("context", "design_context_changed"), ("changed", "design_context_changed"),
    ("timeout", "design_context_timeout"), ("kdb", "kdb_identity_changed"),
    ("policy", "design_context_execution_changed"), ("timeout_error", "design_context_timeout")])
async def test_typed_lifecycle_failure_discards_entire_comparison_and_preserves_cursor(tmp_path, monkeypatch, controls, stage, failure, code):
    _, _, args = await _case(tmp_path, controls)
    server._cursor_store.set("existing", 99, note="must survive")
    args["cursor_name"] = "existing"
    errors = dict(context=ContextUnavailable("design_context_changed"), changed=CaptureAborted("input_changed"),
        timeout=CaptureAborted("timeout"), kdb=KdbBindingChanged(), policy=ExecutionBindingChanged(), timeout_error=TimeoutError())
    def fail(*args):
        raise errors[failure]
    setattr(controls, stage + "_action", fail)
    result = await _trace(args)
    _discarded(result, code)
    assert server._cursor_store.get("existing").time_ps == 99
    assert len(server._cursor_store) == 1
    assert not any(call[0] == "static" for call in controls.calls)


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["binding", "query"])
async def test_operation_cancelled_propagates_without_cursor_or_fallback(tmp_path, controls, stage):
    a, _, args = await _case(tmp_path, controls)
    def cancelled(*args):
        raise OperationCancelled()
    setattr(controls, stage + "_action", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _trace(args)
    assert len(server._cursor_store) == 0
    assert not any(call[0] == "static" for call in controls.calls)
    assert server._design_context_store.remove(a.handle)


@pytest.mark.anyio
@pytest.mark.parametrize("limit", ["max_depth", "max_nodes"])
async def test_real_resource_budget_remains_partial_with_final_checks(tmp_path, monkeypatch, controls, limit):
    _, _, args = await _case(tmp_path, controls)
    args["limits"] = {limit: 0 if limit == "max_depth" else 1}
    result = await _trace(args)
    assert result["status"] == "partial" and result["comparison"]["first_divergence_time_ps"] == 5
    assert result["coverage"]["truncated"] and "budget_exhausted" in result["coverage"]["gaps"]
    assert result["operation_metrics"]["final_identity_validation_ms"] > 0
    if limit == "max_depth":
        assert controls.calls == []
        assert result["contexts"]["a"]["design_context"]["artifact"]["load_status"] == "not_checked"


@pytest.mark.anyio
async def test_source_policy_honestly_uses_static_and_reports_disabled_source(tmp_path, controls):
    _, _, args = await _case(tmp_path, controls, source=True)
    result = await _trace(args)
    assert controls.selections == []
    assert "source_graph_disabled" in json.dumps(result)
    assert controls.calls
    assert all(call[0] == "static" and call[2]["top_modules"] == ["top"] for call in controls.calls)
    assert result["status"] == "partial"


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["npi_load_failed", "npi_operator_semantics_unresolved"])
async def test_only_availability_failures_are_shared_between_sides_and_future_requests(tmp_path, controls, reason):
    a, _, args = await _case(tmp_path, controls)
    controls.reason = reason
    first = await _trace(args)
    selected = len(controls.selections)
    assert first["operation_metrics"]["restart_count"] >= 1
    for label in ("a", "b"):
        sources = {node[label]["structure"]["backend"] for node in first["nodes"] if "structure" in node[label]}
        assert sources <= {"static"}
    with server._design_context_store.pin(a.handle) as entry:
        assert entry.npi_failure == (reason if reason == "npi_load_failed" else None)
    await _trace(args)
    assert (len(controls.selections) == selected) is (reason == "npi_load_failed")


@pytest.mark.anyio
async def test_final_raw_validation_deduplicates_handle_but_not_wave_generations(tmp_path, monkeypatch, controls):
    _, _, args = await _case(tmp_path, controls)
    active = [False]
    source_checks, map_checks = [], []
    original_final = divergence_design_context.DivergenceDesigns.validate_final
    original_source, original_map = DesignContext.current, KdbAnchor.current
    async def final(self, *args):
        active[0] = True
        try:
            return await original_final(self, *args)
        finally:
            active[0] = False
    def source(self, **kwargs):
        if active[0]:
            source_checks.append(self.identity_sha256)
            assert threading.get_ident() != loop_thread
        return original_source(self, **kwargs)
    def maps(self, **kwargs):
        if active[0]:
            map_checks.append(self.identity_sha256)
            assert threading.get_ident() != loop_thread
        return original_map(self, **kwargs)
    loop_thread = threading.get_ident()
    monkeypatch.setattr(divergence_design_context.DivergenceDesigns, "validate_final", final)
    monkeypatch.setattr(DesignContext, "current", source)
    monkeypatch.setattr(KdbAnchor, "current", maps)
    result = await _trace(args)
    assert len(source_checks) == len(map_checks) == 1
    assert result["operation_metrics"]["design_context_count"] == 1
    assert result["contexts"]["a"]["design_context"]["final_validation_ms"] > 0


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["source", "map", "wave", "parser", "reset"])
async def test_final_validation_discards_graph_and_cursor_when_evidence_changes(tmp_path, monkeypatch, controls, changed):
    a, _, args = await _case(tmp_path, controls)
    original = divergence_design_context.DivergenceDesigns.validate_final
    async def final(self, *rest):
        if changed == "source":
            a.source.write_text(RTL + "// new source\n")
        elif changed == "map":
            (a.candidate / "_libFileMap").write_bytes(b"larger map generation")
        elif changed == "wave":
            path = args["side_b"]["wave_path"]
            with open(path, "a") as stream:
                stream.write("#21\n")
        elif changed == "parser":
            parser = server._get_parser(args["side_b"]["wave_path"])
            parser._scope_epoch = object()
        else:
            server.reset_session_state()
        return await original(self, *rest)
    monkeypatch.setattr(divergence_design_context.DivergenceDesigns, "validate_final", final)
    result = await _trace(args)
    code = dict(source="design_context_changed", map="kdb_identity_changed", wave="waveform_changed",
                parser="parser_generation_changed", reset="design_context_generation_changed")[changed]
    _discarded(result, code)
    assert len(server._cursor_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["source", "map"])
async def test_cross_side_metadata_recheck_catches_a_changing_during_b_hash(tmp_path, monkeypatch, controls, changed):
    a, b, args = await _case(tmp_path, controls, same=False)
    active = [False]
    original_final = divergence_design_context.DivergenceDesigns.validate_final
    original = DesignContext.current
    async def final(self, *rest):
        active[0] = True
        try:
            return await original_final(self, *rest)
        finally:
            active[0] = False
    def current(self, **kwargs):
        result = original(self, **kwargs)
        if active[0] and self.identity_sha256 == b.identity:
            path = a.source if changed == "source" else a.candidate / "_libFileMap"
            with open(path, "ab") as stream:
                stream.write(b"\nchanged during B\n")
        return result
    monkeypatch.setattr(divergence_design_context.DivergenceDesigns, "validate_final", final)
    monkeypatch.setattr(DesignContext, "current", current)
    _discarded(await _trace(args), "design_context_changed" if changed == "source" else "kdb_identity_changed")
    assert len(server._cursor_store) == 0


@pytest.mark.anyio
async def test_metadata_only_map_check_is_not_substituted_for_raw_validation(tmp_path, monkeypatch, controls):
    original_stat, original_fstat = kdb_anchor.os.stat, kdb_anchor.os.fstat
    def coarse(value):
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
        fields.update(st_mtime_ns=0, st_ctime_ns=0, st_mtime=0, st_ctime=0)
        return SimpleNamespace(**fields)
    monkeypatch.setattr(kdb_anchor.os, "stat", lambda *args, **kwargs: coarse(original_stat(*args, **kwargs)))
    monkeypatch.setattr(kdb_anchor.os, "fstat", lambda *args, **kwargs: coarse(original_fstat(*args, **kwargs)))
    a, _, args = await _case(tmp_path, controls)
    original_final = divergence_design_context.DivergenceDesigns.validate_final
    async def final(self, *rest):
        path = a.candidate / "_libFileMap"
        path.write_bytes(b"lib_B")
        anchor = self.entries[a.handle].binding.anchor
        assert anchor.metadata_current() is True
        assert anchor.current() is False
        return await original_final(self, *rest)
    monkeypatch.setattr(divergence_design_context.DivergenceDesigns, "validate_final", final)
    _discarded(await _trace(args), "kdb_identity_changed")
    assert len(server._cursor_store) == 0


def _legacy_view(design):
    # A minimal exact compatibility view; not a claimed real hierarchy build.
    with server._design_context_store.pin(design.handle) as entry:
        metadata = entry.context.compile_result()
    raw = dict(compile_log=str(design.log), simulator="vcs", top_hint="top", supplementary_compile_logs=[])
    snapshot = compute_snapshot_fingerprint(str(design.log), "vcs")
    handle = compute_handle(str(design.log), "vcs")
    builder = CompileSessionSnapshotBuilder()
    builder.read_text(str(design.source))
    full = dict(compile_result=metadata, project=dict(top_module="top"), component_tree={"top": {}},
                _compile_session_snapshot=builder.finish(), _hierarchy_snapshot_sha256=snapshot,
                _compile_log_identity_sha256=log_identity(raw))
    server._handle_store.register(handle, full)
    bound, reason = resolve_context(raw, server._handle_store)
    assert reason == "ready"
    return bound.context


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_side", ["a", "b"])
@pytest.mark.parametrize("mapping", [False, True])
async def test_mixed_contexts_keep_legacy_tokens_and_conservative_mapping(tmp_path, monkeypatch, controls, legacy_side, mapping):
    a, _, args = await _case(tmp_path, controls)
    token = _legacy_view(a)
    original_side = args["side_" + legacy_side]
    args["side_" + legacy_side] = {**token, "wave_path": original_side["wave_path"], "signal_path": "top.q"}
    monkeypatch.setattr(divergence_trace, "resolve_context", REAL_RESOLVE)
    monkeypatch.setattr(server, "probe_verdi_backend", lambda *args, **kwargs: dict(
        kdb_path=str(a.candidate), kdb_flow="vcs_two_step", kdb_validation_status="usable"))
    if mapping:
        args["signal_pairs"] = [dict(a="top.d", b="top.d"), dict(a="top.en", b="top.en")]
    result = await _trace(args)
    assert result["contexts"]["a"]["design_identity_sha256"] != result["contexts"]["b"]["design_identity_sha256"]
    if mapping:
        assert len(result["nodes"]) == 2
    else:
        assert "mapping_missing" in result["coverage"]["gaps"]
        assert not any(row["kind"] == "local_logic_candidate" for row in result["findings"])
        relay = next(row for row in result["next_actions"] if row["tool"] == "trace_divergence")
        new_side = "b" if legacy_side == "a" else "a"
        assert relay["arguments"]["side_" + new_side]["design_handle"] == a.handle
    actions = {row["side"]: row for row in result["next_actions"] if row["tool"] == "explain_signal_driver"}
    assert actions[legacy_side]["arguments"]["compile_context"] == token
    other = "b" if legacy_side == "a" else "a"
    assert actions[other]["arguments"]["design_handle"] == a.handle


def _barrier(started, release, stopped):
    started.set()
    try:
        while not release.wait(0.005):
            from src.cancellation import check_cancelled
            check_cancelled()
    finally:
        stopped.set()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_both_entries_are_pinned_before_first_prepare_await_and_released(tmp_path, monkeypatch, controls, cancel):
    a, b, args = await _case(tmp_path, controls, same=False)
    third = await _design(tmp_path / "third")
    store = server._design_context_store
    with store.pin(third.handle) as entry:
        third_context = entry.context
    assert store.remove(third.handle)
    store.max_entries = 2
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    controls.binding_action = lambda context: _barrier(started, release, stopped) if context.identity_sha256 == a.identity else None
    task = asyncio.create_task(_trace(args))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        with pytest.raises(ContextUnavailable) as raised:
            store.register(third_context)
        assert raised.value.code == "context_capacity"
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(stopped.wait, 1)
        else:
            release.set()
            assert (await task)["comparison"]["first_divergence_time_ps"] == 5
        assert store.remove(a.handle) and store.remove(b.handle)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_ab_ba_requests_do_not_nest_binding_locks_or_repeat_discovery(tmp_path, controls):
    _, _, args = await _case(tmp_path, controls, same=False)
    opposite = {**args, "side_a": deepcopy(args["side_b"]), "side_b": deepcopy(args["side_a"])}
    both_started, release, lock = threading.Event(), threading.Event(), threading.Lock()
    seen = set()
    def binding(context):
        with lock:
            seen.add(context.identity_sha256)
            if len(seen) == 2:
                both_started.set()
        while not release.wait(0.005):
            from src.cancellation import check_cancelled
            check_cancelled()
    controls.binding_action = binding
    tasks = [asyncio.create_task(_trace(arguments)) for arguments in (args, opposite)]
    try:
        assert await asyncio.to_thread(both_started.wait, 1)
        release.set()
        with anyio.fail_after(3):
            results = await asyncio.gather(*tasks)
        assert all(result["comparison"]["first_divergence_time_ps"] == 5 for result in results)
        assert len(controls.bindings) == 2
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_first_binding_and_queue_time_consume_original_budget(tmp_path, controls):
    a, _, args = await _case(tmp_path, controls)
    args["limits"] = dict(timeout_sec=0.1)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    controls.binding_action = lambda context: _barrier(started, release, stopped)
    try:
        result = await _trace(args)
        assert started.is_set() and await asyncio.to_thread(stopped.wait, 1)
        assert result["status"] == "inconclusive" and not result["comparison"]
        assert "budget_exhausted" in result["coverage"]["gaps"]
        assert result["operation_metrics"]["final_identity_validation_ms"] > 0
        assert controls.calls == []
        assert 0 < controls.bindings[0][1]["timeout_sec"] <= 0.1
        assert server._design_context_store.remove(a.handle)
    finally:
        release.set()
