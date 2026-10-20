"""Last two public source tools, using independent handwritten semantic facts.

Five declared classes plus one reference-only base render six rows. Class and
file inventories deliberately remain partial. A tiny three-scope instance
tree is separately complete. Fake load labels are control evidence, not NPI,
runtime UVM, source/KDB association or performance acceptance.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import threading

import anyio
import jsonschema
import pytest

import server
from src import connectivity_backend, design_backend_binding, hierarchy_source_ir as ir
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.connectivity_ir import SourceLocation
from src.design_backend_binding import ExecutionBindingChanged
from src.hierarchy_source_ir import HierarchyScope, ScopeLookup, HierarchyBudgetExceeded
from src.hierarchy_source_symbols import ClassFact, SourceSymbol
from src.kdb_anchor import KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable
from tests.test_public_design_instance import anyio_backend, isolate, forbidden, prepare, public
from tests.test_public_design_files import project, no_metadata_backend


METADATA = ('compile_result', 'include_tree', 'filelist_tree')
SEMANTIC = ('files_full', 'interfaces', 'component_tree_full', 'class_hierarchy_full')
CLASS_GAPS = ('npi_class_specialization_inventory_unverified', 'npi_uninstantiated_declaration_inventory_unverified')
FILE_GAPS = ('npi_file_import_inventory_unverified', 'npi_file_variable_inventory_unverified',
             'npi_uninstantiated_declaration_inventory_unverified', 'npi_uvm_usage_inventory_unverified')


class Provider:
    origin, top = 'verdi_npi', 'top'

    def __init__(self, controls, identity):
        self.controls, self.artifact_identity = controls, identity
        self.scopes = {
            'top': HierarchyScope('scope-top', 'top', 'top', 'module', None, 'def-top', 'top',
                SourceLocation('/sdk/cwd/instances.sv', 484), SourceLocation(str(controls.first), 1)),
            'top.g[0]': HierarchyScope('scope-gen', 'top.g[0]', 'g[0]', 'generate', 'top',
                                     location=SourceLocation(str(controls.alias), 2)),
            'top.g[0].u': HierarchyScope('scope-leaf', 'top.g[0].u', 'u', 'module', 'top.g[0]',
                                       'def-leaf', 'leaf', definition_location=SourceLocation('/outside/leaf.sv', 1)),
        }
        self.children = {'top': (self.scopes['top.g[0]'],), 'top.g[0]': (self.scopes['top.g[0].u'],)}

    def lookup_scope(self, path, budget):
        budget.call()
        self.controls.lookups.append(path)
        return ScopeLookup(self.scopes.get(path), True)

    @contextmanager
    def stream(self, label, facts, budget, complete, gaps):
        controls = self.controls
        controls.opened.append(label)
        budget.call()
        class Stream:
            has_uvm_import = uses_uvm = None
            work_scope = 'global_namespaces_bounded'
            def __init__(self):
                self.index, self.complete, self.gaps = 0, False, gaps
            def __iter__(self): return self
            def __next__(self):
                budget.call()
                if controls.on_next: controls.on_next(label, self.index)
                if self.index == len(facts):
                    self.complete = complete
                    raise StopIteration
                fact = facts[self.index]
                self.index += 1
                return fact
        try:
            yield Stream()
        finally:
            controls.closed.append(label)

    def open_children(self, scope, budget):
        return self.stream(scope.path, self.children.get(scope.path, ()), budget, True, ())

    def open_classes(self, budget):
        return self.stream('classes', self.controls.classes, budget,
                           self.controls.complete_classes, () if self.controls.complete_classes else CLASS_GAPS)

    def open_file_symbols(self, entry, budget):
        return self.stream(entry.path, self.controls.files[entry.path], budget, False, FILE_GAPS)


class Controls:
    def __init__(self):
        self.requests, self.opened, self.closed, self.lookups = [], [], [], []
        self.selections = 0
        self.complete_classes = False
        self.on_next = self.after = self.transform = None
        self.thread = threading.get_ident()

    def configure(self, first, second, third, alias):
        self.first, self.second, self.third, self.alias = first, second, third, alias
        def symbol(identity, name, qualified, path, kind='class', gaps=()):
            return SourceSymbol(identity, name, qualified, kind,
                SourceLocation(str(path), 7) if path is not None else None,
                declaration_id=identity, gaps=gaps)
        lb = symbol('class-LB', 'Base', 'left.Base', alias)
        rb = symbol('class-RB', 'Base', 'right.Base', second)
        child = symbol('class-Child', 'Child', 'left.Child', '/sdk/cwd/child.sv')
        ext = symbol('class-External', 'ExternalChild', 'left.ExternalChild', first)
        unknown = symbol('class-Unknown', 'UnknownChild', 'right.UnknownChild', None,
                         gaps=('npi_class_location_unavailable',))
        self.classes = [ClassFact(lb, 'none'), ClassFact(rb, 'none'),
            ClassFact(child, 'resolved', 'class-RB', 'right.Base'),
            ClassFact(ext, 'resolved', 'external-base', 'external.Base'),
            ClassFact(unknown, 'unresolved', base_qualified_name='missing.Base')]
        self.files = {
            str(first): (symbol('decl-top', 'top', 'top', first, 'module'),
                         symbol('if-bus', 'bus_if', 'bus_if', alias, 'interface'), lb),
            str(second): (symbol('if-aux', 'aux_if', 'aux_if', second, 'interface'), rb),
            str(third): (symbol('decl-monitor', 'Monitor', 'p.Monitor', third),),
        }

    def select(self, status, *, fallback):
        self.selections += 1
        controls = self
        class Backend:
            name, uses_external_worker, kdb_status = 'verdi_npi', False, None
            def bind_compile_context(self, value): self.compile_result = deepcopy(value)
            def bind_kdb_anchor(self, anchor): self.anchor = anchor
            def query_source(self, request):
                assert request.operation in {'get_tb_class_hierarchy', 'dump_tb_section'}
                assert threading.get_ident() == controls.thread
                assert self.compile_result == request.context.compile_result()
                controls.requests.append(request)
                identity = hashlib.sha256((self.anchor.identity_sha256 + '\0' + request.context.selected_top).encode()).hexdigest()
                result = request.execute(Provider(controls, identity))
                self.kdb_status = {'load_quality': 'clean'}
                result['context_identity_sha256'] = request.context.identity_sha256
                result['kdb_binding'] = dict(self.anchor.receipt(), load_status='loaded', load_quality='clean')
                if controls.after: controls.after()
                if controls.transform: controls.transform(result)
                return result
        return Backend()


@pytest.fixture
def controls(monkeypatch):
    controls = Controls()
    monkeypatch.setattr(connectivity_backend, 'select_backend', controls.select)
    return controls


async def setup(tmp_path, controls, **kwargs):
    first, second, third, alias, log, maps = project(tmp_path)
    controls.configure(first, second, third, alias)
    return await prepare(log, **kwargs), maps


async def classes(handle, /, **kwargs):
    return await public('get_tb_class_hierarchy', design_handle=handle, **kwargs)


async def export(handle, /, section, **kwargs):
    return await public('dump_tb_section', design_handle=handle, section=section, **kwargs)


def flatten(roots):
    pending, rows = list(roots), {}
    while pending:
        row = pending.pop()
        assert row['symbol_id'] not in rows
        rows[row['symbol_id']] = row
        pending.extend(row['children'])
    return rows


@pytest.mark.anyio
@pytest.mark.parametrize('tool,args', [('get_tb_class_hierarchy', {}), ('dump_tb_section', {'section': 'compile_result'})])
async def test_both_public_schemas_accept_exactly_one_context_selector(tool, args):
    schema = next(t.inputSchema for t in await server.list_tools() if t.name == tool)
    for selector in ({'design_handle': 'dc_example'}, {'handle': 'tbh_example'}):
        jsonschema.validate({**selector, **args}, schema)
    for selector in ({}, {'design_handle': 'dc_example', 'handle': 'tbh_example'}):
        with pytest.raises(jsonschema.ValidationError): jsonschema.validate({**selector, **args}, schema)


@pytest.mark.anyio
@pytest.mark.parametrize('section', METADATA)
async def test_metadata_exports_copy_record_without_any_backend_or_inventory(tmp_path, monkeypatch, controls, section):
    handle, _ = await setup(tmp_path, controls, backend_preference='source')
    no_metadata_backend(monkeypatch)
    with server._design_context_store.pin(handle) as entry:
        record = json.loads(entry.context.compile_result_json)
    expected = record if section == 'compile_result' else record.get(section, {})
    result = await export(handle, section)
    assert result['data'] == expected and result['section'] == section
    assert result['record_export_complete'] is True and result['export_complete'] is True
    assert result['facts_origin'] == 'compile_metadata' and result['semantic_status'] == 'not_run'
    assert result['design_handle'] == handle and not {'backend_status', 'kdb_binding', 'design_context'} & result.keys()
    assert result['query_metrics']['estimated_fact_bytes'] > 0
    result['data'].clear()
    with server._design_context_store.pin(handle) as entry:
        assert json.loads(entry.context.compile_result_json) == record
    assert controls.requests == [] and controls.opened == []


@pytest.mark.anyio
async def test_class_ids_bind_across_packages_and_reference_only_base_does_not_count_as_declaration(tmp_path, controls):
    handle, _ = await setup(tmp_path, controls)
    result = await classes(handle)
    rows = flatten(result['roots'])
    assert set(rows) == {'class-LB', 'class-RB', 'class-Child', 'class-External', 'class-Unknown', 'external-base'}
    assert result['total'] == 6 and result['class_inventory_lower_bound'] == 5
    assert result['class_inventory_count'] is None and result['class_inventory_complete'] is False
    assert result['class_inventory_domain'] == 'provider_declarations' and result['view'] == 'static_class_inheritance'
    assert [r['symbol_id'] for r in rows['class-RB']['children']] == ['class-Child']
    assert rows['class-LB']['children'] == []
    external = rows['external-base']
    assert external['reference_only'] is True and external['external'] is True
    assert external['source_file'] is external['source_line'] is external['source_column'] is None
    assert 'npi_class_location_unbound' not in external['gaps']
    assert rows['class-Child']['gaps'] == ['npi_class_location_unbound']
    assert rows['class-Child']['source_file'] == '/sdk/cwd/child.sv' and rows['class-Child']['source_line'] == 7
    assert rows['class-Unknown']['gaps'] == ['npi_class_location_unavailable']
    assert not any('npi_instance_location_unbound' in r['gaps'] for r in rows.values())
    assert result['coverage_status'] == 'partial' and set(CLASS_GAPS) <= set(result['coverage_gaps'])
    assert sorted(controls.opened) == sorted(controls.closed)


@pytest.mark.anyio
@pytest.mark.parametrize('root', ['class-RB', 'right.Base'])
async def test_exact_class_id_and_qualified_root_select_actual_base_not_lexical_package(tmp_path, controls, root):
    handle, _ = await setup(tmp_path, controls)
    result = await classes(handle, root_class=root)
    assert [r['symbol_id'] for r in result['roots']] == ['class-RB']
    assert [r['symbol_id'] for r in result['roots'][0]['children']] == ['class-Child']
    assert result['total'] == 2 and result['class_inventory_lower_bound'] == 5


@pytest.mark.anyio
async def test_ambiguous_short_name_is_not_guessed_and_unknown_root_is_not_a_complete_negative(tmp_path, controls):
    handle, _ = await setup(tmp_path, controls)
    ambiguous = await classes(handle, root_class='Base', max_nodes=1)
    assert ambiguous['error'] == 'class_name_ambiguous' and ambiguous['roots'] == []
    assert ambiguous['candidates'] == [{'symbol_id': 'class-LB', 'qualified_name': 'left.Base'}]
    assert ambiguous['output_truncated'] is True
    missing = await classes(handle, root_class='not_in_inventory')
    assert missing['error'] == 'class_unresolved' and missing['total'] == 0
    assert missing['class_inventory_count'] is None and missing['coverage_status'] == 'partial'


@pytest.mark.anyio
async def test_display_depth_and_cap_do_not_destroy_complete_declaration_inventory(tmp_path, controls):
    handle, _ = await setup(tmp_path, controls)
    controls.complete_classes = True
    result = await classes(handle, root_class='right.Base', depth=0, max_nodes=1)
    assert result['total'] == 1 and result['output_truncated'] is True
    assert result['roots'][0]['children'] == [] and result['roots'][0]['truncated'] is True
    assert result['class_inventory_complete'] is True and result['class_inventory_count'] == 5
    assert result['coverage_status'] == 'partial'  # External/unresolved relations remain incomplete.
    assert result['analysis_limits'] == []


@pytest.mark.anyio
@pytest.mark.parametrize('section', SEMANTIC)
async def test_all_four_semantic_sections_execute_their_existing_engine_with_positive_facts(tmp_path, controls, section):
    handle, _ = await setup(tmp_path, controls)
    result = await export(handle, section)
    assert result['section'] == section and result['facts_origin'] == 'verdi_npi'
    assert result['backend_status']['actual_backend'] == 'verdi_npi'
    assert result['backend_status']['design_context']['artifact']['source_association'] == 'unverified'
    if section == 'component_tree_full':
        assert result['root'] == result['data']['path'] == 'top'
        assert result['total_descendants'] == 2 and result['export_complete'] is True
        assert result['data']['gaps'] == ['npi_instance_location_unbound']
        generate = result['data']['children'][0]
        assert generate['path'] == 'top.g[0]' and generate['module'] is None
        assert generate['children'][0]['gaps'] == ['npi_definition_location_unbound']
    elif section == 'class_hierarchy_full':
        rows = flatten(result['data'])
        assert len(rows) == result['total'] == 6 and result['class_inventory_lower_bound'] == 5
        assert rows['class-Child']['gaps'] == ['npi_class_location_unbound']
        assert result['export_complete'] is False
    elif section == 'interfaces':
        assert [r['name'] for r in result['data']] == ['bus_if', 'aux_if']
        assert result['count_lower_bound'] == 2 and result['total'] is None
        assert result['export_complete'] is False
    else:
        rows = {r['path']: r for r in result['data']}
        assert len(result['data']) == len(rows) == len(controls.files) and set(rows) == set(controls.files)
        assert rows[str(controls.first)]['modules'] == ['top'] and rows[str(controls.first)]['interfaces'] == ['bus_if']
        assert all(r['semantic_work_scope'] == 'global_namespaces_bounded' for r in result['data'])
        assert result['export_complete'] is False
    if section in {'files_full', 'interfaces'}:
        assert result['ownership_identity'] == result['context_identity_sha256']
    assert sorted(controls.opened) == sorted(controls.closed)


@pytest.mark.anyio
async def test_output_cap_does_not_make_explicit_full_tree_export_complete(tmp_path, controls):
    handle, _ = await setup(tmp_path, controls)
    result = await export(handle, 'component_tree_full', max_items=1)
    assert result['data']['children'] == [] and result['total_descendants'] == 2
    assert result['coverage_status'] == 'complete' and result['output_truncated'] is True
    assert result['export_complete'] is False and result['warning']
    assert set(controls.opened) == {'top', 'top.g[0]', 'top.g[0].u'}


@pytest.mark.anyio
@pytest.mark.parametrize('tool,args,code', [
    ('get_tb_class_hierarchy', {'root_class': ''}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'root_class': False}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'depth': True}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'depth': -2}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'depth': 257}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'max_nodes': 0}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'max_nodes': 5001}, 'invalid_design_class_arguments'),
    ('get_tb_class_hierarchy', {'handle': 'tbh_old'}, 'invalid_design_class_arguments'),
    ('dump_tb_section', {}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'guess'}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'compile_result', 'max_items': True}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'files_full', 'max_items': 0}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'files_full', 'max_items': 5001}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'interfaces', 'unexpected': 1}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'interfaces', 'timeout_sec': 0}, 'invalid_design_section_arguments'),
    ('dump_tb_section', {'section': 'interfaces', 'timeout_sec': float('nan')}, 'invalid_design_section_arguments'),
])
async def test_invalid_queries_reject_before_any_binding(tmp_path, monkeypatch, controls, tool, args, code):
    handle, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    result = await public(tool, design_handle=handle, **args)
    assert result['error_code'] == code and 'data' not in result and 'roots' not in result
    assert controls.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize('corruption', ['envelope_root', 'data_root', 'ownership', 'file_alias', 'interface_member', 'section'])
async def test_export_request_and_captured_ownership_cannot_be_replaced_by_legal_other_facts(tmp_path, controls, corruption):
    handle, _ = await setup(tmp_path, controls)
    section = ('component_tree_full' if corruption in {'envelope_root', 'data_root'} else
               'class_hierarchy_full' if corruption == 'section' else
               'interfaces' if corruption == 'interface_member' else 'files_full')
    def corrupt(value):
        if corruption == 'envelope_root': value['root'] = 'other'
        elif corruption == 'data_root': value['data'].update(path='other', inst='other')
        elif corruption == 'ownership': value['ownership_identity'] = 'foreign-context'
        elif corruption == 'file_alias': value['data'][0]['aliases'].append('/foreign/unobserved.sv')
        elif corruption == 'interface_member': value['data'][0]['source_file'] = '/foreign/bus_if.sv'
        else:
            value.update(section='files_full', data=[], total=None, count_status='lower_bound',
                         count_lower_bound=0, ownership_identity=controls.requests[-1].context.identity_sha256)
        server.schemas.SourceSectionResult.model_validate(value)
    controls.transform = corrupt
    result = await export(handle, section, max_items=1)
    assert 'error' in result and 'data' not in result
    assert len(controls.requests) == controls.selections == 1


@pytest.mark.anyio
@pytest.mark.parametrize('requested', ['class-RB', 'right.Base', 'UnknownChild'])
async def test_successful_class_root_must_match_requested_id_or_qualified_or_unique_short_name(tmp_path, controls, requested):
    handle, _ = await setup(tmp_path, controls)
    def corrupt(value):
        # Internally valid other class, same artifact/context, wrong request.
        value['roots'] = [dict(controls.classes[0].row(), children=[], truncated=False)]
        value['total'] = 1
        value.pop('error', None)
    controls.transform = corrupt
    result = await classes(handle, root_class=requested)
    assert 'error' in result and 'roots' not in result


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['class', 'export'])
@pytest.mark.parametrize('kind,code', [('cancel', None), ('timeout', 'design_context_timeout'),
    ('source', 'design_context_changed'), ('kdb', 'kdb_identity_changed'), ('policy', 'design_context_execution_changed')])
async def test_typed_native_stream_failure_discards_results_and_closes_every_owner(tmp_path, controls, operation, kind, code):
    handle, _ = await setup(tmp_path, controls)
    error = {'cancel': OperationCancelled(), 'timeout': TimeoutError('native deadline'),
             'source': CaptureAborted('input_changed'), 'kdb': KdbBindingChanged(), 'policy': ExecutionBindingChanged()}[kind]
    def fail(label, index):
        if index == 1: raise error
    controls.on_next = fail
    if kind == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            if operation == 'class': await classes(handle)
            else: await export(handle, 'class_hierarchy_full')
    else:
        result = await classes(handle) if operation == 'class' else await export(handle, 'class_hierarchy_full')
        assert result['error_code'] == code and 'roots' not in result and 'data' not in result
    assert controls.requests and controls.opened == controls.closed == ['classes']


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['class', 'export'])
async def test_late_final_budget_preserves_rendered_class_total_but_clears_inventory_proof(tmp_path, monkeypatch, controls, operation):
    handle, _ = await setup(tmp_path, controls)
    controls.complete_classes = True
    original = server._validate_design_binding
    async def validate(binding, **kwargs):
        await original(binding, **kwargs)
        if controls.requests: controls.requests[-1].budget.deadline = -1
    monkeypatch.setattr(server, '_validate_design_binding', validate)
    result = await classes(handle) if operation == 'class' else await export(handle, 'class_hierarchy_full')
    assert result['total'] == 6 and result['class_inventory_lower_bound'] == 5
    assert result['class_inventory_complete'] is False and result['class_inventory_count'] is None
    assert result['coverage_status'] == 'partial' and 'timeout_sec' in result['analysis_limits']
    if operation == 'export': assert result['export_complete'] is False


@pytest.mark.anyio
@pytest.mark.parametrize('change,code', [('source', 'design_context_changed'), ('map', 'kdb_identity_changed'),
                                       ('reset', 'design_context_generation_changed')])
async def test_final_identity_change_discards_entire_class_export(tmp_path, controls, change, code):
    handle, maps = await setup(tmp_path, controls)
    def mutate():
        if change == 'source': controls.first.write_text('module changed; endmodule\n')
        elif change == 'map': (maps / '_libFileMap').write_bytes(b'changed maps')
        else: server.reset_session_state()
    controls.after = mutate
    result = await export(handle, 'class_hierarchy_full')
    assert result['error_code'] == code and 'data' not in result


@pytest.mark.anyio
async def test_class_source_policy_is_unavailable_instead_of_guessed_legacy_topology(tmp_path, monkeypatch, controls):
    handle, _ = await setup(tmp_path, controls, backend_preference='source')
    # Deliberately disabled source provider: this remains a no-process routing
    # control; actual supported class positives live in the real Slang suite.
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    result = await classes(handle)
    assert result['error'] == 'semantic_provider_unavailable' and result['semantic_status'] == 'unavailable'
    assert result['facts_origin'] == 'compile_metadata' and 'roots' not in result
    assert controls.requests == [] and controls.selections == 0


@pytest.mark.anyio
async def test_healthy_native_class_inventory_retains_priority_with_incomplete_source_capture(tmp_path, monkeypatch, controls):
    first, second, third, alias, log, _ = project(tmp_path)
    third.unlink()
    controls.configure(first, second, third, alias)
    handle = await prepare(log)
    monkeypatch.setattr(server, '_query_slang_hierarchy', forbidden)
    result = await classes(handle)
    assert result['facts_origin'] == 'verdi_npi' and result['total'] == 6
    assert result['backend_status']['actual_backend'] == 'verdi_npi'
    assert 'source_capture_incomplete' not in result['coverage_gaps']
    assert len(controls.requests) == 1 and controls.opened == controls.closed == ['classes']


@pytest.mark.anyio
async def test_metadata_record_copy_cannot_erase_protected_input_coverage_gap(tmp_path, monkeypatch, controls):
    first, second, third, alias, log, _ = project(tmp_path)
    first.write_text('`pragma protect begin_protected\nprotected_body\n`pragma protect end_protected\n')
    controls.configure(first, second, third, alias)
    handle = await prepare(log)
    no_metadata_backend(monkeypatch)
    result = await export(handle, 'compile_result')
    assert result['record_export_complete'] is True and result['export_complete'] is False
    assert result['coverage_status'] == 'partial' and 'protected_region' in result['coverage_gaps']
    assert result['warning'] and 'backend_status' not in result


@pytest.mark.anyio
@pytest.mark.parametrize('section', ['files_full', 'interfaces'])
async def test_empty_handbound_compile_catalog_exports_without_backend(monkeypatch, controls, section):
    # This is explicitly an internal hand-bound metadata control, not evidence
    # that the compiler produced an empty KDB or that such a KDB is loadable.
    from tests.test_hierarchy_source_export_request import context
    handle = server._design_context_store.register(context(empty=True))
    no_metadata_backend(monkeypatch)
    result = await export(handle, section)
    assert result['data'] == [] and result['export_complete'] is True
    assert result['facts_origin'] == 'compile_metadata' and result['semantic_status'] == 'not_run'
    assert not {'backend_status', 'kdb_binding', 'design_context'} & result.keys()
    assert controls.requests == []


@pytest.mark.anyio
async def test_own_inventory_budget_stop_keeps_partial_declaration_and_closes_stream(tmp_path, controls):
    handle, _ = await setup(tmp_path, controls)
    def limit(label, index):
        if label == 'classes' and index == 1: raise HierarchyBudgetExceeded('symbol_limit')
    controls.on_next = limit
    result = await classes(handle)
    assert result['total'] == result['class_inventory_lower_bound'] == 1
    assert result['roots'][0]['symbol_id'] == 'class-LB'
    assert result['class_inventory_count'] is None and result['class_inventory_complete'] is False
    assert result['analysis_limits'] == ['symbol_limit'] and result['coverage_status'] == 'partial'
    assert controls.opened == controls.closed == ['classes']


@pytest.mark.anyio
async def test_metadata_export_final_deadline_cannot_publish_complete_export(tmp_path, monkeypatch, controls):
    handle, _ = await setup(tmp_path, controls)
    no_metadata_backend(monkeypatch)
    budgets, validations = [], []
    original_init, original_validate = ir.HierarchyQueryBudget.__init__, server._validate_design_source
    def initialize(budget, *args, **kwargs):
        original_init(budget, *args, **kwargs)
        budgets.append(budget)
    async def validate(context, **kwargs):
        await original_validate(context, **kwargs)
        validations.append(1)
        if len(validations) >= 2: budgets[-1].deadline = -1
    monkeypatch.setattr(ir.HierarchyQueryBudget, '__init__', initialize)
    monkeypatch.setattr(server, '_validate_design_source', validate)
    result = await export(handle, 'include_tree')
    assert result['record_export_complete'] is True and result['export_complete'] is False
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']
    assert controls.requests == []


@pytest.mark.anyio
async def test_legacy_class_and_all_seven_raw_sections_keep_compatibility(controls):
    handle = 'tbh_control'
    record = {'simulator': 'vcs', 'include_tree': {'a.sv': ['h.svh']}, 'filelist_tree': {'input.f': []}}
    full = {'compile_result': record, 'interfaces': [{'name': 'legacy_if'}],
            'files': {'user': [{'path': 'a.sv'}]}, 'component_tree': {'top': {}},
            'class_hierarchy': [{'name': 'raw_legacy_class'}],
            '_scan_results': [{'path': '/legacy/classes.sv', 'classes': ['LegacyChild'],
                               'class_extends': {'LegacyChild': 'ExternalBase'}}]}
    server._handle_store.register(handle, full)
    result = await public('get_tb_class_hierarchy', handle=handle)
    assert result == {'handle': handle, 'roots': [{'name': 'ExternalBase', 'source_file': '', 'source_line': 0,
        'children': [{'name': 'LegacyChild', 'source_file': '/legacy/classes.sv', 'source_line': 0,
                      'children': []}]}], 'total': 2}
    expected = {'compile_result': record, 'include_tree': record['include_tree'],
                'filelist_tree': record['filelist_tree'], 'interfaces': full['interfaces'],
                'files_full': full['files'], 'component_tree_full': full['component_tree'],
                'class_hierarchy_full': full['class_hierarchy']}
    for section, data in expected.items():
        exported = await public('dump_tb_section', handle=handle, section=section)
        assert exported == {'handle': handle, 'section': section, 'data': data, 'warning': ''}
    assert controls.requests == [] and controls.selections == 0
