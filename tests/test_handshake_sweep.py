"""Tests for sweep_handshakes (P1 whole-design handshake anomaly sweep) and the
``ended_in_stall`` deadlock signature added to inspect_handshake.

Self-contained: builds a real multi-scope VCD and drives a real VCDParser, so the
parser contract (get_signal_transitions / get_signal_at_time / search_signals /
get_signal_width) is exercised exactly as in production — no fake parser.
"""

from __future__ import annotations

import itertools
import string
from pathlib import Path

from src.cursor_store import CursorStore
from src.handshake_sweep import sweep_handshake_anomalies
from src.vcd_parser import VCDParser
from src.verify_condition import inspect_handshake


def _multi_stage_vcd(ready_by_stage, *, n_cycles: int = 30, with_clock: bool = True) -> str:
    """One scope per stage (top.uN) with clk/in_valid/in_ready. valid is held
    high; ready is held at the given per-stage value ('0' = deadlock)."""
    ids = iter(string.ascii_letters)
    header = ["$timescale 1ps $end", "$scope module top $end"]
    clk_ids, valid_ids, ready_ids = [], [], []
    for i, _ in enumerate(ready_by_stage):
        header.append(f"$scope module u{i} $end")
        if with_clock:
            cid = next(ids)
            header.append(f"$var wire 1 {cid} clk $end")
            clk_ids.append(cid)
        vid = next(ids)
        rid = next(ids)
        header.append(f"$var wire 1 {vid} in_valid $end")
        header.append(f"$var wire 1 {rid} in_ready $end")
        valid_ids.append(vid)
        ready_ids.append(rid)
        header.append("$upscope $end")
    header.append("$upscope $end")
    header.append("$enddefinitions $end")

    body = ["#0"]
    for cid in clk_ids:
        body.append(f"0{cid}")
    for vid in valid_ids:
        body.append(f"1{vid}")
    for rid, rval in zip(ready_ids, ready_by_stage):
        body.append(f"{rval}{rid}")
    t, lvl = 0, 0
    for _ in range(1, n_cycles * 2):
        t += 10
        lvl ^= 1
        body.append(f"#{t}")
        for cid in clk_ids:
            body.append(f"{lvl}{cid}")
    return "\n".join(header + body) + "\n"


def _single_stage_vcd(ready_seq, *, n_cycles: int = 30) -> str:
    """One scope top with clk/valid/ready; ready_seq is a list of (cycle, value)
    so a stall can recover partway through."""
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 # valid $end",
        "$var wire 1 $ ready $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    ready_at = dict(ready_seq)
    body = ["#0", "0!", "1#", f"{ready_at.get(0, '0')}$"]
    t, lvl, cyc = 0, 0, 0
    for _ in range(1, n_cycles * 2):
        t += 10
        lvl ^= 1
        body.append(f"#{t}")
        body.append(f"{lvl}!")
        if lvl == 1:  # rising edge -> this is cycle (cyc+1)
            cyc += 1
            if cyc in ready_at:
                body.append(f"{ready_at[cyc]}$")
    return "\n".join(header + body) + "\n"


def _parser_factory():
    cache: dict[str, VCDParser] = {}

    def get_parser(path: str) -> VCDParser:
        if path not in cache:
            cache[path] = VCDParser(path)
        return cache[path]

    return get_parser


# One unique filename per write, purely for per-test isolation hygiene.
_wave_seq = itertools.count()


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / f"w{next(_wave_seq)}.vcd"
    p.write_text(body)
    return str(p)


def _sweep(tmp_path, vcd, **kw):
    return sweep_handshake_anomalies(
        get_parser=_parser_factory(), wave_path=_write(tmp_path, vcd), **kw
    )


# --- ended_in_stall (decision A: deadlock signature on inspect_handshake) ----


def test_inspect_handshake_ended_in_stall_deadlock(tmp_path):
    wave = _write(tmp_path, _single_stage_vcd([(0, "0")]))  # ready never asserts
    res = inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave,
        clock="top.clk", valid="top.valid", ready="top.ready", max_wait_cycles=4,
    )
    assert res["ended_in_stall"] is True
    assert res["final_stall_cycles"] > 4
    assert res["transfer_count"] == 0


def test_inspect_handshake_recovered_stall_not_ended_in_stall(tmp_path):
    wave = _write(tmp_path, _single_stage_vcd([(0, "0"), (6, "1")]))  # recovers
    res = inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave,
        clock="top.clk", valid="top.valid", ready="top.ready", max_wait_cycles=4,
    )
    assert res["ended_in_stall"] is False
    assert res["final_stall_cycles"] == 0
    assert res["transfer_count"] > 0


# --- sweep_handshakes (decision B: comparative fact table, not a verdict) -----


def test_deadlocked_stage_sorts_first_and_sets_one_cursor(tmp_path):
    cs = CursorStore()
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]),
                 cursor_store=cs, max_wait_cycles=4)
    assert res["interface_count"] == 3
    assert res["flagged_count"] == 1
    top = res["interfaces"][0]
    assert top["valid"] == "top.u1.in_valid"
    assert top["ended_in_stall"] is True
    assert "ended_in_stall" in top["flags"]
    assert res["cursor"] is not None
    assert len(cs.list()) == 1  # exactly ONE cursor for the whole sweep


def test_all_clean_no_flags_no_cursor(tmp_path):
    cs = CursorStore()
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "1"]),
                 cursor_store=cs, max_wait_cycles=4)
    assert res["interface_count"] == 2
    assert res["flagged_count"] == 0
    assert all(i["flags"] == [] for i in res["interfaces"])
    assert res["cursor"] is None
    assert len(cs.list()) == 0


def test_interface_without_clock_is_skipped(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["0"], with_clock=False),
                 max_wait_cycles=4)
    assert res["interface_count"] == 0
    assert len(res["skipped"]) == 1
    assert "clock" in res["skipped"][0]["reason"]


def test_no_handshake_pairs_returns_honest_reason(tmp_path):
    vcd = (
        "$timescale 1ps $end\n$scope module top $end\n"
        "$var wire 1 ! clk $end\n$var wire 8 # data $end\n"
        "$upscope $end\n$enddefinitions $end\n#0\n0!\nb0 #\n#10\n1!\n"
    )
    res = _sweep(tmp_path, vcd)
    assert res["interface_count"] == 0
    assert res["interfaces"] == []
    assert res["reason"]


def test_truncation_is_loud(tmp_path):
    # 3 interfaces discovered, cap to 1 → must flag truncated and say so loudly,
    # because the dropped tail is exactly where a uniform-pipeline root can hide.
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]),
                 max_interfaces=1, max_wait_cycles=4)
    assert res["truncated"] is True
    assert res["discovered_count"] == 3
    assert res["interface_count"] == 1
    assert "TRUNCATED" in res["note"]


def test_full_coverage_not_truncated(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]), max_wait_cycles=4)
    assert res["truncated"] is False
    assert res["discovered_count"] == res["interface_count"] == 3


def test_facts_exposed_and_note_disclaims_verdict(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["0", "1"]), max_wait_cycles=4)
    for iface in res["interfaces"]:
        assert "max_stall_cycles" in iface
        assert "transfer_count" in iface
    assert res["note"] and "not a verdict" in res["note"]
