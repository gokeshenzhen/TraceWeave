"""Request-owned native reuse, including real IPC, cancellation and cleanup."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
import threading
import time

import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
from fst_fixture import write_fst
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.fst_parser import FSTParser
from src.fst_runtime import FstProcess, FstError, request_budget, metadata_owner
from src.scope_metadata import ScopeIdentityChanged


@pytest.fixture
def sample(tmp_path):
    return write_fst(tmp_path / 'reuse.fst', [(0, 'a', '0'), (10, 'a', '1'), (20, 'a', '0')],
        declarations=[('a', 1, 'wire', 'input', None),
                      *[(f'alias{i}', 1, 'wire', 'output', 'a') for i in range(8)]])


@pytest.fixture
def workers(monkeypatch):
    records = []
    original = FstProcess.__init__
    def track(self, *args, **kwargs):
        original(self, *args, **kwargs)
        records.append((self, self.process, Path(self.directory.name)))
    monkeypatch.setattr(FstProcess, '__init__', track)
    yield records
    assert all(child.poll() is not None and not directory.exists() for _, child, directory in records)


def test_one_request_reuses_pages_search_and_declarations(sample, workers):
    p = FSTParser(sample)
    with request_budget() as deadline:
        paths, cursor = [], None
        while True:
            with request_budget():  # nested consumers must not close the outer lease
                page = p.enumerate_scope_page('top', max_items=1, cursor=cursor)
                paths.extend(r['path'] for r in page['results'])
            cursor = page['cursor']
            if page['complete']:
                break
        assert p.search_signals('alias')['total_matched'] == 8
        assert p.get_signal_declaration('top.a')['storage_key'] == p.get_signal_declaration('top.alias7')['storage_key']
        assert paths == ['top.a', *[f'top.alias{i}' for i in range(8)]]
        assert len(workers) == 1 and workers[0][0].deadline == deadline
        assert workers[0][1].poll() is None
    with request_budget():
        assert p.get_summary()['total_signals'] == 9
    assert len(workers) == 2  # nothing survives across requests


@pytest.mark.parametrize('kind', ['stream', 'batch'])
def test_metadata_worker_is_consumed_and_batch_directory_transferred(sample, workers, kind):
    p = FSTParser(sample)
    with request_budget():
        p.get_signal_width('top.a')
        directory = workers[0][2]
        if kind == 'stream':
            result = p.get_transitions('top.a', 11, 20)
            assert result['predecessor']['time_ps'] == 10
            assert [r['time_ps'] for r in result['transitions']] == [20]
        else:
            with p._event_batch(['top.a', 'top.alias0'], 11, 20) as batch:
                assert directory == Path(batch.directory.name) and directory.exists()
                assert workers[0][1].poll() is not None
                with p._event_pages('top.alias0', 11, 20) as reader:
                    page = reader.read_page()
                    assert page.predecessor.time_ps == 10
                    assert [r.time_ps for r in page.events] == [20]
        assert len(workers) == 1 and not directory.exists()
        assert not p._native_sessions
        p.get_summary()
        assert len(workers) == 2


@pytest.mark.parametrize('stop', ['cancel', 'timeout', 'replace', 'close', 'error'])
def test_idle_worker_cannot_survive_stop_or_invalidation(sample, workers, stop):
    p = FSTParser(sample)
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    try:
        with request_budget():
            page = p.enumerate_scope_page(max_items=1)
            if stop == 'cancel':
                cancel.set()
                with pytest.raises(OperationCancelled):
                    p.get_summary()
            elif stop == 'timeout':
                workers[0][0].deadline = 0
                with pytest.raises(FstError, match='fst_timeout'):
                    p.get_summary()
            elif stop == 'replace':
                sample.write_bytes(sample.read_bytes() + b'\0')
                with pytest.raises(ScopeIdentityChanged):
                    p.get_summary()
            elif stop == 'close':
                p.close()
                assert workers[0][1].poll() is not None
                with pytest.raises(ScopeIdentityChanged):
                    p.enumerate_scope_page(cursor=page['cursor'])
                p.get_summary()
            else:
                with pytest.raises(KeyError):
                    p.get_signal_width('top.missing')
                assert workers[0][1].poll() is not None
                p.get_summary()  # error poisons the old pipe, not future queries
    finally:
        pop_cancel_event(token)


def test_file_switches_release_admission_before_opening_another(tmp_path, sample, workers, monkeypatch):
    from src import fst_runtime
    # Four concurrent requests, each retaining one worker, must be able to
    # switch files. A per-request dictionary of idle readers would deadlock.
    monkeypatch.setattr(fst_runtime, '_slots', threading.BoundedSemaphore(4))
    paths = [write_fst(tmp_path / f'other{i}.fst', [(0, 'a', '1')]) for i in range(4)]
    barrier = threading.Barrier(4)
    def query(i):
        with request_budget():
            p = FSTParser(sample)
            p.get_summary()
            barrier.wait(timeout=5)
            for path in paths:
                q = FSTParser(path)
                with q._event_batch(['top.a'], 0, 20):
                    assert q.get_signal_width('top.a') == 1
            p.get_summary()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(query, range(4)))
    assert len(workers) == 24


def test_copied_context_never_shares_native_pipe_across_threads(sample, workers):
    p = FSTParser(sample)
    with request_budget():
        p.get_summary()
        original = metadata_owner().worker
        ctx = copy_context()
        def query():
            with request_budget():
                p.get_summary()
                assert metadata_owner().worker is not original
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ctx.run, query).result(timeout=5)
        assert original.process is not None
        p.get_summary()
        assert len(workers) == 2


def test_close_from_another_thread_reaps_idle_worker(sample, workers):
    p = FSTParser(sample)
    with request_budget():
        p.get_summary()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(p.close).result(timeout=5)
        assert workers[0][1].poll() is not None
        with pytest.raises(ScopeIdentityChanged):
            p.get_summary()


def test_parser_lock_wait_obeys_request_deadline(sample, workers, monkeypatch):
    from src import fst_runtime
    p = FSTParser(sample)
    monkeypatch.setattr(fst_runtime, 'REQUEST_TIMEOUT_SEC', 0.1)
    def wait():
        with pytest.raises(FstError, match='fst_timeout'):
            with request_budget():
                p.get_summary()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p._lock:
            pool.submit(wait).result(timeout=2)
    assert not workers


def test_cancel_one_request_preserves_other_request_on_same_parser(sample, workers):
    p = FSTParser(sample)
    barrier = threading.Barrier(2)
    cancelled = threading.Event()
    def first():
        event = threading.Event()
        token = push_cancel_event(event)
        try:
            with request_budget():
                p.get_summary()
                barrier.wait(timeout=5)
                event.set()
                with pytest.raises(OperationCancelled):
                    p.get_summary()
            cancelled.set()
        finally:
            pop_cancel_event(token)
    def second():
        with request_budget():
            p.get_summary()
            original = metadata_owner().worker
            barrier.wait(timeout=5)
            assert cancelled.wait(5)
            assert p.get_signal_width('top.a') == 1
            assert original.process.poll() is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        a.result(timeout=10); b.result(timeout=10)
    assert len(workers) == 2


def test_failed_batch_reaps_reused_worker_without_transferring_spool(tmp_path, workers):
    path = write_fst(tmp_path / 'spool.fst', [(t, 'a', str(t % 2)) for t in range(30)])
    p = FSTParser(path)
    with request_budget():
        p.get_summary()
        with pytest.raises(FstError, match='fst_spool_byte_limit'):
            with p._event_batch(['top.a'], 0, 30, spool_bytes=128):
                raise AssertionError('failed spool exposed a reader')
        assert workers[0][1].poll() is not None and not workers[0][2].exists()
        assert p._active_batch is None
        assert p.get_summary()['total_signals'] == 1


def test_public_discovery_pages_share_one_worker(sample, workers):
    from test_fst_integration import call
    result = call('suggest_handshakes', wave_path=str(sample))
    assert 'error' not in result, result
    assert len(workers) == 1
    assert workers[0][1].poll() is not None


@pytest.mark.parametrize('kind', ['stream', 'batch'])
def test_cancel_during_native_work_reaps_reused_worker(tmp_path, workers, kind):
    path = write_fst(tmp_path / 'dense.fst', [(t, 'a', str(t % 2)) for t in range(250000)], end=250000)
    p = FSTParser(path)
    event = threading.Event()
    token = push_cancel_event(event)
    timer = None
    try:
        with pytest.raises(OperationCancelled):
            with request_budget():
                p.get_summary()
                timer = threading.Timer(0.02, event.set)
                timer.start()
                if kind == 'stream':
                    p.get_transitions('top.a', 249999, 250000)
                else:
                    with p._event_batch(['top.a'], 249999, 250000):
                        raise AssertionError('cancelled batch became readable')
        assert len(workers) == 1
        assert not p._native_sessions and p._active_batch is None
    finally:
        if timer is not None:
            timer.join()
        pop_cancel_event(token)


def test_cancel_at_directory_transfer_cleans_parent_owned_spool(sample, workers, monkeypatch):
    original = FstProcess.check
    def check(self):
        if self.process is not None and self.directory is None:
            raise OperationCancelled('cancel at spool ownership transfer')
        original(self)
    monkeypatch.setattr(FstProcess, 'check', check)
    p = FSTParser(sample)
    with request_budget():
        p.get_summary()
        with pytest.raises(OperationCancelled):
            with p._event_batch(['top.a'], 0, 20):
                raise AssertionError('cancelled batch was exposed')
        assert len(workers) == 1 and not workers[0][2].exists()
        assert workers[0][1].poll() is not None


def test_metadata_scope_preserves_frozen_exception_identity():
    from dataclasses import dataclass
    from src.fst_runtime import metadata_request
    @dataclass(frozen=True)
    class FrozenError(Exception):
        code: str
    error = FrozenError('original')
    with pytest.raises(FrozenError) as caught:
        with metadata_request():
            raise error
    assert caught.value is error
