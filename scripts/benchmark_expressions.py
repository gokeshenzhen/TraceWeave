#!/usr/bin/env python3
"""Expression cost on the deterministic benchmark_waveform_reads VCD.

Generate the input with benchmark_waveform_reads.py --generate. Run this in a
fresh process after the reader baseline, without concurrent benchmark jobs.
Includes correctness checks and receipt collection; excludes MCP transport.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.vcd_parser import VCDParser
from src.expression_observe import ExpressionParser
from src.cycle_query import get_signals_by_cycle
from benchmark_waveform_reads import rss


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--wave',type=Path,required=True)
    cli.add_argument('--steps',type=int,default=10000)
    cli.add_argument('--queries',type=int,default=31)
    args=cli.parse_args()
    if args.steps<1000 or args.queries<1:
        cli.error('steps >= 1000 and queries >= 1 required')
    base=VCDParser(str(args.wave))
    base.get_header()  # Warm parse, stated separately from binding/evaluation.
    spec=dict(expr='data[(data >> 5) & 31] + sparse',typing='wave_bits',bindings={
        'data':'top.data','sparse':'top.sparse'})
    def expected(at):
        step=min(at//10,args.steps)
        data=step*2654435761 & 0xffffffff
        sparse=(args.steps//2)*2654435761 & 0xffffffff if step>=args.steps//2 else 0
        return (sparse+((data>>((data>>5)&31))&1)) & 0xffffffff
    def bind():
        p=ExpressionParser(base)
        return p,p.bind(spec)
    def measure(function,n=args.queries):
        times=[]
        for _ in range(n):
            start=time.perf_counter_ns();function();times.append((time.perf_counter_ns()-start)/1e6)
        return statistics.median(times)
    p,key=bind()
    at=(args.steps//2+1)*10
    def point(p=p,key=key):
        assert p.get_value_at_time(key,at)['value']['dec']==expected(at)
    def full_request():
        adapter,bound=bind();point(adapter,bound)
    def window():
        rows=p.get_transitions(key,100,1000)
        assert not rows['truncated']
        for row in rows['transitions']:
            assert row['value']['dec']==expected(row['time_ps'])
    def cycle():
        result=get_signals_by_cycle(p,'top.clk',[key],num_cycles=1000)
        assert result['num_cycles_returned']==1000
        for row in result['cycles']:
            assert row['signals'][key]['dec']==expected(row['time_ps'])
    result=dict(steps=args.steps,queries=args.queries,cache='parsed VCD; OS cache not flushed',
        waveform_bytes=args.wave.stat().st_size,binding_ms=measure(bind),point_bound_ms=measure(point),
        point_request_ms=measure(full_request),window_100_to_1000_ps_ms=measure(window,11),
        cycles_1000_ms=measure(cycle,3),process_peak_rss_kib=rss('VmHWM:'))
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
