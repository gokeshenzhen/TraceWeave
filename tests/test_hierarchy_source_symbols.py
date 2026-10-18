"""Hand-written static class declarations and graph/lifecycle oracles.

No parser, full hierarchy, native API or frontend is exercised. The seven-class
inventory is explicit: three roots, scoped same-name Base/Child, a Grand and a
distinct Child specialization. No expected graph is built by a reference DFS.
"""
from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError, replace
import json
import subprocess
import sys
import threading

import pytest

from src import hierarchy_source_ir as ir
from src import hierarchy_source_symbols as symbols
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.connectivity_ir import SourceLocation
from src.hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget, HierarchyScope
from src.hierarchy_source_symbols import ClassFact, SourceSymbol, query_class_hierarchy


@pytest.fixture(autouse=True)
def no_native_frontend_or_builder(monkeypatch):
    original, previous = builtins.__import__, sys.getprofile()
    def forbidden(*a, **kw):
        pytest.fail('static class renderer invoked process/native/frontend work')
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        names = [name, *(str(item) for item in (fromlist or ()))]
        assert not any(any(piece in {'pynpi', 'pyslang', 'tb_hierarchy_builder', 'structural_scanner'} or
                                  piece.startswith(('slang_', 'source_graph_worker')) for piece in item.split('.'))
                       for item in names), 'forbidden class renderer import'
        return original(name, globals, locals, fromlist, level)
    def profile(frame, event, arg):
        if event == 'call':
            path = frame.f_code.co_filename.replace('\\', '/')
            assert not any(part in path for part in (
                '/pynpi/', '/src/slang_', '/src/tb_hierarchy_builder.py',
                '/src/structural_scanner.py', '/src/verdi_npi_backend.py',
            )), 'forbidden class renderer execution'
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


def symbol(identity, name, qualified, *, scope='left-package', parameters=(), external=False):
    return SourceSymbol(identity, name, qualified, 'class', SourceLocation('classes.sv', 10, 2),
                        scope_id=scope, declaration_id='decl-' + identity,
                        type_id='type-' + identity, parameterization=parameters, external=external)


def inventory():
    # Provider order intentionally differs from name order and base order.
    return [
        ClassFact(symbol('LBase', 'Base', 'left::Base'), 'none'),
        ClassFact(symbol('LChild', 'Child', 'left::Child'), 'resolved', 'LBase', 'left::Base'),
        ClassFact(symbol('LGrand', 'Grand', 'left::Grand'), 'resolved', 'LChild', 'left::Child'),
        ClassFact(symbol('RBase', 'Base', 'right::Base', scope='right-package'), 'none'),
        ClassFact(symbol('RChild', 'Child', 'right::Child', scope='right-package'), 'resolved', 'RBase', 'right::Base'),
        ClassFact(symbol('Special3', 'Child', 'left::Child#(3)', parameters=(('W', '3'),)), 'resolved', 'LBase', 'left::Base'),
        ClassFact(symbol('Lone', 'Lone', 'z::Lone', scope='z-package'), 'none'),
    ]


class ClassStream:
    def __init__(self, owner):
        self.owner, self.position = owner, 0
        self.complete, self.gaps = False, ()

    def __iter__(self):
        return self

    def __next__(self):
        p, budget = self.owner.provider, self.owner.budget
        budget.call()
        index = self.position
        p.events.append(('next', index))
        p.fire('next', index)
        if index == len(p.facts):
            self.complete, self.gaps = p.inventory_complete, p.inventory_gaps
            p.fire('stop', index)
            raise StopIteration
        self.position += 1
        fact = p.facts[index]
        p.fire('value', index)
        return fact


class ClassOwner:
    def __init__(self, provider, budget):
        self.provider, self.budget = provider, budget

    def __enter__(self):
        self.budget.call()
        p = self.provider
        p.fire('before_open', 0)
        assert not p.active
        p.active = True
        p.opens += 1
        p.events.append(('open',))
        return ClassStream(self)

    def __exit__(self, exc_type, exc, tb):
        p = self.provider
        assert p.active
        p.active = False
        p.closes += 1
        p.events.append(('close',))
        # Cleanup must not spend the expired/cancelled work budget.
        p.fire('close', 0)


class Provider:
    origin, artifact_identity = 'slang', 'class-artifact-A'

    def __init__(self, facts=None, *, complete=True, gaps=(), budget=None, hook=None):
        self.facts = inventory() if facts is None else list(facts)
        self.inventory_complete, self.inventory_gaps = complete, gaps
        self.budget, self.hook = budget, hook
        self.opens = self.closes = 0
        self.active, self.events, self.received_budgets = False, [], []

    def fire(self, stage, index):
        if self.hook is not None:
            self.hook(stage, index)

    def open_classes(self, budget):
        assert isinstance(budget, HierarchyQueryBudget)
        if self.budget is not None and budget is not self.budget:
            raise ValueError('native-like provider requires its original budget')
        self.received_budgets.append(budget)
        return ClassOwner(self, budget)


def assert_closed(provider):
    assert not provider.active
    assert provider.opens == provider.closes


def envelope(result, *, declared=7, total=7, inventory_complete=True, coverage='complete', calls=9, symbols_admitted=None):
    assert result['schema_version'] == 2
    assert result['facts_origin'] == 'slang'
    assert result['artifact_identity'] == 'class-artifact-A'
    assert result['view'] == 'static_class_inheritance'
    assert result['class_inventory_domain'] == 'provider_declarations'
    assert result['class_inventory_count'] == (declared if inventory_complete else None)
    assert result['class_inventory_lower_bound'] == declared
    assert result['class_inventory_complete'] is inventory_complete
    assert result['coverage_status'] == coverage
    assert result['total'] == total
    assert result['query_metrics']['native_call_count'] == calls
    assert result['query_metrics']['symbol_count'] == (declared if symbols_admitted is None else symbols_admitted)
    assert result['query_metrics']['scope_count'] == 0
    assert result['query_metrics']['estimated_fact_bytes'] >= 0
    assert not any(key in result for key in ('component_tree', 'runtime_instances', 'uvm_hierarchy'))


def test_scoped_same_names_and_specializations_keep_distinct_bound_identities():
    p = Provider(list(reversed(inventory())))
    result = query_class_hierarchy(p)
    envelope(result)
    roots = result['roots']
    assert [node['symbol_id'] for node in roots] == ['LBase', 'RBase', 'Lone']
    assert [node['symbol_id'] for node in roots[0]['children']] == ['LChild', 'Special3']
    assert roots[0]['children'][0]['children'][0]['symbol_id'] == 'LGrand'
    assert [node['symbol_id'] for node in roots[1]['children']] == ['RChild']
    specialization = roots[0]['children'][1]
    assert specialization['qualified_name'] == 'left::Child#(3)'
    assert specialization['parameterization'] == {'W': '3'}
    assert specialization['scope_id'] == 'left-package'
    assert specialization['declaration_id'] == 'decl-Special3'
    assert specialization['type_id'] == 'type-Special3'
    assert result['coverage_gaps'] == result['analysis_limits'] == []
    assert result['output_truncated'] is False
    assert_closed(p)


@pytest.mark.parametrize('root', ['LChild', 'left::Child'])
def test_exact_identity_or_qualified_root_selects_one_static_declaration(root):
    p = Provider()
    result = query_class_hierarchy(p, root_class=root)
    envelope(result, total=2)
    assert [node['symbol_id'] for node in result['roots']] == ['LChild']
    assert [node['symbol_id'] for node in result['roots'][0]['children']] == ['LGrand']
    assert_closed(p)


def test_actual_base_id_wins_over_derived_class_namespace_or_bare_base_name():
    facts = inventory()
    facts[1] = replace(facts[1], base_symbol_id='RBase', base_qualified_name='right::Base')
    p = Provider(facts)
    result = query_class_hierarchy(p, root_class='RBase')
    envelope(result, total=4)
    root = result['roots'][0]
    assert root['qualified_name'] == 'right::Base'
    assert [node['symbol_id'] for node in root['children']] == ['LChild', 'RChild']
    assert root['children'][0]['children'][0]['symbol_id'] == 'LGrand'
    assert root['children'][0]['base_symbol_id'] == 'RBase'
    assert_closed(p)


@pytest.mark.parametrize('max_nodes,expected_ids,truncated', [(5000, ['LBase', 'RBase'], False), (1, ['LBase'], True)])
def test_ambiguous_bare_name_has_bounded_candidates_without_guessing_root(max_nodes, expected_ids, truncated):
    p = Provider()
    result = query_class_hierarchy(p, root_class='Base', max_nodes=max_nodes)
    envelope(result, total=0)
    assert result['error'] == 'class_name_ambiguous'
    assert result['roots'] == []
    assert [candidate['symbol_id'] for candidate in result['candidates']] == expected_ids
    assert result['output_truncated'] is truncated
    assert_closed(p)


@pytest.mark.parametrize('complete', [True, False])
def test_missing_root_is_proved_only_by_complete_inventory(complete):
    p = Provider(complete=complete, gaps=() if complete else ('class_inventory_unavailable',))
    result = query_class_hierarchy(p, root_class='not_in_inventory')
    envelope(result, total=0, inventory_complete=complete, coverage='complete' if complete else 'partial')
    assert result['roots'] == []
    assert result['error'] == ('class_not_found' if complete else 'class_unresolved')
    assert_closed(p)


@pytest.mark.parametrize('complete', [True, False])
def test_empty_class_stream_is_not_automatically_a_complete_negative(complete):
    p = Provider([], complete=complete, gaps=() if complete else ('class_relation_unavailable',))
    result = query_class_hierarchy(p, root_class='anything')
    envelope(result, declared=0, total=0, calls=2, inventory_complete=complete,
             coverage='complete' if complete else 'partial')
    assert result['error'] == ('class_not_found' if complete else 'class_unresolved')
    assert_closed(p)


def test_unresolved_base_is_not_repaired_from_an_equal_visible_qualified_name():
    base = inventory()[0]
    broken = ClassFact(symbol('Broken', 'Broken', 'm::Broken'), 'unresolved', base_qualified_name='left::Base', gaps=('base_lookup_failed',))
    p = Provider([base, broken])
    result = query_class_hierarchy(p)
    envelope(result, declared=2, total=2, calls=4, coverage='partial')
    assert result['coverage_gaps'] == ['class_base_unresolved']
    assert [node['symbol_id'] for node in result['roots']] == ['LBase', 'Broken']
    assert all(node['children'] == [] for node in result['roots'])
    assert result['roots'][1]['base_symbol_id'] is None
    assert result['roots'][1]['base_status'] == 'unresolved'
    assert result['roots'][1]['binding_gaps'] == ['base_lookup_failed']
    assert_closed(p)


def test_external_reference_is_not_a_recovered_declaration_or_an_inventory_member():
    child = ClassFact(symbol('Local', 'Child', 'local::Child'), 'resolved', 'External', 'vendor::Base')
    p = Provider([child])
    result = query_class_hierarchy(p)
    envelope(result, declared=1, total=2, calls=3, coverage='partial')
    assert result['coverage_gaps'] == ['class_base_outside_inventory']
    root = result['roots'][0]
    assert root['symbol_id'] == 'External' and root['qualified_name'] == 'vendor::Base'
    assert root['external'] and root['reference_only']
    assert root['source_file'] is root['source_line'] is None
    assert [node['symbol_id'] for node in root['children']] == ['Local']
    assert_closed(p)


@pytest.mark.parametrize('self_cycle,depth,total', [(False, -1, 2), (False, 0, 1), (True, -1, 1), (True, 0, 1)])
def test_cycles_and_self_cycles_are_detected_even_when_display_depth_is_zero(self_cycle, depth, total):
    a = ClassFact(symbol('A', 'A', 'pkg::A'), 'resolved', 'A' if self_cycle else 'B', 'pkg::A' if self_cycle else 'pkg::B')
    facts = [a] if self_cycle else [a, ClassFact(symbol('B', 'B', 'pkg::B'), 'resolved', 'A', 'pkg::A')]
    p = Provider(facts)
    result = query_class_hierarchy(p, depth=depth)
    envelope(result, declared=len(facts), total=total, calls=len(facts) + 2, coverage='partial')
    assert result['coverage_gaps'] == ['class_inheritance_cycle']
    assert result['roots'][0]['symbol_id'] == 'A'
    if depth == -1 and not self_cycle:
        assert [node['symbol_id'] for node in result['roots'][0]['children']] == ['B']
        assert result['roots'][0]['children'][0]['children'] == []
    json.dumps(result)  # No recursive object graph, even for a cycle.
    assert_closed(p)


@pytest.mark.parametrize('kwargs,total', [({'depth': 0}, 1), ({'depth': 1}, 3), ({'max_nodes': 2}, 2)])
def test_display_limit_does_not_reduce_inventory_count_or_analysis_coverage(kwargs, total):
    p = Provider()
    result = query_class_hierarchy(p, root_class='LBase', **kwargs)
    envelope(result, total=total)
    assert result['output_truncated'] is True
    assert result['coverage_gaps'] == result['analysis_limits'] == []
    assert len(p.received_budgets) == 1
    assert_closed(p)


@pytest.mark.parametrize('settings,reason,declared,total,calls', [
    ({'max_symbols': 2}, 'symbol_limit', 2, 2, 4),
    ({'max_calls': 2}, 'native_call_limit', 1, 1, 2),
    ({'max_bytes': 1}, 'fact_byte_limit', 0, 0, 2),
    ({'max_depth': 1}, 'scope_depth_limit', 7, 2, 9),
])
def test_work_limits_are_not_display_truncation_and_clear_inventory_completeness(settings, reason, declared, total, calls):
    p = Provider()
    budget = HierarchyQueryBudget(**settings)
    result = query_class_hierarchy(p, root_class='LBase', budget=budget)
    envelope(result, declared=declared, total=total, calls=calls,
             inventory_complete=False, coverage='partial')
    assert result['analysis_limits'] == [reason]
    assert result['output_truncated'] is False
    assert result['query_metrics']['estimated_fact_bytes'] <= budget.max_bytes
    if declared == 0:
        assert result['error'] == 'class_unresolved'
    assert_closed(p)


def test_provider_budget_is_inherited_explicit_conflict_is_rejected_and_stateless_calls_get_fresh_budgets():
    budget = HierarchyQueryBudget(max_symbols=2)
    p = Provider(budget=budget)
    result = query_class_hierarchy(p, root_class='LBase')
    assert p.received_budgets == [budget]
    assert result['analysis_limits'] == ['symbol_limit']
    with pytest.raises(ValueError, match='original budget'):
        query_class_hierarchy(p, budget=HierarchyQueryBudget())
    assert_closed(p)
    stateless = Provider()
    first = query_class_hierarchy(stateless)
    second = query_class_hierarchy(stateless)
    assert stateless.received_budgets[0] is not stateless.received_budgets[1]
    assert first['query_metrics']['symbol_count'] == second['query_metrics']['symbol_count'] == 7
    assert_closed(stateless)


def test_symbol_budget_is_independent_of_scope_count_and_does_not_increment_on_failure():
    budget = HierarchyQueryBudget(max_scopes=1, max_symbols=1)
    budget.admit(HierarchyScope('scope', 'top', 'top', 'module', None), 0)
    fact = inventory()[0]
    budget.admit_symbol(fact)
    before_bytes = budget.fact_bytes
    with pytest.raises(HierarchyBudgetExceeded) as caught:
        budget.admit_symbol(inventory()[1])
    assert caught.value.reason == 'symbol_limit'
    assert budget.scope_count == budget.symbol_count == 1
    assert budget.fact_bytes == before_bytes
    with pytest.raises(HierarchyBudgetExceeded) as caught:
        HierarchyQueryBudget(max_bytes=1).admit_symbol(fact)
    assert caught.value.reason == 'fact_byte_limit'


@pytest.mark.parametrize('stage', ['value', 'stop', 'close'])
def test_late_provider_deadline_including_cleanup_cannot_publish_complete_inventory(monkeypatch, stage):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    budget = HierarchyQueryBudget(timeout_sec=1)
    def hook(current, index):
        if current == stage:
            clock[0] = 12.0
    p = Provider(hook=hook)
    result = query_class_hierarchy(p, root_class='LBase', budget=budget)
    expected = 0 if stage == 'value' else 7
    envelope(result, declared=expected, total=0, calls=2 if stage == 'value' else 9,
             inventory_complete=False, coverage='partial')
    assert result['analysis_limits'] == ['timeout_sec']
    if stage == 'value':
        assert result['error'] == 'class_unresolved'
    assert_closed(p)


@pytest.mark.parametrize('stage', ['value', 'stop', 'close'])
def test_cooperative_cancellation_propagates_after_native_return_or_cleanup(stage):
    event = threading.Event()
    def hook(current, index):
        if current == stage:
            event.set()
    p = Provider(hook=hook)
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            query_class_hierarchy(p)
    finally:
        pop_cancel_event(token)
    assert_closed(p)


def test_cancellation_during_cpu_graph_work_keeps_provider_closed(monkeypatch):
    p = Provider()
    error = OperationCancelled('cancel after inventory')
    def cancelled():
        assert p.closes == 1 and not p.active
        raise error
    monkeypatch.setattr(symbols, 'check_cancelled', cancelled)
    with pytest.raises(OperationCancelled) as caught:
        query_class_hierarchy(p)
    assert caught.value is error
    assert_closed(p)


@pytest.mark.parametrize('stage', ['before_open', 'next', 'close'])
@pytest.mark.parametrize('error', [TimeoutError('typed provider timeout'), OperationCancelled('provider cancelled'),
                                  CaptureAborted('input_changed'), RuntimeError('provider failed')])
def test_provider_and_typed_errors_are_not_budget_timeouts_or_empty_results(stage, error):
    def hook(current, index):
        if current == stage:
            raise error
    p = Provider(hook=hook)
    with pytest.raises(type(error)) as caught:
        query_class_hierarchy(p)
    assert caught.value is error
    assert_closed(p)


@pytest.mark.parametrize('bad', ['duplicate', 'wrong_fact', 'conflicting_base_name', 'nonbool_complete', 'nonfixed_gap'])
def test_invalid_provider_facts_are_rejected_and_inventory_stream_is_closed(bad):
    p = Provider()
    if bad == 'duplicate':
        p.facts = [inventory()[0], inventory()[0]]
    elif bad == 'wrong_fact':
        p.facts = [inventory()[0].symbol]
    elif bad == 'conflicting_base_name':
        p.facts[1] = replace(p.facts[1], base_qualified_name='wrong::Base')
    elif bad == 'nonbool_complete':
        p.inventory_complete = 1
    else:
        p.inventory_gaps = ('free-form error text',)
    with pytest.raises(ValueError):
        query_class_hierarchy(p)
    assert_closed(p)


def test_rows_are_fresh_primitives_and_never_modify_provider_facts():
    parameters, gaps = [['W', '3']], ['semantic_partial', 'semantic_partial']
    source = symbol('Special', 'Child', 'left::Child#(3)', parameters=parameters)
    source = replace(source, gaps=gaps)
    fact = ClassFact(source, 'none', gaps=['binding_informational'])
    parameters[0][1] = '9'
    gaps.append('later_gap')
    assert source.parameterization == (('W', '3'),)
    assert source.gaps == ('semantic_partial',)
    with pytest.raises(FrozenInstanceError):
        source.qualified_name = 'changed'
    with pytest.raises(FrozenInstanceError):
        source.location.line = 99
    with pytest.raises(FrozenInstanceError):
        fact.base_status = 'resolved'
    p = Provider([fact])
    first = query_class_hierarchy(p)
    first['roots'][0]['parameterization']['W'] = '999'
    first['roots'][0]['gaps'].append('invented')
    first['roots'][0]['binding_gaps'].clear()
    second = query_class_hierarchy(Provider([fact]))
    assert second['roots'][0]['parameterization'] == {'W': '3'}
    assert second['roots'][0]['gaps'] == ['semantic_partial']
    assert second['roots'][0]['binding_gaps'] == ['binding_informational']
    assert (second['roots'][0]['source_file'], second['roots'][0]['source_line'],
            second['roots'][0]['source_column']) == ('classes.sv', 10, 2)
    json.dumps(second)


def test_base_argument_observations_remain_separate_and_immutable():
    parameters = [['W', '5']]
    fact = ClassFact(symbol('Child', 'Child', 'pkg::Child'), 'resolved', 'Base', 'pkg::Base',
                     base_value_parameters=parameters, base_parameter_status='observed')
    parameters[0][1] = '3'
    row = fact.row()
    assert row['parameterization'] == {}
    assert row['base_relation'] == 'declaration' and row['base_value_parameters'] == {'W': '5'}
    row['base_value_parameters']['W'] = '9'
    assert fact.row()['base_value_parameters'] == {'W': '5'}
    for status in ['none', 'unresolved']:
        with pytest.raises(ValueError):
            ClassFact(symbol('Child', 'Child', 'pkg::Child'), status,
                      base_value_parameters=(('W', '5'),), base_parameter_status='observed')


@pytest.mark.parametrize('kwargs', [
    {'root_class': ''}, {'root_class': False}, {'depth': True}, {'depth': -2},
    {'depth': 257}, {'max_nodes': 0}, {'max_nodes': True}, {'max_nodes': 5001},
    {'budget': object()},
])
def test_invalid_query_arguments_do_not_open_provider(kwargs):
    p = Provider()
    with pytest.raises((ValueError, TypeError)):
        query_class_hierarchy(p, **kwargs)
    assert p.received_budgets == [] and p.opens == p.closes == 0


@pytest.mark.parametrize('value', [0, True, 1.5, 100001])
def test_invalid_symbol_limits_are_rejected(value):
    with pytest.raises(ValueError):
        HierarchyQueryBudget(max_symbols=value)


@pytest.mark.parametrize('kwargs', [
    {'kind': 'runtime_instance'}, {'scope_id': ''}, {'type_id': ''}, {'external': 1},
    {'parameterization': [('W', '3'), ('W', '4')]}, {'parameterization': [('W', 3)]},
    {'gaps': ['not a fixed gap']}, {'location': {'file': 'x.sv', 'line': 1}},
])
def test_invalid_symbol_evidence_is_not_normalized_into_a_semantic_fact(kwargs):
    with pytest.raises(ValueError):
        replace(symbol('A', 'A', 'pkg::A'), **kwargs)


@pytest.mark.parametrize('status,base_id,base_name', [
    ('resolved', None, 'pkg::Base'), ('resolved', 'Base', None),
    ('none', 'Base', None), ('none', None, 'pkg::Base'),
    ('unresolved', 'Base', 'pkg::Base'), ('guessed', None, None),
])
def test_class_base_binding_status_cannot_claim_identity_without_evidence(status, base_id, base_name):
    with pytest.raises(ValueError):
        ClassFact(symbol('Child', 'Child', 'pkg::Child'), status, base_id, base_name)
