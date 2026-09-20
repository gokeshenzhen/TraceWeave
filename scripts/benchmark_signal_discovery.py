#!/usr/bin/env python3
"""Fresh-process discovery/sweep benchmark; existing waveforms are read only.

First request includes library load and index construction. Later requests use
the same parser; no result is prefetched. OS caches are not flushed. Run each
workload serially in a new process with the same arguments for both revisions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument("--wave", type=Path, required=True)
    cli.add_argument("--wrapper", type=Path)
    cli.add_argument("--scope")
    cli.add_argument("--mode", choices=("discovery", "sweep"), required=True)
    cli.add_argument("--start", type=int, default=0)
    cli.add_argument("--end", type=int, default=-1)
    cli.add_argument("--max-interfaces", type=int, default=64)
    cli.add_argument("--repeats", type=int, default=3)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from src import fsdb_parser, operation_metrics
    from src.handshake_suggest import suggest_handshakes
    from src.handshake_sweep import sweep_handshake_anomalies

    if args.wrapper:
        fsdb_parser._WRAPPER_SO = str(args.wrapper.resolve())
    parser = fsdb_parser.FSDBParser(str(args.wave))
    identity = parser._stat_identity()
    calls = []

    class Metered:
        def __getattr__(self, name):
            target = getattr(parser, name)
            if name not in ("search_signals", "enumerate_scope_page"):
                return target
            def measured(*a, **kw):
                begin = time.perf_counter()
                result = target(*a, **kw)
                calls.append({"kind": name, "elapsed_ms": (time.perf_counter() - begin) * 1000,
                              "returned": len(result.get("results", [])),
                              "visited": result.get("visited"), "bytes": result.get("bytes_returned")})
                return result
            return measured

    rows = []
    metered = Metered()
    try:
        for repeat in range(args.repeats):
            calls.clear()
            metrics = operation_metrics.OperationMetrics()
            token = operation_metrics.push(metrics)
            begin, cpu = time.perf_counter(), time.process_time()
            try:
                common = dict(get_parser=lambda _: metered, wave_path=str(args.wave), scope=args.scope)
                if args.mode == "discovery":
                    facts = suggest_handshakes(**common, max_candidates=args.max_interfaces)
                else:
                    facts = sweep_handshake_anomalies(**common, start_ps=args.start, end_ps=args.end,
                                                     max_interfaces=args.max_interfaces)
            finally:
                operation_metrics.pop(token)
            rows.append({"state": "first_request" if repeat == 0 else "warm_request",
                         "elapsed_ms": (time.perf_counter() - begin) * 1000,
                         "cpu_ms": (time.process_time() - cpu) * 1000,
                         "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                         "calls": list(calls), "facts": facts,
                         "metrics": operation_metrics.snapshot(metrics)})
        wrapper = Path(parser._lib._name).resolve()
        assert wrapper == (args.wrapper.resolve() if args.wrapper else root / "libfsdb_wrapper.so")
        assert parser._stat_identity() == identity, "waveform changed during benchmark"
        sources = ["fsdb_wrapper.cpp", "src/fsdb_parser.py", "src/handshake_suggest.py",
                   "src/handshake_sweep.py", "src/cycle_query.py", "src/verify_condition.py",
                   "src/schemas.py", "fsdb_point_read.h"]
        for module in tuple(sys.modules.values()):
            filename = getattr(module, "__file__", None)
            if filename:
                path = Path(filename).resolve()
                if path.suffix == ".py" and path.is_relative_to(root):
                    sources.append(str(path.relative_to(root)))
        libraries = {line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                     if any(name in line for name in ("libnffr.so", "libnsys.so"))}
        report = {"arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                  "python": sys.executable, "python_version": platform.python_version(),
                  "platform": platform.platform(),
                  "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                  "wave_identity": identity, "wave_sha256": digest(args.wave),
                  "wrapper": str(wrapper), "wrapper_sha256": digest(wrapper),
                  "loaded_python": fsdb_parser.__file__,
                  "harness_sha256": digest(__file__),
                  "runtime_sha256": {p: digest(p) for p in sorted(libraries)},
                  "source_sha256": {p: digest(root / p) for p in sorted(set(sources))}, "rows": rows}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"output": str(args.output), "ms": [round(r["elapsed_ms"], 2) for r in rows]}))
    finally:
        parser.close()


if __name__ == "__main__":
    main()
