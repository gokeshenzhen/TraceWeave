"""
Regression guard for the FSDB wide-bus fixed-buffer truncation bug.

Background: fsdb_wrapper.cpp emitted each transition into a fixed stack buffer
(`char line[512]`). A wide bus (e.g. 1024-bit AXI wdata renders to ~1024 chars
via _VCToStr) overflowed the buffer, so snprintf truncated the line and dropped
the trailing '\n' — gluing every transition into one. get_transitions then
returned transition_count=1 with later timestamps swallowed into the value, and
the payload-hold check (which samples via get_transitions + bisect) saw a
constant value across the stall and silently reported 0 violations on exactly
the wide data buses it targets. get_value_at_time had a sibling 1-bit-truncation
bug (fixed 1024-byte buffer / strncpy) that shifted a 1024-bit hex by one bit.

Fixture wide_bus.fsdb (built from wide_bus_tb.sv with VCS+Verdi) has three
valid/ready interfaces on a 10 ns clock:
  IF1 (vld1/rdy1/dat1, 1024-bit): payload changes mid-stall -> 1 violation
  IF2 (vld2/rdy2/dat2, 1024-bit): payload held during stall -> 0 (no false pos)
  IF3 (vld3/rdy3/dat3,   64-bit): payload changes mid-stall -> 1 (narrow path)
"""

import os
from pathlib import Path

import pytest

from src import handshake_sweep, operation_metrics, verify_condition as vc
from src.fsdb_parser import FSDBParser, get_fsdb_runtime_info
from src.handshake_sweep import sweep_handshake_anomalies

FIXTURE = Path(__file__).parent / "fixtures" / "wide_bus.fsdb"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="wide_bus.fsdb fixture missing",
)


@pytest.fixture(scope="module")
def parser():
    if not get_fsdb_runtime_info().get("enabled"):
        pytest.skip("FSDB runtime unavailable (build libfsdb_wrapper.so / set VERDI_HOME)")
    p = FSDBParser(str(FIXTURE))
    try:
        p._open()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot open FSDB fixture: {exc}")
    yield p
    p.close()


def test_wide_bus_transitions_not_glued(parser):
    """A 1024-bit bus must yield distinct per-change transitions, each a clean
    full-width value — not one glued record (the fixed-buffer truncation bug)."""
    r = parser.get_transitions("tb.dat1[1023:0]")
    # 0 (t=0) -> beat0 (30 ns) -> beat1 (60 ns)
    assert r["transition_count"] >= 3, r["transition_count"]
    hexes = [t["value"]["hex"] for t in r["transitions"]]
    # every value renders as a full 1024-bit hex (0x + 256 digits), none null
    assert all(h is not None and len(h) == 2 + 256 for h in hexes), hexes
    # distinct timestamps carry distinct values (no stale-glue collapse)
    assert len(set(hexes)) == len(hexes)


def test_wide_bus_value_at_time_not_truncated(parser):
    """get_value_at_time must not drop the top bit of a 1024-bit value."""
    v30 = parser.get_value_at_time("tb.dat1[1023:0]", 30000)["value"]
    v60 = parser.get_value_at_time("tb.dat1[1023:0]", 60000)["value"]
    assert v30["bin"] is not None and len(v30["bin"]) == 1024
    assert v30["hex"] != v60["hex"]
    # top byte preserved (0x5a..); a dropped LSB would shift it to 0x2d..
    assert v30["hex"].startswith("0x5a")


def test_group_loaded_transitions_are_byte_equivalent(parser):
    """One native load for several signals must preserve each signal's legacy
    independent-buffer transition result, including the 1024-bit payload."""
    signals = ["tb.aclk", "tb.vld1", "tb.rdy1", "tb.dat1[1023:0]"]
    expected = {path: parser.get_transitions(path) for path in signals}
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value("_sweep_active", True)
    try:
        with parser.transition_group(signals) as active:
            assert active is True
            actual = {path: parser.get_transitions(path) for path in signals}
    finally:
        operation_metrics.pop(token)

    assert actual == expected
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_native_group_count"] == 1
    assert snapshot["sweep_native_group_signal_total"] == len(signals)
    assert snapshot["sweep_native_profiled_read_count"] == len(signals)
    assert snapshot["sweep_native_transition_count"] > 0
    assert snapshot["sweep_native_output_bytes"] > 0


def test_full_sweep_uses_one_native_group_for_shared_clock(parser, monkeypatch):
    candidates = [
        {
            "scope": "tb", "clock": "tb.aclk",
            "valid": f"tb.vld{index}", "ready": f"tb.rdy{index}",
            "payload": [
                f"tb.dat{index}[{'63:0' if index == 3 else '1023:0'}]"
            ],
            "confidence": "high",
        }
        for index in (1, 2, 3)
    ]
    monkeypatch.setattr(
        handshake_sweep,
        "suggest_handshakes",
        lambda **kwargs: {
            "candidate_count": len(candidates), "candidates": candidates,
            "reason": None,
        },
    )
    monkeypatch.setattr(
        handshake_sweep,
        "suggest_protocol_bundles",
        lambda **kwargs: {"candidate_count": 0, "candidates": [], "reason": None},
    )

    # First force the safe oversized fallback, then compare the complete sweep
    # result with the native group path. This guards the fact table and coverage
    # contract, not merely individual transition lists.
    monkeypatch.setenv("TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS", "1")
    legacy_result = sweep_handshake_anomalies(
        get_parser=lambda _: parser,
        wave_path=str(FIXTURE),
        max_interfaces=8,
    )
    monkeypatch.setenv("TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS", "16")
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        result = sweep_handshake_anomalies(
            get_parser=lambda _: parser,
            wave_path=str(FIXTURE),
            max_interfaces=8,
        )
    finally:
        operation_metrics.pop(token)

    assert result == legacy_result
    assert result["coverage_status"] == "complete"
    assert result["interface_count"] == 3
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_unique_clocks"] == 1
    assert snapshot["sweep_native_group_count"] == 1
    assert snapshot["sweep_native_group_fallback_count"] == 0
    assert snapshot["sweep_native_profiled_read_count"] == 10
    assert snapshot["sweep_clock_read_count"] == 1
    assert snapshot["sweep_signal_read_count"] == 9
    assert snapshot["sweep_native_transition_count"] > 0


def test_inspect_handshake_payload_hold_on_wide_bus(parser):
    """End-to-end: payload-hold fires on the wide violating bus, stays silent on
    the wide clean bus, and still fires on the narrow bus."""
    cache = {str(FIXTURE): parser}

    def get_parser(wave):
        return cache[wave]

    common = dict(get_parser=get_parser, wave_path=str(FIXTURE), clock="tb.aclk")

    r1 = vc.inspect_handshake(valid="tb.vld1", ready="tb.rdy1",
                              payload=["tb.dat1[1023:0]"], **common)
    assert r1["payload_hold_violations"] == 1
    assert r1["payload_hold_checked"] is True
    assert r1["violating_signal"] == "tb.dat1[1023:0]"
    assert r1["attribution"].get("violating_side") == "valid_driver"

    r2 = vc.inspect_handshake(valid="tb.vld2", ready="tb.rdy2",
                              payload=["tb.dat2[1023:0]"], **common)
    assert r2["payload_hold_violations"] == 0  # held payload -> no false positive
    assert r2["payload_hold_checked"] is True

    r3 = vc.inspect_handshake(valid="tb.vld3", ready="tb.rdy3",
                              payload=["tb.dat3[63:0]"], **common)
    assert r3["payload_hold_violations"] == 1  # narrow path unchanged
