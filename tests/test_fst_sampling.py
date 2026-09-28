"""Cycle/expression acceptance, with independent values on both formats."""
from contextlib import contextmanager

import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
from fst_fixture import write_fst
from fst_analysis_fixture import write_pair
from src.cycle_query import get_signals_by_cycle, sample_signals_on_edges
from src.expression_observe import ExpressionParser
from src.fst_parser import FSTParser
from src.vcd_parser import VCDParser
from src.scope_metadata import ScopeIdentityChanged
from test_fst_integration import call, clean_state


def expression_wave(tmp_path):
    names = [('clk', 1), ('data [7:0]', 8), ('idx [2:0]', 3), ('e [7:0]', 8),
             ('f [7:0]', 8), ('g [7:0]', 8), ('mem[0] [7:0]', 8), ('mem[1] [7:0]', 8)]
    declarations = [(n, w, 'wire', 'input', None) for n, w in names]
    rows = [(0, n, f'{v:0{w}b}') for (n,w),v in zip(names,[0,8,3,5,0,0,12,31])]
    rows += [(t, 'clk', str((t//5) % 2)) for t in range(5,41,5)]
    rows += [(5,'data [7:0]','00010000'),(10,'idx [2:0]','100'),
             (20,'data [7:0]','00100000'),(30,'idx [2:0]','xxx')]
    return write_pair(tmp_path,declarations,rows)


EXPR = dict(expr='a[i] + e + f + g', typing='wave_bits', bindings={
    'a':'top.data', 'i':'top.idx', 'e':'top.e', 'f':'top.f', 'g':'top.g'})


@pytest.mark.parametrize('phase,expected', [('before',[6,6,5,None]), ('after',[5,6,5,None])])
def test_cycle_expression_uses_physical_edge_phase_and_all_dependencies(tmp_path, phase, expected):
    for cls, path in zip((FSTParser,VCDParser),expression_wave(tmp_path)):
        adapter = ExpressionParser(cls(str(path)))
        key = adapter.bind(EXPR)
        result = get_signals_by_cycle(adapter,'top.clk',[key],num_cycles=4,
                                     sample_phase=phase,sample_offset_ps=0)
        assert result['clock_edges_complete']
        assert [row['signals'][key]['dec'] for row in result['cycles']] == expected
        assert [row['time_ps'] for row in result['cycles']] == [5,15,25,35]


@pytest.mark.parametrize('size',[1,2,1024])
def test_expression_pages_preserve_initial_anchor_and_dump_gaps(tmp_path,size):
    p=FSTParser(write_fst(tmp_path/'gap.fst',[(0,'a','0'),(10,'a','1'),(30,'a','0')],
                        end=40,activity=[(15,0),(20,1)]))
    original=p._event_pages
    @contextmanager
    def small(*args,**kw):
        kw.update(max_events=size,max_bytes=128)
        with original(*args,**kw) as reader:yield reader
    p._event_pages=small
    adapter=ExpressionParser(p)
    key=adapter.bind(dict(expr='a',bindings={'a':'top.a'},typing='wave_bits'))
    initial=adapter.get_transitions(key,1,9)
    assert initial['predecessor'] is None and initial['initial_state']['time_fs']==0
    assert not initial['transitions']
    result=adapter.get_transitions(key,0,40)
    assert result['initial_state']['value']['dec']==0
    assert [(row['time_ps'],row['value']['dec'] if row['value'] else None)
            for row in result['transitions']] == [(10,1),(15,None),(30,0)]


@pytest.mark.parametrize('phase,expected',[('before',[0,1,2,3]),('after',[1,2,3,4])])
def test_sub_ps_edges_with_identical_public_labels_stay_distinct(tmp_path,phase,expected):
    rows=[(0,'clk','0'),(0,'data [3:0]','0000')]
    for t in range(1,9):
        rows.append((t,'clk',str(t%2)))
        if t%2:rows.append((t,'data [3:0]',f'{(t+1)//2:04b}'))
    p=FSTParser(write_fst(tmp_path/'sub.fst',rows,scale=-13,end=10,
        declarations=[('clk',1,'wire','input',None),('data [3:0]',4,'wire','input',None)]))
    result=get_signals_by_cycle(p,'top.clk',['top.data'],num_cycles=4,sample_phase=phase,sample_offset_ps=0)
    assert [row['time_ps'] for row in result['cycles']]==[1]*4
    assert [row['time_fs'] for row in result['cycles']]==[100,300,500,700]
    assert [row['signals']['top.data']['dec'] for row in result['cycles']]==expected
    assert result['clock_period_fs']==200


def test_unknown_clock_prefix_and_recording_gap_cannot_establish_cycles(tmp_path):
    path=write_fst(tmp_path/'clk.fst',[(0,'a','x'),(5,'a','0'),(10,'a','1')])
    for phase in ('before','after'):
        with pytest.raises(ValueError,match='incomplete|ambiguous'):
            get_signals_by_cycle(FSTParser(path),'top.a',['top.a'],num_cycles=1,sample_phase=phase)
    path=write_fst(tmp_path/'off.fst',[(0,'a','0'),(5,'a','1'),(10,'a','0'),(25,'a','1')],
                   activity=[(12,0),(20,1)])
    sampled=sample_signals_on_edges(FSTParser(path),'top.a',['top.a'],sample_phase='before',sample_offset_ps=0)
    assert sampled['transition_data_truncated'] and sampled['total_edges_found']==1
    assert 'dump_inactive' in sampled['sampling_gaps']


def test_types_arrays_templates_missing_elements_and_fixed_selection_through_mcp(tmp_path):
    fst,_=expression_wave(tmp_path)
    def point(spec,at=0):
        r=call('get_signal_at_time',wave_path=str(fst),signal_path=spec,time_ps=at)
        assert 'error' not in r,r
        return r
    explicit=dict(expr='a >>> 1',bindings={'a':'top.mem[1]'},types={'a':dict(width=8,signed=True)})
    assert point(explicit)['value']['dec']==15
    for binding in ({'path_template':'top.mem[{index}][7:0]'},
                    {'elements':[{'indices':[i],'signal':f'top.mem[{i}][7:0]'} for i in range(2)]}):
        spec=dict(expr='mem[i]',bindings={'mem':binding},types={'mem':dict(width=8,unpacked=[[0,1]])},constants={'i':'1'})
        assert point(spec)['value']['dec']==31
    missing=dict(expr='mem[1]',bindings={'mem':{'path_template':'top.absent[{index}][7:0]'}},
                 types={'mem':dict(width=8,unpacked=[[0,1]],two_state=True)})
    r=point(missing)
    assert r.get('value') is None and 'array_element_not_dumped' in str(r['expressions'])
    assert point({'path':'top.data','bits':[3,0]})['value']['bin']=='10'
    result=call('get_signals_by_cycle',wave_path=str(fst),clock_path='top.clk',signal_paths=[EXPR],
                num_cycles=4,sample_phase='before')
    assert 'error' not in result,result
    key=result['expressions'][0]['key']
    assert [row['signals'][key].get('dec') for row in result['cycles']]==[6,6,5,None]


def test_changed_file_never_reuses_fst_clock_edges(tmp_path):
    path,_=expression_wave(tmp_path)
    p=FSTParser(path)
    get_signals_by_cycle(p,'top.clk',['top.data'],num_cycles=1)
    path.write_bytes(path.read_bytes()+b'\0')
    with pytest.raises(ScopeIdentityChanged):
        get_signals_by_cycle(p,'top.clk',['top.data'],num_cycles=1)


def test_after_offset_is_picoseconds_for_derived_and_raw_columns(tmp_path):
    path=write_fst(tmp_path/'offset.fst',[(0,'clk','0'),(0,'a','0'),(5,'clk','1'),
        (10,'clk','0'),(11,'a','1')],scale=-13,end=30,
        declarations=[('clk',1,'wire','input',None),('a',1,'wire','input',None)])
    parser=ExpressionParser(FSTParser(path))
    key=parser.bind(dict(expr='top.a',typing='wave_bits'))
    result=get_signals_by_cycle(parser,'top.clk',['top.a',key],num_cycles=1)
    assert all(v['dec']==1 for v in result['cycles'][0]['signals'].values())


def test_unknown_pre_window_clock_anchor_is_not_a_complete_prefix(tmp_path):
    path=write_fst(tmp_path/'prefix.fst',[(0,'a','x'),(5,'a','0'),(10,'a','1')])
    result=sample_signals_on_edges(FSTParser(path),'top.a',['top.a'],start_ps=1,end_ps=20,
                                  sample_phase='before',sample_offset_ps=0)
    assert result['transition_data_truncated'] and not result['samples']


def test_integer_fs_period_above_float_exact_range():
    from src.cycle_query import _compute_clock_period_ps
    interval = 2**53 + 1
    assert _compute_clock_period_ps([0,interval,2*interval]) == interval
