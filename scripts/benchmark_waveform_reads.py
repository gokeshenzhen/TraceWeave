#!/usr/bin/env python3
"""Reproducible reader benchmark against a deterministic, locally made trace.

Generate with --generate --wave /tmp/activity.vcd --steps 1000000.
For FSDB use Verdi vcd2fsdb on that VCD, then pass the FSDB to --wave.
Compare checkouts with --source-root; --wrapper selects an independently built
native library. Each invocation is a fresh process; OS caches are not flushed.
Timings include expected-value checks, exclude MCP transport and conversion,
and distinguish the first parse/load from subsequent reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys
import time


def generate(path: Path, steps: int) -> None:
    # Refuse to overwrite an existing waveform.
    with path.open("x") as output:
        output.write(
            '$timescale 1ps $end\n$scope module top $end\n'
            '$var wire 1 ! clk $end\n$var wire 32 " data $end\n'
            '$var wire 1024 # wide $end\n$var wire 32 $ sparse $end\n'
            '$upscope $end\n$enddefinitions $end\n'
        )
        for step in range(steps + 1):
            bits = f"{(step * 2654435761) & 0xffffffff:032b}"
            output.write(f'#{step * 10}\n{step & 1}!\nb{bits} "\n')
            if step % 1000 == 0:
                output.write(f"b{bits * 32} #\n")
            if step in (0, steps // 2):
                output.write(f"b{bits} $\n")


def rss(field: str = "VmRSS:") -> int | None:
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith(field):
                return int(line.split()[1])
    return None


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--wave", required=True, type=Path)
    cli.add_argument("--steps", type=int, default=1_000_000)
    cli.add_argument("--queries", type=int, default=31)
    cli.add_argument("--generate", action="store_true")
    cli.add_argument("--workload", choices=("point", "window", "around", "cycle", "all"), default="all")
    cli.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument("--wrapper", type=Path)
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    if args.steps < 1000 or args.queries < 1:
        cli.error("steps must be >= 1000 and queries must be positive")
    if args.generate:
        if args.wave.suffix.lower() != ".vcd":
            cli.error("--generate requires a .vcd path")
        generate(args.wave, args.steps)
        return

    sys.path.insert(0, str(args.source_root.resolve()))
    if args.wave.suffix.lower() == ".vcd":
        from src.vcd_parser import VCDParser
        factory = VCDParser
    else:
        from src import fsdb_parser
        if args.wrapper:
            fsdb_parser._WRAPPER_SO = str(args.wrapper.resolve())
        factory = fsdb_parser.FSDBParser

    digest = hashlib.sha256()
    duration = args.steps * 10

    def expected(leaf: str, timestamp: int) -> str:
        step = min(timestamp // 10, args.steps)
        if leaf == "clk":
            return str(step & 1)
        if leaf == "wide":
            step = (step // 1000) * 1000
        elif leaf == "sparse":
            step = args.steps // 2 if step >= args.steps // 2 else 0
        bits = f"{(step * 2654435761) & 0xffffffff:032b}"
        return bits * 32 if leaf == "wide" else bits

    def point(leaf: str, timestamp: int) -> None:
        bits = reader.get_value_at_time("top." + leaf, timestamp)["value"]["bin"]
        assert bits == expected(leaf, timestamp), (leaf, timestamp, bits)
        digest.update(bits.encode())

    def window(timestamp: int) -> None:
        result = reader.get_transitions("top.data", timestamp, timestamp + 200)
        rows = [(r["time_ps"], r["value"]["bin"]) for r in result["transitions"]]
        assert rows == [(t, expected("data", t)) for t in range((timestamp + 9) // 10 * 10, timestamp + 201, 10)]
        before = result["predecessor"]
        assert before["time_ps"] == (timestamp - 1) // 10 * 10
        assert before["value"]["bin"] == expected("data", before["time_ps"])
        digest.update(json.dumps(rows).encode())

    def around(timestamp: int) -> None:
        leaves = ("data", "wide", "sparse")
        result = reader.get_signals_around_time(["top." + leaf for leaf in leaves], timestamp, 100, 5)
        for leaf in leaves:
            assert result["signals"]["top." + leaf]["value_at_center"]["bin"] == expected(leaf, timestamp)
        digest.update(json.dumps(result, sort_keys=True).encode())

    def cycle(_: int) -> None:
        from src.cycle_query import get_signals_by_cycle
        result = get_signals_by_cycle(reader, "top.clk", ["top.data"],
                                      start_time_ps=duration * 9 // 10,
                                      end_time_ps=duration * 9 // 10 + 200)
        assert result["num_cycles_returned"] == 10
        for row in result["cycles"]:
            assert row["signals"]["top.data"]["bin"] == expected("data", row["time_ps"] + 1)
        digest.update(json.dumps(result, sort_keys=True).encode())

    def measure(function, inputs) -> dict:
        samples = []
        for item in inputs:
            start = time.perf_counter_ns()
            function(item)
            samples.append((time.perf_counter_ns() - start) / 1e6)
        ordered = sorted(samples)
        return {"calls": len(samples), "median_ms": statistics.median(samples),
                "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
                "rss_after_kib": rss()}

    result = {"steps": args.steps, "format": args.wave.suffix, "python": sys.version.split()[0],
              "workload": args.workload, "file_bytes": args.wave.stat().st_size,
              "measurement": "reader API plus correctness checks; fresh process; OS cache not flushed",
              "rss_before_open_kib": rss()}
    start = time.perf_counter_ns()
    reader = factory(str(args.wave))
    try:
        point("data", duration * 95 // 100 + 3)
        result["open_and_first_point_ms"] = (time.perf_counter_ns() - start) / 1e6
        result["rss_after_first_point_kib"] = rss()
        rng = random.Random(91016)
        times = [rng.randrange(duration // 2, duration - 500) for _ in range(args.queries)]
        if args.workload in ("point", "all"):
            result["point_same"] = measure(lambda t: point("data", t), [duration * 95 // 100 + 3] * args.queries)
            result["point_scattered"] = measure(lambda t: point("data", t), times)
            result["point_mixed"] = measure(lambda item: point(*item), [(leaf, t) for t in times[:11] for leaf in ("data", "wide", "sparse")])
            # Ensure point queries do not alter the next range read's state.
            window(times[0])
        if args.workload in ("window", "all"):
            result["window_20_changes"] = measure(window, times)
        if args.workload in ("around", "all"):
            result["around_three_signals"] = measure(around, times[:11])
        if args.workload in ("cycle", "all"):
            result["cycle_first"] = measure(cycle, [0])
            result["cycle_repeat"] = measure(cycle, [0, 0, 0])
        result["query_digest"] = digest.hexdigest()
        result["rss_peak_kib"] = rss("VmHWM:")
    finally:
        close = getattr(reader, "close", None)
        if close:
            close()
    result["rss_after_close_kib"] = rss()
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
