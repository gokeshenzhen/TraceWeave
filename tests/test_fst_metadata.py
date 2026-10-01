"""Capacity and retained-object checks independent of local real-design dumps."""
import sys

import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
from fst_fixture import write_fst
from src.fst_parser import FSTParser
from src.fst_runtime import FstError, FstProcess


def test_declaration_limit_fits_compact_index_and_keeps_alias_identity(tmp_path):
    count = 32768
    path = write_fst(tmp_path / 'capacity.fst', [(0, 'a', '1')], declarations=[
        ('a', 1, 'wire', 'input', None),
        *[(f'alias{i:05}', 1, 'wire', 'output', 'a') for i in range(count - 1)]])
    with FstProcess(path) as worker:
        assert worker.ask({'op': 'metadata'})['count'] == count
        assert worker.metrics['index_accounted_bytes'] <= 32 * 1024 * 1024
        rows, after = [], None
        while True:
            page = worker.ask(dict(op='scope', scope='top', direct=True, after=after,
                                   max_items=1024, max_bytes=512 * 1024, max_visited=4096))
            rows.extend(page['results'])
            after = page['next_path']
            if after is None:
                break
        assert [r['path'] for r in rows] == ['top.a', *[f'top.alias{i:05}' for i in range(count - 1)]]
        assert sum(r['is_alias'] for r in rows) == count - 1
        original = worker.ask({'op': 'declaration', 'path': 'top.a'})['declaration']
        alias = worker.ask({'op': 'declaration', 'path': 'top.alias32766'})['declaration']
        assert alias['handle'] == original['handle']
        assert alias['declared_range'] == original['declared_range'] == {'left': 0, 'right': 0}


def test_accounting_bounds_unique_retained_objects_with_unicode_and_depth(tmp_path):
    from pylibfst import lib, ffi
    from src import fst_worker
    names = ['a', 'bus [1023:0]', 'alias [0:1023]', '旗' * 500]
    declarations = [(names[0], 1, 'wire', 'input', None),
                    (names[1], 1024, 'wire', 'output', None),
                    (names[2], 1024, 'wire', 'input', names[1]),
                    (names[3], 1, 'wire', 'implicit', 'a')]
    scopes = ['top', *[f'层{i}' for i in range(255)]]
    path = write_fst(tmp_path / 'deep.fst', [(0, 'a', '1')], declarations=declarations,
                     scopes_by_name={n: scopes for n in names})
    reader = lib.fstReaderOpen(str(path).encode())
    try:
        index = fst_worker.build_index(lib, ffi, reader)
    finally:
        lib.fstReaderClose(reader)
    stack, seen, retained = [index], set(), 0
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        retained += sys.getsizeof(obj)
        if isinstance(obj, dict):
            stack.extend(obj.keys()); stack.extend(obj.values())
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
    assert retained <= fst_worker.METRICS['index_accounted_bytes'] <= fst_worker.MAX_INDEX_BYTES
    rows = list(index.values())
    assert all(row.scope is rows[0].scope for row in rows)
    assert len(rows[0].scope.ancestors) == 256
    assert fst_worker.declaration(rows[2])['declared_range'] == {'left': 0, 'right': 1023}


@pytest.mark.parametrize('name,width,scopes,reason', [
    ('n' * 4097, 1, ['top'], 'width_or_name_limit'),
    ('a', 65537, ['top'], 'width_or_name_limit'),
    ('a', 1, ['s'] * 257, 'scope_limit'),
    ('a', 1, ['s' * 4097], 'scope_limit'),
])
def test_oversized_metadata_fails_explicitly(tmp_path, name, width, scopes, reason):
    path = write_fst(tmp_path / 'oversized.fst', [(0, name, '0' * width)],
                     declarations=[(name, width, 'wire', 'input', None)],
                     scopes_by_name={name: scopes})
    with pytest.raises(FstError, match=reason):
        FSTParser(path).get_summary()


def test_scope_budget_applies_even_with_few_declarations(tmp_path):
    # libfst's writer shortens long scope names. Several independent deep
    # branches exercise retained path memory using names it preserves.
    names = ['a', 'b', 'c']
    path = write_fst(tmp_path / 'scope-budget.fst', [(0, 'a', '1')],
        declarations=[(n, 1, 'wire', 'input', None) for n in names],
        scopes_by_name={n: [n, *['s' * 500 for _ in range(255)]] for n in names})
    with pytest.raises(FstError, match='metadata_memory_limit'):
        FSTParser(path).get_summary()
