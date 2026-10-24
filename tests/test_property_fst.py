"""Property replay through real FST readers and the public MCP boundary."""
import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
import server
from fst_analysis_fixture import cycle_pair, write_pair
from test_fst_integration import call, clean_state
from test_fst_request_reuse import workers


def prepare(text='req |-> ##3 ack', declarations='logic clk,req,ack;'):
    result=call('explain_property',text=text,unit='u',
        units=[dict(id='u',declarations=declarations,default_clock='posedge clk')],
        include_check_plan=True,detail='summary')
    plan=result['results'][0]['check_plan']
    assert plan['status']=='prepared',result
    return plan['handle']


def check(path,handle,*,fail=35,started=None,**options):
    event=dict(time_ps=fail,instance_path='top.ap')
    if started is not None:event['structured_fields']=dict(start_time_ps=started)
    return call('verify_window',mode='property',wave_path=str(path),property_plan=handle,
                failure_event=event,**options)


def test_exact_response_has_identical_vcd_and_fst_evidence(tmp_path,workers):
    paths=cycle_pair(tmp_path,dict(req=1,ack=1),[
        dict(req=1,ack=0),dict(req=0,ack=1),dict(req=0,ack=0),dict(req=0,ack=0)])
    handle=prepare();results=[check(path,handle) for path in paths]
    for result in results:
        assert result['status']=='failure_reproduced',result
        assert result['attempts'][0]['start_time_ps']==5
        assert result['attempts'][0]['end_time_ps']==35
        assert result['coverage_status']=='complete'
    assert results[0]['attempts']==results[1]['attempts']
    assert workers
    assert all(child.poll() is not None and not directory.exists() for _,child,directory in workers)
    parser=server._get_parser(str(paths[0]))
    assert not parser._native_sessions and parser._active_batch is None


@pytest.mark.parametrize('reset',[0,1])
def test_midrun_constant_reset_uses_initial_snapshot(tmp_path,reset):
    paths=cycle_pair(tmp_path,dict(req=1,ack=1,rst=1),[
        dict(req=int(i==12),ack=0,rst=reset) for i in range(14)])
    handle=prepare('disable iff(rst) (req |=> ack)','logic clk,req,ack,rst;')
    for path in paths:
        result=check(path,handle,fail=135,started=125)
        assert result['sampling']['history_start_ps']==125,result
        assert result['status']==('not_reproduced' if reset else 'failure_reproduced'),result
        assert result['attempts'][0]['status']==('disabled' if reset else 'false'),result


@pytest.mark.parametrize('condition,disabled_at',[('rst',7),('$sampled(rst)',8)])
def test_between_edge_reset_and_sampled_condition_keep_distinct_times(tmp_path,condition,disabled_at):
    declarations=[(name,1,'wire','input',None) for name in ('clk','req','ack','rst')]
    rows=[(0,'clk','0'),(0,'req','1'),(0,'ack','0'),(0,'rst','0'),
          (5,'clk','1'),(7,'rst','1'),(8,'rst','0'),(10,'clk','0'),
          (10,'req','0'),(15,'clk','1'),(20,'clk','0')]
    paths=write_pair(tmp_path,declarations,rows,end=20)
    handle=prepare(f'disable iff({condition}) (req |=> ack)','logic clk,req,ack,rst;')
    results=[check(path,handle,fail=15,started=5) for path in paths]
    for result in results:
        assert result['status']=='not_reproduced',result
        assert result['attempts'][0]['status']=='disabled'
        assert result['attempts'][0]['end_time_ps']==disabled_at
    assert results[0]['attempts']==results[1]['attempts']


def test_native_fst_errors_keep_their_original_codes(tmp_path,workers):
    handle=prepare()
    result=check(tmp_path/'absent.fst',handle)
    assert result['error_code']=='fst_file_not_found',result
    broken=tmp_path/'broken.fst';broken.write_bytes(b'not an FST file')
    result=check(broken,handle)
    assert result['error_code'].startswith('fst_'),result
    assert result.get('fst_runtime'),result
