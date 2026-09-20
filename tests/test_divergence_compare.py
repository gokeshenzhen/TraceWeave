"""Independent comparison oracles for incomplete and same-time wave records."""
import pytest

from src.cancellation import OperationCancelled
from src.divergence_compare import compare_signals
from src.vcd_parser import VCDParser


class Parser:
    def __init__(self, events, *, width=1, end=30, truncated=False, scale=1000):
        self.events, self.width, self.end = events, width, end
        self.truncated, self.scale = truncated, scale

    def get_transitions(self, signal, start, end):
        events = [{"time_ps": t, "value": {"bin": v}} for t, v in self.events[signal]]
        previous = [e for e in events if e["time_ps"] < start]
        return {"transitions": [e for e in events if e["time_ps"] >= start and
                               (end < 0 or e["time_ps"] <= end)],
                "predecessor": previous[-1] if previous else None,
                "truncated": self.truncated}

    def get_signal_width(self, signal):
        return self.width

    def get_summary(self):
        return {"simulation_duration_ps": self.end, "scale_fs_per_tick": self.scale}

    def get_value_at_time(self, signal, time):
        events = [v for t, v in self.events[signal] if t <= time]
        return {"value": {"bin": events[-1] if events else None}}


def compare(parser, **kw):
    return compare_signals(get_parser=lambda _: parser, wave_path_a="a.vcd", signal_a="a",
                           wave_path_b="b.vcd", signal_b="b", **kw)


def reasons(result):
    return {g["reason"] for g in result["coverage_gaps"]}


def test_fold_all_records_at_same_timestamp_before_comparing():
    p = Parser({"a": [(0,"0"),(10,"1"),(10,"0")], "b": [(0,"0")]})
    r = compare(p)
    assert r["comparison_status"] == "equal"
    assert r["coverage_status"] == "complete"


def test_truncated_last_group_is_never_a_positive_observation():
    p = Parser({"a": [(0,"0"),(10,"1")], "b": [(0,"0"),(10,"0")]}, truncated=True)
    r = compare(p)
    assert not r["diverged"]
    assert r["comparison_status"] == "inconclusive"
    assert "transition_data_truncated" in reasons(r)


def test_positive_before_truncation_remains_proven():
    p = Parser({"a": [(0,"0"),(5,"1"),(10,"0")],
                "b": [(0,"0"),(10,"0")]}, truncated=True)
    r = compare(p)
    assert r["first_divergence_time_ps"] == 5
    assert r["earliest_difference_proven"]


def test_unknown_prefix_keeps_later_positive_but_not_earliest_claim():
    p = Parser({"a": [(0,"x"),(10,"0")], "b": [(0,"x"),(10,"1")]})
    r = compare(p)
    assert r["diverged"] and not r["earliest_difference_proven"]
    assert r["first_divergence_time_ps"] == 10
    assert "value_unknown" in reasons(r)


def test_missing_signal_is_inconclusive():
    r = compare(Parser({"a": [(0,"0")]}))
    assert r["missing_b"] and r["comparison_status"] == "inconclusive"


def test_undeclared_width_cannot_be_silently_truncated():
    p = Parser({"a": [(0,"100")], "b": [(0,"00")]}, width=2)
    r = compare(p)
    assert r["comparison_status"] == "inconclusive"
    assert "value_unavailable" in reasons(r)


def test_width_normalization_respects_declaration():
    p = Parser({"a": [(0,"1")], "b": [(0,"0001")]}, width=4)
    assert compare(p)["comparison_status"] == "equal"


def test_no_constant_extension_after_dump_end():
    r = compare(Parser({"a": [(0,"0")], "b": [(0,"0")]}, end=10), end_ps=20)
    assert r["end_ps"] == 10 and r["comparison_status"] == "inconclusive"
    assert len(r["excluded_intervals"]) == 2


def test_seed_uses_predecessor_for_empty_window_transitions():
    r = compare(Parser({"a": [(0,"0")], "b": [(0,"1")]}), start_ps=5, end_ps=10)
    assert r["first_divergence_time_ps"] == 5 and r["earliest_difference_proven"]


def test_real_subps_vcd_does_not_prove_erased_pulse_equal(tmp_path):
    wave = tmp_path / "subps.vcd"
    wave.write_text("""$timescale 100fs $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 @ b $end
$upscope $end
$enddefinitions $end
#0
0!
0@
#1
1!
#2
0!
#20
0@
""")
    parser = VCDParser(str(wave))
    r = compare_signals(get_parser=lambda _: parser, wave_path_a=str(wave), signal_a="top.a",
                        wave_path_b=str(wave), signal_b="top.b")
    assert r["comparison_status"] == "different"
    assert r["first_divergence_time_fs"] == 100
    assert r["first_divergence_time_ps"] == 1
    assert r["earliest_difference_proven"]
    assert "time_precision_loss" not in reasons(r)


def test_cancellation_is_not_missing_data():
    class Cancel(Parser):
        def get_transitions(self, *args):
            raise OperationCancelled("cancelled")
    with pytest.raises(OperationCancelled):
        compare(Cancel({}))


def test_comparison_uses_header_without_summary_listing():
    class HeaderParser(Parser):
        def get_header(self):
            return {"simulation_duration_ps": self.end, "scale_fs_per_tick": self.scale}

        def get_summary(self):
            pytest.fail("comparison must not request sample signals")

    events = {"a": [(0, "0"), (10, "1")], "b": [(0, "0")]}
    assert compare(HeaderParser(events)) == compare(Parser(events))
