"""Pure-file inventory and frozen MCP plan for the explicit dual-sync fixture."""
import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fact(path):
    stat = path.stat()
    return dict(path=str(path), canonical=str(path.resolve()), sha256=sha(path),
                dev=stat.st_dev, ino=stat.st_ino, size=stat.st_size, mode=stat.st_mode,
                mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)


def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture',required=True);p.add_argument('--out',required=True)
    p.add_argument('--checkout',required=True);p.add_argument('--head',required=True)
    a=p.parse_args();fixture=Path(a.fixture).resolve();out=Path(a.out).resolve();work=Path(a.checkout).resolve()
    cases={};plan=[]
    for label in ('a','b'):
        base=fixture/label;top='p4_s9_'+label+'_tb';kdb=base/'simv.daidir/kdb.elab++'
        source=base/(label+'.sv')
        assert source.read_bytes()==(work/'tests/fixtures/dual_sync_npi'/(label+'.sv')).read_bytes()
        files=[dict(relative=str(f.relative_to(kdb)),**fact(f)) for f in sorted(kdb.rglob('*')) if f.is_file()]
        assert files
        tree=hashlib.sha256(json.dumps([(r['relative'],r['sha256']) for r in files],separators=(',',':')).encode()).hexdigest()
        case=dict(top=top,signal=top+'.dut.result[7:0]',source=fact(source),
            compile_log=fact(base/'compile.log'),sim_log=fact(base/'sim.log'),wave=fact(base/'trace.fsdb'),
            kdb=dict(path=str(kdb),canonical=str(kdb.resolve()),all_files=files,tree_content_sha256=tree),
            tracked_source_commit=a.head,source_matches_tracked=True)
        cases[label.upper()]=case;wave=case['wave']['path'];log=case['compile_log']['path']
        def add(key,tool,args,**extra):
            plan.append(dict(id=label+'_'+key,tool=tool,args=args,**extra))
        add('paths','get_sim_paths',dict(verif_root=str(base),compile_log=log,sim_log=case['sim_log']['path'],wave_file=wave))
        add('prepare','prepare_design_context',dict(compile_log=log,simulator='vcs',top_hint=top),parallel_group=label+'_pair')
        add('scan','scan_structural_risks',dict(compile_log=log,simulator='vcs',analysis_mode='fast'),parallel_group=label+'_pair')
        add('log','parse_sim_log',dict(log_path=case['sim_log']['path'],simulator='vcs'))
        add('summary','get_waveform_summary',dict(wave_path=wave))
        add('search','search_signals',dict(wave_path=wave,keyword=top,max_results=64))
        for leaf in ('clk','data','mask','q0','q1','result'):
            signal=top+('.clk' if leaf=='clk' else '.dut.'+leaf+'[7:0]')
            add('trans_'+leaf,'get_signal_transitions',dict(wave_path=wave,signal_path=signal,
                start_time_ps=11000,end_time_ps=36000,max_transitions=32))
        data='00111100' if label=='a' else '11000011'
        for key,leaf,time,value in [('q0_pre','q0',14999,'00000000'),('q0_post','q0',15001,data),
                ('q1_pre','q1',24999,'00000000'),('q1_post','q1',25001,data),
                ('equal','result',16000,'00001111'),('different','result',26000,'00110011' if label=='a' else '11001100')]:
            add(key,'get_signal_at_time',dict(wave_path=wave,signal_path=top+'.dut.'+leaf+'[7:0]',time_ps=time),expected_bin=value)
    out.mkdir(parents=True,exist_ok=True);(out/'mcp_preflight').mkdir(exist_ok=False)
    with (out/'INPUT_INVENTORY.json').open('x') as f:json.dump(dict(method='new isolated real fixture',cases=cases),f,indent=2)
    with (out/'mcp_preflight/MCP_PLAN.json').open('x') as f:
        json.dump(dict(approval='A frozen S9 temporal oracle; one group, all prerequisites charge same attempt',plan=plan),f,indent=2)


if __name__=='__main__':main()
