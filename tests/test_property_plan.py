"""Static plan binding, retained graph identity and bounded handle lifecycle."""
import pytest
from src.property_plan import PropertyPlanStore, PlanUnavailable, prepare_plan
from tests.test_property_semantics import complete


def planned(text,**kwargs):
    return complete(text,execution_plan=True,**kwargs)


def test_plan_uses_bound_typed_values_and_parameters():
    row=planned('a |-> ##N (data == 4\'h3)',declarations='parameter N=3;')
    plan,gaps=prepare_plan(row)
    assert not gaps
    assert set(plan['signals'])=={'__tw_property.clk','__tw_property.a','__tw_property.data'}
    assert plan['signals']['__tw_property.data']['width']==4
    assert any(j['delay']['min']==3 for n in plan['nodes'] for j in n['facts'].get('joins',[]))


def test_history_is_explicit_and_local_state_is_not_a_wave_dependency():
    row=planned('property p; logic [3:0] v; (a,v=data) |=> (data==v && $stable(data)); endproperty')
    plan,gaps=prepare_plan(row)
    assert not gaps
    assert len(plan['histories'])==1
    assert next(iter(plan['histories'].values()))['function']=='$stable'
    assert all(not k.endswith('.v') for k in plan['signals'])


@pytest.mark.parametrize('text',[
    'first_match(a ##[1:3] b) |=> c',
    'a throughout (b ##2 c)',
    '(a ##1 b) intersect (a ##[1:3] c)',
    '(a ##1 b) within (b ##3 c)',
    'disable iff(rst) (a |=> b)',
])
def test_complex_plan_preparation_keeps_temporal_graph(text):
    row=planned(text);plan,gaps=prepare_plan(row)
    assert not gaps
    assert plan['root']==row['root']
    assert len(plan['nodes'])==len(row['nodes'])


def test_store_is_immutable_bounded_and_expires():
    now=[0]
    store=PropertyPlanStore(ttl=10,max_entries=1,clock=lambda:now[0])
    row=planned('a |=> b')
    receipt=store.publish(row)
    entry=store.resolve(receipt['handle'])
    row['nodes'].clear()
    assert entry.plan['nodes']
    other=store.publish(planned('a |-> c'))
    with pytest.raises(PlanUnavailable):store.resolve(receipt['handle'])
    now[0]=11
    with pytest.raises(PlanUnavailable):store.resolve(other['handle'])
    assert store.size==0


def test_incomplete_or_unsupported_inputs_do_not_get_a_handle():
    row=planned('a |=> b')
    row['semantic_status']='partial'
    assert PropertyPlanStore().publish(row)['status']=='unavailable'
    row=planned('a |=> b')
    row['nodes'][0]['op']='unrecognized'
    receipt=PropertyPlanStore().publish(row)
    assert 'property_operator_unsupported:unrecognized' in receipt['gap_codes']


@pytest.mark.anyio
async def test_public_plan_survives_summary_trimming_without_wave_access(monkeypatch):
    import server
    server.reset_session_state()
    def forbidden(*args,**kwargs):raise AssertionError('static preparation opened a waveform')
    monkeypatch.setattr(server,'_get_parser',forbidden)
    args=dict(text='a |-> ##3 b',unit='u',units=[dict(id='u',declarations='logic clk,a,b;',default_clock='posedge clk')],
              include_check_plan=True,detail='summary')
    result=await server._dispatch('explain_property',args)
    row=result.results[0]
    assert row.semantic_status=='complete' and not row.nodes
    assert row.check_plan['status']=='prepared'
    entry=server._property_plans.resolve(row.check_plan['handle'])
    assert entry.plan['nodes'] and entry.plan['histories']=={}
    server.reset_session_state()
    with pytest.raises(PlanUnavailable):server._property_plans.resolve(row.check_plan['handle'])
