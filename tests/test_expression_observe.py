from contextlib import contextmanager
from pathlib import Path
from collections import Counter
import threading

import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.cycle_query import get_signals_by_cycle,sample_signals_on_edges
from src.event_pages import Event,EventPage
from src.expression_observe import ExpressionParser
from src.fsdb_parser import FSDBParser
from src.vcd_parser import VCDParser
from src.waveform_batch import event_readers


def wave(tmp_path,*,scale='1ps',small_pages=None):
    path = tmp_path / 'expression.vcd'
    path.write_text(f'''$timescale {scale} $end
$scope module tb $end
$var wire 8 a data [7:0] $end
$var wire 3 i index [2:0] $end
$var wire 8 e expected [7:0] $end
$var wire 1 c clk $end
$upscope $end
$enddefinitions $end
#0
b00001000 a
b011 i
b00000101 e
0c
#5
1c
#10
0c
b100 i
#15
1c
#20
0c
b00010000 a
#25
1c
#30
0c
bxxx i
#35
1c
#40
0c
''')
    p = VCDParser(str(path))
    if small_pages:
        original = p._event_pages
        @contextmanager
        def pages(*args,**kwargs):
            kwargs.update(max_events=small_pages,max_bytes=128)
            with original(*args,**kwargs) as reader:
                yield reader
        p._event_pages = pages
    return p


def bind(p,expr='a[i] + e'):
    adapter = ExpressionParser(p)
    key = adapter.bind(dict(expr=expr,typing='wave_bits',bindings={
        'a':'tb.data','i':'tb.index','e':'tb.expected'}))
    return adapter,key


def test_point_and_cycles_recompute_index_and_keep_actual_dependencies(tmp_path):
    base = wave(tmp_path)
    p,key = bind(base)
    assert [p.get_value_at_time(key,t)['value']['dec'] for t in (5,15,25,35)] == [6,5,6,None]
    receipts = p.expression_receipts()[0]
    assert receipts['observations'][0]['dependencies'][0]['role'] == 'index'
    assert receipts['observations'][0]['dependencies'][1]['bits'] == [3]
    assert receipts['observations'][1]['dependencies'][1]['bits'] == [4]
    assert 'index_unknown' in receipts['observations'][3]['gaps']
    counts = Counter()
    read = base.get_transitions
    def counted(path,*a,**kw):
        counts[path] += 1
        return read(path,*a,**kw)
    base.get_transitions = counted
    r = get_signals_by_cycle(p,'tb.clk',[key],num_cycles=4)
    assert [row['signals'][key]['dec'] for row in r['cycles']] == [6,5,6,None]
    assert counts == {'tb.clk':1,'tb.data[7:0]':1,'tb.index[2:0]':1,'tb.expected[7:0]':1}


@pytest.mark.parametrize('size',[1,2,7,1024])
def test_event_union_strict_window_and_dependency_anchor(tmp_path,size):
    p,key = bind(wave(tmp_path,small_pages=size))
    r = p.get_transitions(key,5,25)
    assert (r['predecessor']['time_ps'],r['predecessor']['value']['dec']) == (0,6)
    assert [(e['time_ps'],e['value']['dec']) for e in r['transitions']] == [(10,5),(20,6)]
    assert not r['truncated'] and r['predecessor_kind'] == 'dependency_anchor'
    around = p.get_signals_around_time([key],15,5,2)
    assert around['signals'][key]['value_at_center']['dec'] == 5
    assert [r['time_ps'] for r in around['signals'][key]['pre_window_transitions']] == [0]


def test_raw_subps_and_before_phase_use_physical_time(tmp_path):
    p,key = bind(wave(tmp_path,scale='100fs',small_pages=1))
    r = p.get_transitions(key,0,4)
    assert [(e['time_fs'],e['value']['dec']) for e in r['transitions']] == [(0,6),(1000,5),(2000,6),(3000,None)]
    r = sample_signals_on_edges(p,'tb.clk',[key],sample_offset_ps=0,sample_phase='before',compact=True)
    assert [v['dec'] if v else None for v in r['signal_columns'][key]] == [6,5,6,None]


def test_same_physical_group_is_fully_folded_before_evaluation(tmp_path):
    base = wave(tmp_path,small_pages=1)
    text = Path(base.file_path).read_text().replace('#10\n0c\nb100 i',
        '#10\n0c\nb100 i\nb00010000 a\nb011 i\nb00001000 a')
    Path(base.file_path).write_text(text)
    p,key = bind(base)
    r = p.get_transitions(key,5,15)
    assert not r['transitions'] and r['predecessor']['value']['dec'] == 6


def test_lost_group_does_not_emit_a_partial_expression_value(tmp_path):
    base = wave(tmp_path)
    original = base._event_pages
    @contextmanager
    def pages(path,*args,**kwargs):
        with original(path,*args,**kwargs) as reader:
            if path == 'tb.index[2:0]':
                class Broken:
                    end_fs,width = reader.end_fs,reader.width
                    def read_page(self):
                        return EventPage((Event(0,0,'011'),Event(10000,10,'100')),
                            next_time=10000,truncated=True)
                yield Broken()
            else:
                yield reader
    base._event_pages = pages
    p,key = bind(base)
    r = p.get_transitions(key,0,40)
    assert [(e['time_ps'],e['value']['dec']) for e in r['transitions']] == [(0,6)]
    assert r['truncated']
    assert p.expression_receipts()[0]['coverage_status'] == 'partial'


def test_constant_expression_and_one_bit_derived_clock(tmp_path):
    p,key = bind(wave(tmp_path),"8'd42")
    assert p.get_transitions(key,5,10)['predecessor']['value']['dec'] == 42
    assert p.get_transitions(key,0,10)['transitions'][0]['value']['dec'] == 42
    clock = p.bind(dict(expr='!tb.clk',typing='wave_bits'))
    r = get_signals_by_cycle(p,clock,[key],num_cycles=3)
    assert [row['time_ps'] for row in r['cycles']] == [10,20,30]
    assert all(row['signals'][key]['dec']==42 for row in r['cycles'])


def test_column_prefix_limit_does_not_extrapolate(tmp_path):
    base = wave(tmp_path)
    original = base.get_transitions
    def limited(path,*a,**kw):
        r = original(path,*a,**kw)
        if path == 'tb.index[2:0]':
            r['transitions'] = r['transitions'][:1]
            r['truncated'] = True
        return r
    base.get_transitions = limited
    p,key = bind(base)
    r = get_signals_by_cycle(p,'tb.clk',[key],num_cycles=4)
    assert r['transition_data_truncated'] and r['transition_signals_truncated']==[key]
    assert r['cycles'][0]['signals'][key]['dec']==6
    assert all(row['signals'][key]['dec'] is None for row in r['cycles'][1:])


def test_cancellation_and_dependency_budget(tmp_path,monkeypatch):
    import src.expression_observe as observe
    p,key = bind(wave(tmp_path,small_pages=1))
    monkeypatch.setattr(observe,'MAX_EVENTS',2)
    r = p.get_transitions(key)
    assert r['truncated'] and not r['transitions']
    event = threading.Event(); token=push_cancel_event(event)
    try:
        event.set()
        with pytest.raises(OperationCancelled):
            p.get_transitions(key)
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize('scale,expected',[
    ('100fs',[(0,0),(100000000,0xaa),(100100000,0xbb),(100100500,0xcc)]),
    ('1ns',[(0,0),(100000000,0xaa),(101000000,0xbb)])])
def test_native_expression_pages_point_and_cleanup(scale,expected):
    base = FSDBParser(str(Path(__file__).parent/'fixtures'/f'scale_{scale}.fsdb'))
    try:
        p = ExpressionParser(base)
        key = p.bind(dict(expr='a[24 +: 8]',typing='wave_bits',bindings={'a':f'scale_{scale}_tb.addr[31:0]'}))
        r = p.get_transitions(key)
        assert [(e['time_fs'],e['value']['dec']) for e in r['transitions']]==expected
        assert not base._transition_group_active
        assert p.get_value_at_time(key,100000)['value']['dec']==0xaa
        with event_readers([(p,key)],0,-1,max_events=1,max_bytes=128) as readers:
            assert readers[0].read_page().events
        assert not base._transition_group_active
    finally:
        base.close()


def test_oversized_group_fallback_retains_exact_subps_events(tmp_path):
    base = wave(tmp_path,scale='100fs')
    calls = []
    @contextmanager
    def group(paths):
        calls.append(tuple(paths))
        yield len(set(paths)) <= 1
    base.transition_group = group
    p,key = bind(base)
    r = p.get_transitions(key)
    assert [(e['time_fs'],e['value']['dec']) for e in r['transitions']]==[(0,6),(1000,5),(2000,6),(3000,None)]
    assert len(calls)==4 and not r['truncated']


def test_sparse_array_sampling_only_reads_elements_selected_in_window(tmp_path):
    base = wave(tmp_path)
    file = Path(base.file_path)
    file.write_text(file.read_text().replace('$var wire 1 c clk $end',
        '$var wire 8 u unused [7:0] $end\n$var wire 1 c clk $end').replace('#0\n','#0\nb11111111 u\n'))
    p = ExpressionParser(base)
    key = p.bind(dict(expr='m[i]',typing='wave_bits',bindings={
        'm':{'elements':[{'indices':[3],'signal':'tb.data'},
                         {'indices':[4],'signal':'tb.expected'},
                         {'indices':[5],'signal':'tb.unused'}]},
        'i':'tb.index'},types={'m':{'width':8,'unpacked':[[0,7]]}}))
    calls = []
    original = base.get_transitions
    def measured(path,*a,**kw):
        calls.append(path)
        return original(path,*a,**kw)
    base.get_transitions = measured
    r = get_signals_by_cycle(p,'tb.clk',[key],num_cycles=4)
    assert [row['signals'][key]['dec'] for row in r['cycles']] == [8,5,5,None]
    assert 'tb.unused[7:0]' not in calls
