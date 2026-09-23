from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

import server
from src.schemas import WindowVerifyResult,HandshakeInspectResult,TlulInspectResult,TxnReconstructResult
from src.tlul import inspect_tlul
from src.transaction_sampling import TransactionLimits
from src.txn_reconstruct import reconstruct_transactions
from src.verify_condition import inspect_handshake
from src.window_verify import verify_window
from tests.test_expression_observe import wave
from tests.test_tlul import fixture as tlul_fixture


def expr(text,bindings=None):
    return dict(expr=text,typing='wave_bits',bindings=bindings or {})


def test_conditions_truth_unknown_and_sequence_attribution(tmp_path):
    p = wave(tmp_path)
    kw = dict(get_parser=lambda _:p,wave_path=p.file_path,clock='tb.clk')
    r = verify_window(**kw,mode='always',predicate=[expr("tb.data[tb.index] == 1'b1")])
    WindowVerifyResult.model_validate(r)
    assert not r['holds'] and r['counterexample']['time_ps']==15
    assert r['unknown_cycles']==1 and r['coverage_status']=='partial'
    r = verify_window(**kw,mode='always',predicate=[expr("2'b1x")])
    assert r['holds'] and r['unknown_cycles']==0
    r = verify_window(**kw,mode='sequence',predicate=[expr("1'b1")],
        delta=dict(signal=expr('tb.index'),value=1))
    WindowVerifyResult.model_validate(r)
    assert r['violating_signal'] is None and r['violating_expression'].startswith('expr@')
    assert r['next_actions'][0]['signal_path']=='tb.index[2:0]'


def test_unknown_response_is_inconclusive_not_failed(tmp_path):
    p = wave(tmp_path)
    r = verify_window(get_parser=lambda _:p,wave_path=p.file_path,clock='tb.clk',mode='implication',
        antecedent=[expr("1'b1")],consequent=[expr("1'bx")],within_cycles=1,overlap=False)
    assert r['violation_count']==0 and r['inconclusive_count']==4
    assert r['unknown_cycles']==3 and r['coverage_status']=='partial' and not r['vacuous']


def test_missing_element_is_not_is_x_success(tmp_path):
    p = wave(tmp_path)
    e = dict(expr='mem[1]',bindings={'mem':{'elements':[]}},types={'mem':{'width':8,'unpacked':[[0,3]]}})
    r = verify_window(get_parser=lambda _:p,wave_path=p.file_path,clock='tb.clk',mode='eventually',
        predicate=[dict(signal=e,op='is_x')])
    assert not r['holds'] and r['unknown_cycles']==4


def test_handshake_expression_payload_keeps_real_dependencies(tmp_path):
    p = wave(tmp_path)
    r = inspect_handshake(get_parser=lambda _:p,wave_path=p.file_path,clock=expr('$signed(tb.clk)'),
        valid=expr("1'b1"),ready=expr("1'b0"),payload=[expr('tb.index + 3\'d1')])
    HandshakeInspectResult.model_validate(r)
    assert r['payload_hold_violations']==1 and r['sampling_phase']=='before'
    assert r['violating_signal'] is None and r['violating_expression']
    assert r['attribution']['violating_side'] is None
    assert {a['signal_path'] for a in r['next_actions']}=={'tb.index[2:0]'}
    assert all(f.get('signal') is None for f in r['findings'] if f.get('expression_key'))
    with pytest.raises(ValueError,match='1-bit'):
        inspect_handshake(get_parser=lambda _:p,wave_path=p.file_path,clock='tb.clk',
            valid=expr('tb.data'),ready='tb.clk')


@pytest.mark.parametrize('packed',[False,True])
def test_tlul_expression_fields_reuse_before_samples_and_preserve_transactions(tmp_path,packed):
    p,fields = tlul_fixture(tmp_path,packed=packed)
    fields = {name:expr('field',{'field':selection}) for name,selection in fields.items()}
    r = inspect_tlul(get_parser=lambda _:p,wave_path=p.file_path,clock=expr('$signed(tb.clk)'),
        fields=fields,reset=expr('tb.reset'))
    TlulInspectResult.model_validate(r)
    assert r['coverage_status']=='complete' and r['sampling_phase']=='before'
    assert r['accepted_a_count']==r['accepted_d_count']==3
    assert [(t['id'],t['request_time_ps'],t['completion_time_ps']) for t in r['transactions']['transactions']]==[(1,25,35),(2,35,55)]
    assert all(o['phase']=='before' for e in r['expressions'] if e['expr']=='field' for o in e['observations'])
    r = inspect_tlul(get_parser=lambda _:p,wave_path=p.file_path,clock=expr('tb.clk'),
        fields=fields,reset=expr('tb.reset'),_limits=replace(TransactionLimits(),events=2))
    assert r['coverage_status']=='partial' and r['transactions'] is None
    assert 'event_budget' in r['gaps']


def test_transaction_signed_tag_views_correlate_bits(tmp_path):
    p,fields = tlul_fixture(tmp_path,packed=False)
    r = reconstruct_transactions(get_parser=lambda _:p,wave_path=p.file_path,clock='tb.clk',
        req_valid=expr('tb.a_valid'),req_ready=expr('tb.a_ready'),req_id=expr('$signed(tb.a_source)'),
        cmp_valid=expr('tb.d_valid'),cmp_ready=expr('tb.d_ready'),cmp_id=expr('tb.d_source'),
        req_fields=[expr('tb.a_data ^ 8\'hff')],reset='tb.reset')
    TxnReconstructResult.model_validate(r)
    assert r['matched_count']==2 and {t['id'] for t in r['transactions']}=={1,2}
    values = [next(iter(t['req_fields'].values())) for t in r['transactions']]
    assert values==['0xca','0x5a']


def test_derived_clock_keeps_same_time_order_uncertainty(tmp_path):
    from pathlib import Path
    from src.cycle_query import get_signals_by_cycle,sample_signals_on_edges
    from src.expression_observe import ExpressionParser
    p = wave(tmp_path,small_pages=1)
    f = Path(p.file_path)
    f.write_text(f.read_text().replace('#15\n1c','#15\n1c\n0c\n1c'))
    adapter = ExpressionParser(p)
    key = adapter.bind(expr('tb.clk'))
    with pytest.raises(ValueError,match='unresolved'):
        get_signals_by_cycle(adapter,key,['tb.data'])
    r = sample_signals_on_edges(adapter,key,['tb.data'],sample_phase='before',sample_offset_ps=0)
    assert r['transition_data_truncated'] and len(r['samples'])==1
    assert 'clock_event_order_unresolved' in r['sampling_gaps']


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_window_and_protocol_public_schemas(tmp_path):
    tools = {t.name:t for t in await server.list_tools()}
    args = dict(wave_path='a.vcd',clock=expr('tb.clk'),mode='always',predicate=[expr('tb.a || tb.b')])
    Draft202012Validator(tools['verify_window'].inputSchema).validate(args)
    Draft202012Validator(tools['inspect_tlul'].inputSchema).validate(dict(wave_path='a.vcd',clock=expr('tb.clk'),fields={'a_valid':expr('tb.v')}))
    p = wave(tmp_path)
    server.reset_session_state()
    try:
        result = await server._dispatch('verify_window',{**args,'wave_path':p.file_path,'predicate':[expr('tb.data[tb.index]')]})
        WindowVerifyResult.model_validate(result)
        assert result['expressions'] and result['unknown_cycles']==1
    finally:
        server.reset_session_state()
