"""Independent typed-fatal controls; no installed SDK, EDA or frontend.

Injected getters and routing barriers test propagation and discarded work,
not connection semantics, real timeout interruption or licensed behavior.
"""
import asyncio
from contextlib import nullcontext
import subprocess
import threading
from types import SimpleNamespace as NS

import pytest

import server
from config import SourceGraphExecutionConfig
from src import connectivity_backend as connectivity
from src import npi_dynamic, verdi_npi_backend as npi, x_trace
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.kdb_anchor import KdbBindingChanged
from src.source_graph_adapter import AdapterStatus
from src.source_graph_runtime import PrepareStatus
from src.source_graph_x_trace import SourceGraphTraceScopeExpansion


KINDS = ('cancel', 'capture', 'kdb', 'timeout')


def failure(kind):
    return {'cancel': lambda: OperationCancelled('original cancel'),
            'capture': lambda: CaptureAborted('input_changed'),
            'kdb': KdbBindingChanged,
            'timeout': lambda: TimeoutError('original timeout')}[kind]()


def throwing(error):
    def call(*args, **kwargs):
        raise error
    return call


def forbidden(*a, **kw):
    pytest.fail('control test entered real SDK, builder, worker or fallback')


@pytest.fixture(autouse=True)
def no_native_execution(monkeypatch):
    monkeypatch.setattr(npi, '_import_pynpi', forbidden)
    monkeypatch.setattr(npi, 'parse_compile_log', forbidden)
    monkeypatch.setattr(npi, 'probe_verdi_backend', forbidden)
    monkeypatch.setattr(npi, '_silence_native_stdio', nullcontext)
    monkeypatch.setattr(npi, '_NPI_FAN_IN_CALLBACK_LOCK', threading.Lock())
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)


def handle(**overrides):
    values = dict(full_name='top.u:Always0#Always0:11:11:Reg.ROH_q', type='npiNlInstPort',
                  scope_inst=None, def_name='leaf', file='source.sv', begin_line_no=11,
                  src_info={}, direction='npiNlInput', load_list=[], connected_pin=None,
                  connected_net=None)
    values.update(overrides)
    return NS(**{key: (value if callable(value) else lambda val=value: val)
                 for key, value in values.items()})


class Fallback:
    def __init__(self): self.calls = []
    def find_driver(self, *a, **kw):
        self.calls.append('driver')
        return {'backend': 'static', 'control_fallback': True}
    def find_loads(self, *a, **kw):
        self.calls.append('loads')
        return {'backend': 'static', 'control_fallback': True}
    def find_path(self, *a, **kw):
        self.calls.append('path')
        return {'backend': 'static', 'control_fallback': True}


def backend(netlist=None):
    target = npi.VerdiNpiBackend(Fallback())
    target._npi_modules = (object(), netlist or NS())
    target._loaded_top, target._loaded_degraded = 'top', False
    return target


GETTERS = ('module_scope', 'module_definition', 'handle_key', 'output_boundary',
           'driver_boundary', 'scope_inst', 'inst_name', 'inst_definition',
           'inst_file', 'inst_line', 'pin_source', 'driver_format', 'load_format',
           'fan_in_format', 'path_name', 'path_scope', 'own_load_names', 'degraded_top')


def invoke_getter(case, error):
    bad, target = throwing(error), backend()
    if case == 'module_scope': return npi._module_of(handle(scope_inst=bad))
    if case == 'module_definition': return npi._module_of(handle(scope_inst=handle(def_name=bad)))
    if case == 'handle_key': return npi._npi_handle_key(handle(type=bad))
    if case == 'output_boundary': return npi._is_output_boundary_load(handle(direction=bad))
    if case == 'driver_boundary': return npi._is_boundary_driver(handle(full_name=bad))
    if case == 'scope_inst': return npi._scope_inst_of(handle(scope_inst=bad))
    if case == 'inst_name': return npi._inst_full_name(handle(full_name=bad))
    if case == 'inst_definition': return npi._inst_definition_name(handle(def_name=bad))
    if case == 'inst_file': return npi._inst_src_info(handle(file=bad))
    if case == 'inst_line': return npi._inst_src_info(handle(begin_line_no=bad))
    if case == 'pin_source': return npi._safe_src_info(handle(src_info=bad))
    if case == 'driver_format': return target._format_driver(handle(full_name=bad))
    if case == 'load_format': return target._format_load(handle(type=bad), True)
    if case == 'fan_in_format': return target._format_fan_in_pin(handle(type=bad))
    if case == 'path_name': return target._format_path_hop(handle(full_name=bad), 0, is_endpoint=True)
    if case == 'path_scope': return target._format_path_hop(handle(scope_inst=handle(full_name=bad)), 0, is_endpoint=True)
    if case == 'own_load_names': return target._net_load_raws(handle(load_list=[handle(full_name=bad)]))
    if case == 'degraded_top': return target._netlist_usable(NS(get_top_inst_list=bad), 'top')
    raise AssertionError(case)


@pytest.mark.parametrize('case', GETTERS)
@pytest.mark.parametrize('kind', KINDS)
def test_deep_best_effort_getters_preserve_original_fatal_object(case, kind):
    error = failure(kind)
    with pytest.raises(type(error)) as caught:
        invoke_getter(case, error)
    assert caught.value is error


@pytest.mark.parametrize('case', GETTERS)
def test_ordinary_missing_getter_data_keeps_best_effort_compatibility(case):
    value = invoke_getter(case, ValueError('ordinary absent native property'))
    expected = {
        'output_boundary': False, 'driver_boundary': False, 'inst_file': (None, 11),
        'inst_line': ('source.sv', None), 'pin_source': {}, 'degraded_top': False,
    }
    if case == 'handle_key':
        assert value[0] == 'object' and isinstance(value[1], int)
    elif case in expected:
        assert value == expected[case]
    elif case == 'own_load_names':
        assert value is None
    elif case in ('path_name', 'path_scope'):
        assert value['backend'] == 'verdi_npi' and value['is_endpoint'] is True
        assert value['net_path'] == ('' if case == 'path_name' else 'top.u:Always0#Always0:11:11:Reg.ROH_q')
        assert value['scope_inst'] is None
    else:
        assert value is None


@pytest.mark.parametrize('stage', ['get_net', 'get_actual_net'])
@pytest.mark.parametrize('kind', KINDS)
def test_net_resolution_never_tries_alternate_after_fatal(stage, kind):
    error, calls = failure(kind), []
    def get_net(name):
        calls.append('net')
        if stage == 'get_net': raise error
        return None
    def actual(name):
        calls.append('actual')
        raise error
    target = backend(NS(get_net=get_net, get_actual_net=actual))
    with pytest.raises(type(error)) as caught:
        target._resolve_net(target._npi_modules[1], 'top.q[0]')
    assert caught.value is error
    assert calls == (['net'] if stage == 'get_net' else ['net', 'actual'])


def test_ordinary_net_lookup_failure_can_still_use_actual_net():
    expected = object()
    target = backend(NS(get_net=throwing(ValueError('missing alias')),
                        get_actual_net=lambda path: expected))
    assert target._resolve_net(target._npi_modules[1], 'top.q[0]') is expected


@pytest.mark.parametrize('stage', ['connected_pin', 'connected_net', 'parent_loads'])
@pytest.mark.parametrize('kind', (*KINDS, 'ordinary'))
def test_output_boundary_recovery_cannot_return_prefix_consumers_after_fatal(stage, kind):
    error = ValueError('ordinary boundary miss') if kind == 'ordinary' else failure(kind)
    parent = handle(full_name='top.q', type='npiNlDeclNet',
                    load_list=throwing(error) if stage == 'parent_loads' else [])
    peer = handle(connected_net=throwing(error) if stage == 'connected_net' else parent)
    boundary = handle(type='npiNlPort', direction='npiNlOutput',
                      connected_pin=throwing(error) if stage == 'connected_pin' else peer)
    consumer = handle(full_name='top.known_consumer')
    initial = handle(full_name='top.child.q', type='npiNlDeclNet')
    target = backend()
    def query():
        return target._bounded_direct_load_handles(initial, [consumer, boundary], handle_limit=8, state_limit=8)
    if kind == 'ordinary':
        consumers, limited, recovery_failed = query()
        assert consumers == [consumer] and not limited and recovery_failed
    else:
        with pytest.raises(type(error)) as caught:
            query()
        assert caught.value is error
    assert target._fallback.calls == []


@pytest.mark.parametrize('operation', ['driver', 'loads', 'path'])
@pytest.mark.parametrize('kind', (*KINDS, 'ordinary'))
def test_native_collection_catches_propagate_fatal_but_keep_ordinary_status(operation, kind):
    error = RuntimeError('ordinary native failure') if kind == 'ordinary' else failure(kind)
    a, b = handle(full_name='top.a'), handle(full_name='top.b')
    a.driver_list = a.load_list = throwing(error)
    netlist = NS(get_net=lambda path: a if path == 'top.a' else b,
                 sig_to_sig_conn_list=throwing(error))
    target = backend(netlist)
    query = {'driver': lambda: target._npi_find_driver('top.a', 'wave.vcd', 'top', recursive=False),
             'loads': lambda: target._npi_find_loads('top.a', {}, 'candidate', 'top', True, None),
             'path': lambda: target._npi_find_path('top.a', 'top.b', expand_assigns=False)}[operation]
    if kind == 'ordinary':
        result = query()
        assert result['backend'] == 'verdi_npi'
        expected = {'driver': 'npi_driver_list_failed', 'loads': 'npi_load_list_failed', 'path': 'npi_call_failed'}
        assert result.get('unsupported_reason') == expected[operation] or result.get('stopped_at') == expected[operation]
    else:
        with pytest.raises(type(error)) as caught: query()
        assert caught.value is error
    assert target._fallback.calls == []


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('reset_kind', (*KINDS, 'ordinary', 'success'))
@pytest.mark.parametrize('stage', ['registration', 'traversal'])
def test_recursive_callback_reset_cannot_replace_existing_fatal_and_releases_lock(kind, reset_kind, stage):
    primary, calls = failure(kind), []
    secondary = RuntimeError('ordinary reset failure') if reset_kind == 'ordinary' else (
        None if reset_kind == 'success' else failure(reset_kind))
    def register(*args):
        calls.append('register')
        if stage == 'registration': raise primary
        return 1
    def traverse(**kw):
        calls.append('traversal')
        raise primary
    def reset():
        calls.append('reset')
        if secondary is not None: raise secondary
    netlist = NS(FuncType=NS(FAN_IN='fan_in'), register_cb=register, reset_cb=reset)
    target = backend(netlist)
    with pytest.raises(type(primary)) as caught:
        target._bounded_fan_in_chain(NS(fan_in_reg_list=traverse), 'top.q', 'top',
                                    state_limit=8, output_limit=4, initial_work_truncated=False)
    assert caught.value is primary
    assert calls == (['register', 'reset'] if stage == 'registration' else ['register', 'traversal', 'reset'])
    assert not npi._NPI_FAN_IN_CALLBACK_LOCK.locked() and target._fallback.calls == []


@pytest.mark.parametrize('kind', KINDS)
def test_callback_reset_fatal_without_prior_error_propagates(kind):
    error, calls = failure(kind), []
    def reset(): calls.append('reset'); raise error
    target = backend(NS(FuncType=NS(FAN_IN='fan_in'), register_cb=lambda *a: 1, reset_cb=reset))
    with pytest.raises(type(error)) as caught:
        target._bounded_fan_in_chain(NS(fan_in_reg_list=lambda **kw: []), 'top.q', 'top',
                                    state_limit=8, output_limit=4, initial_work_truncated=False)
    assert caught.value is error and calls == ['reset']
    assert not npi._NPI_FAN_IN_CALLBACK_LOCK.locked()


@pytest.mark.parametrize('operation', ['driver', 'loads', 'path'])
@pytest.mark.parametrize('kind', (*KINDS, 'ordinary'))
def test_local_public_backend_catches_do_not_advance_fallback_for_fatal(monkeypatch, operation, kind):
    error = ValueError('ordinary query failure') if kind == 'ordinary' else failure(kind)
    target, entered = backend(), []
    target.bind_compile_context({'top_modules': ['top']})
    monkeypatch.setattr(target, '_kdb_path_from', lambda *a: 'candidate')
    monkeypatch.setattr(target, '_ensure_loaded', lambda *a: True)
    monkeypatch.setattr(target, '_record_query_kdb_status', lambda: None)
    monkeypatch.setattr(target, '_finish_bound_query', lambda result: result)
    def native_query(*args, **kwargs):
        entered.append(operation)
        raise error
    monkeypatch.setattr(target, '_npi_find_' + operation, native_query)
    query = {'driver': lambda: target.find_driver('top.q', 'wave.vcd', 'compile.log'),
             'loads': lambda: target.find_loads('top.q', 'compile.log'),
             'path': lambda: target.find_path('top.a', 'top.b', 'compile.log')}[operation]
    if kind == 'ordinary':
        assert query()['control_fallback'] and target._fallback.calls == [operation]
    else:
        with pytest.raises(type(error)) as caught: query()
        assert caught.value is error and target._fallback.calls == []
    assert entered == [operation]


@pytest.mark.parametrize('kind', (*KINDS, 'ordinary'))
def test_dynamic_projector_catch_never_demotes_fatal_to_semantic_gap(kind):
    error = RuntimeError('ordinary projection failure') if kind == 'ordinary' else failure(kind)
    root = NS(size=lambda: 1, left=lambda: 0, right=lambda: 0, is_signed=lambda: False,
              full_name=lambda: 'top.q', is_literal=lambda: False, type=lambda: 'npiNlDeclNet',
              driver_list=throwing(error))
    target = backend(NS(get_net=lambda path: root))
    if kind == 'ordinary':
        result = npi_dynamic.query_step(target, 'top.q')
        assert not result['complete'] and 'npi_dynamic_query_failed' in result['gaps']
    else:
        with pytest.raises(type(error)) as caught: npi_dynamic.query_step(target, 'top.q')
        assert caught.value is error
    assert target._fallback.calls == []


@pytest.mark.parametrize('kind', KINDS)
def test_outer_local_dynamic_entry_preserves_projector_fatal(monkeypatch, kind):
    error, target, entered = failure(kind), backend(), []
    target.bind_compile_context({'top_modules': ['top']})
    monkeypatch.setattr(target, '_kdb_path_from', lambda *a: 'candidate')
    monkeypatch.setattr(target, '_ensure_loaded', lambda *a: True)
    monkeypatch.setattr(target, '_record_query_kdb_status', lambda: None)
    def project(*args, **kwargs):
        entered.append('project')
        raise error
    monkeypatch.setattr(npi_dynamic, 'query_step', project)
    with pytest.raises(type(error)) as caught: target.get_dynamic_step('top.q', 'compile.log')
    assert caught.value is error and target._fallback.calls == []
    assert entered == ['project']


async def inline(fn, *args, **kwargs): return fn()


async def no_root(*args, **kwargs): return None


def route_args():
    return dict(signal_path='top.q', wave_path='wave.vcd', compile_log='compile.log', time_ps=0,
                from_signal='top.a', to_signal='top.b', top_hint='top')


async def route(operation, args):
    if operation == 'path': return await server._route_public_signal_path(args=args, simulator='vcs')
    if operation == 'x': return await server._handle_trace_x_source(args, 'vcs')
    return await server._route_public_connectivity(operation=operation, args=args, simulator='vcs')


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['driver', 'loads', 'path', 'x'])
@pytest.mark.parametrize('stage', ['selector', 'query'])
@pytest.mark.parametrize('kind', KINDS)
async def test_public_routes_preserve_fatal_at_selector_and_query_without_source_fallback(
    monkeypatch, operation, stage, kind,
):
    error, calls = failure(kind), []
    monkeypatch.setattr(server, '_run_in_cancellable_thread', inline)
    monkeypatch.setattr(server, '_bind_wave_design_root', no_root)
    monkeypatch.setattr(server, '_safe_probe_backend', lambda *a: {})
    monkeypatch.setattr(server, 'get_source_graph_execution_config', forbidden)
    selected = NS(name='verdi_npi')
    def select(*a, **kw):
        calls.append('selector')
        if stage == 'selector': raise error
        return selected
    async def query(*a, **kw):
        calls.append('query')
        raise error
    monkeypatch.setattr(connectivity, 'select_backend', select)
    for name in ('_call_public_connectivity_operation', '_call_signal_path_backend', '_run_trace_x_attempt'):
        monkeypatch.setattr(server, name, query)
    expected_type = asyncio.CancelledError if kind == 'cancel' and stage == 'query' else type(error)
    with pytest.raises(expected_type) as caught:
        await route(operation, route_args())
    assert (caught.value.__cause__ is error if expected_type is asyncio.CancelledError else caught.value is error)
    assert calls == (['selector'] if stage == 'selector' else ['selector', 'query'])


class FallbackReached(BaseException):
    pass


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['driver', 'loads', 'path', 'x'])
@pytest.mark.parametrize('stage', ['selector', 'query'])
async def test_ordinary_public_route_failure_still_reaches_source_policy(monkeypatch, operation, stage):
    calls = []
    monkeypatch.setattr(server, '_run_in_cancellable_thread', inline)
    monkeypatch.setattr(server, '_bind_wave_design_root', no_root)
    monkeypatch.setattr(server, '_safe_probe_backend', lambda *a: {})
    def select(*a, **kw):
        if stage == 'selector': raise ValueError('ordinary setup failure')
        return NS(name='verdi_npi')
    async def query(*a, **kw): raise ValueError('ordinary query failure')
    def source_policy():
        calls.append('source_policy')
        raise FallbackReached()
    monkeypatch.setattr(connectivity, 'select_backend', select)
    monkeypatch.setattr(server, 'get_source_graph_execution_config', source_policy)
    for name in ('_call_public_connectivity_operation', '_call_signal_path_backend', '_run_trace_x_attempt'):
        monkeypatch.setattr(server, name, query)
    with pytest.raises(FallbackReached): await route(operation, route_args())
    assert calls == ['source_policy']  # Control marker only, no fabricated source facts.


@pytest.mark.anyio
@pytest.mark.parametrize('kind', KINDS)
async def test_reactive_source_frontier_fatal_cannot_publish_first_artifact_prefix(monkeypatch, kind):
    error, calls = failure(kind), []
    monkeypatch.setattr(server, '_run_in_cancellable_thread', inline)
    monkeypatch.setattr(server, '_safe_probe_backend', lambda *a: {})
    monkeypatch.setattr(connectivity, 'select_backend', lambda *a, **kw: NS(name='unavailable'))
    monkeypatch.setattr(server, 'get_source_graph_execution_config', lambda: SourceGraphExecutionConfig(
        enabled=True, python_bin='/fake/python', frontend_version='11.0.0', timeout_sec=5))
    async def context(**kw): return {'compile_result': {}}, 'snapshot', None, None
    monkeypatch.setattr(server, '_resolve_connectivity_hierarchy_context', context)
    plan = NS(status=AdapterStatus.READY, scope_expansion_anchors=(),
              request=NS(artifact_identity=NS(semantic_context=None)))
    monkeypatch.setattr(server, 'build_source_graph_plan', lambda **kw: plan)
    monkeypatch.setattr(server, 'compute_source_graph_build_key', lambda request: NS(digest='first-artifact'))
    async def first(**kw):
        calls.append('first_partial_artifact')
        return dict(outcome=None, receipt={'metrics': {}}, reason='scope_incomplete',
                    result={'backend': 'source_graph'},
                    query={'status': 'inconclusive', 'coverage_status': 'partial',
                           'unresolved_boundary_codes': ['hierarchy_projection_scoped']},
                    frontiers=('top.other.q',))
    def expand(**kw): calls.append('frontier'); raise error
    monkeypatch.setattr(server, '_execute_source_graph_connectivity_plan', first)
    monkeypatch.setattr(server, '_single_backend_provenance', lambda *a, **kw: True)
    monkeypatch.setattr(server, 'build_source_graph_frontier_plan', expand)
    monkeypatch.setattr(connectivity.StaticConnectivityBackend, 'find_driver', forbidden)
    expected = asyncio.CancelledError if kind == 'cancel' else type(error)
    with pytest.raises(expected) as caught:
        await route('driver', route_args())
    assert (caught.value.__cause__ is error if kind == 'cancel' else caught.value is error)
    assert calls == ['first_partial_artifact', 'frontier']


@pytest.mark.anyio
@pytest.mark.parametrize('operation,stage', [
    ('path', 'adapter'), ('path', 'prepare'), ('path', 'query'),
    ('x', 'adapter'), ('x', 'prepare'), ('x', 'query'), ('x', 'expanded_adapter'),
])
@pytest.mark.parametrize('kind', KINDS)
async def test_source_path_and_x_fatal_stages_never_become_source_failure_or_static(
    monkeypatch, operation, stage, kind,
):
    # Stage controls deliberately do not claim IR or query semantics. They
    # place a typed error inside each formerly broad best-effort catch.
    error, calls = failure(kind), []
    monkeypatch.setattr(server, '_run_in_cancellable_thread', inline)
    monkeypatch.setattr(server, '_bind_wave_design_root', no_root)
    monkeypatch.setattr(server, '_safe_probe_backend', lambda *a: {})
    monkeypatch.setattr(connectivity, 'select_backend', lambda *a, **kw: NS(name='unavailable'))
    monkeypatch.setattr(server, 'get_source_graph_execution_config', lambda: SourceGraphExecutionConfig(
        enabled=True, python_bin='/fake/python', frontend_version='11.0.0', timeout_sec=5))
    monkeypatch.setattr(server, '_resolve_hierarchy_context', lambda *a: ({'compile_result': {}}, 'snapshot'))
    request = NS()
    plan = NS(status=AdapterStatus.READY, request=request, receipt=NS(to_dict=lambda: {}))
    def adapter(**kw):
        calls.append('adapter')
        if stage == 'adapter' or (stage == 'expanded_adapter' and calls.count('adapter') == 2):
            raise error
        return plan
    monkeypatch.setattr(server, 'build_source_graph_path_plan', adapter)
    monkeypatch.setattr(server, 'build_source_graph_trace_plan', adapter)
    entry = NS(build_key=NS(digest='artifact'), artifact_scope_receipt=NS(scope=object()))
    async def prepare(*a, **kw):
        calls.append('prepare')
        if stage == 'prepare': raise error
        return NS(status=PrepareStatus.READY, entry=entry, metrics=None)
    monkeypatch.setattr(server, 'get_source_graph_runtime', lambda config: NS(prepare=prepare))
    monkeypatch.setattr(server, '_record_source_graph_prepare_metrics', lambda outcome: None)
    monkeypatch.setattr(server, '_accumulate_source_graph_trace_metrics', lambda *a, **kw: None)
    monkeypatch.setattr(server, 'compute_source_graph_build_key', lambda request: NS(digest='artifact'))
    monkeypatch.setattr(server, '_source_graph_receipt_from_prepare', lambda *a, **kw: {'metrics': {}})
    monkeypatch.setattr(server, '_source_graph_backend_for_plan', lambda *a: NS(name='source_graph'))
    monkeypatch.setattr(server, 'SourceGraphTraceConnectivityBackend', lambda **kw: NS(
        require_scope=forbidden, ledger=NS(query_fingerprints_sha256=[]), query_wall_ms=0))
    async def query(*a, **kw):
        calls.append('query')
        if stage == 'expanded_adapter': raise SourceGraphTraceScopeExpansion(['top.other.q'])
        raise error
    monkeypatch.setattr(server, '_call_signal_path_backend', query)
    monkeypatch.setattr(server, '_run_trace_x_attempt', query)
    monkeypatch.setattr(connectivity.StaticConnectivityBackend, 'find_driver', forbidden)
    monkeypatch.setattr(connectivity.StaticConnectivityBackend, 'find_path', forbidden)
    expected = asyncio.CancelledError if kind == 'cancel' else type(error)
    with pytest.raises(expected) as caught: await route(operation, route_args())
    assert (caught.value.__cause__ is error if kind == 'cancel' else caught.value is error)
    assert calls == {'adapter': ['adapter'], 'prepare': ['adapter', 'prepare'],
                     'query': ['adapter', 'prepare', 'query'],
                     'expanded_adapter': ['adapter', 'prepare', 'query', 'adapter']}[stage]


@pytest.mark.anyio
@pytest.mark.parametrize('kind', KINDS)
async def test_x_chain_later_driver_fatal_discards_already_observed_first_node(kind):
    error, calls = failure(kind), []
    def driver(path):
        calls.append(path)
        if path == 'top.upstream': raise error
        return dict(driver_status='resolved', driver_kind='assign', upstream_signals=['top.upstream'],
                    backend='verdi_npi', source_file='source.sv', source_line=11)
    def value(path, time): return {'value': 'x', 'has_x': True, 'has_z': False}
    def upstream(names, current, time):
        return [{'name': 'top.upstream', 'path': 'top.upstream', 'value': value('top.upstream', time)}]
    with pytest.raises(type(error)) as caught:
        await x_trace.trace_x_source('wave.vcd', 'top.q', 0, 'compile.log',
            driver_lookup=driver, value_lookup=value, upstream_lookup=upstream)
    assert caught.value is error and calls == ['top.q', 'top.upstream']


@pytest.mark.parametrize('where', ['candidate', 'search', 'sample'])
@pytest.mark.parametrize('kind', ['cancel', 'timeout'])
def test_x_wave_best_effort_does_not_swallow_cancel_or_timeout(where, kind):
    error = failure(kind)
    reads = []
    def read(path, time):
        reads.append((path, time))
        if where == 'candidate' or (where == 'sample' and time == 7): raise error
        if where == 'search': raise KeyError(path)
        return {'value': 'x'}
    parser = NS(get_value_at_time=read, search_signals=throwing(error))
    with pytest.raises(type(error)) as caught:
        x_trace.inspect_upstream_values(parser, ['q'], 'top.out', 7)
    assert caught.value is error and reads


@pytest.mark.anyio
async def test_dependency_timeout_keeps_existing_positive_driver_and_no_backend_switch(monkeypatch):
    # Optional dependency inventory timeout is intentionally a result gap. It
    # does not invalidate an already resolved driver from that same backend.
    original = {'backend': 'verdi_npi', 'driver_status': 'resolved', 'source_file': 'source.sv', 'source_line': 11}
    result = dict(original)
    target = NS(name='verdi_npi', get_dynamic_step=throwing(TimeoutError('optional inventory deadline')))
    async def query(backend, fn): return fn()
    monkeypatch.setattr(server, '_call_connectivity_backend', query)
    monkeypatch.setattr(server, '_run_in_wave_thread', forbidden)
    await server._attach_driver_dependencies(result, target,
        dict(include_dependencies=True, signal_path='top.q', compile_log='compile.log', wave_path='wave.vcd'), 'vcs')
    assert {key: result[key] for key in original} == original
    assert 'dependency_timeout' in result['dependency_context']['gaps']
    assert not result['dependency_context']['complete']


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['cancel', 'capture', 'kdb'])
async def test_optional_dependency_query_still_propagates_lifecycle_fatal(monkeypatch, kind):
    error = failure(kind)
    original = {'backend': 'verdi_npi', 'driver_status': 'resolved', 'source_line': 11}
    result = dict(original)
    target = NS(name='verdi_npi', get_dynamic_step=throwing(error))
    async def query(backend, fn): return fn()
    monkeypatch.setattr(server, '_call_connectivity_backend', query)
    monkeypatch.setattr(server, '_run_in_wave_thread', forbidden)
    with pytest.raises(type(error)) as caught:
        await server._attach_driver_dependencies(result, target,
            dict(include_dependencies=True, signal_path='top.q', compile_log='compile.log', wave_path='wave.vcd'), 'vcs')
    assert caught.value is error and result == original
