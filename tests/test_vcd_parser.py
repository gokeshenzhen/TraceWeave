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


def test_extra_transitions_zero_returns_no_pre_window_history(tmp_path: Path):
    """extra_transitions=0 must mean ZERO pre-window entries — the bare
    [-0:] slice used to leak the ENTIRE pre-window history."""
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    parser = VCDParser(str(wave))
    result = parser.get_signals_around_time(
        ["top_tb.clk"], center_ps=12000, window_ps=1000, extra_transitions=0
    )

    signal = result["signals"]["top_tb.clk"]
    assert signal["pre_window_transitions"] == []
    # the window itself is empty here; the centre value must still be present
    assert signal["value_at_center"]["bin"] == "0"


def test_extra_transitions_one_returns_only_most_recent(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    parser = VCDParser(str(wave))
    result = parser.get_signals_around_time(
        ["top_tb.clk"], center_ps=12000, window_ps=1000, extra_transitions=1
    )

    signal = result["signals"]["top_tb.clk"]
    assert [t["time_ps"] for t in signal["pre_window_transitions"]] == [10000]


def test_extra_transitions_default_caps_pre_window(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    parser = VCDParser(str(wave))
    result = parser.get_signals_around_time(
        ["top_tb.clk"], center_ps=15000, window_ps=100
    )

    assert result["extra_transitions"] == 5
    signal = result["signals"]["top_tb.clk"]
    assert len(signal["pre_window_transitions"]) <= 5


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


# ── 跨 timescale 回归（sub-ps 刻度曾把 timescale_ps 截成 0，所有时间戳坍缩到 0）──

def _scaled_vcd(timescale: str) -> str:
    return f"""\
$timescale {timescale} $end
$scope module top_tb $end
$var reg 8 ! data $end
$upscope $end
$enddefinitions $end
#0
b00000000 !
#1000
b10101010 !
#1001
b11001100 !
"""


def test_sub_ps_timescale_100fs(tmp_path: Path):
    """100fs/tick: tick 1000 = 100000 fs = 100 ps real time. The old integer-ps
    timescale (int(0.1) == 0) multiplied every timestamp by zero."""
    wave = tmp_path / "wave.vcd"
    wave.write_text(_scaled_vcd("100fs"))

    parser = VCDParser(str(wave))
    summary = parser.get_summary()
    assert summary["scale_fs_per_tick"] == 100
    assert summary["scale_unit"] == "100fs"
    assert summary["timescale_ps"] == 0.1
    assert summary["simulation_duration_ps"] == 101  # ceil(1001 * 0.1)

    r = parser.get_transitions("top_tb.data")
    assert [t["time_ps"] for t in r["transitions"]] == [0, 100, 101]
    # external ground-truth ps input must hit the real instant
    assert parser.get_value_at_time("top_tb.data", 100)["value"]["bin"] == "10101010"
    assert parser.get_value_at_time("top_tb.data", 99)["value"]["bin"] == "00000000"
    # reported-timestamp roundtrip returns the post-transition value
    for t in r["transitions"]:
        back = parser.get_value_at_time("top_tb.data", t["time_ps"])
        assert back["value"]["bin"] == t["value"]["bin"]


def test_super_ps_timescale_1ns(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(_scaled_vcd("1ns"))

    parser = VCDParser(str(wave))
    summary = parser.get_summary()
    assert summary["scale_fs_per_tick"] == 1_000_000
    assert summary["timescale_ps"] == 1000
    assert summary["simulation_duration_ps"] == 1_001_000

    r = parser.get_transitions("top_tb.data")
    assert [t["time_ps"] for t in r["transitions"]] == [0, 1_000_000, 1_001_000]


def test_missing_timescale_assumes_1ps_and_says_so(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    vcd = _scaled_vcd("1ps").replace("$timescale 1ps $end\n", "")
    wave.write_text(vcd)

    parser = VCDParser(str(wave))
    summary = parser.get_summary()
    assert summary["scale_fs_per_tick"] == 1000
    assert summary["scale_unit"] == "1ps(assumed)"
