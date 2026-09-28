"""Real FST pages: independent observations, resource bounds and ownership."""
import os
from pathlib import Path
import threading
import time

import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
from fst_fixture import write_fst
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.event_pages import GroupCursor, page_records
from src.fst_event_pages import FstBatch
from src.fst_parser import FSTParser
from src.fst_runtime import FstError, FstProcess, _slots
from src.scope_metadata import ScopeIdentityChanged
from src.waveform_batch import event_readers, make_batch_reader


def wave(tmp_path, *, name='a.fst', start=0, activity=()):
    return write_fst(tmp_path / name, [(start, 'a', '0'), (10, 'a', '1'),
        (10, 'a', 'x'), (10, 'a', '0'), (20, 'a', 'z')], start=start,
        activity=activity, declarations=[('a', 1, 'wire', 'input', None),
            *[(f'alias{i}', 1, 'wire', 'output', 'a') for i in range(9)]])


@pytest.mark.parametrize('size', [1, 2, 3, 1024])
def test_batch_aliases_use_one_scan_and_independent_positions(tmp_path, size):
    p = FSTParser(wave(tmp_path))
    requests = [(p, f'top.alias{i}') for i in range(9)]
    with event_readers(requests, 0, 30, max_events=size, max_bytes=128) as readers:
        batch = p._active_batch
        private = Path(batch.directory.name)
        assert batch.metrics['storage_streams'] == batch.metrics['native_iterations'] == 1
        assert batch.metrics['native_callbacks'] == 5
        # Native admission is completely free while nine cursors are alive.
        acquired = 0
        try:
            for _ in range(4):
                assert _slots.acquire(blocking=False)
                acquired += 1
        finally:
            for _ in range(acquired):
                _slots.release()
        for reader in reversed(readers):
            rows, initial = [], []
            while True:
                page = reader.read_page()
                assert len(page_records(page)) <= size and page.output_bytes <= 128
                rows.extend((e.time_fs, e.value) for e in page.events)
                if page.initial_state:
                    initial.append(page.initial_state)
                assert not page.truncated
                if page.complete:
                    break
            assert [(e.time_fs, e.value) for e in initial] == [(0, '0')]
            assert rows == [(10000, '1'), (10000, 'x'), (10000, '0'), (20000, 'z')]
    assert not private.exists()
    with pytest.raises(ScopeIdentityChanged):
        readers[0].read_page()


def test_many_files_do_not_hold_native_slots_during_group_open(tmp_path):
    parsers = [FSTParser(wave(tmp_path, name=f'{i}.fst')) for i in range(6)]
    with event_readers([(p, 'top.a') for p in parsers], 0, 30, max_events=1) as readers:
        for reader in readers:
            cursor = GroupCursor(reader)
            assert cursor.take_group(0) == '0'
            assert cursor.take_group(10000) == '0'
            assert cursor.take_group(20000) == 'z'


@pytest.mark.parametrize('start,initial,previous', [(0, 5000, None), (6, 5000, None), (11, None, 10000)])
def test_initial_range_and_strict_predecessor(tmp_path, start, initial, previous):
    p = FSTParser(wave(tmp_path, start=5))
    with event_readers([(p, 'top.a')], start, 19, max_events=1) as readers:
        cursor = GroupCursor(readers[0])
        assert (cursor.initial_state.time_fs if cursor.initial_state else None) == initial
        assert (cursor.predecessor.time_fs if cursor.predecessor else None) == previous
        if start == 0:
            assert cursor.take_group(0) is None
            assert cursor.take_group(5000) == '0'
            assert cursor.take_group(10000) == '0'
        elif start == 6:
            assert cursor.anchor.value == '0' and cursor.anchor.kind == 'initial_state'


def test_dump_gaps_invalidate_state_until_new_observation(tmp_path):
    p = FSTParser(wave(tmp_path, activity=[(12, 0), (16, 1)]))
    with event_readers([(p, 'top.a')], 11, 25, max_events=1) as readers:
        cursor = GroupCursor(readers[0])
        assert cursor.anchor.value == '0'
        assert cursor.take_group(12000) is None
        assert cursor.take_group(16000) is None
        assert cursor.take_group(20000) == 'z'
        assert [(g.start_fs, g.end_fs, g.reason) for g in cursor.recording_gaps] == [
            (12000, 16000, 'dump_inactive'), (16000, 20000, 'unrecorded_after_dump_resume')]
    with event_readers([(p, 'top.a')], 14, 18, max_events=1) as readers:
        cursor = GroupCursor(readers[0])
        assert cursor.take_group(14000) is None
        assert cursor.take_group(16000) is None
        assert cursor.recording_gaps[-1].end_inclusive


@pytest.mark.parametrize('change', ['close', 'file', 'cancel', 'timeout'])
def test_page_lifecycle_always_discards_spool(tmp_path, change):
    path = wave(tmp_path)
    p = FSTParser(path)
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    try:
        error = OperationCancelled if change == 'cancel' else FstError if change == 'timeout' else ScopeIdentityChanged
        with pytest.raises(error):
            with event_readers([(p, 'top.a')], 0, 30) as readers:
                private = Path(p._active_batch.directory.name)
                if change == 'close':
                    p.close()
                elif change == 'file':
                    path.write_bytes(path.read_bytes() + b'\0')
                elif change == 'timeout':
                    p._active_batch.deadline = 0
                else:
                    cancel.set()
                readers[0].read_page()
        assert not private.exists() and p._active_batch is None
    finally:
        pop_cancel_event(token)


def test_spool_limit_rejects_and_reaps_worker(tmp_path, monkeypatch):
    path = write_fst(tmp_path / 'dense.fst', [(t, 'a', str(t % 2)) for t in range(200)], end=200)
    workers = []
    original = FstProcess.__init__
    def track(self, *args, **kwargs):
        original(self, *args, **kwargs)
        workers.append((self.process.pid, Path(self.directory.name)))
    monkeypatch.setattr(FstProcess, '__init__', track)
    with pytest.raises(FstError, match='spool_byte_limit'):
        FstBatch(FSTParser(path), ['top.a'], 0, 200, spool_bytes=128)
    assert len(workers) == 1
    for pid, private in workers:
        assert not private.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_batch_factory_keeps_initial_and_missing_declarations_separate(tmp_path):
    result = make_batch_reader(str(wave(tmp_path))).values_in_window(['top.a', 'top.alias0', 'top.no'], 0, 25)
    assert result['missing'] == ['top.no'] and not result['truncated']
    assert result['reading']['storage_streams'] == 1
    assert result['initial_states']['top.a']['kind'] == 'initial_state'
    assert [r['time_fs'] for r in result['transitions']] == [10000] * 6 + [20000] * 2


def test_sub_ps_raw_times_and_wide_page_stop(tmp_path):
    p = FSTParser(write_fst(tmp_path / 'sub.fst', [(0, 'a', '0'), (1, 'a', '1'), (2, 'a', '0')], scale=-13))
    with event_readers([(p, 'top.a')], 0, 1, max_events=1) as readers:
        cursor = GroupCursor(readers[0])
        assert cursor.take_group(0) == '0'
        assert cursor.take_group(100) == '1' and cursor.take_group(200) == '0'
    p = FSTParser(write_fst(tmp_path / 'wide.fst', [(0, 'a', 'x'*512)],
        declarations=[('a', 512, 'wire', 'input', None)]))
    with event_readers([(p, 'top.a')], 0, 10, max_bytes=128) as readers:
        page = readers[0].read_page()
        assert page.truncated and not page.complete and not page.events


@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
def test_stop_in_native_batch_scan_reaps_before_return(tmp_path, monkeypatch, stop):
    path = write_fst(tmp_path / 'dense.fst', [(i, 'a', str(i % 2)) for i in range(200000)], end=200000)
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    workers, timers = [], []
    original = FstProcess.ask
    def ask(self, request):
        if request['op'] == 'batch':
            workers.append((self.process.pid, Path(self.directory.name)))
            if stop == 'cancel':
                timer = threading.Timer(0.02, cancel.set)
                timers.append(timer)
                timer.start()
            else:
                self.deadline = time.monotonic() + 0.02
        return original(self, request)
    monkeypatch.setattr(FstProcess, 'ask', ask)
    try:
        with pytest.raises(OperationCancelled if stop == 'cancel' else FstError):
            with event_readers([(FSTParser(path), 'top.a')], 199990, 200000):
                pytest.fail('native scan ignored stop')
        for pid, private in workers:
            assert not private.exists()
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for timer in timers:
            timer.join()
        pop_cancel_event(token)
