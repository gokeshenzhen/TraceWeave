"""Tests for sweep_handshakes (P1 whole-design handshake anomaly sweep) and the
``ended_in_stall`` deadlock signature added to inspect_handshake.

Self-contained: builds a real multi-scope VCD and drives a real VCDParser, so the
parser contract (get_signal_transitions / get_signal_at_time / search_signals /
get_signal_width) is exercised exactly as in production — no fake parser.
"""

from __future__ import annotations

import itertools
import string
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import src.cancellation as cancellation
import src.handshake_sweep as handshake_sweep
from src import operation_metrics
from src.cancellation import OperationCancelled
from src.cursor_store import CursorStore
from src.handshake_sweep import (
    _compute_finding_summary,
    _flags,
    _infer_channel_hint,
    _is_clocking_block_scope,
    _pack_clock_units,
    _sort_key,
    sweep_handshake_anomalies,
)
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


def _shared_clock_vcd(ready_by_stage, *, n_cycles: int = 30) -> str:
    """Several child interfaces using one ancestor clock."""
    ids = iter(string.ascii_letters)
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
    ]
    valid_ids: list[str] = []
    ready_ids: list[str] = []
    for index, _ in enumerate(ready_by_stage):
        valid_id = next(ids)
        ready_id = next(ids)
        valid_ids.append(valid_id)
        ready_ids.append(ready_id)
        header.extend(
            [
                f"$scope module u{index} $end",
                f"$var wire 1 {valid_id} in_valid $end",
                f"$var wire 1 {ready_id} in_ready $end",
                "$upscope $end",
            ]
        )
    header.extend(["$upscope $end", "$enddefinitions $end"])
    body = ["#0", "0!"]
    for valid_id, ready_id, ready in zip(valid_ids, ready_ids, ready_by_stage):
        body.extend([f"1{valid_id}", f"{ready}{ready_id}"])
    level = 0
    for tick in range(1, n_cycles * 2):
        level ^= 1
        body.extend([f"#{tick * 10}", f"{level}!"])
    return "\n".join([*header, *body]) + "\n"


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


class _RecordingGroupParser:
    def __init__(self, path: str, limit: int):
        self.inner = VCDParser(path)
        self.transition_group_limit = limit
        self.groups: list[tuple[str, ...]] = []

    @contextmanager
    def transition_group(self, paths):
        self.groups.append(tuple(paths))
        yield True

    def __getattr__(self, name):
        return getattr(self.inner, name)


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


# --- clocking-block scope filter --------------------------------------------

def test_is_clocking_block_scope():
    assert _is_clocking_block_scope("tb_top.m_if0.mdrv_cb")
    assert _is_clocking_block_scope("tb.s_if1.smon_cb")
    assert _is_clocking_block_scope("top.if0.driver_clocking")
    assert _is_clocking_block_scope("top.if0.cb")
    # a real interface scope must NOT be filtered
    assert not _is_clocking_block_scope("tb_top.s_if1")
    assert not _is_clocking_block_scope("top.u_cbus")  # 'cbus' is not '_cb'
    assert not _is_clocking_block_scope("top.acb_if")


# AHB interface top.if0 plus a clocking-block mirror top.if0.drv_cb (htrans/hready,
# no own clock). The sweep must inspect if0 and DROP drv_cb entirely.
def _ahb_with_cb_vcd(n_cycles: int = 12) -> str:
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$scope module if0 $end",
        "$var wire 1 ! hclk $end",
        "$var wire 2 a HTRANS $end",
        "$var wire 1 b HREADY $end",
        "$scope module drv_cb $end",
        "$var wire 2 c HTRANS $end",
        "$var wire 1 d HREADY $end",
        "$upscope $end",
        "$upscope $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    body = ["#0", "0!", "b10 a", "1b", "b10 c", "1d"]
    t, lvl = 0, 0
    for _ in range(1, n_cycles * 2):
        t += 10
        lvl ^= 1
        body.append(f"#{t}")
        body.append(f"{lvl}!")
    return "\n".join(header + body) + "\n"


def test_sweep_drops_clocking_block_scopes(tmp_path):
    r = _sweep(tmp_path, _ahb_with_cb_vcd())
    scopes = [i["scope"] for i in r["interfaces"]] + [s["scope"] for s in r["skipped"]]
    assert "top.if0" in [i["scope"] for i in r["interfaces"]]
    assert all("drv_cb" not in s for s in scopes)        # clocking block dropped
    assert r["discovered_count"] == 1                    # not inflated by the mirror
    assert r["coverage_status"] == "complete"            # no skip -> not degraded


# --- flags / sort: ahb ready_without_valid suppression + x_while_valid ------

def test_flags_suppresses_ready_without_valid_on_ahb():
    # On an AHB row, ready_without_valid means HREADY high while HTRANS idle (an
    # idle bus, not backpressure) — it must NOT surface as an anomaly flag. On a
    # valid_ready row it still does.
    res = {"ready_without_valid_cycles": 5}
    assert "ready_without_valid" not in _flags(res, "ahb")
    assert "ready_without_valid" in _flags(res, "valid_ready")


def test_flags_includes_x_while_valid():
    assert "x_while_valid" in _flags({"x_while_valid_violations": 2}, "ahb")
    assert "x_while_valid" not in _flags({"x_while_valid_violations": 0}, "ahb")


def test_sort_key_ranks_x_while_valid_above_payload_hold():
    xwv = {"x_while_valid_violations": 1, "valid": "a"}
    hold = {"payload_hold_violations": 9, "valid": "b"}
    assert _sort_key(xwv) < _sort_key(hold)


def test_sort_key_excludes_ahb_ready_without_valid_weight():
    # An AHB row's ready_without_valid_cycles must not pull it above an otherwise
    # equal valid_ready row — it carries no anomaly weight on ahb.
    ahb = {"kind": "ahb", "ready_without_valid_cycles": 50, "valid": "a"}
    clean = {"kind": "ahb", "ready_without_valid_cycles": 0, "valid": "a"}
    assert _sort_key(ahb) == _sort_key(clean)


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


def test_compact_inspect_result_matches_legacy_rows(tmp_path):
    wave = _write(tmp_path, _single_stage_vcd([(0, "0"), (6, "1")]))
    parser_factory = _parser_factory()
    arguments = dict(
        get_parser=parser_factory, wave_path=wave,
        clock="top.clk", valid="top.valid", ready="top.ready",
        max_wait_cycles=4,
    )

    legacy = inspect_handshake(**arguments)
    compact = inspect_handshake(**arguments, _compact_sampling=True)

    assert compact == legacy


def test_scheduler_packs_two_small_clock_units_without_changing_facts(tmp_path):
    wave = _write(tmp_path, _multi_stage_vcd(["1", "0"], n_cycles=10))
    parser = _RecordingGroupParser(wave, limit=6)
    grouped = sweep_handshake_anomalies(
        get_parser=lambda _: parser, wave_path=wave, max_wait_cycles=4
    )
    legacy = sweep_handshake_anomalies(
        get_parser=_parser_factory(), wave_path=wave, max_wait_cycles=4
    )

    assert grouped == legacy
    assert len(parser.groups) == 1
    assert len(parser.groups[0]) == 6


def test_scheduler_chunks_oversized_shared_clock_without_group_fallback(tmp_path):
    wave = _write(tmp_path, _shared_clock_vcd(["1", "0", "1"], n_cycles=10))
    parser = _RecordingGroupParser(wave, limit=4)
    grouped = sweep_handshake_anomalies(
        get_parser=lambda _: parser, wave_path=wave, max_wait_cycles=4
    )
    legacy = sweep_handshake_anomalies(
        get_parser=_parser_factory(), wave_path=wave, max_wait_cycles=4
    )

    assert grouped == legacy
    assert [len(group) for group in parser.groups] == [3, 4]
    assert all(len(group) <= parser.transition_group_limit for group in parser.groups)


def test_pack_clock_units_keeps_single_oversized_interface_intact():
    session = object()
    unit = {
        "members": [{"scope": "big"}],
        "session": session,
        "paths": tuple(f"s{i}" for i in range(6)),
        "clock_paths": ("clk",),
        "member_paths": [tuple(f"s{i}" for i in range(6))],
        "clock_count": 1,
    }

    packs = _pack_clock_units([unit], limit=4)

    assert len(packs) == 1
    assert len(packs[0]["paths"]) == 7
    assert packs[0]["chunked"] is True


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
    assert top["coverage"]["stall_checked"] is True
    assert top["coverage"]["backpressure_checked"] is True
    assert res["cursor"] is not None
    assert len(cs.list()) == 1  # exactly ONE cursor for the whole sweep


def test_all_clean_no_flags_no_cursor(tmp_path):
    cs = CursorStore()
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "1"]),
                 cursor_store=cs, max_wait_cycles=4)
    assert res["interface_count"] == 2
    assert res["flagged_count"] == 0
    assert res["coverage_status"] == "complete"
    assert all(i["flags"] == [] for i in res["interfaces"])
    assert res["cursor"] is None
    assert len(cs.list()) == 0


def test_interface_without_clock_is_skipped(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["0"], with_clock=False),
                 max_wait_cycles=4)
    assert res["interface_count"] == 0
    assert res["coverage_status"] == "degraded"
    assert res["coverage_warnings"]
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
    assert res["coverage_status"] == "zero_coverage"
    assert res["interfaces"] == []
    assert res["reason"]
    assert "not a protocol pass" in res["coverage_warnings"][0]


def test_scoped_zero_coverage_is_not_a_protocol_pass(tmp_path):
    res = _sweep(
        tmp_path,
        _multi_stage_vcd(["1", "1"]),
        scope="top.axi4_crossbar_h",
        max_wait_cycles=4,
    )
    assert res["discovered_count"] == 0
    assert res["interface_count"] == 0
    assert res["flagged_count"] == 0
    assert res["coverage_status"] == "zero_coverage"
    assert res["coverage_warnings"]
    assert "ZERO COVERAGE" in res["note"]
    assert "not a protocol pass" in res["coverage_warnings"][0]
    assert res["suggested_next_actions"]
    retry = res["suggested_next_actions"][0]
    assert retry["tool"] == "sweep_handshakes"
    assert "scope" not in retry["arguments"]


def test_truncation_is_loud(tmp_path):
    # 3 interfaces discovered, cap to 1 → must flag truncated and say so loudly,
    # because the dropped tail is exactly where a uniform-pipeline root can hide.
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]),
                 max_interfaces=1, max_wait_cycles=4)
    assert res["truncated"] is True
    assert res["coverage_status"] == "truncated"
    assert res["coverage_warnings"]
    assert res["discovered_count"] == 3
    assert res["interface_count"] == 1
    assert "TRUNCATED" in res["note"]


def test_full_coverage_not_truncated(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]), max_wait_cycles=4)
    assert res["truncated"] is False
    assert res["coverage_status"] == "complete"
    assert res["discovered_count"] == res["interface_count"] == 3


def test_full_sweep_reuses_one_ancestor_clock_without_changing_facts(tmp_path):
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        res = _sweep(
            tmp_path, _shared_clock_vcd(["1", "0", "1"]), max_wait_cycles=4
        )
    finally:
        operation_metrics.pop(token)

    assert res["coverage_status"] == "complete"
    assert res["interface_count"] == 3
    assert res["flagged_count"] == 1
    assert res["interfaces"][0]["valid"] == "top.u1.in_valid"
    assert res["interfaces"][0]["ended_in_stall"] is True
    clean = [row for row in res["interfaces"] if not row["flags"]]
    assert len(clean) == 2
    assert all(row["transfer_count"] > 0 for row in clean)

    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_unique_clocks"] == 1
    assert snapshot["sweep_clock_read_count"] == 1
    assert snapshot["sweep_clock_reuse_hits"] == 2
    assert snapshot["sweep_signal_read_count"] == 6


def test_native_transition_truncation_degrades_coverage_and_is_measured(
    tmp_path, monkeypatch
):
    real_inspect = inspect_handshake

    def truncated_inspect(**kwargs):
        result = real_inspect(**kwargs)
        result["transition_data_truncated"] = True
        result["transition_signals_truncated"] = [kwargs["clock"]]
        result["coverage"]["transition_data_truncated"] = True
        result["coverage"]["transition_signals_truncated"] = 1
        return result

    monkeypatch.setattr(handshake_sweep, "inspect_handshake", truncated_inspect)
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        res = _sweep(tmp_path, _multi_stage_vcd(["1", "1"]), max_wait_cycles=4)
    finally:
        operation_metrics.pop(token)

    assert res["coverage_status"] == "degraded"
    assert res["transition_truncated_count"] == 2
    assert res["flagged_count"] == 0
    assert "not a clean-protocol conclusion" in res["coverage_warnings"][0]
    assert all(row["transition_data_truncated"] for row in res["interfaces"])
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_interfaces_planned"] == 2
    assert snapshot["sweep_interfaces_attempted"] == 2
    assert snapshot["sweep_interfaces_completed"] == 2
    assert snapshot["sweep_unique_clocks"] == 2
    assert snapshot["sweep_transition_truncated_interfaces"] == 2
    assert snapshot["sweep_clock_read_count"] == 2
    assert snapshot["sweep_signal_read_count"] == 4
    assert snapshot["sweep_clock_read_total_ms"] >= 0
    assert snapshot["sweep_signal_read_total_ms"] >= 0
    assert snapshot["sweep_edge_extract_total_ms"] >= 0
    assert snapshot["sweep_value_sample_total_ms"] >= 0
    assert snapshot["sweep_inspect_total_ms"] >= 0
    assert snapshot["sweep_inspect_max_ms"] >= 0


def test_facts_exposed_and_note_disclaims_verdict(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["0", "1"]), max_wait_cycles=4)
    for iface in res["interfaces"]:
        assert "max_stall_cycles" in iface
        assert "transfer_count" in iface
    assert res["note"] and "not a verdict" in res["note"]


# --- AHB coverage (sweep now discovers htrans/hready buses too) --------------


def _ahb_stall_vcd(n_cycles: int = 30) -> str:
    """One AHB interface (top.m_if0) driving HTRANS=NONSEQ with HREADY held low
    -> a master valid (htrans active) against a never-ready slave -> deadlock."""
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$scope module m_if0 $end",
        "$var wire 1 ! HCLK $end",
        "$var wire 2 # HTRANS $end",
        "$var wire 1 $ HREADY $end",
        "$var wire 32 a HADDR $end",
        "$var wire 1 b HWRITE $end",
        "$upscope $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    body = ["#0", "0!", "b10 #", "0$", "b0 a", "0b"]
    t, lvl = 0, 0
    for _ in range(1, n_cycles * 2):
        t += 10
        lvl ^= 1
        body.append(f"#{t}")
        body.append(f"{lvl}!")
    return "\n".join(header + body) + "\n"


def _mixed_ahb_vr_vcd(n_cycles: int = 30) -> str:
    """A design with BOTH an AHB bus (stalling) and a valid/ready bus (clean)."""
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$scope module m_if0 $end",          # AHB, stalling
        "$var wire 1 ! HCLK $end",
        "$var wire 2 # HTRANS $end",
        "$var wire 1 $ HREADY $end",
        "$var wire 32 a HADDR $end",
        "$upscope $end",
        "$scope module s_axi $end",          # A-class, clean
        "$var wire 1 % clk $end",
        "$var wire 1 & wvalid $end",
        "$var wire 1 ( wready $end",
        "$upscope $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    body = ["#0", "0!", "b10 #", "0$", "b0 a", "0%", "1&", "1("]
    t, lvl = 0, 0
    for _ in range(1, n_cycles * 2):
        t += 10
        lvl ^= 1
        body.append(f"#{t}")
        body.append(f"{lvl}!")   # HCLK
        body.append(f"{lvl}%")   # axi clk
    return "\n".join(header + body) + "\n"


def test_sweep_covers_ahb_interface(tmp_path):
    res = _sweep(tmp_path, _ahb_stall_vcd(), max_wait_cycles=4)
    ahb = [i for i in res["interfaces"] if i["kind"] == "ahb"]
    assert len(ahb) == 1, res                 # discovered AND inspected the AHB bus
    assert "HTRANS" in ahb[0]["valid"]        # derived valid = htrans path
    assert "HREADY" in ahb[0]["ready"]
    assert ahb[0]["ended_in_stall"] is True
    assert "ended_in_stall" in ahb[0]["flags"]


def test_sweep_covers_both_ahb_and_valid_ready(tmp_path):
    res = _sweep(tmp_path, _mixed_ahb_vr_vcd(), max_wait_cycles=4)
    kinds = sorted({i["kind"] for i in res["interfaces"]})
    assert kinds == ["ahb", "valid_ready"], res
    assert res["interface_count"] == 2
    # the stalling AHB bus sorts first and is flagged; the A-class bus is clean
    top = res["interfaces"][0]
    assert top["kind"] == "ahb" and top["ended_in_stall"] is True
    vr = [i for i in res["interfaces"] if i["kind"] == "valid_ready"][0]
    assert vr["flags"] == []


# --- premature valid deassertion (master not waiting for ready/HREADY) --------


def _deassert_two_stage_vcd(n_cycles: int = 12) -> str:
    """Two valid/ready stages. u0 is clean (valid & ready high every cycle). u1
    stalls one cycle then the master DROPS valid before ready (the AHB
    master-not-waiting bug) — max_stall_cycles stays 1, no payload-hold, so only
    the valid-hold check can flag it."""
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        "$scope module u0 $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 # in_valid $end",
        "$var wire 1 $ in_ready $end",
        "$upscope $end",
        "$scope module u1 $end",
        "$var wire 1 % clk $end",
        "$var wire 1 & in_valid $end",
        "$var wire 1 ' in_ready $end",
        "$upscope $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    # u1 valid pattern per cycle: stall(1,r0) -> drop(0) -> idle... repeating so
    # several deassertions accrue.
    body: list[str] = []
    t, lvl = 0, 0
    for cyc in range(n_cycles):
        # set values at cycle start (clk low)
        body.append(f"#{t}")
        body.append("0!")
        body.append("0%")
        # u0 clean
        body.append("1#")
        body.append("1$")
        # u1: pattern by cyc % 3
        phase = cyc % 3
        if phase == 0:
            v1, r1 = "1", "0"  # stall
        elif phase == 1:
            v1, r1 = "0", "0"  # premature drop
        else:
            v1, r1 = "0", "0"  # idle
        body.append(f"{v1}&")
        body.append(f"{r1}'")
        # posedge
        t += 5
        body.append(f"#{t}")
        body.append("1!")
        body.append("1%")
        t += 5
    return "\n".join(header + body) + "\n"


def test_sweep_flags_and_ranks_premature_deassertion(tmp_path):
    cs = CursorStore()
    res = _sweep(tmp_path, _deassert_two_stage_vcd(),
                 cursor_store=cs, max_wait_cycles=16)
    assert res["interface_count"] == 2
    # u1 (the deasserting stage) must sort first and carry the factual flag.
    top = res["interfaces"][0]
    assert top["valid"] == "top.u1.in_valid"
    assert top["valid_deassert_violations"] >= 1
    assert "premature_valid_deassertion" in top["flags"]
    assert top["ended_in_stall"] is False  # the bug, not a deadlock
    assert top["max_stall_cycles"] == 1    # the give-away short stall
    # the deasserting row carries structured side attribution
    assert top["attribution"] is not None
    assert top["attribution"]["violating_side"] == "valid_driver"
    assert top["attribution"]["exonerated_side"] == "ready_driver"
    # the clean stage has no flags and no attribution block
    clean = next(i for i in res["interfaces"] if i["valid"] == "top.u0.in_valid")
    assert clean["flags"] == []
    assert clean["valid_deassert_violations"] == 0
    assert clean["attribution"] is None
    # exactly one cursor for the whole sweep, anchored on the flagged stage
    assert res["cursor"] is not None
    assert len(cs.list()) == 1


# --- finding_summary and truncated suggested_next_actions (Phase 1) ----------


def test_finding_summary_is_none_when_no_interfaces(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd([]))
    assert res["finding_summary"] is None


def test_finding_summary_is_none_when_no_flags(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "1"]), max_wait_cycles=4)
    assert res["finding_summary"] is None


def test_finding_summary_counts_flags_and_scopes(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["0", "1"]), max_wait_cycles=4)
    assert res["finding_summary"] is not None
    # u0 has ended_in_stall
    assert res["finding_summary"]["by_flag"].get("ended_in_stall") == 1
    assert "top.u0" in res["finding_summary"]["top_scopes"]


def test_truncation_adds_suggested_next_action(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]),
                 max_interfaces=1, max_wait_cycles=4)
    assert res["coverage_status"] == "truncated"
    assert res["suggested_next_actions"]
    retry = res["suggested_next_actions"][0]
    assert retry["tool"] == "sweep_handshakes"
    assert retry["arguments"]["max_interfaces"] == 3  # discovered_count
    assert "previous sweep" in retry["reason"].lower()


def test_complete_coverage_no_truncation_action(tmp_path):
    res = _sweep(tmp_path, _multi_stage_vcd(["1", "0", "1"]), max_wait_cycles=4)
    assert res["coverage_status"] == "complete"
    assert res["suggested_next_actions"] == []


# --- _infer_channel_hint (pure) ----------------------------------------------


def test_infer_channel_hint_canonical_axi_valids():
    # Canonical *Xvalid names map to their channel; AR/AW must win over R/W.
    assert _infer_channel_hint("hdl_top.m0.m_axi_arvalid") == "AR"
    assert _infer_channel_hint("hdl_top.m0.m_axi_awvalid") == "AW"
    assert _infer_channel_hint("hdl_top.m0.m_axi_wvalid") == "W"
    assert _infer_channel_hint("hdl_top.m0.m_axi_rvalid") == "R"
    assert _infer_channel_hint("hdl_top.m0.m_axi_bvalid") == "B"


def test_infer_channel_hint_infix_form_and_specificity():
    # `_x_` infix form, and AR/AW are not swallowed by the R/W markers.
    assert _infer_channel_hint("top.s00_aw_valid") == "AW"
    assert _infer_channel_hint("top.s00_ar_valid") == "AR"
    assert _infer_channel_hint("top.s00_w_valid") == "W"


def test_infer_channel_hint_unknown_is_other():
    # Generic valid/ready and AHB htrans carry no AXI channel marker.
    assert _infer_channel_hint("top.u1.in_valid") == "other"
    assert _infer_channel_hint("top.ahb_if.htrans") == "other"
    assert _infer_channel_hint("") == "other"


# --- _compute_finding_summary (pure): the DeepSeek-case contrast ------------


def test_finding_summary_clean_channel_shows_explicit_zero():
    # The whole point for the AXI repro case: W is flagged, R is inspected-but-clean.
    # R must appear as an explicit 0, not silently vanish.
    interfaces = [
        {"valid": "m0.m_axi_wvalid", "scope": "hdl_top.m0", "flags": ["payload_hold_violation"]},
        {"valid": "m0.m_axi_rvalid", "scope": "hdl_top.m0", "flags": []},
    ]
    summary = _compute_finding_summary(interfaces)
    assert summary["by_channel_hint"] == {"W": 1, "R": 0}
    assert summary["by_flag"] == {"payload_hold_violation": 1}


def test_finding_summary_top_scopes_deduped_and_top_level_rendered():
    # Two flagged channels in the same scope → scope listed ONCE; an empty scope
    # (top-level interface) renders as "(top)", never dropped.
    interfaces = [
        {"valid": "wvalid", "scope": "", "flags": ["long_stall"]},
        {"valid": "rvalid", "scope": "hdl_top.m1", "flags": ["ended_in_stall"]},
        {"valid": "arvalid", "scope": "hdl_top.m1", "flags": ["long_stall"]},
    ]
    summary = _compute_finding_summary(interfaces)
    assert summary["top_scopes"] == ["(top)", "hdl_top.m1"]  # deduped, top-level kept


def test_sweep_stops_between_discovery_families(monkeypatch):
    event = threading.Event()
    ahb_called = False

    def cancel_after_valid_ready(**kwargs):
        event.set()
        return {"candidate_count": 0, "candidates": [], "reason": "none"}

    def unexpected_ahb(**kwargs):
        nonlocal ahb_called
        ahb_called = True
        raise AssertionError("AHB discovery should not start after cancellation")

    monkeypatch.setattr(handshake_sweep, "suggest_handshakes", cancel_after_valid_ready)
    monkeypatch.setattr(handshake_sweep, "suggest_protocol_bundles", unexpected_ahb)
    token = cancellation.push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            sweep_handshake_anomalies(
                get_parser=lambda _: object(), wave_path="/w/a.fsdb"
            )
    finally:
        cancellation.pop_cancel_event(token)

    assert ahb_called is False


def test_sweep_cancellation_inside_clock_group_releases_native_group(
    tmp_path, monkeypatch
):
    wave_path = _write(tmp_path, _shared_clock_vcd(["1", "1"]))
    parser = VCDParser(wave_path)
    event = threading.Event()
    entered = 0
    exited = 0
    inspect_calls = 0
    real_inspect = inspect_handshake

    @contextmanager
    def tracked_transition_group(_paths):
        nonlocal entered, exited
        entered += 1
        try:
            yield True
        finally:
            exited += 1

    def cancel_after_first_interface(**kwargs):
        nonlocal inspect_calls
        inspect_calls += 1
        result = real_inspect(**kwargs)
        event.set()
        return result

    monkeypatch.setattr(
        parser, "transition_group", tracked_transition_group, raising=False
    )
    monkeypatch.setattr(
        handshake_sweep, "inspect_handshake", cancel_after_first_interface
    )
    token = cancellation.push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            sweep_handshake_anomalies(
                get_parser=lambda _: parser,
                wave_path=wave_path,
                max_wait_cycles=4,
            )
    finally:
        cancellation.pop_cancel_event(token)

    assert inspect_calls == 1
    assert entered == 1
    assert exited == 1
