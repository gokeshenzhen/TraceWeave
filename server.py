#!/usr/bin/env python3
"""
Waveform Analysis MCP Server
用于 Claude Code（全局模式）

支持工具：
  1. get_sim_paths          - 根据项目路径+case名，返回所有标准文件路径
  2. parse_sim_log          - 解析仿真 log（assertion fail + UVM_ERROR/FATAL）
  3. search_signals         - 在波形文件中按关键字搜索信号完整路径
  4. get_signal_at_time     - 查询信号在某时刻的值
  5. get_signal_transitions - 获取信号跳变列表
  6. get_signals_around_time- 获取多个信号在某时刻前后的快照
  7. get_waveform_summary   - 波形文件基本信息
  8. analyze_failures       - 核心工具：log + 波形联合分析，生成根因分析报告
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
    get_elab_log, get_sim_log, get_wave_file, get_case_list, get_work_case_dir,
    DEFAULT_WAVE_WINDOW_PS,
)
from src.log_parser import SimLogParser
from src.vcd_parser import VCDParser
from src.fsdb_parser import FSDBParser
from src.fsdb_signal_index import FSDBSignalIndex
from src.analyzer import WaveformAnalyzer
from src.compile_log_parser import parse_compile_log
from src.tb_hierarchy_builder import build_hierarchy

app = Server("waveform-mcp")

# ── 全局缓存 ──────────────────────────────────────────────────────
_fsdb_index_cache: dict[str, FSDBSignalIndex] = {}
_parser_cache: dict[str, object] = {}          # wave_path → VCDParser / FSDBParser


def _get_parser(wave_path: str):
    """返回缓存的 parser 实例，避免 VCD 重复解析 / FSDB 重复打开"""
    if wave_path in _parser_cache:
        return _parser_cache[wave_path]
    ext = wave_path.lower().rsplit(".", 1)[-1]
    if ext == "vcd":
        parser = VCDParser(wave_path)
    elif ext == "fsdb":
        parser = FSDBParser(wave_path)
    else:
        raise ValueError(f"不支持的波形格式: .{ext}")
    _parser_cache[wave_path] = parser
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
                "根据 verif 目录根路径和 case 名称，返回所有标准仿真文件路径。"
                "调用其他工具前先调用此工具，获取 log_path 和 wave_path。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {"type": "string",
                                   "description": "项目的 verif/ 目录绝对路径，如 /home/robin/Projects/i2c_lib/verif"},
                    "case_name":  {"type": "string",
                                   "description": "case 名称，如 case0（对应 make SV_CASE=case0）"},
                },
                "required": ["verif_root", "case_name"],
            },
        ),

        Tool(
            name="parse_sim_log",
            description=(
                "解析 VCS 或 Xcelium 仿真 log，提取所有 assertion fail、UVM_ERROR、UVM_FATAL。"
                "返回结构化报错列表，包含文件名、行号、时间、消息。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":  {"type": "string", "description": "仿真 log 文件绝对路径（irun.log）"},
                    "simulator": {"type": "string", "description": "vcs / xcelium / auto（默认 auto）",
                                  "default": "auto"},
                },
                "required": ["log_path"],
            },
        ),

        Tool(
            name="search_signals",
            description=(
                "在波形文件（FSDB/VCD）中搜索包含关键字的信号，返回完整层级路径。"
                "当 Claude 从 RTL 代码知道信号名但不知道完整路径时使用。"
                "FSDB 通过遍历 scope 树建索引，不读 value change，适合 GB 级文件。"
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
            description="查询波形文件中某个信号在指定时刻的值（ps 精度）",
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
            description="获取信号在时间范围内的所有跳变记录",
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
                },
                "required": ["wave_path", "signal_paths", "center_time_ps"],
            },
        ),

        Tool(
            name="get_waveform_summary",
            description="获取波形文件基本信息：格式、仿真时长、顶层模块等",
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
                "核心分析工具：读取仿真 log 中所有报错，自动提取每个报错时刻前后的波形上下文，"
                "生成结构化报告供 Claude 做 root cause 分析。"
                "返回数据包含：报错摘要、每个报错的信号快照、分析指引。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":     {"type": "string", "description": "仿真 log 路径（irun.log）"},
                    "wave_path":    {"type": "string", "description": "波形文件路径（top_tb.fsdb）"},
                    "signal_paths": {"type": "array", "items": {"type": "string"},
                                     "description": "需要提取的信号完整路径列表（Claude 从 RTL 推断后用 search_signals 确认）"},
                    "window_ps":    {"type": "integer",
                                     "description": f"每个报错时刻前后的波形窗口 ps，默认 {DEFAULT_WAVE_WINDOW_PS}",
                                     "default": DEFAULT_WAVE_WINDOW_PS},
                    "simulator":    {"type": "string", "default": "auto"},
                },
                "required": ["log_path", "wave_path", "signal_paths"],
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
                            text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def _dispatch(name: str, args: dict):

    if name == "get_sim_paths":
        vr   = args["verif_root"]
        case = args["case_name"]
        return {
            "verif_root":    vr,
            "case_name":     case,
            "elab_log":      get_elab_log(vr),
            "sim_log":       get_sim_log(vr, case),
            "wave_file":     get_wave_file(vr, case),
            "work_case_dir": get_work_case_dir(vr, case),
            "case_list":     get_case_list(vr),
        }

    elif name == "parse_sim_log":
        return SimLogParser(
            args["log_path"],
            args.get("simulator", "auto")
        ).parse()

    elif name == "search_signals":
        wave_path  = args["wave_path"]
        keyword    = args["keyword"]
        max_r      = args.get("max_results", 50)
        ext = wave_path.lower().rsplit(".", 1)[-1]
        if ext == "fsdb":
            if wave_path not in _fsdb_index_cache:
                _fsdb_index_cache[wave_path] = FSDBSignalIndex(wave_path)
            return _fsdb_index_cache[wave_path].search(keyword, max_r)
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
            simulator  = args.get("simulator", "auto"),
        ).analyze(
            signal_paths = args["signal_paths"],
            window_ps    = args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
        )

    else:
        raise ValueError(f"未知工具: {name}")


# ═══════════════════════════════════════════════════════════════════
# Entry
# ═══════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream,
                      app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
