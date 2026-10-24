"""Typed property evidence references and pure client-side fact assembly.

The client retains the original tool response and lets the model select IDs.
Only that retained response, never model-supplied signal/value dictionaries,
is passed to the resolver. No waveform I/O, live handles or global stores exist.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import secrets
import time

from pydantic import BaseModel

from .cancellation import check_cancelled

MAX_REFERENCES = 256
MAX_RECORDS = 4096
_SIGNAL_KINDS = {'signal_sample', 'clock_sample'}
_SAMPLE_FIELDS = ('kind', 'source', 'signal', 'bits', 'tick', 'time_ps', 'time_fs',
                  'sample_phase', 'sample_offset_ps', 'value')
_EXPRESSION_FIELDS = ('kind', 'node', 'tick', 'time_ps', 'time_fs', 'expression',
                     'value', 'locals')


def _payload(row):
    kind = row.get('kind')
    if kind in _SIGNAL_KINDS:
        fields = _SAMPLE_FIELDS
        bits = (row.get('value') or {}).get('bin')
        if (not isinstance(bits, str) or not 1 <= len(bits) <= 4096 or
                any(b not in '01xz' for b in bits) or
                not isinstance(row.get('signal'), str) or
                type(row.get('time_ps')) is not int or type(row.get('time_fs')) is not int or
                row.get('sample_phase') != 'before' or row.get('sample_offset_ps') != 0):
            raise ValueError('property_evidence_payload_invalid')
        selection = row.get('bits')
        if selection is not None and (not isinstance(selection, list) or len(selection) != len(bits) or
                any(type(b) is not int for b in selection) or len(set(selection)) != len(selection)):
            raise ValueError('property_evidence_payload_invalid')
    elif kind == 'expression_result':
        fields = _EXPRESSION_FIELDS
    else:
        raise ValueError('property_evidence_kind_invalid')
    return {key: row[key] for key in fields if key in row}


def _identifier(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    prefix = 's_' if payload['kind'] in _SIGNAL_KINDS else 'x_'
    return prefix + hashlib.sha256(raw.encode()).hexdigest()[:20]


def attach_evidence_references(result, *, deadline=None):
    """Annotate an owned result before output trimming; preserve legacy fields."""
    result['evidence_context'] = dict(version=1, check_id='pc_'+secrets.token_hex(12),
        signal_ids='s_*', expression_ids='x_*', resolver='resolve_property_evidence')
    for attempt in result['attempts']:
        check_cancelled()
        def checkpoint():
            check_cancelled()
            if deadline is not None and time.monotonic()>=deadline:
                raise TimeoutError('property_timeout')
        for row in attempt['sampled_values']:
            checkpoint()
            row.update(kind='signal_sample', sample_phase='before', sample_offset_ps=0)
            row['evidence_id'] = _identifier(_payload(row))
        clock = attempt.get('clock_sample')
        if clock is not None:
            checkpoint()
            clock.update(kind='clock_sample', sample_phase='before', sample_offset_ps=0)
            clock['evidence_id'] = _identifier(_payload(clock))
        for row in attempt['evidence']:
            checkpoint()
            row['kind'] = 'expression_result'
            row['evidence_id'] = _identifier(_payload(row))
    return result


def resolve_property_evidence(result, references):
    """Resolve typed IDs against one retained response, without rewriting facts.

    references contains check_id, signal_evidence_ids and optionally
    expression_evidence_ids. Reject foreign, removed, changed or mistyped facts.
    A hash detects accidental receipt mutation; this is not an authentication
    mechanism for accepting an untrusted replacement response from the model.
    """
    check_cancelled()
    if isinstance(result, BaseModel):
        result = result.model_dump(mode='json')
    if not isinstance(result, dict) or result.get('mode') != 'property':
        raise ValueError('property_evidence_result_required')
    if (not isinstance(references, dict) or
            set(references)-{'check_id', 'signal_evidence_ids', 'expression_evidence_ids'}):
        raise ValueError('property_evidence_references_invalid')
    context = result.get('evidence_context') or {}
    if context.get('version') != 1 or not isinstance(context.get('check_id'), str):
        raise ValueError('property_evidence_contract_missing')
    if references.get('check_id') != context['check_id']:
        raise ValueError('property_evidence_check_mismatch')
    signal_ids = references.get('signal_evidence_ids')
    expression_ids = references.get('expression_evidence_ids', [])
    if any(not isinstance(ids, list) or any(not isinstance(v, str) or len(v)>64 for v in ids)
           for ids in (signal_ids, expression_ids)):
        raise ValueError('property_evidence_references_invalid')
    requested = signal_ids + expression_ids
    if len(requested)>MAX_REFERENCES:
        raise ValueError('property_evidence_reference_limit')
    if len(set(requested)) != len(requested):
        raise ValueError('property_evidence_duplicate_reference')
    catalog = {}
    count = 0
    for attempt in result.get('attempts', []):
        check_cancelled()
        rows = [*attempt.get('sampled_values', []), *attempt.get('evidence', [])]
        if attempt.get('clock_sample') is not None:
            rows.append(attempt['clock_sample'])
        for row in rows:
            check_cancelled(); count += 1
            if count>MAX_RECORDS:
                raise ValueError('property_evidence_record_limit')
            payload = _payload(row)
            identifier = _identifier(payload)
            if row.get('evidence_id') != identifier:
                raise ValueError('property_evidence_identity_changed')
            if identifier in catalog and catalog[identifier] != payload:
                raise ValueError('property_evidence_identity_conflict')
            catalog[identifier] = payload
    def select(ids, kinds):
        output = []
        for identifier in ids:
            check_cancelled()
            if identifier not in catalog:
                raise ValueError('property_evidence_reference_unavailable')
            payload = catalog[identifier]
            if payload['kind'] not in kinds:
                raise ValueError('property_evidence_kind_mismatch')
            output.append(dict(evidence_id=identifier, **deepcopy(payload)))
        return output
    return dict(check_id=context['check_id'],
        signal_facts=select(signal_ids, _SIGNAL_KINDS),
        expression_facts=select(expression_ids, {'expression_result'}),
        display_truncated=bool(result.get('output_truncated')))
