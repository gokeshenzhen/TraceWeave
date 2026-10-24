"""Scope-checked client assembly of tool verdicts and model interpretation.

Only the client's retained original tool response is trusted. No waveform I/O,
oracle, prose classification or model-supplied replacement receipt is accepted.
"""
from __future__ import annotations

from copy import deepcopy

from pydantic import BaseModel

from .cancellation import check_cancelled
from .property_evidence import resolve_property_evidence

_VERDICTS = {'reproduced', 'not_reproduced', 'insufficient_evidence'}


def _start(event):
    fields = event.get('structured_fields') or event.get('parsed_fields') or {}
    if not isinstance(fields, dict):
        raise ValueError('property_verdict_anchor_invalid')
    nested = fields.get('start_time_ps')
    flat = event.get('start_time_ps')
    if nested is not None and flat is not None and nested != flat:
        raise ValueError('property_verdict_anchor_invalid')
    return nested if nested is not None else flat


def _anchor(event):
    if not isinstance(event, dict):
        raise ValueError('property_verdict_anchor_invalid')
    at = event.get('time_ps'); start = _start(event)
    if (type(at) is not int or not 0 <= at < 2**63 or
            not isinstance(event.get('instance_path'), str) or not event['instance_path'] or
            event.get('time_parse_status', 'exact') != 'exact' or
            event.get('failure_source', 'assertion') != 'assertion' or
            start is not None and (type(start) is not int or not 0 <= start <= at)):
        raise ValueError('property_verdict_anchor_invalid')
    return dict(time_ps=at, instance_path=event['instance_path'], start_time_ps=start)


def resolve_property_verdict(result, *, wave_path, failure_event,
                             plan_fingerprint=None, check_id=None):
    """Return an authoritative local verdict only for the supplied request.

    Unknown/pending/unsupported receipts return verdict=None, authoritative=False.
    A non-reproduction is local to this event; it never means a whole-run pass.
    Identity/anchor mismatches raise rather than borrowing another attempt's result.
    """
    check_cancelled()
    expected = _anchor(failure_event)
    if isinstance(result, BaseModel):
        result = result.model_dump(mode='json')
    if not isinstance(result, dict) or result.get('mode') != 'property':
        raise ValueError('property_verdict_result_required')
    if result.get('wave_path') != wave_path:
        raise ValueError('property_verdict_wave_mismatch')
    context = result.get('evidence_context') or {}
    if check_id is not None and context.get('check_id') != check_id:
        raise ValueError('property_verdict_check_mismatch')
    fingerprint = (result.get('plan') or {}).get('fingerprint')
    if plan_fingerprint is not None and fingerprint != plan_fingerprint:
        raise ValueError('property_verdict_plan_mismatch')
    decision = dict(authoritative=False, verdict=None, scope='supplied_failure_event',
        check_id=context.get('check_id'), plan_fingerprint=fingerprint,
        tool_status=result.get('status'), execution_status=result.get('execution_status'),
        coverage_status=result.get('coverage_status'),
        error_code=result.get('error_code'),
        report_time_ps=expected['time_ps'], attempt_start_ps=expected['start_time_ps'],
        instance_path=expected['instance_path'], gap_codes=list(result.get('gap_codes', [])))
    def unresolved(reason):
        return dict(decision, reason_code=reason)
    actual = result.get('failure_event') or {}
    if not actual:
        return unresolved('property_verdict_anchor_unavailable')
    observed = _anchor(actual)
    if any(observed[k] != expected[k] for k in ('time_ps', 'instance_path')):
        raise ValueError('property_verdict_anchor_mismatch')
    if failure_event.get('event_id') is not None and actual.get('event_id') != failure_event['event_id']:
        raise ValueError('property_verdict_event_mismatch')
    target = result.get('target') or {}
    if (target.get('kind') == 'directive' and target.get('path') and
            not target['path'].startswith('__tw_property.') and
            target['path'] != expected['instance_path']):
        raise ValueError('property_verdict_target_mismatch')
    selection = result.get('attempt_selection') or {}
    attempts = result.get('attempts') or []
    actual_start = observed['start_time_ps']
    if actual_start is None and selection.get('basis') == 'logged_attempt_start':
        # Compatibility with version-1 receipts that retained the logged start
        # only in the single selected attempt. Never infer it from a window edge.
        starts = {a.get('start_time_ps') for a in attempts}
        if selection.get('candidate_count') == selection.get('checked_count') == 1 and len(starts) == 1:
            actual_start = next(iter(starts))
    if actual_start is not None and (type(actual_start) is not int or not 0 <= actual_start <= observed['time_ps']):
        raise ValueError('property_verdict_anchor_invalid')
    if expected['start_time_ps'] is not None:
        if actual_start is None:
            return unresolved('property_verdict_start_unverified')
        if actual_start != expected['start_time_ps']:
            raise ValueError('property_verdict_start_mismatch')
    elif actual_start is not None:
        decision['attempt_start_ps'] = actual_start
    if result.get('error_code') is not None:
        return unresolved('property_verdict_execution_error')
    if context.get('version') != 1 or not isinstance(context.get('check_id'), str):
        return unresolved('property_verdict_context_unavailable')
    if result.get('status') == 'failure_reproduced':
        if (result.get('execution_status') not in {'partial', 'completed'} or
                result.get('coverage_status') not in {'partial', 'complete'} or
                type(result.get('reproduced_count')) is not int or result['reproduced_count'] < 1):
            return unresolved('property_verdict_positive_unverified')
        # A retained positive witness must agree. Display trimming may remove the
        # witness, but never invalidates the trusted aggregate local conclusion.
        if not result.get('attempts_truncated'):
            witnesses = [a for a in attempts if a.get('matches_failure_event') is True and
                a.get('status') == 'false' and type(a.get('end_time_ps')) is int and
                a['end_time_ps'] <= expected['time_ps'] and
                (expected['start_time_ps'] is None or a.get('start_time_ps') == expected['start_time_ps'])]
            if not witnesses:return unresolved('property_verdict_positive_unverified')
        decision.update(authoritative=True, verdict='reproduced')
    elif result.get('status') == 'not_reproduced':
        checked = selection.get('checked_count')
        if (result.get('execution_status') != 'completed' or result.get('coverage_status') != 'complete' or
                selection.get('complete') is not True or type(checked) is not int or checked < 1 or
                selection.get('candidate_count') != checked or type(result.get('reproduced_count')) is not int or
                result['reproduced_count'] != 0 or
                any(a.get('status') in {'pending', 'unknown'} or a.get('analysis_limited') for a in attempts)):
            return unresolved('property_verdict_negative_incomplete')
        decision.update(authoritative=True, verdict='not_reproduced')
    else:
        return unresolved('property_verdict_not_decisive')
    return dict(decision, reason_code='property_verdict_from_verified_tool')


def assemble_property_answer(result, proposal, *, wave_path, failure_event,
                             references=None, plan_fingerprint=None):
    """Assemble a final decision while retaining the unverified model proposal.

    An unresolved tool receipt leaves the proposal as an explicit model fallback.
    Model prose is separate from the program-generated decision description and
    is never certified merely because the verdict or sample facts are correct.
    """
    check_cancelled()
    if not isinstance(proposal, dict) or proposal.get('verdict') not in _VERDICTS:
        raise ValueError('property_verdict_proposal_invalid')
    decision = resolve_property_verdict(result, wave_path=wave_path, failure_event=failure_event,
        plan_fingerprint=plan_fingerprint,
        check_id=references.get('check_id') if isinstance(references, dict) else None)
    answer = {k: deepcopy(proposal[k]) for k in ('case_id', 'verdict', 'attempt_start_ps',
        'report_time_ps', 'reason', 'confidence', 'evidence', 'property_evidence') if k in proposal}
    answer['model_proposal'] = deepcopy(proposal)
    if 'confidence' in proposal:answer['confidence_source'] = 'model_proposal'
    answer['model_interpretation'] = proposal.get('reason', '')
    answer['model_interpretation_status'] = 'not_independently_verified'
    answer['decision'] = decision
    answer['verdict_source'] = 'property_tool' if decision['authoritative'] else 'model_fallback'
    answer['proposal_conflicts'] = []
    if decision['authoritative']:
        for field in ('verdict', 'attempt_start_ps', 'report_time_ps'):
            value = decision[field]
            if proposal.get(field) != value:
                answer['proposal_conflicts'].append(field)
            answer[field] = value
        answer['reason'] = ('The property tool reproduced the supplied failure event.'
            if decision['verdict'] == 'reproduced' else
            'The complete property check did not reproduce the supplied failure event.')
    if references is not None:
        answer['resolved_evidence'] = resolve_property_evidence(result, references)
    return answer
