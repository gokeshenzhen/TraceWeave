"""Independent SVA endpoint/obligation oracles, not snapshots of a projected AST."""
from pathlib import Path

import pytest

pytest.importorskip('pyslang')
from src.property_worker import explain


def explain_text(text, *, declarations='', arguments=(), default_clock='posedge clk', default_disable=None, **changes):
    payload=dict(item={'id':'case','text':text,'arguments':list(arguments)},
        unit={'declarations':'logic clk, clk2, a,b,c,rst; logic [3:0] data; int state; '+declarations,
              'default_clock':default_clock,'default_disable':default_disable},
        limits={'max_nodes':1024,'max_depth':64},timeout_sec=10)
    payload.update(changes)
    return explain(payload)


def complete(text, **kwargs):
    r=explain_text(text,**kwargs)
    assert r['semantic_status']=='complete',r['gaps']
    assert r['syntax_status']=='valid'
    return r


def node(r,op):
    return next(n for n in r['nodes'] if n['op']==op)


def test_every_range_antecedent_match_is_an_obligation_but_consequents_choose():
    r=complete('(a ##[1:2] b) |=> ##[2:4] c')
    implication=node(r,'NonOverlappedImplication')['facts']
    assert implication['obligation_quantifier']=='for_every_antecedent_match'
    assert implication['consequent_sequence_choices']=='exists_a_successful_match_for_each_obligation'
    assert implication['start_relation']=='next_tick_after_antecedent_end'
    joins=[j for n in r['nodes'] for j in n['facts'].get('joins',[])]
    assert [(j['delay']['min'],j['delay']['max']) for j in joins]==[(1,2),(2,4)]
    assert joins[0]['left'] is not None and joins[1]['left'] is None


def test_fragment_source_lines_and_columns_refer_to_caller_text():
    r=complete('a |=>\n  b')
    samples=[n for n in r['nodes'] if n['op']=='sample']
    assert [(n['source']['file'],n['source']['line'],n['source']['column']) for n in samples]==[
        ('input',1,1),('input',2,3)]


def test_first_match_is_operand_local_and_retains_tied_branches():
    r=complete('first_match((a ##[1:3] b) or (a ##1 c)) |-> b')
    f=node(r,'first_match')['facts']
    assert f['selection']=='minimum_end_for_this_attempt_and_operand'
    assert f['ties']=='retain_all_matching_branches_at_earliest_end'
    assert node(r,'Or')['domain']=='sequence'
    assert node(r,'Or')['facts']['matches']=='union_of_branch_matches'


def test_sequence_and_property_and_have_different_endpoint_relations():
    seq=complete('a |-> ((a ##1 b) and (a ##2 c)) ##1 b')
    prop=complete('(a |=> b) and (b |=> c)')
    assert node(seq,'And')['facts']['end_relation']=='max(left.end,right.end)'
    assert node(prop,'And')['facts']['end_relation']=='independent_property_completion'
    assert node(prop,'And')['domain']=='property'
    assert node(complete('(a |=> b) or (b |=> c)'),'Or')['facts']['matches']=='disjunction_of_property_truth'


@pytest.mark.parametrize('text,operator',[
    ('a |-> ((1 ##1 b) and (1 ##3 b))','And'),
    ('a |-> (1 ##1 b) and (1 ##3 b)','And'),
    ('(1 ##1 b) and (1 ##3 b)','And'),
    ('a |-> ((1 ##1 b) or (1 ##3 b))','Or'),
    ('(1 ##1 b) or (1 ##3 b)','Or')])
def test_sequence_operators_keep_frontend_domain_when_used_as_properties(text,operator):
    r=complete(text)
    n=node(r,operator)
    assert n['domain']=='sequence'
    assert n['facts']['domain_basis']=='frontend_sequence_syntax'
    if operator=='And':
        assert n['facts']['end_relation']=='max(left.end,right.end)'
        assert n['facts']['requires_same_start'] is True
        assert n['facts']['requires_same_end'] is False
    else:
        assert n['facts']['matches']=='union_of_branch_matches'


def test_alignment_constraints_and_compact_summary_distinguish_and_from_intersect():
    and_result=complete('a |-> ((1 ##1 b) and (1 ##3 c))')
    intersect_result=complete('a |-> ((1 ##1 b) intersect (1 ##3 c))')
    for result,operator,same_end in [(and_result,'And',False),(intersect_result,'Intersect',True)]:
        n=node(result,operator)
        assert n['facts']['requires_same_end'] is same_end
        line=next(s for s in result['summary'] if s.startswith('['+n['id']+']'))
        assert '1 ##1 b' in line and '1 ##3 c' in line
        assert 'requires_same_end='+str(same_end).lower() in line
        assert not any('Evaluate the typed value' in s for s in result['summary'])
    prop=complete('(a |=> b) and (b |=> c)')
    assert node(prop,'And')['domain']=='property'
    assert node(prop,'And')['facts']['domain_basis']=='frontend_property_syntax'
    assert node(prop,'And')['facts']['requires_same_end'] is False
    assert node(prop,'And')['facts']['end_relation']=='independent_property_completion'


def test_interval_containment_and_alignment_are_distinct():
    r=complete('a |-> ((b throughout (1 ##2 c)) intersect (b within c[->1]))')
    assert node(r,'Throughout')['facts']['requirement']=='left_true_at_every_tick_of_right_match'
    assert node(r,'Intersect')['facts']['end_relation']=='same_end'
    within=node(r,'Within')['facts']
    assert within['end_relation']=='inner.end<=outer.end'
    assert within['result_interval']==within['outer']


@pytest.mark.parametrize('rep,kind,endpoint',[
    ('[*2:4]','Consecutive','end_of_last_repetition'),
    ('[->2]','GoTo','last_true_occurrence'),
    ('[=2]','Nonconsecutive','last_true_occurrence_or_a_later_false_tick_before_next_true')])
def test_three_repetitions_preserve_endpoint_and_count(rep,kind,endpoint):
    r=complete('a |-> b'+rep+' ##1 c')
    fact=next(n['facts']['repetition'] for n in r['nodes'] if 'repetition' in n['facts'])
    assert fact['kind']==kind and fact['endpoint']==endpoint
    assert fact['count']['min']==2


def test_empty_match_and_unbounded_count_are_not_flattened():
    r=complete('a |-> b[*0] ##1 c[*1:$]')
    reps=[n['facts']['repetition'] for n in r['nodes'] if 'repetition' in n['facts']]
    assert reps[0]['empty_match_allowed'] and reps[0]['count']['max']==0
    assert reps[1]['count']['unbounded'] and reps[1]['count']['max'] is None
    assert 'N-1' in node(r,'concat')['facts']['joins'][0]['empty']


@pytest.mark.parametrize('text,delay',[
    ('(a[*0] ##2 a[*0])[*1]',2),
    ('a[*0] ##3 a[*0]',3),
    ('a[*0] ##4 a[*0]',4),
    ('a[*0] ##2 b',2),
    ('a ##2 b[*0]',2)])
def test_positive_empty_joins_account_for_both_endpoints(text,delay):
    # Independent endpoint oracle: an empty match has end=start-1. Applying
    # that on both sides gives durations 1,2,3 for delays 2,3,4 respectively;
    # it does not make every empty/empty concatenation unsatisfiable.
    r=complete(text)
    concat=node(r,'concat')
    rules=concat['facts']['empty_join_rules']
    assert concat['facts']['joins'][0]['delay']['min']==delay
    assert rules['empty_endpoint']=='end=start-1'
    assert rules['left_empty_positive_delay']=='right.start=attempt.start+N-1'
    assert rules['right_empty_positive_delay']=='result.end=left.end+N-1'
    assert rules['both_empty_positive_delay'].startswith('duration_ticks=N-1;')
    assert any('empty ##2 empty is a one-tick match' in s for s in r['summary'])


def test_empty_join_summary_follows_a_named_sequence_without_treating_all_sequences_as_empty():
    r=complete('empty_s ##2 empty_s',declarations='sequence empty_s; a[*0]; endsequence')
    assert any('empty ##2 empty is a one-tick match' in s for s in r['summary'])
    ordinary=complete('a ##2 b')
    assert not any('empty ##2 empty is a one-tick match' in s for s in ordinary['summary'])


def test_named_parameter_binding_and_width_survive_local_capture():
    r=complete('property p(x); logic [3:0] v; (a,v=x) |=> v==data; endproperty',arguments=['data'])
    assert r['directive']['kind']=='definition'
    assert r['target']['kind']=='definition'
    assert r['directive']['activation']['kind']=='none_definition_or_fragment'
    assert r['variables'][0]['type']['width']==4
    assert r['variables'][0]['lifetime']=='one_attempt_and_its_matching_branches'
    ref=r['references'][0]
    assert ref['bindings'][0]['actual']['reads']==['__tw_property.data']
    assignment=r['effects'][0]
    assert assignment['phase']=='observed_local_assignment'
    assert assignment['expression']['type']['width']==4


def test_local_updates_with_first_match_are_state_transitions_not_constants():
    r=complete('property p; int v; (a,v=state) |-> first_match((1,v=v-1)[*0:$] ##1 v<=0) ##0 b; endproperty')
    assignments=[e for e in r['effects'] if e['kind']=='assignment']
    assert len(assignments)==2
    assert 'v-1' in assignments[1]['expression']['text']
    assert assignments[1]['reads'] and assignments[1]['writes']
    assert assignments[1]['condition'].startswith('on_match(')


def test_branch_variable_flow_is_retained_and_bad_flow_is_rejected():
    good=complete('property p; int v; a |-> (((b,v=1) ##1 c) or ((b,v=2) ##2 c)) ##0 v==2; endproperty')
    assert node(good,'Or')['facts']['matches']=='union_of_branch_matches'
    bad=explain_text('property p; int v; a |-> ((b,v=1) or c) ##0 v==2; endproperty')
    assert bad['semantic_status']!='complete'
    assert any(d['code']=='local_variable_does_not_flow' for d in bad['gaps'])


def test_sequence_local_ports_transfer_values_only_at_matching_boundaries():
    decl='sequence give(local output int x); (a,x=3); endsequence '
    decl+='sequence add(local inout int x); (b,x=x+1); endsequence '
    decl+='sequence consume(local input int x); x==4; endsequence'
    r=complete('property p; int v; give(v) ##1 add(v) ##1 consume(v); endproperty',declarations=decl)
    transfers=[t for n in r['nodes'] for t in n['facts'].get('local_output_transfers',[])]
    assert len(transfers)==2
    assert all(t['actual']==['__tw_property.p.v'] and t['definitely_assigned'] for t in transfers)
    assert all(t['copy']=='on_successful_sequence_match' for t in transfers)
    bad=explain_text('property p; int v; consume(v); endproperty',declarations=decl)
    assert any(g['code']=='local_variable_does_not_flow' for g in bad['gaps'])


def test_parallel_writers_block_local_flow_and_separate_calls_keep_private_locals():
    bad=explain_text('property p; int v; ((a,v=1) and (b,v=2)) ##1 v==2; endproperty')
    assert any(g['code']=='local_variable_does_not_flow' for g in bad['gaps'])
    good=complete('s ##1 s',declarations='sequence s; int v; (a,v=1) ##1 v==1; endsequence')
    assert len(good['variables'])==2
    assert len({v['owner'] for v in good['variables']})==2


def test_multiclock_implication_and_concatenation_keep_destination_clocks():
    r=complete('@(posedge clk) a |=> @(negedge clk2) b ##1 c')
    imp=node(r,'NonOverlappedImplication')['facts']
    assert imp['cross_clock_alignment']=='nearest_strictly_after'
    dest=imp['destination_clock']
    assert next(c for c in r['clocks'] if c['id']==dest)['edge']=='NegEdge'
    assert node(r,'concat')['facts']['joins'][0]['right_clock']==dest
    r=complete('@(posedge clk) a ##0 @(posedge clk2) b')
    assert 'at_or_after' in node(r,'concat')['facts']['joins'][0]['cross_clock']


def test_clock_scope_does_not_escape_parentheses_or_hide_unclocked_segments():
    r=complete('@(posedge clk) (a ##1 @(posedge clk2) b) ##1 c')
    samples=[n for n in r['nodes'] if n['op']=='sample']
    assert [n['clock'] for n in samples]==['c0','c1','c0']
    r=explain_text('(@(posedge clk2) a) ##1 b',default_clock=None)
    assert r['semantic_status']=='partial'
    assert any(g['code']=='clock_unresolved' for g in r['gaps'])


def test_frontend_analysis_rejects_illegal_multiclock_delay_and_empty_antecedent():
    for text in ['@(posedge clk) a ##2 @(posedge clk2) b','a[*0] |-> b']:
        r=explain_text(text)
        assert r['semantic_status']=='partial'
        assert any(d['severity']=='error' for d in r['diagnostics'])
        assert any(g['category']=='syntax' for g in r['gaps'])


def test_disable_strength_conditional_and_abort_are_distinct():
    r=complete('disable iff(rst) a |-> if(b) strong(1 ##[1:$] c) else weak(1 ##[1:$] c)',default_disable='!a')
    assert node(r,'disable_iff')['facts']['outcome']=='disabled_attempt_neither_passes_nor_fails'
    assert not r['directive']['default_disable']['applied']
    assert node(r,'strong')['facts']['completion']=='nonempty_match_required'
    assert node(r,'weak')['facts']['completion']=='no_finite_bad_prefix'
    abort=complete('a |=> sync_accept_on(rst) b')
    assert node(abort,'sync_accept_on')['facts']['sampling']=='sampled_on_clock'
    default=complete('a |=> b',default_disable='rst')
    assert default['directive']['default_disable']['applied']


def test_sampled_functions_preserve_types_arguments_and_lsb_rules():
    r=complete('$rose(data) |=> ($fell(data) || $stable(data) || $past(data,2,a)==data)')
    calls=[c for n in r['nodes'] for c in n['facts'].get('expression',{}).get('calls',[])]
    by={c['name']:c for c in calls}
    assert by['$rose']['semantics']['vector_rule']=='least_significant_bit'
    assert by['$rose']['arguments'][0]['type']['width']==4
    assert by['$fell']['semantics']['rule']=='lsb_previous_in_1_X_Z_and_current_0'
    assert by['$stable']['semantics']['vector_rule']=='entire_expression'
    assert by['$past']['arguments'][1]['text']=='2'
    assert by['$past']['arguments'][2]['text']=='a'


def test_recursion_is_a_finite_guarded_reference_graph():
    r=complete('property p(x); x and (1 |=> p(x)); endproperty',arguments=['a'])
    cycle=next(ref for ref in r['references'] if ref['recursive'])
    assert cycle['back_to']==r['root']
    assert cycle['conditions'][-1]['advance_ticks']==1
    assert len(r['nodes'])<10


def test_mutual_recursion_retains_argument_and_progress_edges():
    r=complete('p(a)',declarations='property p(x); x and (1 |=> q(x)); endproperty property q(x); x and (1 |=> p(x)); endproperty')
    assert len(r['references'])==3
    assert r['references'][-1]['recursive']
    assert len(r['references'][-1]['conditions'])==2


def test_unary_nexttime_recursion_preserves_clock_progression():
    r=complete('property p; a and nexttime p; endproperty')
    cycle=next(ref for ref in r['references'] if ref['recursive'])
    assert cycle['conditions'][-1]['advance_ticks']==1


def test_generate_specialization_keeps_only_active_assertion_paths(tmp_path):
    source=tmp_path/'generated.sv'
    source.write_text('module child #(parameter N=2)(input clk,a,b); '
        'if(N==2) begin : gsmall ap: assert property(@(posedge clk) a |-> ##N b); end '
        'else begin : big ap: assert property(@(posedge clk) a |-> ##N b); end endmodule '
        'module top; bit clk,a,b; child #(.N(2)) u(clk,a,b); child #(.N(4)) v(clk,a,b); endmodule')
    base={'frontend_args':[str(source),'--top','top'],'limits':{'max_nodes':1024,'max_depth':64},'timeout_sec':10}
    r=explain({**base,'item':{'id':'p','source_file':str(source),'target':'ap'}})
    assert {c['path'] for c in r['candidates']}=={'top.u.gsmall.ap','top.v.big.ap'}
    r=explain({**base,'item':{'id':'p','source_file':str(source),'target':'top.v.big.ap'}})
    assert r['semantic_status']=='complete'
    assert node(r,'concat')['facts']['joins'][0]['delay']['min']==4


def test_realtime_and_unsupported_temporal_operators_keep_their_boundaries():
    r=complete('property p; realtime t; (a,t=$realtime) |=> $realtime-t<10.0; endproperty')
    assert r['variables'][0]['type']['name']=='realtime'
    calls=[c for n in r['nodes'] for c in n['facts'].get('expression',{}).get('calls',[])]
    assert calls[0]['semantics']['rule']=='simulation_time_in_enclosing_timeunit; not_cycle_count'
    r=explain_text('a #-# b')
    assert r['syntax_status']=='valid' and r['semantic_status']=='partial'
    assert any(g['code']=='binary_operator_semantics' for g in r['gaps'])


def test_pure_function_expansion_and_stateful_match_call_are_separate():
    decl='function automatic int f(input int x); return x+1; endfunction function void bump(); state=state+1; endfunction'
    pure=complete('a |=> f(state)==2',declarations=decl)
    call=next(n['facts']['expression']['calls'][0] for n in pure['nodes'] if n['facts'].get('expression',{}).get('calls'))
    assert call['purity']=='pure_single_return' and call['return_expression']['text']=='x+1'
    stateful=complete('assert property ((a,bump()) |=> b) state=state+1; else state=0;',declarations=decl)
    effect=next(e for e in stateful['effects'] if e['kind']=='call')
    assert effect['phase']=='reactive_subroutine_call' and effect['writes']==['__tw_property.state']
    assert {e['condition'] for e in stateful['effects'] if e['phase']=='reactive_action'}=={'on_success','on_failure'}


def test_action_address_reads_compound_updates_and_function_conditions_are_preserved():
    r=complete('assert property(a |=> b) data[state] += 1; else bump();',
               declarations='function void bump(); if(c) state=1; else state=2; endfunction')
    assignment=next(e for e in r['effects'] if e['kind']=='assignment')
    assert assignment['writes']==['__tw_property.data']
    assert assignment['reads']==['__tw_property.data','__tw_property.state']
    call=next(e['call'] for e in r['effects'] if e['kind']=='call')
    assert any(e['kind']=='branch_condition' and e['reads']==['__tw_property.c'] for e in call['effects'])


@pytest.mark.parametrize('text,form',[('assert(a);','immediate'),('expect(a |=> b);','expect'),('cover sequence(a ##1 b);','concurrent'),('cover property(a |=> b);','concurrent')])
def test_directive_classification(text,form):
    r=complete(text)
    assert r['directive']['form']==form
    if form=='immediate': assert r['nodes'][0]['domain']=='value'
    if form=='expect': assert r['directive']['activation']['blocking'].startswith('procedure_waits')


@pytest.mark.parametrize('text', ['a |=> ## b', 'a |=> missing', 'a |-> b.ended'])
def test_invalid_missing_and_legacy_never_report_complete(text):
    r=explain_text(text)
    assert r['semantic_status']!='complete'
    assert r['gaps'] and r['diagnostics']


def test_source_instance_parameter_specializations_and_ambiguity(tmp_path):
    src=tmp_path/'test.sv'
    src.write_text('module child #(parameter N=2)(input clk,a,b); default clocking @(posedge clk); endclocking '
                   'property p; a |-> ##N b; endproperty ap: assert property(p); endmodule '
                   'module top; bit clk,a,b; child #(.N(2)) u(clk,a,b); child #(.N(5)) v(clk,a,b); endmodule')
    payload={'frontend_args':[str(src),'--top','top'],'limits':{'max_nodes':1024,'max_depth':64},'timeout_sec':10}
    for inst,n in [('u',2),('v',5)]:
        r=explain({**payload,'item':{'id':'p','source_file':str(src),'target':f'top.{inst}.ap'}})
        assert r['semantic_status']=='complete',r['gaps']
        assert node(r,'concat')['facts']['joins'][0]['delay']['min']==n
    ambiguous=explain({**payload,'item':{'id':'p','source_file':str(src),'target':'ap'}})
    assert len(ambiguous['candidates'])==2
    definition=explain({**payload,'item':{'id':'p','source_file':str(src),'target':'top.u.p'}})
    assert definition['semantic_status']=='complete',definition['gaps']
    assert definition['directive']['kind']=='definition'


def test_limits_keep_partial_graph_and_specific_recovery():
    r=explain_text('a |-> b ##1 c',limits={'max_nodes':2,'max_depth':64})
    assert r['analysis_limited'] and r['semantic_status']=='partial'
    assert r['gaps'][0]['code']=='node_limit'


def test_diagnostic_display_limit_does_not_become_analysis_limit_or_hide_errors():
    declarations=''.join(f"logic [1:0] x{i}; assign x{i}=32'hffff;" for i in range(140))
    source='module top; bit clk; '+declarations+' ap: assert property(@(posedge clk) 1); endmodule'
    r=complete(source,item={'id':'p','text':source,'target':'ap'})
    assert r['diagnostics_truncated'] and r['output_truncated'] and not r['analysis_limited']
    source=source.replace('@(posedge clk) 1','@(posedge clk) missing')
    r=explain_text(source,item={'id':'p','text':source,'target':'ap'})
    assert r['semantic_status']=='partial'
    assert r['diagnostics'][0]['severity']=='error'
    assert any(g['code']=='UndeclaredIdentifier' for g in r['gaps'])


def test_default_disable_history_keeps_unsampled_and_sampled_dependencies_separate():
    r=complete('a |=> b',default_disable='rst || $past(rst)')
    default=r['directive']['default_disable']
    assert default['sampling'].startswith('asynchronous_unsampled')
    history=default['condition']['calls'][0]
    assert history['name']=='$past' and history['semantics']['default_ticks']==1


def test_static_assertion_control_is_not_runtime_enabled_evidence():
    r=complete('module top; bit clk,a,b; initial $assertoff(0); ap: assert property(@(posedge clk) a |=> b); endmodule',
               item={'id':'p','text':'module top; bit clk,a,b; initial $assertoff(0); ap: assert property(@(posedge clk) a |=> b); endmodule','target':'ap'})
    control=next(e for e in r['effects'] if e['phase']=='procedural_assertion_control')
    assert control['call']['name']=='$assertoff'
    assert r['directive']['runtime_enabled']=='not_observed'


def test_synthetic_default_probes_do_not_duplicate_control_effects():
    source='module top; bit clk,a,b,rst; initial $assertoff(0); default clocking @(posedge clk); endclocking default disable iff(rst); property p; a |=> b; endproperty endmodule'
    r=complete(source,item={'id':'p','text':source,'target':'top.p'})
    assert sum(e['phase']=='procedural_assertion_control' for e in r['effects'])==1


def test_procedural_concurrent_assertion_infers_enclosing_event():
    r=complete('module top; bit clk,a,b; always @(posedge clk) if(a) ap: assert property(a |=> b); endmodule')
    assert r['clocks'][0]['inherited_from']=='procedural_event_inference'
    assert r['directive']['activation']['kind']=='procedural'
    assert any(c['kind']=='ConditionalStatement' for c in r['directive']['activation']['controls'])


@pytest.mark.parametrize('fixture,labels',[
    ('oracle.sv',['cap','side','ap_overlap','ap_nonoverlap','ap_offset','ap_all_matches',
                  'ap_first_match','ap_any_consequent','ap_seq_and','ap_intersect_bad',
                  'ap_throughout_bad','ap_within_ok','ap_within_bad','ap_consecutive_bad',
                  'ap_goto_ok','ap_nonconsecutive_tail','ap_goto_tail_bad','ap_empty',
                  'ap_conditional','ap_disabled','ap_vacuous','ap_weak_pending','ap_strong_pending',
                  'ap_pure_function','assume_bad','assert_bad','cover_match']),
    ('clocks.sv',['c0','c1','cd','imp','cp','rose','fell','stable','all_late','first_early','br','rec']),
    ('activation.sv',['ap','av','sampled'])])
def test_directed_simulation_fixture_targets_all_have_complete_explanations(fixture,labels):
    path=Path(__file__).parent/'fixtures'/'property_semantics'/fixture
    for label in labels:
        r=explain({'item':{'id':label,'source_file':str(path),'target':label},'frontend_args':[str(path)],
                   'limits':{'max_nodes':1024,'max_depth':64},'timeout_sec':10})
        assert r['semantic_status']=='complete',(label,r['gaps'])
        assert r['root'] is not None and r['target']['source']['line']>0
