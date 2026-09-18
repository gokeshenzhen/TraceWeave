#!/usr/bin/env python3
"""Measure license-free structural facts through the real server dispatch."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["TRACEWEAVE_AUTO_KDB"] = "0"
os.environ["TRACEWEAVE_TELEMETRY"] = "0"


async def benchmark(args):
    import server
    base = {"compile_log": args.compile_log, "simulator": args.simulator,
            "semantic_timeout_sec": args.timeout, "semantic_max_rss_mib": args.rss_mib}
    if args.scope:
        base["semantic_scope"] = args.scope
    rows = []
    for mode in ("auto", "deep", "auto"):
        start = time.perf_counter()
        result = await server._dispatch("scan_structural_risks", {**base, "analysis_mode": mode})
        sem = result.semantic
        rows.append({"mode": mode, "wall_ms": (time.perf_counter()-start)*1000,
                     "status": sem.status, "gaps": sem.gaps, "facts": sem.total_facts,
                     "counts": sem.counts, "instances": sem.instances_visited,
                     "templates": sem.template_count, "metrics": sem.metrics,
                     "cache": sem.cache_disposition, "display_truncated": sem.output_truncated})
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", required=True)
    parser.add_argument("--simulator", default="auto")
    parser.add_argument("--scope")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--rss-mib", type=int, default=512)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(args)), indent=2))
