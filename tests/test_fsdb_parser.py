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

pytestmark = pytest.mark.skipif(
    not os.path.exists(REAL_FSDB),
    reason="真实 FSDB 文件不存在，跳过"
)


@pytest.fixture(scope="module")
def parser():
    """module 级别共享，避免重复打开 FSDB"""
    p = FSDBParser(REAL_FSDB)
    yield p
    p.close()


# ═══════════════════════════════════════════════════════════════════
# 信号搜索
# ═══════════════════════════════════════════════════════════════════

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
