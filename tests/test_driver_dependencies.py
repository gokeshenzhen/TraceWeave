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
    assert calls[-1]['include_dependency_candidates'] is True
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


def bus(name, bits, declared):
    return asdict(Expr('signal', len(bits), signal='t.'+name,
                      bits=tuple(bits), declared_bits=tuple(declared)))


def combinational(*inputs):
    return dict(backend='source_graph', boundary='combinational', complete=False,
                data_inputs=list(inputs), branches=[], gaps=['coverage_inconclusive'])


@pytest.mark.parametrize('declared', [tuple(range(1,65)), tuple(range(71,7,-1))])
def test_permutation_single_bits_normalize_and_sample_exactly(tmp_path, declared):
    from src.waveform_selection import SelectionParser
    p = dump(tmp_path, '$scope module t $end\n'
             f'$var wire 64 ! fp [{declared[0]}:{declared[-1]}] $end\n$upscope $end')
    # Include known and four-state data to check actual projections, not paths.
    value = '10xz' * 16
    with open(p.file_path, 'a') as f:
        f.write('#10\nb'+value+' !\n')
    order = declared[::2] + declared[1::2]
    result = inventory(combinational(*(bus('fp', [b], declared) for b in order)), p)
    assert len(result['dependencies']) == 1 and not result['truncated']
    row = result['dependencies'][0]
    assert row['source']['bits'] == list(order) == row['wave']['bits']
    assert row['roles'] == ['data'] and result['gaps'] == ['coverage_inconclusive']
    assert not result['complete']
    selection = SelectionParser(p)
    key = selection.bind(row['wave'])
    merged = selection.get_value_at_time(key, 10)['value']['bin']
    single = ''.join(selection.get_value_at_time(
        selection.bind(dict(path=row['wave']['path'], bits=[b])), 10)['value']['bin'] for b in order)
    assert merged == single == ''.join(value[declared.index(b)] for b in order)


def test_sparse_overlap_and_roles_do_not_widen_or_bleed(tmp_path):
    declared = tuple(range(7,-1,-1))
    p = dump(tmp_path, '$scope module t $end\n$var wire 8 ! fp [7:0] $end\n$upscope $end')
    step = combinational(bus('fp', [7,3], declared), bus('fp', [3,1], declared))
    step.update(clock=dict(expression=bus('fp', [5], declared), edge='posedge'),
                async_controls=[dict(kind='reset', expression=bus('fp', [0], declared), active_value='0'),
                                dict(kind='reset', expression=bus('fp', [0], declared), active_value='1')],
                branches=[dict(guard=bus('fp', [3], declared), value=bus('fp', [2], declared))],
                state=bus('fp', [1], declared))
    rows = inventory(step,p)['dependencies']
    assert any(r['roles']==['data'] and r['source']['bits']==[7,3,2] for r in rows)
    assert any(r['roles']==['control','data'] and r['source']['bits']==[3] for r in rows)
    assert any(r['roles']==['data','feedback'] and r['source']['bits']==[1] for r in rows)
    assert {r['active_value'] for r in rows if r['roles']==['reset']} == {'0','1'}
    assert all(6 not in r.get('source',r['wave'])['bits'] for r in rows)


def test_distinct_source_aliases_and_nonidentity_mapping_stay_separate(tmp_path,monkeypatch):
    import src.driver_dependencies as dependencies
    p, _ = fixture(tmp_path)
    def bind(parser, expr, *args, **kwargs):
        return Expr('signal', expr.width, signal='t.shared[3:0]',
                    bits=tuple(b+2 if expr.signal=='t.a' else b for b in expr.bits),
                    declared_bits=(3,2,1,0))
    monkeypatch.setattr(dependencies,'bind_source_expression',bind)
    rows = inventory(combinational(bus('a',[0],(1,0)),bus('a',[1],(1,0)),
                                  bus('b',[0],(1,0)),bus('b',[1],(1,0))),p)['dependencies']
    assert len(rows)==3
    assert [(r['source']['bits'],r['wave']['bits']) for r in rows if r['source']['path']=='t.a']==[([0],[2]),([1],[3])]
    assert next(r for r in rows if r['source']['path']=='t.b')['wave']['bits']==[0,1]


def test_unresolved_shapes_and_missing_dump_remain_explicit(tmp_path):
    p = dump(tmp_path, '$scope module t $end\n$var wire 8 ! fp [7:0] $end\n$upscope $end')
    result=inventory(combinational(bus('fp',[7],tuple(range(8))),
                                  bus('fp',[7],tuple(range(7,-1,-1))),
                                  bus('absent',[2],(3,2,1,0))),p)
    assert len(result['dependencies'])==3
    assert {r['binding'] for r in result['dependencies']}=={'bound','unresolved','not_dumped'}
    assert {'dependency_binding_unresolved','dependency_not_dumped'} <= set(result['gaps'])


def test_node_output_deadline_and_cancellation_bounds(tmp_path,monkeypatch):
    import src.driver_dependencies as dependencies
    from src.cancellation import OperationCancelled
    p,step=fixture(tmp_path)
    step['data_inputs']=[signal('d')]*300
    result=inventory(step,p)
    assert result['truncated'] and 'dependency_budget_exhausted' in result['gaps']
    assert inventory(step,p,deadline=time.monotonic()-1)['truncated']
    monkeypatch.setitem(dependencies.LIMITS,'output_bytes',2300)
    result=inventory(step,p)
    assert result['truncated'] and 'dependency_output_limit' in result['gaps']
    def cancel(): raise OperationCancelled()
    monkeypatch.setattr(dependencies,'check_cancelled',cancel)
    with pytest.raises(OperationCancelled): inventory(step,p)


def test_bound_independent_candidates_still_truncate_at_32(tmp_path):
    p = dump(tmp_path, '$scope module t $end\n'+'\n'.join(
        f'$var wire 1 s{i} n{i} $end' for i in range(40))+'\n$upscope $end')
    result=inventory(combinational(*(signal('n'+str(i)) for i in range(40))),p)
    assert len(result['dependencies'])==32 and all(r['binding']=='bound' for r in result['dependencies'])
    assert result['truncated'] and 'dependency_selection_limit' in result['gaps']


def test_cancellation_during_binding_propagates(tmp_path,monkeypatch):
    import src.driver_dependencies as dependencies
    from src.cancellation import OperationCancelled
    p,step=fixture(tmp_path)
    def cancel(*args,**kwargs): raise OperationCancelled('during normalization')
    monkeypatch.setattr(dependencies,'bind_source_expression',cancel)
    with pytest.raises(OperationCancelled): inventory(step,p)
