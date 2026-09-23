"""Expression observations retain indices, actual values and one backend."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest
import server
from src.dynamic_evidence import Expr, evaluate
from src.npi_dynamic import query_step
from src.verdi_npi_backend import VerdiNpiBackend
from tests.expression_oracle import cases
from tests.test_dynamic_evidence import project
from tests.test_expression_slang import sample_values


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(scope='module')
def native_design(tmp_path_factory):
    if os.environ.get('TRACEWEAVE_TEST_NPI_DYNAMIC') != '1':
        pytest.skip('set TRACEWEAVE_TEST_NPI_DYNAMIC=1 for live VCS/NPI oracle')
    root = tmp_path_factory.mktemp('expression_npi')
    ports = [f"input logic {'signed' if v.get('signed') else ''} [{v['width']-1}:0] {n}"
             for n,v in cases()[0]['inputs'].items()]
    ports += [f"output wire [{c['expected']['width']-1}:0] y_{c['id']}" for c in cases()]
    source = 'module expression_probe('+','.join(ports)+');\n'+ '\n'.join(
        f"assign y_{c['id']}=({c['expression']});" for c in cases())+'\nendmodule\n'
    src = root/'oracle.sv';src.write_text(source)
    command = ['vcs','-full64','-sverilog','-timescale=1ns/1ps','-debug_access+all','-kdb',
               str(src),'-top','expression_probe','-o','simv','-l','compile.log']
    run = subprocess.run(command,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
    assert run.returncode==0,run.stdout[-3000:]
    return root,source


def test_native_projection_or_slang_fallback_matches_all_oracle_vectors(native_design):
    root,source = native_design
    backend = VerdiNpiBackend()
    assert backend._ensure_loaded(str(root/'simv.daidir/kdb.elab++'),'expression_probe')
    source_backend = project(source)
    native = [];fallback = []
    for case in cases():
        target = 'expression_probe.y_'+case['id']
        step = query_step(backend,target)
        if step['complete']:
            native.append(case['id'])
        else:
            assert step['gaps'] and 'npi_dynamic_query_failed' not in step['gaps'], step
            fallback.append(case['id'])
            step = source_backend.get_dynamic_step(target)
        assert step['complete'],(case['id'],step)
        result = evaluate(Expr.from_dict(step['branches'][0]['value']),sample_values(
            {'expression_probe.'+n:v['value'] for n,v in case['inputs'].items()}))
        assert result.value==case['expected']['bin'],(case['id'],result)
    assert {'precedence','signed_division','power','bit_and','reduce_xor','shift_right'} <= set(native)
    assert {'case_equality','variable_bit','inside_range','clog2'} <= set(fallback)


def write_wave(path, *, index='011', result='00000110', unknown=False, scope='top'):
    path.write_text(f'''$timescale 1ps $end
$scope module {scope} $end
$var wire 8 ! a [7:0] $end
$var wire 3 " b [2:0] $end
$var wire 8 # c [7:0] $end
$var wire 8 $ y_variable_bit [7:0] $end
$upscope $end
$enddefinitions $end
#0
b00001000 !
b011 "
b00000101 #
b00000110 $
#5
b{index} "
b{result} $
#10
''')


async def context(root,monkeypatch,*,native=False,source=None):
    server.reset_session_state()
    monkeypatch.setenv('TRACEWEAVE_CONNECTIVITY_ROUTE','auto' if native else 'source_graph')
    monkeypatch.setenv('TRACEWEAVE_NPI_EXECUTION','local')
    monkeypatch.setenv('TRACEWEAVE_AUTO_KDB','0')
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH_PYTHON',sys.executable)
    if not native:
        src=root/'source.sv';src.write_text(source or 'module top(input logic [7:0] a,c, input logic [2:0] b, output logic [7:0] y_variable_bit); assign y_variable_bit=a[b]+c; endmodule')
        (root/'compile.log').write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {src} -top top\nParsing design file '{src}'\nTop Level Modules:\n       top\n")
    ctx = dict(compile_log=str(root/'compile.log'),simulator='vcs',top_hint='expression_probe' if native else 'top')
    await asyncio.gather(server._dispatch('build_tb_hierarchy',ctx),server._dispatch('scan_structural_risks',ctx))
    return ctx


async def divergence(tmp_path,ctx,scope):
    a,b=tmp_path/'a.vcd',tmp_path/'b.vcd'
    write_wave(a,scope=scope);write_wave(b,index='100',result='00000101',scope=scope)
    common=dict(**ctx,signal_path=scope+'.y_variable_bit')
    return await server._dispatch('trace_divergence',dict(
        side_a=dict(**common,wave_path=str(a)),side_b=dict(**common,wave_path=str(b)),
        start_time_ps=0,end_time_ps=10))


def check_index_trace(result,scope):
    assert result.comparison['first_divergence_time_ps']==5
    root=result.nodes[0]
    assert root['a']['observation']['value']=='00000110'
    assert root['b']['observation']['value']=='00000101'
    assert any(n['a']['signal']==scope+'.b' for n in result.nodes)
    assert any(f['kind']=='index_difference' for f in result.findings)
    assert any('index' in e['roles'] for e in result.edges)
    assert not any(f['reason']=='mapping_missing' for f in result.frontier)


@pytest.mark.anyio
async def test_dynamic_index_difference_is_traced_with_both_selections(tmp_path,monkeypatch):
    ctx=await context(tmp_path,monkeypatch)
    check_index_trace(await divergence(tmp_path,ctx,'top'),'top')


@pytest.mark.anyio
async def test_real_npi_to_slang_restart_keeps_only_final_backend(native_design,tmp_path,monkeypatch):
    root,_=native_design
    ctx=await context(root,monkeypatch,native=True)
    result=await divergence(tmp_path,ctx,'expression_probe')
    check_index_trace(result,'expression_probe')
    assert result.operation_metrics['restart_count']>=1
    for side in ('a','b'):
        status=result.contexts[side]['backend_status']
        assert status['actual_backend']=='source_graph'
        assert any(x['backend']=='verdi_npi' for x in status['attempted_backends'])
        assert all(n[side]['structure']['backend']=='source_graph' for n in result.nodes)


@pytest.mark.anyio
async def test_unknown_index_history_reaches_index_input(tmp_path,monkeypatch):
    ctx=await context(tmp_path,monkeypatch)
    path=tmp_path/'x.vcd';write_wave(path,index='xxx',result='xxxxxxxx')
    r=await server._dispatch('trace_x_source',dict(**ctx,wave_path=str(path),signal_path='top.y_variable_bit',
        mode='history',history_start_ps=0,time_ps=9))
    h=r.history.model_dump()
    assert any(n['signal'].startswith('top.b') for n in h['nodes']),h
    root=h['nodes'][0]
    assert root['observation']['value']=='xxxxxxxx'
    assert any(d['role']=='index' for d in root['observation']['dependencies'])
    assert not h['coverage']['true_origin_proven']


def test_wave_binding_preserves_expression_signedness(tmp_path):
    from src.vcd_parser import VCDParser
    from src.x_history_observe import bind_step_wave
    from src.dynamic_observe import observe_step
    backend=project('module top(input logic signed [7:0] a, output logic signed [7:0] y); assign y=a/8\'sd3; endmodule')
    path=tmp_path/'signed.vcd'
    path.write_text('''$timescale 1ps $end
$scope module top $end
$var wire 8 ! a [7:0] $end
$upscope $end
$enddefinitions $end
#0
b11111001 !
#10
''')
    parser=VCDParser(str(path))
    bound=bind_step_wave(backend.get_dynamic_step('top.y'),parser)
    r=observe_step(bound,get_parser=lambda _:parser,wave=str(path),time=9,history_start=0)
    assert r['complete'] and r['value']=='11111110'
    assert 'reference' not in r


@pytest.mark.anyio
async def test_dumped_memory_x_is_read_and_stops_at_storage_boundary(tmp_path,monkeypatch):
    ctx=await context(tmp_path,monkeypatch,source='module top(input logic [2:0] b, output logic [7:0] y_variable_bit); logic [7:0] mem [0:7]; assign y_variable_bit=mem[b]+8\'d1; endmodule')
    path=tmp_path/'mem.vcd'
    path.write_text('''$timescale 1ps $end
$scope module top $end
$var wire 3 ! b [2:0] $end
$var wire 8 " mem[2] [7:0] $end
$var wire 8 # y_variable_bit [7:0] $end
$upscope $end
$enddefinitions $end
#0
b010 !
b00000000 "
b00000001 #
#5
b0000000x "
bxxxxxxxx #
#10
''')
    result=await server._dispatch('trace_x_source',dict(**ctx,wave_path=str(path),signal_path='top.y_variable_bit',
        mode='history',history_start_ps=0,time_ps=9))
    h=result.history.model_dump()
    assert len(h['nodes'])==2,h
    assert h['nodes'][1]['signal']=='top.mem[2][7:0]'
    assert 'dumped_array_element_boundary' in h['coverage']['gaps']
    assert 'signal_or_selection_unavailable' not in h['coverage']['gaps']
