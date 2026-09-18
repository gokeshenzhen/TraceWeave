#!/usr/bin/env python3
"""Compare uncached, cold-cache and warm scan dispatch; no licensed tools."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["TRACEWEAVE_AUTO_KDB"] = "0"
os.environ["TRACEWEAVE_TELEMETRY"] = "0"


async def benchmark(compile_log, simulator, repeats):
    import server
    from src.design_identity import DesignIdentityReader
    from src.structural_scan_runtime import StructuralScanRuntime
    server._design_identity_reader = DesignIdentityReader()
    server._structural_scan_runtime = StructuralScanRuntime()
    rows = []
    original = server.scan_structural_risks
    calls = 0
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    server.scan_structural_risks = counted
    raw_result = None
    run = server._structural_scan_runtime.run
    async def captured(*args, **kwargs):
        nonlocal raw_result
        raw_result, disposition = await run(*args, **kwargs)
        return raw_result, disposition
    server._structural_scan_runtime.run = captured
    for phase in ["uncached", "cold", *["warm"] * repeats]:
        os.environ["TRACEWEAVE_STRUCTURAL_SCAN_CACHE"] = "0" if phase == "uncached" else "1"
        previous = calls
        started = time.perf_counter()
        result = await server._dispatch("scan_structural_risks", {"compile_log": compile_log, "simulator": simulator})
        elapsed = (time.perf_counter()-started)*1000
        # The public output budget can produce different views when receipts
        # have different sizes. Compare the complete untrimmed result instead.
        payload = {k: v for k, v in raw_result.items() if k != "scan_metrics"}
        metrics = result.scan_metrics
        rows.append({"phase": phase, "wall_ms": round(elapsed, 3),
                     "rule_calls": calls-previous, "result_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                     "cache": metrics["result_cache_disposition"], "cache_bytes": metrics["result_cache_bytes"],
                     "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", required=True)
    parser.add_argument("--simulator", default="auto")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(args.compile_log, args.simulator, args.repeats)), indent=2))
