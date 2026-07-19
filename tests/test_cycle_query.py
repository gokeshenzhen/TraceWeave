from __future__ import annotations

import threading
from pathlib import Path

import pytest

import src.cancellation as cancellation
from src import operation_metrics
from src.cancellation import OperationCancelled
from src.cycle_query import (
    EdgeSamplingSession,
    annotate_center_transients,
    get_signals_by_cycle,
    sample_signals_on_edges,
)
from src.vcd_parser import VCDParser


FIXTURE = Path(__file__).parent / "fixtures" / "cycle_test.vcd"


def _v(h):
    return {"bin": None, "hex": h, "dec": None}


def test_annotate_flags_dip_and_return_glitch():
    # X -> glitch(0) -> X across the clock edge: value_at_center is the glitch, the
    # signal returns to the pre-value 1ns later -> sub-cycle transient.
    res = {
        "center_time_ps": 175001,
        "signals": {
            "s.HWDATA": {
                "value_at_center": _v("0x0"),
                "transitions_in_window": [
                    {"time_ps": 174000, "value": _v("0x2773345d")},
                    {"time_ps": 175000, "value": _v("0x0")},
                    {"time_ps": 176000, "value": _v("0x2773345d")},
                ],
                "pre_window_transitions": [],
            }
        },
    }
    annotate_center_transients(res)
    sig = res["signals"]["s.HWDATA"]
    assert sig["center_transient"] is True
    assert sig["center_settles_to"]["hex"] == "0x2773345d"
    assert sig["center_settle_ps"] == 176000
    assert res["transient_note"] and "SUB-CYCLE TRANSIENT" in res["transient_note"]


def test_annotate_does_not_flag_a_normal_cycle_boundary_change():
    # A legit transfer A -> B (does NOT return to A): not a dip, must not be flagged.
    res = {
        "center_time_ps": 175001,
        "signals": {
            "s.HWDATA": {
                "value_at_center": _v("0xB"),
                "transitions_in_window": [
                    {"time_ps": 174000, "value": _v("0xA")},
                    {"time_ps": 175000, "value": _v("0xB")},
                    {"time_ps": 185000, "value": _v("0xC")},
                ],
                "pre_window_transitions": [],
            }
        },
    }
    annotate_center_transients(res)
    assert res["signals"]["s.HWDATA"].get("center_transient") is None
    assert res.get("transient_note") is None


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


def test_get_signals_by_cycle_start_time_snaps_to_edge():
    # 1000ps falls between edges 500 and 1500 -> snap forward to 1500 (cycle 1).
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        start_time_ps=1000,
        num_cycles=2,
    )

    assert result["resolved_from_time"] is True
    assert result["requested_start_time_ps"] == 1000
    assert result["start_cycle"] == 1
    assert [cycle["time_ps"] for cycle in result["cycles"]] == [1500, 2500]
    assert result["cycles"][0]["signals"]["top_tb.data"]["dec"] == 2


def test_get_signals_by_cycle_start_time_before_first_edge_starts_at_zero():
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        start_time_ps=100,
        num_cycles=1,
    )

    assert result["start_cycle"] == 0
    assert result["cycles"][0]["time_ps"] == 500


def test_get_signals_by_cycle_time_window_counts_edges_inclusive():
    # [500, 1500] inclusive on both ends -> edges 500 and 1500 -> 2 cycles.
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        start_time_ps=500,
        end_time_ps=1500,
    )

    assert result["resolved_from_time"] is True
    assert result["requested_end_time_ps"] == 1500
    assert result["num_cycles_returned"] == 2
    assert [cycle["time_ps"] for cycle in result["cycles"]] == [500, 1500]


def test_get_signals_by_cycle_partial_trailing_period_contributes_no_cycle():
    # [500, 1400]: the trailing partial period (1400 < next edge 1500) holds no
    # edge -> only the 500 edge is counted. No fractional-cycle rounding.
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        start_time_ps=500,
        end_time_ps=1400,
    )

    assert result["num_cycles_returned"] == 1
    assert [cycle["time_ps"] for cycle in result["cycles"]] == [500]


def test_get_signals_by_cycle_time_window_respects_max_cycles_cap():
    result = get_signals_by_cycle(
        parser=_parser(),
        clock_path="top_tb.clk",
        signal_paths=["top_tb.data"],
        start_time_ps=500,
        end_time_ps=2500,
        max_cycles=2,
    )

    assert result["capped"] is True
    assert result["num_cycles_requested"] == 3
    assert result["effective_num_cycles"] == 2
    assert result["num_cycles_returned"] == 2


def test_get_signals_by_cycle_rejects_negative_start_time():
    with pytest.raises(ValueError, match="start_time_ps"):
        get_signals_by_cycle(
            parser=_parser(),
            clock_path="top_tb.clk",
            signal_paths=["top_tb.data"],
            start_time_ps=-1,
        )


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


def test_edge_sampler_propagates_transition_prefix_truncation():
    class TruncatedParser:
        def get_signal_width(self, signal_path: str) -> int:
            return 1

        def get_transitions(self, signal_path: str, start_ps: int = 0, end_ps: int = -1):
            if signal_path == "top.clk":
                return {
                    "truncated": True,
                    "transitions": [
                        {"time_ps": 0, "value": {"bin": "0", "dec": 0}},
                        {"time_ps": 10, "value": {"bin": "1", "dec": 1}},
                    ],
                }
            return {
                "truncated": True,
                "transitions": [
                    {"time_ps": 0, "value": {"bin": "1", "dec": 1}},
                ],
            }

        def get_value_at_time(self, signal_path: str, time_ps: int):
            return {"value": {"bin": "1", "dec": 1}}

    result = sample_signals_on_edges(
        TruncatedParser(), "top.clk", ["top.valid"], start_ps=0, end_ps=20
    )

    assert result["transition_data_truncated"] is True
    assert result["transition_signals_truncated"] == ["top.clk", "top.valid"]


class _CountingSamplingParser:
    def __init__(self):
        self.calls: dict[str, int] = {}
        self.values = {
            "top.clk": [
                {"time_ps": 0, "value": {"bin": "0", "dec": 0}},
                {"time_ps": 10, "value": {"bin": "1", "dec": 1}},
                {"time_ps": 20, "value": {"bin": "0", "dec": 0}},
                {"time_ps": 30, "value": {"bin": "1", "dec": 1}},
            ],
            "top.v0": [{"time_ps": 0, "value": {"bin": "1", "dec": 1}}],
            "top.v1": [{"time_ps": 0, "value": {"bin": "1", "dec": 1}}],
            "top.shared_ready": [
                {"time_ps": 0, "value": {"bin": "1", "dec": 1}}
            ],
        }

    def get_signal_width(self, signal_path: str) -> int:
        return 1

    def get_transitions(self, signal_path: str, start_ps: int = 0, end_ps: int = -1):
        self.calls[signal_path] = self.calls.get(signal_path, 0) + 1
        return {"transitions": self.values[signal_path], "truncated": False}

    def get_value_at_time(self, signal_path: str, time_ps: int):
        return {"value": self.values[signal_path][0]["value"]}


def test_sampling_session_reuses_clock_and_shared_signal_then_evicts():
    parser = _CountingSamplingParser()
    session = EdgeSamplingSession(
        clock_path="top.clk",
        start_ps=0,
        end_ps=40,
        edge="posedge",
        sample_offset_ps=1,
        signal_use_counts={"top.v0": 1, "top.v1": 1, "top.shared_ready": 2},
    )
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value("_sweep_active", True)
    try:
        first = sample_signals_on_edges(
            parser, "top.clk", ["top.v0", "top.shared_ready"],
            start_ps=0, end_ps=40, sampling_session=session,
        )
        second = sample_signals_on_edges(
            parser, "top.clk", ["top.v1", "top.shared_ready"],
            start_ps=0, end_ps=40, sampling_session=session,
        )
    finally:
        operation_metrics.pop(token)

    assert first["samples"][0]["signals"]["top.shared_ready"]["dec"] == 1
    assert second["samples"][0]["signals"]["top.shared_ready"]["dec"] == 1
    assert parser.calls == {
        "top.clk": 1,
        "top.v0": 1,
        "top.shared_ready": 1,
        "top.v1": 1,
    }
    assert session._signal_transition_cache == {}
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_clock_reuse_hits"] == 1
    assert snapshot["sweep_signal_reuse_hits"] == 1


def test_sampling_session_checks_cancellation_before_cached_clock_reuse():
    parser = _CountingSamplingParser()
    session = EdgeSamplingSession(
        clock_path="top.clk", start_ps=0, end_ps=40,
        edge="posedge", sample_offset_ps=1,
    )
    sample_signals_on_edges(
        parser, "top.clk", ["top.v0"],
        start_ps=0, end_ps=40, sampling_session=session,
    )
    event = threading.Event()
    token = cancellation.push_cancel_event(event)
    event.set()
    try:
        with pytest.raises(OperationCancelled):
            sample_signals_on_edges(
                parser, "top.clk", ["top.v1"],
                start_ps=0, end_ps=40, sampling_session=session,
            )
    finally:
        cancellation.pop_cancel_event(token)

    assert parser.calls["top.clk"] == 1


def test_sampling_session_accepts_consistent_resolved_fsdb_path_aliases():
    parser = _CountingSamplingParser()
    parser.values["top.clk[0:0]"] = parser.values.pop("top.clk")
    parser.values["top.ready[0:0]"] = parser.values.pop("top.shared_ready")
    session = EdgeSamplingSession(
        clock_path="top.clk", start_ps=0, end_ps=40,
        edge="posedge", sample_offset_ps=1,
        signal_use_counts={"top.ready": 2},
    )
    session.bind_signal_alias("top.ready", "top.ready[0:0]")

    for _ in range(2):
        sample_signals_on_edges(
            parser, "top.clk[0:0]", ["top.ready[0:0]"],
            start_ps=0, end_ps=40, sampling_session=session,
        )

    assert parser.calls["top.clk[0:0]"] == 1
    assert parser.calls["top.ready[0:0]"] == 1
    assert session._signal_transition_cache == {}


def test_sampling_session_propagates_cached_clock_truncation_to_all_consumers():
    class TruncatedClockParser(_CountingSamplingParser):
        def get_transitions(
            self, signal_path: str, start_ps: int = 0, end_ps: int = -1
        ):
            result = super().get_transitions(signal_path, start_ps, end_ps)
            result["truncated"] = signal_path == "top.clk"
            return result

    parser = TruncatedClockParser()
    session = EdgeSamplingSession(
        clock_path="top.clk", start_ps=0, end_ps=40,
        edge="posedge", sample_offset_ps=1,
    )

    first = sample_signals_on_edges(
        parser, "top.clk", ["top.v0"], start_ps=0, end_ps=40,
        sampling_session=session,
    )
    second = sample_signals_on_edges(
        parser, "top.clk", ["top.v1"], start_ps=0, end_ps=40,
        sampling_session=session,
    )

    assert first["transition_data_truncated"] is True
    assert second["transition_data_truncated"] is True
    assert first["transition_signals_truncated"] == ["top.clk"]
    assert second["transition_signals_truncated"] == ["top.clk"]
    assert parser.calls["top.clk"] == 1


def test_edge_sampler_dedupes_duplicate_signal_within_one_interface():
    parser = _CountingSamplingParser()
    result = sample_signals_on_edges(
        parser, "top.clk", ["top.shared_ready", "top.shared_ready"],
        start_ps=0, end_ps=40,
    )

    assert result["signal_errors"] == {}
    assert parser.calls["top.shared_ready"] == 1
