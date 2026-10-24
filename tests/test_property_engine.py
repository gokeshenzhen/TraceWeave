"""Independent sampled traces with hand-derived endpoints and obligations."""
import pytest
from itertools import product
import threading
from src import cancellation
from src.property_engine import PropertyEngine, UnsupportedPlan
from src.property_plan import prepare_plan
from src.expression_values import Value
from tests.test_property_plan import planned


def make(text,rows,*,declarations=None,**kwargs):
    extra={} if declarations is None else dict(unit={'declarations':declarations,'default_clock':'posedge clk'})
    plan,gaps=prepare_plan(planned(text,**extra))
    assert not gaps,gaps
    widths={s.rsplit('.',1)[-1]:v['width'] for s,v in plan['signals'].items()}
    samples=[]
    for i,row in enumerate(rows):
        values={}
        for name,value in row.items():
            path='__tw_property.'+name
            if path not in plan['signals']:continue
            width=widths[name]
            values[path]={'bin':None if value is None else value*width if value in ('x','z') else format(value%(1<<width),f'0{width}b')}
        samples.append(dict(time_ps=10*i+5,signals=values))
    return PropertyEngine(plan,samples,**kwargs)


@pytest.mark.parametrize('text,rows,status,end',[
    ('a |-> ##3 b',[dict(a=1,b=0),dict(a=0,b=1),dict(a=0,b=0),dict(a=0,b=0)],'false',3),
    ('a |-> ##[1:3] b',[dict(a=1,b=0),dict(a=0,b=1)],'true',1),
    ('a |=> ##[1:3] b',[dict(a=1,b=0),dict(a=0,b=1),dict(a=0,b=0),dict(a=0,b=0),dict(a=0,b=0)],'false',4),
    ('a |-> b',[dict(a=1,b=0),dict(a=0,b=1)],'false',0),
    ('a |=> b',[dict(a=1,b=1),dict(a=0,b=0)],'false',1),
    ('a |=> b',[dict(a=1,b=0)],'pending',None),
    ('a |-> b',[dict(a=0,b=0)],'true',0),
    ('a |-> b',[dict(a='x',b=0)],'true',0),
    ('a |-> b',[dict(a=1,b='x')],'false',0),
    ('a |-> b',[dict(a=None,b=0)],'unknown',None),
    ('a |-> b',[dict(a=1,b=None)],'unknown',None),
    ('a |-> $isunknown(b)',[dict(a=1,b='x')],'true',0),
    ("a |-> (b === 1'bx)",[dict(a=1,b='x')],'true',0),
])
def test_required_samples_and_delay_quantifiers(text,rows,status,end):
    r=make(text,rows).attempt(0)
    assert r['status']==status
    assert r['end_index']==end


def test_all_antecedent_endpoints_but_first_match_selects_earliest():
    rows=[dict(a=1,b=0,c=0),dict(a=0,b=1,c=0),dict(a=0,b=1,c=1),dict(a=0,b=0,c=0)]
    all_=make('(a ##[1:2] b) |=> c',rows).attempt(0)
    first=make('first_match(a ##[1:2] b) |=> c',rows).attempt(0)
    assert all_['status']=='false' and all_['end_index']==3
    assert [o['antecedent_end'] for o in all_['obligations']]==[1,2]
    assert first['status']=='true' and first['end_index']==2
    assert [o['antecedent_end'] for o in first['obligations']]==[1]


@pytest.mark.parametrize('text,rows,status,end',[
    ('a throughout (b ##2 c)',[dict(a=1,b=1,c=0),dict(a=0,b=0,c=0),dict(a=1,b=0,c=1)],'false',1),
    ('a throughout (b ##2 c)',[dict(a=1,b=0,c=0),dict(a=1,b=0,c=0),dict(a=1,b=0,c=1)],'false',0),
    ('(a ##1 b) and (a ##3 c)',[dict(a=1,b=0,c=0),dict(a=0,b=1,c=0),dict(a=0,b=0,c=0),dict(a=0,b=0,c=1)],'true',3),
    ('(a ##1 b) intersect (a ##3 c)',[dict(a=1,b=0,c=0),dict(a=0,b=1,c=0),dict(a=0,b=0,c=0),dict(a=0,b=0,c=1)],'false',0),
    ('(a ##1 b) intersect (a ##[1:3] c)',[dict(a=1,b=0,c=0),dict(a=0,b=1,c=1),dict(a=0,b=0,c=0)],'true',1),
    ('(a ##1 b) within (c ##3 c)',[dict(a=0,b=0,c=1),dict(a=1,b=0,c=0),dict(a=0,b=1,c=0),dict(a=0,b=0,c=1)],'true',3),
    ('(a ##1 b) within (c ##3 c)',[dict(a=0,b=0,c=1),dict(a=1,b=0,c=0),dict(a=0,b=0,c=0),dict(a=0,b=0,c=1)],'false',3),
])
def test_sequence_combinations_need_actual_matches(text,rows,status,end):
    r=make(text,rows).attempt(0)
    assert (r['status'],r['end_index'])==(status,end)


def test_tied_first_match_keeps_branch_private_local_values():
    text='property p; logic [3:0] v; first_match(((a,v=data) ##[1:2] b) or ((a,v=data+1) ##1 b)) |=> data==v; endproperty'
    rows=[dict(a=1,b=0,data=3),dict(a=0,b=1,data=9),dict(a=0,b=0,data=3)]
    r=make(text,rows).attempt(0)
    assert r['status']=='false' and r['end_index']==2
    assert len(r['obligations'])==2
    assert [o['status'] for o in r['obligations']]==['true','false']


def test_overlapping_attempts_do_not_share_local_state():
    text='property p; logic [3:0] v; (a,v=data) |=> data==v; endproperty'
    engine=make(text,[dict(a=1,data=3),dict(a=1,data=3),dict(a=0,data=4)])
    first=engine.attempt(0);second=engine.attempt(1)
    assert first['status']=='true' and second['status']=='false'
    assert first['evidence'] and second['evidence']


@pytest.mark.parametrize('text,rows,status,end',[
    ('a[*2] ##1 b',[dict(a=1,b=0),dict(a=1,b=0),dict(a=0,b=1)],'true',2),
    ('a[*2] ##1 b',[dict(a=1,b=0),dict(a=0,b=0),dict(a=0,b=1)],'false',1),
    ('a[*0:2] ##1 b',[dict(a=0,b=1)],'true',0),
    ('a[->2] ##1 b',[dict(a=0,b=0),dict(a=1,b=0),dict(a=0,b=0),dict(a=1,b=0),dict(a=0,b=1)],'true',4),
    ('a[->2] ##1 b',[dict(a=1,b=0),dict(a=0,b=1)],'pending',None),
    ('a[=1] ##1 b',[dict(a=1,b=0),dict(a=0,b=0),dict(a=0,b=1)],'true',2),
    ('a[->1] ##1 b',[dict(a='x',b=0),dict(a=1,b=0),dict(a=0,b=1)],'false',0),
])
def test_repetition_counts_endpoints_and_waiting(text,rows,status,end):
    r=make(text,rows).attempt(0)
    assert (r['status'],r['end_index'])==(status,end)


def test_sampled_history_is_shared_across_attempts_and_capture_cutoffs_are_explicit():
    engine=make('$stable(data)',[dict(data=3),dict(data=3),dict(data=4)])
    assert engine.attempt(0)['status']=='unknown'
    assert engine.attempt(1)['status']=='true'
    assert engine.attempt(2)['status']=='false'
    assert 'history_before_capture_unavailable' in engine.gaps


def test_operand_observations_are_distinct_from_compound_expression_results():
    result=make('$onehot0(data) && a',[dict(data=3,a=1)]).attempt(0)
    assert result['status']=='false'
    assert result['evidence'][0]['value']['bin']=='0'
    facts={v['source'].rsplit('.',1)[-1]:v['value']['bin'] for v in result['sampled_values']}
    assert facts==dict(data='0011')  # The short-circuited operand was not read.


@pytest.mark.parametrize('bits',['x','z'])
def test_observed_four_state_values_are_not_missing_evidence(bits):
    engine=make('$isunknown(data)',[dict(data=bits)])
    result=engine.attempt(0)
    assert result['status']=='true'
    assert result['sampled_values'][0]['value']['bin']==bits*4
    assert 'signal_not_dumped' not in engine.gaps


def test_history_observations_use_original_ticks_and_never_invent_missing_samples():
    engine=make('$past(data)==data',[dict(data=3),dict(data=4)])
    result=engine.attempt(1)
    assert result['status']=='false'
    assert {(v['time_ps'],v['value']['bin']) for v in result['sampled_values']}=={(5,'0011'),(15,'0100')}
    result=engine.attempt(0)
    assert result['status']=='unknown'
    assert [(v['time_ps'],v['value']['bin']) for v in result['sampled_values']]==[(5,'0011')]
    assert 'history_before_capture_unavailable' in engine.gaps
    assert 'property_history_value_unavailable' in engine.gaps
    assert 'signal_not_dumped' not in engine.gaps
    missing=make('a |-> b',[dict(a=1,b=None)])
    assert missing.attempt(0)['status']=='unknown'
    assert 'signal_not_dumped' in missing.gaps


@pytest.mark.parametrize('function',['$rose','$fell'])
@pytest.mark.parametrize('current',['0','1','x','z'])
def test_current_edge_value_determines_only_history_independent_results(function,current):
    rows=lambda bits:[dict(data=int(v) if v in '01' else v) for v in bits]
    engine=make(f'{function}(data)',rows([current]))
    result=engine.attempt(0)
    completions=[make(f'{function}(data)',rows([prior,current])).attempt(1)['status'] for prior in '01xz']
    expected=completions[0] if len(set(completions))==1 else 'unknown'
    assert result['status']==expected
    assert 'history_before_capture_unavailable' in engine.gaps
    assert all(v['time_ps']==5 for v in result['sampled_values'])


@pytest.mark.parametrize('text,current,status',[
    ('~$past(data) & data',0,'false'),
    ('$past(data) | data',15,'true'),
    ('$past(data) & data',5,'unknown'),
    ('$past(data) | data',0,'unknown'),
])
def test_controlling_mask_preserves_missing_history_without_fabricating_a_sample(text,current,status):
    engine=make(text,[dict(data=current)])
    result=engine.attempt(0)
    assert result['status']==status
    assert 'history_before_capture_unavailable' in engine.gaps
    assert 'property_history_value_unavailable' in engine.gaps
    assert len(result['sampled_values'])==1
    assert result['sampled_values'][0]['value']['bin']==format(current,'04b')


def test_sample_display_budget_and_prefix_probes_do_not_change_semantics():
    engine=make('a |=> b',[dict(a=1,b=0),dict(a=0,b=1)],max_evidence=1)
    result=engine.attempt(0)
    assert result['status']=='true' and not result['analysis_limited']
    assert len(result['sampled_values'])==1 and result['sampled_values_truncated']
    engine=make('a',[dict(a=1),dict(a=1)])
    raw=next(n for n in engine.nodes.values() if n['op']=='sample')['facts']['expression']['execution']['expression']
    engine.probing=True
    engine.read(raw,0,{})
    assert not engine.sampled_values
    engine.probing=False
    # Wide vectors are retained as binary, with a separate 64KiB display cap.
    shape=engine.plan['signals']['__tw_property.a'];shape.update(width=32768,bits=list(range(32767,-1,-1)))
    for row in engine.samples:row['signals']['__tw_property.a']={'bin':'1'*32768}
    engine.read(raw,0,{});engine.read(raw,1,{})
    assert len(engine.sampled_values)==1 and engine.sampled_values_truncated


def test_first_history_uses_declaration_default_and_vector_edges_use_lsb():
    declarations='logic clk; logic [3:0] data=0;'
    rose=make('$rose(data)',[dict(data=2),dict(data=3)],declarations=declarations,history_complete=True)
    assert rose.attempt(0)['status']=='false'
    assert rose.attempt(1)['status']=='true'
    stable=make('$stable(data)',[dict(data=0)],declarations=declarations,history_complete=True)
    assert stable.attempt(0)['status']=='true'


def test_gated_past_counts_prior_qualifying_events():
    engine=make('data==$past(data,2,a)',[dict(a=1,data=3),dict(a=0,data=9),dict(a=1,data=4),dict(a=1,data=3)])
    assert engine.attempt(3)['status']=='true'


def test_between_edge_reset_aborts_an_active_attempt():
    engine=make('disable iff(rst) (a |-> ##2 b)',[dict(a=1,b=0,rst=0)]*3)
    control=next(n['id'] for n in engine.nodes.values() if n['op']=='disable_iff')
    engine.controls={control:[(0,Value('0')),(6000,Value('1')),(7000,Value('0'))]}
    assert engine.attempt(0)['status']=='disabled'
    engine.controls={control:[(0,Value('0'))]}
    assert engine.attempt(0)['status']=='false'


def test_unobserved_async_control_never_uses_only_clock_samples():
    r=make('disable iff(rst) (a |=> b)',[dict(a=1,b=0,rst=0),dict(a=0,b=0,rst=0)]).attempt(0)
    assert r['status']=='unknown'


@pytest.mark.parametrize('op',['until','s_until'])
def test_until_waits_for_temporal_operands(op):
    rows=[dict(a=0,b=1,c=0),dict(a=0,b=0,c=1)]
    assert make(f'a {op} (b |=> c)',rows[:1]).attempt(0)['status']=='pending'
    success=make(f'a {op} (b |=> c)',rows).attempt(0)
    assert (success['status'],success['end_index'])==('true',1)
    failure=make(f'a {op} (b |=> c)',[dict(a=0,b=1,c=0),dict(a=0,b=0,c=0)]).attempt(0)
    assert (failure['status'],failure['end_index'])==('false',1)
    # A later right-hand success does not erase an earlier left obligation.
    failure=make(f'(b |=> a) {op} c',[dict(a=0,b=1,c=0),dict(a=0,b=0,c=1)]).attempt(0)
    assert (failure['status'],failure['end_index'])==('false',1)


@pytest.mark.parametrize('op',['until_with','s_until_with'])
def test_inclusive_until_requires_left_at_the_right_start(op):
    rows=[dict(a=0,b=1,c=0),dict(a=0,b=0,c=1)]
    failure=make(f'a {op} (b |=> c)',rows).attempt(0)
    assert (failure['status'],failure['end_index'])==('false',0)


def test_limits_and_display_trimming_do_not_create_passes():
    rows=[dict(a=1,b=1,c=1)]*8
    engine=make('(a ##[1:7] b) |=> c',rows,max_work=20)
    r=engine.attempt(0)
    assert r['analysis_limited'] and r['status']=='unknown'
    engine=make('a |=> b',rows,max_evidence=1)
    r=engine.attempt(0)
    assert r['status']=='true' and r['evidence_truncated'] and not r['analysis_limited']


@pytest.mark.parametrize('first',[False,True])
def test_exhaustive_small_trace_implication_oracle(first):
    # Independent set-based oracle: every eligible b endpoint requires c one
    # event later, except first_match selects the minimum endpoint first.
    source=('first_match(a ##[0:2] b)' if first else '(a ##[0:2] b)')+' |=> c'
    plan,gaps=prepare_plan(planned(source));assert not gaps
    for bits in product((0,1),repeat=7):
        a,b0,b1,b2,c1,c2,c3=bits
        ends=[i for i,b in enumerate((b0,b1,b2)) if a and b]
        if first:ends=ends[:1]
        expected=all((c1,c2,c3)[i] for i in ends)
        rows=[dict(a=a,b=b0,c=0),dict(a=0,b=b1,c=c1),dict(a=0,b=b2,c=c2),dict(a=0,b=0,c=c3)]
        samples=[dict(time_ps=i*10+5,signals={'__tw_property.'+k:{'bin':str(v)} for k,v in row.items()})
                 for i,row in enumerate(rows)]
        result=PropertyEngine(plan,samples).attempt(0)
        assert result['status']==('true' if expected else 'false'),(bits,result)


def test_empty_operands_preserve_duration_without_boolean_samples():
    r=make('a[*0] ##2 b[*0]',[dict(a=0,b=0)]).attempt(0)
    assert r['status']=='true' and r['end_index']==0


def test_later_reset_does_not_erase_a_completed_failure():
    engine=make('disable iff(rst) ((a ##1 b) intersect (a ##3 c))',
        [dict(a=1,b=0,c=0,rst=0),dict(a=0,b=1,c=0,rst=0),
         dict(a=0,b=0,c=0,rst=0),dict(a=0,b=0,c=1,rst=0)])
    node=next(n['id'] for n in engine.nodes.values() if n['op']=='disable_iff')
    # Fixed disjoint endpoint sets make the intersection impossible at start.
    # Reset at 20ps cannot disable that completed failure.
    engine.controls={node:[(0,Value('0')),(20000,Value('1'))]}
    result=engine.attempt(0)
    assert result['status']=='false' and result['end_index']==0


def test_static_disjoint_endpoints_fail_before_future_samples_exist():
    r=make('a |-> ((a ##1 b) intersect (a ##3 c))',[dict(a=1,b=0,c=0)]).attempt(0)
    assert r['status']=='false' and r['end_index']==0
    assert r['reason']=='disjoint_sequence_endpoints'


def test_engine_observes_cancellation():
    engine=make('a |=> b',[dict(a=1,b=0)]*4)
    event=threading.Event();event.set();token=cancellation.push_cancel_event(event)
    try:
        with pytest.raises(cancellation.OperationCancelled):engine.attempt(0)
    finally:cancellation.pop_cancel_event(token)


def test_actual_end_is_distinct_from_a_prefix_probe_or_wave_cutoff():
    rows=[dict(a=1),dict(a=0)]
    assert make('strong(a[->2])',rows).attempt(0)['status']=='pending'
    strong=make('strong(a[->2])',rows,actual_end=True).attempt(0)
    weak=make('weak(a[->2])',rows,actual_end=True).attempt(0)
    assert strong['status']=='false' and strong['end_index']==1
    assert weak['status']=='true' and weak['end_index']==1
