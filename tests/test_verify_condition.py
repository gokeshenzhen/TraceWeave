"""Unit tests for src.verify_condition (auto-debug v2 MVP).

These tests use small synthesised VCD fixtures so the algorithm is
exercised without an FSDB runtime dependency. The FSDB code path shares
the same ``get_transitions`` / ``get_value_at_time`` contract, so the
algorithm-level coverage here applies to both backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cursor_store import CursorStore
from src.vcd_parser import VCDParser
from src.verify_condition import (
    diff_first_divergence,
    diff_value_distribution,
    period,
)


# Two parallel signals, identical for the first half of the dump, then
# diverge at #20 (signal b stays high while a falls).
TWO_SIGNAL_VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 # b $end
$upscope $end
$enddefinitions $end
#0
0!
0#
#5
1!
1#
#10
0!
0#
#15
1!
1#
#20
0!
1#
#25
1!
1#
"""


IDENTICAL_VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 # b $end
$upscope $end
$enddefinitions $end
#0
0!
0#
#5
1!
1#
#10
0!
0#
"""


# Two signals that DIFFER from time 0 with no transitions inside the
# window — must still be detected via the seed-value read.
DIFFER_AT_START_VCD = """\
$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 # b $end
$upscope $end
$enddefinitions $end
#0
0!
1#
#100
0!
1#
"""


def _write(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def _parser_factory():
    cache: dict[str, VCDParser] = {}

    def get_parser(path: str) -> VCDParser:
        if path not in cache:
            cache[path] = VCDParser(path)
        return cache[path]

    return get_parser


def test_diff_first_divergence_within_one_waveform(tmp_path: Path):
    wave = _write(tmp_path, "two_signals.vcd", TWO_SIGNAL_VCD)
    store = CursorStore()
    result = diff_first_divergence(
        get_parser=_parser_factory(),
        wave_path_a=wave,
        signal_a="top.a",
        wave_path_b=wave,
        signal_b="top.b",
        cursor_store=store,
    )
    assert result["diverged"] is True
    # First divergence is at #20 (20ns = 20_000 ps), where a=0 but b=1.
    assert result["first_divergence_time_ps"] == 20_000
    assert result["value_a"] == "0"
    assert result["value_b"] == "1"
    assert result["cursor"] is not None
    cur = result["cursor"]
    assert cur["time_ps"] == 20_000
    assert cur["name"].startswith("div_")
    assert cur["metadata"]["source"] == "diff_first_divergence"
    # Cursor is registered in the store and recoverable by name.
    assert store.get(cur["name"]) is not None


def test_diff_first_divergence_identical_signals_returns_no_diff(tmp_path: Path):
    wave = _write(tmp_path, "identical.vcd", IDENTICAL_VCD)
    store = CursorStore()
    result = diff_first_divergence(
        get_parser=_parser_factory(),
        wave_path_a=wave,
        signal_a="top.a",
        wave_path_b=wave,
        signal_b="top.b",
        cursor_store=store,
    )
    assert result["diverged"] is False
    assert result["first_divergence_time_ps"] is None
    assert result["cursor"] is None
    # No cursor must have been registered.
    assert len(store) == 0


def test_diff_first_divergence_at_window_start(tmp_path: Path):
    wave = _write(tmp_path, "differ_start.vcd", DIFFER_AT_START_VCD)
    store = CursorStore()
    result = diff_first_divergence(
        get_parser=_parser_factory(),
        wave_path_a=wave,
        signal_a="top.a",
        wave_path_b=wave,
        signal_b="top.b",
        cursor_store=store,
    )
    assert result["diverged"] is True
    assert result["first_divergence_time_ps"] == 0
    assert result["cursor"]["time_ps"] == 0


def test_diff_first_divergence_window_clamps_search(tmp_path: Path):
    wave = _write(tmp_path, "two_signals.vcd", TWO_SIGNAL_VCD)
    # Constrain the window to before the divergence at 20ns.
    result = diff_first_divergence(
        get_parser=_parser_factory(),
        wave_path_a=wave,
        signal_a="top.a",
        wave_path_b=wave,
        signal_b="top.b",
        start_ps=0,
        end_ps=15_000,
    )
    assert result["diverged"] is False
    assert result["first_divergence_time_ps"] is None


def test_diff_first_divergence_explicit_cursor_name(tmp_path: Path):
    wave = _write(tmp_path, "two_signals.vcd", TWO_SIGNAL_VCD)
    store = CursorStore()
    result = diff_first_divergence(
        get_parser=_parser_factory(),
        wave_path_a=wave,
        signal_a="top.a",
        wave_path_b=wave,
        signal_b="top.b",
        cursor_store=store,
        cursor_name="my_divergence",
        cursor_note="run-A vs run-B at fifo_rdata",
    )
    assert result["cursor"]["name"] == "my_divergence"
    assert result["cursor"]["note"] == "run-A vs run-B at fifo_rdata"
    assert store.get("my_divergence").time_ps == result["first_divergence_time_ps"]


class _FsdbStyleParser:
    """Mimics the FSDB backend, which echoes end_ps=-1 ("to end of sim")
    straight back from get_transitions instead of resolving it (the VCD
    backend resolves it). Regression guard for the window-collapse bug."""

    def __init__(self, transitions_a, transitions_b):
        self._t = {"sa": transitions_a, "sb": transitions_b}

    def get_transitions(self, signal, start_ps=0, end_ps=-1):
        return {
            "signal": signal,
            "start_ps": start_ps,
            "end_ps": end_ps,  # echoes -1 unchanged, like FSDB
            "transitions": [
                {"time_ps": t, "value": {"bin": v}} for t, v in self._t[signal]
            ],
        }

    def get_value_at_time(self, signal, time_ps):
        val = None
        for t, v in self._t[signal]:
            if t <= time_ps:
                val = v
            else:
                break
        return {"signal": signal, "time_ps": time_ps, "value": {"bin": val}}


def test_diff_first_divergence_handles_end_ps_minus_one_echo():
    # sa and sb agree until t=300 where sb diverges. With end_ps=-1 echoed
    # back, the window must NOT collapse (the original bug returned False).
    sa = [(0, "0000"), (100, "0001"), (200, "0010"), (300, "1001"), (400, "0100")]
    sb = [(0, "0000"), (100, "0001"), (200, "0010"), (300, "0001"), (400, "0100")]
    parser = _FsdbStyleParser(sa, sb)
    store = CursorStore()
    result = diff_first_divergence(
        get_parser=lambda _p: parser,
        wave_path_a="x.fsdb", signal_a="sa",
        wave_path_b="x.fsdb", signal_b="sb",
        end_ps=-1,
        cursor_store=store,
    )
    assert result["diverged"] is True
    assert result["first_divergence_time_ps"] == 300
    assert result["value_a"] == "1001"
    assert result["value_b"] == "0001"
    assert result["note"] is None


def test_diff_first_divergence_no_cursor_store_returns_no_cursor(tmp_path: Path):
    wave = _write(tmp_path, "two_signals.vcd", TWO_SIGNAL_VCD)
    result = diff_first_divergence(
        get_parser=_parser_factory(),
        wave_path_a=wave,
        signal_a="top.a",
        wave_path_b=wave,
        signal_b="top.b",
        cursor_store=None,
    )
    assert result["diverged"] is True
    assert result["cursor"] is None


# --- period -----------------------------------------------------------------

# Clean 10ps-period clock (1ps timescale): posedges at 10,20,30,40.
CLEAN_CLOCK_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#10
1!
#15
0!
#20
1!
#25
0!
#30
1!
#35
0!
#40
1!
"""

# Same but a beat is late: posedges at 10,20,35,45 → off-beat ends at 35.
JITTER_CLOCK_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#10
1!
#13
0!
#20
1!
#23
0!
#35
1!
#38
0!
#45
1!
"""

# Only two posedges — not enough to estimate a period.
SPARSE_CLOCK_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
#0
0!
#10
1!
#15
0!
#20
1!
"""


def test_period_clean_clock(tmp_path: Path):
    wave = _write(tmp_path, "clean.vcd", CLEAN_CLOCK_VCD)
    store = CursorStore()
    result = period(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.clk",
        edge="posedge",
        cursor_store=store,
    )
    assert result["period_ps"] == 10
    assert result["edges_used"] == 4
    assert result["off_beat_count"] == 0
    assert result["jitter_ps"] == 0
    assert result["first_off_beat_time_ps"] is None
    assert result["cursor"] is None
    assert len(store) == 0


def test_period_detects_off_beat_and_registers_cursor(tmp_path: Path):
    wave = _write(tmp_path, "jitter.vcd", JITTER_CLOCK_VCD)
    store = CursorStore()
    result = period(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.clk",
        edge="posedge",
        cursor_store=store,
    )
    # posedges 10,20,35,45 → deltas 10,15,10 → median 10.
    assert result["period_ps"] == 10
    assert result["off_beat_count"] == 1
    assert result["jitter_ps"] == 5
    assert result["first_off_beat_time_ps"] == 35
    assert result["cursor"]["time_ps"] == 35
    assert result["cursor"]["name"].startswith("beat_")
    assert store.get(result["cursor"]["name"]) is not None


def test_period_not_enough_edges(tmp_path: Path):
    wave = _write(tmp_path, "sparse.vcd", SPARSE_CLOCK_VCD)
    result = period(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.clk",
        edge="posedge",
    )
    assert result["period_ps"] is None
    assert result["edges_used"] == 2
    assert "not enough" in result["reason"]


def test_period_any_edge_counts_all_transitions(tmp_path: Path):
    wave = _write(tmp_path, "clean.vcd", CLEAN_CLOCK_VCD)
    result = period(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.clk",
        edge="any",
    )
    # Transitions at 0,10,15,20,25,30,35,40 → 8 edges, intervals of 10/5/5...
    # median of [10,5,5,5,5,5,5] = 5.
    assert result["edges_used"] == 8
    assert result["period_ps"] == 5


def test_period_explicit_cursor_name(tmp_path: Path):
    wave = _write(tmp_path, "jitter.vcd", JITTER_CLOCK_VCD)
    store = CursorStore()
    result = period(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.clk",
        edge="posedge",
        cursor_store=store,
        cursor_name="clk_stall",
    )
    assert result["cursor"]["name"] == "clk_stall"
    assert store.get("clk_stall").time_ps == 35


def test_period_invalid_edge_raises(tmp_path: Path):
    wave = _write(tmp_path, "clean.vcd", CLEAN_CLOCK_VCD)
    with pytest.raises(ValueError):
        period(
            get_parser=_parser_factory(),
            wave_path=wave,
            signal="top.clk",
            edge="bothedges",
        )


# --- diff_value_distribution ------------------------------------------------

# 8-bit signal. bit 3 is the discriminator: high in the "failing" samples
# (0x08, 0x09, 0x0C) and low in the "passing" samples (0x00, 0x01, 0x04).
# Sample at 0x60 carries an X in bit 2 for the X-tracking test.
DIST_VCD = """\
$timescale 1ps $end
$scope module top $end
$var wire 8 ! sig $end
$upscope $end
$enddefinitions $end
#0
b0 !
#10
b1000 !
#20
b1 !
#30
b1001 !
#40
b100 !
#50
b1100 !
#60
b1x00 !
"""


def test_diff_value_distribution_finds_discriminative_bit(tmp_path: Path):
    wave = _write(tmp_path, "dist.vcd", DIST_VCD)
    result = diff_value_distribution(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.sig",
        group_a_times=[15, 35, 55],  # 0x08, 0x09, 0x0C  (bit3=1)
        group_b_times=[5, 25, 45],   # 0x00, 0x01, 0x04  (bit3=0)
    )
    assert result["group_a"]["n_samples"] == 3
    assert result["group_b"]["n_samples"] == 3
    # Bit 3 perfectly separates the two groups.
    assert result["discriminative_bits"] == [3]
    bit3 = next(b for b in result["bit_diff"] if b["bit"] == 3)
    assert bit3["p1_a"] == 1.0
    assert bit3["p1_b"] == 0.0
    assert bit3["delta"] == 1.0


def test_diff_value_distribution_value_enrichment(tmp_path: Path):
    wave = _write(tmp_path, "dist.vcd", DIST_VCD)
    result = diff_value_distribution(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.sig",
        group_a_times=[15, 35, 55],
        group_b_times=[5, 25, 45],
    )
    # Every distinct value sits entirely in one group → |delta| == 1/3.
    top = result["value_enrichment"][0]
    assert abs(top["delta"]) == pytest.approx(1 / 3, abs=1e-3)
    # A-only values have positive delta, B-only negative.
    for row in result["value_enrichment"]:
        if row["count_a"] > 0:
            assert row["delta"] > 0
        else:
            assert row["delta"] < 0


def test_diff_value_distribution_single_group_histogram(tmp_path: Path):
    wave = _write(tmp_path, "dist.vcd", DIST_VCD)
    result = diff_value_distribution(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.sig",
        group_a_times=[15, 35, 55],
        group_b_times=None,
    )
    assert result["group_b"] is None
    assert result["bit_diff"] == []
    assert result["discriminative_bits"] == []
    assert result["group_a"]["distinct"] == 3
    assert "single-group" in result["note"]


def test_diff_value_distribution_tracks_x_fraction(tmp_path: Path):
    wave = _write(tmp_path, "dist.vcd", DIST_VCD)
    result = diff_value_distribution(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.sig",
        group_a_times=[65],  # 0x1x00 — bit 2 is X
        group_b_times=[5],   # 0x00
    )
    bit2 = next(b for b in result["bit_diff"] if b["bit"] == 2)
    assert bit2["x_frac_a"] == 1.0
    # bit 2 is X in group A, so p1_a is undefined (no known 0/1 sample).
    assert bit2["p1_a"] is None
    assert bit2["delta"] is None


def test_diff_value_distribution_empty_group_note(tmp_path: Path):
    wave = _write(tmp_path, "dist.vcd", DIST_VCD)
    result = diff_value_distribution(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.sig",
        group_a_times=[15],
        group_b_times=[],
    )
    # Empty group_b_times is falsy → single-group histogram path.
    assert result["group_b"] is None
    assert "single-group" in result["note"]


def test_diff_value_distribution_flags_all_unreadable(tmp_path: Path):
    wave = _write(tmp_path, "dist.vcd", DIST_VCD)
    result = diff_value_distribution(
        get_parser=_parser_factory(),
        wave_path=wave,
        signal="top.does_not_exist",  # bad path → every sample is '?'
        group_a_times=[15, 35, 55],
        group_b_times=[5, 25, 45],
    )
    assert result["group_a"]["unreadable"] == 3
    assert "unreadable" in result["note"]
    # Must not silently proceed to a bogus bit_diff.
    assert result["bit_diff"] == []
