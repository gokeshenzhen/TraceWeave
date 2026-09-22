"""Bounded observations at an unmodeled typed asynchronous state boundary.

Control pin kind/polarity identifies observation targets, not assigned values.
Recorded event coincidence is evidence for investigation, never causation.
"""
from .cancellation import check_cancelled
from .dynamic_evidence import Expr, validate_step
from .dynamic_observe import ObservationReader, clock_edges


def observe_async_controls(step, *, get_parser, wave, start, time, phase,
                           unknown_onset_fs, session=None):
    validate_step(step)
    reader = ObservationReader(get_parser, wave, start, time, session=session)
    onset = (unknown_onset_fs // 1000 if isinstance(unknown_onset_fs, int)
             and unknown_onset_fs % 1000 == 0 else None)
    if onset is not None and (onset < start or onset > time or phase == 'before' and onset == time):
        onset = None

    def edges(raw, edge, role):
        check_cancelled()
        expr = Expr.from_dict(raw)
        bound, binding_gaps = reader.bind(expr)
        stream = reader.stream(bound.signal)
        times, gaps = clock_edges(stream, edge, start=start, end=time, before=phase == 'before')
        gaps = [*binding_gaps, *gaps]
        if stream.scale_fs is None:
            gaps.append('sampling_order_unresolved')
        if role != 'clock':
            gaps = ['async_control_edge_unresolved' if g == 'clock_alignment_unproven' else g for g in gaps]
        gaps = list(dict.fromkeys(gaps))
        # At the first recorded instant an absent predecessor cannot disprove
        # an edge; no initial assignment is counted as an assertion.
        onset_covered = (onset is not None and not gaps
                         and (onset > start or stream.predecessor is not None))
        return dict(signal=expr.signal, waveform_signal=bound.signal, edge=edge,
                    status='partial' if gaps else 'complete',
                    observed_edge_count=len(times), last_observed_edge_ps=times[-1] if times else None,
                    edge_at_unknown_onset=(onset in times) if onset_covered else None,
                    gaps=gaps), expr

    clock, _ = edges(step['clock']['expression'], step['clock']['edge'], 'clock')
    controls = []
    gaps = list(step.get('gaps', ())) + clock['gaps']
    for control in step['async_controls']:
        fact, expr = edges(control['expression'], control['assertion_edge'], 'async_control')
        sample = reader.sample(expr, time, phase)
        fact.update(kind=control['kind'], active_value=control['active_value'],
                    observation=sample, active=(sample['value'] == control['active_value'])
                    if sample['value'] in {'0', '1'} else None)
        fact['gaps'] = list(dict.fromkeys(fact['gaps'] + sample['gaps']))
        if fact['active'] is None:
            fact['gaps'].append('async_control_value_unavailable' if sample['value'] is None
                                else 'async_control_value_unknown')
        if fact['gaps']:
            fact['status'] = 'partial'
            fact['edge_at_unknown_onset'] = None
            fact['assertion_status'] = 'inconclusive'
        elif fact['observed_edge_count']:
            fact['assertion_status'] = 'observed'
        elif fact['active']:
            fact['status'] = 'partial'
            fact['assertion_status'] = 'active_without_observed_assertion'
            fact['edge_at_unknown_onset'] = None
            fact['gaps'].append('async_assertion_before_window_or_unrecorded')
        else:
            fact['assertion_status'] = 'not_observed_in_window'
        gaps.extend(fact['gaps'])
        controls.append(fact)
    return dict(status='partial', window=dict(start_ps=start, end_ps=time, phase=phase),
                unknown_onset_fs=unknown_onset_fs, clock=clock, controls=controls,
                assignment_value_status='unmodeled', inference_status='not_run',
                gaps=list(dict.fromkeys(gaps)))
