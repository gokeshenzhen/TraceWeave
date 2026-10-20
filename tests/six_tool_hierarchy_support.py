"""Stdlib-only, handwritten V3 topology/class/export oracle and fresh hook."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from tests.six_tool_class_support import flatten as flatten_classes, segments, sv_integer

FIXTURES = Path(__file__).parent / 'fixtures' / 'six_tool_hierarchy'
ORACLE = json.loads((FIXTURES / 'oracle.json').read_text())
TOP = ORACLE['top']
EXPECTED = {row[0]: row for row in ORACLE['rows']}


def materialize(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for source in FIXTURES.iterdir():
        if source.is_file(): shutil.copyfile(source, root / source.name)
    log = root / 'compile.log'
    log.write_text(log.read_text().replace('${FIXTURE}', str(root)))
    return log


def flatten_tree(node):
    result, pending = [], [node] if node is not None else []
    while pending:
        item = pending.pop()
        result.append(item)
        pending.extend(reversed(item['children']))
    return result


def assert_row(row, root):
    expected = EXPECTED[row['path']]
    path, parent, kind, module, site_file, site_line, parameter = expected
    assert row['parent'] == parent and row['kind'] == kind and row['module'] == module, row
    assert row['source_file'] == str(Path(root) / site_file) and row['source_line'] == site_line, row
    assert row['gaps'] == [], row
    if module is not None:
        definition_file, definition_line = ORACLE['definitions'][module]
        assert row['definition_file'] == str(Path(root) / definition_file), row
        assert row['definition_line'] == definition_line and row['definition_id'], row
    else:
        assert row['definition_id'] is row['definition_file'] is row['definition_line'] is None, row
    if parameter is not None:
        assert sv_integer(row['parameterization']['W']) == parameter, row
    return row


def assert_tree(result, root, *, full=True):
    assert result['facts_origin'] == 'slang' and result['analysis_limits'] == [], result
    node = result.get('node', result.get('data'))
    rows = flatten_tree(node)
    for row in rows: assert_row(row, root)
    if full:
        assert {row['path'] for row in rows} == set(EXPECTED), result
        assert result['total_descendants'] == result['count_lower_bound'] == ORACLE['descendants'], result
        assert result['count_status'] == 'exact' and result['coverage_status'] == 'complete'
        assert not result['output_truncated']
        assert node['child_count'] == len(ORACLE['direct_children'])
        assert {row['path'] for row in node['children']} == {TOP + '.' + name for name in ORACLE['direct_children']}
        by_path = {row['path']: row for row in rows}
        assert by_path[TOP + '.a']['definition_id'] == by_path[TOP + '.b']['definition_id']
        assert by_path[TOP + '.banks[0]']['definition_id'] == by_path[TOP + '.banks[1]']['definition_id']
        assert by_path[TOP + '.low']['definition_id'] != by_path[TOP + '.high']['definition_id']
    return rows


def assert_module(result, root, module='v3_leaf', *, exact=True):
    assert result['facts_origin'] == 'slang' and result['analysis_limits'] == [], result
    expected = {path for path, row in EXPECTED.items() if row[3] == module}
    for row in result['hits']:
        assert_row(row, root)
        assert row['path'] in expected, row
    assert result['count_domain'] == 'matching_instances_under_top'
    if exact:
        assert result['total'] == result['count_lower_bound'] == ORACLE['module_counts'][module], result
        assert result['count_status'] == 'exact' and result['coverage_status'] == 'complete', result
    return result['hits']


def assert_classes(result, root, detail=None, *, section=False):
    roots = result['data'] if section else result['roots']
    rows = flatten_classes({'roots': roots})
    assert result['facts_origin'] == 'slang' and result['analysis_limits'] == [], result
    assert result['class_inventory_count'] == result['class_inventory_lower_bound'] == 2, result
    assert result['class_inventory_complete'] and result['coverage_status'] == 'complete', result
    assert not result['output_truncated'] and result['total'] == 2, result
    by_name = {segments(row['qualified_name']): row for row in rows}
    for qualified, line, base in ORACLE['classes']:
        row = by_name[segments(qualified)]
        assert row['source_file'] == str(Path(root) / 'types.sv') and row['source_line'] == line, row
        assert row['type_identity_domain'] == 'artifact_canonical' and row['declaration_id'] and row['scope_id']
        if base is None:
            assert row['base_status'] == 'none' and row['base_symbol_id'] is None
        else:
            assert row['base_status'] == 'resolved' and row['base_symbol_id'] == by_name[segments(base)]['symbol_id']
        if detail is not None:
            matches = [item for item in detail['symbols'] if item['kind'] == 'class' and item['source_line'] == line]
            assert len(matches) == 1 and matches[0]['declaration_id'] == row['declaration_id'], (row, matches)
            package = [item for item in detail['symbols'] if item['kind'] == 'package' and item['name'] == 'v3_types']
            assert len(package) == 1 and package[0]['declaration_id'] == row['scope_id'] == matches[0]['scope_id']
    return rows


def assert_sequence_result(label, result, results, fixture, spec):
    if label == 'reprepare':
        assert result['prepared']['context_identity_sha256'] == results['02_prepare']['context_identity_sha256']
        assert result['prepared']['design_handle'] != results['02_prepare']['design_handle']
        assert result['prepared']['selected_top'] == TOP
    elif label in {'classes', 'classes_rebuilt', 'classes_after_restart'}:
        assert_classes(result, fixture)
    elif label == 'detail':
        assert_classes(results['classes'], fixture, result)
    elif label in {'modules', 'modules_rebuilt', 'modules_after_restart'}:
        assert_module(result, fixture)
    elif label in {'tree', 'component_tree_full'}:
        assert_tree(result, fixture)
        if label == 'component_tree_full': assert result['export_complete']
    elif label == 'class_hierarchy_full':
        assert_classes(result, fixture, results['detail'], section=True)
        assert result['export_complete']
    elif label == 'files_full':
        assert {Path(item['path']).name for item in result['data']} == {'types.sv', 'hierarchy.sv', 'outside.sv'}
        assert result['facts_origin'] == 'slang' and result['semantic_status'] == 'partial'
        outside = next(item for item in result['data'] if Path(item['path']).name == 'outside.sv')
        assert ORACLE['outside_file_frontier'] in outside['semantic_gaps'], outside
        assert not result['export_complete'] and result['count_status'] == 'lower_bound'
    elif label == 'interfaces':
        assert {item['name']: item['source_line'] for item in result['data']} == {'v3_bus': 7, 'v3_bus_unused': 10}, result
        assert all(item['source_file'] == str(Path(fixture) / 'types.sv') for item in result['data'])
    elif label == 'module_display_limit':
        assert_module(result, fixture)
        assert len(result['hits']) == 1 and result['output_truncated']
    elif label == 'tree_display_limit':
        assert_tree(result, fixture, full=False)
        assert result['total_descendants'] == 28 and result['output_truncated']
        assert len(flatten_tree(result['node'])) == 2
    elif label == 'exact_point':
        assert len(result['hits']) == 1 and result['total'] == 1
        assert_row(result['hits'][0], fixture)
    elif label == 'absent_module':
        assert_module(result, fixture, 'NoSuchModule')
    elif label.startswith('churn_'):
        assert result['facts_origin'] == 'slang' and result['node'] is not None, result
        for row in flatten_tree(result['node']): assert_row(row, fixture)
