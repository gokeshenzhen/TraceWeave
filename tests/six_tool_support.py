"""Static fixtures and handwritten semantic oracle shared by acceptance runners."""
from __future__ import annotations
import json
from pathlib import Path
import re
import shutil

FIXTURES = Path(__file__).parent / 'fixtures' / 'six_tool'
ORACLE = json.loads((FIXTURES / 'oracle_v1.json').read_text())
UNITS = ('uvm_pkg.sv', 'p1.sv', 'p2.sv', 'plain.sv', 'a/unit.sv', 'b/unit.sv',
         'unused.sv', 'usage.sv', 'alias_use.sv', 'nested_use.sv', 'empty.sv', 'top.sv')
TOP = 'service_top'


def materialize(root, *, include_opaque=False, alias=False):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for file in FIXTURES.rglob('*'):
        if file.is_file():
            target = root / file.relative_to(FIXTURES)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    units = list(UNITS)
    if include_opaque: units.append('opaque.vhd')
    if alias:
        (root / 'a/observed_alias.sv').symlink_to(root / 'a/unit.sv')
        units[units.index('a/unit.sv')] = 'a/observed_alias.sv'
    paths = [str(root / name) for name in units]
    records = []
    for name in units:
        records.append(f"Parsing design file '{root / name}'\n")
        if name == 'p1.sv': records.append(f"Parsing included file '{root / 'included.svh'}'.\n")
    log = root / 'compile.log'
    log.write_text('Chronologic VCS simulator\nCommand: vcs -sverilog '
                   + f'+incdir+{root} ' + ' '.join(paths) + f' -top {TOP}\n'
                   + ''.join(records) + f'Top Level Modules:\n       {TOP}\n')
    return log


def segments(name):
    return tuple(re.split(r'::|\.', name))


def assert_file_oracle(result, relative, root, *, all_details=None):
    assert result['facts_origin'] == 'slang', result
    assert result['path'] == str(Path(root) / relative), result
    assert result['symbol_inventory_complete'] == ORACLE['file_predicates'][relative][3], result
    columns = ORACLE['predicate_columns']
    assert [result[key] for key in columns] == ORACLE['file_predicates'][relative], result
    symbols = result['symbols']
    if result['symbol_inventory_complete']:
        expected = {(kind, segments(qualified), str(Path(root) / file), line)
                    for file, kind, qualified, line, _ in ORACLE['declarations'] if file == relative}
        actual = {(row['kind'], segments(row['qualified_name']), row['source_file'], row['source_line'])
                  for row in symbols}
        assert actual == expected, (relative, actual, expected)
    for file, kind, qualified, line, parent in ORACLE['declarations']:
        if file != relative: continue
        found = [row for row in symbols if row['kind'] == kind and segments(row['qualified_name']) == segments(qualified)]
        assert len(found) == 1, (relative, kind, qualified, found, symbols)
        row = found[0]
        assert row['source_file'] == str(Path(root) / file) and row['source_line'] == line, row
        assert row['declaration_id'] and row['symbol_id'], row
        if parent is not None and all_details is not None:
            parents = [candidate for detail in all_details.values() for candidate in detail['symbols']
                       if segments(candidate['qualified_name']) == segments(parent)]
            assert len({item['declaration_id'] for item in parents}) == 1, parents
            assert row['scope_id'] == parents[0]['declaration_id'], (row, parents)
    if relative in ORACLE['unknowns']:
        assert ORACLE['unknowns'][relative] in result['coverage_gaps'], result
    else:
        assert result['semantic_status'] == 'complete' and result['coverage_status'] == 'complete', result
