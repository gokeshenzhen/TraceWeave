"""Passive, local-only usage telemetry for TraceWeave MCP tools.

Why this exists
---------------
The auto-debug v2 retrospective concluded that the real bottleneck is
orchestration (call count / tokens), not raw analysis power, and that we should
*stop building tools for "might be useful" and demand real usage numbers*. This
module is the measuring instrument: it
records one line per MCP tool call so we can later answer, with data:

  * how often the shipped primitives (cursor/period/diff_first_divergence) are
    actually called on real workloads, and
  * in what fraction of debug *sessions* each one shows up at least once
    (the "does it earn its tool-surface slot" number), and
  * whether opt-in Source Graph disk reuse produces enough exact hits and
    frontend build skips to justify its miss, validation, and storage cost,
    and
  * whether Source Graph calls actually repeat inside one case and the default
    60-second semantic-session reuse window often enough to justify retaining
    a frontend process.

Design constraints
------------------
* **Never breaks the call path.** Every public function swallows its own
  exceptions; a telemetry failure must not surface to the user or abort a tool.
* **Local-only.** Appends to a JSONL file under the cache dir. No network.
* **Low-signal payload.** We log argument *keys* and a small whitelist of
  scalar decision flags, never argument values or paths (noise + privacy).
  Failed calls additionally carry a classification `error_code` (a code or
  exception class name, never the message — messages can embed paths).
  Source Graph adds only numeric aggregates and fixed phase/tier/validation
  labels through an independent second allowlist; no artifact fingerprint,
  cache/source/wave path, signal, scope, value, or diagnostic can enter JSONL.
* **Session = a get_sim_paths anchor.** The workflow always starts at
  get_sim_paths, so a new case identity opens a new logical session. The server
  calls `note_session()` from its get_sim_paths handler.

The aggregation half (`aggregate`) is a pure function over already-parsed
records so it can be unit-tested and reused by scripts/telemetry_report.py.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config

# Argument names worth keeping as decision-relevant flags. Only captured when
# the value is a simple scalar; everything else (paths, values) is dropped.
_FLAG_WHITELIST = (
    "profile",
    "edge",
    "detail_level",
    "mode",
    "return_mode",
    "simulator",
    "category",
    "direction",
)

_SCALAR_TYPES = (str, int, float, bool)

# Privacy-safe operation diagnostics. Values are timings/counts plus one fixed
# phase label; paths, scopes, signal names and search keywords are never accepted.
_DIAGNOSTIC_WHITELIST = {
    "wave_lock_wait_ms",
    "preemption_to_cancel_ms",
    "sweep_phase",
    "discover_valid_ready_ms",
    "discover_ahb_ms",
    "search_count",
    "search_total_ms",
    "search_max_ms",
    "sweep_total_ms",
    "sweep_interfaces_planned",
    "sweep_interfaces_attempted",
    "sweep_interfaces_completed",
    "sweep_unique_clocks",
    "sweep_unique_signals",
    "sweep_inspect_total_ms",
    "sweep_inspect_max_ms",
    "sweep_transition_truncated_interfaces",
    "sweep_clock_read_count",
    "sweep_clock_read_total_ms",
    "sweep_clock_read_max_ms",
    "sweep_signal_read_count",
    "sweep_signal_read_total_ms",
    "sweep_signal_read_max_ms",
    "sweep_edge_extract_total_ms",
    "sweep_value_sample_total_ms",
    "sweep_clock_reuse_hits",
    "sweep_signal_reuse_hits",
    "sweep_native_group_count",
    "sweep_native_group_signal_total",
    "sweep_native_group_signal_max",
    "sweep_native_group_load_call_count",
    "sweep_native_group_load_total_ms",
    "sweep_native_group_load_max_ms",
    "sweep_native_group_fallback_count",
    "sweep_native_group_unsupported_count",
    "sweep_native_group_oversized_count",
    "sweep_native_group_begin_error_count",
    "sweep_native_profiled_read_count",
    "sweep_native_standalone_load_call_count",
    "sweep_native_standalone_load_total_ms",
    "sweep_native_standalone_load_max_ms",
    "sweep_native_fallback_signal_total",
    "sweep_native_lookup_total_ms",
    "sweep_native_add_signal_total_ms",
    "sweep_native_load_total_ms",
    "sweep_native_load_max_ms",
    "sweep_native_create_handle_total_ms",
    "sweep_native_seek_total_ms",
    "sweep_native_traverse_format_total_ms",
    "sweep_native_free_handle_total_ms",
    "sweep_native_unload_total_ms",
    "sweep_native_transition_count",
    "sweep_native_output_bytes",
    "sweep_native_truncated_calls",
    "sweep_rss_start_kib",
    "sweep_rss_peak_kib",
    "sweep_rss_end_kib",
    "sweep_rss_peak_delta_kib",
    "sweep_cached_signal_results_peak",
    "sweep_cached_transition_count_peak",
    "sweep_sample_edges_total",
    "sweep_sample_edges_max",
    "sweep_sample_values_total",
    "sweep_sample_values_max",
    "sweep_path_resolution_total_ms",
    "sweep_sample_lookup_total_ms",
    "sweep_sample_materialize_total_ms",
    "sweep_protocol_scan_total_ms",
    "sweep_write_data_scan_total_ms",
    "sweep_group_pack_count",
    "sweep_group_pack_clock_total",
    "sweep_group_chunk_count",
    "sweep_result_build_ms",
    "sweep_result_serialize_ms",
    "sweep_result_bytes",
    "source_graph_phase",
    "source_graph_adapter_ms",
    "source_graph_prepare_total_ms",
    "source_graph_admission_wait_ms",
    "source_graph_build_ms",
    "source_graph_load_ms",
    "source_graph_query_ms",
    "source_graph_actual_build_count",
    "source_graph_coalesced_waiter_count",
    "source_graph_cancel_to_exit_ms",
    "source_graph_worker_cpu_ms",
    "source_graph_rss_start_kib",
    "source_graph_rss_peak_kib",
    "source_graph_rss_end_kib",
    "source_graph_ir_bytes",
    "source_graph_cache_bytes",
    "source_graph_cache_entry_count",
    "source_graph_cache_peak_entry_count",
    "source_graph_cache_peak_bytes",
    "source_graph_cache_eviction_count",
    "source_graph_cache_oversize_bypass_count",
    "source_graph_cache_tier",
    "source_graph_disk_validation_outcome",
    "source_graph_frontend_launch_count",
    "source_graph_semantic_session_hit_count",
    "source_graph_semantic_session_miss_count",
    "source_graph_semantic_session_restart_count",
    "source_graph_semantic_session_eviction_count",
    "source_graph_disk_lookup_ms",
    "source_graph_disk_read_ms",
    "source_graph_disk_validate_ms",
    "source_graph_disk_publish_ms",
    "source_graph_disk_write_ms",
    "source_graph_disk_eviction_ms",
    "source_graph_disk_hit_count",
    "source_graph_disk_miss_count",
    "source_graph_disk_corrupt_count",
    "source_graph_disk_build_skip_count",
    "source_graph_disk_bytes_read",
    "source_graph_disk_bytes_written",
    "source_graph_disk_entry_count",
    "source_graph_disk_bytes",
    "source_graph_disk_eviction_count",
    "source_graph_trace_query_count",
    "source_graph_trace_artifact_attempt_count",
    "source_graph_trace_scope_expansion_count",
    "source_graph_trace_restart_count",
}
_DIAGNOSTIC_FIXED_LABELS = {
    "sweep_phase": {
        "discover_valid_ready",
        "discover_ahb",
        "inspect_interfaces",
        "complete",
    },
    "source_graph_phase": {
        "adapter",
        "prepare",
        "query",
        "fallback",
        "complete",
        "cancelled",
    },
    "source_graph_cache_tier": {"memory", "disk", "build", "handoff"},
    "source_graph_disk_validation_outcome": {
        "disabled",
        "not_checked",
        "hit",
        "not_found",
        "identity_not_reusable",
        "unsafe_namespace",
        "unsafe_entry",
        "manifest_missing",
        "manifest_too_large",
        "manifest_invalid",
        "unknown_format",
        "incomplete_entry",
        "artifact_key_mismatch",
        "artifact_identity_mismatch",
        "build_semantics_mismatch",
        "scope_mismatch",
        "snapshot_mismatch",
        "version_mismatch",
        "coverage_receipt_mismatch",
        "ir_missing",
        "ir_too_large",
        "ir_size_mismatch",
        "ir_digest_mismatch",
        "ir_schema_mismatch",
        "ir_identity_mismatch",
        "io_error",
    },
}
_DIAGNOSTIC_NUMERIC_FIELDS = _DIAGNOSTIC_WHITELIST - set(_DIAGNOSTIC_FIXED_LABELS)

_SOURCE_GRAPH_TIMING_FIELDS = {
    "adapter": "source_graph_adapter_ms",
    "prepare_total": "source_graph_prepare_total_ms",
    "admission_wait": "source_graph_admission_wait_ms",
    "build": "source_graph_build_ms",
    "load": "source_graph_load_ms",
    "query": "source_graph_query_ms",
    "cancel_to_exit": "source_graph_cancel_to_exit_ms",
    "worker_cpu": "source_graph_worker_cpu_ms",
    "disk_lookup": "source_graph_disk_lookup_ms",
    "disk_read": "source_graph_disk_read_ms",
    "disk_validate": "source_graph_disk_validate_ms",
    "disk_publish": "source_graph_disk_publish_ms",
    "disk_write": "source_graph_disk_write_ms",
    "disk_eviction": "source_graph_disk_eviction_ms",
}
_SOURCE_GRAPH_TIERS = ("memory", "disk", "build", "handoff")
_SOURCE_GRAPH_REUSE_WINDOW_SECONDS = 60.0
_SOURCE_GRAPH_SUM_FIELDS = {
    "actual_build_count": "source_graph_actual_build_count",
    "frontend_launch_count": "source_graph_frontend_launch_count",
    "semantic_session_hit_count": "source_graph_semantic_session_hit_count",
    "semantic_session_miss_count": "source_graph_semantic_session_miss_count",
    "semantic_session_restart_count": (
        "source_graph_semantic_session_restart_count"
    ),
    "semantic_session_eviction_count": (
        "source_graph_semantic_session_eviction_count"
    ),
    "coalesced_waiter_count": "source_graph_coalesced_waiter_count",
    "cache_eviction_count": "source_graph_cache_eviction_count",
    "cache_oversize_bypass_count": "source_graph_cache_oversize_bypass_count",
    "disk_hit_count": "source_graph_disk_hit_count",
    "disk_miss_count": "source_graph_disk_miss_count",
    "disk_corrupt_count": "source_graph_disk_corrupt_count",
    "disk_build_skip_count": "source_graph_disk_build_skip_count",
    "disk_bytes_read": "source_graph_disk_bytes_read",
    "disk_bytes_written": "source_graph_disk_bytes_written",
    "disk_eviction_count": "source_graph_disk_eviction_count",
    "trace_query_count": "source_graph_trace_query_count",
    "trace_artifact_attempt_count": ("source_graph_trace_artifact_attempt_count"),
    "trace_scope_expansion_count": "source_graph_trace_scope_expansion_count",
    "trace_restart_count": "source_graph_trace_restart_count",
}
_SOURCE_GRAPH_MAX_FIELDS = {
    "rss_peak_kib": "source_graph_rss_peak_kib",
    "ir_bytes": "source_graph_ir_bytes",
    "memory_cache_entry_count": "source_graph_cache_entry_count",
    "memory_cache_peak_entry_count": "source_graph_cache_peak_entry_count",
    "memory_cache_bytes": "source_graph_cache_bytes",
    "memory_cache_peak_bytes": "source_graph_cache_peak_bytes",
    "disk_entry_count": "source_graph_disk_entry_count",
    "disk_bytes": "source_graph_disk_bytes",
}

# Tools grouped under a logical "feature" for reporting. Anything not listed
# reports under its own name.
PRIMITIVE_GROUPS: dict[str, str] = {
    "cursor_set": "cursor",
    "cursor_list": "cursor",
    "cursor_delete": "cursor",
}

# The auto-debug v2 primitives we specifically want frequency numbers for.
TRACKED_FEATURES = ("cursor", "period", "diff_first_divergence")

_lock = threading.Lock()
_session_id: str | None = None
_session_identity: str | None = None


def _new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def note_session(identity: Any) -> str:
    """Anchor a logical session to a get_sim_paths identity.

    Mints a fresh session id whenever the identity changes (a new case) or when
    no session exists yet; repeated discovery of the same case keeps the id so a
    single debug is not split. `identity` may be any value — it is stringified
    for comparison. Returns the current session id. Best-effort, never raises.
    """
    global _session_id, _session_identity
    try:
        key = None if identity is None else str(identity)
        with _lock:
            if _session_id is None or key != _session_identity:
                _session_id = _new_session_id()
                _session_identity = key
            return _session_id
    except Exception:
        return _session_id or ""


def current_session_id() -> str | None:
    return _session_id


def _extract_flags(args: dict) -> dict:
    flags: dict[str, Any] = {}
    for name in _FLAG_WHITELIST:
        if name in args:
            value = args[name]
            if isinstance(value, _SCALAR_TYPES):
                flags[name] = value
    return flags


def _sanitize_diagnostics(diagnostics: dict | None) -> dict[str, int | float | str]:
    """Apply the recorder's independent fixed-label/numeric allowlist.

    ``operation_metrics.snapshot`` already filters the live values.  Repeating
    the check here is intentional defense in depth: callers and loaded JSONL
    records cannot smuggle a path, scope, digest, or diagnostic string into the
    persistent telemetry stream or its operational report.
    """

    safe: dict[str, int | float | str] = {}
    for raw_key, value in (diagnostics or {}).items():
        key = str(raw_key)
        if key not in _DIAGNOSTIC_WHITELIST:
            continue
        fixed_values = _DIAGNOSTIC_FIXED_LABELS.get(key)
        if fixed_values is not None:
            if isinstance(value, str) and value in fixed_values:
                safe[key] = value
            continue
        if key in _DIAGNOSTIC_NUMERIC_FIELDS:
            number = _nonnegative_number(value)
            if number is not None:
                safe[key] = number
    return safe


def record_call(
    tool: str,
    args: dict | None,
    *,
    result_bytes: int,
    ok: bool,
    blocked: bool = False,
    error_code: str | None = None,
    latency_ms: float | None = None,
    case: str | None = None,
    diagnostics: dict | None = None,
) -> None:
    """Append one JSONL line describing a completed tool call.

    Best-effort: any failure (disk, serialization) is swallowed so telemetry can
    never break a tool call. No-op when TRACEWEAVE_TELEMETRY is disabled.
    """
    if not getattr(config, "TELEMETRY_ENABLED", False):
        return
    try:
        args = args or {}
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": _session_id,
            "case": case,
            "tool": tool,
            "arg_keys": sorted(str(k) for k in args.keys()),
            "flags": _extract_flags(args),
            "ok": bool(ok),
            "blocked": bool(blocked),
            "result_bytes": int(result_bytes),
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        }
        # A classification code, never a message (messages can embed paths).
        # Omitted on success to keep the line slim.
        if error_code is not None:
            record["error_code"] = str(error_code)
        safe_diagnostics = _sanitize_diagnostics(diagnostics)
        if safe_diagnostics:
            record["diagnostics"] = safe_diagnostics
        path = config.telemetry_log_path()
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.is_symlink():
                return
            os.chmod(path.parent, stat.S_IRWXU)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
            try:
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(descriptor, "a", encoding="utf-8") as fh:
                    descriptor = -1
                    fh.write(line + "\n")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    except Exception:
        # Telemetry is strictly best-effort; never let it surface.
        pass


# ── Aggregation (pure, testable) ──────────────────────────────────────────


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = pct / 100.0 * (len(sorted_vals) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_vals) - 1)
    frac = rank - low
    return float(sorted_vals[low] + (sorted_vals[high] - sorted_vals[low]) * frac)


def _dist(values: list[float]) -> dict:
    if not values:
        return {
            "n": 0,
            "min": 0,
            "median": 0,
            "p90": 0,
            "p95": 0,
            "max": 0,
            "total": 0,
        }
    s = sorted(values)
    return {
        "n": len(s),
        "min": s[0],
        "median": _percentile(s, 50),
        "p90": _percentile(s, 90),
        "p95": _percentile(s, 95),
        "max": s[-1],
        "total": sum(s),
    }


def _nonnegative_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not math.isfinite(float(value)):
        return None
    return value


def _rate(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _new_source_graph_tool_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "sessions": set(),
        "cache_tiers": {tier: 0 for tier in _SOURCE_GRAPH_TIERS},
        "tier_latencies": {tier: [] for tier in _SOURCE_GRAPH_TIERS},
        "disk_hit_count": 0,
        "disk_miss_count": 0,
        "disk_corrupt_count": 0,
        "disk_build_skip_count": 0,
        "actual_build_count": 0,
        "frontend_launch_count": 0,
    }


def _timestamp_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _source_graph_operational_report(records: list[dict]) -> dict:
    source_calls = 0
    source_sessions: set[str] = set()
    disk_hit_sessions: set[str] = set()
    phases: dict[str, int] = {}
    validation_outcomes: dict[str, int] = {}
    tier_calls = {tier: 0 for tier in _SOURCE_GRAPH_TIERS}
    tier_sessions = {tier: set() for tier in _SOURCE_GRAPH_TIERS}
    tier_latencies = {tier: [] for tier in _SOURCE_GRAPH_TIERS}
    all_call_latencies: list[float] = []
    timings = {name: [] for name in _SOURCE_GRAPH_TIMING_FIELDS}
    sums: dict[str, int | float] = {name: 0 for name in _SOURCE_GRAPH_SUM_FIELDS}
    maxima: dict[str, int | float] = {name: 0 for name in _SOURCE_GRAPH_MAX_FIELDS}
    per_tool: dict[str, dict[str, Any]] = {}
    session_call_times: dict[str, list[float | None]] = {}
    timestamped_call_count = 0

    for rec in records:
        raw_diagnostics = rec.get("diagnostics")
        diagnostics = _sanitize_diagnostics(
            raw_diagnostics if isinstance(raw_diagnostics, dict) else None
        )
        if not any(key.startswith("source_graph_") for key in diagnostics):
            continue

        source_calls += 1
        raw_session_id = rec.get("session_id")
        sid = (
            str(raw_session_id)
            if raw_session_id
            else f"(unscoped-{source_calls})"
        )
        source_sessions.add(sid)
        timestamp = _timestamp_seconds(rec.get("ts"))
        session_call_times.setdefault(sid, []).append(timestamp)
        if timestamp is not None:
            timestamped_call_count += 1
        tool = str(rec.get("tool") or "(unknown)")
        tool_bucket = per_tool.setdefault(tool, _new_source_graph_tool_bucket())
        tool_bucket["calls"] += 1
        tool_bucket["sessions"].add(sid)

        phase = diagnostics.get("source_graph_phase")
        if isinstance(phase, str):
            phases[phase] = phases.get(phase, 0) + 1
        outcome = diagnostics.get("source_graph_disk_validation_outcome")
        if isinstance(outcome, str):
            validation_outcomes[outcome] = validation_outcomes.get(outcome, 0) + 1

        tier = diagnostics.get("source_graph_cache_tier")
        latency = _nonnegative_number(rec.get("latency_ms"))
        if latency is not None:
            all_call_latencies.append(float(latency))
        if isinstance(tier, str) and tier in tier_calls:
            tier_calls[tier] += 1
            tier_sessions[tier].add(sid)
            tool_bucket["cache_tiers"][tier] += 1
            if latency is not None:
                tier_latencies[tier].append(float(latency))
                tool_bucket["tier_latencies"][tier].append(float(latency))

        for name, field in _SOURCE_GRAPH_TIMING_FIELDS.items():
            value = _nonnegative_number(diagnostics.get(field))
            # Runtime receipts use zero for a stage that was not entered. Keep
            # those placeholders out of latency distributions so memory hits
            # do not dilute disk/build p50 and p95 with non-applicable zeros.
            if value is not None and value > 0:
                timings[name].append(float(value))
        for name, field in _SOURCE_GRAPH_SUM_FIELDS.items():
            value = _nonnegative_number(diagnostics.get(field))
            if value is not None:
                sums[name] += value
        for name, field in _SOURCE_GRAPH_MAX_FIELDS.items():
            value = _nonnegative_number(diagnostics.get(field))
            if value is not None:
                maxima[name] = max(maxima[name], value)

        for name in (
            "disk_hit_count",
            "disk_miss_count",
            "disk_corrupt_count",
            "disk_build_skip_count",
            "actual_build_count",
            "frontend_launch_count",
        ):
            value = _nonnegative_number(diagnostics.get(_SOURCE_GRAPH_SUM_FIELDS[name]))
            if value is not None:
                tool_bucket[name] += value
        record_disk_hits = _nonnegative_number(
            diagnostics.get("source_graph_disk_hit_count")
        )
        if record_disk_hits is not None and record_disk_hits > 0:
            disk_hit_sessions.add(sid)

    disk_lookup_count = sums["disk_hit_count"] + sums["disk_miss_count"]
    calls_per_session = [len(times) for times in session_call_times.values()]
    multi_call_sessions = sum(count >= 2 for count in calls_per_session)
    adjacent_pair_count = sum(max(count - 1, 0) for count in calls_per_session)
    timestamped_pair_count = 0
    within_window_pair_count = 0
    within_window_sessions: set[str] = set()
    inter_call_gap_ms: list[float] = []
    for sid, call_times in session_call_times.items():
        for before, after in zip(call_times, call_times[1:]):
            if before is None or after is None or after < before:
                continue
            timestamped_pair_count += 1
            gap_seconds = after - before
            inter_call_gap_ms.append(gap_seconds * 1000.0)
            if gap_seconds <= _SOURCE_GRAPH_REUSE_WINDOW_SECONDS:
                within_window_pair_count += 1
                within_window_sessions.add(sid)
    tier_report = {
        tier: {
            "calls": tier_calls[tier],
            "sessions": len(tier_sessions[tier]),
            "call_latency_ms": _dist(tier_latencies[tier]),
        }
        for tier in _SOURCE_GRAPH_TIERS
    }
    tool_report = {}
    for tool, bucket in sorted(per_tool.items()):
        lookup_count = bucket["disk_hit_count"] + bucket["disk_miss_count"]
        tool_report[tool] = {
            "calls": bucket["calls"],
            "sessions": len(bucket["sessions"]),
            "cache_tiers": bucket["cache_tiers"],
            "disk_lookup_count": lookup_count,
            "disk_hit_count": bucket["disk_hit_count"],
            "disk_miss_count": bucket["disk_miss_count"],
            "disk_corrupt_count": bucket["disk_corrupt_count"],
            "disk_exact_hit_rate": _rate(bucket["disk_hit_count"], lookup_count),
            "disk_build_skip_count": bucket["disk_build_skip_count"],
            "actual_build_count": bucket["actual_build_count"],
            "frontend_launch_count": bucket["frontend_launch_count"],
            "call_latency_ms_by_tier": {
                tier: _dist(bucket["tier_latencies"][tier])
                for tier in _SOURCE_GRAPH_TIERS
            },
        }

    return {
        "calls_with_metrics": source_calls,
        "sessions_with_metrics": len(source_sessions),
        "sessions_with_disk_hit": len(disk_hit_sessions),
        "disk_hit_session_presence": _rate(
            len(disk_hit_sessions), len(source_sessions)
        ),
        "query_frequency": {
            "calls_per_session": _dist(calls_per_session),
            "sessions_with_multiple_calls": multi_call_sessions,
            "multiple_call_session_presence": _rate(
                multi_call_sessions, len(source_sessions)
            ),
            "timestamped_calls": timestamped_call_count,
            "timestamp_call_coverage": _rate(timestamped_call_count, source_calls),
            "reuse_window_seconds": _SOURCE_GRAPH_REUSE_WINDOW_SECONDS,
            "adjacent_call_pairs": adjacent_pair_count,
            "timestamped_adjacent_call_pairs": timestamped_pair_count,
            "timestamp_pair_coverage": _rate(
                timestamped_pair_count, adjacent_pair_count
            ),
            "pairs_within_reuse_window": within_window_pair_count,
            "within_window_pair_rate": _rate(
                within_window_pair_count, timestamped_pair_count
            ),
            "sessions_with_reuse_opportunity": len(within_window_sessions),
            "reuse_opportunity_session_presence": _rate(
                len(within_window_sessions), len(source_sessions)
            ),
            "inter_call_gap_ms": _dist(inter_call_gap_ms),
        },
        "phases": dict(sorted(phases.items())),
        "validation_outcomes": dict(sorted(validation_outcomes.items())),
        "cache_tiers": tier_report,
        "disk": {
            "lookup_count": disk_lookup_count,
            "hit_count": sums["disk_hit_count"],
            "miss_count": sums["disk_miss_count"],
            "corrupt_count": sums["disk_corrupt_count"],
            "exact_hit_rate": _rate(sums["disk_hit_count"], disk_lookup_count),
            "build_skip_count": sums["disk_build_skip_count"],
            "bytes_read": sums["disk_bytes_read"],
            "bytes_written": sums["disk_bytes_written"],
            "entry_count_max": maxima["disk_entry_count"],
            "bytes_max": maxima["disk_bytes"],
            "eviction_count": sums["disk_eviction_count"],
        },
        "execution": {
            "actual_build_count": sums["actual_build_count"],
            "frontend_launch_count": sums["frontend_launch_count"],
            "semantic_session_hit_count": sums["semantic_session_hit_count"],
            "semantic_session_miss_count": sums["semantic_session_miss_count"],
            "semantic_session_restart_count": sums[
                "semantic_session_restart_count"
            ],
            "semantic_session_eviction_count": sums[
                "semantic_session_eviction_count"
            ],
            "coalesced_waiter_count": sums["coalesced_waiter_count"],
            "memory_cache_eviction_count": sums["cache_eviction_count"],
            "memory_cache_oversize_bypass_count": sums["cache_oversize_bypass_count"],
        },
        "resources_max": maxima,
        "trace": {
            "query_count": sums["trace_query_count"],
            "artifact_attempt_count": sums["trace_artifact_attempt_count"],
            "scope_expansion_count": sums["trace_scope_expansion_count"],
            "restart_count": sums["trace_restart_count"],
        },
        "call_latency_ms": _dist(all_call_latencies),
        "timings_ms": {name: _dist(values) for name, values in timings.items()},
        "by_tool": tool_report,
    }


def feature_of(tool: str) -> str:
    return PRIMITIVE_GROUPS.get(tool, tool)


def aggregate(records: Iterable[dict]) -> dict:
    """Summarize raw telemetry records into a report dict.

    Returns per-tool call counts / ok-rate / session-presence, the call-count
    and result_bytes distributions per session, and a focused block on the
    TRACKED_FEATURES (presence rate = the fraction of sessions that used the
    feature at least once). Records with no session_id are bucketed under a
    synthetic "(none)" session so they are not silently dropped.
    """
    records = list(records)
    sessions: dict[str, dict] = {}
    per_tool: dict[str, dict] = {}
    feature_sessions: dict[str, set] = {}

    for rec in records:
        tool = rec.get("tool")
        if not tool:
            continue
        sid = rec.get("session_id") or "(none)"
        feature = feature_of(tool)

        sess = sessions.setdefault(
            sid, {"calls": 0, "result_bytes": 0, "tools": set(), "features": set()}
        )
        sess["calls"] += 1
        sess["result_bytes"] += int(rec.get("result_bytes") or 0)
        sess["tools"].add(tool)
        sess["features"].add(feature)

        t = per_tool.setdefault(
            tool,
            {
                "calls": 0,
                "ok": 0,
                "blocked": 0,
                "bytes": 0,
                "sessions": set(),
                "error_codes": {},
            },
        )
        t["calls"] += 1
        t["ok"] += 1 if rec.get("ok") else 0
        t["blocked"] += 1 if rec.get("blocked") else 0
        t["bytes"] += int(rec.get("result_bytes") or 0)
        t["sessions"].add(sid)
        if not rec.get("ok"):
            code = rec.get("error_code") or "(unrecorded)"
            t["error_codes"][code] = t["error_codes"].get(code, 0) + 1

        feature_sessions.setdefault(feature, set()).add(sid)

    total_sessions = len(sessions)

    tool_report = {}
    for tool, t in sorted(per_tool.items(), key=lambda kv: (-kv[1]["calls"], kv[0])):
        tool_report[tool] = {
            "calls": t["calls"],
            "ok_rate": round(t["ok"] / t["calls"], 3) if t["calls"] else 0.0,
            "blocked": t["blocked"],
            "sessions": len(t["sessions"]),
            "session_presence": round(len(t["sessions"]) / total_sessions, 3)
            if total_sessions
            else 0.0,
            "total_bytes": t["bytes"],
            "error_codes": dict(
                sorted(t["error_codes"].items(), key=lambda kv: -kv[1])
            ),
        }

    tracked = {}
    for feature in TRACKED_FEATURES:
        used = feature_sessions.get(feature, set())
        calls = sum(per_tool[t]["calls"] for t in per_tool if feature_of(t) == feature)
        tracked[feature] = {
            "calls": calls,
            "sessions_used": len(used),
            "session_presence": round(len(used) / total_sessions, 3)
            if total_sessions
            else 0.0,
        }

    return {
        "total_records": len(records),
        "total_sessions": total_sessions,
        "calls_per_session": _dist([s["calls"] for s in sessions.values()]),
        "result_bytes_per_session": _dist(
            [s["result_bytes"] for s in sessions.values()]
        ),
        "tracked_features": tracked,
        "per_tool": tool_report,
        "source_graph": _source_graph_operational_report(records),
    }


def load_records(path: str | os.PathLike | None = None) -> list[dict]:
    """Read and JSON-parse a usage.jsonl file. Skips malformed lines."""
    log_path = Path(path) if path is not None else config.telemetry_log_path()
    records: list[dict] = []
    if not log_path.exists():
        return records
    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records
