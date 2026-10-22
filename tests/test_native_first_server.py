import asyncio
from dataclasses import replace
import os
import time

import pytest

import config
import server
from src import connectivity_backend, design_backend_binding, design_context_runtime
from src.design_context import DesignContext
from tests.native_first_helpers import NativeControls, arguments, project, public


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    server.reset_session_state()
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    yield
    server.reset_session_state()


@pytest.fixture
def controls(monkeypatch):
    controls = NativeControls()
    monkeypatch.setattr(connectivity_backend, "select_backend", controls.select)
    return controls


def forbidden(*a, **kw):
    pytest.fail("healthy native request touched full source context")


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["explain_signal_driver", "find_signal_loads", "trace_signal_path"])
@pytest.mark.parametrize("mode", ["explicit", "discovery", "kdb_only"])
async def test_native_success_does_not_capture_or_validate_source(tmp_path, monkeypatch, controls, tool, mode):
    source, log, kdb = project(tmp_path)
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", forbidden)
    monkeypatch.setattr(DesignContext, "current", forbidden)
    selector = {} if mode == "kdb_only" else {"compile_log": str(log)}
    if mode != "discovery":
        selector.update(kdb_path=str(kdb), top_hint="top")
        monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", forbidden)
    controls.action = source.unlink
    result = await public(tool, **arguments(tool, **selector))
    assert result["backend_status"]["actual_backend"] == "verdi_npi", result
    route = result["native_route"]
    assert route["source_context_status"] == "not_required"
    assert route["source_design_handle"] is None and route["source_context_identity_sha256"] is None
    assert route["metrics"]["source_prepare_count"] == route["metrics"]["source_capture_read_bytes"] == 0
    assert "design_context" not in result["backend_status"]
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["kdb", "metadata", "policy", "environment"])
async def test_native_change_discards_result_without_source_fallback(tmp_path, monkeypatch, controls, change):
    _, log, kdb = project(tmp_path)
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", forbidden)
    def mutate():
        if change == "kdb":
            path = kdb / "_libFileMap"
            before = path.stat()
            path.write_bytes(b"lib-B")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == "metadata":
            log.write_text(log.read_text() + "changed metadata\n")
        elif change == "policy":
            monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
        else:
            monkeypatch.setenv("VERDI_HOME", "/different/sdk")
    controls.action = mutate
    result = await public("find_signal_loads", **arguments("find_signal_loads", compile_log=str(log)))
    expected = {"kdb": "kdb_identity_changed", "metadata": "compile_metadata_changed"}
    assert result["error_code"] == expected.get(change, "design_context_execution_changed"), result
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["before_capture", "after_capture"])
async def test_verilator_filelist_appearing_during_source_fallback_never_publishes(
        tmp_path, monkeypatch, controls, stage):
    source, log, kdb = project(tmp_path)
    missing = tmp_path / "missing.f"
    log.write_text(f"Command: verilator --cc -f {missing} --top-module top\n")
    controls.failure_reason = "npi_query_failed"
    original = design_context_runtime.prepare_design_context
    captured_units = []

    def prepare(*args, **kwargs):
        if stage == "before_capture":
            missing.write_text(f"{source}\n")
        context = original(*args, **kwargs)
        captured_units.append(context.compile_result()["compile_evidence"]["ordered_compilation_units"])
        if stage == "after_capture":
            missing.write_text(f"{source}\n")
        return context

    monkeypatch.setattr(design_context_runtime, "prepare_design_context", prepare)
    result = await public("find_signal_loads", **arguments("find_signal_loads",
        compile_log=str(log), kdb_path=str(kdb)))
    assert len(controls.calls) == len(captured_units) == 1
    assert len(captured_units[0]) == (1 if stage == "before_capture" else 0)
    assert result.get("error_code") == "compile_metadata_changed", result
    assert result["native_route"]["source_design_handle"] is None
    assert result["native_route"]["source_context_identity_sha256"] is None
    assert not server._design_context_store.summaries()
    assert not server._source_fallback_runtime.aliases
    assert not server._source_fallback_runtime.flights


@pytest.mark.anyio
async def test_native_candidate_never_hashes_cached_source(tmp_path, monkeypatch, controls):
    from src import verdi_backend
    _, log, kdb = project(tmp_path)
    (kdb / "_elabError").write_text("controlled degraded marker")
    monkeypatch.setattr(verdi_backend, "_probe_traceweave_cached_kdb", forbidden)
    result = await public("find_signal_loads", **arguments("find_signal_loads", compile_log=str(log)))
    assert result["backend"] == "verdi_npi", result


@pytest.mark.anyio
async def test_pure_kdb_failure_requires_log_and_remembers_only_load_failure(tmp_path, controls):
    _, _, kdb = project(tmp_path)
    controls.failure_reason = "npi_load_failed"
    args = arguments("find_signal_loads", kdb_path=str(kdb), top_hint="top")
    for expected in (1, 1):
        result = await public("find_signal_loads", **args)
        assert result["error_code"] == "source_context_required", result
        assert len(controls.calls) == expected
    await public("find_signal_loads", **args, retry_npi=True)
    assert len(controls.calls) == 2
    (kdb / "_libFileMap").write_bytes(b"lib-B")
    await public("find_signal_loads", **args)
    assert len(controls.calls) == 3
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
async def test_capability_failure_is_not_sticky(tmp_path, controls):
    _, _, kdb = project(tmp_path)
    args = dict(kdb_path=str(kdb), top_hint="top")
    controls.failure_reason = "npi_query_failed"
    await public("find_signal_loads", **arguments("find_signal_loads", **args))
    controls.failure_reason = None
    result = await public("trace_signal_path", **arguments("trace_signal_path", **args))
    assert result["backend"] == "verdi_npi" and len(controls.calls) == 2
    assert not server._native_failure_memory.entries


@pytest.mark.anyio
async def test_source_fallback_real_slang_oracle_and_strict_reuse(tmp_path, monkeypatch):
    pytest.importorskip("pyslang")
    from src.source_graph_production import _reset_source_graph_runtime_for_tests
    _reset_source_graph_runtime_for_tests()
    source, log, _ = project(tmp_path, candidate=False)
    args = dict(compile_log=str(log), path="top.u")
    first = await public("find_tb_instance", **args)
    assert first["facts_origin"] == "slang" and first["hits"][0]["module"] == "leaf", first
    assert first["hits"][0]["parent"] == "top"
    handle = first["native_route"]["source_design_handle"]
    assert first["native_route"]["source_context_status"] == "prepared"
    second = await public("find_tb_instance", **args)
    assert second["native_route"]["source_design_handle"] == handle
    assert second["native_route"]["source_context_status"] == "reused"
    assert second["native_route"]["metrics"]["source_prepare_count"] == 0
    source.write_text(source.read_text().replace("assign y=a", "assign y=~a"))
    changed = await public("find_tb_instance", **args)
    assert changed["error_code"] == "design_context_changed", changed
    assert changed["native_route"]["source_context_status"] == "changed"
    assert len(server._design_context_store.summaries()) == 1
    assert server._design_context_store.remove(handle)
    expired = await public("find_tb_instance", **args)
    assert expired["error_code"] == "handle_expired", expired
    assert expired["native_route"]["source_context_status"] == "expired"
    _reset_source_graph_runtime_for_tests()


@pytest.mark.anyio
async def test_new_call_never_chooses_cached_legacy_tree(tmp_path, controls):
    _, log, _ = project(tmp_path)
    server._result_cache["build_tb_hierarchy"] = object()
    result = await public("find_signal_loads", **arguments("find_signal_loads", compile_log=str(log)))
    assert result["native_route"]["entry"] == "native_first" and controls.calls


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["_native_query", "_prepared_design", "_request_deadline"])
async def test_private_arguments_cannot_be_injected(key):
    result = await public("find_signal_loads", **{key: None})
    assert result["error_code"] == "invalid_design_context_arguments"


@pytest.mark.anyio
async def test_native_schema_accepts_both_independent_modes():
    import jsonschema
    from src.native_query import NATIVE_TOOLS
    tools = {t.name: t for t in await server.list_tools()}
    for name in NATIVE_TOOLS:
        properties = tools[name].inputSchema["properties"]
        assert {"compile_log", "kdb_path", "top_hint", "retry_npi", "timeout_sec"} <= properties.keys()
        assert properties["backend_preference"]["enum"] == ["auto", "source"]
    for mode in ({"compile_log": "/compile.log"}, {"kdb_path": "/kdb", "top_hint": "top"}):
        jsonschema.validate({"path": "top.u", **mode}, tools["find_tb_instance"].inputSchema)


@pytest.mark.anyio
async def test_cached_kdb_hash_read_bytes_are_measured_separately(tmp_path, monkeypatch, controls):
    from src.kdb_builder import _extract_build_inputs
    from src.native_query import read_native_inputs
    source, log, _ = project(tmp_path, candidate=False)
    inputs = read_native_inputs({"compile_log": str(log)})
    exact = _extract_build_inputs(inputs.compile_result(), top_hint="top")
    cache = config.TRACEWEAVE_CACHE_ROOT / config.KDB_CACHE_SUBDIR / exact["hash"] / "kdb.elab++"
    cache.mkdir(parents=True)
    (cache / "_libFileMap").write_bytes(b"lib-A")
    (cache / "_stnElabDB").write_bytes(b"elab-A")
    monkeypatch.setattr(DesignContext, "current", forbidden)
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", forbidden)
    result = await public("find_signal_loads", **arguments("find_signal_loads", compile_log=str(log)))
    assert result.get("backend") == "verdi_npi", result
    assert result["native_route"]["metrics"]["cached_kdb_source_read_bytes"] == 2 * source.stat().st_size
    assert result["native_route"]["metrics"]["source_prepare_count"] == 0


@pytest.mark.anyio
async def test_late_native_failure_cannot_start_source_preparation(tmp_path, monkeypatch, controls):
    _, log, _ = project(tmp_path)
    controls.failure_reason = "npi_query_failed"
    controls.action = lambda: time.sleep(.03)
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", forbidden)
    result = await public("find_signal_loads", **arguments("find_signal_loads", compile_log=str(log), timeout_sec=.01))
    assert result["error_code"] == "native_query_timeout", result
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
async def test_native_external_cancel_stops_thread_and_preserves_event_loop(tmp_path, controls):
    import threading
    from src.cancellation import check_cancelled
    _, log, _ = project(tmp_path)
    entered, stopped = threading.Event(), threading.Event()
    controls.external = True
    def work():
        entered.set()
        try:
            while True:
                check_cancelled()
                time.sleep(.002)
        finally:
            stopped.set()
    controls.action = work
    task = asyncio.create_task(public("find_signal_loads", **arguments("find_signal_loads", compile_log=str(log))))
    async with asyncio.timeout(3):
        while not entered.is_set():
            await asyncio.sleep(.001)
        light = await public("cursor_list")
        assert "cursors" in light
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        while not stopped.is_set():
            await asyncio.sleep(.001)
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["explain_signal_driver", "find_signal_loads", "trace_signal_path"])
async def test_three_connections_automatically_use_real_source_oracle(tmp_path, monkeypatch, tool):
    pytest.importorskip("pyslang")
    from src.source_graph_production import _reset_source_graph_runtime_for_tests
    _reset_source_graph_runtime_for_tests()
    _, log, _ = project(tmp_path, candidate=False)
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", forbidden)
    result = await public(tool, **arguments(tool, compile_log=str(log), backend_preference="source"))
    assert result.get("backend_status", {}).get("actual_backend") == "source_graph", result
    assert result["native_route"]["source_context_status"] == "prepared"
    if tool == "trace_signal_path":
        assert result["found"] is True
    elif tool == "explain_signal_driver":
        assert result["driver_status"] == "resolved" and result["driver_kind"] == "continuous_assign"
    _reset_source_graph_runtime_for_tests()


@pytest.mark.anyio
async def test_history_source_relay_keeps_native_timeout_contract(tmp_path, monkeypatch):
    import sys
    from tests.test_native_first_consumers import wave
    pytest.importorskip("pyslang")
    _, log, _ = project(tmp_path, candidate=False)
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", sys.executable)
    result = await public("trace_x_source", compile_log=str(log), backend_preference="source",
        wave_path=wave(tmp_path / "history.vcd"), signal_path="top.y", time_ps=10,
        mode="history", history_start_ps=0, timeout_sec=180)
    assert not result.get("error_code"), result
    assert result["native_route"]["source_context_status"] == "prepared"
    assert result["history"]["coverage"]["limits"]["timeout_sec"] == 180


def test_pure_kdb_simulator_is_independent_of_discovery_cache(monkeypatch):
    monkeypatch.setitem(server._result_cache, "get_sim_paths", __import__("types").SimpleNamespace(simulator="xcelium"))
    entry = __import__("types").SimpleNamespace(context=__import__("types").SimpleNamespace(simulator="auto"))
    assert server._resolve_session_simulator({"_native_query": entry}) == "auto"
