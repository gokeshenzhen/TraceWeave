"""Tests for src/usage_telemetry.py — the passive usage telemetry recorder
and the pure aggregation function backing scripts/telemetry_report.py."""

import importlib
import json
import os
import subprocess
import sys

import config
import src.usage_telemetry as ut


def _reset_module():
    # Module holds process-global session state; reload to isolate tests.
    importlib.reload(ut)
    return ut


def _telemetry_enabled_in_fresh_process(value=None):
    env = os.environ.copy()
    if value is None:
        env.pop("TRACEWEAVE_TELEMETRY", None)
    else:
        env["TRACEWEAVE_TELEMETRY"] = value
    output = subprocess.check_output(
        [sys.executable, "-c", "import config; print(config.TELEMETRY_ENABLED)"],
        cwd=config.REPO_ROOT,
        env=env,
        text=True,
    )
    return output.strip()


def test_telemetry_defaults_off_and_can_be_enabled_explicitly():
    assert _telemetry_enabled_in_fresh_process() == "False"
    assert _telemetry_enabled_in_fresh_process("1") == "True"


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


def test_record_call_error_code_written_on_failure_only(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call("get_signal_at_time", {}, result_bytes=80, ok=False,
                    error_code="KeyError")
    mod.record_call("get_signal_at_time", {}, result_bytes=120, ok=True)

    failed, succeeded = [json.loads(l) for l in log.read_text().splitlines()]
    assert failed["error_code"] == "KeyError"
    # Success lines stay slim: no error_code key at all.
    assert "error_code" not in succeeded


def test_record_call_diagnostics_are_strictly_whitelisted(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call(
        "sweep_handshakes",
        {"wave_path": "/secret/design.fsdb"},
        result_bytes=10,
        ok=False,
        diagnostics={
            "sweep_phase": "discover_ahb",
            "search_count": 7,
            "search_total_ms": 123.4,
            "signal_path": "top.secret",
            "scope": "top.customer_block",
            "keyword": "customer_signal",
            "wave_lock_wait_ms": "top.secret_wait",
            "sweep_interfaces_planned": 64,
            "sweep_interfaces_completed": 3,
            "sweep_inspect_total_ms": 456.7,
            "sweep_clock_read_count": 3,
            "sweep_clock_read_total_ms": 300.0,
            "sweep_signal_read_count": 12,
            "sweep_signal_read_total_ms": 150.0,
            "sweep_clock_reuse_hits": 61,
            "sweep_signal_reuse_hits": 4,
            "sweep_native_group_count": 35,
            "sweep_native_load_total_ms": 8000.0,
            "sweep_native_group_load_total_ms": 7000.0,
            "sweep_native_standalone_load_total_ms": 1000.0,
            "sweep_native_fallback_signal_total": 23,
            "sweep_native_transition_count": 1234,
            "sweep_sample_lookup_total_ms": 600.0,
            "sweep_sample_materialize_total_ms": 300.0,
            "sweep_protocol_scan_total_ms": 200.0,
            "sweep_group_pack_count": 12,
            "sweep_group_pack_clock_total": 34,
            "sweep_group_chunk_count": 1,
            "sweep_rss_peak_kib": 456789,
            "sweep_result_bytes": 9876,
            "native_group_path": "top.secret",
        },
    )

    rec = json.loads(log.read_text())
    assert rec["diagnostics"] == {
        "sweep_phase": "discover_ahb",
        "search_count": 7,
        "search_total_ms": 123.4,
        "sweep_interfaces_planned": 64,
        "sweep_interfaces_completed": 3,
        "sweep_inspect_total_ms": 456.7,
        "sweep_clock_read_count": 3,
        "sweep_clock_read_total_ms": 300.0,
        "sweep_signal_read_count": 12,
        "sweep_signal_read_total_ms": 150.0,
        "sweep_clock_reuse_hits": 61,
        "sweep_signal_reuse_hits": 4,
        "sweep_native_group_count": 35,
        "sweep_native_load_total_ms": 8000.0,
        "sweep_native_group_load_total_ms": 7000.0,
        "sweep_native_standalone_load_total_ms": 1000.0,
        "sweep_native_fallback_signal_total": 23,
        "sweep_native_transition_count": 1234,
        "sweep_sample_lookup_total_ms": 600.0,
        "sweep_sample_materialize_total_ms": 300.0,
        "sweep_protocol_scan_total_ms": 200.0,
        "sweep_group_pack_count": 12,
        "sweep_group_pack_clock_total": 34,
        "sweep_group_chunk_count": 1,
        "sweep_rss_peak_kib": 456789,
        "sweep_result_bytes": 9876,
    }
    assert "/secret/design.fsdb" not in log.read_text()
    assert "top.secret" not in log.read_text()
    assert "top.secret_wait" not in log.read_text()


def test_record_call_rejects_non_fixed_phase_label(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call(
        "sweep_handshakes", {}, result_bytes=1, ok=False,
        diagnostics={"sweep_phase": "top.customer_secret"},
    )

    assert "diagnostics" not in json.loads(log.read_text())


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


def test_aggregate_counts_error_codes_for_failures_only():
    records = [
        {"session_id": "s1", "tool": "explain_signal_driver", "ok": False,
         "blocked": True, "error_code": "missing_prerequisite", "result_bytes": 300},
        {"session_id": "s1", "tool": "explain_signal_driver", "ok": False,
         "blocked": True, "error_code": "missing_prerequisite", "result_bytes": 300},
        {"session_id": "s1", "tool": "explain_signal_driver", "ok": True, "result_bytes": 900},
        # pre-error_code record: bucketed as "(unrecorded)", never dropped
        {"session_id": "s1", "tool": "parse_sim_log", "ok": False, "result_bytes": 200},
    ]
    report = ut.aggregate(records)

    esd = report["per_tool"]["explain_signal_driver"]
    assert esd["error_codes"] == {"missing_prerequisite": 2}
    assert esd["blocked"] == 2
    assert report["per_tool"]["parse_sim_log"]["error_codes"] == {"(unrecorded)": 1}
