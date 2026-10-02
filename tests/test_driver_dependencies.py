from dataclasses import asdict
from types import SimpleNamespace
import time

import pytest

from src.dynamic_evidence import Expr, TRUE
from src.driver_dependencies import inventory
from tests.test_wave_design_binding import dump


def signal(name):
    return asdict(Expr('signal',1,signal='t.'+name,bits=(0,),declared_bits=(0,)))


def fixture(tmp_path):
    p = dump(tmp_path, '$scope module t $end\n'+'\n'.join(
        f'$var wire 1 {chr(33+i)} {name} $end' for i,name in enumerate(('clk','rst','en','d','q'))
    )+'\n$upscope $end')
    step=dict(backend='verdi_npi',boundary='sequential',complete=False,state=signal('q'),
        clock=dict(expression=signal('clk'),edge='posedge'),
        async_controls=[dict(kind='reset',expression=signal('rst'),active_value='0')],
        data_inputs=[signal('d')],
        branches=[dict(guard=signal('en'),value=signal('q'))],
        gaps=['async_control_value_unmodeled'])
    return p,step


def test_roles_do_not_claim_active_branch_or_async_assigned_value(tmp_path):
    p,step=fixture(tmp_path)
    result=inventory(step,p)
    rows={r['wave']['path']:r for r in result['dependencies']}
    assert rows['t.clk']['roles']==['clock']
    assert rows['t.rst']['roles']==['reset']
    assert rows['t.en']['roles']==['control']
    assert rows['t.d']['roles']==['data']
    assert rows['t.q']['roles']==['data','feedback']
    assert 'source' not in rows['t.d']
    assert not result['complete'] and result['active_branch']=='not_evaluated'
    assert 'literal_operands' not in result


def test_missing_dump_limits_and_deadline_are_explicit(tmp_path):
    p,step=fixture(tmp_path)
    step['data_inputs']=[signal('absent')]
    result=inventory(step,p)
    assert 'dependency_not_dumped' in result['gaps']
    assert any(r['binding']=='not_dumped' and r['source']['path']=='t.absent' for r in result['dependencies'])
    step['data_inputs']=[signal(str(i)) for i in range(40)]
    result=inventory(step,p)
    assert len(result['dependencies'])==32 and result['truncated']
    assert 'dependency_selection_limit' in result['gaps']
    assert inventory(step,p,deadline=time.monotonic()-1)['truncated']


@pytest.mark.anyio
async def test_public_attachment_defaults_off_and_cannot_change_backend(tmp_path,monkeypatch):
    import server
    p,step=fixture(tmp_path)
    calls=[]
    def query(**kw):
        calls.append(kw)
        return step
    backend=SimpleNamespace(name='verdi_npi',get_dynamic_step=query)
    args=dict(signal_path='t.q',compile_log='unused',wave_path=p.file_path)
    result=dict(backend='verdi_npi',driver_status='resolved')
    await server._attach_driver_dependencies(result,backend,args,'vcs')
    assert calls==[] and 'dependency_context' not in result
    monkeypatch.setattr(server,'_get_parser',lambda _:p)
    await server._attach_driver_dependencies(result,backend,{**args,'include_dependencies':True},'vcs')
    assert len(calls)==1 and result['dependency_context']['dependencies']
    step['backend']='source_graph'
    await server._attach_driver_dependencies(result,backend,{**args,'include_dependencies':True},'vcs')
    assert result['backend']=='verdi_npi'
    assert result['dependency_context']['gaps']==['dependency_backend_mismatch']


def test_unmodeled_operator_retains_candidates_without_evaluating(tmp_path):
    from src.dynamic_evidence import evaluate
    p,step=fixture(tmp_path)
    expr=Expr('unsupported',1,(Expr.from_dict(signal('d')),),reason='npi_operator_semantics_unresolved')
    step.update(boundary='combinational',clock=None,async_controls=[],data_inputs=[],
                branches=[dict(guard=asdict(TRUE),value=asdict(expr))],
                gaps=['npi_operator_semantics_unresolved'])
    result=inventory(step,p)
    assert result['dependencies'][0]['wave']['path']=='t.d'
    assert not result['complete']
    assert evaluate(expr,lambda _:dict(value='1')).value is None


def test_lsf_validates_added_data_pin_facts():
    from src.dynamic_evidence import validate_step
    raw=dict(version='2.0',backend='verdi_npi',boundary='unsupported',branches=[],data_inputs=[signal('d')])
    assert validate_step(raw)==raw
    with pytest.raises(ValueError):
        validate_step({**raw,'data_inputs':[signal('d'),signal('q')]})
