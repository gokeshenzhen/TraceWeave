"""Bounded local edge discovery for the shared cycle sampling kernel.

Only the explicitly selected window origin uses this path. Pages retain raw fs,
and time-window expansion never resets the read budget or jumps over a gap.
"""
from .event_pages import GroupCursor, IncompleteGroup
from .transaction_sampling import TransactionBudget, TransactionBudgetExceeded, TransactionLimits, bounded_parser
from .waveform_batch import event_readers, EventPagingUnavailable

WINDOW_PS = 1_000_000
MAX_WINDOWS = 64
DECODED_BYTES = 64 * 1024 * 1024
EVENT_LIMIT = 1_000_000


def _discover(parser, clock, start, end, edge, wanted, budget):
    from .cycle_query import _before_clock_prefix, _extract_edge_times, _time_fs
    from .divergence_compare import bit_value
    edges, modes = [], set()
    at, span, scanned_fs, eof_fs = start, WINDOW_PS, None, None
    reason, stop_fs = None, None
    window_complete = False
    for _ in range(MAX_WINDOWS):
        budget.check()
        until = at + span if end is None else min(at + span, end)
        try:
            with event_readers([(parser, clock)], at, until,
                               max_events=min(1024, max(8, 2*(wanted-len(edges))+2))) as readers:
                reader = readers[0]
                modes.add(reader.mode)
                eof_fs = reader.end_fs
                if eof_fs is not None and start * 1000 > eof_fs:
                    return edges, 'window_outside_waveform', eof_fs, False, eof_fs, modes
                cursor = GroupCursor(reader, checkpoint=budget.check)
                previous = cursor.anchor
                previous_value = bit_value(previous.value, 1) if previous else None
                # A recording-origin state may seed the sequence. A missing
                # predecessor at a later window cannot prove its first edge.
                if scanned_fs is None and previous_value not in ('0', '1'):
                    if previous_value is not None:
                        reason, stop_fs = 'clock_unknown', start * 1000
                    elif start != 0 or cursor.peek() != 0:
                        reason, stop_fs = 'clock_predecessor_missing', start * 1000
                while reason is None:
                    budget.check()
                    time_fs = cursor.peek()
                    ambiguous = [t for t in getattr(reader, 'ambiguous_times', ())
                                 if t >= start*1000 and (scanned_fs is None or t > scanned_fs)]
                    if ambiguous and min(ambiguous) <= (time_fs if time_fs is not None else until*1000):
                        reason, stop_fs = 'clock_event_order_unresolved', min(ambiguous)
                        break
                    gap = next((g for g in sorted(cursor.recording_gaps, key=lambda g:g.start_fs)
                        if g.end_fs > start * 1000 and (scanned_fs is None or g.end_fs > scanned_fs)), None)
                    if gap is not None and gap.start_fs <= (time_fs if time_fs is not None else until * 1000):
                        reason, stop_fs = gap.reason, max(start * 1000, gap.start_fs)
                        break
                    if time_fs is None:
                        if cursor.page.truncated or not cursor.page.complete:
                            reason, stop_fs = budget.stop_reason or 'transition_data_truncated', scanned_fs
                        break
                    value = bit_value(cursor.take_group(time_fs), 1)
                    if scanned_fs is not None and time_fs <= scanned_fs:
                        previous_value = value
                        continue
                    ambiguous_group = cursor.group_changed or time_fs in getattr(reader, 'ambiguous_times', ())
                    if value not in ('0', '1') or ambiguous_group:
                        reason = 'clock_event_order_unresolved' if ambiguous_group else 'clock_unknown'
                        stop_fs = time_fs
                        break
                    # Reuse the existing polarity rule; X->1 is never a clean edge.
                    rows = [{'time_ps': (time_fs+999)//1000, 'time_fs': time_fs, 'value': {'bin': value}}]
                    predecessor = {'value': {'bin': previous_value}} if previous_value else None
                    edges.extend(_extract_edge_times(rows, edge, predecessor=predecessor, raw_time=True))
                    previous_value = value
                    if len(edges) >= wanted:
                        return edges, None, None, False, eof_fs, modes
        except EventPagingUnavailable:
            # Existing bounded legacy buffer, with honest sub-ps/frontier handling.
            modes.add('legacy_materialized')
            raw = _before_clock_prefix(parser.get_transitions(clock, at, until))
            rows = raw.get('transitions', [])
            from .event_pages import sampling_anchor
            if scanned_fs is None and not sampling_anchor(raw) and (not rows or _time_fs(rows[0]) != 0):
                reason, stop_fs = 'clock_predecessor_missing', start*1000
            candidates = _extract_edge_times(rows, edge, predecessor=sampling_anchor(raw), raw_time=True)
            safe = raw.get('safe_before_fs', _time_fs(rows[-1]) if rows else at*1000)
            edges.extend(t for t in candidates if (scanned_fs is None or t > scanned_fs)
                         and (not raw.get('truncated') or t < safe))
            if len(edges) >= wanted:
                return edges[:wanted], None, None, False, eof_fs, modes
            if raw.get('truncated'):
                reason, stop_fs = budget.stop_reason or next(iter(raw.get('sampling_gaps', [])), 'transition_data_truncated'), safe
            if eof_fs is None:
                header = parser.get_header()
                duration = header.get('simulation_duration_ps')
                eof_fs = duration * 1000 if duration is not None else None
        except IncompleteGroup as exc:
            reason, stop_fs = budget.stop_reason or str(exc), cursor.peek()
        if reason:
            break
        scanned_fs = until * 1000
        if eof_fs is not None and scanned_fs >= eof_fs:
            window_complete = end is not None and end * 1000 <= eof_fs
            if not window_complete:
                reason, stop_fs = 'waveform_end', eof_fs
            break
        if end is not None and until == end:
            window_complete = True
            break
        at, span = until, span * 2
    else:
        reason, stop_fs = 'clock_window_budget', scanned_fs
    return edges, reason, stop_fs, window_complete, eof_fs, modes


def sample_local_cycles(parser, clock, paths, *, start, end, edge, count,
                        requested, capped, phase, offset):
    from .cycle_query import _sample_signals_at_edges, _compute_clock_period_ps, _validate_clock_width
    budget = TransactionBudget(TransactionLimits(decoded_bytes=DECODED_BYTES, events=EVENT_LIMIT))
    parser = bounded_parser(parser, budget, exact=True)
    _validate_clock_width(parser, clock)
    edges, reason, stop_fs, complete, eof_fs, modes = [], None, None, False, None, set()
    values, errors, limited = [], {}, []
    try:
        if count:
            edges, reason, stop_fs, complete, eof_fs, modes = _discover(
                parser, clock, start, end, edge, count + (end is not None), budget)
        else:
            reason = 'cycle_limit'
        found = len(edges)
        if end is not None and found > count:
            capped, reason = True, 'cycle_limit'
        edges = edges[:count]
        if eof_fs is not None and phase == 'after':
            usable = [t for t in edges if t + offset*1000 <= eof_fs]
            if len(usable) != len(edges):
                reason, stop_fs = 'sample_outside_waveform', eof_fs
                edges = usable
        values, errors, limited = _sample_signals_at_edges(parser, paths, edges, offset,
            sample_times=[t + offset*1000 for t in edges], safe_prefix_only=True, sample_phase=phase)
    except TransactionBudgetExceeded:
        reason = budget.stop_reason or 'timeout'
        found = len(edges)
        # Clock-only rows are not a sampled prefix. No partially built columns
        # escape when a timeout interrupts the shared sampling kernel.
        edges, values = [], []
    reason = budget.stop_reason or reason
    partial = bool(reason not in (None, 'cycle_limit') or errors or limited)
    if not reason and end is None and len(edges) < count:
        reason, partial = 'waveform_end', True
    period = _compute_clock_period_ps(edges)
    rows = [dict(cycle=i, time_ps=(t+999)//1000, time_ns=((t+999)//1000)/1000,
                 time_fs=t, sample_time_fs=t+offset*1000, signals=v)
            for i, (t, v) in enumerate(zip(edges, values))]
    return dict(clock_path=clock, edge=edge, cycle_index_origin='window', sample_phase=phase,
        sample_offset_ps=offset, clock_period_ps=period//1000 if period is not None else None,
        clock_period_fs=period, total_edges_found=found, clock_edges_complete=False,
        start_cycle=0, num_cycles_requested=requested if end is None else found,
        effective_num_cycles=count, num_cycles_returned=len(rows), capped=capped,
        truncated=partial or (end is None and len(rows)<count), resolved_from_time=True,
        requested_start_time_ps=start, requested_end_time_ps=end, cycles=rows, signal_errors=errors,
        transition_data_truncated=bool(limited or reason in ('event_budget','decoded_byte_budget','transition_data_truncated')),
        transition_signals_truncated=limited,
        local_coverage=dict(status='partial' if partial else ('complete' if rows else 'zero_coverage'), stop_reason=reason,
            stop_time_fs=stop_fs, window_complete=complete, edge_count_is_lower_bound=not complete,
            confirmed_edges=len(edges), requested_start_fs=start*1000,
            last_confirmed_edge_fs=edges[-1] if edges else None),
        reading=dict(events=budget.events_read, admitted_events=budget.events,
            estimated_decoded_bytes=budget.decoded_bytes, decoded_byte_limit=DECODED_BYTES,
            event_limit=EVENT_LIMIT, pages=budget.pages, read_calls=budget.read_calls,
            modes=sorted(modes), legacy_reads=budget.legacy_reads))
