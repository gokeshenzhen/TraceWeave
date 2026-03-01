"""
analyzer.py
联合分析器：将仿真日志中的报错信息与波形数据进行自动关联，生成结构化报告。
每个报错会包含：
  1. wave_context: 报错瞬间前后的信号快照（便于直观定位问题点）。
  2. signal_history_before_fail: 报错前该信号的所有跳变历史（用于追溯根本原因）。
"""

from .log_parser import SimLogParser
from .vcd_parser import VCDParser
from .fsdb_parser import FSDBParser
from config import DEFAULT_WAVE_WINDOW_PS

def _get_parser(wave_path: str):
    """根据文件后缀名返回对应的波形解析器实例"""
    ext = wave_path.lower().rsplit(".", 1)[-1]
    if ext == "vcd":
        return VCDParser(wave_path)
    elif ext == "fsdb":
        return FSDBParser(wave_path)
    raise ValueError(f"不支持的波形格式: .{ext}，请使用 .vcd 或 .fsdb")

class WaveformAnalyzer:
    def __init__(self, log_path: str, wave_path: str, simulator: str = "auto"):
        """初始化分析器，指定日志路径、波形路径和仿真器类型"""
        self.log_path  = log_path
        self.wave_path = wave_path
        self.simulator = simulator

    def analyze(self, signal_paths: list,
                window_ps: int = DEFAULT_WAVE_WINDOW_PS) -> dict:
        """核心分析函数：提取报错信息并为每个报错关联波形上下文"""

        # 第一步：调用 log_parser 解析日志文件，提取出所有 ERROR 和 FATAL 记录
        log_result = SimLogParser(self.log_path, self.simulator).parse()
        errors     = log_result.get("errors", [])

        # 如果日志里没有报错，直接返回摘要
        if not errors:
            return {
                "summary":    "仿真 log 中未发现任何 ERROR 或 FATAL",
                "log_result": log_result,
            }

        # 第二步：获取波形解析器 (FSDB 或 VCD)
        parser = _get_parser(self.wave_path)

        # 第三步：为了效率，先预取每个目标信号直到最后一次报错时刻的完整跳变历史
        # 这样在后续循环中就不用反复读取 FSDB 文件了
        last_fail_ps = max(e["fail_time_ps"] for e in errors)
        full_history: dict[str, list] = {}
        for path in signal_paths:
            try:
                # 获取从 0 到最后报错时刻的所有跳变
                result = parser.get_transitions(path, 0, last_fail_ps)
                full_history[path] = result["transitions"]
            except Exception as e:
                full_history[path] = [{"error": str(e)}]

        # 第四步：遍历每个报错，为其填充波形快照和历史
        enriched = []
        for err in errors:
            fail_ps = err["fail_time_ps"]

            # 4a. 获取窗口快照：报错时刻前后 window_ps 范围内的信号变化
            try:
                err["wave_context"] = parser.get_signals_around_time(
                    signal_paths, fail_ps, window_ps
                )
            except Exception as e:
                err["wave_context"] = {"error": str(e)}

            # 4b. 获取追溯历史：从预取的数据中截取出该报错时刻之前的跳变记录
            history_before = {}
            for path, trans in full_history.items():
                if isinstance(trans, list) and trans and "error" not in trans[0]:
                    # 过滤出早于或等于报错时刻的跳变
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

        # 第五步：生成供 AI 阅读的文字摘要
        fatal_list = [e for e in enriched if e["severity"] == "FATAL"]

        summary_lines = [
            f"共发现 {log_result['fatal_count']} 个 FATAL，"
            f"{log_result['error_count']} 个 ERROR，"
            f"涉及 {log_result['unique_error_types']} 种类型。",
        ]
        # 如果有 FATAL，在摘要中列出前三个
        if fatal_list:
            summary_lines.append("【FATAL - 优先处理】")
            for e in fatal_list[:3]:
                summary_lines.append(
                    f"  · {e['assertion_name'] or e['error_type']} "
                    f"@ {e['fail_time_ns']:.3f} ns  {e['message'][:80]}"
                )
        # 列出报错频率最高的前五种类型
        for item in log_result.get("error_summary", [])[:5]:
            summary_lines.append(f"  · {item['label']}: {item['count']} 次")

        # 返回最终的结构化大数据集
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
