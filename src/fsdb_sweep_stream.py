"""Bounded before-edge sampling for FSDB handshake sweeps.

The caller owns the process-global waveform lock. A native group owns cursors;
this module retains one event page per signal and one sample block, never the
consumed history. FFR may still retain/decompress whole flush sessions.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import time

from . import operation_metrics
from .cancellation import check_cancelled
from .event_pages import GroupCursor, IncompleteGroup, page_records, limits as page_limits
from .vcd_parser import _enrich_value


_TRANSITION_KIND = frozenset(("transition",))


@dataclass(frozen=True)
class StreamLimits:
    page_events: int = 1024
    page_bytes: int = 262144
    block_edges: int = 2048
    block_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        page_limits(self.page_events, self.page_bytes)
        if not 1 <= self.block_edges <= 65536 or not 1024 <= self.block_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid sweep block limits")


class _WorkStop(Exception):
    pass


class SweepBudget:
    """Total work is independent of intermediate residency; shared by all packs.

    A page is the admission unit, so the event/byte limits can overshoot by at
    most one bounded page. Cancellation is checked before every native page;
    a native call itself remains non-interruptible.
    """
    def __init__(self, max_events=50_000_000, max_bytes=32 * 1024**3, timeout_sec=300):
        self.max_events, self.max_bytes = max_events, max_bytes
        self.deadline = time.monotonic() + timeout_sec
        self.events = self.nbytes = 0
        self.reason = None

    def check(self):
        check_cancelled()
        if self.reason is None:
            if self.events >= self.max_events:
                self.reason = "sweep_event_budget"
            elif self.nbytes >= self.max_bytes:
                self.reason = "sweep_byte_budget"
            elif time.monotonic() >= self.deadline:
                self.reason = "sweep_time_budget"
        if self.reason:
            raise _WorkStop(self.reason)

    def account(self, page):
        rows = page_records(page)
        self.events += len(rows)
        self.nbytes += sum(512 + 4 * len(row.value or "") for row in rows)
        operation_metrics.record_sweep_stream("sweep_stream_events_total", len(rows))


def supports_streaming(parser):
    from .fsdb_parser import FSDBParser
    if not isinstance(parser, FSDBParser):
        return False
    parser._open()
    return all(getattr(parser._lib, name, False) for name in (
        "_traceweave_has_event_pages_v1", "_traceweave_has_transition_group"))


def _peak(name, value):
    operation_metrics.record_sweep_stream(name, value, peak=True)


class _Cursor(GroupCursor):
    """GroupCursor's exact tick folding, with bounded gap state and page metrics."""
    def __init__(self, reader, budget, owner):
        self.owner = owner
        self.read_ms = 0.0
        self.kind = "signal"
        self.logical_bytes = self.record_count = 0
        self.gap_start = None
        self.gap_reason = None
        super().__init__(reader, checkpoint=budget.check, consume=self._account)
        self.current = self.anchor.value if self.anchor else None
        self.enriched = _enrich_value(self.current) if self.current is not None else None
        self.failed = False
        self.owner.cursors.append(self)
        self.owner._page_peaks()

    def _account(self, page):
        self.owner.budget.account(page)
        rows = page_records(page)
        self.record_count = len(rows)
        self.logical_bytes = sum(512 + 4 * len(row.value or "") for row in rows)
        self.owner._page_peaks()

    def _read(self):
        # GroupCursor normally accumulates recording gaps for other consumers.
        # A sweep only needs the earliest uncertainty frontier, not a history.
        self.recording_gaps = ()
        started = time.perf_counter()
        try:
            super()._read()
        finally:
            self.read_ms += (time.perf_counter() - started) * 1000
        for gap in self.recording_gaps:
            if self.gap_start is None or gap.start_fs < self.gap_start:
                self.gap_start, self.gap_reason = gap.start_fs, gap.reason
        self.recording_gaps = ()
        if not self.page.complete and not self.page.truncated and not self._events and self.page.next_time is None:
            raise IncompleteGroup("transition_data_truncated")

    def take(self, at):
        value = self.take_group(at)
        if self.page.truncated and self.peek() is None:
            raise IncompleteGroup("transition_data_truncated")
        if self.gap_start is not None and at >= self.gap_start:
            raise IncompleteGroup(self.gap_reason or "recording_gap")
        return value

    def take_group(self, at):
        # Most groups are one ordinary event. The known successor proves that
        # the timestamp is closed without repeatedly peeking or building a set.
        # Equal/backward successors and all pending initial observations retain
        # the generic folding path, including its cross-page uncertainty rules.
        index, events = self.index, self._events
        if self._pending_initial is None and index < len(events):
            event = events[index]
            following = events[index + 1].time_fs if index + 1 < len(events) else self.page.next_time
            if event.time_fs == at and event.kind == "transition" and (following is None or following > at):
                self.checkpoint()
                if self.last_time is not None and at < self.last_time:
                    raise IncompleteGroup("transition_order_invalid")
                self.index = index + 1
                self.last_time = at
                self.group_changed = False
                self.group_kinds = _TRANSITION_KIND
                self.compared += 1
                return event.value
        return super().take_group(at)

    def before(self, at):
        if self.failed:
            return None
        if self.gap_start is not None and at >= self.gap_start:
            raise IncompleteGroup(self.gap_reason or "recording_gap")
        value = self.current
        upcoming = self.peek()
        while upcoming is not None and upcoming < at:
            value = self.take(upcoming)
            upcoming = self.peek()
        if upcoming is None and not self.page.complete:
            raise IncompleteGroup("transition_data_truncated")
        if self.gap_start is not None and at >= self.gap_start:
            raise IncompleteGroup(self.gap_reason or "recording_gap")
        if self.page.truncated:
            frontier = self.page.next_time
            if frontier is None and self._events:
                frontier = self._events[-1].time_fs
            if frontier is None or at >= frontier:
                raise IncompleteGroup("transition_data_truncated")
        if value != self.current:
            self.current = value
            self.enriched = _enrich_value(value) if value is not None else None
        return self.enriched


class EdgeBlockStream:
    def __init__(self, parser, clock, paths, start, end, edge, limits, budget, stack):
        self.parser, self.clock = parser, clock
        self.paths = list(dict.fromkeys(paths))
        self.start, self.end, self.edge = start, end, edge
        self.limits, self.budget, self.stack = limits, budget, stack
        self.status = {"signal_errors": {}, "sampling_gaps": [], "transition_signals_truncated": []}
        self.cursors = []
        self.first_edge_fs = self.last_edge_fs = None
        self.sample_count = self.block_count = 0
        self._iterator = self._blocks()

    def __iter__(self):
        return self._iterator

    def close(self):
        self._iterator.close()
        for cursor in self.cursors:
            operation_metrics.record_sweep_transition_read(cursor.kind, cursor.read_ms)
        self.cursors.clear()

    def _page_peaks(self):
        _peak("sweep_stream_live_page_records_peak", sum(c.record_count for c in self.cursors))
        _peak("sweep_stream_live_page_bytes_peak", sum(c.logical_bytes for c in self.cursors))
        _peak("sweep_stream_active_cursors_peak", len(self.cursors))

    def _limited(self, path, reason=None):
        limited = self.status["transition_signals_truncated"]
        if path not in limited:
            limited.append(path)
        if reason and reason not in self.status["sampling_gaps"]:
            self.status["sampling_gaps"].append(reason)

    def _open(self, path, start):
        self.budget.check()
        reader = self.stack.enter_context(self.parser._event_pages(path, start, self.end,
            max_events=self.limits.page_events, max_bytes=self.limits.page_bytes))
        # GroupCursor uses start_fs to classify a typed initial-state anchor.
        if not hasattr(reader, "start_fs"):
            reader.start_fs = start * 1000
        return _Cursor(reader, self.budget, self)

    def _block(self, edges, columns, logical_bytes):
        self.block_count += 1
        self.sample_count += len(edges)
        operation_metrics.record_sweep_stream("sweep_stream_blocks", 1)
        _peak("sweep_stream_block_edges_peak", len(edges))
        _peak("sweep_stream_block_bytes_peak", logical_bytes)
        operation_metrics.record_sweep_sampling_shape(len(edges), len(columns))
        operation_metrics.record_sweep_rss(phase="sample")
        return {"edge_times": edges, "signal_columns": columns, **self.status}

    def _blocks(self):
        if self.edge not in ("posedge", "negedge"):
            raise ValueError("edge must be posedge or negedge")
        from .cycle_query import _validate_clock_width
        _validate_clock_width(self.parser, self.clock)
        edges, columns = [], {}
        signal_cursors = None
        row_bytes = 0
        try:
            clock = self._open(self.clock, self.start)
            clock.kind = "clock"
            previous = clock.current
            if previous is not None and previous not in ("0", "1"):
                self._limited(self.clock, "clock_unknown")
                return
            while (at := clock.peek()) is not None:
                self.budget.check()
                try:
                    current = clock.take(at)
                except IncompleteGroup as exc:
                    self._limited(self.clock, str(exc))
                    break
                if current not in ("0", "1") or clock.group_changed:
                    self._limited(self.clock, "clock_unknown" if current not in ("0", "1") else "clock_event_order_unresolved")
                    break
                matched = ((previous == "0" and current == "1") if self.edge == "posedge"
                           else (previous == "1" and current == "0"))
                previous = current
                if not matched:
                    continue
                if at < self.start * 1000 or (self.end != -1 and at > self.end * 1000):
                    continue
                if signal_cursors is None:
                    signal_cursors = {}
                    row_bytes = 64
                    for path in self.paths:
                        try:
                            # A separate cursor for a clock also used as data
                            # preserves strict-before semantics at this edge.
                            signal_cursors[path] = self._open(path, at // 1000)
                            width = max(1, self.parser.get_signal_width(path))
                            row_bytes += 512 + 4 * width
                        except KeyError as exc:
                            self.status["signal_errors"][path] = str(exc)
                    columns = {path: [] for path in signal_cursors}
                    if row_bytes > self.limits.block_bytes:
                        self._limited(self.clock, "sweep_sample_row_budget")
                        break
                values = {}
                for path, cursor in signal_cursors.items():
                    try:
                        values[path] = cursor.before(at)
                    except IncompleteGroup as exc:
                        cursor.failed = True
                        self._limited(path)
                        values[path] = None
                # Publish a row only after every signal's before-value is safe.
                # Public ps may coincide for distinct sub-ps physical edges.
                if self.first_edge_fs is None:
                    self.first_edge_fs = at
                self.last_edge_fs = at
                edges.append((at + 999) // 1000)
                for path in columns:
                    columns[path].append(values[path])
                if len(edges) >= self.limits.block_edges or (len(edges) + 1) * row_bytes > self.limits.block_bytes:
                    yield self._block(edges, columns, len(edges) * row_bytes)
                    edges, columns = [], {path: [] for path in signal_cursors}
            if not clock.page.complete and clock.peek() is None:
                self._limited(self.clock, "transition_data_truncated")
        except _WorkStop as exc:
            self._limited(self.clock, str(exc))
            operation_metrics.record_sweep_stream("sweep_stream_work_limit_stops", 1)
        except IncompleteGroup as exc:
            self._limited(self.clock, str(exc))
        if edges:
            yield self._block(edges, columns, len(edges) * row_bytes)


@contextmanager
def iter_edge_blocks(parser, clock, paths, start, end, edge="posedge", *, limits=None, budget=None):
    """Yield a single-use block stream. Its status is final after consumption.

    The pure page consumer also accepts VCD test readers; public production
    dispatch selects only capable FSDB parsers. Close on all exits, including
    cancelled suspended iteration. An existing native group is borrowed.
    """
    from .waveform_batch import EventPagingUnavailable
    limits = limits or StreamLimits()
    budget = budget or SweepBudget()
    with ExitStack() as stack:
        if not getattr(parser, "_supports_event_pages", lambda: False)():
            raise EventPagingUnavailable()
        group = getattr(parser, "_event_transition_group", None)
        if group is not None and not getattr(parser, "_transition_group_active", False):
            if not stack.enter_context(group(list(dict.fromkeys([clock, *paths])), start, end)):
                raise EventPagingUnavailable()
        stream = EdgeBlockStream(parser, clock, paths, start, end, edge, limits, budget, stack)
        try:
            yield stream
        finally:
            stream.close()


def stream_fsdb_pack(parser, entries, resolution_cache, *, wave_path, start_ps, end_ps,
                     edge, max_wait_cycles, budget):
    """Consume each clock once per bounded pack, sharing block columns."""
    from .verify_condition import inspect_handshake
    groups = {}
    for nb, _ in entries:
        if nb.get("clock") and nb.get("semantics_confirmed", True):
            groups.setdefault(resolution_cache[nb["clock"]], []).append(nb)
    results = {}
    for clock, members in groups.items():
        check_cancelled()
        started = time.perf_counter()
        consumers = []
        try:
            for nb in members:
                consumer = inspect_handshake(get_parser=lambda _: parser, wave_path=wave_path,
                    clock=nb["clock"], ready=nb["ready"], payload=nb["payload"],
                    edge=edge, start_ps=start_ps, end_ps=end_ps, max_wait_cycles=max_wait_cycles,
                    cursor_store=None, _resolution_cache=resolution_cache,
                    _compact_sampling=True, _defer_sampling=True, **nb["inspect_kwargs"])
                consumers.append((nb, consumer))
            paths = list(dict.fromkeys(path for _, c in consumers for path in (
                c.settings["valid_signal"], c.settings["ready"], *c.settings["payload"],
                *c.settings["extra_signals"])))
            with iter_edge_blocks(parser, clock, paths, start_ps, end_ps, edge, budget=budget) as stream:
                for block in stream:
                    for _, consumer in consumers:
                        # Per-interface metadata excludes other members' failures.
                        owned = {consumer.settings["valid_signal"], consumer.settings["ready"],
                                 *consumer.settings["payload"], *consumer.settings["extra_signals"]}
                        consumer.consume({**block,
                            "signal_errors": {p: e for p, e in block["signal_errors"].items() if p in owned},
                            "transition_signals_truncated": [p for p in block["transition_signals_truncated"] if p == clock or p in owned]})
                    del block
                for nb, consumer in consumers:
                    owned = {consumer.settings["valid_signal"], consumer.settings["ready"],
                             *consumer.settings["payload"], *consumer.settings["extra_signals"]}
                    status = {**stream.status,
                        "signal_errors": {p: e for p, e in stream.status["signal_errors"].items() if p in owned},
                        "transition_signals_truncated": [p for p in stream.status["transition_signals_truncated"] if p == clock or p in owned]}
                    results[id(nb)] = (consumer.finish(status), 0.0)
            # Shared sampling cost is attributed once across the group, not N times.
            elapsed = (time.perf_counter() - started) * 1000 / len(consumers)
            for nb, _ in consumers:
                results[id(nb)] = (results[id(nb)][0], elapsed)
        finally:
            for _, consumer in consumers:
                consumer.close()
    return results
