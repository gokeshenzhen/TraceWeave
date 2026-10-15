"""Private, request-scoped event pages. All times used for ordering are fs.

A page's next_time is an unread event, not coverage. In particular a tail group
is unfinished when next_time equals its timestamp. Readers retain their cursor
until close, and must remain inside the caller's waveform lock/group.
"""
from dataclasses import dataclass
import sys
import time

from .cancellation import check_cancelled
from . import operation_metrics

PAGE_EVENTS = 1024
PAGE_BYTES = 256 * 1024


@dataclass(frozen=True)
class Event:
    time_fs: int
    time_ps: int
    value: str | None
    # State observations and recording boundaries are not value-change facts.
    kind: str = 'transition'


@dataclass(frozen=True)
class RecordingGap:
    start_fs: int
    end_fs: int
    reason: str
    start_inclusive: bool = True
    end_inclusive: bool = False


@dataclass(frozen=True)
class EventPage:
    events: tuple[Event, ...]
    predecessor: Event | None = None
    next_time: int | None = None
    complete: bool = False
    truncated: bool = False
    output_bytes: int = 0
    initial_state: Event | None = None
    recording_gaps: tuple[RecordingGap, ...] = ()


@dataclass(frozen=True)
class EventCountPage:
    """Consumed cursor records without values, including an unread anchor.

    Callers counting a window suffix must first consume/filter its anchors and
    boundary records using detail pages. This is a record count, not a count
    of physical toggles after subtracting the initial observation.
    """
    events: int
    next_time: int | None = None
    complete: bool = False
    truncated: bool = False
    boundary_ps: int | None = None
    events_before_boundary: int = 0


def page_records(page):
    """Account for every retained record, including a separately typed anchor."""
    return (*((page.initial_state,) if page.initial_state else ()),
            *((page.predecessor,) if page.predecessor else ()), *page.events)


def sampling_anchor(result):
    """Select state strictly before the read window without renaming its origin."""
    initial = result.get('initial_state')
    if initial and initial.get('time_fs', initial['time_ps'] * 1000) >= result.get('start_ps', 0) * 1000:
        initial = None
    return result.get('predecessor') or initial


def limits(events, nbytes):
    if not 1 <= events <= 4096 or not 128 <= nbytes <= 1048576:
        raise ValueError('event page limits require 1..4096 events and 128..1048576 bytes')


class IncompleteGroup(Exception):
    pass


class GroupCursor:
    """Fold a raw-time group with constant carry memory, across arbitrary pages."""
    def __init__(self, reader, checkpoint=None, consume=None):
        self.reader, self.checkpoint = reader, checkpoint or check_cancelled
        self.consume = consume
        self.page, self.index = None, 0
        self.predecessor = None
        self.initial_state = None
        self._pending_initial = None
        self.recording_gaps = ()
        self.pages = self.events = self.nbytes = self.compared = 0
        self.last_time = None
        self._read()

    def _read(self):
        self.checkpoint()
        page = self.reader.read_page()
        self.checkpoint()
        self.pages += 1
        self.events += len(page_records(page))
        self.nbytes += page.output_bytes
        if self.consume:
            self.consume(page)
        if self.page is None:
            self.predecessor = page.predecessor
            self.initial_state = page.initial_state
            if page.initial_state and page.initial_state.time_fs >= getattr(self.reader, 'start_fs', 0):
                self._pending_initial = page.initial_state
        elif page.predecessor is not None or page.initial_state is not None:
            raise ValueError('replayed_event_predecessor')
        self.recording_gaps += page.recording_gaps
        self._events = page.events
        self.page, self.index = page, 0

    @property
    def anchor(self):
        """Known state strictly before the window; never invent a change time."""
        initial = self.initial_state
        if initial and initial.time_fs >= getattr(self.reader, 'start_fs', 0):
            initial = None
        return self.predecessor or initial

    def peek(self):
        upcoming = (self._events[self.index].time_fs if self.index < len(self._events)
                    else self.page.next_time)
        if self._pending_initial:
            return min(upcoming, self._pending_initial.time_fs) if upcoming is not None else self._pending_initial.time_fs
        return upcoming

    def take_group(self, at):
        value = None
        self.group_changed = False
        self.group_kinds = set()
        while self.peek() == at:
            self.checkpoint()
            if self._pending_initial and self._pending_initial.time_fs == at:
                event, self._pending_initial = self._pending_initial, None
            elif self.index == len(self._events):
                if self.page.truncated or self.page.complete:
                    raise IncompleteGroup('transition_data_truncated')
                self._read()
                if not self._events and self.page.next_time == at:
                    raise IncompleteGroup('transition_data_truncated')
                continue
            else:
                event = self._events[self.index]
                self.index += 1
            if self.last_time is not None and event.time_fs < self.last_time:
                raise IncompleteGroup('transition_order_invalid')
            self.last_time = event.time_fs
            self.group_kinds.add(event.kind)
            if value is not None:
                from .divergence_compare import bit_value
                width = getattr(self.reader,'width',None)
                self.group_changed |= (bit_value(value,width) != bit_value(event.value,width)) if width else value != event.value
            value = event.value
            self.compared += 1
        if self.peek() is not None and self.peek() < at:
            raise IncompleteGroup('transition_order_invalid')
        return value


class VcdEventReader:
    mode = 'vcd_index_pages'
    native = False

    def __init__(self, parser, path, start, end, max_events, max_bytes):
        from bisect import bisect_left, bisect_right
        limits(max_events, max_bytes)
        parser._ensure_parsed()
        if not parser._event_order_valid:
            raise ValueError('transition_order_invalid')
        self.parser, self.identity = parser, parser._parsed_identity
        self.epoch = parser._scope_epoch
        sym = parser._resolve(path)
        self.rows = parser._transitions.get(sym, ())
        self.ticks = parser._raw_ticks.get(sym)
        self.scale = parser._timescale_fs
        self.width = parser.get_signal_width(path)
        self.end_fs = parser._end_time_fs
        self.max_events, self.max_bytes = max_events, max_bytes
        end_fs = self.end_fs if end < 0 else end * 1000
        # Bisect the original records/compact raw tick array; no duplicate index.
        key = (lambda tick: tick * self.scale) if self.ticks is not None else (lambda row: row[0]*1000)
        ordered = self.ticks if self.ticks is not None else self.rows
        self.index = bisect_left(ordered, start*1000, key=key)
        self.stop = bisect_right(ordered, end_fs, key=key)
        self.first = True

    def _event(self, i):
        ps, value = self.rows[i]
        fs = self.ticks[i]*self.scale if self.ticks is not None else ps*1000
        return Event(fs, ps, value)

    def read_page(self):
        from .scope_metadata import file_identity, ScopeIdentityChanged
        check_cancelled()
        if self.epoch is not self.parser._scope_epoch or self.identity != file_identity(self.parser.file_path):
            raise ScopeIdentityChanged('VCD changed during event iteration')
        previous = self._event(self.index-1) if self.first and self.index else None
        rows, nbytes = [], 0
        if previous:
            nbytes = len(previous.value or '') + 32
            if nbytes >= self.max_bytes:
                return EventPage((), next_time=previous.time_fs, truncated=True)
        self.first = False
        while self.index < self.stop and len(rows) + bool(previous) < self.max_events:
            if len(rows) % 128 == 0:
                check_cancelled()
            event = self._event(self.index)
            size = 32 + len(event.value or '')
            if size > self.max_bytes - nbytes:
                break
            rows.append(event); nbytes += size; self.index += 1
        complete = self.index >= self.stop
        return EventPage(tuple(rows), previous,
            None if complete else self._event(self.index).time_fs,
            complete, not complete and not rows and previous is None, nbytes)

    def close(self):
        self.rows = ()
        self.ticks = None


class FsdbEventReader:
    mode = 'native_event_pages_v1'
    native = True

    def __init__(self, parser, path, start, end, max_events, max_bytes):
        import ctypes
        limits(max_events, max_bytes)
        self.parser, self.identity = parser, parser._file_identity
        self.epoch = parser._scope_epoch
        self.handle = parser._handle
        self.cursor = ctypes.c_uint64()
        self.buffer = ctypes.create_string_buffer(max_bytes)
        self.max_events, self.max_bytes = max_events, max_bytes
        self.width = parser.get_signal_width(path)
        self.index_counts = False
        raw_end = ctypes.c_uint64()
        rc = parser._lib.fsdb_event_end_tick_v1(self.handle, ctypes.byref(raw_end))
        parser._check_native_cleanup(rc, "event-duration")
        self.end_fs = raw_end.value * parser._scale_fs if rc == 0 else None
        started = time.perf_counter()
        rc = parser._lib.fsdb_event_open_v1(self.handle, path.encode(), start,
            0xffffffffffffffff if end == -1 else end, ctypes.byref(self.cursor))
        operation_metrics.record_sweep_native_event("open", (time.perf_counter() - started) * 1000)
        parser._check_native_cleanup(rc, "event-open")
        if rc == -2:
            raise KeyError(path)
        if rc < 0:
            raise RuntimeError('FSDB event cursor could not open')

    def _check_owner(self):
        check_cancelled()
        self.parser._check_metadata_identity(self.identity)
        if self.handle != self.parser._handle or self.epoch is not self.parser._scope_epoch:
            raise RuntimeError('FSDB event cursor owner changed')

    def _check_read_result(self, rc, operation):
        try:
            self.parser._check_native_cleanup(rc, operation)
        except RuntimeError:
            # Invalidate a poisoned reader even when native return and request
            # cancellation coincide, then preserve the cancellation exception.
            check_cancelled()
            raise
        check_cancelled()
        self.parser._check_metadata_identity(self.identity)
        if rc < 0:
            raise RuntimeError(f'FSDB {operation.replace("-", " ")} read failed')

    def read_page(self):
        import ctypes
        from .fsdb_parser import _NativeEventPageV1
        self._check_owner()
        receipt = _NativeEventPageV1()
        started = time.perf_counter()
        try:
            rc = self.parser._lib.fsdb_event_page_v1(self.handle, self.cursor,
                self.max_events, self.buffer, self.max_bytes, ctypes.byref(receipt), ctypes.sizeof(receipt))
        finally:
            operation_metrics.record_sweep_native_event("page", (time.perf_counter() - started) * 1000,
                transitions=receipt.events, output_bytes=receipt.output_bytes, truncated=bool(receipt.truncated))
        self._check_read_result(rc, "event-page")
        rows, previous = [], None
        for line in self.buffer.raw[:receipt.output_bytes].decode().splitlines():
            kind, ps, tick, value = line.split('\t', 3)
            event = Event(int(tick)*self.parser._scale_fs, int(ps), value)
            if kind == 'P':
                previous = event
            else:
                rows.append(event)
        return EventPage(tuple(rows), previous,
            receipt.next_tick*self.parser._scale_fs if receipt.has_next else None,
            bool(receipt.complete), bool(receipt.truncated), receipt.output_bytes)

    def count_page(self, max_events=65536):
        """Advance a bounded suffix without requesting or formatting values."""
        import ctypes
        from .fsdb_parser import _NativeEventPageV1, _NativeCountBoundaryV1
        if type(max_events) is not int or not 1 <= max_events <= 65536:
            raise ValueError('event count limit requires 1..65536 records')
        self._check_owner()
        if not getattr(self.parser._lib, '_traceweave_has_event_count_v1', False):
            raise RuntimeError('FSDB event count capability unavailable')
        receipt = _NativeEventPageV1()
        boundary = _NativeCountBoundaryV1()
        indexed = self.index_counts and getattr(self.parser._lib,
            '_traceweave_has_event_count_index_v1', False)
        started = time.perf_counter()
        try:
            if indexed:
                rc = self.parser._lib.fsdb_event_count_index_v1(self.handle, self.cursor,
                    max_events, ctypes.byref(receipt), ctypes.sizeof(receipt),
                    ctypes.byref(boundary), ctypes.sizeof(boundary))
            else:
                rc = self.parser._lib.fsdb_event_count_v1(self.handle, self.cursor,
                    max_events, ctypes.byref(receipt), ctypes.sizeof(receipt))
        finally:
            operation_metrics.record_sweep_native_event("page", (time.perf_counter() - started) * 1000,
                transitions=receipt.events, output_bytes=receipt.output_bytes, truncated=bool(receipt.truncated))
        self._check_read_result(rc, "event-count")
        if not receipt.events and not receipt.complete and not receipt.truncated:
            raise RuntimeError('FSDB event count made no progress')
        if boundary.valid and not 0 < boundary.events_before_boundary <= receipt.events:
            raise RuntimeError('FSDB count boundary receipt invalid')
        return EventCountPage(int(receipt.events),
            receipt.next_tick*self.parser._scale_fs if receipt.has_next else None,
            bool(receipt.complete), bool(receipt.truncated),
            int(boundary.boundary_ps) if boundary.valid else None,
            int(boundary.events_before_boundary) if boundary.valid else 0)

    def close(self):
        unwinding = sys.exc_info()[0] is not None
        try:
            if self.cursor.value and self.parser._handle == self.handle and self.epoch is self.parser._scope_epoch:
                started = time.perf_counter()
                try:
                    rc = self.parser._lib.fsdb_event_close_v1(self.handle, self.cursor)
                    self.parser._check_native_cleanup(rc, "event-close")
                    if rc < 0:
                        self.parser.close()
                        raise RuntimeError('FSDB event cursor cleanup failed; reader closed for recovery')
                finally:
                    operation_metrics.record_sweep_native_event("close", (time.perf_counter() - started) * 1000)
        except Exception:
            # Resource release must run during cancellation, but a cleanup
            # failure cannot replace the body/cancellation exception.
            if not unwinding:
                raise
        finally:
            self.cursor.value = 0
            self.buffer = None
