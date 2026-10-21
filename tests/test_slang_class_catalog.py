"""Finite class capability, binding and corrupted-envelope controls."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS

import pytest

from src.slang_class_catalog import ClassCollector
from src.slang_source_codec import decode_source_catalog
from src.source_graph_contract import SemanticScopeSearch, compare_source_graph_artifact_scopes
from tests.test_slang_source_catalog import payload


def search(files=('/rtl/a.sv',), **kwargs):
    return SemanticScopeSearch(purpose='source_catalog', source_files=files,
        source_capabilities=('class_inheritance', 'file_declarations'), **kwargs)


def class_payload():
    value = payload()
    value.update(capabilities=['class_inheritance', 'file_declarations'], member_visits=20)
    value['classes'] = dict(domain='declarations_and_referenced_specializations',
        complete=True, gaps=[], type_keys=['c' * 64], rows=[dict(key='c' * 64,
        declaration_key='d' * 64, type_key='c' * 64, name='C', qualified_name='pkg::C',
        location=dict(file='/rtl/a.sv', line=8, column=1), scope_key=None,
        declaration_status='declaration', parameterization=[], type_parameter_bindings=[],
        base_status='none', base_key=None, base_qualified_name=None, base_relation=None,
        base_value_parameters=[], base_type_parameter_bindings=[], base_parameter_status='not_observed', gaps=[])])
    return value


def test_class_codec_keeps_declaration_and_canonical_identities_separate():
    value = class_payload()
    assert decode_source_catalog(json.dumps(value), search()) == value
    row = value['classes']['rows'][0]
    row.update(base_status='resolved', base_key='c' * 64, base_qualified_name='pkg::C',
        base_relation='specialization', base_type_parameter_bindings=[['T', 'c' * 64]],
        base_parameter_status='observed')
    assert decode_source_catalog(json.dumps(value), search()) == value


@pytest.mark.parametrize('corruption', ['missing_classes', 'extra_field', 'wrong_domain',
    'unknown_type', 'phantom_base', 'unknown_parameter_type', 'duplicate_type', 'duplicate_class',
    'foreign_location', 'bool_line', 'specialization_uses_declaration_key', 'base_relation_without_base',
    'duplicate_parameter', 'lost_frontier', 'bool_complete', 'missing_declaration'])
def test_class_codec_rejects_corrupted_identity_binding_or_domain(corruption):
    value = class_payload()
    classes = value['classes']
    row = classes['rows'][0]
    if corruption == 'missing_classes': del value['classes']
    elif corruption == 'extra_field': classes['guessed'] = True
    elif corruption == 'wrong_domain': classes['domain'] = 'all_possible_specializations'
    elif corruption == 'unknown_type': row['type_key'] = 'e' * 64
    elif corruption == 'phantom_base':
        row.update(base_status='resolved', base_key='e' * 64,
                   base_qualified_name='outside::Base', base_relation='specialization')
    elif corruption == 'unknown_parameter_type': row['type_parameter_bindings'] = [['T', 'e' * 64]]
    elif corruption == 'duplicate_type': classes['type_keys'].append('c' * 64)
    elif corruption == 'duplicate_class': classes['rows'].append(deepcopy(row))
    elif corruption == 'foreign_location': row['location']['file'] = '/outside/a.sv'
    elif corruption == 'bool_line': row['location']['line'] = True
    elif corruption == 'specialization_uses_declaration_key':
        row.update(declaration_status='specialization', key='d' * 64)
    elif corruption == 'base_relation_without_base': row['base_relation'] = 'specialization'
    elif corruption == 'duplicate_parameter': row['type_parameter_bindings'] = [['T', 'c' * 64]] * 2
    elif corruption == 'lost_frontier': value.update(limited=True, stop_reason='source_catalog_visit_limit')
    elif corruption == 'bool_complete': classes['complete'] = 1
    elif corruption == 'missing_declaration': row['declaration_key'] = None
    with pytest.raises(ValueError):
        decode_source_catalog(json.dumps(value), search())


def test_catalog_class_superset_reuses_only_requested_files_capabilities_and_exact_other_inputs():
    from tests.test_source_graph_runtime import _request
    base = _request().artifact_identity.scope
    broad = replace(base, semantic_search=search(('/rtl/a.sv', '/rtl/b.sv')))
    narrow_search = SemanticScopeSearch(purpose='source_catalog', source_files=('/rtl/a.sv',),
                                      source_capabilities=('file_declarations',))
    narrow = replace(base, semantic_search=narrow_search)
    assert compare_source_graph_artifact_scopes(broad, narrow).value == 'superset'
    assert compare_source_graph_artifact_scopes(narrow, broad).value == 'subset'
    assert compare_source_graph_artifact_scopes(broad, replace(base,
        semantic_search=replace(narrow_search, source_max_depth=127))).value == 'superset'
    for changed in (replace(narrow_search, source_aliases=(('/alias/a.sv', '/rtl/a.sv'),)),
                    replace(narrow_search, source_files=('/rtl/c.sv',)),
                    SemanticScopeSearch(roots=(base.top,), depth=2, purpose='hierarchy')):
        assert compare_source_graph_artifact_scopes(broad, replace(base, semantic_search=changed)).value == 'unproven'
    with pytest.raises(ValueError):
        decode_source_catalog(json.dumps(payload()), search())


class NativeType:
    """Same source/name/display/width, distinct nominal matching identities."""
    kind = NS(name='ClassType')
    isError = False
    genericClass = None
    baseClass = None
    syntax = NS(extendsClause=None)
    name = 'Nested'
    lexicalPath = 'p::Outer::Nested'
    def __init__(self, token, members=()):
        self.token, self.members = token, members
        self.canonicalType = self
    def isMatching(self, other): return self.token == other.token
    def __iter__(self): return iter(self.members)
    def __str__(self): return 'identical display'
    @property
    def bitWidth(self): raise AssertionError('width cannot define type identity')


def collector(*, max_depth=20, budget=10000):
    calls = [0]
    class Stop(Exception): pass
    def charge():
        calls[0] += 1
        if calls[0] > budget: raise Stop('visits')
    def depth_limit(): raise Stop('depth')
    where = dict(file='/rtl/a.sv', line=8, column=1)
    value = ClassCollector(charge=charge, source=lambda _: where,
        identity=lambda *args: hashlib.sha256(b'one declaration').hexdigest(),
        enqueue=lambda *args: None, kind_name=lambda x: x.kind.name,
        files={'/rtl/a.sv'}, max_depth=max_depth, depth_limit=depth_limit,
        parameter_limit=lambda: depth_limit(), reserve=lambda _: None)
    return value, Stop


def test_nominal_nested_types_do_not_collapse_by_declaration_display_or_width_and_aliases_match():
    value, _ = collector()
    first, second, alias = NativeType('A'), NativeType('B'), NativeType('A')
    a, b, same = map(value.observe_type, (first, second, alias))
    assert a != b and same == a
    assert len(value.rows) == 2
    assert len({row['declaration_key'] for row in value.rows.values()}) == 1
    assert len({row['type_key'] for row in value.rows.values()}) == 2


def test_class_depth_limit_preserves_unresolved_incomplete_base_binding():
    value, Stop = collector(max_depth=1)
    first, second, third = NativeType('A'), NativeType('B'), NativeType('C')
    first.syntax = second.syntax = NS(extendsClause=object())
    first.baseClass, second.baseClass = second, third
    with pytest.raises(Stop, match='depth'): value.observe_type(first)
    assert value.rows
    assert all(row['base_status'] == 'unresolved' and
               'source_class_binding_incomplete' in row['gaps'] for row in value.rows.values())


def test_parameter_inventory_stops_at_codec_bound_with_explicit_incomplete_binding():
    value, Stop = collector()
    def parameter_limit(): raise Stop('parameters')
    value.parameter_limit = parameter_limit
    members = [NS(kind=NS(name='Parameter'), isPortParam=True, name=f'N{index}', value='3')
               for index in range(129)]
    with pytest.raises(Stop, match='parameters'):
        value.observe_type(NativeType('large', members))
    row = next(iter(value.rows.values()))
    assert 'source_class_binding_incomplete' in row['gaps']
    assert len(row['parameterization']) <= 128
    assert not value.payload(limited=True, diagnostics=False, context_incomplete=False)['complete']


def test_parameter_display_bytes_are_charged_before_retaining_each_pair():
    value, Stop = collector()
    retained = [0]
    def reserve(size):
        retained[0] += size
        if retained[0] > 5000: raise Stop('bytes')
    value.reserve = reserve
    member = NS(kind=NS(name='Parameter'), isPortParam=True, name='N', value='1' * 2000)
    with pytest.raises(Stop, match='bytes'):
        value.observe_type(NativeType('large_value', (member,)))
    assert all('source_class_binding_incomplete' in row['gaps'] for row in value.rows.values())


def test_file_declaration_artifact_cannot_supply_a_class_inventory():
    from src.slang_source_provider import SlangSourceProvider
    from src.hierarchy_source_ir import HierarchyQueryBudget
    from tests.test_slang_source_catalog import _catalog_artifact_fixture
    request, ir, receipt = _catalog_artifact_fixture()
    entry = NS(build_key=NS(digest='a' * 64), artifact_scope_receipt=receipt, ir=ir)
    context = NS(selected_top=receipt.scope.top)
    with pytest.raises(ValueError, match='capability'):
        SlangSourceProvider(entry, context, HierarchyQueryBudget(), ('/rtl/a.sv',),
                            capabilities=('class_inheritance', 'file_declarations'))


def test_unresolved_type_argument_is_never_relabelled_as_a_value_parameter():
    value, _ = collector()
    error = NS(isError=True)
    error.canonicalType = error
    parameter = NS(kind=NS(name='TypeParameter'), isPortParam=True, name='T',
                   targetType=NS(type=error))
    base = NativeType('base', (parameter,))
    child = NativeType('child')
    child.syntax, child.baseClass = NS(extendsClause=object()), base
    key = value.observe_type(child)
    row = value.rows[key]
    assert row['base_status'] == 'resolved'
    assert row['base_value_parameters'] == row['base_type_parameter_bindings'] == []
    assert row['base_parameter_status'] == 'partial'
    assert 'source_class_type_binding_unresolved' in row['gaps']
