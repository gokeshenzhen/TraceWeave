"""
analyzer.py
联合分析器：log 报错 + 波形上下文 → 结构化报告
每个报错返回两份波形数据：
  wave_context            : 报错时刻前后窗口快照（快速定位）
  signal_history_before_fail : 报错前完整跳变历史（根因追溯，彻底消除窗口不够问题）
"""

from .log_parser import SimLogParser
from .vcd_parser import VCDParser
from .fsdb_parser import FSDBParser
from config import DEFAULT_WAVE_WINDOW_PS


def _get_parser(wave_path: str):
    ext = wave_path.lower().rsplit(".", 1)[-1]
    if ext == "vcd":
        return VCDParser(wave_path)
    elif ext == "fsdb":
        return FSDBParser(wave_path)
    raise ValueError(f"不支持的波形格式: .{ext}，请使用 .vcd 或 .fsdb")


class WaveformAnalyzer:
    def __init__(self, log_path: str, wave_path: str, simulator: str = "auto"):
        self.log_path  = log_path
        self.wave_path = wave_path
        self.simulator = simulator

    def analyze(self, signal_paths: list,
                window_ps: int = DEFAULT_WAVE_WINDOW_PS) -> dict:

        # Step 1: 解析 log
        log_result = SimLogParser(self.log_path, self.simulator).parse()
        errors     = log_result.get("errors", [])

        if not errors:
            return {
                "summary":    "仿真 log 中未发现任何 ERROR 或 FATAL",
                "log_result": log_result,
            }

        # Step 2: 打开波形解析器
        parser = _get_parser(self.wave_path)

        # Step 3: 预取每个信号从仿真开始到最后报错时刻的完整历史
        # 只取一次，所有报错共用，避免重复读取 FSDB
        last_fail_ps = max(e["fail_time_ps"] for e in errors)
        full_history: dict[str, list] = {}
        for path in signal_paths:
            try:
                result = parser.get_transitions(path, 0, last_fail_ps)
                full_history[path] = result["transitions"]
            except Exception as e:
                full_history[path] = [{"error": str(e)}]

        # Step 4: 对每条报错，组装两份波形数据
        enriched = []
        for err in errors:
            fail_ps = err["fail_time_ps"]

            # 4a. 窗口快照（报错时刻前后，快速定位）
            try:
                err["wave_context"] = parser.get_signals_around_time(
                    signal_paths, fail_ps, window_ps
                )
            except Exception as e:
                err["wave_context"] = {"error": str(e)}

            # 4b. 报错前完整跳变历史（根因追溯）
            # 从预取的完整历史中截取到 fail_ps 为止
            history_before = {}
            for path, trans in full_history.items():
                if isinstance(trans, list) and trans and "error" not in trans[0]:
                    history_before[path] = [
                        t for t in trans if t["time_ps"] <= fail_ps
                    ]
                else:
                    history_before[path] = trans
            err["signal_history_before_fail"] = {
                "up_to_time_ps": fail_ps,
                "up_to_time_ns": fail_ps / 1000,
                "signals":       history_before,
            }

            enriched.append(err)

        # Step 5: 生成摘要
        fatal_list = [e for e in enriched if e["severity"] == "FATAL"]

        summary_lines = [
            f"共发现 {log_result['fatal_count']} 个 FATAL，"
            f"{log_result['error_count']} 个 ERROR，"
            f"涉及 {log_result['unique_error_types']} 种类型。",
        ]
        if fatal_list:
            summary_lines.append("【FATAL - 优先处理】")
            for e in fatal_list[:3]:
                summary_lines.append(
                    f"  · {e['assertion_name'] or e['error_type']} "
                    f"@ {e['fail_time_ns']:.3f} ns  {e['message'][:80]}"
                )
        for item in log_result.get("error_summary", [])[:5]:
            summary_lines.append(f"  · {item['label']}: {item['count']} 次")

        return {
            "summary":            "\n".join(summary_lines),
            "simulator":          log_result["simulator"],
            "total_errors":       log_result["total_errors"],
            "fatal_count":        log_result["fatal_count"],
            "error_count":        log_result["error_count"],
            "unique_error_types": log_result["unique_error_types"],
            "error_summary":      log_result["error_summary"],
            "errors_with_wave_context": enriched,
            "signals_queried":    signal_paths,
            "wave_window_ps":     window_ps,
            "analysis_guide": {
                "step1": "优先处理 FATAL，通常意味着仿真无法继续",
                "step2": "对每个 ERROR，对照 sva_file + line_number 找到对应的 SVA/TB 代码",
                "step3": "查看 wave_context 中各信号在 fail_time_ns 时刻及前后的跳变（快速定位）",
                "step4": "查看 signal_history_before_fail 中各信号从仿真开始到报错时刻的完整跳变历史（根因追溯）",
                "step5": "结合 RTL 代码，判断是设计 bug 还是 testbench/constraint 问题",
                "step6": "如果多个 ERROR 在相近时刻且涉及相同信号，很可能是同一根因",
            },
        }
