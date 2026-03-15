#!/usr/bin/env python3
"""
Waveform Analysis MCP Server
用于支持 MCP 的调试客户端（例如 Codex、Claude Code）

支持工具：
  1. get_sim_paths          - 自动发现 compile/sim/wave 路径，或列出可用 case
  2. parse_sim_log          - 解析仿真 log 摘要分组
  3. get_error_context      - 按行号提取报错上下文
  4. search_signals         - 在波形文件中按关键字搜索信号完整路径
  5. get_signal_at_time     - 查询信号在某时刻的值
  6. get_signal_transitions - 获取信号跳变列表
  7. get_signals_around_time- 获取多个信号在某时刻前后的快照
  8. get_waveform_summary   - 波形文件基本信息
  9. analyze_failures       - 聚焦单个报错分组做 log + 波形联合分析
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
    DEFAULT_EXTRA_TRANSITIONS, DEFAULT_LOG_CONTEXT_AFTER, DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_MAX_GROUPS, DEFAULT_WAVE_WINDOW_PS,
)
from src.log_parser import SimLogParser, get_error_context
from src.vcd_parser import VCDParser
from src.fsdb_parser import FSDBParser
from src.fsdb_signal_index import FSDBSignalIndex
from src.analyzer import WaveformAnalyzer
from src.compile_log_parser import parse_compile_log
from src.path_discovery import discover_sim_paths
from src.tb_hierarchy_builder import build_hierarchy
from config import get_fsdb_runtime_info


SERVER_INSTRUCTIONS = """
Waveform debug workflow:

1. ALWAYS start with get_sim_paths to discover file paths and simulator type.
   - Inspect discovery_mode first: root_dir, case_dir, or unknown.
   - If discovery_mode is unknown, do not guess deeper paths; follow returned hints.
   - If case_name is unknown in root_dir mode, omit it to get available_cases first.
   - Inform the user early when hints show missing logs, empty logs, or missing waves.
   - Prefer compile_logs entries with phase="elaborate" for build_tb_hierarchy.
   - If fsdb_runtime.enabled is false, prefer .vcd entries in wave_files over .fsdb.

2. Call build_tb_hierarchy before analyzing failures.
   - Use the elaborate-phase compile_log and simulator from step 1.

3. Call parse_sim_log with sim_logs[0].path and simulator from step 1 when sim_logs is non-empty.
   - Use grouped errors to choose the first group_index to inspect.

4. Call search_signals to confirm full hierarchical signal paths.
   - Derive keywords from hierarchy output, error messages, or RTL source.

5. Call analyze_failures with log_path, wave_path, simulator, and confirmed signal_paths.
   - Follow analysis_guide in the result.

6. Use deep-dive tools when needed:
   - get_error_context for other groups
   - get_signal_transitions for longer history
   - get_signals_around_time for additional signals
   - get_signal_at_time for exact values
   - get_waveform_summary for waveform sanity checks
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
                },
                "required": ["log_path", "simulator"],
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
    ]


# ═══════════════════════════════════════════════════════════════════
# Tool 调用处理
# ═══════════════════════════════════════════════════════════════════

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text",
                            text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text",
                            text=json.dumps(_format_error(e), ensure_ascii=False))]


async def _dispatch(name: str, args: dict):

    if name == "get_sim_paths":
        return discover_sim_paths(
            args["verif_root"],
            args.get("case_name"),
        )

    elif name == "parse_sim_log":
        return SimLogParser(
            args["log_path"],
            args["simulator"]
        ).parse(max_groups=args.get("max_groups", DEFAULT_MAX_GROUPS))

    elif name == "get_error_context":
        return get_error_context(
            args["log_path"],
            line=args["line"],
            before=args.get("before", DEFAULT_LOG_CONTEXT_BEFORE),
            after=args.get("after", DEFAULT_LOG_CONTEXT_AFTER),
        )

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
            return _fsdb_index_cache[wave_path][1].search(keyword, max_r)
        elif ext == "vcd":
            return _get_parser(wave_path).search_signals(keyword, max_r)
        else:
            raise ValueError(f"不支持的格式: .{ext}")

    elif name == "get_signal_at_time":
        return _get_parser(args["wave_path"]).get_value_at_time(
            args["signal_path"], args["time_ps"]
        )

    elif name == "get_signal_transitions":
        return _get_parser(args["wave_path"]).get_transitions(
            args["signal_path"],
            args.get("start_time_ps", 0),
            args.get("end_time_ps", -1),
        )

    elif name == "get_signals_around_time":
        return _get_parser(args["wave_path"]).get_signals_around_time(
            args["signal_paths"],
            args["center_time_ps"],
            args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
            args.get("extra_transitions", DEFAULT_EXTRA_TRANSITIONS),
        )

    elif name == "get_waveform_summary":
        return _get_parser(args["wave_path"]).get_summary()

    elif name == "build_tb_hierarchy":
        return build_hierarchy(
            parse_compile_log(
                args["compile_log"],
                args.get("simulator", "auto"),
            )
        )

    elif name == "analyze_failures":
        return WaveformAnalyzer(
            log_path   = args["log_path"],
            parser     = _get_parser(args["wave_path"]),
            simulator  = args["simulator"],
        ).analyze(
            signal_paths = args["signal_paths"],
            group_index  = args.get("group_index", 0),
            window_ps    = args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
            extra_transitions = args.get("extra_transitions", DEFAULT_EXTRA_TRANSITIONS),
        )

    else:
        raise ValueError(f"未知工具: {name}")


def _format_error(exc: Exception) -> dict:
    message = str(exc)
    if "FSDB 解析不可用" in message:
        return {
            "error": message,
            "error_code": "fsdb_runtime_unavailable",
            "fsdb_runtime": get_fsdb_runtime_info(),
            "fallback": {
                "supported_wave_formats": ["vcd"],
                "action": "prefer_vcd_waveforms",
            },
        }
    return {"error": message}


# ═══════════════════════════════════════════════════════════════════
# Entry
# ═══════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream,
                      app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
