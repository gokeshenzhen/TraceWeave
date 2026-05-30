#!/usr/bin/env python3
"""
TraceWeave MCP Server
For MCP-compatible debug clients such as Codex and Claude Code.

This server provides waveform-debug workflow tools, including:
- path discovery and session/workflow gating
- compile/sim log parsing and failure normalization
- testbench hierarchy and source/driver correlation
- VCD/FSDB waveform queries and signal search
- failure recommendation, structural risk scanning, and X/Z trace
"""

import asyncio
from collections.abc import Callable
import hashlib
import json
import sys
import os
import time

# Ensure the TraceWeave repo root is on the Python path.
sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from config import (
    AUTO_DOWNGRADE_THRESHOLD,
    CLOCK_DETECT_SAMPLE_PS,
    DEFAULT_DETAIL_LEVEL,
    DEFAULT_EXTRA_TRANSITIONS, DEFAULT_LOG_CONTEXT_AFTER, DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_MAX_EVENTS_PER_GROUP,
    FALLBACK_WAVE_WINDOW_PS,
    MAX_CYCLES_PER_QUERY,
    MAX_WAVE_WINDOW_CYCLES,
    DEFAULT_MAX_GROUPS, DEFAULT_WAVE_WINDOW_PS,
    DEFAULT_X_TRACE_MAX_DEPTH,
    get_fsdb_runtime_info,
)
from src.log_parser import SimLogParser, diff_failure_events, get_error_context
from src.vcd_parser import VCDParser
from src.fsdb_parser import FSDBParser
from src.fsdb_signal_index import FSDBSignalIndex
from src.analyzer import WaveformAnalyzer
from src.compile_log_parser import parse_compile_log
from src.cursor_store import CursorStore
import src.usage_telemetry as usage_telemetry
from src.hierarchy_handles import HandleStore, compute_handle
from src.timespec import resolve_timespec
# diff_value_distribution is implemented in src.verify_condition but deliberately
# not wired up as an MCP tool — see docs/auto-debug-v2-pilot-results.md.
from src.verify_condition import diff_first_divergence, period, inspect_handshake
from src.handshake_suggest import suggest_handshakes
from src.handshake_sweep import sweep_handshake_anomalies
from src.path_discovery import discover_sim_paths
from src.problem_hints import compute_problem_hints, compute_xprop_priority_for_group
from src.tb_hierarchy_builder import build_hierarchy, build_slim_payload
from src.signal_driver import explain_signal_driver
from src.signal_load import find_signal_loads
from src.verdi_backend import probe_verdi_backend
from src.structural_scanner import ALL_CATEGORIES, scan_structural_risks
from src.x_trace import trace_x_source
from src.cycle_query import (
    _compute_clock_period_ps,
    _extract_edge_times,
    get_signals_by_cycle,
)
from pydantic import BaseModel
import src.schemas as schemas


# Session state and workflow prerequisite gating.
_session_state: dict[str, dict | None] = {
    "get_sim_paths": None,
    "build_tb_hierarchy": None,
}

_result_cache: dict[str, schemas.SchemaModel | None] = {
    "get_sim_paths": None,
    "build_tb_hierarchy": None,
    "parse_sim_log": None,
    "scan_structural_risks": None,
    "recommend_failure_debug_next_steps": None,
}

_result_provenance: dict[str, dict | None] = {
    "get_sim_paths": None,
    "build_tb_hierarchy": None,
    "parse_sim_log": None,
    "scan_structural_risks": None,
    "recommend_failure_debug_next_steps": None,
}

# In-process snapshots of parsed log failure events. These preserve the
# baseline across common rerun flows where the simulator overwrites the same
# run.log path before the LLM asks for an explicit diff.
_log_snapshots: dict[str, dict] = {}
_log_snapshot_history: dict[tuple[str, str], list[str]] = {}

# Holds the full build_tb_hierarchy payload keyed by content-addressed handle.
# The slim LLM-facing payload references this via `hierarchy_handle`; handle
# tools resolve through this store. Lifetime is tied to build_tb_hierarchy's
# cache entry — see _invalidate_downstream / _clear_result_state.
_handle_store = HandleStore()

# Named time anchors for the auto-debug v2 workflow (decision 5). Lifetime
# is process-scoped — same semantics as _handle_store: no persistence,
# server restart drops every cursor.
_cursor_store = CursorStore()

_DOWNSTREAM_DEPS: dict[str, list[str]] = {
    "get_sim_paths": ["build_tb_hierarchy", "parse_sim_log", "recommend_failure_debug_next_steps"],
    "build_tb_hierarchy": ["recommend_failure_debug_next_steps"],
    "parse_sim_log": ["recommend_failure_debug_next_steps"],
    "scan_structural_risks": ["recommend_failure_debug_next_steps"],
}

_PREREQUISITES: dict[str, list[str]] = {
    "parse_sim_log": ["get_sim_paths"],
    "diff_sim_failure_results": ["get_sim_paths"],
    "get_error_context": ["get_sim_paths"],
    "recommend_failure_debug_next_steps": ["get_sim_paths", "build_tb_hierarchy"],
    "analyze_failures": ["get_sim_paths", "build_tb_hierarchy"],
    "analyze_failure_event": ["get_sim_paths", "build_tb_hierarchy"],
    "explain_signal_driver": ["build_tb_hierarchy"],
    "find_signal_loads": ["build_tb_hierarchy"],
    "trace_signal_path": ["build_tb_hierarchy"],
    "trace_x_source": ["build_tb_hierarchy"],
    "get_tb_subtree": ["build_tb_hierarchy"],
    "lookup_tb_files": ["build_tb_hierarchy"],
    "find_tb_instance": ["build_tb_hierarchy"],
    "get_tb_file_detail": ["build_tb_hierarchy"],
    "get_tb_class_hierarchy": ["build_tb_hierarchy"],
    "dump_tb_section": ["build_tb_hierarchy"],
}

_PREREQUISITE_REASONS: dict[str, str] = {
    "get_sim_paths": (
        "get_sim_paths must be called first to discover simulator type, "
        "file paths, and FSDB runtime status."
    ),
    "build_tb_hierarchy": (
        "build_tb_hierarchy must be called first to build the testbench "
        "hierarchy used for source-aware analysis."
    ),
}


def _check_prerequisites(tool_name: str) -> dict | None:
    prereqs = _PREREQUISITES.get(tool_name)
    if not prereqs:
        return None
    for step in prereqs:
        if _session_state[step] is None:
            block = {
                "ok": False,
                "error_code": "missing_prerequisite",
                "missing_step": step,
                "required_before": tool_name,
                "reason": _PREREQUISITE_REASONS[step],
                "suggested_call": _build_suggested_call(step),
            }
            return schemas.PrerequisiteBlockResult.model_validate(block)
    return None


def _build_suggested_call(step: str) -> dict:
    if step == "get_sim_paths":
        return {"tool": "get_sim_paths", "arguments": {}}
    if step == "build_tb_hierarchy":
        sim_state = _session_state.get("get_sim_paths")
        if sim_state and sim_state.get("compile_log"):
            args: dict = {"compile_log": sim_state["compile_log"]}
            if sim_state.get("simulator"):
                args["simulator"] = sim_state["simulator"]
            return {"tool": "build_tb_hierarchy", "arguments": args}
        return {"tool": "build_tb_hierarchy", "arguments": {}}
    if step == "parse_sim_log":
        sim_result = _result_cache.get("get_sim_paths")
        if sim_result and sim_result.sim_logs:
            return {
                "tool": "parse_sim_log",
                "arguments": {
                    "log_path": sim_result.sim_logs[0].path,
                    "simulator": sim_result.simulator or "auto",
                },
            }
        return {"tool": "parse_sim_log", "arguments": {}}
    if step == "recommend_failure_debug_next_steps":
        args: dict = {}
        sim_result = _result_cache.get("get_sim_paths")
        if sim_result:
            if sim_result.sim_logs:
                args["log_path"] = sim_result.sim_logs[0].path
            if sim_result.wave_files:
                args["wave_path"] = sim_result.wave_files[0].path
            if sim_result.simulator:
                args["simulator"] = sim_result.simulator
        hier_state = _session_state.get("build_tb_hierarchy")
        if hier_state and hier_state.get("compile_log"):
            args["compile_log"] = hier_state["compile_log"]
        return {"tool": "recommend_failure_debug_next_steps", "arguments": args}
    return {"tool": step, "arguments": {}}


def _invalidate_downstream(from_tool: str):
    for downstream in _DOWNSTREAM_DEPS.get(from_tool, []):
        if downstream in _session_state:
            _session_state[downstream] = None
        if downstream in _result_cache:
            _result_cache[downstream] = None
        if downstream in _result_provenance:
            _result_provenance[downstream] = None
        if downstream == "build_tb_hierarchy":
            _handle_store.invalidate()


def _clear_result_state():
    for key in _result_cache:
        _result_cache[key] = None
    for key in _result_provenance:
        _result_provenance[key] = None
    _log_snapshots.clear()
    _log_snapshot_history.clear()
    _handle_store.invalidate()
    _cursor_store.clear()


def _session_identity(sim_result: schemas.SimPathsResult | dict | None) -> tuple | None:
    if sim_result is None:
        return None
    if isinstance(sim_result, schemas.SimPathsResult):
        verif_root = sim_result.verif_root
        case_name = sim_result.case_name
        compile_logs = [entry.model_dump() for entry in sim_result.compile_logs]
    else:
        verif_root = sim_result.get("verif_root")
        case_name = sim_result.get("case_name")
        compile_logs = list(sim_result.get("compile_logs", []))

    compile_log = None
    for entry in compile_logs:
        if entry.get("phase") == "elaborate":
            compile_log = entry
            break
    if compile_log is None and compile_logs:
        compile_log = compile_logs[0]
    if compile_log is None:
        compile_sig = None
    else:
        compile_sig = (
            os.path.realpath(compile_log.get("path", "")) if compile_log.get("path") else None,
            compile_log.get("size"),
            compile_log.get("mtime"),
        )
    return verif_root, case_name, compile_sig


def _safe_probe_backend(compile_log: str, simulator: str) -> dict:
    """Probe Verdi backend status, tolerating missing/unparseable logs.

    Connectivity tools may be invoked with mocked compile_log paths
    (the underlying backend uses a monkey-patched parse_compile_log).
    The dispatch-level probe must not raise on the real path being
    absent — degrade to a Static-only status.
    """
    try:
        compile_result = parse_compile_log(compile_log, simulator)
        return probe_verdi_backend(compile_result, compile_log_path=compile_log)
    except Exception:
        return {
            "simulator": simulator if simulator in ("vcs", "xcelium") else "unknown",
            "backend": "static",
            "parser_match": "approximate",
            "kdb_path": None,
            "kdb_flow": "none",
            "kdb_hint": None,
        }


def _resolve_session_simulator(args: dict) -> str:
    explicit = args.get("simulator")
    if explicit and explicit != "auto":
        return explicit
    sim_result = _result_cache.get("get_sim_paths")
    if sim_result is not None and getattr(sim_result, "simulator", None):
        return sim_result.simulator
    requested_compile_log = args.get("compile_log")
    hierarchy_provenance = _result_provenance.get("build_tb_hierarchy")
    if (
        hierarchy_provenance
        and hierarchy_provenance.get("simulator")
        and _same_realpath(hierarchy_provenance.get("compile_log"), requested_compile_log)
    ):
        return hierarchy_provenance["simulator"]
    hierarchy_result = _result_cache.get("build_tb_hierarchy")
    if (
        hierarchy_result is not None
        and hierarchy_result.project.get("simulator")
        and hierarchy_provenance is not None
        and _same_realpath(hierarchy_provenance.get("compile_log"), requested_compile_log)
    ):
        return hierarchy_result.project["simulator"]
    return "auto"


def _log_stat_info(log_path: str) -> dict:
    stat_result = os.stat(log_path)
    return {
        "realpath": os.path.realpath(log_path),
        "mtime": stat_result.st_mtime,
        "mtime_ns": stat_result.st_mtime_ns,
        "size": stat_result.st_size,
    }


def _log_snapshot_id(
    log_path: str,
    simulator: str,
    stat_info: dict,
    all_failure_events: list[dict],
) -> str:
    events_material = json.dumps(all_failure_events, sort_keys=True, default=str)
    events_digest = hashlib.sha256(events_material.encode("utf-8")).hexdigest()[:16]
    material = "|".join(
        [
            stat_info["realpath"],
            simulator,
            events_digest,
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"log_{digest}"


def _capture_log_snapshot(
    log_path: str,
    simulator: str,
    all_failure_events: list[dict],
    stat_info: dict | None = None,
) -> str:
    stat_info = stat_info or _log_stat_info(log_path)
    snapshot_id = _log_snapshot_id(log_path, simulator, stat_info, all_failure_events)
    entry = {
        "snapshot_id": snapshot_id,
        "log_path": log_path,
        "realpath": stat_info["realpath"],
        "simulator": simulator,
        "all_failure_events": list(all_failure_events),
        "log_mtime": stat_info["mtime"],
        "log_mtime_ns": stat_info["mtime_ns"],
        "log_size": stat_info["size"],
    }
    _log_snapshots[snapshot_id] = entry
    key = (entry["realpath"], simulator)
    history = _log_snapshot_history.setdefault(key, [])
    if snapshot_id not in history:
        history.append(snapshot_id)
    return snapshot_id


def _find_previous_log_snapshot(
    log_path: str,
    simulator: str,
    exclude_snapshot_id: str | None = None,
) -> dict | None:
    realpath = os.path.realpath(log_path)
    history = _log_snapshot_history.get((realpath, simulator), [])
    for snapshot_id in reversed(history):
        if snapshot_id == exclude_snapshot_id:
            continue
        snapshot = _log_snapshots.get(snapshot_id)
        if snapshot is None:
            continue
        return snapshot
    return None


def _snapshot_events(snapshot_id: str, simulator: str) -> tuple[list[dict], dict]:
    snapshot = _log_snapshots.get(snapshot_id)
    if snapshot is None:
        raise ValueError(
            f"log snapshot is not available in this MCP session: {snapshot_id}. "
            "Re-run parse_sim_log before rerunning simulation to create a baseline snapshot."
        )
    if snapshot.get("simulator") != simulator:
        raise ValueError(
            f"log snapshot {snapshot_id} was parsed with simulator={snapshot.get('simulator')}, "
            f"but this diff requested simulator={simulator}."
        )
    return list(snapshot["all_failure_events"]), {
        "source": "snapshot",
        "snapshot_id": snapshot_id,
        "log_file": snapshot.get("log_path"),
    }


def _parse_log_events_for_diff(log_path: str, simulator: str) -> tuple[list[dict], dict]:
    stat_info = _log_stat_info(log_path)
    events = SimLogParser(log_path, simulator).parse_failure_events()
    snapshot_id = _capture_log_snapshot(log_path, simulator, events, stat_info)
    return events, {
        "source": "path",
        "snapshot_id": snapshot_id,
        "log_file": log_path,
    }


def _resolve_base_events_for_diff(args: dict, simulator: str) -> tuple[list[dict], dict]:
    if args.get("base_snapshot_id"):
        return _snapshot_events(args["base_snapshot_id"], simulator)

    base_log_path = args.get("base_log_path")
    new_log_path = args.get("new_log_path")
    if base_log_path and new_log_path and os.path.realpath(base_log_path) == os.path.realpath(new_log_path):
        previous = _find_previous_log_snapshot(new_log_path, simulator)
        if previous is not None:
            return list(previous["all_failure_events"]), {
                "source": "auto_previous_snapshot",
                "snapshot_id": previous["snapshot_id"],
                "log_file": previous.get("log_path"),
            }

    if base_log_path:
        return _parse_log_events_for_diff(base_log_path, simulator)

    if new_log_path:
        previous = _find_previous_log_snapshot(new_log_path, simulator)
        if previous is not None:
            return list(previous["all_failure_events"]), {
                "source": "auto_previous_snapshot",
                "snapshot_id": previous["snapshot_id"],
                "log_file": previous.get("log_path"),
            }
        raise ValueError(
            "baseline_snapshot_missing: no previous parsed snapshot exists for "
            f"{new_log_path}. Call parse_sim_log before rerunning simulation so "
            "TraceWeave can preserve the baseline even if the simulator overwrites the log."
        )

    raise ValueError("diff_sim_failure_results requires base_snapshot_id or base_log_path, or a new_log_path with a previous parsed snapshot.")


def _resolve_new_events_for_diff(args: dict, simulator: str) -> tuple[list[dict], dict]:
    if args.get("new_snapshot_id"):
        return _snapshot_events(args["new_snapshot_id"], simulator)
    if args.get("new_log_path"):
        return _parse_log_events_for_diff(args["new_log_path"], simulator)
    if args.get("base_snapshot_id") and not args.get("base_log_path"):
        parse_cache = _result_provenance.get("parse_sim_log")
        if parse_cache and parse_cache.get("log_snapshot_id"):
            return _snapshot_events(parse_cache["log_snapshot_id"], simulator)
    raise ValueError("diff_sim_failure_results requires new_snapshot_id or new_log_path.")


def _diff_source(base_meta: dict, new_meta: dict) -> str:
    if base_meta.get("source") == "auto_previous_snapshot":
        return "auto_previous_snapshot"
    if base_meta.get("source") == "snapshot" and new_meta.get("source") == "snapshot":
        return "snapshots"
    if base_meta.get("source") == "path" and new_meta.get("source") == "path":
        return "paths"
    return "mixed"


def _update_session_state(tool_name: str, args: dict, result: dict):
    if tool_name == "get_sim_paths":
        previous_identity = _session_identity(_result_cache.get("get_sim_paths"))
        new_identity = _session_identity(result)
        if previous_identity is not None and previous_identity != new_identity:
            _session_state["get_sim_paths"] = None
            _session_state["build_tb_hierarchy"] = None
            _clear_result_state()
        else:
            _invalidate_downstream(tool_name)
        compile_log = None
        for entry in result.get("compile_logs", []):
            if entry.get("phase") == "elaborate":
                compile_log = entry["path"]
                break
        if compile_log is None:
            logs = result.get("compile_logs", [])
            if logs:
                compile_log = logs[0]["path"]
        _session_state["get_sim_paths"] = {
            "verif_root": result.get("verif_root"),
            "case_dir": result.get("case_dir"),
            "simulator": result.get("simulator"),
            "compile_log": compile_log,
        }
        # Anchor a telemetry session to the discovered case identity: a new case
        # opens a new logical session; re-discovering the same case keeps it.
        usage_telemetry.note_session(new_identity)
    elif tool_name == "build_tb_hierarchy":
        _invalidate_downstream(tool_name)
        _session_state["build_tb_hierarchy"] = {
            "compile_log": args.get("compile_log"),
            "simulator": args.get("simulator") or result.get("project", {}).get("simulator", "auto"),
        }


def reset_session_state():
    _session_state["get_sim_paths"] = None
    _session_state["build_tb_hierarchy"] = None
    _clear_result_state()


SERVER_INSTRUCTIONS = """
Waveform debug workflow:

0. Call get_diagnostic_snapshot at session start before any other step.
   - Zero-cost: only reads cached results, never triggers sub-steps.
   - If prior steps are already cached, skip them and continue from the current state.
   - Returns availability status for: sim_paths, hierarchy, log_analysis, recommended_next
   - Missing items include suggested_call with pre-filled arguments

1. ALWAYS start with get_sim_paths to discover file paths and simulator type.
   (Skip if step 0 confirmed sim_paths is already cached and up to date.)
   - Inspect discovery_mode first: root_dir, case_dir, or unknown.
   - If discovery_mode is unknown, do not guess deeper paths; follow returned hints.
   - If case_name is unknown in root_dir mode, omit it to get available_cases first.
   - Inform the user early when hints show missing logs, empty logs, or missing waves.
   - Prefer compile_logs entries with phase="elaborate" for build_tb_hierarchy.
   - If fsdb_runtime.enabled is false, prefer .vcd entries in wave_files over .fsdb.

2. MUST call build_tb_hierarchy AND scan_structural_risks before analyzing failures.
   Both independently parse the same compile_log — call them in parallel.
   - build_tb_hierarchy: builds testbench hierarchy for source-aware analysis.
     Use the elaborate-phase compile_log and simulator from step 1.
     Returns a slim payload (project, stats, tree_skeleton truncated to depth 2,
     interfaces, ambiguous_basenames, hierarchy_handle). The full file list,
     full component_tree, class hierarchy, and raw compile_result are NOT in
     the response — fetch them on demand via the handle tools listed in
     handle_tools. Pass the returned hierarchy_handle to every handle tool.
     * If ambiguous_basenames is non-empty, the compile_log contains multiple
       files sharing a basename (e.g. xxx_v1.v vs xxx_v2.v). Before reading
       any of them, call lookup_tb_files(basename=...) to confirm which
       path was actually compiled in this session.
     * Before reading an RTL/TB source file, call get_tb_file_detail(path=...)
       (or lookup_tb_files with a filter) to verify the path is in the
       compile set. Do NOT find/grep to scan directories for source files.
     * Use get_tb_subtree(root="top.x.y", depth=N) to drill into the
       hierarchy instead of asking for the whole tree.
     * Use find_tb_instance(path=... or module=...) to jump directly to a
       failing instance.
     * dump_tb_section is a heavy escape hatch — prefer the targeted tools
       above.
   - scan_structural_risks: detects static structural risks (slice_overlap, multi_drive, etc.).
     Use the same compile_log and simulator. Do not wait for parse_sim_log results.
     Structural risks that overlap with failing signal paths are high-priority root cause candidates.

3. Call parse_sim_log with sim_logs[0].path and simulator from step 1 when sim_logs is non-empty.
   - Prefer normalized failure_events[].time_ps over re-parsing raw message text.
   - Use grouped errors to choose the first group_index to inspect.
   - first_group_context contains ~200 lines of raw log text around the first error.
     Use get_error_context only for other groups.
   - If previous_log_detected is true, consider diff_sim_failure_results early.
   - When parse_sim_log returns auto_diff, it contains a diff against the previous
     parse of the same log. Use it to verify which failures were resolved or
     introduced by the latest code change. Do not ignore resolved/introduced counts.
   - parse_sim_log also returns log_snapshot_id and, for same-path reruns,
     previous_log_snapshot_id. If the simulator overwrites the same log name
     between runs, call diff_sim_failure_results with snapshot IDs or with only
     new_log_path; TraceWeave will use the previous parsed snapshot as baseline.
   - For large error counts (>100), use detail_level="summary" first, then inspect specific groups with get_error_context or detail_level="full".
   - Default detail_level is "summary" to keep MCP responses below harness budget.

4. Call recommend_failure_debug_next_steps to get a default target and role-ranked signals.

5. Call search_signals to confirm full hierarchical signal paths when needed.
   - Derive keywords from build_tb_hierarchy output, error messages, recommend_failure_debug_next_steps, or RTL source.
   - When reading RTL source, verify the path is in the compile set first —
     call get_tb_file_detail(path=...) or lookup_tb_files(...) with the
     hierarchy_handle returned in step 2. The compile_log is the single
     source of truth for which file version was actually compiled.

6. Call analyze_failures with log_path, wave_path, simulator, and confirmed signal_paths.
   - Follow analysis_guide in the result.

7. Use deep-dive tools when needed:
   - analyze_failure_event for failure-centric instance/source correlation
   - explain_signal_driver when a suspicious waveform signal needs RTL driver lookup
   - trace_x_source when a signal shows X/Z values; if it stops at instance port connections, inspect listed bit-ranges for gaps or overlaps
   - get_signals_by_cycle for clock-aligned cycle-level signal value tables; ideal for state machines, pipelines, and algorithm core round-by-round comparison
   - get_error_context for other groups
   - get_signal_transitions for longer history
   - get_signals_around_time for additional signals
   - get_signal_at_time for exact values
   - get_waveform_summary for waveform sanity checks

8. Call get_diagnostic_snapshot at any time to check workflow state.
   - Does NOT execute any sub-steps; only reads cached results
""".strip()

app = Server("traceweave", instructions=SERVER_INSTRUCTIONS)

# Global parser cache.
_fsdb_index_cache: dict[str, tuple[tuple[int, int], FSDBSignalIndex]] = {}
_parser_cache: dict[str, tuple[tuple[int, int], object]] = {}          # wave_path → ((mtime_ns, size), parser)


def _get_wave_signature(wave_path: str) -> tuple[int, int]:
    stat = os.stat(wave_path)
    return stat.st_mtime_ns, stat.st_size


def _dispose_cached_object(obj: object):
    close = getattr(obj, "close", None)
    if callable(close):
        close()
        return
    parser = getattr(obj, "_parser", None)
    parser_close = getattr(parser, "close", None)
    if callable(parser_close):
        parser_close()


def _resolve_time(spec, *, allow_sentinel: bool = False) -> int:
    """Resolve a TimeSpec (int ps, '@cursor', or unit literal) to ps.

    Thin adapter binding the shared resolver to this server's cursor
    store so every time-taking dispatch branch gets cursor + unit
    support without threading the store through each call site.
    """
    return resolve_timespec(spec, _cursor_store, allow_sentinel=allow_sentinel)


def _get_parser(wave_path: str):
    """Return a cached parser instance to avoid reparsing VCDs or reopening FSDBs."""
    signature = _get_wave_signature(wave_path)
    cached = _parser_cache.get(wave_path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    if cached is not None:
        _dispose_cached_object(cached[1])
    ext = wave_path.lower().rsplit(".", 1)[-1]
    if ext == "vcd":
        parser = VCDParser(wave_path)
    elif ext == "fsdb":
        parser = FSDBParser(wave_path)
    else:
        raise ValueError(f"Unsupported waveform format: .{ext}")
    _parser_cache[wave_path] = (signature, parser)
    return parser


def _detect_wave_clock(parser) -> tuple[str | None, int | None]:
    """Best-effort clock auto-detect, cached on the parser instance."""
    cached = getattr(parser, "_cached_clock_info", None)
    if cached is not None:
        return cached

    clock_path: str | None = None
    period_ps: int | None = None
    detect_reason: str | None = None

    try:
        candidate_paths: set[str] = set()
        for keyword in ("clk", "clock"):
            try:
                search = parser.search_signals(keyword, max_results=20)
            except Exception as exc:
                if detect_reason is None:
                    detect_reason = (
                        f"search_signals({keyword!r}) failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                continue
            for item in search.get("results", []):
                if item.get("width", 0) == 1 and item.get("path"):
                    candidate_paths.add(item["path"])

        scored: list[tuple[str, int, int]] = []
        for candidate in sorted(candidate_paths, key=lambda path: (path.count("."), len(path))):
            try:
                transitions = parser.get_transitions(
                    candidate, 0, CLOCK_DETECT_SAMPLE_PS
                ).get("transitions", [])
                edge_times = _extract_edge_times(transitions, "posedge")
                period = _compute_clock_period_ps(edge_times)
                if period and period > 0:
                    scored.append((candidate, period, len(edge_times)))
            except Exception as exc:
                if detect_reason is None:
                    detect_reason = (
                        f"get_transitions({candidate!r}) failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                continue

        if scored:
            scored.sort(key=lambda item: -item[2])
            clock_path, period_ps, _ = scored[0]
            detect_reason = None
    except Exception as exc:
        detect_reason = f"{type(exc).__name__}: {exc}"

    try:
        parser._cached_clock_info = (clock_path, period_ps)
        parser._cached_clock_detect_reason = detect_reason
    except Exception:
        pass

    return clock_path, period_ps


def _validate_signals_around_time_args(
    parser,
    center_ps: int,
    window_ps: int,
    signal_paths: list[str] | None,
) -> None:
    """Guardrails for get_signals_around_time; raise ValueError with recovery hints."""
    signal_paths = signal_paths or []

    if window_ps < 0:
        raise ValueError("window_ps must be non-negative")

    clock_path, clock_period_ps = _detect_wave_clock(parser)

    if clock_period_ps and clock_period_ps > 0:
        requested_cycles = window_ps // clock_period_ps
        if requested_cycles > MAX_WAVE_WINDOW_CYCLES:
            raise ValueError(
                f"window_ps={window_ps} (±{window_ps/1000:.0f} ns) "
                f"= {requested_cycles} clock cycles, exceeds the per-call cap "
                f"MAX_WAVE_WINDOW_CYCLES={MAX_WAVE_WINDOW_CYCLES} "
                f"(clock_period_ps={clock_period_ps}, detected from {clock_path}). "
                f"This tool is for local causal-chain inspection around a failure "
                f"timestamp. For multi-cycle sampling use get_signals_by_cycle "
                f"(same {MAX_CYCLES_PER_QUERY}-cycle budget). "
                f"Typical window_ps: glitch 1-5 ns; 1 clock cycle = clock_period_ps; "
                f"N cycles = N * clock_period_ps."
            )
    elif window_ps > FALLBACK_WAVE_WINDOW_PS:
        detect_reason = getattr(parser, "_cached_clock_detect_reason", None)
        reason_suffix = (
            f" (detection error: {detect_reason})" if detect_reason else ""
        )
        raise ValueError(
            f"window_ps={window_ps} (±{window_ps/1000:.0f} ns) exceeds the "
            f"fallback cap FALLBACK_WAVE_WINDOW_PS={FALLBACK_WAVE_WINDOW_PS} ps "
            f"(auto-detect found no 1-bit clock signal matching 'clk'/'clock' "
            f"in this waveform{reason_suffix}). For multi-cycle sampling use "
            f"get_signals_by_cycle."
        )

    sim_end_ps = 0
    try:
        sim_end_ps = int(parser.get_summary().get("simulation_duration_ps") or 0)
    except Exception:
        pass

    if sim_end_ps > 0 and center_ps > sim_end_ps:
        raise ValueError(
            f"center_time_ps={center_ps} ({center_ps/1000:.0f} ns, "
            f"{center_ps/1_000_000_000:.3f} ms) is past the recorded waveform end "
            f"(simulation_duration_ps={sim_end_ps}, "
            f"{sim_end_ps/1_000_000_000:.3f} ms). "
            f"Common pitfall: ns->ps conversion - if the sim log shows `Time: X ns`, "
            f"set center_time_ps = X*1000. Call get_waveform_summary to confirm "
            f"the recorded duration."
        )


# ═══════════════════════════════════════════════════════════════════
# Tool definitions
# ═══════════════════════════════════════════════════════════════════

# Time inputs accept a TimeSpec: an integer (ps), a cursor reference
# "@<name>", or a unit literal like "12.34ns". See src/timespec.py.
_TIMESPEC_TYPE = ["integer", "string"]
_TIMESPEC_HINT = " Accepts an integer (ps), a cursor reference like '@div_3a7c', or a unit literal like '12.34ns'."


@app.list_tools()
async def list_tools():
    _tools = [

        Tool(
            name="get_sim_paths",
            description=(
                "Discover compile logs, simulation logs, and waveform files under a verif directory. "
                "If case_name is omitted, the tool returns available cases."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {"type": "string",
                                   "description": "Absolute path to the project's verif/ directory, for example /home/robin/Projects/i2c_lib/verif"},
                    "case_name":  {"type": "string",
                                   "description": "Optional case name, for example case0 (matching make SV_CASE=case0)"},
                },
                "required": ["verif_root"],
            },
        ),

        Tool(
            name="parse_sim_log",
            description=(
                "Parse a VCS or Xcelium simulation log and return grouped runtime failures by signature. "
                "The simulator argument is required and is not auto-detected here. "
                "The first error group automatically includes about 100 lines of surrounding log context "
                "in first_group_context; use get_error_context for other groups."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":  {"type": "string", "description": "Absolute path to the simulation log, for example irun.log"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                    "max_groups": {
                        "type": "integer",
                        "description": f"Maximum number of error groups to return. Default: {DEFAULT_MAX_GROUPS}",
                        "default": DEFAULT_MAX_GROUPS,
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["summary", "compact", "full"],
                        "description": f"Detail level to return. Default: {DEFAULT_DETAIL_LEVEL}",
                        "default": DEFAULT_DETAIL_LEVEL,
                    },
                    "max_events_per_group": {
                        "type": "integer",
                        "description": f"Maximum failure_events returned per group in compact/full modes. Default: {DEFAULT_MAX_EVENTS_PER_GROUP}",
                        "default": DEFAULT_MAX_EVENTS_PER_GROUP,
                    },
                },
                "required": ["log_path", "simulator"],
            },
        ),

        Tool(
            name="diff_sim_failure_results",
            description=(
                "Compare normalized failure events from two simulation logs. "
                "Returns resolved, persistent, and newly introduced failures, plus changes in failure type, "
                "X/Z presence, first-failure timing, and a convergence summary. "
                "If a simulator overwrites the same log path between runs, pass new_log_path only "
                "after parse_sim_log has captured the baseline snapshot, or pass snapshot IDs returned "
                "by parse_sim_log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "base_log_path": {"type": "string", "description": "Baseline simulation log. Optional when base_snapshot_id is supplied, or when new_log_path has a previous parsed snapshot."},
                    "new_log_path": {"type": "string", "description": "New simulation log. For same-path reruns, this may be the overwritten log path."},
                    "base_snapshot_id": {"type": "string", "description": "Baseline log snapshot ID returned by parse_sim_log."},
                    "new_snapshot_id": {"type": "string", "description": "New log snapshot ID returned by parse_sim_log."},
                    "simulator": {"type": "string", "description": "vcs / xcelium / auto. Defaults to simulator discovered by get_sim_paths when omitted."},
                },
                "required": [],
            },
        ),

        Tool(
            name="get_error_context",
            description=(
                "Extract raw log text around a given error line. "
                "Typically used with first_line returned by parse_sim_log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path": {"type": "string", "description": "Absolute path to the simulation log, for example irun.log"},
                    "line": {"type": "integer", "description": "Center error line number"},
                    "before": {
                        "type": "integer",
                        "description": f"Number of lines before the target line. Default: {DEFAULT_LOG_CONTEXT_BEFORE}",
                        "default": DEFAULT_LOG_CONTEXT_BEFORE,
                    },
                    "after": {
                        "type": "integer",
                        "description": f"Number of lines after the target line. Default: {DEFAULT_LOG_CONTEXT_AFTER}",
                        "default": DEFAULT_LOG_CONTEXT_AFTER,
                    },
                },
                "required": ["log_path", "line"],
            },
        ),

        Tool(
            name="search_signals",
            description=(
                "Search for signals in a waveform file (FSDB/VCD) and return full hierarchical paths. "
                "Use this when the client knows a leaf signal name but not the full path. "
                "Each result also carries `direction` (input/output/inout/implicit/...) and `var_type` "
                "(wire/reg/integer/real/parameter/memory/...), so callers can filter by port direction "
                "or language type within a scope by combining a hierarchical keyword with these fields — "
                "no separate listing tool is needed. "
                "Note: VCD format does not encode port direction; `direction` is always null for VCD waves, "
                "while `var_type` is populated. FSDB populates both. "
                "FSDB search uses a scope-tree index and does not read value changes, so it scales well to large files. "
                "FSDB support depends on fsdb_runtime.enabled returned by get_sim_paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "Absolute path to the waveform file"},
                    "keyword":   {"type": "string", "description": "Signal keyword, for example s_bits, clk, or data"},
                    "max_results": {"type": "integer", "description": "Maximum number of matches to return. Default: 50",
                                    "default": 50},
                },
                "required": ["wave_path", "keyword"],
            },
        ),

        Tool(
            name="get_signal_at_time",
            description="Query a signal value in a waveform file at a specific time in ps. FSDB support depends on fsdb_runtime.enabled.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":   {"type": "string"},
                    "signal_path": {"type": "string",
                                    "description": "Full hierarchical path, for example top_tb.dut.s_bits"},
                    "time_ps":     {"type": _TIMESPEC_TYPE, "description": "Query time." + _TIMESPEC_HINT},
                },
                "required": ["wave_path", "signal_path", "time_ps"],
            },
        ),

        Tool(
            name="get_signal_transitions",
            description="Return all transitions for a signal over a time range. FSDB support depends on fsdb_runtime.enabled.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_path":   {"type": "string"},
                    "start_time_ps": {"type": _TIMESPEC_TYPE, "default": 0, "description": "Window start." + _TIMESPEC_HINT},
                    "end_time_ps":   {"type": _TIMESPEC_TYPE, "default": -1,
                                      "description": "-1 means through the end of simulation." + _TIMESPEC_HINT},
                },
                "required": ["wave_path", "signal_path"],
            },
        ),

        Tool(
            name="get_signals_around_time",
            description=(
                "Return values and transitions for multiple signals in a NARROW window "
                "around a target timestamp (typically the failure time). Designed for "
                "local causal-chain inspection; NOT for bulk trace extraction. For "
                "round-by-round or multi-cycle sampling use get_signals_by_cycle.\n"
                "\n"
                "Unit reminder: all times are picoseconds. If the sim log reports "
                "`Time: X ns`, set center_time_ps = X*1000 (example: 75,100 ns -> "
                "75,100,000 ps).\n"
                "\n"
                "Typical window_ps:\n"
                "  - Glitch inspection:     1,000 - 5,000 ps\n"
                "  - One clock cycle:       = clock_period_ps (NOT exposed by\n"
                "                             get_waveform_summary; use\n"
                "                             get_signals_by_cycle after you\n"
                "                             identify a clock_path, or read it\n"
                "                             from your sim environment /\n"
                "                             compile log)\n"
                "  - N cycles around fail:  N * clock_period_ps\n"
                "\n"
                "The server enforces a cap of MAX_WAVE_WINDOW_CYCLES (default 256) "
                "clock cycles per call, computed at runtime from an auto-detected "
                "clock_period_ps. It also rejects center_time_ps past the recorded "
                "simulation end. For multi-cycle sampling, get_signals_by_cycle "
                "still requires an explicit clock_path. FSDB support depends on "
                "fsdb_runtime.enabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_paths":  {"type": "array", "items": {"type": "string"},
                                      "description": "List of full hierarchical signal paths"},
                    "center_time_ps": {
                        "type": _TIMESPEC_TYPE,
                        "description": (
                            "Center time in PICOSECONDS (not ns). Convert sim-log ns "
                            "via *1000. Must be within the waveform duration reported "
                            "by get_waveform_summary." + _TIMESPEC_HINT
                        ),
                    },
                    "window_ps": {
                        "type": "integer",
                        "description": (
                            f"Half-window in ps (center +/- window_ps). "
                            f"Default: {DEFAULT_WAVE_WINDOW_PS}. "
                            f"Hard cap: MAX_WAVE_WINDOW_CYCLES clock cycles. "
                            f"For N-cycle sweeps prefer get_signals_by_cycle."
                        ),
                        "default": DEFAULT_WAVE_WINDOW_PS,
                    },
                    "extra_transitions": {
                        "type": "integer",
                        "description": f"Extra transitions to include before the time window. Default: {DEFAULT_EXTRA_TRANSITIONS}",
                        "default": DEFAULT_EXTRA_TRANSITIONS,
                    },
                },
                "required": ["wave_path", "signal_paths", "center_time_ps"],
            },
        ),

        Tool(
            name="get_signals_by_cycle",
            description=(
                "Return cycle-by-cycle sampled values for multiple signals aligned to a clock edge. "
                "Useful for state machines, pipelines, and round-by-round algorithm checks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "Absolute path to the waveform file"},
                    "clock_path": {
                        "type": "string",
                        "description": "Full hierarchical clock path, for example top_tb.des_clk",
                    },
                    "edge": {
                        "type": "string",
                        "enum": ["posedge", "negedge"],
                        "description": "Sampling edge. Default: posedge",
                        "default": "posedge",
                    },
                    "signal_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of full hierarchical signal paths to sample",
                    },
                    "start_cycle": {
                        "type": "integer",
                        "description": "Starting cycle index (0-based). Default: 0",
                        "default": 0,
                        "minimum": 0,
                    },
                    "num_cycles": {
                        "type": "integer",
                        "description": f"Number of cycles to sample. Default: 16. The server caps a single query at {MAX_CYCLES_PER_QUERY} cycles.",
                        "default": 16,
                        "minimum": 0,
                    },
                    "sample_offset_ps": {
                        "type": "integer",
                        "description": "Sampling offset relative to the clock edge in ps. Default: 1, to capture post-delta register values.",
                        "default": 1,
                        "minimum": 0,
                    },
                },
                "required": ["wave_path", "clock_path", "signal_paths"],
            },
        ),

        Tool(
            name="get_waveform_summary",
            description="Return basic waveform metadata such as format, duration, and top modules. FSDB support depends on fsdb_runtime.enabled.",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string"},
                },
                "required": ["wave_path"],
            },
        ),

        Tool(
            name="build_tb_hierarchy",
            description=(
                "Parse the compile/elaborate log, scan source files, and cache the full testbench hierarchy server-side. "
                "Returns a SLIM payload: project, stats, tree_skeleton (depth 2), interfaces, ambiguous_basenames, "
                "and hierarchy_handle. Use the handle with get_tb_subtree / lookup_tb_files / find_tb_instance / "
                "get_tb_file_detail / get_tb_class_hierarchy / dump_tb_section to access the full data on demand."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compile_log": {"type": "string", "description": "Absolute path to a compile or elaborate log"},
                    "simulator": {"type": "string", "description": "vcs / xcelium / auto (default: auto)",
                                  "default": "auto"},
                },
                "required": ["compile_log"],
            },
        ),

        Tool(
            name="scan_structural_risks",
            description=(
                "Run a Scope 1 regex-based structural risk scan on RTL/TB source files from the compile file list. "
                "This is a heuristic detector: it reports suspicious patterns, not confirmed root causes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compile_log": {"type": "string", "description": "Absolute path to a compile or elaborate log"},
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto (default: auto)",
                        "default": "auto",
                    },
                    "scan_scope": {
                        "type": "string",
                        "description": "Scan scope version. Currently only scope1 is supported.",
                        "default": "scope1",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": ALL_CATEGORIES},
                        "description": "Optional list of risk categories to scan. If omitted, all categories are scanned.",
                    },
                },
                "required": ["compile_log"],
            },
        ),

        Tool(
            name="analyze_failures",
            description=(
                "Core failure-analysis tool. Focuses on the first occurrence of a single failure group and returns "
                "the log summary, raw error context, and waveform snapshot. FSDB support depends on fsdb_runtime.enabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":     {"type": "string", "description": "Simulation log path, for example irun.log"},
                    "wave_path":    {"type": "string", "description": "Waveform file path, for example top_tb.fsdb"},
                    "signal_paths": {"type": "array", "items": {"type": "string"},
                                     "description": "Signal paths to inspect. Clients should confirm full paths with search_signals after inferring candidates from RTL or log output."},
                    "window_ps":    {"type": "integer",
                                     "description": f"Waveform window around each failure time in ps. Default: {DEFAULT_WAVE_WINDOW_PS}",
                                     "default": DEFAULT_WAVE_WINDOW_PS},
                    "simulator":    {"type": "string", "description": "vcs / xcelium"},
                    "group_index":  {"type": "integer", "description": "Failure group index to analyze. Default: 0", "default": 0},
                    "extra_transitions": {
                        "type": "integer",
                        "description": f"Extra transitions to include before the window for each signal. Default: {DEFAULT_EXTRA_TRANSITIONS}",
                        "default": DEFAULT_EXTRA_TRANSITIONS,
                    },
                },
                "required": ["log_path", "wave_path", "signal_paths", "simulator"],
            },
        ),

        Tool(
            name="analyze_failure_event",
            description=(
                "Start from a single normalized failure_event and combine waveform, hierarchy, and source information "
                "to return recommended instances, signals, and source files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path": {"type": "string"},
                    "wave_path": {"type": "string"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                    "failure_event": {"type": "object", "description": "Normalized failure_event from parse_sim_log for the same log"},
                    "compile_log": {"type": "string"},
                    "top_hint": {"type": "string"},
                },
                "required": ["log_path", "wave_path", "simulator", "failure_event"],
            },
        ),

        Tool(
            name="recommend_failure_debug_next_steps",
            description=(
                "Choose the highest-priority failure to investigate from the current log, waveform, and optional hierarchy, "
                "then recommend signals, instances, and suspected failure class. "
                "Also suggests a diff_sim_failure_results call to use on the next run."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path": {"type": "string"},
                    "wave_path": {"type": "string"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                    "compile_log": {"type": "string"},
                    "top_hint": {"type": "string"},
                },
                "required": ["log_path", "wave_path", "simulator"],
            },
        ),

        Tool(
            name="get_diagnostic_snapshot",
            description=(
                "Cold-start accelerator that aggregates cached tool results into a single summary view. "
                "It never triggers sub-steps and only reads cache. "
                "Returns availability status, compact summaries, and suggested calls for missing steps. "
                "The result cache is process-global and survives across cases, so at the start of a new "
                "session pass your target case (verif_root and/or case_dir): the snapshot validates the "
                "cache against it and reports a clean cold start if the cache belongs to a different case. "
                "If you pass no target, a cached sim_paths is returned with summary.carried_over=true to "
                "signal it may belong to a previous case — confirm it or re-run get_sim_paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {
                        "type": "string",
                        "description": (
                            "Absolute path to the project's verif/ directory. Builds a suggested_call "
                            "when get_sim_paths has not run, and validates that a cached get_sim_paths "
                            "result belongs to this project (mismatch ⇒ honest cold start)."
                        ),
                    },
                    "case_dir": {
                        "type": "string",
                        "description": (
                            "Absolute path to the specific case directory you are debugging. When given, "
                            "the snapshot confirms the cached get_sim_paths is for this case; if it is for "
                            "a different case, the snapshot degrades to a cold start instead of leaking the "
                            "previous case's paths/hierarchy/log."
                        ),
                    },
                },
                "required": [],
            },
        ),

        Tool(
            name="explain_signal_driver",
            description=(
                "Trace a waveform signal path back to the most likely RTL driver. "
                "Supports direct assigns, simple always blocks, and module output ports. "
                "Set recursive=true to walk multiple hops upstream across instance boundaries. "
                "When a Verdi KDB is detected, an NPI backend transparently engages and walks "
                "the elaborated netlist with fan_in_reg_list, crossing instance port boundaries "
                "the static source-regex backend cannot reach; otherwise the static backend "
                "runs. Each driver_chain hop carries source_info_origin ('compile_log' or 'npi') "
                "so consumers can tell which provenance produced its file:line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "signal_path": {"type": "string"},
                    "wave_path": {"type": "string"},
                    "compile_log": {"type": "string"},
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto. Optional — if omitted, server auto-injects the value discovered by get_sim_paths.",
                    },
                    "top_hint": {"type": "string"},
                    "recursive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to trace the upstream driver chain recursively",
                    },
                    "max_depth": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum recursive depth when recursive=true",
                    },
                },
                "required": ["signal_path", "wave_path", "compile_log"],
            },
        ),

        Tool(
            name="find_signal_loads",
            description=(
                "List places that consume (load) a signal: child instance input ports, "
                "RHS of assigns/procedural assignments, and always-block sensitivity lists. "
                "When a Verdi KDB is detected, an NPI backend transparently engages and "
                "resolves the cross-hierarchy / interface-positional / generate-block cases "
                "that the static source-regex backend cannot reach; otherwise the static "
                "backend runs (shallow_only) and surfaces gaps in stopped_at. Each load "
                "carries source_info_origin ('compile_log' or 'npi') so consumers can tell "
                "which provenance produced its file:line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "signal_path": {"type": "string"},
                    "compile_log": {"type": "string"},
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto. Optional — if omitted, server auto-injects the value discovered by get_sim_paths.",
                    },
                    "top_hint": {"type": "string"},
                    "max_depth": {
                        "type": "integer",
                        "default": 1,
                        "description": (
                            "Reserved for future transitive walks. Static backend "
                            "always behaves as 1; NPI backend's fan-out walk is "
                            "depth-bounded internally regardless of this argument."
                        ),
                    },
                    "include_expr": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include the surrounding expression for each load.",
                    },
                    "kind_filter": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["module_input", "rhs_expr", "always_sensitivity"],
                        },
                        "description": "Restrict result to a subset of load kinds.",
                    },
                },
                "required": ["signal_path", "compile_log"],
            },
        ),

        Tool(
            name="trace_signal_path",
            description=(
                "Find a connectivity path between two signals in the elaborated "
                "netlist (NPI-only). Returns one connected chain of nets walking "
                "across assigns, interface bindings, and instance boundaries. "
                "This is connectivity, NOT temporal driver direction — use "
                "explain_signal_driver for driver semantics. Without a Verdi KDB "
                "this tool returns unsupported_reason='static_backend_no_path_api' "
                "because source-regex cannot reproduce sig_to_sig_conn_list "
                "honestly; in that case fall back to explain_signal_driver + "
                "find_signal_loads."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_signal": {"type": "string"},
                    "to_signal": {"type": "string"},
                    "compile_log": {"type": "string"},
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto. Optional — auto-injected from get_sim_paths.",
                    },
                    "top_hint": {"type": "string"},
                    "expand_assigns": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, NPI treats `assign` as a separate cell, "
                            "yielding longer paths with explicit assign hops. "
                            "Useful when debugging RTL aliases."
                        ),
                    },
                },
                "required": ["from_signal", "to_signal", "compile_log"],
            },
        ),

        Tool(
            name="build_kdb",
            description=(
                "Auto-build a Verdi KDB from a parsed compile log using vericom + elabcom. "
                "Use this when the simulator is Xcelium (xrun) and the NPI backend reports no KDB, "
                "or to force-refresh a stale cached KDB. Output is cached under TRACEWEAVE_CACHE_DIR "
                "(default ~/.cache/traceweave/kdb/<hash>/); cache hits reuse the previous KDB without "
                "re-invoking Verdi. A runnable build.sh is written next to the KDB for inspection or "
                "reproduction. Requires VERDI_HOME with bin/vericom and bin/elabcom."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compile_log": {
                        "type": "string",
                        "description": "Absolute path to the compile/elaborate log to drive the build from.",
                    },
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto. Optional — auto-detected from the log when omitted.",
                    },
                    "top_hint": {
                        "type": "string",
                        "description": "Override the top module. Defaults to the first non-recorder top in compile_result.",
                    },
                    "force_rebuild": {
                        "type": "boolean",
                        "description": "Rebuild even if the cache key matches an existing KDB. Default: false.",
                        "default": False,
                    },
                },
                "required": ["compile_log"],
            },
        ),

        Tool(
            name="trace_x_source",
            description=(
                "When a signal shows X/Z at a target time, trace its propagation chain through upstream driver logic. "
                "If the trace reaches instance port connections, the tool lists them and stops there."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string"},
                    "signal_path": {"type": "string"},
                    "time_ps": {"type": _TIMESPEC_TYPE, "description": "Trace start time." + _TIMESPEC_HINT},
                    "compile_log": {"type": "string"},
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto. Optional — if omitted, server auto-injects the value discovered by get_sim_paths.",
                    },
                    "top_hint": {"type": "string"},
                    "max_depth": {
                        "type": "integer",
                        "description": f"Maximum trace depth. Default: {DEFAULT_X_TRACE_MAX_DEPTH}",
                        "default": DEFAULT_X_TRACE_MAX_DEPTH,
                    },
                },
                "required": ["wave_path", "signal_path", "time_ps", "compile_log"],
            },
        ),

        # ── Hierarchy handle tools (phase 4) ────────────────────────────
        # All six share the same access pattern: resolve `handle` via the
        # in-process HandleStore (registered by build_tb_hierarchy), then
        # return a typed slice or a HandleErrorResult.

        Tool(
            name="get_tb_subtree",
            description=(
                "Return a slice of the component_tree starting at `root` (dotted instance path) "
                "with up to `depth` levels. Use after build_tb_hierarchy to drill into a branch "
                "without pulling the whole tree into context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "hierarchy_handle from build_tb_hierarchy"},
                    "root": {"type": "string", "default": "",
                              "description": "dotted instance path (e.g. 'top.u_cpu'); empty = top module"},
                    "depth": {"type": "integer", "default": 1,
                              "description": "-1 = unbounded; otherwise number of levels to include"},
                    "max_nodes": {"type": "integer", "default": 500,
                                  "description": "hard cap on emitted nodes"},
                },
                "required": ["handle"],
            },
        ),

        Tool(
            name="lookup_tb_files",
            description=(
                "Query the compiled file set by objective scan facts. At least one filter is "
                "required. Use this to disambiguate multi-version files (basename collisions "
                "are also reported via build_tb_hierarchy.ambiguous_basenames)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "basename": {"type": "string", "description": "exact basename match"},
                    "name_contains": {"type": "string"},
                    "path_contains": {"type": "string"},
                    "has_module": {"type": "string", "description": "file defines this module"},
                    "contains_uvm": {"type": "boolean",
                                      "description": "scan saw `import uvm_pkg::` or `extends uvm_*`"},
                    "file_type": {"type": "string",
                                  "description": "module | interface | package | class | program (from SV scan)"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["handle"],
            },
        ),

        Tool(
            name="find_tb_instance",
            description=(
                "Locate instance(s) in the component_tree by exact path OR by module name. "
                "`path` and `module` are mutually exclusive."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "path": {"type": "string", "description": "exact dotted instance path"},
                    "module": {"type": "string", "description": "module name; returns all instances"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["handle"],
            },
        ),

        Tool(
            name="get_tb_file_detail",
            description=(
                "Return symbols (modules/classes/interfaces) defined in a single compiled file. "
                "If the path is not in the compile set, error includes basename-similar suggestions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["handle", "path"],
            },
        ),

        Tool(
            name="get_tb_class_hierarchy",
            description=(
                "Return UVM/class inheritance tree built from compiled-source scan results. "
                "Use `root_class` to start from a specific class; empty = all roots."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "root_class": {"type": "string"},
                    "depth": {"type": "integer", "default": -1},
                },
                "required": ["handle"],
            },
        ),

        Tool(
            name="dump_tb_section",
            description=(
                "Escape hatch: return a named raw section of the full hierarchy result. "
                "Prefer targeted handle tools — this is intentionally heavy."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "section": {
                        "type": "string",
                        "enum": [
                            "compile_result", "include_tree", "filelist_tree",
                            "interfaces", "files_full",
                            "component_tree_full", "class_hierarchy_full",
                        ],
                    },
                },
                "required": ["handle", "section"],
            },
        ),

        Tool(
            name="cursor_set",
            description=(
                "Register a named time anchor (in ps) for the current session. "
                "Other tools that take a time may reference '@<name>' instead of "
                "copying ps integers across calls. Cursors are process-scoped "
                "and dropped on server restart. Names must match [A-Za-z_][A-Za-z0-9_-]*."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Cursor name."},
                    "time_ps": {"type": "integer", "description": "Anchor time in ps. Must be >= 0."},
                    "note": {"type": "string", "description": "Optional human-readable note."},
                },
                "required": ["name", "time_ps"],
            },
        ),

        Tool(
            name="cursor_list",
            description="List all cursors registered in the current session, ordered by time.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        Tool(
            name="cursor_delete",
            description="Delete a named cursor. Returns whether the cursor existed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Cursor name to delete."},
                },
                "required": ["name"],
            },
        ),

        Tool(
            name="diff_first_divergence",
            description=(
                "Find the first time two signals hold unequal values. Works across "
                "two waveforms (passing run vs failing run) or within one waveform "
                "between two signals (expected vs actual). Auto-registers a cursor at "
                "the divergence time so downstream calls can reference it by name. "
                "Reads existing waveforms only — does NOT rerun simulation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path_a": {"type": "string", "description": "First waveform (FSDB or VCD)."},
                    "signal_a": {"type": "string", "description": "Full hierarchical signal path in wave_path_a."},
                    "wave_path_b": {"type": "string", "description": "Second waveform. May equal wave_path_a for within-run diff."},
                    "signal_b": {"type": "string", "description": "Full hierarchical signal path in wave_path_b."},
                    "start_time_ps": {"type": _TIMESPEC_TYPE, "description": "Start of comparison window. Default 0." + _TIMESPEC_HINT, "default": 0},
                    "end_time_ps": {"type": _TIMESPEC_TYPE, "description": "End of comparison window. -1 means end of simulation." + _TIMESPEC_HINT, "default": -1},
                    "cursor_name": {"type": "string", "description": "Optional explicit cursor name. If omitted, a deterministic name (div_<sha8>) is generated."},
                    "cursor_note": {"type": "string", "description": "Optional note attached to the registered cursor."},
                },
                "required": ["wave_path_a", "signal_a", "wave_path_b", "signal_b"],
            },
        ),

        Tool(
            name="period",
            description=(
                "Estimate a signal's dominant period inside a window and flag the "
                "first beat that deviates from it. Use for rhythm/throughput "
                "questions an LLM cannot eyeball from a transition dump: stalled "
                "clocks, dropped burst beats, backpressure bubbles, irregular "
                "strobes. The dominant period is the median edge-to-edge interval; "
                "the first off-beat is auto-registered as a cursor. Reads existing "
                "waveforms only — does NOT rerun simulation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "Waveform (FSDB or VCD)."},
                    "signal": {"type": "string", "description": "Full hierarchical signal path."},
                    "edge": {
                        "type": "string",
                        "enum": ["posedge", "negedge", "any"],
                        "description": "Edge to count. 'any' for multi-bit/strobe signals. Default posedge.",
                        "default": "posedge",
                    },
                    "start_time_ps": {"type": _TIMESPEC_TYPE, "description": "Window start. Default 0." + _TIMESPEC_HINT, "default": 0},
                    "end_time_ps": {"type": _TIMESPEC_TYPE, "description": "Window end. -1 means end of simulation." + _TIMESPEC_HINT, "default": -1},
                    "tolerance_frac": {
                        "type": "number",
                        "description": "Fraction of the period a beat may deviate before counting as an off-beat. Default 0.05 (5%).",
                        "default": 0.05,
                    },
                    "cursor_name": {"type": "string", "description": "Optional explicit cursor name for the first off-beat. Defaults to beat_<sha8>."},
                    "cursor_note": {"type": "string", "description": "Optional note attached to the registered cursor."},
                },
                "required": ["wave_path", "signal"],
            },
        ),

        Tool(
            name="suggest_handshakes",
            description=(
                "Scan a waveform and propose ready-to-use inspect_handshake bundles: "
                "it pairs *valid/*ready signals by scope and stem, finds the clock, and "
                "groups the channel payload buses (the signals that must hold steady "
                "during a stall). Use this BEFORE inspect_handshake so you don't have to "
                "hand-assemble {clock, valid, ready, payload} signal paths. Covers AXI "
                "*valid/*ready, generic valid/ready, and req/ack. It does NOT synthesise "
                "an AHB 'valid' (there is no literal valid signal — it is htrans != IDLE); "
                "for AHB pass valid manually. Reads existing waveforms only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "Waveform (FSDB or VCD)."},
                    "scope": {"type": "string", "description": "Optional hierarchy prefix to restrict candidates (e.g. 'tb_top.u_dut')."},
                    "max_candidates": {"type": "integer", "description": "Max bundles to return. Default 8.", "default": 8},
                },
                "required": ["wave_path"],
            },
        ),

        Tool(
            name="sweep_handshakes",
            description=(
                "Whole-design handshake anomaly sweep: discover EVERY valid/ready "
                "interface and inspect each over the window in one call, returning a "
                "comparative fact table (per-interface stalls, deadlock signature "
                "ended_in_stall, payload-hold, backpressure) ordered by a transparent "
                "mechanical key. Use on opaque global symptoms (timeout/hang) when you "
                "don't know which of many interfaces misbehaves — it collapses N "
                "suggest+inspect round-trips into one. Returns FACTS, not a root-cause "
                "verdict; re-rank as the symptom warrants. Reads existing waveforms only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "Waveform (FSDB or VCD)."},
                    "scope": {"type": "string", "description": "Optional hierarchy prefix to limit the sweep (e.g. 'tb_top.u_dut')."},
                    "edge": {"type": "string", "enum": ["posedge", "negedge"], "description": "Clock edge to sample on. Default posedge.", "default": "posedge"},
                    "start_time_ps": {"type": ["integer", "string"], "description": "Window start (ps int, '@cursor', or unit literal like '12.3ns'). Default 0.", "default": 0},
                    "end_time_ps": {"type": ["integer", "string"], "description": "Window end. -1 = end of trace. Accepts ps int, '@cursor', or unit literal.", "default": -1},
                    "max_wait_cycles": {"type": "integer", "description": "Stall length (cycles) above which a stall becomes a long_stall finding. Default 16.", "default": 16},
                    "max_interfaces": {"type": "integer", "description": "Max interfaces to sweep (default 64). If discovery exceeds this the result is flagged truncated=true — raise it for full coverage.", "default": 64},
                },
                "required": ["wave_path"],
            },
        ),

        Tool(
            name="inspect_handshake",
            description=(
                "Classify a clocked valid/ready handshake cycle-by-cycle and report "
                "protocol facts that leave no value pattern in scoreboard logs: stalls "
                "(valid high, ready low), the longest/over-threshold stall windows, "
                "backpressure imbalance (ready high, valid low), and — when payload "
                "signals are given — payload-hold violations (a payload that changes "
                "while the transfer is still stalled). Protocol-agnostic: AXI "
                "*valid/*ready, an AHB pair (ready=hready, valid=a 1-bit 'htrans!=IDLE' "
                "signal, payload=[htrans,haddr,hwrite,hsize] which must hold while "
                "hready is low), a generic valid-ready stream, or a credit interface. "
                "Auto-registers a cursor at the first problem (hold violation > long "
                "stall > longest stall). Reads existing waveforms only — does NOT rerun "
                "simulation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "Waveform (FSDB or VCD)."},
                    "clock": {"type": "string", "description": "1-bit clock signal full path."},
                    "valid": {"type": "string", "description": "Initiator valid/request signal (1-bit). Provide this OR valid_htrans."},
                    "valid_htrans": {"type": "string", "description": "AHB only: path to the htrans signal. A derived valid is computed from it (AHB has no literal valid). Provide this OR valid, not both."},
                    "htrans_rule": {
                        "type": "string",
                        "enum": ["active", "non_idle"],
                        "description": "How valid_htrans derives valid. 'active' (default) = NONSEQ/SEQ (htrans[1]==1); 'non_idle' = htrans != IDLE (counts BUSY too).",
                        "default": "active",
                    },
                    "ready": {"type": "string", "description": "Receiver ready/grant signal (1-bit). For AHB, hready."},
                    "payload": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional signals that MUST stay stable while stalled (e.g. AHB htrans/haddr/hwrite/hsize, AXI awaddr/awlen). A mid-stall change is a payload_hold_violation.",
                    },
                    "edge": {
                        "type": "string",
                        "enum": ["posedge", "negedge"],
                        "description": "Clock edge to sample on. Default posedge.",
                        "default": "posedge",
                    },
                    "start_time_ps": {"type": _TIMESPEC_TYPE, "description": "Window start. Default 0." + _TIMESPEC_HINT, "default": 0},
                    "end_time_ps": {"type": _TIMESPEC_TYPE, "description": "Window end. -1 means end of simulation." + _TIMESPEC_HINT, "default": -1},
                    "max_wait_cycles": {
                        "type": "integer",
                        "description": "A stall longer than this many cycles becomes a long_stall finding. Default 16.",
                        "default": 16,
                    },
                    "check_payload_hold": {
                        "type": "boolean",
                        "description": "Flag payload changes during a stall. Default true (only meaningful when payload is given).",
                        "default": True,
                    },
                    "active_high": {
                        "type": "boolean",
                        "description": "valid/ready are active-high. Set false for active-low handshakes. Default true.",
                        "default": True,
                    },
                    "cursor_name": {"type": "string", "description": "Optional explicit cursor name. Defaults to hs_<sha8>."},
                    "cursor_note": {"type": "string", "description": "Optional note attached to the registered cursor."},
                },
                "required": ["wave_path", "clock", "ready"],
            },
        ),

        # NOTE: diff_value_distribution is intentionally NOT registered as an
        # MCP tool. Its blind-A/B pilots (docs/auto-debug-v2-pilot-results.md)
        # showed no clear benefit over baseline on the common "scoreboard
        # data-mismatch + readable RTL" flow, so it is kept out of the tool
        # surface to avoid LLM selection noise. The implementation, schema and
        # tests are retained in src/verify_condition.py / schemas.py / tests so
        # it can be re-registered here in one block if a real use-case appears.
    ]
    # A/B harness toggle: hide the WHOLE handshake feature (both
    # suggest_handshakes and inspect_handshake) from list_tools so a cold
    # "baseline" session cannot see or be hinted by it. Enabled by either
    # TRACEWEAVE_AB_HIDE_HANDSHAKE=1 or the presence of the sentinel file
    # /tmp/tw_ab_hide_handshake (touch it + reconnect for Arm A, rm it +
    # reconnect for Arm B). Used only for the handshake blind A/B pilots; off by
    # default for normal operation.
    if os.environ.get("TRACEWEAVE_AB_HIDE_HANDSHAKE") == "1" or os.path.exists("/tmp/tw_ab_hide_handshake"):
        hidden = {"inspect_handshake", "suggest_handshakes", "sweep_handshakes"}
        return [t for t in _tools if t.name not in hidden]
    return _tools


# ═══════════════════════════════════════════════════════════════════
# Tool dispatch
# ═══════════════════════════════════════════════════════════════════

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    start = time.perf_counter()
    ok = True
    blocked = False
    text = ""
    try:
        result = await _dispatch(name, arguments)
        text = _serialize_result(result)
        ok = not isinstance(result, (schemas.ToolErrorResult, schemas.PrerequisiteBlockResult))
        blocked = isinstance(result, schemas.PrerequisiteBlockResult)
        return [TextContent(type="text", text=text)]
    except Exception as e:
        ok = False
        text = _serialize_result(_format_error(e))
        return [TextContent(type="text", text=text)]
    finally:
        latency_ms = (time.perf_counter() - start) * 1000.0
        case = None
        sim_state = _session_state.get("get_sim_paths")
        if isinstance(sim_state, dict) and sim_state.get("case_dir"):
            case = os.path.basename(str(sim_state["case_dir"]).rstrip("/"))
        usage_telemetry.record_call(
            name,
            arguments,
            result_bytes=len(text.encode("utf-8")),
            ok=ok,
            blocked=blocked,
            latency_ms=latency_ms,
            case=case,
        )


async def _dispatch(name: str, args: dict):
    block = _check_prerequisites(name)
    if block is not None:
        return schemas.PrerequisiteBlockResult.model_validate(block)

    if name == "get_sim_paths":
        result = discover_sim_paths(
            args["verif_root"],
            args.get("case_name"),
        )
        _update_session_state(name, args, result)
        validated = schemas.SimPathsResult.model_validate(result)
        _result_cache["get_sim_paths"] = validated
        _result_provenance["get_sim_paths"] = _build_result_provenance(name, args, validated)
        return validated

    elif name == "parse_sim_log":
        return _handle_parse_sim_log(args)

    elif name == "diff_sim_failure_results":
        simulator = _resolve_session_simulator(args)
        base_events, base_meta = _resolve_base_events_for_diff(args, simulator)
        new_events, new_meta = _resolve_new_events_for_diff(args, simulator)
        result = diff_failure_events(base_events, new_events)
        result.update({
            "base_log_file": base_meta.get("log_file"),
            "new_log_file": new_meta.get("log_file"),
            "base_snapshot_id": base_meta.get("snapshot_id"),
            "new_snapshot_id": new_meta.get("snapshot_id"),
            "diff_source": _diff_source(base_meta, new_meta),
        })
        return schemas.DiffResult.model_validate(result)

    elif name == "get_error_context":
        result = get_error_context(
            args["log_path"],
            line=args["line"],
            before=args.get("before", DEFAULT_LOG_CONTEXT_BEFORE),
            after=args.get("after", DEFAULT_LOG_CONTEXT_AFTER),
        )
        return schemas.ErrorContextResult.model_validate(result)

    elif name == "search_signals":
        wave_path  = args["wave_path"]
        keyword    = args["keyword"]
        max_r      = args.get("max_results", 50)
        ext = wave_path.lower().rsplit(".", 1)[-1]
        if ext == "fsdb":
            signature = _get_wave_signature(wave_path)
            cached = _fsdb_index_cache.get(wave_path)
            if cached is None or cached[0] != signature:
                if cached is not None:
                    _dispose_cached_object(cached[1])
                _fsdb_index_cache[wave_path] = (signature, FSDBSignalIndex(wave_path))
            result = _fsdb_index_cache[wave_path][1].search(keyword, max_r)
            return schemas.SearchSignalsResult.model_validate(result)
        elif ext == "vcd":
            result = _get_parser(wave_path).search_signals(keyword, max_r)
            return schemas.SearchSignalsResult.model_validate(result)
        else:
            raise ValueError(f"Unsupported format: .{ext}")

    elif name == "get_signal_at_time":
        result = _get_parser(args["wave_path"]).get_value_at_time(
            args["signal_path"], _resolve_time(args["time_ps"]),
        )
        return schemas.SignalAtTimeResult.model_validate(result)

    elif name == "get_signal_transitions":
        result = _get_parser(args["wave_path"]).get_transitions(
            args["signal_path"],
            _resolve_time(args.get("start_time_ps", 0)),
            _resolve_time(args.get("end_time_ps", -1), allow_sentinel=True),
        )
        return schemas.SignalTransitionsResult.model_validate(result)

    elif name == "get_signals_around_time":
        parser = _get_parser(args["wave_path"])
        center_ps = _resolve_time(args["center_time_ps"])
        window_ps = int(args.get("window_ps", DEFAULT_WAVE_WINDOW_PS))
        signal_paths = args.get("signal_paths") or []
        _validate_signals_around_time_args(
            parser, center_ps, window_ps, signal_paths
        )
        result = parser.get_signals_around_time(
            signal_paths,
            center_ps,
            window_ps,
            args.get("extra_transitions", DEFAULT_EXTRA_TRANSITIONS),
        )
        return schemas.SignalsAroundTimeResult.model_validate(result)

    elif name == "get_signals_by_cycle":
        requested_num_cycles = args.get("num_cycles", 16)
        effective_num_cycles = min(requested_num_cycles, MAX_CYCLES_PER_QUERY)
        result = get_signals_by_cycle(
            parser=_get_parser(args["wave_path"]),
            clock_path=args["clock_path"],
            signal_paths=args["signal_paths"],
            edge=args.get("edge", "posedge"),
            start_cycle=args.get("start_cycle", 0),
            num_cycles=effective_num_cycles,
            sample_offset_ps=args.get("sample_offset_ps", 1),
            requested_num_cycles=requested_num_cycles,
            capped=requested_num_cycles > MAX_CYCLES_PER_QUERY,
        )
        return schemas.GetSignalsByCycleResult.model_validate(result)

    elif name == "get_waveform_summary":
        result = _get_parser(args["wave_path"]).get_summary()
        return schemas.WaveformSummaryResult.model_validate(result)

    elif name == "build_tb_hierarchy":
        simulator = _resolve_session_simulator(args)
        resolved_args = {**args, "simulator": simulator}
        compile_log = args["compile_log"]
        full_result = build_hierarchy(
            parse_compile_log(
                compile_log,
                simulator,
            ),
            compile_log_path=compile_log,
        )
        _update_session_state(name, resolved_args, full_result)
        scan_call = None
        if _get_compatible_scan_cache(compile_log, simulator) is None:
            scan_call = _build_scan_required_next_call(compile_log, simulator)
        suggested = None
        if scan_call is not None:
            suggested = {
                **scan_call,
                "reason": (
                    "scan_structural_risks independently parses the same compile_log "
                    "to detect structural risks (slice_overlap, multi_drive, etc.). "
                    "Results feed into recommend_failure_debug_next_steps."
                ),
            }

        # Legacy escape hatch: temporary migration safety net. Remove once
        # the slim payload is proven across real cases.
        if os.environ.get("TRACEWEAVE_LEGACY_HIERARCHY_PAYLOAD") == "1":
            legacy = dict(full_result)
            legacy.pop("_scan_results", None)
            legacy["required_next_call"] = scan_call
            legacy["suggested_next"] = suggested
            validated = schemas.BuildTbHierarchyResultLegacy.model_validate(legacy)
            _result_cache["build_tb_hierarchy"] = validated
            _result_provenance["build_tb_hierarchy"] = _build_result_provenance(
                name, resolved_args, validated
            )
            return validated

        # Slim path. Register full result against a content-addressed
        # handle so handle tools (phase 4) can resolve later. The slim
        # payload is what crosses the wire to the LLM.
        handle = compute_handle(compile_log, simulator)
        _handle_store.register(handle, full_result)
        slim = build_slim_payload(full_result, handle, kdb_hint=None)
        slim["required_next_call"] = scan_call
        slim["suggested_next"] = suggested
        validated = schemas.BuildTbHierarchyResult.model_validate(slim)
        _result_cache["build_tb_hierarchy"] = validated
        _result_provenance["build_tb_hierarchy"] = _build_result_provenance(name, resolved_args, validated)
        return validated

    elif name == "scan_structural_risks":
        simulator = _resolve_session_simulator(args)
        resolved_args = {**args, "simulator": simulator}
        result = scan_structural_risks(
            compile_log=args["compile_log"],
            simulator=simulator,
            scan_scope=args.get("scan_scope", "scope1"),
            categories=args.get("categories"),
        )
        validated = _enforce_output_budget(
            schemas.ScanStructuralRisksResult.model_validate(result),
            [
                _shrink_scan_structural_risks_stage1,
                _shrink_scan_structural_risks_stage2,
                _shrink_scan_structural_risks_terminal,
            ],
        )
        _invalidate_downstream("scan_structural_risks")
        _result_cache["scan_structural_risks"] = validated
        _result_provenance["scan_structural_risks"] = _build_result_provenance(name, resolved_args, validated)
        return validated

    elif name == "analyze_failures":
        simulator = _resolve_session_simulator(args)
        request_context = _build_recommend_request_context(args)
        result = WaveformAnalyzer(
            log_path=args["log_path"],
            parser=_get_parser(args["wave_path"]),
            simulator=simulator,
        ).analyze(
            signal_paths=args["signal_paths"],
            group_index=args.get("group_index", 0),
            window_ps=args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
            extra_transitions = args.get("extra_transitions", DEFAULT_EXTRA_TRANSITIONS),
        )
        if _get_compatible_recommend_scan_cache(request_context) is None:
            original_guide = result.get("analysis_guide", {})
            result["analysis_guide"] = {
                "step0": "scan_structural_risks has not been run, so this analysis does not include structural risk correlation.",
                **original_guide,
            }
        return _enforce_output_budget(
            schemas.AnalyzeFailuresResult.model_validate(result),
            [
                _shrink_analyze_failures_stage1,
                _shrink_analyze_failures_stage2,
                _shrink_analyze_failures_terminal,
            ],
        )

    elif name == "analyze_failure_event":
        simulator = _resolve_session_simulator(args)
        result = WaveformAnalyzer(
            log_path=args["log_path"],
            parser=_get_parser(args["wave_path"]),
            simulator=simulator,
        ).analyze_failure_event(
            failure_event=args["failure_event"],
            wave_path=args["wave_path"],
            compile_log=args.get("compile_log"),
            top_hint=args.get("top_hint"),
        )
        return schemas.AnalyzeFailureEventResult.model_validate(result)

    elif name == "recommend_failure_debug_next_steps":
        simulator = _resolve_session_simulator(args)
        resolved_args = {**args, "simulator": simulator}
        request_context = _build_recommend_request_context(args)
        scan_cache = _get_compatible_recommend_scan_cache(request_context)
        parse_cache = _get_compatible_recommend_parse_cache(request_context)
        result = WaveformAnalyzer(
            log_path=args["log_path"],
            parser=_get_parser(args["wave_path"]),
            simulator=simulator,
        ).recommend_debug_next_steps(
            wave_path=args["wave_path"],
            compile_log=args.get("compile_log"),
            top_hint=args.get("top_hint"),
            structural_risks=[risk.model_dump() for risk in scan_cache.risks] if scan_cache is not None else None,
            problem_hints=parse_cache.problem_hints.model_dump() if parse_cache and parse_cache.problem_hints else None,
        )
        has_failure_context = False
        if parse_cache is not None:
            has_failure_context = parse_cache.runtime_total_errors > 0
        elif (
            result.get("primary_failure_target") is not None
            and result.get("suspected_failure_class") != "no_failure_detected"
        ):
            has_failure_context = True
        if scan_cache is None and has_failure_context:
            result["workflow_incomplete"] = True
            result["degraded_reason"] = "missing_structural_scan"
            result["required_next_call"] = _build_scan_required_next_call(
                request_context.get("compile_log"),
                request_context.get("simulator"),
            )
            result["missing_inputs"] = []
        else:
            result["workflow_incomplete"] = False
            result["degraded_reason"] = None
            result["required_next_call"] = None
        validated = schemas.RecommendNextStepsResult.model_validate(result)
        _result_cache["recommend_failure_debug_next_steps"] = validated
        _result_provenance["recommend_failure_debug_next_steps"] = _build_result_provenance(name, resolved_args, validated)
        return validated

    elif name == "get_diagnostic_snapshot":
        return _handle_diagnostic_snapshot(args)

    elif name == "explain_signal_driver":
        simulator = _resolve_session_simulator(args)
        backend_status = _safe_probe_backend(args["compile_log"], simulator)
        from src.connectivity_backend import select_backend  # noqa: PLC0415
        backend = select_backend(backend_status)
        result = backend.find_driver(
            signal_path=args["signal_path"],
            wave_path=args["wave_path"],
            compile_log=args["compile_log"],
            top_hint=args.get("top_hint"),
            recursive=args.get("recursive", False),
            max_depth=args.get("max_depth", 10),
            simulator=simulator,
        )
        backend_status = dict(backend_status)
        backend_status["backend"] = backend.name
        fallback_reason = result.get("_npi_fallback_reason")
        actual_backend = "static" if fallback_reason else backend.name
        backend_status["actual_backend"] = actual_backend
        if fallback_reason:
            backend_status["fallback_reason"] = fallback_reason
        if actual_backend == "verdi_npi":
            backend_status["parser_match"] = "exact"
        result.pop("_npi_fallback_reason", None)
        result["backend"] = actual_backend
        result["backend_status"] = backend_status
        return schemas.ExplainDriverResult.model_validate(result)

    elif name == "find_signal_loads":
        simulator = _resolve_session_simulator(args)
        backend_status = _safe_probe_backend(args["compile_log"], simulator)
        from src.connectivity_backend import select_backend  # noqa: PLC0415
        backend = select_backend(backend_status)
        result = backend.find_loads(
            signal_path=args["signal_path"],
            compile_log=args["compile_log"],
            top_hint=args.get("top_hint"),
            max_depth=args.get("max_depth", 1),
            include_expr=args.get("include_expr", True),
            kind_filter=args.get("kind_filter"),
            simulator=simulator,
        )
        # Reflect the backend that actually produced the result. NPI
        # backend tags every hop with backend='verdi_npi' on success,
        # 'static' on internal fallback. The status field surfaces the
        # active connectivity backend at the result envelope.
        backend_status = dict(backend_status)
        backend_status["backend"] = backend.name
        fallback_reason = result.get("_npi_fallback_reason")
        actual_backend = "static" if fallback_reason else backend.name
        backend_status["actual_backend"] = actual_backend
        if fallback_reason:
            backend_status["fallback_reason"] = fallback_reason
        if actual_backend == "verdi_npi":
            backend_status["parser_match"] = "exact"
        result.pop("_npi_fallback_reason", None)
        result["backend_status"] = backend_status
        return schemas.FindSignalLoadsResult.model_validate(result)

    elif name == "trace_signal_path":
        simulator = _resolve_session_simulator(args)
        backend_status = _safe_probe_backend(args["compile_log"], simulator)
        from src.connectivity_backend import select_backend  # noqa: PLC0415
        backend = select_backend(backend_status)
        result = backend.find_path(
            from_signal=args["from_signal"],
            to_signal=args["to_signal"],
            compile_log=args["compile_log"],
            top_hint=args.get("top_hint"),
            expand_assigns=args.get("expand_assigns", False),
            simulator=simulator,
        )
        backend_status = dict(backend_status)
        backend_status["backend"] = backend.name
        fallback_reason = result.get("_npi_fallback_reason")
        # Static returning static_backend_no_path_api is the expected
        # answer when NPI is unavailable, not a fallback. Only treat
        # _npi_fallback_reason (set by VerdiNpiBackend internals) as a
        # true fallback.
        actual_backend = "static" if fallback_reason else backend.name
        backend_status["actual_backend"] = actual_backend
        if fallback_reason:
            backend_status["fallback_reason"] = fallback_reason
        if actual_backend == "verdi_npi":
            backend_status["parser_match"] = "exact"
        result.pop("_npi_fallback_reason", None)
        result.pop("_npi_call_error", None)
        result["backend_status"] = backend_status
        return schemas.TraceSignalPathResult.model_validate(result)

    elif name == "build_kdb":
        from src.kdb_builder import build_kdb as _build_kdb
        simulator = _resolve_session_simulator(args)
        compile_log = args["compile_log"]
        cr = parse_compile_log(compile_log, simulator)
        result = _build_kdb(
            cr,
            top_hint=args.get("top_hint"),
            force_rebuild=bool(args.get("force_rebuild", False)),
        )
        # If the build succeeded (or cache-hit), wipe the verdi probe
        # cache so the next get_sim_paths / find_driver call picks up
        # the new KDB path.
        if result.get("status") in ("rebuilt", "cached"):
            _result_cache.pop("get_sim_paths", None)
        return result

    elif name == "trace_x_source":
        simulator = _resolve_session_simulator(args)
        result = trace_x_source(
            wave_path=args["wave_path"],
            signal_path=args["signal_path"],
            time_ps=_resolve_time(args["time_ps"]),
            compile_log=args["compile_log"],
            parser=_get_parser(args["wave_path"]),
            top_hint=args.get("top_hint"),
            max_depth=args.get("max_depth", DEFAULT_X_TRACE_MAX_DEPTH),
            simulator=simulator,
        )
        return schemas.TraceXSourceResult.model_validate(result)

    elif name == "cursor_set":
        ref = _cursor_store.set(
            args["name"],
            int(args["time_ps"]),
            note=args.get("note"),
        )
        return schemas.CursorSetResult.model_validate({"cursor": ref.as_dict()})

    elif name == "cursor_list":
        return schemas.CursorListResult.model_validate({
            "cursors": [ref.as_dict() for ref in _cursor_store.list()],
        })

    elif name == "cursor_delete":
        deleted = _cursor_store.delete(args["name"])
        return schemas.CursorDeleteResult.model_validate({
            "name": args["name"],
            "deleted": deleted,
        })

    elif name == "diff_first_divergence":
        result = diff_first_divergence(
            get_parser=_get_parser,
            wave_path_a=args["wave_path_a"],
            signal_a=args["signal_a"],
            wave_path_b=args["wave_path_b"],
            signal_b=args["signal_b"],
            start_ps=_resolve_time(args.get("start_time_ps", 0)),
            end_ps=_resolve_time(args.get("end_time_ps", -1), allow_sentinel=True),
            cursor_store=_cursor_store,
            cursor_name=args.get("cursor_name"),
            cursor_note=args.get("cursor_note"),
        )
        return schemas.DiffFirstDivergenceResult.model_validate(result)

    elif name == "period":
        result = period(
            get_parser=_get_parser,
            wave_path=args["wave_path"],
            signal=args["signal"],
            start_ps=_resolve_time(args.get("start_time_ps", 0)),
            end_ps=_resolve_time(args.get("end_time_ps", -1), allow_sentinel=True),
            edge=args.get("edge", "posedge"),
            tolerance_frac=args.get("tolerance_frac", 0.05),
            cursor_store=_cursor_store,
            cursor_name=args.get("cursor_name"),
            cursor_note=args.get("cursor_note"),
        )
        return schemas.PeriodResult.model_validate(result)

    elif name == "suggest_handshakes":
        result = suggest_handshakes(
            get_parser=_get_parser,
            wave_path=args["wave_path"],
            scope=args.get("scope"),
            max_candidates=args.get("max_candidates", 8),
        )
        return schemas.SuggestHandshakesResult.model_validate(result)

    elif name == "sweep_handshakes":
        result = sweep_handshake_anomalies(
            get_parser=_get_parser,
            wave_path=args["wave_path"],
            scope=args.get("scope"),
            edge=args.get("edge", "posedge"),
            start_ps=_resolve_time(args.get("start_time_ps", 0)),
            end_ps=_resolve_time(args.get("end_time_ps", -1), allow_sentinel=True),
            max_wait_cycles=args.get("max_wait_cycles", 16),
            max_interfaces=args.get("max_interfaces", 64),
            cursor_store=_cursor_store,
        )
        return schemas.HandshakeSweepResult.model_validate(result)

    elif name == "inspect_handshake":
        result = inspect_handshake(
            get_parser=_get_parser,
            wave_path=args["wave_path"],
            clock=args["clock"],
            valid=args.get("valid"),
            valid_htrans=args.get("valid_htrans"),
            htrans_rule=args.get("htrans_rule", "active"),
            ready=args["ready"],
            payload=args.get("payload"),
            edge=args.get("edge", "posedge"),
            start_ps=_resolve_time(args.get("start_time_ps", 0)),
            end_ps=_resolve_time(args.get("end_time_ps", -1), allow_sentinel=True),
            max_wait_cycles=args.get("max_wait_cycles", 16),
            check_payload_hold=args.get("check_payload_hold", True),
            active_high=args.get("active_high", True),
            cursor_store=_cursor_store,
            cursor_name=args.get("cursor_name"),
            cursor_note=args.get("cursor_note"),
        )
        return schemas.HandshakeInspectResult.model_validate(result)

    elif name in {
        "get_tb_subtree", "lookup_tb_files", "find_tb_instance",
        "get_tb_file_detail", "get_tb_class_hierarchy", "dump_tb_section",
    }:
        return _dispatch_handle_tool(name, args)

    else:
        raise ValueError(f"Unknown tool: {name}")


def _dispatch_handle_tool(name: str, args: dict):
    from src import handle_tools

    handle = args.get("handle") or ""
    full = _handle_store.resolve(handle)
    if full is None:
        return schemas.HandleErrorResult.model_validate({
            "error": "handle_expired",
            "hint": "the handle is unknown to this server — re-run build_tb_hierarchy",
            "current_handle": None,
        })

    if name == "get_tb_subtree":
        raw = handle_tools.get_tb_subtree(
            full, handle,
            root=args.get("root", ""),
            depth=args.get("depth", 1),
            max_nodes=args.get("max_nodes", 500),
        )
        return _wrap_handle_result(raw, schemas.GetTbSubtreeResult)

    if name == "lookup_tb_files":
        raw = handle_tools.lookup_tb_files(
            full, handle,
            basename=args.get("basename"),
            name_contains=args.get("name_contains"),
            path_contains=args.get("path_contains"),
            has_module=args.get("has_module"),
            contains_uvm=args.get("contains_uvm"),
            file_type=args.get("file_type"),
            limit=args.get("limit", 50),
        )
        return _wrap_handle_result(raw, schemas.LookupTbFilesResult)

    if name == "find_tb_instance":
        raw = handle_tools.find_tb_instance(
            full, handle,
            path=args.get("path"),
            module=args.get("module"),
            limit=args.get("limit", 100),
        )
        return _wrap_handle_result(raw, schemas.FindTbInstanceResult)

    if name == "get_tb_file_detail":
        raw = handle_tools.get_tb_file_detail(full, handle, path=args["path"])
        return _wrap_handle_result(raw, schemas.GetTbFileDetailResult)

    if name == "get_tb_class_hierarchy":
        raw = handle_tools.get_tb_class_hierarchy(
            full, handle,
            root_class=args.get("root_class"),
            depth=args.get("depth", -1),
        )
        return _wrap_handle_result(raw, schemas.GetTbClassHierarchyResult)

    if name == "dump_tb_section":
        raw = handle_tools.dump_tb_section(full, handle, section=args["section"])
        return _wrap_handle_result(raw, schemas.DumpTbSectionResult)

    raise ValueError(f"unhandled handle tool: {name}")


def _wrap_handle_result(raw: dict, result_schema):
    if "error" in raw:
        return schemas.HandleErrorResult.model_validate(raw)
    return result_schema.model_validate(raw)


def _truncate_failure_events_by_group(events: list[dict], max_per_group: int) -> list[dict]:
    counts: dict[str, int] = {}
    result: list[dict] = []
    for event in events:
        signature = event["group_signature"]
        count = counts.get(signature, 0)
        if count < max_per_group:
            result.append(event)
            counts[signature] = count + 1
    return result


# ── Diagnostic Snapshot helpers ──────────────────────────────────

def _extract_sim_paths_summary(result: schemas.SimPathsResult) -> dict:
    return {
        "verif_root": result.verif_root,
        "case_dir": result.case_dir,
        "simulator": result.simulator,
        "discovery_mode": result.discovery_mode,
        "compile_log_count": len(result.compile_logs),
        "sim_log_count": len(result.sim_logs),
        "wave_file_count": len(result.wave_files),
        "hints": result.hints,
    }


def _extract_hierarchy_summary(result: schemas.BuildTbHierarchyResult) -> dict:
    stats = getattr(result, "stats", {}) or {}
    return {
        "top_module": result.project.get("top_module"),
        "file_count": stats.get("file_count", 0),
        "uvm_file_count": stats.get("uvm_file_count", 0),
        "interface_count": stats.get("interface_count", len(result.interfaces)),
        "component_tree_depth": stats.get("tree_depth", 0),
    }


def _extract_log_summary(result: schemas.ParseSimLogResult) -> dict:
    summary = {
        "log_file": result.log_file,
        "runtime_total_errors": result.runtime_total_errors,
        "group_count": len(result.groups),
        "problem_hints": result.problem_hints.model_dump() if result.problem_hints else None,
        "first_group_signature": result.groups[0].signature if result.groups else None,
        "previous_log_detected": result.previous_log_detected,
    }
    if result.auto_diff is not None:
        summary.update(
            {
                "auto_diff_available": True,
                "auto_diff_resolved_count": len(result.auto_diff.resolved_events),
                "auto_diff_introduced_count": len(result.auto_diff.new_events),
            }
        )
    else:
        summary["auto_diff_available"] = False
    return summary


def _extract_structural_scan_summary(result: schemas.ScanStructuralRisksResult) -> dict:
    return {
        "files_scanned": result.files_scanned,
        "total_risks": result.total_risks,
        "high_risk_count": sum(1 for risk in result.risks if risk.risk_level == "high"),
    }


def _extract_recommend_summary(result: schemas.RecommendNextStepsResult) -> dict:
    return {
        "suspected_failure_class": result.suspected_failure_class,
        "failure_window_center_ps": result.failure_window_center_ps,
        "primary_failure_target": result.primary_failure_target,
        "signal_count": len(result.recommended_signals),
        "instance_count": len(result.recommended_instances),
    }


def _build_recommend_request_context(args: dict) -> dict[str, str | None]:
    hier_state = _session_state.get("build_tb_hierarchy") or {}
    sim_state = _session_state.get("get_sim_paths") or {}
    return {
        "log_path": args.get("log_path"),
        "wave_path": args.get("wave_path"),
        "simulator": _resolve_session_simulator(args) or sim_state.get("simulator"),
        "compile_log": args.get("compile_log") or hier_state.get("compile_log") or sim_state.get("compile_log"),
    }


def _same_realpath(path_a: str | None, path_b: str | None) -> bool:
    if not path_a or not path_b:
        return False
    return os.path.realpath(path_a) == os.path.realpath(path_b)


def _scan_request_is_compatible(
    compile_log: str | None,
    simulator: str | None,
    provenance: dict | None,
) -> bool:
    if provenance is None:
        return False
    if not _same_realpath(provenance.get("compile_log"), compile_log):
        return False
    provenance_simulator = provenance.get("simulator")
    if provenance_simulator not in {None, "auto"} and provenance_simulator != simulator:
        return False
    return True


def _build_scan_required_next_call(
    compile_log: str | None,
    simulator: str | None,
) -> dict[str, dict[str, str]] | None:
    if not compile_log or not simulator:
        return None
    return {
        "tool": "scan_structural_risks",
        "arguments": {
            "compile_log": compile_log,
            "simulator": simulator,
        },
    }


def _get_compatible_scan_cache(
    compile_log: str | None,
    simulator: str | None,
) -> schemas.ScanStructuralRisksResult | None:
    scan_cache = _result_cache.get("scan_structural_risks")
    provenance = _result_provenance.get("scan_structural_risks")
    if scan_cache is None:
        return None
    if not _scan_request_is_compatible(compile_log, simulator, provenance):
        return None
    return scan_cache


def _get_compatible_recommend_parse_cache(
    request_context: dict[str, str | None],
) -> schemas.ParseSimLogResult | None:
    parse_cache = _result_cache.get("parse_sim_log")
    provenance = _result_provenance.get("parse_sim_log")
    if parse_cache is None or provenance is None:
        return None
    if provenance.get("simulator") != request_context.get("simulator"):
        return None
    if not _same_realpath(provenance.get("log_path"), request_context.get("log_path")):
        return None
    return parse_cache


def _get_compatible_recommend_scan_cache(
    request_context: dict[str, str | None],
) -> schemas.ScanStructuralRisksResult | None:
    return _get_compatible_scan_cache(
        request_context.get("compile_log"),
        request_context.get("simulator"),
    )


def _build_result_provenance(tool_name: str, args: dict, result: schemas.SchemaModel) -> dict | None:
    if tool_name == "get_sim_paths":
        compile_log = None
        for entry in result.compile_logs:
            if entry.phase == "elaborate":
                compile_log = entry.path
                break
        if compile_log is None and result.compile_logs:
            compile_log = result.compile_logs[0].path
        return {
            "verif_root": result.verif_root,
            "case_dir": result.case_dir,
            "simulator": result.simulator,
            "compile_log": compile_log,
        }
    if tool_name == "build_tb_hierarchy":
        return {
            "compile_log": args.get("compile_log"),
            "simulator": args.get("simulator") or result.project.get("simulator") or "auto",
        }
    if tool_name == "scan_structural_risks":
        return {
            "compile_log": args.get("compile_log"),
            "simulator": _resolve_session_simulator(args),
        }
    if tool_name == "recommend_failure_debug_next_steps":
        log_path = args.get("log_path")
        log_mtime = None
        log_size = None
        if log_path:
            try:
                stat_result = os.stat(log_path)
                log_mtime = stat_result.st_mtime
                log_size = stat_result.st_size
            except OSError:
                pass
        return {
            "log_path": log_path,
            "wave_path": args.get("wave_path"),
            "simulator": _resolve_session_simulator(args),
            "compile_log": args.get("compile_log"),
            "log_mtime": log_mtime,
            "log_size": log_size,
        }
    return None


def _can_suggest_parse_sim_log(anchor: dict | None) -> bool:
    sim_result = _result_cache.get("get_sim_paths")
    return bool(anchor and anchor.get("simulator") and sim_result and sim_result.sim_logs)


def _can_suggest_recommend(anchor: dict | None) -> bool:
    sim_result = _result_cache.get("get_sim_paths")
    return bool(
        anchor
        and anchor.get("simulator")
        and anchor.get("compile_log")
        and sim_result
        and sim_result.sim_logs
        and sim_result.wave_files
        and _session_state.get("build_tb_hierarchy") is not None
    )


def _is_under_case_dir(path: str | None, case_dir: str | None) -> bool:
    if not path or not case_dir:
        return False
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(case_dir)]) == os.path.realpath(case_dir)
    except ValueError:
        return False


def _path_matches_session(path: str | None, candidates: list[str], case_dir: str | None) -> bool:
    if not path:
        return False
    real_path = os.path.realpath(path)
    if candidates:
        return real_path in {os.path.realpath(candidate) for candidate in candidates}
    return _is_under_case_dir(real_path, case_dir)


def _file_unchanged(provenance: dict, path_key: str, mtime_key: str, size_key: str) -> bool:
    """Return True when the file on disk still matches cached provenance."""
    fpath = provenance.get(path_key)
    expected_mtime = provenance.get(mtime_key)
    expected_size = provenance.get(size_key)
    if fpath is None or expected_mtime is None or expected_size is None:
        return True
    try:
        stat_result = os.stat(fpath)
    except OSError:
        return False
    return (
        stat_result.st_mtime == expected_mtime
        and stat_result.st_size == expected_size
    )


def _matches_anchor(tool_name: str, anchor: dict | None, provenance: dict | None) -> bool:
    if anchor is None or provenance is None:
        return False
    sim_result = _result_cache.get("get_sim_paths")
    sim_logs = [entry.path for entry in sim_result.sim_logs] if sim_result is not None else []
    wave_files = [entry.path for entry in sim_result.wave_files] if sim_result is not None else []
    case_dir = anchor.get("case_dir")
    if tool_name == "build_tb_hierarchy":
        return (
            provenance.get("compile_log") == anchor.get("compile_log")
            and provenance.get("simulator") == anchor.get("simulator")
        )
    if tool_name == "parse_sim_log":
        return (
            provenance.get("simulator") == anchor.get("simulator")
            and _path_matches_session(provenance.get("log_path"), sim_logs, case_dir)
            and _file_unchanged(provenance, "log_path", "log_mtime", "log_size")
        )
    if tool_name == "scan_structural_risks":
        return _scan_request_is_compatible(
            anchor.get("compile_log"),
            anchor.get("simulator"),
            provenance,
        )
    if tool_name == "recommend_failure_debug_next_steps":
        return (
            provenance.get("simulator") == anchor.get("simulator")
            and provenance.get("compile_log") == anchor.get("compile_log")
            and _path_matches_session(provenance.get("log_path"), sim_logs, case_dir)
            and _path_matches_session(provenance.get("wave_path"), wave_files, case_dir)
            and _file_unchanged(provenance, "log_path", "log_mtime", "log_size")
        )
    return False


def _handle_diagnostic_snapshot(args: dict) -> schemas.DiagnosticSnapshot:
    sections: dict[str, schemas.DiagnosticSnapshotSection] = {}
    quick_ref: dict[str, object] = {}
    missing_steps: list[dict] = []

    sim_result = _result_cache.get("get_sim_paths")
    anchor = _result_provenance.get("get_sim_paths")

    # Identity guard. The result cache is process-global and outlives a single
    # case (it self-heals only when get_sim_paths re-runs with a new identity).
    # This snapshot is documented to run *before* get_sim_paths, so a fresh
    # session targeting a new case would otherwise be handed the *previous*
    # case's paths/hierarchy/log as if they were current. When the caller names
    # its target (verif_root/case_dir) and the cache is for a different case,
    # present an honest cold start for the requested case rather than leaking
    # the stale one.
    requested_root = args.get("verif_root")
    requested_case = args.get("case_dir")
    case_mismatch = False
    if sim_result is not None:
        if requested_case and not _same_realpath(requested_case, sim_result.case_dir):
            case_mismatch = True
        if requested_root and not _same_realpath(requested_root, sim_result.verif_root):
            case_mismatch = True
    if case_mismatch:
        sim_result = None
        anchor = None

    if sim_result is not None:
        summary = _extract_sim_paths_summary(sim_result)
        # Served from a prior call without a confirming target: the snapshot
        # cannot prove this cache belongs to the case the caller is now
        # debugging. Flag it so a fresh session does not trust a stale case.
        if not (requested_root or requested_case):
            summary = {**(summary or {}), "carried_over": True}
        sections["sim_paths"] = schemas.DiagnosticSnapshotSection(
            available=True,
            summary=summary,
        )
        quick_ref["simulator"] = sim_result.simulator
        quick_ref["case_dir"] = sim_result.case_dir
    else:
        suggested = _build_suggested_call("get_sim_paths")
        if requested_root:
            suggested["arguments"]["verif_root"] = requested_root
        sections["sim_paths"] = schemas.DiagnosticSnapshotSection(
            available=False,
            suggested_call=suggested,
        )
        missing_steps.append({
            "tool": "get_sim_paths",
            "arguments": suggested["arguments"],
            "reason": (
                "Cached get_sim_paths is for a different case than the requested "
                "target; re-run get_sim_paths for the current case."
                if case_mismatch
                else "Path discovery has not run yet, so simulation artifacts cannot be located."
            ),
        })

    # Suggest build_kdb when the active simulator is Xcelium and the
    # probe positively confirms there is no KDB. We deliberately do
    # *not* surface this when the compile log fails to parse — that
    # signals a degraded probe, not a missing KDB.
    try:
        from config import AUTO_KDB_BUILD  # noqa: PLC0415
    except Exception:
        AUTO_KDB_BUILD = False
    if AUTO_KDB_BUILD and sim_result is not None and getattr(sim_result, "simulator", None) == "xcelium":
        cl_entries = getattr(sim_result, "compile_logs", []) or []
        compile_log_path = cl_entries[0].path if cl_entries else None
        if compile_log_path:
            try:
                _cr = parse_compile_log(compile_log_path, "xcelium")
                _probe = probe_verdi_backend(_cr, compile_log_path=compile_log_path)
                _probe_ok = True
            except Exception:
                _probe_ok = False
                _probe = {}
            if _probe_ok and not _probe.get("kdb_path"):
                missing_steps.append({
                    "tool": "build_kdb",
                    "arguments": {"compile_log": compile_log_path},
                    "reason": (
                        "Xcelium flow has no Verdi KDB yet; running build_kdb "
                        "produces one so the NPI backend can answer cross-hierarchy "
                        "driver/load queries."
                    ),
                })

    hier_result = None if case_mismatch else _result_cache.get("build_tb_hierarchy")
    if hier_result is not None:
        is_stale = anchor is not None and not _matches_anchor(
            "build_tb_hierarchy",
            anchor,
            _result_provenance.get("build_tb_hierarchy"),
        )
        sections["hierarchy"] = schemas.DiagnosticSnapshotSection(
            available=True,
            stale=is_stale,
            summary=_extract_hierarchy_summary(hier_result),
        )
        if not is_stale and anchor is not None:
            quick_ref["top_module"] = hier_result.project.get("top_module")
    else:
        sections["hierarchy"] = schemas.DiagnosticSnapshotSection(
            available=False,
            suggested_call=_build_suggested_call("build_tb_hierarchy") if anchor is not None else None,
        )
    if anchor is not None and (hier_result is None or sections["hierarchy"].stale):
        suggested = _build_suggested_call("build_tb_hierarchy")
        sections["hierarchy"].suggested_call = suggested
        missing_steps.append({
            "tool": "build_tb_hierarchy",
            "arguments": suggested["arguments"],
            "reason": "Hierarchy has not been built yet, so module and instance relationships are unknown.",
        })

    log_result = None if case_mismatch else _result_cache.get("parse_sim_log")
    compatible_log_result = None
    if log_result is not None:
        is_stale = anchor is not None and not _matches_anchor(
            "parse_sim_log",
            anchor,
            _result_provenance.get("parse_sim_log"),
        )
        sections["log_analysis"] = schemas.DiagnosticSnapshotSection(
            available=True,
            stale=is_stale,
            summary=_extract_log_summary(log_result),
        )
        if not is_stale and anchor is not None:
            quick_ref["total_errors"] = log_result.runtime_total_errors
            quick_ref["problem_hints"] = log_result.problem_hints
            compatible_log_result = log_result
    else:
        sections["log_analysis"] = schemas.DiagnosticSnapshotSection(available=False)
    if anchor is not None and (log_result is None or sections["log_analysis"].stale):
        suggested = _build_suggested_call("parse_sim_log") if _can_suggest_parse_sim_log(anchor) else None
        sections["log_analysis"].suggested_call = suggested
        missing_steps.append({
            "tool": "parse_sim_log",
            "arguments": suggested["arguments"] if suggested else {},
            "reason": "Simulation log analysis has not run yet, so failure information is unavailable.",
        })

    scan_result = None if case_mismatch else _result_cache.get("scan_structural_risks")
    compatible_hierarchy = bool(
        anchor is not None
        and hier_result is not None
        and not sections["hierarchy"].stale
    )
    compatible_scan_result = (
        _get_compatible_scan_cache(anchor.get("compile_log"), anchor.get("simulator"))
        if anchor is not None
        else None
    )
    if scan_result is not None:
        is_stale = anchor is not None and not _matches_anchor(
            "scan_structural_risks",
            anchor,
            _result_provenance.get("scan_structural_risks"),
        )
        sections["structural_scan"] = schemas.DiagnosticSnapshotSection(
            available=True,
            stale=is_stale,
            summary=_extract_structural_scan_summary(scan_result),
        )
    else:
        sections["structural_scan"] = None
    if anchor is not None and compatible_hierarchy and compatible_scan_result is None:
        has_failure_context = bool(
            compatible_log_result is not None
            and compatible_log_result.runtime_total_errors > 0
        )
        scan_call = _build_scan_required_next_call(
            anchor.get("compile_log"),
            anchor.get("simulator"),
        )
        missing_steps.append({
            "tool": "scan_structural_risks",
            "arguments": scan_call["arguments"] if scan_call else {},
            "reason": (
                "Structural scan is missing, so recommendation quality will be degraded."
                if has_failure_context
                else "Structural scan has not been run yet."
            ),
        })

    is_clean_run = (
        anchor is not None
        and log_result is not None
        and not sections["log_analysis"].stale
        and getattr(log_result, "runtime_total_errors", None) == 0
    )
    rec_result = None if case_mismatch else _result_cache.get("recommend_failure_debug_next_steps")
    if rec_result is not None:
        is_stale = anchor is not None and not _matches_anchor(
            "recommend_failure_debug_next_steps",
            anchor,
            _result_provenance.get("recommend_failure_debug_next_steps"),
        )
        sections["recommended_next"] = schemas.DiagnosticSnapshotSection(
            available=True,
            stale=is_stale,
            summary=_extract_recommend_summary(rec_result),
        )
        if not is_stale and anchor is not None:
            quick_ref["primary_failure_target"] = rec_result.primary_failure_target
            quick_ref["suspected_failure_class"] = rec_result.suspected_failure_class
            quick_ref["recommended_signals"] = rec_result.recommended_signals
    elif is_clean_run:
        sections["recommended_next"] = schemas.DiagnosticSnapshotSection(available=False)
    else:
        sections["recommended_next"] = schemas.DiagnosticSnapshotSection(available=False)
    if anchor is not None and not is_clean_run and (rec_result is None or sections["recommended_next"].stale):
        suggested = _build_suggested_call("recommend_failure_debug_next_steps") if _can_suggest_recommend(anchor) else None
        sections["recommended_next"].suggested_call = suggested
        missing_steps.append({
            "tool": "recommend_failure_debug_next_steps",
            "arguments": suggested["arguments"] if suggested else {},
            "reason": "Recommendation analysis has not run yet, so no prioritized debug target is available.",
        })

    if missing_steps:
        problem_hints = compatible_log_result.problem_hints if compatible_log_result is not None else None
        prioritize_scan = bool(
            problem_hints
            and (
                problem_hints.has_x
                or problem_hints.has_z
                or problem_hints.error_pattern in {"xprop", "mismatch"}
            )
        )
        workflow_order = {
            "get_sim_paths": 0,
            "build_tb_hierarchy": 1,
            "scan_structural_risks": 2,
            "parse_sim_log": 3,
            "recommend_failure_debug_next_steps": 4,
        }
        missing_steps.sort(
            key=lambda step: (
                0 if prioritize_scan and step["tool"] == "scan_structural_risks" else 1,
                workflow_order.get(step["tool"], 99),
            )
        )

    return schemas.DiagnosticSnapshot(
        sim_paths=sections["sim_paths"],
        hierarchy=sections["hierarchy"],
        log_analysis=sections["log_analysis"],
        structural_scan=sections["structural_scan"],
        recommended_next=sections["recommended_next"],
        missing_steps=missing_steps if missing_steps else None,
        **quick_ref,
    )


def _enforce_output_budget(
    model: schemas.TruncatableResult,
    shrink_stages: list[Callable[[schemas.TruncatableResult], schemas.TruncatableResult]],
) -> schemas.TruncatableResult:
    payload = model.model_dump_json(exclude_none=True)
    model.payload_bytes = len(payload)
    if model.payload_bytes <= schemas.TOKEN_BUDGET_SOFT_LIMIT:
        return model

    current = model
    for shrink in shrink_stages:
        current = shrink(current)
        current.auto_downgraded = True
        payload = current.model_dump_json(exclude_none=True)
        current.payload_bytes = len(payload)
        if current.payload_bytes <= schemas.TOKEN_BUDGET_SOFT_LIMIT:
            return current
    return current


def _shrink_parse_sim_log_stage1(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.ParseSimLogResult)
    groups = []
    for group in model.groups[:3]:
        payload = group.model_dump()
        payload["sample_message"] = payload["sample_message"][:40]
        groups.append(payload)
    return schemas.ParseSimLogResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "groups": groups,
            "max_groups": min(model.max_groups, len(groups)),
            "detail_level": "summary",
            "detail_hint": (
                "Call parse_sim_log with detail_level=\"full\" and max_groups=<n> "
                "for a targeted follow-up."
            ),
            "failure_events": [],
            "failure_events_returned": 0,
            "failure_events_truncated": model.failure_events_total > 0,
            "candidate_previous_logs": [],
            "first_group_context": None,
            "auto_diff": None,
        }
    )


def _shrink_parse_sim_log_stage2(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.ParseSimLogResult)
    groups = []
    if model.groups:
        payload = model.groups[0].model_dump()
        payload["sample_message"] = payload["sample_message"][:24]
        groups.append(payload)
    return schemas.ParseSimLogResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "groups": groups,
            "max_groups": min(model.max_groups, len(groups)),
            "detail_level": "summary",
            "detail_hint": "Call get_error_context or rerun parse_sim_log for a specific group.",
            "candidate_previous_logs": [],
            "first_group_context": None,
            "parser_capabilities": [],
            "auto_diff": None,
        }
    )


def _shrink_parse_sim_log_terminal(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.ParseSimLogResult)
    return schemas.ParseSimLogResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "groups": [],
            "max_groups": 0,
            "detail_level": "summary",
            "detail_hint": "Response truncated to fit budget. Re-run for one target group.",
            "failure_events": [],
            "failure_events_returned": 0,
            "failure_events_truncated": model.failure_events_total > 0,
            "candidate_previous_logs": [],
            "parser_capabilities": [],
            "first_group_context": None,
            "auto_diff": None,
        }
    )


def _trim_group_like_payload(group: dict | None, sample_limit: int) -> dict | None:
    if not isinstance(group, dict):
        return None
    trimmed = dict(group)
    sample_message = trimmed.get("sample_message")
    if isinstance(sample_message, str):
        trimmed["sample_message"] = sample_message[:sample_limit]
    return trimmed


def _trim_focused_event(event: dict | None, message_limit: int = 96) -> dict | None:
    if not isinstance(event, dict):
        return None
    allowed_keys = [
        "event_id",
        "group_signature",
        "time_ps",
        "source_file",
        "source_line",
        "instance_path",
        "mechanism",
        "log_phase",
        "time_parse_status",
        "value_repr",
        "message",
    ]
    trimmed = {key: event[key] for key in allowed_keys if key in event}
    if isinstance(trimmed.get("message"), str):
        trimmed["message"] = trimmed["message"][:message_limit]
    return trimmed


def _trim_analyze_summary(summary: dict, group_limit: int, sample_limit: int) -> dict:
    trimmed = dict(summary)
    groups = trimmed.get("groups")
    if isinstance(groups, list):
        trimmed["groups"] = [
            _trim_group_like_payload(group, sample_limit)
            for group in groups[:group_limit]
            if isinstance(group, dict)
        ]
    return trimmed


def _summarize_wave_context(wave_context: dict | None, signal_limit: int, transition_limit: int) -> dict | None:
    if not isinstance(wave_context, dict):
        return None
    trimmed = {
        key: value
        for key, value in wave_context.items()
        if key != "signals"
    }
    signals = wave_context.get("signals")
    if not isinstance(signals, dict):
        return trimmed
    trimmed_signals: dict[str, dict] = {}
    for signal_name, signal_payload in list(signals.items())[:signal_limit]:
        if not isinstance(signal_payload, dict):
            continue
        entry = dict(signal_payload)
        transitions = entry.get("transitions")
        if isinstance(transitions, list):
            entry["transitions"] = transitions[:transition_limit]
        trimmed_signals[signal_name] = entry
    trimmed["signals"] = trimmed_signals
    return trimmed


def _shrink_analyze_failures_stage1(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.AnalyzeFailuresResult)
    summary = _trim_analyze_summary(model.summary, group_limit=1, sample_limit=80)
    wave_context = _summarize_wave_context(model.wave_context, signal_limit=1, transition_limit=4)
    log_context = model.log_context
    if isinstance(log_context, dict) and isinstance(log_context.get("context"), str):
        log_context = {
            **log_context,
            "context": log_context["context"][:400],
        }
    return schemas.AnalyzeFailuresResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "detail_hint": (
                "Narrow signal_paths or inspect a single failure group to get the full waveform payload."
            ),
            "summary": summary,
            "focused_group": _trim_group_like_payload(model.focused_group, 80),
            "focused_event": _trim_focused_event(model.focused_event, 96),
            "log_context": log_context,
            "wave_context": wave_context,
            "signals_queried": (model.signals_queried or [])[:2],
        }
    )


def _shrink_analyze_failures_stage2(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.AnalyzeFailuresResult)
    summary = _trim_analyze_summary(model.summary, group_limit=1, sample_limit=32)
    return schemas.AnalyzeFailuresResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "detail_hint": "Response truncated. Re-run analyze_failures for one group and fewer signals.",
            "summary": summary,
            "focused_group": _trim_group_like_payload(model.focused_group, 32),
            "focused_event": _trim_focused_event(model.focused_event, 48),
            "log_context": None,
            "wave_context": None,
            "signals_queried": (model.signals_queried or [])[:1],
            "analysis_guide": {
                "step1": "Re-run analyze_failures with a single target signal for full context.",
            },
        }
    )


def _shrink_analyze_failures_terminal(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.AnalyzeFailuresResult)
    summary = {
        "runtime_total_errors": model.summary.get("runtime_total_errors"),
        "total_groups": model.summary.get("total_groups"),
        "truncated": True,
    }
    return schemas.AnalyzeFailuresResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "detail_level": "summary",
            "detail_hint": "Response truncated to fit budget. Re-run analyze_failures for one group.",
            "summary": summary,
            "focused_group": None,
            "focused_event": None,
            "log_context": None,
            "wave_context": None,
            "signals_queried": [],
            "analysis_guide": {
                "step1": "Re-run analyze_failures with one group_index and one signal_path.",
            },
        }
    )


def _truncate_risk_payload(risk: schemas.StructuralRisk, detail_limit: int, evidence_limit: int) -> dict:
    payload = risk.model_dump()
    payload["detail"] = payload["detail"][:detail_limit]
    payload["evidence"] = payload["evidence"][:evidence_limit]
    return payload


def _shrink_scan_structural_risks_stage1(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.ScanStructuralRisksResult)
    risks = [_truncate_risk_payload(risk, detail_limit=120, evidence_limit=2) for risk in model.risks[:10]]
    return schemas.ScanStructuralRisksResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "detail_hint": "Re-run scan_structural_risks with narrower categories if you need the full risk list.",
            "risks": risks,
            "total_risks": model.total_risks,
            "skipped_files": [],
        }
    )


def _shrink_scan_structural_risks_stage2(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.ScanStructuralRisksResult)
    risks = [_truncate_risk_payload(risk, detail_limit=64, evidence_limit=0) for risk in model.risks[:3]]
    return schemas.ScanStructuralRisksResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "detail_hint": "Response truncated. Re-run scan_structural_risks with narrower categories.",
            "risks": risks,
            "categories_scanned": model.categories_scanned[:3],
            "skipped_files": [],
        }
    )


def _shrink_scan_structural_risks_terminal(model: schemas.TruncatableResult) -> schemas.TruncatableResult:
    assert isinstance(model, schemas.ScanStructuralRisksResult)
    return schemas.ScanStructuralRisksResult.model_validate(
        {
            **model.model_dump(exclude_none=True),
            "detail_level": "summary",
            "detail_hint": "Response truncated to fit budget. Re-run scan_structural_risks with one category.",
            "risks": [],
            "categories_scanned": model.categories_scanned[:3],
            "skipped_files": [],
        }
    )


def _handle_parse_sim_log(args: dict) -> schemas.ParseSimLogResult:
    prev_provenance = _result_provenance.get("parse_sim_log")
    simulator = _resolve_session_simulator(args)
    stat_info = _log_stat_info(args["log_path"])
    log_mtime = stat_info["mtime"]
    log_size = stat_info["size"]
    parser = SimLogParser(args["log_path"], simulator)
    summary = parser.parse(max_groups=args.get("max_groups", DEFAULT_MAX_GROUPS))
    detail_level = args.get("detail_level", DEFAULT_DETAIL_LEVEL)
    max_events_per_group = args.get("max_events_per_group", DEFAULT_MAX_EVENTS_PER_GROUP)

    if detail_level not in {"summary", "compact", "full"}:
        raise ValueError("detail_level must be one of: summary, compact, full")
    if max_events_per_group <= 0:
        raise ValueError("max_events_per_group must be greater than 0")

    allowed_signatures = {group["signature"] for group in summary.get("groups", [])}
    all_events = parser.parse_failure_events()
    log_snapshot_id = _capture_log_snapshot(args["log_path"], simulator, all_events, stat_info)
    previous_snapshot = _find_previous_log_snapshot(
        args["log_path"],
        simulator,
        exclude_snapshot_id=log_snapshot_id,
    )

    if detail_level == "summary":
        total = len(all_events)
        returned_events = []
        summary["detail_hint"] = (
            'Call parse_sim_log with detail_level="full" and max_groups=<n> '
            "for a specific follow-up."
        )
    else:
        scoped_events = [
            event for event in all_events
            if event["group_signature"] in allowed_signatures
        ]
        total = len(scoped_events)
        if detail_level == "full" and total <= AUTO_DOWNGRADE_THRESHOLD:
            returned_events = scoped_events
        else:
            returned_events = _truncate_failure_events_by_group(scoped_events, max_events_per_group)
            if detail_level == "full" and total > AUTO_DOWNGRADE_THRESHOLD:
                summary["auto_downgraded"] = True

    first_group_context = None
    groups = summary.get("groups", [])
    if groups:
        first_line = groups[0].get("first_line")
        if isinstance(first_line, int) and first_line > 0:
            try:
                context = get_error_context(
                    args["log_path"],
                    first_line,
                    before=DEFAULT_LOG_CONTEXT_BEFORE,
                    after=DEFAULT_LOG_CONTEXT_AFTER,
                )
                first_group_context = schemas.ErrorContextResult.model_validate(context)
            except Exception:
                first_group_context = None

    summary["detail_level"] = detail_level
    summary["auto_downgraded"] = False
    summary["failure_events"] = returned_events
    summary["failure_events_total"] = total
    summary["failure_events_returned"] = len(returned_events)
    summary["failure_events_truncated"] = len(returned_events) < total
    summary["first_group_context"] = first_group_context
    problem_hints = compute_problem_hints(summary, all_events)
    summary["problem_hints"] = problem_hints
    summary["log_snapshot_id"] = log_snapshot_id
    summary["previous_log_snapshot_id"] = (
        previous_snapshot.get("snapshot_id") if previous_snapshot is not None else None
    )
    grouped_events: dict[str, list[dict]] = {}
    for event in all_events:
        grouped_events.setdefault(event["group_signature"], []).append(event)
    for group in summary.get("groups", []):
        group["xprop_priority"] = compute_xprop_priority_for_group(
            grouped_events.get(group["signature"], []),
            problem_hints.has_x,
            problem_hints.has_z,
        )

    auto_diff = None
    if (
        prev_provenance is not None
        and isinstance(prev_provenance.get("all_failure_events"), list)
        and prev_provenance.get("simulator") == simulator
        and _same_realpath(prev_provenance.get("log_path"), args["log_path"])
        and (
            prev_provenance.get("log_mtime") != log_mtime
            or prev_provenance.get("log_size") != log_size
        )
    ):
        auto_diff = diff_failure_events(
            prev_provenance["all_failure_events"],
            all_events,
        )
    summary["auto_diff"] = auto_diff

    validated = _enforce_output_budget(
        schemas.ParseSimLogResult.model_validate(summary),
        [
            _shrink_parse_sim_log_stage1,
            _shrink_parse_sim_log_stage2,
            _shrink_parse_sim_log_terminal,
        ],
    )
    _invalidate_downstream("parse_sim_log")
    _result_cache["parse_sim_log"] = validated
    _result_provenance["parse_sim_log"] = {
        "log_path": validated.log_file,
        "simulator": validated.simulator,
        "all_failure_events": all_events,
        "log_mtime": log_mtime,
        "log_mtime_ns": stat_info["mtime_ns"],
        "log_size": log_size,
        "log_snapshot_id": log_snapshot_id,
        "previous_log_snapshot_id": summary["previous_log_snapshot_id"],
    }
    return validated


def _serialize_result(result: BaseModel | dict) -> str:
    if isinstance(result, BaseModel):
        return result.model_dump_json(indent=2, exclude_none=True)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _format_error(exc: Exception) -> schemas.ToolErrorResult:
    message = str(exc)
    if "FSDB parsing unavailable" in message:
        return schemas.ToolErrorResult.model_validate({
            "error": message,
            "error_code": "fsdb_runtime_unavailable",
            "fsdb_runtime": get_fsdb_runtime_info(),
            "fallback": {
                "supported_wave_formats": ["vcd"],
                "action": "prefer_vcd_waveforms",
            },
        })
    return schemas.ToolErrorResult.model_validate({"error": message})


# ═══════════════════════════════════════════════════════════════════
# Entry
# ═══════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream,
                      app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
