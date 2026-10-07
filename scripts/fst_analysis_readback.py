"""Fresh-MCP analysis matrix over independent synthetic event tables."""
import asyncio
from pathlib import Path

from fst_analysis_fixture import cycle_pair, write_pair


async def validate_analyses(call, work, report, fingerprint):
    matrix=[]
    async def checked(tool,args,mode='direct'):
        result=await call(tool,args)
        assert 'error' not in result,(tool,result)
        matrix.append(dict(tool=tool,input_mode=mode))
        return result
    def record(paths):report['fixtures'].extend(fingerprint(Path(p)) for p in paths)
    fst,vcd=cycle_pair(work,dict(valid=1,ready=1,data=8),[
        dict(valid=1,data=1),dict(valid=1,data=2),{},dict(valid=1,ready=1,data=3)],name='protocol')
    record((fst,vcd))
    for path in (fst,vcd):
        common=dict(wave_path=str(path))
        r=await checked('suggest_handshakes',common)
        assert r['candidate_count']==1
        args=dict(**common,clock='top.clk',valid='top.valid',ready='top.ready',payload=['top.data'])
        for compact in (False,True):
            r=await checked('inspect_handshake',{**args,**({'output_format':'compact'} if compact else {})},
                            'compact' if compact else 'direct')
            assert (r['transfer_count'],r['stall_count'],r['payload_hold_violations'],r['valid_deassert_violations'])==(1,2,1,1)
        r=await checked('sweep_handshakes',common)
        assert r['coverage_status']=='complete' and r['flagged_count']==1
        r=await checked('sweep_handshakes',{**common,'scope':'top.absent'},'zero_coverage')
        assert r['coverage_status']=='zero_coverage'
        r=await checked('period',{**common,'signal':'top.clk'})
        assert r['period_ps']==10 and r['edges_used']==4
        r=await checked('verify_window',{**common,'clock':'top.clk','mode':'implication','overlap':False,'within_cycles':1,
            'antecedent':[dict(signal='top.valid',op='eq',value=1),dict(signal='top.ready',op='eq',value=0)],
            'consequent':[dict(signal='top.valid',op='eq',value=1)]},'nested predicates')
        assert not r['holds'] and not r['vacuous'] and r['violation_count']==1
        r=await checked('verify_window',{**common,'clock':'top.clk','mode':'always',
            'predicate':[dict(expr="top.data <= 8'd3",typing='wave_bits')]},'nested expression')
        assert r['holds'] and r['cycles_evaluated']==4
    for a,b in ((fst,fst),(fst,vcd),(vcd,fst)):
        r=await checked('diff_first_divergence',dict(wave_path_a=str(a),wave_path_b=str(b),
            signal_a='top.data',signal_b=dict(expr="top.data + 8'd1",typing='wave_bits'),
            start_time_ps=1,end_time_ps=30),'cross-format expression')
        assert r['first_divergence_time_ps']==1 and r['earliest_difference_proven']

    widths=dict(rv=1,rr=1,rid=4,cv=1,cr=1,cid=4,last=1,length=4,rst=1,dv=1,dr=1,dl=1,data=8)
    def req(i=0,**kw):return dict(rv=1,rr=1,rid=i,**kw)
    def cmp(i=0,**kw):return dict(cv=1,cr=1,cid=i,**kw)
    txn=dict(clock='top.clk',req_valid='top.rv',req_ready='top.rr',req_id='top.rid',
        cmp_valid='top.cv',cmp_ready='top.cr',cmp_id='top.cid')
    paths=cycle_pair(work,widths,[req(length=1),cmp(last=0),cmp(last=1),req(1,length=3),cmp(1,last=1),req(2)],name='txn')
    record(paths)
    for path in paths:
        for cap in (1,64):
            r=await checked('reconstruct_transactions',dict(wave_path=str(path),**txn,req_len='top.length',
                cmp_last='top.last',max_transactions=cap,output_format='compact'),'ID/LAST/display cap')
            assert r['matched_count']==2 and r['beat_count_mismatch_count']==1
            assert r['unmatched_requests']==[dict(id=2,request_time_ps=55)]
            assert r['coverage_status']=='complete' and r['analysis']['analyzed_samples']==6
    paths=cycle_pair(work,widths,[dict(dv=1,dr=1,data=17),dict(dv=1,dr=1,dl=1,data=34),req(3,length=1),
        cmp(3),req(4),dict(rst=0),cmp(4)],name='early_data');record(paths)
    for path in paths:
        r=await checked('reconstruct_transactions',dict(wave_path=str(path),**txn,req_len='top.length',
            data_valid='top.dv',data_ready='top.dr',data_last='top.dl',data_fields=['top.data'],reset='top.rst',capture_beats=True),
            'W-before-request/reset')
        assert r['matched_count']==1 and r['reset_clears']==1 and r['unmatched_completion_count']==1
        assert r['transactions'][0]['data_complete'] and r['transactions'][0]['beat_count']==2

    names=dict(a_valid=1,a_ready=1,a_opcode=3,a_size=2,a_source=2,a_data=8,
        d_valid=1,d_ready=1,d_opcode=3,d_size=2,d_source=2,d_data=8,d_error=1)
    rows=[dict(reset=0),dict(a_valid=1,a_source=1,a_data=0x35),
        dict(a_valid=1,a_ready=1,a_source=1,a_data=0x35),
        dict(a_valid=1,a_ready=1,a_source=2,a_data=0xa5,d_valid=1,d_ready=1,d_source=1,d_data=0x3c),
        dict(d_valid=1,d_source=2,d_data=0x59),dict(d_valid=1,d_ready=1,d_source=2,d_data=0x59),
        dict(d_valid=1,d_ready=1,d_source=3),dict(a_valid=1,a_ready=1,a_source=1,a_data=0x42),{}]
    fields={};position=0
    for name,width in names.items():
        fields[name]=dict(path='top.bundle',lsb=position,width=width);position+=width
    packed=[dict(reset=r.get('reset',1),bundle=sum(r.get(n,0)<<fields[n]['lsb'] for n in names)) for r in rows]
    paths=cycle_pair(work,dict(bundle=position,reset=1),packed,name='tlul');record(paths)
    for path in paths:
        r=await checked('inspect_tlul',dict(wave_path=str(path),clock='top.clk',reset='top.reset',
            fields=fields,output_format='compact'),'nested packed fields / compact')
        assert r['accepted_a_count']==r['accepted_d_count']==3 and r['reset_cycles']==1
        assert [(t['id'],t['request_time_ps'],t['completion_time_ps']) for t in r['transactions']['transactions']]==[(1,25,35),(2,35,55)]
        assert r['transactions']['outstanding_at_end']==r['transactions']['unmatched_completion_count']==1
    paths=cycle_pair(work,dict(htrans=2,hready=1,haddr=8,psel=1,penable=1,pready=1),
        [dict(htrans=2,hready=1)]*3,name='buses');record(paths)
    for protocol in ('ahb','apb'):
        r=await checked('suggest_protocol_bundles',dict(wave_path=str(paths[0]),protocol=protocol))
        assert r['candidate_count']==1
    r=await checked('inspect_handshake',dict(wave_path=str(paths[0]),clock='top.clk',valid_htrans='top.htrans',ready='top.hready'))
    assert r['transfer_count']==3

    # One real compile context for structural/packed/dynamic paths. The log
    # and waveform are synthetic owned fixtures, with independent event facts.
    source=work/'analysis.sv'
    source.write_text('''module top(input logic clk,rst,en,d, output logic q, output wire y);
always_ff @(posedge clk) if(rst) q<=1'b0; else if(en) q<=d;
assign y=q;
typedef struct packed {logic valid; logic [3:0] data; logic ready;} packet_t;
packet_t bus;
endmodule
''')
    compile_log=work/'compile.log'
    compile_log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n       top\n")
    ctx=dict(compile_log=str(compile_log),simulator='vcs')
    names=[('clk',1),('rst',1),('en',1),('d',1),('q',1),('y',1),('bus [5:0]',6)]
    rows=[(0,n,f'{v:0{w}b}') for (n,w),v in zip(names,[0,0,1,0,0,0,43])]
    rows += [(t,'clk',str(t//5%2)) for t in range(5,41,5)]
    rows += [(12,'d','x'),(15,'q','x'),(15,'y','x'),(18,'d','1'),(20,'en','0')]
    fst,vcd=write_pair(work,[(n,w,'wire','input',None) for n,w in names],rows,name='history');record((fst,vcd))
    log=work/'sim.log';log.write_text('UVM_ERROR analysis.sv(2) @ 15 ps: top [MISMATCH] q expected 0 got X\n')
    await checked('get_sim_paths',dict(verif_root=str(work),wave_file=str(fst)))
    await asyncio.gather(*(checked(tool,ctx,'parallel compile context') for tool in ('build_tb_hierarchy','scan_structural_risks')))
    parsed=await checked('parse_sim_log',dict(log_path=str(log),simulator='vcs'))
    assert parsed['runtime_total_errors']==1
    sweep=await checked('sweep_handshakes',dict(wave_path=str(fst)))
    assert sweep['coverage_status']=='zero_coverage'
    common=dict(**ctx,wave_path=str(fst),log_path=str(log),top_hint='top')
    r=await checked('analyze_failures',dict(**common,signal_paths=['top.q'],window_ps=5))
    assert r['wave_context']['signals']['top.q']['value_at_center']['bin']=='x'
    await checked('analyze_failure_event',dict(**common,failure_event=r['focused_event']))
    r=await checked('recommend_failure_debug_next_steps',common)
    assert r['runtime_protocol_coverage']['coverage_status']=='zero_coverage'
    r=await checked('explain_signal_driver',dict(**ctx,wave_path=str(fst),signal_path='top.q',time_ps=36))
    assert r['backend_status']['actual_backend']=='source_graph'
    r=await checked('find_signal_loads',dict(**ctx,wave_path=str(fst),signal_path='top.q'))
    assert r['backend_status']['actual_backend']=='source_graph'
    assert [(load['load_path'],load['kind']) for load in r['loads']]==[('top.y','rhs_expr')]
    assert r['loads'][0]['source_info_origin']=='source_graph'
    r=await checked('trace_signal_path',dict(**ctx,wave_path=str(fst),
        from_signal='top.q',to_signal='top.y',expand_assigns=True))
    assert r['backend_status']['actual_backend']=='source_graph' and r['found']
    assert r['hops']==1 and [hop['net_path'] for hop in r['path']]==['top.q[0]','top.y[0]'],r['path']
    assert r['path'][-1]['edge_kind']=='continuous_assign'
    r=await checked('resolve_packed_fields',dict(**ctx,wave_path=str(fst),source_signal='top.bus',
        signal_path='top.bus[5:0]',fields=['valid','data','ready']))
    assert r['status']=='resolved' and r['fields']['data']['bits']==[4,3,2,1]
    for path in (fst,vcd):
        for mode in ('snapshot','history'):
            r=await checked('trace_x_source',dict(**ctx,wave_path=str(path),signal_path='top.q',time_ps=36,
                mode=mode,**({'history_start_ps':0} if mode=='history' else {})),mode)
            if mode=='snapshot':assert r['propagation_chain'][0]['signal_path']=='top.q'
            else:
                h=r['history']
                assert [(n['signal'],n['time_ps']) for n in h['nodes']]==[('top.q',36),('top.q',35),('top.q',25),('top.d',15)]
                assert h['nodes'][-1]['interval']['active_interval_start_time_fs']==12000
                assert not any(n['interval']['true_origin_proven'] for n in h['nodes'])
    changed=[(t,n,('1' if n in {'q','y'} and t==0 else v)) for t,n,v in rows]
    different,_=write_pair(work,[(n,w,'wire','input',None) for n,w in names],changed,name='different');record((different,))
    for a,b in ((fst,different),(vcd,different),(different,vcd)):
        r=await checked('trace_divergence',dict(side_a={**ctx,'wave_path':str(a),'signal_path':'top.y'},
            side_b={**ctx,'wave_path':str(b),'signal_path':'top.y'},end_time_ps=10),'nested side_a/side_b')
        assert r['comparison']['first_divergence_time_ps']==0
        assert r['contexts']['a']['backend_status']['actual_backend']=='source_graph'
    report['analysis_matrix']=matrix
    report['compile_hierarchy_scan_log_sweep']='passed: synthetic owned compile/log/wave context, parallel hierarchy/scan; zero protocol coverage retained'
