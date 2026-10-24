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
    assert result.failure_event['start_time_ps']==5


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
    assert all('@bits(' not in v['signal'] and 'bits' not in v
               for v in result.attempts[0]['sampled_values'])


@pytest.mark.anyio
async def test_sampled_operands_retain_reordered_dump_selection(tmp_path):
    path=tmp_path/'selected.vcd'
    path.write_text(_build_vcd([dict(bus=10)],[('!','clk',1),('#','bus',4)]))
    handle=await plan('data==3',declarations='logic clk; logic [1:0] data;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_event=dict(time_ps=5,instance_path='top.ap',structured_fields={'start_time_ps':5}),
        property_bindings={'data':dict(path='top.bus',bits=[1,3])}))
    assert result.status=='not_reproduced',result
    observed=result.attempts[0]['sampled_values']
    assert len(observed)==1
    assert observed[0]['signal']=='top.bus' and observed[0]['bits']==[1,3]
    assert observed[0]['value']['bin']=='11' and observed[0]['time_fs']==5000


@pytest.mark.anyio
async def test_missing_history_is_not_a_missing_dump_signal(tmp_path):
    handle=await plan('req |-> $past(ack)')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path),property_plan=handle,
        failure_event=dict(time_ps=5,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='insufficient_evidence'
    assert 'history_before_capture_unavailable' in result.gap_codes
    assert 'property_history_value_unavailable' in result.gap_codes
    assert 'signal_not_dumped' not in result.gap_codes
    assert {v['signal'] for v in result.attempts[0]['sampled_values']}=={'top.req','top.ack'}


@pytest.mark.anyio
async def test_pending_cutoff_suggests_bounded_samples_without_changing_verdict(tmp_path):
    handle=await plan('req |=> ack');path=wave(tmp_path)
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,property_plan=handle,
        failure_event=dict(time_ps=5,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='insufficient_evidence' and result.attempts[0]['status']=='pending'
    assert result.sampling['read_end_ps']==5
    assert 'property_pending_at_report_cutoff' in result.gap_codes
    action=result.suggested_next_action
    tool=next(t for t in await server.list_tools() if t.name==action['tool'])
    Draft202012Validator(tool.inputSchema).validate(action['arguments'])
    observed=await server._dispatch(action['tool'],action['arguments'])
    assert not getattr(observed,'error_code',None),observed
    assert action['arguments']['sample_phase']=='before' and action['arguments']['num_cycles']<=256


@pytest.mark.anyio
async def test_sampled_value_display_trims_before_dropping_attempts(tmp_path):
    handle=await plan('req[*30]')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path,[dict(req=1,ack=0)]*30),
        property_plan=handle,max_output_bytes=4096,
        failure_event=dict(time_ps=295,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='not_reproduced' and result.coverage_status=='complete',result
    assert len(result.attempts)==1 and not result.attempts_truncated
    assert result.output_truncated and result.attempts[0]['sampled_values_truncated']
    assert len(result.model_dump_json().encode())<=4096


@pytest.mark.anyio
async def test_constant_property_exposes_an_actual_clock_sample_for_reference_assembly(tmp_path):
    from src.property_evidence import resolve_property_evidence
    handle=await plan("1'b0",declarations='logic clk;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=wave(tmp_path),property_plan=handle,
        failure_event=dict(time_ps=5,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='failure_reproduced',result
    clock=result.attempts[0]['clock_sample']
    assert clock['signal']=='top.clk' and clock['value']['bin']=='0'
    refs=dict(check_id=result.evidence_context['check_id'],signal_evidence_ids=[clock['evidence_id']],
              expression_evidence_ids=[result.attempts[0]['evidence'][0]['evidence_id']])
    assembled=resolve_property_evidence(result,refs)
    assert assembled['signal_facts'][0]['sample_phase']=='before'
    assert assembled['expression_facts'][0]['value']['bin']=='0'
    assert 'signal' not in assembled['expression_facts'][0]


@pytest.mark.anyio
async def test_answer_assembly_preserves_tool_verdict_against_reversed_model_json(tmp_path):
    from src.property_verdict import assemble_property_answer
    handle=await plan();path=wave(tmp_path)
    event=dict(time_ps=35,instance_path='top.ap',structured_fields={'start_time_ps':5})
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,
        property_plan=handle,failure_event=event))
    sample=result.attempts[0]['sampled_values'][0]
    references=dict(check_id=result.evidence_context['check_id'],
                    signal_evidence_ids=[sample['evidence_id']],expression_evidence_ids=[])
    answer=assemble_property_answer(result,dict(verdict='not_reproduced',attempt_start_ps=5,
        report_time_ps=35,reason='model says not reproduced'),wave_path=path,
        failure_event=event,plan_fingerprint=result.plan['fingerprint'],references=references)
    assert answer['verdict']=='reproduced' and answer['proposal_conflicts']==['verdict']
    assert answer['resolved_evidence']['signal_facts'][0]['value']==sample['value']


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
@pytest.mark.parametrize('condition,disabled_at',[
    ('rst',7),('$sampled(rst)',8),('$sampled(rst) && !rst',8),('captured',None),
])
async def test_async_sampled_control_observes_time_slot_prestate(tmp_path,condition,disabled_at):
    # Native VCS: raw reset disables at its rise; $sampled(reset) sees the
    # previous value at its fall. A clocked register remains zero throughout.
    signals=_RA_SIGS+[('&','rst',1),('*','captured',1)]
    vcd=_build_vcd([dict(req=1,ack=0,rst=0,captured=0),dict(req=0,ack=0,rst=0,captured=0)],signals)
    path=tmp_path/'sampled-reset.vcd';path.write_text(vcd.replace('#10\n','#7\n1&\n#8\n0&\n#10\n'))
    handle=await plan(f'disable iff({condition}) (req |=> ack)',declarations='logic clk,req,ack,rst,captured;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_event=dict(time_ps=15,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    if disabled_at is None:
        assert result.status=='failure_reproduced',result
    else:
        assert result.status=='not_reproduced',result
        assert result.attempts[0]['status']=='disabled'
        assert result.attempts[0]['end_time_ps']==disabled_at


@pytest.mark.anyio
async def test_sampled_value_in_consequent_uses_assertion_clock(tmp_path):
    signals=_RA_SIGS+[('&','rst',1)]
    vcd=_build_vcd([dict(req=1,ack=0,rst=0),dict(req=0,ack=0,rst=0)],signals)
    path=tmp_path/'sampled-value.vcd';path.write_text(vcd.replace('#10\n','#7\n1&\n#8\n0&\n#10\n'))
    handle=await plan('req |=> $sampled(rst)',declarations='logic clk,req,ack,rst;')
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=str(path),property_plan=handle,
        failure_event=dict(time_ps=15,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='failure_reproduced',result


@pytest.mark.anyio
async def test_property_only_controls_require_property_mode(tmp_path):
    result=await server._dispatch('verify_window',dict(mode='always',wave_path=wave(tmp_path),
        clock='top.clk',property_plan='unused',predicate=[dict(signal='top.req',op='eq',value=1)]))
    assert result.error_code=='property_options_require_property_mode'


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


@pytest.mark.anyio
async def test_logged_attempt_correlates_an_earlier_semantic_failure(tmp_path):
    handle=await plan('req |-> ((req ##1 ack) intersect (req ##3 ack))')
    path=wave(tmp_path)
    result=await server._dispatch('verify_window',dict(mode='property',wave_path=path,property_plan=handle,
        failure_event=dict(time_ps=15,instance_path='top.ap',structured_fields={'start_time_ps':5})))
    assert result.status=='failure_reproduced',result
    attempt=result.attempts[0]
    assert attempt['end_time_ps']==5 and not attempt['report_timing_exact']
    assert attempt['report_alignment']=='same_logged_attempt'
