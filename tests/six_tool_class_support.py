"""Handwritten finite class oracle; stdlib-only for independent saved raw checks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

FIXTURES = Path(__file__).parent / 'fixtures' / 'six_tool_class'
ORACLE = json.loads((FIXTURES / 'oracle.json').read_text())
TOP = ORACLE['top']


def materialize(root, *, missing=False, diagnostic=False):
    if missing and diagnostic:
        raise ValueError('missing-base and blocking-diagnostic controls are separate compilations')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for source in FIXTURES.iterdir():
        if source.is_file():
            shutil.copyfile(source, root / source.name)
    log = root / 'compile.log'
    text = (FIXTURES / 'compile.log').read_text().replace('${FIXTURE}', str(root))
    if missing or diagnostic:
        name = 'missing.sv' if missing else 'diagnostic.sv'
        top = 'missing_class_top' if missing else ORACLE['diagnostic']['top']
        text = ('Chronologic VCS simulator\nCommand: vcs -sverilog '
                f'{root / name} -top {top}\n'
                f"Parsing design file '{root / name}'\n"
                f'Top Level Modules:\n       {top}\n')
    log.write_text(text)
    return log


def segments(name):
    return tuple(re.split(r'::|\.', name))


def flatten(result):
    work = list(reversed(result['roots']))
    rows = []
    while work:
        row = work.pop()
        rows.append(row)
        work.extend(reversed(row['children']))
    return rows


def named(rows, qualified, *, status=None):
    matches = [row for row in rows if segments(row['qualified_name']) == segments(qualified)
               and (status is None or row['declaration_status'] == status)]
    assert len(matches) == 1, (qualified, status, matches)
    return matches[0]


def sv_integer(value):
    match = re.fullmatch(r"(?:(?:\d+)?'[sS]?[dD])?([+-]?\d+)", value)
    assert match is not None, value
    return int(match.group(1))


def assert_class_oracle(result, root, *, details=None):
    assert result['facts_origin'] == 'slang', result
    assert result['class_inventory_domain'] == 'declarations_and_referenced_specializations'
    assert result['class_inventory_complete'] is False
    assert result['class_inventory_count'] is None
    assert result['class_inventory_lower_bound'] == ORACLE['class_inventory_lower_bound'], result
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == [], result
    assert ORACLE['generic_declaration_frontier'] in result['coverage_gaps'], result
    assert not result['output_truncated']
    rows = flatten(result)
    assert result['total'] == len(rows) == ORACLE['class_inventory_lower_bound']
    assert len({row['symbol_id'] for row in rows}) == len(rows)
    for file, qualified, line, base in ORACLE['declarations']:
        observed = [row for row in rows if segments(row['qualified_name']) == segments(qualified)
                    and row['declaration_status'] == 'declaration']
        assert observed, (qualified, rows)
        for row in observed:
            assert row['source_file'] == str(Path(root) / file) and row['source_line'] == line, row
            assert row['declaration_id'] and row['type_identity_domain'] == 'artifact_canonical', row
            if base is not None:
                assert row['base_status'] == 'resolved', row
                assert segments(row['base_qualified_name']) == segments(base), row
            if details is not None:
                matches = [item for item in details[file]['symbols']
                           if item['kind'] == 'class' and item['source_line'] == line
                           and item['name'] == row['name']]
                assert matches, (row, details[file])
                assert {item['declaration_id'] for item in matches} == {row['declaration_id']}, (row, matches)
                assert {item['source_file'] for item in matches} == {row['source_file']}
                parent = ORACLE['declaration_scopes'][qualified]
                parent_rows = [item for detail in details.values() for item in detail['symbols']
                               if segments(item['qualified_name']) == segments(parent)]
                assert parent_rows, (row, parent)
                assert {item['declaration_id'] for item in parent_rows} == {row['scope_id']}, (row, parent_rows)
                assert {item['scope_id'] for item in matches} == {row['scope_id']}, (row, matches)
    arg_a, arg_b = named(rows, 'alpha::ArgA'), named(rows, 'alpha::ArgB')
    assert arg_a['type_id'] != arg_b['type_id']
    by_id = {row['symbol_id']: row for row in rows}
    for derived_name, argument, value in ORACLE['specialization_bindings']:
        derived = named(rows, derived_name)
        base = by_id[derived['base_symbol_id']]
        actual_argument = named(rows, argument)
        assert derived['base_relation'] == 'specialization'
        assert derived['base_type_parameter_bindings'] == {'T': actual_argument['type_id']}, derived
        assert derived['base_parameter_status'] == 'observed'
        assert sv_integer(derived['base_value_parameters']['N']) == value
        assert base['declaration_status'] == 'specialization'
        assert base['type_parameter_bindings'] == derived['base_type_parameter_bindings']
        assert sv_integer(base['parameterization']['N']) == value
    for left, right in ORACLE['identity_equal']:
        assert named(rows, left)['base_symbol_id'] == named(rows, right)['base_symbol_id']
    for left, right in ORACLE['identity_different']:
        assert named(rows, left)['base_symbol_id'] != named(rows, right)['base_symbol_id']
    typed = named(rows, 'beta::TypeBound')
    assert typed['base_value_parameters'] == {} and typed['base_parameter_status'] == 'observed'
    assert typed['base_type_parameter_bindings'] == {'T': arg_a['type_id']}
    inner = [row for row in rows if row['source_line'] == 15 and row['name'] == 'Inner']
    assert len(inner) == ORACLE['nested_actual_type_count'], inner
    assert len({row['type_id'] for row in inner}) == len(inner), inner
    assert len({row['declaration_id'] for row in inner}) == 1, inner
    assert len({row['declaration_id'] for row in rows if row['name'] == 'Same'}) == 2
    return rows


def assert_sequence_result(label, result, results, fixture, spec):
    """Called only after the transport saved raw and decoded public output."""
    if label in {'classes', 'repeat'}:
        assert_class_oracle(result, fixture)
    elif label == 'file_classes':
        assert_class_oracle(results['classes'], fixture,
                            details={'classes.sv': result, 'external.sv': results['file_external']})
    elif label == 'bare_name':
        assert result['error'] == 'class_name_ambiguous' and len(result['candidates']) == 2
    elif label == 'qualified':
        rows = flatten(result)
        assert len(rows) == 1 and segments(rows[0]['qualified_name']) == segments('beta::BoundA')
    elif label == 'depth_zero':
        assert result['total'] == 1 and result['output_truncated']
    elif label == 'display_limit':
        assert result['total'] == 2 and result['output_truncated']
    elif label == 'stable_identity':
        expected = named(flatten(results['classes']), 'beta::Same')
        assert result['total'] == 1 and result['roots'][0]['symbol_id'] == expected['symbol_id']
