"""Real isolated Slang layout queries. VCD declarations are authored fixtures."""
from dataclasses import replace
import json
import traceback
from pathlib import Path

import pytest
import server
from src import source_graph_runtime
from src.source_graph_contract import SemanticScopeSearch, compare_source_graph_artifact_scopes
from tests.test_prepared_source_connectivity import anyio_backend, isolation, public, prepare

FIXTURE = Path(__file__).parent / 'fixtures/prepared_trace/packed.sv'


def project(tmp):
    source = tmp / 'packed.sv'
    source.write_bytes(FIXTURE.read_bytes())
    log = tmp / 'compile.log'
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n       top\n")
    wave = tmp / 'packed.vcd'
    wave.write_text('$timescale 1ps $end\n$scope module top $end\n$scope module active $end\n'
        '$scope module small_bus $end\n$var wire 6 ! bus [5:0] $end\n$upscope $end\n'
        '$scope module large_bus $end\n$var wire 9 @ bus [8:0] $end\n$upscope $end\n'
        '$upscope $end\n$upscope $end\n$enddefinitions $end\n#0\nb000000 !\nb000000000 @\n')
    return source, log, wave


async def packed(tmp, handle, wave, instance='small_bus', **kw):
    width = 6 if instance == 'small_bus' else 9
    return await public(tmp, 'resolve_packed_fields', dict(design_handle=handle, wave_path=str(wave),
        signal_path=f'top.active.{instance}.bus[{width-1}:0]', source_signal=f'top.active.{instance}.bus',
        fields=['valid', 'payload.tag', 'payload.data'], timeout_sec=60, **kw))


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source', 'auto'])
async def test_prepared_generate_layout_and_exact_reuse(tmp_path, monkeypatch, policy):
    source, log, wave = project(tmp_path)
    calls = []
    original = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    async def observed(self, *a, **kw):
        assert not any(lock.locked() for lock in server._wave_locks_for([str(wave)]))
        calls.append(a[0].to_dict())
        return await original(self, *a, **kw)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', observed)
    handle = await prepare(tmp_path, log, policy)
    assert calls == []
    for instance, valid, tag, data in [('small_bus', [5], [4,3], [2,1,0]),
                                      ('large_bus', [8], [7,6], [5,4,3,2,1,0])]:
        result = await packed(tmp_path, handle, wave, instance)
        assert result['status'] == 'resolved', result
        assert result['backend_status']['actual_backend'] == 'source_graph', result
        assert {k:v['bits'] for k,v in result['fields'].items()} == {
            'valid': valid, 'payload.tag': tag, 'payload.data': data}
        count = len(calls)
        repeated = await packed(tmp_path, handle, wave, instance)
        assert repeated['fields'] == result['fields'] and len(calls) == count
        assert result['evidence']['source_graph']['prepare_status'] == 'ready'
    assert len(calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize('negative', ['missing', 'plain', 'field', 'dump_range'])
async def test_prepared_mapping_required_retains_reason(tmp_path, negative):
    source, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    args = dict(design_handle=handle, wave_path=str(wave), signal_path='top.active.small_bus.bus[5:0]',
                source_signal='top.active.small_bus.bus', fields=['valid'], timeout_sec=60)
    if negative == 'missing': args['source_signal'] = 'top.inactive.absent.bus'
    if negative == 'field': args['fields'] = ['absent']
    if negative == 'plain': args['source_signal'] = 'top.active.small_bus.plain'
    if negative == 'dump_range':
        wave.write_text(wave.read_text().replace('bus [5:0]', 'bus [6:1]'))
        args['signal_path'] = 'top.active.small_bus.bus[6:1]'
    result = await public(tmp_path, 'resolve_packed_fields', args)
    assert result['status'] == 'mapping_required' and result.get('fields', {}) == {}, result
    expected = {'missing': {'signal_not_declared', 'semantic_type_coverage_incomplete'},
                'plain': {'packed_member_type_evidence_missing'}, 'field': {'packed_member_type_evidence_missing'},
                'dump_range': {'dump_and_type_range_mismatch'}}
    assert set(result['reasons']) <= expected[negative] and result['reasons'], result


def test_layout_purpose_is_exact_capability_and_roundtrips():
    from tests.test_source_graph_runtime import _request
    scope = _request().artifact_identity.scope
    layout = replace(scope, semantic_search=SemanticScopeSearch(purpose='packed_layout'))
    assert type(layout).from_dict(layout.to_dict()) == layout
    for purpose in ('connectivity', 'hierarchy', 'trace'):
        other = replace(scope, semantic_search=SemanticScopeSearch(purpose=purpose,
            **({'roots': (scope.top,), 'depth': 0} if purpose == 'hierarchy' else {})))
        assert compare_source_graph_artifact_scopes(layout, other).value == 'unproven'
        assert compare_source_graph_artifact_scopes(other, layout).value == 'unproven'


@pytest.mark.anyio
@pytest.mark.parametrize('stop', ['timeout', 'cancel', 'changed'])
async def test_packed_preparation_stop_never_queries_layout(tmp_path, monkeypatch, stop):
    import asyncio
    from src.compile_input_capture import CaptureAborted
    source, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    calls = []
    async def stopped(self, *args, **kwargs):
        calls.append('prepare')
        if stop == 'timeout': raise TimeoutError('controlled worker deadline')
        if stop == 'cancel': raise asyncio.CancelledError
        raise CaptureAborted('input_changed')
    def forbidden(*args, **kwargs):
        calls.append('layout')
        raise AssertionError('layout after stopped preparation')
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', stopped)
    monkeypatch.setattr(server, 'packed_field_selections', forbidden)
    if stop == 'cancel':
        with pytest.raises(asyncio.CancelledError): await packed(tmp_path, handle, wave)
    else:
        result = await packed(tmp_path, handle, wave)
        assert result['error_code'] == ('design_context_timeout' if stop == 'timeout' else 'design_context_changed'), result
    assert calls == ['prepare']


# The injected Language SDK is a routing control, never real NPI evidence.
from tests.test_public_design_packed import controls
from tests.test_public_design_instance import prepare as native_prepare
_REAL_LAYOUT = server._resolve_source_packed_fields


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['unsupported_type', 'ordinary_failure'])
async def test_native_control_falls_back_to_real_source_layout(controls, monkeypatch, failure):
    monkeypatch.setattr(server, '_resolve_source_packed_fields', _REAL_LAYOUT)
    handle = await native_prepare(controls.log)
    if failure == 'unsupported_type':
        controls.sdk.nodes['@struct']['type'] = 'npiUnionTypespec'
    else:
        def failed(*args): raise RuntimeError('injected ordinary native failure')
        controls.before = failed
    result = await public(controls.source.parent, 'resolve_packed_fields', dict(design_handle=handle,
        wave_path=str(controls.wave), signal_path='top.packet[7:0]', source_signal='top.packet',
        fields=['tag', 'payload'], timeout_sec=60))
    assert result['status'] == 'resolved', result
    assert result['backend_status']['actual_backend'] == 'source_graph', result
    assert result['evidence']['layout_source'] == 'source_graph_packed_members', result
    assert {k:v['bits'] for k,v in result['fields'].items()} == {'tag':[7,6,5], 'payload':[4,3,2,1,0]}
    assert controls.requests


@pytest.mark.anyio
async def test_prepared_trace_union_is_bounded_and_preserves_all_targets(tmp_path):
    from src.prepared_source_connectivity import build_prepared_source_plan
    source, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    targets = ('top.active.small_bus.bus', 'top.active.large_bus.bus', 'top.other.bus')
    with server._design_context_store.pin(handle) as entry:
        plan = build_prepared_source_plan(context=entry.context, operation='driver', args={'signal_path':targets[0]},
            config=server.get_source_graph_execution_config(), signal_paths=targets,
            search=SemanticScopeSearch(purpose='trace'))
    assert plan.receipt.endpoint_count == 3 and plan.receipt.scope_kind == 'multi_endpoint_trace'
    assert set(plan.request.scope.semantic_search.assignment_candidates) == {
        'top.active.small_bus', 'top.active.large_bus', 'top.other'}
    assert type(plan.request).from_dict(plan.request.to_dict()) == plan.request
    with pytest.raises(ValueError):
        SemanticScopeSearch(purpose='trace', assignment_candidates=tuple(f'top.u{i}' for i in range(65)))


@pytest.mark.anyio
async def test_packed_cold_single_flight_and_cancelled_waiter(tmp_path, monkeypatch):
    import asyncio
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    entered, release = asyncio.Event(), asyncio.Event()
    original = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    calls = []
    async def held(self, *args, **kw):
        calls.append(1); entered.set()
        await release.wait()
        return await original(self, *args, **kw)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', held)
    one = asyncio.create_task(packed(tmp_path, handle, wave))
    await asyncio.wait_for(entered.wait(), 10)
    two = asyncio.create_task(packed(tmp_path, handle, wave))
    async def joined():
        from src import source_graph_production
        while not any(f.waiter_count >= 2 for f in source_graph_production._PROCESS_SESSION._runtime._inflight.values()):
            await asyncio.sleep(.005)
    await asyncio.wait_for(joined(), 10)
    one.cancel()
    with pytest.raises(asyncio.CancelledError): await one
    release.set()
    result = await asyncio.wait_for(two, 60)
    assert result['status'] == 'resolved' and len(calls) == 1, result
    assert result['backend_status']['source_graph']['flight_disposition'] == 'coalesced'


@pytest.mark.anyio
async def test_packed_last_waiter_cleans_real_worker(tmp_path, monkeypatch):
    import asyncio
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    entered = asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []
    async def observed(*a, **kw):
        proc = await original(*a, **kw)
        if any('source_graph_worker' in str(x) for x in a):
            processes.append(proc); entered.set()
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', observed)
    task = asyncio.create_task(packed(tmp_path, handle, wave))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    for proc in processes:
        await asyncio.wait_for(proc.wait(), 5)
        assert not Path(f'/proc/{proc.pid}').exists()
    assert processes


@pytest.mark.anyio
async def test_cached_connection_and_hierarchy_cannot_supply_layout(tmp_path, monkeypatch):
    from src.packed_layout import packed_field_selections
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    builds, entries = [], []
    original_run = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    original_prepare = source_graph_runtime.SourceGraphRuntime.prepare
    async def run(self, *a, **kw):
        builds.append(1); return await original_run(self, *a, **kw)
    async def observe(self, *a, **kw):
        outcome = await original_prepare(self, *a, **kw)
        if outcome.entry: entries.append(outcome.entry)
        return outcome
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', run)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', observe)
    await public(tmp_path, 'find_tb_instance', dict(design_handle=handle, path='top.active.small_bus'))
    await public(tmp_path, 'explain_signal_driver', dict(design_handle=handle,
        signal_path='top.active.small_bus.bus', wave_path=str(wave), timeout_sec=60))
    before = len(builds)
    result = await packed(tmp_path, handle, wave)
    assert result['status'] == 'resolved' and len(builds) == before + 1, result
    entry = entries[-1]
    # Preserve the real producer before testing the earliest rejecting boundary.
    (tmp_path / 'layout-producer.raw.json').write_bytes(entry.ir_json_bytes)
    def observed_rejection(label, scope, action):
        (tmp_path / f'{label}.scope.json').write_text(json.dumps(scope.to_dict()))
        try:
            return action()
        except BaseException as exc:
            (tmp_path / f'{label}.exception.json').write_text(json.dumps({
                'type': type(exc).__name__, 'message': str(exc)}))
            (tmp_path / f'{label}.exception.txt').write_text(traceback.format_exc())
            raise

    for purpose in ('hierarchy', 'connectivity'):
        scope = replace(entry.artifact_identity.scope, semantic_search=replace(
            entry.artifact_identity.scope.semantic_search, purpose=purpose,
            **({'roots': (entry.artifact_identity.scope.top,), 'depth': 0}
               if purpose == 'hierarchy' else {})))
        def attempt():
            poisoned = replace(entry, artifact_identity=replace(entry.artifact_identity, scope=scope),
                artifact_scope_receipt=replace(entry.artifact_scope_receipt, scope=scope))
            return packed_field_selections(poisoned, 'top.active.small_bus.bus', 'dump', ['valid'])
        expected = ('hierarchy artifact contains missing or mixed facts' if purpose == 'hierarchy'
                    else 'artifact_type_layout_capability_missing')
        with pytest.raises(ValueError, match=expected):
            observed_rejection('poison-' + purpose, scope, attempt)
    metadata = dict(entry.ir.metadata); metadata['semantic_assignment_paths'] = '[]'
    def missing_layout():
        changed_ir = replace(entry.ir, metadata=tuple(metadata.items()))
        (tmp_path / 'poison-layout.ir.raw.json').write_bytes(changed_ir.to_json_bytes())
        poisoned = replace(entry, ir=changed_ir)
        return packed_field_selections(poisoned, 'top.active.small_bus.bus', 'dump', ['valid'])
    with pytest.raises(ValueError, match='port_binding_only_scope_has_no_complete_type_layout'):
        observed_rejection('poison-layout', entry.artifact_identity.scope, missing_layout)



@pytest.mark.anyio
@pytest.mark.parametrize('change', ['same_size_mtime', 'policy'])
async def test_packed_real_prepare_identity_change_discards_fields(tmp_path, monkeypatch, change):
    import os
    source, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    original = source_graph_runtime.SourceGraphRuntime.prepare
    async def changed(self, *a, **kw):
        outcome = await original(self, *a, **kw)
        if change == 'same_size_mtime':
            stat = source.stat()
            source.write_text(source.read_text().replace('W=3', 'W=4'))
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        else:
            monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT', '91')
        return outcome
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', changed)
    result = await packed(tmp_path, handle, wave)
    assert result['error_code'] == ('design_context_changed' if change == 'same_size_mtime' else 'design_context_execution_changed'), result
    assert not result.get('fields')


@pytest.mark.anyio
async def test_native_failure_policy_change_stops_before_source_plan(controls, monkeypatch):
    from src import prepared_source_connectivity
    monkeypatch.setattr(server, '_resolve_source_packed_fields', _REAL_LAYOUT)
    handle = await native_prepare(controls.log)
    calls = []
    def changed(*a):
        monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT', '91')
        raise RuntimeError('injected native failure')
    def forbidden(*a, **kw):
        calls.append(1); raise AssertionError('new source work after policy change')
    controls.before = changed
    monkeypatch.setattr(prepared_source_connectivity, 'build_prepared_source_plan', forbidden)
    result = await public(controls.source.parent, 'resolve_packed_fields', dict(design_handle=handle,
        wave_path=str(controls.wave), signal_path='top.packet[7:0]', source_signal='top.packet', fields=['tag']))
    assert result['error_code'] == 'design_context_execution_changed', result
    assert calls == []


@pytest.mark.anyio
async def test_policy_changed_during_planner_stops_runtime(tmp_path, monkeypatch):
    from src import prepared_source_connectivity
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    original = prepared_source_connectivity.build_prepared_source_plan
    calls = []
    def changed(**kw):
        plan = original(**kw)
        monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT', '91')
        return plan
    async def forbidden(*a, **kw):
        calls.append(1); raise AssertionError('worker after changed planner policy')
    monkeypatch.setattr(prepared_source_connectivity, 'build_prepared_source_plan', changed)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', forbidden)
    result = await packed(tmp_path, handle, wave)
    assert result['error_code'] == 'design_context_execution_changed', result
    assert calls == []
