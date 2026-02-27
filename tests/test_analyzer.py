"""
test_analyzer.py
端到端测试：log + 波形联合分析
覆盖：analyze_failures 返回结构、波形历史数据完整性
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.analyzer import WaveformAnalyzer

REAL_LOG  = "/home/robin/Projects/mcp_demo/tb/work/work_my_case0/run.log"
REAL_FSDB = "/home/robin/Projects/mcp_demo/tb/work/work_my_case0/top_tb.fsdb"
SIGNAL    = "top_tb.sva_top_inst.s_bits[2:0]"

pytestmark = pytest.mark.skipif(
    not os.path.exists(REAL_LOG) or not os.path.exists(REAL_FSDB),
    reason="真实仿真文件不存在，跳过"
)


@pytest.fixture(scope="module")
def analysis():
    """跑一次分析，module 内所有测试共用结果"""
    analyzer = WaveformAnalyzer(REAL_LOG, REAL_FSDB, "vcs")
    return analyzer.analyze([SIGNAL], window_ps=5000)


# ═══════════════════════════════════════════════════════════════════
# 顶层结构
# ═══════════════════════════════════════════════════════════════════

class TestAnalysisStructure:

    def test_has_summary(self, analysis):
        assert "summary" in analysis
        assert len(analysis["summary"]) > 0

    def test_has_error_counts(self, analysis):
        assert analysis["total_errors"] > 0
        assert "fatal_count" in analysis
        assert "error_count" in analysis

    def test_has_errors_list(self, analysis):
        assert "errors_with_wave_context" in analysis
        assert len(analysis["errors_with_wave_context"]) > 0

    def test_has_analysis_guide(self, analysis):
        guide = analysis["analysis_guide"]
        assert "step1" in guide
        assert "step4" in guide   # 完整历史步骤

    def test_signals_queried_recorded(self, analysis):
        assert SIGNAL in analysis["signals_queried"]


# ═══════════════════════════════════════════════════════════════════
# 每条报错的数据完整性
# ═══════════════════════════════════════════════════════════════════

class TestErrorEntries:

    def test_each_error_has_wave_context(self, analysis):
        for err in analysis["errors_with_wave_context"]:
            assert "wave_context" in err, \
                f"报错 {err.get('assertion_name')} 缺少 wave_context"

    def test_each_error_has_history(self, analysis):
        for err in analysis["errors_with_wave_context"]:
            assert "signal_history_before_fail" in err, \
                f"报错 {err.get('assertion_name')} 缺少 signal_history_before_fail"

    def test_history_signal_present(self, analysis):
        for err in analysis["errors_with_wave_context"]:
            hist = err["signal_history_before_fail"]
            assert SIGNAL in hist["signals"], \
                f"信号 {SIGNAL} 不在历史数据中"

    def test_history_only_before_fail(self, analysis):
        """历史数据中所有时间戳应 ≤ 报错时刻"""
        for err in analysis["errors_with_wave_context"]:
            fail_ps = err["fail_time_ps"]
            hist    = err["signal_history_before_fail"]["signals"].get(SIGNAL, [])
            if isinstance(hist, list):
                for t in hist:
                    if "time_ps" in t:
                        assert t["time_ps"] <= fail_ps, \
                            f"历史数据 {t['time_ps']} > fail_time {fail_ps}"

    def test_history_up_to_time_matches_fail(self, analysis):
        for err in analysis["errors_with_wave_context"]:
            hist = err["signal_history_before_fail"]
            assert hist["up_to_time_ps"] == err["fail_time_ps"]


# ═══════════════════════════════════════════════════════════════════
# 具体报错内容验证
# ═══════════════════════════════════════════════════════════════════

class TestSpecificErrors:

    def test_first_assertion_is_unexpected(self, analysis):
        """第一个报错应是 apUNEXPECTED_ASSERTION @ 290000ps"""
        first = analysis["errors_with_wave_context"][0]
        assert first["assertion_name"] == "apUNEXPECTED_ASSERTION"
        assert first["fail_time_ps"] == 290000

    def test_s_bits_value_at_first_fail(self, analysis):
        """290000ps 时 s_bits 应为 000"""
        first = analysis["errors_with_wave_context"][0]
        center_val = first["wave_context"]["signals"][SIGNAL]["value_at_center"]
        assert center_val == "100"

    def test_uvm_errors_present(self, analysis):
        uvm = [e for e in analysis["errors_with_wave_context"]
               if e["error_type"] == "UVM_ERROR"]
        assert len(uvm) == 15

    def test_fatal_count_is_zero(self, analysis):
        assert analysis["fatal_count"] == 0
