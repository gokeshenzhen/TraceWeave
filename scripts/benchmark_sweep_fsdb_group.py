#!/usr/bin/env python3
"""Compare full FSDB handshake sweeps with legacy vs group loading.

The waveform may be proprietary: output contains only aggregate counts,
timings, coverage, and result-equivalence status. It never prints signal paths,
sampled values, interface scopes, or finding details.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import operation_metrics
from src.fsdb_parser import FSDBParser
from src.handshake_sweep import sweep_handshake_anomalies


GROUP_LIMIT_ENV = "TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS"


def _run_once(
    wave: Path,
    *,
    grouped: bool,
    group_limit: int,
    max_interfaces: int,
    max_wait_cycles: int,
) -> tuple[dict, dict]:
    previous_limit = os.environ.get(GROUP_LIMIT_ENV)
    # A handshake group always contains clock+valid+ready, so a limit of one
    # deterministically selects the ordinary per-signal path.
    os.environ[GROUP_LIMIT_ENV] = str(group_limit if grouped else 1)
    parser = FSDBParser(str(wave))
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    started = time.perf_counter()
    try:
        result = sweep_handshake_anomalies(
            get_parser=lambda _: parser,
            wave_path=str(wave),
            max_interfaces=max_interfaces,
            max_wait_cycles=max_wait_cycles,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    finally:
        operation_metrics.pop(token)
        parser.close()
        if previous_limit is None:
            os.environ.pop(GROUP_LIMIT_ENV, None)
        else:
            os.environ[GROUP_LIMIT_ENV] = previous_limit

    snapshot = operation_metrics.snapshot(metrics)
    summary = {
        "elapsed_ms": round(elapsed_ms, 3),
        "coverage_status": result.get("coverage_status"),
        "discovered_count": result.get("discovered_count"),
        "interface_count": result.get("interface_count"),
        "flagged_count": result.get("flagged_count"),
        "transition_truncated_count": result.get("transition_truncated_count"),
        "native_group_count": snapshot.get("sweep_native_group_count", 0),
        "native_group_fallback_count": snapshot.get(
            "sweep_native_group_fallback_count", 0
        ),
        "native_group_signal_max": snapshot.get(
            "sweep_native_group_signal_max", 0
        ),
        "native_group_load_total_ms": snapshot.get(
            "sweep_native_group_load_total_ms", 0
        ),
        "native_standalone_load_total_ms": snapshot.get(
            "sweep_native_standalone_load_total_ms", 0
        ),
        "native_fallback_signal_total": snapshot.get(
            "sweep_native_fallback_signal_total", 0
        ),
        "group_pack_count": snapshot.get("sweep_group_pack_count", 0),
        "group_pack_clock_total": snapshot.get(
            "sweep_group_pack_clock_total", 0
        ),
        "group_chunk_count": snapshot.get("sweep_group_chunk_count", 0),
        "native_load_total_ms": snapshot.get("sweep_native_load_total_ms", 0),
        "native_traverse_format_total_ms": snapshot.get(
            "sweep_native_traverse_format_total_ms", 0
        ),
        "clock_read_count": snapshot.get("sweep_clock_read_count", 0),
        "signal_read_count": snapshot.get("sweep_signal_read_count", 0),
        "sample_lookup_total_ms": snapshot.get(
            "sweep_sample_lookup_total_ms", 0
        ),
        "sample_materialize_total_ms": snapshot.get(
            "sweep_sample_materialize_total_ms", 0
        ),
        "protocol_scan_total_ms": snapshot.get(
            "sweep_protocol_scan_total_ms", 0
        ),
        "rss_peak_delta_kib": snapshot.get("sweep_rss_peak_delta_kib", 0),
    }
    return result, summary


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("--wave", type=Path, required=True)
    cli.add_argument("--repeats", type=int, default=5)
    cli.add_argument("--group-limit", type=int, default=16)
    cli.add_argument("--max-interfaces", type=int, default=64)
    cli.add_argument("--max-wait-cycles", type=int, default=16)
    args = cli.parse_args()
    if not args.wave.exists():
        cli.error(f"waveform does not exist: {args.wave}")
    if args.repeats < 1 or not 2 <= args.group_limit <= 256:
        cli.error("repeats must be >=1 and group-limit must be in [2, 256]")

    legacy_results: list[dict] = []
    grouped_results: list[dict] = []
    legacy_runs: list[dict] = []
    grouped_runs: list[dict] = []
    for repeat in range(args.repeats):
        order = (False, True) if repeat % 2 == 0 else (True, False)
        for grouped in order:
            result, summary = _run_once(
                args.wave,
                grouped=grouped,
                group_limit=args.group_limit,
                max_interfaces=args.max_interfaces,
                max_wait_cycles=args.max_wait_cycles,
            )
            if grouped:
                grouped_results.append(result)
                grouped_runs.append(summary)
            else:
                legacy_results.append(result)
                legacy_runs.append(summary)
            gc.collect()

    expected = legacy_results[0]
    equivalent = all(
        result == expected for result in [*legacy_results, *grouped_results]
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
            "wave_bytes": args.wave.stat().st_size,
            "repeats": args.repeats,
            "group_limit": args.group_limit,
            "max_interfaces": args.max_interfaces,
        },
        "results_equivalent": equivalent,
        "legacy_median_ms": round(legacy_median, 3),
        "grouped_median_ms": round(grouped_median, 3),
        "elapsed_reduction_percent": round(reduction, 1),
        "legacy_runs": legacy_runs,
        "grouped_runs": grouped_runs,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
