from __future__ import annotations

from pathlib import Path

import pytest

from src.cycle_query import get_signals_by_cycle
from src.vcd_parser import VCDParser


FIXTURE = Path(__file__).parent / "fixtures" / "cycle_test.vcd"


def _parser() -> VCDParser:
    return VCDParser(str(FIXTURE))


def test_get_signals_by_cycle_basic():
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data", "top_tb.const_high"],
        num_cycles=3,
    )

    assert result["clock_period_ps"] == 1000
    assert result["total_edges_found"] == 3
    assert result["effective_num_cycles"] == 3
    assert result["num_cycles_returned"] == 3
    assert result["capped"] is False
    assert result["truncated"] is False
    assert [cycle["time_ps"] for cycle in result["cycles"]] == [500, 1500, 2500]
    assert result["cycles"][0]["signals"]["top_tb.data"]["dec"] == 1
    assert result["cycles"][1]["signals"]["top_tb.data"]["dec"] == 2
    assert result["cycles"][2]["signals"]["top_tb.data"]["dec"] == 3
    assert all(cycle["signals"]["top_tb.const_high"]["dec"] == 1 for cycle in result["cycles"])


def test_get_signals_by_cycle_supports_negedge():
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        edge="negedge",
        num_cycles=3,
    )

    assert [cycle["time_ps"] for cycle in result["cycles"]] == [1000, 2000, 3000]
    assert [cycle["signals"]["top_tb.data"]["dec"] for cycle in result["cycles"]] == [1, 2, 3]


def test_get_signals_by_cycle_honors_start_cycle_and_truncates():
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        start_cycle=1,
        num_cycles=5,
    )

    assert result["start_cycle"] == 1
    assert result["num_cycles_requested"] == 5
    assert result["effective_num_cycles"] == 5
    assert result["num_cycles_returned"] == 2
    assert result["truncated"] is True
    assert [cycle["cycle"] for cycle in result["cycles"]] == [1, 2]


def test_get_signals_by_cycle_reports_missing_signal_without_failing():
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data", "top_tb.missing"],
        num_cycles=2,
    )

    assert "top_tb.missing" in result["signal_errors"]
    assert [cycle["signals"]["top_tb.data"]["dec"] for cycle in result["cycles"]] == [1, 2]
    assert all("top_tb.missing" not in cycle["signals"] for cycle in result["cycles"])


def test_get_signals_by_cycle_skips_x_to_one_clock_edges(tmp_path: Path):
    wave = tmp_path / "xclock.vcd"
    wave.write_text(
        """\
$timescale 1ps $end
$scope module top_tb $end
$var wire 1 ! clk $end
$var wire 1 " data $end
$upscope $end
$enddefinitions $end
#0
x!
0"
#10
1!
1"
#20
0!
#30
1!
"""
    )
    result = get_signals_by_cycle(
        parser=VCDParser(str(wave)),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        num_cycles=2,
    )

    assert result["total_edges_found"] == 1
    assert [cycle["time_ps"] for cycle in result["cycles"]] == [30]


def test_get_signals_by_cycle_rejects_multibit_clock(tmp_path: Path):
    wave = tmp_path / "multibit_clock.vcd"
    wave.write_text(
        """\
$timescale 1ps $end
$scope module top_tb $end
$var wire 2 ! clk $end
$upscope $end
$enddefinitions $end
#0
b00 !
#10
b01 !
"""
    )

    with pytest.raises(ValueError, match="clock signal must be 1-bit"):
        get_signals_by_cycle(
            parser=VCDParser(str(wave)),
            clock_path="top_tb.clk",
            signal_paths=[],
        )


def test_get_signals_by_cycle_honors_sample_offset(tmp_path: Path):
    wave = tmp_path / "offset.vcd"
    wave.write_text(
        """\
$timescale 1ps $end
$scope module top_tb $end
$var wire 1 ! clk $end
$var wire 1 " q $end
$upscope $end
$enddefinitions $end
#0
0!
0"
#10
1!
#11
1"
#20
0!
"""
    )
    parser = VCDParser(str(wave))

    at_edge = get_signals_by_cycle(
        parser=parser,
        clock_path="top_tb.clk",
        signal_paths=["top_tb.q"],
        num_cycles=1,
        sample_offset_ps=0,
    )
    after_edge = get_signals_by_cycle(
        parser=parser,
        clock_path="top_tb.clk",
        signal_paths=["top_tb.q"],
        num_cycles=1,
        sample_offset_ps=1,
    )

    assert at_edge["cycles"][0]["signals"]["top_tb.q"]["dec"] == 0
    assert after_edge["cycles"][0]["signals"]["top_tb.q"]["dec"] == 1


def test_get_signals_by_cycle_rejects_negative_sample_offset():
    with pytest.raises(ValueError, match="sample_offset_ps must be >= 0"):
        get_signals_by_cycle(
            parser=_parser(),
            clock_path="top_tb.clk",
            signal_paths=["top_tb.data"],
            sample_offset_ps=-1,
        )


def test_get_signals_by_cycle_does_not_swallow_backend_runtime_error():
    class BrokenParser:
        def get_signal_width(self, signal_path: str) -> int:
            return 1

        def get_transitions(self, signal_path: str, start_ps: int = 0, end_ps: int = -1):
            if signal_path == "top_tb.clk":
                return {
                    "transitions": [
                        {"time_ps": 0, "value": {"bin": "0", "dec": 0}},
                        {"time_ps": 10, "value": {"bin": "1", "dec": 1}},
                    ]
                }
            raise RuntimeError("backend failed")

        def get_value_at_time(self, signal_path: str, time_ps: int):
            raise RuntimeError("backend failed")

    with pytest.raises(RuntimeError, match="backend failed"):
        get_signals_by_cycle(
            parser=BrokenParser(),
            clock_path="top_tb.clk",
            signal_paths=["top_tb.data"],
            num_cycles=1,
        )
