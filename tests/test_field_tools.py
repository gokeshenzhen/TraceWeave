"""Public dispatch, trusted layout identities, and selection receipts."""
import asyncio
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import server
import src.source_graph_production as production
from src.schemas import HandshakeInspectResult, TxnReconstructResult
from src.verify_condition import inspect_handshake
from src.txn_reconstruct import reconstruct_transactions
from tests.test_tlul import fixture
from tests.test_waveform_selection import dump


@pytest.mark.anyio
async def test_public_point_transition_cycle_and_window_selections(tmp_path, monkeypatch):
    parser = dump(tmp_path)
    monkeypatch.setattr(server, "_get_parser", lambda _: parser)
    monkeypatch.setattr(server, "_check_prerequisites", lambda *a: None)
    selection = {"path": "tb.bus[15:8]", "lsb": 8, "width": 4}
    common = {"wave_path": parser.file_path}
    point = await server._dispatch("get_signal_at_time", {**common, "signal_path": selection, "time_ps": 5})
    assert point.value["bin"] == "xz10"
    assert point.selections[0].declared_range == {"left": 15, "right": 8}
    transitions = await server._dispatch("get_signal_transitions", {**common, "signal_path": selection})
    assert [row["value"]["bin"] for row in transitions.transitions] == ["xz10", "xxxx", "zzzz"]
    cycles = await server._dispatch("get_signals_by_cycle", {**common, "clock_path": "tb.clk",
                                    "signal_paths": [selection], "num_cycles": 4})
    assert len(cycles.selections) == 1
    assert [list(c.signals.values())[0].bin for c in cycles.cycles] == ["xz10", "xz10", "xxxx", "zzzz"]
    around = await server._dispatch("get_signals_around_time", {**common, "signal_paths": [selection],
                                  "center_time_ps": 15, "window_ps": 5, "extra_transitions": 1})
    assert list(around.signals.values())[0]["value_at_center"]["bin"] == "xz10"
    assert len(around.selections) == 1


def test_generic_engines_accept_the_same_explicit_selections(tmp_path):
    parser, fields = fixture(tmp_path, packed=True)
    common = dict(get_parser=lambda _: parser, wave_path=parser.file_path, clock="tb.clk")
    hs = inspect_handshake(**common, valid=fields["a_valid"], ready=fields["a_ready"], payload=[fields["a_data"]])
    assert hs["transfer_count"] == 3
    assert len(HandshakeInspectResult.model_validate(hs).selections) == 3
    tx = reconstruct_transactions(**common, req_valid=fields["a_valid"], req_ready=fields["a_ready"],
        req_id=fields["a_source"], cmp_valid=fields["d_valid"], cmp_ready=fields["d_ready"], cmp_id=fields["d_source"])
    assert tx["matched_count"] == 2
    assert len(TxnReconstructResult.model_validate(tx).selections) == 6


@pytest.mark.anyio
async def test_public_layout_uses_current_types_and_rejects_changed_source(tmp_path, monkeypatch):
    source = tmp_path / "top.sv"
    source.write_text("""module leaf #(parameter W=5) ();
typedef struct packed {logic [1:0] kind; logic [W-1:0] data;} payload_t;
typedef struct packed {logic valid; payload_t payload; logic ready;} packet_t;
packet_t bus;
endmodule
module top;
leaf #(.W(5)) u();
endmodule
""")
    log = tmp_path / "build.log"
    log.write_text(f"Command: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n top\n")
    wave = tmp_path / "typed.vcd"
    wave.write_text("""$scope module top $end
$scope module u $end
$var wire 9 b bus [8:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b101010101 b
""")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", sys.executable)
    monkeypatch.setenv("TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY", "off")
    monkeypatch.setattr(production, "_PROCESS_SESSION", production.SourceGraphRuntimeSession())
    monkeypatch.setattr(server, "_check_prerequisites", lambda *a: None)
    args = {"compile_log": str(log), "simulator": "vcs"}
    await asyncio.gather(server._dispatch("build_tb_hierarchy", args), server._dispatch("scan_structural_risks", args))
    query = {**args, "wave_path": str(wave), "signal_path": "top.u.bus[8:0]", "source_signal": "top.u.bus",
             "fields": ["valid", "payload.kind", "payload.data", "ready"]}
    r = await server._dispatch("resolve_packed_fields", query)
    assert r.status == "resolved", r
    assert {k: v.bits for k, v in r.fields.items()} == {
        "valid": [8], "payload.kind": [7, 6], "payload.data": [5, 4, 3, 2, 1], "ready": [0]}
    assert r.evidence["parameterization"]
    source.write_text(source.read_text().replace(".W(5)", ".W(6)"))
    stale = await server._dispatch("resolve_packed_fields", query)
    assert stale.status == "mapping_required" and stale.fields == {}
    assert "compile_session_snapshot_changed" in stale.reasons


@pytest.mark.anyio
async def test_tool_selection_schema_is_shared_and_tlul_missing_map_is_explicit(tmp_path, monkeypatch):
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["get_signal_at_time"].inputSchema
    validator = Draft202012Validator(schema)
    for signal in ("tb.bus", {"path": "tb.bus", "bits": [7, 2]}):
        validator.validate({"wave_path": "test.vcd", "signal_path": signal, "time_ps": 0})
    assert not validator.is_valid({"wave_path": "test.vcd", "time_ps": 0,
        "signal_path": {"path": "tb.bus", "bits": ["bad"]}})
    monkeypatch.setattr(server, "_check_prerequisites", lambda *a: None)
    r = await server._dispatch("inspect_tlul", {"wave_path": str(tmp_path / "absent.vcd"), "clock": "tb.clk"})
    assert r.mapping_status == "mapping_required" and r.checks == []
