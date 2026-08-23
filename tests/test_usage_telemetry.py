"""Tests for src/usage_telemetry.py — the passive usage telemetry recorder
and the pure aggregation function backing scripts/telemetry_report.py."""

import importlib
import json
import os
import stat
import subprocess
import sys

import config
from src import operation_metrics
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
    mod.record_call(
        "period",
        {"signal_path": "/tb/clk", "edge": "posedge"},
        result_bytes=120,
        ok=True,
        latency_ms=4.2,
        case="cc28",
    )

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
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_record_call_tightens_existing_telemetry_permissions(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    log.parent.mkdir(mode=0o755)
    log.write_text("", encoding="utf-8")
    log.chmod(0o644)
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call("period", {}, result_bytes=10, ok=True)

    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_record_call_refuses_symlink_telemetry_directory(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    telemetry = tmp_path / "telemetry"
    telemetry.symlink_to(target, target_is_directory=True)
    log = telemetry / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call("period", {}, result_bytes=10, ok=True)

    assert not (target / "usage.jsonl").exists()


def test_record_call_error_code_written_on_failure_only(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call(
        "get_signal_at_time", {}, result_bytes=80, ok=False, error_code="KeyError"
    )
    mod.record_call("get_signal_at_time", {}, result_bytes=120, ok=True)

    failed, succeeded = [json.loads(line) for line in log.read_text().splitlines()]
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
        "sweep_handshakes",
        {},
        result_bytes=1,
        ok=False,
        diagnostics={"sweep_phase": "top.customer_secret"},
    )

    assert "diagnostics" not in json.loads(log.read_text())


def test_source_graph_diagnostics_persist_only_numeric_and_fixed_labels(
    tmp_path, monkeypatch
):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call(
        "explain_signal_driver",
        {"signal_path": "top.secret", "compile_log": "/private/compile.log"},
        result_bytes=100,
        ok=True,
        diagnostics={
            "source_graph_phase": "complete",
            "source_graph_cache_tier": "disk",
            "source_graph_disk_validation_outcome": "hit",
            "source_graph_adapter_ms": 7.5,
            "source_graph_disk_lookup_ms": 3.25,
            "source_graph_disk_hit_count": 1,
            "source_graph_disk_miss_count": 0,
            "source_graph_disk_build_skip_count": 1,
            "source_graph_frontend_launch_count": 0,
            "source_graph_semantic_session_hit_count": 2,
            "source_graph_semantic_session_miss_count": 1,
            "source_graph_semantic_session_restart_count": 0,
            "source_graph_semantic_session_eviction_count": 1,
            "source_graph_disk_bytes_read": 8192,
            "source_graph_disk_entry_count": 1,
            "source_graph_disk_bytes": 12288,
            "source_graph_artifact_digest": "a" * 64,
            "source_graph_scope": "top.customer",
            "source_graph_cache_root": "/private/cache",
            "source_graph_disk_bytes_written": "/private/entry",
        },
    )

    text = log.read_text()
    rec = json.loads(text)
    assert rec["diagnostics"] == {
        "source_graph_phase": "complete",
        "source_graph_cache_tier": "disk",
        "source_graph_disk_validation_outcome": "hit",
        "source_graph_adapter_ms": 7.5,
        "source_graph_disk_lookup_ms": 3.25,
        "source_graph_disk_hit_count": 1,
        "source_graph_disk_miss_count": 0,
        "source_graph_disk_build_skip_count": 1,
        "source_graph_frontend_launch_count": 0,
        "source_graph_semantic_session_hit_count": 2,
        "source_graph_semantic_session_miss_count": 1,
        "source_graph_semantic_session_restart_count": 0,
        "source_graph_semantic_session_eviction_count": 1,
        "source_graph_disk_bytes_read": 8192,
        "source_graph_disk_entry_count": 1,
        "source_graph_disk_bytes": 12288,
    }
    assert "top.secret" not in text
    assert "top.customer" not in text
    assert "/private" not in text
    assert "a" * 64 not in text


def test_source_graph_persistent_allowlist_matches_operation_metrics_contract():
    operation_fields = {
        field
        for field in operation_metrics._PUBLIC_FIELDS
        if field.startswith("source_graph_")
    }
    persistent_fields = {
        field for field in ut._DIAGNOSTIC_WHITELIST if field.startswith("source_graph_")
    }
    assert persistent_fields == operation_fields
    assert (
        ut._DIAGNOSTIC_FIXED_LABELS["source_graph_phase"]
        == operation_metrics._SOURCE_GRAPH_PHASES
    )
    assert (
        ut._DIAGNOSTIC_FIXED_LABELS["source_graph_cache_tier"]
        == operation_metrics._SOURCE_GRAPH_CACHE_TIERS
    )
    assert (
        ut._DIAGNOSTIC_FIXED_LABELS["source_graph_disk_validation_outcome"]
        == operation_metrics._SOURCE_GRAPH_DISK_OUTCOMES
    )


def test_source_graph_handoff_tier_is_persisted_and_aggregated(
    tmp_path, monkeypatch
):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call(
        "find_signal_loads",
        {"signal_path": "top.secret"},
        result_bytes=100,
        ok=True,
        diagnostics={
            "source_graph_phase": "complete",
            "source_graph_cache_tier": "handoff",
            "source_graph_actual_build_count": 0,
        },
    )

    record = json.loads(log.read_text())
    assert record["diagnostics"] == {
        "source_graph_phase": "complete",
        "source_graph_cache_tier": "handoff",
        "source_graph_actual_build_count": 0,
    }
    assert "top.secret" not in log.read_text()
    source_graph = mod.aggregate([record])["source_graph"]
    assert source_graph["cache_tiers"]["handoff"]["calls"] == 1
    assert source_graph["by_tool"]["find_signal_loads"]["cache_tiers"] == {
        "memory": 0,
        "disk": 0,
        "build": 0,
        "handoff": 1,
    }


def test_source_graph_non_fixed_labels_are_not_persisted_or_aggregated(
    tmp_path, monkeypatch
):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    mod = _reset_module()

    mod.record_call(
        "explain_signal_driver",
        {},
        result_bytes=1,
        ok=True,
        diagnostics={
            "source_graph_phase": "top.secret",
            "source_graph_cache_tier": "/private/cache",
            "source_graph_disk_validation_outcome": "customer_entry",
            "source_graph_disk_hit_count": True,
            "source_graph_disk_lookup_ms": -1,
            "source_graph_build_ms": float("nan"),
        },
    )

    rec = json.loads(log.read_text())
    assert "diagnostics" not in rec
    assert mod.aggregate([rec])["source_graph"]["calls_with_metrics"] == 0


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
    s2 = mod.note_session("case-B")  # new case mints a new session

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
        {
            "session_id": "s1",
            "tool": "explain_signal_driver",
            "ok": False,
            "blocked": True,
            "error_code": "missing_prerequisite",
            "result_bytes": 300,
        },
        {
            "session_id": "s1",
            "tool": "explain_signal_driver",
            "ok": False,
            "blocked": True,
            "error_code": "missing_prerequisite",
            "result_bytes": 300,
        },
        {
            "session_id": "s1",
            "tool": "explain_signal_driver",
            "ok": True,
            "result_bytes": 900,
        },
        # pre-error_code record: bucketed as "(unrecorded)", never dropped
        {"session_id": "s1", "tool": "parse_sim_log", "ok": False, "result_bytes": 200},
    ]
    report = ut.aggregate(records)

    esd = report["per_tool"]["explain_signal_driver"]
    assert esd["error_codes"] == {"missing_prerequisite": 2}
    assert esd["blocked"] == 2
    assert report["per_tool"]["parse_sim_log"]["error_codes"] == {"(unrecorded)": 1}


def test_aggregate_source_graph_operational_cache_metrics():
    records = [
        {
            "session_id": "s1",
            "tool": "explain_signal_driver",
            "ok": True,
            "result_bytes": 500,
            "latency_ms": 220.0,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "build",
                "source_graph_disk_validation_outcome": "not_found",
                "source_graph_actual_build_count": 1,
                "source_graph_frontend_launch_count": 1,
                "source_graph_semantic_session_miss_count": 1,
                "source_graph_disk_lookup_ms": 2.0,
                "source_graph_disk_publish_ms": 4.0,
                "source_graph_disk_miss_count": 1,
                "source_graph_disk_bytes_written": 140,
                "source_graph_disk_entry_count": 1,
                "source_graph_disk_bytes": 140,
            },
        },
        {
            "session_id": "s2",
            "tool": "explain_signal_driver",
            "ok": True,
            "result_bytes": 500,
            "latency_ms": 30.0,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "disk",
                "source_graph_disk_validation_outcome": "hit",
                "source_graph_actual_build_count": 0,
                "source_graph_frontend_launch_count": 0,
                "source_graph_semantic_session_hit_count": 1,
                "source_graph_disk_lookup_ms": 12.0,
                "source_graph_disk_validate_ms": 10.0,
                "source_graph_disk_hit_count": 1,
                "source_graph_disk_build_skip_count": 1,
                "source_graph_disk_bytes_read": 140,
                "source_graph_disk_entry_count": 1,
                "source_graph_disk_bytes": 140,
            },
        },
        {
            "session_id": "s2",
            "tool": "find_signal_loads",
            "ok": True,
            "result_bytes": 400,
            "latency_ms": 2.0,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "memory",
                "source_graph_disk_validation_outcome": "not_checked",
                "source_graph_actual_build_count": 0,
                "source_graph_frontend_launch_count": 0,
            },
        },
        {
            "session_id": "s3",
            "tool": "trace_x_source",
            "ok": True,
            "result_bytes": 800,
            "latency_ms": 240.0,
            "diagnostics": {
                "source_graph_phase": "complete",
                "source_graph_cache_tier": "build",
                "source_graph_disk_validation_outcome": "ir_digest_mismatch",
                "source_graph_actual_build_count": 1,
                "source_graph_frontend_launch_count": 1,
                "source_graph_disk_lookup_ms": 3.0,
                "source_graph_disk_miss_count": 1,
                "source_graph_disk_corrupt_count": 1,
                "source_graph_disk_bytes_written": 150,
                "source_graph_disk_entry_count": 1,
                "source_graph_disk_bytes": 150,
                "source_graph_trace_query_count": 2,
                "source_graph_trace_artifact_attempt_count": 1,
                "source_graph_trace_restart_count": 1,
            },
        },
    ]

    source_graph = ut.aggregate(records)["source_graph"]
    assert source_graph["calls_with_metrics"] == 4
    assert source_graph["sessions_with_metrics"] == 3
    assert source_graph["sessions_with_disk_hit"] == 1
    assert source_graph["disk_hit_session_presence"] == 0.333333
    frequency = source_graph["query_frequency"]
    assert frequency["calls_per_session"]["min"] == 1
    assert frequency["calls_per_session"]["max"] == 2
    assert frequency["sessions_with_multiple_calls"] == 1
    assert frequency["multiple_call_session_presence"] == 0.333333
    assert frequency["timestamp_call_coverage"] == 0.0
    assert frequency["adjacent_call_pairs"] == 1
    assert frequency["timestamped_adjacent_call_pairs"] == 0
    assert frequency["pairs_within_reuse_window"] == 0
    assert source_graph["cache_tiers"]["memory"]["calls"] == 1
    assert source_graph["cache_tiers"]["disk"]["calls"] == 1
    assert source_graph["cache_tiers"]["build"]["calls"] == 2
    assert source_graph["cache_tiers"]["disk"]["call_latency_ms"]["p95"] == 30

    disk = source_graph["disk"]
    assert disk == {
        "lookup_count": 3,
        "hit_count": 1,
        "miss_count": 2,
        "corrupt_count": 1,
        "exact_hit_rate": 0.333333,
        "build_skip_count": 1,
        "bytes_read": 140,
        "bytes_written": 290,
        "entry_count_max": 1,
        "bytes_max": 150,
        "eviction_count": 0,
    }
    assert source_graph["execution"]["actual_build_count"] == 2
    assert source_graph["execution"]["frontend_launch_count"] == 2
    assert source_graph["execution"]["semantic_session_hit_count"] == 1
    assert source_graph["execution"]["semantic_session_miss_count"] == 1
    assert source_graph["execution"]["semantic_session_restart_count"] == 0
    assert source_graph["execution"]["semantic_session_eviction_count"] == 0
    assert source_graph["timings_ms"]["disk_lookup"]["n"] == 3
    assert source_graph["trace"] == {
        "query_count": 2,
        "artifact_attempt_count": 1,
        "scope_expansion_count": 0,
        "restart_count": 1,
    }

    driver = source_graph["by_tool"]["explain_signal_driver"]
    assert driver["cache_tiers"] == {
        "memory": 0,
        "disk": 1,
        "build": 1,
        "handoff": 0,
    }
    assert driver["disk_lookup_count"] == 2
    assert driver["disk_exact_hit_rate"] == 0.5
    assert source_graph["by_tool"]["find_signal_loads"]["disk_lookup_count"] == 0


def test_telemetry_report_renders_source_graph_operational_section():
    from scripts.telemetry_report import render

    report = ut.aggregate(
        [
            {
                "session_id": "s1",
                "tool": "explain_signal_driver",
                "ok": True,
                "result_bytes": 500,
                "latency_ms": 25.0,
                "diagnostics": {
                    "source_graph_phase": "complete",
                    "source_graph_cache_tier": "disk",
                    "source_graph_disk_validation_outcome": "hit",
                    "source_graph_disk_hit_count": 1,
                    "source_graph_disk_build_skip_count": 1,
                },
            }
        ]
    )

    text = render(report)
    assert "Source Graph disk cache — operational telemetry" in text
    assert "calls per session" in text
    assert "<=60s reuse upper" in text
    assert "memory=0  disk=1  build=0  handoff=0" in text
    assert "hit=1  miss=0  corrupt=0  hit-rate=100.0%" in text
    assert "semantic session" in text
    assert "explain_signal_driver" in text


def test_source_graph_query_frequency_counts_only_timestamped_adjacent_pairs():
    def record(session_id: str, timestamp: str | None) -> dict:
        value = {
            "session_id": session_id,
            "tool": "explain_signal_driver",
            "ok": True,
            "result_bytes": 1,
            "diagnostics": {"source_graph_phase": "complete"},
        }
        if timestamp is not None:
            value["ts"] = timestamp
        return value

    records = [
        record("within", "2026-08-23T10:00:00.000+00:00"),
        record("within", "2026-08-23T10:00:30.000+00:00"),
        record("outside", "2026-08-23T10:00:00.000+00:00"),
        record("outside", "2026-08-23T10:01:01.000+00:00"),
        record("missing", "2026-08-23T10:00:00.000+00:00"),
        record("missing", None),
        record("single", "2026-08-23T10:00:00.000+00:00"),
    ]

    frequency = ut.aggregate(records)["source_graph"]["query_frequency"]

    assert frequency["calls_per_session"]["n"] == 4
    assert frequency["calls_per_session"]["max"] == 2
    assert frequency["sessions_with_multiple_calls"] == 3
    assert frequency["adjacent_call_pairs"] == 3
    assert frequency["timestamped_adjacent_call_pairs"] == 2
    assert frequency["timestamp_pair_coverage"] == 0.666667
    assert frequency["pairs_within_reuse_window"] == 1
    assert frequency["within_window_pair_rate"] == 0.5
    assert frequency["sessions_with_reuse_opportunity"] == 1
    assert frequency["reuse_opportunity_session_presence"] == 0.25
    assert frequency["inter_call_gap_ms"]["min"] == 30_000
    assert frequency["inter_call_gap_ms"]["max"] == 61_000
    assert "2026-08-23" not in json.dumps(frequency)


def test_source_graph_query_frequency_does_not_merge_missing_session_ids():
    records = [
        {
            "tool": "explain_signal_driver",
            "ok": True,
            "result_bytes": 1,
            "ts": f"2026-08-23T10:00:0{index}.000+00:00",
            "diagnostics": {"source_graph_phase": "complete"},
        }
        for index in range(2)
    ]

    source_graph = ut.aggregate(records)["source_graph"]

    assert source_graph["sessions_with_metrics"] == 2
    assert source_graph["query_frequency"]["sessions_with_multiple_calls"] == 0
    assert source_graph["query_frequency"]["adjacent_call_pairs"] == 0
