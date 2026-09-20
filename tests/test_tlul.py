"""Hand-authored A/D events, independently encoded as scalar and packed dumps."""
from collections import Counter
import pytest

from src.schemas import TlulInspectResult
from src.tlul import inspect_tlul
from src.vcd_parser import VCDParser


def fixture(tmp_path, *, packed, data_width=8, id_width=2, edits=None):
    widths = {"a_valid": 1, "a_opcode": 3, "a_size": 2, "a_source": id_width,
              "a_data": data_width, "d_ready": 1, "d_valid": 1, "d_opcode": 3,
              "d_size": 2, "d_source": id_width, "d_data": data_width, "d_error": 1, "a_ready": 1}
    rows = [
        {"reset": 0},
        {"a_valid": 1, "a_source": 1, "a_data": 0x35},
        {"a_valid": 1, "a_ready": 1, "a_source": 1, "a_data": 0x35},
        {"a_valid": 1, "a_ready": 1, "a_source": 2, "a_data": 0xa5,
         "d_valid": 1, "d_ready": 1, "d_source": 1, "d_data": 0x3c},
        {"d_valid": 1, "d_source": 2, "d_data": 0x59},
        {"d_valid": 1, "d_ready": 1, "d_source": 2, "d_data": 0x59},
        {"d_valid": 1, "d_ready": 1, "d_source": 3},
        {"a_valid": 1, "a_ready": 1, "a_source": 1, "a_data": 0x42},
        {},
    ]
    for i, edit in (edits or {}).items():
        rows[i].update(edit)
    path = tmp_path / ("packed.vcd" if packed else "scalar.vcd")
    text = ["$timescale 1ps $end", "$scope module tb $end", "$var wire 1 c clk $end",
            "$var wire 1 r reset $end"]
    groups = [[n for n in widths if n.startswith("a_") and n != "a_ready"] + ["d_ready"],
              [n for n in widths if n.startswith("d_") and n != "d_ready"] + ["a_ready"]]
    fields, symbols = {}, {}
    if packed:
        for index, names in enumerate(groups):
            width = sum(widths[n] for n in names)
            bus = "request" if index == 0 else "response"
            symbol = "p" if index == 0 else "q"
            text.append(f"$var wire {width} {symbol} {bus} [{width-1}:0] $end")
            # Independent MSB-first declaration serialization, separate from
            # the selection implementation's declared-coordinate lookup.
            position = width
            for name in names:
                position -= widths[name]
                fields[name] = {"path": f"tb.{bus}[{width-1}:0]", "lsb": position, "width": widths[name]}
            symbols[symbol] = names
    else:
        for i, (name, width) in enumerate(widths.items()):
            symbol = "s" + str(i)
            text.append(f"$var wire {width} {symbol} {name} $end")
            fields[name] = "tb." + name
            symbols[symbol] = [name]
    text += ["$upscope $end", "$enddefinitions $end"]
    for i, row in enumerate(rows):
        text += [f"#{i*10}", "0c", str(row.get("reset", 1)) + "r"]
        for symbol, names in symbols.items():
            bits = ""
            for name in names:
                value = row.get(name, 0)
                bits += value * widths[name] if isinstance(value, str) else format(value, f"0{widths[name]}b")
            text.append("b" + bits + " " + symbol)
        text += [f"#{i*10+5}", "1c"]
    text += ["#90", "0c"]
    path.write_text("\n".join(text) + "\n")
    return VCDParser(str(path)), fields


def run(parser, fields, **kw):
    result = inspect_tlul(get_parser=lambda _: parser, wave_path=parser.file_path,
                          clock="tb.clk", fields=fields, reset="tb.reset", **kw)
    TlulInspectResult.model_validate(result)
    return result


@pytest.mark.parametrize("data_width,id_width", [(8, 2), (17, 5)])
def test_scalar_and_packed_match_independent_events(tmp_path, data_width, id_width):
    results = []
    for packed in (False, True):
        parser, fields = fixture(tmp_path, packed=packed, data_width=data_width, id_width=id_width)
        counts = Counter()
        original = parser.get_transitions
        def measured(path, *a, **kw):
            counts[path] += 1
            return original(path, *a, **kw)
        parser.get_transitions = measured
        r = run(parser, fields)
        assert r["coverage_status"] == "complete"
        assert r["reset_cycles"] == 1
        assert r["accepted_a_count"] == r["accepted_d_count"] == 3
        assert r["channels"]["a"]["transfer_count"] == 3
        assert r["channels"]["d"]["transfer_count"] == 3
        assert r["channels"]["a"]["stall_count"] == r["channels"]["d"]["stall_count"] == 1
        t = r["transactions"]
        facts = [(x["id"], x["request_time_ps"], x["completion_time_ps"], x["latency_cycles"]) for x in t["transactions"]]
        assert facts == [(1, 25, 35, 1), (2, 35, 55, 2)]
        assert t["unmatched_requests"] == [{"id": 1, "request_time_ps": 75}]
        assert t["unmatched_completions"] == [{"id": 3, "completion_time_ps": 65}]
        assert t["max_outstanding"] == 2
        assert not any("hang/deadlock" in w for w in t["warnings"])
        if packed:
            assert len(counts) == 4  # clock, reset, and two backing buses
            assert all(n == 1 for n in counts.values())
        results.append(r)
    assert results[0]["field_facts"] == results[1]["field_facts"]
    for channel in ("a", "d"):
        for key in ("transfer_count", "stall_count", "max_stall_cycles", "payload_hold_violations", "valid_deassert_violations"):
            assert results[0]["channels"][channel][key] == results[1]["channels"][channel][key]


def test_reset_and_unknown_history_cannot_create_false_matching(tmp_path):
    parser, fields = fixture(tmp_path, packed=True, edits={3: {"reset": "x"}, 4: {"reset": 0},
                                                         7: {"a_source": "z"}})
    r = run(parser, fields)
    assert r["unknown_reset_cycles"] == 1
    assert r["unknown_id_beats"] == 1
    assert r["correlation_breaks"] == 2
    assert r["transactions"]["matched_count"] == 0
    assert r["coverage_status"] == "partial"
    assert r["unknown_fields"]["a_source"] == 1


def test_no_mapping_no_guessing_and_partial_window_limits(tmp_path):
    parser, fields = fixture(tmp_path, packed=True)
    r = inspect_tlul(get_parser=lambda _: parser, wave_path=parser.file_path, clock="tb.clk")
    assert r["mapping_status"] == "mapping_required" and r["coverage_status"] == "zero_coverage"
    assert r["checks"] == []
    r = run(parser, fields, start_ps=30, max_cycles=3, max_transactions=1)
    assert r["carry_in"] == "unknown" and r["sample_limit_reached"]
    assert r["coverage_status"] == "partial"
    assert "window_carry_in_unknown" in r["gaps"] and "sample_limit" in r["gaps"]


def test_unknown_payload_is_retained_and_never_called_integrity_pass(tmp_path):
    parser, fields = fixture(tmp_path, packed=True, edits={2: {"a_data": "x"}})
    r = run(parser, fields)
    assert r["field_facts"]["a_data"]["unknown_accepted"] == 1
    assert r["coverage_status"] == "partial"
    assert not any("integrity" in check for check in r["checks"])
    assert any(
        "xxxxxxxx" in v for v in r["transactions"]["transactions"][0]["req_fields"].values())


def test_transition_prefix_never_invents_repeated_accepts(tmp_path):
    parser, fields = fixture(tmp_path, packed=True)
    original = parser.get_transitions
    def limited(path, *a, **kw):
        r = original(path, *a, **kw)
        if path == fields["a_valid"]["path"]:
            r.update(transitions=[t for t in r["transitions"] if t["time_ps"] <= 30], truncated=True)
        return r
    parser.get_transitions = limited
    r = run(parser, fields)
    assert r["transition_data_truncated"] and r["coverage_status"] == "partial"
    assert r["tail"] == "transition_prefix"
    assert r["accepted_a_count"] == 1
    assert r["transactions"]["matched_count"] == 0
    assert r["correlation_breaks"] == 6


def test_same_edge_pairing_output_cap_and_reset_clear_are_distinct(tmp_path):
    parser, fields = fixture(tmp_path, packed=True, edits={2: {"d_valid": 1, "d_ready": 1, "d_source": 1}})
    r = run(parser, fields, max_transactions=1)
    assert r["transactions"]["matched_count"] == 2
    assert len(r["transactions"]["transactions"]) == 1
    assert r["transactions"]["transactions"][0]["latency_cycles"] == 0
    assert not r["sample_limit_reached"]
    parser, fields = fixture(tmp_path, packed=True, edits={3: {"reset": "x"}, 7: {"a_source": "z"}})
    r = run(parser, fields)
    assert r["correlation_breaks"] == 2
    assert r["transactions"]["unknown_history_clears"] == 1  # Only one boundary clears pending work.
    assert r["transactions"]["reset_clears"] == 0
    parser, fields = fixture(tmp_path, packed=True, edits={3: {"reset": 0}})
    r = run(parser, fields)
    assert r["transactions"]["reset_clears"] == 1
    assert r["transactions"]["unknown_history_clears"] == 0
