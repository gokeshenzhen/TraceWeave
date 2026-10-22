"""Native consumer controls with real small logs/VCD and handwritten SDK facts."""
from types import SimpleNamespace

import pytest

import server
from src import connectivity_backend, verdi_npi_backend
from src.dynamic_evidence import unsupported_step
from src.hierarchy_source_ir import HierarchyBudgetExceeded
from tests.native_first_helpers import project, public
from tests.test_native_source_services import isolate, use_sdk
from tests.test_npi_hierarchy_files import FileSdk, A, B
from tests.test_npi_packed_layout import PackedSdk


@pytest.fixture
def anyio_backend():
    return "asyncio"


def wave(path, value="x"):
    path.write_text('$timescale 1ps $end\n$scope module top $end\n'
        '$var wire 1 ! y $end\n$var wire 8 " packet [7:0] $end\n'
        '$upscope $end\n$enddefinitions $end\n#0\n' + value + '!\nbxxxxxxxx "\n#20\n0!\nb00000000 "\n')
    return str(path)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["snapshot", "history"])
async def test_native_x_trace_has_no_early_source_preparation(tmp_path, monkeypatch, mode):
    _, log, _ = project(tmp_path)
    use_sdk(monkeypatch, PackedSdk(top="top"))
    def driver(self, **kw):
        assert self._ensure_loaded(self._bound_kdb_anchor.path, "top")
        self._record_query_kdb_status()
        return dict(signal_path=kw["signal_path"], wave_path=kw["wave_path"],
            backend="verdi_npi", driver_status="testbench_driven", upstream_signals=[], recursive=False)
    def dynamic(self, signal_path, **kw):
        assert self._ensure_loaded(self._bound_kdb_anchor.path, "top")
        self._record_query_kdb_status()
        return unsupported_step(signal_path, "verdi_npi", "testbench_driven")
    monkeypatch.setattr(verdi_npi_backend.VerdiNpiBackend, "find_driver", driver)
    monkeypatch.setattr(verdi_npi_backend.VerdiNpiBackend, "get_dynamic_step", dynamic)
    extra = {"mode": "history", "history_start_ps": 0} if mode == "history" else {}
    result = await public("trace_x_source", compile_log=str(log), wave_path=wave(tmp_path / "x.vcd"),
        signal_path="top.y", time_ps=10, **extra)
    assert result.get("backend_status", {}).get("actual_backend") == "verdi_npi", result
    assert result["native_route"]["source_context_status"] == "not_required"
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
@pytest.mark.parametrize("budget_stop", [False, True])
async def test_native_packed_handwritten_bit_identity_and_budget_stop(tmp_path, monkeypatch, budget_stop):
    _, log, _ = project(tmp_path)
    use_sdk(monkeypatch, PackedSdk(top="top"))
    if budget_stop:
        original = verdi_npi_backend.VerdiNpiBackend.query_source
        def limited(self, request):
            request.budget.max_calls = 1
            return original(self, request)
        monkeypatch.setattr(verdi_npi_backend.VerdiNpiBackend, "query_source", limited)
    result = await public("resolve_packed_fields", compile_log=str(log), wave_path=wave(tmp_path / "packet.vcd"),
        signal_path="top.packet[7:0]", source_signal="top.packet", fields=["tag", "payload"])
    if budget_stop:
        assert result["status"] == "mapping_required" and result.get("fields", {}) == {}, result
    else:
        assert result.get("status") == "resolved", result
        assert result["fields"]["tag"]["bits"] == [7, 6, 5]
        assert result["fields"]["payload"]["bits"] == [4, 3, 2, 1, 0]
    assert result["native_route"]["metrics"]["source_prepare_count"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["analyze_failure_event", "recommend_failure_debug_next_steps"])
@pytest.mark.parametrize("partial", [False, True])
async def test_failure_consumers_keep_native_partial_priority(tmp_path, monkeypatch, tool, partial):
    source, log, _ = project(tmp_path)
    sdk = FileSdk()
    use_sdk(monkeypatch, sdk, classes=True)
    for node in sdk.nodes.values():
        for key in ("file", "def_file"):
            if node.get(key) in (A, B):
                node[key] = str(source)
    if partial:
        monkeypatch.setattr(verdi_npi_backend.VerdiNpiBackend, "query_source",
                            lambda self, request: request._unstarted("scope_limit"))
    log_path = tmp_path / "sim.log"
    log_path.write_text('"/checker.sv", 3: top.u: started at 0ps failed at 10ps\n')
    extra = ({"failure_event": {"event_id": "hand-event", "kind": "assertion", "time_ps": 10,
              "line": 1, "instance_path": "top.u", "message": "assertion failed"}}
             if tool == "analyze_failure_event" else {})
    result = await public(tool, compile_log=str(log), log_path=str(log_path),
        wave_path=wave(tmp_path / "failure.vcd"), **extra)
    assert "error_code" not in result, result
    assert result["source_correlation"]["facts_origin"] == ("log_only" if partial else "verdi_npi")
    assert result["source_correlation"]["full_hierarchy_fallback"] is False
    assert result["native_route"]["source_context_status"] == "not_required"
    assert result["native_route"]["metrics"]["source_prepare_count"] == 0


@pytest.mark.anyio
async def test_dual_design_native_binding_never_borrows_other_side(tmp_path, monkeypatch):
    left, right = tmp_path / "left", tmp_path / "right"
    _, left_log, left_kdb = project(left)
    _, right_log, right_kdb = project(right)
    sdk = PackedSdk(top="top")
    use_sdk(monkeypatch, sdk)
    seen = []
    def dynamic(self, signal_path, **kw):
        seen.append(self._bound_kdb_anchor.path)
        assert self._ensure_loaded(self._bound_kdb_anchor.path, "top")
        self._record_query_kdb_status()
        return unsupported_step(signal_path, "verdi_npi", "testbench_driven")
    monkeypatch.setattr(verdi_npi_backend.VerdiNpiBackend, "get_dynamic_step", dynamic)
    result = await public("trace_divergence",
        side_a=dict(compile_log=str(left_log), wave_path=wave(left / "left.vcd", "1"), signal_path="top.y"),
        side_b=dict(compile_log=str(right_log), wave_path=wave(right / "right.vcd", "0"), signal_path="top.y"),
        start_time_ps=0, end_time_ps=10)
    assert result.get("comparison", {}).get("diverged"), result
    assert {str(left_kdb), str(right_kdb)} <= set(seen)
    for side in ("a", "b"):
        assert result["contexts"][side]["native_route"]["source_context_status"] == "not_required"
        assert result["contexts"][side]["native_route"]["source_design_handle"] is None
    assert result["contexts"]["a"]["native_route"]["compile_metadata_identity_sha256"] != result["contexts"]["b"]["native_route"]["compile_metadata_identity_sha256"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["compile_log", "kdb_only"])
@pytest.mark.parametrize("stage", ["before_final", "after_final"])
async def test_dual_native_trace_reset_discards_graph_and_preserves_new_session_cursor(
        tmp_path, monkeypatch, mode, stage):
    from src.divergence_design_context import DivergenceDesigns

    use_sdk(monkeypatch, PackedSdk(top="top"))

    def dynamic(self, signal_path, **kwargs):
        assert self._ensure_loaded(self._bound_kdb_anchor.path, "top")
        self._record_query_kdb_status()
        return unsupported_step(signal_path, "verdi_npi", "testbench_driven")

    monkeypatch.setattr(verdi_npi_backend.VerdiNpiBackend, "get_dynamic_step", dynamic)
    sides = {}
    for label, value in (("a", "1"), ("b", "0")):
        root = tmp_path / label
        _, log, kdb = project(root)
        selector = {"compile_log": str(log)} if mode == "compile_log" else {
            "kdb_path": str(kdb), "top_hint": "top"}
        sides[f"side_{label}"] = dict(selector, wave_path=wave(root / "trace.vcd", value),
                                     signal_path="top.y")
    replacement = []

    def reset():
        server.reset_session_state()
        replacement.append(server._cursor_store.set("generation_cursor", 777, note="new session"))

    original = DivergenceDesigns.validate_final

    async def final(self, *args):
        assert not self.entries and set(self.native_bounds) == {"a", "b"}
        if stage == "before_final":
            reset()
        changed = await original(self, *args)
        if stage == "after_final":
            reset()
        return changed

    monkeypatch.setattr(DivergenceDesigns, "validate_final", final)
    result = await public("trace_divergence", **sides, start_time_ps=0, end_time_ps=10,
                          cursor_name="generation_cursor")
    assert result.get("status") == "inconclusive", result
    assert result["coverage"]["gaps"] == ["design_context_generation_changed"]
    assert result["comparison"] == {} and result.get("cursor") is None
    assert result["nodes"] == result["edges"] == result["findings"] == []
    assert server._cursor_store.list() == replacement
    assert not server._design_context_store.summaries()
    for label in ("a", "b"):
        assert result["contexts"][label]["native_route"]["source_design_handle"] is None
    assert len(result["next_actions"]) == 2
    for action in result["next_actions"]:
        assert action["tool"] == "explain_signal_driver" and action["status"] == "ready"
        assert action["evidence"]["context_status"] == "design_context_generation_changed"
        assert "compile_context" not in action["arguments"]
        assert "design_handle" not in action["arguments"]
        selector = "compile_log" if mode == "compile_log" else "kdb_path"
        assert action["arguments"][selector] == sides[f"side_{action['side']}"][selector]


@pytest.mark.anyio
async def test_wave_and_analyze_failures_do_not_create_native_or_source_context(tmp_path, monkeypatch):
    from src import native_dispatch
    monkeypatch.setattr(native_dispatch, "dispatch_native_query", lambda *a: pytest.fail("unexpected design query"))
    path = wave(tmp_path / "plain.vcd", "1")
    result = await public("get_signal_at_time", wave_path=path, signal_path="top.y", time_ps=0)
    assert result["value"]["bin"] == "1" and "native_route" not in result
    assert not server._design_context_store.summaries()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["compile_log", "kdb_only"])
async def test_native_recommendation_cache_keeps_its_own_identity(tmp_path, monkeypatch, mode):
    source, log, kdb = project(tmp_path)
    sdk = FileSdk()
    use_sdk(monkeypatch, sdk, classes=True)
    for node in sdk.nodes.values():
        for key in ("file", "def_file"):
            if node.get(key) in (A, B):
                node[key] = str(source)
    monkeypatch.setitem(server._result_cache, "get_sim_paths", SimpleNamespace(simulator="xcelium"))
    sim_log = tmp_path / "sim.log"
    sim_log.write_text('"/checker.sv", 3: top.u: started at 0ps failed at 10ps\n')
    selector = {"compile_log": str(log)} if mode == "compile_log" else {"kdb_path": str(kdb), "top_hint": "top", "simulator": "vcs"}
    result = await public("recommend_failure_debug_next_steps", **selector, log_path=str(sim_log),
        wave_path=wave(tmp_path / "cache.vcd"))
    assert not result.get("error_code"), result
    provenance = server._result_provenance["recommend_failure_debug_next_steps"]
    assert provenance["simulator"] == "vcs"
    assert provenance["compile_metadata_identity_sha256"] == result["source_correlation"]["compile_metadata_identity_sha256"]
    assert provenance["context_identity_sha256"] is None and provenance["design_handle"] is None


@pytest.mark.anyio
async def test_kdb_only_log_consumer_requires_simulator_before_native_work(tmp_path):
    _, _, kdb = project(tmp_path)
    result = await public("recommend_failure_debug_next_steps", kdb_path=str(kdb), top_hint="top",
        log_path=str(tmp_path / "not_read.log"), wave_path=str(tmp_path / "not_read.vcd"))
    assert result["error_code"] == "simulator_required"
    assert not server._design_context_store.summaries()


def test_native_recommend_request_does_not_borrow_another_compile_log(monkeypatch):
    monkeypatch.setitem(server._session_state, "get_sim_paths", {"compile_log": "/other/build.log"})
    entry = SimpleNamespace(context=SimpleNamespace(simulator="vcs"))
    context = server._build_recommend_request_context({"_native_query": entry})
    assert context["compile_log"] is None and context["simulator"] == "vcs"
