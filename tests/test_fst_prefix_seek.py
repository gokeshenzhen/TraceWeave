"""Filtered native reads require genuine prefix evidence, never block snapshots."""
from pathlib import Path
import sys
import threading
import time

import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
from fst_fixture import write_fst
from src import fst_parser
from src.fst_runtime import FstProcess, FstError
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event


class FullPrefix(FstProcess):
    @staticmethod
    def command():
        source = Path(fst_parser.__file__).with_name('fst_worker.py')
        code = f"import runpy; n=runpy.run_path({str(source)!r}); g=n['main'].__globals__\n"
        code += "def full(*args):\n g['METRICS'].update(native_prefix_probe_iterations=0,native_prefix_probe_callbacks=0,native_prefix_blocks_skipped=0)\n return 0\n"
        code += "g['window_scan_start']=full\ntry: n['main']()\nexcept EOFError: pass\nexcept ValueError as e: n['emit']({'kind':'error','reason':str(e)})\n"
        return [sys.executable, '-c', code]


def fixture(tmp_path, scale=-12, activity=()):
    rows = [(i, 'a', str(i % 2)) for i in range(501)]
    rows += [(0, 'constant', '1'), (0, 'slow', '0'), (20, 'slow', '1'), (400, 'slow', '0')]
    rows += [(250, 'a', 'x'), (250, 'a', 'z'), (250, 'a', '1')]
    return write_fst(tmp_path / 'blocks.fst', rows, scale=scale, end=500,
        flush=[99, 199, 299, 399], activity=activity, declarations=[
            ('a', 1, 'wire', 'input', None), ('alias', 1, 'wire', 'output', 'a'),
            ('constant', 1, 'wire', 'input', None), ('slow', 1, 'wire', 'input', None)])


@pytest.mark.parametrize('scale,start,end', [(-12, 250, 310), (-13, 25, 31), (-15, 1, 1)])
def test_filtered_windows_match_original_scan_at_physical_boundaries(tmp_path, monkeypatch, scale, start, end):
    path = fixture(tmp_path, scale)
    p = fst_parser.FSTParser(path)
    actual = p.get_transitions('top.a', start, end)
    point = p.get_value_at_time('top.alias', start)
    monkeypatch.setattr(fst_parser, 'FstProcess', FullPrefix)
    original = fst_parser.FSTParser(path)
    assert actual == original.get_transitions('top.a', start, end)
    assert point == original.get_value_at_time('top.alias', start)
    if scale in (-12, -13):
        factor = 10 ** (scale + 15)
        assert actual['predecessor']['time_fs'] == 249 * factor
        assert [(r['time_fs'], r['value']['bin']) for r in actual['transitions'][:4]] == [
            (250 * factor, v) for v in ('0', 'x', 'z', '1')]
        assert actual['initial_state'] is None


def test_batch_filters_only_handles_with_a_real_prefix(tmp_path):
    path = fixture(tmp_path)
    p = fst_parser.FSTParser(path)
    for names, fallback in [(['top.a', 'top.alias'], 0),
                            (['top.a', 'top.constant'], 1), (['top.a', 'top.slow'], 1)]:
        with p._event_batch(names, 250, 310) as batch:
            assert batch.metrics['native_prefix_blocks_skipped'] > 0
            assert batch.metrics['native_prefix_filtered_handles'] == 1
            assert batch.metrics['native_prefix_fallback_handles'] == fallback
            assert batch.metrics['native_prefix_probe_iterations'] == 1
            assert batch.metrics['native_iterations'] == 2 + bool(fallback)
            with p._event_pages(names[-1], 250, 310) as reader:
                page = reader.read_page()
                if names[-1] == 'top.constant':
                    assert page.predecessor is None and not page.events
                    assert page.initial_state.time_fs == 0 and page.initial_state.value == '1'
                elif names[-1] == 'top.slow':
                    assert page.predecessor.time_fs == 20000 and not page.events
                else:
                    assert page.predecessor.time_fs == 249000
                    assert [e.value for e in page.events[:4]] == ['0', 'x', 'z', '1']


@pytest.mark.parametrize('scale', [-12, -13, -15])
def test_sparse_batch_preserves_facts_without_replaying_dense_clock(tmp_path, monkeypatch, scale):
    rows = [(i, 'clk', str(i % 2)) for i in range(40001)]
    rows += [(0, 'constant', '1'), (0, 'slow', '0'), (20, 'slow', '1'), (400, 'slow', '0')]
    path = write_fst(tmp_path / 'mixed.fst', rows, end=40000, scale=scale,
        flush=list(range(3999, 40000, 4000)), declarations=[
            ('clk', 1, 'wire', 'input', None), ('alias', 1, 'wire', 'output', 'clk'),
            ('constant', 1, 'wire', 'input', None), ('slow', 1, 'wire', 'input', None)])
    names = ['top.clk', 'top.alias', 'top.constant', 'top.slow']
    scale_fs = 10 ** (scale + 15)
    start_ps = 35000 * scale_fs // 1000
    end_ps = (35010 * scale_fs + 999) // 1000
    first_tick = start_ps * 1000 // scale_fs
    last_tick = end_ps * 1000 // scale_fs

    def read():
        parser = fst_parser.FSTParser(path)
        with parser._event_batch(names, start_ps, end_ps) as batch:
            pages = {}
            for name in names:
                with parser._event_pages(name, start_ps, end_ps) as reader:
                    pages[name] = reader.read_page()
            return pages, batch.metrics

    actual, metrics = read()
    # An independent event oracle also verifies true prefix times and aliases.
    clock = actual['top.clk']
    assert clock.predecessor.time_fs == (first_tick - 1) * scale_fs
    assert [(e.time_fs, e.value) for e in clock.events] == [
        (i * scale_fs, str(i % 2)) for i in range(first_tick, last_tick + 1)]
    assert actual['top.alias'] == clock
    assert actual['top.constant'].initial_state.time_fs == 0
    assert actual['top.constant'].predecessor is None
    assert not actual['top.constant'].events
    assert actual['top.slow'].predecessor.time_fs == 400 * scale_fs
    assert not actual['top.slow'].events
    assert metrics['native_prefix_filtered_handles'] == 1
    assert metrics['native_prefix_fallback_handles'] == 2

    monkeypatch.setattr(fst_parser, 'FstProcess', FullPrefix)
    original, original_metrics = read()
    assert actual == original
    assert metrics['native_callbacks'] < original_metrics['native_callbacks'] // 3


@pytest.mark.parametrize('window', [(0, 5), (99, 101), (100, 100), (199, 201), (200, 200),
                                     (249, 250), (250, 250), (301, 305), (499, 510)])
def test_boundary_windows_and_sparse_signals_preserve_all_fields(tmp_path, monkeypatch, window):
    path = fixture(tmp_path)
    p = fst_parser.FSTParser(path)
    actual = {n: p.get_transitions('top.' + n, *window) for n in ('a', 'constant', 'slow')}
    monkeypatch.setattr(fst_parser, 'FstProcess', FullPrefix)
    original = fst_parser.FSTParser(path)
    for name, result in actual.items():
        assert result == original.get_transitions('top.' + name, *window)


def test_dump_gaps_keep_full_history_and_never_use_a_resume_snapshot(tmp_path, monkeypatch):
    path = fixture(tmp_path, activity=[(210, 0), (240, 1), (280, 0), (290, 1)])
    p = fst_parser.FSTParser(path)
    actual = p.get_transitions('top.slow', 250, 310)
    with p._event_batch(['top.a', 'top.slow'], 250, 310) as batch:
        assert batch.metrics['native_prefix_probe_iterations'] == 0
        assert batch.metrics['native_prefix_blocks_skipped'] == 0
        assert batch.metrics['native_prefix_filtered_handles'] == 0
        assert batch.metrics['native_prefix_fallback_handles'] == 2
        with p._event_pages('top.slow', 250, 310) as reader:
            assert reader.read_page().recording_gaps
    monkeypatch.setattr(fst_parser, 'FstProcess', FullPrefix)
    assert actual == fst_parser.FSTParser(path).get_transitions('top.slow', 250, 310)
    assert actual['fst_reading']['coverage_status'] == 'partial'


def test_window_snapshot_does_not_become_true_predecessor(tmp_path):
    path = fixture(tmp_path)
    p = fst_parser.FSTParser(path)
    result = p.get_transitions('top.slow', 250, 310)
    assert result['predecessor']['time_ps'] == 20
    assert result['transitions'] == []
    result = p.get_transitions('top.constant', 250, 310)
    assert result['predecessor'] is None
    assert result['initial_state']['time_ps'] == 0


@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
def test_stop_during_filtered_prefix_scan_reaps_worker(tmp_path, stop):
    # Stream generation: a million Python tuples plus the writer helper's
    # per-time dictionary would inflate the entire pytest process's RSS.
    from pylibfst import lib, ffi
    path = tmp_path / 'dense.fst'
    writer = lib.fstWriterCreate(str(path).encode(), 1)
    assert writer != ffi.NULL
    try:
        lib.fstWriterSetTimescale(writer, -12)
        lib.fstWriterSetScope(writer, lib.FST_ST_VCD_MODULE, b'top', ffi.NULL)
        handle = lib.fstWriterCreateVar(writer, lib.FST_VT_VCD_WIRE, lib.FST_VD_INPUT, 1, b'a', 0)
        lib.fstWriterSetUpscope(writer)
        for i in range(1000000):
            lib.fstWriterEmitTimeChange(writer, i)
            lib.fstWriterEmitValueChange(writer, handle, b'1' if i % 2 else b'0')
            if i in (333333, 666666):
                lib.fstWriterFlushContext(writer)
        lib.fstWriterEmitTimeChange(writer, 1000000)
    finally:
        lib.fstWriterClose(writer)
    event = threading.Event()
    token = push_cancel_event(event)
    timer = None
    try:
        with pytest.raises(OperationCancelled if stop == 'cancel' else FstError):
            with FstProcess(path) as worker:
                worker.ask({'op': 'metadata'})
                child, directory = worker.process, Path(worker.directory.name)
                if stop == 'cancel':
                    timer = threading.Timer(0.02, event.set); timer.start()
                else:
                    worker.deadline = time.monotonic() + 0.02
                worker.ask(dict(op='stream', path='top.a', start_fs=999990000,
                                end_fs=999999000, max_events=1024, max_bytes=262144))
        assert child.poll() is not None and not directory.exists()
    finally:
        if timer is not None:
            timer.join()
        pop_cancel_event(token)


def test_femtosecond_prefix_and_period_survive_several_blocks(tmp_path, monkeypatch):
    from src.verify_condition import period
    path = write_fst(tmp_path / 'fs.fst', [(i, 'a', str(i % 2)) for i in range(10001)],
                     scale=-15, end=10000, flush=[1999, 3999, 5999, 7999])
    p = fst_parser.FSTParser(path)
    actual = p.get_transitions('top.a', 5, 7)
    assert actual['predecessor']['time_fs'] == 4999
    assert actual['predecessor']['time_ps'] == 5
    assert [r['time_fs'] for r in actual['transitions']] == list(range(5000, 7001))
    measured = period(get_parser=lambda _: p, wave_path=str(path), signal='top.a', start_ps=5, end_ps=7)
    assert measured['period_fs'] == 2
    monkeypatch.setattr(fst_parser, 'FstProcess', FullPrefix)
    q = fst_parser.FSTParser(path)
    assert actual == q.get_transitions('top.a', 5, 7)
    assert measured == period(get_parser=lambda _: q, wave_path=str(path), signal='top.a', start_ps=5, end_ps=7)
