"""End-to-end discovery reuse and conservative automatic sweep semantics."""
from pathlib import Path

import pytest

from src import handshake_suggest as suggest
from src.handshake_sweep import sweep_handshake_anomalies
from src.schemas import HandshakeSweepResult, SuggestHandshakesResult
from src.vcd_parser import VCDParser


def waveform(tmp_path, req_ack=False):
    valid, ready = ("req_o", "ack_i") if req_ack else ("valid_o", "ready_i")
    path = tmp_path / "both.vcd"
    path.write_text(f"""$timescale 1ps $end
$scope module tb $end
$var wire 1 ! clk_i $end
$var wire 1 v {valid} $end
$var wire 1 r {ready} $end
$var wire 2 h htrans_o $end
$var wire 1 q hready_i $end
$upscope $end
$enddefinitions $end
#0
0!
0v
1r
b00 h
1q
#5
1!
#10
0!
#15
1!
#20
0!
""")
    return VCDParser(str(path))


def test_sweep_shares_one_snapshot_keeps_idle_counts_and_no_flags(tmp_path, monkeypatch):
    parser = waveform(tmp_path)
    real = parser.enumerate_scope_page
    pages = []
    def metered(*args, **kw):
        pages.append(1)
        return real(*args, **kw)
    monkeypatch.setattr(parser, "enumerate_scope_page", metered)
    monkeypatch.setattr(parser, "search_signals", lambda *a, **k: pytest.fail("unnecessary search"))
    result = sweep_handshake_anomalies(get_parser=lambda _: parser, wave_path=parser.file_path)
    assert len(pages) == 1
    assert result["interface_count"] == 2 and result["flagged_count"] == 0
    assert result["discovery"]["valid_ready"] == result["discovery"]["ahb"]
    assert result["coverage_status"] == "complete"
    assert all(row["ready_without_valid_cycles"] == 2 for row in result["interfaces"])
    assert all(not row["flags"] for row in result["interfaces"])
    assert result["finding_summary"] is None
    HandshakeSweepResult.model_validate(result)


def test_req_ack_not_silently_checked_with_valid_hold_rules(tmp_path):
    parser = waveform(tmp_path, req_ack=True)
    result = sweep_handshake_anomalies(get_parser=lambda _: parser, wave_path=parser.file_path)
    assert result["interface_count"] == 1 and result["coverage_status"] == "degraded"
    assert any("explicit valid-hold semantics" in s["reason"] for s in result["skipped"])


def test_partial_snapshot_shared_and_never_upgraded_by_all_candidates_checked(tmp_path, monkeypatch):
    parser = waveform(tmp_path)
    monkeypatch.setattr(suggest, "DISCOVERY_SIGNAL_LIMIT", 4)
    result = sweep_handshake_anomalies(get_parser=lambda _: parser, wave_path=parser.file_path)
    assert result["coverage_status"] != "complete"
    for receipt in result["discovery"].values():
        assert receipt["status"] == "partial" and receipt["scope_total"] is None
        assert receipt["scope_total_lower_bound"] == 4


def test_legacy_wrapper_uses_one_search_in_sweep(tmp_path, monkeypatch):
    parser = waveform(tmp_path)
    searches = []
    real = parser.search_signals
    monkeypatch.setattr(parser, "enumerate_scope_page", lambda *a, **k: (_ for _ in ()).throw(NotImplementedError()))
    def counted(*args, **kwargs):
        searches.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(parser, "search_signals", counted)
    result = sweep_handshake_anomalies(get_parser=lambda _: parser, wave_path=parser.file_path)
    assert len(searches) == 1 and result["interface_count"] == 2
    assert result["discovery"]["ahb"]["mode"] == "legacy_search"


def test_empty_discovery_hints_reuse_snapshot_and_report_zero_coverage(tmp_path, monkeypatch):
    parser = waveform(tmp_path)
    path = Path(parser.file_path)
    path.write_text(path.read_text().replace("valid_o", "packed_req_o").replace("ready_i", "packed_rsp_i").replace("htrans_o", "bus_request"))
    monkeypatch.setattr(parser, "search_signals", lambda *a, **k: pytest.fail("empty result probed again"))
    result = suggest.suggest_handshakes(get_parser=lambda _: parser, wave_path=parser.file_path)
    assert result["candidate_count"] == 0
    SuggestHandshakesResult.model_validate(result)
    sweep = sweep_handshake_anomalies(get_parser=lambda _: parser, wave_path=parser.file_path)
    assert sweep["coverage_status"] == "zero_coverage"
