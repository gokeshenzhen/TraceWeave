"""Resolve typed evidence against retained tool facts, never generated values."""
from copy import deepcopy
import threading
import time

import pytest

from src import cancellation
from src.property_evidence import attach_evidence_references, resolve_property_evidence


def receipt():
    sample=dict(source='design.data',signal='tb.bus[7:0]',bits=[5,1],tick=0,
                time_ps=1,time_fs=100,value={'bin':'xz'})
    clock=dict(source='design.clk',signal='tb.clk',tick=0,time_ps=1,time_fs=100,value={'bin':'0'})
    expression=dict(node='n1',tick=0,time_ps=1,expression='$isunknown(data)',
                    value={'bin':'1','hex':'0x1','dec':1},locals={})
    return attach_evidence_references(dict(mode='property',output_truncated=False,
        attempts=[dict(sampled_values=[sample],clock_sample=clock,evidence=[expression])]))


def references(result):
    a=result['attempts'][0]
    return dict(check_id=result['evidence_context']['check_id'],
        signal_evidence_ids=[a['sampled_values'][0]['evidence_id'],a['clock_sample']['evidence_id']],
        expression_evidence_ids=[a['evidence'][0]['evidence_id']])


def test_resolver_keeps_physical_and_derived_facts_separate_with_exact_bits_and_phase():
    result=receipt();refs=references(result)
    assembled=resolve_property_evidence(result,refs)
    data,clock=assembled['signal_facts']
    assert data['signal']=='tb.bus[7:0]' and data['bits']==[5,1]
    assert data['value']=={'bin':'xz'}
    assert data['time_ps']==1 and data['time_fs']==100
    assert data['sample_phase']=='before' and data['sample_offset_ps']==0
    assert clock['kind']=='clock_sample' and clock['value']['bin']=='0'
    assert assembled['expression_facts'][0]['expression']=='$isunknown(data)'
    assert assembled['expression_facts'][0]['value']['bin']=='1'
    assert not assembled['display_truncated']
    assembled['signal_facts'][0]['value']['bin']='11'
    assert result['attempts'][0]['sampled_values'][0]['value']['bin']=='xz'


def test_ids_are_stable_for_identical_facts_but_checks_cannot_be_crossed():
    first=receipt();second=receipt()
    assert first['attempts']==second['attempts']
    assert first['evidence_context']['check_id']!=second['evidence_context']['check_id']
    with pytest.raises(ValueError,match='property_evidence_check_mismatch'):
        resolve_property_evidence(second,references(first))


def test_a_changed_value_or_selection_cannot_reuse_an_old_id():
    for field,value in [('value',{'bin':'00'}),('bits',[1,5]),('time_fs',101)]:
        result=receipt();refs=references(result)
        result['attempts'][0]['sampled_values'][0][field]=value
        with pytest.raises(ValueError,match='property_evidence_identity_changed'):
            resolve_property_evidence(result,refs)


def test_removed_display_rows_and_wrong_kinds_are_rejected():
    result=receipt();refs=references(result)
    invalid=deepcopy(refs);invalid['signal_evidence_ids']=[refs['expression_evidence_ids'][0]]
    invalid['expression_evidence_ids']=[]
    with pytest.raises(ValueError,match='property_evidence_kind_mismatch'):
        resolve_property_evidence(result,invalid)
    result['attempts'][0]['sampled_values']=[];result['output_truncated']=True
    with pytest.raises(ValueError,match='property_evidence_reference_unavailable'):
        resolve_property_evidence(result,refs)
    refs['signal_evidence_ids']=refs['signal_evidence_ids'][1:]
    assert resolve_property_evidence(result,refs)['display_truncated']


@pytest.mark.parametrize('edit,reason',[
    (lambda r:r.update(signal_evidence_ids='s_bad'),'references_invalid'),
    (lambda r:r.update(signal_evidence_ids=[123]),'references_invalid'),
    (lambda r:r.update(signal_evidence_ids=['s_bad']*257),'reference_limit'),
    (lambda r:r['signal_evidence_ids'].append(r['signal_evidence_ids'][0]),'duplicate_reference'),
    (lambda r:r.update(value='0'),'references_invalid'),
])
def test_reference_input_is_bounded_and_never_accepts_generated_values(edit,reason):
    result=receipt();refs=references(result);edit(refs)
    with pytest.raises(ValueError,match='property_evidence_'+reason):
        resolve_property_evidence(result,refs)


def test_cancellation_and_annotation_deadline_remain_observable():
    result=receipt();refs=references(result)
    event=threading.Event();event.set();token=cancellation.push_cancel_event(event)
    try:
        with pytest.raises(cancellation.OperationCancelled):resolve_property_evidence(result,refs)
    finally:cancellation.pop_cancel_event(token)
    with pytest.raises(TimeoutError,match='property_timeout'):
        attach_evidence_references(result,deadline=time.monotonic()-1)
