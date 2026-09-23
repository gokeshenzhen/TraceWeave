import json

import pytest
from jsonschema import Draft202012Validator

import server
from src.cursor_store import CursorStore
from src.verify_condition import diff_first_divergence,period
from src.schemas import DiffFirstDivergenceResult,PeriodResult
from tests.test_expression_observe import wave


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def reset():
    server.reset_session_state()
    yield
    server.reset_session_state()


def expression(expr='a[i] + e'):
    return dict(expr=expr,typing='wave_bits',bindings={'a':'tb.data','i':'tb.index','e':'tb.expected'})


@pytest.mark.anyio
async def test_public_point_transitions_around_and_cycle_inputs(tmp_path):
    base = wave(tmp_path)
    common = dict(wave_path=base.file_path)
    point = await server._dispatch('get_signal_at_time',{**common,'signal_path':expression(),'time_ps':5})
    assert point.value['dec']==6 and point.expressions[0].width==8
    assert point.expressions[0].observations[0].dependencies[0].role=='index'
    transitions = await server._dispatch('get_signal_transitions',{**common,'signal_path':expression(),
        'start_time_ps':5,'end_time_ps':25,'max_transitions':1})
    assert transitions.transition_count==2 and transitions.truncated
    assert not transitions.transition_count_is_lower_bound
    assert transitions.predecessor_kind=='dependency_anchor'
    assert transitions.expressions[0].coverage_status=='complete'
    around = await server._dispatch('get_signals_around_time',{**common,'signal_paths':[expression()],
        'center_time_ps':15,'window_ps':5,'return_mode':'values_only'})
    entry = around.signals[around.expressions[0].key]
    assert entry['value_at_center']['dec']==5 and entry['window_transition_count']==2
    cycle = await server._dispatch('get_signals_by_cycle',{**common,'signal_paths':[expression()],
        'clock_path':dict(expr='tb.clk',typing='wave_bits'),'num_cycles':4})
    key = next(e.key for e in cycle.expressions if e.expr=='a[i] + e')
    assert [row.signals[key].dec for row in cycle.cycles]==[6,5,6,None]


@pytest.mark.anyio
async def test_input_json_schema_resolves_shared_recursive_type_definitions(tmp_path):
    tools = {t.name:t for t in await server.list_tools()}
    specs = {
        'get_signal_at_time':dict(wave_path='a.vcd',signal_path=expression(),time_ps=0),
        'get_signal_transitions':dict(wave_path='a.vcd',signal_path=expression()),
        'get_signals_around_time':dict(wave_path='a.vcd',signal_paths=[expression()],center_time_ps=0),
        'get_signals_by_cycle':dict(wave_path='a.vcd',clock_path='tb.clk',signal_paths=[expression()]),
        'diff_first_divergence':dict(wave_path_a='a.vcd',signal_a=expression(),wave_path_b='b.vcd',signal_b=expression()),
        'period':dict(wave_path='a.vcd',signal=dict(expr='tb.clk',typing='wave_bits')),
    }
    for name,args in specs.items():
        Draft202012Validator.check_schema(tools[name].inputSchema)
        Draft202012Validator(tools[name].inputSchema).validate(args)
    assert not any('expression' in name for name in tools)


def test_same_wave_independent_expression_bindings_and_exact_diff(tmp_path):
    base = wave(tmp_path,scale='100fs',small_pages=1)
    a = expression('a[i]')
    b = expression("1'b1")
    result = diff_first_divergence(get_parser=lambda _:base,wave_path_a=base.file_path,
        signal_a=a,wave_path_b=base.file_path,signal_b=b,cursor_store=CursorStore())
    parsed = DiffFirstDivergenceResult.model_validate(result)
    assert parsed.first_divergence_time_fs==1000 and parsed.earliest_difference_proven
    assert {r.side for r in parsed.expressions}=={'a','b'}
    assert parsed.cursor.metadata['signal_a'].startswith('expr@')


@pytest.mark.anyio
async def test_public_diff_relays_real_dependencies_not_display_keys(tmp_path):
    base = wave(tmp_path)
    result = await server._dispatch('diff_first_divergence',dict(wave_path_a=base.file_path,
        signal_a=expression('a[i]'),wave_path_b=base.file_path,signal_b=expression("1'b1")))
    assert result.diverged and result.first_divergence_time_ps==10
    assert result.next_actions
    assert {a.arguments['signal_path'] for a in result.next_actions}=={'tb.data[7:0]','tb.index[2:0]'}
    assert all(a.reason=='inspect_expression_dependency' for a in result.next_actions)


def test_derived_period_uses_exact_event_edges_and_raw_units(tmp_path):
    base = wave(tmp_path,scale='100fs')
    result = period(get_parser=lambda _:base,wave_path=base.file_path,
        signal=dict(expr='tb.clk',typing='wave_bits'),cursor_store=CursorStore())
    parsed = PeriodResult.model_validate(result)
    assert parsed.period_fs==1000 and parsed.period_ps==1 and parsed.edges_used==4
    with pytest.raises(ValueError,match='1-bit'):
        period(get_parser=lambda _:base,wave_path=base.file_path,signal=expression())


@pytest.mark.anyio
async def test_public_dynamic_dimension_queries_need_only_index_waveform(tmp_path):
    base=wave(tmp_path)
    spec=dict(expr='$size(a,dim)',bindings={'dim':'tb.index'},types={
        'a':dict(width=8,packed=[[7,0]],unpacked=[[1,2],[0,2]]),'dim':dict(width=3)})
    point=await server._dispatch('get_signal_at_time',dict(wave_path=base.file_path,signal_path=spec,time_ps=5))
    assert point.value['dec']==8
    assert point.expressions[0].dependencies==['tb.index[2:0]']
    rows=await server._dispatch('get_signal_transitions',dict(wave_path=base.file_path,signal_path=spec,
        start_time_ps=5,end_time_ps=35))
    assert rows.predecessor['value']['dec']==8
    assert [row['time_ps'] for row in rows.transitions]==[10]
    assert 'dimension_unknown' in rows.expressions[0].gaps
