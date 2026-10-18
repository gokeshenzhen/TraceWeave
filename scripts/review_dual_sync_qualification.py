"""Pure-file review of the frozen synchronous MCP responses; no product imports."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args();out=Path(a.out).resolve();pre=out/'mcp_preflight'
    def read(path):return json.loads(path.read_text())
    plan=read(pre/'MCP_PLAN.json')['plan'];inv=read(out/'INPUT_INVENTORY.json')['cases'];results={}
    transport=read(pre/'transport_execution.json');finished=read(pre/'stdio_process_finished.json')
    assert transport['status']=='RAW_PLAN_COMPLETE_REQUIRES_INDEPENDENT_ORACLE_REVIEW'
    assert transport['call_count']==len(plan)==36 and not transport['sdk_forced_terminations'] and not transport.get('forced_cleanup')
    assert finished['exit_code']==0 and not finished['live_group_members']
    for item in plan:
        record=read(pre/(item['id']+'.json'));assert record['request']==item and not record['raw'].get('isError')
        blocks=record['raw']['content'];assert len(blocks)==1 and blocks[0]['type']=='text'
        result=json.loads(blocks[0]['text']);assert not result.get('error') and not result.get('error_code')
        results[item['id']]=result
        if 'expected_bin' in item:
            assert result['value']['bin']==item['expected_bin'] and result['signal']==item['args']['signal_path'] and result['time_ps']==item['args']['time_ps']
    for label in ('a','b'):
        c=inv[label.upper()];data='00111100' if label=='a' else '11000011';value='00110011' if label=='a' else '11001100'
        def get(name):return results[label+'_'+name]
        assert get('prepare')['semantic_status']==get('scan')['semantic']['status']=='not_run'
        assert get('prepare')['kdb_status']=='not_checked' and get('scan')['lexical_coverage_status']=='complete'
        assert get('log')['runtime_total_errors']==0, 'Failed simulation requires parse/sweep and group stop'
        assert get('summary')['scale_fs_per_tick']==1000 and get('summary')['simulation_duration_ps']==46000
        assert get('summary')['top_modules']==[c['top']] and not get('search')['truncated']
        declarations={r['path']:r for r in get('search')['results']}
        for leaf in ('data','mask','q0','q1','result'):
            assert declarations[c['top']+'.dut.'+leaf+'[7:0]']['width']==8
        expected={'clk':[(15000,'1'),(20000,'0'),(25000,'1'),(30000,'0'),(35000,'1')],
            'data':[(12000,data)],'mask':[],'q0':[(15000,data)],
            'q1':[(15000,'00000000'),(25000,data)],'result':[(15000,'00001111'),(25000,value)]}
        for leaf,values in expected.items():
            row=get('trans_'+leaf)
            assert not row['truncated'] and row['predecessor']['time_ps']<11000
            assert [(t['time_ps'],t['value']['bin']) for t in row['transitions']]==values, leaf
        for file in [c[k] for k in ('source','compile_log','sim_log','wave')]+c['kdb']['all_files']:
            assert hashlib.sha256(Path(file['path']).read_bytes()).hexdigest()==file['sha256']
    report=dict(status='QUALIFICATION_VERIFIED_STOP_FOR_A_REVIEW',head=transport['head'],calls=36,
        owned=finished,SDK_forced_terminations=[],input_hashes_unchanged=True,
        oracle='handwritten two-register timing and XOR; actual runtime values checked against frozen plan',
        comparison_sample_times_ps=[16000,26000],previous_state_verified=True,bit_order=list(range(7,-1,-1)),
        cost=transport['cost'],native_boundary_started=False)
    with (pre/'execution_report.json').open('x') as f:json.dump(report,f,indent=2)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
