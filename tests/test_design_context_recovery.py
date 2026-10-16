"""Explicit public prepare recovery: content identity, never a native session.

Real small text/map fixtures establish bounded identity facts. Publication spies,
failure sentinels and event barriers establish control flow only; no EDA or real
NPI/LSF behavior is exercised by this file.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import threading
from types import SimpleNamespace

import pytest

import config
import server
from src import cancellation, design_backend_binding, design_context_runtime, verdi_npi_backend
from src.design_context_runtime import ContextUnavailable, DesignContextStore
from src.kdb_anchor import capture_kdb_anchor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _forbidden(*args, **kwargs):
    pytest.fail("explicit prepare recovery entered backend, hierarchy or wave work")


@pytest.fixture(autouse=True)
def isolated_prepare_only(monkeypatch):
    server.reset_session_state()
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "build_hierarchy", _forbidden)
    monkeypatch.setattr(server, "scan_structural_risks", _forbidden)
    monkeypatch.setattr(server, "prepare_backend_binding", _forbidden)
    monkeypatch.setattr(server, "_get_parser", _forbidden)
    monkeypatch.setattr(server, "_wave_locks_for", _forbidden)
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", _forbidden)
    monkeypatch.setattr(design_backend_binding, "capture_kdb_anchor", _forbidden)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", _forbidden)
    yield
    server.reset_session_state()


@pytest.fixture
def publications(monkeypatch):
    events = {"registration": [], "publish": []}
    original_registration = DesignContextStore.prepare_registration
    original_publish = DesignContextStore.publish

    def registration(self, context, **kwargs):
        events["registration"].append(context.identity_sha256)
        return original_registration(self, context, **kwargs)

    def publish(self, ticket):
        events["publish"].append(ticket.handle)
        return original_publish(self, ticket)

    monkeypatch.setattr(DesignContextStore, "prepare_registration", registration)
    monkeypatch.setattr(DesignContextStore, "publish", publish)
    return events


def _design(root, *, supplements=False, environment=False, include=False):
    root.mkdir(parents=True, exist_ok=True)
    source, log = root / "top.sv", root / "compile.log"
    source.write_text(('`include "defs.svh"\n' if include else "") +
        "module top; wire a; endmodule\nmodule alternate; endmodule\n")
    header = root / "defs.svh"
    if include:
        header.write_text("`define TEST_RECOVERY_VALUE 1\n")
    option = "+define+RECOVERY_VALUE=$TW_RECOVERY_VALUE " if environment else "+define+RECOVERY_VALUE=1 "
    log.write_text("Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {option}{source} -top top -top alternate\n"
        f"Parsing design file '{source}'\nTop Level Modules:\n       top\n       alternate\n")
    logs = []
    if supplements:
        for name in ("first", "second"):
            extra = root / f"{name}.sv"
            extra.write_text(f"package {name}; endpackage\n")
            phase = root / f"{name}.log"
            phase.write_text(f"Chronologic VCS simulator\nCommand: vlogan -sverilog {extra}\n"
                             f"Parsing design file '{extra}'\n")
            logs.append(str(phase))
    return SimpleNamespace(source=source, log=log, header=header, arguments=dict(
        compile_log=str(log), supplementary_compile_logs=logs, simulator="vcs", top_hint="top"))


async def _prepare(arguments):
    blocks = await server.call_tool("prepare_design_context", arguments)
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


def _error(result, code):
    assert result["error_code"] == code
    assert "design_handle" not in result and "identity_verification" not in result


def _clear(events):
    for values in events.values():
        values.clear()


def _expired(store, handle):
    with pytest.raises(ContextUnavailable) as raised:
        with store.pin(handle):
            pytest.fail("expired handle remained available")
    assert raised.value.code == "handle_expired"


@pytest.mark.anyio
async def test_recovery_input_and_receipt_schema_are_explicit():
    tool = next(tool for tool in await server.list_tools() if tool.name == "prepare_design_context")
    field = tool.inputSchema["properties"]["context_identity_sha256"]
    assert "context_identity_sha256" not in tool.inputSchema["required"]
    alternatives = field.get("anyOf", [field])
    assert any(value.get("pattern") == "^[0-9a-f]{64}$" for value in alternatives)
    assert server.schemas.PrepareDesignContextOptions.model_validate({
        "compile_log": "/compile.log", "context_identity_sha256": "a" * 64,
    }).context_identity_sha256 == "a" * 64


@pytest.mark.anyio
@pytest.mark.parametrize("expiry", ["reset", "eviction"])
async def test_unchanged_token_replays_after_expiry_with_new_handle(tmp_path, expiry, publications, monkeypatch):
    design = _design(tmp_path / "first", supplements=True)
    if expiry == "eviction":
        monkeypatch.setattr(server, "_design_context_store", DesignContextStore(max_entries=1))
    first = await _prepare({**design.arguments, "backend_preference": "source"})
    assert first["identity_verification"] == "current_inputs"
    token = deepcopy(first["recovery_context"])
    original_token = deepcopy(token)
    if expiry == "reset":
        server.reset_session_state()
    else:
        other = _design(tmp_path / "other")
        await _prepare(other.arguments)
    _expired(server._design_context_store, first["design_handle"])
    _clear(publications)
    recovered = await _prepare(token)  # Deliberately pass the complete token unchanged.
    server.schemas.PrepareDesignContextResult.model_validate(recovered)
    assert token == original_token
    assert recovered["identity_verification"] == "matched_recovery_identity"
    assert recovered["design_handle"] != first["design_handle"]
    assert recovered["context_identity_sha256"] == first["context_identity_sha256"]
    assert recovered["recovery_context"] == token
    assert recovered["backend_preference"] == "source"
    assert recovered["semantic_status"] == "not_run" and recovered["kdb_status"] == "not_checked"
    assert publications["publish"] == [recovered["design_handle"]]
    with server._design_context_store.pin(recovered["design_handle"]) as entry:
        assert entry.context.ordered_compile_logs == tuple([str(design.log), *design.arguments["supplementary_compile_logs"]])
        assert entry.binding is None and entry.npi_failure is None
    assert server._session_state["get_sim_paths"] is None
    assert server._session_state["build_tb_hierarchy"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source", "log", "options", "top", "supplement_order", "environment", "candidate_path"])
async def test_changed_recovery_identity_is_rejected_before_registration(tmp_path, monkeypatch, publications, change):
    monkeypatch.setenv("TW_RECOVERY_VALUE", "1")
    design = _design(tmp_path, supplements=True, environment=True)
    first = await _prepare(design.arguments)
    token = deepcopy(first["recovery_context"])
    _clear(publications)
    if change == "source":
        previous = design.source.stat()
        design.source.write_text(design.source.read_text().replace("wire a", "wire b"))
        os.utime(design.source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "log":
        design.log.write_text(design.log.read_text() + "// additional compile observation\n")
    elif change == "options":
        design.log.write_text(design.log.read_text().replace("-sverilog", "-sverilog +define+NEW_OPTION=1"))
    elif change == "top":
        token["top_hint"] = "alternate"  # Both tops have real compile-log evidence.
    elif change == "supplement_order":
        token["supplementary_compile_logs"].reverse()
    elif change == "environment":
        monkeypatch.setenv("TW_RECOVERY_VALUE", "2")
    else:
        token["kdb_path"] = str(tmp_path / "another_candidate")
    _error(await _prepare(token), "design_context_changed")
    assert publications == {"registration": [], "publish": []}


@pytest.mark.anyio
@pytest.mark.parametrize("expected", ["0" * 64, "f" * 64])
async def test_valid_but_wrong_expected_identity_does_not_publish(tmp_path, publications, expected):
    design = _design(tmp_path)
    _error(await _prepare({**design.arguments, "context_identity_sha256": expected}), "design_context_changed")
    assert publications == {"registration": [], "publish": []}


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, True, 7, [], {}])
async def test_malformed_identity_is_an_argument_error(tmp_path, publications, invalid):
    design = _design(tmp_path)
    _error(await _prepare({**design.arguments, "context_identity_sha256": invalid}), "invalid_design_context_arguments")
    assert publications == {"registration": [], "publish": []}


@pytest.mark.anyio
@pytest.mark.parametrize("limit", ["files", "bytes", "references", "depth"])
async def test_new_partial_budget_cannot_claim_old_complete_identity(tmp_path, publications, limit):
    design = _design(tmp_path, include=True)
    first = await _prepare(design.arguments)
    assert first["source_snapshot_status"] == "complete" and first["include_resolution_status"] == "complete"
    token = deepcopy(first["recovery_context"])
    budget = {"files": {"max_files": 1}, "bytes": {"max_bytes": design.log.stat().st_size + design.source.stat().st_size - 1},
              "references": {"max_references": 2}, "depth": {"max_include_depth": 0}}[limit]
    _clear(publications)
    _error(await _prepare({**token, **budget}), "design_context_changed")
    assert publications == {"registration": [], "publish": []}


@pytest.mark.anyio
async def test_matching_partial_token_remains_explicitly_partial(tmp_path, publications):
    design = _design(tmp_path)
    design.source.unlink()
    first = await _prepare(design.arguments)
    assert first["source_snapshot_status"] == "partial"
    recovered = await _prepare(first["recovery_context"])
    assert recovered["identity_verification"] == "matched_recovery_identity"
    assert recovered["preparation_status"] == "partial" and recovered["source_snapshot_status"] == "partial"
    assert recovered["gaps"] == first["gaps"] and recovered["gaps"]
    assert recovered["semantic_status"] == "not_run" and recovered["kdb_status"] == "not_checked"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["same_content_new_stat", "unreferenced_environment"])
async def test_recovery_compares_content_identity_not_old_observation_metadata(tmp_path, monkeypatch, change):
    design = _design(tmp_path)
    first = await _prepare(design.arguments)
    if change == "same_content_new_stat":
        previous = design.source.stat()
        os.utime(design.source, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    else:
        monkeypatch.setenv("TW_UNREFERENCED_RECOVERY_VARIABLE", "unrelated")
    recovered = await _prepare(first["recovery_context"])
    assert recovered["identity_verification"] == "matched_recovery_identity"
    assert recovered["context_identity_sha256"] == first["context_identity_sha256"]
    with server._design_context_store.lease(recovered["design_handle"]) as context:
        assert context.current()


@pytest.mark.anyio
async def test_matching_digest_still_requires_registration_content_validation(tmp_path, monkeypatch, publications):
    design = _design(tmp_path)
    first = await _prepare(design.arguments)
    _clear(publications)
    original = design_context_runtime.prepare_design_context

    def changed_after_capture(*args, **kwargs):
        context = original(*args, **kwargs)
        assert context.identity_sha256 == first["context_identity_sha256"]
        design.source.write_text(design.source.read_text().replace("wire a", "wire b"))
        return context

    monkeypatch.setattr(design_context_runtime, "prepare_design_context", changed_after_capture)
    _error(await _prepare(first["recovery_context"]), "design_context_changed")
    assert publications["registration"] == [first["context_identity_sha256"]]
    assert publications["publish"] == []


def _barrier(started, release, stopped):
    started.set()
    try:
        while not release.wait(0.01):
            cancellation.check_cancelled()
        cancellation.check_cancelled()
    finally:
        stopped.set()


@pytest.mark.anyio
@pytest.mark.parametrize("interruption", ["cancel", "reset", "timeout"])
async def test_matching_recovery_abandoned_after_registration_never_publishes(tmp_path, monkeypatch, publications, interruption):
    design = _design(tmp_path)
    first = await _prepare(design.arguments)
    _clear(publications)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = DesignContextStore.prepare_registration

    def delayed(self, context, **kwargs):
        ticket = original(self, context, **kwargs)
        _barrier(started, release, stopped)
        return ticket

    monkeypatch.setattr(DesignContextStore, "prepare_registration", delayed)
    arguments = deepcopy(first["recovery_context"])
    if interruption == "timeout":
        arguments["timeout_sec"] = 0.2
    task = asyncio.create_task(_prepare(arguments))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif interruption == "reset":
            server.reset_session_state()
            release.set()
            _error(await task, "design_context_generation_changed")
        else:
            _error(await task, "design_context_timeout")
        assert await asyncio.to_thread(stopped.wait, 1)
        assert publications["registration"] == [first["context_identity_sha256"]]
        assert publications["publish"] == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["map", "opaque_payload"])
@pytest.mark.parametrize("preference", ["auto", "source"])
async def test_recovery_matches_source_but_never_inherits_kdb_or_native_state(tmp_path, publications, change, preference):
    design = _design(tmp_path)
    kdb = tmp_path / "existing_kdb"
    kdb.mkdir()
    mapfile = kdb / "_libFileMap"
    mapfile.write_bytes(b"lib_map_A")
    (kdb / "_stnElabDB").write_bytes(b"elab_A")
    payload = kdb / "opaque_payload"
    payload.write_bytes(b"opaque_A")
    old_anchor = capture_kdb_anchor(str(kdb))  # Bounded fake-map control, no native load.
    first = await _prepare({**design.arguments, "backend_preference": preference, "kdb_path": str(kdb)})
    assert first["recovery_context"]["kdb_path"] == str(kdb)
    old_store = server._design_context_store
    with old_store.pin(first["design_handle"]) as entry:
        # Deliberately seed control state; this is not evidence of a real SDK session.
        sentinel = design_backend_binding.DesignBackendBinding(
            context_identity_sha256=entry.context.identity_sha256,
            backend_preference=preference,
            anchor=old_anchor if preference == "auto" else None,
            status_json="{}", execution=config.get_npi_execution_config(),
            route=config.get_connectivity_route_config(), runtime_environment=(),
            allow_degraded=config.NPI_ALLOW_DEGRADED_KDB,
            disposition="candidate" if preference == "auto" else "source_policy",
            gaps=(), metrics=(),
        )
        old_store.publish_binding(first["design_handle"], sentinel)
        old_store.note_npi_failure(first["design_handle"], "npi_load_failed")
        assert entry.binding is sentinel and entry.npi_failure == "npi_load_failed"
    if change == "map":
        previous = mapfile.stat()
        mapfile.write_bytes(b"lib_map_B")
        os.utime(mapfile, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert not old_anchor.current()
    else:
        payload.write_bytes(b"opaque_B")
        assert old_anchor.current()  # Explicitly outside bounded map identity.
    server.reset_session_state()
    _clear(publications)
    recovered = await _prepare(first["recovery_context"])
    assert recovered["identity_verification"] == "matched_recovery_identity"
    assert recovered["context_identity_sha256"] == first["context_identity_sha256"]
    assert recovered["backend_preference"] == preference
    assert recovered["semantic_status"] == "not_run" and recovered["kdb_status"] == "not_checked"
    with server._design_context_store.pin(recovered["design_handle"]) as entry:
        assert entry.context.requested_kdb_path == str(kdb)
        assert entry.binding is None and entry.npi_failure is None
    assert publications["publish"] == [recovered["design_handle"]]
