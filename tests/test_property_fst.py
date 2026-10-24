"""Property replay through real FST readers and the public MCP boundary."""
import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
import server
from fst_analysis_fixture import cycle_pair, write_pair
from fst_fixture import write_fst
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


@pytest.mark.parametrize('gap',[(12,20),(40,50)])
def test_recording_gap_only_affects_intersecting_checks(tmp_path,gap,workers):
    paths=cycle_pair(tmp_path,dict(req=1,ack=1),[
        dict(req=1,ack=0),dict(req=0,ack=0),dict(req=0,ack=0),
        dict(req=0,ack=0),dict(req=0,ack=0),dict(req=0,ack=0)],
        activity=[(gap[0],0),(gap[1],1)])
    result=check(paths[0],prepare(),started=5)
    if gap[0]<35:
        assert result['status']=='insufficient_evidence',result
        assert result['coverage_status']!='complete'
        assert result['reproduced_count']==0 and result['gap_codes']
        assert 'dump_inactive' in result['gap_codes'],result
    else:
        assert result['status']=='failure_reproduced',result
        assert result['coverage_status']=='complete'


def test_recovered_recording_supports_a_fresh_attempt(tmp_path):
    declarations=[(name,1,'wire','input',None) for name in ('clk','req','ack')]
    rows=[(0,'clk','0'),(0,'req','0'),(0,'ack','0'),(5,'clk','1'),(10,'clk','0'),
          (21,'clk','1'),(22,'clk','0'),(23,'req','1'),(23,'ack','1'),
          (24,'ack','0'),(25,'clk','1'),(30,'clk','0'),(30,'req','0'),(35,'clk','1')]
    path=write_fst(tmp_path/'recovered.fst',rows,declarations=declarations,
                   end=40,activity=[(12,0),(20,1)])
    result=check(path,prepare('req |=> ack'),started=25)
    assert result['status']=='failure_reproduced',result
    assert result['sampling']['history_start_ps']>=20
    assert result['attempts'][0]['start_time_ps']==25


def test_missing_history_and_truncated_reads_remain_inconclusive(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from dataclasses import replace
    from src.fst_parser import FSTParser
    paths=cycle_pair(tmp_path,dict(req=1,ack=1),[dict(req=1,ack=0),dict(req=0,ack=0)])
    result=check(paths[0],prepare('req |-> $past(ack)'),fail=5,started=5)
    assert result['status']=='insufficient_evidence',result
    assert 'history_before_capture_unavailable' in result['gap_codes']
    original=FSTParser._event_pages
    @contextmanager
    def truncated(self,*args,**kwargs):
        with original(self,*args,**kwargs) as reader:
            read=reader.read_page
            reader.read_page=lambda:replace(read(),truncated=True)
            yield reader
    monkeypatch.setattr(FSTParser,'_event_pages',truncated)
    result=check(paths[0],prepare('req |=> ack'),fail=15,started=5)
    assert result['status']=='insufficient_evidence',result
    assert result['coverage_status']!='complete' and result['reproduced_count']==0


def test_subps_clock_preserves_sampling_and_rejects_ambiguous_anchor(tmp_path):
    declarations=[(name,1,'wire','input',None) for name in ('clk','req','ack')]
    paths=write_pair(tmp_path,declarations,[
        (0,'clk','0'),(0,'req','1'),(0,'ack','0'),(1,'clk','1'),(1,'ack','1'),
        (15,'clk','0'),(20,'clk','1')],scale=-13,end=30)
    handle=prepare('req |-> ack')
    results=[check(path,handle,fail=1,started=1) for path in paths]
    for result in results:
        assert result['status']=='failure_reproduced',result
        assert result['attempts'][0]['end_time_fs']==100
    assert results[0]['attempts']==results[1]['attempts']
    path=write_fst(tmp_path/'ambiguous.fst',[(0,'clk','0'),(0,'req','1'),(0,'ack','0'),
        (1,'clk','1'),(2,'clk','0'),(3,'clk','1')],declarations=declarations,scale=-13,end=10)
    result=check(path,handle,fail=1)
    assert result['status']=='insufficient_evidence',result
    assert result['error_code']=='property_failure_clock_ambiguous'


@pytest.mark.anyio
@pytest.mark.parametrize('stop',['cancel','property_timeout','fst_timeout'])
async def test_active_property_reads_reap_workers_on_stop(tmp_path,monkeypatch,workers,stop):
    import anyio,threading,time
    from src import fst_runtime
    from src.fst_runtime import FstProcess
    from test_property_check import plan
    path,_=cycle_pair(tmp_path,dict(req=1,ack=1),[dict(req=1,ack=0)]*4)
    handle=await plan()
    entered=threading.Event();original=FstProcess.ask;results=[]
    def blocked(self,command):
        if command.get('op')=='batch':
            entered.set()
            while True:
                self.check();time.sleep(.005)
        return original(self,command)
    monkeypatch.setattr(FstProcess,'ask',blocked)
    if stop=='fst_timeout':monkeypatch.setattr(fst_runtime,'REQUEST_TIMEOUT_SEC',1)
    async def request():
        import json
        response=await server.call_tool('verify_window',dict(mode='property',wave_path=str(path),
            property_plan=handle,failure_event=dict(time_ps=35,instance_path='top.ap'),
            timeout_sec=1 if stop=='property_timeout' else 10))
        results.append(json.loads(response[0].text))
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            assert await anyio.to_thread.run_sync(entered.wait,2)
            if stop=='cancel':group.cancel_scope.cancel()
        while any(child.poll() is None or directory.exists() for _,child,directory in workers):
            await anyio.sleep(.01)
    assert workers
    if stop!='cancel':assert results[0]['error_code']==stop,results
    parser=server._get_parser(str(path))
    assert not parser._native_sessions and parser._active_batch is None


@pytest.mark.anyio
async def test_fst_plan_and_replay_over_stdio(tmp_path):
    import json,os,sys
    from pathlib import Path
    import anyio
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    path,_=cycle_pair(tmp_path,dict(req=1,ack=1),[
        dict(req=1,ack=0),dict(req=0,ack=1),dict(req=0,ack=0),dict(req=0,ack=0)])
    params=StdioServerParameters(command=sys.executable,args=[str(Path(server.__file__).resolve())],
        env={**os.environ,'TRACEWEAVE_TELEMETRY':'0'})
    with anyio.fail_after(30):
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                explained=await client.call_tool('explain_property',dict(text='req |-> ##3 ack',unit='u',
                    units=[dict(id='u',declarations='logic clk,req,ack;',default_clock='posedge clk')],
                    detail='summary',include_check_plan=True))
                handle=json.loads(explained.content[0].text)['results'][0]['check_plan']['handle']
                checked=await client.call_tool('verify_window',dict(mode='property',wave_path=str(path),
                    property_plan=handle,failure_event=dict(time_ps=35,instance_path='top.ap')))
                result=json.loads(checked.content[0].text)
                assert not checked.isError and result['status']=='failure_reproduced',result
                assert result['attempts'][0]['start_time_ps']==5
                assert result['attempts'][0]['end_time_fs']==35000
