"""Pure request/export controls with handwritten ownership and semantic facts.

No native adapter, parser or frontend is exercised. A complete fake provider
models only its declared fixture domain; it proves no real KDB coverage.
"""
import builtins
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src import hierarchy_source_ir as ir
from src import hierarchy_source_request as requests
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.compile_session_snapshot import CompileSessionSnapshot, FileContentSnapshot
from src.connectivity_ir import SourceLocation
from src.design_context import DesignContext
from src.hierarchy_source_export import export_hierarchy_section
from src.hierarchy_source_ir import HierarchyQueryBudget, HierarchyScope, ScopeLookup
from src.hierarchy_source_metadata import CompileSourceCatalog, CompileSourceFile
from src.hierarchy_source_service import finish_hierarchy_query
from src.hierarchy_source_symbols import ClassFact, SourceSymbol
from src.kdb_anchor import KdbBindingChanged


A, B, ALIAS = '/compile/a.sv', '/compile/b.sv', '/captured/alias.sv'
IDENTITY = 'a' * 64
METADATA = ('compile_result', 'include_tree', 'filelist_tree')
SEMANTIC = ('files_full', 'interfaces', 'component_tree_full', 'class_hierarchy_full')


def forbidden(*a, **kw):
    raise AssertionError('unexpected filesystem/native/frontend/provider work')


@pytest.fixture(autouse=True)
def no_heavy_execution(monkeypatch):
    previous, original = sys.getprofile(), builtins.__import__
    def imports(name, globals=None, locals=None, fromlist=(), level=0):
        names = [name, *(str(x) for x in fromlist or ())]
        assert not any(any(part in {'pynpi', 'pyslang', 'tb_hierarchy_builder', 'structural_scanner'}
                           for part in item.split('.')) for item in names)
        return original(name, globals, locals, fromlist, level)
    def profile(frame, event, arg):
        if event == 'call':
            path = frame.f_code.co_filename.replace('\\', '/')
            assert not any(part in path for part in ('/pynpi/', '/src/slang_', '/src/tb_hierarchy_builder.py',
                '/src/structural_scanner.py', '/src/verdi_npi_backend.py', '/src/source_graph_worker.py')), path
    monkeypatch.setattr(builtins, '__import__', imports)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


@contextmanager
def no_io(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(os.path, 'realpath', forbidden)
        for name in ('open', 'stat', 'lstat', 'scandir', 'listdir', 'readlink'):
            patch.setattr(os, name, forbidden)
        for name in ('open', 'read_text', 'read_bytes', 'stat', 'lstat', 'resolve'):
            patch.setattr(Path, name, forbidden)
        yield


def compile_record(*, empty=False):
    return dict(compile_cwd='/compile', simulator='vcs', top_modules=['top'],
        files={'user': [] if empty else [{'path': A, 'type': 'module'}, {'path': B, 'type': 'class'}]},
        include_tree={}, filelist_tree={'/compile/input.f': {'children': [], 'options': ['+define+MODE=1']}})


def context(*, gaps=(), empty=False):
    records = () if empty else tuple(FileContentSnapshot(path=path, device=1, inode=i, size=20,
        mtime_ns=7, ctime_ns=8, sha256='b' * 64) for i, path in enumerate((A, B), 1))
    return DesignContext(ordered_compile_logs=('/compile/compile.log',), simulator='vcs',
        selected_top='top', backend_preference='auto', compile_result_json=json.dumps(compile_record(empty=empty)),
        snapshot=CompileSessionSnapshot(records, True, (), 'c' * 64),
        aliases=() if empty else ((ALIAS, A),), unavailable=(), include_resolutions=(), environment=(),
        identity_sha256=IDENTITY, gaps=tuple(gaps), include_resolution_complete=True, metrics=())


def catalog(*, identity=IDENTITY, gaps=(), empty=False):
    entries = () if empty else tuple(CompileSourceFile('file-' + str(i), path,
        (ALIAS,) if path == A else (), ('rtl',), ('compiler_hint',), ('project',), (),
        True, False, False, 'captured') for i, path in enumerate((A, B)))
    return CompileSourceCatalog(identity, entries, tuple(gaps), 800, 0)


def symbol(identity, name, kind, path):
    return SourceSymbol(identity, name, name, kind, SourceLocation(path, 11), declaration_id=identity)


class Stream:
    def __init__(self, owner):
        self.owner, self.index, self.complete = owner, 0, False
        self.gaps = owner.provider.gaps
        self.has_uvm_import = self.uses_uvm = False

    def __iter__(self): return self

    def __next__(self):
        o, index = self.owner, self.index
        o.budget.call()
        o.provider.fire('next', o.label, index)
        if index == len(o.facts):
            self.complete = o.provider.complete
            o.provider.fire('stop', o.label, index)
            raise StopIteration
        self.index += 1
        return o.facts[index]


class Owner:
    def __init__(self, provider, label, facts, budget):
        self.provider, self.label, self.facts, self.budget = provider, label, facts, budget

    def __enter__(self):
        assert self.budget is self.provider.budget
        self.budget.call()
        self.provider.events.append(('open', self.label))
        self.provider.active += 1
        return Stream(self)

    def __exit__(self, *exc):
        self.provider.events.append(('close', self.label))
        self.provider.active -= 1
        self.provider.fire('close', self.label, 0)


class Provider:
    origin, artifact_identity, top = 'slang', 'artifact-fixture', 'top'

    def __init__(self, budget=None, *, complete=True, gaps=()):
        self.budget = budget or HierarchyQueryBudget()
        self.complete, self.gaps, self.hook = complete, gaps, None
        self.events, self.active = [], 0
        self.scopes = {
            'top': HierarchyScope('root', 'top', 'top', 'module', None, 'def-top', 'top'),
            'top.g[0]': HierarchyScope('gen', 'top.g[0]', 'g[0]', 'generate', 'top'),
            'top.g[0].u': HierarchyScope('leaf', 'top.g[0].u', 'u', 'module', 'top.g[0]', 'def-leaf', 'leaf'),
        }
        self.children = {'top': [self.scopes['top.g[0]']], 'top.g[0]': [self.scopes['top.g[0].u']]}
        base, child = symbol('Base', 'pkg::Base', 'class', A), symbol('Child', 'pkg::Child', 'class', B)
        self.classes = [ClassFact(base, 'none'), ClassFact(child, 'resolved', 'Base', 'pkg::Base')]
        self.file_symbols = {A: [symbol('Top', 'top', 'module', A), symbol('Bus', 'bus_if', 'interface', A), base],
                             B: [symbol('Aux', 'aux_if', 'interface', B), child]}

    def fire(self, phase, label, index):
        if self.hook: self.hook(phase, label, index)

    def lookup_scope(self, path, budget):
        assert budget is self.budget
        budget.call()
        self.events.append(('lookup', path))
        return ScopeLookup(self.scopes.get(path), self.complete, self.gaps)

    def open_children(self, scope, budget):
        return Owner(self, scope.path, self.children.get(scope.path, []), budget)

    def open_classes(self, budget):
        return Owner(self, 'classes', self.classes, budget)

    def open_file_symbols(self, entry, budget):
        return Owner(self, entry.path, self.file_symbols[entry.path], budget)


class PoisonProvider:
    def __getattribute__(self, name):
        raise AssertionError('metadata request inspected provider: ' + name)


def export(monkeypatch, ctx, provider=None, **kw):
    with no_io(monkeypatch):
        return export_hierarchy_section(ctx, provider, **kw)


def prepare(monkeypatch, ctx, operation, args, **kw):
    with no_io(monkeypatch):
        return requests.prepare_source_query(ctx, operation, args, **kw)


def execute(monkeypatch, plan, provider=None):
    with no_io(monkeypatch): return plan.execute(provider)


@pytest.mark.parametrize('section', METADATA)
def test_metadata_export_copies_captured_section_without_catalog_or_provider(monkeypatch, section):
    ctx, budget = context(), HierarchyQueryBudget()
    expected = compile_record() if section == 'compile_result' else (
        {} if section == 'include_tree' else {'/compile/input.f': {'children': [], 'options': ['+define+MODE=1']}})
    import src.hierarchy_source_export as exports
    monkeypatch.setattr(exports, 'build_compile_source_catalog', forbidden)
    result = export(monkeypatch, ctx, PoisonProvider(), section=section, budget=budget)
    assert result['data'] == expected and result['record_export_complete'] and result['export_complete']
    assert result['semantic_status'] == 'not_run' and result['facts_origin'] == 'compile_metadata'
    assert result['coverage_status'] == 'complete' and result['query_metrics']['estimated_fact_bytes'] > 0
    assert budget.call_count == budget.file_count == budget.symbol_count == budget.scope_count == 0
    result['data'].clear()
    original = ctx.compile_result()
    assert original['files']['user'][0]['path'] == A
    assert original['filelist_tree']['/compile/input.f']['options'] == ['+define+MODE=1']


@pytest.mark.parametrize('section', METADATA)
def test_metadata_record_copy_is_separate_from_gap_free_export(monkeypatch, section):
    result = export(monkeypatch, context(gaps=('compile_inputs_incomplete',)), PoisonProvider(), section=section)
    assert result['record_export_complete'] is True and result['export_complete'] is False
    assert result['coverage_status'] == 'partial' and result['coverage_gaps'] == ['compile_inputs_incomplete']
    assert result['semantic_status'] == 'not_run' and result['warning']


@pytest.mark.parametrize('max_bytes,complete', [(520, True), (519, False)])
def test_empty_metadata_payload_has_independent_exact_byte_boundary(monkeypatch, max_bytes, complete):
    # The literal {} is 2 ASCII JSON chars: fixed512 + 4*2 = 520 bytes.
    budget = HierarchyQueryBudget(max_bytes=max_bytes)
    result = export(monkeypatch, context(), PoisonProvider(), section='include_tree', budget=budget)
    assert result['export_complete'] is complete and result['semantic_status'] == 'not_run'
    if complete:
        assert result['data'] == {} and budget.fact_bytes == 520
    else:
        assert result['data'] is None and result['error'] == 'section_export_incomplete'
        assert result['analysis_limits'] == ['fact_byte_limit'] and result['coverage_status'] == 'partial'


@pytest.mark.parametrize('section', SEMANTIC)
def test_four_semantic_exports_use_one_provider_and_handwritten_truth(monkeypatch, section):
    p = Provider()
    result = export(monkeypatch, context(), p, section=section, catalog=catalog())
    assert result['export_complete'] and result['coverage_status'] == result['semantic_status'] == 'complete'
    assert result['facts_origin'] == 'slang' and result['artifact_identity'] == 'artifact-fixture'
    assert not result['output_truncated'] and result['analysis_limits'] == []
    if section == 'files_full':
        assert [r['path'] for r in result['data']] == [A, B] and result['total'] == 2
        assert result['data'][0]['modules'] == ['top'] and result['data'][1]['classes'] == ['pkg::Child']
        assert p.budget.file_count == 2 and p.budget.symbol_count == 5
    elif section == 'interfaces':
        assert [r['name'] for r in result['data']] == ['bus_if', 'aux_if'] and result['total'] == 2
        assert [r['source_file'] for r in result['data']] == [A, B]
    elif section == 'component_tree_full':
        root = result['data']
        assert root['path'] == 'top' and root['children'][0]['path'] == 'top.g[0]'
        assert root['children'][0]['children'][0]['path'] == 'top.g[0].u'
        assert result['total_descendants'] == result['count_lower_bound'] == 2
        assert result['query_metrics']['scope_count'] == 3 and result['view'] == 'elaborated_instances'
    else:
        assert result['total'] == result['class_inventory_count'] == 2
        assert result['data'][0]['qualified_name'] == 'pkg::Base'
        child = result['data'][0]['children'][0]
        assert child['qualified_name'] == 'pkg::Child' and child['base_symbol_id'] == 'Base'
        assert result['class_inventory_complete'] and result['view'] == 'static_class_inheritance'
    assert p.active == 0 and len([e for e in p.events if e[0] == 'open']) == len([e for e in p.events if e[0] == 'close'])
    if section == 'files_full':
        result['data'][0]['symbols'][0]['name'] = 'caller mutation'
        assert p.file_symbols[A][0].name == 'top'
    elif section == 'interfaces':
        result['data'][0]['name'] = 'caller mutation'
        assert p.file_symbols[A][1].name == 'bus_if'
    elif section == 'component_tree_full':
        result['data']['children'].clear()
        assert p.children['top'][0].path == 'top.g[0]'
    else:
        result['data'][0]['children'].clear()
        assert p.classes[1].base_symbol_id == 'Base'


@pytest.mark.parametrize('section', SEMANTIC)
def test_output_cap_is_distinct_from_full_analysis_and_export_completeness(monkeypatch, section):
    p = Provider()
    result = export(monkeypatch, context(), p, section=section, max_items=1, catalog=catalog())
    assert not result['export_complete'] and result['output_truncated'] and result['warning']
    assert result['analysis_limits'] == [] and p.active == 0
    if section == 'files_full':
        assert result['count_lower_bound'] == result['examined_files'] == 1 and result['total'] is None
        assert p.events == [('open', A), ('close', A)]  # No omitted file semantic query.
    elif section == 'interfaces':
        assert result['count_lower_bound'] == 2 and result['examined_files'] == 2 and result['total'] is None
        assert [row['name'] for row in result['data']] == ['bus_if']
    elif section == 'component_tree_full':
        assert result['coverage_status'] == 'complete' and result['total_descendants'] == 2
        assert result['data']['children'] == [] and result['query_metrics']['scope_count'] == 3
    else:
        assert result['coverage_status'] == 'complete' and result['total'] == 1
        assert result['class_inventory_count'] == 2 and result['class_inventory_complete']


@pytest.mark.parametrize('section', SEMANTIC)
def test_partial_provider_never_exports_complete_section(monkeypatch, section):
    p = Provider(complete=False, gaps=('provider_inventory_unverified',))
    result = export(monkeypatch, context(), p, section=section, catalog=catalog())
    assert not result['export_complete'] and result['coverage_status'] == 'partial'
    assert 'provider_inventory_unverified' in result['coverage_gaps'] and p.active == 0


@pytest.mark.parametrize('section', ['files_full', 'interfaces'])
def test_foreign_catalog_is_rejected_before_file_provider(monkeypatch, section):
    p = Provider()
    with pytest.raises(ValueError, match='different context'):
        export(monkeypatch, context(), p, section=section, catalog=catalog(identity='other-context'))
    assert p.events == [] and p.budget.call_count == 0


def test_conflicting_interface_identity_cannot_be_silently_deduplicated(monkeypatch):
    p = Provider()
    p.file_symbols[B][0] = replace(p.file_symbols[B][0], symbol_id='Bus')
    with pytest.raises(ValueError, match='conflicting interface'):
        export(monkeypatch, context(), p, section='interfaces', catalog=catalog())
    assert p.active == 0


@pytest.mark.parametrize('section', SEMANTIC)
def test_absent_semantic_provider_is_unavailable_not_success(monkeypatch, section):
    result = export(monkeypatch, context(), section=section, catalog=catalog())
    assert not result['export_complete'] and result['coverage_status'] == 'partial'
    assert result['semantic_status'] == 'unavailable'
    assert 'semantic_provider_unavailable' in result['coverage_gaps']


OPERATIONS = [
    ('get_tb_subtree', {'root': 'top', 'depth': -1, 'count_mode': 'exact'}, True),
    ('find_tb_instance', {'path': 'top.g[0].u'}, True),
    ('lookup_tb_files', {'basename': 'a.sv'}, False),
    ('get_tb_file_detail', {'path': ALIAS}, True),
    ('get_tb_class_hierarchy', {}, True),
    ('dump_tb_section', {'section': 'include_tree'}, False),
]


@pytest.mark.parametrize('operation,args,needed', OPERATIONS)
def test_six_requests_validate_then_execute_only_the_demanded_provider(monkeypatch, operation, args, needed):
    budget = HierarchyQueryBudget()
    plan = prepare(monkeypatch, context(), operation, args, budget=budget)
    assert plan.needs_provider is needed and plan.preparation_limit is None
    p = Provider(budget)
    result = execute(monkeypatch, plan, p if needed else PoisonProvider())
    assert result['schema_version'] == 2 and result['coverage_status'] == 'complete'
    if operation == 'get_tb_subtree': assert result['total_descendants'] == 2
    elif operation == 'find_tb_instance':
        assert [row['path'] for row in result['hits']] == ['top.g[0].u']
        assert p.events == [('lookup', 'top.g[0].u')]
    elif operation == 'lookup_tb_files': assert [row['path'] for row in result['matches']] == [A]
    elif operation == 'get_tb_file_detail': assert result['path'] == A and result['symbol_count'] == 3
    elif operation == 'get_tb_class_hierarchy': assert result['total'] == 2
    else: assert result['data'] == {} and result['semantic_status'] == 'not_run'
    assert p.active == 0


@pytest.mark.parametrize('operation,args', [
    ('get_tb_file_detail', {'path': '/other/a.sv'}),
    ('lookup_tb_files', {'basename': 'not-compiled.sv', 'has_module': 'top'}),
    ('lookup_tb_files', {'basename': 'not-compiled.sv', 'contains_uvm': False}),
    ('lookup_tb_files', {'basename': 'a.sv'}),
    ('dump_tb_section', {'section': 'compile_result'}),
])
def test_nonmember_empty_selection_and_metadata_do_not_even_inspect_provider(monkeypatch, operation, args):
    plan = prepare(monkeypatch, context(), operation, args)
    assert not plan.needs_provider
    result = execute(monkeypatch, plan, PoisonProvider())
    assert result['semantic_status'] == 'not_run'
    assert result['query_metrics']['native_call_count'] == result['query_metrics']['symbol_count'] == 0
    if operation == 'get_tb_file_detail': assert result['error'] == 'file_not_in_compile_set'


@pytest.mark.parametrize('section', ['files_full', 'interfaces'])
def test_empty_captured_file_set_can_finish_without_loading_semantics(monkeypatch, section):
    plan = prepare(monkeypatch, context(empty=True), 'dump_tb_section', {'section': section})
    assert not plan.needs_provider
    result = execute(monkeypatch, plan, PoisonProvider())
    assert result['data'] == [] and result['total'] == 0 and result['export_complete']
    assert result['semantic_status'] == 'not_run'


@pytest.mark.parametrize('operation,args', [
    ('unknown', {}), ('find_tb_instance', {}), ('get_tb_subtree', {'depth': True}),
    ('get_tb_class_hierarchy', {'max_nodes': 0}), ('lookup_tb_files', {}),
    ('lookup_tb_files', {'basename': '', 'contains_uvm': False}), ('get_tb_file_detail', {'path': 1}),
    ('dump_tb_section', {'section': 'unknown'}), ('dump_tb_section', {'section': 'files_full', 'extra': 1}),
])
def test_invalid_requests_fail_before_catalog_or_backend_selection(monkeypatch, operation, args):
    monkeypatch.setattr(requests, 'build_compile_source_catalog', forbidden)
    with pytest.raises(ValueError): prepare(monkeypatch, context(), operation, args)


def test_plan_copies_arguments_and_rejects_foreign_catalog_before_provider(monkeypatch):
    args = {'path': ALIAS}
    plan = prepare(monkeypatch, context(), 'get_tb_file_detail', args)
    args['path'] = B
    args['extra'] = True
    assert json.loads(plan.arguments_json) == {'path': ALIAS}
    with pytest.raises(FrozenInstanceError): plan.operation = 'lookup_tb_files'
    p = Provider(plan.budget)
    assert execute(monkeypatch, plan, p)['path'] == A
    foreign = replace(plan, catalog=catalog(identity='foreign-design'))
    before = list(p.events)
    with pytest.raises(ValueError): execute(monkeypatch, foreign, p)
    assert p.events == before


@pytest.mark.parametrize('operation,args', [
    ('lookup_tb_files', {'basename': 'a.sv'}),
    ('lookup_tb_files', {'has_module': 'top'}),
    ('dump_tb_section', {'section': 'compile_result'}),
])
def test_foreign_catalog_is_rejected_even_for_metadata_only_execution(monkeypatch, operation, args):
    plan = prepare(monkeypatch, context(), operation, args)
    foreign = replace(plan, catalog=catalog(identity='foreign-design'))
    with pytest.raises(ValueError): execute(monkeypatch, foreign, PoisonProvider())


@pytest.mark.parametrize('operation,args', [
    ('get_tb_file_detail', {'path': A}), ('lookup_tb_files', {'has_module': 'top'}),
    ('dump_tb_section', {'section': 'files_full'}),
])
def test_catalog_preparation_byte_stop_releases_catalog_and_disables_provider(monkeypatch, operation, args):
    plan = prepare(monkeypatch, context(), operation, args, budget=HierarchyQueryBudget(max_bytes=513))
    assert not plan.needs_provider and plan.catalog is None
    assert plan.preparation_limit == 'metadata_byte_limit'
    result = execute(monkeypatch, plan, PoisonProvider())
    assert result['error'] == 'hierarchy_query_incomplete' and result['semantic_status'] == 'not_run'
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['metadata_byte_limit']


def test_catalog_build_uses_remaining_cumulative_budget_and_charges_it_once(monkeypatch):
    budget, observed = HierarchyQueryBudget(max_bytes=8192), []
    budget.reserve_bytes(100)
    original = requests.build_compile_source_catalog
    def tracked(*a, **kw):
        observed.append(kw['max_bytes'])
        return original(*a, **kw)
    monkeypatch.setattr(requests, 'build_compile_source_catalog', tracked)
    plan = prepare(monkeypatch, context(), 'lookup_tb_files', {'has_module': 'top'}, budget=budget)
    assert observed == [7580]  # 8192 - earlier100 - reservation512.
    assert plan.needs_provider and plan.catalog is not None
    assert budget.fact_bytes == 100 + 512 + plan.catalog.metadata_bytes
    assert budget.file_count == budget.symbol_count == budget.call_count == 0


def test_cancellation_is_checked_before_returning_even_unstarted_plan(monkeypatch):
    plan = prepare(monkeypatch, context(), 'get_tb_file_detail', {'path': A},
                   budget=HierarchyQueryBudget(max_bytes=513))
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled): execute(monkeypatch, plan, PoisonProvider())
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize('section', SEMANTIC)
@pytest.mark.parametrize('error', [OperationCancelled('cancelled'), CaptureAborted('input_changed'),
                                   KdbBindingChanged(), TimeoutError('explicit provider timeout'), RuntimeError('provider failed')])
def test_export_provider_fatal_or_ordinary_failure_propagates_without_second_provider(monkeypatch, section, error):
    p = Provider()
    def hook(phase, label, index):
        if phase == 'next': raise error
    p.hook = hook
    with pytest.raises(type(error)) as caught:
        export(monkeypatch, context(), p, section=section, catalog=catalog())
    assert caught.value is error and p.active == 0
    assert p.events[-1][0] == 'close'


@pytest.mark.parametrize('operation,args', [
    ('get_tb_subtree', {'root': 'top', 'depth': -1}),
    ('find_tb_instance', {'module': 'leaf'}),
    ('get_tb_class_hierarchy', {}), ('get_tb_file_detail', {'path': A}),
    ('lookup_tb_files', {'has_module': 'top'}), ('dump_tb_section', {'section': 'files_full'}),
])
def test_request_execute_preserves_typed_provider_error_and_closes_resources(monkeypatch, operation, args):
    error = CaptureAborted('input_changed')
    plan = prepare(monkeypatch, context(), operation, args)
    p = Provider(plan.budget)
    def hook(phase, label, index):
        if phase == 'next': raise error
    p.hook = hook
    with pytest.raises(CaptureAborted) as caught: execute(monkeypatch, plan, p)
    assert caught.value is error and p.active == 0 and p.events[-1][0] == 'close'


def test_execute_deadline_expired_after_preparation_never_inspects_provider(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    plan = prepare(monkeypatch, context(), 'get_tb_file_detail', {'path': A},
                   budget=HierarchyQueryBudget(timeout_sec=1))
    assert plan.needs_provider
    clock[0] = 12.0
    result = execute(monkeypatch, plan, PoisonProvider())
    assert result['error'] == 'hierarchy_query_incomplete' and result['semantic_status'] == 'not_run'
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']


@pytest.mark.parametrize('error', [OperationCancelled('prepare cancel'), CaptureAborted('timeout'),
                                  KdbBindingChanged(), TimeoutError('typed prepare timeout')])
def test_preparation_checkpoint_fatal_is_not_a_budget_partial(monkeypatch, error):
    budget = HierarchyQueryBudget()
    def checkpoint(): raise error
    monkeypatch.setattr(budget, 'checkpoint', checkpoint)
    with pytest.raises(type(error)) as caught:
        prepare(monkeypatch, context(), 'get_tb_file_detail', {'path': A}, budget=budget)
    assert caught.value is error


@pytest.mark.parametrize('section', SEMANTIC)
def test_export_analysis_budget_keeps_observed_prefix_but_never_complete(monkeypatch, section):
    budget = HierarchyQueryBudget(max_files=1, max_symbols=1, max_scopes=1)
    p = Provider(budget)
    result = export(monkeypatch, context(), p, section=section, catalog=catalog())
    assert not result['export_complete'] and result['coverage_status'] == 'partial'
    assert result['analysis_limits'] and p.active == 0


@pytest.mark.parametrize('section', (*METADATA, *SEMANTIC))
def test_late_final_validation_invalidates_completeness_without_erasing_positive_data(monkeypatch, section):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    budget = HierarchyQueryBudget(timeout_sec=1)
    p = Provider(budget)
    result = export(monkeypatch, context(), p, section=section, catalog=catalog(), budget=budget)
    assert result['export_complete']
    original_data = result['data']
    clock[0] = 12.0
    with no_io(monkeypatch): finish_hierarchy_query(result, budget)
    assert result['data'] is original_data and not result['export_complete']
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']
    assert result['warning']
    if section == 'class_hierarchy_full':
        assert result['total'] == 2 and result['class_inventory_count'] is None
        assert not result['class_inventory_complete']
    elif section in ('files_full', 'interfaces'):
        assert result['total'] is None and result['count_lower_bound'] == 2
    elif section == 'component_tree_full': assert result['total_descendants'] is None
    else: assert result['record_export_complete'] and result['semantic_status'] == 'not_run'


@pytest.mark.parametrize('kind', ['detail_empty', 'lookup_empty', 'compile_miss'])
def test_late_finish_cannot_retain_complete_file_negative(monkeypatch, kind):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    budget = HierarchyQueryBudget(timeout_sec=1)
    p = Provider(budget)
    p.file_symbols[A] = []
    operation, args = ('lookup_tb_files', {'basename': 'a.sv', 'has_module': 'absent'}) if kind == 'lookup_empty' else (
        'get_tb_file_detail', {'path': A if kind == 'detail_empty' else '/absent/a.sv'})
    plan = prepare(monkeypatch, context(), operation, args, budget=budget)
    result = execute(monkeypatch, plan, p)
    clock[0] = 12.0
    finish_hierarchy_query(result, budget)
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']
    if kind == 'detail_empty': assert not result['symbol_inventory_complete'] and result['symbol_count'] is None
    elif kind == 'lookup_empty': assert result['total'] is None and result['count_status'] == 'lower_bound'
    else: assert result['error'] == 'file_query_incomplete'
