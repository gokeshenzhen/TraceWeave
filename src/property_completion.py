"""Static sequence-as-property completion rules, not observed run outcomes."""
from __future__ import annotations


def completion_policy(directive):
    kind=directive.get('kind')
    policy=dict(runtime_outcome='not_evaluated',
                requested_window_end='not_automatically_the_actual_simulation_end',
                enclosing_operators='negation_abort_and_property_composition_still_apply')
    if kind in {'Assert','Assume'}:
        policy['whole_property_outcomes']=dict(true='check_satisfied',false='check_failed',
            disabled='neither_success_nor_failure',pending='no_completed_result_yet',
            scope='whole_property_after_enclosing_operators; not_each_operand')
    if directive.get('form')=='immediate':
        policy.update(default_sequence_strength='not_applicable',basis='immediate_value_check')
    elif kind=='CoverSequence':
        policy.update(default_sequence_strength='not_applicable',basis='sequence_cover_seeks_a_match')
    elif kind in {'Assert','Assume'}:
        policy.update(default_sequence_strength='weak',basis='assert_or_assume_property_default')
    elif kind in {'CoverProperty','Expect','Restrict'}:
        policy.update(default_sequence_strength='strong',basis='other_property_statement_default')
    else:
        policy.update(default_sequence_strength='caller_dependent',basis='definition_or_fragment_has_no_calling_statement',
                      calling_statement_defaults={'assert_property':'weak','assume_property':'weak','other_property_statements':'strong'})
    return policy


def sequence_property_rules(directive, explicit=None, match_node=None):
    policy=directive.get('completion_policy') or completion_policy(directive)
    strength=explicit or policy['default_sequence_strength']
    row=dict(effective_strength=strength,basis='explicit_strength_operator' if explicit else policy['basis'],
        scope='this_sequence_property_at_its_supplied_start; enclosing_operators_still_apply',
        matching_completion='one_nonempty_match_suffices',
        finite_bad_prefix='all_legal_future_matches_for_this_sequence_start_are_impossible',
        requested_window_cutoff='cutoff_alone_does_not_establish_completion_or_failure',
        runtime_outcome='not_evaluated')
    # A false required Boolean at its prescribed tick is already a bad prefix.
    # Occurrence repetitions can instead wait through false ticks. Never apply
    # the single-sample failure rule to their base occurrence or a composition.
    match_node=match_node or {}
    repetition=match_node.get('facts',{}).get('repetition',{})
    if match_node.get('op')=='sample' and not repetition:
        row.update(match_class='required_single_sample',
            decisive_sample='true_matches; false_X_Z_boolean_result_is_a_finite_bad_prefix_at_the_required_tick',
            later_values='cannot_repair_this_supplied_start')
    elif match_node.get('op')=='sample' and repetition.get('kind') in {'GoTo','Nonconsecutive'}:
        row.update(match_class='occurrence_wait',
            false_gap='may_wait_for_later_occurrences_if_a_legal_continuation_remains')
    else:
        row['match_class']='composed_or_repeated_sequence'
    row.update(required_candidate_mismatch='eliminates_that_candidate_at_its_prescribed_ticks; no_retry_of_that_sample_at_a_later_tick',
        all_candidates_eliminated='finite_bad_prefix; this_sequence_property_is_false_for_both_weak_and_strong')
    if strength=='weak':
        row.update(truth_requirement='no_finite_bad_prefix',
                   failure_requires='finite_bad_prefix',
                   no_completion_yet='pending_if_a_legal_future_match_remains_possible',
                   unfinished_at_actual_simulation_end='missing_completion_alone_is_not_failure_of_this_weak_sequence_property')
    elif strength=='strong':
        row.update(truth_requirement='nonempty_matching_completion',
                   failure_requires='finite_bad_prefix_or_actual_end_without_nonempty_completion',
                   no_completion_yet='pending_while_a_future_nonempty_match_remains_possible',
                   unfinished_at_actual_simulation_end='not_satisfied_without_nonempty_completion')
    else:
        row.update(truth_requirement='calling_statement_or_explicit_strength_required',
                   failure_requires='depends_on_effective_strength',
                   no_completion_yet='absence_of_match_alone_is_not_a_runtime_verdict',
                   unfinished_at_actual_simulation_end='depends_on_effective_strength')
    return row
