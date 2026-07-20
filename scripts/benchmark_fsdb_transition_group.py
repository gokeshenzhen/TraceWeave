#!/usr/bin/env python3
"""Compare legacy per-signal FSDB loads with the P2 group-load session.

The default fixture includes a shared clock, scalar handshake signals, and a
1024-bit payload. A private workload can be supplied with ``--wave`` and one or
more exact ``--signal`` arguments. Output contains counts/timings only; signal
paths and sampled values are never printed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import operation_metrics
from src.fsdb_parser import FSDBParser


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WAVE = ROOT / "tests" / "fixtures" / "wide_bus.fsdb"
DEFAULT_SIGNALS = [
    "tb.aclk",
    "tb.vld1",
    "tb.rdy1",
    "tb.dat1[1023:0]",
    "tb.vld2",
    "tb.rdy2",
    "tb.dat2[1023:0]",
]


def _run_once(
    wave: Path, signals: list[str], *, grouped: bool, iterations: int
) -> dict:
    parser = FSDBParser(str(wave))
    parser.get_summary()  # exclude file-open/tree-index cost from this benchmark
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value("_sweep_active", True)
    started = time.perf_counter()
    try:
        active = grouped
        for _ in range(iterations):
            if grouped:
                with parser.transition_group(signals) as iteration_active:
                    active = active and iteration_active
                    results = [parser.get_transitions(path) for path in signals]
            else:
                results = [parser.get_transitions(path) for path in signals]
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        operation_metrics.pop(token)
        parser.close()
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "group_active": active,
        "transition_counts": [result["transition_count"] for result in results],
        "truncated": [bool(result["truncated"]) for result in results],
        "metrics": operation_metrics.snapshot(metrics),
    }


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("--wave", type=Path, default=DEFAULT_WAVE)
    cli.add_argument("--signal", action="append", dest="signals")
    cli.add_argument("--repeats", type=int, default=5)
    cli.add_argument(
        "--iterations", type=int, default=50,
        help="load/read/unload iterations inside each timed repeat",
    )
    args = cli.parse_args()
    signals = args.signals or DEFAULT_SIGNALS
    if args.repeats < 1 or args.iterations < 1 or not signals:
        cli.error("repeats>=1, iterations>=1, and at least one signal are required")
    if not args.wave.exists():
        cli.error(f"waveform does not exist: {args.wave}")

    legacy_runs: list[dict] = []
    grouped_runs: list[dict] = []
    for repeat in range(args.repeats):
        order = (False, True) if repeat % 2 == 0 else (True, False)
        for grouped in order:
            run = _run_once(
                args.wave, signals, grouped=grouped, iterations=args.iterations
            )
            (grouped_runs if grouped else legacy_runs).append(run)

    expected = legacy_runs[0]
    equivalent = all(
        run["transition_counts"] == expected["transition_counts"]
        and run["truncated"] == expected["truncated"]
        for run in [*legacy_runs, *grouped_runs]
    )
    legacy_ms = [run["elapsed_ms"] for run in legacy_runs]
    grouped_ms = [run["elapsed_ms"] for run in grouped_runs]
    legacy_median = statistics.median(legacy_ms)
    grouped_median = statistics.median(grouped_ms)
    reduction = (
        (legacy_median - grouped_median) / legacy_median * 100.0
        if legacy_median else 0.0
    )
    print(json.dumps({
        "workload": {
            "signal_count": len(signals),
            "repeats": args.repeats,
            "iterations_per_repeat": args.iterations,
            "wave_bytes": args.wave.stat().st_size,
        },
        "legacy_median_ms": round(legacy_median, 3),
        "grouped_median_ms": round(grouped_median, 3),
        "elapsed_reduction_percent": round(reduction, 1),
        "results_equivalent": equivalent,
        "group_path_active": all(run["group_active"] for run in grouped_runs),
        "transition_counts": expected["transition_counts"],
        "truncated": expected["truncated"],
        "legacy_elapsed_ms": legacy_ms,
        "grouped_elapsed_ms": grouped_ms,
        "legacy_metrics_last": legacy_runs[-1]["metrics"],
        "grouped_metrics_last": grouped_runs[-1]["metrics"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
