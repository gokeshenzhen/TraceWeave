"""Independent workflow/diagnostic controls over real tiny compile captures.

No HDL semantic facts or root causes are asserted. Registry observations are
intentionally unvalidated by diagnostics; query-time validation remains a
separate contract. All licensed, frontend, full-tree and waveform work is
forbidden in this file.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import jsonschema
import pytest

import config
import server
from src import design_context_runtime, path_discovery, schemas
from src.compile_input_capture import CompileInputCapture
from src.design_context import DesignContext
from src.design_context_runtime import DesignContextStore
from src.divergence_context import driver_action


TRACKING = ("explain_signal_driver", "find_signal_loads", "trace_signal_path", "trace_x_source")
HANDLE_TOOLS = ("get_tb_subtree", "find_tb_instance", "lookup_tb_files", "get_tb_file_detail",
                "get_tb_class_hierarchy", "dump_tb_section")
TOKENS = {"hierarchy_handle": "tbh_historical", "snapshot_sha256": "a" * 64,
          "source_snapshot_sha256": "b" * 64, "compile_log_identity_sha256": "c" * 64}


def forbidden(*args, **kwargs):
    raise AssertionError("workflow hint entered a hierarchy, semantic, wave, license or scheduler path")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    server.reset_session_state()
    monkeypatch.setattr(config, "AUTO_KDB_BUILD", False)
    monkeypatch.setattr(server.usage_telemetry, "record_call", lambda *args, **kwargs: None)
    for name in ("build_hierarchy", "scan_structural_risks", "probe_verdi_backend", "prepare_backend_binding",
                 "_get_parser", "get_source_graph_runtime", "_query_prepared_hierarchy", "_run_in_wave_thread"):
        monkeypatch.setattr(server, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    original = builtins.__import__

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if any(part in {"pynpi", "pyslang"} for item in (name, *(str(x) for x in fromlist or ()))
               for part in item.split(".")):
            forbidden()
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    captures = []
    original_capture = CompileInputCapture.__init__

    def capture(self, *args, **kwargs):
        original_capture(self, *args, **kwargs)
        captures.append(self)

    monkeypatch.setattr(CompileInputCapture, "__init__", capture)
    yield
    assert all(item.body_count == 0 and item.resident_bytes == 0 for item in captures)
    server.reset_session_state()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def entry(path, **extra):
    return dict(path=str(path), size=path.stat().st_size, mtime="2026-10-16T00:00:00",
                age_hours=0.0, **extra)


class Case:
    def __init__(self, root, simulator="vcs"):
        self.root, self.simulator = root, simulator
        self.case = root / "case0"
        self.source = write(root / "design.sv", "module top; wire q; endmodule\nmodule other; endmodule\n")
        self.primary = root / "compile.log"
        if simulator == "vcs":
            text = (f"Chronologic VCS simulator\nCommand: vcs -sverilog {self.source} -top top -top other\n"
                    f"Parsing design file '{self.source}'\nTop Level Modules:\n       top\n       other\n")
        else:
            text = f"xrun\n  -top top\n  {self.source}\nfile: {self.source}\n  module worklib.top:v\n"
        write(self.primary, text)
        self.log = write(self.case / "run.log", "UVM_ERROR @ 10 ns: reporter [SCB] compare failed\n")
        self.wave = write(self.case / "top.vcd", "$timescale 1ps $end\n$scope module top $end\n"
            "$var wire 1 ! q $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#10\n1!\n")

    def arguments(self, *, top="top", policy="auto", supplements=()):
        return dict(compile_log=str(self.primary), simulator=self.simulator, top_hint=top,
                    backend_preference=policy, supplementary_compile_logs=list(map(str, supplements)))

    def prepare(self, *, store=None, **kwargs):
        args = self.arguments(**kwargs)
        ctx = design_context_runtime.prepare_design_context(
            args["compile_log"], simulator=args["simulator"], top_hint=args["top_hint"],
            backend_preference=args["backend_preference"], supplementary_compile_logs=args["supplementary_compile_logs"])
        store = store if store is not None else server._design_context_store
        return store.register(ctx), ctx

    def supplement(self):
        source = write(self.root / "helper.sv", "module helper; endmodule\n")
        return write(self.root / "compile_extra.log", f"Chronologic VCS simulator\nCommand: vlogan {source}\n"
                     f"Parsing design file '{source}'\n")

    def discover_state(self):
        result = schemas.SimPathsResult(verif_root=str(self.root), case_dir=str(self.case), case_name="case0",
            config_source="fixture", discovery_mode="case_dir", simulator=self.simulator,
            compile_logs=[entry(self.primary, phase="elaborate")],
            sim_logs=[entry(self.log, phase="runtime")], wave_files=[entry(self.wave, format="vcd")])
        provenance = server._build_result_provenance("get_sim_paths", {}, result)
        server._result_cache["get_sim_paths"] = result
        server._result_provenance["get_sim_paths"] = provenance
        server._session_state["get_sim_paths"] = deepcopy(provenance)
        return result

    def parse_state(self, count=1):
        result = schemas.ParseSimLogResult(log_file=str(self.log), simulator=self.simulator,
            schema_version="2.0", contract_version="1.3", failure_events_schema_version="1.0",
            runtime_total_errors=count, runtime_error_count=count, runtime_fatal_count=0, unique_types=int(bool(count)),
            total_groups=0, truncated=False, max_groups=50, first_error_line=1 if count else 0)
        server._result_cache["parse_sim_log"] = result
        server._result_provenance["parse_sim_log"] = dict(log_path=str(self.log), simulator=self.simulator)
        return result

    def recommend_state(self, handle=None, context=None, *, origin="verdi_npi", coverage="partial"):
        result = schemas.RecommendNextStepsResult(suspected_failure_class="data-path corruption",
            primary_failure_target={"event_id": "fixture-event", "time_ps": 10000},
            recommended_signals=[{"path": "top.q", "role": "fixture"}],
            source_correlation={"facts_origin": origin, "coverage_status": coverage,
                                "coverage_gaps": ["failure_candidate_scope_bounded"]})
        args = dict(log_path=str(self.log), wave_path=str(self.wave), compile_log=str(self.primary), simulator=self.simulator)
        if handle is not None:
            args.update(design_handle=handle, _prepared_design=SimpleNamespace(context=context))
        provenance = server._build_result_provenance("recommend_failure_debug_next_steps", args, result)
        server._result_cache["recommend_failure_debug_next_steps"] = result
        server._result_provenance["recommend_failure_debug_next_steps"] = provenance
        return result, provenance


@pytest.fixture
def case(tmp_path):
    return Case(tmp_path)


@contextmanager
def metadata_only(monkeypatch):
    """Diagnostics may compare paths/stat, but must never validate source content."""
    with monkeypatch.context() as patch:
        patch.setattr(DesignContext, "current", forbidden)
        patch.setattr(DesignContext, "metadata_current", forbidden)
        patch.setattr(DesignContextStore, "pin", forbidden)
        patch.setattr(DesignContextStore, "lease", forbidden)
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "read_bytes", forbidden)
        patch.setattr(Path, "read_text", forbidden)
        patch.setattr(server, "_dispatch_design_query", forbidden)
        yield


def tools_in(result):
    return [row["tool"] for row in result.missing_steps or []]


async def public(name, arguments):
    blocks = await server.call_tool(name, arguments)
    assert len(blocks) == 1 and blocks[0].type == "text"
    return json.loads(blocks[0].text)


def test_store_summaries_are_detached_bounded_observations_without_io_pin_or_lru(case, monkeypatch):
    store = DesignContextStore(max_entries=2)
    first, first_ctx = case.prepare(store=store)
    second, _ = case.prepare(store=store, policy="source")
    before = store.summaries()
    with metadata_only(monkeypatch), monkeypatch.context() as patch:
        for name in ("stat", "lstat", "scandir", "listdir", "readlink", "open"):
            patch.setattr(os, name, forbidden)
        patch.setattr(os.path, "realpath", forbidden)
        rows = store.summaries()
        assert [row["design_handle"] for row in rows] == [first, second]
        assert rows[0]["context_identity_sha256"] == first_ctx.identity_sha256
        assert all(row["identity_validation"] == "not_revalidated" and row["query_validation_required"] for row in rows)
        assert all(row["binding_observed"] is False for row in rows)
        rows[0]["coverage_gaps"].append("caller_mutation")
        rows[0]["supplementary_compile_logs"].append("/caller/mutation")
        assert store.summaries() == before
    third, _ = case.prepare(store=store, top="other")
    assert [row["design_handle"] for row in store.summaries()] == [second, third]
    assert all(row["design_handle"] != first for row in store.summaries())


@pytest.mark.anyio
async def test_real_public_prepare_then_readonly_diagnostic_does_not_set_build_state(case, monkeypatch):
    prepared = await public("prepare_design_context", case.arguments())
    handle = prepared["design_handle"]
    assert prepared["selected_top"] == "top"
    with metadata_only(monkeypatch):
        result = await public("get_diagnostic_snapshot", {"design_handle": handle})
    assert result["design_context"]["available"] is True
    summary = result["design_context"]["summary"]
    assert summary["design_handle"] == handle and summary["identity_validation"] == "not_revalidated"
    assert summary["query_validation_required"] is True and summary["binding_observed"] is False
    assert result["hierarchy"]["available"] is False and result["top_module"] == "top"
    assert server._session_state["build_tb_hierarchy"] is None
    assert server._result_cache["build_tb_hierarchy"] is None
    assert server._handle_store.resolve("tbh_any") is None


def test_discovery_suggests_scan_and_native_recommend_without_legacy_tree(case):
    case.discover_state()
    result = server._handle_diagnostic_snapshot({"case_dir": str(case.case)})
    assert result.design_context.available is False and result.hierarchy.available is False
    assert "prepare_design_context" not in tools_in(result) and "scan_structural_risks" in tools_in(result)
    assert "build_tb_hierarchy" not in tools_in(result)
    assert result.recommended_next.suggested_call == {
        "tool": "recommend_failure_debug_next_steps", "arguments": dict(
            log_path=str(case.log), wave_path=str(case.wave), simulator="vcs", compile_log=str(case.primary))}


@pytest.mark.anyio
async def test_cached_discovery_anchor_without_session_suggests_native_query(case):
    case.discover_state()
    server._session_state["get_sim_paths"] = None
    # Diagnostics must use the visible cache anchor, without restoring/mutating
    # mutable workflow state merely to construct an executable suggestion.
    result = server._handle_diagnostic_snapshot({"case_dir": str(case.case)})
    call = result.design_context.suggested_call
    assert call == {"tool": "get_tb_subtree", "arguments": dict(
        compile_log=str(case.primary), simulator="vcs")}
    schema = next(item.inputSchema for item in await server.list_tools() if item.name == call["tool"])
    jsonschema.validate(call["arguments"], schema)
    assert server._session_state["get_sim_paths"] is None


@pytest.mark.parametrize("variant", ["top", "supplement", "policy", "same_identity_two_handles"])
def test_implicit_diagnostic_never_selects_latest_of_multiple_contexts(case, variant, monkeypatch):
    first, _ = case.prepare()
    args = {"top": "other"} if variant == "top" else {"policy": "source"} if variant == "policy" else (
        {"supplements": [case.supplement()]} if variant == "supplement" else {})
    second, _ = case.prepare(**args)
    case.discover_state()
    with metadata_only(monkeypatch):
        result = server._handle_diagnostic_snapshot({})
    assert result.design_context.available is False
    assert result.design_context.summary["status"] == "ambiguous"
    assert {row["design_handle"] for row in result.design_context.summary["candidates"]} == {first, second}
    assert result.top_module is None and "prepare_design_context" not in tools_in(result)
    assert "build_tb_hierarchy" not in tools_in(result)


@pytest.mark.parametrize("selection", ["explicit", "implicit"])
def test_unique_or_explicit_context_selection_stays_unrevalidated(case, selection, monkeypatch):
    handle, ctx = case.prepare()
    case.discover_state()
    if selection == "explicit": case.prepare(top="other")
    args = {"design_handle": handle} if selection == "explicit" else {}
    with metadata_only(monkeypatch):
        result = server._handle_diagnostic_snapshot(args)
    assert result.design_context.available and result.top_module == "top"
    assert result.design_context.summary["context_identity_sha256"] == ctx.identity_sha256
    assert result.design_context.summary["identity_validation"] == "not_revalidated"
    assert "prepare_design_context" not in tools_in(result) and "build_tb_hierarchy" not in tools_in(result)
    assert result.recommended_next.suggested_call["arguments"]["design_handle"] == handle
    assert "scan_structural_risks" in tools_in(result)


@pytest.mark.parametrize("mismatch", ["primary", "simulator", "case", "root", "unknown_handle", "evicted_handle"])
def test_diagnostic_context_matching_never_crosses_identity_or_case(case, tmp_path, mismatch, monkeypatch):
    handle, _ = case.prepare()
    case.discover_state()
    args = {"design_handle": handle}
    if mismatch == "primary":
        server._result_provenance["get_sim_paths"]["compile_log"] = str(tmp_path / "unrelated.log")
    elif mismatch == "simulator": server._result_provenance["get_sim_paths"]["simulator"] = "xcelium"
    elif mismatch == "case": args["case_dir"] = str(tmp_path / "another_case")
    elif mismatch == "root": args["verif_root"] = str(tmp_path / "another_root")
    elif mismatch == "unknown_handle": args["design_handle"] = "dc_unknown"
    else: assert server._design_context_store.remove(handle)
    with metadata_only(monkeypatch): result = server._handle_diagnostic_snapshot(args)
    assert not result.design_context.available and result.top_module is None
    assert result.design_context.summary["identity_validation"] == "not_revalidated"
    if mismatch in {"case", "root"}:
        assert tools_in(result) == ["get_sim_paths"]
        assert result.design_context.summary["candidates"] == []
        assert not result.recommended_next.available


def test_implicit_diagnostic_requires_discovery_anchor_not_recent_context(case):
    case.prepare()
    result = server._handle_diagnostic_snapshot({})
    assert not result.design_context.available and result.design_context.summary["status"] == "not_prepared"
    assert result.design_context.summary["candidates"] == [] and result.top_module is None


@pytest.mark.parametrize("policy", ["auto", "source"])
def test_selected_xcelium_context_never_runs_legacy_parse_or_probe(tmp_path, monkeypatch, policy):
    case = Case(tmp_path, "xcelium")
    handle, _ = case.prepare(policy=policy)
    case.discover_state()
    monkeypatch.setattr(config, "AUTO_KDB_BUILD", True)
    monkeypatch.setattr(server, "parse_compile_log", forbidden)
    with metadata_only(monkeypatch):
        result = server._handle_diagnostic_snapshot({"design_handle": handle})
    assert result.design_context.available
    assert result.design_context.summary["backend_preference"] == policy
    assert "build_kdb" not in tools_in(result)


def test_unselected_xcelium_diagnostic_never_probes_kdb(tmp_path, monkeypatch):
    case = Case(tmp_path, "xcelium")
    case.discover_state()
    monkeypatch.setattr(config, "AUTO_KDB_BUILD", True)
    observed = []
    monkeypatch.setattr(server, "parse_compile_log", lambda path, sim: observed.append((path, sim)) or {"simulator": sim})
    monkeypatch.setattr(server, "probe_verdi_backend", lambda cr, **kw: {"kdb_path": None})
    result = server._handle_diagnostic_snapshot({})
    assert observed == []
    assert "build_kdb" not in tools_in(result) and "prepare_design_context" not in tools_in(result)
    assert not result.design_context.available


@pytest.mark.parametrize("errors,has_wave", [(1, True), (0, True), (1, False)])
def test_scan_and_optional_wave_evidence_remain_independent_of_tree(case, errors, has_wave):
    case.discover_state()
    case.parse_state(errors)
    if not has_wave:
        server._result_cache["get_sim_paths"] = server._result_cache["get_sim_paths"].model_copy(update={"wave_files": []})
    result = server._handle_diagnostic_snapshot({})
    assert "scan_structural_risks" in tools_in(result) and not result.hierarchy.available
    assert "sweep_handshakes" not in tools_in(result)
    if has_wave:
        assert result.protocol_health.summary["coverage_status"] == "not_run"
    else:
        assert result.protocol_health is None
    if not errors: assert "recommend_failure_debug_next_steps" not in tools_in(result)


def test_stale_legacy_tree_does_not_suppress_current_compile_scan_or_override_prepared_top(case):
    handle, _ = case.prepare(top="other")
    case.discover_state()
    server._result_cache["build_tb_hierarchy"] = schemas.BuildTbHierarchyResult(
        project={"top_module": "top", "simulator": "vcs"}, hierarchy_handle="tbh_old")
    server._result_provenance["build_tb_hierarchy"] = dict(compile_log="/other/compile.log", simulator="vcs")
    result = server._handle_diagnostic_snapshot({"design_handle": handle})
    assert result.hierarchy.available and result.hierarchy.stale
    assert result.top_module == "other" and "scan_structural_risks" in tools_in(result)
    assert "build_tb_hierarchy" not in tools_in(result)


@pytest.mark.parametrize("mismatch", ["primary", "simulator", "top", "supplements"])
def test_explicit_context_without_discovery_never_marks_an_unrelated_legacy_tree_current(case, mismatch, monkeypatch):
    supplement = case.supplement() if mismatch == "supplements" else None
    handle, _ = case.prepare(supplements=[supplement] if supplement else [])
    legacy = dict(compile_log=str(case.primary), simulator="vcs", supplementary_compile_logs=[])
    project = dict(top_module="top", simulator="vcs")
    if mismatch == "primary": legacy["compile_log"] = "/foreign/compile.log"
    elif mismatch == "simulator": legacy["simulator"] = project["simulator"] = "xcelium"
    elif mismatch == "top": project["top_module"] = "other"
    server._result_cache["build_tb_hierarchy"] = schemas.BuildTbHierarchyResult(project=project, hierarchy_handle="tbh_foreign")
    server._result_provenance["build_tb_hierarchy"] = legacy
    assert server._result_provenance["get_sim_paths"] is None
    with metadata_only(monkeypatch):
        result = server._handle_diagnostic_snapshot({"design_handle": handle})
    assert result.design_context.available and result.top_module == "top"
    assert result.hierarchy.available and result.hierarchy.stale is True
    assert result.design_context.summary["identity_validation"] == "not_revalidated"


@pytest.mark.parametrize("origin", ["verdi_npi", "static_inference", "log_only"])
def test_recommend_provenance_keeps_design_identity_and_coverage_without_current_claim(case, monkeypatch, origin):
    handle, ctx = case.prepare()
    case.discover_state()
    _, provenance = case.recommend_state(handle, ctx, origin=origin)
    assert provenance["design_handle"] == handle and provenance["context_identity_sha256"] == ctx.identity_sha256
    assert provenance["facts_origin"] == origin and provenance["source_coverage_status"] == "partial"
    write(case.source, "module top; wire changed; endmodule\nmodule other; endmodule\n")
    with metadata_only(monkeypatch): result = server._handle_diagnostic_snapshot({"design_handle": handle})
    assert result.recommended_next.available and result.recommended_next.stale is False
    summary = result.recommended_next.summary
    assert summary["facts_origin"] == origin and summary["source_coverage_status"] == "partial"
    assert summary["identity_validation"] == "not_revalidated"
    assert summary["context_identity_sha256"] == ctx.identity_sha256
    assert "current" not in summary and "load_status" not in summary


@pytest.mark.parametrize("change", ["selected_other", "ambiguous", "unknown", "removed", "registry_reset", "wrong_fingerprint"])
def test_recommend_cache_cannot_be_current_for_another_or_missing_context(case, change, monkeypatch):
    handle, ctx = case.prepare()
    case.discover_state()
    case.recommend_state(handle, ctx)
    args = {"design_handle": handle}
    if change in {"selected_other", "ambiguous"}:
        other, _ = case.prepare(top="other")
        args = {"design_handle": other} if change == "selected_other" else {}
    elif change == "unknown": args["design_handle"] = "dc_unknown"
    elif change == "removed": assert server._design_context_store.remove(handle)
    elif change == "registry_reset": monkeypatch.setattr(server, "_design_context_store", DesignContextStore())
    else: server._result_provenance["recommend_failure_debug_next_steps"]["context_identity_sha256"] = "f" * 64
    with metadata_only(monkeypatch): result = server._handle_diagnostic_snapshot(args)
    assert result.recommended_next.available and result.recommended_next.stale
    assert result.primary_failure_target is None and result.recommended_signals is None
    assert result.recommended_next.summary["identity_validation"] == "not_revalidated"


def test_recent_tree_is_never_used_as_recommend_request_compile_identity(case):
    case.discover_state()
    server._session_state["build_tb_hierarchy"] = dict(compile_log="/foreign/latest.log", simulator="xcelium")
    server._result_provenance["build_tb_hierarchy"] = dict(server._session_state["build_tb_hierarchy"])
    context = server._build_recommend_request_context(dict(log_path=str(case.log), wave_path=str(case.wave)))
    suggested = server._build_suggested_call("recommend_failure_debug_next_steps")
    assert context["compile_log"] == str(case.primary)
    assert suggested["arguments"]["compile_log"] == str(case.primary)
    server._session_state["get_sim_paths"] = None
    server._result_cache["get_sim_paths"] = None
    server._result_provenance["get_sim_paths"] = None
    assert server._build_recommend_request_context({})["compile_log"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("tool", TRACKING)
async def test_native_tracking_entry_rejects_missing_requested_metadata_not_ambient_tree(case, tool):
    case.discover_state()
    requested = dict(compile_log="/requested/compile.log", simulator="verilator", top_hint="requested_top",
                     supplementary_compile_logs=["/requested/phase0.log", "/requested/phase1.log"])
    args = {**requested, "wave_path": str(case.wave), "signal_path": "top.q"}
    if tool == "trace_signal_path":
        args.pop("signal_path")
        args.update(from_signal="top.q", to_signal="top.r")
    if tool == "trace_x_source": args["time_ps"] = 10
    result = await public(tool, args)
    assert result["error_code"] == "compile_metadata_unavailable"
    assert "suggested_call" not in result
    assert result["native_route"]["source_context_status"] == "not_required"
    assert server._session_state["build_tb_hierarchy"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("tool", HANDLE_TOOLS)
async def test_legacy_handle_gate_retains_explicit_full_build_compatibility(case, tool):
    case.discover_state()
    args = {"handle": "tbh_unknown"}
    args.update({"root": "top"} if tool == "get_tb_subtree" else {"path": "top"} if tool == "find_tb_instance" else
                {"basename": "design.sv"} if tool == "lookup_tb_files" else {"path": str(case.source)} if tool == "get_tb_file_detail" else
                {"section": "compile_result"} if tool == "dump_tb_section" else {})
    result = await public(tool, args)
    assert result["missing_step"] == "build_tb_hierarchy"
    assert result["suggested_call"] == {"tool": "build_tb_hierarchy", "arguments": dict(
        compile_log=str(case.primary), simulator="vcs")}


@pytest.mark.parametrize("logs", ["elaborate", "compile_only", "none"])
def test_discovery_next_step_is_scan_without_probe_or_context_creation(case, monkeypatch, logs):
    monkeypatch.setattr(path_discovery, "get_fsdb_runtime_info", lambda: {"enabled": False})
    monkeypatch.setattr(path_discovery, "fst_runtime_info", lambda: {})
    compile_logs = [] if logs == "none" else [entry(case.primary, phase="elaborate" if logs == "elaborate" else "compile")]
    result = path_discovery._build_discovery_result(request_root=case.root, case_name="case0", config_source="fixture",
        config_root=None, discovery_mode="case_dir", target_case_dir=case.case, compile_logs=compile_logs,
        sim_logs=[entry(case.log, phase="runtime")], wave_files=[entry(case.wave, format="vcd")], available_cases=[], hints=[])
    if logs == "none": assert "next_required_step" not in result
    else:
        step = result["next_required_step"]
        assert step["tool"] == "scan_structural_risks" and step["compile_log"] == str(case.primary)
        assert step["simulator"] == "vcs" and "native-first" in step["reason"]
        schemas.NextRequiredStep.model_validate(step)
    assert server._design_context_store.summaries() == []
    assert server._session_state["build_tb_hierarchy"] is None


def legacy_context():
    return dict(compile_log="/exact/primary.log", supplementary_compile_logs=["/exact/phase_a.log", "/exact/phase_b.log"],
                simulator="vcs", top_hint="top", **TOKENS)


def divergent_result(side):
    return {f"wave_path_{side}": f"/waves/{side}.vcd", f"signal_{side}": "top.q",
            "first_divergence_time_ps": 42, "cursor": {"name": "divergence"}}


@pytest.mark.anyio
@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("reason,status", [("hierarchy_unavailable", "ready"), ("handle_expired", "ready"),
    ("compile_context_changed", "needs_context"), ("compile_context_ambiguous", "needs_context")])
async def test_legacy_divergence_action_preserves_all_historical_tokens_and_independent_scans(side, reason, status):
    original = legacy_context()
    action = driver_action(result=divergent_result(side), side=side, context=original, reason=reason)
    assert action["status"] == status
    assert action["arguments"]["compile_context"] == original
    assert all(action["arguments"]["compile_context"][key] == value for key, value in TOKENS.items())
    assert action["evidence"]["cursor_name"] == "divergence" and action["evidence"]["time_ps"] == 42
    assert [call["tool"] for call in action["prerequisite_calls"]] == ["scan_structural_risks"] * 3
    assert [call["arguments"]["compile_log"] for call in action["prerequisite_calls"]] == [
        original["compile_log"], *original["supplementary_compile_logs"]]
    assert all(call["depends_on"] == [] and call["parallel_group"] == f"compile_{side}"
               for call in action["prerequisite_calls"])
    if status == "ready":
        assert action["missing_fields"] == [] and action["evidence"]["context_recovery"] == "required_before_query"
    else: assert action["missing_fields"] == [f"context_{side}.verified_identity"]
    tools = {item.name: item.inputSchema for item in await server.list_tools()}
    jsonschema.validate(action["arguments"], tools[action["tool"]])
    for call in action["prerequisite_calls"]: jsonschema.validate(call["arguments"], tools[call["tool"]])
    assert original == legacy_context()


@pytest.mark.anyio
async def test_expired_prepared_action_declares_new_current_inputs_and_never_reuses_historical_identity():
    ctx = {**legacy_context(), "design_handle": "dc_expired", "backend_preference": "source",
           "kdb_path": "/declared/kdb.elab++", "context_identity_sha256": "e" * 64}
    action = driver_action(result=divergent_result("b"), side="b", context=ctx, reason="handle_expired")
    assert action["status"] == "needs_prerequisite"
    assert action["evidence"]["preparation_identity"] == "current_inputs_only"
    prepare, *scans = action["prerequisite_calls"]
    assert prepare["tool"] == "prepare_design_context" and prepare["depends_on"] == []
    assert prepare["arguments"] == {key: ctx[key] for key in (
        "compile_log", "simulator", "supplementary_compile_logs", "top_hint", "backend_preference", "kdb_path")}
    assert not (set(TOKENS) | {"design_handle", "context_identity_sha256"}) & prepare["arguments"].keys()
    assert len(scans) == 3 and all(call["tool"] == "scan_structural_risks" for call in scans)
    assert all(call["parallel_group"] == prepare["parallel_group"] for call in scans)
    tools = {item.name: item.inputSchema for item in await server.list_tools()}
    for call in action["prerequisite_calls"]: jsonschema.validate(call["arguments"], tools[call["tool"]])


def test_action_with_no_context_or_bound_prepared_context_never_borrows_session(case):
    handle, _ = case.prepare()
    case.discover_state()
    missing = driver_action(result=divergent_result("a"), side="a", context=None)
    assert missing["status"] == "needs_context" and missing["prerequisite_calls"] == []
    assert "design_handle" not in missing["arguments"] and "compile_log" not in missing["arguments"]
    ready = driver_action(result=divergent_result("a"), side="a", context={"design_handle": "old"},
                          bound=SimpleNamespace(design_handle=handle))
    assert ready["status"] == "ready" and ready["arguments"]["design_handle"] == handle
    assert ready["prerequisite_calls"] == []
