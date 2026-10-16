"""Independent hand-written topology/control tests, without a native frontend.

The provider below is a text-free semantic oracle, not an SDK mock proving NPI.
Each lookup, iterator open, and next attempt charges one provider call. Expected
cardinalities come from the ten listed scopes; no reference traversal mirrors
the service. Cleanup itself spends no query budget.
"""
from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError, replace
import importlib
import re
import sys
import threading

import pytest

from src import hierarchy_source_ir as ir
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.connectivity_ir import SourceLocation
from src.hierarchy_source_ir import (
    HierarchyBudgetExceeded, HierarchyQueryBudget, HierarchyScope,
    ScopeLookup, semantic_fact_id,
)
from src.hierarchy_source_service import HierarchySourceService


ROOT = 'top'
A = 'top.u_a'
GEN = 'top.g[0]'
U = 'top.g[0].u'
DEEP = 'top.g[0].u.deep'
ARRAY = 'top.lanes'
ELEMENT = 'top.lanes[0]'
ESCAPED = 'top.\\escaped.dot '
BUS = 'top.bus'
MON = 'top.bus.monitor'
ALL_PATHS = (ROOT, A, GEN, U, DEEP, ARRAY, ELEMENT, ESCAPED, BUS, MON)


@pytest.fixture(autouse=True)
def no_frontend_native_or_full_build(monkeypatch):
    """Queries must remain pure provider work even in a warm test interpreter."""
    original_import = builtins.__import__
    original_dynamic = importlib.import_module
    previous_profile = sys.getprofile()

    def blocked(name):
        pieces = name.split('.')
        return any(part in {'pynpi', 'pyslang', 'tb_hierarchy_builder',
                            'structural_scanner', 'fsdb_parser'} or
                   part.startswith(('slang_', 'source_graph_worker', 'structural_semantic_worker'))
                   for part in pieces)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert not blocked(name), f'forbidden dependency: {name}'
        assert not any(blocked(str(item)) for item in (fromlist or ())), 'forbidden from-import'
        return original_import(name, globals, locals, fromlist, level)

    def guarded_dynamic(name, package=None):
        assert not blocked(name), f'forbidden dynamic import: {name}'
        return original_dynamic(name, package)

    def profile(frame, event, arg):
        if event == 'call':
            filename = frame.f_code.co_filename.replace('\\', '/')
            assert not any(token in filename for token in (
                '/pynpi/', '/tb_hierarchy_builder.py', '/structural_scanner.py',
                '/slang_', '/source_graph_worker.py', '/structural_semantic_worker.py',
            )), f'forbidden execution: {filename}'

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(importlib, 'import_module', guarded_dynamic)
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous_profile)


def facts(*, origin='slang', identity='design-A'):
    def scope(path, name, kind, parent, definition=None, width=None, line=1):
        definition_key = definition + ':' + str(width) if definition else None
        return HierarchyScope(
            semantic_fact_id(origin, identity, 'scope', path), path, name, kind, parent,
            definition_id=semantic_fact_id(origin, identity, 'definition', definition_key) if definition else None,
            definition_name=definition,
            location=SourceLocation('instances.sv', line),
            definition_location=SourceLocation('definitions.sv', 70) if definition else None,
            parameterization=(('WIDTH', str(width)),) if width is not None else (),
        )
    # ELEMENT really belongs to ROOT, not ARRAY. ESCAPED contains a literal dot.
    return {
        ROOT: scope(ROOT, 'top', 'module', None, 'top', line=2),
        A: scope(A, 'u_a', 'module', ROOT, 'leaf', 8, 10),
        GEN: scope(GEN, 'g[0]', 'generate', ROOT, line=11),
        U: scope(U, 'u', 'module', GEN, 'leaf', 4, 12),
        DEEP: scope(DEEP, 'deep', 'module', U, 'cell', line=13),
        ARRAY: scope(ARRAY, 'lanes', 'instance_array', ROOT, line=14),
        ELEMENT: scope(ELEMENT, 'lanes[0]', 'module', ROOT, 'leaf', 2, 15),
        ESCAPED: scope(ESCAPED, '\\escaped.dot ', 'module', ROOT, 'leaf', 16, 16),
        BUS: scope(BUS, 'bus', 'interface', ROOT, 'bus_if', line=17),
        MON: scope(MON, 'monitor', 'program', BUS, 'monitor', line=18),
    }


class Stream:
    def __init__(self, provider, parent, budget):
        self.provider, self.parent, self.budget = provider, parent, budget
        self.position = 0
        # Completeness is deliberately unavailable until StopIteration.
        self.complete, self.gaps = False, ()

    def __iter__(self):
        return self

    def __next__(self):
        self.budget.call()
        p, path, index = self.provider, self.parent.path, self.position
        p.events.append(('next', path, index))
        p.fire('next', path, index)
        children = p.children.get(path, ())
        if index == len(children):
            self.complete = p.stream_complete.get(path, True)
            self.gaps = p.stream_gaps.get(path, ())
            p.fire('stop', path, index)
            raise StopIteration
        item = children[index]
        child = p.facts[item] if isinstance(item, str) else item
        self.position += 1
        p.observed.append(child.path)
        p.fire('value', path, index)
        return child


class Owner:
    def __init__(self, provider, parent, budget):
        self.provider, self.parent, self.budget = provider, parent, budget

    def __enter__(self):
        self.budget.call()
        p, path = self.provider, self.parent.path
        p.fire('before_open', path, 0)
        assert path not in p.active, 'fixture should have no simultaneously repeated scope'
        p.active.add(path)
        p.opened.append(path)
        p.events.append(('open', path))
        return Stream(p, self.parent, self.budget)

    def __exit__(self, exc_type, exc, tb):
        p, path = self.provider, self.parent.path
        assert path in p.active
        p.active.remove(path)
        p.closed.append(path)
        p.events.append(('close', path))
        # Never call budget.call/checkpoint here: exhausted/cancelled work must
        # still release every ancestor's resource.
        p.fire('close', path, 0)


class Provider:
    top = ROOT

    def __init__(self, *, origin='slang', identity='design-A', hook=None):
        self.origin, self.artifact_identity = origin, identity
        self.facts = facts(origin=origin, identity=identity)
        self.children = {
            ROOT: (A, GEN, ARRAY, ELEMENT, ESCAPED, BUS),
            GEN: (U,), U: (DEEP,), BUS: (MON,),
        }
        self.lookup_overrides = {}
        self.stream_complete, self.stream_gaps = {}, {}
        self.events, self.opened, self.closed, self.observed = [], [], [], []
        self.active = set()
        self.hook = hook

    def fire(self, stage, path, index):
        if self.hook is not None:
            self.hook(stage, path, index)

    def lookup_scope(self, path, budget):
        budget.call()
        self.events.append(('lookup', path))
        self.fire('lookup', path, 0)
        return self.lookup_overrides.get(path, ScopeLookup(self.facts.get(path), True))

    def open_children(self, scope, budget):
        return Owner(self, scope, budget)


def assert_closed(provider):
    assert not provider.active
    assert sorted(provider.closed) == sorted(provider.opened)
    assert len(provider.closed) == len(set(provider.closed))


def query(provider, operation='subtree', **kwargs):
    service = HierarchySourceService(provider)
    return (service.subtree(**kwargs) if operation == 'subtree'
            else service.find_instances(**kwargs))


def assert_envelope(result, *, complete, count, scopes, calls, truncated=False):
    assert result['schema_version'] == 2
    assert result['facts_origin'] == 'slang'
    assert result['artifact_identity'] == 'design-A'
    assert result['coverage_status'] == ('complete' if complete else 'partial')
    assert result['count_status'] == ('exact' if complete else 'lower_bound')
    assert result['count_lower_bound'] == count
    assert result['output_truncated'] is truncated
    assert result['truncated'] is truncated
    assert result['query_metrics']['scope_count'] == scopes
    assert result['query_metrics']['native_call_count'] == calls
    assert result['query_metrics']['estimated_fact_bytes'] >= 0
    assert result['query_metrics']['query_ms'] >= 0


@pytest.mark.parametrize('path', [ROOT, GEN, ARRAY, ELEMENT, ESCAPED])
def test_exact_path_is_one_lookup_without_opening_children(path):
    provider = Provider()
    result = query(provider, 'find', path=path)
    assert result['hits'] == [provider.facts[path].row()]
    assert result['total'] == 1
    assert result['count_domain'] == 'exact_path'
    assert result['count_mode'] == 'bounded'
    assert_envelope(result, complete=True, count=1, scopes=1, calls=1)
    assert provider.events == [('lookup', path)]
    assert result['coverage_gaps'] == result['analysis_limits'] == []
    assert_closed(provider)


def test_generate_array_parent_escaped_name_and_parameter_definition_truth():
    provider = Provider()
    generated = query(provider, 'find', path=GEN)['hits'][0]
    assert generated['kind'] == 'generate'
    assert generated['module'] is generated['definition_id'] is None
    element = query(provider, 'find', path=ELEMENT)['hits'][0]
    assert element['parent'] == ROOT
    escaped = query(provider, 'find', path=ESCAPED)['hits'][0]
    assert escaped['path'] == ESCAPED and escaped['parent'] == ROOT
    assert escaped['inst'] == '\\escaped.dot '
    a, u = provider.facts[A].row(), provider.facts[U].row()
    assert a['module'] == u['module'] == 'leaf'
    assert a['definition_id'] != u['definition_id']
    assert a['parameterization'] == {'WIDTH': '8'}
    assert u['parameterization'] == {'WIDTH': '4'}
    assert (a['source_file'], a['source_line']) == ('instances.sv', 10)
    assert (a['definition_file'], a['definition_line']) == ('definitions.sv', 70)


def test_default_bounded_depth_has_nullable_totals_and_one_peek_per_frontier():
    provider = Provider()
    result = query(provider)
    assert_envelope(result, complete=False, count=8, scopes=9, calls=21, truncated=True)
    assert result['total_descendants'] is None
    assert result['coverage_gaps'] == ['descendants_not_enumerated']
    assert result['analysis_limits'] == []
    root = result['node']
    assert root['path'] == ROOT
    assert [row['path'] for row in root['children']] == [A, GEN, ARRAY, ELEMENT, ESCAPED, BUS]
    assert root['child_count'] == root['child_count_lower_bound'] == 6
    assert root['truncated'] is False
    for index in (1, 5):
        frontier = root['children'][index]
        assert frontier['children'] == []
        assert frontier['child_count'] is None
        assert frontier['child_count_lower_bound'] == 1
        assert frontier['truncated'] is True
    assert provider.observed == [A, GEN, U, ARRAY, ELEMENT, ESCAPED, BUS, MON]
    assert DEEP not in provider.opened and U not in provider.opened and MON not in provider.opened
    assert_closed(provider)


@pytest.mark.parametrize('kwargs', [{'depth': 0}, {'depth': -1, 'max_nodes': 1}])
def test_root_included_in_display_cap_and_only_one_omitted_edge_is_observed(kwargs):
    provider = Provider()
    result = query(provider, **kwargs)
    assert_envelope(result, complete=False, count=1, scopes=2, calls=3, truncated=True)
    assert result['node']['children'] == []
    assert result['node']['child_count'] is None
    assert result['node']['child_count_lower_bound'] == 1
    assert result['total_descendants'] is None
    assert provider.observed == [A]
    assert provider.opened == provider.closed == [ROOT]


def test_output_cap_observes_one_extra_scope_and_closes_active_ancestors():
    provider = Provider()
    result = query(provider, depth=-1, max_nodes=3)
    assert_envelope(result, complete=False, count=3, scopes=4, calls=8, truncated=True)
    assert [row['path'] for row in result['node']['children']] == [A, GEN]
    assert provider.observed == [A, GEN, U]
    assert provider.opened == [ROOT, A, GEN]
    assert result['total_descendants'] is None
    assert_closed(provider)


@pytest.mark.parametrize('kwargs', [{'depth': 0}, {'depth': 1}, {'depth': -1, 'max_nodes': 3}])
def test_exact_count_traverses_undisplayed_nodes_without_claiming_partial_analysis(kwargs):
    provider = Provider()
    result = query(provider, count_mode='exact', **kwargs)
    assert_envelope(result, complete=True, count=9, scopes=10, calls=30, truncated=True)
    assert result['total_descendants'] == 9
    assert result['node']['child_count'] == 6
    assert result['coverage_gaps'] == result['analysis_limits'] == []
    assert set(provider.opened) == set(ALL_PATHS)
    assert_closed(provider)


def test_full_subtree_matches_hand_written_multibranch_topology():
    provider = Provider()
    result = query(provider, depth=-1)
    assert_envelope(result, complete=True, count=9, scopes=10, calls=30)
    assert result['total_descendants'] == 9
    assert result['count_domain'] == 'descendants_of_root'
    children = result['node']['children']
    assert [row['path'] for row in children] == [A, GEN, ARRAY, ELEMENT, ESCAPED, BUS]
    assert children[1]['children'][0]['path'] == U
    assert children[1]['children'][0]['children'][0]['path'] == DEEP
    assert children[5]['children'][0]['path'] == MON
    assert [row['child_count'] for row in children] == [0, 1, 0, 0, 0, 1]
    assert_closed(provider)


def test_module_bounded_search_stops_at_first_undisplayed_matching_fact():
    provider = Provider()
    result = query(provider, 'find', module='leaf', limit=2)
    assert_envelope(result, complete=False, count=3, scopes=7, calls=18, truncated=True)
    assert [row['path'] for row in result['hits']] == [A, U]
    assert result['count_domain'] == 'matching_instances_under_top'
    assert result['total'] is None
    assert result['coverage_gaps'] == ['matching_instances_not_enumerated']
    assert result['analysis_limits'] == []
    assert provider.observed == [A, GEN, U, DEEP, ARRAY, ELEMENT]
    assert ELEMENT not in provider.opened and ESCAPED not in provider.observed
    assert_closed(provider)


def test_module_exact_count_and_interface_program_matches():
    provider = Provider()
    result = query(provider, 'find', module='leaf', limit=2, count_mode='exact')
    assert_envelope(result, complete=True, count=4, scopes=10, calls=30, truncated=True)
    assert [row['path'] for row in result['hits']] == [A, U]
    assert result['total'] == 4
    assert result['coverage_gaps'] == result['analysis_limits'] == []
    for module, path, kind in [('bus_if', BUS, 'interface'), ('monitor', MON, 'program')]:
        p = Provider()
        match = query(p, 'find', module=module)
        assert [(row['path'], row['kind']) for row in match['hits']] == [(path, kind)]
        assert match['total'] == 1
        assert_closed(p)
    assert_closed(provider)


@pytest.mark.parametrize('operation', ['subtree', 'find'])
@pytest.mark.parametrize('complete', [True, False])
def test_complete_missing_path_and_unresolved_path_have_distinct_negative_evidence(operation, complete):
    provider = Provider()
    provider.lookup_overrides['missing'] = ScopeLookup(None, complete, () if complete else ('lookup_unavailable',))
    kwargs = {'root': 'missing'} if operation == 'subtree' else {'path': 'missing'}
    result = query(provider, operation, **kwargs)
    assert_envelope(result, complete=complete, count=0, scopes=0, calls=1)
    assert provider.opened == []
    if operation == 'subtree':
        assert result['node'] is None
        assert result['error'] == ('instance_not_found' if complete else 'instance_unresolved')
        assert result['total_descendants'] == (0 if complete else None)
    else:
        assert result['hits'] == []
        assert result['total'] == (0 if complete else None)


@pytest.mark.parametrize('complete', [True, False])
@pytest.mark.parametrize('operation', ['subtree', 'find'])
def test_absent_iterator_cannot_become_a_complete_empty_subtree(complete, operation):
    provider = Provider()
    provider.children = {ROOT: ()}
    provider.stream_complete[ROOT] = complete
    provider.stream_gaps[ROOT] = () if complete else ('native_children_unavailable',)
    result = query(provider, operation, **({} if operation == 'subtree' else {'module': 'leaf'}))
    assert_envelope(result, complete=complete, count=0, scopes=1, calls=3)
    if operation == 'subtree':
        assert result['node']['child_count'] == (0 if complete else None)
        assert result['total_descendants'] == (0 if complete else None)
    else:
        assert result['hits'] == []
        assert result['total'] == (0 if complete else None)
    assert result['coverage_gaps'] == ([] if complete else ['native_children_unavailable'])
    assert_closed(provider)


@pytest.mark.parametrize('module', ['does_not_exist', 'lea.*'])
def test_complete_module_negative_requires_traversing_all_scopes(module):
    provider = Provider()
    result = query(provider, 'find', module=module, limit=1)
    assert_envelope(result, complete=True, count=0, scopes=10, calls=30)
    assert result['hits'] == [] and result['total'] == 0
    assert_closed(provider)


def test_positive_partial_lookup_keeps_fact_but_not_an_exact_total():
    provider = Provider()
    provider.lookup_overrides[A] = ScopeLookup(provider.facts[A], False, ('lookup_partial',))
    result = query(provider, 'find', path=A)
    assert_envelope(result, complete=False, count=1, scopes=1, calls=1)
    assert result['hits'] == [provider.facts[A].row()]
    assert result['total'] is None
    assert result['coverage_gaps'] == ['lookup_partial']


@pytest.mark.parametrize('settings,reason,count,scopes,calls', [
    ({'max_scopes': 2}, 'scope_limit', 1, 2, 6),
    ({'max_calls': 2}, 'native_call_limit', 0, 1, 2),
    ({'max_depth': 1}, 'scope_depth_limit', 2, 3, 8),
    ({'max_bytes': 1}, 'fact_byte_limit', 0, 0, 1),
])
def test_analysis_budgets_are_distinct_from_display_truncation(settings, reason, count, scopes, calls):
    provider = Provider()
    budget = HierarchyQueryBudget(**settings)
    result = query(provider, depth=-1, budget=budget)
    assert_envelope(result, complete=False, count=count, scopes=scopes, calls=calls)
    assert result['analysis_limits'] == [reason]
    assert result['total_descendants'] is None
    assert result['query_metrics']['estimated_fact_bytes'] <= budget.max_bytes
    assert_closed(provider)


@pytest.mark.parametrize('settings,reason,matches,scopes,calls', [
    ({'max_scopes': 2}, 'scope_limit', 1, 2, 6),
    ({'max_calls': 2}, 'native_call_limit', 0, 1, 2),
    ({'max_depth': 1}, 'scope_depth_limit', 1, 3, 8),
    ({'max_bytes': 1}, 'fact_byte_limit', 0, 0, 1),
])
def test_module_search_charges_nonmatching_scopes_and_preserves_only_admitted_hits(settings, reason, matches, scopes, calls):
    provider = Provider()
    result = query(provider, 'find', module='leaf', budget=HierarchyQueryBudget(**settings))
    assert_envelope(result, complete=False, count=matches, scopes=scopes, calls=calls)
    assert result['analysis_limits'] == [reason]
    assert result['total'] is None
    assert [row['path'] for row in result['hits']] == ([A] if matches else [])
    assert_closed(provider)


def test_exact_count_does_not_escape_work_budget_after_display_is_full():
    provider = Provider()
    result = query(provider, depth=0, count_mode='exact', budget=HierarchyQueryBudget(max_scopes=2))
    assert_envelope(result, complete=False, count=1, scopes=2, calls=6, truncated=True)
    assert result['node']['children'] == []
    assert result['analysis_limits'] == ['scope_limit']
    assert result['total_descendants'] is None
    assert_closed(provider)


@pytest.mark.parametrize('method', ['call', 'admit'])
def test_budget_admission_and_call_limits_do_not_increment_after_rejection(method):
    budget = HierarchyQueryBudget(max_calls=1, max_scopes=1)
    if method == 'call':
        budget.call()
        with pytest.raises(HierarchyBudgetExceeded, match='native_call_limit'):
            budget.call()
        assert budget.call_count == 1
    else:
        budget.admit(facts()[ROOT], 0)
        before = budget.fact_bytes
        with pytest.raises(HierarchyBudgetExceeded, match='scope_limit'):
            budget.admit(facts()[A], 1)
        assert budget.scope_count == 1 and budget.fact_bytes == before


@pytest.mark.parametrize('stage', ['stop', 'close'])
def test_late_native_deadline_including_stopiteration_and_cleanup_is_not_success(monkeypatch, stage):
    clock = [100.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    budget = HierarchyQueryBudget(timeout_sec=0.5)
    def hook(current, path, index):
        if current == stage and path == ROOT:
            clock[0] = 101.0
    provider = Provider(hook=hook)
    result = query(provider, depth=-1, budget=budget)
    assert result['coverage_status'] == 'partial'
    assert result['analysis_limits'] == ['timeout_sec']
    assert result['total_descendants'] is None
    assert result['count_lower_bound'] == 9
    assert result['query_metrics']['scope_count'] == 10
    assert result['query_metrics']['native_call_count'] == 30
    assert_closed(provider)


@pytest.mark.parametrize('stage', ['lookup', 'value'])
def test_late_lookup_or_returned_child_is_rejected_before_admission(monkeypatch, stage):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    budget = HierarchyQueryBudget(timeout_sec=1)
    def hook(current, path, index):
        if current == stage and path == ROOT and index == 0:
            clock[0] = 12.0
    provider = Provider(hook=hook)
    result = query(provider, depth=-1, budget=budget)
    assert result['analysis_limits'] == ['timeout_sec']
    assert result['count_lower_bound'] == 0
    assert result['query_metrics']['scope_count'] == (0 if stage == 'lookup' else 1)
    assert_closed(provider)


@pytest.mark.parametrize('stage,path', [('stop', ROOT), ('close', ROOT), ('value', ROOT)])
def test_cooperative_cancellation_never_returns_partial_success_and_closes_every_stream(stage, path):
    event = threading.Event()
    def hook(current, current_path, index):
        if current == stage and current_path == path:
            event.set()
    provider = Provider(hook=hook)
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            query(provider, depth=-1)
    finally:
        pop_cancel_event(token)
    assert_closed(provider)


@pytest.mark.parametrize('operation', ['subtree', 'find'])
@pytest.mark.parametrize('stage,path', [('before_open', GEN), ('next', GEN), ('close', GEN)])
@pytest.mark.parametrize('error', [TimeoutError('backend timeout'), RuntimeError('provider failed'),
                                  OperationCancelled('provider cancelled'), CaptureAborted('input_changed')])
def test_typed_and_provider_errors_propagate_unchanged_while_all_resources_close(operation, stage, path, error):
    def hook(current, current_path, index):
        if current == stage and current_path == path:
            raise error
    provider = Provider(hook=hook)
    kwargs = {'depth': -1} if operation == 'subtree' else {'module': 'leaf', 'count_mode': 'exact'}
    with pytest.raises(type(error)) as caught:
        query(provider, operation, **kwargs)
    assert caught.value is error
    assert_closed(provider)


@pytest.mark.parametrize('operation', ['subtree', 'find'])
@pytest.mark.parametrize('bad', ['wrong_parent', 'duplicate', 'wrong_lookup'])
def test_bad_provider_cannot_publish_wrong_parent_duplicate_or_wrong_exact_fact(operation, bad):
    provider = Provider()
    if bad == 'wrong_parent':
        provider.children[ROOT] = (replace(provider.facts[A], parent_path='somewhere_else'),)
    elif bad == 'duplicate':
        provider.children[ROOT] = (A, A)
    else:
        provider.lookup_overrides[ROOT] = ScopeLookup(provider.facts[A], True)
    kwargs = {'depth': -1} if operation == 'subtree' else {'module': 'leaf', 'count_mode': 'exact'}
    with pytest.raises(ValueError):
        query(provider, operation, **kwargs)
    assert_closed(provider)


def test_scope_ids_are_stable_but_separate_origin_artifact_kind_and_escaped_key():
    original = semantic_fact_id('slang', 'design-A', 'scope', ESCAPED)
    assert re.fullmatch(r'hs_[0-9a-f]{64}', original)
    assert original == semantic_fact_id('slang', 'design-A', 'scope', ESCAPED)
    alternatives = {
        semantic_fact_id('verdi_npi', 'design-A', 'scope', ESCAPED),
        semantic_fact_id('slang', 'design-B', 'scope', ESCAPED),
        semantic_fact_id('slang', 'design-A', 'definition', ESCAPED),
        semantic_fact_id('slang', 'design-A', 'scope', ESCAPED.rstrip()),
    }
    assert original not in alternatives and len(alternatives) == 4
    assert semantic_fact_id('a', 'b:c', 'd', 'e') != semantic_fact_id('a:b', 'c', 'd', 'e')
    p = Provider(origin='verdi_npi', identity='other-artifact')
    result = query(p, 'find', path=A)
    assert result['facts_origin'] == 'verdi_npi'
    assert result['artifact_identity'] == 'other-artifact'
    assert result['hits'][0]['scope_id'] != facts()[A].scope_id


def test_scope_and_lookup_are_deeply_immutable_and_public_rows_are_fresh():
    parameters, gaps = [['WIDTH', '8']], ['protected_region', 'protected_region']
    scope = replace(facts()[A], parameterization=parameters, gaps=gaps)
    lookup = ScopeLookup(scope, True, ['known_gap', 'known_gap'])
    parameters[0][1] = '999'
    gaps.append('later_gap')
    assert scope.parameterization == (('WIDTH', '8'),)
    assert scope.gaps == ('protected_region',)
    assert lookup.gaps == ('known_gap',)
    with pytest.raises(FrozenInstanceError):
        scope.path = 'changed'
    with pytest.raises(FrozenInstanceError):
        scope.location.line = 99
    with pytest.raises(FrozenInstanceError):
        lookup.complete = False
    first = scope.row()
    first['parameterization']['WIDTH'] = '999'
    first['gaps'].append('late')
    second = scope.row()
    assert second['parameterization'] == {'WIDTH': '8'}
    assert second['gaps'] == ['protected_region']
    assert second['source_line'] == 10 and second['definition_line'] == 70


@pytest.mark.parametrize('kwargs', [
    {'timeout_sec': True}, {'timeout_sec': 0}, {'timeout_sec': float('nan')},
    {'timeout_sec': float('inf')}, {'timeout_sec': 301},
    {'max_scopes': 0}, {'max_scopes': 100001}, {'max_scopes': 1.5},
    {'max_calls': False}, {'max_calls': 1000001},
    {'max_bytes': 0}, {'max_bytes': 64 * 1024 * 1024 + 1},
    {'max_depth': 0}, {'max_depth': 257},
])
def test_invalid_budget_types_and_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        HierarchyQueryBudget(**kwargs)


@pytest.mark.parametrize('kwargs', [
    {'depth': True}, {'depth': -2}, {'depth': 257}, {'root': 5},
    {'max_nodes': False}, {'max_nodes': 0}, {'max_nodes': 5001},
    {'count_mode': 'approximate'},
])
def test_invalid_subtree_arguments_do_no_provider_work(kwargs):
    provider = Provider()
    with pytest.raises(ValueError):
        query(provider, **kwargs)
    assert provider.events == []


@pytest.mark.parametrize('kwargs', [
    {}, {'path': ''}, {'path': 1}, {'path': ROOT, 'module': 'top'},
    {'module': 'leaf', 'limit': True}, {'module': 'leaf', 'limit': 0},
    {'module': 'leaf', 'limit': 5001}, {'module': 'leaf', 'count_mode': 'approximate'},
])
def test_invalid_search_arguments_do_no_provider_work(kwargs):
    provider = Provider()
    with pytest.raises(ValueError):
        query(provider, 'find', **kwargs)
    assert provider.events == []
