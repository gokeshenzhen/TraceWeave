"""Primitive corruption and artifact capability controls; no frontend needed."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from src.slang_source_codec import decode_source_catalog
from src.source_graph_contract import SemanticScopeSearch


def search(**kwargs):
    return SemanticScopeSearch(purpose='source_catalog', source_files=('/rtl/a.sv',),
                               source_capabilities=('file_declarations',), **kwargs)


def payload():
    return dict(version=1, capabilities=['file_declarations'], source_files=['/rtl/a.sv'],
        declaration_domain='compilation_declarations_and_elaborated_scopes',
        usage_domain='resolved_types_imports_and_expression_references',
        member_visits=1, scope_visits=1, limited=False, stop_reason=None,
        limits=dict(members=4096, scopes=1024, depth=128),
        files={'/rtl/a.sv': dict(symbols=[dict(key='a' * 64, name='A', qualified_name='A',
            kind='module', location=dict(file='/rtl/a.sv', line=7, column=1), scope_key=None,
            declaration_key='a' * 64, type_key=None, parameterization=[], external=False,
            gaps=[], declaration_status='declaration')], inventory_gaps=[], import_gaps=[],
            usage_gaps=[], gaps=[], complete=True, has_uvm_import=False, uses_uvm=False)})


def test_catalog_codec_retains_semantic_location_and_separate_usage_coverage():
    value = payload()
    row = value['files']['/rtl/a.sv']
    row.update(uses_uvm=None, usage_gaps=['source_type_usage_domain_uncovered'],
               gaps=['source_type_usage_domain_uncovered'])
    result = decode_source_catalog(json.dumps(value), search())
    assert result == value and result['files']['/rtl/a.sv']['complete'] is True
    assert result['files']['/rtl/a.sv']['symbols'][0]['location']['line'] == 7


@pytest.mark.parametrize('corruption', [
    'bool_version', 'bool_visits', 'bool_scope', 'extra_field', 'wrong_capability',
    'wrong_files', 'wrong_domain', 'wrong_limits', 'bool_complete', 'false_with_gap',
    'null_without_gap', 'duplicate_symbol', 'foreign_location', 'bool_line',
    'bad_key', 'lost_frontier', 'missing_field', 'duplicate_json_key',
])
def test_catalog_codec_rejects_corruption_before_any_fact_is_published(corruption):
    value = payload()
    row = value['files']['/rtl/a.sv']
    if corruption == 'bool_version': value['version'] = True
    elif corruption == 'bool_visits': value['member_visits'] = True
    elif corruption == 'bool_scope': value['scope_visits'] = True
    elif corruption == 'extra_field': value['guessed'] = True
    elif corruption == 'wrong_capability': value['capabilities'] = ['connectivity']
    elif corruption == 'wrong_files': value['source_files'] = ['/other/a.sv']
    elif corruption == 'wrong_domain': value['usage_domain'] = 'all_text'
    elif corruption == 'wrong_limits': value['limits']['depth'] = 256
    elif corruption == 'bool_complete': row['complete'] = 1
    elif corruption == 'false_with_gap':
        row['usage_gaps'] = row['gaps'] = ['source_type_usage_domain_uncovered']
    elif corruption == 'null_without_gap': row['uses_uvm'] = None
    elif corruption == 'duplicate_symbol':
        value['member_visits'] = 2
        row['symbols'].append(deepcopy(row['symbols'][0]))
    elif corruption == 'foreign_location': row['symbols'][0]['location']['file'] = '/other/a.sv'
    elif corruption == 'bool_line': row['symbols'][0]['location']['line'] = True
    elif corruption == 'bad_key': row['symbols'][0]['declaration_key'] = 'bare-name'
    elif corruption == 'lost_frontier':
        value.update(limited=True, stop_reason='source_catalog_visit_limit')
    elif corruption == 'missing_field': del row['inventory_gaps']
    raw = json.dumps(value)
    if corruption == 'duplicate_json_key': raw = raw.replace('"version": 1', '"version": 1, "version": 1')
    with pytest.raises(ValueError): decode_source_catalog(raw, search())


def test_catalog_scope_identity_and_exact_isolation():
    from tests.test_source_graph_runtime import _request
    from src.source_graph_contract import compare_source_graph_artifact_scopes, compute_source_graph_artifact_key
    request = _request()
    identity = replace(request.artifact_identity,
                       scope=replace(request.artifact_identity.scope, semantic_search=search()))
    key = compute_source_graph_artifact_key(identity).digest
    assert SemanticScopeSearch.from_dict(search().to_dict()) == search()
    for changed in (search(source_max_depth=127), search(source_max_scopes=100),
                    search(source_aliases=(('/alias/a.sv', '/rtl/a.sv'),))):
        other = replace(identity, scope=replace(identity.scope, semantic_search=changed))
        assert compute_source_graph_artifact_key(other).digest != key
        assert compare_source_graph_artifact_scopes(identity.scope, other.scope).value == 'unproven'
    connection = replace(identity.scope, semantic_search=SemanticScopeSearch())
    assert compare_source_graph_artifact_scopes(identity.scope, connection).value == 'unproven'
    assert compare_source_graph_artifact_scopes(connection, identity.scope).value == 'unproven'


@pytest.mark.parametrize('kwargs', [dict(source_version=True), dict(source_version=2),
    dict(source_max_scopes=True), dict(source_max_depth=257),
    dict(source_aliases=(('same', '/rtl/a.sv'), ('same', '/rtl/b.sv')))])
def test_invalid_catalog_inputs_rejected_before_worker(kwargs):
    with pytest.raises(ValueError): search(**kwargs)
