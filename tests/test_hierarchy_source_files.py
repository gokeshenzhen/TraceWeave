"""Handwritten compile membership and semantic-file projection controls.

Fake streams establish ownership, three-state filters, budgets and lifecycle;
they provide no installed SDK/frontend or public-tool semantic acceptance.
"""
import builtins
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src import hierarchy_source_ir as ir
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.connectivity_ir import SourceLocation
from src.hierarchy_source_files import lookup_source_files, query_file_detail
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_metadata import CompileSourceCatalog, CompileSourceFile
from src.hierarchy_source_symbols import SourceSymbol


A, B = '/v1/shared.sv', '/v2/shared.sv'
EMPTY, PARTIAL, IMPORT = '/rtl/empty.sv', '/rtl/partial.sv', '/rtl/import.sv'
ALIAS = '/work/alias.sv'


def forbidden(*a, **kw):
    raise AssertionError('file projection attempted IO/native/frontend/full build')


@contextmanager
def no_io(monkeypatch):
    previous = sys.getprofile()
    def guard(frame, event, arg):
        if event == 'call':
            path = frame.f_code.co_filename.replace('\\', '/')
            assert not any(part in path for part in (
                '/pynpi/', '/src/slang_', '/src/tb_hierarchy_builder.py',
                '/src/structural_scanner.py', '/src/verdi_npi_backend.py',
                '/src/source_graph_worker.py',
            )), path
    original_import = builtins.__import__
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        names = [name, *(str(value) for value in fromlist or ())]
        assert not any(any(piece in {'pynpi', 'pyslang', 'tb_hierarchy_builder', 'structural_scanner'}
                           for piece in item.split('.')) for item in names)
        return original_import(name, globals, locals, fromlist, level)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(builtins, '__import__', guarded_import)
        patch.setattr(os.path, 'realpath', forbidden)
        for name in ('open', 'stat', 'lstat', 'scandir', 'listdir', 'readlink'):
            patch.setattr(os, name, forbidden)
        for name in ('open', 'read_text', 'read_bytes', 'stat', 'lstat', 'resolve'):
            patch.setattr(Path, name, forbidden)
        patch.setattr(subprocess, 'Popen', forbidden)
        patch.setattr(subprocess, 'run', forbidden)
        sys.setprofile(guard)
        try:
            yield
        finally:
            sys.setprofile(previous)


def catalog(*, gaps=()):
    files = tuple(CompileSourceFile(
        file_id='file-' + str(i), path=path, aliases=(ALIAS,) if path == A else (),
        categories=('rtl',), compiler_types=('class' if path == A else 'module',),
        roles=('project',), includes=(), is_compilation_unit=True,
        is_reported_include=False, is_dependency_include=False,
        source_status='not_captured' if path == PARTIAL else 'unavailable' if path == IMPORT else 'captured',
    ) for i, path in enumerate((A, B, EMPTY, PARTIAL, IMPORT)))
    return CompileSourceCatalog('compile-identity', files, tuple(gaps), 1024, 0.0)


def declaration(identity, name, kind, path, *, qualified=None):
    return SourceSymbol(identity, name, qualified or name, kind,
                        location=SourceLocation(path, 17), scope_id='scope-' + path,
                        declaration_id='decl-' + identity)


class Stream:
    def __init__(self, owner):
        self.owner, self.index = owner, 0
        self.complete, self.gaps = False, owner.spec['gaps']
        self.has_uvm_import = self.uses_uvm = None

    def __iter__(self): return self

    def __next__(self):
        p, path, budget, spec = self.owner.provider, self.owner.path, self.owner.budget, self.owner.spec
        budget.call()
        index = self.index
        p.fire('next', path, index)
        if index == len(spec['symbols']):
            self.complete, self.gaps = spec['complete'], spec['gaps']
            self.has_uvm_import, self.uses_uvm = spec['imported'], spec['used']
            p.fire('stop', path, index)
            raise StopIteration
        self.index += 1
        return spec['symbols'][index]


class Owner:
    def __init__(self, provider, entry, budget):
        self.provider, self.path, self.budget = provider, entry.path, budget
        self.spec = provider.inventory[entry.path]

    def __enter__(self):
        self.budget.call()
        self.provider.events.append(('open', self.path))
        self.provider.active += 1
        self.provider.fire('open', self.path, 0)
        return Stream(self)

    def __exit__(self, *exc):
        self.provider.events.append(('close', self.path))
        self.provider.active -= 1
        # Cleanup is mandatory and cannot spend an exhausted analysis budget.
        self.provider.fire('close', self.path, 0)


class Provider:
    origin, artifact_identity = 'slang', 'semantic-artifact'

    def __init__(self, *, budget=None):
        self.budget = budget or HierarchyQueryBudget()
        self.events, self.active, self.hook = [], 0, None
        def entry(symbols=(), *, complete=True, gaps=(), imported=False, used=False):
            return dict(symbols=list(symbols), complete=complete, gaps=gaps, imported=imported, used=used)
        self.inventory = {
            A: entry((declaration('M', 'M', 'module', A),
                      declaration('C', 'C', 'class', A, qualified='left::C'),
                      declaration('I', 'I', 'interface', A))),
            B: entry((declaration('other-C', 'C', 'class', B, qualified='right::C'),), used=True),
            EMPTY: entry(),
            PARTIAL: entry((declaration('partial-I', 'I', 'interface', PARTIAL),), complete=False,
                           gaps=('file_declarations_unverified',), imported=None, used=None),
            IMPORT: entry(imported=True, used=True),
        }

    def fire(self, phase, path, index):
        if self.hook:
            self.hook(phase, path, index)

    def open_file_symbols(self, entry, budget):
        assert budget is self.budget
        return Owner(self, entry, budget)


def detail(monkeypatch, cat, provider=None, **kw):
    with no_io(monkeypatch):
        return query_file_detail(cat, provider, **kw)


def lookup(monkeypatch, cat, provider=None, **kw):
    with no_io(monkeypatch):
        return lookup_source_files(cat, provider, **kw)


def test_exact_membership_and_suggestions_do_not_open_semantic_provider(monkeypatch):
    p, cat = Provider(), catalog()
    result = detail(monkeypatch, cat, p, path='/unknown/shared.sv')
    assert result['error'] == 'file_not_in_compile_set'
    assert result['did_you_mean'] == [A, B]
    assert result['facts_origin'] == 'compile_metadata' and result['artifact_identity'] == 'compile-identity'
    assert result['semantic_status'] == 'not_run' and result['coverage_status'] == 'complete'
    assert p.events == [] and p.budget.file_count == p.budget.call_count == 0
    # Only captured spellings establish aliases, never a live filesystem.
    result = detail(monkeypatch, cat, p, path='/v1/../v1/shared.sv')
    assert result['error'] == 'file_not_in_compile_set' and p.events == []


def test_exact_alias_binds_canonical_version_and_copies_semantic_facts(monkeypatch):
    p, cat = Provider(), catalog()
    result = detail(monkeypatch, cat, p, path=ALIAS)
    assert result['path'] == A and result['aliases'] == [ALIAS]
    assert result['ownership_identity'] == 'compile-identity'
    assert result['artifact_identity'] == 'semantic-artifact' and result['facts_origin'] == 'slang'
    assert result['compiler_file_types'] == ['class'] and result['file_type'] == 'module'
    assert result['modules'] == ['M'] and result['classes'] == ['left::C'] and result['interfaces'] == ['I']
    assert result['semantic_status'] == result['coverage_status'] == 'complete'
    assert result['symbol_inventory_complete'] and result['symbol_count'] == result['symbol_count_lower_bound'] == 3
    assert result['query_metrics']['symbol_count'] == 3 and result['query_metrics']['file_count'] == 1
    assert result['query_metrics']['native_call_count'] == 5  # open + 3 declarations + NULL.
    assert p.events == [('open', A), ('close', A)] and p.active == 0
    result['symbols'][0]['name'] = 'caller mutation'
    result['classes'].append('invented')
    result['aliases'].clear()
    assert p.inventory[A]['symbols'][0].name == 'M' and cat.files[0].aliases == (ALIAS,)


@pytest.mark.parametrize('path,classification,modules,classes,interfaces,status', [
    (B, 'class', [], ['right::C'], [], 'complete'),
    (EMPTY, 'unknown', [], [], [], 'complete'),
    (PARTIAL, None, [], [], ['I'], 'partial'),
])
def test_semantic_type_requires_positive_or_complete_priority_evidence(
    monkeypatch, path, classification, modules, classes, interfaces, status,
):
    p = Provider()
    result = detail(monkeypatch, catalog(), p, path=path)
    assert (result['file_type'], result['modules'], result['classes'], result['interfaces']) == (
        classification, modules, classes, interfaces)
    assert result['semantic_status'] == status
    if path == PARTIAL:
        assert result['source_status'] == 'not_captured'
        assert result['symbol_count'] is None and result['symbol_count_lower_bound'] == 1


def test_uvm_import_and_usage_are_independent_explicit_facts(monkeypatch):
    p = Provider()
    b = detail(monkeypatch, catalog(), p, path=B)
    assert b['has_uvm_import'] is False and b['contains_uvm'] is True
    imported = detail(monkeypatch, catalog(), p, path=IMPORT)
    assert imported['has_uvm_import'] is imported['contains_uvm'] is True
    assert imported['source_status'] == 'unavailable'  # Compile observation, independent native declaration facts.
    partial = detail(monkeypatch, catalog(), p, path=PARTIAL)
    assert partial['has_uvm_import'] is partial['contains_uvm'] is None


@pytest.mark.parametrize('flags', [dict(imported=True, used=None), dict(imported=True, used=False),
                                   dict(imported=0), dict(used='false')])
def test_invalid_uvm_evidence_is_rejected_after_resource_close(monkeypatch, flags):
    p = Provider()
    p.inventory[A].update(flags)
    with pytest.raises(ValueError):
        detail(monkeypatch, catalog(), p, path=A)
    assert p.active == 0 and p.events[-1] == ('close', A)


def test_captured_alias_location_is_allowed_but_other_version_or_duplicate_id_is_rejected(monkeypatch):
    p = Provider()
    first = p.inventory[A]['symbols'][0]
    p.inventory[A]['symbols'] = [replace(first, location=SourceLocation(ALIAS, 22))]
    result = detail(monkeypatch, catalog(), p, path=A)
    assert result['symbols'][0]['source_file'] == ALIAS
    p.inventory[A]['symbols'] = [replace(first, location=SourceLocation(B, 22))]
    with pytest.raises(ValueError, match='another compile member'):
        detail(monkeypatch, catalog(), p, path=A)
    p.inventory[A]['symbols'] = [first, first]
    with pytest.raises(ValueError, match='duplicate'):
        detail(monkeypatch, catalog(), p, path=A)
    assert p.active == 0


def test_missing_location_preserves_positive_declaration_but_marks_semantic_gap(monkeypatch):
    p = Provider()
    p.inventory[A]['symbols'] = [replace(p.inventory[A]['symbols'][0], location=None)]
    result = detail(monkeypatch, catalog(), p, path=A)
    assert result['modules'] == ['M'] and result['symbols'][0]['source_file'] is None
    assert result['symbol_inventory_complete'] and result['symbol_count'] == 1
    assert result['semantic_status'] == 'partial'
    assert result['semantic_gaps'] == ['symbol_source_location_unavailable']


def test_metadata_only_filter_and_display_limit_never_query_provider(monkeypatch):
    p, cat = Provider(), catalog()
    result = lookup(monkeypatch, cat, p, basename='shared.sv', limit=1, count_mode='exact')
    assert [row['path'] for row in result['matches']] == [A]
    assert result['total'] == result['count_lower_bound'] == result['examined_files'] == 2
    assert result['truncated'] and result['output_truncated']
    assert result['count_status'] == 'exact' and result['coverage_status'] == 'complete'
    assert result['semantic_status'] == 'not_run' and result['semantic_file_queries'] == 0
    assert result['facts_origin'] == 'compile_metadata' and p.events == []
    assert result['matches'][0]['file_type'] is result['matches'][0]['symbols'] is None
    assert result['query_metrics']['native_call_count'] == result['query_metrics']['symbol_count'] == 0


def test_captured_alias_metadata_filters_and_complete_empty_semantic_negative(monkeypatch):
    p, cat = Provider(), catalog()
    result = lookup(monkeypatch, cat, p, basename='alias.sv', path_contains='/work/')
    assert [row['path'] for row in result['matches']] == [A] and result['total'] == 1
    assert p.events == [] and result['semantic_status'] == 'not_run'
    result = lookup(monkeypatch, cat, p, basename='empty.sv', has_module='M')
    assert result['matches'] == [] and result['total'] == 0 and result['count_status'] == 'exact'
    assert result['coverage_status'] == result['semantic_status'] == 'complete'
    assert result['unknown_filter_files'] == 0 and result['semantic_file_queries'] == 1


def test_explicit_false_uvm_filter_is_not_omitted_and_unknown_is_not_false(monkeypatch):
    p = Provider()
    result = lookup(monkeypatch, catalog(), p, contains_uvm=False, count_mode='exact')
    assert [row['path'] for row in result['matches']] == [A, EMPTY]
    assert result['count_lower_bound'] == 2 and result['total'] is None
    assert result['unknown_filter_files'] == 1 and result['semantic_file_queries'] == 5
    assert result['coverage_status'] == 'partial'


def test_bounded_count_peeks_one_omitted_match_and_exact_count_remains_distinct(monkeypatch):
    cat = catalog()
    bounded = lookup(monkeypatch, cat, path_contains='/', limit=1)
    exact = lookup(monkeypatch, cat, path_contains='/', limit=1, count_mode='exact')
    assert bounded['total'] is None and bounded['count_lower_bound'] == bounded['examined_files'] == 2
    assert bounded['count_status'] == 'lower_bound' and bounded['coverage_status'] == 'partial'
    assert bounded['coverage_gaps'] == ['matching_files_not_enumerated']
    assert exact['total'] == exact['count_lower_bound'] == exact['examined_files'] == 5
    assert exact['count_status'] == 'exact' and exact['coverage_status'] == 'complete'
    assert bounded['output_truncated'] and exact['output_truncated']
    assert [r['path'] for r in bounded['matches']] == [r['path'] for r in exact['matches']] == [A]


def test_semantic_filters_use_three_state_and_without_compiler_hint_fallback(monkeypatch):
    p = Provider()
    result = lookup(monkeypatch, catalog(), p, has_module='M', count_mode='exact')
    assert [row['path'] for row in result['matches']] == [A]
    assert result['count_lower_bound'] == 1 and result['total'] is None
    assert result['unknown_filter_files'] == 1 and result['examined_files'] == result['semantic_file_queries'] == 5
    assert result['coverage_status'] == result['semantic_status'] == 'partial'
    assert result['query_metrics']['native_call_count'] == 15
    assert p.active == 0 and len([e for e in p.events if e[0] == 'close']) == 5
    unavailable = lookup(monkeypatch, catalog(), has_module='M', count_mode='exact')
    assert unavailable['matches'] == [] and unavailable['unknown_filter_files'] == 5
    assert unavailable['total'] is None and unavailable['semantic_status'] == 'unavailable'


def test_metadata_and_semantic_filters_are_and_and_false_dominates_unknown(monkeypatch):
    p = Provider()
    result = lookup(monkeypatch, catalog(), p, basename='shared.sv', path_contains='/v2/',
                    file_type='class', contains_uvm=True, count_mode='exact')
    assert [row['path'] for row in result['matches']] == [B]
    assert result['total'] == result['examined_files'] == result['semantic_file_queries'] == 1
    assert result['unknown_filter_files'] == 0 and result['coverage_status'] == 'complete'
    assert p.events == [('open', B), ('close', B)]
    p = Provider()
    # M is not disproved in a partial declaration stream, but a known module
    # classification disproves the simultaneous class-only predicate.
    p.inventory[PARTIAL]['symbols'] = [declaration('other-M', 'Other', 'module', PARTIAL)]
    result = lookup(monkeypatch, catalog(), p, basename='partial.sv', has_module='M', file_type='class')
    assert result['matches'] == [] and result['unknown_filter_files'] == 0
    assert result['total'] == 0 and result['coverage_status'] == 'partial'


@pytest.mark.parametrize('arguments', [{}, {'basename': ''}, {'has_module': ''},
    {'file_type': '', 'basename': 'shared.sv'}, {'contains_uvm': 1},
    {'basename': 'shared.sv', 'limit': True}, {'basename': 'shared.sv', 'count_mode': 'all'}])
def test_invalid_filters_reject_before_provider_work(monkeypatch, arguments):
    p = Provider()
    with pytest.raises(ValueError):
        lookup(monkeypatch, catalog(), p, **arguments)
    assert p.events == [] and p.budget.call_count == p.budget.file_count == 0


@pytest.mark.parametrize('stage', ['membership', 'suggestions', 'admission'])
def test_ownership_prelude_timeout_is_partial_not_semantic_or_complete_miss(monkeypatch, stage):
    # Control-only phase barriers, not evidence about filesystem performance.
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    p, cat = Provider(budget=HierarchyQueryBudget(timeout_sec=1)), catalog()
    name = {'membership': 'find_file', 'suggestions': 'suggest_files', 'admission': 'admit_file'}[stage]
    cls = HierarchyQueryBudget if stage == 'admission' else CompileSourceCatalog
    original = getattr(cls, name)
    def late(self, *a, **kw):
        clock[0] = 12.0
        return original(self, *a, **kw)
    monkeypatch.setattr(cls, name, late)
    result = detail(monkeypatch, cat, p, path='/absent/shared.sv' if stage == 'suggestions' else A)
    assert result['error'] == 'file_query_incomplete'
    assert result['semantic_status'] == 'not_run' and result['coverage_status'] == 'partial'
    assert result['analysis_limits'] == ['timeout_sec'] and 'did_you_mean' not in result
    assert p.events == [] and p.active == 0 and p.budget.call_count == 0


def test_file_admission_and_metadata_selection_stop_without_provider(monkeypatch):
    budget = HierarchyQueryBudget(max_files=1)
    p, cat = Provider(budget=budget), catalog()
    budget.admit_file(cat.files[0])  # The same owner's earlier work used its sole file allowance.
    result = detail(monkeypatch, cat, p, path=A)
    assert result['error'] == 'file_query_incomplete' and result['analysis_limits'] == ['file_limit']
    assert result['semantic_status'] == 'not_run' and p.events == []
    result = lookup(monkeypatch, cat, p, basename='shared.sv', has_module='M')
    assert result['semantic_status'] == 'not_run' and result['semantic_file_queries'] == 0
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['file_limit']
    assert result['examined_files'] == 0 and result['total'] is None and p.events == []


@pytest.mark.parametrize('limits,reason,observed', [
    ({'max_symbols': 1}, 'symbol_limit', 1), ({'max_calls': 1}, 'native_call_limit', 0),
])
def test_semantic_budget_stops_retain_only_observed_facts_and_close(monkeypatch, limits, reason, observed):
    p = Provider(budget=HierarchyQueryBudget(**limits))
    result = detail(monkeypatch, catalog(), p, path=A)
    assert result['symbol_count'] is None and result['symbol_count_lower_bound'] == observed
    assert result['semantic_status'] == 'partial' and result['analysis_limits'] == [reason]
    assert p.active == 0 and p.events[-1] == ('close', A)
    assert result['contains_uvm'] is None and result['has_uvm_import'] is None


def test_byte_limit_during_file_admission_does_not_open_provider(monkeypatch):
    p = Provider(budget=HierarchyQueryBudget(max_bytes=1))
    result = detail(monkeypatch, catalog(), p, path=A)
    assert result['error'] == 'file_query_incomplete' and result['analysis_limits'] == ['fact_byte_limit']
    assert result['semantic_status'] == 'not_run' and p.events == []


@pytest.mark.parametrize('phase', ['stop', 'close'])
def test_late_stream_exhaustion_or_cleanup_is_in_analysis_deadline(monkeypatch, phase):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    p = Provider(budget=HierarchyQueryBudget(timeout_sec=1))
    p.hook = lambda at, path, i: clock.__setitem__(0, 12.0) if at == phase else None
    result = detail(monkeypatch, catalog(), p, path=A)
    assert result['modules'] == ['M'] and result['symbol_count_lower_bound'] == 3
    assert result['symbol_count'] is None and not result['symbol_inventory_complete']
    assert result['analysis_limits'] == ['timeout_sec'] and result['semantic_status'] == 'partial'
    assert result['has_uvm_import'] is result['contains_uvm'] is None
    assert p.active == 0


@pytest.mark.parametrize('error', [TimeoutError('explicit provider timeout'),
    CaptureAborted('input_changed'), OperationCancelled('cancelled'), RuntimeError('provider failed')])
@pytest.mark.parametrize('phase', ['next', 'close'])
def test_typed_and_ordinary_provider_errors_propagate_original_without_fallback(monkeypatch, error, phase):
    p = Provider()
    def hook(at, path, i):
        if at == phase:
            raise error
    p.hook = hook
    with pytest.raises(type(error)) as caught:
        detail(monkeypatch, catalog(), p, path=A)
    assert caught.value is error and p.active == 0
    assert p.events[-1] == ('close', A)


def test_cancellation_after_positive_symbol_does_not_turn_into_partial_success(monkeypatch):
    p, event = Provider(), threading.Event()
    p.hook = lambda at, path, i: event.set() if at == 'next' and i == 1 else None
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            detail(monkeypatch, catalog(), p, path=A)
    finally:
        pop_cancel_event(token)
    assert p.active == 0 and p.events[-1] == ('close', A)


def test_catalog_gap_and_absent_provider_cannot_publish_complete_semantic_result(monkeypatch):
    cat = catalog(gaps=('compile_inputs_incomplete',))
    p = Provider()
    result = detail(monkeypatch, cat, p, path=EMPTY)
    assert result['symbol_count'] == 0 and result['semantic_status'] == 'complete'
    assert result['coverage_status'] == 'partial' and result['coverage_gaps'] == ['compile_inputs_incomplete']
    result = detail(monkeypatch, cat, path=A)
    assert result['semantic_status'] == 'unavailable' and result['file_type'] is None
    assert result['symbol_count'] is None and result['modules'] is None
    assert result['coverage_status'] == 'partial' and 'semantic_provider_unavailable' in result['coverage_gaps']
