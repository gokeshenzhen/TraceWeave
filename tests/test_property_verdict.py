"""Local tool verdicts cannot be reversed or borrowed from another request."""
import threading

import pytest

from src import cancellation
from src.property_verdict import assemble_property_answer, resolve_property_verdict


def receipt(status='failure_reproduced'):
    positive=status=='failure_reproduced'
    return dict(mode='property',wave_path='owned.vcd',status=status,
        execution_status='completed',coverage_status='complete',error_code=None,
        evidence_context={'version':1,'check_id':'pc_one'},plan={'fingerprint':'fingerprint-one'},
        target={'kind':'directive','path':'tb.ap'},
        failure_event={'time_ps':35,'start_time_ps':5,'instance_path':'tb.ap'},
        attempt_selection={'basis':'logged_attempt_start','candidate_count':1,'checked_count':1,'complete':True},
        attempts=[{'start_time_ps':5,'end_time_ps':35,'status':'false' if positive else 'true',
                   'matches_failure_event':positive,'analysis_limited':False}],
        attempts_truncated=False,reproduced_count=int(positive),gap_codes=[])


def request():
    return dict(wave_path='owned.vcd',failure_event={'time_ps':35,'instance_path':'tb.ap',
        'structured_fields':{'start_time_ps':5}},plan_fingerprint='fingerprint-one')


def proposal():
    return dict(case_id='case',verdict='not_reproduced',attempt_start_ps=0,report_time_ps=30,
                reason='model interpretation says satisfied',confidence=.5)


def test_program_decision_retains_conflicting_model_proposal_without_certifying_its_prose():
    original=proposal();result=receipt()
    answer=assemble_property_answer(result,original,**request())
    assert answer['verdict']=='reproduced' and answer['verdict_source']=='property_tool'
    assert answer['attempt_start_ps']==5 and answer['report_time_ps']==35
    assert set(answer['proposal_conflicts'])=={'verdict','attempt_start_ps','report_time_ps'}
    assert answer['model_proposal']==original
    assert answer['model_interpretation']==original['reason'] and answer['reason']!=original['reason']
    assert answer['model_interpretation_status']=='not_independently_verified'
    assert answer['decision']['scope']=='supplied_failure_event'
    assert original==proposal() and result==receipt()


@pytest.mark.parametrize('edit,error',[
    (lambda r:r.update(wave_path='other.fst'),'wave_mismatch'),
    (lambda r:r['failure_event'].update(time_ps=45),'anchor_mismatch'),
    (lambda r:r['failure_event'].update(instance_path='tb.other'),'anchor_mismatch'),
    (lambda r:r['failure_event'].update(start_time_ps=15),'start_mismatch'),
    (lambda r:r['target'].update(path='tb.other'),'target_mismatch'),
    (lambda r:r['plan'].update(fingerprint='other'),'plan_mismatch'),
])
def test_scope_and_identity_mismatches_cannot_supply_a_verdict(edit,error):
    r=receipt();edit(r)
    with pytest.raises(ValueError,match='property_verdict_'+error):resolve_property_verdict(r,**request())


def test_check_and_event_ids_cannot_be_crossed():
    with pytest.raises(ValueError,match='check_mismatch'):
        resolve_property_verdict(receipt(),check_id='pc_other',**request())
    expected=request();expected['failure_event']['event_id']='event-one'
    r=receipt();r['failure_event']['event_id']='event-two'
    with pytest.raises(ValueError,match='event_mismatch'):resolve_property_verdict(r,**expected)


@pytest.mark.parametrize('field,value',[
    ('time_ps',True),('time_parse_status','approximate'),('failure_source','scoreboard'),
    ('instance_path',''),('start_time_ps',-1),
])
def test_untrusted_or_uncertain_request_anchor_is_rejected(field,value):
    expected=request();expected['failure_event'][field]=value
    with pytest.raises(ValueError,match='anchor_invalid'):resolve_property_verdict(receipt(),**expected)


@pytest.mark.parametrize('status',['insufficient_evidence','unsupported'])
def test_missing_history_and_pending_never_become_tool_negatives(status):
    r=receipt(status);r.update(execution_status='partial',coverage_status='partial',
        gap_codes=['history_before_capture_unavailable','property_pending_at_report_cutoff'])
    r['attempts'][0]['status']='pending'
    decision=resolve_property_verdict(r,**request())
    assert decision['verdict'] is None and not decision['authoritative']
    answer=assemble_property_answer(r,proposal(),**request())
    assert answer['verdict']=='not_reproduced' and answer['verdict_source']=='model_fallback'
    assert answer['decision']['gap_codes']==r['gap_codes']


def test_partial_positive_and_earlier_same_attempt_failure_stay_local_facts():
    r=receipt();r.update(execution_status='partial',coverage_status='partial')
    r['attempt_selection']['complete']=False;r['attempts'][0]['end_time_ps']=25
    result=resolve_property_verdict(r,**request())
    assert result['authoritative'] and result['verdict']=='reproduced'
    assert result['report_time_ps']==35 and result['attempt_start_ps']==5


@pytest.mark.parametrize('field,value',[('end_time_ps',45),('start_time_ps',15)])
def test_a_later_failure_or_another_attempt_cannot_supply_the_positive_witness(field,value):
    r=receipt();r['attempts'][0][field]=value
    assert not resolve_property_verdict(r,**request())['authoritative']


@pytest.mark.parametrize('state',['true','disabled'])
def test_complete_satisfied_disabled_or_vacuous_attempt_is_only_a_local_non_reproduction(state):
    r=receipt('not_reproduced');r['attempts'][0].update(status=state,vacuous=True)
    decision=resolve_property_verdict(r,**request())
    assert decision['authoritative'] and decision['verdict']=='not_reproduced'
    assert decision['scope']=='supplied_failure_event'


@pytest.mark.parametrize('edit',[
    lambda r:r.update(execution_status='partial'),
    lambda r:r.update(coverage_status='partial'),
    lambda r:r['attempt_selection'].update(complete=False),
    lambda r:r['attempt_selection'].update(checked_count=0),
    lambda r:r['attempts'][0].update(status='pending'),
    lambda r:r.update(error_code='property_sampling_incomplete'),
])
def test_incomplete_negatives_are_never_authoritative(edit):
    r=receipt('not_reproduced');edit(r)
    assert not resolve_property_verdict(r,**request())['authoritative']


def test_trimmed_attempts_use_retained_canonical_anchor_and_legacy_receipts_use_logged_start():
    r=receipt();r.update(attempts=[],attempts_truncated=True)
    assert resolve_property_verdict(r,**request())['authoritative']
    del r['failure_event']['start_time_ps']
    assert not resolve_property_verdict(r,**request())['authoritative']
    r=receipt();del r['failure_event']['start_time_ps']
    assert resolve_property_verdict(r,**request())['authoritative']
    r['attempt_selection']['basis']='bounded_temporal_horizon'
    assert not resolve_property_verdict(r,**request())['authoritative']


def test_model_cannot_inject_resolved_evidence_or_authority_fields():
    p=proposal();p.update(resolved_evidence={'fake':True},verdict_source='fake',decision={'fake':True})
    answer=assemble_property_answer(receipt(),p,**request())
    assert 'resolved_evidence' not in answer
    assert answer['verdict_source']=='property_tool' and 'fake' not in answer['decision']
    assert answer['model_proposal']==p


def test_cancellation_remains_observable():
    event=threading.Event();event.set();token=cancellation.push_cancel_event(event)
    try:
        with pytest.raises(cancellation.OperationCancelled):resolve_property_verdict(receipt(),**request())
    finally:cancellation.pop_cancel_event(token)
