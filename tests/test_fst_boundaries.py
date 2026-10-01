"""Exercise real native decoding, resource rejection and cancellation cleanup."""
import os
from pathlib import Path
import struct
import sys
import threading
import time

import pytest

pytest.importorskip("pylibfst", reason="install the optional [fst] extra")
from fst_fixture import write_fst
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.fst_parser import FSTParser
from src.fst_runtime import FstError, FstProcess


@pytest.fixture(scope="module")
def dense(tmp_path_factory):
    count = 250000
    return write_fst(tmp_path_factory.mktemp("dense-fst") / "dense.fst",
                     [(i, "a", str(i % 2)) for i in range(count)], end=count)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_stop_during_native_prefix_scan_reaps_actual_worker(dense, stop):
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    stopped = []
    timer = None
    try:
        with pytest.raises(OperationCancelled if stop == "cancel" else FstError):
            with FstProcess(dense) as reader:
                reader.ask({"op": "metadata"})
                pid, private = reader.process.pid, reader.directory.name
                if stop == "cancel":
                    def trigger():
                        stopped.append(time.monotonic())
                        cancel.set()
                    timer = threading.Timer(0.02, trigger)
                    timer.start()
                else:
                    reader.deadline = time.monotonic() + 0.02
                    stopped.append(reader.deadline)
                # No callback page is returned until the late window, so the
                # stop occurs while libfst is actively scanning the prefix.
                reader.ask({"op": "stream", "path": "top.a", "start_fs": 249999000,
                            "end_fs": 250000000, "max_events": 1024, "max_bytes": 262144})
        assert stopped and time.monotonic() - stopped[0] < 1
        assert not Path(private).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if timer:
            timer.join()
        pop_cancel_event(token)


def test_parser_lock_wait_cancellation_never_opens_worker(tmp_path, monkeypatch):
    import src.fst_parser as module
    path = write_fst(tmp_path / "a.fst", [(0, "a", "1")])
    parser = FSTParser(path)
    cancel, started = threading.Event(), threading.Event()
    outcome = []
    def forbidden(*args, **kwargs):
        raise AssertionError("cancelled lock waiter opened a native worker")
    monkeypatch.setattr(module, "FstProcess", forbidden)
    def wait_for_parser():
        token = push_cancel_event(cancel)
        try:
            started.set()
            parser.get_summary()
        except BaseException as exc:
            outcome.append(exc)
        finally:
            pop_cancel_event(token)
    with parser._lock:
        worker = threading.Thread(target=wait_for_parser)
        worker.start()
        assert started.wait(1)
        cancel.set()
        worker.join(1)
        assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], OperationCancelled)


def test_corrupt_compressed_block_cannot_be_reported_complete(dense, tmp_path):
    path = tmp_path / "corrupt.fst"
    data = bytearray(dense.read_bytes())
    offset = 0
    while data[offset] not in (1, 5, 8):
        offset += 1 + struct.unpack(">Q", data[offset + 1:offset + 9])[0]
    length = struct.unpack(">Q", data[offset + 1:offset + 9])[0]
    tail = offset + length + 1 - 24
    unpacked, packed, _ = struct.unpack(">QQQ", data[tail:tail + 24])
    assert packed < unpacked
    data[tail - packed] ^= 0xFF
    path.write_bytes(data)
    with pytest.raises(FstError, match="fst_(worker_failed|decode_failed)"):
        with FstProcess(path) as reader:
            pid, private = reader.process.pid, reader.directory.name
            reader.ask({"op": "stream", "path": "top.a", "start_fs": 0,
                        "end_fs": 250000000, "max_events": 1024, "max_bytes": 262144})
    assert not Path(private).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_wide_signal_pages_stop_explicitly_when_one_record_will_not_fit(tmp_path):
    path = write_fst(tmp_path / "wide.fst", [(0, "wide [65535:0]", "x" * 65536),
                    (10, "wide [65535:0]", "z" * 65536)],
                    declarations=[("wide [65535:0]", 65536, "wire", "input", None)])
    result = FSTParser(path).get_transitions("top.wide")
    assert result["initial_state"]["value"]["bin"] == "x" * 65536
    assert result["transitions"][0]["value"]["bin"] == "z" * 65536
    with FstProcess(path) as reader:
        page = reader.ask({"op": "stream", "path": "top.wide", "start_fs": 0,
                           "end_fs": 30000, "max_events": 1, "max_bytes": 128})
        assert page["truncated"] and not page["complete"] and not page["events"]


@pytest.mark.parametrize("count,code", [(6000, "metadata_memory_limit"), (32769, "metadata_entry_limit")])
def test_metadata_work_is_bounded_before_returning_partial_names(tmp_path, count, code):
    path = write_fst(tmp_path / "index.fst", [(0, "a", "1")], declarations=[
        ("a", 1, "wire", "input", None),
        *[(f"alias{i}" + ("n" * 4000 if code == "metadata_memory_limit" else ""),
           1, "wire", "output", "a") for i in range(count - 1)]])
    with pytest.raises(FstError, match=code):
        FSTParser(path).get_summary()


def test_multiple_native_blocks_do_not_fabricate_snapshot_transitions(tmp_path):
    rows = [(i, "a", str(i % 2)) for i in range(300)]
    path = write_fst(tmp_path / "blocks.fst", rows, flush=[99, 199], end=300)
    result = FSTParser(path).get_transitions("top.a", 150, 205)
    assert [(r["time_ps"], r["value"]["bin"]) for r in result["transitions"]] == [
        (i, str(i % 2)) for i in range(150, 206)]
    assert result["predecessor"]["time_ps"] == 149


def test_real_type_metadata_does_not_allow_digital_value_queries(tmp_path):
    path = write_fst(tmp_path / "real.fst", [(0, "a", "1")], declarations=[
        ("a", 1, "wire", "input", None), ("voltage", 64, "real", "implicit", None)])
    parser = FSTParser(path)
    row = parser.search_signals("voltage")["results"][0]
    assert row["var_type"] == "real" and row["supported"] is False
    with pytest.raises(FstError, match="signal_type_unsupported"):
        parser.get_value_at_time("top.voltage", 0)


def test_permission_failure_is_distinct_from_corrupt_data(tmp_path):
    path = write_fst(tmp_path / "private.fst", [(0, "a", "1")])
    path.chmod(0)
    try:
        if os.geteuid() == 0:
            pytest.skip("root can read mode-000 files")
        with pytest.raises(PermissionError):
            FstProcess(path)
        with pytest.raises(FstError, match="fst_permission_denied"):
            FSTParser(path).get_summary()
    finally:
        path.chmod(0o600)


def test_pipe_backpressure_cannot_hide_the_deadline(tmp_path, monkeypatch):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "1")])
    # Transport-only fault injection: this child responds ready but never
    # drains stdin. A blocking write would bypass every cancellation check.
    script = 'import time; print(\'{"kind":"ready"}\', flush=True); time.sleep(60)'
    monkeypatch.setattr(FstProcess, "command", staticmethod(lambda: [sys.executable, "-c", script]))
    started = time.monotonic()
    with pytest.raises(FstError, match="fst_timeout"):
        with FstProcess(path, timeout_sec=0.15) as reader:
            pid = reader.process.pid
            for _ in range(32):
                reader.send({"padding": "x" * 60000})
    assert time.monotonic() - started < 1
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
