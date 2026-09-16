"""Bounded reuse of complete clock indexes, owned by parser snapshots.

Only compact timestamps and the global period survive a query. No native
handles, parsers or transition dictionaries are retained. The lock protects
bookkeeping across VCD workers; it is never held during waveform I/O.
"""

from __future__ import annotations

from array import array
from collections import OrderedDict
from dataclasses import dataclass
import sys
import threading
import weakref

from config import (
    CLOCK_EDGE_CACHE_MAX_BYTES,
    CLOCK_EDGE_CACHE_ENTRY_MAX_BYTES,
    CLOCK_EDGE_CACHE_MAX_ENTRIES,
)
from .cancellation import check_cancelled

_EDGE_BYTES = array("Q").itemsize
_EMPTY_ARRAY_BYTES = sys.getsizeof(array("Q"))


class ClockCacheToken:
    """Opt-in identity whose lifetime follows one parser's waveform snapshot."""

    __slots__ = ("__weakref__",)


@dataclass(frozen=True)
class _Entry:
    key: tuple[str, str]
    edges: array
    period: int | None
    size: int


class ClockEdgeCache:
    def __init__(self, *, max_bytes=CLOCK_EDGE_CACHE_MAX_BYTES,
                 entry_max_bytes=CLOCK_EDGE_CACHE_ENTRY_MAX_BYTES,
                 max_entries=CLOCK_EDGE_CACHE_MAX_ENTRIES):
        self.max_bytes = max_bytes
        self.entry_max_bytes = entry_max_bytes
        self.max_entries = max_entries
        self._entries: OrderedDict[weakref.ReferenceType, _Entry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, token, key):
        if token is None:
            return None
        with self._lock:
            ref = weakref.ref(token)
            entry = self._entries.get(ref)
            if entry is None or entry.key != key:
                return None
            self._entries.move_to_end(ref)
            return entry.edges, entry.period

    def put(self, token, key, edges, period):
        if token is None or self.max_entries <= 0:
            return
        limit = min(self.entry_max_bytes, self.max_bytes)
        if len(edges) * _EDGE_BYTES + _EMPTY_ARRAY_BYTES > limit:
            return
        check_cancelled()
        try:
            compact = array("Q", edges)
        except (OverflowError, TypeError, MemoryError):
            # Non-uint64 times or allocation pressure are optimization bypasses.
            return
        size = sys.getsizeof(compact)
        if size > limit:
            return
        check_cancelled()
        with self._lock:
            ref = weakref.ref(token, self._discard_ref)
            prior = self._entries.pop(ref, None)
            if prior is not None:
                self._bytes -= prior.size
            while self._entries and (
                self._bytes + size > self.max_bytes or
                len(self._entries) >= self.max_entries
            ):
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= evicted.size
            self._entries[ref] = _Entry(key, compact, period, size)
            self._bytes += size

    def _discard_ref(self, ref):
        with self._lock:
            entry = self._entries.pop(ref, None)
            if entry is not None:
                self._bytes -= entry.size

    def discard(self, token):
        if token is not None:
            self._discard_ref(weakref.ref(token))

    def snapshot(self):
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes}


cache = ClockEdgeCache()


def discard_clock_edges(parser):
    cache.discard(getattr(parser, "_clock_cache_token", None))
