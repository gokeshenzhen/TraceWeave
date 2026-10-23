"""Private copied scan frontiers. No native handles or source bodies survive."""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
import re
import secrets
import time

from .structural_semantic_router import NPI_CATEGORIES

MAX_FRONTIER_BYTES = 1024 * 1024
MAX_PENDING = 8192
MAX_CUMULATIVE = 1_000_000_000


class ScanContinuationInvalid(ValueError):
    pass


def new_job(path, parent=None, depth=0):
    return dict(path=path, parent=parent, depth=depth, phase='ports', port_index=0,
                child_index=0, port_fingerprint='', child_fingerprint='', children=[],
                signal_index=0, bit_index=0, signal_fingerprint='', signal_current_fingerprint='')


def job_position(job):
    return tuple(job[k] for k in ('path','phase','port_index','signal_index','bit_index','child_index'))


def initial_state(root, categories):
    return dict(version=2, pending=[new_job(root)], completed_instances=0,
                counts={}, category_gaps={c: [] for c in categories & NPI_CATEGORIES}, gap_summary=[])


def validate_state(state, *, top, root, categories):
    from .schemas import StructuralGapSummary
    if not isinstance(state, dict) or len(json.dumps(state).encode()) > MAX_FRONTIER_BYTES:
        raise ValueError('invalid scan frontier')
    if (set(state) != {'version', 'pending', 'completed_instances', 'counts', 'category_gaps', 'gap_summary'}
            or type(state['version']) is not int or state['version'] != 2):
        raise ValueError('invalid scan state fields')
    if type(state['completed_instances']) is not int or not 0 <= state['completed_instances'] <= MAX_CUMULATIVE:
        raise ValueError('invalid cumulative scan count')
    if (not isinstance(state['counts'], dict) or set(state['counts']) - (set(categories) & NPI_CATEGORIES)
            or any(type(n) is not int or not 0 <= n <= MAX_CUMULATIVE for n in state['counts'].values())):
        raise ValueError('invalid cumulative fact counts')
    if not isinstance(state['category_gaps'], dict) or set(state['category_gaps']) != set(categories) & NPI_CATEGORIES:
        raise ValueError('invalid cumulative scan gaps')
    for gaps in state['category_gaps'].values():
        if not isinstance(gaps, list) or len(gaps) > 64 or any(not isinstance(g, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,127}', g) for g in gaps):
            raise ValueError('invalid scan gap labels')
    if not isinstance(state['gap_summary'], list) or len(state['gap_summary']) > 64:
        raise ValueError('invalid scan diagnostics')
    for row in state['gap_summary']:
        StructuralGapSummary.model_validate(row)
    if sum(len(r['examples']) for r in state['gap_summary']) > 16:
        raise ValueError('unbounded scan examples')
    pending = state['pending']
    if not isinstance(pending, list) or len(pending) > MAX_PENDING:
        raise ValueError('invalid pending scopes')
    paths = set()
    def job(row, child=False):
        if not isinstance(row, dict) or set(row) != {'path', 'parent', 'depth', 'phase', 'port_index', 'child_index', 'port_fingerprint', 'child_fingerprint', 'children', 'signal_index', 'bit_index', 'signal_fingerprint', 'signal_current_fingerprint'}:
            raise ValueError('invalid scope job')
        path, parent = row['path'], row['parent']
        if (not isinstance(path, str) or not 1 <= len(path) <= 16384
                or not (path == root or path.startswith(root + '.'))
                or not (path == top or path.startswith(top + '.')) or path in paths):
            raise ValueError('foreign or duplicate scan scope')
        paths.add(path)
        if len(paths) > MAX_PENDING:
            raise ValueError('unbounded child frontier')
        if parent is None:
            if path != root or row['depth'] != 0:
                raise ValueError('unbound scan scope')
        elif (not isinstance(parent, str) or not path.startswith(parent + '.') or path == root
                or not (parent == root or parent.startswith(root + '.')) or row['depth'] == 0):
            raise ValueError('invalid scan parent')
        if type(row['depth']) is not int or not 0 <= row['depth'] <= 129:
            raise ValueError('invalid scan depth')
        if row['phase'] not in {'ports', 'signals', 'children'} or child and row['phase'] != 'ports':
            raise ValueError('invalid scope phase')
        if any(type(row[k]) is not int or not 0 <= row[k] <= 100000 for k in ['port_index', 'child_index', 'signal_index']):
            raise ValueError('invalid scope position')
        if type(row['bit_index']) is not int or not 0 <= row['bit_index'] <= 4096:
            raise ValueError('invalid bit position')
        if child and (row['port_index'] or row['child_index'] or row['signal_index'] or row['bit_index']):
            raise ValueError('uninspected child has a position')
        for index, fingerprint in [('port_index', 'port_fingerprint'), ('child_index', 'child_fingerprint'), ('signal_index', 'signal_fingerprint')]:
            value = row[fingerprint]
            if not isinstance(value, str) or (value != '' if row[index] == 0 else not re.fullmatch(r'[0-9a-f]{64}', value)):
                raise ValueError('invalid scan position fingerprint')
        current = row['signal_current_fingerprint']
        if (not isinstance(current, str) or (current != '' if row['bit_index'] == 0 else not re.fullmatch(r'[0-9a-f]{64}', current))):
            raise ValueError('invalid current signal fingerprint')
        if row['phase'] == 'ports' and (row['child_index'] or row['children'] or row['signal_index'] or row['bit_index']):
            raise ValueError('port phase has inspected children')
        if row['phase'] == 'signals' and (row['child_index'] or row['children']):
            raise ValueError('signal phase has inspected children')
        if row['phase'] == 'children' and row['bit_index']:
            raise ValueError('child phase has unfinished signal')
        if not isinstance(row['children'], list) or len(row['children']) > MAX_PENDING or child and row['children']:
            raise ValueError('invalid child frontier')
        for c in row['children']:
            if not isinstance(c, dict) or c.get('parent') != path or c.get('depth') != row['depth'] + 1:
                raise ValueError('invalid child binding')
            job(c, True)
    for row in pending:
        job(row)
    return copy.deepcopy(state)


class ScanContinuationStore:
    """Event-loop owned, bounded, expiring, consumed only after validation."""
    def __init__(self, *, capacity=16, ttl=600, max_bytes=16 * 1024 * 1024):
        self.capacity, self.ttl, self.max_bytes = capacity, ttl, max_bytes
        self.entries = OrderedDict()

    def _prune(self):
        now = time.monotonic()
        for token, (expiry, _, _, _) in list(self.entries.items()):
            if expiry <= now:
                del self.entries[token]

    def get(self, token, key):
        self.check(token, key)
        _, stored_key, state, size = self.entries[token]
        self.entries[token] = (time.monotonic() + self.ttl, stored_key, state, size)
        self.entries.move_to_end(token)
        return copy.deepcopy(state)

    def check(self, token, key):
        self._prune()
        item = self.entries.get(token)
        if item is None or item[1] != key:
            raise ScanContinuationInvalid('native_scan_continuation_expired_or_changed')

    def publish(self, old_token, key, state):
        self._prune()
        size = len(json.dumps(state).encode())
        if size > MAX_FRONTIER_BYTES:
            raise ScanContinuationInvalid('native_frontier_limit')
        if old_token is not None:
            self.check(old_token, key)
            del self.entries[old_token]
        token = secrets.token_urlsafe(24)
        while self.entries and (len(self.entries) >= self.capacity or
                               sum(v[3] for v in self.entries.values()) + size > self.max_bytes):
            self.entries.popitem(last=False)
        self.entries[token] = (time.monotonic() + self.ttl, key, copy.deepcopy(state), size)
        return token

    def discard(self, token):
        self.entries.pop(token, None)
