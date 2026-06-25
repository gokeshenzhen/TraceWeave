"""
test_fsdb_parser.py
用真实 top_tb.fsdb 测试 FSDB 解析器
覆盖：信号搜索、时刻值查询、跳变列表、时间窗口查询
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.fsdb_parser import FSDBParser

REAL_FSDB = "/home/robin/Projects/mcp_demo/tb/work/work_my_case0/top_tb.fsdb"
SIGNAL    = "top_tb.sva_top_inst.s_bits[2:0]"   # 已确认存在

# 依赖具体信号 SIGNAL 的测试类，需要这个特定 FSDB 才能跑。
_needs_real_fsdb = pytest.mark.skipif(
    not os.path.exists(REAL_FSDB),
    reason="真实 FSDB 文件不存在，跳过"
)

# 摘要/时长测试不依赖任何特定信号，可对任意 FSDB 验证；
# 用 TW_SUMMARY_FSDB 覆盖即可在没有上面那个私有 FSDB 时也能跑。
SUMMARY_FSDB = os.environ.get("TW_SUMMARY_FSDB", REAL_FSDB)


@pytest.fixture(scope="module")
def parser():
    """module 级别共享，避免重复打开 FSDB"""
    p = FSDBParser(REAL_FSDB)
    yield p
    p.close()


# ═══════════════════════════════════════════════════════════════════
# 信号搜索
# ═══════════════════════════════════════════════════════════════════

@_needs_real_fsdb
class TestSearchSignals:

    def test_search_s_bits_found(self, parser):
        result = parser.search_signals("s_bits")
        assert result["total_matched"] >= 1

    def test_search_result_has_full_path(self, parser):
        result = parser.search_signals("s_bits")
        paths = [r["path"] for r in result["results"]]
        assert any("top_tb" in p for p in paths)

    def test_search_result_has_width(self, parser):
        result = parser.search_signals("s_bits")
        assert result["results"][0]["width"] > 0

    def test_search_nonexist_returns_empty(self, parser):
        result = parser.search_signals("xyzzy_nonexistent_signal_abc")
        assert result["total_matched"] == 0
        assert result["results"] == []

    def test_search_case_insensitive(self, parser):
        r1 = parser.search_signals("s_bits")
        r2 = parser.search_signals("S_BITS")
        assert r1["total_matched"] == r2["total_matched"]

    def test_search_max_results(self, parser):
        result = parser.search_signals("top_tb", max_results=3)
        assert len(result["results"]) <= 3

    def test_search_hint_present(self, parser):
        result = parser.search_signals("s_bits")
        assert "hint" in result


# ═══════════════════════════════════════════════════════════════════
# 时刻值查询
# ═══════════════════════════════════════════════════════════════════

@_needs_real_fsdb
class TestGetValueAtTime:

    def test_value_at_310000ps(self, parser):
        """310000ps 时 s_bits 应为 000（assertion fail 时刻）"""
        result = parser.get_value_at_time(SIGNAL, 310000)
        assert result["value"]["bin"] == "000"
        assert result["value"]["hex"] == "0x0"
        assert result["value"]["dec"] == 0
        assert result["time_ps"] == 310000
        assert result["time_ns"] == 310.0

    def test_value_at_250000ps(self, parser):
        """250000ps 时 s_bits 应为 100（跳变后）"""
        result = parser.get_value_at_time(SIGNAL, 250000)
        assert result["value"]["bin"] == "100"

    def test_value_at_0ps(self, parser):
        """仿真开始时应为 000"""
        result = parser.get_value_at_time(SIGNAL, 0)
        assert result["value"]["bin"] == "000"

    def test_result_has_required_fields(self, parser):
        result = parser.get_value_at_time(SIGNAL, 310000)
        for field in ["signal", "time_ps", "time_ns", "value"]:
            assert field in result

    def test_nonexistent_signal_raises(self, parser):
        with pytest.raises(KeyError, match="Signal not found"):
            parser.get_value_at_time("top_tb.nonexistent.signal", 0)


# ═══════════════════════════════════════════════════════════════════
# 跳变列表查询
# ═══════════════════════════════════════════════════════════════════

@_needs_real_fsdb
class TestGetTransitions:

    def test_transitions_in_range(self, parser):
        """0~500000ps 范围内应有 9 个跳变（已验证过）"""
        result = parser.get_transitions(SIGNAL, 0, 500000)
        assert result["transition_count"] == 9

    def test_transitions_sorted_by_time(self, parser):
        result = parser.get_transitions(SIGNAL, 0, 500000)
        times = [t["time_ps"] for t in result["transitions"]]
        assert times == sorted(times)

    def test_transition_has_required_fields(self, parser):
        result = parser.get_transitions(SIGNAL, 0, 500000)
        t = result["transitions"][0]
        for field in ["time_ps", "time_ns", "value"]:
            assert field in t

    def test_transition_values_are_binary(self, parser):
        """s_bits 是 3bit reg，值应为 0/1/x/z 组成的字符串"""
        result = parser.get_transitions(SIGNAL, 0, 500000)
        for t in result["transitions"]:
            assert all(c in "01xzXZu?" for c in t["value"]["bin"])

    def test_narrow_range_returns_subset(self, parser):
        """窄范围应返回更少的跳变"""
        full   = parser.get_transitions(SIGNAL, 0, 500000)
        narrow = parser.get_transitions(SIGNAL, 200000, 300000)
        assert narrow["transition_count"] <= full["transition_count"]

    def test_time_ns_conversion(self, parser):
        result = parser.get_transitions(SIGNAL, 230000, 230000)
        if result["transitions"]:
            t = result["transitions"][0]
            assert t["time_ns"] == t["time_ps"] / 1000


# ═══════════════════════════════════════════════════════════════════
# 多信号时间窗口查询
# ═══════════════════════════════════════════════════════════════════

@_needs_real_fsdb
class TestGetSignalsAroundTime:

    def test_center_value_correct(self, parser):
        """310000ps 时 s_bits=000，center 值应一致"""
        result = parser.get_signals_around_time([SIGNAL], 310000, 5000)
        assert result["signals"][SIGNAL]["value_at_center"]["bin"] == "000"

    def test_window_contains_transitions(self, parser):
        """以 310000ps 为中心，窗口 ±50000ps 内应有跳变"""
        result = parser.get_signals_around_time([SIGNAL], 310000, 50000)
        trans = result["signals"][SIGNAL]["transitions_in_window"]
        assert len(trans) > 0

    def test_pre_window_transitions_present(self, parser):
        result = parser.get_signals_around_time([SIGNAL], 310000, 5000)
        assert "pre_window_transitions" in result["signals"][SIGNAL]
        pre = result["signals"][SIGNAL]["pre_window_transitions"]
        times = [item["time_ps"] for item in pre]
        assert times == sorted(times, reverse=True)

    def test_result_structure(self, parser):
        result = parser.get_signals_around_time([SIGNAL], 310000, 5000)
        assert "center_time_ps" in result
        assert "center_time_ns" in result
        assert "window_ps" in result
        assert "truncated" in result
        assert "signals" in result

    def test_nonexistent_signal_returns_error_key(self, parser):
        """不存在的信号不应抛出异常，而是在结果里标注 error"""
        result = parser.get_signals_around_time(
            ["top_tb.nonexistent.sig"], 310000, 5000
        )
        assert "error" in result["signals"]["top_tb.nonexistent.sig"]

    def test_multiple_signals(self, parser):
        """多个信号同时查询"""
        signals = [SIGNAL, "top_tb.nonexistent.sig"]
        result  = parser.get_signals_around_time(signals, 310000, 5000)
        assert SIGNAL in result["signals"]
        assert "top_tb.nonexistent.sig" in result["signals"]


# ═══════════════════════════════════════════════════════════════════
# 波形摘要 / 仿真时长（端到端验证 native 全局时间 API）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not os.path.exists(SUMMARY_FSDB),
    reason="没有可用 FSDB（设置 TW_SUMMARY_FSDB 指向任意 FSDB 即可运行）"
)
class TestGetSummary:

    @pytest.fixture(scope="class")
    def summary_parser(self):
        p = FSDBParser(SUMMARY_FSDB)
        yield p
        p.close()

    def test_duration_is_positive_for_valid_fsdb(self, summary_parser):
        """一个有效的、含跳变的 FSDB，时长必须为正。

        回归保护：原 native 实现从“最大 idcode 的单个信号”推导结束时间，
        该信号若是 static/记账信号会让有效 FSDB 报出 0。现已改为优先调用
        文件级全局 API ffrGetMaxFsdbTag64()。
        """
        s = summary_parser.get_summary()
        assert s["simulation_duration_ps"] > 0

    def test_native_global_end_time_is_positive(self, summary_parser):
        """直接验证 native 层：全局时间 API 自身就应返回非零，
        不依赖 Python 侧的逐信号兜底。"""
        summary_parser._open()
        end_ps = int(summary_parser._lib.fsdb_get_end_time(summary_parser._handle))
        assert end_ps > 0

    def test_duration_ns_consistent_with_ps(self, summary_parser):
        s = summary_parser.get_summary()
        assert s["simulation_duration_ns"] == s["simulation_duration_ps"] / 1000

    def test_summary_structure(self, summary_parser):
        s = summary_parser.get_summary()
        for key in ("file", "format", "simulation_duration_ps",
                    "simulation_duration_ns", "total_signals",
                    "top_modules", "sample_signals"):
            assert key in s
        assert s["format"] == "FSDB"
