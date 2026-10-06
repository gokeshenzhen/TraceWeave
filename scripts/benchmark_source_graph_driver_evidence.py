#!/usr/bin/env python3
"""Cold isolated Slang + exact memory-hit driver evidence benchmark.

Run in a fresh process per revision and fixture variant, using the same fixture
path and Python environment. Every repeat starts an empty memory cache and
issues one cold and one warm query. Disk caching and persistent sessions are
off; all staging and source copies live in a private temporary directory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.source_graph_contract import (
    BoundaryMode, CompileInputManifest, ConnectivityTarget, CoverageBoundary,
    QueryOperation, RequestedCone, SourceGraphBuildRequest, SourceGraphBuildScope,
    SourceGraphIdentity,
)
from src.source_graph_runtime import IsolatedSourceGraphProcessRunner, SourceGraphRuntime
from src.source_graph_backend import SourceGraphConnectivityBackend
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME


async def measure(fixture: Path, corrected: bool, repeats: int) -> dict:
    source = fixture.read_text()
    if corrected:
        source = source.replace("S[6:9]", "S[5:8]")
    digest = hashlib.sha256(source.encode()).hexdigest()
    rows = []
    with tempfile.TemporaryDirectory(prefix="traceweave-driver-measure-") as directory:
        root = Path(directory)
        os.environ["TRACEWEAVE_CACHE_DIR"] = str(root / "cache")
        path = root / "case_bank.sv"
        path.write_text(source)
        instances = ("bank", *(f"bank.u{i}" for i in range(8)))
        scope = SourceGraphBuildScope(design="case_bank", top="bank",
            target=ConnectivityTarget(operation=QueryOperation.DRIVER, signal_path="bank.S"),
            hierarchy_ancestors=("bank",),
            requested_cone=RequestedCone(operation=QueryOperation.DRIVER, max_hops=10, instance_paths=instances),
            coverage_boundary=CoverageBoundary(mode=BoundaryMode.EXPLICIT, instance_paths=instances))
        request = SourceGraphBuildRequest(scope=scope, identity=SourceGraphIdentity(
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version=importlib.metadata.version("pyslang"),
            compile_inputs=CompileInputManifest(fingerprint=digest,
                ordered_inputs=(str(path),), ordered_options=("--compat", "all"),
                ordered_tops=("bank",), inputs_complete=True, options_complete=True, tops_complete=True)))
        for repeat in range(repeats):
            runtime = SourceGraphRuntime(IsolatedSourceGraphProcessRunner(
                python_executable=sys.executable, working_directory=ROOT, staging_directory=root))
            for temperature in ("cold", "warm"):
                outcome = await runtime.prepare(request, timeout_seconds=30)
                if outcome.entry is None:
                    raise RuntimeError(outcome.to_receipt(include_blocker_message=True))
                start = time.perf_counter()
                result = SourceGraphConnectivityBackend(outcome.entry).find_driver(
                    "bank.S", "synthetic.vcd", "synthetic.log", recursive=True)
                query_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                serialize_ms = (time.perf_counter() - start) * 1000
                receipt = result["_source_graph_query_receipt"]
                rows.append(dict(repeat=repeat, temperature=temperature,
                    prepare=outcome.metrics.to_dict(), artifact_attempt_count=1,
                    query_ms=query_ms, serialize_ms=serialize_ms,
                    response_characters=len(serialized), response_bytes=len(serialized.encode()),
                    statement_evidence_count=receipt["match_count"],
                    structural_driver_count=receipt.get("structural_driver_count"),
                    driver_chain_groups=len(result["driver_chain"] or []),
                    bit_counts=[result[k] for k in ("resolved_bit_count", "unresolved_bit_count", "multi_driver_bit_count")],
                    claim_semantics=result["claim_semantics"], traversal=result["traversal"],
                    parent_rss_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    summary = {}
    for temperature in ("cold", "warm"):
        samples = [r for r in rows if r["temperature"] == temperature]
        summary[temperature] = {
            key: statistics.median(r[key] for r in samples)
            for key in ("query_ms", "serialize_ms", "response_characters", "response_bytes")}
        summary[temperature].update(
            prepare_ms=statistics.median(r["prepare"]["total_wall_ms"] for r in samples),
            frontend_launch_count=sum(r["prepare"]["frontend_launch_count"] for r in samples),
            worker_rss_peak_kib=max(r["prepare"].get("rss_peak_kib", 0) for r in samples))
    return dict(benchmark="source_graph_driver_evidence_v1", corrected=corrected,
        input_sha256=digest, repeats=repeats, cache="fresh memory per repeat; one exact warm hit",
        disk_cache=False, persistent_session=False, python=sys.version.split()[0],
        pyslang=importlib.metadata.version("pyslang"), summary=summary, samples=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/source_graph_drivers/case_bank.sv")
    parser.add_argument("--corrected", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps(asyncio.run(measure(args.fixture, args.corrected, args.repeats)), indent=2))
