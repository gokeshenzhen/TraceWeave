"""
analyzer.py
聚焦单个报错分组的联合分析器：log 摘要 + 原始上下文 + 波形窗口
"""

from __future__ import annotations

from config import (
    DEFAULT_LOG_CONTEXT_AFTER,
    DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_WAVE_WINDOW_PS,
)
from .log_parser import SimLogParser


class WaveformAnalyzer:
    def __init__(self, log_path: str, parser, simulator: str):
        self.log_path = log_path
        self.parser = parser
        self.simulator = simulator

    def analyze(
        self,
        signal_paths: list[str],
        group_index: int = 0,
        window_ps: int = DEFAULT_WAVE_WINDOW_PS,
        log_before: int = DEFAULT_LOG_CONTEXT_BEFORE,
        log_after: int = DEFAULT_LOG_CONTEXT_AFTER,
    ) -> dict:
        log_parser = SimLogParser(self.log_path, self.simulator)
        log_result = log_parser.parse()
        groups = log_result.get("groups", [])

        if not groups:
            return {
                "summary": log_result,
                "focused_group": None,
                "log_context": None,
                "wave_context": None,
                "remaining_groups": 0,
                "analysis_guide": {
                    "step1": "仿真 log 中未发现 ERROR 或 FATAL",
                },
            }

        if group_index < 0 or group_index >= len(groups):
            raise IndexError(f"group_index {group_index} 超出范围，当前 groups={len(groups)}")

        focused_group = dict(groups[group_index])
        first_time_ps = focused_group["first_time_ps"]

        log_context = log_parser.get_error_context(
            line=focused_group["first_line"],
            before=log_before,
            after=log_after,
        )
        wave_context = self.parser.get_signals_around_time(
            signal_paths,
            first_time_ps,
            window_ps,
        )

        return {
            "summary": log_result,
            "focused_group": focused_group,
            "log_context": log_context,
            "wave_context": wave_context,
            "remaining_groups": len(groups) - group_index - 1,
            "signals_queried": signal_paths,
            "analysis_guide": {
                "step1": "先看 focused_group 是否是最早出现且次数最多的报错类型",
                "step2": "结合 log_context 判断该次报错前后的 transaction 和 checker 输出",
                "step3": "在 wave_context 中核对关键信号在首次报错时刻附近的取值和跳变",
                "step4": "若需要更长历史，再单独调用 get_signal_transitions 追踪根因",
            },
        }
