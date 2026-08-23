#!/usr/bin/env python3
"""Benchmark hierarchy-prefix resolution and wide-bit Source Graph queries.

Run each workload in a fresh process so process peak RSS remains attributable::

    python3.11 scripts/benchmark_connectivity_query_indexes.py \
        --workload instance-resolution --size 30000 --repeats 100
    python3.11 scripts/benchmark_connectivity_query_indexes.py \
        --workload wide-load --size 4096 --repeats 100

``size`` is the number of direct child instances for ``instance-resolution``
and the packed signal width for ``wide-load``.
"""

from __future__ import annotations

import argparse
import gc
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
    AssignmentFact,
    BitMapping,
    BitRange,
    BindingStyle,
    BoundaryKind,
    ConnectivityIR,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    DependencyFact,
    EdgeKind,
    InstanceDecl,
    PortBinding,
    PortDecl,
    PortDirection,
    SignalDecl,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
    SymbolKind,
)
from src.connectivity_query import ConnectivityQueryEngine  # noqa: E402


BENCHMARK_NAME = "source_graph_query_indexes_v1"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _current_rss_kib() -> int | None:
    try:
        lines = Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


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
        "min": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "max": round(max(values), 6),
    }


def _complete_coverage() -> CoverageReport:
    return CoverageReport(
        status=CoverageStatus.COMPLETE,
        files_total=1,
        files_projected=1,
    )


def build_instance_resolution_ir(instance_count: int) -> ConnectivityIR:
    location = SourceLocation(file="synthetic/instance_resolution.sv", line=1)
    definition = DefinitionTemplate(
        definition_id="node",
        name="node",
        kind=DefinitionKind.MODULE,
        location=location,
        signals=(
            SignalDecl("sig", SymbolKind.NET, BitRange.scalar(), location),
        ),
    )
    instances = [InstanceDecl("top", "top", "node", None, location)]
    instances.extend(
        InstanceDecl(
            f"top.u{index:05d}",
            f"u{index:05d}",
            "node",
            "top",
            location,
        )
        for index in range(instance_count)
    )
    return ConnectivityIR(
        frontend_name="synthetic_benchmark",
        frontend_version="1",
        definitions=(definition,),
        instances=tuple(instances),
        bindings=(),
        coverage=_complete_coverage(),
        top_instances=("top",),
    )


def build_wide_load_ir(width: int) -> ConnectivityIR:
    location = SourceLocation(file="synthetic/wide_load.sv", line=1)
    bits = BitRange.from_width(width)
    top = DefinitionTemplate(
        definition_id="top",
        name="top",
        kind=DefinitionKind.MODULE,
        location=location,
        signals=(SignalDecl("payload", SymbolKind.NET, bits, location),),
    )
    assignment = AssignmentFact(
        assignment_id="leaf:consume",
        kind=EdgeKind.CONTINUOUS_ASSIGN,
        target=SignalSelection.template("sink", bits),
        dependencies=(
            DependencyFact(
                source=SignalSelection.template("data", bits),
                target=SignalSelection.template("sink", bits),
            ),
        ),
        boundary=BoundaryKind.COMBINATIONAL,
        evidence=SourceEvidence(
            construct="synthetic_wide_load",
            location=SourceLocation(file=location.file, line=10),
            frontend="synthetic_benchmark",
            frontend_version="1",
        ),
    )
    leaf = DefinitionTemplate(
        definition_id="leaf",
        name="leaf",
        kind=DefinitionKind.MODULE,
        location=location,
        ports=(PortDecl("data", PortDirection.INPUT, bits, 0, location),),
        signals=(SignalDecl("sink", SymbolKind.NET, bits, location),),
        assignments=(assignment,),
    )
    binding = PortBinding(
        binding_id="top.u:0:data",
        instance_path="top.u",
        port_name="data",
        direction=PortDirection.INPUT,
        style=BindingStyle.NAMED,
        mappings=(
            BitMapping(
                source=SignalSelection("payload", bits.indices, "top"),
                target=SignalSelection("data", bits.indices, "top.u"),
            ),
        ),
        evidence=SourceEvidence(
            construct="synthetic_wide_binding",
            location=SourceLocation(file=location.file, line=5),
            frontend="synthetic_benchmark",
            frontend_version="1",
        ),
    )
    return ConnectivityIR(
        frontend_name="synthetic_benchmark",
        frontend_version="1",
        definitions=(top, leaf),
        instances=(
            InstanceDecl("top", "top", "top", None, location),
            InstanceDecl("top.u", "u", "leaf", "top", location),
        ),
        bindings=(binding,),
        coverage=_complete_coverage(),
        top_instances=("top",),
    )


def run_benchmark(*, workload: str, size: int, repeats: int) -> dict[str, Any]:
    if workload not in {"instance-resolution", "wide-load"}:
        raise ValueError("unknown workload")
    if size < 1 or repeats < 1:
        raise ValueError("size and repeats must be positive")

    rss_start = _current_rss_kib()
    started = time.perf_counter()
    ir = (
        build_instance_resolution_ir(size)
        if workload == "instance-resolution"
        else build_wide_load_ir(size)
    )
    ir_build_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    engine = ConnectivityQueryEngine(ir)
    index_build_ms = (time.perf_counter() - started) * 1000.0
    rss_before_queries = _current_rss_kib()

    query_ms: list[float] = []
    serialization_ms: list[float] = []
    fingerprints: list[str] = []
    json_bytes = 0
    result_summary: dict[str, Any] | None = None
    gc.collect()
    for _ in range(repeats):
        started = time.perf_counter()
        if workload == "instance-resolution":
            result = engine.resolve_signal("top.sig")
            query_ms.append((time.perf_counter() - started) * 1000.0)
            payload = {
                "instance_path": result.instance_path,
                "symbol": result.symbol,
                "bits": result.bits,
            }
            result_summary = {
                "resolved_width": result.width,
                "resolved_instance_depth": (result.instance_path or "").count("."),
            }
        else:
            result = engine.query_loads("top.payload")
            query_ms.append((time.perf_counter() - started) * 1000.0)
            payload = result.to_dict()
            result_summary = {
                "match_count": len(result.matches),
                "inspected_edge_count": result.inspected_edge_count,
                "query_truncated": result.truncated,
                "covered_bit_count": len(result.resolved_bits),
            }
        started = time.perf_counter()
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        serialization_ms.append((time.perf_counter() - started) * 1000.0)
        json_bytes = len(encoded)
        fingerprints.append(hashlib.sha256(encoded).hexdigest())
        del encoded, payload, result

    assert result_summary is not None
    return {
        "benchmark": BENCHMARK_NAME,
        "workload": {
            "name": workload,
            "size": size,
            "repeats": repeats,
            "instance_count": size + 1 if workload == "instance-resolution" else 2,
            "signal_width": size if workload == "wide-load" else 1,
        },
        "environment": {
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "timings_ms": {
            "ir_build": round(ir_build_ms, 3),
            "index_build": round(index_build_ms, 3),
            "query": _distribution(query_ms),
            "serialization": _distribution(serialization_ms),
        },
        "memory_kib": {
            "rss_start": rss_start,
            "rss_before_queries": rss_before_queries,
            "rss_after_queries": _current_rss_kib(),
            "process_peak": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "result": {
            **result_summary,
            "json_bytes": json_bytes,
            "stable_across_repeats": len(set(fingerprints)) == 1,
            "fingerprint_sha256": fingerprints[0],
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        choices=("instance-resolution", "wide-load"),
        required=True,
    )
    parser.add_argument("--size", type=_positive_int, required=True)
    parser.add_argument("--repeats", type=_positive_int, default=100)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    print(
        json.dumps(
            run_benchmark(
                workload=args.workload,
                size=args.size,
                repeats=args.repeats,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
