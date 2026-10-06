#!/usr/bin/env python3
"""Cold/warm Slang queries, same-result driver encoding and actual MCP text.

Run in a fresh process per revision and fixture variant, using the same fixture
path and Python environment. Every repeat starts an empty memory cache and
issues one cold and one warm query. Disk caching and persistent sessions are
off; all staging and source copies live in a private temporary directory.
Presentation defaults to 50 repeats; memory modes use separate execs with the
input resident before measurement. --output-directory keeps reproducible captures.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
from unittest.mock import patch
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
from src.schemas import ExplainDriverResult


async def measure(fixture: Path, corrected: bool, repeats: int, *, guards=None,
                  query_limits=None, presentation_repeats=50, codec_limits=None,
                  output_directory=None, memory=True) -> dict:
    # No inherited user disk/session cache, LSF policy, telemetry or NPI overlay.
    with patch.dict(os.environ, {
        "TRACEWEAVE_CONNECTIVITY_ROUTE": "source_graph", "TRACEWEAVE_AUTO_KDB": "0",
        "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE": "0", "TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION": "0",
        "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY": "off", "TRACEWEAVE_TELEMETRY": "0",
        "TRACEWEAVE_NPI_EXECUTION": "local",
    }):
        return await _measure(fixture, corrected, repeats, guards=guards, query_limits=query_limits,
            presentation_repeats=presentation_repeats, codec_limits=codec_limits,
            output_directory=output_directory, memory=memory)


async def _measure(fixture, corrected, repeats, *, guards, query_limits,
                   presentation_repeats, codec_limits, output_directory, memory):
    from scripts.driver_compact_measurement import (
        guards_source, query_result, short_path_model, measure_presentation, capture_wire)
    source = fixture.read_text() if guards is None else guards_source(guards)
    target = "bank.S" if guards is None else "t.q"
    top = target.split(".")[0]
    if corrected:
        source = source.replace("S[6:9]", "S[5:8]")
    digest = hashlib.sha256(source.encode()).hexdigest()
    rows = []
    with tempfile.TemporaryDirectory(prefix="traceweave-driver-measure-") as directory:
        root = Path(directory)
        os.environ["TRACEWEAVE_CACHE_DIR"] = str(root / "cache")
        path = root / "case_bank.sv"
        path.write_text(source)
        instances = ("bank", *(f"bank.u{i}" for i in range(8))) if guards is None else ("t",)
        scope = SourceGraphBuildScope(design="driver_evidence_benchmark", top=top,
            target=ConnectivityTarget(operation=QueryOperation.DRIVER, signal_path=target),
            hierarchy_ancestors=(top,),
            requested_cone=RequestedCone(operation=QueryOperation.DRIVER, max_hops=10, instance_paths=instances),
            coverage_boundary=CoverageBoundary(mode=BoundaryMode.EXPLICIT, instance_paths=instances))
        request = SourceGraphBuildRequest(scope=scope, identity=SourceGraphIdentity(
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version=importlib.metadata.version("pyslang"),
            compile_inputs=CompileInputManifest(fingerprint=digest,
                ordered_inputs=(str(path),), ordered_options=("--compat", "all"),
                ordered_tops=(top,), inputs_complete=True, options_complete=True, tops_complete=True)))
        for repeat in range(repeats):
            runtime = SourceGraphRuntime(IsolatedSourceGraphProcessRunner(
                python_executable=sys.executable, working_directory=ROOT, staging_directory=root))
            for temperature in ("cold", "warm"):
                outcome = await runtime.prepare(request, timeout_seconds=30)
                if outcome.entry is None:
                    raise RuntimeError(outcome.to_receipt(include_blocker_message=True))
                start = time.perf_counter()
                result = query_result(SourceGraphConnectivityBackend(outcome.entry), target,
                                      query_limits, wave="synthetic.vcd")
                query_ms = (time.perf_counter() - start) * 1000
                # Preserve the raw measurement too, but measure the public
                # schema with the server's explicit-full JSON formatting.
                raw_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                start = time.perf_counter()
                model = ExplainDriverResult.model_validate({
                    key: value for key, value in result.items() if not key.startswith("_")})
                validate_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                serialized = model.model_dump_json(indent=2, exclude_none=True)
                serialize_ms = (time.perf_counter() - start) * 1000
                receipt = result["_source_graph_query_receipt"]
                rows.append(dict(repeat=repeat, temperature=temperature,
                    prepare=outcome.metrics.to_dict(), artifact_attempt_count=1,
                    query_ms=query_ms, validate_ms=validate_ms, serialize_ms=serialize_ms,
                    response_characters=len(serialized), response_bytes=len(serialized.encode()),
                    raw_backend_compact_bytes=len(raw_json.encode()),
                    statement_evidence_count=receipt["match_count"],
                    structural_driver_count=receipt.get("structural_driver_count"),
                    driver_chain_groups=len(result["driver_chain"] or []),
                    bit_counts=[result[k] for k in ("resolved_bit_count", "unresolved_bit_count", "multi_driver_bit_count")],
                    claim_semantics=result["claim_semantics"], traversal=result["traversal"],
                    parent_rss_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
        capture_dir = Path(output_directory) if output_directory is not None else root / "captures"
        presentation = measure_presentation(short_path_model(source, target, query_limits),
            capture_dir, repeats=presentation_repeats, limits=codec_limits, memory=memory)
        wire, wire_model = await capture_wire(path, target, root / "wire", query_limits=query_limits,
                                             repeats=repeats)
        wire["presentation"] = measure_presentation(wire_model, capture_dir,
            repeats=presentation_repeats, stem="wire", memory=memory)
        if output_directory is not None:
            import shutil
            shutil.copytree(root / "wire", capture_dir / "wire_calls", dirs_exist_ok=True)
    summary = {}
    for temperature in ("cold", "warm"):
        samples = [r for r in rows if r["temperature"] == temperature]
        summary[temperature] = {
            key: statistics.median(r[key] for r in samples)
            for key in ("query_ms", "validate_ms", "serialize_ms", "response_characters", "response_bytes", "raw_backend_compact_bytes")}
        summary[temperature].update(
            prepare_ms=statistics.median(r["prepare"]["total_wall_ms"] for r in samples),
            frontend_launch_count=sum(r["prepare"]["frontend_launch_count"] for r in samples),
            worker_rss_peak_kib=max(r["prepare"].get("rss_peak_kib", 0) for r in samples),
            parent_rss_peak_kib=max(r["parent_rss_peak_kib"] for r in samples))
    return dict(benchmark="source_graph_driver_evidence_v3", corrected=corrected,
        guards=guards, query_limits=query_limits or {}, presentation=presentation, wire=wire,
        captures_persisted=output_directory is not None,
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        implementation_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "src/driver_evidence_codec.py", "src/evidence_output.py", "src/schemas.py", "server.py",
            "scripts/benchmark_source_graph_driver_evidence.py", "scripts/driver_compact_measurement.py")},
        os=platform.platform(), path_shape="presentation: literal source; isolated/wire: private absolute path",
        artifact_attempt_count_basis="isolated samples: one explicit prepare (fixed); wire: actual routing receipt",
        response_format="ExplainDriverResult; indent=2, exclude_none=True; excludes server routing/recovery receipts",
        input_sha256=digest, repeats=repeats, cache="fresh memory per repeat; one exact warm hit",
        disk_cache=False, persistent_session=False, python=sys.version.split()[0],
        pyslang=importlib.metadata.version("pyslang"), summary=summary, samples=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/source_graph_drivers/case_bank.sv")
    parser.add_argument("--corrected", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--guards", type=int, help="independent guards plus one initial assignment (0..511)")
    parser.add_argument("--assignment-limit", type=int)
    parser.add_argument("--evidence-limit", type=int)
    parser.add_argument("--max-depth", type=int)
    parser.add_argument("--presentation-repeats", type=int, default=50)
    parser.add_argument("--memo-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--no-memory", action="store_true", help="skip independent memory child processes")
    args = parser.parse_args()
    if args.repeats < 1 or args.presentation_repeats < 1 or args.memo_bytes < 0:
        parser.error("repeats must be positive and memo bytes nonnegative")
    if args.guards is not None and not 0 <= args.guards <= 511:
        parser.error("--guards must be 0..511 (9-bit fixture operands)")
    query_limits = {key: getattr(args, key) for key in ("assignment_limit", "evidence_limit", "max_depth")
                    if getattr(args, key) is not None}
    if any(value < (0 if key == "max_depth" else 1) for key, value in query_limits.items()):
        parser.error("analysis limits must be positive; max depth can be zero")
    from src.driver_evidence_codec import EncodeLimits
    print(json.dumps(asyncio.run(measure(args.fixture, args.corrected, args.repeats,
        guards=args.guards, query_limits=query_limits, presentation_repeats=args.presentation_repeats,
        codec_limits=EncodeLimits(memo_bytes=args.memo_bytes), output_directory=args.output_directory,
        memory=not args.no_memory)), indent=2))
