"""Bounded recorded history for X tracing; no RTL execution or time rounding inference."""
from dataclasses import asdict, replace
from itertools import groupby
import resource

from .cancellation import OperationCancelled, check_cancelled
from .divergence_budget import Budget, BudgetExceeded
from .divergence_compare import SignalStream, bit_value, known, read_stream
from .divergence_mapping import split_selection
from .dynamic_evidence import Expr, expression_gaps
from .dynamic_binding import signal_expression
from .dynamic_selection import select_expression as _select
from .operation_metrics import read_process_rss_kib
from .waveform_batch import EventPagingUnavailable, event_readers


class HistoryBudget(Budget):
    def __init__(self, limits):
        super().__init__(limits)
        self.reads = self.pages = self.native_pages = self.record_bytes = 0
        self.estimated_bytes = self.legacy_reads = 0
        self.modes = set()
        self.preparation_count = self.build_count = 0
        self.frontend_cpu_ms = self.frontend_peak_rss_kib = 0
        self.rss_start_kib = self.rss()

    @staticmethod
    def rss():
        return read_process_rss_kib() or 0

    def record_preparation(self, metrics):
        self.preparation_count += 1
        self.build_count += metrics.actual_build_count
        self.frontend_cpu_ms += metrics.worker_cpu_ms or 0
        self.frontend_peak_rss_kib = max(self.frontend_peak_rss_kib, metrics.rss_peak_kib or 0)

    def account(self, rows, nbytes, *, native=False, legacy=False):
        self.transitions += len(rows)
        self.record_bytes += nbytes
        self.estimated_bytes += sum(768 + len(str(r.value if hasattr(r, 'value') else r.get('value', ''))) * 4 for r in rows)
        self.pages += int(not legacy)
        self.native_pages += int(native)
        self.check()
        if self.transitions > self.limits['max_events']:
            raise BudgetExceeded('max_events')
        if self.estimated_bytes > self.limits['max_read_bytes']:
            raise BudgetExceeded('max_read_bytes')

    def metrics(self):
        return {**super().metrics(), 'waveform_read_calls': self.reads,
                'event_page_calls': self.pages, 'native_page_calls': self.native_pages,
                'legacy_materialized_reads': self.legacy_reads,
                'record_bytes_read': self.record_bytes,
                'estimated_event_bytes_read': self.estimated_bytes,
                'source_graph_prepare_count': self.preparation_count,
                'semantic_build_count': self.build_count,
                'frontend_cpu_ms': self.frontend_cpu_ms,
                'frontend_peak_rss_kib': self.frontend_peak_rss_kib,
                'process_rss_start_kib': self.rss_start_kib,
                'process_rss_end_kib': self.rss(),
                'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}

    def read_stream(self, get_parser, wave, signal, start, end):
        """Keep pages inside one wave lock; cursors/groups exit before connectivity."""
        self.check()
        if self.transitions >= self.limits['max_events']:
            raise BudgetExceeded('max_events')
        parser = get_parser(wave)
        self.reads += 1
        stream = SignalStream(parser=parser)
        try:
            with event_readers([(parser, signal)], start, end,
                               max_events=min(1024, self.limits['max_events'] - self.transitions)) as readers:
                reader = readers[0]
                self.modes.add(reader.mode)
                stream.width = reader.width
                stream.end = (reader.end_fs + 999) // 1000 if reader.end_fs is not None else -1
                stream.range_known = reader.end_fs is not None
                header = parser.get_header()
                stream.scale_fs = header.get('scale_fs_per_tick')
                if stream.scale_fs is None and header.get('timescale_ps') is not None:
                    stream.scale_fs = int(header['timescale_ps'] * 1000)
                previous_time = None
                first = True
                while True:
                    self.check()
                    allowance = min(1024, self.limits['max_events'] - self.transitions,
                        (self.limits['max_read_bytes'] - self.estimated_bytes) // (768 + 4 * (reader.width + 1)))
                    if allowance < 1:
                        raise BudgetExceeded('max_events' if self.transitions >= self.limits['max_events'] else 'max_read_bytes')
                    reader.max_events = allowance
                    page = reader.read_page()
                    rows = (*((page.predecessor,) if page.predecessor else ()), *page.events)
                    self.account(rows, page.output_bytes, native=reader.native)
                    def row(event):
                        return dict(time_ps=event.time_ps, time_fs=event.time_fs, value=event.value)
                    if page.predecessor:
                        if not first:
                            raise ValueError('replayed_event_predecessor')
                        stream.predecessor = row(page.predecessor)
                    first = False
                    for event in page.events:
                        if previous_time is not None and event.time_fs < previous_time:
                            raise ValueError('transition_order_invalid')
                        previous_time = event.time_fs
                        stream.transitions.append(row(event))
                    if page.truncated:
                        stream.truncated = True
                        # Exclude the entire unfinished raw-time tail group.
                        tail = min(t for t in (previous_time, page.next_time, end * 1000) if t is not None)
                        stream.safe_before = tail // 1000
                        break
                    if page.complete:
                        break
                    if not rows:
                        raise ValueError('event_page_no_progress')
                    if self.transitions >= self.limits['max_events']:
                        raise BudgetExceeded('max_events')
                if reader.end_fs is not None and end * 1000 > reader.end_fs:
                    stream.error = 'outside_recorded_range'
        except EventPagingUnavailable:
            self.legacy_reads += 1
            self.modes.add('legacy_materialized')
            stream = read_stream(get_parser, wave, signal, start, end)
            rows = [*((stream.predecessor,) if stream.predecessor else ()), *stream.transitions]
            self.account(rows, sum(32 + len(str(r.get('value', ''))) for r in rows), legacy=True)
            if stream.scale_fs is None or stream.scale_fs < 1000:
                stream.error = 'exact_history_time_unavailable'
        except (OperationCancelled, BudgetExceeded):
            raise
        except KeyError:
            stream.missing, stream.error = True, 'signal_not_dumped'
        except Exception:
            stream.error = 'history_read_failed'
        return stream


def unknown_interval(stream, expr, start, time, phase):
    """Report the observed prefix and the active X/Z interval, never true origin."""
    gaps = []
    if stream.error:
        gaps.append(stream.error)
    if not stream.range_known:
        gaps.append('recorded_range_unavailable')
    if stream.truncated:
        gaps.append('transition_data_truncated')
    if time > stream.end or time < start:
        gaps.append('history_window_exhausted')
    if stream.width != len(expr.declared_bits):
        gaps.append('dynamic_bit_mapping_unavailable')
    def value(row):
        bits = bit_value(row.get('value'), stream.width) if row else None
        if bits is None or stream.width != len(expr.declared_bits):
            return None
        return ''.join(bits[expr.declared_bits.index(b)] for b in expr.bits)
    current = value(stream.predecessor)
    first_unknown = start * 1000 if current is not None and not known(current) else None
    onset = first_unknown
    boundary = 'window_start_unknown' if onset is not None else 'predecessor_missing'
    prev_known = known(current)
    for at, rows in groupby(stream.transitions, key=lambda r: r.get('time_fs', r['time_ps'] * 1000)):
        check_cancelled()
        if at > time * 1000 or phase == 'before' and at == time * 1000:
            break
        last = None
        for last in rows:
            check_cancelled()
        next_value = value(last)
        if next_value is not None and not known(next_value):
            if first_unknown is None:
                first_unknown = at
            if current is None or known(current):
                onset = at
                boundary = 'observed_transition' if known(current) else 'dump_prefix_unknown'
                prev_known = known(current)
        else:
            onset = None
        current = next_value
    if current is None:
        gaps.append('value_unavailable')
    if stream.truncated and time >= stream.safe_before:
        current = None
    return dict(value=current, first_observed_unknown_time_fs=first_unknown,
                active_interval_start_time_fs=onset, start_boundary=boundary if onset is not None else None,
                predecessor_known=bool(prev_known) if onset is not None else False,
                true_origin_proven=False, observation_time_ps=time, observation_phase=phase,
                coverage=dict(history_start_ps=start, observed_through_ps=time,
                    recorded_end_ps=stream.end, range_known=stream.range_known,
                    predecessor_time_fs=(stream.predecessor.get('time_fs', stream.predecessor['time_ps'] * 1000)
                                         if stream.predecessor else None),
                    transition_data_truncated=stream.truncated),
                gaps=list(dict.fromkeys(gaps)))


def select_step(step, bits):
    original = tuple(step.get('bits', ()))
    if not original or not set(bits).issubset(original):
        return {**step, 'complete': False, 'gaps': [*step.get('gaps', ()), 'dynamic_bit_mapping_unavailable']}
    positions = tuple(original.index(b) for b in bits)
    branches = []
    for branch in step['branches']:
        value = _select(Expr.from_dict(branch['value']), positions)
        if 'dynamic_expression_limit' in expression_gaps(value):
            raise ValueError('dynamic_expression_limit')
        branches.append({**branch, 'value': asdict(value)})
    result = {**step, 'bits': list(bits), 'width': len(bits), 'branches': branches}
    if step.get('state'):
        result['state'] = asdict(_select(Expr.from_dict(step['state']), positions))
    return result


def bind_step_wave(step, parser):
    """Bind typed source references to exact dump declarations, including FSDB ranges."""
    def bind(raw):
        expr = Expr.from_dict(raw)
        def visit(node):
            if node.op == 'signal':
                try:
                    return signal_expression(parser, node.signal, node.bits, node.declared_bits)
                except (KeyError, ValueError):
                    return node  # Sampling returns explicit missing/mapping evidence.
            return replace(node, args=tuple(visit(a) for a in node.args))
        return asdict(visit(expr))
    result = {**step, 'branches': [{**b, 'guard': bind(b['guard']), 'value': bind(b['value'])}
                                  for b in step.get('branches', ())]}
    if step.get('state'):
        result['state'] = bind(step['state'])
    if step.get('clock'):
        result['clock'] = {**step['clock'], 'expression': bind(step['clock']['expression'])}
    return result
