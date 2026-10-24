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


def test_target_discovery_skips_unrelated_task_and_value_trees():
    # The selected property needs only a few nodes. A large testbench driver
    # must not consume its projection budget while discovering assertion sites.
    assignments='\n'.join(f'data={i};' for i in range(5000))
    source=('module top; logic clk,a,b; int data;\n'
            'task drive; begin\n'+assignments+'\nend endtask\n'
            'initial begin drive(); $assertoff; $asserton; ap: assert property(@(posedge clk) a |=> b); end\n'
            'endmodule')
    result=explain_text(source,item=dict(id='large',text=source,target='top.ap',arguments=[]),
        limits=dict(max_nodes=64,max_depth=64),execution_plan=True)
    assert result['semantic_status']=='complete',result['gaps']
    assert result['target']['path']=='top.ap'
    assert node(result,'NonOverlappedImplication')
    controls=[e for e in result['effects'] if e.get('phase')=='procedural_assertion_control']
    assert len(controls)==2,result['effects']
    from src.property_plan import prepare_plan
    plan,gaps=prepare_plan(result)
    assert plan is not None and not gaps,gaps


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


def test_sequence_matching_requirements_do_not_become_implication_vacuity():
    implication=complete('a |-> b')
    throughout=complete('a throughout (b ##2 c)')
    within=complete('(a ##1 b) within (b ##3 c)')
    assert node(implication,'OverlappedImplication')['facts']['when_no_antecedent_match']==(
        'property_succeeds_vacuously; no_consequent_obligation')
    assert any('If the antecedent has no match, the implication succeeds vacuously' in s
               for s in implication['summary'])
    t=node(throughout,'Throughout')['facts']
    assert t['right_sequence_match']=='required'
    assert t['vacuity']=='no_implication_style_vacuous_success_from_absent_right_match'
    assert any('The right sequence must match' in s for s in throughout['summary'])
    assert node(within,'Within')['facts']['match_preconditions']==(
        'both_inner_and_outer_sequences_must_actually_match_their_sampled_values')
    assert any('Both inner and outer sequences must actually match their sampled values' in s
               for s in within['summary'])


@pytest.mark.parametrize('rep',['[->2:4]','[=2:4]'])
def test_finite_occurrence_count_does_not_promise_a_finite_wait(rep):
    r=complete('a'+rep)
    repetition=node(r,'sample')['facts']['repetition']
    assert repetition['count']==dict(min=2,max=4,unbounded=False)
    assert repetition['waiting_bound']=='unbounded_between_occurrences_even_with_finite_count'
    assert repetition['finite_window_without_required_occurrences'].startswith('can_remain_pending;')
    assert any('count bounds occurrences, not elapsed clock ticks' in s for s in r['summary'])


@pytest.mark.parametrize('rep,kind,endpoint',[
    ('[*2:4]','Consecutive','end_of_last_repetition'),
    ('[->2]','GoTo','last_true_occurrence'),
    ('[=2]','Nonconsecutive','last_true_occurrence_or_a_later_false_tick_before_next_true')])
def test_three_repetitions_preserve_endpoint_and_count(rep,kind,endpoint):
    r=complete('a |-> b'+rep+' ##1 c')
    fact=next(n['facts']['repetition'] for n in r['nodes'] if 'repetition' in n['facts'])
    assert fact['kind']==kind and fact['endpoint']==endpoint
    assert fact['count']['min']==2


@pytest.mark.parametrize('rep',['[*2:4]','[->3]','[=3]'])
def test_repetition_scopes_base_truth_separately_from_the_repeated_interval(rep):
    r=complete('a'+rep)
    n=node(r,'sample');f=n['facts']
    assert n['domain']=='sequence'
    assert f['match_scope']=='entire_repeated_sequence'
    assert f['endpoints']!='end=start'
    assert f['base_match']==dict(scope='one_base_occurrence_before_repetition',
        match='one_tick_when_expression_true',unknown='X_or_Z_boolean_result_is_not_a_match',endpoints='end=start')
    assert 'one-tick sequence match' not in n['explanation']
    if rep.startswith(('[->','[=')):
        assert 'do_not_skip_earlier_true_occurrences' in f['repetition']['occurrence_selection']


def test_unrepeated_sample_retains_its_one_tick_contract():
    f=node(complete('a'),'sample')['facts']
    assert f['match']=='one_tick_when_expression_true'
    assert f['endpoints']=='end=start'
    assert 'base_match' not in f


@pytest.mark.parametrize('rep',['[->3]','[=3]'])
def test_summary_keeps_repetition_even_under_a_concat_operator(rep):
    r=complete('a'+rep+' ##1 b')
    repeated=node(r,'sample')
    line=next(s for s in r['summary'] if s.startswith('['+repeated['id']+']'))
    assert 'count 3' in line
    assert 'do not skip earlier true occurrences' in line
    assert 'finite simulation end' in line


def test_summary_keeps_negation_and_scopes_it_to_the_same_start():
    r=complete('not (a ##1 b)')
    n=node(r,'Not')
    line=next(s for s in r['summary'] if s.startswith('['+n['id']+']'))
    assert 'Negate' in line and 'THIS same start' in line


@pytest.mark.parametrize('function',['$fell','$stable','$changed'])
def test_summary_exposes_input_history_default_instead_of_boolean_return_default(function):
    r=complete(function+'(a)')
    call=node(r,'sample')['facts']['expression']['calls'][0]
    assert call['return_type']['four_state'] is False
    assert call['history_default']['type']['four_state'] is True
    assert call['history_default']['value']=="1'bx"
    line=next(s for s in r['summary'] if function+':' in s)
    assert "1'bx" in line and 'input `a`' in line
    assert 'return type does not determine' in line
    if function=='$fell':assert 'was 1, X or Z' in line


def test_sampled_current_value_does_not_fabricate_a_history_requirement():
    r=complete('$sampled(a)')
    call=node(r,'sample')['facts']['expression']['calls'][0]
    assert 'history_default' not in call
    assert any('no prior history is required' in s for s in r['summary'])


def test_two_state_and_vector_history_defaults_follow_the_frontend_input_type():
    bit=complete('$stable(two)',declarations='bit two;')
    vector=complete('$fell(data)')
    assert node(bit,'sample')['facts']['expression']['calls'][0]['history_default']['value']=="1'b0"
    assert node(vector,'sample')['facts']['expression']['calls'][0]['history_default']['value']=="4'bxxxx"


def test_summary_distinguishes_initial_activation_from_concurrent_declaration():
    initial=complete('module m; logic clk,a,b; initial begin ap: assert property(@(posedge clk) a |-> b); end endmodule')
    concurrent=complete('module m; logic clk,a,b; ap: assert property(@(posedge clk) a |-> b); endmodule')
    assert any('AT MOST ONE' in s and 'do not restart' in s for s in initial['summary'])
    assert any('new attempt at each leading clock event' in s for s in concurrent['summary'])
    assert any('Within ONE attempt' in s for s in initial['summary'])


def test_initial_site_bound_is_not_an_observed_runtime_activation():
    r=complete('module m; bit clk,a,b; initial if(a) ap: assert property(@(posedge clk) b); endmodule')
    s=r['directive']['activation']['schedule']
    assert s['policy']=='at_most_one_initial_encounter'
    assert s['max_site_encounters_per_process_start']==1
    assert s['zero_encounters_possible']
    assert s['actual_attempt_count']==s['first_start_time']=='not_observed'


@pytest.mark.parametrize('body,control',[
    ('repeat(2) ap: assert property(@(posedge clk) a);','RepeatLoopStatement'),
    ('while(a) ap: assert property(@(posedge clk) a);','WhileLoopStatement'),
    ('do ap: assert property(@(posedge clk) a); while(a);','DoWhileStatement'),
    ('forever ap: assert property(@(posedge clk) a);','ForeverStatement'),
    ('for(int i=0;i<2;i++) ap: assert property(@(posedge clk) a);','ForLoopStatement'),
    ('foreach(v[i]) ap: assert property(@(posedge clk) a);','ForeachLoopStatement')])
def test_initial_loop_is_never_misclassified_as_one_encounter(body,control):
    r=complete('module m; bit clk,a; bit[1:0] v; initial '+body+' endmodule')
    activation=r['directive']['activation'];schedule=activation['schedule']
    assert any(c['kind']==control for c in activation['controls'])
    assert schedule['policy']=='per_procedural_encounter'
    assert schedule['max_site_encounters_per_process_start'] is None
    assert not any('AT MOST ONE' in s for s in r['summary'])


def test_concurrent_and_always_activation_have_distinct_schedules():
    decl=complete('module m; bit clk,a; ap: assert property(@(posedge clk) a); endmodule')
    proc=complete('module m; bit clk,a; always @(posedge clk) ap: assert property(a); endmodule')
    assert decl['directive']['activation']['schedule']['policy']=='each_leading_clock_event'
    assert proc['directive']['activation']['schedule']['policy']=='per_procedural_encounter'
    fragment=complete('a')
    assert fragment['directive']['activation']['schedule']['policy']=='caller_activation_required'


@pytest.mark.parametrize('directive,strength',[
    ('assert property','weak'),('assume property','weak'),
    ('cover property','strong'),('expect','strong')])
def test_sequence_property_default_completion_follows_the_calling_statement(directive,strength):
    r=complete(directive+'(a[->2]);')
    assert r['directive']['completion_policy']['default_sequence_strength']==strength
    rule=node(r,'sample')['facts']['sequence_property']
    assert rule['effective_strength']==strength
    assert rule['runtime_outcome']=='not_evaluated'
    assert rule['finite_bad_prefix']=='all_legal_future_matches_for_this_sequence_start_are_impossible'


@pytest.mark.parametrize('text',['a[->2]','property p; a[->2]; endproperty'])
def test_bare_fragment_or_definition_does_not_inherit_its_synthetic_assert_strength(text):
    r=complete(text)
    policy=r['directive']['completion_policy']
    assert policy['default_sequence_strength']=='caller_dependent'
    assert policy['calling_statement_defaults']['assert_property']=='weak'
    assert node(r,'sample')['facts']['sequence_property']['effective_strength']=='caller_dependent'


def test_explicit_strength_overrides_the_assert_default_without_leaking_into_sequence_components():
    r=complete('assert property(strong(a[->2] ##1 b));')
    assert r['directive']['completion_policy']['default_sequence_strength']=='weak'
    assert node(r,'strong')['facts']['sequence_property']['effective_strength']=='strong'
    assert all('sequence_property' not in n['facts'] for n in r['nodes'] if n['op'] in {'concat','sample'})


def test_sequence_completion_is_attached_to_consequent_property_not_antecedent_matches():
    r=complete('assert property((a ##1 b) |=> c[->2]);')
    imp=node(r,'NonOverlappedImplication')
    ant=next(n for n in r['nodes'] if n['id']==imp['facts']['antecedent'])
    con=next(n for n in r['nodes'] if n['id']==imp['facts']['consequent'])
    assert 'sequence_property' not in ant['facts']
    assert con['facts']['sequence_property']['effective_strength']=='weak'


def test_no_completion_is_distinct_from_finite_bad_prefix_and_actual_end():
    r=complete('assert property(a[=2:4]);')
    rule=node(r,'sample')['facts']['sequence_property']
    assert rule['failure_requires']=='finite_bad_prefix'
    assert rule['no_completion_yet']=='pending_if_a_legal_future_match_remains_possible'
    assert rule['requested_window_cutoff']=='cutoff_alone_does_not_establish_completion_or_failure'
    assert rule['unfinished_at_actual_simulation_end'].startswith('missing_completion_alone_is_not_failure')
    assert any('THAT still-extendible weak attempt' in s for s in r['summary'])
    neg=complete('assert property(not a[->2]);')
    assert any('Negate' in s for s in neg['summary'])
    assert 'enclosing_operators_still_apply' in node(neg,'sample')['facts']['sequence_property']['scope']


@pytest.mark.parametrize('text',['a','$rose(a)','strong(a)','weak(a)'])
def test_required_single_sample_cannot_be_excused_as_weak_waiting(text):
    r=complete('assert property('+text+');')
    rules=[n['facts']['sequence_property'] for n in r['nodes'] if 'sequence_property' in n['facts']]
    assert len(rules)==1
    assert rules[0]['match_class']=='required_single_sample'
    assert rules[0]['decisive_sample']=='true_matches; false_X_Z_boolean_result_is_a_finite_bad_prefix_at_the_required_tick'
    assert rules[0]['later_values']=='cannot_repair_this_supplied_start'
    assert any('FALSE immediately, even if WEAK' in s for s in r['summary'])
    assert not any('THAT still-extendible weak attempt' in s for s in r['summary'])


@pytest.mark.parametrize('text',['a[->2]','a[=2]','strong(a[->2])'])
def test_occurrence_wait_is_not_confused_with_failed_required_single_sample(text):
    r=complete('assert property('+text+');')
    rule=next(n['facts']['sequence_property'] for n in r['nodes'] if 'sequence_property' in n['facts'])
    assert rule['match_class']=='occurrence_wait'
    assert rule['false_gap']=='may_wait_for_later_occurrences_if_a_legal_continuation_remains'
    assert 'decisive_sample' not in rule
    assert not any('FALSE immediately, even if WEAK' in s for s in r['summary'])


@pytest.mark.parametrize('text',['a ##1 b','a[*1:2]','a[*0:2] ##1 b','a throughout b[->2]'])
def test_composed_sequence_bad_prefix_does_not_turn_any_false_sample_into_failure(text):
    r=complete('assert property('+text+');')
    rule=next(n['facts']['sequence_property'] for n in r['nodes'] if 'sequence_property' in n['facts'])
    assert rule['match_class']=='composed_or_repeated_sequence'
    assert rule['all_candidates_eliminated'].startswith('finite_bad_prefix;')
    assert 'decisive_sample' not in rule


def test_check_failure_mapping_is_whole_property_scoped_after_negation():
    r=complete('assert property(not a);')
    mapping=r['directive']['completion_policy']['whole_property_outcomes']
    assert mapping==dict(true='check_satisfied',false='check_failed',disabled='neither_success_nor_failure',
        pending='no_completed_result_yet',scope='whole_property_after_enclosing_operators; not_each_operand')
    assert any('Negate' in s for s in r['summary'])
    assert any('After applying ALL enclosing operators' in s for s in r['summary'])


def test_single_tick_implication_does_not_wait_for_a_later_true_antecedent():
    r=complete('a |-> b')
    facts=node(r,'OverlappedImplication')['facts']
    assert facts['single_sample_antecedent']['tick']=='this_supplied_implication_start'
    assert facts['single_tick_implication']['sample_scope']=='this_supplied_start_only'
    assert facts['single_tick_implication']['future_ticks'].startswith('do_not_recheck_this_completed_implication')
    assert any('does not wait until a later true tick' in s for s in r['summary'])
    assert any('check the consequent NOW and finish' in s for s in r['summary'])


def test_single_sample_scope_survives_named_definitions_and_explicit_clocks():
    r=complete('left_s |-> right_p',declarations='sequence left_s; @(posedge clk) a; endsequence property right_p; @(posedge clk) b; endproperty')
    assert 'single_tick_implication' in node(r,'OverlappedImplication')['facts']
    reference=complete('s',declarations='sequence s; a; endsequence')
    assert node(reference,'reference')['facts']['sequence_property']['match_class']=='required_single_sample'


@pytest.mark.parametrize('text',[
    '(a ##1 b) |-> c','a[->1] |-> b','a |=> b','a |-> @(posedge clk2) b','a |-> not b'])
def test_temporal_or_cross_clock_implication_is_not_collapsed_to_one_tick(text):
    r=complete(text)
    n=next(n for n in r['nodes'] if n['op'].endswith('Implication'))
    assert 'single_tick_implication' not in n['facts']
    if text.startswith('(a') or text.startswith('a['):
        assert 'single_sample_antecedent' not in n['facts']


@pytest.mark.parametrize('function,typ,predicate',[
    ('$rose','logic','current_LSB_is_1'),('$fell','logic','current_LSB_is_0'),
    ('$stable','logic','all_current_bits_are_X'),('$changed','logic','not_all_current_bits_are_X'),
    ('$rose','bit','current_LSB_is_1'),('$fell','bit','always_false_with_prior_0'),
    ('$stable','bit','all_current_bits_are_0'),('$changed','bit','not_all_current_bits_are_0')])
def test_first_sampling_rules_use_symbolic_input_defaults_without_reading_runtime(function,typ,predicate):
    r=complete(function+'(input_value)',declarations=typ+' [3:0] input_value;')
    call=node(r,'sample')['facts']['expression']['calls'][0]
    first=call['first_sample']
    assert first['true_iff']==predicate
    assert first['runtime_value']=='not_evaluated'
    assert 'not_each_attempt_or_wave_window_start' in first['scope']
    assert any('FIRST sampling event without prior history' in s for s in r['summary'])
    if function in {'$stable','$changed'}:
        assert any('different, not an unknown comparison' in s for s in r['summary'])


def test_first_sample_specialization_keeps_mixed_or_missing_defaults_unresolved():
    from src.property_history import first_sample_rules
    for value in ['',"4'bxx00","8'hf0",'named_default',"4'b1","32'd11","4'h1"]:
        assert first_sample_rules('$fell',{'value':value}) is None
    assert first_sample_rules('$past',{'value':"1'bx"}) is None


@pytest.mark.parametrize('expression,declarations,value',[
    ('initialized','logic initialized=0;',"1'b0"),
    ('initialized','logic initialized=1;',"1'b1"),
    ('a === b','',"1'b1"),
    ('a !== b','',"1'b0"),
    ('1\'b1','',"1'b1"),
    ('!initialized','logic initialized=0;',"1'b1")])
def test_default_sampled_value_uses_initializers_and_recursive_expression_defaults(expression,declarations,value):
    r=complete('$changed('+expression+')',declarations=declarations)
    default=node(r,'sample')['facts']['expression']['calls'][0]['history_default']
    assert default['status']=='resolved'
    assert default['value']==value
    assert 'declaration initializers and component defaults' in ' '.join(r['summary'])


def test_nonconstant_static_initializer_is_not_replaced_by_a_type_default():
    r=complete('$fell(initialized)',declarations='logic initialized=a;')
    call=node(r,'sample')['facts']['expression']['calls'][0]
    assert call['history_default']['status']=='symbolic'
    assert call['history_default']['unresolved_reason']=='static_initializer_not_a_bounded_constant'
    assert 'value' not in call['history_default'] and 'first_sample' not in call
    assert any('default sampled value' in s and 'unresolved' in s for s in r['summary'])


def test_history_default_folding_does_not_invoke_initializer_functions():
    r=complete('$fell(initialized)',declarations='function automatic logic init_fn(); return 0; endfunction logic initialized=init_fn();')
    default=node(r,'sample')['facts']['expression']['calls'][0]['history_default']
    assert default['status']=='symbolic' and 'value' not in default


def test_sequence_cover_and_immediate_assertions_do_not_claim_sequence_property_default_strength():
    cover=complete('cover sequence(a ##1 b);')
    immediate=complete('assert(a);')
    for r in [cover,immediate]:
        assert r['directive']['completion_policy']['default_sequence_strength']=='not_applicable'
        assert all('sequence_property' not in n['facts'] for n in r['nodes'])


def test_summary_bounds_are_explicit_when_more_semantic_entries_exist():
    from src.property_summary import summarize_property
    r=complete('(a |-> b) and (b |-> c)')
    summary=summarize_property(r,max_lines=3)
    assert len(summary)==3
    assert 'Additional semantic entries omitted' in summary[-1]


def test_named_sequence_repetition_has_sequence_scope_without_fabricating_a_boolean_base():
    r=complete('s[*2]',declarations='sequence s; a ##2 b; endsequence')
    f=node(r,'reference')['facts']
    assert f['match_scope']=='entire_repeated_sequence'
    assert 'base_match' not in f
    assert f['repetition']['count']==dict(min=2,max=2,unbounded=False)


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
