#!/usr/bin/env python3
"""Compare bounded NPI and Slang hierarchy providers on one SoC snapshot.

Each provider runs in a fresh process. NPI loads the existing elaborated KDB
once and performs only exact target-prefix lookups. Slang builds the normal
proof-backed Source Graph projection for each target, then reads hierarchy
bindings from the already prepared compact IR. The final receipt contains
only counts, fixed labels, timings, resource measurements, and SHA-256 facts;
source paths, instance paths, definition names, and signal names are omitted.

This is an opt-in development benchmark. It requires a usable local NPI/KDB
environment for the NPI arm and the pinned Source Graph frontend for the Slang
arm. It never changes public MCP routing or hierarchy overlay policy.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BENCHMARK_NAME = "soc_hierarchy_provider_differential_v1"
_NPI_OVERLAY_ENV = "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"


class BenchmarkInputError(ValueError):
    """A requested provider comparison cannot be reproduced safely."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_identity(value: str | None) -> str | None:
    """Normalize absolute Slang and KDB-relative NPI source identities."""

    if not value:
        return None
    normalized = os.path.normpath(value).replace("\\", "/")
    marker = "/fusesoc-work/"
    if marker in normalized:
        return normalized.rsplit(marker, 1)[1]
    if normalized.startswith("src/"):
        return normalized
    if "/src/" in normalized:
        return f"src/{normalized.rsplit('/src/', 1)[1]}"
    return normalized


def _binding_oracle(binding: Any) -> dict[str, Any]:
    source = _source_identity(binding.source_file)
    return {
        "instance_path_sha256": _sha256_text(binding.path),
        "definition_sha256": _sha256_text(binding.definition_name),
        "source_sha256": _sha256_text(source) if source is not None else None,
        "source_line": binding.source_line,
    }


def _resolution_summary(
    resolution: Any | None,
    *,
    signal: str,
) -> dict[str, Any]:
    target_sha256 = _sha256_text(signal)
    if resolution is None:
        return {
            "target_sha256": target_sha256,
            "signal_depth": len(signal.split(".")),
            "status": "unresolved",
            "provider_kind": None,
            "ancestor_count": 0,
            "binding_count": 0,
            "coverage_gap_codes": [],
            "binding_oracle": [],
            "binding_oracle_sha256": _sha256_json([]),
        }
    oracle = [_binding_oracle(binding) for binding in resolution.bindings]
    return {
        "target_sha256": target_sha256,
        "signal_depth": len(signal.split(".")),
        "status": resolution.status,
        "provider_kind": resolution.provider_kind.value,
        "ancestor_count": len(resolution.ancestors),
        "binding_count": len(resolution.bindings),
        "remaining_path_segment_count": resolution.remaining_path_segment_count,
        "coverage_gap_codes": list(resolution.gap_codes),
        "binding_oracle": oracle,
        "binding_oracle_sha256": _sha256_json(oracle),
    }


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


def _process_memory() -> dict[str, int]:
    return {
        "self_peak_rss_kib": int(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        ),
        "child_peak_rss_kib": int(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        ),
    }


@contextmanager
def _without_npi_hierarchy_overlay():
    previous = os.environ.get(_NPI_OVERLAY_ENV)
    os.environ[_NPI_OVERLAY_ENV] = "off"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_NPI_OVERLAY_ENV, None)
        else:
            os.environ[_NPI_OVERLAY_ENV] = previous


def _provider_workload(args: argparse.Namespace) -> dict[str, Any]:
    stat_result = args.compile_log.stat()
    return {
        "simulator": args.simulator,
        "compile_log_bytes": stat_result.st_size,
        "target_count": len(args.signal),
        "target_sha256": [_sha256_text(signal) for signal in args.signal],
        "signal_depths": [len(signal.split(".")) for signal in args.signal],
        "top_sha256": _sha256_text(args.top) if args.top else None,
        "max_candidate_paths": args.max_candidate_paths,
        "max_depth": args.max_depth,
    }


def run_npi(args: argparse.Namespace) -> dict[str, Any]:
    from src.verdi_npi_backend import VerdiNpiBackend  # noqa: PLC0415

    backend = VerdiNpiBackend()
    targets: list[dict[str, Any]] = []
    for signal in args.signal:
        started = time.perf_counter()
        provider = backend.build_hierarchy_provider(
            os.fspath(args.compile_log),
            signal,
            args.simulator,
            top_hint=args.top,
            max_candidate_paths=args.max_candidate_paths,
        )
        provider_wall_ms = (time.perf_counter() - started) * 1000.0
        top = provider.top if provider is not None else (args.top or "")
        lookup_started = time.perf_counter()
        resolution = (
            provider.resolve_scope(top=top, signal_path=signal)
            if provider is not None and top
            else None
        )
        lookup_wall_ms = (time.perf_counter() - lookup_started) * 1000.0
        targets.append(
            {
                "provider_wall_ms": round(provider_wall_ms, 3),
                "lookup_wall_ms": round(lookup_wall_ms, 3),
                "provider_metrics": backend.hierarchy_provider_metrics,
                "lookup_metrics": backend.instance_src_map_metrics,
                "resolution": _resolution_summary(resolution, signal=signal),
            }
        )
    return {
        "benchmark": BENCHMARK_NAME,
        "provider": "npi",
        "workload": _provider_workload(args),
        "targets": targets,
        "process_memory": _process_memory(),
    }


async def _run_slang_async(args: argparse.Namespace) -> dict[str, Any]:
    import server  # noqa: PLC0415
    from src.source_graph_adapter import build_source_graph_plan  # noqa: PLC0415
    from src.source_graph_contract import QueryOperation  # noqa: PLC0415
    from src.source_graph_runtime import (  # noqa: PLC0415
        IsolatedSourceGraphProcessRunner,
        SourceGraphRuntime,
    )

    hierarchy_started = time.perf_counter()
    with _without_npi_hierarchy_overlay():
        await server._dispatch(
            "build_tb_hierarchy",
            {
                "compile_log": os.fspath(args.compile_log),
                "simulator": args.simulator,
            },
        )
    hierarchy_wall_ms = (time.perf_counter() - hierarchy_started) * 1000.0
    hierarchy, snapshot = server._resolve_hierarchy_context(
        os.fspath(args.compile_log), args.simulator
    )
    compile_result = hierarchy.get("compile_result")
    if not isinstance(compile_result, Mapping):
        raise BenchmarkInputError("hierarchy result lacks compile context")

    runtime = SourceGraphRuntime(
        IsolatedSourceGraphProcessRunner(
            python_executable=os.fspath(args.frontend_python)
        )
    )
    targets: list[dict[str, Any]] = []
    for signal in args.signal:
        plan_started = time.perf_counter()
        plan = build_source_graph_plan(
            compile_log=os.fspath(args.compile_log),
            compile_result=compile_result,
            hierarchy_result=hierarchy,
            hierarchy_snapshot_sha256=snapshot,
            operation=QueryOperation.DRIVER,
            signal_path=signal,
            top_hint=args.top,
            max_hops=args.max_depth,
            frontend_version=args.frontend_version,
            recursive=True,
            include_expr=True,
            kind_filter=(),
        )
        planning_wall_ms = (time.perf_counter() - plan_started) * 1000.0
        target: dict[str, Any] = {
            "planning_wall_ms": round(planning_wall_ms, 3),
            "plan_status": plan.status.value,
            "resolution": _resolution_summary(None, signal=signal),
        }
        if plan.request is None:
            blocker = plan.receipt.blocker
            target["plan_blocker"] = blocker.code if blocker is not None else None
            targets.append(target)
            continue

        projection = plan.request.artifact_identity.compile_projection
        target["projection_input_count"] = (
            len(projection.ordered_inputs) if projection is not None else None
        )
        target["projection_instance_count"] = len(
            plan.request.artifact_identity.scope.projection_instance_paths
        )
        prepare_started = time.perf_counter()
        outcome = await runtime.prepare(
            plan.request,
            timeout_seconds=args.timeout_seconds,
        )
        target["prepare_wall_ms"] = round(
            (time.perf_counter() - prepare_started) * 1000.0,
            3,
        )
        target["prepare_status"] = outcome.status.value
        target["prepare_metrics"] = outcome.metrics.to_dict()
        if outcome.entry is not None:
            resolved_top = plan.request.scope.top
            lookup_started = time.perf_counter()
            resolution = outcome.entry.hierarchy_provider.resolve_scope(
                top=resolved_top,
                signal_path=signal,
            )
            target["lookup_wall_ms"] = round(
                (time.perf_counter() - lookup_started) * 1000.0,
                3,
            )
            target["resolution"] = _resolution_summary(
                resolution,
                signal=signal,
            )
        targets.append(target)

    return {
        "benchmark": BENCHMARK_NAME,
        "provider": "slang",
        "workload": _provider_workload(args),
        "hierarchy_wall_ms": round(hierarchy_wall_ms, 3),
        "targets": targets,
        "runtime": runtime.stats_snapshot(),
        "process_memory": _process_memory(),
    }


def run_slang(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_run_slang_async(args))


def _target_map(run: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    targets = run.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        resolution = target.get("resolution")
        if not isinstance(resolution, Mapping):
            continue
        key = resolution.get("target_sha256")
        if isinstance(key, str):
            result[key] = resolution
    return result


def compare_provider_runs(
    npi: Mapping[str, Any],
    slang: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare identity-free binding facts and classify every mismatch."""

    npi_targets = _target_map(npi)
    slang_targets = _target_map(slang)
    target_keys = sorted(set(npi_targets) | set(slang_targets))
    counts = {
        "target_missing_npi": 0,
        "target_missing_slang": 0,
        "status_mismatch": 0,
        "ancestor_count_mismatch": 0,
        "binding_missing_npi": 0,
        "binding_missing_slang": 0,
        "definition_mismatch": 0,
        "source_mismatch": 0,
        "source_line_mismatch": 0,
    }
    compared_bindings = 0
    for key in target_keys:
        left = npi_targets.get(key)
        right = slang_targets.get(key)
        if left is None:
            counts["target_missing_npi"] += 1
            continue
        if right is None:
            counts["target_missing_slang"] += 1
            continue
        if left.get("status") != right.get("status"):
            counts["status_mismatch"] += 1
        if left.get("ancestor_count") != right.get("ancestor_count"):
            counts["ancestor_count_mismatch"] += 1
        left_rows = {
            row["instance_path_sha256"]: row
            for row in left.get("binding_oracle", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("instance_path_sha256"), str)
        }
        right_rows = {
            row["instance_path_sha256"]: row
            for row in right.get("binding_oracle", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("instance_path_sha256"), str)
        }
        counts["binding_missing_npi"] += len(set(right_rows) - set(left_rows))
        counts["binding_missing_slang"] += len(set(left_rows) - set(right_rows))
        for path_key in set(left_rows) & set(right_rows):
            compared_bindings += 1
            left_row = left_rows[path_key]
            right_row = right_rows[path_key]
            if left_row.get("definition_sha256") != right_row.get(
                "definition_sha256"
            ):
                counts["definition_mismatch"] += 1
            if left_row.get("source_sha256") != right_row.get("source_sha256"):
                counts["source_mismatch"] += 1
            if left_row.get("source_line") != right_row.get("source_line"):
                counts["source_line_mismatch"] += 1
    return {
        "target_count": len(target_keys),
        "compared_binding_count": compared_bindings,
        "mismatch_counts": counts,
        "exact_binding_match": not any(counts.values()),
    }


def _child_command(args: argparse.Namespace, provider: str) -> list[str]:
    command = [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "--compile-log",
        os.fspath(args.compile_log),
        "--simulator",
        args.simulator,
        "--provider",
        provider,
        "--frontend-python",
        os.fspath(args.frontend_python),
        "--frontend-version",
        args.frontend_version,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--max-candidate-paths",
        str(args.max_candidate_paths),
        "--max-depth",
        str(args.max_depth),
    ]
    if args.top:
        command.extend(("--top", args.top))
    for signal in args.signal:
        command.extend(("--signal", signal))
    return command


def _run_child(args: argparse.Namespace, provider: str) -> dict[str, Any]:
    completed = subprocess.run(
        _child_command(args, provider),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BenchmarkInputError(
            f"{provider} provider child failed with status {completed.returncode}"
        )
    try:
        # Some NPI releases register a native C atexit banner after Python's
        # stdout redirection hooks. Decode the first JSON value and ignore
        # only that known trailing native output; benchmark facts stay intact.
        payload, _ = json.JSONDecoder().raw_decode(completed.stdout.lstrip())
    except json.JSONDecodeError as exc:
        raise BenchmarkInputError(
            f"{provider} provider child returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise BenchmarkInputError(f"{provider} provider child result is invalid")
    return payload


def run_compare(args: argparse.Namespace) -> dict[str, Any]:
    npi = _run_child(args, "npi")
    slang = _run_child(args, "slang")
    comparison = compare_provider_runs(npi, slang)
    return {
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
        },
        "workload": _provider_workload(args),
        "providers": {"npi": npi, "slang": slang},
        "comparison": comparison,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument(
        "--signal",
        action="append",
        required=True,
        help="Repeat for the cold target and different-scope targets.",
    )
    parser.add_argument("--top")
    parser.add_argument(
        "--simulator",
        choices=("auto", "vcs", "xcelium"),
        default="auto",
    )
    parser.add_argument(
        "--provider",
        choices=("compare", "npi", "slang"),
        default="compare",
    )
    parser.add_argument(
        "--frontend-python",
        type=Path,
        default=ROOT / ".venv/bin/python",
    )
    parser.add_argument("--frontend-version", default="11.0.0")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--max-candidate-paths", type=int, default=256)
    parser.add_argument("--max-depth", type=int, default=20)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    args.compile_log = args.compile_log.absolute()
    args.frontend_python = args.frontend_python.absolute()
    if not args.compile_log.is_file():
        raise BenchmarkInputError("compile log is unavailable")
    if not args.frontend_python.is_file():
        raise BenchmarkInputError("Source Graph frontend Python is unavailable")
    if (
        args.timeout_seconds <= 0
        or not 1 <= args.max_candidate_paths <= 1024
        or args.max_depth < 1
    ):
        raise BenchmarkInputError("provider budgets must be positive and bounded")
    if any(not signal or len(signal.split(".")) < 2 for signal in args.signal):
        raise BenchmarkInputError("each target signal must be a dotted path")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        _validate_args(args)
        if args.provider == "compare":
            result = run_compare(args)
        elif args.provider == "npi":
            result = run_npi(args)
        else:
            result = run_slang(args)
    except BenchmarkInputError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
