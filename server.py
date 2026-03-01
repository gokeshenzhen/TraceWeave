#!/usr/bin/env python3
"""
Waveform Analysis MCP Server
用于将芯片验证环境（EDA）与 AI 助手（如 Claude Code, Cursor）通过 MCP 协议连接。

该脚本是 MCP 服务的运行入口，负责：
  1. 定义 AI 可见的工具接口（Tools）。
  2. 接收标准输入（stdio）的 JSON-RPC 请求。
  3. 调用底层 src/ 目录下的解析引擎处理 Log 和波形。
  4. 将处理结果返回给 AI。
"""

import asyncio
import json
import sys
import os

# 确保项目根目录在 Python 的搜索路径中，以便能正确导入 config 和 src
sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# 导入配置函数和逻辑模块
from config import (
    get_elab_log, get_sim_log, get_wave_file, get_case_list, get_work_case_dir,
    DEFAULT_WAVE_WINDOW_PS,
)
from src.log_parser import SimLogParser
from src.vcd_parser import VCDParser
from src.fsdb_parser import FSDBParser
from src.fsdb_signal_index import FSDBSignalIndex
from src.analyzer import WaveformAnalyzer

# 创建 MCP 服务实例，名称为 waveform-mcp
app = Server("waveform-mcp")

# ── 全局缓存 ───────────────────────────────────────────────────────
# 针对 FSDB 文件的索引缓存，避免 AI 在同一次会话中多次搜索时反复重建索引
_fsdb_index_cache: dict[str, FSDBSignalIndex] = {}

def _get_parser(wave_path: str):
    """辅助函数：根据文件名后缀自动选择 VCD 或 FSDB 解析器"""
    ext = wave_path.lower().rsplit(".", 1)[-1]
    if ext == "vcd":
        return VCDParser(wave_path)
    elif ext == "fsdb":
        return FSDBParser(wave_path)
    raise ValueError(f"不支持的波形格式: .{ext}")

# ═══════════════════════════════════════════════════════════════════
# Tool 定义：以下部分定义了 AI 在对话框中能看到的“技能列表”
# ═══════════════════════════════════════════════════════════════════

@app.list_tools()
async def list_tools():
    """向 AI 声明该服务器支持的所有工具及其参数格式 (JSON Schema)"""
    return [

        Tool(
            name="get_sim_paths",
            description=(
                "根据项目验证根目录和 case 名称，返回所有标准仿真文件路径（如 log、fsdb）。"
                "这是 debug 的第一步，用于获取其他工具所需的路径参数。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "verif_root": {"type": "string",
                                   "description": "项目的 verif/ 目录绝对路径"},
                    "case_name":  {"type": "string",
                                   "description": "测试用例名称，如 case0"},
                },
                "required": ["verif_root", "case_name"],
            },
        ),

        Tool(
            name="parse_sim_log",
            description=(
                "解析仿真日志文件，提取所有 Assertion Failure、UVM_ERROR 和 UVM_FATAL。"
                "返回包含报错时刻（ps）、文件名、行号和详细消息的列表。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":  {"type": "string", "description": "仿真 log 的绝对路径"},
                    "simulator": {"type": "string", "description": "仿真器类型：vcs / xcelium / auto",
                                  "default": "auto"},
                },
                "required": ["log_path"],
            },
        ),

        Tool(
            name="search_signals",
            description=(
                "在波形文件（FSDB/VCD）中模糊搜索信号名，返回完整层级路径。"
                "当你知道信号关键字（如 'data'）但不知道其在 tb 中的完整路径时使用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string", "description": "波形文件的绝对路径"},
                    "keyword":   {"type": "string", "description": "信号名关键字，支持部分匹配"},
                    "max_results": {"type": "integer", "description": "最多返回的结果数",
                                    "default": 50},
                },
                "required": ["wave_path", "keyword"],
            },
        ),

        Tool(
            name="get_signal_at_time",
            description="查询波形文件中某个信号在指定时刻（ps）的具体值。",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":   {"type": "string"},
                    "signal_path": {"type": "string",
                                    "description": "信号的完整层级路径，如 top_tb.dut.clk"},
                    "time_ps":     {"type": "integer", "description": "查询的仿真时刻，精度为 ps"},
                },
                "required": ["wave_path", "signal_path", "time_ps"],
            },
        ),

        Tool(
            name="get_signal_transitions",
            description="获取信号在指定时间范围内的所有跳变（Value Change）历史。",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_path":   {"type": "string"},
                    "start_time_ps": {"type": "integer", "default": 0},
                    "end_time_ps":   {"type": "integer", "default": -1,
                                      "description": "-1 表示直到仿真结束"},
                },
                "required": ["wave_path", "signal_path"],
            },
        ),

        Tool(
            name="get_signals_around_time",
            description=(
                "同时获取多个信号在某一特定时刻前后的值和跳变过程。"
                "常用于在报错瞬间查看相关总线信号的上下文。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path":     {"type": "string"},
                    "signal_paths":  {"type": "array", "items": {"type": "string"},
                                      "description": "信号完整路径列表"},
                    "center_time_ps":{"type": "integer", "description": "中心时刻（通常为报错时刻）"},
                    "window_ps":     {"type": "integer",
                                      "description": f"前后各观察多少 ps 范围，默认 {DEFAULT_WAVE_WINDOW_PS}",
                                      "default": DEFAULT_WAVE_WINDOW_PS},
                },
                "required": ["wave_path", "signal_paths", "center_time_ps"],
            },
        ),

        Tool(
            name="get_waveform_summary",
            description="获取波形文件的基本统计信息（如仿真时长、信号总数、顶层模块名）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "wave_path": {"type": "string"},
                },
                "required": ["wave_path"],
            },
        ),

        Tool(
            name="analyze_failures",
            description=(
                "【核心自动化工具】：全自动读取 log 中的报错，并自动提取每个报错时刻的波形上下文。"
                "它会生成一份包含“报错+波形”的综合报告，供 AI 直接进行根因分析（Root Cause Analysis）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_path":     {"type": "string", "description": "仿真 log 路径"},
                    "wave_path":    {"type": "string", "description": "波形文件路径"},
                    "signal_paths": {"type": "array", "items": {"type": "string"},
                                     "description": "需要观测的核心信号路径列表"},
                    "window_ps":    {"type": "integer",
                                     "description": "报错前后的观察窗口大小",
                                     "default": DEFAULT_WAVE_WINDOW_PS},
                    "simulator":    {"type": "string", "default": "auto"},
                },
                "required": ["log_path", "wave_path", "signal_paths"],
            },
        ),
    ]

# ═══════════════════════════════════════════════════════════════════
# Tool 调用处理：当 AI 决定使用某个工具时，会进入此函数
# ═══════════════════════════════════════════════════════════════════

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    """分发 AI 的调用请求到具体的处理函数，并处理可能的异常"""
    try:
        result = await _dispatch(name, arguments)
        # 将结果转化为标准文本内容返回给 AI
        return [TextContent(type="text",
                            text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        # 如果内部报错，返回错误信息给 AI 告知执行失败原因
        return [TextContent(type="text",
                            text=json.dumps({"error": str(e)}, ensure_ascii=False))]

async def _dispatch(name: str, args: dict):
    """内部路由：根据工具名执行相应的 Python 逻辑"""

    if name == "get_sim_paths":
        # 获取 case 相关的各种标准路径
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
        # 解析仿真日志
        return SimLogParser(
            args["log_path"],
            args.get("simulator", "auto")
        ).parse()

    elif name == "search_signals":
        # 搜索信号路径
        wave_path  = args["wave_path"]
        keyword    = args["keyword"]
        max_r      = args.get("max_results", 50)
        ext = wave_path.lower().rsplit(".", 1)[-1]
        
        if ext == "fsdb":
            # 对于 FSDB，使用带缓存的索引器以提高速度
            if wave_path not in _fsdb_index_cache:
                _fsdb_index_cache[wave_path] = FSDBSignalIndex(wave_path)
            return _fsdb_index_cache[wave_path].search(keyword, max_r)
        elif ext == "vcd":
            # 对于 VCD，直接调用其内部搜索方法
            return VCDParser(wave_path).search_signals(keyword, max_r)
        else:
            raise ValueError(f"不支持的格式: .{ext}")

    elif name == "get_signal_at_time":
        # 查单时刻值
        return _get_parser(args["wave_path"]).get_value_at_time(
            args["signal_path"], args["time_ps"]
        )

    elif name == "get_signal_transitions":
        # 查跳变序列
        return _get_parser(args["wave_path"]).get_transitions(
            args["signal_path"],
            args.get("start_time_ps", 0),
            args.get("end_time_ps", -1),
        )

    elif name == "get_signals_around_time":
        # 查多信号快照
        return _get_parser(args["wave_path"]).get_signals_around_time(
            args["signal_paths"],
            args["center_time_ps"],
            args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
        )

    elif name == "get_waveform_summary":
        # 波形概况
        return _get_parser(args["wave_path"]).get_summary()

    elif name == "analyze_failures":
        # 联合分析：日志报错 + 波形联动
        return WaveformAnalyzer(
            log_path   = args["log_path"],
            wave_path  = args["wave_path"],
            simulator  = args.get("simulator", "auto"),
        ).analyze(
            signal_paths = args["signal_paths"],
            window_ps    = args.get("window_ps", DEFAULT_WAVE_WINDOW_PS),
        )

    else:
        raise ValueError(f"未知工具: {name}")

# ═══════════════════════════════════════════════════════════════════
# Entry：服务器启动点
# ═══════════════════════════════════════════════════════════════════

async def main():
    """启动 stdio 服务器，进入监听循环"""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream,
                      app.create_initialization_options())

if __name__ == "__main__":
    # 使用 Python 的异步事件循环运行服务
    asyncio.run(main())
