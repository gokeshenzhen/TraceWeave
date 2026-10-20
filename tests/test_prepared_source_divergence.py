"""Independent source designs, explicitly authored dual VCDs and fixed oracles."""
import asyncio
from dataclasses import asdict
from pathlib import Path

import pytest
import server
from src import source_graph_runtime
from tests.test_prepared_source_connectivity import anyio_backend, isolation, public, prepare
from tests.test_prepared_source_trace import project


async def case(tmp, *, native_a=False, policy='source'):
    sa, la, wa = project(tmp/'a', value='0001')
    sb, lb, wb = project(tmp/'b', value='0010')
    sb.write_text(sb.read_text().replace('assign y = a;', 'assign y = ~a;'))
    text = wb.read_text()
    for symbol in '#&(@+': text = text.replace(f'b0010 {symbol}\n', f'b1101 {symbol}\n')
    wb.write_text(text)
    if native_a:
        maps = tmp/'a/simv.daidir/kdb.elab++'; maps.mkdir(parents=True)
        (maps/'_libFileMap').write_bytes(b'map_A'); (maps/'_stnElabDB').write_bytes(b'elab_A')
    ha = await prepare(tmp, la, 'auto' if native_a else policy)
    hb = await prepare(tmp, lb, policy)
    args = dict(side_a=dict(design_handle=ha,wave_path=str(wa),signal_path='top.y'),
                side_b=dict(design_handle=hb,wave_path=str(wb),signal_path='top.y'),
                scope_pairs=[{'a':'top','b':'top'}], start_time_ps=0,end_time_ps=20,
                limits={'timeout_sec':60,'max_depth':10})
    return args, (sa,sb), (ha,hb)


def assert_sources(result, sources, handles, *, a_backend='source_graph'):
    assert result['comparison']['first_divergence_time_ps'] == 8, result
    assert len(result['nodes']) >= 4 and result['edges'], result
    assert result.get('cursor') is not None, result
    assert result['operation_metrics']['restart_count'] >= 1, result
    assert result['contexts']['a']['design_context']['context_identity_sha256'] != result['contexts']['b']['design_context']['context_identity_sha256']
    for label, source, handle, expected in zip(('a','b'), sources, handles, (a_backend,'source_graph')):
        context = result['contexts'][label]
        assert context['design_context']['design_handle'] == handle, context
        assert context['backend_status']['actual_backend'] == expected, context
        rows = [n[label] for n in result['nodes']]
        values = {'a':{'top.y':'0001','top.dst.a':'0001','top.g.mid.a':'0001','top.src.a':'0001','top.d':'0001'},
                  'b':{'top.y':'1101','top.dst.a':'0010','top.g.mid.a':'1101','top.src.a':'0010','top.d':'0010'}}
        for row in rows:
            assert row['bits'] == [3,2,1,0] and row['time_ps'] == 8, row
            assert row['value'] == values[label][row['signal'].split('[')[0]], row
        assert any(r['signal'].split('[')[0] == 'top.d' for r in rows), rows
        assert {r['structure']['backend'] for r in rows} == {expected}, rows
        assert len({r['structure']['artifact_sha256'] for r in rows}) == 1, rows
        if expected == 'source_graph':
            assignments = [a for r in rows for a in r['structure']['assignments'] if a.get('source')]
            assert len(assignments) >= 3, rows
            assert all(Path(a['source']['file']).resolve() == source.resolve() and a['source']['line'] == 2 for a in assignments), assignments
            sg = context['backend_status']['source_graph']
            assert sg['scope_expansion_count'] >= 1 and sg['positive_query_count'] >= 3
            assert sg['complete_negative_query_count'] == 0
            assert sg['query_count'] == len(sg['query_statuses']) == len(sg['query_fingerprints_sha256'])
            assert sg['final_artifact_fingerprint_sha256'] == rows[0]['structure']['artifact_sha256']
    assert len(server._cursor_store) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source','auto'])
async def test_two_source_designs_reach_distinct_content_and_reuse_unchanged_side(tmp_path, monkeypatch, policy):
    args, sources, handles = await case(tmp_path, policy=policy)
    calls, queries = [], []
    run = source_graph_runtime.IsolatedSourceGraphProcessRunner.run
    from src.source_graph_backend import SourceGraphConnectivityBackend
    query = SourceGraphConnectivityBackend.get_dynamic_step
    async def observed(self, *a, **kw):
        assert not any(lock.locked() for lock in server._wave_locks_for([args['side_a']['wave_path'], args['side_b']['wave_path']]))
        calls.append(a[0].to_dict()); return await run(self, *a, **kw)
    def queried(self, signal_path, **kw):
        queries.append((self._entry.build_key.digest, signal_path))
        return query(self, signal_path, **kw)
    monkeypatch.setattr(source_graph_runtime.IsolatedSourceGraphProcessRunner, 'run', observed)
    monkeypatch.setattr(SourceGraphConnectivityBackend, 'get_dynamic_step', queried)
    result = await public(tmp_path, 'trace_divergence', args)
    assert_sources(result, sources, handles)
    for label in ('a','b'):
        receipt = result['contexts'][label]['backend_status']['source_graph']
        artifact = receipt['final_artifact_fingerprint_sha256']
        assert receipt['query_count'] == sum(a == artifact for a,_ in queries), (receipt, queries)
    assert len(calls) == 4, calls  # initial+expanded, independently per design
    fingerprints = {}
    for artifact, signal in queries: fingerprints.setdefault(artifact, signal)
    assert len(fingerprints) == 4 and set(fingerprints.values()) == {'top.y'}, queries
    assert result['operation_metrics']['restart_count'] == 2
    # The unchanged A artifact survives B expansion; each side prepares only twice.
    count = len(calls)
    server._cursor_store.clear()
    repeated = await public(tmp_path, 'trace_divergence', args)
    assert_sources(repeated, sources, handles)
    assert len(calls) == count


class NativeControl:
    name, uses_external_worker, execution_mode = 'verdi_npi', False, 'local'
    kdb_status = {'load_quality':'clean'}
    def __init__(self, calls, fail): self.calls,self.fail=calls,fail
    def bind_compile_context(self, value): self.compile_result=value
    def bind_kdb_anchor(self, value): self.anchor=value
    def get_dynamic_step(self, signal_path, **kw):
        from src.dynamic_evidence import Expr, unsupported_step
        signal = signal_path.split('[')[0]; self.calls.append(signal)
        if self.fail and signal == 'top.dst.a':
            return unsupported_step(signal_path, 'verdi_npi', 'dynamic_evidence_unavailable')
        target = {'top.y':'top.dst.a','top.dst.a':'top.g.mid.a','top.g.mid.a':'top.src.a','top.src.a':'top.d'}.get(signal)
        state = Expr('signal',4,signal=signal,bits=(3,2,1,0),declared_bits=(3,2,1,0))
        result = dict(version='2.0',backend='verdi_npi',signal=signal,width=4,bits=[3,2,1,0],state=asdict(state),
                      complete=True,boundary='combinational' if target else 'input',clock=None,gaps=[],branches=[])
        if target:
            result['branches']=[dict(id='controlled-native',order=0,guard=asdict(Expr('const',1,value='1')),
                value=asdict(Expr('signal',4,signal=target,bits=(3,2,1,0),declared_bits=(3,2,1,0))),
                source={'file':'controlled-native.sv','line':999})]
        return result


@pytest.mark.anyio
@pytest.mark.parametrize('fallback', [False, True])
async def test_mixed_route_control_and_single_side_backend_restart(tmp_path, monkeypatch, fallback):
    from src import connectivity_backend
    args, sources, handles = await case(tmp_path, native_a=True)
    calls=[]
    monkeypatch.setattr(connectivity_backend, 'select_backend', lambda *a, **kw: NativeControl(calls, fallback))
    result = await public(tmp_path, 'trace_divergence', args)
    assert_sources(result, sources, handles, a_backend='source_graph' if fallback else 'verdi_npi')
    assert calls[0] == 'top.y' and 'top.dst.a' in calls
    if fallback:
        assert 'controlled-native.sv' not in str(result)
        assert 'npi_dynamic_fallback' in result['coverage']['restart_reasons']
    else:
        assert result['contexts']['a']['backend_status'].get('source_graph') is None


@pytest.mark.anyio
@pytest.mark.parametrize('stop', ['identity', 'policy', 'cancel', 'worker_timeout'])
@pytest.mark.parametrize('prepare_index', [1,3])
async def test_divergence_typed_stop_discards_comparison_graph_and_cursor(tmp_path, monkeypatch, stop, prepare_index):
    from types import SimpleNamespace
    from src.source_graph_runtime import PrepareStatus
    args, sources, handles = await case(tmp_path)
    original = source_graph_runtime.SourceGraphRuntime.prepare
    calls=[]
    preserved = server._cursor_store.set('keep', 3, note='preexisting')
    async def changed(self, *a, **kw):
        calls.append(1)
        if len(calls) != prepare_index: return await original(self, *a, **kw)
        if stop == 'worker_timeout': return SimpleNamespace(status=PrepareStatus.TIMED_OUT)
        outcome = await original(self, *a, **kw)
        if stop == 'identity': sources[0].write_text(sources[0].read_text()+'// changed\n')
        elif stop == 'policy': monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_TIMEOUT','91')
        else: raise asyncio.CancelledError
        return outcome
    monkeypatch.setattr(source_graph_runtime.SourceGraphRuntime, 'prepare', changed)
    if stop == 'cancel':
        with pytest.raises(asyncio.CancelledError): await public(tmp_path,'trace_divergence',args)
    else:
        result = await public(tmp_path,'trace_divergence',args)
        assert result['status'] == 'inconclusive' and not result['nodes'] and not result.get('comparison'),result
        assert not result.get('cursor'),result
        expected = {'identity':'design_context_changed','policy':'design_context_execution_changed','worker_timeout':'design_context_timeout'}[stop]
        assert expected in result['coverage']['gaps'],result
    assert len(calls) == prepare_index
    assert len(server._cursor_store) == 1 and server._cursor_store.get('keep') == preserved


@pytest.mark.anyio
async def test_source_to_static_receipt_marks_discarded_source_attempt(tmp_path, monkeypatch):
    from src.source_graph_backend import SourceGraphConnectivityBackend
    from src.dynamic_evidence import unsupported_step
    from tests.test_prepared_source_trace import trace
    _, log, wave = project(tmp_path)
    handle = await prepare(tmp_path, log, 'source')
    original = SourceGraphConnectivityBackend.get_dynamic_step
    def unavailable(self, signal_path, **kw):
        real = original(self, signal_path, **kw)
        result = unsupported_step(signal_path, 'source_graph', 'dynamic_evidence_unavailable')
        result['_source_graph_query_receipt'] = real['_source_graph_query_receipt']
        return result
    monkeypatch.setattr(SourceGraphConnectivityBackend, 'get_dynamic_step', unavailable)
    result = await trace(tmp_path, handle, wave, 'history')
    assert result['backend_status']['actual_backend'] == 'static', result
    assert result['backend_status']['source_graph']['fallback_used'] is True, result
    assert all(n['structure']['backend'] == 'static' for n in result['history']['nodes'] if n.get('structure')), result
