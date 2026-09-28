"""Round-two protocol oracles shared only at the fixture event table."""
from dataclasses import replace
from contextlib import contextmanager

import pytest

pytest.importorskip('pylibfst', reason='install the optional [fst] extra')
from fst_analysis_fixture import cycle_pair
from src.evidence_output import expand_compact_result
from src.fst_parser import FSTParser
from src.transaction_sampling import TransactionLimits
from src.txn_reconstruct import reconstruct_transactions
from src.vcd_parser import VCDParser
from test_fst_integration import call, clean_state
from test_transaction_sampling import WIDTHS, request, response


def checked(tool, path, **args):
    result = call(tool, wave_path=str(path), **args)
    assert 'error' not in result, result
    return result


def facts(value):
    if isinstance(value,dict):
        return {k:facts(v) for k,v in value.items() if k not in {'sample_ms','walk_ms','projection_ms'}}
    if isinstance(value,list):
        return [facts(v) for v in value]
    return value


TXN = dict(clock='top.clk', req_valid='top.rv', req_ready='top.rr', req_id='top.rid',
           cmp_valid='top.cv', cmp_ready='top.cr', cmp_id='top.cid')


def test_id_last_length_tail_and_display_caps_through_mcp(tmp_path):
    paths = cycle_pair(tmp_path, WIDTHS, [request(length=1), response(last=0), response(last=1),
        request(1,length=3), response(1,last=1), request(2)])
    for path in paths:
        for cap in (1, 64):
            r = checked('reconstruct_transactions', path, **TXN, req_len='top.length',
                        cmp_last='top.last', max_transactions=cap, timeout_cycles=1)
            assert (r['matched_count'],r['beat_count_mismatch_count'],r['slow_count']) == (2,1,1)
            assert r['unmatched_requests'] == [dict(id=2,request_time_ps=55)]
            assert r['analysis']['analyzed_samples'] == 6 and r['coverage_status'] == 'complete'
            assert len(r['transactions']) == min(cap,2)
        compact = checked('reconstruct_transactions', path, **TXN, req_len='top.length',
                          cmp_last='top.last', max_transactions=64, timeout_cycles=1, output_format='compact')
        assert facts(expand_compact_result(compact)) == facts(r)


def test_fifo_carry_in_reset_and_data_before_request(tmp_path):
    rows = [dict(dv=1,dr=1,data=17),dict(dv=1,dr=1,dl=1,data=34),request(3,length=1),
            response(3),request(4),dict(rst=0),response(4),request(5,length=1),
            dict(dv=1,dr=1,data=51),response(5),dict(dv=1,dr=1,dl=1,data=68)]
    for path in cycle_pair(tmp_path,WIDTHS,rows):
        r = checked('reconstruct_transactions',path,**TXN,req_len='top.length',data_valid='top.dv',
                    data_ready='top.dr',data_last='top.dl',data_fields=['top.data'],reset='top.rst',capture_beats=True)
        assert (r['matched_count'],r['reset_clears'],r['unmatched_completion_count'],r['orphan_data_beats']) == (2,1,1,1)
        a,b = r['transactions']
        assert a['data_complete'] and a['beat_count'] == 2
        assert [x['fields']['top.data'] for x in a['data_beats']] == ['0x11','0x22']
        assert not b['data_complete'] and b['beat_count_mismatch']
    for path in cycle_pair(tmp_path,WIDTHS,[request(7),{},response(7),request(8)],name='carry'):
        r = checked('reconstruct_transactions',path,**TXN,start_time_ps=10)
        assert (r['matched_count'],r['unmatched_completion_count'],r['outstanding_at_end']) == (0,1,1)
    args = {k:v for k,v in TXN.items() if k not in {'req_id','cmp_id'}}
    for path in cycle_pair(tmp_path,WIDTHS,[request(),request()]+[{}]*128+[response(),response()],name='fifo'):
        r = checked('reconstruct_transactions',path,**args,max_transactions=1)
        assert r['matched_count'] == 2 and r['latency']['mean_cycles'] == 130
        assert r['analysis']['analyzed_samples'] == 132


@pytest.mark.parametrize('limit,reason', [(dict(events=5),'event_budget'),
    (dict(decoded_bytes=512),'decoded_byte_budget'),(dict(timeout_sec=0),'timeout'),
    (dict(samples=3),'sample_budget'),(dict(pending=3),'pending_budget')])
def test_transaction_budgets_keep_partial_coverage(tmp_path,limit,reason):
    fst,_ = cycle_pair(tmp_path,WIDTHS,[request(i) for i in range(8)]+[response(i) for i in range(8)])
    p = FSTParser(str(fst))
    r = reconstruct_transactions(get_parser=lambda _:p,wave_path=str(fst),**TXN,
        _limits=replace(TransactionLimits(),**limit))
    assert r['coverage_status'] == 'partial' and r['analysis']['stop_reason'] == reason
    assert r['matched_count'] < 8


def test_handshake_window_period_and_discovery_public_oracles(tmp_path):
    widths = dict(valid=1,ready=1,data=8)
    rows = [dict(valid=1,data=1),dict(valid=1,data=2),{},dict(valid=1,ready=1,data=3)]
    for path in cycle_pair(tmp_path,widths,rows):
        args = dict(clock='top.clk',valid='top.valid',ready='top.ready',payload=['top.data'])
        r = checked('inspect_handshake',path,**args)
        assert (r['transfer_count'],r['stall_count'],r['payload_hold_violations'],r['valid_deassert_violations']) == (1,2,1,1)
        c = checked('inspect_handshake',path,**args,output_format='compact')
        assert expand_compact_result(c) == r
        r = checked('verify_window',path,clock='top.clk',mode='implication',overlap=False,within_cycles=1,
            antecedent=[dict(signal='top.valid',op='eq',value=1),dict(signal='top.ready',op='eq',value=0)],
            consequent=[dict(signal='top.valid',op='eq',value=1)])
        assert not r['holds'] and not r['vacuous'] and r['violation_count'] == 1
        r = checked('verify_window',path,clock='top.clk',mode='always',predicate=[
            dict(expr='top.data <= 8\'d3',typing='wave_bits')])
        assert r['holds'] and r['cycles_evaluated'] == 4
        r = checked('period',path,signal='top.clk')
        assert r['period_ps'] == 10 and r['edges_used'] == 4 and r['end_ps'] == 40
        found = checked('suggest_handshakes',path)
        assert len(found['candidates']) == 1
        sweep = checked('sweep_handshakes',path)
        assert sweep['coverage_status'] == 'complete' and sweep['flagged_count'] == 1
        empty = checked('sweep_handshakes',path,scope='top.absent')
        assert empty['coverage_status'] == 'zero_coverage'


def test_dump_gap_and_unknown_clock_do_not_report_clean_protocols(tmp_path):
    fst,_ = cycle_pair(tmp_path,dict(valid=1,ready=1),[dict(valid=1,ready=1)]*5,activity=[(12,0),(30,1)])
    r = checked('inspect_handshake',fst,clock='top.clk',valid='top.valid',ready='top.ready')
    assert r['transition_data_truncated'] and r['sample_count'] == 1
    assert 'dump_inactive' in r['coverage']['sampling_gaps']
    sweep = checked('sweep_handshakes',fst)
    assert sweep['coverage_status'] != 'complete'
    p = checked('period',fst,signal='top.clk')
    assert p.get('period_ps') is None and 'incomplete' in p['reason']
    r = checked('verify_window',fst,clock='top.clk',mode='always',predicate=[dict(signal='top.valid',op='eq',value=1)])
    assert r['coverage_status'] == 'partial'


def test_period_keeps_sub_ps_intervals_and_large_integer_median(tmp_path):
    from src.verify_condition import _median
    fst,_ = cycle_pair(tmp_path,dict(data=8),[dict(data=i) for i in range(6)],scale=-14)
    r = checked('period',fst,signal='top.clk')
    assert r['period_fs'] == 100 and r['period_ps'] == 1 and r['edges_used'] == 6
    assert _median([2**53+1,2**53+1]) == 2**53+1


def test_tlul_scalar_packed_and_expression_fields(tmp_path):
    widths = dict(a_valid=1,a_ready=1,a_opcode=3,a_size=2,a_source=2,a_data=8,
                  d_valid=1,d_ready=1,d_opcode=3,d_size=2,d_source=2,d_data=8,d_error=1)
    rows = [dict(reset=0),dict(a_valid=1,a_source=1,a_data=0x35),
        dict(a_valid=1,a_ready=1,a_source=1,a_data=0x35),
        dict(a_valid=1,a_ready=1,a_source=2,a_data=0xa5,d_valid=1,d_ready=1,d_source=1,d_data=0x3c),
        dict(d_valid=1,d_source=2,d_data=0x59),dict(d_valid=1,d_ready=1,d_source=2,d_data=0x59),
        dict(d_valid=1,d_ready=1,d_source=3),dict(a_valid=1,a_ready=1,a_source=1,a_data=0x42),{}]
    for packed in (False,True):
        if packed:
            fields,pos = {},0
            for n,w in widths.items():
                fields[n] = dict(path='top.bundle',lsb=pos,width=w)
                pos += w
            waves = [dict(reset=r.get('reset',1),bundle=sum(r.get(n,0)<<fields[n]['lsb'] for n in widths)) for r in rows]
            decl = dict(bundle=pos,reset=1)
        else:
            fields = {n:dict(expr='top.'+n,typing='wave_bits') for n in widths}
            waves,decl = rows,dict(widths,reset=1)
        for path in cycle_pair(tmp_path,decl,waves,name='tlul'+str(packed)):
            args = dict(clock='top.clk',reset='top.reset',fields=fields)
            r = checked('inspect_tlul',path,**args)
            assert r['coverage_status'] == 'complete' and r['reset_cycles'] == 1
            assert r['accepted_a_count'] == r['accepted_d_count'] == 3
            assert r['channels']['a']['stall_count'] == r['channels']['d']['stall_count'] == 1
            t = r['transactions']
            assert [(x['id'],x['request_time_ps'],x['completion_time_ps'],x['latency_cycles'])
                for x in t['transactions']] == [(1,25,35,1),(2,35,55,2)]
            assert t['outstanding_at_end'] == t['unmatched_completion_count'] == 1
            c = checked('inspect_tlul',path,**args,output_format='compact')
            assert facts(expand_compact_result(c)) == facts(r)


def test_ahb_apb_discovery_does_not_guess_direction(tmp_path):
    fst,_ = cycle_pair(tmp_path,dict(htrans=2,hready=1,haddr=8,hwrite=1,hwdata=8,
        psel=1,penable=1,pready=1,paddr=8),[dict(htrans=2,hready=1)]*3)
    r = checked('suggest_protocol_bundles',fst,protocol='ahb')
    assert len(r['candidates']) == 1
    ahb = r['candidates'][0]
    assert 'write_data' not in ahb.get('inspect_args',{})
    assert len(checked('suggest_protocol_bundles',fst,protocol='apb')['candidates']) == 1
    r = checked('inspect_handshake',fst,clock='top.clk',ready='top.hready',valid_htrans='top.htrans',payload=['top.haddr'])
    assert r['transfer_count'] == 3 and r['payload_hold_violations'] == 0


def test_sweep_reuses_bounded_native_packs_and_cleans_cancel(tmp_path,monkeypatch):
    from src.handshake_sweep import sweep_handshake_anomalies
    from src.cancellation import OperationCancelled
    widths={f'p{i}_{field}':1 for i in range(12) for field in ('valid','ready')}
    rows=[{name:int(not name.endswith('ready') or not name.startswith('p0_')) for name in widths}]*20
    fst,_=cycle_pair(tmp_path,widths,rows)
    parser=FSTParser(fst); original=parser._event_batch; packs=[]; directories=[]
    @contextmanager
    def measured(paths,*args,**kw):
        with original(paths,*args,**kw) as batch:
            packs.append(paths);directories.append(batch.directory.name)
            yield batch
    parser._event_batch=measured
    r=sweep_handshake_anomalies(get_parser=lambda _:parser,wave_path=str(fst))
    assert r['interface_count']==12 and r['flagged_count']==1 and r['coverage_status']=='complete'
    assert len(packs)==2 and all(len(p)<=16 for p in packs)
    from pathlib import Path
    assert all(not Path(p).exists() for p in directories)
    assert parser._active_batch is None
    import src.handshake_sweep as sweep
    def cancel(**kwargs):raise OperationCancelled()
    monkeypatch.setattr(sweep,'inspect_handshake',cancel)
    with pytest.raises(OperationCancelled):
        sweep_handshake_anomalies(get_parser=lambda _:parser,wave_path=str(fst))
    assert parser._active_batch is None and all(not Path(p).exists() for p in directories)
