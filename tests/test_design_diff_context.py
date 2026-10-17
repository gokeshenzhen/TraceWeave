"""Prepared contexts on the public waveform diff/action boundary.

Tiny real captures, registry entries and handwritten VCD events provide the
identity/time oracle. No native query, backend discovery or semantic frontend
is permitted. Map fixtures establish bounded anchor identity only, not a
loadable KDB or license behavior. Mixed legacy tokens use real snapshots.
"""
from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
import subprocess
import threading

import anyio
import jsonschema
import pytest

import config
import server
from src import connectivity_backend, design_backend_binding, design_context_runtime
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.compile_session_snapshot import CompileSessionSnapshotBuilder
from src.cursor_store import CursorStore
from src.design_backend_binding import DesignBackendBinding
from src.design_context import DesignContext
from src.design_context_runtime import ContextUnavailable, DesignContextStore
from src.divergence_context import log_identity, resolve_context
from src.hierarchy_handles import compute_handle, compute_snapshot_fingerprint
from src.kdb_anchor import capture_kdb_anchor
from src.schemas import DivergenceQueryContext
from src.vcd_parser import VCDParser


def forbidden(*args, **kwargs):
    raise AssertionError("diff/action requested a backend, full tree, frontend, subprocess or implicit preparation")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    server.reset_session_state()
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    for module, name in ((server, "build_hierarchy"), (server, "scan_structural_risks"),
            (server, "prepare_backend_binding"), (server, "probe_verdi_backend"),
            (server, "_query_prepared_hierarchy"), (server, "get_source_graph_runtime"),
            (server, "_prepare_pinned_design_entry"), (server, "_build_recovery_context"),
            (design_backend_binding, "prepare_backend_binding"), (design_backend_binding, "probe_verdi_backend"),
            (connectivity_backend, "select_backend"), (subprocess, "Popen"), (subprocess, "run")):
        monkeypatch.setattr(module, name, forbidden)
    original_import = builtins.__import__

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if any(part in {"pynpi", "pyslang"} for item in (name, *(str(x) for x in fromlist or ()))
               for part in item.split(".")):
            forbidden()
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    captures = []
    original_capture = CompileInputCapture.__init__

    def captured(self, *args, **kwargs):
        original_capture(self, *args, **kwargs)
        captures.append(self)

    monkeypatch.setattr(CompileInputCapture, "__init__", captured)
    yield
    assert all(item.body_count == 0 and item.resident_bytes == 0 for item in captures)
    server.reset_session_state()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@dataclass
class Design:
    handle: str
    context: DesignContext
    source: object
    log: object
    supplements: list


def design(root, *, top="top", policy="auto", supplement=False):
    source = write(root / "design.sv", "module top; wire [3:0] q; endmodule\nmodule other; endmodule\n")
    log = write(root / "compile.log", f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top -top other\n"
        f"Parsing design file '{source}'\nTop Level Modules:\n       top\n       other\n")
    supplements = []
    if supplement:
        package = write(root / "support.sv", "package support; endpackage\n")
        supplements.append(str(write(root / "support.log", f"Chronologic VCS simulator\nCommand: vlogan {package}\n"
                                    f"Parsing design file '{package}'\n")))
    context = design_context_runtime.prepare_design_context(str(log), simulator="vcs", top_hint=top,
        backend_preference=policy, supplementary_compile_logs=supplements)
    handle = server._design_context_store.register(context)
    return Design(handle, context, source, log, supplements)


def wave(path, *, different=False):
    # Both sides agree at 0 and 5. B.q (or A.r in a same-file comparison)
    # becomes 1010 at exactly 7 ps, while A.q remains 0011 until 12 ps.
    return write(path, "$timescale 1ps $end\n$scope module top $end\n"
        "$var wire 4 ! q [3:0] $end\n$var wire 4 \" r [3:0] $end\n"
        "$upscope $end\n$enddefinitions $end\n#0\nb0000 !\nb0000 \"\n"
        "#5\nb0011 !\nb0011 \"\n#7\n" + ("b1010 !\n" if different else "b0011 !\n")
        + "b1010 \"\n#12\nb0101 !\nb0101 \"\n#20\nb0101 !\nb0101 \"\n")


def case(root, *, same=False, equal=False, same_wave=False, source=False, supplement=False):
    a = design(root / "a", policy="source" if source else "auto", supplement=supplement)
    b = a if same else design(root / "b", policy="source" if source else "auto", supplement=supplement)
    wa = wave(root / "a.vcd")
    wb = wa if same_wave else wave(root / "b.vcd", different=not equal)
    args = dict(wave_path_a=str(wa), signal_a="top.q", wave_path_b=str(wb),
                signal_b="top.r" if same_wave else "top.q", start_time_ps=0, end_time_ps=20,
                context_a={"design_handle": a.handle}, context_b={"design_handle": b.handle}, cursor_name="diff_anchor")
    return a, b, args


async def public(arguments):
    blocks = await server.call_tool("diff_first_divergence", arguments)
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


class Controls:
    def __init__(self, monkeypatch):
        self.patch, self.armed = monkeypatch, False
        self.source_calls, self.binding_calls, self.wave_calls, self.parser_calls = [], [], [], []
        self.pin_events, self.cursor_writes = [], []
        self.before_source = self.after_source = self.after_wave = self.parser_hook = None
        self.main_thread = threading.get_ident()
        original_pin = DesignContextStore.pin
        original_source, original_binding = server._validate_design_source, server._validate_design_binding
        original_wave, original_parser = server._run_in_wave_request, server._get_parser
        original_current, original_metadata = DesignContext.current, DesignContext.metadata_current
        original_set, original_auto = CursorStore.set, CursorStore.auto_set

        @contextmanager
        def pin(store, handle):
            with original_pin(store, handle) as entry:
                observing = self.armed and store is self.store
                if observing: self.pin_events.append(("enter", handle))
                try:
                    yield entry
                finally:
                    if observing: self.pin_events.append(("exit", handle))

        async def source(context, **kwargs):
            if self.armed:
                self.outside_wave()
                self.all_pinned()
                self.source_calls.append(context.identity_sha256)
                occurrence = self.source_calls.count(context.identity_sha256)
                await self.fire(self.before_source, context, occurrence)
            result = await original_source(context, **kwargs)
            if self.armed: await self.fire(self.after_source, context, occurrence)
            return result

        async def binding(value, **kwargs):
            if self.armed:
                self.outside_wave()
                self.all_pinned()
                self.binding_calls.append(value)
            return await original_binding(value, **kwargs)

        async def waveform(paths, fn, **kwargs):
            if self.armed:
                self.outside_wave()
                self.all_pinned()
            result = await original_wave(paths, fn, **kwargs)
            if self.armed:
                self.wave_calls.append(tuple(paths))
                await self.fire(self.after_wave, len(self.wave_calls))
            return result

        def parser(path):
            if self.armed:
                assert threading.get_ident() != self.main_thread
                assert all(lock.locked() for lock in self.locks)
                self.all_pinned()
                self.parser_calls.append(path)
            value = original_parser(path)
            if self.armed and self.parser_hook:
                replacement = self.parser_hook(path, value, self.parser_calls.count(path))
                if replacement is not None: value = replacement
            return value

        def current(context, **kwargs):
            if self.armed: self.outside_wave()
            return original_current(context, **kwargs)

        def metadata(context, **kwargs):
            if self.armed: self.outside_wave()
            return original_metadata(context, **kwargs)

        def publish(method):
            def wrapped(store, *args, **kwargs):
                if self.armed:
                    assert threading.get_ident() == self.main_thread
                    self.outside_wave()
                    self.all_pinned()
                    assert all(self.source_calls.count(identity) >= 2 for identity in self.identities)
                    self.cursor_writes.append((store, args, deepcopy(kwargs)))
                return method(store, *args, **kwargs)
            return wrapped

        monkeypatch.setattr(DesignContextStore, "pin", pin)
        monkeypatch.setattr(server, "_validate_design_source", source)
        monkeypatch.setattr(server, "_validate_design_binding", binding)
        monkeypatch.setattr(server, "_run_in_wave_request", waveform)
        monkeypatch.setattr(server, "_get_parser", parser)
        monkeypatch.setattr(DesignContext, "current", current)
        monkeypatch.setattr(DesignContext, "metadata_current", metadata)
        monkeypatch.setattr(CursorStore, "set", publish(original_set))
        monkeypatch.setattr(CursorStore, "auto_set", publish(original_auto))

    async def fire(self, hook, *args):
        if hook:
            result = hook(*args)
            if inspect.isawaitable(result): await result

    def arm(self, arguments):
        self.store = server._design_context_store
        self.handles = list(dict.fromkeys(ctx["design_handle"] for key in ("context_a", "context_b")
            if (ctx := arguments.get(key)) and ctx.get("design_handle") is not None))
        rows = {row["design_handle"]: row for row in self.store.summaries()}
        self.identities = {rows[h]["context_identity_sha256"] for h in self.handles if h in rows}
        self.paths = [arguments["wave_path_a"], arguments["wave_path_b"]]
        self.locks = server._wave_locks_for(self.paths)
        self.patch.setattr(server, "_handle_prepare_design_context", forbidden)
        self.armed = True

    def outside_wave(self):
        assert not server._FSDB_WAVE_LOCK.locked()
        assert all(not lock.locked() for lock in self.locks)

    def all_pinned(self):
        # Called before the first actual validation await, inside every parser
        # request, and at cursor publication; checks all unique sides together.
        for handle in self.handles:
            assert self.store._entries[handle].leases == 1

    def released(self):
        assert all(entry.leases == 0 for entry in self.store._entries.values())
        assert sorted(h for event, h in self.pin_events if event == "enter") == sorted(
            h for event, h in self.pin_events if event == "exit")
        assert all(not lock.locked() for lock in self.locks)


@pytest.fixture
def controls(monkeypatch):
    value = Controls(monkeypatch)
    yield value
    if value.armed: value.released()


def failed(result, controls, code):
    assert result.get("error_code") == code, result
    assert "diverged" not in result and not result.get("next_actions") and not result.get("cursor")
    assert controls.cursor_writes == [] and len(server._cursor_store) == 0
    controls.released()


def add_binding(design, root):
    candidate = root / "captured_kdb"
    candidate.mkdir(parents=True)
    (candidate / "_libFileMap").write_bytes(b"fixture library map\n")
    (candidate / "_stnElabDB").write_bytes(b"fixture elaboration map\n")
    anchor = capture_kdb_anchor(str(candidate))
    assert anchor.artifact_status == "elaborated_candidate" and anchor.identity_complete
    binding = DesignBackendBinding(design.context.identity_sha256, design.context.backend_preference, anchor,
        json.dumps({"simulator": "vcs", "kdb_path": str(candidate), "kdb_flow": "explicit"}),
        config.get_npi_execution_config(), config.get_connectivity_route_config(), (),
        config.NPI_ALLOW_DEGRADED_KDB, "candidate", anchor.gaps, ())
    with server._design_context_store.pin(design.handle):
        server._design_context_store.publish_binding(design.handle, binding)
    return binding, candidate


def legacy(design):
    ctx = dict(compile_log=str(design.log), simulator="vcs", top_hint=design.context.selected_top,
               supplementary_compile_logs=list(design.supplements))
    snapshot = compute_snapshot_fingerprint(str(design.log), "vcs", design.supplements)
    handle = compute_handle(str(design.log), "vcs", design.supplements)
    source = CompileSessionSnapshotBuilder()
    for path in (str(design.log), *design.supplements, str(design.source)): source.read_text(path)
    captured = source.finish()
    full = dict(project={"top_module": design.context.selected_top}, compile_result=design.context.compile_result(),
        component_tree={design.context.selected_top: {}}, _hierarchy_snapshot_sha256=snapshot,
        _compile_session_snapshot=captured, _compile_log_identity_sha256=log_identity(ctx))
    server._handle_store.register(handle, full)
    return dict(ctx, hierarchy_handle=handle, snapshot_sha256=snapshot,
                source_snapshot_sha256=captured.content_fingerprint_sha256,
                compile_log_identity_sha256=log_identity(ctx))


@pytest.mark.anyio
@pytest.mark.parametrize("same,same_wave", [(False, False), (True, False), (True, True)])
async def test_real_vcd_first_difference_actions_bind_each_side_without_backend_work(tmp_path, controls, same, same_wave):
    a, b, args = case(tmp_path, same=same, same_wave=same_wave)
    controls.arm(args)
    result = await public(args)
    server.schemas.DiffFirstDivergenceResult.model_validate(result)
    assert result["diverged"] and result["first_divergence_time_ps"] == 7
    assert result["value_a"] == "0011" and result["value_b"] == "1010"
    assert result["cursor"]["time_ps"] == 7 and result["cursor"]["name"] == "diff_anchor"
    assert len(controls.cursor_writes) == 1
    actions = result["next_actions"]
    assert len(actions) == 2 and [row["side"] for row in actions] == ["a", "b"]
    for side, design, action in zip(("a", "b"), (a, b), actions):
        assert action["status"] == "ready" and action["prerequisite_calls"] == []
        assert action["arguments"] == dict(wave_path=args[f"wave_path_{side}"], signal_path=args[f"signal_{side}"],
                                           recursive=False, design_handle=design.handle)
        assert action["evidence"]["context_identity_sha256"] == design.context.identity_sha256
        assert action["evidence"]["cursor_name"] == "diff_anchor"
    unique = 1 if same else 2
    assert len([event for event in controls.pin_events if event[0] == "enter"]) == unique
    assert len(controls.source_calls) == 2 * unique and controls.binding_calls == []
    assert len(controls.wave_calls) == 2
    assert all(not row["binding_observed"] for row in controls.store.summaries())
    assert server._session_state["build_tb_hierarchy"] is None


@pytest.mark.anyio
async def test_public_schema_accepts_independent_or_legacy_sides_and_actions(tmp_path, controls):
    a, b, args = case(tmp_path)
    schema = next(item.inputSchema for item in await server.list_tools() if item.name == "diff_first_divergence")
    jsonschema.validate(args, schema)
    legacy_args = {**args, "context_a": {"compile_log": str(a.log), "simulator": "vcs"}}
    jsonschema.validate(legacy_args, schema)
    assert DivergenceQueryContext.model_validate(args["context_a"]).design_handle == a.handle
    assert DivergenceQueryContext.model_validate(legacy_args["context_a"]).compile_log == str(a.log)
    controls.arm(args)
    result = await public(args)
    driver_schema = next(item.inputSchema for item in await server.list_tools() if item.name == "explain_signal_driver")
    for action in result["next_actions"]: jsonschema.validate(action["arguments"], driver_schema)


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["hierarchy_handle", "snapshot_sha256", "source_snapshot_sha256", "compile_log_identity_sha256"])
async def test_prepared_and_legacy_identity_tokens_are_rejected_before_pin_or_wave(tmp_path, controls, field):
    _, _, args = case(tmp_path)
    args["context_a"][field] = "tbh_old" if field == "hierarchy_handle" else "a" * 64
    controls.arm(args)
    result = await public(args)
    assert "error" in result and "diverged" not in result
    assert controls.pin_events == controls.source_calls == controls.wave_calls == controls.cursor_writes == []


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [("compile_log", "/foreign/compile.log"), ("simulator", "xcelium"),
    ("top_hint", "other"), ("supplementary_compile_logs", [])])
async def test_conflicting_context_metadata_is_rejected_before_validation_or_wave(tmp_path, controls, field, value):
    _, _, args = case(tmp_path, supplement=True)
    args["context_b"][field] = value
    controls.arm(args)
    failed(await public(args), controls, "design_context_arguments_mismatch")
    assert not controls.source_calls and not controls.wave_calls


@pytest.mark.anyio
async def test_matching_explicit_context_and_omitted_supplements_both_use_captured_identity(tmp_path, controls):
    a, b, args = case(tmp_path, supplement=True)
    args["context_a"].update(compile_log=str(a.log), simulator="vcs", top_hint="top", supplementary_compile_logs=a.supplements)
    controls.arm(args)
    result = await public(args)
    assert result["diverged"] and [row["arguments"]["design_handle"] for row in result["next_actions"]] == [a.handle, b.handle]


@pytest.mark.anyio
async def test_same_primary_different_top_and_poison_recent_context_do_not_rebind_actions(tmp_path, controls):
    a, _, args = case(tmp_path, same=True)
    context = design_context_runtime.prepare_design_context(str(a.log), simulator="vcs", top_hint="other")
    other = server._design_context_store.register(context)
    args["context_b"] = {"design_handle": other}
    server._session_state["build_tb_hierarchy"] = dict(compile_log="/ambient/compile.log", simulator="xcelium")
    server._result_provenance["build_tb_hierarchy"] = dict(server._session_state["build_tb_hierarchy"])
    server._session_state["get_sim_paths"] = dict(compile_log="/another/recent.log", simulator="verilator")
    controls.arm(args)
    result = await public(args)
    assert result["diverged"]
    assert [row["arguments"]["design_handle"] for row in result["next_actions"]] == [a.handle, other]
    assert [row["evidence"]["context_identity_sha256"] for row in result["next_actions"]] == [
        a.context.identity_sha256, context.identity_sha256]


@pytest.mark.anyio
@pytest.mark.parametrize("policy", ["auto", "source"])
async def test_normal_no_difference_still_final_validates_without_binding_cursor_or_action(tmp_path, controls, policy):
    a, b, args = case(tmp_path, equal=True, source=policy == "source")
    controls.arm(args)
    result = await public(args)
    assert result["diverged"] is False and result.get("cursor") is None and result["next_actions"] == []
    assert len(controls.source_calls) == 4 and len(controls.wave_calls) == 2
    assert not controls.binding_calls and not controls.cursor_writes
    assert all(not row["binding_observed"] for row in controls.store.summaries())


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["before", "after_wave", "after_second_design_final_hash"])
async def test_real_source_mutation_discards_diff_without_actions_or_cursor(tmp_path, controls, phase):
    a, b, args = case(tmp_path)
    if phase == "before": write(a.source, "module top; wire changed; endmodule\nmodule other; endmodule\n")
    elif phase == "after_wave":
        controls.after_wave = lambda number: write(a.source, "changed source\n") if number == 1 else None
    else:
        controls.after_source = lambda ctx, n: write(a.source, "changed after A hash\n") if ctx is b.context and n == 2 else None
    controls.arm(args)
    failed(await public(args), controls, "design_context_changed")


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["before", "after_wave"])
async def test_existing_bound_anchor_is_checked_without_new_binding_or_load(tmp_path, controls, phase):
    a, _, args = case(tmp_path)
    binding, candidate = add_binding(a, tmp_path)
    if phase == "before": (candidate / "_stnElabDB").write_bytes(b"new generation before comparison\n")
    else:
        controls.after_wave = lambda number: (candidate / "_stnElabDB").write_bytes(b"new generation after comparison\n") if number == 1 else None
    controls.arm(args)
    failed(await public(args), controls, "kdb_identity_changed")
    assert controls.binding_calls and all(value is binding for value in controls.binding_calls)


@pytest.mark.anyio
async def test_unchanged_existing_binding_is_validated_but_never_reported_loaded(tmp_path, controls):
    a, _, args = case(tmp_path, same=True)
    binding, _ = add_binding(a, tmp_path)
    controls.arm(args)
    result = await public(args)
    assert result["diverged"] and controls.binding_calls == [binding, binding]
    assert "backend_status" not in result and "loaded" not in json.dumps(result)


@pytest.mark.anyio
async def test_execution_policy_change_invalidates_existing_binding_without_retry(tmp_path, controls, monkeypatch):
    a, _, args = case(tmp_path)
    add_binding(a, tmp_path)
    def change(number):
        if number == 1: monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    controls.after_wave = change
    controls.arm(args)
    failed(await public(args), controls, "design_context_execution_changed")


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["epoch", "parser", "file_content", "file_replacement"])
async def test_wave_or_parser_generation_change_cannot_publish_old_diff(tmp_path, controls, mutation):
    _, _, args = case(tmp_path)
    target = args["wave_path_a"]
    def after(number):
        if number != 1: return
        if mutation == "epoch":
            parser = server._parser_cache[target][1]
            # Parser generations are opaque identity tokens, not counters.
            parser._scope_epoch = object()
        elif mutation == "file_content":
            with open(target, "a") as stream: stream.write("#21\nb0000 !\n")
        elif mutation == "file_replacement":
            replacement = tmp_path / "replacement.vcd"
            replacement.write_bytes((tmp_path / "a.vcd").read_bytes())
            replacement.replace(target)
    if mutation == "parser":
        controls.parser_hook = lambda path, parser, n: VCDParser(path) if path == target and n == 2 else None
    else: controls.after_wave = after
    controls.arm(args)
    failed(await public(args), controls, "parser_generation_changed" if mutation in {"epoch", "parser"} else "waveform_changed")


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["source_pre", "source_final", "wave"])
@pytest.mark.parametrize("failure,code", [(lambda: TimeoutError(), "design_context_timeout"),
    (lambda: CaptureAborted("timeout"), "design_context_timeout"),
    (lambda: CaptureAborted("input_changed"), "design_context_changed")])
async def test_typed_control_failure_never_returns_ready_actions_or_partial_cursor(tmp_path, controls, where, failure, code):
    a, _, args = case(tmp_path, same=True)
    def source(ctx, n):
        if n == (1 if where == "source_pre" else 2): raise failure()
    if where == "wave":
        def after(number):
            if number == 1: raise failure()
        controls.after_wave = after
    else: controls.before_source = source
    controls.arm(args)
    failed(await public(args), controls, code)


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["source_pre", "source_final", "wave"])
async def test_operation_cancelled_propagates_as_cancellation_and_releases_all_pins(tmp_path, controls, where):
    _, _, args = case(tmp_path, same=True)
    def source(ctx, n):
        if n == (1 if where == "source_pre" else 2): raise OperationCancelled()
    if where == "wave":
        def after(number):
            if number == 1: raise OperationCancelled()
        controls.after_wave = after
    else: controls.before_source = source
    controls.arm(args)
    with pytest.raises(asyncio.CancelledError): await public(args)
    assert not controls.cursor_writes and len(server._cursor_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["source", "wave"])
async def test_async_cancel_waiter_has_no_late_cursor_publication(tmp_path, controls, where):
    _, _, args = case(tmp_path)
    entered = anyio.Event()
    async def block(*ignored):
        entered.set()
        await anyio.sleep_forever()
    if where == "source": controls.before_source = block
    else: controls.after_wave = block
    controls.arm(args)
    task = asyncio.create_task(public(args))
    with anyio.fail_after(3): await entered.wait()
    task.cancel()
    with anyio.fail_after(3), pytest.raises(asyncio.CancelledError): await task
    assert not controls.cursor_writes and len(server._cursor_store) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["pre_validation", "final_parser"])
async def test_registry_reset_at_await_or_final_parser_discards_all_old_actions(tmp_path, controls, where):
    _, _, args = case(tmp_path)
    if where == "pre_validation":
        def reset(ctx, n):
            if len(controls.source_calls) == 1: server.reset_session_state()
        controls.after_source = reset
    else:
        controls.after_wave = lambda number: server.reset_session_state() if number == 2 else None
    controls.arm(args)
    failed(await public(args), controls, "design_context_generation_changed")


@pytest.mark.anyio
@pytest.mark.parametrize("side", ["a", "b"])
async def test_expired_side_unwinds_other_pin_without_validation_or_wave(tmp_path, controls, side):
    a, b, args = case(tmp_path)
    expired = a if side == "a" else b
    assert server._design_context_store.remove(expired.handle)
    controls.arm(args)
    failed(await public(args), controls, "handle_expired")
    assert not controls.source_calls and not controls.wave_calls


@pytest.mark.anyio
async def test_all_unique_handles_are_pinned_before_first_await_and_cannot_be_evicted(tmp_path, controls, monkeypatch):
    monkeypatch.setattr(server, "_design_context_store", DesignContextStore(max_entries=2))
    a, b, args = case(tmp_path)
    third_source = write(tmp_path / "third.sv", "module third; endmodule\n")
    third_log = write(tmp_path / "third.log", f"Chronologic VCS simulator\nCommand: vcs {third_source} -top third\n"
                      f"Parsing design file '{third_source}'\nTop Level Modules:\n       third\n")
    third = design_context_runtime.prepare_design_context(str(third_log), simulator="vcs", top_hint="third")
    def capacity(ctx, n):
        assert not controls.store.remove(a.handle) and not controls.store.remove(b.handle)
        with pytest.raises(ContextUnavailable) as error: controls.store.register(third)
        assert error.value.code == "context_capacity"
    controls.before_source = capacity
    controls.arm(args)
    result = await public(args)
    assert result["diverged"] and {row["design_handle"] for row in controls.store.summaries()} == {a.handle, b.handle}


@pytest.mark.anyio
@pytest.mark.parametrize("prepared_side", ["a", "b"])
async def test_mixed_legacy_and_prepared_contexts_preserve_separate_exact_actions(tmp_path, controls, prepared_side):
    a, b, args = case(tmp_path)
    legacy_side = "b" if prepared_side == "a" else "a"
    old = legacy(b if legacy_side == "b" else a)
    args[f"context_{legacy_side}"] = old
    controls.arm(args)
    result = await public(args)
    assert result["diverged"]
    actions = {row["side"]: row for row in result["next_actions"]}
    assert actions[legacy_side]["status"] == "ready"
    assert actions[legacy_side]["arguments"]["compile_context"] == old
    prepared = a if prepared_side == "a" else b
    assert actions[prepared_side]["arguments"]["design_handle"] == prepared.handle
    assert actions[prepared_side]["evidence"]["context_identity_sha256"] == prepared.context.identity_sha256
    assert len(controls.source_calls) == 2


@pytest.mark.anyio
async def test_mixed_legacy_change_during_prepared_final_hash_cannot_publish_ready_action(tmp_path, controls):
    a, b, args = case(tmp_path)
    old = legacy(b)
    bound, reason = resolve_context(old, server._handle_store)
    assert reason == "ready" and bound.current()
    args["context_b"] = old
    def mutate(ctx, n):
        if ctx is a.context and n == 2: write(b.source, "module top; wire legacy_changed; endmodule\n")
    controls.after_source = mutate
    controls.arm(args)
    failed(await public(args), controls, "compile_context_changed")
    assert old["hierarchy_handle"] and old["source_snapshot_sha256"] == bound.context["source_snapshot_sha256"]


@pytest.mark.anyio
async def test_no_difference_cannot_early_return_before_source_final_validation(tmp_path, controls):
    a, _, args = case(tmp_path, equal=True)
    controls.after_wave = lambda number: write(a.source, "source changed after clean comparison\n") if number == 1 else None
    controls.arm(args)
    failed(await public(args), controls, "design_context_changed")


@pytest.mark.anyio
async def test_plain_wave_only_diff_retains_needs_context_actions(tmp_path, controls):
    _, _, args = case(tmp_path)
    args.pop("context_a")
    args.pop("context_b")
    controls.arm(args)
    result = await public(args)
    assert result["diverged"] and result["first_divergence_time_ps"] == 7
    assert [row["status"] for row in result["next_actions"]] == ["needs_context", "needs_context"]
    assert not controls.pin_events and not controls.source_calls and not controls.binding_calls
    assert len(controls.wave_calls) == 1
