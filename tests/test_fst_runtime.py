"""Native-process lifecycle and bounded wire protocol tests."""
import os
from pathlib import Path
import threading
import time

import pytest

pytest.importorskip("pylibfst", reason="install the optional [fst] extra")
from fst_fixture import write_fst
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.fst_runtime import FstProcess, FstError, _slots
from src.scope_metadata import ScopeIdentityChanged


@pytest.mark.parametrize("cap", [1, 2, 1024])
def test_real_pages_keep_initial_state_and_same_tick_changes(tmp_path, cap):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0"), (10, "a", "1"),
        (10, "a", "x"), (10, "a", "0"), (20, "a", "z")])
    with FstProcess(path) as reader:
        pid, private = reader.process.pid, reader.directory.name
        page = reader.ask(dict(op="stream", path="top.a", start_fs=0, end_fs=30000,
                               max_events=cap, max_bytes=256))
        events, anchors = [], []
        while True:
            assert len(page["events"]) + bool(page["predecessor"]) + bool(page["initial_state"]) <= cap
            assert page["record_bytes"] <= 256
            assert not page["truncated"]
            events.extend(page["events"])
            if page["initial_state"]:
                anchors.append(page["initial_state"])
            if page["complete"]:
                break
            page = reader.ask({"op": "next"})
        assert anchors == [[0, "0"]]
        assert events == [[10, "1"], [10, "x"], [10, "0"], [20, "z"]]
    assert not Path(private).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_strict_window_and_real_predecessor(tmp_path):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0"), (10, "a", "1"), (20, "a", "0")])
    with FstProcess(path) as reader:
        page = reader.ask(dict(op="stream", path="top.a", start_fs=11000, end_fs=19000,
                               max_events=1, max_bytes=128))
        assert page["events"] == [] and page["complete"]
        assert page["predecessor"] == [10, "1"]
        assert page["initial_state"] is None


def test_initial_state_is_not_a_fabricated_predecessor(tmp_path):
    path = write_fst(tmp_path / "a.fst", [(5, "a", "1")], start=5)
    with FstProcess(path) as reader:
        page = reader.ask(dict(op="stream", path="top.a", start_fs=10000, end_fs=20000,
                               max_events=1, max_bytes=128))
        assert page["complete"] and not page["events"]
        assert page["predecessor"] is None and page["initial_state"] == [5, "1"]


def test_cancel_reaps_worker_and_discards_private_directory(tmp_path):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0"), (10, "a", "1")])
    event = threading.Event()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            with FstProcess(path) as reader:
                pid, private = reader.process.pid, reader.directory.name
                reader.ask(dict(op="stream", path="top.a", start_fs=0, end_fs=30000,
                                max_events=1, max_bytes=128))
                event.set()
                started = time.monotonic()
                reader.ask({"op": "next"})
        assert time.monotonic() - started < 1
        assert not Path(private).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        pop_cancel_event(token)


def test_admission_wait_is_cancellable_and_has_deadline(tmp_path):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0")])
    for _ in range(4):
        _slots.acquire()
    event = threading.Event()
    token = push_cancel_event(event)
    try:
        with pytest.raises(FstError, match="fst_timeout"):
            FstProcess(path, timeout_sec=0.1)
        event.set()
        with pytest.raises(OperationCancelled):
            FstProcess(path)
    finally:
        pop_cancel_event(token)
        for _ in range(4):
            _slots.release()


def test_file_change_invalidates_reader(tmp_path):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0")])
    with FstProcess(path) as reader:
        path.write_bytes(path.read_bytes() + b"\0")
        with pytest.raises(ScopeIdentityChanged):
            reader.ask({"op": "metadata"})


@pytest.mark.parametrize("change,reason", [("text", "format_mismatch"), ("truncate", "corrupt")])
def test_bad_files_are_diagnosed_without_native_crash(tmp_path, change, reason):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0")])
    path.write_bytes(b"not a waveform at all" if change == "text" else path.read_bytes()[:-9])
    with pytest.raises(FstError, match=reason):
        FstProcess(path)


@pytest.mark.parametrize("scale,offset,reason", [(-16, 0, "time_precision"), (-12, 5, "timezero")])
def test_unsupported_time_contract_is_explicit(tmp_path, scale, offset, reason):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0")], scale=scale, timezero=offset)
    with pytest.raises(FstError, match=reason):
        FstProcess(path)


def test_declared_native_allocation_is_rejected_before_decode(tmp_path):
    import struct
    path = write_fst(tmp_path / "a.fst", [(0, "a", "0")])
    data = bytearray(path.read_bytes())
    offset = 0
    while data[offset] not in (1, 5, 8):
        offset += 1 + struct.unpack(">Q", data[offset + 1:offset + 9])[0]
    data[offset + 25:offset + 33] = struct.pack(">Q", 1024**3)
    path.write_bytes(data)
    with pytest.raises(FstError, match="native_block_memory_limit"):
        FstProcess(path)
