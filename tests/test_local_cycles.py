"""Local cycle acceptance: physical phase, contiguous coverage and bounded reads."""
from contextlib import contextmanager
import json
import threading

import anyio
import pytest

import src.local_cycles as local
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.cycle_query import get_signals_by_cycle
from src.vcd_parser import VCDParser
from src.waveform_selection import SelectionParser


def wave(tmp_path, rows, scale='1ps'):
    path = tmp_path / 'local.vcd'
    text = f'$timescale {scale} $end\n$scope module top $end\n$var wire 1 ! clk $end\n$var wire 1 " d $end\n$upscope $end\n$enddefinitions $end\n'
    text += ''.join(f'#{t}\n{clk}!\n{d}"\n' for t, clk, d in rows)
    path.write_text(text)
    return VCDParser(str(path))


def sample(p, **kw):
    args = dict(start_time_ps=0, num_cycles=3, cycle_index_origin='window')
    args.update(kw)
    return get_signals_by_cycle(p, 'top.clk', ['top.d'], **args)


@pytest.mark.parametrize('phase,values', [('before',[0,1]), ('after',[1,0])])
def test_late_known_window_ignores_only_earlier_x(tmp_path, phase, values):
    p = wave(tmp_path, [(0,'x','0'),(10,'0','0'),(15,'1','1'),(20,'0','1'),(25,'1','0'),(30,'0','0')])
    r = sample(p,start_time_ps=14,num_cycles=2,sample_phase=phase)
    assert [row['signals']['top.d']['dec'] for row in r['cycles']] == values
    assert [row['cycle'] for row in r['cycles']] == [0,1]
    assert r['local_coverage']['status'] == 'complete'
    assert r['clock_edges_complete'] is False
    initial = sample(p,sample_phase=phase)
    assert initial['num_cycles_returned'] == 0
    assert initial['local_coverage']['stop_reason'] == 'clock_unknown'


@pytest.mark.parametrize('phase',['before','after'])
def test_clock_x_stops_contiguous_prefix_and_does_not_fill_after_recovery(tmp_path, phase):
    p = wave(tmp_path, [(0,'0','x'),(5,'1','x'),(10,'0','1'),(15,'1','1'),
                       (20,'x','0'),(25,'1','1'),(30,'0','0'),(35,'1','1'),(40,'0','0')])
    r = sample(p,sample_phase=phase)
    assert [row['time_ps'] for row in r['cycles']] == [5,15]
    assert r['cycles'][0]['signals']['top.d']['dec'] is None
    assert r['local_coverage']['status'] == 'partial'
    assert r['local_coverage']['stop_time_fs'] == 20000
    recovered = sample(p,start_time_ps=26,num_cycles=1,sample_phase=phase)
    assert [row['time_ps'] for row in recovered['cycles']] == [35]


@pytest.mark.parametrize('phase,values',[('before',[0,1,0]),('after',[1,0,1])])
def test_sub_ps_edges_keep_distinct_raw_times_and_before_excludes_same_time(tmp_path,phase,values):
    p=wave(tmp_path,[(0,'0','0'),(1,'1','1'),(2,'0','1'),(3,'1','0'),(4,'0','0'),(5,'1','1'),(10,'0','1')],scale='100fs')
    r=sample(p,sample_phase=phase,sample_offset_ps=0)
    assert [row['time_ps'] for row in r['cycles']]==[1,1,1]
    assert [row['time_fs'] for row in r['cycles']]==[100,300,500]
    assert [row['signals']['top.d']['dec'] for row in r['cycles']]==values


def test_same_time_clock_toggle_removes_entire_ambiguous_group(tmp_path):
    p=wave(tmp_path,[(0,'0','0'),(5,'1','1'),(10,'0','0'),(15,'1','1'),(15,'0','0'),(20,'1','1')])
    r=sample(p)
    assert [row['time_ps'] for row in r['cycles']]==[5]
    assert r['local_coverage']['stop_reason']=='clock_event_order_unresolved'
    from src.expression_observe import ExpressionParser
    ep=ExpressionParser(p)
    clock=ep.bind(dict(expr='clk',scope='top',typing='wave_bits'))
    derived=get_signals_by_cycle(ep,clock,['top.d'],start_time_ps=0,num_cycles=3,cycle_index_origin='window')
    assert [row['time_ps'] for row in derived['cycles']]==[5]
    assert derived['local_coverage']['stop_reason']=='clock_event_order_unresolved'


def test_gated_clock_expansion_does_not_skip_sub_ps_boundary_or_read_history(tmp_path,monkeypatch):
    p=wave(tmp_path,[(0,'0','0'),(11,'1','1'),(12,'0','1'),(201,'1','0'),(202,'0','0'),(250,'0','0')],scale='100fs')
    monkeypatch.setattr(local,'WINDOW_PS',1)
    calls=[]
    original=p._event_pages
    @contextmanager
    def record(path,start,end,**kw):
        calls.append((start,end))
        with original(path,start,end,**kw) as r:yield r
    p._event_pages=record
    r=sample(p,start_time_ps=1,num_cycles=2,sample_offset_ps=0)
    assert [row['time_fs'] for row in r['cycles']]==[1100,20100]
    assert all(start>=1 and end>=start for start,end in calls)
    assert r['reading']['estimated_decoded_bytes']<=local.DECODED_BYTES


def test_end_window_cap_counts_are_explicit(tmp_path):
    p=wave(tmp_path,[(0,'0','0'),(5,'1','1'),(10,'0','1'),(15,'1','0'),(20,'0','0')])
    r=sample(p,start_time_ps=5,end_time_ps=15,max_cycles=1)
    assert [row['time_ps'] for row in r['cycles']]==[5]
    assert r['capped'] and r['local_coverage']['stop_reason']=='cycle_limit'
    full=sample(p,start_time_ps=5,end_time_ps=15,max_cycles=3)
    assert [row['time_ps'] for row in full['cycles']]==[5,15]
    assert full['local_coverage']['window_complete'] and not full['truncated']


def test_cumulative_byte_budget_and_cancel_are_observable(tmp_path,monkeypatch):
    p=wave(tmp_path,[(0,'0','0'),(5,'1','1'),(10,'0','0'),(15,'1','1'),(20,'0','0')])
    monkeypatch.setattr(local,'DECODED_BYTES',1600)
    r=sample(p)
    assert r['local_coverage']['status']=='partial'
    assert r['local_coverage']['stop_reason']=='decoded_byte_budget'
    assert r['reading']['estimated_decoded_bytes']<=1600
    flag=threading.Event();flag.set();token=push_cancel_event(flag)
    try:
        with pytest.raises(OperationCancelled):sample(p)
    finally:pop_cancel_event(token)


def test_selection_and_global_default_retain_phase_and_numbering(tmp_path):
    raw=wave(tmp_path,[(0,'0','0'),(5,'1','1'),(10,'0','1'),(15,'1','0'),(20,'0','0')])
    p=SelectionParser(raw); key=p.bind({'path':'top.d','bits':[0]})
    r=get_signals_by_cycle(p,'top.clk',[key],start_time_ps=11,num_cycles=1,cycle_index_origin='window',sample_phase='before')
    assert r['cycles'][0]['signals'][key]['dec']==1
    global_result=get_signals_by_cycle(raw,'top.clk',['top.d'],start_time_ps=11,num_cycles=1)
    assert global_result['cycles'][0]['cycle']==1


def test_eof_offset_and_missing_predecessor_are_not_fabricated(tmp_path):
    p=wave(tmp_path,[(5,'1','1'),(10,'0','0'),(15,'1','1')])
    missing=sample(p,start_time_ps=5)
    assert missing['local_coverage']['stop_reason']=='clock_predecessor_missing'
    end=sample(p,start_time_ps=11,num_cycles=1)
    assert end['local_coverage']['stop_reason']=='sample_outside_waveform' and not end['cycles']
    outside=sample(p,start_time_ps=30)
    assert outside['local_coverage']['stop_reason']=='window_outside_waveform'
    past_end=sample(p,start_time_ps=11,end_time_ps=40,sample_phase='before')
    assert not past_end['local_coverage']['window_complete']
    assert past_end['local_coverage']['stop_reason']=='waveform_end'


def test_missing_predecessor_and_legacy_precision_remain_frontiers(tmp_path,monkeypatch):
    p=wave(tmp_path,[(0,'0','0'),(5,'1','1'),(10,'0','0')],scale='100fs')
    monkeypatch.setattr(p,'_supports_event_pages',lambda:False)
    r=sample(p)
    assert r['local_coverage']['status']=='partial' and not r['cycles']


def test_native_cancellation_closes_clock_group(monkeypatch,require_fsdb_runtime):
    from pathlib import Path
    from src.fsdb_parser import FSDBParser
    p=FSDBParser(str(Path(__file__).parent/'fixtures/scale_100fs.fsdb'))
    p._open()
    original=p._event_pages
    flag=threading.Event();token=push_cancel_event(flag)
    @contextmanager
    def cancel(*args,**kw):
        with original(*args,**kw) as reader:
            flag.set()
            yield reader
    monkeypatch.setattr(p,'_event_pages',cancel)
    try:
        # The clock name comes from the retained cross-scale fixture.
        with pytest.raises(OperationCancelled):
            get_signals_by_cycle(p,'scale_100fs_tb.clk',[],start_time_ps=1,num_cycles=1,cycle_index_origin='window')
        assert not p._transition_group_active
    finally:
        pop_cancel_event(token)
        p.close()


def test_local_mode_public_schema_and_validation(tmp_path):
    import server
    p=wave(tmp_path,[(0,'0','0'),(5,'1','1'),(10,'0','0')])
    args=dict(wave_path=p.file_path,clock_path='top.clk',signal_paths=['top.d'],
              cycle_index_origin='window',start_time_ps=1,num_cycles=1,sample_phase='before')
    try:
        r=json.loads(anyio.run(server.call_tool,'get_signals_by_cycle',args)[0].text)
        assert r['cycle_index_origin']=='window' and r['cycles'][0]['sample_time_fs']==5000
        args.pop('start_time_ps')
        r=json.loads(anyio.run(server.call_tool,'get_signals_by_cycle',args)[0].text)
        assert 'window origin requires' in r['error']
    finally:
        server.reset_session_state()
        for _,parser in server._parser_cache.values():server._dispose_cached_object(parser)
        server._parser_cache.clear()
