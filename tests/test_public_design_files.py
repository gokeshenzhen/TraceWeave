"""Public file tools: real captured membership, handwritten semantic controls.

No SDK or frontend is executed. Positive fake declarations and loaded labels
test routing/ownership/serialization only; real cc31 acceptance is separate.
Two canonical shared.sv versions and one captured alias must never collapse.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import os
import threading
from types import SimpleNamespace

import anyio
import jsonschema
import pytest

import server
from src import connectivity_backend, design_backend_binding, hierarchy_source_ir as ir
from src import slang_hierarchy_source
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.connectivity_ir import SourceLocation
from src.design_backend_binding import ExecutionBindingChanged
from src.hierarchy_source_symbols import SourceSymbol
from src.hierarchy_source_request import prepare_source_query
from src.kdb_anchor import KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable
from tests.test_public_design_instance import anyio_backend, isolate, forbidden, prepare, public


TOOLS = ('get_tb_file_detail', 'lookup_tb_files')
FILE_GAPS = {'npi_uninstantiated_declaration_inventory_unverified',
             'npi_file_variable_inventory_unverified', 'npi_file_import_inventory_unverified',
             'npi_uvm_usage_inventory_unverified'}


def project(root, *, candidate=True):
    (root / 'v1').mkdir()
    (root / 'v2').mkdir()
    first, second, third = root / 'v1/shared.sv', root / 'v2/shared.sv', root / 'third.sv'
    first.write_text('module top; endmodule\nmodule alpha; endmodule\n')
    second.write_text('module beta; endmodule\n')
    third.write_text('package p; class Monitor; endclass endpackage\n')
    alias = root / 'first_alias.sv'
    alias.symlink_to(first)
    log = root / 'comp.log'
    paths = (alias, second, third)
    log.write_text('Chronologic VCS simulator\n'
                   + 'Command: vcs -sverilog ' + ' '.join(map(str, paths)) + ' -top top\n'
                   + ''.join(f"Parsing design file '{path}'\n" for path in paths)
                   + 'Top Level Modules:\n       top\n')
    maps = root / 'simv.daidir/kdb.elab++'
    if candidate:
        maps.mkdir(parents=True)
        (maps / '_libFileMap').write_bytes(b'map-A')
        (maps / '_stnElabDB').write_bytes(b'elab-A')
    return first, second, third, alias, log, maps


class Facts:
    origin = 'verdi_npi'

    def __init__(self, controls, identity):
        self.controls, self.artifact_identity = controls, identity

    @contextmanager
    def open_file_symbols(self, entry, budget):
        controls = self.controls
        controls.opened.append(entry.path)
        budget.call()
        budget.admit_namespace('module', 'control_namespace', 0)
        symbols = controls.symbols[entry.path]
        if controls.foreign_symbol:
            symbols = [controls.symbols[str(controls.files[1])][0]]

        class Stream:
            complete, gaps = False, tuple(sorted(FILE_GAPS))
            has_uvm_import = None
            uses_uvm = True if entry.path == str(controls.files[0]) else None
            work_scope = 'global_namespaces_bounded'
            def __init__(self): self.index = 0
            def __iter__(self): return self
            def __next__(self):
                budget.call()
                if controls.stream_action: controls.stream_action(entry.path, self.index)
                if self.index == len(symbols): raise StopIteration
                symbol = symbols[self.index]
                self.index += 1
                return symbol
        try:
            yield Stream()
        finally:
            controls.closed.append(entry.path)


class Controls:
    def __init__(self):
        self.files = self.alias = None
        self.symbols = {}
        self.requests, self.opened, self.closed = [], [], []
        self.selections = 0
        self.stream_action = self.after = self.transform = self.selector_action = None
        self.foreign_symbol = False
        self.thread = threading.get_ident()

    def configure(self, files, alias):
        self.files, self.alias = files, alias
        first, second, third = map(str, files)
        self.symbols = {
            first: [SourceSymbol('decl-alpha', 'alpha', 'alpha', 'module', SourceLocation(str(alias), 2),
                                 declaration_id='decl-alpha', parameterization=(('WIDTH', '8'),),
                                 declaration_status='specialization')],
            second: [SourceSymbol('decl-beta', 'beta', 'beta', 'module', SourceLocation(second, 1),
                                  declaration_id='decl-beta', parameterization=(('WIDTH', '16'),),
                                  declaration_status='specialization')],
            third: [SourceSymbol('decl-monitor', 'Monitor', 'p.Monitor', 'class', SourceLocation(third, 1),
                                 declaration_id='decl-monitor')],
        }

    def select(self, status, *, fallback):
        self.selections += 1
        if self.selector_action: self.selector_action()
        controls = self
        class Backend:
            name, uses_external_worker, kdb_status = 'verdi_npi', False, None
            def bind_compile_context(self, metadata): self.compile_result = deepcopy(metadata)
            def bind_kdb_anchor(self, anchor): self.anchor = anchor
            def query_source(self, request):
                assert threading.get_ident() == controls.thread
                assert request.operation in TOOLS
                assert request.needs_provider and request.context.compile_result() == self.compile_result
                controls.requests.append(request)
                identity = hashlib.sha256((self.anchor.identity_sha256 + '\0' + request.context.selected_top).encode()).hexdigest()
                value = request.execute(Facts(controls, identity))
                self.kdb_status = {'load_quality': 'clean'}
                value['context_identity_sha256'] = request.context.identity_sha256
                value['kdb_binding'] = dict(self.anchor.receipt(), load_status='loaded', load_quality='clean')
                if controls.after: controls.after()
                if controls.transform: controls.transform(value)
                return value
        return Backend()


@pytest.fixture
def controls(monkeypatch):
    value = Controls()
    monkeypatch.setattr(connectivity_backend, 'select_backend', value.select)
    return value


async def setup(tmp_path, controls, **kwargs):
    first, second, third, alias, log, maps = project(tmp_path)
    controls.configure((first, second, third), alias)
    handle = await prepare(log, **kwargs)
    return handle, first, second, third, alias, log, maps


async def detail(handle, /, path, **kwargs):
    return await public('get_tb_file_detail', design_handle=handle, path=str(path), **kwargs)


async def lookup(handle, /, **kwargs):
    return await public('lookup_tb_files', design_handle=handle, **kwargs)


def no_metadata_backend(monkeypatch):
    for module, name in ((design_backend_binding, 'prepare_backend_binding'),
                         (design_backend_binding, 'probe_verdi_backend'),
                         (design_backend_binding, 'capture_kdb_anchor'),
                         (connectivity_backend, 'select_backend')):
        monkeypatch.setattr(module, name, forbidden)


def metadata_result(value, handle):
    assert value['facts_origin'] == 'compile_metadata' and value['semantic_status'] == 'not_run'
    assert value['design_handle'] == handle and len(value['context_identity_sha256']) == 64
    assert not {'backend_status', 'kdb_binding', 'design_context'} & value.keys()


@pytest.mark.anyio
@pytest.mark.parametrize('name', TOOLS)
async def test_public_file_schemas_allow_one_context_and_keep_legacy_handle(name):
    schema = next(tool.inputSchema for tool in await server.list_tools() if tool.name == name)
    arguments = {'path': '/captured/input.sv'} if name == 'get_tb_file_detail' else {'basename': 'input.sv'}
    for selector in ({'design_handle': 'dc_example'}, {'handle': 'tbh_example'}):
        jsonschema.validate({**selector, **arguments}, schema)
    for selector in ({}, {'design_handle': 'dc_example', 'handle': 'tbh_example'}):
        with pytest.raises(jsonschema.ValidationError): jsonschema.validate({**selector, **arguments}, schema)


@pytest.mark.anyio
@pytest.mark.parametrize('preference', ['auto', 'source'])
async def test_metadata_filters_preserve_both_versions_and_never_acquire_backend(tmp_path, monkeypatch, controls, preference):
    handle, first, second, _, _, _, _ = await setup(tmp_path, controls, backend_preference=preference)
    no_metadata_backend(monkeypatch)
    value = await lookup(handle, basename='shared.sv')
    metadata_result(value, handle)
    assert [row['path'] for row in value['matches']] == [str(first), str(second)]
    assert value['total'] == value['count_lower_bound'] == 2 and value['count_status'] == 'exact'
    assert value['ownership_identity'] == value['context_identity_sha256']
    for row in value['matches']:
        for key in ('file_type', 'modules', 'classes', 'interfaces', 'symbols', 'contains_uvm', 'has_uvm_import', 'symbol_count'):
            assert key in row and row[key] is None
        assert row['source_status'] == 'captured' and row['semantic_status'] == 'not_run'
    narrowed = await lookup(handle, basename='shared.sv', path_contains='/v2/', name_contains='shared')
    assert [row['path'] for row in narrowed['matches']] == [str(second)]
    assert not controls.requests and not controls.opened and controls.selections == 0


@pytest.mark.anyio
async def test_metadata_bounded_display_and_exact_count_are_independent(tmp_path, monkeypatch, controls):
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    for mode, total, status in [('bounded', None, 'lower_bound'), ('exact', 2, 'exact')]:
        value = await lookup(handle, basename='shared.sv', limit=1, count_mode=mode)
        metadata_result(value, handle)
        assert [row['path'] for row in value['matches']] == [str(first)]
        assert 'total' in value and value['total'] == total and value['count_lower_bound'] == 2
        assert value['count_status'] == status and value['output_truncated'] is True


@pytest.mark.anyio
async def test_nonmember_same_basename_suggests_canonical_versions_without_native_work(tmp_path, monkeypatch, controls):
    handle, first, second, _, _, _, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    value = await detail(handle, tmp_path / 'not_compiled/shared.sv')
    metadata_result(value, handle)
    assert value['error'] == 'file_not_in_compile_set'
    assert value['did_you_mean'] == [str(first), str(second)]
    assert 'symbols' not in value and controls.requests == []


@pytest.mark.anyio
async def test_empty_metadata_candidate_with_semantic_filter_is_still_not_run(tmp_path, monkeypatch, controls):
    handle, _, _, _, _, _, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    value = await lookup(handle, basename='absent.sv', has_module='alpha')
    metadata_result(value, handle)
    assert value['matches'] == [] and value['total'] == 0 and value['unknown_filter_files'] == 0
    assert value['semantic_file_queries'] == 0 and controls.requests == []


@pytest.mark.anyio
async def test_member_alias_returns_canonical_owned_positive_partial_facts(tmp_path, controls):
    handle, first, _, _, alias, _, _ = await setup(tmp_path, controls)
    value = await detail(handle, alias)
    server.schemas.SourceFileDetailResult.model_validate(value)
    assert value['path'] == str(first) and str(alias) in value['aliases']
    assert value['ownership_identity'] == value['context_identity_sha256']
    assert value['modules'] == ['alpha'] and value['file_type'] == 'module'
    assert value['symbol_count_lower_bound'] == 1 and value['symbol_count'] is None
    assert value['symbol_inventory_complete'] is False and value['semantic_status'] == 'partial'
    assert value['semantic_work_scope'] == 'global_namespaces_bounded'
    assert FILE_GAPS <= set(value['semantic_gaps']) and value['source_status'] == 'captured'
    assert value['contains_uvm'] is True and value['has_uvm_import'] is None
    symbol = value['symbols'][0]
    assert {key: symbol[key] for key in ('symbol_id', 'name', 'kind', 'source_file', 'source_line', 'parameterization')} == {
        'symbol_id': 'decl-alpha', 'name': 'alpha', 'kind': 'module', 'source_file': str(alias),
        'source_line': 2, 'parameterization': {'WIDTH': '8'}}
    assert value['backend_status']['actual_backend'] == 'verdi_npi'
    assert value['backend_status']['design_context']['artifact']['source_association'] == 'unverified'
    assert controls.opened == controls.closed == [str(first)]


@pytest.mark.anyio
async def test_semantic_filters_keep_partial_unknowns_and_and_metadata_selection(tmp_path, controls):
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls)
    all_versions = await lookup(handle, basename='shared.sv', has_module='alpha')
    assert [row['path'] for row in all_versions['matches']] == [str(first)]
    assert all_versions['unknown_filter_files'] == 1 and all_versions['total'] is None
    assert all_versions['count_lower_bound'] == 1 and all_versions['coverage_status'] == 'partial'
    restricted = await lookup(handle, basename='shared.sv', path_contains='/v1/', has_module='alpha', contains_uvm=True)
    assert [row['path'] for row in restricted['matches']] == [str(first)]
    assert restricted['semantic_file_queries'] == 1 and restricted['unknown_filter_files'] == 0
    unknown_false = await lookup(handle, basename='shared.sv', path_contains='/v2/', contains_uvm=False)
    assert unknown_false['matches'] == [] and unknown_false['unknown_filter_files'] == 1
    assert unknown_false['count_status'] == 'lower_bound' and unknown_false['total'] is None
    assert sorted(controls.opened) == sorted(controls.closed)


@pytest.mark.anyio
async def test_partial_class_observation_does_not_prove_absence_of_higher_priority_module(tmp_path, controls):
    handle, _, _, third, _, _, _ = await setup(tmp_path, controls)
    value = await detail(handle, third)
    assert value['classes'] == ['p.Monitor'] and value['file_type'] is None
    filtered = await lookup(handle, basename='third.sv', file_type='class')
    assert filtered['matches'] == [] and filtered['unknown_filter_files'] == 1 and filtered['total'] is None


@pytest.mark.anyio
@pytest.mark.parametrize('name,arguments', [
    ('get_tb_file_detail', {}), ('get_tb_file_detail', {'path': ''}), ('get_tb_file_detail', {'path': 1}),
    ('get_tb_file_detail', {'path': '/a', 'handle': 'tbh_old'}),
    ('get_tb_file_detail', {'path': '/a', 'unexpected': 1}),
    ('lookup_tb_files', {}), ('lookup_tb_files', {'basename': ''}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'has_module': ''}),
    ('lookup_tb_files', {'contains_uvm': 1}), ('lookup_tb_files', {'file_type': False}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'limit': True}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'limit': 0}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'limit': 5001}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'count_mode': 'guess'}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'timeout_sec': float('inf')}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'timeout_sec': True}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'timeout_sec': 0}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'timeout_sec': 301}),
    ('lookup_tb_files', {'basename': 'shared.sv', 'compile_log': '/foreign.log'}),
])
async def test_invalid_public_file_queries_fail_before_any_binding(tmp_path, monkeypatch, controls, name, arguments):
    handle, _, _, _, _, _, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    value = await public(name, design_handle=handle, **arguments)
    assert value['error_code'] == 'invalid_design_file_arguments'
    assert 'matches' not in value and 'symbols' not in value
    assert controls.requests == [] and controls.selections == 0


@pytest.mark.anyio
@pytest.mark.parametrize('corruption', ['canonical_other', 'alias_path', 'alias_smuggle', 'ownership', 'context', 'foreign_symbol'])
async def test_schema_legal_cross_file_or_context_payload_is_rejected(tmp_path, controls, corruption):
    handle, first, second, _, alias, _, _ = await setup(tmp_path, controls)
    def corrupt(value):
        if corruption == 'canonical_other':
            value['path'] = str(second)
            value['aliases'] = [str(alias)]  # Keeps source row schema legal.
        elif corruption == 'alias_path':
            value['path'] = str(alias)
            value['aliases'] = [str(first), str(alias)]
        elif corruption == 'alias_smuggle':
            value['aliases'].append('/outside/unobserved.sv')
            value['symbols'][0]['source_file'] = '/outside/unobserved.sv'
        elif corruption == 'ownership': value['ownership_identity'] = 'foreign-compile'
        elif corruption == 'context': value['context_identity_sha256'] = '0' * 64
        else:
            value['symbols'][0]['source_file'] = str(second)
            value['aliases'].append(str(second))
        # These are valid SourceFileRow shapes. Their mismatch to the captured
        # request, rather than basic schema validation, must cause rejection.
        server.schemas.SourceFileDetailResult.model_validate(value)
    controls.transform = corrupt
    value = await detail(handle, alias)
    assert 'error' in value and 'symbols' not in value
    assert len(controls.requests) == controls.selections == 1


@pytest.mark.anyio
async def test_provider_cannot_emit_a_symbol_from_another_compile_member(tmp_path, monkeypatch, controls):
    # This ownership/cleanup control deliberately has no semantic fallback.
    # Real ordinary-NPI-to-Slang fallback has its own frontend acceptance case.
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls)
    controls.foreign_symbol = True
    value = await detail(handle, first)
    assert 'symbols' not in value or value['symbols'] is None
    assert value.get('facts_origin') != 'verdi_npi'
    assert sorted(controls.opened) == sorted(controls.closed)


@pytest.mark.anyio
@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('kind,code', [('cancel', None), ('timeout', 'design_context_timeout'),
    ('source', 'design_context_changed'), ('kdb', 'kdb_identity_changed'), ('policy', 'design_context_execution_changed')])
async def test_typed_stream_failure_never_becomes_fallback_file_facts(tmp_path, controls, tool, kind, code):
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls)
    error = {'cancel': OperationCancelled(), 'timeout': TimeoutError('native timeout'),
             'source': CaptureAborted('input_changed'), 'kdb': KdbBindingChanged(),
             'policy': ExecutionBindingChanged()}[kind]
    def fail(path, index): raise error
    controls.stream_action = fail
    if kind == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            if tool == 'get_tb_file_detail': await detail(handle, first)
            else: await lookup(handle, basename='shared.sv', has_module='alpha')
    else:
        value = (await detail(handle, first) if tool == 'get_tb_file_detail'
                 else await lookup(handle, basename='shared.sv', has_module='alpha'))
        assert value['error_code'] == code and 'symbols' not in value and 'matches' not in value
    assert len(controls.requests) == 1 and controls.opened
    assert sorted(controls.opened) == sorted(controls.closed)


@pytest.mark.anyio
@pytest.mark.parametrize('change,code', [('source', 'design_context_changed'), ('map', 'kdb_identity_changed'),
                                       ('reset', 'design_context_generation_changed')])
async def test_positive_file_facts_are_discarded_on_final_identity_failure(tmp_path, controls, change, code):
    handle, first, _, _, _, _, maps = await setup(tmp_path, controls)
    def mutate():
        if change == 'source': first.write_text('module changed; endmodule\n')
        elif change == 'map': (maps / '_libFileMap').write_bytes(b'changed maps')
        else: server.reset_session_state()
    controls.after = mutate
    value = await detail(handle, first)
    assert value['error_code'] == code and 'symbols' not in value


@pytest.mark.anyio
async def test_late_semantic_validation_keeps_positive_file_rows_but_no_completion_proof(tmp_path, monkeypatch, controls):
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls)
    original = server._validate_design_binding
    async def validate(binding, **kwargs):
        await original(binding, **kwargs)
        if controls.requests: controls.requests[-1].budget.deadline = -1
    monkeypatch.setattr(server, '_validate_design_binding', validate)
    value = await detail(handle, first)
    assert value['symbols'][0]['name'] == 'alpha' and value['symbol_count'] is None
    assert value['symbol_inventory_complete'] is False and value['coverage_status'] == 'partial'
    assert 'timeout_sec' in value['analysis_limits']


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['miss', 'lookup'])
async def test_metadata_final_deadline_drops_exact_negative_or_exact_total(tmp_path, monkeypatch, controls, operation):
    handle, _, _, _, _, _, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    budgets, validations = [], []
    original_init, original_validate = ir.HierarchyQueryBudget.__init__, server._validate_design_source
    def initialize(budget, *args, **kwargs):
        original_init(budget, *args, **kwargs)
        budgets.append(budget)
    async def validate(context, **kwargs):
        await original_validate(context, **kwargs)
        validations.append(1)
        if len(validations) >= 2:
            assert budgets
            budgets[-1].deadline = -1
    monkeypatch.setattr(ir.HierarchyQueryBudget, '__init__', initialize)
    monkeypatch.setattr(server, '_validate_design_source', validate)
    value = (await detail(handle, '/nonmember/shared.sv') if operation == 'miss'
             else await lookup(handle, basename='shared.sv'))
    assert value['coverage_status'] == 'partial' and 'timeout_sec' in value['analysis_limits']
    if operation == 'miss': assert value['error'] == 'file_query_incomplete'
    else: assert value['total'] is None and value['count_status'] == 'lower_bound'
    assert not controls.requests


@pytest.mark.anyio
async def test_metadata_pin_is_held_until_final_validation_and_reset_prevents_success(tmp_path, monkeypatch, controls):
    handle, _, _, _, _, _, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    entered, release, validations, output = anyio.Event(), anyio.Event(), [], []
    original = server._validate_design_source
    async def validate(context, **kwargs):
        await original(context, **kwargs)
        validations.append(1)
        if len(validations) >= 2:
            entered.set()
            await release.wait()
    monkeypatch.setattr(server, '_validate_design_source', validate)
    async def request(): output.append(await lookup(handle, basename='shared.sv'))
    async with anyio.create_task_group() as group:
        group.start_soon(request)
        with anyio.fail_after(5): await entered.wait()
        assert server._design_context_store.remove(handle) is False
        server.reset_session_state()
        release.set()
    assert output[0]['error_code'] == 'design_context_generation_changed' and 'matches' not in output[0]


@pytest.mark.anyio
async def test_capture_bodies_are_released_after_metadata_and_semantic_calls(tmp_path, monkeypatch, controls):
    captures = []
    original = CompileInputCapture.__init__
    def initialize(capture, *args, **kwargs):
        original(capture, *args, **kwargs)
        captures.append(capture)
    monkeypatch.setattr(CompileInputCapture, '__init__', initialize)
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls)
    await lookup(handle, basename='shared.sv')
    await detail(handle, first)
    assert captures and all(capture.body_count == capture.resident_bytes == 0 for capture in captures)


@pytest.mark.anyio
async def test_metadata_after_native_success_does_not_validate_or_inherit_its_kdb(tmp_path, monkeypatch, controls):
    handle, first, _, _, _, _, maps = await setup(tmp_path, controls)
    initial = await detail(handle, first)
    assert initial['backend_status']['design_context']['artifact']['load_status'] == 'loaded'
    (maps / '_libFileMap').write_bytes(b'changed only KDB maps')
    no_metadata_backend(monkeypatch)
    monkeypatch.setattr(server, '_validate_design_binding', forbidden)
    value = await lookup(handle, basename='shared.sv')
    metadata_result(value, handle)
    assert value['total'] == 2 and len(controls.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('reason', ['source', 'unavailable'])
async def test_unavailable_semantics_keep_owned_metadata_and_unknown_symbol_inventory(tmp_path, monkeypatch, controls, reason):
    # An unavailable frontend retains metadata. Enabled Slang has independent
    # positive acceptance in test_slang_source_services.py.
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls,
        backend_preference='source' if reason == 'source' else 'auto')
    if reason == 'unavailable':
        def fail(path, index): raise NpiHierarchyUnavailable('npi_load_failed')
        controls.stream_action = fail
    value = await detail(handle, first)
    assert value['path'] == str(first) and value['ownership_identity'] == value['context_identity_sha256']
    assert value['symbols'] is None and value['modules'] is None and value['file_type'] is None
    assert value['semantic_status'] == 'unavailable' and value['coverage_status'] == 'partial'
    assert value['facts_origin'] == 'compile_metadata' and value['symbol_inventory_complete'] is False
    assert value['backend_status'].get('actual_backend') is None
    if reason == 'source': assert controls.requests == [] and controls.selections == 0


def no_source_planning(monkeypatch):
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '1')
    monkeypatch.setattr(slang_hierarchy_source, 'build_instance_plan', forbidden)
    monkeypatch.setattr(server, 'get_source_graph_runtime', forbidden)


async def partial_source_setup(tmp_path, controls, *, preference='source', kind='missing', candidate=False):
    first, second, third, alias, log, maps = project(tmp_path, candidate=candidate)
    controls.configure((first, second, third), alias)
    if kind == 'missing':
        third.unlink()
    handle = await prepare(log, backend_preference=preference, **({'max_files': 1} if kind == 'budget' else {}))
    return handle, first, second, third, alias, log, maps


def partial_file_metadata(value, path, source_status):
    assert value['path'] == str(path) and value['source_status'] == source_status
    assert value['facts_origin'] == 'compile_metadata' and value['semantic_status'] == 'unavailable'
    assert value['coverage_status'] == 'partial' and 'source_capture_incomplete' in value['coverage_gaps']
    assert value['ownership_identity'] == value['context_identity_sha256']
    assert value['symbols'] is None and value['contains_uvm'] is None and value['has_uvm_import'] is None
    assert value['file_type'] is None and value['symbol_inventory_complete'] is False
    assert value['symbol_count'] is None and value['symbol_count_lower_bound'] == 0
    assert value['backend_status'].get('actual_backend') is None
    assert not value['backend_status'].get('source_graph')


@pytest.mark.anyio
@pytest.mark.parametrize('preference', ['source', 'auto'])
@pytest.mark.parametrize('kind', ['missing', 'budget'])
async def test_stable_partial_source_capture_retains_metadata_and_unknown_filters(
    tmp_path, monkeypatch, controls, preference, kind,
):
    handle, first, _, third, _, _, _ = await partial_source_setup(
        tmp_path, controls, preference=preference, kind=kind)
    no_source_planning(monkeypatch)
    with server._design_context_store.pin(handle) as entry:
        context = entry.context
        # A source-stage file cap can leave an internally complete snapshot
        # of the log alone. It still cannot authorize semantic source replay.
        assert context.snapshot.complete is (kind == 'budget')
        gap = 'compile_input_unavailable' if kind == 'missing' else 'source_capture_file_limit'
        assert gap in context.gaps and context.current()
    for _ in range(2):
        missing = await detail(handle, third)
        partial_file_metadata(missing, third, 'unavailable' if kind == 'missing' else 'not_captured')
        assert gap in missing['coverage_gaps']
    available = await detail(handle, first)
    partial_file_metadata(available, first, 'captured' if kind == 'missing' else 'not_captured')
    for filters in ({'has_module': 'alpha'}, {'contains_uvm': False}, {'file_type': 'module'}):
        value = await lookup(handle, basename='shared.sv', count_mode='exact', **filters)
        assert value['matches'] == [] and value['unknown_filter_files'] == 2
        assert value['total'] is None and value['count_status'] == 'lower_bound'
        assert value['count_lower_bound'] == 0 and value['semantic_file_queries'] == 0
        assert value['semantic_status'] == 'unavailable' and value['coverage_status'] == 'partial'
        assert {'source_capture_incomplete', gap} <= set(value['coverage_gaps'])
        assert not value['backend_status'].get('source_graph')
    assert controls.requests == [] and controls.opened == []


@pytest.mark.anyio
async def test_missing_source_keeps_healthy_native_semantic_opportunity(tmp_path, monkeypatch, controls):
    handle, _, _, third, _, _, _ = await partial_source_setup(
        tmp_path, controls, preference='auto', candidate=True)
    no_source_planning(monkeypatch)
    value = await detail(handle, third)
    assert value['path'] == str(third) and value['source_status'] == 'unavailable'
    assert value['facts_origin'] == 'verdi_npi' and value['classes'] == ['p.Monitor']
    assert value['symbols'][0]['symbol_id'] == 'decl-monitor'
    assert value['backend_status']['actual_backend'] == 'verdi_npi'
    assert 'source_capture_incomplete' not in value['coverage_gaps']
    assert len(controls.requests) == 1 and controls.opened == controls.closed == [str(third)]


@pytest.mark.anyio
@pytest.mark.parametrize('when', ['before_query', 'after_query'])
@pytest.mark.parametrize('change', ['appeared', 'rewritten', 'deleted', 'alias'])
async def test_partial_capture_rejects_input_changes_before_metadata_publication(
    tmp_path, monkeypatch, controls, when, change,
):
    handle, first, second, third, alias, _, _ = await partial_source_setup(tmp_path, controls)
    no_source_planning(monkeypatch)
    def mutate():
        if change == 'appeared':
            third.write_text('package p; class Monitor; endclass endpackage\n')
        elif change == 'rewritten':
            before = second.stat()
            second.write_text(second.read_text().replace('beta', 'zeta'))
            os.utime(second, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == 'deleted':
            second.unlink()
        else:
            alias.unlink()
            alias.symlink_to(second)
    if when == 'before_query':
        mutate()
    else:
        original = server._query_slang_hierarchy
        async def query(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result[0]['facts_origin'] == 'compile_metadata'
            mutate()
            return result
        monkeypatch.setattr(server, '_query_slang_hierarchy', query)
    value = await detail(handle, third)
    assert value['error_code'] == 'design_context_changed'
    assert not {'path', 'symbols', 'matches', 'artifact_identity'} & value.keys()
    assert controls.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize('gap', [
    'source_capture_byte_limit', 'source_capture_file_limit',
    'source_capture_reference_limit', 'source_capture_dependency_work_limit',
])
async def test_source_capture_limit_with_complete_record_snapshot_blocks_file_replay(
    tmp_path, monkeypatch, controls, gap,
):
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls, backend_preference='source')
    no_source_planning(monkeypatch)
    with server._design_context_store.pin(handle) as entry:
        # Controlled preparation receipt: the raw record snapshot is complete,
        # but preparation stopped before proving the complete capture closure.
        context = replace(entry.context, gaps=(gap,))
        assert context.snapshot.complete
        plan = prepare_source_query(context, 'get_tb_file_detail', {'path': str(first)})
        result, receipt = await server._query_slang_hierarchy(
            SimpleNamespace(context=context), plan, {'path': str(first)}, plan.budget)
    assert receipt is None and result['path'] == str(first) and result['symbols'] is None
    assert result['semantic_status'] == 'unavailable'
    assert {'source_capture_incomplete', gap} <= set(result['coverage_gaps'])


@pytest.mark.anyio
@pytest.mark.parametrize('gap', ['opaque_vhdl_boundary', 'compile_options_incomplete', 'include_inventory_incomplete'])
async def test_dependency_gaps_alone_do_not_trigger_source_capture_guard(tmp_path, monkeypatch, controls, gap):
    handle, first, _, _, _, _, _ = await setup(tmp_path, controls, backend_preference='source')
    # Disabled provider is a later, distinct decision. Existing real opaque
    # language tests separately require positive Slang provenance.
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    with server._design_context_store.pin(handle) as entry:
        context = replace(entry.context, gaps=(gap,))
        assert context.snapshot.complete
        plan = prepare_source_query(context, 'get_tb_file_detail', {'path': str(first)})
        result, receipt = await server._query_slang_hierarchy(
            SimpleNamespace(context=context), plan, {'path': str(first)}, plan.budget)
    assert receipt is None and 'source_hierarchy_provider_unavailable' in result['coverage_gaps']
    assert 'source_capture_incomplete' not in result['coverage_gaps']


@pytest.mark.anyio
@pytest.mark.parametrize('corruption', ['unknown_member', 'foreign_ownership', 'alias_smuggle'])
async def test_semantic_lookup_rows_must_belong_to_the_same_captured_catalog(tmp_path, controls, corruption):
    handle, _, _, _, _, _, _ = await setup(tmp_path, controls)
    def corrupt(value):
        row = value['matches'][0]
        if corruption == 'unknown_member': row['path'] = '/not_compiled/shared.sv'
        elif corruption == 'foreign_ownership': value['ownership_identity'] = 'other-design'
        else: row['aliases'].append('/other/unobserved.sv')
        server.schemas.SourceFileLookupResult.model_validate(value)
    controls.transform = corrupt
    value = await lookup(handle, basename='shared.sv', has_module='alpha')
    assert 'error' in value and 'matches' not in value
    assert len(controls.requests) == controls.selections == 1


@pytest.mark.anyio
async def test_two_legacy_file_tools_preserve_their_original_shapes(controls):
    path, handle = '/legacy/old.sv', 'tbh_control'
    server._handle_store.register(handle, {'_scan_results': [{
        'path': path, 'name': 'old.sv', 'type': 'module', 'modules': ['old'],
        'classes': [], 'interfaces': [], 'source_text': ''}]})
    found = await public('lookup_tb_files', handle=handle, basename='old.sv')
    assert found == {'handle': handle, 'matches': [{'path': path, 'file_type': 'module',
        'modules': ['old'], 'classes': [], 'has_uvm_import': False}], 'total': 1, 'truncated': False}
    inspected = await public('get_tb_file_detail', handle=handle, path=path)
    assert inspected == {'handle': handle, 'path': path, 'file_type': 'module',
        'symbols': [{'name': 'old', 'kind': 'module', 'line': 0}], 'includes': [], 'has_uvm_import': False}
    assert controls.selections == 0 and controls.requests == []
