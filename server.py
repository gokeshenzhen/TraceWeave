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
import json
import sys
import os

# Ensure the TraceWeave repo root is on the Python path.
sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from config import (
    AUTO_DOWNGRADE_THRESHOLD,
    DEFAULT_DETAIL_LEVEL,
    DEFAULT_EXTRA_TRANSITIONS, DEFAULT_LOG_CONTEXT_AFTER, DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_MAX_EVENTS_PER_GROUP,
    MAX_CYCLES_PER_QUERY,
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
from src.path_discovery import discover_sim_paths
from src.problem_hints import compute_problem_hints, compute_xprop_priority_for_group
from src.tb_hierarchy_builder import build_hierarchy
from src.signal_driver import explain_signal_driver
from src.structural_scanner import ALL_CATEGORIES, scan_structural_risks
from src.x_trace import trace_x_source
from src.cycle_query import get_signals_by_cycle
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
    "trace_x_source": ["build_tb_hierarchy"],
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


def _clear_result_state():
    for key in _result_cache:
        _result_cache[key] = None
    for key in _result_provenance:
        _result_provenance[key] = None


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
     The returned file list represents the ONLY files compiled in this session.
     Use this file list to scope all subsequent source reads — do NOT use find/grep to scan directories for source files.
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
   - For large error counts (>100), use detail_level="summary" first, then inspect specific groups with get_error_context or detail_level="full".
   - Default detail_level is "summary" to keep MCP responses below harness budget.

4. Call recommend_failure_debug_next_steps to get a default target and role-ranked signals.

5. Call search_signals to confirm full hierarchical signal paths when needed.
   - Derive keywords from build_tb_hierarchy output, error messages, recommend_failure_debug_next_steps, or RTL source.
   - When reading RTL source, only read files listed in build_tb_hierarchy results.

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


# ═══════════════════════════════════════════════════════════════════
# Tool definitions
# ═══════════════════════════════════════════════════════════════════

@app.list_tools()
async def list_tools():
    return [

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
                "X/Z presence, first-failure timing, and a convergence summary."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "base_log_path": {"type": "string", "description": "Baseline simulation log"},
                    "new_log_path": {"type": "string", "description": "New simulation log"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                },
                "required": ["base_log_path", "new_log_path", "simulator"],
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
                    "time_ps":     {"type": "integer", "description": "Query time in ps"},
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
                    "start_time_ps": {"type": "integer", "default": 0},
                    "end_time_ps":   {"type": "integer", "default": -1,
                                      "description": "-1 means through the end of simulation"},
                },
                "required": ["wave_path", "signal_path"],
            },
        ),

        Tool(
            name="get_signals_around_time",
            description=(
                "Return values and transitions for multiple signals around a target time window. "
                "Commonly used to inspect signal context around a known failure timestamp. "
                "FSDB support depends on fsdb_runtime.enabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_paths":  {"type": "array", "items": {"type": "string"},
                                      "description": "List of full hierarchical signal paths"},
                    "center_time_ps":{"type": "integer", "description": "Center time in ps, usually the failure timestamp"},
                    "window_ps":     {"type": "integer",
                                      "description": f"Window size before and after the center time in ps. Default: {DEFAULT_WAVE_WINDOW_PS}",
                                      "default": DEFAULT_WAVE_WINDOW_PS},
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
                "Extract user files from a compile or elaborate log, scan source files, and build the full testbench hierarchy. "
                "Returns top module, file grouping, component tree, class hierarchy, and interfaces."
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
                "Returns availability status, compact summaries, and suggested calls for missing steps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {
                        "type": "string",
                        "description": (
                            "Absolute path to the project's verif/ directory. "
                            "Used only to build a suggested_call when get_sim_paths has not run yet."
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
                "Set recursive=true to walk multiple hops upstream across instance boundaries."
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
                    "time_ps": {"type": "integer"},
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
    ]


# ═══════════════════════════════════════════════════════════════════
# Tool dispatch
# ═══════════════════════════════════════════════════════════════════

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=_serialize_result(result))]
    except Exception as e:
        return [TextContent(type="text", text=_serialize_result(_format_error(e)))]


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
        result = SimLogParser(
            args["base_log_path"],
            simulator,
        ).diff_against(args["new_log_path"])
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
            args["signal_path"], args["time_ps"]
        )
        return schemas.SignalAtTimeResult.model_validate(result)

    elif name == "get_signal_transitions":
        result = _get_parser(args["wave_path"]).get_transitions(
            args["signal_path"],
            args.get("start_time_ps", 0),
            args.get("end_time_ps", -1),
        )
        return schemas.SignalTransitionsResult.model_validate(result)

    elif name == "get_signals_around_time":
        result = _get_parser(args["wave_path"]).get_signals_around_time(
            args["signal_paths"],
            args["center_time_ps"],
            args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
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
        result = build_hierarchy(
            parse_compile_log(
                args["compile_log"],
                simulator,
            )
        )
        _update_session_state(name, resolved_args, result)
        scan_call = None
        compile_log = args.get("compile_log")
        if _get_compatible_scan_cache(compile_log, simulator) is None:
            scan_call = _build_scan_required_next_call(compile_log, simulator)
        result["required_next_call"] = scan_call
        result["suggested_next"] = None
        if scan_call is not None:
            result["suggested_next"] = {
                **scan_call,
                "reason": (
                    "scan_structural_risks independently parses the same compile_log "
                    "to detect structural risks (slice_overlap, multi_drive, etc.). "
                    "Results feed into recommend_failure_debug_next_steps."
                ),
            }
        validated = schemas.BuildTbHierarchyResult.model_validate(result)
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
        result = explain_signal_driver(
            signal_path=args["signal_path"],
            wave_path=args["wave_path"],
            compile_log=args["compile_log"],
            top_hint=args.get("top_hint"),
            recursive=args.get("recursive", False),
            max_depth=args.get("max_depth", 10),
            simulator=simulator,
        )
        return schemas.ExplainDriverResult.model_validate(result)

    elif name == "trace_x_source":
        simulator = _resolve_session_simulator(args)
        result = trace_x_source(
            wave_path=args["wave_path"],
            signal_path=args["signal_path"],
            time_ps=args["time_ps"],
            compile_log=args["compile_log"],
            parser=_get_parser(args["wave_path"]),
            top_hint=args.get("top_hint"),
            max_depth=args.get("max_depth", DEFAULT_X_TRACE_MAX_DEPTH),
            simulator=simulator,
        )
        return schemas.TraceXSourceResult.model_validate(result)

    else:
        raise ValueError(f"Unknown tool: {name}")


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
    return {
        "top_module": result.project.get("top_module"),
        "rtl_file_count": len(result.files.get("rtl", [])),
        "tb_file_count": len(result.files.get("tb", [])),
        "interface_count": len(result.interfaces),
        "component_tree_depth": _tree_depth(result.component_tree),
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


def _tree_depth(tree: dict, _current: int = 0) -> int:
    if not tree:
        return _current

    depths = []
    for payload in tree.values():
        children = payload.get("children", {})
        if isinstance(children, dict) and children:
            depths.append(_tree_depth(children, _current + 1))
        elif isinstance(children, list) and children:
            depths.append(max(_tree_depth(child, _current + 1) for child in children))
        else:
            depths.append(_current + 1)
    return max(depths, default=_current)


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
    if sim_result is not None:
        sections["sim_paths"] = schemas.DiagnosticSnapshotSection(
            available=True,
            summary=_extract_sim_paths_summary(sim_result),
        )
        quick_ref["simulator"] = sim_result.simulator
        quick_ref["case_dir"] = sim_result.case_dir
    else:
        suggested = _build_suggested_call("get_sim_paths")
        if args.get("verif_root"):
            suggested["arguments"]["verif_root"] = args["verif_root"]
        sections["sim_paths"] = schemas.DiagnosticSnapshotSection(
            available=False,
            suggested_call=suggested,
        )
        missing_steps.append({
            "tool": "get_sim_paths",
            "arguments": suggested["arguments"],
            "reason": "Path discovery has not run yet, so simulation artifacts cannot be located.",
        })

    hier_result = _result_cache.get("build_tb_hierarchy")
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

    log_result = _result_cache.get("parse_sim_log")
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

    scan_result = _result_cache.get("scan_structural_risks")
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
    rec_result = _result_cache.get("recommend_failure_debug_next_steps")
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
    log_mtime = os.path.getmtime(args["log_path"])
    log_size = os.path.getsize(args["log_path"])
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
        "log_size": log_size,
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
