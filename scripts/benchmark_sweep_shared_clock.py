#!/usr/bin/env python3
"""Reproducible full-sweep benchmark for shared-clock interface fan-out.

The generated VCD models many independent valid/ready interfaces under one
ancestor clock. It is intentionally protocol-simple: the benchmark measures
execution structure (clock reads, edge extraction, signal reads, wall time,
and incremental Python peak memory), not anomaly-classification complexity.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import operation_metrics
from src.handshake_sweep import sweep_handshake_anomalies
from src.vcd_parser import VCDParser


def _symbol(index: int) -> str:
    """Return a compact printable VCD identifier."""
    alphabet = [chr(code) for code in range(33, 127)]
    base = len(alphabet)
    chars: list[str] = []
    value = index
    while True:
        chars.append(alphabet[value % base])
        value //= base
        if value == 0:
            return "".join(chars)


def build_shared_clock_vcd(interface_count: int, cycles: int) -> str:
    clock_symbol = _symbol(0)
    header = [
        "$timescale 1ps $end",
        "$scope module top $end",
        f"$var wire 1 {clock_symbol} clk $end",
    ]
    valid_symbols: list[str] = []
    ready_symbols: list[str] = []
    next_symbol = 1
    for index in range(interface_count):
        valid_symbol = _symbol(next_symbol)
        ready_symbol = _symbol(next_symbol + 1)
        next_symbol += 2
        valid_symbols.append(valid_symbol)
        ready_symbols.append(ready_symbol)
        header.extend(
            [
                f"$scope module u{index} $end",
                f"$var wire 1 {valid_symbol} in_valid $end",
                f"$var wire 1 {ready_symbol} in_ready $end",
                "$upscope $end",
            ]
        )
    header.extend(["$upscope $end", "$enddefinitions $end"])

    body = ["#0", f"0{clock_symbol}"]
    for valid_symbol, ready_symbol in zip(valid_symbols, ready_symbols):
        body.extend([f"1{valid_symbol}", f"1{ready_symbol}"])
    level = 0
    for tick in range(1, cycles * 2 + 1):
        level ^= 1
        body.extend([f"#{tick * 5}", f"{level}{clock_symbol}"])
    return "\n".join([*header, *body]) + "\n"


def run_once(path: Path, interface_count: int) -> dict[str, object]:
    parser = VCDParser(str(path))
    parser.get_summary()  # exclude one-time VCD parsing from the sweep timing
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = sweep_handshake_anomalies(
            get_parser=lambda _: parser,
            wave_path=str(path),
            max_interfaces=interface_count,
        )
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        operation_metrics.pop(token)
    return {
        "elapsed_ms": round(elapsed_ms, 1),
        "peak_incremental_mib": round(peak_bytes / (1024 * 1024), 2),
        "interface_count": result["interface_count"],
        "coverage_status": result["coverage_status"],
        "operation_metrics": operation_metrics.snapshot(metrics),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interfaces", type=int, default=32)
    parser.add_argument("--cycles", type=int, default=20_000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.interfaces < 1 or args.cycles < 2 or args.repeats < 1:
        parser.error("interfaces>=1, cycles>=2, and repeats>=1 are required")

    with tempfile.TemporaryDirectory(prefix="traceweave-sweep-bench-") as tmp:
        wave_path = Path(tmp) / "shared_clock.vcd"
        wave_path.write_text(
            build_shared_clock_vcd(args.interfaces, args.cycles),
            encoding="utf-8",
        )
        runs = [run_once(wave_path, args.interfaces) for _ in range(args.repeats)]

    elapsed = [float(run["elapsed_ms"]) for run in runs]
    peaks = [float(run["peak_incremental_mib"]) for run in runs]
    print(
        json.dumps(
            {
                "workload": {
                    "interfaces": args.interfaces,
                    "cycles": args.cycles,
                    "repeats": args.repeats,
                    "shared_clocks": 1,
                    "signals_per_interface": 2,
                },
                "median_elapsed_ms": round(statistics.median(elapsed), 1),
                "max_peak_incremental_mib": round(max(peaks), 2),
                "runs": runs,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
