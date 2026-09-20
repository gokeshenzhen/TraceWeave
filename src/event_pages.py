"""Private, request-scoped event pages. All times used for ordering are fs.

A page's next_time is an unread event, not coverage. In particular a tail group
is unfinished when next_time equals its timestamp. Readers retain their cursor
until close, and must remain inside the caller's waveform lock/group.
"""
from dataclasses import dataclass

from .cancellation import check_cancelled

PAGE_EVENTS = 1024
PAGE_BYTES = 256 * 1024


@dataclass(frozen=True)
class Event:
    time_fs: int
    time_ps: int
    value: str | None


@dataclass(frozen=True)
class EventPage:
    events: tuple[Event, ...]
    predecessor: Event | None = None
    next_time: int | None = None
    complete: bool = False
    truncated: bool = False
    output_bytes: int = 0


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
        self.pages = self.events = self.nbytes = self.compared = 0
        self.last_time = None
        self._read()

    def _read(self):
        self.checkpoint()
        page = self.reader.read_page()
        self.checkpoint()
        self.pages += 1
        self.events += len(page.events) + int(page.predecessor is not None)
        self.nbytes += page.output_bytes
        if self.consume:
            self.consume(page)
        if self.page is None:
            self.predecessor = page.predecessor
        elif page.predecessor is not None:
            raise ValueError('replayed_event_predecessor')
        self.page, self.index = page, 0

    def peek(self):
        if self.index < len(self.page.events):
            return self.page.events[self.index].time_fs
        return self.page.next_time

    def take_group(self, at):
        value = None
        while self.peek() == at:
            self.checkpoint()
            if self.index == len(self.page.events):
                if self.page.truncated or self.page.complete:
                    raise IncompleteGroup('transition_data_truncated')
                self._read()
                if not self.page.events and self.page.next_time == at:
                    raise IncompleteGroup('transition_data_truncated')
                continue
            event = self.page.events[self.index]
            if self.last_time is not None and event.time_fs < self.last_time:
                raise IncompleteGroup('transition_order_invalid')
            self.last_time = event.time_fs
            value = event.value
            self.index += 1
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
        raw_end = ctypes.c_uint64()
        rc = parser._lib.fsdb_event_end_tick_v1(self.handle, ctypes.byref(raw_end))
        self.end_fs = raw_end.value * parser._scale_fs if rc == 0 else None
        rc = parser._lib.fsdb_event_open_v1(self.handle, path.encode(), start,
            0xffffffffffffffff if end == -1 else end, ctypes.byref(self.cursor))
        if rc == -2:
            raise KeyError(path)
        if rc < 0:
            raise RuntimeError('FSDB event cursor could not open')

    def read_page(self):
        import ctypes
        from .fsdb_parser import _NativeEventPageV1
        check_cancelled()
        self.parser._check_metadata_identity(self.identity)
        if self.handle != self.parser._handle or self.epoch is not self.parser._scope_epoch:
            raise RuntimeError('FSDB event cursor owner changed')
        receipt = _NativeEventPageV1()
        rc = self.parser._lib.fsdb_event_page_v1(self.handle, self.cursor,
            self.max_events, self.buffer, self.max_bytes, ctypes.byref(receipt), ctypes.sizeof(receipt))
        check_cancelled()
        self.parser._check_metadata_identity(self.identity)
        if rc < 0:
            raise RuntimeError('FSDB event page read failed')
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

    def close(self):
        if self.cursor.value and self.parser._handle == self.handle and self.epoch is self.parser._scope_epoch:
            self.parser._lib.fsdb_event_close_v1(self.handle, self.cursor)
        self.cursor.value = 0
        self.buffer = None
