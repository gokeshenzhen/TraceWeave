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
    (the "does it earn its tool-surface slot" number).

Design constraints
------------------
* **Never breaks the call path.** Every public function swallows its own
  exceptions; a telemetry failure must not surface to the user or abort a tool.
* **Local-only.** Appends to a JSONL file under the cache dir. No network.
* **Low-signal payload.** We log argument *keys* and a small whitelist of
  scalar decision flags, never argument values or paths (noise + privacy).
  Failed calls additionally carry a classification `error_code` (a code or
  exception class name, never the message — messages can embed paths).
* **Session = a get_sim_paths anchor.** The workflow always starts at
  get_sim_paths, so a new case identity opens a new logical session. The server
  calls `note_session()` from its get_sim_paths handler.

The aggregation half (`aggregate`) is a pure function over already-parsed
records so it can be unit-tested and reused by scripts/telemetry_report.py.
"""

from __future__ import annotations

import json
import os
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
}
_DIAGNOSTIC_PHASES = {
    "discover_valid_ready", "discover_ahb", "inspect_interfaces", "complete"
}
_DIAGNOSTIC_NUMERIC_FIELDS = _DIAGNOSTIC_WHITELIST - {"sweep_phase"}

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
        safe_diagnostics = {
            str(key): value
            for key, value in (diagnostics or {}).items()
            if key in _DIAGNOSTIC_WHITELIST and isinstance(value, _SCALAR_TYPES)
            and (key != "sweep_phase" or value in _DIAGNOSTIC_PHASES)
            and (
                key not in _DIAGNOSTIC_NUMERIC_FIELDS
                or (not isinstance(value, bool) and isinstance(value, (int, float)))
            )
        }
        if safe_diagnostics:
            record["diagnostics"] = safe_diagnostics
        path = config.telemetry_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
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
        return {"n": 0, "min": 0, "median": 0, "p90": 0, "max": 0, "total": 0}
    s = sorted(values)
    return {
        "n": len(s),
        "min": s[0],
        "median": _percentile(s, 50),
        "p90": _percentile(s, 90),
        "max": s[-1],
        "total": sum(s),
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

        sess = sessions.setdefault(sid, {"calls": 0, "result_bytes": 0, "tools": set(), "features": set()})
        sess["calls"] += 1
        sess["result_bytes"] += int(rec.get("result_bytes") or 0)
        sess["tools"].add(tool)
        sess["features"].add(feature)

        t = per_tool.setdefault(
            tool,
            {"calls": 0, "ok": 0, "blocked": 0, "bytes": 0, "sessions": set(), "error_codes": {}},
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
            "session_presence": round(len(t["sessions"]) / total_sessions, 3) if total_sessions else 0.0,
            "total_bytes": t["bytes"],
            "error_codes": dict(sorted(t["error_codes"].items(), key=lambda kv: -kv[1])),
        }

    tracked = {}
    for feature in TRACKED_FEATURES:
        used = feature_sessions.get(feature, set())
        calls = sum(per_tool[t]["calls"] for t in per_tool if feature_of(t) == feature)
        tracked[feature] = {
            "calls": calls,
            "sessions_used": len(used),
            "session_presence": round(len(used) / total_sessions, 3) if total_sessions else 0.0,
        }

    return {
        "total_records": len(records),
        "total_sessions": total_sessions,
        "calls_per_session": _dist([s["calls"] for s in sessions.values()]),
        "result_bytes_per_session": _dist([s["result_bytes"] for s in sessions.values()]),
        "tracked_features": tracked,
        "per_tool": tool_report,
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
