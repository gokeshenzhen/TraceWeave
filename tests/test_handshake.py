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
        "$var wire 1 ) hwrite $end",
        "$var wire 8 * hwdata $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    for i, c in enumerate(cycles):
        lines.append(f"#{10*i}")
        lines.append("0!")
        lines.append(f"{int(c['hready'])}%")
        lines.append(f"b{int(c['htrans']):02b} (")
        haddr = c.get("haddr", 0)
        if haddr == "x":
            lines.append("bxxxxxxxx &")
        else:
            lines.append(f"b{int(haddr):08b} &")
        lines.append(f"{int(c.get('hwrite', 0))})")
        hwdata = c.get("hwdata", 0)
        if hwdata == "x":
            lines.append("bxxxxxxxx *")
        else:
            lines.append(f"b{int(hwdata):08b} *")
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


def test_ahb_x_while_valid_flags_control_x_under_asserted_valid(tmp_path: Path):
    # A NONSEQ beat (valid asserted) carrying an x on HADDR is a definite
    # violation: an active transfer with an unknown address field.
    cycles = [
        {"htrans": 2, "hready": 1, "haddr": 0x10},   # NONSEQ, known addr — clean
        {"htrans": 2, "hready": 1, "haddr": "x"},     # NONSEQ, addr x -> x_while_valid
        {"htrans": 0, "hready": 1, "haddr": 0x00},     # IDLE
    ]
    store = CursorStore()
    r = _run_ahb(tmp_path, "ahb_xwv.vcd", cycles, payload=["top.haddr"],
                 max_wait_cycles=8, cursor_store=store)
    assert r["coverage"]["x_while_valid_checked"] is True
    assert r["x_while_valid_violations"] == 1
    viol = [f for f in r["findings"] if f["type"] == "x_while_valid"]
    assert len(viol) == 1 and viol[0]["signal"] == "top.haddr"
    # one-sided: attributed to the valid-driver, bridges to driver lookup
    assert r["violating_signal"] == "top.haddr"
    assert r["attribution"]["violating_side"] == "valid_driver"
    assert r["next_actions"] and r["next_actions"][0]["tool"] == "explain_signal_driver"
    assert r["cursor"] is not None


def test_ahb_x_while_valid_debounces_persistent_x(tmp_path: Path):
    # An x that persists across multiple asserted-valid cycles reports ONCE per
    # episode, not every cycle; a fresh beat after a known cycle re-reports.
    cycles = [
        {"htrans": 2, "hready": 1, "haddr": "x"},   # episode 1 begins
        {"htrans": 2, "hready": 1, "haddr": "x"},   # same episode — no new finding
        {"htrans": 2, "hready": 1, "haddr": 0x10},   # known — episode ends
        {"htrans": 2, "hready": 1, "haddr": "x"},   # episode 2 — re-reports
    ]
    r = _run_ahb(tmp_path, "ahb_xwv_deb.vcd", cycles, payload=["top.haddr"],
                 max_wait_cycles=8)
    assert r["x_while_valid_violations"] == 2


def test_ahb_x_while_valid_not_flagged_when_valid_deasserted(tmp_path: Path):
    # x on HADDR while HTRANS is IDLE (no active transfer) is NOT a violation.
    cycles = [
        {"htrans": 0, "hready": 1, "haddr": "x"},   # IDLE, addr x -> benign
        {"htrans": 2, "hready": 1, "haddr": 0x10},   # NONSEQ, known
    ]
    r = _run_ahb(tmp_path, "ahb_xwv_idle.vcd", cycles, payload=["top.haddr"],
                 max_wait_cycles=8)
    assert r["x_while_valid_violations"] == 0


def test_literal_valid_does_not_run_x_while_valid(tmp_path: Path):
    # For a literal-valid interface the payload may be data lanes (legally x on
    # disabled byte strobes), so the check stays OFF — no false positive.
    cycles = [
        {"valid": 1, "ready": 1, "addr": "x"},
        {"valid": 1, "ready": 1, "addr": 0x10},
    ]
    r = _run(tmp_path, "litval_x.vcd", cycles, with_addr=True, payload=["top.addr"],
             max_wait_cycles=8)
    assert r["coverage"]["x_while_valid_checked"] is False
    assert r["x_while_valid_violations"] == 0


def test_ahb_write_data_hold_violation(tmp_path: Path):
    # Write addr accepted (c0); data phase c1..c3 with HREADY low at c1/c2. HWDATA
    # must stay constant through the wait; changing it mid-wait is a violation.
    cycles = [
        {"htrans": 2, "hready": 1, "hwrite": 1, "haddr": 0x10, "hwdata": 0x00},  # write addr accepted
        {"htrans": 0, "hready": 0, "hwdata": 0xAA},   # data phase starts (latch 0xAA)
        {"htrans": 0, "hready": 0, "hwdata": 0xBB},   # HWDATA moved mid-wait -> violation
        {"htrans": 0, "hready": 1, "hwdata": 0xBB},   # data accepted
        {"htrans": 0, "hready": 1, "hwdata": 0x00},   # next: HWDATA may change (legal)
    ]
    store = CursorStore()
    r = _run_ahb(tmp_path, "ahb_wdh.vcd", cycles, payload=["top.haddr"],
                 hwrite="top.hwrite", write_data="top.hwdata",
                 max_wait_cycles=8, cursor_store=store)
    assert r["coverage"]["write_data_hold_checked"] is True
    assert r["write_data_hold_violations"] == 1
    viol = [f for f in r["findings"] if f["type"] == "write_data_hold_violation"]
    assert len(viol) == 1 and viol[0]["signal"] == "top.hwdata"
    assert r["violating_signal"] == "top.hwdata"
    assert r["attribution"]["violating_side"] == "valid_driver"
    assert r["next_actions"] and r["next_actions"][0]["tool"] == "explain_signal_driver"
    assert r["cursor"] is not None


def test_ahb_write_data_held_through_wait_is_clean(tmp_path: Path):
    cycles = [
        {"htrans": 2, "hready": 1, "hwrite": 1, "haddr": 0x10, "hwdata": 0x00},
        {"htrans": 0, "hready": 0, "hwdata": 0xAA},
        {"htrans": 0, "hready": 0, "hwdata": 0xAA},   # held — legal
        {"htrans": 0, "hready": 1, "hwdata": 0xAA},   # accepted
        {"htrans": 0, "hready": 1, "hwdata": 0x55},   # next data, legal
    ]
    r = _run_ahb(tmp_path, "ahb_wdh_clean.vcd", cycles,
                 hwrite="top.hwrite", write_data="top.hwdata", max_wait_cycles=8)
    assert r["write_data_hold_violations"] == 0


def test_ahb_read_data_phase_not_checked_for_write_hold(tmp_path: Path):
    # A read transfer (HWRITE=0) drives no write data; HWDATA churn must NOT flag.
    cycles = [
        {"htrans": 2, "hready": 1, "hwrite": 0, "haddr": 0x10, "hwdata": 0x00},
        {"htrans": 0, "hready": 0, "hwdata": 0xAA},
        {"htrans": 0, "hready": 0, "hwdata": 0xBB},   # churn, but it's a READ -> ignore
        {"htrans": 0, "hready": 1, "hwdata": 0xCC},
    ]
    r = _run_ahb(tmp_path, "ahb_rd_nohold.vcd", cycles,
                 hwrite="top.hwrite", write_data="top.hwdata", max_wait_cycles=8)
    assert r["write_data_hold_violations"] == 0


def test_ahb_emits_protocol_semantics_receipt(tmp_path: Path):
    # An AHB run carries a receipt naming which metrics are faithful vs suppressed,
    # so a reader cannot wave a real finding away as a valid/ready-vs-AHB mismatch.
    cycles = [{"htrans": 2, "hready": 1, "haddr": 0x10},
              {"htrans": 0, "hready": 1, "haddr": 0x00}]
    r = _run_ahb(tmp_path, "ahb_psem.vcd", cycles, payload=["top.haddr"])
    ps = r["protocol_semantics"]
    assert ps is not None and ps["protocol"] == "ahb"
    assert "faithful" in ps["valid_hold"]
    assert "not_a_violation" in ps["ready_without_valid"]
    assert "HWDATA" in ps["payload_hold"]


def test_literal_valid_has_no_protocol_semantics(tmp_path: Path):
    # A literal-valid interface needs no receipt — every metric is faithful as-is.
    cycles = [{"valid": 1, "ready": 1}, {"valid": 0, "ready": 0}]
    r = _run(tmp_path, "litval_psem.vcd", cycles)
    assert r["protocol_semantics"] is None


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


def test_payload_hold_violation_attribution(tmp_path: Path):
    # The payload-hold violation names the master-driven signal and bridges to
    # explain_signal_driver — bus facts do not self-attribute master vs slave.
    cycles = [
        {"valid": 1, "ready": 1, "addr": 0x10},
        {"valid": 1, "ready": 0, "addr": 0x10},
        {"valid": 1, "ready": 0, "addr": 0x11},
    ]
    r = _run(tmp_path, "hold_attr.vcd", cycles, with_addr=True,
             payload=["top.addr"], max_wait_cycles=100)
    assert r["payload_hold_violations"] == 1
    assert r["violating_signal"] == "top.addr"
    assert len(r["next_actions"]) == 1
    na = r["next_actions"][0]
    assert na["tool"] == "explain_signal_driver"
    assert na["signal_path"] == "top.addr"
    assert "master" in na["reason"]


def test_stall_attribution_points_at_ready(tmp_path: Path):
    # A long stall has no single faulty signal; attribution lever is ready
    # (slave back-pressure) vs the master's valid -> next_action targets ready,
    # violating_signal stays None (it is a relationship, not one signal).
    cycles = [{"valid": 1, "ready": 1}] + [{"valid": 1, "ready": 0}] * 5 + [{"valid": 1, "ready": 1}]
    r = _run(tmp_path, "stall_attr.vcd", cycles, max_wait_cycles=3)
    assert any(f["type"] == "long_stall" for f in r["findings"])
    assert r["violating_signal"] is None
    assert len(r["next_actions"]) == 1
    assert r["next_actions"][0]["tool"] == "explain_signal_driver"
    assert r["next_actions"][0]["signal_path"] == "top.ready"


def test_clean_handshake_has_no_next_actions(tmp_path: Path):
    cycles = [{"valid": 1, "ready": 1} for _ in range(4)]
    r = _run(tmp_path, "clean_attr.vcd", cycles)
    assert r["findings"] == []
    assert r["violating_signal"] is None
    assert r["next_actions"] == []


# ---------------------------------------------------------------------------
# premature valid deassertion (wait-state hold) — the AHB
# master-not-waiting-for-HREADY signature payload_hold cannot see.
# ---------------------------------------------------------------------------


def test_premature_valid_deassertion(tmp_path: Path):
    # 1-cycle stall (valid high, ready low), then the master drops valid before
    # ready ever arrived. payload_hold cannot catch this (stall is 1 cycle, and
    # valid itself is not a payload signal); the wait-state hold check does.
    cycles = [
        {"valid": 1, "ready": 1},
        {"valid": 1, "ready": 0},  # stall begins (cycle 1, posedge 15000)
        {"valid": 0, "ready": 0},  # master drops the transfer (cycle 2, 25000)
        {"valid": 0, "ready": 0},
    ]
    store = CursorStore()
    r = _run(tmp_path, "deassert.vcd", cycles, store=store, max_wait_cycles=100)
    assert r["valid_deassert_violations"] == 1
    assert r["payload_hold_violations"] == 0
    assert r["max_stall_cycles"] == 1
    viols = [f for f in r["findings"] if f["type"] == "premature_valid_deassertion"]
    assert len(viols) == 1
    f = viols[0]
    assert f["signal"] == "top.valid"
    assert f["time_ps"] == 25000
    assert f["stall_begin_ps"] == 15000
    assert f["stall_cycles"] == 1
    # the witness: the dropped beat was never accepted (forecloses the
    # "just pipeline overlap" misreading of this true positive)
    assert f["accepted_before_deassert"] is False
    assert f["severity"] == "error"
    # cursor anchored at the deassertion
    assert r["cursor"]["time_ps"] == 25000
    assert len(store) == 1
    # attribution points at the master-driven valid + bridges to driver lookup
    assert r["violating_signal"] == "top.valid"
    assert len(r["next_actions"]) == 1
    na = r["next_actions"][0]
    assert na["tool"] == "explain_signal_driver"
    assert na["signal_path"] == "top.valid"
    assert "master" in na["reason"]
    assert r["coverage"]["valid_hold_requested"] is True
    assert r["coverage"]["valid_hold_checked"] is True


def test_deassert_against_ready_high_is_violation(tmp_path: Path):
    # Stall, then valid drops the same cycle ready goes high: the beat the slave
    # was finally ready to accept is gone — still a hold violation.
    cycles = [
        {"valid": 1, "ready": 0},  # stall (cycle 0, posedge 5000)
        {"valid": 0, "ready": 1},  # master drops valid as ready arrives
    ]
    r = _run(tmp_path, "deassert_rdy.vcd", cycles, max_wait_cycles=100)
    assert r["valid_deassert_violations"] == 1
    assert [f["type"] for f in r["findings"]] == ["premature_valid_deassertion"]


def test_normal_stall_then_accept_is_clean(tmp_path: Path):
    # valid held through the stall until accepted -> no deassertion.
    cycles = [
        {"valid": 1, "ready": 0},
        {"valid": 1, "ready": 0},
        {"valid": 1, "ready": 1},  # accepted, valid was held the whole time
        {"valid": 0, "ready": 0},  # legitimate drop AFTER acceptance
    ]
    r = _run(tmp_path, "held.vcd", cycles, max_wait_cycles=100)
    assert r["valid_deassert_violations"] == 0
    assert [f for f in r["findings"] if f["type"] == "premature_valid_deassertion"] == []


def test_unknown_valid_after_stall_is_not_deassertion(tmp_path: Path):
    # An x on valid the cycle after a stall is "unknown", not a deassertion.
    cycles = [
        {"valid": 1, "ready": 0},   # stall
        {"valid": "x", "ready": 0},  # unknown valid -> counted unknown, no flag
    ]
    r = _run(tmp_path, "deassert_x.vcd", cycles, max_wait_cycles=100)
    assert r["valid_deassert_violations"] == 0
    assert r["unknown_sample_cycles"] == 1
    assert [f for f in r["findings"] if f["type"] == "premature_valid_deassertion"] == []


def test_check_valid_hold_false_disables_check(tmp_path: Path):
    cycles = [
        {"valid": 1, "ready": 0},
        {"valid": 0, "ready": 0},
    ]
    r = _run(tmp_path, "deassert_off.vcd", cycles, check_valid_hold=False,
             max_wait_cycles=100)
    assert r["valid_deassert_violations"] == 0
    assert r["findings"] == []
    assert r["coverage"]["valid_hold_requested"] is False
    assert r["coverage"]["valid_hold_checked"] is False


def test_ahb_htrans_deassertion(tmp_path: Path):
    # AHB master not waiting for HREADY: htrans NONSEQ (2) while hready low, then
    # back to IDLE (0) the next cycle. valid is derived from htrans.
    lines = [
        "$timescale 1ns $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        "$var wire 2 # htrans $end",
        "$var wire 1 % hready $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    # cycle 0: htrans=NONSEQ(10), hready=0 (stall); cycle 1: htrans=IDLE(00)
    htrans_bits = ["10", "00"]
    hready_bits = ["0", "0"]
    for i, (ht, hr) in enumerate(zip(htrans_bits, hready_bits)):
        t_low = 10 * i
        t_high = 10 * i + 5
        lines.append(f"#{t_low}")
        lines.append("0!")
        lines.append(f"b{ht} #")
        lines.append(f"{hr}%")
        lines.append(f"#{t_high}")
        lines.append("1!")
    wave = _write(tmp_path, "ahb_deassert.vcd", "\n".join(lines) + "\n")
    r = inspect_handshake(
        get_parser=_parser_factory(),
        wave_path=wave,
        clock="top.clk",
        valid_htrans="top.htrans",
        htrans_rule="active",
        ready="top.hready",
        max_wait_cycles=100,
    )
    assert r["valid_source"] == "htrans:active"
    assert r["valid_deassert_violations"] == 1
    viols = [f for f in r["findings"] if f["type"] == "premature_valid_deassertion"]
    assert len(viols) == 1
    assert viols[0]["signal"] == "top.htrans"


# ---------------------------------------------------------------------------
# structured side attribution (one-sided violations -> valid_driver side;
# two-sided stall -> no side). Role from protocol, never read off the trace.
# ---------------------------------------------------------------------------


def test_deassertion_attribution_blames_valid_driver(tmp_path: Path):
    cycles = [
        {"valid": 1, "ready": 0},
        {"valid": 0, "ready": 0},
    ]
    r = _run(tmp_path, "attr_deassert.vcd", cycles, max_wait_cycles=100)
    a = r["attribution"]
    assert a["violating_side"] == "valid_driver"
    assert a["exonerated_side"] == "ready_driver"
    assert a["basis"] == "protocol_valid_hold_obligation"
    assert "slave driver/monitor" in a["note"]


def test_payload_hold_attribution_blames_valid_driver(tmp_path: Path):
    cycles = [
        {"valid": 1, "ready": 0, "addr": 0x10},
        {"valid": 1, "ready": 0, "addr": 0x11},
    ]
    r = _run(tmp_path, "attr_hold.vcd", cycles, with_addr=True,
             payload=["top.addr"], max_wait_cycles=100)
    assert r["payload_hold_violations"] == 1
    a = r["attribution"]
    assert a["violating_side"] == "valid_driver"
    assert a["exonerated_side"] == "ready_driver"
    assert a["basis"] == "protocol_payload_hold_obligation"
    assert "slave driver/monitor" in a["note"]


def test_plain_stall_attribution_is_two_sided(tmp_path: Path):
    # A long stall (valid held, ready never comes) is genuinely two-sided: the
    # trace cannot attribute a side.
    cycles = [{"valid": 1, "ready": 0}] * 8
    r = _run(tmp_path, "attr_stall.vcd", cycles, max_wait_cycles=3)
    a = r["attribution"]
    assert a["violating_side"] is None
    assert a["exonerated_side"] is None
    assert a["basis"] == "two_sided_stall"


def test_clean_attribution_is_empty(tmp_path: Path):
    r = _run(tmp_path, "attr_clean.vcd", [{"valid": 1, "ready": 1}] * 4)
    a = r["attribution"]
    assert a["violating_side"] is None and a["basis"] is None and a["note"] is None


def test_ahb_attribution_note_says_master(tmp_path: Path):
    # For an AHB (htrans-derived) valid the note can name the master directly,
    # since HTRANS is always master-driven.
    lines = [
        "$timescale 1ns $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        "$var wire 2 # htrans $end",
        "$var wire 1 % hready $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    for i, (ht, hr) in enumerate(zip(["10", "00"], ["0", "0"])):
        t_low, t_high = 10 * i, 10 * i + 5
        lines += [f"#{t_low}", "0!", f"b{ht} #", f"{hr}%", f"#{t_high}", "1!"]
    wave = _write(tmp_path, "attr_ahb.vcd", "\n".join(lines) + "\n")
    r = inspect_handshake(
        get_parser=_parser_factory(), wave_path=wave, clock="top.clk",
        valid_htrans="top.htrans", htrans_rule="active", ready="top.hready",
        max_wait_cycles=100,
    )
    a = r["attribution"]
    assert a["violating_side"] == "valid_driver"
    assert "master" in a["note"]
