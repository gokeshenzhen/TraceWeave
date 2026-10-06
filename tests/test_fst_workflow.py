"""Remaining public inputs and cumulative native-request lifecycle."""
import json
from pathlib import Path
import time

import anyio
import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
import server
from fst_fixture import write_fst
from src.evidence_output import expand_compact_result
from src.fst_parser import FSTParser
from src import fst_runtime
from src.fst_runtime import FstError, FstProcess, request_budget, has_fst_input
from test_fst_history import context, history_pair
from test_fst_integration import call, clean_state


def test_log_wave_workflow_with_partial_protocol_coverage(tmp_path,monkeypatch):
    fst,_=history_pair(tmp_path)
    ctx=context(tmp_path,monkeypatch)
    log=tmp_path/'sim.log'
    log.write_text('UVM_ERROR design.sv(2) @ 15 ps: top [MISMATCH] q expected 0 got X\n')
    assert call('get_sim_paths',verif_root=str(tmp_path),wave_file=str(fst))['wave_files'][0]['format']=='fst'
    parsed=call('parse_sim_log',log_path=str(log),simulator='vcs')
    assert parsed['runtime_total_errors']==1,parsed
    sweep=call('sweep_handshakes',wave_path=str(fst))
    assert sweep['coverage_status']=='zero_coverage'
    common={**ctx,'log_path':str(log),'wave_path':str(fst),'top_hint':'top'}
    r=call('analyze_failures',**common,signal_paths=['top.q'],window_ps=5)
    assert 'error' not in r,r
    assert r['wave_context']['signals']['top.q']['value_at_center']['bin']=='x'
    r=call('analyze_failure_event',**common,failure_event=r['focused_event'])
    assert 'error' not in r and r['time_anchor']['time_ps']==15,r
    r=call('recommend_failure_debug_next_steps',**common)
    assert 'error' not in r,r
    assert r['runtime_protocol_coverage']['coverage_status']=='zero_coverage'
    r=call('explain_signal_driver',**ctx,wave_path=str(fst),signal_path='top.q',time_ps=36)
    assert 'error' not in r,r
    assert r['format']=='traceweave.driver.compact.v1'
    r=expand_compact_result(r)
    assert r['backend_status']['actual_backend']=='source_graph'


def test_packed_field_layout_from_real_source_graph(tmp_path,monkeypatch):
    ctx=context(tmp_path,monkeypatch,'''module top;
typedef struct packed {logic valid; logic [3:0] data; logic ready;} packet_t;
packet_t bus;
endmodule
''')
    path=write_fst(tmp_path/'typed.fst',[(0,'bus [5:0]','101011')],
        declarations=[('bus [5:0]',6,'wire','input',None)])
    r=call('resolve_packed_fields',**ctx,wave_path=str(path),signal_path='top.bus[5:0]',
           source_signal='top.bus',fields=['valid','data','ready'])
    assert r.get('status')=='resolved',r
    assert {k:v['bits'] for k,v in r['fields'].items()}=={'valid':[5],'data':[4,3,2,1],'ready':[0]}
    assert call('get_signal_at_time',wave_path=str(path),signal_path=r['fields']['data'],time_ps=0)['value']['dec']==5


def test_total_request_deadline_is_shared_across_native_children(tmp_path,monkeypatch):
    path=write_fst(tmp_path/'deadline.fst',[(0,'a','0')])
    with request_budget() as deadline:
        for _ in range(2):
            with FstProcess(path,timeout_sec=300) as child:
                assert child.deadline==deadline
        parser=FSTParser(path)
        with parser._event_batch(['top.a'],0,20,deadline=deadline+500) as batch:
            assert batch.deadline==deadline
        monkeypatch.setattr(fst_runtime.time,'monotonic',lambda:deadline+1)
        with pytest.raises(FstError,match='fst_timeout'):
            FstProcess(path,timeout_sec=300)
        monkeypatch.undo()


def test_public_deadline_includes_nested_paths_and_async_work(monkeypatch):
    assert has_fst_input({'side_a':{'wave_path':'a.FST'}})
    assert not has_fst_input({'side_a':{'wave_path':'a.vcd'}})
    async def slow(*args):
        await anyio.sleep(10)
        raise AssertionError('deadline did not cancel dispatch')
    monkeypatch.setattr(server,'_dispatch',slow)
    monkeypatch.setattr(fst_runtime,'REQUEST_TIMEOUT_SEC',0.02)
    started=time.monotonic()
    r=call('trace_divergence',side_a={'wave_path':'a.fst'})
    assert r['error_code']=='fst_timeout' and time.monotonic()-started<1


def test_public_catalog_has_no_unvalidated_wave_inputs():
    for tool in anyio.run(server.list_tools):
        if {'wave_path','wave_path_a','wave_path_b','side_a'} & tool.inputSchema.get('properties',{}).keys():
            assert tool.name in fst_runtime.FST_SUPPORTED_TOOLS
