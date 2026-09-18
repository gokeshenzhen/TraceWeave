#!/usr/bin/env python3
"""License-free repeated-hierarchy scaling check for constant propagation.

This generated combinational control workload isolates propagation cost; it is
not an OpenTitan replacement or a simulation result. The normal isolated scan
worker enforces its time/RSS budget, and the report checks the final leaf's
result before discarding the full facts.
"""
import argparse
import asyncio
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.structural_semantic_runtime import run_isolated_scan
from src.structural_semantics import ScanLimits


async def benchmark(args):
    with tempfile.TemporaryDirectory(prefix="traceweave-propagation-") as directory:
        source = Path(directory) / "design.sv"
        parts = ["module leaf(input a,output y);assign y=~a;endmodule"]
        child = "leaf"
        for i in range(args.depth):
            parts.append(f"module wrap{i}(input a,output y);{child} u(.a(a),.y(y));endmodule")
            child = f"wrap{i}"
        parts.append(f"module top;for(genvar i=0;i<{args.copies};i++)begin:g wire y;{child} u(.a(1'b0),.y(y));end endmodule")
        source.write_text("\n".join(parts))
        limits = ScanLimits(max_facts=50_000, max_propagation_steps=1_000_000)
        started = time.perf_counter()
        result = await run_isolated_scan({
            "frontend_version": importlib.metadata.version("pyslang"),
            "frontend_args": [str(source), "--top", "top"],
            "categories": ["propagated_constant", "constant_control"],
            "limits": asdict(limits), "scope": None,
            "timeout_sec": args.timeout, "max_rss_mib": args.rss_mib,
        }, args.python)
        last = f"top.g[{args.copies-1}].y"
        last_fact = next((f for f in result.get("facts", []) if f.get("signal") == last), None)
        return {"workload": "generated_combinational_wrappers", "copies": args.copies,
                "wrapper_depth": args.depth, "expected_instances": 1+args.copies*(args.depth+1),
                "wall_ms": (time.perf_counter()-started)*1000,
                "status": result["status"], "gaps": result.get("gaps", []),
                "last_output_proven_one": bool(last_fact and last_fact["state"] == "fully_known"
                    and last_fact["constant_regions"] == [{"lsb_offset": 0, "width": 1, "bits": "1"}]),
                "facts": result.get("total_facts", 0), "metrics": result.get("metrics", {}),
                "propagation": result.get("propagation", {}),
                "limits": {**asdict(limits), "timeout_sec": args.timeout, "max_rss_mib": args.rss_mib}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copies", type=int, default=3000)
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--rss-mib", type=int, default=512)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    if not 1 <= args.copies <= 5000 or not 0 <= args.depth <= 8:
        parser.error("copies must be 1..5000 and depth must be 0..8")
    print(json.dumps(asyncio.run(benchmark(args)), indent=2))
