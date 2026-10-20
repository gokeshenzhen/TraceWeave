"""Public prepare contract and asynchronous ownership tests.

Real text fixtures establish capture/registry behavior only. Event barriers and
injected errors below establish control-flow behavior; they are not semantic,
NPI, license, query or LSF acceptance evidence.
"""
from __future__ import annotations

import asyncio
import builtins
from copy import deepcopy
import json
import math
import os
import threading
import traceback

import anyio
import pytest

import server
from src import cancellation, compile_input_capture, design_context_runtime
from src.compile_input_capture import CaptureAborted
from src.design_context_runtime import ContextUnavailable, DesignContextStore


_public_artifact_dir = None
_public_call_index = 0


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch, tmp_path):
    global _public_artifact_dir, _public_call_index
    _public_artifact_dir = tmp_path / "public-prepare-evidence"
    _public_call_index = 0
    server.reset_session_state()
    # Public serialization is real; persistent usage recording is outside this
    # local fixture's contract and must not write a user's telemetry file.
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    yield
    server.reset_session_state()
    _public_artifact_dir = None
    _public_call_index = 0


@pytest.fixture
def publication_events(monkeypatch):
    events = {"attempts": [], "published": []}
    original = DesignContextStore.publish

    def publish(self, registration):
        events["attempts"].append(registration.handle)
        handle = original(self, registration)
        events["published"].append(handle)
        return handle

    monkeypatch.setattr(DesignContextStore, "publish", publish)
    return events


def _design(root, name="top", *, missing=False):
    root.mkdir(parents=True, exist_ok=True)
    source = root / f"{name}.sv"
    source.write_text(f"module {name}; wire q; endmodule\n")
    log = root / f"{name}.log"
    log.write_text(
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {source} -top {name}\n"
        f"Parsing design file '{source}'\n"
        f"Top Level Modules:\n       {name}\n"
    )
    if missing:
        source.unlink()
    return source, {"compile_log": str(log), "simulator": "vcs", "top_hint": name}


async def _public(arguments):
    global _public_call_index
    _public_call_index += 1
    directory = _public_artifact_dir / f"{_public_call_index:04d}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "request.json").write_text(json.dumps({
        "tool": "prepare_design_context", "arguments": arguments}) + "\n")
    try:
        blocks = await server.call_tool("prepare_design_context", arguments)
    except BaseException as exc:
        (directory / "call_exception.json").write_text(json.dumps({
            "module": type(exc).__module__, "type": type(exc).__name__, "message": str(exc)}) + "\n")
        (directory / "call_exception.txt").write_text(traceback.format_exc())
        raise
    (directory / "response.repr.txt").write_text(repr(blocks))
    try:
        raw = [block.model_dump(mode="json") for block in blocks]
        (directory / "response.raw.json").write_text(json.dumps({"blocks": raw}) + "\n")
        for index, block in enumerate(raw):
            if isinstance(block.get("text"), str):
                (directory / f"block_{index:03d}.raw").write_bytes(block["text"].encode("utf-8"))
        assert len(blocks) == 1 and blocks[0].type == "text"
        value = json.loads(blocks[0].text)
        (directory / "decoded.json").write_text(json.dumps(value) + "\n")
        return value
    except BaseException as exc:
        (directory / "response_exception.json").write_text(json.dumps({
            "module": type(exc).__module__, "type": type(exc).__name__, "message": str(exc)}) + "\n")
        (directory / "response_exception.txt").write_text(traceback.format_exc())
        raise


def _forbidden(*args, **kwargs):
    pytest.fail("prepare entered an unrelated hierarchy, waveform or backend path")


def _no_fallback(monkeypatch):
    monkeypatch.setattr(server, "build_hierarchy", _forbidden)
    monkeypatch.setattr(server, "scan_structural_risks", _forbidden)

    async def forbidden_async(*args, **kwargs):
        _forbidden()

    monkeypatch.setattr(server, "_route_public_connectivity", forbidden_async)
    monkeypatch.setattr(server, "_run_in_wave_thread", forbidden_async)
    monkeypatch.setattr(server, "_wave_locks_for", _forbidden)
    monkeypatch.setattr(server, "_get_parser", _forbidden)


def _assert_error(payload, code):
    assert payload["error_code"] == code
    assert "design_handle" not in payload


def _assert_expired(store, handle):
    with pytest.raises(ContextUnavailable) as raised:
        with store.lease(handle):
            pytest.fail("expired context was yielded")
    assert raised.value.code == "handle_expired"


@pytest.mark.anyio
async def test_public_prepare_is_listed_with_strict_bounded_schema():
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert "prepare_design_context" in tools
    schema = tools["prepare_design_context"].inputSchema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["compile_log"]
    fields = schema["properties"]
    assert "kdb_path" in fields and "design_handle" not in fields
    assert fields["kdb_path"]["default"] is None
    assert fields["simulator"]["enum"] == ["auto", "vcs", "xcelium", "verilator"]
    assert fields["backend_preference"]["enum"] == ["auto", "source"]
    assert fields["supplementary_compile_logs"]["maxItems"] == 16
    for name, maximum in (("max_files", 32768), ("max_bytes", 128 * 1024 * 1024), ("max_references", 65536)):
        assert fields[name]["minimum"] == 1
        assert fields[name]["maximum"] == maximum
        assert fields[name]["default"] == maximum
    assert fields["max_include_depth"]["minimum"] == 0
    assert fields["max_include_depth"]["maximum"] == 64
    assert fields["timeout_sec"]["default"] == 30
    assert fields["timeout_sec"]["exclusiveMinimum"] == 0
    assert fields["timeout_sec"]["maximum"] == 300


@pytest.mark.anyio
async def test_real_public_prepare_has_no_prerequisites_and_honest_capabilities(tmp_path, publication_events, monkeypatch):
    _, arguments = _design(tmp_path)
    _no_fallback(monkeypatch)
    assert server._session_state["get_sim_paths"] is None
    assert server._session_state["build_tb_hierarchy"] is None
    payload = await _public({**arguments, "backend_preference": "source"})
    validated = server.schemas.PrepareDesignContextResult.model_validate(payload)
    assert validated.design_handle.startswith("dc_")
    assert payload["handle_lifetime"] == "process"
    assert len(payload["context_identity_sha256"]) == 64
    assert all(char in "0123456789abcdef" for char in payload["context_identity_sha256"])
    assert payload["simulator"] == "vcs" and payload["selected_top"] == "top"
    assert payload["backend_preference"] == "source"
    assert payload["preparation_status"] == "prepared"
    assert payload["source_snapshot_status"] == "complete"
    assert payload["include_resolution_status"] == "complete"
    assert payload["semantic_status"] == "not_run" and payload["kdb_status"] == "not_checked"
    assert payload["capabilities"] == ["compile_metadata", "source_observations"]
    assert payload["gaps"] == []
    expected_metrics = {
        "parse_ms", "prepare_ms", "input_file_count", "captured_bytes", "read_bytes",
        "dependency_file_visits", "registration_ms", "total_ms", "retained_metadata_bytes",
    }
    assert expected_metrics <= set(payload["metrics"])
    assert all(isinstance(payload["metrics"][name], (int, float))
               and not isinstance(payload["metrics"][name], bool)
               and math.isfinite(payload["metrics"][name]) and payload["metrics"][name] >= 0
               for name in expected_metrics)
    assert publication_events["published"] == [payload["design_handle"]]
    with server._design_context_store.lease(payload["design_handle"]) as context:
        assert context.identity_sha256 == payload["context_identity_sha256"]
        assert context.backend_preference == "source"
    assert server._session_state["get_sim_paths"] is None
    assert server._session_state["build_tb_hierarchy"] is None


@pytest.mark.anyio
async def test_missing_source_is_partial_without_invented_semantic_or_kdb_work(tmp_path, publication_events):
    _, arguments = _design(tmp_path, missing=True)
    payload = await _public(arguments)
    assert payload["preparation_status"] == "partial"
    assert payload["source_snapshot_status"] == "partial"
    assert payload["include_resolution_status"] == "partial"
    assert payload["semantic_status"] == "not_run" and payload["kdb_status"] == "not_checked"
    assert payload["gaps"]
    assert publication_events["published"] == [payload["design_handle"]]
    with server._design_context_store.lease(payload["design_handle"]) as context:
        assert not context.snapshot.complete


@pytest.mark.anyio
async def test_ordered_supplements_and_source_preference_survive_recovery_token(tmp_path):
    _, arguments = _design(tmp_path)
    logs = []
    for name in ("first", "second"):
        source = tmp_path / f"{name}.sv"
        source.write_text(f"package {name}; endpackage\n")
        log = tmp_path / f"{name}.log"
        log.write_text(f"Chronologic VCS\nCommand: vlogan -sverilog {source}\nParsing design file '{source}'\n")
        logs.append(str(log))
    payload = await _public({**arguments, "supplementary_compile_logs": logs, "backend_preference": "source"})
    token = payload["recovery_context"]
    assert token["compile_log"] == arguments["compile_log"]
    assert token["supplementary_compile_logs"] == logs
    assert token["simulator"] == "vcs" and token["top_hint"] == "top"
    assert token["backend_preference"] == "source"
    assert token["context_identity_sha256"] == payload["context_identity_sha256"]
    # Omitting the expected identity explicitly establishes current inputs.
    # Exact recovery_context replay is covered by the recovery contract tests.
    repeat_arguments = {key: value for key, value in token.items() if key != "context_identity_sha256"}
    repeated = await _public(repeat_arguments)
    assert repeated["context_identity_sha256"] == payload["context_identity_sha256"]
    reversed_ = await _public({**repeat_arguments, "supplementary_compile_logs": list(reversed(logs))})
    assert reversed_["context_identity_sha256"] != payload["context_identity_sha256"]


@pytest.mark.anyio
async def test_prepare_preserves_existing_hierarchy_state_cache_and_handles(tmp_path):
    _, arguments = _design(tmp_path)
    old_state = {"compile_log": "/legacy/compile.log", "simulator": "vcs", "hierarchy_handle": "tbh_existing"}
    server._session_state["get_sim_paths"] = {"compile_log": "/legacy/compile.log", "simulator": "vcs"}
    server._session_state["build_tb_hierarchy"] = deepcopy(old_state)
    old_result = object()
    server._result_cache["build_tb_hierarchy"] = old_result
    full = {"component_tree": {}, "compile_result": {"simulator": "vcs"}}
    server._handle_store.register("tbh_existing", full)
    before = deepcopy(server._session_state)
    payload = await _public(arguments)
    assert payload["design_handle"].startswith("dc_")
    assert server._session_state == before
    assert server._result_cache["build_tb_hierarchy"] is old_result
    assert server._handle_store.resolve("tbh_existing") is full


@pytest.mark.anyio
async def test_control_only_legacy_state_updates_do_not_select_or_drop_prepared_context(tmp_path):
    _, arguments = _design(tmp_path)
    payload = await _public(arguments)
    store = server._design_context_store
    server._update_session_state("get_sim_paths", {"verif_root": str(tmp_path)}, {
        "verif_root": str(tmp_path), "case_dir": str(tmp_path), "simulator": "vcs",
        "compile_logs": [{"path": arguments["compile_log"], "phase": "elaborate"}],
    })
    server._update_session_state("build_tb_hierarchy", {
        **arguments, "_hierarchy_handle": "tbh_legacy_explicit",
    }, {"project": {"simulator": "vcs"}})
    assert server._design_context_store is store
    for name in ("get_sim_paths", "build_tb_hierarchy"):
        assert "design_handle" not in server._session_state[name]
    with store.lease(payload["design_handle"]) as context:
        assert context.identity_sha256 == payload["context_identity_sha256"]


@pytest.mark.anyio
async def test_repeated_public_prepare_does_not_reuse_a_stale_response(tmp_path):
    source, arguments = _design(tmp_path)
    first = await _public(arguments)
    source.write_text("module top; wire changed; endmodule\n")
    second = await _public(arguments)
    assert second["context_identity_sha256"] != first["context_identity_sha256"]
    assert second["design_handle"] != first["design_handle"]
    with pytest.raises(ContextUnavailable) as raised:
        with server._design_context_store.lease(first["design_handle"]):
            pytest.fail("old source facts were exposed after a change")
    assert raised.value.code == "design_context_changed"


INVALID_ARGUMENTS = [
    {}, {"compile_log": ""}, {"compile_log": "   "}, {"compile_log": None},
    {"compile_log": 7}, {"compile_log": False},
    {"simulator": "VCS"}, {"simulator": "other"}, {"simulator": 1},
    {"supplementary_compile_logs": "one.log"}, {"supplementary_compile_logs": [""]},
    {"supplementary_compile_logs": ["  "]}, {"supplementary_compile_logs": [1]},
    {"supplementary_compile_logs": [False]}, {"supplementary_compile_logs": [None]},
    {"supplementary_compile_logs": ["one.log"] * 17},
    {"top_hint": ""}, {"top_hint": "   "}, {"top_hint": False},
    {"backend_preference": "npi"}, {"backend_preference": False},
    {"timeout_sec": 0}, {"timeout_sec": -1}, {"timeout_sec": 301},
    {"timeout_sec": float("nan")}, {"timeout_sec": float("inf")},
    {"timeout_sec": True}, {"timeout_sec": "1"}, {"timeout_sec": None},
    {"max_files": 0}, {"max_files": 32769}, {"max_files": True}, {"max_files": 1.0}, {"max_files": "1"},
    {"max_bytes": 0}, {"max_bytes": 128 * 1024 * 1024 + 1}, {"max_bytes": False}, {"max_bytes": "4"},
    {"max_references": 0}, {"max_references": 65537}, {"max_references": True},
    {"max_include_depth": -1}, {"max_include_depth": 65}, {"max_include_depth": False},
    {"max_include_depth": 0.5}, {"max_include_depth": "0"},
    {"kdb_path": ""}, {"kdb_path": "   "}, {"kdb_path": False}, {"kdb_path": 12},
    {"unknown_option": 1},
    {"context_identity_sha256": "not-a-sha256"},
]


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", INVALID_ARGUMENTS)
async def test_invalid_public_arguments_are_rejected_before_capture(tmp_path, invalid, monkeypatch, publication_events):
    _, valid = _design(tmp_path)
    arguments = {} if invalid == {} else {**valid, **invalid}
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", _forbidden)
    payload = await _public(arguments)
    _assert_error(payload, "invalid_design_context_arguments")
    assert publication_events["attempts"] == []


@pytest.mark.anyio
async def test_options_model_accepts_exact_legal_numeric_boundaries(tmp_path):
    _, arguments = _design(tmp_path)
    for updates in (
        {"timeout_sec": 300, "max_files": 32768, "max_bytes": 128 * 1024 * 1024, "max_references": 65536, "max_include_depth": 64},
        {"timeout_sec": 0.001, "max_files": 1, "max_bytes": 1, "max_references": 1, "max_include_depth": 0},
    ):
        options = server.schemas.PrepareDesignContextOptions.model_validate({**arguments, **updates})
        for key, value in updates.items():
            assert getattr(options, key) == value


@pytest.mark.anyio
@pytest.mark.parametrize("failure,code", [
    (ValueError("compile evidence mismatch"), "invalid_design_context"),
    (FileNotFoundError("local fixture unavailable"), "compile_log_unavailable"),
    (CaptureAborted("input_changed"), "design_context_changed"),
    (CaptureAborted("timeout"), "design_context_timeout"),
    (CaptureAborted("byte_limit"), "design_context_byte_limit"),
    (ContextUnavailable("context_capacity"), "context_capacity"),
])
async def test_control_only_core_failures_have_fixed_codes_and_never_publish(tmp_path, monkeypatch, publication_events, failure, code):
    _, arguments = _design(tmp_path)
    _no_fallback(monkeypatch)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(design_context_runtime, "prepare_design_context", fail)
    _assert_error(await _public(arguments), code)
    assert publication_events["attempts"] == []


@pytest.mark.anyio
async def test_source_change_between_capture_and_registration_is_not_published(tmp_path, monkeypatch, publication_events):
    source, arguments = _design(tmp_path)
    original = design_context_runtime.prepare_design_context

    def change_after_capture(*args, **kwargs):
        context = original(*args, **kwargs)
        source.write_text("module top; wire later; endmodule\n")
        return context

    monkeypatch.setattr(design_context_runtime, "prepare_design_context", change_after_capture)
    _assert_error(await _public(arguments), "design_context_changed")
    assert publication_events["attempts"] == []


@pytest.mark.anyio
async def test_capture_and_registration_run_in_worker_publish_runs_on_event_loop_without_file_io(tmp_path, monkeypatch):
    _, arguments = _design(tmp_path)
    loop_thread = threading.get_ident()
    store = server._design_context_store
    original_read = compile_input_capture.CompileInputCapture._read
    original_prepare = store.prepare_registration
    original_publish = store.publish
    read_threads, registration_threads, publication_threads = [], [], []
    publishing = [False]

    def read(self, *args, **kwargs):
        read_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_read(self, *args, **kwargs)

    def prepare_registration(context, *, timeout_sec=30):
        registration_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_prepare(context, timeout_sec=timeout_sec)

    def publish(registration):
        publication_threads.append(threading.get_ident())
        assert threading.get_ident() == loop_thread
        publishing[0] = True
        try:
            return original_publish(registration)
        finally:
            publishing[0] = False

    def file_guard(original):
        def call(*args, **kwargs):
            assert not (publishing[0] and threading.get_ident() == loop_thread), "publication performed file I/O"
            return original(*args, **kwargs)
        return call

    monkeypatch.setattr(compile_input_capture.CompileInputCapture, "_read", read)
    monkeypatch.setattr(store, "prepare_registration", prepare_registration)
    monkeypatch.setattr(store, "publish", publish)
    monkeypatch.setattr(builtins, "open", file_guard(builtins.open))
    for name in ("open", "stat", "lstat"):
        monkeypatch.setattr(os, name, file_guard(getattr(os, name)))
    _no_fallback(monkeypatch)
    payload = await _public(arguments)
    assert payload["design_handle"].startswith("dc_")
    assert read_threads and registration_threads and publication_threads == [loop_thread]


def _cooperative_barrier(started, release, stopped):
    started.set()
    try:
        while not release.wait(0.002):
            cancellation.check_cancelled()
        cancellation.check_cancelled()
    except cancellation.OperationCancelled:
        stopped.set()
        raise


@pytest.mark.anyio
async def test_control_only_prepare_wait_does_not_block_light_event_loop_calls(tmp_path, monkeypatch):
    _, arguments = _design(tmp_path)
    original = design_context_runtime.prepare_design_context
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        _cooperative_barrier(started, release, stopped)
        return original(*args, **kwargs)

    monkeypatch.setattr(design_context_runtime, "prepare_design_context", blocked)
    task = asyncio.create_task(_public(arguments))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        with anyio.fail_after(1):
            light = await server._dispatch("cursor_list", {})
        assert light is not None and not task.done()
        release.set()
        assert (await task)["design_handle"].startswith("dc_")
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["prepare", "registration"])
async def test_control_only_client_cancellation_stops_worker_without_publication(tmp_path, monkeypatch, publication_events, stage):
    _, arguments = _design(tmp_path)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    if stage == "prepare":
        original = design_context_runtime.prepare_design_context

        def blocked(*args, **kwargs):
            _cooperative_barrier(started, release, stopped)
            return original(*args, **kwargs)

        monkeypatch.setattr(design_context_runtime, "prepare_design_context", blocked)
    else:
        original = DesignContextStore.prepare_registration

        def blocked(self, context, *, timeout_sec=30):
            ticket = original(self, context, timeout_sec=timeout_sec)
            _cooperative_barrier(started, release, stopped)
            return ticket

        monkeypatch.setattr(DesignContextStore, "prepare_registration", blocked)
    _no_fallback(monkeypatch)
    task = asyncio.create_task(_public(arguments))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(stopped.wait, 1)
        assert publication_events["attempts"] == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["prepare", "registration"])
async def test_control_only_total_deadline_covers_prepare_and_registration(tmp_path, monkeypatch, publication_events, stage):
    _, arguments = _design(tmp_path)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    remaining = []
    if stage == "prepare":
        def blocked(*args, **kwargs):
            _cooperative_barrier(started, release, stopped)
            pytest.fail("deadline should abandon blocked preparation")
        monkeypatch.setattr(design_context_runtime, "prepare_design_context", blocked)
    else:
        def blocked(self, context, *, timeout_sec=30):
            remaining.append(timeout_sec)
            _cooperative_barrier(started, release, stopped)
            pytest.fail("deadline should abandon blocked registration")
        monkeypatch.setattr(DesignContextStore, "prepare_registration", blocked)
    _no_fallback(monkeypatch)
    try:
        payload = await _public({**arguments, "timeout_sec": 0.1})
        _assert_error(payload, "design_context_timeout")
        assert started.is_set()
        assert await asyncio.to_thread(stopped.wait, 1)
        if stage == "registration":
            assert len(remaining) == 1 and 0 < remaining[0] < 0.1
        assert publication_events["attempts"] == []
    finally:
        release.set()


@pytest.mark.anyio
async def test_control_only_worker_queue_wait_is_inside_total_deadline(tmp_path, monkeypatch, publication_events):
    _, arguments = _design(tmp_path)
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous_tokens = limiter.total_tokens
    occupied, release = threading.Event(), threading.Event()
    prepare_calls = []

    def occupy():
        occupied.set()
        release.wait(3)

    def must_not_start(*args, **kwargs):
        prepare_calls.append(1)
        pytest.fail("queued request started preparation after its deadline")

    monkeypatch.setattr(design_context_runtime, "prepare_design_context", must_not_start)
    limiter.total_tokens = 1
    holder = asyncio.create_task(anyio.to_thread.run_sync(occupy))
    try:
        assert await asyncio.to_thread(occupied.wait, 1)
        _assert_error(await _public({**arguments, "timeout_sec": 0.05}), "design_context_timeout")
        assert prepare_calls == [] and publication_events["attempts"] == []
    finally:
        release.set()
        await holder
        limiter.total_tokens = previous_tokens
    # Drain a subsequent worker admission so an abandoned queued request
    # cannot escape the assertion by starting only after the holder releases.
    await anyio.to_thread.run_sync(lambda: None)
    assert prepare_calls == [] and publication_events["attempts"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["prepare", "registration"])
async def test_control_only_reset_during_work_rejects_old_generation_ticket(tmp_path, monkeypatch, publication_events, stage):
    _, arguments = _design(tmp_path)
    old_store = server._design_context_store
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    if stage == "prepare":
        original = design_context_runtime.prepare_design_context

        def blocked(*args, **kwargs):
            context = original(*args, **kwargs)
            _cooperative_barrier(started, release, stopped)
            return context

        monkeypatch.setattr(design_context_runtime, "prepare_design_context", blocked)
    else:
        original = DesignContextStore.prepare_registration

        def blocked(self, context, *, timeout_sec=30):
            ticket = original(self, context, timeout_sec=timeout_sec)
            _cooperative_barrier(started, release, stopped)
            return ticket

        monkeypatch.setattr(DesignContextStore, "prepare_registration", blocked)
    task = asyncio.create_task(_public(arguments))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        server.reset_session_state()
        assert server._design_context_store is not old_store
        release.set()
        _assert_error(await task, "design_context_generation_changed")
        assert publication_events["attempts"] == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.anyio
async def test_reset_expires_public_design_handle_and_next_prepare_gets_new_generation(tmp_path):
    _, arguments = _design(tmp_path)
    first = await _public(arguments)
    server.reset_session_state()
    _assert_expired(server._design_context_store, first["design_handle"])
    second = await _public(arguments)
    assert second["design_handle"] != first["design_handle"]
    assert second["context_identity_sha256"] == first["context_identity_sha256"]


def test_pending_ticket_is_not_visible_and_does_not_evict_an_existing_entry(tmp_path):
    _, first_args = _design(tmp_path / "first")
    _, second_args = _design(tmp_path / "second")
    first = design_context_runtime.prepare_design_context(**first_args)
    second = design_context_runtime.prepare_design_context(**second_args)
    store = DesignContextStore(max_entries=1)
    existing = store.register(first)
    ticket = store.prepare_registration(second)
    _assert_expired(store, ticket.handle)
    with store.lease(existing) as context:
        assert context.identity_sha256 == first.identity_sha256
    published = store.publish(ticket)
    assert published == ticket.handle
    _assert_expired(store, existing)
    with store.lease(published) as context:
        assert context.identity_sha256 == second.identity_sha256


def test_cross_store_and_repeated_ticket_publication_are_rejected(tmp_path):
    _, arguments = _design(tmp_path)
    context = design_context_runtime.prepare_design_context(**arguments)
    owner, other = DesignContextStore(), DesignContextStore()
    ticket = owner.prepare_registration(context)
    with pytest.raises(ContextUnavailable) as cross_store:
        other.publish(ticket)
    assert cross_store.value.code == "design_context_generation_changed"
    _assert_expired(other, ticket.handle)
    # A rejection by another owner must not consume the valid owner's ticket.
    handle = owner.publish(ticket)
    with pytest.raises(ContextUnavailable) as duplicate:
        owner.publish(ticket)
    assert duplicate.value.code == "design_context_generation_changed"
    with owner.lease(handle) as leased:
        assert leased.identity_sha256 == context.identity_sha256


def test_ticket_publication_performs_no_context_validation_or_file_io(tmp_path, monkeypatch):
    _, arguments = _design(tmp_path)
    context = design_context_runtime.prepare_design_context(**arguments)
    store = DesignContextStore()
    ticket = store.prepare_registration(context)
    with monkeypatch.context() as blocked:
        blocked.setattr(type(context), "current", _forbidden)
        blocked.setattr(type(context), "metadata_current", _forbidden)
        blocked.setattr(builtins, "open", _forbidden)
        for name in ("open", "stat", "lstat"):
            blocked.setattr(os, name, _forbidden)
        handle = store.publish(ticket)
    assert handle == ticket.handle
    with store.lease(handle) as leased:
        assert leased.identity_sha256 == context.identity_sha256


def test_expired_ticket_cannot_publish_or_evict_existing_context(tmp_path, monkeypatch):
    _, first_args = _design(tmp_path / "first")
    _, second_args = _design(tmp_path / "second")
    first = design_context_runtime.prepare_design_context(**first_args)
    second = design_context_runtime.prepare_design_context(**second_args)
    store = DesignContextStore(max_entries=1)
    existing = store.register(first)
    ticket = store.prepare_registration(second, timeout_sec=1)
    after_expiry = design_context_runtime.time.monotonic() + 2
    monkeypatch.setattr(design_context_runtime.time, "monotonic", lambda: after_expiry)
    with pytest.raises(CaptureAborted) as expired:
        store.publish(ticket)
    assert expired.value.code == "timeout"
    _assert_expired(store, ticket.handle)
    with store.lease(existing) as context:
        assert context.identity_sha256 == first.identity_sha256


def test_busy_publication_capacity_failure_does_not_consume_ticket(tmp_path):
    _, first_args = _design(tmp_path / "first")
    _, second_args = _design(tmp_path / "second")
    first = design_context_runtime.prepare_design_context(**first_args)
    second = design_context_runtime.prepare_design_context(**second_args)
    store = DesignContextStore(max_entries=1)
    existing = store.register(first)
    ticket = store.prepare_registration(second)
    with store.lease(existing):
        with pytest.raises(ContextUnavailable) as capacity:
            store.publish(ticket)
        assert capacity.value.code == "context_capacity"
        _assert_expired(store, ticket.handle)
    assert store.publish(ticket) == ticket.handle
