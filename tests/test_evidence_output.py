"""Lossless wire projections checked against independent waveform expectations."""
from copy import deepcopy
from dataclasses import replace
import asyncio
import json
from pathlib import Path
import sys
import threading

import pytest

import server
from src import schemas
from src.cancellation import OperationCancelled, check_cancelled
from src.evidence_output import compact_evidence_result, expand_compact_result
from src.transaction_sampling import TransactionLimits
from test_tlul import fixture as tlul_fixture


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    server.reset_session_state()
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", sys.executable)
    monkeypatch.setenv("TRACEWEAVE_AUTO_KDB", "0")
    yield
    server.reset_session_state()


async def compact_call(monkeypatch, tool, args):
    """Compare the exact same analysis, including volatile measurements."""
    captured = []
    original = server._dispatch

    async def capture(name, arguments):
        assert "output_format" not in arguments
        result = await original(name, arguments)
        captured.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(server, "_dispatch", capture)
        contents = await server.call_tool(tool, {**args, "output_format": "compact"})
    wire = json.loads(contents[0].text)
    assert wire["format"] == "traceweave.compact.v1", wire
    expanded = expand_compact_result(wire)
    assert expanded == json.loads(server._serialize_result(captured[0]))
    return expanded, wire


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["normal", "missing", "unknown", "no_edges", "sample_budget", "state_budget", "display"])
async def test_tlul_projection_preserves_checked_missing_unknown_and_partial(tmp_path, monkeypatch, case):
    edits = {2: {"a_source": "x"}} if case == "unknown" else None
    parser, fields = tlul_fixture(tmp_path, packed=True, edits=edits)
    args = dict(wave_path=parser.file_path, clock="tb.clk", reset="tb.reset", fields=fields)
    if case == "missing":
        args["fields"] = {}
    elif case == "no_edges":
        args.update(start_time_ps=0, end_time_ps=4)
    elif case == "sample_budget":
        args["max_cycles"] = 2
    elif case == "display":
        args["max_transactions"] = 1
    elif case == "state_budget":
        original = server.inspect_tlul
        monkeypatch.setattr(server, "inspect_tlul", lambda **kw: original(
            **kw, _limits=replace(TransactionLimits(), pending=1)))
    result, wire = await compact_call(monkeypatch, "inspect_tlul", args)
    if case == "missing":
        assert result["mapping_status"] == "mapping_required"
        assert result["coverage_status"] == "zero_coverage" and result["checks"] == []
    elif case == "no_edges":
        assert result["coverage_status"] == "zero_coverage"
        assert result["gaps"] == ["no_clock_edges"]
    elif case in {"unknown", "sample_budget", "state_budget"}:
        assert result["coverage_status"] == "partial" and result["gaps"]
        if case == "unknown":
            assert result["unknown_id_beats"] == 1
        elif case == "sample_budget":
            assert result["sample_limit_reached"] and result["tail"] == "sample_limit"
        else:
            assert result["transactions"]["analysis"]["stop_reason"] == "pending_budget"
    else:
        assert result["coverage_status"] == "complete"
        assert result["accepted_a_count"] == result["accepted_d_count"] == 3
        txn = result["transactions"]
        assert txn["matched_count"] == 2 and txn["outstanding_at_end"] == 1
        assert txn["unmatched_completion_count"] == 1
        assert txn["analysis"]["analyzed_samples"] == 9
        assert txn["analysis"]["display_status"] == ("partial" if case == "display" else "complete")
        assert len(wire["references"]) == 3
        assert "selections" not in wire["result"]["transactions"]
        assert [(t["request_time_ps"], t["completion_time_ps"]) for t in txn["transactions"]] == (
            [(25, 35)] if case == "display" else [(25, 35), (35, 55)])
    # Explicit window boundaries and limitations survive every representation.
    assert "carry_in" in result and "tail" in result and result["warnings"]


def test_projection_is_immutable_and_only_factors_exact_ordered_receipts():
    selection = {"path": "top.bus[7:0]", "key": "top.bus@bits(7,0)",
                 "declared_range": {"left": 7, "right": 0}, "bits": [7, 0], "width": 2}
    different = {**selection, "bits": [0, 7]}
    original = dict(selections=[selection], channels={
        "a": {"selections": [selection]}, "d": {"selections": [different]}},
        transactions={"selections": [selection], "gaps": ["unknown_history"]})
    before = deepcopy(original)
    projection = compact_evidence_result("inspect_tlul", original)
    assert original == before
    assert len(projection.references) == 2
    assert projection.result["channels"]["d"]["selections"] == [different]
    expanded = expand_compact_result(projection)
    assert expanded == before
    expanded["channels"]["a"]["selections"][0]["bits"][0] = 5
    assert expanded["selections"][0]["bits"] == [7, 0]
    # References contain their own evidence and need no server lifecycle.
    server.reset_session_state()
    assert expand_compact_result(projection) == before


@pytest.mark.parametrize("corruption", ["target", "duplicate", "missing_parent", "existing_field", "wrong_tool", "too_many"])
def test_bad_references_are_errors_not_empty_evidence(corruption):
    raw = dict(format="traceweave.compact.v1", tool="inspect_tlul",
               result={"selections": [{"bits": [1]}], "channels": {"a": {}}},
               references=[{"path": "/result/channels/a/selections", "target": "/result/selections"}])
    if corruption == "target":
        del raw["result"]["selections"]
    elif corruption == "duplicate":
        raw["references"] *= 2
    elif corruption == "missing_parent":
        raw["result"]["channels"].clear()
    elif corruption == "existing_field":
        raw["result"]["channels"]["a"]["selections"] = []
    elif corruption == "wrong_tool":
        raw["tool"] = "reconstruct_transactions"
    else:
        raw["references"] *= 4
    with pytest.raises(ValueError):
        expand_compact_result(raw)


@pytest.mark.anyio
async def test_compact_caps_cursor_names_filters_and_summary(monkeypatch, tmp_path):
    parser, fields = tlul_fixture(tmp_path, packed=True)
    base = dict(wave_path=parser.file_path, clock="tb.clk", reset="tb.reset",
                req_valid=fields["a_valid"], req_ready=fields["a_ready"], req_id=fields["a_source"],
                cmp_valid=fields["d_valid"], cmp_ready=fields["d_ready"], cmp_id=fields["d_source"])
    results = []
    for cap, name in ((1, "small"), (32, "large")):
        r, _ = await compact_call(monkeypatch, "reconstruct_transactions",
                                  {**base, "max_transactions": cap, "cursor_name": name})
        assert r["matched_count"] == 2 and r["cursor"]["name"] == name
        results.append(r)
    assert results[0]["cursor"]["time_ps"] == results[1]["cursor"]["time_ps"] == 75
    assert results[0]["latency"] == results[1]["latency"]
    for scope in ("tb", "absent"):
        r, _ = await compact_call(monkeypatch, "suggest_handshakes",
            dict(wave_path=parser.file_path, scope=scope, max_candidates=1))
        assert r["discovery"]["status"] == "complete"
        assert r["candidate_count"] == 0  # packed declarations are not guessed as roles
    summary, _ = await compact_call(monkeypatch, "get_waveform_summary", {"wave_path": parser.file_path})
    assert summary["format"] == "VCD" and summary["simulation_duration_ps"] == 90


@pytest.mark.anyio
async def test_comparison_preserves_unknown_prefix_and_exact_evidence(tmp_path, monkeypatch):
    from test_divergence_compare import Parser
    parser = Parser({"a": [(0, "x"), (5, "0"), (8, "1")], "b": [(0, "0")]})
    monkeypatch.setattr(server, "_get_parser", lambda _: parser)
    r, _ = await compact_call(monkeypatch, "diff_first_divergence", dict(
        wave_path_a="a.vcd", wave_path_b="a.vcd", signal_a="a", signal_b="b", end_time_ps=10))
    assert r["diverged"] and not r["earliest_difference_proven"]
    assert r["first_divergence_time_ps"] == 8 and r["coverage_gaps"]
    assert any(g["reason"] == "value_unknown" for g in r["coverage_gaps"])
    assert len(r["next_actions"]) == 2


@pytest.mark.anyio
async def test_history_dynamic_observation_and_frontier_survive(tmp_path, monkeypatch):
    from test_x_history import setup
    args = await setup(tmp_path)
    r, _ = await compact_call(monkeypatch, "trace_x_source", args)
    history = r["history"]
    assert r["trace_status"] == "partial" and history["coverage"]["true_origin_proven"] is False
    assert [n.get("relation") for n in history["nodes"][:3]] == ["hold", "hold", "register_sample"]
    assert [n["observation"]["trigger_time_ps"] for n in history["nodes"][:3]] == [35, 25, 15]
    assert history["context"]["backend_status"]["single_backend_provenance"]
    assert history["frontier"]


@pytest.mark.anyio
async def test_divergence_preserves_both_contexts_and_dynamic_sides(tmp_path, monkeypatch):
    from test_trace_divergence import setup_case
    args = await setup_case(tmp_path)
    r, _ = await compact_call(monkeypatch, "trace_divergence", args)
    assert set(r["contexts"]) == {"a", "b"}
    assert r["comparison"]["first_divergence_time_ps"] == 5
    assert r["nodes"][0]["a"]["observation"]["trigger_time_ps"] == 5
    assert r["nodes"][0]["b"]["observation"]["trigger_time_ps"] == 5


@pytest.mark.parametrize("semantic_status", ["not_run", "partial", "unavailable", "complete"])
def test_structural_summary_never_combines_independent_coverage(semantic_status):
    result = schemas.ScanStructuralRisksResult(
        analysis_mode="auto", lexical_coverage_status="degraded", coverage_status="degraded",
        files_scanned=1, eligible_file_count=2, coverage_warnings=["missing_source"],
        total_risks=5, categories_scanned=["a", "b", "c", "d"],
        semantic=schemas.SemanticScanResult(status=semantic_status, scope="top.u",
            gaps=["query_artifact_port_bindings_only"], query_artifact_status="reused",
            propagation=schemas.StructuralPropagationReceipt(status="partial", gaps=["writer_inventory_incomplete"])))
    for shrink in (server._shrink_scan_structural_risks_stage1,
                   server._shrink_scan_structural_risks_stage2, server._shrink_scan_structural_risks_terminal):
        trimmed = shrink(result)
        assert trimmed.categories_scanned == ["a", "b", "c", "d"]
        summary = server._extract_structural_scan_summary(trimmed)
        assert summary["lexical_coverage_status"] == "degraded"
        assert summary["semantic_status"] == semantic_status
        assert summary["semantic_gaps"] == ["query_artifact_port_bindings_only"]
        assert summary["semantic_propagation"]["status"] == "partial"
        assert summary["semantic_query_artifact_status"] == "reused"
        assert summary["display_truncated"] and summary["high_risk_count_basis"] == "displayed_risks"


def test_snapshot_protocol_summary_keeps_discovery_and_zero_coverage():
    result = schemas.HandshakeSweepResult(wave_path="w.vcd", coverage_status="zero_coverage",
        scope="top.u", start_ps=1, end_ps=50,
        discovery={"valid_ready": schemas.SignalDiscoveryCoverage(status="partial", reasons=["signal_limit"])},
        suggested_next_actions=[{"tool": "sweep_handshakes", "arguments": {"scope": "top.u.child"}}])
    summary = server._extract_protocol_health_summary(result)
    assert summary["coverage_status"] == "zero_coverage"
    assert summary["discovery"]["valid_ready"]["status"] == "partial"
    assert summary["end_ps"] == 50 and summary["suggested_next_actions"]


def test_output_budget_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(schemas, "TOKEN_BUDGET_SOFT_LIMIT", 1200)
    result = schemas.ScanStructuralRisksResult(coverage_warnings=["证据" * 100])
    assert len(result.model_dump_json(exclude_none=True)) < 1200
    assert len(result.model_dump_json(exclude_none=True).encode()) > 1200
    called = []
    def shrink(value):
        called.append(True)
        return value
    server._enforce_output_budget(result, [shrink])
    assert called and result.payload_bytes > 1200


@pytest.mark.anyio
async def test_registration_defaults_and_public_validation_share_schema(monkeypatch):
    tools = {t.name: t for t in await server.list_tools()}
    options = schemas.EvidenceOutputOptions.model_json_schema()["properties"]
    for name in schemas.COMPACT_OUTPUT_TOOLS:
        assert tools[name].inputSchema["properties"]["output_format"] == options["output_format"]
    for name, model in (("inspect_tlul", schemas.TlulAnalysisOptions),
                        ("reconstruct_transactions", schemas.TransactionDisplayOptions)):
        for key, prop in model.model_json_schema()["properties"].items():
            assert all(tools[name].inputSchema["properties"][key][k] == v for k, v in prop.items())
    for args in ({"max_transactions": 0}, {"max_transactions": 65537},
                 {"max_transactions": True}, {"max_cycles": 0}, {"max_wait_cycles": -1}, {"edge": "both"}):
        # Validate even if the incomplete mapping would otherwise return early.
        result = json.loads((await server.call_tool("inspect_tlul", {
            "wave_path": "missing.fsdb", "clock": "top.clk", **args}))[0].text)
        assert "error" in result and "format" not in result
    result = json.loads((await server.call_tool("get_waveform_summary", {
        "wave_path": "missing.vcd", "output_format": "short"}))[0].text)
    assert "error" in result


@pytest.mark.anyio
async def test_full_default_and_error_envelopes_are_compatible(monkeypatch):
    model = schemas.ToolErrorResult(error="missing signal")
    async def fake(name, args):
        return model
    monkeypatch.setattr(server, "_dispatch", fake)
    for options in ({}, {"output_format": "full"}, {"output_format": "compact"}):
        result = await server.call_tool("inspect_tlul", options)
        assert result[0].text == server._serialize_result(model)
    model = schemas.PrerequisiteBlockResult(missing_step="build_tb_hierarchy", required_before="trace_x_source", reason="missing")
    result = await server.call_tool("trace_x_source", {"output_format": "compact"})
    assert result[0].text == server._serialize_result(model)


@pytest.mark.anyio
async def test_cancelled_projection_exits_worker_and_next_request_succeeds(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    async def fake(name, args):
        return {"coverage_status": "partial", "gaps": ["unknown"]}
    original = server.serialize_compact_result
    def slow(name, result):
        entered.set()
        try:
            assert release.wait(3)
            check_cancelled()
            return original(name, result)
        finally:
            finished.set()
    monkeypatch.setattr(server, "_dispatch", fake)
    monkeypatch.setattr(server, "serialize_compact_result", slow)
    task = asyncio.create_task(server.call_tool("inspect_tlul", {"output_format": "compact"}))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 3)
    monkeypatch.setattr(server, "serialize_compact_result", original)
    r, _ = await compact_call(monkeypatch, "inspect_tlul", {})
    assert r["coverage_status"] == "partial"


@pytest.mark.anyio
async def test_native_compact_preserves_scale_predecessor_and_display_cap(monkeypatch, require_fsdb_runtime):
    path = str(Path(__file__).parent / "fixtures/scale_100fs.fsdb")
    original = server.serialize_compact_result
    def unlocked(tool, result):
        lock = server._wave_locks_for([path])[0]
        assert lock.acquire(blocking=False), "output projection must run after releasing the wave lock"
        lock.release()
        return original(tool, result)
    monkeypatch.setattr(server, "serialize_compact_result", unlocked)
    r, _ = await compact_call(monkeypatch, "get_signal_transitions", dict(
        wave_path=path, signal_path="scale_100fs_tb.addr[31:0]",
        start_time_ps=50000, end_time_ps=100100, max_transitions=1))
    assert r["predecessor"]["time_ps"] < 50000
    assert len(r["transitions"]) == 1
    assert all(50000 <= event["time_ps"] <= 100100 for event in r["transitions"])
    parser = server._get_parser(path)
    assert parser._supports_event_pages() and not parser._transition_group_active


@pytest.mark.anyio
async def test_response_references_do_not_reuse_evidence_after_file_change(tmp_path, monkeypatch):
    parser, fields = tlul_fixture(tmp_path, packed=True)
    args = dict(wave_path=parser.file_path, clock="tb.clk", reset="tb.reset", fields=fields)
    before, captured = await compact_call(monkeypatch, "inspect_tlul", args)
    tlul_fixture(tmp_path, packed=True, edits={2: {"a_source": "x"}})
    after, _ = await compact_call(monkeypatch, "inspect_tlul", args)
    assert before["coverage_status"] == "complete" and after["coverage_status"] == "partial"
    assert after["unknown_id_beats"] == 1
    server.reset_session_state()
    assert expand_compact_result(captured) == before  # a historical response, not current evidence
