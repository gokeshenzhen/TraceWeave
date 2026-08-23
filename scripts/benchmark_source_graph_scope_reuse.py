#!/usr/bin/env python3
"""Benchmark a dominating Source Graph artifact across compile projections.

The first request builds an artifact covering two sibling scopes from an
80-input dependency closure. The second request asks for one covered scope from
a 64-input closure over the same immutable full compile snapshot. A correct
cross-projection dominating hit skips the second synthetic frontend delay.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.connectivity_ir import (  # noqa: E402
    BitRange,
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    InstanceDecl,
    SignalDecl,
    SourceLocation,
    SymbolKind,
)
from src.source_graph_contract import (  # noqa: E402
    BoundaryMode,
    CompileInputManifest,
    CompileProjectionMode,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    RequestedCone,
    SOURCE_GRAPH_COMPILE_PROJECTION_GAP,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactIdentity,
    SourceGraphArtifactScope,
    SourceGraphArtifactScopeReceipt,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphCompileProjection,
    SourceGraphIdentity,
    SourceGraphQueryIdentity,
)
from src.source_graph_runtime import (  # noqa: E402
    SourceGraphRuntime,
    WorkerBuildResult,
    WorkerResourceMetrics,
)


BENCHMARK_NAME = "source_graph_cross_projection_reuse_v1"
OBJECTIVE_EXCLUSION = SOURCE_GRAPH_COMPILE_PROJECTION_GAP


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return parsed


def _fingerprint(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _git_head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _git_dirty() -> bool | None:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout) if completed.returncode == 0 else None


def _distribution(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "max": round(max(values), 3),
    }


def _synthetic_ir() -> ConnectivityIR:
    location = SourceLocation(file="synthetic/scope_reuse.sv", line=1)
    definition = DefinitionTemplate(
        definition_id="node",
        name="node",
        kind=DefinitionKind.MODULE,
        location=location,
        signals=(SignalDecl("q", SymbolKind.NET, BitRange.scalar(), location),),
    )
    gap = CoverageGap(
        code=OBJECTIVE_EXCLUSION,
        message="synthetic dependency closure omits unrelated inputs",
        impact=CoverageStatus.INCONCLUSIVE,
        scopes=("*",),
    )
    return ConnectivityIR(
        frontend_name="synthetic_scope_reuse",
        frontend_version="1",
        definitions=(definition,),
        instances=(
            InstanceDecl("top", "top", "node", None, location),
            InstanceDecl("top.u_left", "u_left", "node", "top", location),
            InstanceDecl("top.u_right", "u_right", "node", "top", location),
        ),
        bindings=(),
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=200,
            files_projected=80,
            gaps=(gap,),
        ),
        top_instances=("top",),
    )


def build_requests() -> tuple[SourceGraphBuildRequest, SourceGraphBuildRequest]:
    full_inputs = tuple(f"synthetic/input_{index:03d}.sv" for index in range(200))
    manifest = CompileInputManifest(
        fingerprint=_fingerprint("full_compile_manifest"),
        ordered_inputs=full_inputs,
        ordered_options=("--compat", "all"),
        ordered_tops=("top",),
        inputs_complete=True,
        options_complete=True,
        tops_complete=True,
    )
    source = SourceGraphIdentity(
        compile_inputs=manifest,
        frontend_name="synthetic_scope_reuse",
        frontend_version="1",
    )
    boundary = CoverageBoundary(
        mode=BoundaryMode.EXPLICIT,
        instance_paths=("top", "top.u_left"),
        objective_exclusions=(OBJECTIVE_EXCLUSION,),
    )
    build_scope = SourceGraphBuildScope(
        design="synthetic_scope_reuse",
        top="top",
        target=ConnectivityTarget(
            operation=QueryOperation.DRIVER,
            signal_path="top.u_left.q",
            instance_path_hint="top.u_left",
        ),
        hierarchy_ancestors=("top", "top.u_left"),
        requested_cone=RequestedCone(
            operation=QueryOperation.DRIVER,
            max_hops=4,
            instance_paths=("top", "top.u_left"),
        ),
        coverage_boundary=boundary,
    )
    narrow_scope = SourceGraphArtifactScope.from_build_scope(
        build_scope,
        hierarchy_snapshot_sha256=_fingerprint("hierarchy_snapshot"),
    )
    expanded_scope = SourceGraphArtifactScope(
        design=narrow_scope.design,
        top=narrow_scope.top,
        hierarchy_snapshot_sha256=narrow_scope.hierarchy_snapshot_sha256,
        proved_ancestor_chains=(
            ("top", "top.u_left"),
            ("top", "top.u_right"),
        ),
        proved_lcas=("top",),
        projection_instance_paths=("top", "top.u_left", "top.u_right"),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top", "top.u_left", "top.u_right"),
            objective_exclusions=(OBJECTIVE_EXCLUSION,),
        ),
    )
    common = {
        "source": source,
        "compile_snapshot_sha256": _fingerprint("compile_snapshot"),
        "adapter_version": "benchmark_adapter_1",
        "worker_protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    }
    expanded = SourceGraphArtifactIdentity(
        **common,
        scope=expanded_scope,
        compile_projection=SourceGraphCompileProjection(
            mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
            ordered_inputs=full_inputs[:80],
            full_input_count=len(full_inputs),
            seed_symbol_count=8,
            dependency_symbol_count=72,
        ),
    )
    narrow = SourceGraphArtifactIdentity(
        **common,
        scope=narrow_scope,
        compile_projection=SourceGraphCompileProjection(
            mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
            ordered_inputs=full_inputs[:64],
            full_input_count=len(full_inputs),
            seed_symbol_count=6,
            dependency_symbol_count=58,
        ),
    )
    query = SourceGraphQueryIdentity.from_build_scope(build_scope)
    return (
        SourceGraphBuildRequest(
            identity=source,
            scope=build_scope,
            artifact=expanded,
            query=query,
        ),
        SourceGraphBuildRequest(
            identity=source,
            scope=build_scope,
            artifact=narrow,
            query=query,
        ),
    )


class DelayedWorker:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.count = 0
        self.ir = _synthetic_ir()

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.count += 1
        await asyncio.sleep(self.delay_seconds)
        receipt = SourceGraphArtifactScopeReceipt(
            scope=request.artifact_identity.scope,
            coverage_status=self.ir.coverage.status,
            gap_codes=(OBJECTIVE_EXCLUSION,),
        )
        return WorkerBuildResult.ready(
            self.ir,
            receipt,
            metrics=WorkerResourceMetrics(
                wall_time_ms=self.delay_seconds * 1000.0,
                ir_bytes=len(self.ir.to_json_bytes()),
            ),
        )


async def _run_once(delay_seconds: float) -> dict[str, Any]:
    expanded, narrow = build_requests()
    worker = DelayedWorker(delay_seconds)
    runtime = SourceGraphRuntime(worker)
    started = time.perf_counter()
    first = await runtime.prepare(expanded)
    second_started = time.perf_counter()
    second = await runtime.prepare(narrow)
    second_ms = (time.perf_counter() - second_started) * 1000.0
    total_ms = (time.perf_counter() - started) * 1000.0
    return {
        "first_status": first.status.value,
        "second_status": second.status.value,
        "total_ms": total_ms,
        "second_prepare_ms": second_ms,
        "worker_build_count": worker.count,
        "second_cache_disposition": second.metrics.cache_disposition.value,
        "second_cache_lookup_reason": second.cache_lookup_reason.value,
        "second_scope_relation": (
            second.scope_match.relation.value if second.scope_match else None
        ),
        "selected_first_artifact": second.entry is first.entry,
    }


async def run_benchmark(*, delay_ms: float, repeats: int) -> dict[str, Any]:
    if delay_ms < 0 or repeats < 1:
        raise ValueError("delay_ms must be non-negative and repeats positive")
    samples = [await _run_once(delay_ms / 1000.0) for _ in range(repeats)]
    return {
        "benchmark": BENCHMARK_NAME,
        "workload": {
            "repeats": repeats,
            "synthetic_frontend_delay_ms": delay_ms,
            "available_scope_instance_count": 3,
            "requested_scope_instance_count": 2,
            "available_compile_input_count": 80,
            "requested_compile_input_count": 64,
            "full_compile_input_count": 200,
        },
        "environment": {
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "timings_ms": {
            "total": _distribution([item["total_ms"] for item in samples]),
            "second_prepare": _distribution(
                [item["second_prepare_ms"] for item in samples]
            ),
        },
        "memory_kib": {
            "process_peak": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "result": {
            "first_statuses": [item["first_status"] for item in samples],
            "second_statuses": [item["second_status"] for item in samples],
            "worker_build_counts": [item["worker_build_count"] for item in samples],
            "second_cache_dispositions": [
                item["second_cache_disposition"] for item in samples
            ],
            "second_cache_lookup_reasons": [
                item["second_cache_lookup_reason"] for item in samples
            ],
            "second_scope_relations": [
                item["second_scope_relation"] for item in samples
            ],
            "selected_first_artifact": all(
                item["selected_first_artifact"] for item in samples
            ),
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-ms", type=_nonnegative_float, default=50.0)
    parser.add_argument("--repeats", type=_positive_int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    print(
        json.dumps(
            asyncio.run(run_benchmark(delay_ms=args.delay_ms, repeats=args.repeats)),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
