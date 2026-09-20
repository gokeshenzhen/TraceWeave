#!/usr/bin/env python3
"""Read-only single-pair benchmark; compare checkouts with identical inputs.

First query includes parser open/index/parse. Warm queries reuse that parser,
never comparison results. OS caches are not flushed. ABI bytes are uncompressed
returned records, not physical FSDB I/O; /proc IO is reported separately.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import sys
import threading
import time


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def io_counts():
    return {k: int(v) for k, v in (line.split(':') for line in Path('/proc/self/io').read_text().splitlines())}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument('--wave')
    cli.add_argument('--generate-dir', type=Path)
    cli.add_argument('--steps', type=int, default=160000)
    cli.add_argument('--other-wave')
    cli.add_argument('--signal-a', default='top.a')
    cli.add_argument('--signal-b', default='top.b')
    cli.add_argument('--start', type=int, default=0)
    cli.add_argument('--end', type=int, default=-1)
    cli.add_argument('--wrapper')
    cli.add_argument('--output', type=Path)
    args = cli.parse_args()
    if args.generate_dir:
        from divergence_benchmark_workloads import write_read_workloads
        write_read_workloads(args.generate_dir, args.steps)
        return
    if not args.wave or not args.output:
        cli.error('measurement requires --wave and --output')
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from src.divergence_compare import compare_signals
    from src import fsdb_parser
    from src.vcd_parser import VCDParser
    from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
    if args.wrapper:
        fsdb_parser._WRAPPER_SO = args.wrapper
    waves = list(dict.fromkeys([args.wave, args.other_wave or args.wave]))
    parsers = {p: fsdb_parser.FSDBParser(p) if p.endswith('.fsdb') else VCDParser(p) for p in waves}
    counts = {}
    def count_read(fn):
        def read(*a, **kw):
            result = fn(*a, **kw)
            rows = result.get('transitions', ())
            counts['legacy_reads'] = counts.get('legacy_reads', 0) + 1
            counts['legacy_events'] = counts.get('legacy_events', 0) + len(rows)
            return result
        return read
    # Probe lazy library loading separately from measurement without opening a
    # waveform. This does not build a signal index or prefetch query results.
    lib = fsdb_parser._load_wrapper() if any(p.endswith('.fsdb') for p in waves) else None
    if lib:
        for name in ('fsdb_get_transitions', 'fsdb_get_transitions_profiled', 'fsdb_get_loaded_transitions'):
            if not hasattr(lib, name):
                continue
            fn = getattr(lib, name)
            def native(*a, _fn=fn):
                result = _fn(*a)
                counts['native_reads'] = counts.get('native_reads', 0) + 1
                counts['native_bytes'] = counts.get('native_bytes', 0) + len(a[4].value)
                counts['native_events'] = counts.get('native_events', 0) + len(a[4].value.splitlines())
                return result
            setattr(lib, name, native)
        if getattr(lib, '_traceweave_has_event_pages_v1', False):
            original_page = lib.fsdb_event_page_v1
            def page(*a):
                rc = original_page(*a)
                receipt = a[5]._obj
                counts['native_reads'] = counts.get('native_reads', 0) + 1
                counts['native_events'] = counts.get('native_events', 0) + receipt.events
                counts['native_bytes'] = counts.get('native_bytes', 0) + receipt.output_bytes
                return rc
            lib.fsdb_event_page_v1 = page
    for p in parsers.values():
        if isinstance(p, fsdb_parser.FSDBParser):
            p._lib = lib
            original_open = p._open
            def prepare(_p=p, _fn=original_open):
                cold = not _p._handle
                t = time.perf_counter()
                result = _fn()
                if cold:
                    counts['open_index_ms'] = counts.get('open_index_ms', 0) + (time.perf_counter()-t)*1000
                return result
            p._open = prepare
        else:
            original_parse = p._parse
            def parse(_p=p, _fn=original_parse):
                t = time.perf_counter()
                result = _fn()
                counts['initial_parse_ms'] = counts.get('initial_parse_ms', 0) + (time.perf_counter()-t)*1000
                counts['initial_parse_events'] = counts.get('initial_parse_events', 0) + sum(map(len,_p._transitions.values()))
                return result
            p._parse = parse
        p.get_transitions = count_read(p.get_transitions)
    def compare():
        return compare_signals(get_parser=parsers.__getitem__, wave_path_a=args.wave,
            signal_a=args.signal_a, wave_path_b=args.other_wave or args.wave,
            signal_b=args.signal_b, start_ps=args.start, end_ps=args.end)
    results = []
    for repeat in range(3):
        counts = {}
        before = io_counts()
        wall, cpu = time.perf_counter(), time.process_time()
        result = compare()
        elapsed, used_cpu = time.perf_counter()-wall, time.process_time()-cpu
        after = io_counts()
        if 'reading' not in result:
            # Baseline VCD rows: measured full-window output count, with the
            # same documented 32-byte time/record estimate as the page reader.
            counts['event_records_read'] = counts.get('legacy_events', 0)
        results.append(dict(state='first' if repeat == 0 else 'warm', wall_ms=elapsed*1000,
            cpu_ms=used_cpu*1000, peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            counts=dict(counts), io_delta={k:after[k]-before[k] for k in before}, result=result))
    event, requested = threading.Event(), []
    def cancel():
        requested.append(time.perf_counter()); event.set()
    timer = threading.Timer(0.001, cancel)
    token = push_cancel_event(event)
    timer.start()
    try:
        compare()
        cancellation = {'status': 'completed_before_observed_cancellation'}
    except OperationCancelled:
        cancellation = {'status': 'cancelled', 'latency_ms': (time.perf_counter()-requested[0])*1000}
    finally:
        timer.cancel(); timer.join(); pop_cancel_event(token)
    modules = {name:dict(path=m.__file__, sha256=sha(m.__file__)) for name,m in list(sys.modules.items())
               if name.startswith('src.') and getattr(m,'__file__',None)}
    native_paths = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                           if any(n in line for n in ('libfsdb_wrapper', 'libnffr', 'libnsys'))})
    report = dict(arguments=vars(args)|{'source_root':str(root),'output':str(args.output)},
        python=sys.executable, modules=modules, native={p:sha(p) for p in native_paths},
        inputs={p:dict(bytes=Path(p).stat().st_size,sha256=sha(p)) for p in waves},
        conditions='one fresh process, three queries; warm OS cache uncontrolled; no cache flushing; import/native dlopen excluded; first waveform open/index included; ABI bytes are not disk bytes',
        results=results, cancellation=cancellation)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'output':str(args.output), 'wall_ms':[r['wall_ms'] for r in results]}))


if __name__ == '__main__':
    main()
