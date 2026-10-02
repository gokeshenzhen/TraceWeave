"""Fresh-process cold/warm sampling measurements (no OS cache flushing)."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from src.cycle_query import get_signals_by_cycle
from src.event_pages import page_records
from src.fsdb_parser import FSDBParser
from src.fst_parser import FSTParser


def memory():
    return {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/self/status').read_text().splitlines()
            if line.startswith(('VmRSS:', 'VmHWM:'))}


def main(args):
    cases = json.loads((Path(__file__).resolve().parents[1] / 'cases.json').read_text())
    a = cases['artifacts'][args.project]
    scope = cases['scope_aliases']['uart_tx' if args.project == 'ot' else 'dma']
    parser = (FSDBParser if args.project == 'ot' else FSTParser)(a['wave_path'])
    original = parser._event_pages
    metrics = {}

    @contextmanager
    def pages(path, start, end, **kw):
        metrics.setdefault('reads', []).append(dict(path=path, start=start, end=end))
        with original(path, start, end, **kw) as reader:
            class Counted:
                def __getattr__(self, key):
                    return getattr(reader, key)
                def read_page(self):
                    page = reader.read_page()
                    events = page_records(page)
                    metrics['pages'] = metrics.get('pages', 0) + 1
                    metrics['events'] = metrics.get('events', 0) + len(events)
                    metrics['estimated_decoded_bytes'] = metrics.get('estimated_decoded_bytes', 0) + sum(512 + 4*len(e.value or '') for e in events)
                    return page
            yield Counted()

    parser._event_pages = pages
    report = dict(project=args.project, origin=args.origin, phase=args.phase, runs=[],
        source_sha256=hashlib.sha256((ROOT/'src/cycle_query.py').read_bytes()).hexdigest(),
        conditions='fresh process; first/warm parser; OS cache uncontrolled; RSS is process high-water KiB')
    try:
        for temperature in ('first', 'warm'):
            metrics.clear()
            memory_start = memory()
            start = time.perf_counter()
            cpu = time.process_time()
            result = None
            error = None
            try:
                kw = {} if args.origin == 'global' else dict(cycle_index_origin=args.origin)
                result = get_signals_by_cycle(parser, scope + ('.clk_i' if args.project == 'ot' else '.clk_cg'),
                    [scope + '.' + s for s in (('tx_q','tx_d','tick_baud_q') if args.project == 'ot'
                                              else ('dma_state_q[31:0]','dma_state_d[31:0]','dma_done'))],
                    start_time_ps=7074050 if args.project == 'ot' else 944095000,
                    num_cycles=5 if args.project == 'ot' else 9, sample_phase=args.phase, **kw)
            except Exception as exc:
                error = str(exc)
            report['runs'].append(dict(temperature=temperature, elapsed=time.perf_counter()-start,
                cpu_seconds=time.process_time()-cpu, peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                memory_start_kib=memory_start, memory_end_kib=memory(),
                reading=dict(metrics), result=result, error=error,
                result_bytes=len(json.dumps(result).encode()) if result else 0))
    finally:
        parser.close()
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    for r in report['runs']:
        print(args.project, args.origin, args.phase, r['temperature'], round(r['elapsed'], 3),
              r['peak_rss_kib'], r['reading'].get('events'), r['error'], flush=True)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--project', choices=('ot','x1'), required=True)
    cli.add_argument('--origin', choices=('global','window'), required=True)
    cli.add_argument('--phase', choices=('before','after'), required=True)
    cli.add_argument('--output', required=True)
    main(cli.parse_args())
