"""Real native identity regressions; raw facts precede every assertion.

Declared parent relationships and physical locations are handwritten. These
tests do not recover expected identities from the product's old output.
"""
from dataclasses import replace
import json
import re
import threading
import traceback
from types import SimpleNamespace

import pytest

from src.cancellation import pop_cancel_event, push_cancel_event
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.slang_connectivity_projector import ProjectionOptions, _kind_name
from src.slang_source_catalog import collect_source_catalog, _declaration_identity
from src.slang_source_codec import decode_source_catalog
from src.source_graph_contract import SemanticScopeSearch
from tests.test_prepared_source_connectivity import isolation, anyio_backend, public, prepare
from tests.six_tool_support import materialize, TOP
from tests import six_tool_class_support as class_support


def _save(root, name, value):
    (root / name).write_text(json.dumps(value, indent=2) + '\n')


def _named(rows, qualified):
    matches = [row for row in rows if tuple(re.split(r'::|\.', row['qualified_name']))
               == tuple(qualified.split('::'))]
    assert len(matches) == 1, (qualified, matches)
    return matches[0]


async def _call(root, handle, tool='get_tb_file_detail', **arguments):
    return await public(root, tool, dict(design_handle=handle, timeout_sec=30, **arguments))


@pytest.mark.anyio
async def test_native_cross_file_parent_and_nested_scope_identity_across_artifacts(tmp_path):
    handle = await prepare(tmp_path, materialize(tmp_path), 'source', TOP)
    included = await _call(tmp_path, handle, path=str(tmp_path / 'included.svh'))
    parent = await _call(tmp_path, handle, path=str(tmp_path / 'p1.sv'))
    classes = await _call(tmp_path, handle, 'get_tb_class_hierarchy')
    repeat = await _call(tmp_path, handle, path=str(tmp_path / 'included.svh'))
    _save(tmp_path, 'identity_sequence.raw.json', dict(
        included=included, parent=parent, classes=classes, repeat=repeat))
    package = _named(parent['symbols'], 'p1')
    child = _named(included['symbols'], 'p1::Included')
    same = _named(parent['symbols'], 'p1::Same')
    nested = _named(parent['symbols'], 'p1::Same::Nested')
    assert child['scope_id'] == same['scope_id'] == package['declaration_id']
    assert nested['scope_id'] == same['declaration_id']
    assert child['source_file'] == str(tmp_path / 'included.svh') and child['source_line'] == 1
    assert package['source_file'] == str(tmp_path / 'p1.sv') and package['source_line'] == 1
    class_rows = class_support.flatten(classes)
    for declared in (child, same, nested):
        observed = _named(class_rows, declared['qualified_name'])
        assert observed['declaration_id'] == declared['declaration_id']
        assert observed['scope_id'] == declared['scope_id']
    assert included['artifact_identity'] != parent['artifact_identity'] != classes['artifact_identity']
    assert repeat['artifact_identity'] == included['artifact_identity']
    assert repeat['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
    assert _named(repeat['symbols'], 'p1::Included')['scope_id'] == package['declaration_id']


@pytest.mark.anyio
async def test_native_declaration_anchor_does_not_merge_type_specializations(tmp_path):
    handle = await prepare(tmp_path, class_support.materialize(tmp_path), 'source', class_support.TOP)
    classes = await _call(tmp_path, handle, 'get_tb_class_hierarchy')
    details = {}
    for name in ('external.sv', 'classes.sv'):
        details[name] = await _call(tmp_path, handle, path=str(tmp_path / name))
    repeated = await _call(tmp_path, handle, 'get_tb_class_hierarchy')
    _save(tmp_path, 'canonical_and_declaration_identity.raw.json', dict(
        details=details, classes=classes, repeat=repeated))
    rows = class_support.assert_class_oracle(classes, tmp_path, details=details)
    # The frozen class oracle checks native matching for distinct nominal
    # arguments, an actual alias and same-declaration nested specializations.
    inner = [row for row in rows if row['name'] == 'Inner' and row['source_line'] == 15]
    assert len(inner) == class_support.ORACLE['nested_actual_type_count']
    assert len({row['type_id'] for row in inner}) == len(inner)
    assert len({row['declaration_id'] for row in inner}) == 1
    assert repeated['artifact_identity'] == classes['artifact_identity']
    assert repeated['backend_status']['source_graph']['metrics']['actual_build_count'] == 0


@pytest.mark.anyio
async def test_native_parent_alias_and_line_directives_keep_identity_and_reuse(tmp_path):
    log = materialize(tmp_path)
    parent = tmp_path / 'p1.sv'
    header = tmp_path / 'included.svh'
    parent.write_text('`line 700 "display_parent.sv" 0\n' + parent.read_text())
    header.write_text('`line 900 "display_header.svh" 0\n' + header.read_text())
    alias = tmp_path / 'observed_parent.sv'
    alias.symlink_to(parent)
    log.write_text(log.read_text().replace(str(parent), str(alias)))
    handle = await prepare(tmp_path, log, 'source', TOP)
    package_result = await _call(tmp_path, handle, path=str(alias))
    child_result = await _call(tmp_path, handle, path=str(header))
    repeated = await _call(tmp_path, handle, path=str(parent))
    _save(tmp_path, 'alias_line_identity.raw.json', dict(
        parent=package_result, child=child_result, repeat=repeated))
    package = _named(package_result['symbols'], 'p1')
    child = _named(child_result['symbols'], 'p1::Included')
    assert package['source_file'] == str(parent) and package['source_line'] == 2
    assert child['source_file'] == str(header) and child['source_line'] == 2
    assert child['scope_id'] == package['declaration_id']
    assert str(alias) in package_result['aliases']
    assert repeated['artifact_identity'] == package_result['artifact_identity']
    assert repeated['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
    assert _named(repeated['symbols'], 'p1')['declaration_id'] == package['declaration_id']


def _native_parent(root, *, long_parent=False):
    import pyslang

    parent = root / 'parent.sv'
    child = root / 'child.svh'
    parent.write_text('package parent;\n`include "child.svh"\nendpackage\n'
                      'module anchor_top; endmodule\n' + ('// unrelated line\n' * 10000 if long_parent else ''))
    child.write_text('class Child;\nendclass\n')
    manager = pyslang.SourceManager()
    manager.addUserDirectories(str(root))
    tree = pyslang.syntax.SyntaxTree.fromFiles([str(parent)], manager)
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    native_root = compilation.getRoot()
    search = SemanticScopeSearch(purpose='source_catalog', source_files=(str(child),),
        source_capabilities=('file_declarations',), source_aliases=((str(parent), str(parent)),
        (str(child), str(child))), max_members=256)
    return manager, native_root, search, (compilation, tree)


def _catalog(root, label, manager, native_root, search):
    reads = []
    class ObservedManager:
        def __getattr__(self, name):
            return getattr(manager, name)
        def getSourceText(self, buffer):
            reads.append(str(manager.getFullPath(buffer)))
            return manager.getSourceText(buffer)
    projector = SimpleNamespace(source_manager=ObservedManager(), options=ProjectionOptions(
        source_root=root, semantic_search=search, files_total=2, files_projected=2,
        blocking_diagnostic_total=0))
    try:
        raw = json.dumps(collect_source_catalog(projector, native_root), sort_keys=True)
        (root / (label + '.raw.json')).write_text(raw + '\n')
        _save(root, label + '.source_text_reads.json', reads)
        return decode_source_catalog(raw, search), reads
    except BaseException as exc:
        _save(root, label + '.exception.json', dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc()))
        raise


def test_native_out_of_domain_parent_anchor_needs_no_foreign_line_index(tmp_path):
    manager, native_root, search, owners = _native_parent(tmp_path, long_parent=True)
    search = replace(search, max_members=1024)
    small, reads = _catalog(tmp_path, 'child_small', manager, native_root, search)
    parent_search = replace(search, source_files=(str(tmp_path / 'parent.sv'),), max_members=50000)
    parent, parent_reads = _catalog(tmp_path, 'parent_large', manager, native_root, parent_search)
    child = _named(small['files'][str(tmp_path / 'child.svh')]['symbols'], 'parent::Child')
    package = _named(parent['files'][str(tmp_path / 'parent.sv')]['symbols'], 'parent')
    assert child['scope_key'] == package['declaration_key']
    assert not small['limited'] and small['member_visits'] <= search.max_members
    assert small['coordinate_bytes'] < 4096
    assert reads == [str(tmp_path / 'child.svh')]
    assert str(tmp_path / 'parent.sv') in parent_reads
    assert parent['coordinate_bytes'] > 10000 * 64


def test_native_unbound_parent_is_unknown_without_a_name_only_scope_key(tmp_path):
    manager, native_root, search, owners = _native_parent(tmp_path)
    search = replace(search, source_aliases=((str(tmp_path / 'child.svh'), str(tmp_path / 'child.svh')),))
    result, reads = _catalog(tmp_path, 'unbound_parent', manager, native_root, search)
    row = result['files'][str(tmp_path / 'child.svh')]
    child = _named(row['symbols'], 'parent::Child')
    assert child['scope_key'] is None
    assert 'source_scope_identity_unbound' in child['gaps']
    assert not row['complete'] and 'source_scope_identity_unbound' in row['inventory_gaps']
    assert reads == [str(tmp_path / 'child.svh')]


@pytest.mark.parametrize('boundary', ['unbound', 'cancel', 'budget'])
def test_native_anchor_identity_charges_before_native_work_and_never_reads_text(tmp_path, boundary):
    """Native package input with controlled alias/cancel/call-cap boundaries."""
    manager, native_root, search, owners = _native_parent(tmp_path)
    package = next(symbol for symbol in native_root.compilation.getPackages() if str(symbol.name) == 'parent')
    calls = []
    class ObservedManager:
        def __getattr__(self, name):
            def call(*args):
                calls.append(name)
                return getattr(manager, name)(*args)
            return call
    budget = HierarchyQueryBudget(max_calls=1)
    event = threading.Event()
    token = push_cancel_event(event)
    result = None
    error = None
    try:
        if boundary == 'budget':
            budget.call()
        if boundary == 'cancel':
            event.set()
        try:
            result = _declaration_identity(package, ObservedManager(), lambda path: None,
                                           budget.call, _kind_name)
        except BaseException as exc:
            error = dict(type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
    finally:
        pop_cancel_event(token)
    _save(tmp_path, 'anchor_boundary.raw.json', dict(
        boundary=boundary, result=result, error=error, native_calls=calls, budget_calls=budget.call_count))
    assert result is None and 'getSourceText' not in calls
    if boundary == 'unbound':
        assert error is None and calls and budget.call_count == 1
    else:
        assert calls == []
        assert error is not None
        assert error['type'] == ('OperationCancelled' if boundary == 'cancel' else 'HierarchyBudgetExceeded')
