"""Public static-plan to waveform checks, with independent VCD/log fixtures."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
import server
from tests.test_window_verify import _build_vcd, _RA_SIGS


@pytest.fixture(autouse=True)
def reset():
    server.reset_session_state()
    yield
    server.reset_session_state()


async def plan(text='req |-> ##3 ack',*,declarations='logic clk,req,ack;',**extra):
    result=await server._dispatch('explain_property',dict(text=text,unit='u',
        units=[dict(id='u',declarations=declarations,default_clock='posedge clk')],
        include_check_plan=True,detail='summary',**extra))
    assert result.results[0].check_plan['status']=='prepared',result
    return result.results[0].check_plan['handle']


def wave(tmp_path,cycles=None):
    path=tmp_path/'wave.vcd'
    path.write_text(_build_vcd(cycles or [dict(req=1,ack=0),dict(req=1,ack=0),
        dict(req=0,ack=1),dict(req=0,ack=0),dict(req=0,ack=0)],_RA_SIGS))
    return str(path)


@pytest.mark.anyio
async def test_first_failure_drives_attempt_history_and_infers_scope(tmp_path):
    handle=await plan();path=wave(tmp_path)
    args=dict(mode='property',wave_path=path,property_plan=handle,
              failure_event=dict(time_ps=35,instance_path='top.ap'))
    tool=next(t for t in await server.list_tools() if t.name=='verify_window')
    Draft202012Validator(tool.inputSchema).validate(args)
    result=await server._dispatch('verify_window',args)
    assert result.status=='failure_reproduced',result
    assert result.coverage_status=='complete'
    assert result.attempt_selection['checked_count']==1
    assert result.attempt_selection['basis']=='response_deadline'
    assert result.attempts[0]['start_time_ps']==5 and result.attempts[0]['end_time_ps']==35
    assert result.sampling['phase']=='before'
    assert any(e['value']['bin']=='0' for e in result.attempts[0]['evidence'] if e['expression']=='ack')


@pytest.mark.anyio
async def test_log_selects_first_failure_of_exact_source_instance(tmp_path):
    source=tmp_path/'top.sv'
    source.write_text('module top; logic clk,req,ack; ap: assert property(@(posedge clk) req |-> ##3 ack); endmodule')
    explained=await server._dispatch('explain_property',dict(source_file=str(source),target='top.ap',include_check_plan=True))
    handle=explained.results[0].check_plan['handle']
    log=tmp_path/'sim.log'
    log.write_text(f'"{source}", 1: top.other: started at 0ps failed at 5ps\n'
                   f'"{source}", 1: top.ap: started at 5ps failed at 35ps\n'
                   f'"{source}", 1: top.ap: started at 15ps failed at 45ps\n')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path),
        property_plan=handle,log_path=str(log),simulator='vcs'))
    assert result.status=='failure_reproduced',result
    assert result.failure_event['time_ps']==35
    assert result.failure_event['selection_basis']=='first_target_failure_from_log'
    assert result.attempt_selection['basis']=='logged_attempt_start'


@pytest.mark.anyio
async def test_stale_source_is_rejected_before_wave_io(tmp_path,monkeypatch):
    source=tmp_path/'top.sv';source.write_text('module top; logic clk,a,b; ap: assert property(@(posedge clk) a |=> b); endmodule')
    result=await server._dispatch('explain_property',dict(source_file=str(source),target='top.ap',include_check_plan=True))
    handle=result.results[0].check_plan['handle']
    source.write_text(source.read_text()+'\n// changed')
    def forbidden(*args):raise AssertionError('stale source touched wave')
    monkeypatch.setattr(server,'_get_parser',forbidden)
    result=await server._dispatch('verify_window',dict(mode='property',wave_path='unused.vcd',property_plan=handle,failure_time_ps=15))
    assert result.error_code=='property_source_identity_changed'


@pytest.mark.anyio
async def test_wave_changed_before_publication_discards_result(tmp_path,monkeypatch):
    import src.property_check as runtime
    path=wave(tmp_path);handle=await plan();original=runtime.check_property
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        with open(path,'a') as out:out.write('\n#100\n')
        return result
    monkeypatch.setattr(runtime,'check_property',changed)
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,property_plan=handle,
        failure_time_ps=35,property_scope='top'))
    assert result.error_code=='property_wave_identity_changed' and not result.attempts


@pytest.mark.anyio
async def test_scope_and_fixed_selection_bindings(tmp_path):
    handle=await plan()
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path),property_plan=handle,
        failure_time_ps=35,property_bindings={'clk':{'path':'top.clk','bits':[0]},
            'req':'top.req','ack':{'path':'top.ack','bits':[0]}}))
    assert result.status=='failure_reproduced',result


@pytest.mark.anyio
async def test_matching_samples_do_not_become_a_global_pass(tmp_path):
    handle=await plan()
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path,[
        dict(req=1,ack=0),dict(req=0,ack=0),dict(req=0,ack=0),dict(req=0,ack=1)]),
        property_plan=handle,failure_time_ps=35,property_scope='top'))
    assert result.status=='not_reproduced' and result.reproduced_count==0
    assert result.attempts[0]['status']=='true'


@pytest.mark.anyio
async def test_public_sub_ps_clock_collision_remains_ambiguous(tmp_path):
    path=tmp_path/'sub.vcd';path.write_text('''$timescale 100fs $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 # req $end
$var wire 1 % ack $end
$upscope $end
$enddefinitions $end
#0
0!
1#
0%
#2
1!
#3
0!
#4
1!
''')
    handle=await plan('req |-> ack')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_time_ps=1,property_scope='top'))
    assert result.error_code=='property_failure_clock_ambiguous'


@pytest.mark.anyio
async def test_expired_plan_and_legacy_parameter_mixing_are_explicit(tmp_path):
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path),property_plan='expired',failure_time_ps=35))
    assert result.error_code=='property_plan_expired'
    handle=await plan()
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path),property_plan=handle,
        failure_time_ps=35,within_cycles=3,property_scope='top'))
    assert result.error_code=='property_check_arguments_invalid'


@pytest.mark.anyio
async def test_between_edge_reset_is_read_from_waveform(tmp_path):
    signals=_RA_SIGS+[('&','rst',1)]
    vcd=_build_vcd([dict(req=1,ack=0,rst=0),dict(req=0,ack=0,rst=0)],signals)
    vcd=vcd.replace('#10\n','#7\n1&\n#8\n0&\n#10\n')
    path=tmp_path/'reset.vcd';path.write_text(vcd)
    handle=await plan('disable iff(rst) (req |=> ack)',declarations='logic clk,req,ack,rst;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_event=dict(time_ps=15,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='not_reproduced',result
    assert result.attempts[0]['status']=='disabled'


@pytest.mark.anyio
async def test_output_limit_trims_display_independently(tmp_path):
    handle=await plan('(req ##[1:3] ack) |=> req')
    path=wave(tmp_path,[dict(req=1,ack=0),dict(req=0,ack=1),dict(req=0,ack=1),dict(req=0,ack=1),dict(req=0,ack=0)])
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,property_plan=handle,
        failure_event=dict(time_ps=25,instance_path='top.ap',structured_fields={'start_time_ps':5}),max_output_bytes=4096))
    assert result.status=='failure_reproduced',result
    assert len(result.model_dump_json().encode())<=4096


@pytest.mark.anyio
async def test_missing_binding_and_outside_capture_are_inconclusive(tmp_path):
    handle=await plan();path=wave(tmp_path)
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,property_plan=handle,
        failure_time_ps=35,property_scope='missing'))
    assert result.error_code=='property_signal_unavailable'
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,property_plan=handle,
        failure_time_ps=55,property_scope='top'))
    assert result.status=='insufficient_evidence' and result.reproduced_count==0


@pytest.mark.anyio
async def test_public_stdio_plan_and_execution(tmp_path):
    import os,sys
    import anyio
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from src.schemas import PropertyCheckResult
    path=wave(tmp_path)
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
                checked=await client.call_tool('verify_window',dict(mode='property',wave_path=path,
                    property_plan=handle,failure_event=dict(time_ps=35,instance_path='top.ap')))
                assert not checked.isError
                result=PropertyCheckResult.model_validate_json(checked.content[0].text)
                assert result.status=='failure_reproduced' and result.attempts[0]['start_time_ps']==5


@pytest.mark.anyio
async def test_gated_history_is_read_before_the_attempt_window(tmp_path):
    signals=[('!','clk',1),('#','req',1),('%','data',4)]
    path=tmp_path/'history.vcd';path.write_text(_build_vcd([
        dict(req=1,data=3),dict(req=0,data=9),dict(req=1,data=4),dict(req=1,data=3),dict(req=1,data=8)],signals))
    handle=await plan('req |-> data==$past(data,2,req)',declarations='logic clk,req; logic [3:0] data;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_time_ps=45,property_scope='top'))
    assert result.status=='failure_reproduced',result
    assert result.sampling['history_start_ps']==0


@pytest.mark.anyio
async def test_sampled_disable_does_not_use_between_edge_raw_reset(tmp_path):
    signals=_RA_SIGS+[('&','rst',1)]
    vcd=_build_vcd([dict(req=1,ack=0,rst=0),dict(req=0,ack=0,rst=0)],signals)
    path=tmp_path/'sampled-reset.vcd';path.write_text(vcd.replace('#10\n','#7\n1&\n#8\n0&\n#10\n'))
    handle=await plan('disable iff($sampled(rst)) (req |=> ack)',declarations='logic clk,req,ack,rst;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_event=dict(time_ps=15,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='failure_reproduced',result


@pytest.mark.anyio
async def test_property_only_controls_and_unvalidated_format_are_explicit(tmp_path):
    result=await server._dispatch('verify_window',dict(mode='always',wave_path=wave(tmp_path),
        clock='top.clk',property_plan='unused',predicate=[dict(signal='top.req',op='eq',value=1)]))
    assert result.error_code=='property_options_require_property_mode'
    result=await server._dispatch('verify_window',dict(mode='property',wave_path='unused.fst',
        property_plan='unused',failure_time_ps=35))
    assert result.status=='unsupported' and result.error_code=='property_fst_not_validated'


@pytest.mark.anyio
async def test_cancelled_property_waiter_does_not_touch_parser(tmp_path,monkeypatch):
    import anyio,threading
    path=wave(tmp_path);handle=await plan()
    started=threading.Event();release=threading.Event();calls=[]
    def hold():
        started.set();release.wait(timeout=5)
    def parser(*args):calls.append(args);raise AssertionError('cancelled waiter touched parser')
    monkeypatch.setattr(server,'_get_parser',parser)
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(server._run_in_wave_thread,path,hold)
            assert await anyio.to_thread.run_sync(started.wait,2)
            with anyio.move_on_after(.2):
                await server._dispatch('verify_window',dict(mode='property',wave_path=path,
                    property_plan=handle,failure_time_ps=35,property_scope='top'))
            release.set()
    finally:release.set()
    assert calls==[]
