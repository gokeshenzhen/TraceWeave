"""Lexical candidate bounds must preserve parsed membership and page receipts."""
import json
import random

from src.fst_worker import Declaration, Scope, public, scope_page


def declaration(parts, name):
    ancestors = tuple('.'.join(parts[:i]) for i in range(1, len(parts) + 1))
    scope = Scope('.'.join(parts), ancestors, parts[0] if parts else None)
    path = scope.path + '.' + name if parts else name
    return Declaration(path, path, name, scope, 1, 1, False, 'unknown', 'wire', (0, 0), True)


def reference(index, message):
    # Original full-scan algorithm retained as a differential oracle. It has
    # no prefix bounds/bisect and deliberately visits descendants in direct
    # mode, even when none will be returned on this page.
    rows, used, visited, after, reason = [], 0, 0, None, None
    for path in sorted(index):
        if message['after'] is not None and path < message['after']:
            continue
        row = index[path]
        if message['scope'] and message['scope'] not in row.scope.ancestors:
            continue
        if len(rows) >= message['max_items'] or visited >= message['max_visited']:
            after = path
            reason = 'page_items' if len(rows) >= message['max_items'] else 'page_scan'
            break
        visited += 1
        if message['direct'] and row.scope.path != message['scope']:
            continue
        result = public(row)
        size = len(json.dumps(result, ensure_ascii=True).encode())
        if used + size > message['max_bytes']:
            after, reason = path, 'page_bytes'
            break
        rows.append(result); used += size
    return dict(kind='scope', results=rows, bytes_returned=used, visited=visited,
                next_path=after, stop_reason=reason)


def test_scope_pages_match_full_scan_for_escaped_names_and_all_limits():
    parts = [[], ['top'], ['top', 'a'], ['top', 'a', 'child'], ['top', 'a/neighbor'],
             ['top', '\\with.dot'], ['top', '\\with.dot', 'child'],
             ['top', '层', '深'], ['top', 'a.ambiguous'], ['top', 'a', 'ambiguous'],
             ['top', '\\ends.'], ['top', 'gen[3]']]
    rows = [declaration(p, f'{n}{i}') for i, p in enumerate(parts)
            for n in ('s', '\\signal.with.dot', 'a.fake_scope.signal', 'z', '旗')]
    index = {row.path: row for row in rows}
    paths = sorted(index)
    scopes = ['', 'top', 'top.a', 'top.a.ambiguous', 'top.\\with', 'top.\\with.dot',
              'top.\\ends.', 'top.层', 'top.gen[3]', 'absent']
    rng = random.Random(20261001)
    for _ in range(1000):
        query = dict(scope=rng.choice(scopes), direct=rng.choice([True, False]),
                     after=rng.choice([None, '', 'zzzz', *paths]), max_items=rng.choice([1, 3, 512]),
                     max_visited=rng.choice([1, 4, 4096]), max_bytes=rng.choice([1, 200, 600, 65536]))
        assert scope_page(index, paths, query) == reference(index, query)
    # Inclusive cursor and repeated/zero-progress byte pages retain exactly
    # the original next_path, visited count and stop reason.
    for scope in scopes:
        for direct in (True, False):
            after = None
            while True:
                query = dict(scope=scope, direct=direct, after=after,
                             max_items=1, max_visited=2, max_bytes=4096)
                page = scope_page(index, paths, query)
                assert page == reference(index, query)
                assert scope_page(index, paths, query) == page
                if page['next_path'] is None:
                    break
                assert page['next_path'] != after
                after = page['next_path']


def test_narrow_scope_never_looks_up_unrelated_declarations():
    class Counted(dict):
        reads = 0
        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)
    rows = [declaration(['top', f's{i:05}'], 'a') for i in range(10000)]
    index = Counted((r.path, r) for r in rows)
    paths = sorted(index)
    query = dict(scope='top.s09999', direct=False, after=None,
                 max_items=512, max_visited=4096, max_bytes=65536)
    page = scope_page(index, paths, query)
    assert len(page['results']) == 1 and index.reads == 1
    index.reads = 0
    assert scope_page(index, paths, {**query, 'scope': 'top.s09999.absent'})['results'] == []
    assert index.reads == 0


def test_dot_in_signal_name_is_not_a_scope():
    rows = [declaration(['top'], '\\ghost.inner.signal'),
            declaration(['top', '\\real.inner'], 'signal')]
    index = {r.path: r for r in rows}
    query = dict(scope='top.\\ghost', direct=False, after=None,
                 max_items=512, max_visited=4096, max_bytes=65536)
    page = scope_page(index, sorted(index), query)
    assert page['results'] == [] and page['visited'] == 0 and page['next_path'] is None
    page = scope_page(index, sorted(index), {**query, 'scope': 'top.\\real'})
    assert page['results'] == [] and page['visited'] == 0
