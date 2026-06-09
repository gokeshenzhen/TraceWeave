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


# --- overlap / non-overlapping implication + vacuity (A+B) ------------------
# AHB-style hold property: a stalled beat (valid high, ready low) must STILL hold
# valid the NEXT cycle. The antecedent already implies the consequent on its own
# cycle, so overlap=True is a vacuous pass; overlap=false is the right shape.
_VR_SIGS = [("!", "clk", 1), ("#", "valid", 1), ("%", "ready", 1)]
_DEASSERT = [  # valid dropped the cycle after the stall (premature deassertion)
    {"valid": 0, "ready": 0}, {"valid": 1, "ready": 0},
    {"valid": 0, "ready": 1}, {"valid": 0, "ready": 0},
]
_HELD = [  # valid held through the wait state until ready (legal)
    {"valid": 0, "ready": 0}, {"valid": 1, "ready": 0},
    {"valid": 1, "ready": 1}, {"valid": 0, "ready": 0},
]
_HOLD_ANT = [{"signal": "top.valid", "op": "eq", "value": 1},
             {"signal": "top.ready", "op": "eq", "value": 0}]
_HOLD_CON = [{"signal": "top.valid", "op": "eq", "value": 1}]


def test_implication_overlap_true_is_vacuous_pass_on_hold_property(tmp_path):
    """overlap=True (default) on a hold property whose antecedent implies the
    consequent on its own cycle → trivially holds, but flagged vacuous + warned.
    This is the exact trap codex hit on premature_valid_deassertion."""
    r = _run(tmp_path, _DEASSERT, _VR_SIGS, mode="implication",
             antecedent=_HOLD_ANT, consequent=_HOLD_CON, within_cycles=1)
    assert r["holds"] is True            # vacuously
    assert r["vacuous"] is True
    assert r["overlap"] is True
    assert r["antecedent_count"] == 1
    assert r["violation_count"] == 0
    assert any("VACUOUS" in w for w in r["warnings"])


def test_implication_non_overlap_catches_premature_deassertion(tmp_path):
    """overlap=false starts the response window the NEXT cycle → it sees valid
    drop and reports a real violation, not a vacuous pass."""
    r = _run(tmp_path, _DEASSERT, _VR_SIGS, mode="implication",
             antecedent=_HOLD_ANT, consequent=_HOLD_CON,
             within_cycles=1, overlap=False)
    assert r["holds"] is False
    assert r["vacuous"] is False
    assert r["overlap"] is False
    assert r["violation_count"] == 1
    assert r["counterexample"]["cycle_index"] == 1
    assert not any("VACUOUS" in w for w in r["warnings"])


def test_implication_non_overlap_holds_when_valid_is_held(tmp_path):
    """overlap=false on a trace where valid IS held until ready → genuine pass,
    not vacuous."""
    r = _run(tmp_path, _HELD, _VR_SIGS, mode="implication",
             antecedent=_HOLD_ANT, consequent=_HOLD_CON,
             within_cycles=1, overlap=False)
    assert r["holds"] is True
    assert r["vacuous"] is False
    assert r["violation_count"] == 0


def test_implication_non_overlap_requires_within_ge_1(tmp_path):
    r = _run(tmp_path, _HELD, _VR_SIGS, mode="implication",
             antecedent=_HOLD_ANT, consequent=_HOLD_CON,
             within_cycles=0, overlap=False)
    assert r["holds"] is False
    assert "overlap=false" in (r["reason"] or "")


def test_implication_genuine_later_response_not_vacuous(tmp_path):
    """A real response-arrives-later property (req -> ack within 3) is NOT
    vacuous even with overlap=True — the window genuinely contributed."""
    cycles = [{"req": 0, "ack": 0}, {"req": 1, "ack": 0}, {"req": 0, "ack": 0},
              {"req": 0, "ack": 1}, {"req": 0, "ack": 0}]
    r = _run(tmp_path, cycles, _RA_SIGS, mode="implication",
             antecedent=[{"signal": "top.req", "op": "eq", "value": 1}],
             consequent=[{"signal": "top.ack", "op": "eq", "value": 1}],
             within_cycles=3)
    assert r["holds"] is True
    assert r["vacuous"] is False
    assert not any("VACUOUS" in w for w in r["warnings"])


def test_implication_inconclusive_not_mislabeled_vacuous(tmp_path):
    """An inconclusive-at-end PASS must NOT be flagged vacuous (no antecedent was
    satisfied on its own cycle)."""
    cycles = [{"req": 0, "ack": 0}, {"req": 0, "ack": 0}, {"req": 1, "ack": 0}]
    r = _run(tmp_path, cycles, _RA_SIGS, mode="implication",
             antecedent=[{"signal": "top.req", "op": "eq", "value": 1}],
             consequent=[{"signal": "top.ack", "op": "eq", "value": 1}],
             within_cycles=3)
    assert r["holds"] is True
    assert r["inconclusive_count"] == 1
    assert r["vacuous"] is False


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


# --- sequence mode (per-accepted-beat increment / address stride) ------------
_SEQ_SIGS = [("!", "clk", 1), ("#", "acc", 1), ("%", "addr", 8), ("&", "start", 1)]


def test_sequence_incr_holds(tmp_path):
    # accepted every cycle; addr 0,1,2,3 -> +1 stride holds
    cycles = [{"acc": 1, "addr": a, "start": 0} for a in range(4)]
    r = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence",
             predicate=[{"signal": "top.acc", "op": "eq", "value": 1}],
             delta={"signal": "top.addr", "value": 1})
    assert r["holds"] is True
    assert r["violation_count"] == 0
    assert r["beats_evaluated"] == 3  # first beat seeds, 3 deltas checked


def test_sequence_jump_is_violation_with_attribution(tmp_path):
    store = CursorStore()
    cycles = [{"acc": 1, "addr": a, "start": 0} for a in (0, 1, 5, 6)]
    r = _run(tmp_path, cycles, _SEQ_SIGS, store=store, mode="sequence",
             predicate=[{"signal": "top.acc", "op": "eq", "value": 1}],
             delta={"signal": "top.addr", "value": 1})
    assert r["holds"] is False
    assert r["violation_count"] == 1
    ce = r["counterexample"]
    assert ce["cycle_index"] == 2
    assert ce["signal_values"]["top.addr"] == "0x5"
    assert ce["signal_values"]["top.addr@prev_beat"] == "0x1"
    assert ce["signal_values"]["actual_increment"] == "4"
    # attribution: names the master-driven signal + bridges to driver lookup
    assert r["violating_signal"] == "top.addr"
    assert r["next_actions"][0]["tool"] == "explain_signal_driver"
    assert r["next_actions"][0]["signal_path"] == "top.addr"
    assert r["cursor"] is not None
    assert len(store.list()) == 1


def test_sequence_wait_states_do_not_false_fail(tmp_path):
    # addr only advances on accepted beats; it holds while acc==0 (wait state).
    cycles = [
        {"acc": 1, "addr": 0, "start": 0},
        {"acc": 0, "addr": 0, "start": 0},  # wait state, addr holds — skipped
        {"acc": 1, "addr": 1, "start": 0},
        {"acc": 0, "addr": 1, "start": 0},
        {"acc": 1, "addr": 2, "start": 0},
    ]
    r = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence",
             predicate=[{"signal": "top.acc", "op": "eq", "value": 1}],
             delta={"signal": "top.addr", "value": 1})
    assert r["holds"] is True
    assert r["beats_evaluated"] == 2


def test_sequence_wrap_modulo_accepts_wraparound(tmp_path):
    # WRAP byte burst, region 4 bytes: 0x6,0x7,0x4,0x5. raw delta at the wrap is
    # -3; modulo 4 makes it +1 so the legal wrap is not a violation.
    cycles = [{"acc": 1, "addr": a, "start": 0} for a in (0x6, 0x7, 0x4, 0x5)]
    gate = [{"signal": "top.acc", "op": "eq", "value": 1}]
    wrapped = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence", predicate=gate,
                   delta={"signal": "top.addr", "value": 1, "modulo": 4})
    assert wrapped["holds"] is True
    # without modulo the same trace is flagged at the wrap beat
    plain = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence", predicate=gate,
                 delta={"signal": "top.addr", "value": 1})
    assert plain["holds"] is False
    assert plain["counterexample"]["cycle_index"] == 2


def test_sequence_restart_when_reseeds_at_burst_start(tmp_path):
    # two bursts: 0,1 then 8,9. start=1 marks each burst's first beat.
    cycles = [
        {"acc": 1, "addr": 0, "start": 1},
        {"acc": 1, "addr": 1, "start": 0},
        {"acc": 1, "addr": 8, "start": 1},  # new burst — re-seed, do not check 8-1
        {"acc": 1, "addr": 9, "start": 0},
    ]
    gate = [{"signal": "top.acc", "op": "eq", "value": 1}]
    with_restart = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence", predicate=gate,
                        delta={"signal": "top.addr", "value": 1,
                               "restart_when": [{"signal": "top.start", "op": "eq", "value": 1}]})
    assert with_restart["holds"] is True
    # without restart_when the cross-burst jump 1->8 is flagged
    without = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence", predicate=gate,
                   delta={"signal": "top.addr", "value": 1})
    assert without["holds"] is False
    assert without["counterexample"]["cycle_index"] == 2


def test_sequence_xz_breaks_continuity_no_false_fail(tmp_path):
    # an x on the tracked signal breaks the chain — counted unknown, the next
    # known beat re-seeds rather than computing a bogus 2-step delta.
    cycles = [
        {"acc": 1, "addr": 0, "start": 0},
        {"acc": 1, "addr": 1, "start": 0},
        {"acc": 1, "addr": "x", "start": 0},
        {"acc": 1, "addr": 3, "start": 0},
    ]
    r = _run(tmp_path, cycles, _SEQ_SIGS, mode="sequence",
             predicate=[{"signal": "top.acc", "op": "eq", "value": 1}],
             delta={"signal": "top.addr", "value": 1})
    assert r["holds"] is True
    assert r["unknown_cycles"] == 1
    assert r["beats_evaluated"] == 1


def test_sequence_missing_delta_is_loud(tmp_path):
    r = _run(tmp_path, [{"acc": 1, "addr": 0, "start": 0}], _SEQ_SIGS, mode="sequence",
             predicate=[{"signal": "top.acc", "op": "eq", "value": 1}])
    assert r["reason"] and "delta" in r["reason"]


def test_delta_on_non_sequence_mode_is_loud(tmp_path):
    r = _run(tmp_path, _cnt_cycles(), _CNT_SIGS, mode="always",
             predicate=[{"signal": "top.cnt", "op": "le", "value": 5}],
             delta={"signal": "top.cnt", "value": 1})
    assert r["reason"] and "delta" in r["reason"]
