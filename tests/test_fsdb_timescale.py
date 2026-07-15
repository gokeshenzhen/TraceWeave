"""
Cross-scale regression for the FSDB tick<->ps conversion (the time-scale bug).

Background: FSDB tags are tick counts, not picoseconds; real_time = tick *
scale, with scale declared in the file header (ffrGetScaleUnit(), e.g.
"100fs"). fsdb_wrapper.cpp historically never read the scale and treated
1 tick as 1 ps, so on any non-1ps FSDB every time-based query was off by a
constant factor in BOTH directions: user ps input was interpreted as ticks
(query landed at the wrong instant, silently returning a wrong value) and
returned timestamps were raw ticks (inflated/deflated). The two errors cancel
when a tool-reported timestamp is fed back into a tool, so tool-only loops
looked correct — only external ground-truth times (Verdi cursor, sim-log
times) exposed the bug. These tests therefore assert against REAL picosecond
ground truth baked into the fixtures at simulation time.

Fixtures (sources committed next to them):
  scale_100fs.fsdb (scale_100fs_tb.v, `timescale 1ns/100fs): 1 tick = 0.1 ps.
    addr = 0 @ 0 ps, AAAA0000 @ 100000 ps, BBBB0000 @ 100100 ps,
    CCCC0000 @ 100100.5 ps (sub-ps; reported as 100101 under ceil-on-output).
  scale_1ns.fsdb (scale_1ns_tb.v, `timescale 1ns/1ns): 1 tick = 1000 ps.
    addr = 0 @ 0 ps, AAAA0000 @ 100000 ps, BBBB0000 @ 101000 ps.
  wide_bus.fsdb (`timescale 1ns/1ps): the historical "invisible" case —
    factor 1, guards that a 1ps FSDB is bit-for-bit unaffected by the fix.

The buggy wrapper fails these loudly: on scale_100fs, value@100000ps reads the
0.124-ms-earlier initial value 0x00000000 and transitions report ~10x times.
"""

import os
from pathlib import Path

import pytest

from src.fsdb_parser import FSDBParser, get_fsdb_runtime_info

FIXTURES = Path(__file__).parent / "fixtures"

FSDB_100FS = FIXTURES / "scale_100fs.fsdb"
FSDB_1NS = FIXTURES / "scale_1ns.fsdb"
FSDB_1PS = FIXTURES / "wide_bus.fsdb"

SIG_100FS = "scale_100fs_tb.addr[31:0]"
SIG_1NS = "scale_1ns_tb.addr[31:0]"

pytestmark = pytest.mark.skipif(
    not (FSDB_100FS.exists() and FSDB_1NS.exists()),
    reason="cross-scale FSDB fixtures missing",
)


def _open_parser(path: Path) -> FSDBParser:
    if not get_fsdb_runtime_info().get("enabled"):
        pytest.skip("FSDB runtime unavailable (build libfsdb_wrapper.so / set VERDI_HOME)")
    p = FSDBParser(str(path))
    try:
        p._open()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot open FSDB fixture: {exc}")
    return p


@pytest.fixture(scope="module")
def p100fs():
    p = _open_parser(FSDB_100FS)
    yield p
    p.close()


@pytest.fixture(scope="module")
def p1ns():
    p = _open_parser(FSDB_1NS)
    yield p
    p.close()


class TestSubPsScale100fs:
    """scale = 100fs (0.1 ps/tick): the buggy wrapper shifts everything 10x."""

    def test_summary_exposes_scale(self, p100fs):
        s = p100fs.get_summary()
        assert s["scale_unit"] == "100fs"
        assert s["scale_fs_per_tick"] == 100
        assert "scale_warning" not in s

    def test_duration_is_real_ps_not_ticks(self, p100fs):
        # Simulation ends at 110 ns = 110000 ps. Tick count would be 1100000.
        s = p100fs.get_summary()
        assert 105000 <= s["simulation_duration_ps"] <= 111000

    def test_value_at_external_ground_truth_time(self, p100fs):
        # THE original symptom: real ps input must hit the real instant, not
        # be reinterpreted as ticks (which lands 10x earlier at the initial 0).
        assert p100fs.get_value_at_time(SIG_100FS, 100000)["value"]["hex"] == "0xaaaa0000"
        assert p100fs.get_value_at_time(SIG_100FS, 99999)["value"]["hex"] == "0x00000000"
        assert p100fs.get_value_at_time(SIG_100FS, 100100)["value"]["hex"] == "0xbbbb0000"

    def test_transition_timestamps_are_real_ps(self, p100fs):
        r = p100fs.get_transitions(SIG_100FS)
        got = [(t["time_ps"], t["value"]["hex"]) for t in r["transitions"]]
        assert got == [
            (0, "0x00000000"),
            (100000, "0xaaaa0000"),
            (100100, "0xbbbb0000"),
            # true time 100100.5 ps: sub-ps transitions are reported ceil'ed
            # to the next integer ps (see the rounding contract in
            # fsdb_wrapper.cpp) so a re-query at the reported time lands
            # at-or-after the transition.
            (100101, "0xcccc0000"),
        ]

    def test_reported_timestamp_roundtrip_returns_post_transition_value(self, p100fs):
        """Feeding a tool-reported transition timestamp back into
        get_value_at_time must return that transition's (new) value — incl.
        the sub-ps transition, where a floor-on-output convention would land
        before the edge and return the stale value."""
        r = p100fs.get_transitions(SIG_100FS)
        for t in r["transitions"]:
            back = p100fs.get_value_at_time(SIG_100FS, t["time_ps"])
            assert back["value"]["hex"] == t["value"]["hex"], t

    def test_around_time_window_in_real_ps(self, p100fs):
        r = p100fs.get_signals_around_time([SIG_100FS], 100000, 2000)
        sig = r["signals"][SIG_100FS]
        assert sig["value_at_center"]["bin"].endswith("0" * 16)  # AAAA0000
        times = [t["time_ps"] for t in sig["transitions_in_window"]]
        # ffrGotoXTag(start) lands at-or-before the window start, so the FSDB
        # window has always carried one leading pre-window transition (here
        # t=0); that pre-existing quirk is scale-independent. The point of
        # this assertion is that every in-window timestamp is real ps.
        assert [t for t in times if t >= 98000] == [100000, 100100, 100101]


class TestSuperPsScale1ns:
    """scale = 1ns (1000 ps/tick): the buggy wrapper shrinks times 1000x."""

    def test_summary_exposes_scale(self, p1ns):
        s = p1ns.get_summary()
        assert s["scale_unit"] == "1ns"
        assert s["scale_fs_per_tick"] == 1_000_000

    def test_transition_timestamps_are_real_ps(self, p1ns):
        r = p1ns.get_transitions(SIG_1NS)
        got = [(t["time_ps"], t["value"]["hex"]) for t in r["transitions"]]
        assert got == [
            (0, "0x00000000"),
            (100000, "0xaaaa0000"),
            (101000, "0xbbbb0000"),
        ]

    def test_value_at_external_ground_truth_time(self, p1ns):
        assert p1ns.get_value_at_time(SIG_1NS, 100000)["value"]["hex"] == "0xaaaa0000"
        assert p1ns.get_value_at_time(SIG_1NS, 100999)["value"]["hex"] == "0xaaaa0000"
        assert p1ns.get_value_at_time(SIG_1NS, 101000)["value"]["hex"] == "0xbbbb0000"

    def test_duration_is_real_ps_not_ticks(self, p1ns):
        s = p1ns.get_summary()
        assert 105000 <= s["simulation_duration_ps"] <= 111000


@pytest.mark.skipif(not FSDB_1PS.exists(), reason="wide_bus.fsdb fixture missing")
class Test1psScaleUnchanged:
    """scale = 1ps: the historical no-offset case must stay exact (factor 1)."""

    def test_scale_read_as_1ps(self):
        p = _open_parser(FSDB_1PS)
        try:
            s = p.get_summary()
            assert s["scale_fs_per_tick"] == 1000
            assert s["scale_unit"] == "1ps"
        finally:
            p.close()
