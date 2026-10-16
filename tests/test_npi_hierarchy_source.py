"""Native-adapter controls against a handwritten, invalidating capsule model.

No installed SDK/frontend is imported. Fake calls test ownership, projection,
limits and routing, not NPI semantics or license checkout. The separate real
P0 probe is required for product/SDK acceptance.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import FrozenInstanceError
import json
import os
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from src import hierarchy_source_ir as ir
from src import npi_hierarchy_source as adapter
from src import verdi_npi_backend as npi
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget, HierarchyScope
from src.hierarchy_source_service import HierarchySourceService
from src.kdb_anchor import KdbBindingChanged, capture_kdb_anchor


TOP = 'test_top'
LEAF = TOP + '.u0'
GEN = TOP + '.g[0]'
GENERATED = GEN + '.u'
ARRAY = TOP + '.arr'
ELEMENT = TOP + '.arr[0]'
ESCAPED = TOP + '.\\escaped.dot '


def forbidden(*a, **kw):
    pytest.fail('control test reached real SDK, parsing, discovery, build or fallback')


@pytest.fixture(autouse=True)
def no_real_execution(monkeypatch):
    monkeypatch.setattr(npi, '_import_pynpi', forbidden)
    monkeypatch.setattr(npi, 'parse_compile_log', forbidden)
    monkeypatch.setattr(npi, 'probe_verdi_backend', forbidden)
    monkeypatch.setattr(npi, '_silence_native_stdio', nullcontext)
    monkeypatch.setattr(npi, '_install_shutdown_banner_silencer', lambda: None)
    monkeypatch.setattr(npi, '_NPI_INITIALIZED_IDS', set())
    monkeypatch.setattr(npi, '_NPI_ACTIVE_DESIGNS', {})
    monkeypatch.setattr(npi, '_NPI_LOAD_GENERATIONS', {})
    monkeypatch.setattr(adapter, '_OWNER_LOCK', threading.Lock())
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    previous = sys.getprofile()
    def no_heavy_calls(frame, event, arg):
        if event != 'call':
            return
        path = frame.f_code.co_filename.replace('\\', '/')
        assert not any(part in path for part in (
            '/pynpi/', '/src/tb_hierarchy_builder.py', '/src/structural_scanner.py',
            '/src/slang_', '/src/source_graph_worker.py', '/src/structural_semantic_worker.py',
        )), f'forbidden heavy/native execution: {path}'
    sys.setprofile(no_heavy_calls)
    try:
        yield
    finally:
        sys.setprofile(previous)


class Capsule:
    def __init__(self, sdk, payload):
        self.sdk, self.payload = sdk, payload
        self.generation, self.valid = sdk.generation, True
        sdk.capsules.append(self)


class NativeHandle:
    def __init__(self, raw):
        self.cps_obj = raw

    def _property(self, key):
        sdk, raw = self.cps_obj.sdk, self.cps_obj
        sdk.validate(raw)
        value = raw.payload.get(key)
        sdk.emit(('property', raw.payload.get('full_name'), key))
        return value

    def type(self): return self._property('type')
    def full_name(self): return self._property('full_name')
    def name(self): return self._property('name')
    def file(self): return self._property('file')
    def line_no(self): return self._property('line_no')
    def def_name(self): return self._property('def_name')
    def def_file(self): return self._property('def_file')
    def def_line_no(self): return self._property('def_line_no')
    def value(self): return self._property('value')

    def scope_handle(self):
        raw, sdk = self.cps_obj, self.cps_obj.sdk
        sdk.validate(raw)
        parent = raw.payload.get('parent')
        value = NativeHandle(sdk.raw(parent)) if parent is not None else None
        sdk.emit(('parent', raw.payload.get('full_name')))
        return value


class FakeSdk:
    InternalScope, Parameter = 'InternalScope', 'Parameter'

    def __init__(self):
        def node(path, name, kind='npiModule', parent=TOP, definition='leaf'):
            return {'type': kind, 'full_name': path, 'name': name, 'parent': parent,
                    'file': 'instantiations.sv', 'line_no': 20,
                    'def_name': definition, 'def_file': 'definitions.sv', 'def_line_no': 3}
        self.nodes = {
            TOP: node(TOP, TOP, parent=None, definition=TOP),
            LEAF: node(LEAF, 'u0'),
            GEN: node(GEN, 'g[0]', 'npiGenScope', definition=None),
            GENERATED: node(GENERATED, 'u', parent=GEN),
            ARRAY: node(ARRAY, 'arr', 'npiModuleArray', parent=None, definition=None),
            ELEMENT: node(ELEMENT, 'arr[0]'),
            ESCAPED: node(ESCAPED, '\\escaped.dot '),
            'other': node('other', 'other', parent=None),
        }
        self.children = {TOP: [LEAF, GEN, ELEMENT], GEN: [GENERATED]}
        self.parameters = {LEAF: [('W', 4)], GENERATED: [('W', 2)], ELEMENT: [('W', 1)]}
        self.events, self.capsules, self.iterators = [], [], []
        self.generation, self.hook = 1, None
        self.release_rc, self.global_rc = 1, 1
        self.global_count = 0
        self.init_count = self.load_count = 0
        self.load_rc, self.load_error = 1, None
        self.system = SimpleNamespace(init=self.init, load_design=self.load)
        self.low = SimpleNamespace(InternalScope=self.InternalScope, Parameter=self.Parameter,
                                   handle_by_name=self.lookup, iterate=self.iterate,
                                   scan=self.scan, release_handle=self.release)
        self.lang = SimpleNamespace(release_all_handles=self.release_all,
                                    get_top_inst_list=forbidden)
        self.util = SimpleNamespace(handle=NativeHandle, GET_VALUE_ERROR='FAILED_TO_GET_VALUE',
                                    get_1m_list=forbidden)

    def emit(self, event):
        self.events.append(event)
        if self.hook is not None:
            self.hook(event)

    def validate(self, raw):
        assert raw.valid, 'invalid/double-released capsule used'
        assert raw.generation == self.generation, 'another generation capsule used'

    def raw(self, path):
        return Capsule(self, dict(self.nodes[path]))

    def lookup(self, path, scope):
        assert scope is None
        raw = self.raw(path) if path in self.nodes else None
        self.emit(('lookup', path))
        return raw

    def iterate(self, relation, reference):
        self.validate(reference)
        path = reference.payload['full_name']
        assert relation in (self.InternalScope, self.Parameter), 'unverified SDK relationship used'
        items = self.children.get(path, []) if relation == self.InternalScope else self.parameters.get(path, [])
        raw = None
        if items:
            raw = Capsule(self, {'relation': relation, 'reference': path, 'items': list(items), 'index': 0})
            self.iterators.append(raw)
        self.emit(('iterate', relation, path))
        return raw

    def scan(self, iterator):
        self.validate(iterator)
        data = iterator.payload
        index, relation, path = data['index'], data['relation'], data['reference']
        raw = None
        if index == len(data['items']):
            iterator.valid = False  # SDK automatically frees on NULL.
        else:
            data['index'] += 1
            item = data['items'][index]
            if relation == self.InternalScope:
                raw = self.raw(item)
            else:
                name, value = item
                raw = Capsule(self, {'type': 'npiParameter', 'name': name, 'value': value})
        self.emit(('scan', relation, path, index, raw is None))
        return raw

    def release(self, raw):
        self.events.append(('release', raw.payload.get('relation'), raw.payload.get('reference')))
        self.validate(raw)
        assert 'relation' in raw.payload, 'borrowed scope/base released individually'
        if self.release_rc == 1:
            raw.valid = False
        return self.release_rc

    def release_all(self):
        self.global_count += 1
        self.emit(('release_all', self.generation))
        if self.global_rc == 1:
            for raw in self.capsules:
                if raw.generation == self.generation:
                    raw.valid = False
        return self.global_rc

    def init(self, args):
        self.init_count += 1
        return 1

    def load(self, args):
        self.load_count += 1
        self.change_generation()  # Every load attempt can invalidate old handles.
        self.emit(('load', args[-1]))
        if self.load_error is not None:
            raise self.load_error
        return self.load_rc

    def change_generation(self):
        for raw in self.capsules:
            raw.valid = False
        self.generation += 1


def install_sdk(monkeypatch, sdk):
    package = ModuleType('pynpi')
    package.__path__ = []
    package.lang, package.lang_l0, package.lang_l0_util = sdk.lang, sdk.low, sdk.util
    monkeypatch.setitem(sys.modules, 'pynpi', package)


def owner_guard(sdk):
    expected, thread = sdk.generation, threading.get_ident()
    def require():
        if sdk.generation != expected or threading.get_ident() != thread:
            raise adapter.NpiHierarchyUnavailable('npi_loaded_generation_changed')
    return require


@contextmanager
def owned(monkeypatch, sdk, *, budget=None, degraded=False, failures=None):
    install_sdk(monkeypatch, sdk)
    with adapter.language_query_owner(
        top=TOP, artifact_identity='bounded-map-generation',
        budget=budget or HierarchyQueryBudget(), require_owner=owner_guard(sdk),
        degraded=degraded, cleanup_failed=lambda: failures.append('cleanup_failed') if failures is not None else None,
    ) as provider:
        yield provider


def count_events(sdk, event, *prefix):
    return sum(row[:len(prefix) + 1] == (event, *prefix) for row in sdk.events)


def assert_no_live_capsules(sdk):
    assert all(not raw.valid for raw in sdk.capsules)


def test_exact_generated_scope_uses_true_ancestors_without_enumerating_siblings(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as provider:
        result = HierarchySourceService(provider).find_instances(path=GENERATED)
        row = result['hits'][0]
        assert row['path'] == GENERATED and row['parent'] == GEN
        assert row['parameterization'] == {'W': '2'}
        assert (row['source_file'], row['source_line']) == ('instantiations.sv', 20)
        assert (row['definition_file'], row['definition_line']) == ('definitions.sv', 3)
        assert 'npi_type_parameter_inventory_unverified' in row['gaps']
        assert result['facts_origin'] == 'verdi_npi'
        assert result['artifact_identity'] == 'bounded-map-generation'
        assert result['total'] == 1
        assert count_events(sdk, 'iterate', 'InternalScope') == 0
        assert [event[1] for event in sdk.events if event[0] == 'lookup'] == [TOP, GENERATED]
        assert_no_individual_scope_release(sdk)
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


def assert_no_individual_scope_release(sdk):
    assert all(event[1] in ('InternalScope', 'Parameter') for event in sdk.events if event[0] == 'release')


@pytest.mark.parametrize('path', [GEN, ELEMENT, ESCAPED])
def test_scope_paths_and_semantic_parents_are_not_inferred_from_dot_or_array_spelling(monkeypatch, path):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as p:
        result = p.lookup_scope(path, p.budget)
        assert result.fact.path == path
        assert result.fact.parent_path == TOP
        if path == GEN:
            assert result.fact.kind == 'generate'
            assert result.fact.definition_id is result.fact.definition_name is None
        if path == ESCAPED:
            assert result.fact.name == '\\escaped.dot '
    assert_no_live_capsules(sdk)


def test_orphan_array_container_and_foreign_scope_are_unresolved_not_guessed(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as p:
        for path in (ARRAY, 'other'):
            result = p.lookup_scope(path, p.budget)
            assert result.fact is None and not result.complete
            assert result.gaps == ('npi_scope_ancestor_unresolved',)
        assert count_events(sdk, 'iterate', 'InternalScope') == 0


def test_exact_lookup_requires_selected_top_and_observed_ancestor_chain(monkeypatch):
    sdk = FakeSdk()
    sdk.nodes[GEN]['parent'] = LEAF
    sdk.nodes[LEAF]['parent'] = GEN  # A real getter cycle, despite TOP-looking spelling.
    with owned(monkeypatch, sdk) as p:
        result = p.lookup_scope(GENERATED, p.budget)
        assert result.fact is None and not result.complete
        assert result.gaps == ('npi_scope_ancestor_unresolved',)
        assert count_events(sdk, 'iterate', 'InternalScope') == 0
    del sdk.nodes[TOP]
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_selected_top_unavailable'):
        with owned(monkeypatch, sdk) as p:
            p.lookup_scope(GENERATED, p.budget)


def test_budget_cannot_be_swapped_and_closed_owner_cannot_be_reused(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as p:
        with pytest.raises(ValueError):
            p.lookup_scope(TOP, HierarchyQueryBudget())
        root = p.lookup_scope(TOP, p.budget).fact
        with pytest.raises(ValueError):
            with p.open_children(root, HierarchyQueryBudget()):
                forbidden()
    events = list(sdk.events)
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_language_owner_unavailable'):
        p.lookup_scope(TOP, p.budget)
    assert sdk.events == events
    assert root.row()['path'] == TOP  # Copied facts survive native global invalidation.


@pytest.mark.parametrize('degraded', [False, True])
def test_exact_missing_scope_is_negative_only_for_healthy_design(monkeypatch, degraded):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk, degraded=degraded) as p:
        result = p.lookup_scope(TOP + '.missing', p.budget)
        assert result.fact is None and result.complete is (not degraded)
        assert result.gaps == (('npi_degraded_design',) if degraded else ())


def test_early_prefix_releases_live_iterator_once_and_drain_never_releases_exhausted_iterator(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as p:
        root = p.lookup_scope(TOP, p.budget).fact
        with p.open_children(root, p.budget) as stream:
            assert next(stream).path == LEAF
        assert count_events(sdk, 'scan', 'InternalScope', TOP) == 1
        assert count_events(sdk, 'release', 'InternalScope', TOP) == 1
        with p.open_children(root, p.budget) as stream:
            assert [fact.path for fact in stream] == [LEAF, GEN, ELEMENT]
            assert stream.complete is True
        assert count_events(sdk, 'release', 'InternalScope', TOP) == 1
        assert count_events(sdk, 'scan', 'InternalScope', TOP) == 5  # first prefix + 3 values + NULL
        assert p.lookup_scope(LEAF, p.budget).fact.definition_name == 'leaf'
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


def test_null_internal_scope_iterator_is_known_empty_but_array_children_are_unverified(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as p:
        leaf = p.lookup_scope(LEAF, p.budget).fact
        with p.open_children(leaf, p.budget) as children:
            assert list(children) == []
            assert children.complete and children.gaps == ()
        assert count_events(sdk, 'scan', 'InternalScope', LEAF) == 0
        array = HierarchyScope('array-id', ARRAY, 'arr', 'instance_array', TOP)
        with p.open_children(array, p.budget) as children:
            assert list(children) == []
            assert not children.complete
            assert children.gaps == ('npi_array_container_children_unavailable',)
        assert count_events(sdk, 'iterate', 'InternalScope', ARRAY) == 0


@pytest.mark.parametrize('native_kind,gap', [('npiClassDefn', None), ('npiUnmodeledScope', 'npi_child_scope_kind_unmodeled')])
def test_noninstance_scopes_are_skipped_and_unknown_kinds_downgrade_enumeration(monkeypatch, native_kind, gap):
    sdk = FakeSdk()
    sdk.nodes['hidden'] = dict(sdk.nodes[LEAF], type=native_kind, full_name='hidden')
    sdk.children[TOP] = ['hidden', LEAF]
    with owned(monkeypatch, sdk) as p:
        root = p.lookup_scope(TOP, p.budget).fact
        with p.open_children(root, p.budget) as stream:
            assert [fact.path for fact in stream] == [LEAF]
            assert stream.complete is (gap is None)
            assert stream.gaps == ((gap,) if gap else ())


def test_unproven_native_parent_edge_is_rejected(monkeypatch):
    sdk = FakeSdk()
    sdk.children[TOP] = [ARRAY]
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_scope_parent_mismatch'):
        with owned(monkeypatch, sdk) as p:
            HierarchySourceService(p).subtree(depth=-1)
    assert_no_live_capsules(sdk)


def test_parameter_sentinel_duplicate_and_specialization_do_not_overstate_evidence(monkeypatch):
    sdk = FakeSdk()
    sdk.parameters[LEAF] = [('W', 4), ('W', 8), ('BAD', 'FAILED_TO_GET_VALUE'), ('NONE', None)]
    with owned(monkeypatch, sdk) as p:
        first = p.lookup_scope(LEAF, p.budget).fact
        assert first.parameterization == (('W', '4'),)
        assert set(first.gaps) >= {'npi_parameter_value_unavailable', 'npi_parameter_name_ambiguous',
                                   'npi_type_parameter_inventory_unverified'}
        second = p.lookup_scope(ELEMENT, p.budget).fact
        assert first.definition_name == second.definition_name == 'leaf'
        assert first.definition_id != second.definition_id
        with pytest.raises(FrozenInstanceError):
            first.parent_path = 'invented'
        json.dumps(first.row())  # No capsule or native object crosses the boundary.


@pytest.mark.parametrize('case,expected', [('valid', 128), ('sentinel', 0), ('duplicate', 1)])
def test_parameter_observation_cap_counts_read_items_even_when_no_value_is_usable(monkeypatch, case, expected):
    sdk = FakeSdk()
    sdk.parameters[LEAF] = [
        ('W' if case == 'duplicate' else f'P{i}', 'FAILED_TO_GET_VALUE' if case == 'sentinel' else i)
        for i in range(140)
    ]
    with owned(monkeypatch, sdk) as p:
        fact = p.lookup_scope(LEAF, p.budget).fact
        assert len(fact.parameterization) == expected
        assert count_events(sdk, 'scan', 'Parameter', LEAF) == 129
        assert count_events(sdk, 'release', 'Parameter', LEAF) == 1
        assert 'npi_parameter_limit' in fact.gaps
        assert 'npi_type_parameter_inventory_unverified' in fact.gaps


@pytest.mark.parametrize('settings,reason', [
    ({'max_calls': 1}, 'native_call_limit'), ({'max_bytes': 1}, 'fact_byte_limit'),
    ({'max_scopes': 1}, 'scope_limit'), ({'max_depth': 1}, 'scope_depth_limit'),
])
def test_service_work_budgets_still_cleanup_native_owner(monkeypatch, settings, reason):
    sdk = FakeSdk()
    budget = HierarchyQueryBudget(**settings)
    with owned(monkeypatch, sdk, budget=budget) as p:
        result = HierarchySourceService(p).subtree(depth=-1, count_mode='exact')
        assert result['coverage_status'] == 'partial'
        assert result['analysis_limits'] == [reason]
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


@pytest.mark.parametrize('property_value,reason', [('x' * 16385, 'native_property_size_limit'), ({'not': 'scalar'}, None)])
def test_non_scalar_and_oversized_native_properties_are_not_published(monkeypatch, property_value, reason):
    sdk = FakeSdk()
    sdk.nodes[LEAF]['name'] = property_value
    with owned(monkeypatch, sdk) as p:
        if reason:
            with pytest.raises(HierarchyBudgetExceeded) as caught:
                p.lookup_scope(LEAF, p.budget)
            assert caught.value.reason == reason
        else:
            with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_property_not_scalar'):
                p.lookup_scope(LEAF, p.budget)
    assert_no_live_capsules(sdk)


def test_native_handle_admission_is_bounded_without_exempting_repeated_handles(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as p:
        raw = sdk.raw(TOP)
        for _ in range(16384):
            p.wrap(raw)
        with pytest.raises(HierarchyBudgetExceeded) as caught:
            p.wrap(raw)
        assert caught.value.reason == 'native_handle_limit'


@pytest.mark.parametrize('error', [TimeoutError('typed backend timeout'), CaptureAborted('input_changed'),
                                  OperationCancelled('cancelled'), ValueError('SDK failure')])
def test_failed_scan_never_manually_releases_possibly_invalid_iterator(monkeypatch, error):
    sdk = FakeSdk()
    def hook(event):
        if event[:3] == ('scan', 'InternalScope', TOP):
            for raw in sdk.iterators:
                raw.valid = False
            raise error
    sdk.hook = hook
    with pytest.raises(type(error)) as caught:
        with owned(monkeypatch, sdk) as p:
            HierarchySourceService(p).subtree(depth=-1)
    assert caught.value is error
    assert count_events(sdk, 'release', 'InternalScope', TOP) == 0
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


@pytest.mark.parametrize('where', ['value', 'stop'])
def test_cancellation_after_native_scan_cleans_without_budget_checkpoint_in_cleanup(monkeypatch, where):
    sdk = FakeSdk()
    event = threading.Event()
    def hook(call):
        if call[:3] == ('scan', 'InternalScope', TOP) and call[-1] is (where == 'stop'):
            event.set()
    sdk.hook = hook
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            with owned(monkeypatch, sdk) as p:
                HierarchySourceService(p).subtree(depth=-1)
    finally:
        pop_cancel_event(token)
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


def test_native_late_deadline_still_releases_live_iterator_and_global_owner(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    sdk = FakeSdk()
    def hook(event):
        if event[:3] == ('scan', 'InternalScope', TOP):
            clock[0] = 12.0
    sdk.hook = hook
    budget = HierarchyQueryBudget(timeout_sec=1)
    with owned(monkeypatch, sdk, budget=budget) as p:
        result = HierarchySourceService(p).subtree(depth=-1)
        assert result['analysis_limits'] == ['timeout_sec']
    assert count_events(sdk, 'release', 'InternalScope', TOP) == 1
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


@pytest.mark.parametrize('cleanup', ['iterator', 'global'])
def test_cleanup_zero_is_fatal_unconfirmed_and_not_retried(monkeypatch, cleanup):
    sdk, failures = FakeSdk(), []
    if cleanup == 'iterator':
        sdk.release_rc = 0
    else:
        sdk.global_rc = 0
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='cleanup_unconfirmed'):
        with owned(monkeypatch, sdk, failures=failures) as p:
            HierarchySourceService(p).subtree(depth=0)
    assert failures == ['cleanup_failed']
    assert sdk.global_count == 1
    assert count_events(sdk, 'release', 'InternalScope', TOP) == 1


def test_primary_typed_error_survives_secondary_cleanup_failure(monkeypatch):
    sdk, failures = FakeSdk(), []
    sdk.global_rc = 0
    error = TimeoutError('primary query error')
    with pytest.raises(TimeoutError) as caught:
        with owned(monkeypatch, sdk, failures=failures):
            raise error
    assert caught.value is error
    assert failures == ['cleanup_failed']


def test_generation_replacement_abandons_old_capsules_without_releasing_other_design(monkeypatch):
    sdk, failures = FakeSdk(), []
    def hook(event):
        if event[:3] == ('scan', 'InternalScope', TOP):
            sdk.change_generation()
    sdk.hook = hook
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_loaded_generation_changed'):
        with owned(monkeypatch, sdk, failures=failures) as p:
            HierarchySourceService(p).subtree(depth=-1)
    assert sdk.global_count == 0
    assert count_events(sdk, 'release') == 0
    assert failures == ['cleanup_failed']
    sdk.hook = None
    with owned(monkeypatch, sdk):
        pass
    assert sdk.global_count == 1  # Exclusive lock was released after old owner failed.


def test_foreign_thread_cannot_read_or_release_existing_owner_capsules(monkeypatch):
    sdk = FakeSdk()
    p = adapter.NpiHierarchySourceProvider(sdk.lang, sdk.low, sdk.util, top=TOP,
        artifact_identity='generation', budget=HierarchyQueryBudget(), require_owner=owner_guard(sdk))
    p.lookup_scope(TOP, p.budget)
    before = list(sdk.events)
    errors = []
    def foreign():
        for operation in (lambda: p.lookup_scope(TOP, p.budget), p.close):
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(errors) == 2 and all(isinstance(e, adapter.NpiHierarchyUnavailable) for e in errors)
    assert sdk.events == before and sdk.global_count == 0
    assert p.active is True
    assert p.lookup_scope(LEAF, p.budget).fact.path == LEAF
    p.close()  # The original thread still owns every native resource.
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


def test_repeated_close_is_idempotent_and_cannot_free_a_later_owners_handles(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk) as old:
        old.lookup_scope(TOP, old.budget)
    assert sdk.global_count == 1
    with owned(monkeypatch, sdk) as current:
        current.lookup_scope(TOP, current.budget)
        before = list(sdk.events)
        old.close()
        old.close()
        assert sdk.events == before
        assert sdk.global_count == 1
        assert current.lookup_scope(LEAF, current.budget).fact.path == LEAF
    assert sdk.global_count == 2
    assert_no_live_capsules(sdk)


def test_nested_owner_is_rejected_before_import_or_global_cleanup(monkeypatch):
    sdk = FakeSdk()
    with owned(monkeypatch, sdk):
        with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_language_owner_busy'):
            with owned(monkeypatch, sdk):
                pytest.fail('nested owner admitted')
        assert sdk.global_count == 0
    assert sdk.global_count == 1


def test_concurrent_owner_rejects_before_prepare_import_or_releasing_outer_handles(monkeypatch):
    sdk, errors = FakeSdk(), []
    with owned(monkeypatch, sdk) as provider:
        provider.lookup_scope(TOP, provider.budget)
        before = list(sdk.events)
        def attempt():
            try:
                with adapter.language_query_owner(
                    top=TOP, artifact_identity='other', budget=HierarchyQueryBudget(),
                    require_owner=forbidden, cleanup_failed=forbidden, prepare_owner=forbidden,
                ):
                    forbidden()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(errors) == 1 and errors[0].reason == 'npi_language_owner_busy'
        assert sdk.events == before
        assert provider.lookup_scope(LEAF, provider.budget).fact.path == LEAF
    assert sdk.global_count == 1


def test_prepare_failure_has_no_language_cleanup_and_releases_exclusive_lock(monkeypatch):
    sdk = FakeSdk()
    install_sdk(monkeypatch, sdk)
    error = TimeoutError('preparation failed before language ownership')
    def prepare():
        raise error
    with pytest.raises(TimeoutError) as caught:
        with adapter.language_query_owner(
            top=TOP, artifact_identity='maps', budget=HierarchyQueryBudget(),
            prepare_owner=prepare, require_owner=forbidden, cleanup_failed=forbidden,
        ):
            forbidden()
    assert caught.value is error
    assert sdk.events == [] and sdk.global_count == 0
    with owned(monkeypatch, sdk):
        pass
    assert sdk.global_count == 1


def test_degraded_flag_is_resolved_after_prepare_not_before(monkeypatch):
    sdk, state = FakeSdk(), {'degraded': False}
    install_sdk(monkeypatch, sdk)
    def prepare():
        state['degraded'] = True
    with adapter.language_query_owner(
        top=TOP, artifact_identity='maps', budget=HierarchyQueryBudget(),
        prepare_owner=prepare, require_owner=owner_guard(sdk), cleanup_failed=forbidden,
        degraded=lambda: state['degraded'],
    ) as provider:
        result = provider.lookup_scope(TOP + '.missing', provider.budget)
        assert not result.complete and result.gaps == ('npi_degraded_design',)


class NoFallback:
    find_driver = find_loads = find_path = forbidden


def backend_fixture(monkeypatch, tmp_path, *, bind=True):
    maps = tmp_path / 'kdb'
    maps.mkdir()
    (maps / '_libFileMap').write_bytes(b'lib-A')
    (maps / '_stnElabDB').write_bytes(b'elab-A')
    sdk = FakeSdk()
    install_sdk(monkeypatch, sdk)
    monkeypatch.setattr(npi, '_import_pynpi', lambda: (sdk.system, SimpleNamespace()))
    backend = npi.VerdiNpiBackend(NoFallback())
    anchor = capture_kdb_anchor(str(maps))
    if bind:
        backend.bind_compile_context({'simulator': 'vcs', 'primary_top': TOP, 'files': {'user': []}})
        backend.bind_kdb_anchor(anchor)
    return backend, sdk, maps, anchor


@pytest.mark.parametrize('operation,arguments,top', [
    ('unsupported', {}, TOP), ('get_tb_subtree', {'depth': True}, TOP),
    ('get_tb_subtree', {'extra': 1}, TOP), ('find_tb_instance', {}, TOP),
    ('find_tb_instance', {'path': LEAF, 'module': 'leaf'}, TOP),
    ('get_tb_subtree', {}, ''),
])
def test_backend_prevalidation_does_not_initialize_or_load(monkeypatch, tmp_path, operation, arguments, top):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        backend.query_hierarchy(operation, arguments, top=top)
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0


def test_backend_requires_both_prepared_bindings_before_load(monkeypatch, tmp_path):
    backend, sdk, _, anchor = backend_fixture(monkeypatch, tmp_path, bind=False)
    for stage in ('neither', 'anchor_only'):
        if stage == 'anchor_only':
            backend.bind_kdb_anchor(anchor)
        with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_prepared_context_required'):
            backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0
    compile_only = npi.VerdiNpiBackend(NoFallback())
    compile_only.bind_compile_context({'simulator': 'vcs', 'primary_top': TOP})
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_prepared_context_required'):
        compile_only.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0


def test_backend_runs_real_control_flow_reuses_load_and_returns_only_native_fact_receipt(monkeypatch, tmp_path):
    backend, sdk, _, anchor = backend_fixture(monkeypatch, tmp_path)
    first = backend.query_hierarchy('find_tb_instance', {'path': GENERATED}, top=TOP)
    second = backend.query_hierarchy('get_tb_subtree', {'root': GEN, 'depth': -1}, top=TOP)
    assert first['hits'][0]['path'] == GENERATED
    assert first['facts_origin'] == second['facts_origin'] == 'verdi_npi'
    assert first['artifact_identity'] == second['artifact_identity']
    assert second['total_descendants'] == 1
    assert second['node']['children'][0]['path'] == GENERATED
    assert sdk.init_count == sdk.load_count == 1
    assert sdk.global_count == 2
    for result in (first, second):
        assert result['coverage_status'] == 'complete'
        metrics = result['query_metrics']
        for key in ('native_handle_count', 'native_iterator_release_count', 'language_cleanup_ms',
                    'design_load_ms', 'query_with_cleanup_validation_ms'):
            assert isinstance(metrics[key], (int, float)) and metrics[key] >= 0
        json.dumps(result)
    assert backend.kdb_status['load_quality'] == 'clean'
    assert anchor.current()
    assert_no_live_capsules(sdk)


def test_backend_checks_maps_before_load_and_after_native_query_without_fallback(monkeypatch, tmp_path):
    backend, sdk, maps, _ = backend_fixture(monkeypatch, tmp_path)
    def hook(event):
        if event == ('lookup', LEAF):
            path = maps / '_libFileMap'
            old = path.stat()
            path.write_bytes(b'lib-B')
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    sdk.hook = hook
    with pytest.raises(KdbBindingChanged):
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert sdk.global_count == 1
    before = sdk.load_count
    with pytest.raises(KdbBindingChanged):
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert sdk.load_count == before


def test_explicit_new_anchor_creates_new_artifact_and_scope_identity(monkeypatch, tmp_path):
    backend, sdk, maps, original = backend_fixture(monkeypatch, tmp_path)
    first = backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    (maps / '_libFileMap').write_bytes(b'new-generation')
    replacement = capture_kdb_anchor(str(maps))
    assert replacement.identity_sha256 != original.identity_sha256
    backend.bind_kdb_anchor(replacement)
    second = backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert first['artifact_identity'] != second['artifact_identity']
    assert first['hits'][0]['scope_id'] != second['hits'][0]['scope_id']
    assert first['hits'][0]['definition_id'] != second['hits'][0]['definition_id']
    assert sdk.init_count == 1 and sdk.load_count == sdk.global_count == 2


@pytest.mark.parametrize('replacement_top', [TOP, 'other'])
def test_backend_reload_even_same_identity_cannot_release_old_capsules_or_invalidate_new_owner(monkeypatch, tmp_path, replacement_top):
    backend, sdk, _, anchor = backend_fixture(monkeypatch, tmp_path)
    replacement = npi.VerdiNpiBackend(NoFallback())
    replacement.bind_compile_context({'simulator': 'vcs', 'primary_top': replacement_top})
    replacement.bind_kdb_anchor(anchor)
    def hook(event):
        if event == ('lookup', LEAF):
            assert replacement._ensure_loaded(anchor.path, replacement_top)
    sdk.hook = hook
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_loaded_generation_changed'):
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert npi._NPI_ACTIVE_DESIGNS[id(sdk.system)] == (
        anchor.path, replacement_top, ('bounded_maps', anchor.identity_sha256))
    assert npi._NPI_LOAD_GENERATIONS[id(sdk.system)] == 2
    assert replacement._state == 'ready' and sdk.load_count == 2
    assert sdk.global_count == 0 and count_events(sdk, 'release') == 0


def test_backend_busy_owner_is_rejected_before_any_native_load(monkeypatch, tmp_path):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    with owned(monkeypatch, sdk):
        with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_language_owner_busy'):
            backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
        assert sdk.init_count == sdk.load_count == sdk.global_count == 0
    assert sdk.global_count == 1


def test_backend_failed_load_has_no_language_cleanup_or_fallback(monkeypatch, tmp_path):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    sdk.load_rc = 0
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_load_failed'):
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert sdk.init_count == sdk.load_count == 1
    assert sdk.global_count == 0 and not sdk.capsules


@pytest.mark.parametrize('error', [TimeoutError('typed load timeout'), CaptureAborted('input_changed'), OperationCancelled('cancel load')])
def test_backend_typed_load_failure_is_preserved_without_language_cleanup(monkeypatch, tmp_path, error):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    sdk.load_error = error
    with pytest.raises(type(error)) as caught:
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert caught.value is error
    assert sdk.global_count == 0 and count_events(sdk, 'lookup') == 0


@pytest.mark.parametrize('error', [TimeoutError('SDK query timeout'), CaptureAborted('input_changed'), OperationCancelled('cancel')])
def test_backend_typed_query_errors_do_not_invoke_fallback(monkeypatch, tmp_path, error):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    def hook(event):
        if event == ('lookup', LEAF):
            raise error
    sdk.hook = hook
    with pytest.raises(type(error)) as caught:
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert caught.value is error
    assert sdk.global_count == 1


def test_backend_observes_cancellation_after_mandatory_global_cleanup(monkeypatch, tmp_path):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    event = threading.Event()
    def hook(call):
        if call[0] == 'release_all':
            event.set()
    sdk.hook = hook
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    finally:
        pop_cancel_event(token)
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


def test_backend_typed_cleanup_timeout_is_not_a_successful_query(monkeypatch, tmp_path):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    error = TimeoutError('typed global cleanup timeout')
    def hook(call):
        if call[0] == 'release_all':
            raise error
    sdk.hook = hook
    with pytest.raises(TimeoutError) as caught:
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert caught.value is error
    assert sdk.global_count == 1
    assert backend._state == 'failed'


@pytest.mark.parametrize('late_phase', ['cleanup', 'final_validation'])
@pytest.mark.parametrize('operation,arguments,expected_count', [
    ('find_tb_instance', {'path': LEAF}, 1),
    ('find_tb_instance', {'path': TOP + '.missing'}, 0),
    ('get_tb_subtree', {'root': LEAF, 'depth': -1}, 0),
    ('get_tb_subtree', {'root': TOP + '.missing'}, 0),
])
def test_full_query_deadline_includes_mandatory_cleanup_and_final_validation(
    monkeypatch, tmp_path, late_phase, operation, arguments, expected_count,
):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    backend, sdk, _, anchor = backend_fixture(monkeypatch, tmp_path)
    budget = HierarchyQueryBudget(timeout_sec=1)
    if late_phase == 'cleanup':
        def hook(event):
            if event[0] == 'release_all':
                clock[0] = 12.0
        sdk.hook = hook
    else:
        original = type(anchor).require_current
        def validate_current(self):
            result = original(self)
            if sdk.global_count == 1:
                clock[0] = 12.0
            return result
        monkeypatch.setattr(type(anchor), 'require_current', validate_current)
    result = backend.query_hierarchy(operation, arguments, top=TOP, budget=budget)
    assert result['coverage_status'] == 'partial'
    assert result['count_status'] == 'lower_bound'
    assert result['analysis_limits'] == ['timeout_sec']
    assert result['count_lower_bound'] == expected_count
    if operation == 'find_tb_instance':
        assert result['total'] is None
        assert [hit['path'] for hit in result['hits']] == ([LEAF] if expected_count else [])
    else:
        assert result['total_descendants'] is None
        if arguments['root'] == LEAF:
            assert result['node']['path'] == LEAF
        else:
            assert result['node'] is None
            assert result['error'] == 'instance_unresolved'
    assert sdk.global_count == 1
    assert sdk.init_count == sdk.load_count == 1
    assert_no_live_capsules(sdk)


def test_backend_cleanup_failure_invalidates_its_loaded_state(monkeypatch, tmp_path):
    backend, sdk, _, _ = backend_fixture(monkeypatch, tmp_path)
    sdk.global_rc = 0
    with pytest.raises(adapter.NpiHierarchyUnavailable, match='npi_language_cleanup_unconfirmed'):
        backend.query_hierarchy('find_tb_instance', {'path': LEAF}, top=TOP)
    assert backend._state == 'failed'
    assert id(sdk.system) not in npi._NPI_ACTIVE_DESIGNS
