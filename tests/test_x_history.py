"""Independent recorded time tables for history X tracing; no simulator oracle reuse."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sys
import threading

import pytest

import server
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.divergence_budget import BudgetExceeded, TraceRestart
from src.hierarchy_handles import HandleStore
from src.observation_session import ObservationSession
from src.vcd_parser import VCDParser
from src.x_history import trace_x_history
from src.x_history_observe import HistoryBudget, signal_expression, unknown_interval

RTL = '''module top(input logic clk, rst, en, d, output logic q, output wire y);
always_ff @(posedge clk) if (rst) q <= 1'b0; else if (en) q <= d;
assign y = q;
endmodule
'''


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    server.reset_session_state()
    monkeypatch.setattr(server, '_handle_store', HandleStore())
    monkeypatch.setenv('TRACEWEAVE_CONNECTIVITY_ROUTE', 'source_graph')
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_PYTHON', sys.executable)
    monkeypatch.setenv('TRACEWEAVE_AUTO_KDB', '0')


def write_wave(path, changes=None, *, scale='1ps', initial_q='0', extra='', end=40):
    # X is injected at 12, captured at 15, input recovers at 18. Enable is
    # disabled at 20: Q must retain X at 25 and 35 despite known present D.
    rows = {0: dict(clk='0', rst='0', en='1', d='0', q=initial_q, y=initial_q),
            5: dict(clk='1'), 10: dict(clk='0'), 12: dict(d='x'),
            15: dict(clk='1', q='x', y='x'), 18: dict(d='1'),
            20: dict(clk='0', en='0'), 25: dict(clk='1'),
            30: dict(clk='0'), 35: dict(clk='1'), 40: dict(clk='0')}
    for at, fields in (changes or {}).items():
        rows.setdefault(at, {}).update(fields)
    symbols = dict(clk='!', rst='@', en='#', d='$', q='%', y='&')
    text = f'$timescale {scale} $end\n$scope module top $end\n'
    text += ''.join(f'$var wire 1 {sym} {name} $end\n' for name, sym in symbols.items())
    text += extra + '$upscope $end\n$enddefinitions $end\n'
    for at, fields in sorted(rows.items()):
        if at <= end:
            text += f'#{at}\n' + ''.join(f'{value}{symbols[name]}\n' for name, value in fields.items() if value is not None)
    text += f'#{end}\n'
    path.write_text(text)


async def setup(tmp_path, source=RTL, **wave_options):
    source_path = tmp_path / 'design.sv'
    source_path.write_text(source)
    log = tmp_path / 'compile.log'
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source_path} -top top\nParsing design file '{source_path}'\nTop Level Modules:\n       top\n")
    ctx = dict(compile_log=str(log), simulator='vcs')
    await asyncio.gather(server._dispatch('build_tb_hierarchy', ctx), server._dispatch('scan_structural_risks', ctx))
    wave = tmp_path / 'wave.vcd'
    write_wave(wave, **wave_options)
    return dict(**ctx, wave_path=str(wave), signal_path='top.q', time_ps=36,
                mode='history', history_start_ps=0)


async def run(args):
    return (await server._dispatch('trace_x_source', args)).history


@pytest.mark.anyio
async def test_past_injection_survives_recovery_and_two_hold_cycles(tmp_path):
    args = await setup(tmp_path)
    result = await run(args)
    assert result.status == 'partial'
    nodes = result.nodes
    assert [(n['signal'], n['time_ps'], n['phase']) for n in nodes] == [
        ('top.q', 36, 'after'), ('top.q', 35, 'before'),
        ('top.q', 25, 'before'), ('top.d', 15, 'before')], result.model_dump()
    assert [n.get('relation') for n in nodes[:3]] == ['hold', 'hold', 'register_sample']
    assert [n['observation']['trigger_time_ps'] for n in nodes[:3]] == [35, 25, 15]
    assert nodes[0]['interval']['active_interval_start_time_fs'] == 15000
    assert nodes[-1]['interval']['active_interval_start_time_fs'] == 12000
    assert nodes[-1]['value'] == 'x'
    assert result.operation_metrics['observation_cache_hits'] > 0
    assert not any(n['interval']['true_origin_proven'] for n in nodes)
    # Opposite side: actual selected enable/reset controls are observed known;
    # current recovered D is not substituted for D before the capturing edge.
    assert any(d['signal'] == 'top.en' and d['value'] == '0' for d in nodes[0]['checked_dependencies'])
    assert any(d['signal'] == 'top.d' and d['value'] == 'x' for d in nodes[2]['checked_dependencies'])


@pytest.mark.anyio
async def test_combinational_propagation_precedes_register_history(tmp_path):
    args = await setup(tmp_path)
    r = await run({**args, 'signal_path': 'top.y'})
    assert r.nodes[0]['relation'] == 'combinational'
    assert r.nodes[1]['relation'] == 'hold'
    assert r.nodes[-1]['signal'] == 'top.d'


@pytest.mark.anyio
@pytest.mark.parametrize('changes,gap', [
    ({20: dict(en='x')}, 'guard_unresolved'),
    ({35: dict(en='1')}, 'sampling_order_unresolved'),
    ({20: dict(rst='x')}, 'guard_unresolved'),
    ({35: dict(rst='1')}, 'sampling_order_unresolved')])
async def test_unknown_or_same_edge_control_stops_at_candidate(tmp_path, changes, gap):
    args = await setup(tmp_path, changes=changes)
    r = await run(args)
    assert gap in r.coverage['gaps']
    assert len(r.nodes) == 1 and not r.edges


@pytest.mark.anyio
async def test_sync_reset_comes_from_guard_polarity_not_name(tmp_path):
    source = RTL.replace('if (rst)', 'if (!rst)')
    args = await setup(tmp_path, source, changes={0: dict(rst='1'), 22: dict(rst='0')})
    r = await run(args)
    assert r.nodes[0]['observation']['value'] == '0'
    assert 'observed_value_not_explained' in r.coverage['gaps']
    assert not r.edges  # A reset-to-zero cannot explain an observed X.
    assert r.candidates[0]['kind'] == 'local_behavior_candidate'


@pytest.mark.anyio
@pytest.mark.parametrize('source', [RTL.replace('@(posedge clk)', '@(posedge clk or negedge rst)'),
                                  RTL.replace('q <= d', 'q <= $urandom')])
async def test_unsupported_or_async_structure_does_not_infer_time(tmp_path, source):
    args = await setup(tmp_path, source)
    r = await run(args)
    assert len(r.nodes) == 1 and not r.edges
    assert any(g in r.coverage['gaps'] for g in ('temporal_context_unavailable', 'dynamic_evidence_unavailable'))


@pytest.mark.anyio
async def test_missing_clock_and_bounded_history(tmp_path):
    args = await setup(tmp_path)
    r = await run({**args, 'history_start_ps': 32})
    assert 'history_window_exhausted' in r.coverage['gaps']
    assert all(n['time_ps'] >= 32 for n in r.nodes)
    p = Path(args['wave_path'])
    p.write_text(p.read_text().replace('$var wire 1 ! clk $end\n', ''))
    r = await run(args)
    assert 'signal_not_dumped' in r.coverage['gaps']
    assert not r.edges


@pytest.mark.anyio
async def test_dump_start_unknown_is_not_true_origin(tmp_path):
    args = await setup(tmp_path, initial_q='x', changes={0: dict(en='0'), 15: dict(q=None, y=None)})
    r = await run(args)
    assert r.nodes[0]['interval']['start_boundary'] == 'dump_prefix_unknown'
    assert r.nodes[0]['interval']['first_observed_unknown_time_fs'] == 0
    assert not r.nodes[0]['interval']['predecessor_known']
    assert 'history_window_exhausted' in r.coverage['gaps']


@pytest.mark.anyio
async def test_native_precision_not_replaced_with_rounded_sample(tmp_path):
    args = await setup(tmp_path, scale='100fs')
    r = await run({**args, 'time_ps': 3})
    assert r.nodes[0]['interval']['active_interval_start_time_fs'] == 1500
    assert 'sampling_order_unresolved' in r.coverage['gaps']
    assert not r.edges


@pytest.mark.anyio
async def test_same_edge_recovery_keeps_only_a_labelled_predecessor_candidate(tmp_path, monkeypatch):
    from tests.test_evidence_output import compact_call
    from src.schemas import XHistoryEvidence
    args = await setup(tmp_path, changes={15: dict(d='1')})
    expanded, _ = await compact_call(monkeypatch, 'trace_x_source', args)
    r = XHistoryEvidence.model_validate(expanded['history'])
    assert r.status == 'partial' and not r.coverage['true_origin_proven']
    assert 'sampling_order_unresolved' in r.coverage['gaps']
    upstream = [n for n in r.nodes if n['signal'] == 'top.d']
    assert len(upstream) == 1 and upstream[0]['time_ps'] == 15 and upstream[0]['phase'] == 'before'
    assert upstream[0]['interval']['active_interval_start_time_fs'] == 12000
    assert any(e['relation'] == 'candidate_register_sample' for e in r.edges)
    assert any(c['kind'] == 'predecessor_sampling_candidate' for c in r.candidates)


@pytest.mark.anyio
async def test_ambiguous_clock_cannot_use_data_predecessor_candidate(tmp_path):
    args = await setup(tmp_path, changes={15: dict(d='1')})
    p = Path(args['wave_path'])
    p.write_text(p.read_text().replace('#15\n1!', '#15\n1!\n0!\n1!'))
    r = await run(args)
    assert 'sampling_order_unresolved' in r.coverage['gaps']
    assert not any(n['signal'] == 'top.d' for n in r.nodes)
    assert not any(e['relation'] == 'candidate_register_sample' for e in r.edges)


@pytest.mark.anyio
@pytest.mark.parametrize('limit,value', [('max_nodes', 1), ('max_depth', 0), ('max_events', 1), ('max_read_bytes', 1024)])
async def test_budget_exhaustion_is_partial_not_exclusion(tmp_path, limit, value):
    args = await setup(tmp_path)
    r = await run({**args, limit: value})
    assert r.status in {'partial', 'inconclusive'} and r.coverage['truncated']
    assert any(f.get('budget') == limit for f in r.frontier)


@pytest.mark.anyio
async def test_public_input_rejects_silent_mode_and_window_defaults(tmp_path):
    args = await setup(tmp_path)
    for bad in ({k: v for k, v in args.items() if k != 'history_start_ps'},
                {**args, 'history_start_ps': 40}, {**args, 'mode': 'snapshot'}):
        with pytest.raises(ValueError):
            await run(bad)
    tools = await server.list_tools()
    schema = next(t.inputSchema for t in tools if t.name == 'trace_x_source')
    assert schema['properties']['mode']['default'] == 'snapshot'


@pytest.mark.anyio
async def test_source_or_wave_change_discards_all_old_evidence(tmp_path, monkeypatch):
    from src.divergence_routing import DynamicRoute
    args = await setup(tmp_path)
    for target in (tmp_path / 'design.sv', Path(args['wave_path'])):
        original = DynamicRoute.query
        async def change(self, signal):
            result = await original(self, signal)
            target.write_text(target.read_text() + '\n')
            return result
        with monkeypatch.context() as m:
            m.setattr(DynamicRoute, 'query', change)
            r = await run(args)
        assert not r.nodes and not r.edges and not r.candidates
        assert r.status == 'inconclusive'
        assert any('changed' in f['reason'] for f in r.frontier)
        await server._dispatch('build_tb_hierarchy', {k: args[k] for k in ('compile_log', 'simulator')})


def test_required_native_history_budget_timeout_cancel_cleanup(monkeypatch, require_fsdb_runtime):
    from src.fsdb_parser import FSDBParser
    p = FSDBParser(str(Path(__file__).parent / 'fixtures/scale_100fs.fsdb'))
    signal = 'scale_100fs_tb.addr[31:0]'
    limits = dict(max_depth=20, max_nodes=128, max_events=65536, max_read_bytes=32*1024*1024, timeout_sec=30)
    try:
        b = HistoryBudget(limits)
        stream = b.read_stream(lambda _: p, p.file_path, signal, 0, 100102)
        assert stream.transitions[-1]['time_fs'] == 100100500
        assert b.native_pages > 0 and not p._transition_group_active
        original = p._lib.fsdb_event_page_v1
        for cause in ('budget', 'timeout', 'cancel'):
            b = HistoryBudget({**limits, 'max_events': 1} if cause == 'budget' else limits)
            event = threading.Event()
            token = push_cancel_event(event)
            def page(*args):
                rc = original(*args)
                if cause == 'timeout': b.deadline = 0
                if cause == 'cancel': event.set()
                return rc
            try:
                with monkeypatch.context() as m:
                    m.setattr(p._lib, 'fsdb_event_page_v1', page)
                    with pytest.raises(OperationCancelled if cause == 'cancel' else BudgetExceeded):
                        b.read_stream(lambda _: p, p.file_path, signal, 0, 100102)
                assert not p._transition_group_active
            finally:
                pop_cancel_event(token)
        assert p.get_value_at_time(signal, 100101)['value']['dec'] == 0xcccc0000
    finally:
        p.close()


@pytest.mark.anyio
async def test_ordered_bits_aliases_and_before_after_are_distinct(tmp_path):
    source = RTL.replace('en, d, output logic q, output wire y', 'en, input logic [3:0] d, output logic [3:0] q, output wire [3:0] y')
    args = await setup(tmp_path, source)
    p = Path(args['wave_path'])
    text = p.read_text()
    for sym, name in (('$','d'), ('%','q'), ('&','y')):
        text = text.replace(f'$var wire 1 {sym} {name} $end', f'$var wire 4 {sym} {name} [3:0] $end')
        for val, vec in (('0','0000'), ('1','1111'), ('x','10xz')):
            text = text.replace(f'\n{val}{sym}\n', f'\nb{vec} {sym}\n')
    p.write_text(text)
    r = await run({**args, 'signal_path': 'top.q[3:0]', 'signal_bits': [0, 2]})
    assert [(n['value'], n['bits']) for n in r.nodes] == [('z0', [0, 2])] * 4, r.model_dump()
    assert r.nodes[-1]['signal'] == 'top.d[3:0]'
    clean = await run({**args, 'signal_path': 'top.q[3:0]', 'signal_bits': [3, 2]})
    assert clean.status == 'signal_is_clean' and clean.operation_metrics['backend_query_count'] == 0
    before = await run({**args, 'time_ps': 15, 'phase': 'before'})
    after = await run({**args, 'time_ps': 15, 'phase': 'after'})
    assert before.status == 'signal_is_clean' and after.nodes[0]['value'] == '10xz'
    # A distinct declared alias retains its semantic path while sharing dump storage.
    p.write_text(text.replace('$var wire 4 & y [3:0] $end', '$var wire 4 % y [3:0] $end'))
    alias = await run({**args, 'signal_path':'top.y[3:0]', 'signal_bits':[0, 2]})
    assert alias.nodes[0]['signal'] == 'top.y[3:0]'
    assert alias.nodes[1]['signal'] == 'top.q[3:0]'


@pytest.mark.anyio
async def test_cdc_stops_before_inventing_launch_history(tmp_path):
    source = '''module top(input logic clk, rst, en, clk2, a, output logic d, q, output wire y);
always_ff @(posedge clk) if(en) q<=d;
always_ff @(posedge clk2) d<=a;
assign y=q;
endmodule'''
    args = await setup(tmp_path, source)
    p = Path(args['wave_path'])
    p.write_text(p.read_text().replace('$upscope $end', "$var wire 1 ' clk2 $end\n$var wire 1 ( a $end\n$upscope $end")
                 .replace('#0\n', "#0\n0'\nx(\n").replace('#12\n', "#12\n1'\n"))
    r = await run(args)
    assert r.nodes[-1]['signal'] == 'top.d'
    assert 'clock_domain_crossing_unverified' in r.coverage['gaps']
    assert 'observation' not in r.nodes[-1]


@pytest.mark.anyio
async def test_backend_and_artifact_restart_drop_graph_and_cache(tmp_path):
    from tests.test_dynamic_evidence import project
    backend = project(RTL)
    class Route:
        def __init__(self, s, label, side, bound, budget):
            self.stage, self.artifact, self.receipt = 'verdi_npi', 'old', {}
            self.calls = 0
            self.budget = budget
        async def query(self, signal):
            self.calls += 1
            self.budget.queries += 1
            if self.calls == 2:
                self.stage, self.artifact = 'source_graph', 'new'
                raise TraceRestart('x', 'npi_dynamic_fallback')
            step = backend.get_dynamic_step(signal)
            step.update(backend=self.stage, artifact_sha256=self.artifact)
            self.receipt = dict(actual_backend=self.stage)
            return step
        def current(self): return True
    args = await setup(tmp_path)
    r = (await trace_x_history(server, args, route_factory=Route)).history
    assert r.operation_metrics['restart_count'] == 1
    assert {n['structure']['artifact_sha256'] for n in r.nodes} == {'new'}
    assert len(r.nodes) == 4
    assert r.operation_metrics['backend_query_count'] == 6


@pytest.mark.anyio
async def test_exhausted_restart_does_not_report_old_backend_receipt(tmp_path):
    from src import divergence_budget
    class Route:
        def __init__(self, *a):
            self.stage, self.artifact, self.receipt = 'source_graph', 'new', {}
        async def query(self, signal):
            self.receipt = dict(actual_backend='verdi_npi', artifact_sha256='discarded')
            raise TraceRestart('x', 'source_graph_scope_expansion')
        def current(self): return True
    args = await setup(tmp_path)
    r = (await trace_x_history(server, args, route_factory=Route)).history
    assert not r.nodes and not r.edges
    assert r.operation_metrics['restart_count'] == divergence_budget.DIVERGENCE_MAX_RESTARTS
    receipt = r.context['backend_status']
    assert receipt['current_backend_stage'] == 'source_graph'
    assert receipt['last_attempt_status'] == 'incomplete'
    assert 'actual_backend' not in receipt and 'artifact_sha256' not in receipt


@pytest.mark.anyio
async def test_parser_generation_and_artifact_change_invalidate_evidence(tmp_path, monkeypatch):
    from src.divergence_routing import DynamicRoute
    args = await setup(tmp_path)
    original = DynamicRoute.query
    async def reopen(self, signal):
        step = await original(self, signal)
        server._get_parser(args['wave_path'])._scope_epoch = object()
        return step
    with monkeypatch.context() as m:
        m.setattr(DynamicRoute, 'query', reopen)
        r = await run(args)
    assert r.status == 'inconclusive' and not r.nodes
    assert 'parser_generation_changed' in r.coverage['gaps']
    with monkeypatch.context() as m:
        m.setattr(DynamicRoute, 'current', lambda self: False)
        r = await run(args)
    assert not r.nodes and r.coverage['gaps'] == ['compile_artifact_changed']


@pytest.mark.anyio
async def test_dynamic_queries_release_wave_lock_and_cancellation_never_falls_back(tmp_path, monkeypatch):
    from src.divergence_routing import DynamicRoute
    args = await setup(tmp_path)
    original = DynamicRoute.query
    calls = []
    async def query(self, signal):
        assert not server._wave_locks_for([args['wave_path']])[0].locked()
        calls.append(signal)
        if len(calls) == 2:
            raise OperationCancelled()
        return await original(self, signal)
    monkeypatch.setattr(DynamicRoute, 'query', query)
    with pytest.raises(asyncio.CancelledError):
        await run(args)
    assert len(calls) == 2


@pytest.mark.anyio
async def test_public_timeout_is_bounded_and_keeps_no_exclusion(tmp_path, monkeypatch):
    from src.divergence_routing import DynamicRoute
    args = await setup(tmp_path)
    async def slow(self, signal):
        await asyncio.sleep(10)
    monkeypatch.setattr(DynamicRoute, 'query', slow)
    r = await run({**args, 'timeout_sec': 0.05})
    assert any(f.get('budget') == 'timeout_sec' for f in r.frontier)
    assert r.operation_metrics['elapsed_ms'] < 2000
    assert not r.candidates


@pytest.mark.anyio
async def test_truncated_tail_and_missing_predecessor_are_not_proof(tmp_path, monkeypatch):
    from dataclasses import replace
    args = await setup(tmp_path, changes={0: dict(q=None, y=None)})
    r = await run(args)
    assert r.nodes[0]['interval']['start_boundary'] == 'dump_prefix_unknown'
    assert not r.nodes[0]['interval']['predecessor_known']
    parser = server._get_parser(args['wave_path'])
    original = parser._event_pages
    @contextmanager
    def truncated(*a, **kw):
        with original(*a, **kw) as reader:
            read = reader.read_page
            def page():
                result = read()
                return replace(result, truncated=True, complete=False, next_time=result.events[-1].time_fs)
            reader.read_page = page
            yield reader
    monkeypatch.setattr(parser, '_event_pages', truncated)
    r = await run(args)
    assert 'transition_data_truncated' in r.coverage['gaps']
    assert not r.nodes and r.status == 'inconclusive'


def test_legacy_fallback_and_subps_cache_slicing_preserve_raw_boundaries(tmp_path):
    path = tmp_path / 'wave.vcd'
    write_wave(path, scale='100fs')
    parser = VCDParser(str(path))
    b = HistoryBudget(dict(max_depth=20, max_nodes=128, max_events=65536, max_read_bytes=32*1024*1024, timeout_sec=30))
    session = ObservationSession(b, stream_reader=b.read_stream)
    session.stream(lambda _:parser, str(path), 'top.d', 0, 4)
    sliced = session.stream(lambda _:parser, str(path), 'top.d', 2, 3)
    assert not sliced.transitions and sliced.predecessor['time_fs'] == 1800
    parser._supports_event_pages = lambda: False
    stream = b.read_stream(lambda _:parser, str(path), 'top.d', 0, 4)
    assert stream.error == 'exact_history_time_unavailable'
    assert b.legacy_reads == 1


@pytest.mark.anyio
async def test_parameterized_generate_preserves_actual_state_path(tmp_path):
    from tests.test_dynamic_evidence import project
    # The semantic artifact owns specialization and the generate path. The
    # history walker does not flatten it into a guessed lexical instance.
    source = '''module lane #(parameter W=2)(input logic clk,en,input logic[W-1:0] d,output logic[W-1:0] q);
      always_ff @(posedge clk) if(en) q<=d;
    endmodule
    module top(input logic clk,en,input logic[3:0] d,output wire[3:0] q);
      for(genvar i=0;i<1;i++) begin:g lane #(.W(4)) u(clk,en,d,q); end
    endmodule'''
    backend = project(source)
    class PreparedRoute:
        def __init__(self, *a):
            self.stage, self.artifact, self.receipt = 'source_graph', 'prepared', {}
        async def query(self, signal):
            return {**backend.get_dynamic_step(signal), 'artifact_sha256': self.artifact}
        def current(self): return True
    args = await setup(tmp_path, source)
    path = Path(args['wave_path'])
    path.write_text('''$timescale 1ps $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 # en $end
$var wire 4 $ d [3:0] $end
$scope module g[0] $end
$scope module u $end
$var wire 1 ! clk $end
$var wire 1 # en $end
$var wire 4 $ d [3:0] $end
$var wire 4 % q [3:0] $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
1#
b0000 $
b0000 %
#12
b10xz $
#15
1!
b10xz %
#18
b1111 $
#20
0!
0#
#25
1!
#30
0!
#35
1!
#40
0!
''')
    r = (await trace_x_history(server, {**args, 'signal_path':'top.g[0].u.q[3:0]', 'signal_bits':[1,0]}, route_factory=PreparedRoute)).history
    assert [n['signal'] for n in r.nodes[:3]] == ['top.g[0].u.q[3:0]'] * 3, r.model_dump()
    assert [n['value'] for n in r.nodes[:3]] == ['xz'] * 3
    assert [n['observation']['trigger_time_ps'] for n in r.nodes[:3]] == [35,25,15]
    assert 'dynamic_bit_mapping_unavailable' not in r.coverage['gaps']


@pytest.mark.anyio
async def test_incomplete_driver_positive_and_testbench_crosscheck_survive(tmp_path):
    from tests.test_dynamic_evidence import project
    backend = project(RTL)
    class PartialRoute:
        def __init__(self, *a):
            self.stage, self.artifact, self.receipt = 'source_graph', 'partial', {}
        async def query(self, signal):
            step = backend.get_dynamic_step(signal)
            step.update(artifact_sha256=self.artifact, complete=False, gaps=['driver_set_incomplete'],
                        traversal=dict(search_exhaustive=False, returned_fact_count=1, incomplete_reasons=['state_limit']))
            return step
        def current(self):return True
    args = await setup(tmp_path)
    r = (await trace_x_history(server, args, route_factory=PartialRoute)).history
    assert 'driver_traversal_incomplete' in r.coverage['gaps']
    assert r.nodes[0]['structure']['traversal']['returned_fact_count'] == 1
    assert r.edges and all(e['relation'].startswith('candidate_') for e in r.edges)
    class TbRoute(PartialRoute):
        async def query(self, signal):
            return dict(signal=signal, backend='verdi_npi', artifact_sha256='tb', bits=[0], width=1,
                        boundary='unsupported', complete=False, branches=[], clock=None,
                        gaps=['testbench_driven'], cross_check=dict(conflict=True,
                        reason='driver_is_load_real_driver_is_testbench'))
    r = (await trace_x_history(server, args, route_factory=TbRoute)).history
    assert len(r.nodes) == 1 and not r.edges
    assert r.nodes[0]['structure']['cross_check']['conflict']
    assert 'testbench_driven' in r.coverage['gaps']


def test_npi_dynamic_uses_original_driver_own_load_alias_guard(monkeypatch):
    from types import SimpleNamespace
    from src.npi_dynamic import query_step
    from tests.test_verdi_npi_backend import _MockNet, _MockPin, _MockNetlist, _make_backend_with_mock_npi
    alias = _MockPin('top.intf#[3:4]')
    initial = _MockPin('top.top:Init2#Init2:87:89:Init.data')
    net = _MockNet(drivers=[alias, initial], loads=[alias])
    net.size = lambda:1
    net.left = net.right = lambda:0
    net.full_name = lambda:'top.data'
    net.is_signed = lambda:False
    net.is_literal = lambda:False
    net.type = lambda:'npiNlNet'
    netlist = _MockNetlist({'top.data':net})
    backend, _, _ = _make_backend_with_mock_npi(monkeypatch, netlist_obj=netlist)
    backend._npi_modules = (SimpleNamespace(), netlist)
    result = query_step(backend, 'top.data')
    assert result['gaps'] == ['testbench_driven']
    assert result['cross_check']['conflict'] and not result['branches']


@pytest.mark.anyio
async def test_mux_feedback_is_hold_only_for_exact_selected_state(tmp_path):
    from dataclasses import asdict
    from src.dynamic_evidence import Expr, TRUE, evaluate
    from tests.test_dynamic_evidence import project
    backend = project(RTL)
    ref = lambda name: Expr('signal', 1, signal='top.' + name, bits=(0,), declared_bits=(0,))
    # NPI represents a register enable as a data mux with Q feedback. Its
    # selected reference, not equality between two X values, proves retention.
    class MuxRoute:
        def __init__(self, *a):
            self.stage, self.artifact, self.receipt = 'verdi_npi', 'mux', {}
        async def query(self, signal):
            step = backend.get_dynamic_step(signal)
            step.update(backend=self.stage, artifact_sha256=self.artifact)
            if signal == 'top.q':
                step['branches'] = [dict(id='mux', order=0, guard=asdict(TRUE),
                    value=asdict(Expr('mux', 1, (ref('en'), ref('d'), ref('q')))))]
            return step
        def current(self): return True
    args = await setup(tmp_path)
    result = (await trace_x_history(server, args, route_factory=MuxRoute)).history
    assert [n.get('relation') for n in result.nodes[:3]] == ['hold', 'hold', 'register_sample']
    assert result.nodes[-1]['signal'] == 'top.d'
    # A boolean operation on unknown Q can still produce X, but is not Q hold.
    value = evaluate(Expr('not', 1, (ref('q'),)), lambda e: dict(value='x', gaps=[]))
    assert value.value == 'x' and value.reference is None


def test_selection_expansion_retains_expression_budget():
    from dataclasses import asdict
    from src.dynamic_evidence import Expr, TRUE
    from src.x_history_observe import select_step
    a = Expr('signal', 200, signal='a', bits=tuple(range(199, -1, -1)))
    b = Expr('signal', 200, signal='b', bits=tuple(range(199, -1, -1)))
    step = dict(bits=list(range(399, -1, -1)), width=400,
                branches=[dict(guard=asdict(TRUE), value=asdict(Expr('concat', 400, (a,b))))])
    # Interleaving 400 bits cannot bypass the 256-node typed-expression limit.
    bits = tuple(x for pair in zip(range(399,199,-1), range(199,-1,-1)) for x in pair)
    with pytest.raises(ValueError, match='dynamic_expression_limit'):
        select_step(step, bits)
