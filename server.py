#!/usr/bin/env python3
"""
Waveform Analysis MCP Server
用于支持 MCP 的调试客户端（例如 Codex、Claude Code）

本服务围绕波形调试工作流提供 MCP tools，包括：
- 路径发现和 session/workflow gate
- compile/sim log 解析与 failure normalization
- testbench hierarchy、源码/driver 关联分析
- VCD/FSDB 波形查询与信号搜索
- failure recommendation、结构风险扫描和 X/Z trace
"""

import asyncio
import json
import sys
import os

# 确保 waveform_mcp/ 目录在 Python 路径中
sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from config import (
    AUTO_DOWNGRADE_THRESHOLD,
    DEFAULT_DETAIL_LEVEL,
    DEFAULT_EXTRA_TRANSITIONS, DEFAULT_LOG_CONTEXT_AFTER, DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_MAX_EVENTS_PER_GROUP,
    DEFAULT_MAX_GROUPS, DEFAULT_WAVE_WINDOW_PS,
    DEFAULT_X_TRACE_MAX_DEPTH,
)
from src.log_parser import SimLogParser, get_error_context
from src.vcd_parser import VCDParser
from src.fsdb_parser import FSDBParser
from src.fsdb_signal_index import FSDBSignalIndex
from src.analyzer import WaveformAnalyzer
from src.compile_log_parser import parse_compile_log
from src.path_discovery import discover_sim_paths
from src.problem_hints import compute_problem_hints
from src.tb_hierarchy_builder import build_hierarchy
from src.signal_driver import explain_signal_driver
from src.structural_scanner import ALL_CATEGORIES, scan_structural_risks
from src.x_trace import trace_x_source
from config import get_fsdb_runtime_info
from pydantic import BaseModel
import src.schemas as schemas


# ── Session 状态机：工作流前置条件门禁 ──────────────────────────────
_session_state: dict[str, dict | None] = {
    "get_sim_paths": None,
    "build_tb_hierarchy": None,
}

_result_cache: dict[str, schemas.SchemaModel | None] = {
    "get_sim_paths": None,
    "build_tb_hierarchy": None,
    "parse_sim_log": None,
    "recommend_failure_debug_next_steps": None,
}

_result_provenance: dict[str, dict | None] = {
    "get_sim_paths": None,
    "build_tb_hierarchy": None,
    "parse_sim_log": None,
    "recommend_failure_debug_next_steps": None,
}

_DOWNSTREAM_DEPS: dict[str, list[str]] = {
    "get_sim_paths": ["build_tb_hierarchy", "parse_sim_log", "recommend_failure_debug_next_steps"],
    "build_tb_hierarchy": ["recommend_failure_debug_next_steps"],
    "parse_sim_log": ["recommend_failure_debug_next_steps"],
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


def _update_session_state(tool_name: str, args: dict, result: dict):
    _invalidate_downstream(tool_name)
    if tool_name == "get_sim_paths":
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
        _session_state["build_tb_hierarchy"] = {
            "compile_log": args.get("compile_log"),
            "simulator": args.get("simulator", "auto"),
        }


def reset_session_state():
    _session_state["get_sim_paths"] = None
    _session_state["build_tb_hierarchy"] = None
    for key in _result_cache:
        _result_cache[key] = None
    for key in _result_provenance:
        _result_provenance[key] = None


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

2. MUST call build_tb_hierarchy before reading any RTL/TB source files or analyzing failures.
   - Use the elaborate-phase compile_log and simulator from step 1.
   - The returned file list represents the ONLY files compiled in this session.
   - Use this file list to scope all subsequent source reads — do NOT use find/grep to scan directories for source files.
   - Immediately after build_tb_hierarchy, call scan_structural_risks with the same compile_log
     and simulator. Do not wait for parse_sim_log results before calling it.
     Structural risks that overlap with failing signal paths are high-priority root cause candidates.

3. Call parse_sim_log with sim_logs[0].path and simulator from step 1 when sim_logs is non-empty.
   - Prefer normalized failure_events[].time_ps over re-parsing raw message text.
   - Use grouped errors to choose the first group_index to inspect.
   - first_group_context contains ~200 lines of raw log text around the first error.
     Use get_error_context only for other groups.
   - If previous_log_detected is true, consider diff_sim_failure_results early.
   - For large error counts (>100), use detail_level="summary" first, then inspect specific groups with get_error_context or detail_level="compact".
   - Default detail_level is "compact" which limits failure_events per group for manageable output.

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
   - get_error_context for other groups
   - get_signal_transitions for longer history
   - get_signals_around_time for additional signals
   - get_signal_at_time for exact values
   - get_waveform_summary for waveform sanity checks

8. Call get_diagnostic_snapshot at any time to check workflow state.
   - Does NOT execute any sub-steps; only reads cached results
""".strip()

app = Server("waveform-mcp", instructions=SERVER_INSTRUCTIONS)

# ── 全局缓存 ──────────────────────────────────────────────────────
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
    """返回缓存的 parser 实例，避免 VCD 重复解析 / FSDB 重复打开"""
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
        raise ValueError(f"不支持的波形格式: .{ext}")
    _parser_cache[wave_path] = (signature, parser)
    return parser


# ═══════════════════════════════════════════════════════════════════
# Tool 定义
# ═══════════════════════════════════════════════════════════════════

@app.list_tools()
async def list_tools():
    return [

        Tool(
            name="get_sim_paths",
            description=(
                "自动发现 verif 目录下的编译日志、仿真日志和波形文件。"
                "case_name 可选；省略时返回可用 case 列表。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {"type": "string",
                                   "description": "项目的 verif/ 目录绝对路径，如 /home/robin/Projects/i2c_lib/verif"},
                    "case_name":  {"type": "string",
                                   "description": "可选，case 名称，如 case0（对应 make SV_CASE=case0）"},
                },
                "required": ["verif_root"],
            },
        ),

        Tool(
            name="parse_sim_log",
            description=(
                "解析 VCS 或 Xcelium 仿真 log，返回按 signature 分组的报错摘要。"
                "simulator 必传，不再自动识别。"
                "自动附带首个 error group 前后各 100 行的 log context（first_group_context 字段），"
                "其余 group 按需调 get_error_context。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":  {"type": "string", "description": "仿真 log 文件绝对路径（irun.log）"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                    "max_groups": {
                        "type": "integer",
                        "description": f"最多返回多少个 error group，默认 {DEFAULT_MAX_GROUPS}",
                        "default": DEFAULT_MAX_GROUPS,
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["summary", "compact", "full"],
                        "description": f"返回详细程度，默认 {DEFAULT_DETAIL_LEVEL}",
                        "default": DEFAULT_DETAIL_LEVEL,
                    },
                    "max_events_per_group": {
                        "type": "integer",
                        "description": f"compact/full 降级时每个 group 最多返回几条 failure_event，默认 {DEFAULT_MAX_EVENTS_PER_GROUP}",
                        "default": DEFAULT_MAX_EVENTS_PER_GROUP,
                    },
                },
                "required": ["log_path", "simulator"],
            },
        ),

        Tool(
            name="diff_sim_failure_results",
            description=(
                "比较两次仿真 log 的标准化 failure_event，"
                "输出已解决、持续存在和新增的失败。"
                "增强输出包含：问题类型变化、X/Z 消失/出现、"
                "首次失败时间移动、收敛趋势总结。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "base_log_path": {"type": "string", "description": "基线仿真 log"},
                    "new_log_path": {"type": "string", "description": "新仿真 log"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                },
                "required": ["base_log_path", "new_log_path", "simulator"],
            },
        ),

        Tool(
            name="get_error_context",
            description=(
                "根据报错行号，从仿真 log 中提取前后 N 行原始文本。"
                "通常配合 parse_sim_log 返回的 first_line 使用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path": {"type": "string", "description": "仿真 log 文件绝对路径（irun.log）"},
                    "line": {"type": "integer", "description": "中心报错行号"},
                    "before": {
                        "type": "integer",
                        "description": f"向前取多少行，默认 {DEFAULT_LOG_CONTEXT_BEFORE}",
                        "default": DEFAULT_LOG_CONTEXT_BEFORE,
                    },
                    "after": {
                        "type": "integer",
                        "description": f"向后取多少行，默认 {DEFAULT_LOG_CONTEXT_AFTER}",
                        "default": DEFAULT_LOG_CONTEXT_AFTER,
                    },
                },
                "required": ["log_path", "line"],
            },
        ),

        Tool(
            name="search_signals",
            description=(
                "在波形文件（FSDB/VCD）中搜索包含关键字的信号，返回完整层级路径。"
                "当客户端已知信号名但不知道完整层级路径时使用。"
                "FSDB 通过遍历 scope 树建索引，不读 value change，适合 GB 级文件。"
                "对 .fsdb 的支持受 get_sim_paths 返回的 fsdb_runtime.enabled 约束。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "波形文件绝对路径"},
                    "keyword":   {"type": "string", "description": "信号名关键字，如 s_bits、clk、data"},
                    "max_results": {"type": "integer", "description": "最多返回结果数，默认 50",
                                    "default": 50},
                },
                "required": ["wave_path", "keyword"],
            },
        ),

        Tool(
            name="get_signal_at_time",
            description="查询波形文件中某个信号在指定时刻的值（ps 精度）。对 .fsdb 的支持受 fsdb_runtime.enabled 约束。",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":   {"type": "string"},
                    "signal_path": {"type": "string",
                                    "description": "完整层级路径，如 top_tb.dut.s_bits"},
                    "time_ps":     {"type": "integer", "description": "查询时刻（ps）"},
                },
                "required": ["wave_path", "signal_path", "time_ps"],
            },
        ),

        Tool(
            name="get_signal_transitions",
            description="获取信号在时间范围内的所有跳变记录。对 .fsdb 的支持受 fsdb_runtime.enabled 约束。",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_path":   {"type": "string"},
                    "start_time_ps": {"type": "integer", "default": 0},
                    "end_time_ps":   {"type": "integer", "default": -1,
                                      "description": "-1 表示到仿真结束"},
                },
                "required": ["wave_path", "signal_path"],
            },
        ),

        Tool(
            name="get_signals_around_time",
            description=(
                "获取多个信号在指定时刻前后窗口内的值和跳变。"
                "常用于：已知报错时刻，查看相关信号的上下文。"
                "对 .fsdb 的支持受 fsdb_runtime.enabled 约束。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_paths":  {"type": "array", "items": {"type": "string"},
                                      "description": "信号完整路径列表"},
                    "center_time_ps":{"type": "integer", "description": "中心时刻（ps），通常为报错时刻"},
                    "window_ps":     {"type": "integer",
                                      "description": f"前后各取多少 ps，默认 {DEFAULT_WAVE_WINDOW_PS}",
                                      "default": DEFAULT_WAVE_WINDOW_PS},
                    "extra_transitions": {
                        "type": "integer",
                        "description": f"窗口前额外回溯多少次跳变，默认 {DEFAULT_EXTRA_TRANSITIONS}",
                        "default": DEFAULT_EXTRA_TRANSITIONS,
                    },
                },
                "required": ["wave_path", "signal_paths", "center_time_ps"],
            },
        ),

        Tool(
            name="get_waveform_summary",
            description="获取波形文件基本信息：格式、仿真时长、顶层模块等。对 .fsdb 的支持受 fsdb_runtime.enabled 约束。",
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
                "从编译阶段 log 自动提取用户文件并扫描源代码，构建完整 testbench hierarchy。"
                "返回 top module、文件分类、component tree、class hierarchy、interfaces。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compile_log": {"type": "string", "description": "编译或 elaborate 阶段 log 的绝对路径"},
                    "simulator": {"type": "string", "description": "vcs / xcelium / auto（默认 auto）",
                                  "default": "auto"},
                },
                "required": ["compile_log"],
            },
        ),

        Tool(
            name="scan_structural_risks",
            description=(
                "对编译文件列表中的 RTL/TB 源码做 Scope 1 正则静态结构风险扫描。"
                "这是感知层工具，只报告值得关注的模式，不做确诊判断。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compile_log": {"type": "string", "description": "编译或 elaborate 阶段 log 的绝对路径"},
                    "simulator": {
                        "type": "string",
                        "description": "vcs / xcelium / auto（默认 auto）",
                        "default": "auto",
                    },
                    "scan_scope": {
                        "type": "string",
                        "description": "扫描范围版本，当前仅支持 scope1",
                        "default": "scope1",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": ALL_CATEGORIES},
                        "description": "可选，仅扫描指定风险类别；省略时扫描全部类别",
                    },
                },
                "required": ["compile_log"],
            },
        ),

        Tool(
            name="analyze_failures",
            description=(
                "核心分析工具：聚焦单个报错 group 的第一次出现，"
                "返回 log 摘要、报错原始上下文和波形快照。"
                "对 .fsdb 的支持受 get_sim_paths 返回的 fsdb_runtime.enabled 约束。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":     {"type": "string", "description": "仿真 log 路径（irun.log）"},
                    "wave_path":    {"type": "string", "description": "波形文件路径（top_tb.fsdb）"},
                    "signal_paths": {"type": "array", "items": {"type": "string"},
                                     "description": "需要提取的信号完整路径列表（客户端从 RTL 或 log 推断后用 search_signals 确认）"},
                    "window_ps":    {"type": "integer",
                                     "description": f"每个报错时刻前后的波形窗口 ps，默认 {DEFAULT_WAVE_WINDOW_PS}",
                                     "default": DEFAULT_WAVE_WINDOW_PS},
                    "simulator":    {"type": "string", "description": "vcs / xcelium"},
                    "group_index":  {"type": "integer", "description": "分析哪个报错分组，默认 0", "default": 0},
                    "extra_transitions": {
                        "type": "integer",
                        "description": f"每个信号在窗口前额外回溯多少次跳变，默认 {DEFAULT_EXTRA_TRANSITIONS}",
                        "default": DEFAULT_EXTRA_TRANSITIONS,
                    },
                },
                "required": ["log_path", "wave_path", "signal_paths", "simulator"],
            },
        ),

        Tool(
            name="analyze_failure_event",
            description=(
                "从单个标准化 failure_event 出发，"
                "联动波形、hierarchy 和源码信息返回推荐实例、信号和源码文件。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path": {"type": "string"},
                    "wave_path": {"type": "string"},
                    "simulator": {"type": "string", "description": "vcs / xcelium"},
                    "failure_event": {"type": "object", "description": "parse_sim_log 对应 log 的标准化 failure_event"},
                    "compile_log": {"type": "string"},
                    "top_hint": {"type": "string"},
                },
                "required": ["log_path", "wave_path", "simulator", "failure_event"],
            },
        ),

        Tool(
            name="recommend_failure_debug_next_steps",
            description=(
                "根据当前 log、wave 和可选 hierarchy，"
                "自动选择优先分析的失败并推荐下一步看的信号、实例和故障类型。"
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
                "冷启动加速器：聚合已有 tool 结果的摘要视图。"
                "不执行任何子步骤，只读取缓存。"
                "返回各数据源的可用性状态和精简摘要，以及缺失项的建议调用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {
                        "type": "string",
                        "description": (
                            "项目的 verif/ 目录绝对路径。"
                            "仅在 get_sim_paths 尚未执行时用于生成 suggested_call。"
                        ),
                    },
                },
                "required": [],
            },
        ),

        Tool(
            name="explain_signal_driver",
            description=(
                "从波形信号路径回溯最可能的 RTL 驱动位置。"
                "支持 direct assign、简单 always 块和 module output port。"
                "设置 recursive=true 可沿驱动链递归回溯多跳，包括穿越实例边界。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "signal_path": {"type": "string"},
                    "wave_path": {"type": "string"},
                    "compile_log": {"type": "string"},
                    "top_hint": {"type": "string"},
                    "recursive": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否递归追踪上游驱动链",
                    },
                    "max_depth": {
                        "type": "integer",
                        "default": 10,
                        "description": "递归最大深度（仅 recursive=true 时生效）",
                    },
                },
                "required": ["signal_path", "wave_path", "compile_log"],
            },
        ),

        Tool(
            name="trace_x_source",
            description=(
                "当信号在指定时刻出现 X/Z 时，自动沿驱动逻辑追踪传播链。"
                "遇到实例端口连接时会列出连接列表并停止。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string"},
                    "signal_path": {"type": "string"},
                    "time_ps": {"type": "integer"},
                    "compile_log": {"type": "string"},
                    "top_hint": {"type": "string"},
                    "max_depth": {
                        "type": "integer",
                        "description": f"最大追踪深度，默认 {DEFAULT_X_TRACE_MAX_DEPTH}",
                        "default": DEFAULT_X_TRACE_MAX_DEPTH,
                    },
                },
                "required": ["wave_path", "signal_path", "time_ps", "compile_log"],
            },
        ),
    ]


# ═══════════════════════════════════════════════════════════════════
# Tool 调用处理
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
        result = SimLogParser(
            args["base_log_path"],
            args["simulator"],
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
            raise ValueError(f"不支持的格式: .{ext}")

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

    elif name == "get_waveform_summary":
        result = _get_parser(args["wave_path"]).get_summary()
        return schemas.WaveformSummaryResult.model_validate(result)

    elif name == "build_tb_hierarchy":
        result = build_hierarchy(
            parse_compile_log(
                args["compile_log"],
                args.get("simulator", "auto"),
            )
        )
        _update_session_state(name, args, result)
        validated = schemas.BuildTbHierarchyResult.model_validate(result)
        _result_cache["build_tb_hierarchy"] = validated
        _result_provenance["build_tb_hierarchy"] = _build_result_provenance(name, args, validated)
        return validated

    elif name == "scan_structural_risks":
        result = scan_structural_risks(
            compile_log=args["compile_log"],
            simulator=args.get("simulator", "auto"),
            scan_scope=args.get("scan_scope", "scope1"),
            categories=args.get("categories"),
        )
        return schemas.ScanStructuralRisksResult.model_validate(result)

    elif name == "analyze_failures":
        result = WaveformAnalyzer(
            log_path=args["log_path"],
            parser=_get_parser(args["wave_path"]),
            simulator=args["simulator"],
        ).analyze(
            signal_paths=args["signal_paths"],
            group_index=args.get("group_index", 0),
            window_ps=args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
            extra_transitions = args.get("extra_transitions", DEFAULT_EXTRA_TRANSITIONS),
        )
        return schemas.AnalyzeFailuresResult.model_validate(result)

    elif name == "analyze_failure_event":
        result = WaveformAnalyzer(
            log_path=args["log_path"],
            parser=_get_parser(args["wave_path"]),
            simulator=args["simulator"],
        ).analyze_failure_event(
            failure_event=args["failure_event"],
            wave_path=args["wave_path"],
            compile_log=args.get("compile_log"),
            top_hint=args.get("top_hint"),
        )
        return schemas.AnalyzeFailureEventResult.model_validate(result)

    elif name == "recommend_failure_debug_next_steps":
        result = WaveformAnalyzer(
            log_path=args["log_path"],
            parser=_get_parser(args["wave_path"]),
            simulator=args["simulator"],
        ).recommend_debug_next_steps(
            wave_path=args["wave_path"],
            compile_log=args.get("compile_log"),
            top_hint=args.get("top_hint"),
        )
        validated = schemas.RecommendNextStepsResult.model_validate(result)
        _result_cache["recommend_failure_debug_next_steps"] = validated
        _result_provenance["recommend_failure_debug_next_steps"] = _build_result_provenance(name, args, validated)
        return validated

    elif name == "get_diagnostic_snapshot":
        return _handle_diagnostic_snapshot(args)

    elif name == "explain_signal_driver":
        result = explain_signal_driver(
            signal_path=args["signal_path"],
            wave_path=args["wave_path"],
            compile_log=args["compile_log"],
            top_hint=args.get("top_hint"),
            recursive=args.get("recursive", False),
            max_depth=args.get("max_depth", 10),
        )
        return schemas.ExplainDriverResult.model_validate(result)

    elif name == "trace_x_source":
        result = trace_x_source(
            wave_path=args["wave_path"],
            signal_path=args["signal_path"],
            time_ps=args["time_ps"],
            compile_log=args["compile_log"],
            parser=_get_parser(args["wave_path"]),
            top_hint=args.get("top_hint"),
            max_depth=args.get("max_depth", DEFAULT_X_TRACE_MAX_DEPTH),
        )
        return schemas.TraceXSourceResult.model_validate(result)

    else:
        raise ValueError(f"未知工具: {name}")


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
    return {
        "log_file": result.log_file,
        "runtime_total_errors": result.runtime_total_errors,
        "group_count": len(result.groups),
        "problem_hints": result.problem_hints.model_dump() if result.problem_hints else None,
        "first_group_signature": result.groups[0].signature if result.groups else None,
        "previous_log_detected": result.previous_log_detected,
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
            "simulator": result.project.get("simulator"),
        }
    if tool_name == "parse_sim_log":
        return {
            "log_path": result.log_file,
            "simulator": result.simulator,
        }
    if tool_name == "recommend_failure_debug_next_steps":
        return {
            "log_path": args.get("log_path"),
            "wave_path": args.get("wave_path"),
            "simulator": args.get("simulator"),
            "compile_log": args.get("compile_log"),
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
        )
    if tool_name == "recommend_failure_debug_next_steps":
        return (
            provenance.get("simulator") == anchor.get("simulator")
            and provenance.get("compile_log") == anchor.get("compile_log")
            and _path_matches_session(provenance.get("log_path"), sim_logs, case_dir)
            and _path_matches_session(provenance.get("wave_path"), wave_files, case_dir)
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
            "reason": "路径发现尚未执行，无法确定仿真产物位置",
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
            "reason": "层级结构尚未构建，无法确定模块/实例关系",
        })

    log_result = _result_cache.get("parse_sim_log")
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
    else:
        sections["log_analysis"] = schemas.DiagnosticSnapshotSection(available=False)
    if anchor is not None and (log_result is None or sections["log_analysis"].stale):
        suggested = _build_suggested_call("parse_sim_log") if _can_suggest_parse_sim_log(anchor) else None
        sections["log_analysis"].suggested_call = suggested
        missing_steps.append({
            "tool": "parse_sim_log",
            "arguments": suggested["arguments"] if suggested else {},
            "reason": "仿真日志尚未解析，无法获取错误信息",
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
            "reason": "推荐分析尚未执行，无法给出优先调试目标",
        })

    return schemas.DiagnosticSnapshot(
        sim_paths=sections["sim_paths"],
        hierarchy=sections["hierarchy"],
        log_analysis=sections["log_analysis"],
        recommended_next=sections["recommended_next"],
        missing_steps=missing_steps if missing_steps else None,
        **quick_ref,
    )


def _handle_parse_sim_log(args: dict) -> schemas.ParseSimLogResult:
    parser = SimLogParser(args["log_path"], args["simulator"])
    summary = parser.parse(max_groups=args.get("max_groups", DEFAULT_MAX_GROUPS))
    detail_level = args.get("detail_level", DEFAULT_DETAIL_LEVEL)
    max_events_per_group = args.get("max_events_per_group", DEFAULT_MAX_EVENTS_PER_GROUP)

    if detail_level not in {"summary", "compact", "full"}:
        raise ValueError("detail_level 必须为 summary / compact / full")
    if max_events_per_group <= 0:
        raise ValueError("max_events_per_group 必须大于 0")

    allowed_signatures = {group["signature"] for group in summary.get("groups", [])}
    all_events = parser.parse_failure_events()

    if detail_level == "summary":
        total = summary["runtime_total_errors"]
        returned_events = []
        summary["detail_hint"] = "use detail_level='compact' or get_error_context(group_index=N) for details"
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
    summary["failure_events"] = returned_events
    summary["failure_events_total"] = total
    summary["failure_events_returned"] = len(returned_events)
    summary["failure_events_truncated"] = len(returned_events) < total
    summary["first_group_context"] = first_group_context
    summary["problem_hints"] = compute_problem_hints(summary, all_events)
    validated = schemas.ParseSimLogResult.model_validate(summary)
    _invalidate_downstream("parse_sim_log")
    _result_cache["parse_sim_log"] = validated
    _result_provenance["parse_sim_log"] = _build_result_provenance("parse_sim_log", args, validated)
    return validated


def _serialize_result(result: BaseModel | dict) -> str:
    if isinstance(result, BaseModel):
        return result.model_dump_json(indent=2, exclude_none=True)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _format_error(exc: Exception) -> schemas.ToolErrorResult:
    message = str(exc)
    if "FSDB 解析不可用" in message:
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
