"""Check real local cycle rows against retained helper and independent point reads."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from src.cycle_query import sample_signals_on_edges
from src.fsdb_parser import FSDBParser
from src.fst_parser import FSTParser


def main():
    out=Path(__file__).parent/'p0-sampling'
    checked=[]
    for project,cls in (('ot',FSDBParser),('x1',FSTParser)):
        calls=[json.loads(s) for s in (out/f'verified-{project}'/'calls.jsonl').read_text().splitlines()]
        requests=[r for r in calls if r['tool']=='get_signals_by_cycle']
        parser=cls(requests[0]['arguments']['wave_path'])
        try:
            for request in requests:
                a,r=request['arguments'],request['result']
                assert r['local_coverage']['status']=='complete',r
                assert r['num_cycles_returned']==(5 if project=='ot' else 7 if 'end_time_ps' in a else 9)
                rows=r['cycles'];phase=a['sample_phase']
                helper=sample_signals_on_edges(parser,a['clock_path'],a['signal_paths'],
                    start_ps=rows[0]['time_ps'],end_ps=rows[-1]['time_ps'],sample_phase=phase,
                    sample_offset_ps=0 if phase=='before' else 1)
                assert [s['signals'] for s in helper['samples']]==[s['signals'] for s in rows]
                for row in rows:
                    # Both retained real traces have 1 ps precision; synthetic
                    # regressions separately cover sub-ps strict-before semantics.
                    at=row['time_ps']+(-1 if phase=='before' else 1)
                    for path,value in row['signals'].items():
                        point=parser.get_value_at_time(path,at)['value']
                        assert value['dec']==point['dec'],(path,at,value,point)
                checked.append(dict(project=project,phase=phase,start=a['start_time_ps'],
                    rows=len(rows),point_comparisons=sum(len(row['signals']) for row in rows),
                    helper_equal=True,points_equal=True))
                if project=='x1' and 'end_time_ps' in a:
                    row=next(row for row in rows if row['time_ps']==944515000)
                    done=next(v['dec'] for p,v in row['signals'].items() if p.endswith('.dma_done'))
                    state=next(v['dec'] for p,v in row['signals'].items() if p.endswith('.dma_state_q[31:0]'))
                    assert (state,done)==((2,1) if phase=='before' else (0,0))
                print(checked[-1],flush=True)
        finally:parser.close()
    (out/'real-row-checks.json').write_text(json.dumps(checked,indent=2)+'\n')


if __name__=='__main__':main()
