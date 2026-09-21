"""Bounded read-only observations belonging to one divergence graph attempt."""
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType
from itertools import chain

from .cancellation import check_cancelled
from .divergence_compare import file_identity, read_stream

MAX_EVENTS = 65536


def selection_identity(parser, signal):
    """A display key is never a selection identity; keep ordered declared bits."""
    projection = getattr(parser, 'projections', {}).get(signal)
    if projection is not None:
        parser._validate(projection)
        return (projection.path, projection.declared.left, projection.declared.right,
                projection.selection.bits)
    # Exact declarations can safely share storage aliases. Suffix-compatible
    # library parsers keep the original path when no declaration is available.
    getter = getattr(parser, 'get_signal_declaration', None)
    if getter is not None:
        try:
            declaration = getter(signal)
            bounds = declaration['declared_range']
            return (declaration.get('storage_key', declaration['path']),
                    bounds['left'], bounds['right'], declaration['width'])
        except (KeyError, ValueError):
            pass
    return (signal,)


class ObservationSession:
    def __init__(self, budget=None, *, max_events=MAX_EVENTS, max_bytes=None, stream_reader=read_stream):
        from . import divergence_budget
        self.budget = budget
        self.max_events = max_events
        self.max_bytes = divergence_budget.DIVERGENCE_MAX_WAVE_CACHE_BYTES if max_bytes is None else max_bytes
        self.entries = OrderedDict()
        self.events = self.nbytes = self.peak_events = self.peak_bytes = 0
        self.hits = self.misses = self.evictions = self.bypasses = 0
        self.namespace = None
        self.identities = {}
        self.stream_reader = stream_reader

    def clear(self):
        self.entries.clear()
        self.events = self.nbytes = 0
        if self.budget:
            self.budget.cache_bytes = 0

    def bind(self, namespace):
        if self.namespace != namespace:
            self.clear()
            self.namespace = namespace

    def _check(self):
        check_cancelled()
        if self.budget:
            self.budget.check()

    def identity(self, parser, wave, signal):
        self._check()
        identity = file_identity(wave)
        if wave in self.identities and self.identities[wave] != identity:
            self.clear()
        self.identities[wave] = identity
        if identity is None:
            return None
        return (self.namespace, identity, getattr(parser, '_scope_epoch', id(parser)), selection_identity(parser, signal))

    def _put(self, key, value, events, nbytes):
        if events > self.max_events or nbytes > self.max_bytes:
            self.bypasses += 1
            return
        if key in self.entries:
            _, old_events, old_bytes = self.entries.pop(key)
            self.events -= old_events
            self.nbytes -= old_bytes
        while self.entries and (self.events + events > self.max_events or self.nbytes + nbytes > self.max_bytes):
            _, (_, old_events, old_bytes) = self.entries.popitem(last=False)
            self.events -= old_events; self.nbytes -= old_bytes; self.evictions += 1
        self.entries[key] = (value, events, nbytes)
        self.events += events; self.nbytes += nbytes
        self.peak_events = max(self.peak_events, self.events)
        self.peak_bytes = max(self.peak_bytes, self.nbytes)
        if self.budget:
            self.budget.cache_bytes = self.nbytes
            self.budget.cache_peak = max(self.budget.cache_peak, self.nbytes)

    def stream(self, get_parser, wave, signal, start, end, consume=None):
        parser = get_parser(wave)
        identity = self.identity(parser, wave, signal)
        if identity is not None:
            for key, (stream, _, _) in reversed(self.entries.items()):
                if key[0] == 'stream' and key[1] == identity and key[2] <= start and key[3] >= end >= 0:
                    self.hits += 1
                    self.entries.move_to_end(key)
                    rows = stream.transitions
                    lo = bisect_left(rows, start * 1000, key=lambda e:e.get('time_fs', e['time_ps'] * 1000))
                    hi = bisect_right(rows, end * 1000, key=lambda e:e.get('time_fs', e['time_ps'] * 1000))
                    return replace(stream, transitions=rows[lo:hi],
                                   predecessor=rows[lo-1] if lo else stream.predecessor)
        self.misses += 1
        stream = self.stream_reader(lambda _: parser, wave, signal, start, end)
        if consume:
            consume(stream)
        self._check()
        if identity is None or stream.error or stream.truncated or identity != self.identity(parser, wave, signal):
            return stream
        events = len(stream.transitions) + bool(stream.predecessor)
        # Conservative object/key overhead, including enriched integer values.
        nbytes = 1024 + len(repr(identity))*4
        for i, row in enumerate(chain(stream.transitions, (stream.predecessor,) if stream.predecessor else ())):
            if i % 1024 == 0:
                self._check()
            nbytes += 768 + len(str(row.get('value', '')))*4
        if events > self.max_events or nbytes > self.max_bytes:
            self.bypasses += 1
            return stream
        def freeze(row):
            if row is None:
                return None
            value = row.get('value')
            return MappingProxyType({**row, 'value':MappingProxyType(dict(value)) if isinstance(value, dict) else value})
        stream = replace(stream, transitions=tuple(freeze(r) for r in stream.transitions),
                         predecessor=freeze(stream.predecessor))
        covered_end = stream.end if end < 0 else end
        self._put(('stream', identity, start, covered_end), stream, events, nbytes)
        return stream

    def sample_key(self, parser, wave, expr, start, end, time, phase, offset):
        identity = self.identity(parser, wave, expr.signal)
        return None if identity is None else ('sample', identity, expr.bits, expr.declared_bits,
                                              expr.width, start, end, time, phase, offset)

    def sample_get(self, key):
        self._check()
        if key is not None and key in self.entries:
            self.hits += 1
            self.entries.move_to_end(key)
            return deepcopy(self.entries[key][0])
        return None

    def sample_put(self, key, result):
        self._check()
        if key is not None:
            self._put(key, deepcopy(result), 1, 2048 + len(repr(key))*4 + len(str(result))*4)

    def metrics(self):
        return dict(observation_cache_hits=self.hits, observation_cache_misses=self.misses,
            observation_cache_evictions=self.evictions, observation_cache_bypasses=self.bypasses,
            observation_cache_peak_events=self.peak_events, observation_cache_peak_bytes=self.peak_bytes)
