import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.vcd_parser import VCDParser
from src.waveform_batch import (
    VCDBatchReader,
    WaveformBatchReader,
    make_batch_reader,
)


_VCD = os.path.join(os.path.dirname(__file__), "fixtures", "cycle_test.vcd")


def test_vcd_batch_reader_satisfies_protocol():
    reader = VCDBatchReader(VCDParser(_VCD))
    assert isinstance(reader, WaveformBatchReader)


def test_vcd_batch_window_basic():
    reader = VCDBatchReader(VCDParser(_VCD))
    r = reader.values_in_window(["top_tb.clk", "top_tb.data"], 0, 2000)
    assert r["start_ps"] == 0
    assert r["end_ps"] == 2000
    assert r["missing"] == []
    assert not r["truncated"]
    times = sorted({tr["time_ps"] for tr in r["transitions"]})
    assert times == [t for t in times if t <= 2000]
    paths = {tr["signal"] for tr in r["transitions"]}
    assert paths == {"top_tb.clk", "top_tb.data"}


def test_vcd_batch_window_chronological_order():
    reader = VCDBatchReader(VCDParser(_VCD))
    r = reader.values_in_window(["top_tb.clk"], 0, 3000)
    times = [tr["time_ps"] for tr in r["transitions"]]
    assert times == sorted(times)


def test_vcd_batch_missing_signal():
    reader = VCDBatchReader(VCDParser(_VCD))
    r = reader.values_in_window(["top_tb.clk", "top_tb.ghost"], 0, 1000)
    assert "top_tb.ghost" in r["missing"]
    assert all(tr["signal"] == "top_tb.clk" for tr in r["transitions"])


def test_vcd_batch_empty_signals():
    reader = VCDBatchReader(VCDParser(_VCD))
    r = reader.values_in_window([], 0, 1000)
    assert r["transitions"] == []
    assert r["missing"] == []


def test_vcd_batch_window_excludes_outside_range():
    reader = VCDBatchReader(VCDParser(_VCD))
    r = reader.values_in_window(["top_tb.clk"], 1500, 2000)
    times = {tr["time_ps"] for tr in r["transitions"]}
    assert all(1500 <= t <= 2000 for t in times)


def test_make_batch_reader_dispatches_by_extension():
    reader = make_batch_reader(_VCD)
    assert isinstance(reader, VCDBatchReader)


def test_make_batch_reader_rejects_unknown_extension():
    with pytest.raises(ValueError):
        make_batch_reader("foo.bar")


def test_make_batch_reader_fsdb_returns_fsdb_impl():
    """We don't open a real FSDB here — just type check the dispatch."""
    from src.waveform_batch import FSDBBatchReader
    reader = make_batch_reader("/no/such/file.fsdb")
    assert isinstance(reader, FSDBBatchReader)
