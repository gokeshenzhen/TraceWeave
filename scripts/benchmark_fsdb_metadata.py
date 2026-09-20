#!/usr/bin/env python3
"""Read-only FSDB metadata / sweep / diff benchmark, one fresh process per run.

Select a checkout with --source-root; build its wrapper before running. The
first query includes lazy native loading and preparation. Later rows are hot.
OS caches are not flushed. Timings are inclusive and must not be added together.
No prefetch or injected result cache is used. Full fact digests exclude timings.
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
from collections import defaultdict
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(path):
    s = path.stat()
    return [str(path.resolve()), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument("--wave", type=Path, required=True)
    cli.add_argument("--other-wave", type=Path)
    cli.add_argument("--wrapper", type=Path, help="Explicit old-wrapper compatibility probe")
    cli.add_argument("--scope", required=True)
    cli.add_argument("--mode", choices=("metadata", "sweep", "diff", "uart_compare"), required=True)
    cli.add_argument("--start", type=int, default=4_000_000_000)
    cli.add_argument("--end", type=int, default=4_010_000_000)
    cli.add_argument("--repeats", type=int, default=3)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    if args.repeats < 1 or (args.mode == "uart_compare" and not args.other_wave):
        cli.error("repeats must be positive; uart_compare requires --other-wave")
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    from src import fsdb_parser, operation_metrics
    from src.divergence_compare import compare_signals
    from src.handshake_sweep import sweep_handshake_anomalies

    assert Path(fsdb_parser.__file__).resolve() == root / "src/fsdb_parser.py"
    if args.wrapper:
        fsdb_parser._WRAPPER_SO = str(args.wrapper.resolve())
    paths = list(dict.fromkeys([args.wave, args.other_wave or args.wave]))
    before = [identity(p) for p in paths]
    phases = defaultdict(lambda: {"calls": 0, "ms": 0.0})

    def timed(name, fn, parser=None):
        def call(*a, **kw):
            preparing = name == "_open" and parser._handle is None
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                phases[name]["calls"] += 1
                elapsed = (time.perf_counter() - t) * 1000
                phases[name]["ms"] += elapsed
                if preparing:
                    phases["first_prepare"]["calls"] += 1
                    phases["first_prepare"]["ms"] += elapsed
        return call

    fsdb_parser._parse_trans_buf = timed("transition_parse", fsdb_parser._parse_trans_buf)
    parsers = {str(p): fsdb_parser.FSDBParser(str(p)) for p in paths}
    for p in parsers.values():
        for method in ("_open", "get_header", "get_summary", "get_signal_width", "search_signals", "get_transitions"):
            if hasattr(p, method):
                setattr(p, method, timed(method, getattr(p, method), p))
    p = parsers[str(args.wave)]
    rows = []
    for repeat in range(args.repeats):
        phases.clear()
        metrics = operation_metrics.OperationMetrics()
        metrics.values["_sweep_active"] = True
        token = operation_metrics.push(metrics)
        # Reuse existing native profile aggregates for diff as well as sweep.
        operation_metrics.record_sweep_rss(phase="start")
        started = time.perf_counter()
        if args.mode == "metadata":
            stages = {}
            t = time.perf_counter()
            header = getattr(p, "get_header", p.get_summary)()
            stages["header_ms"] = (time.perf_counter() - t) * 1000
            stages["rss_after_header_kib"] = operation_metrics.read_process_rss_kib()
            t = time.perf_counter()
            summary = p.get_summary()
            stages["summary_ms"] = (time.perf_counter() - t) * 1000
            stages["rss_after_summary_kib"] = operation_metrics.read_process_rss_kib()
            widths = []
            stages["exact_width_ms"] = []
            for leaf in ("clk_i", "tx_fifo_rvalid"):
                t = time.perf_counter()
                widths.append(p.get_signal_width(args.scope + "." + leaf))
                stages["exact_width_ms"].append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            search = p.search_signals(args.scope + ".", max_results=10000)
            stages["scope_search_ms"] = (time.perf_counter() - t) * 1000
            facts = {"header": {k: header.get(k) for k in ("total_signals", "scale_unit", "scale_fs_per_tick", "simulation_duration_ps")},
                     "widths": widths, "scope_search": search}
            detail = {"facts": facts["header"], "widths": widths, "scope_matched": search["total_matched"],
                      "summary": summary, "stages": stages}
        elif args.mode == "sweep":
            facts = sweep_handshake_anomalies(get_parser=parsers.__getitem__, wave_path=str(args.wave),
                start_ps=args.start, end_ps=args.end, max_interfaces=64)
            detail = {k: facts.get(k) for k in ("coverage_status", "discovered_count", "interface_count", "flagged_count", "transition_truncated_count", "discovery")}
        else:
            a = args.scope + ".clk_i" if args.mode == "diff" else "tb.dut.cio_tx_o"
            b = args.scope + ".tx_fifo_rvalid" if args.mode == "diff" else a
            facts = compare_signals(get_parser=parsers.__getitem__, wave_path_a=str(args.wave),
                signal_a=a, wave_path_b=str(args.other_wave or args.wave), signal_b=b,
                start_ps=args.start, end_ps=args.end)
            detail = {k: facts.get(k) for k in ("comparison_status", "coverage_status", "coverage_gaps", "first_divergence_time_ps", "earliest_difference_proven", "transitions_compared")}
        elapsed = (time.perf_counter() - started) * 1000
        operation_metrics.pop(token)
        rows.append({"state": "first_request" if repeat == 0 else "hot", "elapsed_ms": elapsed,
                     "result_digest": digest(facts), "result": detail, "inclusive_phases": dict(phases),
                     "metrics": operation_metrics.snapshot(metrics),
                     "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    wrapper = Path(p._lib._name).resolve()
    assert wrapper == (args.wrapper.resolve() if args.wrapper else root / "libfsdb_wrapper.so")
    mappings = [line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines() if "libfsdb_wrapper.so" in line]
    assert mappings and all(Path(m).resolve() == wrapper for m in mappings)
    report = {"mode": args.mode, "start_ps": args.start, "end_ps": args.end, "rows": rows,
              "python": sys.executable, "python_version": platform.python_version(), "platform": platform.platform(),
              "source_root": str(root), "head": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
              "loaded_python": fsdb_parser.__file__, "loaded_wrapper": str(wrapper),
              "wrapper_sha256": file_digest(wrapper), "wrapper_override": bool(args.wrapper),
              "source_sha256": {name: file_digest(root / name) for name in
                                ("src/fsdb_parser.py", "fsdb_wrapper.cpp", "src/divergence_compare.py")},
              "native_metadata_v1": getattr(p._lib, "_traceweave_has_metadata_v1", False),
              "native_transition_group": getattr(p._lib, "_traceweave_has_transition_group", False),
              "wave_identities": before, "wave_sha256": [file_digest(path) for path in paths],
              "metadata_cache_entries": len(getattr(p, "_metadata_cache", {})),
              "metadata_cache_estimated_bytes": getattr(p, "_metadata_bytes", 0)}
    for parser in parsers.values():
        parser.close()
    assert before == [identity(p) for p in paths], "waveform changed during evaluation"
    report["wave_identity_unchanged"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "elapsed_ms": [r["elapsed_ms"] for r in rows],
                      "process_peak_rss_kib": rows[-1]["process_peak_rss_kib"]}))


if __name__ == "__main__":
    main()
