"""Second public source tool: handwritten topology and control-only backend.

Real compile capture/binding/codec/serialization execute; no actual SDK runs.
R has A/G/B; G has U; A/B/U are leaves. Five scopes, four descendants.
An omitted but observed U is evidence of an edge, not of U's own emptiness.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import threading

import anyio
import jsonschema
import pytest

import server
from src import connectivity_backend, design_backend_binding
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.connectivity_ir import SourceLocation
from src.design_backend_binding import ExecutionBindingChanged
from src.hierarchy_source_codec import validate_source_result
from src.hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyScope, ScopeLookup
from src.kdb_anchor import KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable

# Only stable fixture/guards are reused. Neither provider nor expected topology
# is derived from another test's expected results or a product-built hierarchy.
from tests.test_public_design_instance import anyio_backend, isolate, forbidden, prepare, project, public


R, A, G, U, B = 'top', 'top.a', 'top.g[0]', 'top.g[0].u', 'top.b'
INSTANCE_GAP, DEFINITION_GAP = 'npi_instance_location_unbound', 'npi_definition_location_unbound'


class Topology:
    origin, top = 'verdi_npi', R

    def __init__(self, controls, identity, source):
        self.controls, self.artifact_identity = controls, identity
        self.facts = {
            R: HierarchyScope('scope-R', R, 'top', 'module', None, 'def-top', 'top',
                              SourceLocation('/vendor/cwd/instances.sv', 484), SourceLocation(source, 2)),
            A: HierarchyScope('scope-A', A, 'a', 'module', R, 'def-leaf', 'leaf',
                              SourceLocation(source, 2), SourceLocation('/unbound/leaf.sv', 1)),
            G: HierarchyScope('scope-G', G, 'g[0]', 'generate', R, location=SourceLocation(source, 2)),
            U: HierarchyScope('scope-U', U, 'u', 'module', G, 'def-leaf', 'leaf',
                              gaps=('npi_location_unavailable',)),
            B: HierarchyScope('scope-B', B, 'b', 'module', R, 'def-leaf', 'leaf',
                              SourceLocation('/vendor/cwd/instances.sv', 484), SourceLocation(source, 1)),
        }
        self.children = {R: (A, G, B), G: (U,)}

    def lookup_scope(self, path, budget):
        budget.call()
        self.controls.lookups.append(path)
        return ScopeLookup(self.facts.get(path), path != 'top.unknown',
                           ('control_lookup_unknown',) if path == 'top.unknown' else ())

    @contextmanager
    def open_children(self, scope, budget):
        self.controls.opened.append(scope.path)
        budget.call()
        provider = self
        values = self.children.get(scope.path, ())

        class Stream:
            complete, gaps = False, ()
            def __init__(self): self.index = 0
            def __iter__(self): return self
            def __next__(self):
                budget.call()
                provider.controls.events.append((scope.path, self.index))
                if provider.controls.on_next:
                    provider.controls.on_next(scope.path, self.index)
                if self.index == len(values):
                    self.complete = True
                    raise StopIteration
                fact = provider.facts[values[self.index]]
                self.index += 1
                provider.controls.observed.append(fact.path)
                return fact
        try:
            yield Stream()
        finally:
            self.controls.closed.append(scope.path)


class Controls:
    def __init__(self):
        self.requests, self.lookups, self.opened, self.closed, self.observed, self.events = [], [], [], [], [], []
        self.selections = 0
        self.on_next = self.after = self.transform = None
        self.source = '/unset-control-source.sv'
        self.thread = threading.get_ident()

    def select(self, status, *, fallback):
        self.selections += 1
        controls = self
        class Backend:
            name, uses_external_worker, kdb_status = 'verdi_npi', False, None
            def bind_compile_context(self, compile_result): self.compile_result = deepcopy(compile_result)
            def bind_kdb_anchor(self, anchor): self.anchor = anchor
            def query_source(self, request):
                assert threading.get_ident() == controls.thread
                assert request.operation == 'get_tb_subtree'
                assert request.context.compile_result() == self.compile_result
                controls.requests.append(request)
                identity = hashlib.sha256((self.anchor.identity_sha256 + '\0' + request.context.selected_top).encode()).hexdigest()
                value = request.execute(Topology(controls, identity, controls.source))
                self.kdb_status = {'load_quality': 'clean'}
                value['context_identity_sha256'] = request.context.identity_sha256
                value['kdb_binding'] = dict(self.anchor.receipt(), load_status='loaded', load_quality='clean')
                if controls.after: controls.after()
                if controls.transform: controls.transform(value)
                return value
        return Backend()


@pytest.fixture
def controls(monkeypatch):
    controls = Controls()
    monkeypatch.setattr(connectivity_backend, 'select_backend', controls.select)
    return controls


async def setup(tmp_path, controls, **kwargs):
    source, log, maps = project(tmp_path)
    controls.source = str(source)
    return await prepare(log, **kwargs), source, log, maps


async def query(handle, /, **kwargs):
    return await public('get_tb_subtree', design_handle=handle, **kwargs)


def closed(controls):
    assert sorted(controls.closed) == sorted(controls.opened)


@pytest.mark.anyio
async def test_schema_accepts_exactly_one_selector_and_exposes_bounded_controls():
    schema = next(tool.inputSchema for tool in await server.list_tools() if tool.name == 'get_tb_subtree')
    for args in ({'design_handle': 'dc_one'}, {'handle': 'tbh_one'},
                 {'design_handle': 'dc_one', 'root': R, 'depth': -1, 'max_nodes': 1,
                  'count_mode': 'exact', 'timeout_sec': 2}):
        jsonschema.validate(args, schema)
    for args in ({}, {'handle': 'tbh_one', 'design_handle': 'dc_one'}):
        with pytest.raises(jsonschema.ValidationError): jsonschema.validate(args, schema)


@pytest.mark.anyio
async def test_default_depth_one_has_three_visible_children_and_one_unproved_frontier(tmp_path, controls, monkeypatch):
    captures = []
    original_init = CompileInputCapture.__init__
    def initialize(capture, *args, **kwargs):
        original_init(capture, *args, **kwargs)
        captures.append(capture)
    monkeypatch.setattr(CompileInputCapture, '__init__', initialize)
    handle, _, _, _ = await setup(tmp_path, controls)
    outputs = [await query(handle) for _ in range(2)]
    for index, value in enumerate(outputs):
        validate_source_result('get_tb_subtree', value)
        assert value['root'] == R and value['node']['path'] == R
        assert [node['path'] for node in value['node']['children']] == [A, G, B]
        assert value['count_status'] == 'lower_bound' and value['count_lower_bound'] == 4
        assert value['coverage_status'] == 'partial' and value['total_descendants'] is None
        assert value['analysis_limits'] == [] and value['output_truncated'] is True
        assert value['node']['child_count'] == value['node']['child_count_lower_bound'] == 3
        generate = value['node']['children'][1]
        assert generate['kind'] == 'generate' and generate['module'] is generate['definition_id'] is None
        assert generate['child_count'] is None and generate['child_count_lower_bound'] == 1
        assert generate['children'] == [] and generate['truncated'] is True
        receipt = value['backend_status']['design_context']
        assert receipt['binding_reused'] is bool(index) and receipt['design_handle'] == handle
        assert receipt['artifact']['source_association'] == 'unverified'
        assert receipt['artifact']['load_status'] == 'loaded'
        assert value['facts_origin'] == value['backend_status']['actual_backend'] == 'verdi_npi'
    assert controls.lookups == [R, R] and U not in controls.opened
    assert controls.observed == [A, G, U, B, A, G, U, B]
    closed(controls)
    assert captures and all(capture.body_count == capture.resident_bytes == 0 for capture in captures)
    assert server._session_state['build_tb_hierarchy'] is None


@pytest.mark.anyio
@pytest.mark.parametrize('arguments', [{'depth': 0}, {'depth': -1, 'max_nodes': 1}])
async def test_root_consumes_display_cap_and_only_one_omitted_edge_is_peeked(tmp_path, controls, arguments):
    handle, _, _, _ = await setup(tmp_path, controls)
    value = await query(handle, **arguments)
    assert value['node']['children'] == [] and value['node']['child_count'] is None
    assert value['node']['child_count_lower_bound'] == 1
    assert value['count_lower_bound'] == 1 and value['query_metrics']['scope_count'] == 2
    assert value['total_descendants'] is None and value['coverage_status'] == 'partial'
    assert value['output_truncated'] is True and value['analysis_limits'] == []
    assert controls.observed == [A] and controls.opened == controls.closed == [R]


@pytest.mark.anyio
async def test_two_node_display_cap_does_not_open_the_omitted_generate_subtree(tmp_path, controls):
    handle, _, _, _ = await setup(tmp_path, controls)
    value = await query(handle, depth=-1, max_nodes=2)
    assert [node['path'] for node in value['node']['children']] == [A]
    assert controls.observed == [A, G] and G not in controls.opened
    assert value['count_lower_bound'] == 2 and value['total_descendants'] is None
    assert value['query_metrics']['scope_count'] == 3
    closed(controls)


@pytest.mark.anyio
async def test_explicit_exact_count_can_be_complete_with_only_root_displayed(tmp_path, controls):
    handle, _, _, _ = await setup(tmp_path, controls)
    value = await query(handle, depth=0, max_nodes=1, count_mode='exact')
    assert value['node']['children'] == [] and value['node']['child_count'] == 3
    assert value['total_descendants'] == value['count_lower_bound'] == 4
    assert value['query_metrics']['scope_count'] == 5
    assert value['count_status'] == 'exact' and value['coverage_status'] == 'complete'
    assert value['output_truncated'] is True and value['analysis_limits'] == []
    assert set(controls.opened) == {R, A, G, U, B}
    closed(controls)


@pytest.mark.anyio
async def test_all_rendered_nodes_keep_role_specific_raw_location_gaps(tmp_path, controls):
    handle, source, _, _ = await setup(tmp_path, controls)
    value = await query(handle, depth=-1)
    root, a, generate, b = value['node'], *value['node']['children']
    u = generate['children'][0]
    assert root['gaps'] == b['gaps'] == [INSTANCE_GAP]
    assert a['gaps'] == [DEFINITION_GAP] and generate['gaps'] == []
    assert u['gaps'] == ['npi_location_unavailable']
    assert root['source_file'] == '/vendor/cwd/instances.sv' and root['source_line'] == 484
    assert root['definition_file'] == str(source) and root['definition_line'] == 2
    assert a['definition_file'] == '/unbound/leaf.sv'
    assert u['source_file'] is u['source_line'] is u['definition_file'] is u['definition_line'] is None
    assert value['total_descendants'] == 4 and value['coverage_status'] == 'complete'


@pytest.mark.anyio
@pytest.mark.parametrize('root,error,complete', [('top.absent', 'instance_not_found', True),
                                               ('top.unknown', 'instance_unresolved', False)])
async def test_complete_missing_and_unknown_are_distinct(tmp_path, controls, root, error, complete):
    handle, _, _, _ = await setup(tmp_path, controls)
    value = await query(handle, root=root)
    assert value['error'] == error and value['node'] is None and value['root'] == root
    assert value['total_descendants'] == (0 if complete else None)
    assert value['count_status'] == ('exact' if complete else 'lower_bound')
    assert controls.opened == []


@pytest.mark.anyio
@pytest.mark.parametrize('arguments', [
    {'root': None}, {'root': 1}, {'depth': True}, {'depth': -2}, {'depth': 257}, {'depth': 0.5},
    {'max_nodes': True}, {'max_nodes': 0}, {'max_nodes': 5001}, {'count_mode': 'approx'},
    {'timeout_sec': 0}, {'timeout_sec': True}, {'timeout_sec': float('nan')}, {'timeout_sec': 301},
    {'handle': 'tbh_old'}, {'unexpected': 1}, {'compile_log': '/unbound/comp.log'},
])
async def test_invalid_subtree_arguments_fail_before_binding(tmp_path, monkeypatch, controls, arguments):
    handle, _, _, _ = await setup(tmp_path, controls)
    monkeypatch.setattr(design_backend_binding, 'prepare_backend_binding', forbidden)
    value = await query(handle, **arguments)
    assert value['error_code'] == 'invalid_design_subtree_arguments' and 'node' not in value
    assert controls.requests == [] and controls.selections == 0


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['iterator', 'final_validation'])
@pytest.mark.parametrize('kind,code', [('cancel', None), ('timeout', 'design_context_timeout'),
    ('source', 'design_context_changed'), ('kdb', 'kdb_identity_changed'), ('policy', 'design_context_execution_changed')])
async def test_typed_failure_discards_partial_tree_and_never_falls_back(tmp_path, monkeypatch, controls, stage, kind, code):
    handle, _, _, _ = await setup(tmp_path, controls)
    error = {'cancel': OperationCancelled(), 'timeout': TimeoutError('native timeout'),
             'source': CaptureAborted('input_changed'), 'kdb': KdbBindingChanged(),
             'policy': ExecutionBindingChanged()}[kind]
    def fail(): raise error
    if stage == 'iterator':
        def next_hook(path, index):
            if path == R and index == 1: fail()
        controls.on_next = next_hook
    else:
        original = server._validate_design_binding
        async def validate(binding, **kwargs):
            await original(binding, **kwargs)
            if controls.requests: fail()
        monkeypatch.setattr(server, '_validate_design_binding', validate)
    if kind == 'cancel':
        with pytest.raises(asyncio.CancelledError): await query(handle, depth=-1)
    else:
        value = await query(handle, depth=-1)
        assert value['error_code'] == code and 'node' not in value
    assert len(controls.requests) == 1 and A in controls.observed
    closed(controls)


@pytest.mark.anyio
async def test_own_work_limit_retains_positive_prefix_as_partial_and_closes_resources(tmp_path, controls):
    handle, _, _, _ = await setup(tmp_path, controls)
    def stop(path, index):
        if path == R and index == 1: raise HierarchyBudgetExceeded('scope_limit')
    controls.on_next = stop
    value = await query(handle, depth=-1)
    assert value['analysis_limits'] == ['scope_limit'] and value['coverage_status'] == 'partial'
    assert value['total_descendants'] is None and value['count_lower_bound'] == 1
    assert [node['path'] for node in value['node']['children']] == [A]
    closed(controls)


@pytest.mark.anyio
@pytest.mark.parametrize('root', [R, 'top.absent'])
async def test_late_final_validation_removes_exact_total_and_complete_negative(tmp_path, monkeypatch, controls, root):
    handle, _, _, _ = await setup(tmp_path, controls)
    original = server._validate_design_binding
    async def validate(binding, **kwargs):
        await original(binding, **kwargs)
        if controls.requests: controls.requests[-1].budget.deadline = -1
    monkeypatch.setattr(server, '_validate_design_binding', validate)
    value = await query(handle, root=root, depth=-1)
    assert value['count_status'] == 'lower_bound' and value['total_descendants'] is None
    assert value['coverage_status'] == 'partial' and 'timeout_sec' in value['analysis_limits']
    if root != R:
        assert value['error'] == 'instance_unresolved' and value['node'] is None
    else:
        assert value['node']['children'][1]['children'][0]['path'] == U


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['source', 'map', 'reset'])
async def test_post_query_changes_discard_the_whole_tree(tmp_path, controls, failure):
    handle, source, _, maps = await setup(tmp_path, controls)
    def change():
        if failure == 'source': source.write_text('module changed; endmodule\n')
        elif failure == 'map': (maps / '_libFileMap').write_bytes(b'changed maps')
        else: server.reset_session_state()
    controls.after = change
    value = await query(handle, depth=-1)
    expected = {'source': 'design_context_changed', 'map': 'kdb_identity_changed', 'reset': 'design_context_generation_changed'}
    assert value['error_code'] == expected[failure] and 'node' not in value
    closed(controls)


@pytest.mark.anyio
@pytest.mark.parametrize('reason', ['source', 'unavailable'])
async def test_unavailable_source_planner_cannot_become_empty_native_tree(tmp_path, controls, reason, monkeypatch):
    from dataclasses import replace
    unavailable_config = replace(server.get_source_graph_execution_config(), enabled=False)
    monkeypatch.setattr(server, "get_source_graph_execution_config", lambda: unavailable_config)
    handle, _, _, _ = await setup(tmp_path, controls, backend_preference='source' if reason == 'source' else 'auto')
    if reason == 'unavailable':
        def unavailable(path, index): raise NpiHierarchyUnavailable('npi_load_failed')
        controls.on_next = unavailable
    value = await query(handle)
    assert value['error'] == 'semantic_provider_unavailable' and value['semantic_status'] == 'unavailable'
    assert value['facts_origin'] == 'compile_metadata' and value['coverage_status'] == 'partial'
    assert 'node' not in value and 'total_descendants' not in value
    if reason == 'source': assert controls.selections == 0
    closed(controls)


@pytest.mark.anyio
async def test_legacy_subtree_shape_and_counts_stay_unchanged(controls):
    server._handle_store.register('tbh_control', {'component_tree': {R: {'a': {
        'class': 'leaf', 'source_file': '/rtl/leaf.sv', 'source_line': 4, 'children': {}}}}})
    value = await public('get_tb_subtree', handle='tbh_control', root=A, depth=0)
    assert value == {'handle': 'tbh_control', 'root': A, 'node': {
        'inst': 'a', 'module': 'leaf', 'source_file': '/rtl/leaf.sv', 'source_line': 4,
        'child_count': 0, 'truncated': False, 'children': []}, 'total_descendants': 0, 'truncated': False}
    assert controls.requests == [] and controls.selections == 0


@pytest.mark.anyio
@pytest.mark.parametrize('mismatch', ['node_vs_envelope', 'envelope_vs_request'])
async def test_two_root_mismatch_boundaries_are_rejected_without_fallback(tmp_path, controls, mismatch):
    handle, _, _, _ = await setup(tmp_path, controls)
    def corrupt(value):
        value['root'] = 'other'
        if mismatch == 'envelope_vs_request':
            # This is internally consistent and passes the standalone codec,
            # yet answers a different request within the same KDB generation.
            value['node'].update(path='other', inst='other')
            validate_source_result('get_tb_subtree', value)
    controls.transform = corrupt
    value = await query(handle, root=A, depth=-1)
    assert 'error' in value and 'node' not in value
    assert len(controls.requests) == 1 and controls.selections == 1


@pytest.mark.anyio
async def test_depth_256_survives_public_iterative_validation_and_json_serialization(tmp_path, controls):
    handle, source, _, _ = await setup(tmp_path, controls)
    # Handwritten output chain: exactly 257 rows and 256 parent edges. Returning
    # copied facts bypasses native enumeration solely to stress the wire path.
    def chain(value):
        def row(index, path, parent):
            return {'scope_id': f'deep-{index}', 'path': path, 'inst': 'top' if index == 0 else 'u',
                    'kind': 'module', 'parent': parent, 'definition_id': 'leaf-def', 'module': 'leaf',
                    'source_file': None, 'source_line': None, 'definition_file': str(source), 'definition_line': 1,
                    'parameterization': {}, 'gaps': [], 'children': [],
                    'child_count': 0 if index == 256 else 1,
                    'child_count_lower_bound': 0 if index == 256 else 1, 'truncated': False}
        root = current = row(0, R, None)
        for index in range(1, 257):
            parent = current['path']
            child = row(index, parent + '.u', parent)
            current['children'].append(child)
            current = child
        value.update(root=R, node=root, total_descendants=256, count_lower_bound=256,
                     count_status='exact', coverage_status='complete', coverage_gaps=[], analysis_limits=[],
                     output_truncated=False, truncated=False)
        value['query_metrics']['scope_count'] = 257
    controls.transform = chain
    value = await query(handle, root=R, depth=256, max_nodes=500)
    assert value['schema_version'] == 2 and value['total_descendants'] == 256, value
    current = value['node']
    assert current['parent'] is None
    for index in range(257):
        assert current['scope_id'] == f'deep-{index}'
        assert current['source_file'] is current['source_line'] is None
        assert current['gaps'] == []
        if index < 256:
            assert len(current['children']) == 1
            child = current['children'][0]
            assert child['parent'] == current['path']
            current = child
        else:
            assert current['children'] == [] and current['child_count'] == 0


def wire_tree():
    """Separate three-row codec oracle, not copied from service output."""
    def row(identity, path, inst, parent):
        return {'scope_id': identity, 'path': path, 'inst': inst, 'kind': 'module', 'parent': parent,
                'definition_id': None, 'module': None, 'source_file': None, 'source_line': None,
                'definition_file': None, 'definition_line': None, 'parameterization': {}, 'gaps': [],
                'children': [], 'child_count': 0, 'child_count_lower_bound': 0, 'truncated': False}
    root = row('wire-R', R, 'top', None)
    root.update(children=[row('wire-A', A, 'a', R), row('wire-B', B, 'b', R)],
                child_count=2, child_count_lower_bound=2)
    return {'schema_version': 2, 'facts_origin': 'verdi_npi', 'artifact_identity': 'wire-design',
            'coverage_status': 'complete', 'coverage_gaps': [], 'analysis_limits': [],
            'count_status': 'exact', 'count_lower_bound': 2, 'count_mode': 'bounded',
            'count_domain': 'descendants_of_root', 'output_truncated': False,
            'root': R, 'node': root, 'total_descendants': 2, 'truncated': False,
            'query_metrics': {'scope_count': 3, 'native_call_count': 4, 'symbol_count': 0,
                              'file_count': 0, 'estimated_fact_bytes': 2048, 'query_ms': 1.0}}


@pytest.mark.parametrize('corruption', ['root', 'parent', 'id', 'path', 'total', 'visible_lower_bound', 'complete_limit'])
def test_codec_itself_rejects_wrong_root_edges_duplicates_and_count_proofs(corruption):
    payload = wire_tree()
    if corruption == 'root': payload['root'] = 'other'
    elif corruption == 'parent': payload['node']['children'][0]['parent'] = 'other'
    elif corruption == 'id': payload['node']['children'][1]['scope_id'] = 'wire-A'
    elif corruption == 'path': payload['node']['children'][1]['path'] = A
    elif corruption == 'total': payload['total_descendants'] = 3
    elif corruption == 'visible_lower_bound': payload.update(total_descendants=1, count_lower_bound=1)
    else: payload['analysis_limits'] = ['timeout_sec']
    with pytest.raises(ValueError): validate_source_result('get_tb_subtree', payload)


def test_codec_preserves_unknown_tree_counts_null_locations_and_returns_fresh_rows():
    payload = wire_tree()
    payload.update(count_status='lower_bound', total_descendants=None, coverage_status='partial')
    payload['node']['child_count'] = None
    result = validate_source_result('get_tb_subtree', payload)
    assert result == payload
    assert result['total_descendants'] is None and result['node']['child_count'] is None
    assert result['node']['children'][0]['source_file'] is None
    result['node']['children'][0]['gaps'].append('control_gap')
    assert payload['node']['children'][0]['gaps'] == []
