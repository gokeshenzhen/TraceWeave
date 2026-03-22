"""
test_analyzer.py
覆盖：只聚焦单个 group 的联合分析结果
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.analyzer import WaveformAnalyzer
from src.log_parser import SimLogParser


LOG_SAMPLE = """\
header
"/path/sva_top.sv", 66: top_tb.sva_top_inst.apUNEXPECTED_ASSERTION: started at 270000ps failed at 290000ps
middle
UVM_ERROR /path/top_tb.sv(125) @ 1661.000 ns: reporter [TOP] a=1, b=0
tail
"""


class FakeWaveParser:
    def __init__(self):
        self.calls = []
        self.search_calls = []

    def get_signals_around_time(self, signal_paths, center_time_ps, window_ps, extra_transitions):
        self.calls.append(
            {
                "signal_paths": signal_paths,
                "center_time_ps": center_time_ps,
                "window_ps": window_ps,
                "extra_transitions": extra_transitions,
            }
        )
        return {
            "center_time_ps": center_time_ps,
            "window_ps": window_ps,
            "signals": {
                signal_path: {
                    "value_at_center": {"bin": "0", "hex": "0x0", "dec": 0},
                    "transitions_in_window": [{"time_ps": center_time_ps, "value": "1"}] if signal_path.endswith(".req") else [],
                    "pre_window_transitions": [],
                }
                for signal_path in signal_paths
            },
        }

    def search_signals(self, keyword, max_results):
        self.search_calls.append({"keyword": keyword, "max_results": max_results})
        samples = {
            "dut": [
                {"path": "top_tb.dut.req", "name": "req", "width": 1},
                {"path": "top_tb.scoreboard.req", "name": "req", "width": 1},
            ],
            "req": [
                {"path": "top_tb.dut.req", "name": "req", "width": 1},
                {"path": "top_tb.monitor.req", "name": "req", "width": 1},
            ],
            "apUNEXPECTED_ASSERTION": [],
        }
        return {"results": samples.get(keyword, [])}


@pytest.fixture
def log_path():
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    handle.write(LOG_SAMPLE)
    handle.close()
    yield handle.name
    os.unlink(handle.name)


@pytest.fixture
def analysis(log_path):
    parser = FakeWaveParser()
    analyzer = WaveformAnalyzer(log_path, parser, "vcs")
    result = analyzer.analyze(["top_tb.dut.req"], group_index=0, window_ps=5000, log_before=1, log_after=1)
    return result, parser


class TestAnalysisStructure:
    def test_summary_kept(self, analysis):
        result, _ = analysis
        assert result["summary"]["runtime_total_errors"] == 2
        assert len(result["summary"]["groups"]) == 2

    def test_focused_group(self, analysis):
        result, _ = analysis
        assert result["focused_group"]["signature"] == "ASSERTION_FAIL: apUNEXPECTED_ASSERTION"
        assert result["focused_group"]["first_time_ps"] == 290000

    def test_log_context(self, analysis):
        result, _ = analysis
        assert result["log_context"]["center_line"] == 2
        assert result["log_context"]["start_line"] == 1
        assert result["log_context"]["end_line"] == 3
        assert "apUNEXPECTED_ASSERTION" in result["log_context"]["context"]

    def test_wave_context(self, analysis):
        result, parser = analysis
        assert parser.calls[0]["center_time_ps"] == 290000
        assert parser.calls[0]["extra_transitions"] == 5
        assert result["wave_context"]["signals"]["top_tb.dut.req"]["value_at_center"]["bin"] == "0"

    def test_remaining_groups(self, analysis):
        result, _ = analysis
        assert result["remaining_groups"] == 1

    def test_focused_event(self, analysis):
        result, _ = analysis
        assert result["focused_event"]["group_signature"] == "ASSERTION_FAIL: apUNEXPECTED_ASSERTION"
        assert result["focused_event"]["instance_path"] == "top_tb.sva_top_inst.apUNEXPECTED_ASSERTION"

    def test_problem_hints(self, analysis):
        result, _ = analysis
        assert result["problem_hints"]["has_x"] is False
        assert result["problem_hints"]["has_z"] is False
        assert result["problem_hints"]["first_error_time_ps"] == 290000
        assert result["problem_hints"]["error_pattern"] == "protocol"


class TestAnalysisEdgeCases:
    def test_group_index_out_of_range(self, log_path):
        analyzer = WaveformAnalyzer(log_path, FakeWaveParser(), "vcs")
        with pytest.raises(IndexError):
            analyzer.analyze(["top_tb.dut.req"], group_index=5)

    def test_problem_hints_detects_z_heuristically(self):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        handle.write("module_z ERROR output is z @ 4 ns\n")
        handle.close()
        try:
            analyzer = WaveformAnalyzer(handle.name, FakeWaveParser(), "vcs")
            result = analyzer.analyze([], group_index=0)
            assert result["problem_hints"]["has_x"] is True
            assert result["problem_hints"]["has_z"] is True
            assert result["problem_hints"]["error_pattern"] == "zprop"
        finally:
            os.unlink(handle.name)


class TestFailureEventAnalysis:
    def test_analyze_failure_event_ranks_dut_signals(self, log_path):
        analyzer = WaveformAnalyzer(log_path, FakeWaveParser(), "vcs")
        event = SimLogParser(log_path, "vcs").parse_failure_events()[0]
        result = analyzer.analyze_failure_event(event, wave_path="/tmp/wave.vcd", top_hint="top_tb")

        assert result["time_anchor"]["kind"] == "exact"
        assert result["likely_instances"][0]["instance_path"] == "top_tb.sva_top_inst.apUNEXPECTED_ASSERTION"
        assert result["recommended_signals"][0]["path"] == "top_tb.dut.req"
        assert result["recommended_signals"][0]["role"] == "handshake"
        assert "active_near_failure" in result["recommended_signals"][0]["reason_codes"]

    def test_recommend_debug_next_steps_picks_primary_target(self, log_path):
        parser = FakeWaveParser()
        analyzer = WaveformAnalyzer(log_path, parser, "vcs")
        result = analyzer.recommend_debug_next_steps(wave_path="/tmp/wave.vcd", top_hint="top_tb")

        assert result["primary_failure_target"]["group_signature"] == "ASSERTION_FAIL: apUNEXPECTED_ASSERTION"
        assert result["recommended_signals"][0]["path"] == "top_tb.dut.req"
        assert result["suspected_failure_class"] == "assertion/protocol issue"
        assert result["recommendation_strategy"] == "role_rank_v1"
        assert result["failure_window_center_ps"] == 290000

    def test_recommend_debug_next_steps_without_top_hint(self, log_path):
        parser = FakeWaveParser()
        analyzer = WaveformAnalyzer(log_path, parser, "vcs")
        result = analyzer.recommend_debug_next_steps(wave_path="/tmp/wave.vcd")

        assert result["primary_failure_target"]["group_signature"] == "ASSERTION_FAIL: apUNEXPECTED_ASSERTION"
        assert result["recommended_signals"][0]["path"] == "top_tb.dut.req"
