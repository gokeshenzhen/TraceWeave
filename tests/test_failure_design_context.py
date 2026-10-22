"""Failure consumers over independent designs, with handwritten source facts.

Real tiny compile text/capture and VCD reads; the NPI-labelled five-scope
provider is a control only. No SDK/LSF/Slang or real elaboration is exercised.
The exact log hint and direct-child candidate domain never prove root cause.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
import threading
import time

import anyio
import jsonschema
import pytest

import server
from src import analyzer, connectivity_backend
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.connectivity_ir import SourceLocation
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context_runtime import ContextUnavailable
from src.hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyScope, ScopeLookup
from src.kdb_anchor import KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable
from tests.test_public_design_instance import anyio_backend, isolate, forbidden, prepare, project, public


TOOLS = ('analyze_failure_event', 'recommend_failure_debug_next_steps')
ORIGINAL_GET_PARSER = server._get_parser
EVENT = {'event_id': 'hand-event', 'kind': 'assertion', 'time_ps': 2000, 'line': 1,
         'instance_path': 'top.a.deep', 'source_file': '/raw/checker.sv', 'message': 'deep assertion failed'}


class Provider:
    origin, top = 'verdi_npi', 'top'

    def __init__(self, controls, artifact):
        self.controls, self.artifact_identity = controls, artifact
        self.facts = {}
        for path, parent, definition in (('top', None, 'top'), ('top.a', 'top', 'leaf'),
            ('top.b', 'top', 'leaf'), ('top.c', 'top', 'leaf'), ('top.a.deep', 'top.a', 'leaf')):
            self.facts[path] = HierarchyScope('scope-' + path, path, path.rsplit('.', 1)[-1], 'module', parent,
                'def-' + definition, definition, SourceLocation('/raw/sdk/instances.sv', 23),
                SourceLocation(str(controls.source), 1))

    def lookup_scope(self, path, budget):
        self.controls.outside_wave()
        budget.call()
        self.controls.lookups.append(path)
        return ScopeLookup(None if self.controls.empty else self.facts.get(path), not self.controls.partial,
                           ('hand_scope_incomplete',) if self.controls.partial else ())

    @contextmanager
    def open_children(self, scope, budget):
        controls = self.controls
        controls.outside_wave()
        controls.opened.append(scope.path)
        budget.call()
        paths = {'top': ('top.a', 'top.b', 'top.c'), 'top.a': ('top.a.deep',)}.get(scope.path, ())
        facts = [self.facts[path] for path in paths]
        class Stream:
            complete, gaps = False, ('hand_scope_incomplete',) if controls.partial else ()
            def __init__(self): self.index = 0
            def __iter__(self): return self
            def __next__(self):
                budget.call()
                if self.index == len(facts):
                    self.complete = not controls.partial
                    raise StopIteration
                value = facts[self.index]
                self.index += 1
                return value
        try:
            yield Stream()
        finally:
            controls.closed.append(scope.path)
            if controls.close_action: controls.close_action()


class Controls:
    def __init__(self):
        self.thread = threading.get_ident()
        self.requests, self.selections, self.lookups, self.opened, self.closed = [], [], [], [], []
        self.wave_threads, self.validation_wave_states = [], []
        self.wave_active = False
        self.empty = self.partial = False
        self.action = self.after = self.selector_action = self.close_action = None

    def outside_wave(self):
        assert not self.wave_active

    def select(self, status, *, fallback):
        self.outside_wave()
        assert threading.get_ident() == self.thread
        self.selections.append(deepcopy(status))
        if self.selector_action: self.selector_action()
        controls = self
        class Backend:
            name, uses_external_worker, kdb_status = 'verdi_npi', False, None
            def bind_compile_context(self, metadata): self.metadata = deepcopy(metadata)
            def bind_kdb_anchor(self, anchor): self.anchor = anchor
            def query_source(self, request):
                controls.outside_wave()
                assert threading.get_ident() == controls.thread
                assert self.metadata == request.context.compile_result()
                controls.requests.append(request)
                if controls.action: controls.action(request, len(controls.requests))
                identity = hashlib.sha256((self.anchor.identity_sha256 + '\0' + request.context.selected_top).encode()).hexdigest()
                value = request.execute(Provider(controls, identity))
                self.kdb_status = {'load_quality': 'clean'}
                if hasattr(request.context, 'identity_sha256'):
                    value['context_identity_sha256'] = request.context.identity_sha256
                value['kdb_binding'] = dict(self.anchor.receipt(), load_status='loaded', load_quality='clean')
                if controls.after: controls.after(request, len(controls.requests))
                return value
        return Backend()


@pytest.fixture
def controls(monkeypatch, tmp_path):
    value = Controls()
    value.captures = []
    capture_init = CompileInputCapture.__init__
    def observed_capture(self, *a, **kw):
        capture_init(self, *a, **kw)
        value.captures.append(self)
    monkeypatch.setattr(CompileInputCapture, '__init__', observed_capture)
    value.source, value.log, value.maps = project(tmp_path)
    value.wave, value.run = tmp_path / 'wave.vcd', tmp_path / 'run.log'
    value.wave.write_text('$timescale 1ps $end\n$scope module top $end\n$var wire 1 ! req $end\n'
        '$scope module a $end\n$var wire 1 " req $end\n$scope module deep $end\n$var wire 1 # req $end\n'
        '$upscope $end\n$upscope $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n0"\n0#\n#2000\n1!\n1"\n1#\n')
    value.run.write_text('"/raw/checker.sv", 3: top.a.deep: started at 1000ps failed at 2000ps\n')
    monkeypatch.setattr(connectivity_backend, 'select_backend', value.select)
    monkeypatch.setattr(server, '_build_recovery_context', forbidden)
    original_wave = server._run_in_wave_request
    async def wave_request(paths, fn, **kwargs):
        def checked():
            assert threading.get_ident() != value.thread
            locks = server._wave_locks_for([paths] if isinstance(paths, str) else paths)
            assert all(lock.locked() for lock in locks)
            value.wave_active = True
            value.wave_threads.append(threading.get_ident())
            try: return fn()
            finally: value.wave_active = False
        return await original_wave(paths, checked, **kwargs)
    monkeypatch.setattr(server, '_run_in_wave_request', wave_request)
    def get_parser(path):
        assert value.wave_active and threading.get_ident() != value.thread
        return ORIGINAL_GET_PARSER(path)
    monkeypatch.setattr(server, '_get_parser', get_parser)
    for name in ('_validate_design_source', '_validate_design_binding'):
        original = getattr(server, name)
        async def validate(*a, _fn=original, **kwargs):
            value.validation_wave_states.append(value.wave_active)
            value.outside_wave()
            return await _fn(*a, **kwargs)
        monkeypatch.setattr(server, name, validate)
    return value


def arguments(controls, tool, **extra):
    result = {'log_path': str(controls.run), 'wave_path': str(controls.wave), 'simulator': 'vcs', **extra}
    if tool == 'analyze_failure_event': result['failure_event'] = deepcopy(EVENT)
    return result


async def invoke(controls, tool, **extra):
    return await public(tool, **arguments(controls, tool, **extra))


def candidates(result, tool):
    return result['likely_instances' if tool == TOOLS[0] else 'recommended_instances']


@pytest.mark.anyio
@pytest.mark.parametrize('tool', TOOLS)
async def test_public_schemas_accept_independent_and_automatic_compile_context(tool):
    schema = next(item.inputSchema for item in await server.list_tools() if item.name == tool)
    args = {'log_path': '/run.log', 'wave_path': '/wave.vcd', 'simulator': 'vcs'}
    if tool == TOOLS[0]: args['failure_event'] = deepcopy(EVENT)
    jsonschema.validate(dict(args, design_handle='dc_example', timeout_sec=1.5), schema)
    jsonschema.validate(dict(args, compile_log='/compile.log', top_hint='top'), schema)
    for timeout in (0, -1, 301, 'slow', True):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(dict(args, design_handle='dc_example', timeout_sec=timeout), schema)


@pytest.mark.anyio
@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('automatic', [False, True])
async def test_five_scope_hand_oracle_no_hidden_tree_and_shared_budget(controls, tool, automatic):
    selector = {'compile_log': str(controls.log)} if automatic else {'design_handle': await prepare(controls.log)}
    result = await invoke(controls, tool, **selector)
    assert 'error' not in result, result
    assert [r.operation for r in controls.requests] == ['find_tb_instance', 'get_tb_subtree']
    exact, shallow = controls.requests
    assert json.loads(exact.arguments_json) == {'path': 'top.a.deep'}
    assert json.loads(shallow.arguments_json) == {'root': 'top', 'depth': 1, 'max_nodes': 128, 'count_mode': 'bounded'}
    assert exact.budget is shallow.budget
    assert (exact.budget.max_scopes, exact.budget.max_calls, exact.budget.max_bytes) == (256, 8192, 1024 * 1024)
    observed = candidates(result, tool)
    # The existing heuristic scores suffix overlap, not every path ancestor.
    assert [row['instance_path'] for row in observed] == ['top.a.deep', 'top', 'top.a', 'top.b', 'top.c']
    assert [row['score'] for row in observed] == [10, 2, 2, 2, 2]
    assert {row['instance_path'] for row in observed} == {'top', 'top.a', 'top.b', 'top.c', 'top.a.deep'}
    assert all(row['src'] == str(controls.source) for row in observed)
    deep = next(row['source_evidence'] for row in observed if row['instance_path'] == 'top.a.deep')
    assert deep['definition_file'] == str(controls.source) and deep['definition_line'] == 1
    assert deep['source_file'] == '/raw/sdk/instances.sv' and 'npi_instance_location_unbound' in deep['gaps']
    correlation = result['source_correlation']
    assert correlation['facts_origin'] == 'verdi_npi' and correlation['candidate_count'] == 5
    assert correlation['coverage_status'] == 'partial' and correlation['full_hierarchy_fallback'] is False
    assert correlation['selection'] == 'exact_log_path_and_top_direct_children'
    assert 'failure_candidate_scope_bounded' in correlation['coverage_gaps']
    assert controls.wave_threads and controls.thread not in controls.wave_threads
    assert not any(controls.validation_wave_states)
    assert bool(controls.validation_wave_states) is (not automatic)
    assert sorted(controls.opened) == sorted(controls.closed)
    assert controls.captures and all(c.body_count == c.resident_bytes == 0 for c in controls.captures)
    assert server._session_state['build_tb_hierarchy'] is None and server._result_cache['build_tb_hierarchy'] is None
    if automatic:
        assert result['native_route']['source_context_status'] == 'not_required'
        assert 'design_context' not in result['backend_status']
    else:
        receipt = result['backend_status']['design_context']
        assert receipt['artifact']['load_status'] == 'loaded' and receipt['artifact']['source_association'] == 'unverified'
    if tool == TOOLS[1]:
        assert server._result_cache[tool] is not None
        assert result['workflow_incomplete'] and result['required_next_call']['tool'] == 'scan_structural_risks'


@pytest.mark.anyio
@pytest.mark.parametrize('partial', [False, True])
async def test_native_empty_or_partial_never_fills_candidates_with_full_source(controls, partial):
    controls.empty, controls.partial = True, partial
    handle = await prepare(controls.log)
    result = await invoke(controls, TOOLS[0], design_handle=handle)
    assert 'error' not in result and candidates(result, TOOLS[0]) == []
    assert result['source_correlation']['candidate_count'] == 0
    assert result['source_correlation']['facts_origin'] == 'verdi_npi'
    assert result['source_correlation']['full_hierarchy_fallback'] is False
    assert len(controls.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize('second_stop', ['unavailable', 'native_call_limit'])
async def test_second_unavailable_keeps_only_first_native_artifact(controls, second_stop):
    handle = await prepare(controls.log)
    def action(request, index):
        if index == 2:
            if second_stop == 'native_call_limit': raise HierarchyBudgetExceeded(second_stop)
            raise NpiHierarchyUnavailable('npi_load_failed')
    controls.action = action
    result = await invoke(controls, TOOLS[0], design_handle=handle)
    assert [row['instance_path'] for row in candidates(result, TOOLS[0])] == ['top.a.deep']
    assert result['source_correlation']['facts_origin'] == 'verdi_npi'
    assert 'failure_candidate_query_unavailable' in result['source_correlation']['coverage_gaps']
    assert result['backend_status']['actual_backend'] == 'verdi_npi'
    assert result['source_correlation']['full_hierarchy_fallback'] is False
    attempts = result['backend_status']['attempted_backends']
    assert len(attempts) >= 2
    assert result['source_correlation']['queries'][1]['backend_status']['attempted_backends'][-1] in attempts


@pytest.mark.anyio
async def test_unavailable_source_is_log_only_without_legacy_build(controls, monkeypatch):
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    handle = await prepare(controls.log)
    controls.action = lambda *_: (_ for _ in ()).throw(NpiHierarchyUnavailable('npi_load_failed'))
    result = await invoke(controls, TOOLS[0], design_handle=handle)
    assert len(controls.requests) == 1
    assert result['source_correlation']['facts_origin'] == 'log_only'
    assert result['source_correlation']['full_hierarchy_fallback'] is False
    assert result['backend_status'].get('actual_backend') is None
    assert server._result_cache['build_tb_hierarchy'] is None


@pytest.mark.anyio
@pytest.mark.parametrize('reason', ['native_call_limit', 'scope_limit', 'fact_byte_limit'])
async def test_native_analysis_limit_is_not_provider_unavailability(controls, reason):
    handle = await prepare(controls.log)
    controls.action = lambda *_: (_ for _ in ()).throw(HierarchyBudgetExceeded(reason))
    result = await invoke(controls, TOOLS[0], design_handle=handle)
    assert 'error' not in result, result
    assert len(controls.requests) == 1
    assert candidates(result, TOOLS[0]) == [{'instance_path': 'top.a.deep', 'score': 10, 'reason': 'exact log instance'}]
    assert result['source_correlation']['coverage_status'] == 'partial'
    assert reason in result['source_correlation']['coverage_gaps']
    assert result['source_correlation']['full_hierarchy_fallback'] is False


@pytest.mark.anyio
async def test_unresolved_top_keeps_raw_log_hint_without_any_source_query(controls):
    handle = await prepare(controls.log)
    with server._design_context_store.pin(handle) as entry:
        unresolved = replace(entry.context, selected_top=None, gaps=tuple(sorted(set(entry.context.gaps) | {'compile_tops_unresolved'})))
    handle = server._design_context_store.register(unresolved)
    result = await invoke(controls, TOOLS[0], design_handle=handle)
    assert controls.requests == controls.selections == []
    assert candidates(result, TOOLS[0]) == [{'instance_path': 'top.a.deep', 'score': 10, 'reason': 'exact log instance'}]
    assert result['source_correlation']['facts_origin'] == 'log_only'
    assert result['source_correlation']['full_hierarchy_fallback'] is False
    assert result['source_correlation']['queries'] == []
    assert 'compile_tops_unresolved' in result['source_correlation']['coverage_gaps']
    assert 'backend_status' not in result


TYPED = [(lambda: CaptureAborted('input_changed'), 'design_context_changed'),
         (lambda: CaptureAborted('byte_limit'), 'design_context_byte_limit'),
         (KdbBindingChanged, 'kdb_identity_changed'), (ExecutionBindingChanged, 'design_context_execution_changed'),
         (lambda: ContextUnavailable('design_context_changed'), 'design_context_changed'),
         (lambda: TimeoutError('explicit backend timeout'), 'design_context_timeout')]


@pytest.mark.anyio
@pytest.mark.parametrize('tool', TOOLS)
@pytest.mark.parametrize('factory,code', TYPED)
@pytest.mark.parametrize('stage', ['select', 'query', 'cleanup'])
async def test_typed_failure_never_advances_fallback_wave_or_cache(controls, tool, factory, code, stage):
    handle = await prepare(controls.log)
    error, entered = factory(), []
    def fail(*args):
        entered.append(stage)
        raise error
    if stage == 'select': controls.selector_action = fail
    elif stage == 'query': controls.action = fail
    else: controls.close_action = fail
    result = await invoke(controls, tool, design_handle=handle)
    # Unwinding a failed child close must still close its live parent. Each
    # separate open iterator gets one close, even when every close raises.
    assert entered == [stage] * (len(controls.opened) if stage == 'cleanup' else 1)
    assert result['error_code'] == code
    assert 'source_correlation' not in result and not controls.wave_threads
    assert server._result_cache[TOOLS[1]] is None
    if stage == 'cleanup': assert controls.opened and sorted(controls.opened) == sorted(controls.closed)


@pytest.mark.anyio
@pytest.mark.parametrize('tool', TOOLS)
async def test_operation_cancelled_keeps_cancellation_boundary_without_fallback(controls, tool):
    handle = await prepare(controls.log)
    entered = []
    def cancel(*args):
        entered.append(True)
        raise OperationCancelled('hand cancellation')
    controls.action = cancel
    with pytest.raises(asyncio.CancelledError):
        await invoke(controls, tool, design_handle=handle)
    assert entered == [True] and not controls.wave_threads
    assert server._result_cache[TOOLS[1]] is None
    assert server._design_context_store.remove(handle) is True


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['source', 'map', 'policy', 'reset'])
async def test_final_change_discards_recommendation_and_releases_pin(controls, change):
    handle = await prepare(controls.log)
    def changed(request, index):
        if index != 2: return
        if change == 'source': controls.source.write_text(controls.source.read_text() + '// change\n')
        elif change == 'map': (controls.maps / '_libFileMap').write_bytes(b'map-B')
        elif change == 'policy': os_environment_change()
        else: server.reset_session_state()
    # The policy mismatch is real frozen environment evidence, not a fake status.
    def os_environment_change():
        import os
        os.environ['TRACEWEAVE_NPI_EXECUTION'] = 'lsf'
    controls.after = changed
    result = await invoke(controls, TOOLS[1], design_handle=handle)
    expected = {'source': 'design_context_changed', 'map': 'kdb_identity_changed',
                'policy': 'design_context_execution_changed', 'reset': 'design_context_generation_changed'}
    assert len(controls.requests) == 2 and result['error_code'] == expected[change]
    assert server._result_cache[TOOLS[1]] is None
    assert 'recommended_instances' not in result and 'source_correlation' not in result
    if change != 'reset': assert server._design_context_store.remove(handle)


class ServerClock:
    def __init__(self): self.offset = 0
    def monotonic(self): return time.monotonic() + self.offset
    def __getattr__(self, name): return getattr(time, name)


@pytest.mark.anyio
async def test_native_metadata_consumes_original_total_deadline(controls, monkeypatch):
    from src import native_dispatch, native_query
    clock, entered = ServerClock(), []
    original = native_dispatch.read_native_inputs
    def read_then_advance(*a, **kw):
        result = original(*a, **kw)
        entered.append(result)
        clock.offset = 2
        return result
    monkeypatch.setattr(native_dispatch, 'time', clock)
    monkeypatch.setattr(native_query, 'time', clock)
    monkeypatch.setattr(native_dispatch, 'read_native_inputs', read_then_advance)
    result = await invoke(controls, TOOLS[1], compile_log=str(controls.log), timeout_sec=1)
    assert len(entered) == 1 and result['error_code'] == 'native_query_timeout'
    assert controls.requests == controls.selections == controls.wave_threads == []
    assert server._result_cache[TOOLS[1]] is None


@pytest.mark.anyio
async def test_late_final_validation_cannot_publish_recommend_cache(controls, monkeypatch):
    handle = await prepare(controls.log)
    clock, entered = ServerClock(), []
    original = server._validate_design_binding
    async def validate(*a, **kw):
        await original(*a, **kw)
        if len(controls.requests) == 2:
            entered.append('final')
            clock.offset = 10
    monkeypatch.setattr(server, 'time', clock)
    monkeypatch.setattr(server, '_validate_design_binding', validate)
    result = await invoke(controls, TOOLS[1], design_handle=handle, timeout_sec=1)
    assert entered == ['final'] and controls.wave_threads
    assert result['error_code'] == 'design_context_timeout' and server._result_cache[TOOLS[1]] is None
    assert server._design_context_store.remove(handle)


@pytest.mark.anyio
async def test_repeat_handle_does_not_reprepare_or_rediscover(controls, monkeypatch):
    handle = await prepare(controls.log)
    first = await invoke(controls, TOOLS[0], design_handle=handle)
    assert len(controls.requests) == 2
    monkeypatch.setattr(server, 'prepare_backend_binding', forbidden)
    monkeypatch.setattr(server, '_handle_prepare_design_context', forbidden)
    second = await invoke(controls, TOOLS[0], design_handle=handle)
    assert candidates(first, TOOLS[0]) == candidates(second, TOOLS[0])
    assert first['backend_status']['design_context']['binding_reused'] is False
    assert second['backend_status']['design_context']['binding_reused'] is True


@pytest.mark.anyio
@pytest.mark.parametrize('timeout', [0, -1, 301, True, 'slow', float('nan'), float('inf')])
async def test_invalid_failure_timeout_is_rejected_before_binding(controls, timeout):
    handle = await prepare(controls.log)
    result = await invoke(controls, TOOLS[0], design_handle=handle, timeout_sec=timeout)
    assert result['error_code'] == 'invalid_failure_analysis_arguments'
    assert controls.selections == controls.requests == controls.wave_threads == []


@pytest.mark.anyio
@pytest.mark.parametrize('factory,code', TYPED)
async def test_typed_final_validation_cannot_cache_a_completed_wave_result(controls, monkeypatch, factory, code):
    handle = await prepare(controls.log)
    original = server._validate_design_binding
    entered, error = [], factory()
    async def validate(*a, **kw):
        await original(*a, **kw)
        if len(controls.requests) == 2:
            entered.append(True)
            raise error
    monkeypatch.setattr(server, '_validate_design_binding', validate)
    result = await invoke(controls, TOOLS[1], design_handle=handle)
    assert entered == [True] and controls.wave_threads
    assert result['error_code'] == code and server._result_cache[TOOLS[1]] is None
    assert 'recommended_instances' not in result and server._design_context_store.remove(handle)


@pytest.mark.anyio
async def test_pin_covers_await_and_cancel_releases_without_late_cache(controls, monkeypatch):
    handle = await prepare(controls.log)
    original = server._run_in_wave_request
    waiting, release = asyncio.Event(), asyncio.Event()
    async def paused_wave(*a, **kw):
        waiting.set()
        await release.wait()
        return await original(*a, **kw)
    monkeypatch.setattr(server, '_run_in_wave_request', paused_wave)
    task = asyncio.create_task(invoke(controls, TOOLS[1], design_handle=handle))
    try:
        with anyio.fail_after(3): await waiting.wait()
        assert len(controls.requests) == 2 and server._design_context_store.remove(handle) is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert server._design_context_store.remove(handle) is True
        assert server._result_cache[TOOLS[1]] is None and not controls.wave_threads
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass


@pytest.mark.anyio
async def test_analyze_failures_reads_vcd_without_build_or_source_query(controls):
    server._session_state['get_sim_paths'] = {'simulator': 'vcs', 'compile_log': str(controls.log)}
    result = await public('analyze_failures', **arguments(controls, 'analyze_failures'), signal_paths=['top.a.deep.req'])
    assert 'error' not in result and result['summary']['runtime_total_errors'] == 1
    assert result['focused_event']['time_ps'] == 2000
    assert 'top.a.deep.req' in result['wave_context']['signals'] and controls.wave_threads
    assert controls.requests == controls.selections == []
    assert server._session_state['build_tb_hierarchy'] is None


def test_core_compile_log_is_only_hint_and_supplied_views_are_not_rebuilt(controls, monkeypatch):
    monkeypatch.setattr(server, 'parse_compile_log', forbidden)
    class Parser:
        def search_signals(self, *a, **kw): return {'results': []}
    core = analyzer.WaveformAnalyzer(str(controls.run), Parser(), 'vcs')
    result = core.analyze_failure_event(deepcopy(EVENT), str(controls.wave), compile_log=str(controls.log))
    assert result['source_correlation'] == {'facts_origin': 'log_only', 'coverage_status': 'partial',
                                            'coverage_gaps': ['source_context_not_supplied']}
    hierarchy = {'project': {'top_module': 'top'}, 'component_tree': {'top': {'class': 'top', 'src': '/legacy/top.sv'}}}
    original = deepcopy(hierarchy)
    result = core.analyze_failure_event(deepcopy(EVENT), str(controls.wave), hierarchy=hierarchy)
    assert hierarchy == original and result['likely_instances'][0]['instance_path'] == 'top'
    assert controls.requests == []
