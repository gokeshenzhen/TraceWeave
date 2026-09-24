"""Independent edge-before oracles for protocol acceptance and its boundaries."""
import pytest

from src.cycle_query import get_signals_by_cycle, sample_signals_on_edges
from src.cursor_store import CursorStore
from src.txn_reconstruct import reconstruct_transactions
from src.vcd_parser import VCDParser
from src.verify_condition import inspect_handshake


def wave(tmp_path, body, *, scale="1ps", signals=None):
    signals = signals or [(1, "!", "clk"), (1, "v", "valid"), (1, "r", "ready")]
    path = tmp_path / "edges.vcd"
    header = [f"$timescale {scale} $end", "$scope module tb $end"]
    header += [f"$var wire {width} {code} {name} $end" for width, code, name in signals]
    path.write_text("\n".join([*header, "$upscope $end", "$enddefinitions $end", body]))
    return VCDParser(str(path))


def inspect(parser, **kwargs):
    return inspect_handshake(get_parser=lambda _: parser, wave_path=parser.file_path,
                             clock="tb.clk", valid="tb.valid", ready="tb.ready", **kwargs)


@pytest.mark.parametrize("compact", [False, True])
def test_acceptance_precedes_same_timestamp_deassertion(tmp_path, compact):
    # Ready and valid fall in the same timestamp as the accepting edge. Neither
    # that update nor a later idle sample can undo the one completed transfer.
    p = wave(tmp_path, "#0\n0!\n0v\n1r\n#5\n1!\n#10\n0!\n1v\n"
             "#15\n1!\n0r\n#20\n0!\n0v\n#25\n1!\n#30\n0!")
    r = inspect(p, _compact_sampling=compact)
    assert r["sampling_phase"] == "before"
    assert r["transfer_count"] == 1
    assert r["valid_deassert_violations"] == r["payload_hold_violations"] == 0
    assert r["unknown_sample_cycles"] == 0
    assert not r["transition_data_truncated"]
    assert r["coverage"]["stall_checked"]


def test_generic_cycle_query_keeps_post_edge_default(tmp_path):
    p = wave(tmp_path, "#0\n0!\n1v\n1r\n#5\n1!\n0v\n#10\n0!")
    result = get_signals_by_cycle(p, "tb.clk", ["tb.valid"], num_cycles=1)
    assert result["cycles"][0]["signals"]["tb.valid"]["dec"] == 0
    assert inspect(p)["transfer_count"] == 1


def test_public_before_phase_keeps_sub_ps_edges_and_cache_units_separate(tmp_path):
    p = wave(tmp_path, "#0\n0!\n0v\n1r\n#1\n1v\n#5\n1!\n0v\n1v\n"
             "#6\n0v\n#7\n0!\n#8\n1!\n1v\n#10\n0!", scale="100fs")
    after = get_signals_by_cycle(p, "tb.clk", ["tb.valid"], num_cycles=2)
    for _ in range(2):  # same physical index must also survive a cache hit
        result = get_signals_by_cycle(p, "tb.clk", ["tb.valid"], num_cycles=2, sample_phase="before")
        assert result["sample_offset_ps"] == 0 and result["sample_phase"] == "before"
        assert [r["time_ps"] for r in result["cycles"]] == [1, 1]
        assert [r["signals"]["tb.valid"]["dec"] for r in result["cycles"]] == [1, 0]
    assert get_signals_by_cycle(p, "tb.clk", ["tb.valid"], num_cycles=2) == after
    next_cycle = get_signals_by_cycle(p, "tb.clk", ["tb.valid"], start_cycle=1, num_cycles=1, sample_phase="before")
    assert next_cycle["cycles"][0]["cycle"] == 1
    assert next_cycle["cycles"][0]["signals"]["tb.valid"]["dec"] == 0
    # A 1ps start excludes both physical edges, even though both display as 1ps.
    assert not get_signals_by_cycle(p, "tb.clk", ["tb.valid"], start_time_ps=1, sample_phase="before")["cycles"]


@pytest.mark.parametrize("body", ["x!", "1!\n0!\n1!"])
def test_public_before_phase_does_not_invent_global_cycles_after_ambiguous_clock(tmp_path, body):
    p = wave(tmp_path, "#0\n0!\n1v\n1r\n#5\n1!\n#10\n0!\n#15\n" + body)
    with pytest.raises(ValueError, match="ambiguous clock"):
        get_signals_by_cycle(p, "tb.clk", ["tb.valid"], sample_phase="before")


def test_public_before_samples_expression_operands_at_same_physical_edge(tmp_path):
    from src.waveform_selection import prepare_selections
    p = wave(tmp_path, "#0\n0!\n1v\n1r\n#5\n1!\n0v\n#10\n0!")
    adapter, selected = prepare_selections(p, {"signal_paths": [
        {"expr": "v && r", "typing": "wave_bits", "bindings": {"v": "tb.valid", "r": "tb.ready"}}]})
    paths = selected["signal_paths"]
    before = get_signals_by_cycle(adapter, "tb.clk", paths, sample_phase="before", num_cycles=1)
    after = get_signals_by_cycle(adapter, "tb.clk", paths, num_cycles=1)
    assert before["cycles"][0]["signals"][paths[0]]["dec"] == 1
    assert after["cycles"][0]["signals"][paths[0]]["dec"] == 0


@pytest.mark.parametrize("compact", [False, True])
def test_sub_ps_edges_do_not_merge_or_sample_future_values(tmp_path, compact):
    p = wave(tmp_path, "#0\n0!\n0v\n1r\n#1\n1v\n#5\n1!\n0v\n1v\n"
             "#6\n0v\n#7\n0!\n#8\n1!\n1v\n#10\n0!", scale="100fs")
    sampled = sample_signals_on_edges(p, "tb.clk", ["tb.valid"],
        sample_phase="before", sample_offset_ps=0, compact=compact)
    times = sampled["edge_times"] if compact else [s["time_ps"] for s in sampled["samples"]]
    values = sampled["signal_columns"]["tb.valid"] if compact else [s["signals"]["tb.valid"] for s in sampled["samples"]]
    assert times == [1, 1]  # Two distinct physical edges share a public ps label.
    assert [v["dec"] for v in values] == [1, 0]
    assert inspect(p)["transfer_count"] == 1


def test_same_timestamp_group_spanning_pages_is_excluded(tmp_path):
    changes = "\n".join(f"{i % 2}v" for i in range(1100))
    p = wave(tmp_path, f"#0\n0!\n1v\n1r\n#5\n1!\n{changes}\n0v\n#10\n0!")
    assert inspect(p)["transfer_count"] == 1


@pytest.mark.parametrize("bad_clock,gap", [("x!", "clock_unknown"),
                                          ("1!\n0!\n1!", "clock_event_order_unresolved")])
def test_ambiguous_clock_stops_at_proven_prefix(tmp_path, bad_clock, gap):
    p = wave(tmp_path, f"#0\n0!\n1v\n1r\n#5\n1!\n#10\n0!\n#15\n{bad_clock}\n#20\n0!")
    r = inspect(p)
    assert r["transfer_count"] == r["sample_count"] == 1
    assert r["transition_data_truncated"]
    assert gap in r["warnings"]
    assert r["coverage"]["sampling_gaps"] == [gap]


@pytest.mark.parametrize("initial", ["", "xv\n", "zv\n"])
def test_missing_prefix_and_unknown_are_not_idle_or_acceptance(tmp_path, initial):
    p = wave(tmp_path, f"#0\n0!\n1r\n{initial}#5\n1!\n1v\n#10\n0!")
    r = inspect(p)
    assert r["transfer_count"] == 0
    assert r["unknown_sample_cycles"] == 1


def test_missing_signal_and_unexecuted_clock_window_remain_explicit(tmp_path):
    p = wave(tmp_path, "#0\n0!\n1v\n1r\n#5\n1!\n#10\n0!")
    absent = inspect_handshake(get_parser=lambda _: p, wave_path=p.file_path,
                               clock="tb.clk", valid="tb.absent", ready="tb.ready")
    assert "tb.absent" in absent["signal_errors"]
    assert not absent["coverage"]["stall_checked"]
    empty = inspect(p, start_ps=6, end_ps=9)
    assert empty["sample_count"] == 0 and empty["reason"]
    assert not empty["coverage"]["clock_sampled"]


def test_transaction_projection_and_cursor_do_not_change_acceptance(tmp_path):
    body = ["#0", "0!", "0v", "1r", "0c", "b00 i", "b00 j"]
    changes = {10: ["1v", "b10 i"], 15: ["0v"], 20: ["1c", "b10 j"],
               25: ["0c"], 30: ["1v", "b11 i"], 35: ["0v"],
               40: ["1c", "b11 j"], 45: ["0c"]}
    for t in range(5, 51, 5):
        body.extend([f"#{t}", f"{int(t % 10 == 5)}!", *changes.get(t, [])])
    p = wave(tmp_path, "\n".join(body), signals=[(1, "!", "clk"), (1, "v", "valid"),
        (1, "r", "ready"), (1, "c", "complete"), (2, "i", "id [1:0]"), (2, "j", "cid [1:0]")])
    results = []
    for cap, cursor in [(1, "first"), (8, "another")]:
        result = reconstruct_transactions(get_parser=lambda _: p, wave_path=p.file_path,
            clock="tb.clk", req_valid="tb.valid", req_ready="tb.ready",
            cmp_valid="tb.complete", cmp_ready="tb.ready", req_id="tb.id", cmp_id="tb.cid",
            max_transactions=cap, cursor_store=CursorStore(), cursor_name=cursor)
        assert result["coverage_status"] == "complete"
        assert result["matched_count"] == 2
        assert result["unmatched_completion_count"] == result["unmatched_request_count"] == 0
        assert result["sampling_phase"] == "before"
        results.append(result)
    assert len(results[0]["transactions"]) == 1
    assert [(t["id"], t["request_time_ps"], t["completion_time_ps"]) for t in results[1]["transactions"]] == [(2, 15, 25), (3, 35, 45)]


def test_before_phase_rejects_offset_and_incompatible_reuse(tmp_path):
    from src.cycle_query import EdgeSamplingSession
    p = wave(tmp_path, "#0\n0!\n1v\n1r\n#5\n1!\n#10\n0!")
    with pytest.raises(ValueError, match="zero offset"):
        sample_signals_on_edges(p, "tb.clk", ["tb.valid"], sample_phase="before")
    session = EdgeSamplingSession(clock_path="tb.clk", start_ps=0, end_ps=-1,
                                  edge="posedge", sample_offset_ps=0)
    with pytest.raises(ValueError, match="incompatible"):
        sample_signals_on_edges(p, "tb.clk", ["tb.valid"], sample_phase="before",
                               sample_offset_ps=0, sampling_session=session)
