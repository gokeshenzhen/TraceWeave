"""Bounded request-local FST spools and independently positioned logical pages.

Native readers are reaped before exposing a cursor. Aliases share one immutable
spool, not cursor position. No spool or decoded event survives its batch context.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import time

from .cancellation import check_cancelled
from .event_pages import Event, EventPage, RecordingGap, limits
from .fst_runtime import FstError, metadata_owner, request_deadline
from .scope_metadata import ScopeIdentityChanged, file_identity

RECORD = struct.Struct('>qBI')
MAX_SPOOL_BYTES = 64 * 1024 * 1024


class FstBatch:
    def __init__(self, parser, paths, start, end, *, deadline=None, spool_bytes=MAX_SPOOL_BYTES):
        self.parser, self.epoch = parser, parser._scope_epoch
        self.identity = file_identity(parser.file_path)
        if parser._file_identity is not None and parser._file_identity != self.identity:
            raise ScopeIdentityChanged('FST changed; obtain a new parser')
        parser._file_identity = self.identity
        self.deadline = request_deadline(deadline if deadline is not None else time.monotonic() + 30)
        self.directory = None
        self.closed = False
        self.readers = set()
        self.start_fs, self.end_fs = start * 1000, end * 1000 if end >= 0 else -1
        self.check()
        try:
            with parser._session(consume=True, deadline=self.deadline) as worker:
                self.header = worker.header
                result = worker.ask({'op': 'batch', 'paths': paths, 'start_fs': self.start_fs,
                                     'end_fs': self.end_fs, 'spool_bytes': spool_bytes})
                self.declarations, self.errors, self.streams = result['declarations'], result['errors'], result['streams']
                self.metrics = {**worker.metrics, **result['batch_metrics']}
                # Transfer only the directory. Also cover errors in session
                # exit: ownership has already moved when its final check runs.
                self.directory, worker.directory = worker.directory, None
            self.metrics.update(worker.metrics)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        check_cancelled()
        if self.closed or self.epoch is not self.parser._scope_epoch:
            raise ScopeIdentityChanged('FST event cursor owner changed or closed')
        if self.identity != file_identity(self.parser.file_path):
            raise ScopeIdentityChanged('FST changed during event iteration')
        if time.monotonic() >= self.deadline:
            raise FstError('fst_timeout: batch request deadline exceeded')

    def declaration(self, path):
        self.check()
        if path in self.errors:
            reason = self.errors[path]
            if reason in {'fst_signal_not_found', 'fst_signal_ambiguous'}:
                raise KeyError(reason + ': ' + path)
            raise FstError(reason)
        if path in self.declarations:
            return self.declarations[path]
        matches = [r for r in self.declarations.values() if path in (r['path'], r['base'])]
        unique = {r['path']: r for r in matches}
        if len(unique) == 1:
            return next(iter(unique.values()))
        raise KeyError('fst_batch_signal_not_selected: ' + path)

    def close(self):
        self.closed = True
        for reader in tuple(self.readers):
            reader.close()
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


class FstEventReader:
    mode = 'fst_spool_pages_v1'
    native = False  # pages are private spool reads, not new native calls

    def __init__(self, batch, path, start, end, max_events, max_bytes):
        limits(max_events, max_bytes)
        batch.check()
        self.batch, self.closed = batch, False
        self.max_events, self.max_bytes = max_events, max_bytes
        self.start_fs = start * 1000
        self.end_fs = batch.header['end_tick'] * batch.header['scale_fs']
        requested_end = self.end_fs if end < 0 else end * 1000
        if self.start_fs < batch.start_fs or requested_end > (self.end_fs if batch.end_fs < 0 else batch.end_fs):
            raise FstError('fst_batch_window_mismatch')
        self.requested_end = requested_end
        declaration = batch.declaration(path)
        self.width = declaration['width']
        handle = str(declaration['handle'])
        self.stop = batch.streams[handle]['event_bytes']
        self.file = (Path(batch.directory.name) / f'{handle}.events').open('rb')
        batch.readers.add(self)
        self.file.seek(self.stop)
        self.prefix = self._read_record()
        if self.prefix and self.prefix[0].time_fs > requested_end:
            self.prefix = None
        self.file.seek(0)
        self.pending = self._next_event()
        gap_at_start = None
        while self.pending and self.pending[0].time_fs < self.start_fs:
            batch.check()
            event, gap, size = self.pending
            if gap:
                gap_at_start = (event, gap, size) if gap.end_fs >= self.start_fs else None
            else:
                self.prefix = self.pending
                gap_at_start = None
            self.pending = self._next_event()
        self.deferred = None
        if gap_at_start:
            from dataclasses import replace
            event, gap, size = gap_at_start
            self.deferred, self.pending = self.pending, (replace(event, time_fs=self.start_fs,
                time_ps=start), replace(gap, start_fs=self.start_fs), size)
        self.first = True

    def _read_record(self):
        raw = self.file.read(RECORD.size)
        if not raw:
            return None
        if len(raw) != RECORD.size:
            raise FstError('fst_spool_corrupt')
        at, kind, count = RECORD.unpack(raw)
        if count > max(self.width, 256) or kind not in (0, 1, 2, 3):
            raise FstError('fst_spool_corrupt')
        value = self.file.read(count)
        if len(value) != count:
            raise FstError('fst_spool_corrupt')
        value = value.decode('ascii')
        gap = None
        if kind == 3:
            end, reason, inclusive = json.loads(value)
            gap = RecordingGap(at, end, reason, end_inclusive=inclusive)
            value = None
        event = Event(at, (at + 999) // 1000, value,
                      'initial_state' if kind == 1 else 'recording_gap' if kind == 3 else 'transition')
        return event, gap, 64 + count

    def _next_event(self):
        row = self._read_record() if self.file.tell() < self.stop else None
        if row and row[0].time_fs > self.requested_end:
            return None
        if row and row[1] and row[1].end_fs > self.requested_end:
            from dataclasses import replace
            row = row[0], replace(row[1], end_fs=self.requested_end, end_inclusive=True), row[2]
        return row

    def read_page(self):
        self.batch.check()
        if self.closed:
            raise ScopeIdentityChanged('FST event cursor closed')
        previous = initial = None
        rows, gaps, used, count = [], [], 0, 0
        if self.first and self.prefix:
            event, _, size = self.prefix
            if size > self.max_bytes:
                return EventPage((), next_time=event.time_fs, truncated=True)
            if event.kind == 'initial_state':
                initial = event
            else:
                previous = event
            used, count = size, 1
        self.first = False
        while self.pending and count < self.max_events:
            self.batch.check()
            event, gap, size = self.pending
            if used + size > self.max_bytes:
                break
            rows.append(event)
            if gap:
                gaps.append(gap)
            used += size
            count += 1
            if self.deferred:
                self.pending, self.deferred = self.deferred, None
            else:
                self.pending = self._next_event()
        complete = self.pending is None
        return EventPage(tuple(rows), previous,
                         None if complete else self.pending[0].time_fs,
                         complete, not complete and not count, used, initial, tuple(gaps))

    def close(self):
        self.closed = True
        self.file.close()
        self.prefix = self.pending = None
        self.batch.readers.discard(self)


@contextmanager
def fst_batch(parser, paths, start, end, *, deadline=None, spool_bytes=MAX_SPOOL_BYTES):
    deadline = request_deadline(deadline if deadline is not None else time.monotonic() + 30)
    owner = metadata_owner()
    if owner is not None:
        owner.prepare(parser)
    while not parser._lock.acquire(timeout=0.05):
        check_cancelled()
        if time.monotonic() >= deadline:
            raise FstError('fst_timeout: waiting for batch owner')
    old = parser._active_batch
    batch = None
    try:
        batch = FstBatch(parser, paths, start, end, deadline=deadline, spool_bytes=spool_bytes)
        parser._active_batch = batch
        yield batch
        batch.check()
    finally:
        parser._active_batch = old
        if batch is not None:
            batch.close()
        parser._lock.release()
