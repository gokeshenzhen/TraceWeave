"""Tests for verify_window (P3 small templated temporal-verify engine).

Self-contained: builds a real VCD and drives a real VCDParser.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from src.cursor_store import CursorStore
from src.vcd_parser import VCDParser
from src.window_verify import verify_window

_seq = itertools.count()


def _build_vcd(cycles: list[dict], signals: list[tuple[str, str, int]]) -> str:
    """cycles[i] maps signal-name -> value (int, or 'x'). signals = (id,name,width)."""
    hdr = ["$timescale 1ps $end", "$scope module top $end"]
    hdr += [f"$var wire {w} {sid} {nm} $end" for sid, nm, w in signals]
    hdr += ["$upscope $end", "$enddefinitions $end"]
    name2id = {nm: sid for sid, nm, _ in signals}
    width = {nm: w for _, nm, w in signals}
    body: list[str] = []
    t, lvl = 0, 0
    # clk is signal 'clk'; we drive a posedge at 10*i+5
    for i, c in enumerate(cycles):
        body.append(f"#{10 * i}")
        body.append(f"0{name2id['clk']}")
        for nm, val in c.items():
            sid = name2id[nm]
            if width[nm] == 1:
                body.append(f"{'x' if val == 'x' else int(val)}{sid}")
            else:
                bits = "x" * width[nm] if val == "x" else format(int(val), f"0{width[nm]}b")
                body.append(f"b{bits} {sid}")
        body.append(f"#{10 * i + 5}")
        body.append(f"1{name2id['clk']}")
    return "\n".join(hdr + body) + "\n"


def _write(tmp_path: Path, vcd: str) -> str:
    p = tmp_path / f"vw{next(_seq)}.vcd"
    p.write_text(vcd)
    return str(p)


def _run(tmp_path, cycles, signals, *, store=None, **kw):
    wave = _write(tmp_path, _build_vcd(cycles, signals))
    return verify_window(get_parser=lambda _w: VCDParser(wave), wave_path=wave,
                         clock="top.clk", cursor_store=store, **kw)


# counter 0..5 over 6 cycles
_CNT_SIGS = [("!", "clk", 1), ("#", "cnt", 4)]
def _cnt_cycles():
    return [{"cnt": i} for i in range(6)]


def test_always_holds(tmp_path):
    r = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="always",
             predicate=[{"signal": "top.cnt", "op": "le", "value": 5}])
    assert r["holds"] is True
    assert r["counterexample"] is None
    assert r["cycles_evaluated"] == 6


def test_always_fails_with_counterexample(tmp_path):
    store = CursorStore()
    r = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, store=store, mode="always",
             predicate=[{"signal": "top.cnt", "op": "le", "value": 3}])
    assert r["holds"] is False
    assert r["counterexample"]["cycle_index"] == 4
    assert r["counterexample"]["signal_values"]["top.cnt"] in ("0x4", "0b0100", "4")
    assert r["cursor"] is not None  # anchored at the counterexample
    assert len(store.list()) == 1


def test_never_holds_and_fails(tmp_path):
    ok = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="never",
              predicate=[{"signal": "top.cnt", "op": "eq", "value": 9}])
    assert ok["holds"] is True
    bad = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="never",
               predicate=[{"signal": "top.cnt", "op": "eq", "value": 3}])
    assert bad["holds"] is False
    assert bad["counterexample"]["cycle_index"] == 3


def test_eventually_witness_and_miss(tmp_path):
    hit = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="eventually",
               predicate=[{"signal": "top.cnt", "op": "eq", "value": 4}])
    assert hit["holds"] is True
    assert hit["witness"]["cycle_index"] == 4
    miss = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="eventually",
                predicate=[{"signal": "top.cnt", "op": "eq", "value": 12}])
    assert miss["holds"] is False
    assert miss["witness"] is None


_RA_SIGS = [("!", "clk", 1), ("#", "req", 1), ("%", "ack", 1)]


def test_implication_holds_within_window(tmp_path):
    # req at cycle1, ack at cycle3 (2 later) -> holds for within>=2
    cycles = [{"req": 0, "ack": 0}, {"req": 1, "ack": 0}, {"req": 0, "ack": 0},
              {"req": 0, "ack": 1}, {"req": 0, "ack": 0}]
    r = _run(tmp_path, cycles, _RA_SIGS, mode="implication",
             antecedent=[{"signal": "top.req", "op": "eq", "value": 1}],
             consequent=[{"signal": "top.ack", "op": "eq", "value": 1}],
             within_cycles=3)
    assert r["holds"] is True
    assert r["antecedent_count"] == 1
    assert r["violation_count"] == 0


def test_implication_violation(tmp_path):
    cycles = [{"req": 0, "ack": 0}, {"req": 1, "ack": 0}, {"req": 0, "ack": 0},
              {"req": 0, "ack": 1}, {"req": 0, "ack": 0}]
    r = _run(tmp_path, cycles, _RA_SIGS, mode="implication",
             antecedent=[{"signal": "top.req", "op": "eq", "value": 1}],
             consequent=[{"signal": "top.ack", "op": "eq", "value": 1}],
             within_cycles=1)
    assert r["holds"] is False
    assert r["violation_count"] == 1
    assert r["counterexample"]["cycle_index"] == 1


def test_implication_inconclusive_at_window_end(tmp_path):
    # req fires on the last cycle -> response window runs past trace end
    cycles = [{"req": 0, "ack": 0}, {"req": 0, "ack": 0}, {"req": 1, "ack": 0}]
    r = _run(tmp_path, cycles, _RA_SIGS, mode="implication",
             antecedent=[{"signal": "top.req", "op": "eq", "value": 1}],
             consequent=[{"signal": "top.ack", "op": "eq", "value": 1}],
             within_cycles=3)
    assert r["antecedent_count"] == 1
    assert r["violation_count"] == 0
    assert r["inconclusive_count"] == 1
    assert r["holds"] is True  # inconclusive is not a violation
    assert any("inconclusive" in w for w in r["warnings"])


def test_and_of_two_terms(tmp_path):
    sigs = [("!", "clk", 1), ("#", "a", 1), ("%", "b", 1)]
    cycles = [{"a": 1, "b": 1}, {"a": 1, "b": 0}]
    # never (a==1 AND b==1) -> fails at cycle0
    r = _run(tmp_path, cycles, sigs, mode="never",
             predicate=[{"signal": "top.a", "op": "eq", "value": 1},
                        {"signal": "top.b", "op": "eq", "value": 1}])
    assert r["holds"] is False
    assert r["counterexample"]["cycle_index"] == 0


def test_unknown_x_cycle_reported_not_passed(tmp_path):
    cycles = [{"cnt": 1}, {"cnt": "x"}, {"cnt": 2}]
    r = _run(tmp_path, cycles, _CNT_SIGS, mode="always",
             predicate=[{"signal": "top.cnt", "op": "le", "value": 5}])
    assert r["unknown_cycles"] == 1
    assert any("x/z" in w for w in r["warnings"])
    assert r["holds"] is True  # the two known cycles satisfy; x is not a pass nor fail


def test_unresolved_signal_is_loud(tmp_path):
    r = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="always",
             predicate=[{"signal": "top.nope", "op": "eq", "value": 1}])
    assert r["reason"] and "not found" in r["reason"]
    assert r["holds"] is False


def test_bad_mode_is_loud(tmp_path):
    r = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="sometimes",
             predicate=[{"signal": "top.cnt", "op": "le", "value": 5}])
    assert r["reason"] and "mode must be" in r["reason"]


def test_wrong_param_combo_is_loud(tmp_path):
    r = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="always",
             antecedent=[{"signal": "top.cnt", "op": "eq", "value": 1}])
    assert r["reason"] and "predicate" in r["reason"]


def test_is_x_op(tmp_path):
    cycles = [{"cnt": 1}, {"cnt": "x"}, {"cnt": 2}]
    r = _run(tmp_path, cycles, _CNT_SIGS, mode="eventually",
             predicate=[{"signal": "top.cnt", "op": "is_x"}])
    assert r["holds"] is True
    assert r["witness"]["cycle_index"] == 1
