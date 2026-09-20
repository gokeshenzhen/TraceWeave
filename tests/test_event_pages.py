"""Independent time/value tables, including native fixtures without simulation."""
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.divergence_compare import compare_signals
from src.event_pages import Event, EventPage, GroupCursor, IncompleteGroup
from src.fsdb_parser import FSDBParser
from src.vcd_parser import VCDParser
from src.waveform_batch import event_readers
from src.waveform_selection import SelectionParser


def waveform(tmp_path, rows, *, width=1, scale='1ps', end=30, name='a.vcd'):
    path = tmp_path / name
    path.write_text(f'$timescale {scale} $end\n$scope module top $end\n'
        f'$var wire {width} ! a $end\n$var wire {width} @ b $end\n'
        '$upscope $end\n$enddefinitions $end\n' +
        ''.join(f'#{t}\nb{a} !\nb{b} @\n' for t,a,b in rows) + f'#{end}\n')
    return path


class SmallPages(VCDParser):
    def __init__(self, path, size):
        super().__init__(str(path))
        self.size = size

    @contextmanager
    def _event_pages(self, path, start=0, end=-1, **kw):
        with super()._event_pages(path, start, end, max_events=self.size, max_bytes=128) as reader:
            yield reader


def compare(parser, **kw):
    return compare_signals(get_parser=lambda _:parser, wave_path_a=parser.file_path,
        signal_a='top.a', wave_path_b=parser.file_path, signal_b='top.b', **kw)


@pytest.mark.parametrize('size', [1, 2, 3, 7, 1024])
def test_same_tick_group_is_folded_across_every_page_boundary(tmp_path, size):
    wave = waveform(tmp_path, [(0,'0','0'), (10,'1','0'), (10,'x','z'),
                              (10,'0','0'), (20,'1','0')])
    result = compare(SmallPages(wave, size))
    assert result['first_divergence_time_ps'] == 20
    assert result['first_divergence_time_fs'] == 20000
    assert result['earliest_difference_proven']
    assert result['transitions_compared'] == 10
    assert not any(g['reason'] == 'value_unknown' for g in result['coverage_gaps'])


@pytest.mark.parametrize('state', ['x', 'z'])
def test_unknown_prefix_cannot_prove_later_positive_is_earliest(tmp_path, state):
    result = compare(SmallPages(waveform(tmp_path, [(0,state,state),(10,'1','0')]),1))
    assert result['comparison_status'] == 'different'
    assert result['first_divergence_time_ps'] == 10
    assert not result['earliest_difference_proven']


@pytest.mark.parametrize('start,end,expected', [(1,9,1),(10,19,None),(31,40,None)])
def test_predecessor_empty_window_and_strict_bounds(tmp_path,start,end,expected):
    p = SmallPages(waveform(tmp_path, [(0,'1','0'),(10,'0','0'),(20,'0','1')]),1)
    r = compare(p,start_ps=start,end_ps=end)
    assert r['first_divergence_time_ps'] == expected
    assert r['comparison_status'] == ('different' if expected else 'equal' if end<30 else 'inconclusive')
    with p._event_pages('top.a',start,end) as reader:
        pages=[]
        while True:
            page=reader.read_page(); pages.append(page)
            if page.complete or page.truncated: break
        assert all(start*1000 <= e.time_fs <= end*1000 for pg in pages for e in pg.events)
        if pages[0].predecessor:
            assert pages[0].predecessor.time_fs < start*1000


def test_raw_subps_times_not_collapsed_and_predecessor_is_physical(tmp_path):
    p = SmallPages(waveform(tmp_path, [(0,'0','0'),(1,'1','0'),(2,'0','0'),
                                    (9,'1','0'),(10,'0','0')], scale='100fs'),1)
    r=compare(p)
    assert (r['first_divergence_time_fs'], r['first_divergence_time_ps']) == (100,1)
    assert r['earliest_difference_proven']
    r=compare(p,start_ps=1,end_ps=2)
    assert r['comparison_status']=='equal'  # 0.9ps pulse is outside this window.
    with p._event_pages('top.a',1,2) as reader:
        page=reader.read_page()
        assert page.predecessor.time_fs==900 and page.predecessor.value=='b1'


def test_lost_or_unfinished_tail_cannot_be_a_difference():
    class Reader:
        def read_page(self):
            return EventPage((Event(0,0,'0'),Event(10,1,'1')),next_time=10,truncated=True)
    cursor=GroupCursor(Reader())
    assert cursor.take_group(0)=='0'
    with pytest.raises(IncompleteGroup,match='truncated'):
        cursor.take_group(10)


def test_early_difference_does_not_materialize_suffix(tmp_path,monkeypatch):
    p=VCDParser(str(waveform(tmp_path,[(t,str(t%2),str(1-t%2)) for t in range(20000)],end=20000)))
    monkeypatch.setattr(p,'get_transitions',lambda *a:pytest.fail('whole-window read'))
    r=compare(p)
    assert r['first_divergence_time_ps']==0 and r['earliest_difference_proven']
    assert r['reading']['events_read']==2048
    assert r['reading']['pages_read']==2


def test_sparse_large_time_window_uses_event_pages(tmp_path):
    p=VCDParser(str(waveform(tmp_path,[(0,'0','0'),(10**12,'1','1')],end=10**15)))
    r=compare(p)
    assert r['comparison_status']=='equal' and r['reading']['pages_read']==2
    assert r['reading']['events_read']==4


def test_unequal_durations_no_extension_width_mismatch_and_missing(tmp_path):
    a=VCDParser(str(waveform(tmp_path,[(0,'0','0')],end=10)))
    b=VCDParser(str(waveform(tmp_path,[(0,'0','0')],end=20,name='b.vcd')))
    def run(signal='top.b'):
        return compare_signals(get_parser={a.file_path:a,b.file_path:b}.__getitem__,
            wave_path_a=a.file_path,signal_a='top.a',wave_path_b=b.file_path,signal_b=signal)
    r=run()
    assert r['comparison_status']=='inconclusive' and r['end_ps']==10
    assert r['excluded_intervals']==[dict(side='a',start_ps=10,end_ps=20,reason='outside_recorded_range')]
    r=run('top.missing')
    assert r['missing_b'] and not r['diverged']
    b=VCDParser(str(waveform(tmp_path,[(0,'00','00')],width=2,end=20,name='b.vcd')))
    assert any(g['reason']=='width_mismatch' for g in run()['coverage_gaps'])


def test_no_dump_values_are_unknown_not_equal(tmp_path):
    r=compare(VCDParser(str(waveform(tmp_path,[]))))
    assert r['comparison_status']=='inconclusive'
    assert {g['reason'] for g in r['coverage_gaps']}=={'value_unavailable'}


def test_oversized_predecessor_retains_truncation_gap(tmp_path):
    p=SmallPages(waveform(tmp_path, [(0,'0'*256,'1'*256)],width=256),1)
    r=compare(p,start_ps=1,end_ps=10)
    assert r['comparison_status']=='inconclusive' and not r['earliest_difference_proven']
    assert {g['reason'] for g in r['coverage_gaps']}=={'transition_data_truncated'}


def test_page_identity_change_rejects_old_prefix(tmp_path):
    wave=waveform(tmp_path,[(0,'0','0'),(10,'1','0')])
    p=SmallPages(wave,1)
    with p._event_pages('top.a') as reader:
        reader.read_page()
        wave.write_text(wave.read_text()+'\n')
        with pytest.raises(RuntimeError,match='changed'):
            reader.read_page()


@pytest.mark.parametrize('filename,signal,times,values',[
    ('scale_100fs.fsdb','scale_100fs_tb.addr[31:0]',[0,100000000,100100000,100100500],
     [0,0xaaaa0000,0xbbbb0000,0xcccc0000]),
    ('scale_1ns.fsdb','scale_1ns_tb.addr[31:0]',[0,100000000,101000000],
     [0,0xaaaa0000,0xbbbb0000])])
def test_required_native_exact_time_pages_and_cleanup(filename,signal,times,values):
    # Required PR acceptance: no skip when the tested ABI or fixture is missing.
    p=FSDBParser(str(Path(__file__).parent/'fixtures'/filename))
    try:
        assert p._supports_event_pages()
        with event_readers([(p,signal)],0,-1,max_events=1,max_bytes=128) as readers:
            rows=[]
            while True:
                page=readers[0].read_page();rows.extend(page.events)
                assert len(page.events)<=1 and page.output_bytes<=128
                if page.complete:break
            assert [e.time_fs for e in rows]==times
            assert [int(e.value,2) for e in rows]==values
        assert not p._transition_group_active
        assert p.get_value_at_time(signal,100000)['value']['dec']==0xaaaa0000
        with pytest.raises(OperationCancelled):
            with event_readers([(p,signal)],0,-1) as readers:
                cancel=threading.Event();token=push_cancel_event(cancel)
                try:
                    cancel.set();readers[0].read_page()
                finally:pop_cancel_event(token)
        assert not p._transition_group_active
        with event_readers([(p,signal)],100001,100099,max_events=1,max_bytes=128) as readers:
            page=readers[0].read_page()
            assert page.predecessor.time_fs==100000000
            assert not page.events and page.complete
    finally:p.close()


def test_native_old_wrapper_capability_fallback_is_materialized(monkeypatch):
    p=FSDBParser(str(Path(__file__).parent/'fixtures/scale_1ns.fsdb'))
    try:
        p._open(); monkeypatch.setattr(p._lib,'_traceweave_has_event_pages_v1',False)
        r=compare_signals(get_parser=lambda _:p,wave_path_a=p.file_path,signal_a='scale_1ns_tb.addr[31:0]',
            wave_path_b=p.file_path,signal_b='scale_1ns_tb.addr[31:0]')
        assert r['comparison_status']=='equal'
        assert r['reading']['mode_a']=='legacy_materialized'
    finally:p.close()


def test_selection_event_projection_keeps_ordered_bits_and_unknowns(tmp_path):
    raw=VCDParser(str(waveform(tmp_path,[(0,'1001','1001'),(1,'1xz0','1000')],width=4)))
    p=SelectionParser(raw)
    a=p.bind({'path':'top.a','bits':[0,2]});b=p.bind({'path':'top.b','bits':[0,2]})
    r=compare_signals(get_parser=lambda _:p,wave_path_a=raw.file_path,signal_a=a,
        wave_path_b=raw.file_path,signal_b=b)
    assert r['comparison_status']=='inconclusive'
    assert r['width_a']==2 and any(g['reason']=='value_unknown' for g in r['coverage_gaps'])


def test_native_cancellation_after_read_and_group_exception_cleanup(monkeypatch):
    p=FSDBParser(str(Path(__file__).parent/'fixtures/scale_100fs.fsdb'))
    signal='scale_100fs_tb.addr[31:0]'
    event=threading.Event();token=push_cancel_event(event)
    try:
        p._open(); original=p._lib.fsdb_event_page_v1
        def cancel_after_native(*args):
            rc=original(*args);event.set();return rc
        monkeypatch.setattr(p._lib,'fsdb_event_page_v1',cancel_after_native)
        with pytest.raises(OperationCancelled):
            compare_signals(get_parser=lambda _:p,wave_path_a=p.file_path,signal_a=signal,
                wave_path_b=p.file_path,signal_b=signal)
        assert not p._transition_group_active
        event.clear();monkeypatch.setattr(p._lib,'fsdb_event_page_v1',original)
        with pytest.raises(ValueError,match='body'):
            with event_readers([(p,signal)],0,-1) as readers:
                readers[0].read_page();raise ValueError('body')
        assert not p._transition_group_active
        assert p.get_value_at_time(signal,100100)['value']['dec']==0xbbbb0000
    finally:pop_cancel_event(token);p.close()


def test_native_byte_capacity_reports_unread_tail_and_never_loops():
    p=FSDBParser(str(Path(__file__).parent/'fixtures/wide_bus.fsdb'))
    try:
        signal='tb.dat1[1023:0]'
        with event_readers([(p,signal)],0,-1,max_bytes=128) as readers:
            page=readers[0].read_page()
            assert page.truncated and not page.complete and not page.events
            assert page.next_time is not None and page.output_bytes==0
    finally:p.close()


def test_page_deadline_checked_after_native_return(monkeypatch):
    from src.divergence_budget import Budget, BudgetExceeded
    p=FSDBParser(str(Path(__file__).parent/'fixtures/scale_1ns.fsdb'))
    signal='scale_1ns_tb.addr[31:0]'
    budget=Budget({'timeout_sec':30})
    try:
        p._open();original=p._lib.fsdb_event_page_v1
        def expire(*args):
            rc=original(*args);budget.deadline=0;return rc
        monkeypatch.setattr(p._lib,'fsdb_event_page_v1',expire)
        with pytest.raises(BudgetExceeded,match='timeout_sec'):
            compare_signals(get_parser=lambda _:p,wave_path_a=p.file_path,signal_a=signal,
                wave_path_b=p.file_path,signal_b=signal,consume=budget.consume)
        assert not p._transition_group_active
    finally:p.close()


def test_out_of_order_dump_cannot_prove_an_early_prefix(tmp_path):
    p=VCDParser(str(waveform(tmp_path,[(0,'1','0'),(10,'1','0'),(5,'x','x')])))
    r=compare(p)
    assert r['comparison_status']=='inconclusive' and not r['earliest_difference_proven']


def test_changed_file_during_page_invalidates_positive(tmp_path,monkeypatch):
    wave=waveform(tmp_path,[(0,'1','0'),(10,'0','0')])
    p=VCDParser(str(wave));original=p._event_pages
    @contextmanager
    def change(*args,**kwargs):
        with original(*args,**kwargs) as reader:
            read=reader.read_page
            def page():
                result=read();wave.write_text(wave.read_text()+'\n');return result
            reader.read_page=page
            yield reader
    monkeypatch.setattr(p,'_event_pages',change)
    r=compare(p)
    assert not r['diverged'] and r['first_divergence_time_fs'] is None
    assert any(g['reason']=='waveform_changed' for g in r['coverage_gaps'])


def test_public_raw_time_cursor_is_marked_as_rounded(tmp_path):
    from src.cursor_store import CursorStore
    from src.verify_condition import diff_first_divergence
    from src.schemas import DiffFirstDivergenceResult
    p=VCDParser(str(waveform(tmp_path,[(0,'0','0'),(1,'1','0'),(2,'0','0')],scale='100fs')))
    r=diff_first_divergence(get_parser=lambda _:p,wave_path_a=p.file_path,signal_a='top.a',
        wave_path_b=p.file_path,signal_b='top.b',cursor_store=CursorStore())
    parsed=DiffFirstDivergenceResult.model_validate(r)
    assert parsed.first_divergence_time_fs==100 and parsed.cursor.time_ps==1
    assert r['cursor']['metadata']['cursor_time_rounded_up']
    assert r['cursor']['metadata']['first_divergence_time_fs']==100


def test_native_reader_cannot_survive_owner_generation_change():
    p=FSDBParser(str(Path(__file__).parent/'fixtures/scale_1ns.fsdb'))
    try:
        with event_readers([(p,'scale_1ns_tb.addr[31:0]')],0,-1) as readers:
            p._scope_epoch=object()
            with pytest.raises(RuntimeError,match='owner changed'):
                readers[0].read_page()
        # Native group cleanup also frees the cursor whose Python owner changed.
        assert not p._transition_group_active
        assert p.get_value_at_time('scale_1ns_tb.addr[31:0]',100000)['value']['dec']==0xaaaa0000
    finally:p.close()
