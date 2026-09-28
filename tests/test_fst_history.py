"""Cross-format comparison, dynamic sampling and real source-graph X history."""
import asyncio
import json
import sys
from contextlib import contextmanager

import anyio
import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
import server
from fst_fixture import write_fst
from fst_analysis_fixture import write_pair
from src.fst_parser import FSTParser
from src.vcd_parser import VCDParser
from src.divergence_compare import read_stream
from src.dynamic_evidence import Expr
from src.dynamic_observe import ObservationReader
from src.observation_session import ObservationSession
from test_fst_integration import call, clean_state
from test_x_history import RTL


def pair(tmp_path, rows, *, names=('a','b'), **kwargs):
    return write_pair(tmp_path,[(n,1,'wire','input',None) for n in names],rows,**kwargs)


def diff(a,b,**kwargs):
    r=call('diff_first_divergence',wave_path_a=str(a),wave_path_b=str(b),
           signal_a='top.a',signal_b='top.b',**kwargs)
    assert 'error' not in r,r
    return r


@pytest.mark.parametrize('page_size',[1,2,1024])
def test_comparison_initial_prefix_subps_and_both_directions(tmp_path,page_size,monkeypatch):
    original=FSTParser._event_pages
    @contextmanager
    def pages(self,*args,**kw):
        kw.update(max_events=page_size,max_bytes=128)
        with original(self,*args,**kw) as reader: yield reader
    monkeypatch.setattr(FSTParser,'_event_pages',pages)
    fst,vcd=pair(tmp_path,[(0,'a','0'),(0,'b','0'),(10,'a','1'),(12,'a','0'),(20,'b','1')],scale=-13)
    for a,b in ((fst,fst),(fst,vcd),(vcd,fst)):
        r=diff(a,b,end_time_ps=3)
        assert r['first_divergence_time_fs']==1000 and r['earliest_difference_proven']
        same=call('diff_first_divergence',wave_path_a=str(a),signal_a='top.a',
                  wave_path_b=str(b),signal_b='top.a',start_time_ps=0,end_time_ps=3)
        assert same['comparison_status']=='equal' and same['coverage_status']=='complete'
        r=diff(a,b,start_time_ps=0,end_time_ps=0)
        assert r['comparison_status']=='equal'
    fst,vcd=pair(tmp_path,[(0,'a','0'),(0,'b','1')],name='constant')
    r=diff(fst,vcd,start_time_ps=5,end_time_ps=9)
    assert r['first_divergence_time_fs']==5000 and r['earliest_difference_proven']


def test_unknown_missing_and_recording_gaps_never_prove_equality(tmp_path):
    fst,vcd=pair(tmp_path,[(0,'a','0'),(0,'b','0'),(30,'a','1'),(30,'b','0')],activity=[(10,0),(20,1)])
    r=diff(fst,vcd)
    assert r['first_divergence_time_ps']==30 and not r['earliest_difference_proven']
    assert {'dump_inactive','unrecorded_after_dump_resume'} <= {g['reason'] for g in r['coverage_gaps']}
    r=diff(fst,vcd,end_time_ps=5)
    assert r['comparison_status']=='equal'  # Future gap must not contaminate a proven prefix.
    r=diff(fst,vcd,start_time_ps=15,end_time_ps=18)
    assert not r['diverged'] and r['comparison_status']=='inconclusive'
    missing=call('diff_first_divergence',wave_path_a=str(fst),signal_a='top.absent',
                 wave_path_b=str(vcd),signal_b='top.b')
    assert missing['missing_a'] and missing['comparison_status']=='inconclusive'
    fst,vcd=pair(tmp_path,[(0,'a','x'),(0,'b','z'),(5,'a','1'),(5,'b','0')],name='unknown')
    r=diff(fst,vcd)
    assert r['diverged'] and not r['earliest_difference_proven']


def test_observation_cache_keeps_initial_origin_and_gap_invalidates_state(tmp_path):
    fst,_=pair(tmp_path,[(0,'a','1'),(30,'a','0')],names=('a',),activity=[(10,0),(20,1)])
    p=FSTParser(str(fst)); session=ObservationSession()
    e=Expr('signal',1,signal='top.a',bits=(0,),declared_bits=(0,))
    reader=ObservationReader(lambda _:p,str(fst),0,40,session=session)
    assert reader.sample(e,5)['value']=='1'
    for start,time in ((12,15),(21,25)):
        sub=ObservationReader(lambda _:p,str(fst),start,35,session=session)
        r=sub.sample(e,time)
        assert r['value'] is None and r['gaps']
        assert sub.stream('top.a').predecessor is None
    assert reader.sample(e,35)['value']=='0'
    sub=ObservationReader(lambda _:p,str(fst),1,9,session=session)
    assert sub.sample(e,5)['value']=='1'
    assert sub.stream('top.a').predecessor is None
    assert sub.stream('top.a').initial_state['time_fs']==0


def test_native_fsdb_comparison_and_cycle_analysis(tmp_path,require_fsdb_runtime):
    from pathlib import Path
    fsdb=Path(__file__).parent/'fixtures/scale_100fs.fsdb'
    assert fsdb.is_file(), 'FST acceptance requires the existing native scale fixture'
    rows=[(t,'addr [31:0]',f'{v:032b}') for t,v in
          ((0,0),(1000000,0xaaaa0000),(1001000,0xbbbb0000),(1001005,0xcccc0000))]
    rows += [(t,'clk',str((t//50000)%2)) for t in range(0,1100001,50000)]
    fst=write_fst(tmp_path/'scale.fst',rows,scale=-13,end=1100000,
        declarations=[('addr [31:0]',32,'wire','input',None),('clk',1,'wire','input',None)])
    # Scope/name correspondence and expected values are explicit, never guessed.
    for a,sa,b,sb in ((fst,'top.addr',fsdb,'scale_100fs_tb.addr[31:0]'),
                     (fsdb,'scale_100fs_tb.addr[31:0]',fst,'top.addr')):
        r=call('diff_first_divergence',wave_path_a=str(a),signal_a=sa,wave_path_b=str(b),signal_b=sb,
               start_time_ps=0,end_time_ps=109999)
        assert r.get('comparison_status')=='equal',r
    for path,scope in ((fst,'top'),(fsdb,'scale_100fs_tb')):
        r=call('get_signals_by_cycle',wave_path=str(path),clock_path=scope+'.clk',
            signal_paths=[scope+'.addr[31:0]'],start_cycle=9,num_cycles=2,sample_phase='before')
        assert [list(row['signals'].values())[0]['dec'] for row in r['cycles']]==[0,0xcccc0000]


def history_pair(tmp_path,activity=(),name='history',scale=-12):
    rows=[(0,n,v) for n,v in dict(clk='0',rst='0',en='1',d='0',q='0',y='0').items()]
    rows += [(t,'clk',str((t//5)%2)) for t in range(5,41,5)]
    rows += [(12,'d','x'),(15,'q','x'),(15,'y','x'),(18,'d','1'),(20,'en','0')]
    return pair(tmp_path,rows,names=('clk','rst','en','d','q','y'),activity=activity,name=name,scale=scale)


def context(tmp_path,monkeypatch,source=RTL):
    monkeypatch.setenv('TRACEWEAVE_CONNECTIVITY_ROUTE','source_graph')
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_PYTHON',sys.executable)
    monkeypatch.setenv('TRACEWEAVE_AUTO_KDB','0')
    src=tmp_path/'design.sv'; src.write_text(source)
    log=tmp_path/'compile.log'
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {src} -top top\nParsing design file '{src}'\nTop Level Modules:\n       top\n")
    ctx=dict(compile_log=str(log),simulator='vcs')
    async def build():
        replies=await asyncio.gather(*(server.call_tool(tool,ctx) for tool in ('build_tb_hierarchy','scan_structural_risks')))
        for reply in replies: assert 'error' not in json.loads(reply[0].text)
    anyio.run(build)
    return ctx


def test_history_after_recovery_snapshot_and_raw_time_boundaries(tmp_path,monkeypatch):
    ctx=context(tmp_path,monkeypatch)
    for path in history_pair(tmp_path):
        args=dict(**ctx,wave_path=str(path),signal_path='top.q',time_ps=36)
        r=call('trace_x_source',**args,mode='history',history_start_ps=0)
        assert 'error' not in r,r
        h=r['history']
        assert [(n['signal'],n['time_ps'],n['phase']) for n in h['nodes']] == [
            ('top.q',36,'after'),('top.q',35,'before'),('top.q',25,'before'),('top.d',15,'before')]
        assert h['nodes'][0]['interval']['active_interval_start_time_fs']==15000
        assert h['nodes'][-1]['interval']['active_interval_start_time_fs']==12000
        assert not any(n['interval']['true_origin_proven'] for n in h['nodes'])
        snapshot=call('trace_x_source',**args,mode='snapshot')
        assert 'error' not in snapshot,snapshot
        assert snapshot['propagation_chain'] and snapshot['propagation_chain'][0]['signal_path']=='top.q'
    fst,_=history_pair(tmp_path,activity=[(10,0),(20,1)],name='gap')
    snapshot=call('trace_x_source',**ctx,wave_path=str(fst),signal_path='top.q',time_ps=16,mode='snapshot')
    assert snapshot['trace_status']=='value_unavailable' and not snapshot.get('root_cause')
    assert snapshot['propagation_chain'][0]['fst_reading']['gaps']
    h=call('trace_x_source',**ctx,wave_path=str(fst),signal_path='top.q',time_ps=36,
           mode='history',history_start_ps=0)['history']
    assert h['status']!='complete' and not h['edges']
    assert 'dump_inactive' in h['coverage']['gaps']
    fst,_=history_pair(tmp_path,name='subps',scale=-13)
    h=call('trace_x_source',**ctx,wave_path=str(fst),signal_path='top.q',time_ps=3,
           mode='history',history_start_ps=0)['history']
    assert h['nodes'][0]['interval']['active_interval_start_time_fs']==1500
    assert 'sampling_order_unresolved' in h['coverage']['gaps'] and not h['edges']


def test_nested_trace_divergence_and_async_observation(tmp_path,monkeypatch):
    from test_trace_divergence import RTL as COMPARE_RTL
    ctx=context(tmp_path,monkeypatch,COMPARE_RTL)
    names=[('clk',1),('ena',1),('enb',1),('a [7:0]',8),('b [7:0]',8),('qa [7:0]',8),('qb [7:0]',8)]
    rows=[(0,n,f'{v:0{w}b}') for (n,w),v in zip(names,[0,1,1,85,85,0,0])]
    rows += [(3,'b [7:0]',f'{106:08b}'),(5,'qa [7:0]',f'{85:08b}'),(5,'qb [7:0]',f'{106:08b}')]
    rows += [(t,'clk',str((t//5)%2)) for t in range(5,21,5)]
    fst,vcd=write_pair(tmp_path,[(n,w,'wire','input',None) for n,w in names],rows,end=20)
    for a,b in ((fst,vcd),(vcd,fst),(fst,fst)):
        r=call('trace_divergence',side_a={**ctx,'wave_path':str(a),'signal_path':'top.qa'},
            side_b={**ctx,'wave_path':str(b),'signal_path':'top.qb'},
            signal_pairs=[dict(a='top.a',b='top.b'),dict(a='top.ena',b='top.enb')],end_time_ps=20)
        assert 'error' not in r,r
        assert r['comparison']['first_divergence_time_ps']==5 and len(r['nodes'])==2
        assert r['nodes'][1]['a']['signal']=='top.a' and r['nodes'][1]['b']['signal']=='top.b'
        assert r['contexts']['a']['backend_status']['actual_backend']=='source_graph'
    from test_async_observe import step
    from src.async_observe import observe_async_controls
    fst,_=pair(tmp_path,[(0,'clk','0'),(0,'mode','1'),(0,'q','0'),(5,'clk','1'),
        (10,'clk','0'),(12,'mode','0'),(12,'q','x')],names=('clk','mode','q'),name='async')
    p=FSTParser(str(fst))
    r=observe_async_controls(step(),get_parser=lambda _:p,wave=str(fst),start=0,time=13,
                             phase='after',unknown_onset_fs=12000)
    assert r['inference_status']=='not_run' and r['assignment_value_status']=='unmodeled'
    assert r['controls'][0]['edge_at_unknown_onset'] is True
