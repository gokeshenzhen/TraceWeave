"""Tests for src/usage_telemetry.py — the passive usage telemetry recorder
and the pure aggregation function backing scripts/telemetry_report.py."""

import importlib
import json

import config
import src.usage_telemetry as ut


def _reset_module():
    # Module holds process-global session state; reload to isolate tests.
    importlib.reload(ut)
    return ut


def test_record_call_appends_jsonl(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.note_session("case-A")
    mod.record_call("period", {"signal_path": "/tb/clk", "edge": "posedge"},
                    result_bytes=120, ok=True, latency_ms=4.2, case="cc28")

    lines = log.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "period"
    # arg values are NOT logged; only keys + whitelisted scalar flags.
    assert rec["arg_keys"] == ["edge", "signal_path"]
    assert rec["flags"] == {"edge": "posedge"}
    assert "signal_path" not in rec["flags"]
    assert rec["ok"] is True
    assert rec["result_bytes"] == 120
    assert rec["case"] == "cc28"
    assert rec["session_id"]


def test_opt_out_writes_nothing(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", False)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call("period", {}, result_bytes=10, ok=True)
    assert not log.exists()


def test_record_call_never_raises(monkeypatch):
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(config, "telemetry_log_path", boom)
    mod = _reset_module()
    # Must swallow the path error rather than propagate into the call path.
    mod.record_call("period", {}, result_bytes=10, ok=True)


def test_session_minting_on_identity_change(monkeypatch):
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    mod = _reset_module()

    s1 = mod.note_session("case-A")
    s1b = mod.note_session("case-A")  # same case keeps the session
    s2 = mod.note_session("case-B")   # new case mints a new session

    assert s1 == s1b
    assert s2 != s1


def test_aggregate_presence_and_distribution():
    records = [
        # session 1: uses period once, plus two other tools
        {"session_id": "s1", "tool": "period", "ok": True, "result_bytes": 100},
        {"session_id": "s1", "tool": "parse_sim_log", "ok": True, "result_bytes": 400},
        {"session_id": "s1", "tool": "cursor_set", "ok": True, "result_bytes": 50},
        # session 2: no tracked primitives at all
        {"session_id": "s2", "tool": "parse_sim_log", "ok": False, "result_bytes": 200},
    ]
    report = ut.aggregate(records)

    assert report["total_records"] == 4
    assert report["total_sessions"] == 2

    # period used in 1 of 2 sessions -> 0.5 presence
    assert report["tracked_features"]["period"]["sessions_used"] == 1
    assert report["tracked_features"]["period"]["session_presence"] == 0.5
    # diff_first_divergence never used
    assert report["tracked_features"]["diff_first_divergence"]["sessions_used"] == 0

    # cursor_set rolls up under the "cursor" feature
    assert report["tracked_features"]["cursor"]["calls"] == 1

    # parse_sim_log: 2 calls, ok in 1 -> 0.5 ok_rate, present in both sessions
    pst = report["per_tool"]["parse_sim_log"]
    assert pst["calls"] == 2
    assert pst["ok_rate"] == 0.5
    assert pst["session_presence"] == 1.0

    # per-session call counts: s1=3, s2=1
    cps = report["calls_per_session"]
    assert cps["min"] == 1 and cps["max"] == 3


def test_aggregate_buckets_missing_session():
    records = [{"tool": "period", "ok": True, "result_bytes": 10}]
    report = ut.aggregate(records)
    assert report["total_sessions"] == 1  # synthetic "(none)" bucket
