#!/usr/bin/env python3
"""Measure VCD cold load, retained-parser repeats, new-wave load and log reads.

Run each comparison in a fresh process with identical immutable inputs. Hashing
primes the OS cache: cold means a fresh parser, not disk-cold I/O. This serial
backend microbenchmark excludes handler/scheduler/transport and log snapshot
overhead. No simulation, source build or licensed tool is run. Output directories
must be new.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from benchmark_vcd_replay import resources, sha


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.source_root.resolve()
    os.environ["TRACEWEAVE_CACHE_DIR"] = str(args.output / "cache")
    os.environ["TRACEWEAVE_TELEMETRY"] = "0"
    sys.path.insert(0, str(root))
    import server
    from src import operation_metrics
    from src.log_parser import SimLogParser

    inputs = {key: path.resolve() for key in ("wave", "next_wave", "log", "next_log")
              if (path := getattr(args, key)) is not None}
    manifest = dict(
        source_root=str(root),
        commit=subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        sources={name: sha(root / name) for name in (
            "src/vcd_parser.py", "src/log_parser.py", "src/operation_metrics.py")},
        inputs={key: dict(path=str(path), bytes=path.stat().st_size, sha256=sha(path))
                for key, path in inputs.items()},
        python=sys.version, platform=platform.platform(),
        os_cache_condition="Input hashes prime OS cache; no cache flush; fresh process/parser",
        sequence="initial wave, same-wave repeats, new wave while initial parser remains resident; then logs",
        warm_repeats=args.warm_repeats,
        measurement_boundary="serial VCD/log backends; excludes handler, scheduling, transport and log snapshots",
    )
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    measurements, facts = [], []

    def measure(label, name, arguments):
        metrics = operation_metrics.OperationMetrics()
        token = operation_metrics.push(metrics)
        start = resources()
        try:
            if name == "sweep_handshakes":
                result = server.sweep_handshake_anomalies(get_parser=server._get_parser, **arguments)
            else:
                result = SimLogParser(**arguments).parse()
        finally:
            operation_metrics.pop(token)
        end = resources()
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        row = dict(
            label=label, tool=name, wall_ms=1000 * (end["wall"] - start["wall"]),
            cpu_ms=1000 * (end["cpu"] - start["cpu"]),
            rss_start_kib=start["rss_kib"], rss_end_kib=end["rss_kib"],
            process_peak_rss_kib=end["peak_rss_kib"],
            minor_faults=end["minor_faults"] - start["minor_faults"],
            major_faults=end["major_faults"] - start["major_faults"],
            metrics=operation_metrics.snapshot(metrics), fact_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        )
        measurements.append(row)
        facts.append(dict(label=label, result=result))
        print(json.dumps({key: row[key] for key in ("label", "wall_ms", "cpu_ms", "rss_end_kib")}), flush=True)

    wave_args = dict(scope=args.scope, start_ps=args.start_ps, end_ps=args.end_ps,
                     max_interfaces=args.max_interfaces)
    if args.scope is None:
        wave_args.pop("scope")
    measure("initial_wave", "sweep_handshakes", dict(wave_path=str(inputs["wave"]), **wave_args))
    for index in range(args.warm_repeats):
        measure(f"same_wave_{index + 1}", "sweep_handshakes", dict(wave_path=str(inputs["wave"]), **wave_args))
    if "next_wave" in inputs:
        measure("new_wave", "sweep_handshakes", dict(wave_path=str(inputs["next_wave"]), **wave_args))
    for key in ("log", "next_log"):
        if key not in inputs:
            continue
        path = inputs[key]
        measure(key, "parse_sim_log", dict(log_path=str(path), simulator=args.simulator))
        if key == "log":
            for index in range(args.warm_repeats):
                measure(f"same_log_{index + 1}", "parse_sim_log",
                        dict(log_path=str(path), simulator=args.simulator))
    for key, path in inputs.items():
        if sha(path) != manifest["inputs"][key]["sha256"]:
            raise RuntimeError("Benchmark input changed during measurement")
    (args.output / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n")
    (args.output / "facts.json").write_text(json.dumps(facts, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--wave", type=Path, required=True)
    parser.add_argument("--next-wave", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--next-log", type=Path)
    parser.add_argument("--simulator", choices=("vcs", "xcelium", "verilator"), default="vcs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warm-repeats", type=int, default=1)
    parser.add_argument("--scope")
    parser.add_argument("--start-ps", type=int, default=0)
    parser.add_argument("--end-ps", type=int, default=-1)
    parser.add_argument("--max-interfaces", type=int, default=64)
    args = parser.parse_args()
    if args.warm_repeats < 0 or args.max_interfaces < 1:
        parser.error("warm-repeats must be nonnegative and max-interfaces positive")
    if any(path is not None and path.suffix.lower() != ".vcd"
           for path in (args.wave, args.next_wave)):
        parser.error("this isolated serial benchmark accepts VCD inputs only")
    run(args)


if __name__ == "__main__":
    main()
