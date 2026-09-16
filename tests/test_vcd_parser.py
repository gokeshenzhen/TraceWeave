"""
test_vcd_parser.py
覆盖：VCD parser 与 FSDB parser 对齐的 around-time 返回结构。
"""

from __future__ import annotations

from pathlib import Path
import random

import pytest

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


def test_get_transitions_is_strict_and_exposes_predecessor(tmp_path: Path):
    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)

    result = VCDParser(str(wave)).get_transitions(
        "top_tb.clk", start_ps=6000, end_ps=15000
    )

    assert [item["time_ps"] for item in result["transitions"]] == [10000, 15000]
    assert result["predecessor"]["time_ps"] == 5000
    assert result["predecessor"]["value"]["bin"] == "1"
    assert all(6000 <= item["time_ps"] <= 15000 for item in result["transitions"])


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


@pytest.mark.parametrize("scale,fs", [("100fs", 100), ("1ps", 1000), ("1ns", 1_000_000)])
def test_reads_match_linear_oracle_with_duplicate_times(tmp_path, scale, fs):
    """Preserve event order, X/Z, closed boundaries and strict predecessors."""
    rng = random.Random(716)
    content = [f"$timescale {scale} $end", "$scope module dut $end",
               "$var wire 1 ! sig $end", "$var wire 1 ? empty $end",
               "$upscope $end", "$enddefinitions $end"]
    rows = []
    tick = 7  # Also exercise queries before the first recorded value.
    for _ in range(100):
        tick += rng.randrange(4)
        value = rng.choice("01xz")
        timestamp = (tick * fs + 999) // 1000
        enriched = {"bin": value, "hex": "0x" + value if value in "01" else None,
                    "dec": int(value) if value in "01" else None}
        rows.append({"time_ps": timestamp, "time_ns": timestamp / 1000, "value": enriched})
        content.extend((f"#{tick}", value + "!"))
    wave = tmp_path / "boundaries.vcd"
    wave.write_text("\n".join(content))
    parser = VCDParser(str(wave))
    end = rows[-1]["time_ps"]
    queries = [-1, 0, end, end + 1] + [row["time_ps"] for row in rows]
    for timestamp in queries:
        preceding = [r for r in rows if r["time_ps"] <= timestamp]
        assert parser.get_value_at_time("sig", timestamp)["value"] == (preceding[-1]["value"] if preceding else None)
        if timestamp < 0:  # -1 is the range API's end-of-file sentinel.
            continue
        for width in (0, 1, 1000):
            start, stop = max(0, timestamp - width), timestamp + width
            inside = [r for r in rows if start <= r["time_ps"] <= stop]
            before = [r for r in rows if r["time_ps"] < start]
            result = parser.get_transitions("dut.sig", start, stop)
            assert result["transitions"] == inside
            assert result["transition_count"] == len(inside)
            assert result["predecessor"] == (before[-1] if before else None)
            for extra in (0, 1, 5):
                around = parser.get_signals_around_time(["dut.sig", "dut.empty"], timestamp, width, extra)
                signal = around["signals"]["dut.sig"]
                assert signal["value_at_center"] == (preceding[-1]["value"] if preceding else None)
                assert signal["transitions_in_window"] == inside
                assert signal["pre_window_transitions"] == (before[-extra:] if extra else [])
                assert around["signals"]["dut.empty"] == {
                    "value_at_center": None, "transitions_in_window": [], "pre_window_transitions": []}
    assert parser.get_transitions("sig", end + 1, -1)["transitions"] == []
    assert parser.get_value_at_time("empty", end)["value"] is None


def test_small_reads_do_not_visit_full_signal_history(tmp_path):
    """A million-event signal must support small reads with bounded work."""
    class Records:
        visits = 0

        def __len__(self):
            return 1_000_001

        def __getitem__(self, index):
            if isinstance(index, slice):
                return [self[i] for i in range(*index.indices(len(self)))]
            if index < 0:
                index += len(self)
            if not 0 <= index < len(self):
                raise IndexError(index)
            self.visits += 1
            assert self.visits < 1000, "small query walked the full signal history"
            return index * 10, str(index % 2)

    wave = tmp_path / "wave.vcd"
    wave.write_text(VCD_SAMPLE)
    parser = VCDParser(str(wave))
    parser._ensure_parsed()
    records = Records()
    parser._transitions["!"] = records
    assert parser.get_value_at_time("top_tb.clk", 9_500_010)["value"]["bin"] == "1"
    result = parser.get_transitions("top_tb.clk", 9_500_010, 9_500_030)
    assert [r["time_ps"] for r in result["transitions"]] == [9_500_010, 9_500_020, 9_500_030]
    signal = parser.get_signals_around_time(["top_tb.clk"], 9_500_020, 10, 2)["signals"]["top_tb.clk"]
    assert signal["transitions_in_window"] == result["transitions"]
    assert [r["time_ps"] for r in signal["pre_window_transitions"]] == [9_499_990, 9_500_000]
