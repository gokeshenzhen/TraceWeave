"""Characterize the pinned reader's evidence before adapting its callbacks."""
import pytest

pylibfst = pytest.importorskip("pylibfst", reason="install the optional [fst] extra")
from fst_fixture import write_fst


def test_raw_ticks_four_states_aliases_and_blackouts(tmp_path):
    lib, ffi = pylibfst.lib, pylibfst.ffi
    path = write_fst(tmp_path / "facts.fst", [
        (10, "bus [7:4]", "0000"), (11, "bus [7:4]", "10xz"),
        (11, "bus [7:4]", "1010"), (20, "bus [7:4]", "1111")],
        declarations=[("bus [7:4]", 4, "wire", "input", None),
                      ("alias [0:3]", 4, "wire", "output", "bus [7:4]")],
        scale=-13, start=10, end=25, activity=[(15, 0), (20, 1)])
    reader = lib.fstReaderOpen(str(path).encode())
    assert reader != ffi.NULL
    try:
        assert lib.fstReaderGetTimescale(reader) == -13
        assert (lib.fstReaderGetStartTime(reader), lib.fstReaderGetEndTime(reader)) == (10, 25)
        declarations = []
        while True:
            node = lib.fstReaderIterateHier(reader)
            if node == ffi.NULL:
                break
            if node.htyp == lib.FST_HT_VAR:
                declarations.append((ffi.string(node.u.var.name), node.u.var.handle, node.u.var.is_alias))
        assert declarations == [(b"bus [7:4]", 1, 0), (b"alias [0:3]", 1, 1)]
        assert [(lib.fstReaderGetDumpActivityChangeTime(reader, i),
                 lib.fstReaderGetDumpActivityChangeValue(reader, i))
                for i in range(lib.fstReaderGetNumberDumpActivityChanges(reader))] == [(15, 0), (20, 1)]
        lib.fstReaderSetFacProcessMask(reader, 1)
        events = []
        assert pylibfst.fstReaderIterBlocks(reader,
            lambda _, t, h, v: events.append((t, h, ffi.string(v)))) == 1
        assert events == [(10, 1, b"0000"), (11, 1, b"10xz"),
                          (11, 1, b"1010"), (20, 1, b"1111")]
    finally:
        lib.fstReaderClose(reader)


def test_native_window_is_not_a_strict_event_filter(tmp_path):
    lib, ffi = pylibfst.lib, pylibfst.ffi
    path = write_fst(tmp_path / "window.fst", [(0, "a", "0"), (10, "a", "1"), (20, "a", "0")])
    reader = lib.fstReaderOpen(str(path).encode())
    try:
        lib.fstReaderSetFacProcessMaskAll(reader)
        lib.fstReaderSetLimitTimeRange(reader, 11, 19)
        events = []
        pylibfst.fstReaderIterBlocks(reader,
            lambda _, t, h, v: events.append((t, ffi.string(v))))
        # The filter admits a compressed block; the adapter must clip events.
        assert events and any(t < 11 or t > 19 for t, _ in events)
        assert not [(t, v) for t, v in events if 11 <= t <= 19]
    finally:
        lib.fstReaderClose(reader)
