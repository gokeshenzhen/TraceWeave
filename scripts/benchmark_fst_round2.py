#!/usr/bin/env python3
"""Reproducible FST/VCD resource acceptance; no external design or simulation.

Generate equivalent event tables once, then measure each backend in a fresh
process with three requests on the same parser. OS caches are not flushed.
Native phase timings include Python callback work; decompression alone is not
measurable through libfst. All JSON results stay in the chosen local directory.
"""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def fingerprint(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): digest.update(block)
    return dict(path=str(path),bytes=path.stat().st_size,sha256=digest.hexdigest())


def generate(directory,large_ticks):
    from fst_analysis_fixture import cycle_pair, write_pair
    directory.mkdir(parents=True,exist_ok=True)
    catalog={}
    def keep(name,paths,**details):
        catalog[name]=dict(files=[fingerprint(p) for p in paths],**details)
    rows=[(t,n,str((t//step)%2)) for n,step in (('a',1),('b',2)) for t in range(0,100000,step)]
    keep('narrow',write_pair(directory,[(n,1,'wire','input',None) for n in ('a','b')],rows,
        name='narrow',end=100000),start=99900,end=99999,signals=['top.a','top.b'],events=150000,expected=150)
    rows=[(t,'clk',str(t%2)) for t in range(100001)]
    keep('dense_clock',write_pair(directory,[('clk',1,'wire','input',None)],rows,name='dense',end=100000),
         expected_edges=50000,events=len(rows))
    widths={f'p{i}_{field}':w for i in range(12) for field,w in (('valid',1),('ready',1),('data',8))}
    cycles=[{f'p{i}_{field}':v for i in range(12) for field,v in
             (('valid',1),('ready',int(i%6!=0)),('data',7))} for _ in range(800)]
    keep('sweep',cycle_pair(directory,widths,cycles,name='sweep'),interfaces=12,cycles=800,expected_flagged=2)
    rows=[]
    for i in range(3000):
        rows.extend([dict(rv=1,rr=1,rid=i%16),dict(cv=1,cr=1,cid=i%16)])
    widths=dict(rv=1,rr=1,rid=4,cv=1,cr=1,cid=4)
    keep('transactions',cycle_pair(directory,widths,rows,name='txn'),cycles=6000,matched=3000)
    decl=[(f's{i:04d} [7:0]',8,'wire','input',None) for i in range(3000)]
    keep('metadata',write_pair(directory,decl,[(0,n,'00000000') for n,*_ in decl],name='metadata'),declarations=3000)
    catalog['compare']={**catalog['narrow'],'start':0,'end':99999,'signal':'top.a'}

    # Streaming generation keeps generator memory separate from measured reads.
    from pylibfst import lib,ffi
    fst,vcd=directory/'large.fst',directory/'large.vcd'
    writer=lib.fstWriterCreate(str(fst).encode(),1)
    assert writer!=ffi.NULL
    table_hash=hashlib.sha256(); tail_hash=hashlib.sha256(); rng=random.Random(20260928)
    names=[f'd{i} [127:0]' for i in range(8)]
    handles=[]
    try:
        lib.fstWriterSetTimescale(writer,-12)
        lib.fstWriterSetScope(writer,lib.FST_ST_VCD_MODULE,b'top',ffi.NULL)
        for n in names: handles.append(lib.fstWriterCreateVar(writer,lib.FST_VT_VCD_WIRE,lib.FST_VD_INPUT,128,n.encode(),0))
        lib.fstWriterSetUpscope(writer)
        with vcd.open('w') as out:
            out.write('$timescale 1ps $end\n$scope module top $end\n')
            for i,n in enumerate(names): out.write(f'$var wire 128 s{i} {n} $end\n')
            out.write('$upscope $end\n$enddefinitions $end\n')
            for tick in range(large_ticks):
                lib.fstWriterEmitTimeChange(writer,tick); out.write(f'#{tick}\n')
                for i,handle in enumerate(handles):
                    bits=f'{rng.getrandbits(128):0128b}'
                    lib.fstWriterEmitValueChange(writer,handle,bits.encode())
                    out.write(f'b{bits} s{i}\n')
                    table_hash.update(f'{tick}|{i}|{bits}\n'.encode())
                    if tick>=large_ticks-1000 and i<2: tail_hash.update(f'{tick}|top.d{i}|{bits}\n'.encode())
                if tick and tick%50000==0: lib.fstWriterFlushContext(writer)
            lib.fstWriterEmitTimeChange(writer,large_ticks); out.write(f'#{large_ticks}\n')
    finally: lib.fstWriterClose(writer)
    keep('large', (fst,vcd),start=large_ticks-1000,end=large_ticks-1,signals=['top.d0[127:0]','top.d1[127:0]'],
         events=large_ticks*8,expected=2000,table_sha256=table_hash.hexdigest(),tail_sha256=tail_hash.hexdigest(),seed=20260928)
    (directory/'fixtures.json').write_text(json.dumps(catalog,indent=2))
    return catalog


def rss(pid,field='VmRSS'):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith(field+':'):return int(line.split()[1])
    except (FileNotFoundError,ProcessLookupError): pass
    return 0


class Monitor:
    def __init__(self):
        self.stop=threading.Event(); self.lock=threading.Lock(); self.children=set()
        self.parent_peak=self.child_peak=self.aggregate_peak=0
        self.thread=threading.Thread(target=self.run,daemon=True)
    def sample(self):
        parent=rss(os.getpid())
        with self.lock: children=sum(rss(p) for p in self.children)
        self.parent_peak=max(self.parent_peak,parent); self.child_peak=max(self.child_peak,children)
        self.aggregate_peak=max(self.aggregate_peak,parent+children)
    def run(self):
        while not self.stop.wait(.005): self.sample()
    def __enter__(self):self.sample(); self.thread.start();return self
    def __exit__(self,*args):self.stop.set();self.thread.join();self.sample()


def measure(directory,workload,backend,repeats):
    from src import fst_parser,fst_event_pages
    from src.fst_runtime import FstProcess,request_budget
    from src.vcd_parser import VCDParser
    from src.waveform_batch import FSTBatchReader,VCDBatchReader
    from src.verify_condition import period
    from src.handshake_sweep import sweep_handshake_anomalies
    from src.txn_reconstruct import reconstruct_transactions
    from src.divergence_compare import compare_signals
    from src import operation_metrics
    fixture=json.loads((directory/'fixtures.json').read_text())[workload]
    path=fixture['files'][0 if backend=='fst' else 1]['path']
    parser=fst_parser.FSTParser(path) if backend=='fst' else VCDParser(path)
    workers=[]; batches=[]; monitor=Monitor()
    class Measured(FstProcess):
        def __init__(self,*args,**kw):
            started=time.perf_counter(); super().__init__(*args,**kw)
            self.phases=dict(open_and_prepare_ms=(time.perf_counter()-started)*1000,metadata_ipc_ms=0,read_ipc_ms=0)
            self.pid=self.process.pid; self.private=self.directory.name
            workers.append(self)
            with monitor.lock: monitor.children.add(self.pid)
        def ask(self,request):
            started=time.perf_counter(); result=super().ask(request)
            key='read_ipc_ms' if request['op'] in {'stream','next','batch'} else 'metadata_ipc_ms'
            self.phases[key]+=(time.perf_counter()-started)*1000
            if 'batch_metrics' in result: batches.append(result['batch_metrics'])
            return result
    fst_parser.FstProcess=fst_event_pages.FstProcess=Measured
    other = ((VCDParser(fixture['files'][1]['path']) if backend=='fst'
              else fst_parser.FSTParser(fixture['files'][0]['path'])) if workload=='compare' else None)
    def execute():
        if workload in {'narrow','large'}:
            reader=FSTBatchReader(parser) if backend=='fst' else VCDBatchReader(parser)
            result=reader.values_in_window(fixture['signals'],fixture['start'],fixture['end'])
            changes=result['transitions']; assert len(changes)==fixture['expected'],len(changes)
            if workload=='large':
                h=hashlib.sha256()
                for r in sorted(changes,key=lambda r:(r['time_ps'],r['signal'])):
                    v=r['value']; v=v.get('bin') if isinstance(v,dict) else v
                    h.update(f"{r['time_ps']}|{r['signal'].split('[')[0]}|{v.removeprefix('b')}\n".encode())
                assert h.hexdigest()==fixture['tail_sha256']
            else:
                assert all(int(str(r['value']).removeprefix('b'),2)==(r['time_ps']//(1 if r['signal']=='top.a' else 2))%2 for r in changes)
            return result,dict(transitions=len(changes),signals=2)
        if workload=='dense_clock':
            result=period(get_parser=lambda _:parser,wave_path=path,signal='top.clk')
            assert result['period_ps']==2 and result['edges_used']==fixture['expected_edges']
            return result,dict(edges=result['edges_used'])
        if workload=='metadata':
            result=parser.search_signals('s',max_results=100)
            assert result['total_matched']==fixture['declarations']
            return result,dict(declarations=result['total_matched'],returned=len(result['results']))
        if workload=='sweep':
            result=sweep_handshake_anomalies(get_parser=lambda _:parser,wave_path=path,max_wait_cycles=16)
            assert result['interface_count']==12 and result['flagged_count']==2,result
            assert result['coverage_status']=='complete'
            return result,dict(interfaces=12,flagged=2,cycles=800)
        if workload=='transactions':
            result=reconstruct_transactions(get_parser=lambda _:parser,wave_path=path,clock='top.clk',
                req_valid='top.rv',req_ready='top.rr',req_id='top.rid',cmp_valid='top.cv',cmp_ready='top.cr',
                cmp_id='top.cid',max_transactions=1)
            assert result['matched_count']==3000 and result['coverage_status']=='complete'
            assert result['latency']['mean_cycles']==1
            return result,dict(matched=3000,samples=result['analysis']['analyzed_samples'],displayed=len(result['transactions']))
        other_path=other.file_path
        result=compare_signals(get_parser=lambda p:parser if p==path else other,wave_path_a=path,wave_path_b=other_path,
            signal_a='top.a',signal_b='top.a',start_ps=0,end_ps=99999)
        assert result['comparison_status']=='equal',result
        return result,dict(events=result['transitions_compared'],equal=True)
    trials=[]; initial=rss(os.getpid())
    with monitor:
        for i in range(repeats):
            workers.clear(); batches.clear(); metric=operation_metrics.OperationMetrics(); token=operation_metrics.push(metric)
            try:
                started=time.perf_counter()
                with request_budget(): result,facts=execute()
                analysis_ms=(time.perf_counter()-started)*1000
                started=time.perf_counter(); encoded=json.dumps(result,separators=(',',':'),default=str).encode()
                serialize_ms=(time.perf_counter()-started)*1000
                trials.append(dict(request=i+1,analysis_ms=analysis_ms,serialize_ms=serialize_ms,
                    total_ms=analysis_ms+serialize_ms,json_bytes=len(encoded),facts=facts,rss_after_kib=rss(os.getpid()),
                    workers=[dict(**w.phases,**w.metrics,reaped=not Path(f'/proc/{w.pid}').exists(),
                                  private_removed=not Path(w.private).exists()) for w in workers],
                    batches=list(batches),operation_metrics=operation_metrics.snapshot(metric),
                    analysis_resources=result.get('analysis'),reading=result.get('reading')))
            finally: operation_metrics.pop(token)
    sources=[ROOT/'server.py',*sorted((ROOT/'src').glob('fst*.py')),ROOT/'src/waveform_batch.py',
             ROOT/'src/cycle_query.py',ROOT/'src/handshake_sweep.py']
    return dict(workload=workload,backend=backend,python=sys.executable,python_version=sys.version,
        native_library=fingerprint(Path(importlib.util.find_spec('_libfstapi').origin)),
        dependencies={n:importlib.metadata.version(n) for n in ('pylibfst','mcp','anyio','pyslang')},
        source_commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
        sources=[fingerprint(p) for p in sources],fixture=fixture,trials=trials,
        rss_start_kib=initial,rss_end_kib=rss(os.getpid()),parent_vm_hwm_kib=rss(os.getpid(),'VmHWM'),
        sampled_parent_peak_kib=monitor.parent_peak,sampled_children_peak_kib=monitor.child_peak,
        sampled_aggregate_peak_kib=monitor.aggregate_peak,rss_sampling_ms=5,
        conditions='fresh process, repeated same-parser requests, OS cache unflushed; no CPU affinity',
        unavailable=['independent decompression CPU/bytes','index-only allocator RSS (reported delta includes native work)',
                     'projection time except transaction receipt','MCP round trip (separate acceptance script)'])


def lifecycle(directory,repeats):
    from src.fst_runtime import FstProcess,FstError
    from src.fst_parser import FSTParser
    from src.cancellation import OperationCancelled,push_cancel_event,pop_cancel_event
    from src.scope_metadata import ScopeIdentityChanged
    source=directory/'large.fst'; receipts=[]
    workload=json.loads((directory/'fixtures.json').read_text())['large']
    window=dict(start_fs=workload['start']*1000,end_fs=workload['end']*1000)
    for _ in range(repeats):
        cancel=threading.Event(); token=push_cancel_event(cancel); requested=[]
        timer=None
        try:
            try:
                with FstProcess(source) as child:
                    pid,private=child.process.pid,child.directory.name
                    child.ask({'op':'metadata'})
                    def request_cancel():
                        requested.append(time.perf_counter());cancel.set()
                    timer=threading.Timer(.1,request_cancel);timer.start()
                    child.ask(dict(op='batch',paths=['top.d0','top.d1'],**window))
                    raise AssertionError('workload finished before cancellation')
            except OperationCancelled:
                assert requested and not Path(f'/proc/{pid}').exists() and not Path(private).exists()
                receipts.append(dict(cancel_to_reaped_and_removed_ms=(time.perf_counter()-requested[0])*1000,
                    child_reaped=True,private_removed=True,phase='batch request in flight'))
        finally:
            if timer:timer.cancel();timer.join()
            pop_cancel_event(token)
    started=time.perf_counter()
    try:
        with FstProcess(source,timeout_sec=.15) as child:
            pid,private=child.process.pid,child.directory.name
            child.ask(dict(op='batch',paths=['top.d0','top.d1'],**window))
        raise AssertionError('timeout not reached')
    except FstError as exc:
        assert 'fst_timeout' in str(exc),exc
        timeout=dict(total_to_reaped_and_removed_ms=(time.perf_counter()-started)*1000,
                     child_reaped=not Path(f'/proc/{pid}').exists(),private_removed=not Path(private).exists())
    path=directory/'invalidated.fst';path.write_bytes((directory/'narrow.fst').read_bytes())
    parser=FSTParser(path);parser.get_summary()
    with path.open('ab') as f:f.write(b'\0')
    started=time.perf_counter()
    try:
        parser.get_summary();raise AssertionError('changed file was reused')
    except ScopeIdentityChanged:
        invalidation_ms=(time.perf_counter()-started)*1000
    finally: parser.close();path.unlink()
    return dict(cancellation=receipts,timeout=timeout,identity_invalidation_ms=invalidation_ms)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work-dir',type=Path,required=True);ap.add_argument('--generate',action='store_true')
    ap.add_argument('--large-ticks',type=int,default=200000);ap.add_argument('--repeats',type=int,default=3)
    ap.add_argument('--workload');ap.add_argument('--backend',choices=['fst','vcd']);ap.add_argument('--output',type=Path)
    args=ap.parse_args();directory=args.work_dir.resolve()
    if args.generate: report=generate(directory,args.large_ticks)
    elif args.workload=='lifecycle': report=lifecycle(directory,args.repeats)
    elif args.workload: report=measure(directory,args.workload,args.backend,args.repeats)
    else: ap.error('choose --generate or --workload with --backend')
    if args.output: args.output.write_text(json.dumps(report,indent=2,default=str)+'\n')
    else: print(json.dumps(report,indent=2,default=str))


if __name__=='__main__':main()
