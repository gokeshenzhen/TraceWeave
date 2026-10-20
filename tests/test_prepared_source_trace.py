"""Real prepared X traces over authored HDL and explicitly constructed VCD."""
import asyncio
from pathlib import Path

import pytest
import server
from src import source_graph_runtime
from tests.test_prepared_source_connectivity import anyio_backend, isolation, public, prepare

FIXTURE = Path(__file__).parent / 'fixtures/prepared_trace/chain.sv'


def project(tmp, *, value='xxxx'):
    tmp.mkdir(exist_ok=True)
    source = tmp / 'chain.sv'; source.write_bytes(FIXTURE.read_bytes())
    log = tmp / 'compile.log'
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n       top\n")
    wave = tmp / 'chain.vcd'
    wave.write_text('$timescale 1ps $end\n$scope module top $end\n'
        '$var wire 4 ! d [3:0] $end\n$var wire 4 @ y [3:0] $end\n'
        '$var wire 4 # left [3:0] $end\n$var wire 4 $ right [3:0] $end\n'
        '$scope module src $end\n$var wire 4 % a [3:0] $end\n$var wire 4 & y [3:0] $end\n$upscope $end\n'
        '$scope module g $end\n$scope module mid $end\n$var wire 4 ( a [3:0] $end\n$var wire 4 ) y [3:0] $end\n$upscope $end\n$upscope $end\n'
        '$scope module dst $end\n$var wire 4 * a [3:0] $end\n$var wire 4 + y [3:0] $end\n$upscope $end\n'
        '$upscope $end\n$enddefinitions $end\n#0\n'+''.join(f'b0000 {s}\n' for s in '!@#$%&()*+')+
        '#5\n'+''.join(f'b{value} {s}\n' for s in '!%')+
        '#6\n'+''.join(f'b{value} {s}\n' for s in '#&(')+
        '#7\n'+''.join(f'b{value} {s}\n' for s in '$)*')+
        '#8\n'+''.join(f'b{value} {s}\n' for s in '@+')+'#30\n')
    return source, log, wave


async def trace(tmp, handle, wave, mode, **extra):
    args = dict(design_handle=handle, wave_path=str(wave), signal_path='top.y', time_ps=20,
                mode=mode, max_depth=10)
    if mode == 'history': args.update(history_start_ps=0, timeout_sec=60)
    args.update(extra)
    return await public(tmp, 'trace_x_source', args)


def assert_source(result, mode):
    assert result['backend_status']['actual_backend'] == 'source_graph', result
    sg = result['backend_status']['source_graph']
    assert sg['single_artifact_provenance'] and sg['scope_expansion_count'] >= 1, result
    assert len(set(sg['attempted_artifact_fingerprints_sha256'])) >= 2, result
    assert result['trace_restarted'], result
    if mode == 'snapshot':
        nodes = result['propagation_chain']
        assert len(nodes) >= 3, result
        assert any(n['signal_path'].split('[')[0] == 'top.src.a' for n in nodes), result
        assert result.get('root_cause') is None, result
        assert sg['complete_negative_query_count'] == 0, result
        assert sum(n.get('source_line') == 2 for n in nodes) >= 3, result
    else:
        nodes = result['history']['nodes']
        assert len(nodes) >= 3 and result['history']['edges'], result
        assert any(n['signal'].split('[')[0] == 'top.d' for n in nodes), result
        assert sg['query_count'] == len(sg['query_statuses']) == len(sg['query_fingerprints_sha256'])
        assert sg['positive_query_count'] >= 3 and sg['complete_negative_query_count'] == 0, sg
        structures = [n['structure'] for n in nodes if n.get('structure')]
        assert {s['backend'] for s in structures} == {'source_graph'}, result
        assert len({s['artifact_sha256'] for s in structures}) == 1, result
        onset = {'top.y':8000, 'top.dst.a':7000, 'top.g.mid.a':6000, 'top.src.a':5000, 'top.d':5000}
        for node in nodes:
            base = node['signal'].split('[')[0]
            assert node['bits'] == [3,2,1,0], node
            assert node['interval']['active_interval_start_time_fs'] == onset[base], node
        assignments = [a for n in nodes for a in n.get('structure', {}).get('assignments', [])]
        assert sum(a.get('source', {}).get('line') == 2 for a in assignments) >= 3, assignments
        assert sum(bool(a.get('port_hops')) for a in assignments) >= 3, assignments


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source', 'auto'])
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
async def test_prepared_x_reaches_input_and_restarts_expanded_artifact(tmp_path, monkeypatch, policy, mode):
    _, log, wave = project(tmp_path)
    builds = []
    original = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    async def observed(self, *a, **kw):
        assert not any(lock.locked() for lock in server._wave_locks_for([str(wave)]))
        builds.append(a[0].to_dict())
        return await original(self, *a, **kw)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', observed)
    handle = await prepare(tmp_path, log, policy)
    assert builds == []
    result = await trace(tmp_path, handle, wave, mode)
    assert_source(result, mode)
    assert len(builds) >= 2
    count = len(builds)
    again = await trace(tmp_path, handle, wave, mode)
    assert_source(again, mode)
    assert len(builds) == count


@pytest.mark.anyio
async def test_history_prepare_timeout_keeps_partial_frontier(tmp_path, monkeypatch):
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    async def stalled(*a, **kw): await asyncio.sleep(60)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', stalled)
    result = await trace(tmp_path, handle, wave, 'history', timeout_sec=.1)
    assert result['trace_status'] == 'partial' and result['history']['nodes'], result
    assert any(x['reason'] == 'budget_exhausted' for x in result['history']['frontier']), result
    assert 'error_code' not in result


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
@pytest.mark.parametrize('change', ['identity', 'policy', 'cancel'])
async def test_x_preparation_typed_stop_discards_graph(tmp_path, monkeypatch, mode, change):
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    original = source_graph_runtime.SourceGraphRuntime.prepare
    async def changed(self, *a, **kw):
        outcome = await original(self, *a, **kw)
        if change == 'identity': log.write_text(log.read_text() + '\n// changed\n')
        elif change == 'policy': monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT', '91')
        else: raise asyncio.CancelledError
        return outcome
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', changed)
    if change == 'cancel':
        with pytest.raises(asyncio.CancelledError): await trace(tmp_path, handle, wave, mode)
    else:
        result = await trace(tmp_path, handle, wave, mode)
        assert result['error_code'] == ('design_context_changed' if change == 'identity' else 'design_context_execution_changed'), result
        assert not result.get('propagation_chain') and not result.get('history')


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
async def test_runtime_worker_timeout_is_typed_and_discards_graph(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    from src.source_graph_runtime import PrepareStatus
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    async def timed_out(*a, **kw): return SimpleNamespace(status=PrepareStatus.TIMED_OUT)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', timed_out)
    result = await trace(tmp_path, handle, wave, mode)
    assert result['error_code'] == 'design_context_timeout', result
    assert not result.get('history') and not result.get('propagation_chain')


@pytest.mark.anyio
async def test_history_own_deadline_after_real_positive_retains_prefix(tmp_path, monkeypatch):
    import time
    from src.source_graph_backend import SourceGraphConnectivityBackend
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    assert_source(await trace(tmp_path, handle, wave, 'history'), 'history')
    original = SourceGraphConnectivityBackend.get_dynamic_step
    calls = []
    def delayed(self, signal_path, **kw):
        result = original(self, signal_path, **kw)
        calls.append(signal_path)
        if signal_path.startswith('top.dst.a'):
            time.sleep(.3)
        return result
    monkeypatch.setattr(SourceGraphConnectivityBackend, 'get_dynamic_step', delayed)
    result = await trace(tmp_path, handle, wave, 'history', timeout_sec=.2)
    nodes = result['history']['nodes']
    assert result['trace_status'] == 'partial' and any(n.get('structure', {}).get('backend') == 'source_graph' for n in nodes), result
    assert any(f['reason'] == 'budget_exhausted' for f in result['history']['frontier']), result
    await asyncio.sleep(.35)  # Join the cancelled query thread's bounded delay.


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
async def test_native_control_partial_chain_restarts_into_real_source(tmp_path, monkeypatch, mode):
    from dataclasses import asdict
    from src import connectivity_backend
    from src.dynamic_evidence import Expr, unsupported_step
    _, log, wave = project(tmp_path)
    maps = tmp_path / 'simv.daidir/kdb.elab++'; maps.mkdir(parents=True)
    (maps / '_libFileMap').write_bytes(b'map_A')
    (maps / '_stnElabDB').write_bytes(b'elab_A')
    calls = []
    class NativeControl:
        name, uses_external_worker, execution_mode = 'verdi_npi', False, 'local'
        kdb_status = {'load_quality':'clean'}
        def bind_compile_context(self, value): self.compile_result = value
        def bind_kdb_anchor(self, value): self.anchor = value
        def find_driver(self, **kw):
            signal = kw['signal_path']; calls.append(signal)
            if signal != 'top.y': raise RuntimeError('ordinary injected NPI failure')
            return dict(backend='verdi_npi', driver_status='resolved', driver_kind='assign',
                        source_file='control-only.sv', source_line=999, upstream_signals=['top.dst.a'])
        def get_dynamic_step(self, signal_path, **kw):
            calls.append(signal_path)
            if signal_path != 'top.y': return unsupported_step(signal_path, 'verdi_npi', 'dynamic_evidence_unavailable')
            expr = Expr('signal', 4, signal='top.dst.a', bits=(3,2,1,0), declared_bits=(3,2,1,0))
            return dict(version='2.0', backend='verdi_npi', signal=signal_path, width=4,
                bits=[3,2,1,0], complete=True, boundary='combinational', clock=None, gaps=[],
                state=asdict(Expr('signal',4,signal='top.y',bits=(3,2,1,0),declared_bits=(3,2,1,0))),
                branches=[dict(id='control-only', order=0, guard=asdict(Expr('const',1,value='1')),
                    value=asdict(expr), source={'file':'control-only.sv','line':999})])
    monkeypatch.setattr(connectivity_backend, 'select_backend', lambda *a, **kw: NativeControl())
    handle = await prepare(tmp_path, log, 'auto')
    result = await trace(tmp_path, handle, wave, mode)
    assert_source(result, mode)
    assert calls[:2] == ['top.y', 'top.dst.a'], calls
    assert 'control-only.sv' not in str(result) and 'control-only' not in str(result)
    assert result['backend_status']['whole_trace_restart_count'] >= 2


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
async def test_trace_cold_single_flight_cancel_preserves_other_request(tmp_path, monkeypatch, mode):
    from src import source_graph_production
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    entered, release = asyncio.Event(), asyncio.Event()
    original = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    builds = []
    async def held(self, *a, **kw):
        builds.append(1); entered.set(); await release.wait()
        return await original(self, *a, **kw)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', held)
    one = asyncio.create_task(trace(tmp_path, handle, wave, mode))
    await asyncio.wait_for(entered.wait(), 10)
    two = asyncio.create_task(trace(tmp_path, handle, wave, mode))
    async def joined():
        while not any(f.waiter_count >= 2 for f in source_graph_production._PROCESS_SESSION._runtime._inflight.values()):
            await asyncio.sleep(.005)
    await asyncio.wait_for(joined(), 10)
    one.cancel()
    with pytest.raises(asyncio.CancelledError): await one
    release.set()
    result = await asyncio.wait_for(two, 60)
    assert_source(result, mode)
    assert len(builds) == 2, builds  # one initial scope, one expanded scope


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
async def test_trace_last_waiter_cleans_real_worker(tmp_path, monkeypatch, mode):
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    entered = asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []
    async def observed(*a, **kw):
        process = await original(*a, **kw)
        if any('source_graph_worker' in str(x) for x in a):
            processes.append(process); entered.set()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', observed)
    task = asyncio.create_task(trace(tmp_path, handle, wave, mode))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    for process in processes:
        await asyncio.wait_for(process.wait(), 5)
        assert not Path(f'/proc/{process.pid}').exists()
    assert processes


@pytest.mark.anyio
async def test_snapshot_real_bounded_positive_remains_source_partial(tmp_path, monkeypatch):
    source, log, wave = project(tmp_path)
    source.write_text('module top(input logic [3:0] d, output wire [3:0] y);\n'
                      'assign y[1:0] = d[1:0];\nassign y[3:2] = d[3:2];\nendmodule\n')
    handle = await prepare(tmp_path, log, 'source')
    original = server._source_graph_backend_for_plan
    def bounded(entry, plan):
        backend = original(entry, plan)
        real_find = backend.find_driver
        def find(*a, **kw):
            backend.query_budget.evidence = 1
            return real_find(*a, **kw)
        backend.find_driver = find
        return backend
    monkeypatch.setattr(server, '_source_graph_backend_for_plan', bounded)
    result = await trace(tmp_path, handle, wave, 'snapshot')
    assert result['backend_status']['actual_backend'] == 'source_graph', result
    assert result['trace_status'] == 'driver_traversal_incomplete', result
    node = result['propagation_chain'][0]
    assert node['driver_status'] == 'partial' and node['source_line'] in (2,3), result
    assert node['traversal']['returned_fact_count'] == 1, result
    assert result.get('root_cause') is None


@pytest.mark.anyio
async def test_snapshot_exhausted_prefix_never_starts_static(tmp_path, monkeypatch):
    from src.connectivity_backend import StaticConnectivityBackend
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    original = server._source_graph_backend_for_plan
    calls = []
    def bounded(entry, plan):
        backend = original(entry, plan)
        real_find = backend.find_driver
        def find(*a, **kw):
            result = real_find(*a, **kw)
            if result['_source_graph_query_receipt']['status'] == 'found':
                backend.query_budget.states = 0
            return result
        backend.find_driver = find
        return backend
    def static(*a, **kw):
        calls.append(1); raise AssertionError('Static after source budget stop')
    monkeypatch.setattr(server, '_source_graph_backend_for_plan', bounded)
    monkeypatch.setattr(StaticConnectivityBackend, 'find_driver', static)
    result = await trace(tmp_path, handle, wave, 'snapshot')
    assert result['error_code'] == 'design_context_source_scope_limit', result
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['snapshot', 'history'])
@pytest.mark.parametrize('stage', ['planner', 'expansion'])
@pytest.mark.parametrize('change', ['policy', 'identity', 'cancel'])
async def test_trace_plan_barrier_never_starts_next_worker(tmp_path, monkeypatch, mode, stage, change):
    from src.prepared_source_trace import PreparedTracePlan
    from src.cancellation import OperationCancelled
    source, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    build = PreparedTracePlan.build
    run = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    calls, workers = [], []
    async def observed(self, *a, **kw):
        workers.append(1); return await run(self, *a, **kw)
    def changed(self, targets):
        plan = build(self, targets)
        calls.append(1)
        if len(calls) == (1 if stage == 'planner' else 2):
            if change == 'policy': monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT', '91')
            elif change == 'identity': source.write_text(source.read_text() + '// changed\n')
            else: raise OperationCancelled()
        return plan
    monkeypatch.setattr(PreparedTracePlan, 'build', changed)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', observed)
    if change == 'cancel':
        with pytest.raises(asyncio.CancelledError): await trace(tmp_path, handle, wave, mode)
    else:
        result = await trace(tmp_path, handle, wave, mode)
        assert result['error_code'] == ('design_context_changed' if change == 'identity' else 'design_context_execution_changed'), result
    assert len(workers) == (0 if stage == 'planner' else 1), workers


@pytest.mark.anyio
async def test_snapshot_planner_uses_original_deadline(tmp_path, monkeypatch):
    import time
    from src.prepared_source_trace import PreparedTracePlan
    _, log, wave = project(tmp_path)
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT', '.05')
    handle = await prepare(tmp_path, log, 'source')
    original = PreparedTracePlan.build
    calls = []
    def delayed(self, targets):
        time.sleep(.1)
        return original(self, targets)
    async def forbidden(*a, **kw):
        calls.append(1); raise AssertionError('worker after snapshot deadline')
    monkeypatch.setattr(PreparedTracePlan, 'build', delayed)
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', forbidden)
    result = await trace(tmp_path, handle, wave, 'snapshot')
    assert result['error_code'] == 'design_context_timeout', result
    assert calls == []
    await asyncio.sleep(.15)


@pytest.mark.anyio
async def test_real_input_frontier_shortcut_exclusions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.prepared_source_trace import PreparedTraceArtifactGuard
    from src.source_graph_x_trace import SourceGraphTraceConnectivityBackend, SourceGraphTraceScopeExpansion
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    original = source_graph_runtime.SourceGraphRuntime.prepare
    entries = []
    async def observed(self, request, **kw):
        outcome = await original(self, request, **kw)
        if outcome.entry: entries.append((outcome.entry, request))
        return outcome
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', observed)
    result = await trace(tmp_path, handle, wave, 'snapshot')
    assert_source(result, 'snapshot')
    entry, request = entries[-1]
    guard = PreparedTraceArtifactGuard(entry, SimpleNamespace(request=request))
    assert guard.input_frontier(['top.d[3:0]'])
    for paths in ([], ['top.dst.a'], ['top.y'], ['top.missing'], ['top.d', 'top.y'], ['top.d', 'top.dst.a']):
        assert not guard.input_frontier(paths), paths
    backend = SourceGraphTraceConnectivityBackend(backend=server._source_graph_backend_for_plan(entry, SimpleNamespace(unprojected_instance_candidates=())),
        guard=guard, artifact_scope=entry.artifact_scope_receipt.scope, hierarchy_result=None,
        artifact_fingerprint_sha256=entry.build_key.digest)
    # An isolated input/alias query without prior positives cannot claim a trace.
    with pytest.raises(SourceGraphTraceScopeExpansion):
        backend.find_driver('top.src.a', str(wave), str(log))
    assert backend.ledger.positive_query_count == 0


@pytest.mark.anyio
async def test_alias_path_shares_remaining_query_budget_and_receipt(tmp_path, monkeypatch):
    import time
    from src.source_graph_backend import SourceGraphConnectivityBackend
    from src.source_graph_dynamic import query_step
    from src.prepared_source_connectivity import SourceQueryBudget
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    entries = []
    prepare_entry = source_graph_runtime.SourceGraphRuntime.prepare
    async def observed(self, request, **kw):
        outcome = await prepare_entry(self, request, **kw)
        if outcome.entry: entries.append(outcome.entry)
        return outcome
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', observed)
    assert_source(await trace(tmp_path, handle, wave, 'snapshot'), 'snapshot')
    entry = entries[-1]
    backend = SourceGraphConnectivityBackend(entry)
    engine = entry.query_engine
    driver = engine.query_driver('top.src.a', max_depth=16)
    original_path = engine.query_path
    paths = []
    def path(*a, **kw):
        result = original_path(*a, **kw)
        paths.append((kw, result))
        return result
    monkeypatch.setattr(engine, 'query_path', path)
    backend.query_budget = SourceQueryBudget(time.monotonic()+60, states=64, edges=64, evidence=64)
    result = query_step(backend, 'top.src.a')
    assert len(paths) == 1 and result['branches'], result
    kwargs, path_result = paths[0]
    assert kwargs['traversal_limit'] == min(64-driver.inspected_edge_count, 64-driver.visited_state_count-1)
    assert kwargs['output_limit'] == 64
    receipt = result['_source_graph_query_receipt']
    assert receipt['visited_state_count'] == driver.visited_state_count+path_result.visited_state_count
    assert receipt['inspected_edge_count'] == driver.inspected_edge_count+path_result.traversed_edge_count
    assert receipt['retained_evidence_count'] == len(driver.matches)+len(path_result.path)
    paths.clear()
    backend.query_budget = SourceQueryBudget(time.monotonic()+60, states=driver.visited_state_count)
    limited = query_step(backend, 'top.src.a')
    assert paths == [] and limited['_source_graph_query_receipt']['query_truncated'], limited
    assert limited['gaps'] == ['source_query_budget_exceeded'], limited
