"""Same-process consumer sequences over two independent prepared designs.

Real compile capture, registry lifecycle, codecs and hand-written VCD reads
are exercised. The copied Language/connectivity facts below are controls,
not SDK, checkout, source/KDB association or performance acceptance. Existing
per-tool tests own exhaustive argument/budget/typed-error matrices.
"""
from __future__ import annotations

import asyncio
import builtins
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace

import anyio
import pytest

import config
import server
from src import connectivity_backend, divergence_context, divergence_routing, x_history
from src.compile_input_capture import CompileInputCapture
from tests.test_public_design_instance import anyio_backend, isolate, forbidden, prepare, public as _public
from tests.test_public_design_files import project
from tests.test_public_design_classes_exports import Controls, Provider, flatten, METADATA, SEMANTIC
from tests.test_design_context_x_server import _step


ORIGINAL_GET_PARSER = server._get_parser
VECTORS = {'d', 'q', 'y', 'packet'}


async def public(name, **arguments):
    # Driver defaults to the compact evidence envelope. This sequence checks
    # the full contract; presentation equivalence has its own regression.
    if name == 'explain_signal_driver':
        arguments.setdefault('output_format', 'full')
    return await _public(name, **arguments)


def wave(path, label):
    """Known divergence at 5; only A injects X at 12 and captures it at 15.

    Enable goes low at 20: the edges at 25 and 35 hold the captured value.
    These are synthetic controls, not outputs from a simulated user design.
    """
    known = '01010101' if label == 'A' else '01101010'
    rows = {0: dict(clk='0', rst='0', en='1', d=known, q='00000000',
                    y='00000000', packet='00000000'),
            3: dict(d=known), 5: dict(clk='1', q=known, y=known),
            10: dict(clk='0'), 12: dict(d='xxxxxxxx' if label == 'A' else known),
            15: dict(clk='1', q='xxxxxxxx' if label == 'A' else known,
                     y='xxxxxxxx' if label == 'A' else known),
            18: dict(d=known), 20: dict(clk='0', en='0'), 25: dict(clk='1'),
            30: dict(clk='0'), 35: dict(clk='1'), 40: dict(clk='0')}
    symbols = dict(clk='!', rst='@', en='#', d='$', q='%', y='&', packet='?')
    text = '$timescale 1ps $end\n$scope module top $end\n'
    for name, symbol in symbols.items():
        width = 8 if name in VECTORS else 1
        suffix = ' [7:0]' if width == 8 else ''
        text += f'$var wire {width} {symbol} {name}{suffix} $end\n'
    text += '$upscope $end\n$enddefinitions $end\n'
    for at, values in sorted(rows.items()):
        text += f'#{at}\n'
        for name, value in values.items():
            text += f'b{value} {symbols[name]}\n' if name in VECTORS else f'{value}{symbols[name]}\n'
    path.write_text(text)


class World:
    def __init__(self):
        self.thread = threading.get_ident()
        self.cases, self.calls, self.selections, self.wave_threads = [], [], [], []
        self.captures, self.validations = [], []
        self.action = None

    def outside_wave(self):
        paths = [str(case.wave) for case in self.cases]
        assert not any(lock.locked() for lock in server._wave_locks_for(paths))

    async def case(self, root, label):
        root.mkdir()
        first, second, third, alias, log, maps = project(root)
        # Same top and basename deliberately occur in both independent cases.
        members = ('logic [2:0] tag; logic [4:0] payload;' if label == 'A'
                   else 'logic [4:0] payload; logic [2:0] tag;')
        first.write_text(f'// independently captured design {label}\n'
            'module top(input logic clk,rst,en,input logic [7:0] d,output logic [7:0] q,output wire [7:0] y);\n'
            'always_ff @(posedge clk) if(rst) q<=0; else if(en) q<=d; assign y=q;\n'
            f'typedef struct packed {{{members}}} packet_t; packet_t packet;\n'
            'endmodule\nmodule alpha; endmodule\n')
        (maps / '_libFileMap').write_bytes(('map-' + label).encode())
        (maps / '_stnElabDB').write_bytes(('elab-' + label).encode())
        facts = Controls()
        facts.configure(first, second, third, alias)
        case = SimpleNamespace(root=root, label=label, first=first, second=second, third=third,
                               alias=alias, log=log, maps=maps, facts=facts,
                               wave=root / 'wave.vcd', run=root / 'run.log')
        wave(case.wave, label)
        case.run.write_text(f'"{first}", 1: top.g[0].u: started at 30ps failed at 36ps\n')
        case.handle = await prepare(log, simulator='vcs', top_hint='top', kdb_path=str(maps))
        with server._design_context_store.pin(case.handle) as entry:
            case.context, case.identity = entry.context, entry.context.identity_sha256
        self.cases.append(case)
        return case

    def select(self, status, *, fallback):
        self.outside_wave()
        assert threading.get_ident() == self.thread
        self.selections.append(deepcopy(status))
        return Backend(self)


class Backend:
    name, execution_mode, uses_external_worker = 'verdi_npi', 'local', False

    def __init__(self, world):
        self.world, self.loaded, self.anchor = world, False, None

    @property
    def kdb_status(self):
        # Loading an earlier backend must not make a clean later X look loaded.
        return {'load_quality': 'clean'} if self.loaded else None

    def bind_compile_context(self, value):
        self.metadata = deepcopy(value)
        matches = [case for case in self.world.cases if str(case.root) in json.dumps(value)]
        assert len(matches) == 1, 'bound compile copy must identify exactly one case'
        self.case = matches[0]

    def bind_kdb_anchor(self, anchor):
        assert anchor.canonical_path == str(self.case.maps)
        self.anchor = anchor

    def enter(self, operation, target):
        self.world.outside_wave()
        assert threading.get_ident() == self.world.thread
        assert self.anchor is not None and self.metadata == self.case.context.compile_result()
        self.loaded = True
        self.world.calls.append((self.case.label, operation, target))
        if self.world.action:
            self.world.action(self, operation, target)

    def query_source(self, request):
        self.enter(request.operation, request.arguments_json)
        assert request.context.identity_sha256 == self.case.identity
        identity = hashlib.sha256((self.anchor.identity_sha256 + '\0top').encode()).hexdigest()
        if request.operation == 'resolve_packed_fields':
            # Independent copied-layout routing control, not a Typespec oracle.
            bits = ({'tag': [7, 6, 5], 'payload': [4, 3, 2, 1, 0]} if self.case.label == 'A'
                    else {'tag': [2, 1, 0], 'payload': [7, 6, 5, 4, 3]})
            value = dict(schema_version=2, facts_origin='verdi_npi', artifact_identity=identity,
                coverage_status='complete', coverage_domain='requested_packed_fields', coverage_gaps=[],
                analysis_limits=[], query_metrics=request.budget.metrics(), semantic_status='complete',
                source_signal='top.packet', status='resolved', field_bits=bits,
                declared_range={'left': 7, 'right': 0})
        else:
            value = request.execute(Provider(self.case.facts, identity))
        value['context_identity_sha256'] = self.case.identity
        value['kdb_binding'] = dict(self.anchor.receipt(), load_status='loaded', load_quality='clean')
        return value

    def find_driver(self, **kwargs):
        signal = kwargs['signal_path']
        self.enter('driver', signal)
        leaf = signal.rsplit('.', 1)[-1].split('[', 1)[0]
        upstream = ['top.q'] if leaf == 'y' else ['top.d'] if leaf == 'q' else []
        return dict(signal_path=signal, wave_path=kwargs['wave_path'], resolved_rtl_name=leaf,
            driver_status='resolved' if upstream else 'testbench_driven', driver_kind='assign',
            source_file=str(self.case.first), source_line=1, upstream_signals=upstream,
            expression_summary='copied-' + self.case.label, recursive=kwargs.get('recursive', False),
            backend='verdi_npi')

    def find_loads(self, **kwargs):
        signal = kwargs['signal_path']
        self.enter('loads', signal)
        return dict(signal_path=signal, resolved_rtl_name=signal.rsplit('.', 1)[-1], resolved_module='top',
            loads=[dict(load_path='top.y', kind='rhs_expr', source_file=str(self.case.first),
                        source_line=1, expr='copied-' + self.case.label, backend='verdi_npi')],
            completeness='exact', backend='verdi_npi')

    def find_path(self, **kwargs):
        self.enter('path', (kwargs['from_signal'], kwargs['to_signal']))
        return dict(from_signal=kwargs['from_signal'], to_signal=kwargs['to_signal'], found=True, hops=1,
            path=[dict(index=i, net_path=signal, is_endpoint=True, source_file=str(self.case.first),
                       source_line=1, backend='verdi_npi')
                  for i, signal in enumerate((kwargs['from_signal'], kwargs['to_signal']))], backend='verdi_npi')

    def get_dynamic_step(self, signal_path, **kwargs):
        self.enter('dynamic', signal_path)
        result = _step(signal_path.split('[', 1)[0], width=8)
        result['sources'] = [{'file': str(self.case.first), 'line': 1}]
        return result


@pytest.fixture
def world(monkeypatch, isolate):
    world = World()
    monkeypatch.setattr(config, 'AUTO_KDB_BUILD', False)
    import_module = builtins.__import__
    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if any(part in {'pynpi', 'pyslang'} for item in (name, *(str(x) for x in fromlist or ()))
               for part in item.split('.')):
            forbidden()
        return import_module(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', importing)
    for module, name in ((server, '_build_recovery_context'), (server, '_resolve_hierarchy_context'),
                         (server, '_resolve_source_packed_fields'), (server, 'get_source_graph_runtime'),
                         (server, 'build_source_graph_trace_plan'), (x_history, 'resolve_context'),
                         (divergence_context, 'resolve_context')):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(connectivity_backend, 'select_backend', world.select)
    monkeypatch.setattr(divergence_routing, 'select_backend', world.select)
    monkeypatch.setattr(connectivity_backend, 'StaticConnectivityBackend', forbidden)
    monkeypatch.setattr(divergence_routing, 'StaticConnectivityBackend', forbidden)
    capture_init = CompileInputCapture.__init__
    def capture(self, *a, **kw):
        capture_init(self, *a, **kw)
        world.captures.append(self)
    monkeypatch.setattr(CompileInputCapture, '__init__', capture)
    original_wave = server._run_in_wave_request
    async def wave_request(paths, fn, **kwargs):
        paths = [paths] if isinstance(paths, str) else paths
        def checked():
            assert threading.get_ident() != world.thread
            assert all(lock.locked() for lock in server._wave_locks_for(paths))
            world.wave_threads.append(threading.get_ident())
            return fn()
        return await original_wave(paths, checked, **kwargs)
    monkeypatch.setattr(server, '_run_in_wave_request', wave_request)
    def parser(path):
        assert threading.get_ident() != world.thread
        assert all(lock.locked() for lock in server._wave_locks_for([path]))
        return ORIGINAL_GET_PARSER(path)
    monkeypatch.setattr(server, '_get_parser', parser)
    for name in ('_validate_design_source', '_validate_design_binding'):
        original = getattr(server, name)
        async def validation(*args, _fn=original, **kwargs):
            world.outside_wave()
            world.validations.append(_fn.__name__)
            return await _fn(*args, **kwargs)
        monkeypatch.setattr(server, name, validation)
    yield world
    assert world.captures and all(c.body_count == c.resident_bytes == 0 for c in world.captures)
    assert all(sorted(c.facts.opened) == sorted(c.facts.closed) for c in world.cases)
    assert server._session_state['build_tb_hierarchy'] is None
    assert server._result_cache['build_tb_hierarchy'] is None


async def pair(world, tmp_path):
    a, b = await world.case(tmp_path / 'A', 'A'), await world.case(tmp_path / 'B', 'B')
    assert a.identity != b.identity and a.handle != b.handle
    return a, b


def semantic(result, case):
    assert 'error' not in result, result
    status = result['backend_status']
    assert status['actual_backend'] == 'verdi_npi'
    receipt = status['design_context']
    assert receipt['design_handle'] == case.handle
    assert receipt['context_identity_sha256'] == case.identity
    assert receipt['artifact']['source_association'] == 'unverified'


async def discover(case):
    result = await public('get_sim_paths', verif_root=str(case.root), compile_log=str(case.log),
                          sim_log=str(case.run), wave_file=str(case.wave))
    assert 'error' not in result, result
    assert result['next_required_step']['tool'] == 'scan_structural_risks'
    return result


async def driver(case, **extra):
    return await public('explain_signal_driver', design_handle=case.handle, wave_path=str(case.wave),
                        signal_path='top.q', recursive=False, **extra)


async def recommend(case):
    return await public('recommend_failure_debug_next_steps', design_handle=case.handle,
                        log_path=str(case.run), wave_path=str(case.wave), simulator='vcs')


def cursors():
    return [row.as_dict() for row in server._cursor_store.list()]


@pytest.mark.anyio
async def test_interleaved_six_tools_seven_sections_and_connectivity_keep_each_captured_design(world, tmp_path):
    a, b = await pair(world, tmp_path)
    for case in (a, b):
        before = len(world.selections)
        metadata = await public('lookup_tb_files', design_handle=case.handle, basename='shared.sv')
        assert metadata['facts_origin'] == 'compile_metadata'
        assert {r['path'] for r in metadata['matches']} == {str(case.first), str(case.second)}
        assert metadata['ownership_identity'] == case.identity
        assert not {'backend_status', 'kdb_binding', 'design_context'} & metadata.keys()
        assert len(world.selections) == before

    for operation in ('find_tb_instance', 'get_tb_subtree', 'get_tb_file_detail', 'lookup_tb_files',
                      'get_tb_class_hierarchy'):
        for case in (a, b):
            arguments = {'find_tb_instance': dict(path='top.g[0].u'),
                'get_tb_subtree': dict(root='top', depth=3, max_nodes=10, count_mode='exact'),
                'get_tb_file_detail': dict(path=str(case.alias)),
                'lookup_tb_files': dict(has_module='top'), 'get_tb_class_hierarchy': {}}[operation]
            result = await public(operation, design_handle=case.handle, **arguments)
            semantic(result, case)
            if operation == 'find_tb_instance':
                assert [r['path'] for r in result['hits']] == ['top.g[0].u']
                assert result['hits'][0]['definition_file'] == '/outside/leaf.sv'
                assert 'npi_definition_location_unbound' in result['hits'][0]['gaps']
            elif operation == 'get_tb_subtree':
                root = result['node']
                assert root['path'] == 'top' and result['total_descendants'] == 2
                gen = root['children'][0]
                assert gen['path'] == 'top.g[0]' and gen['children'][0]['parent'] == 'top.g[0]'
                assert gen['source_file'] == str(case.alias)
                assert 'npi_instance_location_unbound' not in gen['gaps']
            elif operation == 'get_tb_file_detail':
                assert result['path'] == str(case.first) and result['modules'] == ['top']
                assert result['ownership_identity'] == case.identity
                assert result['semantic_work_scope'] == 'global_namespaces_bounded'
            elif operation == 'lookup_tb_files':
                assert [r['path'] for r in result['matches']] == [str(case.first)]
                assert result['coverage_status'] == 'partial'
            else:
                rows = flatten(result['roots'])
                assert len(rows) == result['total'] == 6 and result['class_inventory_lower_bound'] == 5
                assert [r['symbol_id'] for r in rows['class-RB']['children']] == ['class-Child']
                assert rows['class-LB']['source_file'] == str(case.alias)
                assert result['coverage_status'] == 'partial' and result['class_inventory_complete'] is False

    for section in (*METADATA, *SEMANTIC):
        for case in (a, b):
            before = len(world.selections)
            result = await public('dump_tb_section', design_handle=case.handle, section=section)
            assert result['section'] == section and result['context_identity_sha256'] == case.identity
            if section in METADATA:
                record = case.context.compile_result()
                assert result['data'] == (record if section == 'compile_result' else record.get(section, {}))
                assert result['record_export_complete'] is True
                assert result['facts_origin'] == 'compile_metadata' and result['semantic_status'] == 'not_run'
                assert len(world.selections) == before and 'backend_status' not in result
            else:
                semantic(result, case)
                if section == 'files_full':
                    assert {r['path'] for r in result['data']} == {str(case.first), str(case.second), str(case.third)}
                elif section == 'interfaces':
                    assert [r['name'] for r in result['data']] == ['bus_if', 'aux_if']
                    assert [r['source_file'] for r in result['data']] == [str(case.alias), str(case.second)]
                elif section == 'class_hierarchy_full':
                    assert len(flatten(result['data'])) == 6
                else:
                    assert result['root'] == result['data']['path'] == 'top'
                    assert result['total_descendants'] == 2 and result['export_complete'] is True
                if section != 'component_tree_full':
                    assert result['export_complete'] is False

    for operation in ('explain_signal_driver', 'find_signal_loads', 'trace_signal_path'):
        for case in (a, b):
            args = dict(design_handle=case.handle, wave_path=str(case.wave))
            args.update(dict(from_signal='top.d', to_signal='top.q') if operation == 'trace_signal_path'
                        else dict(signal_path='top.q'))
            result = await public(operation, **args)
            semantic(result, case)
            if operation == 'explain_signal_driver':
                assert result['source_file'] == str(case.first) and result['expression_summary'] == 'copied-' + case.label
            elif operation == 'find_signal_loads':
                assert [(r['load_path'], r['source_file']) for r in result['loads']] == [('top.y', str(case.first))]
            else:
                assert result['found'] is True
                assert [r['net_path'] for r in result['path']] == ['top.d', 'top.q']
    assert {label for label, _, _ in world.calls} == {'A', 'B'}
    assert all(case.context.current() for case in (a, b))


@pytest.mark.anyio
async def test_trace_failure_packed_diff_actions_and_diagnostic_share_only_explicit_identity(world, tmp_path):
    a, b = await pair(world, tmp_path)
    await discover(a)
    # B has genuinely entered our control backend before this clean X query.
    semantic(await driver(b), b)
    before = list(world.calls)
    clean = await public('trace_x_source', design_handle=b.handle, wave_path=str(b.wave),
                         signal_path='top.q', time_ps=36)
    assert clean['trace_status'] == 'signal_is_clean' and world.calls == before
    assert clean['backend_status']['design_context']['artifact']['load_status'] == 'not_checked'
    snapshot = await public('trace_x_source', design_handle=a.handle, wave_path=str(a.wave),
                            signal_path='top.y', time_ps=36)
    semantic(snapshot, a)
    assert snapshot['propagation_chain']
    assert all(row['source_file'] == str(a.first) for row in snapshot['propagation_chain'])
    history = await public('trace_x_source', design_handle=a.handle, wave_path=str(a.wave),
        signal_path='top.q', time_ps=36, mode='history', history_start_ps=0)
    semantic(history, a)
    nodes = history['history']['nodes']
    # VCD vector declarations carry their full range into history selections.
    assert [(row['signal'], row['time_ps'], row['phase']) for row in nodes] == [
        ('top.q[7:0]', 36, 'after'), ('top.q[7:0]', 35, 'before'),
        ('top.q[7:0]', 25, 'before'), ('top.d[7:0]', 15, 'before')]
    assert [row['relation'] for row in nodes[:3]] == ['hold', 'hold', 'register_sample']
    assert nodes[-1]['value'] == 'xxxxxxxx'
    assert history['history']['coverage']['true_origin_proven'] is False

    for case in (a, b):
        event = dict(event_id='hand-' + case.label, kind='assertion', time_ps=36, line=1,
                     instance_path='top.g[0].u', source_file=str(case.first), message='handwritten failure')
        result = await public('analyze_failure_event', design_handle=case.handle, failure_event=event,
            log_path=str(case.run), wave_path=str(case.wave), simulator='vcs')
        semantic(result, case)
        assert [r['instance_path'] for r in result['likely_instances']] == ['top.g[0].u', 'top', 'top.g[0]']
        correlation = result['source_correlation']
        assert correlation['context_identity_sha256'] == case.identity
        assert correlation['full_hierarchy_fallback'] is False and correlation['coverage_status'] == 'partial'
        assert len(correlation['queries']) == 2
        rec = await recommend(case)
        semantic(rec, case)
        assert rec['recommended_instances'][0]['instance_path'] == 'top.g[0].u'
        assert rec['workflow_incomplete'] and rec['required_next_call']['tool'] == 'scan_structural_risks'
        assert server._result_provenance['recommend_failure_debug_next_steps']['design_handle'] == case.handle
        packed = await public('resolve_packed_fields', design_handle=case.handle, wave_path=str(case.wave),
            signal_path='top.packet[7:0]', source_signal='top.packet', fields=['tag', 'payload'])
        semantic(packed, case)
        expected = {'tag': [7, 6, 5], 'payload': [4, 3, 2, 1, 0]} if case is a else {
            'tag': [2, 1, 0], 'payload': [7, 6, 5, 4, 3]}
        assert packed['fields'] == {name: {'path': 'top.packet[7:0]', 'bits': bits} for name, bits in expected.items()}

    before = (len(world.selections), len(world.calls))
    diff = await public('diff_first_divergence', wave_path_a=str(a.wave), signal_a='top.q',
        wave_path_b=str(b.wave), signal_b='top.q', start_time_ps=0, end_time_ps=10,
        context_a={'design_handle': a.handle}, context_b={'design_handle': b.handle})
    assert diff['diverged'] is True and diff['first_divergence_time_ps'] == 5
    assert (len(world.selections), len(world.calls)) == before  # Actions do not load NPI.
    actions = [r for r in diff['next_actions'] if r['tool'] == 'explain_signal_driver']
    assert [r['arguments']['design_handle'] for r in actions] == [a.handle, b.handle]
    assert [r['evidence']['context_identity_sha256'] for r in actions] == [a.identity, b.identity]
    for action, case in zip(actions, (a, b)):
        semantic(await public(action['tool'], **action['arguments']), case)

    graph = await public('trace_divergence',
        side_a=dict(design_handle=a.handle, wave_path=str(a.wave), signal_path='top.q'),
        side_b=dict(design_handle=b.handle, wave_path=str(b.wave), signal_path='top.q'),
        start_time_ps=0, end_time_ps=10,
        signal_pairs=[dict(a='top.' + name, b='top.' + name) for name in ('d', 'en', 'rst')])
    assert graph['comparison']['first_divergence_time_ps'] == 5
    assert graph['nodes'][0]['a']['observation']['trigger_time_ps'] == 5
    data = next(row for row in graph['nodes'] if row['a']['signal'] == 'top.d')
    assert (data['a']['value'], data['b']['value']) == ('01010101', '01101010')
    assert graph['operation_metrics']['design_context_count'] == 2
    for side, case in (('a', a), ('b', b)):
        assert graph['contexts'][side]['design_context']['design_handle'] == case.handle
        assert graph['contexts'][side]['backend_status']['actual_backend'] == 'verdi_npi'

    # Discovery anchors diagnostics; the most recent successful recommendation
    # belongs to B and cannot become a current recommendation for selected A.
    before = (len(world.selections), len(world.calls))
    diagnostic = await public('get_diagnostic_snapshot', design_handle=a.handle)
    assert diagnostic['design_context']['summary']['context_identity_sha256'] == a.identity
    assert diagnostic['design_context']['summary']['identity_validation'] == 'not_revalidated'
    assert diagnostic['recommended_next']['stale'] is True
    assert diagnostic['hierarchy']['available'] is False
    analyzed = await public('analyze_failures', log_path=str(a.run), wave_path=str(a.wave),
                            simulator='vcs', signal_paths=['top.q'])
    assert analyzed['summary']['runtime_total_errors'] == 1 and analyzed['focused_event']['time_ps'] == 36
    assert 'top.q' in analyzed['wave_context']['signals']
    assert (len(world.selections), len(world.calls)) == before  # Wave-only and cache-only consumers.
    assert world.wave_threads and world.validations


@pytest.mark.anyio
@pytest.mark.parametrize('changed', ['source', 'maps'])
async def test_invalidated_A_discards_later_graph_and_cursors_while_B_remains_usable(world, tmp_path, changed):
    a, b = await pair(world, tmp_path)
    await discover(a)
    semantic(await driver(a), a)
    semantic(await driver(b), b)
    assert 'error' not in await recommend(a)
    before_cursor = cursors()
    target = a.first if changed == 'source' else a.maps / '_stnElabDB'
    target.write_bytes(target.read_bytes() + b' changed')
    rejected = await driver(a)
    assert rejected['error_code'] == ('design_context_changed' if changed == 'source' else 'kdb_identity_changed')
    semantic(await driver(b), b)
    graph = await public('trace_divergence',
        side_a=dict(design_handle=a.handle, wave_path=str(a.wave), signal_path='top.q'),
        side_b=dict(design_handle=b.handle, wave_path=str(b.wave), signal_path='top.q'),
        start_time_ps=0, end_time_ps=10)
    assert graph['status'] == 'inconclusive' and graph['comparison'] == {}
    assert graph['nodes'] == graph['edges'] == graph['findings'] == [] and graph.get('cursor') is None
    assert cursors() == before_cursor
    diagnostic = await public('get_diagnostic_snapshot', design_handle=a.handle)
    assert diagnostic['design_context']['summary']['identity_validation'] == 'not_revalidated'
    assert server._design_context_store.remove(a.handle) is True
    diagnostic = await public('get_diagnostic_snapshot', design_handle=a.handle)
    assert diagnostic['design_context']['available'] is False and diagnostic['recommended_next']['stale'] is True
    assert b.context.current()


@pytest.mark.anyio
@pytest.mark.parametrize('interruption', ['cancel', 'reset'])
async def test_waiting_failure_consumer_holds_pin_and_cannot_publish_after_cancel_or_reset(
        world, tmp_path, monkeypatch, interruption):
    a, b = await pair(world, tmp_path)
    initial_cursors = cursors()
    started, release = anyio.Event(), anyio.Event()
    original_wave = server._run_in_wave_request
    async def gated(paths, fn, **kwargs):
        normalized = [paths] if isinstance(paths, str) else paths
        if str(a.wave) in normalized:
            started.set()
            await release.wait()  # No worker/lock exists yet; B remains usable.
        return await original_wave(paths, fn, **kwargs)
    monkeypatch.setattr(server, '_run_in_wave_request', gated)
    task = asyncio.create_task(recommend(a))
    try:
        with anyio.fail_after(3):
            await started.wait()
        assert server._design_context_store.remove(a.handle) is False
        assert any(label == 'A' for label, _, _ in world.calls)
        semantic(await driver(b), b)
        if interruption == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert server._design_context_store.remove(a.handle) is True
        else:
            server.reset_session_state()
            release.set()
            result = await task
            assert result['error_code'] == 'design_context_generation_changed'
        assert server._result_cache['recommend_failure_debug_next_steps'] is None
        assert server._result_provenance.get('recommend_failure_debug_next_steps') is None
        assert cursors() == initial_cursors
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
