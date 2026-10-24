"""Public exact-delay controls must survive schema and dispatch."""
import pytest
from jsonschema import Draft202012Validator
import server
from tests.test_window_verify import _build_vcd, _RA_SIGS


@pytest.mark.anyio
async def test_window_exact_controls_reach_sampler(tmp_path):
    wave=tmp_path/'window.vcd'
    wave.write_text(_build_vcd([
        {'req':1,'ack':0},{'req':1,'ack':1},{'req':0,'ack':0},{'req':0,'ack':0}],_RA_SIGS))
    tool=next(t for t in await server.list_tools() if t.name=='verify_window')
    args=dict(wave_path=str(wave),clock='top.clk',mode='implication',
        antecedent=[dict(signal='top.req',op='eq',value=1)],
        consequent=[dict(signal='top.ack',op='eq',value=1)],
        sample_phase='before',response_min_cycles=3,response_max_cycles=3,attempt_start_indices=[0])
    Draft202012Validator(tool.inputSchema).validate(args)
    r=await server._dispatch('verify_window',args)
    assert r.violation_count==1 and r.attempts_evaluated==1
    assert r.sample_phase=='before' and r.sample_offset_ps==0
    assert r.response_min_cycles==r.response_max_cycles==3
    assert 'default' not in tool.inputSchema['properties']['within_cycles']
    assert 'default' not in tool.inputSchema['properties']['overlap']
