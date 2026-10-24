from dataclasses import replace
import asyncio
import json
import os
import sys
import time

import anyio
import pytest

from config import get_source_graph_execution_config
from src.property_model import ExplainPropertyOptions, ExplainPropertyResult
from src.property_runtime import PropertyRunRegistry, explain_properties, run_worker
import src.property_runtime as runtime

UNIT={'id':'u','declarations':'bit clk,a,b;','default_clock':'posedge clk'}


def test_summary_limits_warning_details_without_hiding_errors_or_status():
    from copy import deepcopy
    from src.property_records import explanation_record
    row=explanation_record('warnings')
    row.update(semantic_status='partial',target=dict(source=dict(file='target.sv',line=100)))
    errors=[dict(severity='error',message='binding error',file='other.sv',line=1)]
    warnings=[dict(severity='warning',message=f'unrelated {i}',file='other.sv',line=i) for i in range(20)]
    warnings.append(dict(severity='warning',message='selected expression warning',file='target.sv',line=99))
    row['diagnostics']=[*errors,*warnings]
    result=dict(results=[deepcopy(row)],output_truncated=False)
    summary=runtime._trim(result,ExplainPropertyOptions(text='1',detail='summary'))['results'][0]
    assert summary['semantic_status']=='partial'
    assert summary['diagnostics'][0]==errors[0]
    assert len(summary['diagnostics'])==9
    assert summary['diagnostics'][1]['message']=='selected expression warning'
    assert summary['diagnostics_truncated'] and summary['output_truncated']
    full=runtime._trim(dict(results=[deepcopy(row)],output_truncated=False),ExplainPropertyOptions(text='1'))['results'][0]
    assert full['diagnostics']==row['diagnostics']


async def run(args, **kwargs):
    return await explain_properties(args,registry=kwargs.pop('registry',PropertyRunRegistry()),
        config=kwargs.pop('config',get_source_graph_execution_config()),run_thread=asyncio.to_thread,**kwargs)


@pytest.mark.anyio
async def test_mixed_batch_preserves_order_ids_and_independent_shared_units():
    pytest.importorskip('pyslang')
    r=await run({'items':[{'id':'good','text':'p','unit':'u'},
                         {'id':'bad','text':'a |=> ## b','unit':'u'},
                         {'id':'other','text':'p','unit':'v'}],
        'units':[{**UNIT,'declarations':UNIT['declarations']+' property p; a |=> b; endproperty'},
                 {**UNIT,'id':'v','declarations':UNIT['declarations']+' property p; a |-> b; endproperty'}]})
    assert [x.id for x in r.results]==['good','bad','other']
    assert [x.semantic_status for x in r.results]==['complete','partial','complete']
    assert r.results[1].syntax_status=='invalid'
    assert all(x.mode['value']=='unknown' for x in r.results)
    assert r.results[0].identity['input_sha256']!=r.results[2].identity['input_sha256']


@pytest.mark.anyio
async def test_missing_shared_definition_does_not_remove_other_unit_results():
    pytest.importorskip('pyslang')
    r=await run({'items':[{'id':'missing','text':'p','unit':'u'}, {'id':'valid','text':'a |=> b','unit':'v'}],
                 'units':[UNIT,{**UNIT,'id':'v'}]})
    assert r.results[0].semantic_status!='complete' and r.results[0].gaps
    assert r.results[1].semantic_status=='complete'
    r=await run({'text':'a |=> b','unit':'not_here'})
    assert r.results[0].gaps[0].code=='shared_unit_not_found'


@pytest.mark.anyio
async def test_single_and_batch_produce_identical_explanations():
    pytest.importorskip('pyslang')
    item={'id':'p','text':'a |=> b','unit':'u'}
    a=await run({**item,'units':[UNIT]})
    b=await run({'items':[item],'units':[UNIT]})
    assert a.results[0]==b.results[0]


def test_mode_registry_retains_independent_runs_and_never_guesses_latest(tmp_path):
    sim=tmp_path/'sim.log'; sim.write_text('one')
    formal=tmp_path/'formal.log'; formal.write_text('one')
    second=tmp_path/'second.log';second.write_text('two')
    registry=PropertyRunRegistry()
    registry.note('simulation',{'sim_logs':[{'path':str(sim)}]})
    registry.note('formal',{'formal_logs':[{'path':str(formal)}]})
    registry.note('simulation',{'sim_logs':[{'path':str(second)}]})
    assert registry.resolve('auto',None)['value']=='unknown'
    assert registry.resolve('auto',str(sim))['value']=='simulation'
    assert registry.resolve('auto',str(formal))['value']=='formal'
    sim.write_text('changed')
    assert registry.resolve('auto',str(sim))['basis']=='discovery_artifacts_changed'
    registry.note('formal',{'formal_logs':[{'path':str(second)}]})
    assert registry.resolve('auto',str(second))['basis']=='ambiguous_run_association'


@pytest.mark.anyio
async def test_assume_roles_keep_simulation_truth_and_formal_evidence_separate():
    pytest.importorskip('pyslang')
    for mode in ['simulation','formal','unknown']:
        r=await run({'text':'assume property(a |=> b);','units':[UNIT],'unit':'u','mode':mode,
                     'role_context':{'source':'cov.tcl:29','provenance':'script_declaration','role':'assume','enabled':True}})
        d=r.results[0].directive
        assert d['conditional_roles']['simulation']=='trajectory_property_check'
        assert d['conditional_roles']['formal']=='constraint_if_enabled'
        assert d['actual_formal_role']=='not_confirmed'
        assert d['role_context_status']=='script_declaration_not_execution'
        assert d['proof_status']=='not_observed'


@pytest.mark.anyio
async def test_total_timeout_preserves_finished_items_without_new_worker(monkeypatch):
    launches=[]
    async def fake(payload,python_bin,deadline):
        launches.append(payload['item']['id'])
        if len(launches)==2:
            await asyncio.sleep(1)
        from src.property_model import PropertyExplanation
        return PropertyExplanation(id=payload['item']['id'],semantic_status='complete').model_dump()
    monkeypatch.setattr(runtime,'run_worker',fake)
    r=await run({'items':[{'id':str(i),'text':'1'} for i in range(3)],'timeout_sec':.1})
    assert launches==['0','1']
    assert r.results[0].semantic_status=='complete'
    assert all(x.gaps[0].code=='total_timeout' for x in r.results[1:])


@pytest.mark.anyio
async def test_real_worker_timeout_and_unavailable_frontend_are_structured(tmp_path):
    r=await run({'text':'1','timeout_sec':.001})
    assert r.results[0].gaps[0].code=='total_timeout'
    cfg=replace(get_source_graph_execution_config(),python_bin='/does/not/exist')
    r=await run({'text':'1'},config=cfg)
    assert r.results[0].semantic_status=='not_run'
    # -S denies site imports, including optional pyslang, using an executable wrapper.
    wrapper=tmp_path/'python-no-site'
    wrapper.write_text('#!/bin/sh\nexec '+sys.executable+' -S "$@"\n');wrapper.chmod(0o700)
    r=await run({'text':'1'},config=replace(cfg,python_bin=str(wrapper)))
    assert r.results[0].semantic_status=='not_run'


@pytest.mark.anyio
async def test_cancelled_waiter_never_launches_or_uses_wave_locks(monkeypatch):
    from src.source_graph_runtime import _PROCESS_COLD_BUILD_LOCK
    launches=[]
    async def launch(*args,**kwargs): launches.append(args);raise AssertionError('must not launch')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    assert _PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
    try:
        task=asyncio.create_task(run_worker({'item':{'id':'p'}},sys.executable,time.monotonic()+10))
        await asyncio.sleep(.02);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not launches
    finally:_PROCESS_COLD_BUILD_LOCK.release()


@pytest.mark.anyio
async def test_active_worker_cancellation_terminates_child_and_releases_admission(monkeypatch,tmp_path):
    real=asyncio.create_subprocess_exec;children=[]
    async def launch(*args,**kwargs):
        p=await real(sys.executable,'-c','import time; time.sleep(60)',**kwargs)
        children.append(p);return p
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    task=asyncio.create_task(run_worker({'item':{'id':'p'}},sys.executable,time.monotonic()+10))
    while not children:await asyncio.sleep(.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert children[0].returncode is not None
    from src.source_graph_runtime import _PROCESS_COLD_BUILD_LOCK
    assert _PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
    _PROCESS_COLD_BUILD_LOCK.release()


@pytest.mark.anyio
@pytest.mark.parametrize('cancel_during',['spawn','cleanup'])
async def test_cancellation_at_lifecycle_boundaries_keeps_child_owned(monkeypatch,cancel_during):
    from src.source_graph_runtime import _PROCESS_COLD_BUILD_LOCK
    real_launch=asyncio.create_subprocess_exec
    real_cleanup=runtime._terminate_process_group
    entered=asyncio.Event();children=[]
    async def launch(*args,**kwargs):
        p=await real_launch(sys.executable,'-c',
            'import sys; sys.stdin.read(); print("{}")' if cancel_during=='cleanup' else 'import time; time.sleep(60)',**kwargs)
        children.append(p)
        if cancel_during=='spawn':
            entered.set();await asyncio.sleep(.03)
        return p
    async def cleanup(p):
        if cancel_during=='cleanup':
            entered.set();await asyncio.sleep(.03)
        await real_cleanup(p)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    monkeypatch.setattr(runtime,'_terminate_process_group',cleanup)
    task=asyncio.create_task(run_worker({'item':{'id':'p'}},sys.executable,time.monotonic()+5))
    await asyncio.wait_for(entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert all(p.returncode is not None for p in children)
    assert _PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
    _PROCESS_COLD_BUILD_LOCK.release()


@pytest.mark.anyio
async def test_anyio_scope_cancellation_reaps_worker(monkeypatch):
    from src.source_graph_runtime import _PROCESS_COLD_BUILD_LOCK
    real=asyncio.create_subprocess_exec;children=[]
    async def launch(*args,**kwargs):
        p=await real(sys.executable,'-c','import time; time.sleep(60)',**kwargs)
        children.append(p);return p
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    with anyio.move_on_after(.05) as scope:
        await run_worker({'item':{'id':'p'}},sys.executable,time.monotonic()+10)
    assert scope.cancel_called and children[0].returncode is not None
    assert _PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
    _PROCESS_COLD_BUILD_LOCK.release()


@pytest.mark.anyio
async def test_output_trim_is_distinct_from_analysis_limit():
    pytest.importorskip('pyslang')
    r=await run({'text':'a |=> b ##1 a ##1 b','unit':'u','units':[UNIT],'detail':'summary'})
    assert r.results[0].semantic_status=='complete'
    assert r.results[0].output_truncated and not r.results[0].analysis_limited
    assert not r.results[0].nodes and r.results[0].node_count>0
    r=await run({'text':'a |=> b ##1 a ##1 b','unit':'u','units':[UNIT],'max_output_bytes':4096})
    assert len(json.dumps(r.model_dump(),separators=(',',':')).encode())<=4096


@pytest.mark.anyio
async def test_summary_mode_retains_concrete_alignment_rule_after_graph_is_omitted():
    pytest.importorskip('pyslang')
    r=await run({'text':'a |-> ((1 ##1 b) and (1 ##3 a))','unit':'u','units':[UNIT],'detail':'summary'})
    item=r.results[0]
    assert item.semantic_status=='complete' and not item.nodes
    assert item.output_truncated and not item.analysis_limited
    assert any('1 ##1 b' in s and '1 ##3 a' in s and 'requires_same_end=false' in s for s in item.summary)


@pytest.mark.anyio
async def test_summary_mode_retains_repetition_negation_and_sampled_history_without_nodes():
    pytest.importorskip('pyslang')
    r=await run({'text':'not (a[->3] ##1 $fell(b))','unit':'u',
                 'units':[{**UNIT,'declarations':'logic clk,a,b;'}],'detail':'summary'})
    item=r.results[0]
    assert item.semantic_status=='complete'
    assert item.output_truncated and item.nodes==[]
    assert any('Negate' in s for s in item.summary)
    assert any('count 3' in s for s in item.summary)
    assert any("1'bx" in s and '$fell:' in s for s in item.summary)


@pytest.mark.anyio
async def test_summary_mode_exposes_completion_policy_without_claiming_a_runtime_verdict():
    pytest.importorskip('pyslang')
    r=await run({'text':'initial_assert: assert property(a[->2]);','unit':'u','units':[UNIT],'detail':'summary'})
    item=r.results[0]
    assert item.semantic_status=='complete' and not item.nodes
    assert item.directive['completion_policy']['default_sequence_strength']=='weak'
    assert item.directive['completion_policy']['runtime_outcome']=='not_evaluated'
    assert any('THAT still-extendible weak attempt' in s for s in item.summary)


@pytest.mark.anyio
async def test_source_binding_and_changed_input_do_not_advance_fallback(tmp_path,monkeypatch):
    pytest.importorskip('pyslang')
    source=tmp_path/'top.sv';source.write_text('module top; bit clk,a,b; ap: assert property(@(posedge clk) a |=> b); endmodule')
    r=await run({'source_file':str(source),'target':'ap'})
    assert r.results[0].semantic_status=='complete',r.results[0].gaps
    original=runtime.run_worker
    async def mutate(*args,**kwargs):
        result=await original(*args,**kwargs)
        source.write_text(source.read_text()+'\n// changed\n')
        return result
    monkeypatch.setattr(runtime,'run_worker',mutate)
    r=await run({'source_file':str(source),'target':'ap'})
    assert r.results[0].gaps[0].code=='source_identity_changed'
    assert not r.results[0].nodes


def test_contract_rejects_duplicate_ids_and_nonfinite_deadlines():
    with pytest.raises(ValueError):ExplainPropertyOptions(items=[{'id':'x','text':'1'},{'id':'x','text':'0'}])
    with pytest.raises(ValueError):ExplainPropertyOptions(text='1',timeout_sec=float('nan'))
    with pytest.raises(ValueError):ExplainPropertyOptions(text='1',items=[{'id':'x','text':'0'}])
