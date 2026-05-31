"""Unit tests for src.verify_condition.inspect_handshake (auto-debug T1).

Synthetic VCD fixtures drive the cycle-by-cycle classifier without an FSDB
runtime. The FSDB path shares the same get_transitions / get_value_at_time
contract via cycle_query.sample_signals_on_edges, so algorithm coverage here
applies to both backends (a real-FSDB smoke test is tracked separately, since
the diff_first_divergence end_ps=-1 bug showed VCD-only tests can miss
backend-specific echo quirks).
"""

from __future__ import annotations

from pathlib import Path

from src.cursor_store import CursorStore
from src.vcd_parser import VCDParser
from src.verify_condition import inspect_handshake


# Clock: period 10ns, posedge of `clk` at 10*i+5 for cycle i. Each cycle's
# valid/ready/addr are set at the cycle start (10*i) so they are stable at the
# sample point (posedge + default offset 1 = 10*i+6).
def _build_vcd(cycles: list[dict], with_addr: bool = False) -> str:
    lines = [
        "$timescale 1ns $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 # valid $end",
        "$var wire 1 % ready $end",
    ]
    if with_addr:
        lines.append("$var wire 8 & addr $end")
    lines += ["$upscope $end", "$enddefinitions $end"]

    def _bit(v):
        return "x" if v == "x" else str(int(v))

    for i, c in enumerate(cycles):
        t_low = 10 * i
        t_high = 10 * i + 5
        lines.append(f"#{t_low}")
        lines.append("0!")  # clk low at cycle start
        lines.append(f"{_bit(c['valid'])}#")
        lines.append(f"{_bit(c['ready'])}%")
        if with_addr:
            addr = c.get("addr", 0)
            if addr == "x":
                lines.append("bxxxxxxxx &")
            else:
                lines.append(f"b{int(addr):08b} &")
        lines.append(f"#{t_high}")
        lines.append("1!")  # posedge
    return "\n".join(lines) + "\n"


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


def _run(tmp_path, name, cycles, *, with_addr=False, store=None, **kwargs):
    wave = _write(tmp_path, name, _build_vcd(cycles, with_addr=with_addr))
    return inspect_handshake(
        get_parser=_parser_factory(),
        wave_path=wave,
        clock="top.clk",
        valid="top.valid",
        ready="top.ready",
        cursor_store=store,
        **kwargs,
    )


def test_clean_handshake_no_findings(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1} for _ in range(6)]
    store = CursorStore()
    r = _run(tmp_path, "clean.vcd", cycles, store=store)
    assert r["sample_count"] == 6
    assert r["transfer_count"] == 6
    assert r["stall_count"] == 0
    assert r["max_stall_cycles"] == 0
    assert r["findings"] == []
    assert r["cursor"] is None
    assert len(store) == 0
    assert r["reason"] is None
    assert r["coverage"]["valid_ready_resolved"] is True
    assert r["coverage"]["clock_sampled"] is True
    assert r["coverage"]["stall_checked"] is True
    assert r["coverage"]["backpressure_checked"] is True
    assert r["coverage"]["payload_hold_requested"] is False


def test_stall_counts_and_long_stall_finding(tmp_path: Path):
    # valid stays high; ready low for 5 consecutive cycles, then transfers.
    cycles = (
        [{"valid": 1, "ready": 1}]
        + [{"valid": 1, "ready": 0}] * 5
        + [{"valid": 1, "ready": 1}] * 2
    )
    store = CursorStore()
    r = _run(tmp_path, "stall.vcd", cycles, store=store, max_wait_cycles=3)
    assert r["stall_count"] == 5
    assert r["max_stall_cycles"] == 5
    # cycle 1 is the first stall; posedge of cycle i is at 10*i+5 ns -> 15000 ps
    # ($timescale 1ns, parser reports picoseconds).
    assert r["max_stall_begin_ps"] == 15000
    long_stalls = [f for f in r["findings"] if f["type"] == "long_stall"]
    assert len(long_stalls) == 1
    assert long_stalls[0]["cycles"] == 5
    assert long_stalls[0]["begin_ps"] == 15000
    # cursor anchored at the long stall start
    assert r["cursor"]["time_ps"] == 15000
    assert len(store) == 1


def test_stall_below_threshold_no_long_stall(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1}] + [{"valid": 1, "ready": 0}] * 2 + [{"valid": 1, "ready": 1}]
    r = _run(tmp_path, "short.vcd", cycles, max_wait_cycles=3)
    assert r["stall_count"] == 2
    assert r["max_stall_cycles"] == 2
    assert [f for f in r["findings"] if f["type"] == "long_stall"] == []


def test_payload_hold_violation(tmp_path: Path):
    # 4-cycle stall; addr held at 0x10 then changes to 0x11 mid-stall (the AHB
    # "haddr must hold while hready low" violation).
    cycles = [
        {"valid": 1, "ready": 1, "addr": 0x10},
        {"valid": 1, "ready": 0, "addr": 0x10},  # stall begins, latch 0x10
        {"valid": 1, "ready": 0, "addr": 0x10},
        {"valid": 1, "ready": 0, "addr": 0x11},  # violation here
        {"valid": 1, "ready": 1, "addr": 0x11},
    ]
    store = CursorStore()
    r = _run(tmp_path, "hold.vcd", cycles, with_addr=True, store=store,
             payload=["top.addr"], max_wait_cycles=100)
    assert r["payload_hold_violations"] == 1
    viols = [f for f in r["findings"] if f["type"] == "payload_hold_violation"]
    assert len(viols) == 1
    assert viols[0]["signal"] == "top.addr"
    assert viols[0]["from_value"] == "0x10"
    assert viols[0]["to_value"] == "0x11"
    # violation cycle is index 3 -> posedge at 35 ns -> 35000 ps
    assert viols[0]["time_ps"] == 35000
    # cursor prioritises the hold violation over any stall
    assert r["cursor"]["time_ps"] == 35000
    assert len(store) == 1


def test_payload_change_outside_stall_is_not_a_violation(tmp_path: Path):
    # addr changes between two clean transfers — perfectly legal.
    cycles = [
        {"valid": 1, "ready": 1, "addr": 0x10},
        {"valid": 1, "ready": 1, "addr": 0x20},
        {"valid": 1, "ready": 1, "addr": 0x30},
    ]
    r = _run(tmp_path, "noviol.vcd", cycles, with_addr=True, payload=["top.addr"])
    assert r["payload_hold_violations"] == 0
    assert r["findings"] == []


def test_ready_without_valid_counts_backpressure_imbalance(tmp_path: Path):
    cycles = [{"valid": 0, "ready": 1} for _ in range(3)] + [{"valid": 1, "ready": 1}]
    r = _run(tmp_path, "rwv.vcd", cycles)
    assert r["ready_without_valid_cycles"] == 3
    assert r["transfer_count"] == 1
    assert r["stall_count"] == 0


def test_unknown_valid_does_not_extend_stall(tmp_path: Path):
    # stall, then an x on valid, then stall resumes — the x must break the run
    # so neither stall window exceeds 1, and x is counted as unknown.
    cycles = [
        {"valid": 1, "ready": 0},
        {"valid": "x", "ready": 0},
        {"valid": 1, "ready": 0},
    ]
    r = _run(tmp_path, "unknown.vcd", cycles, max_wait_cycles=1)
    assert r["unknown_sample_cycles"] == 1
    assert r["max_stall_cycles"] == 1
    assert [f for f in r["findings"] if f["type"] == "long_stall"] == []


def test_no_edges_returns_reason(tmp_path: Path):
    # clk never rises (held low) -> no posedges to sample.
    body = (
        "$timescale 1ns $end\n$scope module top $end\n"
        "$var wire 1 ! clk $end\n$var wire 1 # valid $end\n$var wire 1 % ready $end\n"
        "$upscope $end\n$enddefinitions $end\n#0\n0!\n1#\n1%\n#100\n0!\n"
    )
    wave = _write(tmp_path, "noedge.vcd", body)
    r = inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave,
        clock="top.clk", valid="top.valid", ready="top.ready",
    )
    assert r["sample_count"] == 0
    assert r["reason"] is not None
    assert "edge" in r["reason"]
    assert r["coverage"]["valid_ready_resolved"] is True
    assert r["coverage"]["clock_sampled"] is False
    assert r["coverage"]["stall_checked"] is False
    assert r["coverage"]["backpressure_checked"] is False


def test_missing_valid_signal_reports_reason(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1} for _ in range(3)]
    wave = _write(tmp_path, "missing.vcd", _build_vcd(cycles))
    r = inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave,
        clock="top.clk", valid="top.does_not_exist", ready="top.ready",
    )
    assert r["reason"] is not None
    assert "valid" in r["reason"]


def test_active_low_inverts_polarity(tmp_path: Path):
    # With active_high=False, 0/0 means both asserted -> transfer.
    cycles = [{"valid": 0, "ready": 0} for _ in range(3)]
    r = _run(tmp_path, "activelow.vcd", cycles, active_high=False)
    assert r["transfer_count"] == 3
    assert r["stall_count"] == 0


def test_unresolved_payload_is_loud_not_silently_clean(tmp_path: Path):
    # The A/B footgun: a payload signal that does not resolve must NOT read as
    # "0 violations / clean". It must be flagged loudly and the hold check
    # marked as not run, while the stall metrics still compute.
    cycles = [{"valid": 1, "ready": 1}] + [{"valid": 1, "ready": 0}] * 3 + [{"valid": 1, "ready": 1}]
    r = _run(tmp_path, "loud.vcd", cycles, payload=["top.nonexistent_bus"], max_wait_cycles=1)
    assert r["payload_unresolved"] == ["top.nonexistent_bus"]
    assert r["payload_hold_checked"] is False
    assert r["coverage"]["payload_hold_requested"] is True
    assert r["coverage"]["payload_hold_checked"] is False
    assert r["coverage"]["payload_signals_checked"] == 0
    assert r["coverage"]["payload_signals_unresolved"] == 1
    assert r["payload_hold_violations"] == 0
    assert any("did NOT run" in w for w in r["warnings"])
    # stall analysis is unaffected by the unresolved payload
    assert r["max_stall_cycles"] == 3
    assert r["reason"] is None  # not a hard failure


def test_resolved_payload_marks_hold_checked(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1, "addr": 0x10} for _ in range(4)]
    r = _run(tmp_path, "checked.vcd", cycles, with_addr=True, payload=["top.addr"])
    assert r["payload_hold_checked"] is True
    assert r["payload_unresolved"] == []
    assert r["coverage"]["payload_hold_requested"] is True
    assert r["coverage"]["payload_hold_checked"] is True
    assert r["coverage"]["payload_signals_checked"] == 1
    assert r["coverage"]["payload_signals_unresolved"] == 0
    assert r["warnings"] == []


def test_mixed_payload_resolution_reports_partial_coverage(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1, "addr": 0x10} for _ in range(4)]
    r = _run(
        tmp_path,
        "partial.vcd",
        cycles,
        with_addr=True,
        payload=["top.addr", "top.missing"],
    )

    assert r["payload_hold_checked"] is False
    assert r["coverage"]["payload_hold_checked"] is False
    assert r["coverage"]["payload_hold_partially_checked"] is True
    assert r["coverage"]["payload_signals_checked"] == 1
    assert r["coverage"]["payload_signals_unresolved"] == 1


# ── AHB-style derived valid (valid_htrans) ────────────────────────────────
# Cycle i: posedge at 10*i+5, sampled at +1. Sets htrans (2-bit), hready,
# haddr at cycle start. AHB encodings: IDLE=0, BUSY=1, NONSEQ=2, SEQ=3.
def _build_ahb_vcd(cycles: list[dict]) -> str:
    lines = [
        "$timescale 1ns/1ps",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        "$var wire 1 % hready $end",
        "$var wire 2 ( htrans $end",
        "$var wire 8 & haddr $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    for i, c in enumerate(cycles):
        lines.append(f"#{10*i}")
        lines.append("0!")
        lines.append(f"{int(c['hready'])}%")
        lines.append(f"b{int(c['htrans']):02b} (")
        lines.append(f"b{int(c.get('haddr', 0)):08b} &")
        lines.append(f"#{10*i+5}")
        lines.append("1!")
    return "\n".join(lines) + "\n"


def _run_ahb(tmp_path, name, cycles, **kwargs):
    wave = _write(tmp_path, name, _build_ahb_vcd(cycles))
    return inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave,
        clock="top.clk", ready="top.hready", valid_htrans="top.htrans", **kwargs,
    )


def test_ahb_active_rule_classifies_and_finds_hold_violation(tmp_path: Path):
    # IDLE -> NONSEQ accepted -> SEQ stalled (hready low) with haddr changing
    # mid-stall (the AHB violation) -> SEQ accepted -> IDLE.
    cycles = [
        {"htrans": 2, "hready": 1, "haddr": 0x10},  # NONSEQ, transfer
        {"htrans": 3, "hready": 0, "haddr": 0x11},  # SEQ, stall begins (latch 0x11)
        {"htrans": 3, "hready": 0, "haddr": 0x12},  # SEQ, stall, haddr moved -> violation
        {"htrans": 3, "hready": 1, "haddr": 0x12},  # SEQ, transfer
        {"htrans": 0, "hready": 1, "haddr": 0x00},  # IDLE -> not valid
    ]
    store = CursorStore()
    r = _run_ahb(tmp_path, "ahb.vcd", cycles, payload=["top.haddr"],
                 max_wait_cycles=8, cursor_store=store)
    assert r["valid_source"] == "htrans:active"
    assert r["transfer_count"] == 2          # the two accepted beats
    assert r["stall_count"] == 2             # two SEQ-with-hready-low cycles
    assert r["payload_hold_violations"] == 1
    viol = [f for f in r["findings"] if f["type"] == "payload_hold_violation"][0]
    assert viol["signal"] == "top.haddr"
    assert r["cursor"] is not None


def test_ahb_busy_counts_only_under_non_idle(tmp_path: Path):
    # A BUSY (htrans=1) cycle with hready low: under 'active' it is not a valid
    # beat (no stall); under 'non_idle' it counts as valid -> a stall.
    cycles = [
        {"htrans": 2, "hready": 1, "haddr": 0x20},  # NONSEQ transfer
        {"htrans": 1, "hready": 0, "haddr": 0x20},  # BUSY, hready low
        {"htrans": 2, "hready": 1, "haddr": 0x21},  # NONSEQ transfer
    ]
    active = _run_ahb(tmp_path, "busy_a.vcd", cycles, max_wait_cycles=8)
    non_idle = _run_ahb(tmp_path, "busy_n.vcd", cycles, htrans_rule="non_idle", max_wait_cycles=8)
    assert active["stall_count"] == 0
    assert non_idle["stall_count"] == 1


def test_ahb_missing_htrans_is_loud(tmp_path: Path):
    cycles = [{"htrans": 2, "hready": 1, "haddr": 0x10}]
    wave = _write(tmp_path, "ahb_missing.vcd", _build_ahb_vcd(cycles))
    r = inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave,
        clock="top.clk", ready="top.hready", valid_htrans="top.does_not_exist",
    )
    assert r["reason"] is not None
    assert "valid_htrans" in r["reason"]


def test_requires_exactly_one_valid_source(tmp_path: Path):
    cycles = [{"htrans": 2, "hready": 1, "haddr": 0x10}]
    wave = _write(tmp_path, "ahb_src.vcd", _build_ahb_vcd(cycles))
    gp = _parser_factory()
    neither = inspect_handshake(get_parser=gp, wave_path=wave, clock="top.clk", ready="top.hready")
    both = inspect_handshake(get_parser=gp, wave_path=wave, clock="top.clk", ready="top.hready",
                             valid="top.hready", valid_htrans="top.htrans")
    assert neither["reason"] and "valid" in neither["reason"]
    assert both["reason"] and "not both" in both["reason"]


def test_explicit_cursor_name(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1}] + [{"valid": 1, "ready": 0}] * 4 + [{"valid": 1, "ready": 1}]
    store = CursorStore()
    r = _run(tmp_path, "named.vcd", cycles, store=store, max_wait_cycles=2,
             cursor_name="stall_start")
    assert r["cursor"]["name"] == "stall_start"
    assert store.get("stall_start") is not None
