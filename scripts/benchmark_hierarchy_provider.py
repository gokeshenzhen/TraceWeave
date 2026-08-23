#!/usr/bin/env python3
"""Benchmark repeated IR-index copies against the semantic hierarchy provider.

Each measured mode runs in a fresh process. The legacy control reproduces the
former Source Graph formatting path, where every instance/definition lookup
materialized new dictionaries from ``ConnectivityIR``. The provider path
shares the immutable indexes already owned by ``ConnectivityQueryEngine``.
Only counts, timings, RSS, and an identity-free result digest are emitted.
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
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.connectivity_ir import (  # noqa: E402
    ConnectivityIR,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    InstanceDecl,
    SourceLocation,
)
from src.connectivity_query import ConnectivityQueryEngine  # noqa: E402
from src.hierarchy_provider import ConnectivityIRHierarchyProvider  # noqa: E402
from src.operation_metrics import read_process_rss_kib  # noqa: E402


BENCHMARK_NAME = "semantic_hierarchy_provider_v1"


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


def _build_ir(instance_count: int) -> ConnectivityIR:
    location = SourceLocation("synthetic.sv", 1)
    definitions = (
        DefinitionTemplate("top", "top", DefinitionKind.MODULE, location),
        DefinitionTemplate("leaf", "leaf", DefinitionKind.MODULE, location),
    )
    instances = [InstanceDecl("top", "top", "top", None, location)]
    instances.extend(
        InstanceDecl(
            f"top.u_leaf_{index}",
            f"u_leaf_{index}",
            "leaf",
            "top",
            location,
        )
        for index in range(instance_count)
    )
    return ConnectivityIR(
        frontend_name="benchmark",
        frontend_version="1",
        definitions=definitions,
        instances=tuple(instances),
        bindings=(),
        coverage=CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=1,
            files_projected=1,
        ),
        top_instances=("top",),
    )


def run_trial(args: argparse.Namespace) -> dict[str, Any]:
    ir = _build_ir(args.instances)
    engine = ConnectivityQueryEngine(ir)
    provider = ConnectivityIRHierarchyProvider(
        ir,
        design_identity="synthetic_snapshot",
        instance_index=engine.instance_index,
        definition_index=engine.definition_index,
    )
    target_indexes = tuple(
        (index * 104729) % args.instances for index in range(args.lookups)
    )
    gc.collect()
    rss_start_kib = read_process_rss_kib()
    started = time.perf_counter()
    definitions: list[str] = []
    if args.mode == "legacy":
        for index in target_indexes:
            instance = ir.instance_index[f"top.u_leaf_{index}"]
            definitions.append(ir.definition_index[instance.definition_id].name)
    else:
        for index in target_indexes:
            binding = provider.lookup_instance(
                top="top", instance_path=f"top.u_leaf_{index}"
            )
            if binding is None:
                raise AssertionError("provider failed to resolve a synthetic instance")
            definitions.append(binding.definition_name)
    lookup_wall_ms = (time.perf_counter() - started) * 1000.0

    scope_wall_ms = None
    if args.mode == "provider":
        started = time.perf_counter()
        for index in target_indexes:
            resolution = provider.resolve_scope(
                top="top", signal_path=f"top.u_leaf_{index}.value"
            )
            if resolution is None or resolution.ancestors[-1] != (
                f"top.u_leaf_{index}"
            ):
                raise AssertionError("provider failed bounded scope resolution")
        scope_wall_ms = (time.perf_counter() - started) * 1000.0

    rss_end_kib = read_process_rss_kib()
    peak_rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    result_sha256 = hashlib.sha256(
        json.dumps(definitions, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "benchmark": BENCHMARK_NAME,
        "mode": args.mode,
        "workload": {
            "instance_count": args.instances + 1,
            "lookup_count": args.lookups,
        },
        "measurement": {
            "lookup_wall_ms": round(lookup_wall_ms, 3),
            "scope_wall_ms": (
                round(scope_wall_ms, 3) if scope_wall_ms is not None else None
            ),
            "rss_start_kib": rss_start_kib,
            "rss_end_kib": rss_end_kib,
            "rss_delta_kib": (
                rss_end_kib - rss_start_kib
                if isinstance(rss_start_kib, int)
                and isinstance(rss_end_kib, int)
                else None
            ),
            "peak_rss_kib": peak_rss_kib,
        },
        "behavior_oracle": {
            "resolved_count": len(definitions),
            "result_sha256": result_sha256,
        },
    }


def _median(runs: Sequence[Mapping[str, Any]], field: str) -> float:
    return round(
        statistics.median(float(run["measurement"][field]) for run in runs),
        3,
    )


def aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, Any] = {}
    for mode in ("legacy", "provider"):
        selected = [run for run in runs if run["mode"] == mode]
        if not selected:
            continue
        by_mode[mode] = {
            "run_count": len(selected),
            "lookup_wall_median_ms": _median(selected, "lookup_wall_ms"),
            "rss_delta_median_kib": _median(selected, "rss_delta_kib"),
            "peak_rss_median_kib": _median(selected, "peak_rss_kib"),
            "behavior_oracles": sorted(
                {run["behavior_oracle"]["result_sha256"] for run in selected}
            ),
        }
        if mode == "provider":
            by_mode[mode]["scope_wall_median_ms"] = _median(
                selected, "scope_wall_ms"
            )

    comparison = None
    if set(by_mode) == {"legacy", "provider"}:
        legacy = by_mode["legacy"]
        provider = by_mode["provider"]
        baseline = float(legacy["lookup_wall_median_ms"])
        comparison = {
            "lookup_wall_reduction_percent": (
                round(
                    (baseline - float(provider["lookup_wall_median_ms"]))
                    * 100.0
                    / baseline,
                    3,
                )
                if baseline > 0
                else None
            ),
            "peak_rss_change_kib": round(
                float(provider["peak_rss_median_kib"])
                - float(legacy["peak_rss_median_kib"]),
                3,
            ),
            "behavior_equal": (
                legacy["behavior_oracles"] == provider["behavior_oracles"]
            ),
        }
    return {"by_mode": by_mode, "comparison": comparison}


def _child_command(args: argparse.Namespace, mode: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--instances",
        str(args.instances),
        "--lookups",
        str(args.lookups),
        "--mode",
        mode,
    ]


def run_compare(args: argparse.Namespace) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for _ in range(args.repeats):
        for mode in ("legacy", "provider"):
            completed = subprocess.run(
                _child_command(args, mode),
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            runs.append(json.loads(completed.stdout))
    return {
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
        },
        "workload": runs[0]["workload"] if runs else {},
        "runs": runs,
        "aggregate": aggregate_runs(runs),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", type=int, default=100_000)
    parser.add_argument("--lookups", type=int, default=100)
    parser.add_argument(
        "--mode", choices=("legacy", "provider", "compare"), default="compare"
    )
    parser.add_argument("--repeats", type=int, default=3)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    if args.instances < 1 or args.lookups < 1 or args.repeats < 1:
        raise SystemExit("instances, lookups, and repeats must be positive")
    result = run_compare(args) if args.mode == "compare" else run_trial(args)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
