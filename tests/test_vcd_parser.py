"""
test_vcd_parser.py
覆盖：VCD parser 与 FSDB parser 对齐的 around-time 返回结构。
"""

from __future__ import annotations

from pathlib import Path

from src.vcd_parser import VCDParser


VCD_SAMPLE = """\
$timescale 1ns $end
$scope module top_tb $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#10
0!
#15
1!
"""


def test_get_signals_around_time_has_pre_window_transitions(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    parser = VCDParser(str(wave))
    result = parser.get_signals_around_time(["top_tb.clk"], center_ps=12000, window_ps=3000, extra_transitions=2)

    assert result["extra_transitions"] == 2
    assert result["truncated"] is False
    signal = result["signals"]["top_tb.clk"]
    assert signal["value_at_center"]["bin"] == "0"
    assert len(signal["pre_window_transitions"]) == 2
    assert signal["pre_window_transitions"][0]["time_ps"] == 0
    assert signal["pre_window_transitions"][1]["time_ps"] == 5000
    assert signal["transitions_in_window"][0]["time_ps"] == 10000


def test_get_value_and_transitions_are_enriched(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    parser = VCDParser(str(wave))
    value = parser.get_value_at_time("top_tb.clk", 12000)
    transitions = parser.get_transitions("top_tb.clk")

    assert value["value"]["bin"] == "0"
    assert value["value"]["hex"] == "0x0"
    assert transitions["transitions"][1]["value"]["bin"] == "1"


def test_search_signals_prefers_dut_paths(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(
        """\
$timescale 1ns $end
$scope module top_tb $end
$scope module scoreboard $end
$var wire 1 ! req $end
$upscope $end
$scope module dut $end
$var wire 1 \" req $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
0"
"""
    )

    parser = VCDParser(str(wave))
    result = parser.search_signals("req")

    assert result["results"][0]["path"] == "top_tb.dut.req"


def test_search_signals_exposes_var_type_and_null_direction(tmp_path: Path):
    """VCD $var carries language type (wire/reg/...) but never carries port
    direction. search_signals should surface var_type and leave direction None
    so clients can detect the limitation without parsing the file format."""
    wave = tmp_path / "wave.vcd"
    wave.write_text(
        """\
$timescale 1ns $end
$scope module dut $end
$var wire 1 ! clk $end
$var reg  8 " state $end
$var real 1 # vdd $end
$var integer 32 $ counter $end
$var parameter 16 % WIDTH $end
$upscope $end
$enddefinitions $end
#0
"""
    )

    parser = VCDParser(str(wave))
    result = parser.search_signals("dut.")

    by_name = {item["path"].split(".")[-1]: item for item in result["results"]}
    assert by_name["clk"]["var_type"] == "wire"
    assert by_name["state"]["var_type"] == "reg"
    assert by_name["vdd"]["var_type"] == "real"
    assert by_name["counter"]["var_type"] == "integer"
    assert by_name["WIDTH"]["var_type"] == "parameter"
    # VCD has no port direction; every entry must expose this explicitly.
    assert all(item["direction"] is None for item in result["results"])


def test_summary_uses_transition_end_time_fallback(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    parser = VCDParser(str(wave))
    summary = parser.get_summary()

    assert summary["simulation_duration_ps"] == 15000
