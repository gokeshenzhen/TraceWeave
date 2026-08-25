#!/usr/bin/env python3
"""A/B Source Graph semantic-design lifecycle strategies on a real SoC.

The benchmark compares four fresh-process strategies over the same ordered
target sequence:

* ``scoped``: today's one-shot worker and one compact IR per target scope.
* ``union``: one compact IR for the exact, already-known target union.
* ``persistent_parent``: one in-process Slang Compilation/root built from a
  bounded direct-child parent closure, followed by narrow per-target
  projections while the semantic session stays resident.
* ``compact_parent``: one-shot materialization of every admitted direct child
  under that parent (the compact-full-fragment counterfactual).

Only ``scoped`` is the production control. The other modes are development
prototypes and do not alter MCP routing. Every externally emitted target,
instance, definition, and source identity is SHA-256 hashed.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import gc
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

from scripts.benchmark_hierarchy_provider_soc import (  # noqa: E402
    BenchmarkInputError,
    _git_dirty,
    _git_head,
    _process_memory,
    _provider_workload,
    _resolution_summary,
    _sha256_json,
    _without_npi_hierarchy_overlay,
)
from src.hdl_suffixes import FRONTEND_HDL_SUFFIXES  # noqa: E402


BENCHMARK_NAME = "semantic_design_store_ab_v1"
MODES = ("scoped", "union", "persistent_parent", "compact_parent")


def _current_rss_kib() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _plan_shape(plan: Any) -> dict[str, Any]:
    if plan.request is None:
        blocker = plan.receipt.blocker
        return {
            "status": plan.status.value,
            "blocker": blocker.code if blocker is not None else None,
        }
    artifact = plan.request.artifact_identity
    projection = artifact.compile_projection
    return {
        "status": plan.status.value,
        "compile_input_count": (
            len(projection.ordered_inputs) if projection is not None else None
        ),
        "projection_instance_count": len(
            artifact.scope.projection_instance_paths
        ),
        "coverage_instance_count": len(
            artifact.scope.coverage_boundary.instance_paths
        ),
    }


def _ancestor_chain_to_parent(plan: Any, parent_scope: str) -> tuple[str, ...]:
    if plan.request is None:
        raise BenchmarkInputError("base Source Graph plan is unavailable")
    ancestors = plan.request.scope.hierarchy_ancestors
    try:
        end = ancestors.index(parent_scope) + 1
    except ValueError as exc:
        raise BenchmarkInputError(
            "parent scope is not on the first target ancestor chain"
        ) from exc
    return ancestors[:end]


def _lca(chains: Sequence[tuple[str, ...]]) -> str:
    if not chains:
        raise BenchmarkInputError("target hierarchy chains are unavailable")
    common = chains[0]
    for chain in chains[1:]:
        count = 0
        for left, right in zip(common, chain):
            if left != right:
                break
            count += 1
        common = common[:count]
    if not common:
        raise BenchmarkInputError("targets do not share a hierarchy top")
    return common[-1]


async def _prepare_plans(args: argparse.Namespace) -> dict[str, Any]:
    import server  # noqa: PLC0415
    from src.hierarchy_provider import LexicalHierarchyProvider  # noqa: PLC0415
    from src.source_graph_adapter import (  # noqa: PLC0415
        _expanded_single_endpoint_plan,
        build_source_graph_plan,
        build_source_graph_trace_plan,
    )
    from src.source_graph_contract import QueryOperation  # noqa: PLC0415

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

    common = {
        "compile_log": os.fspath(args.compile_log),
        "compile_result": compile_result,
        "hierarchy_result": hierarchy,
        "hierarchy_snapshot_sha256": snapshot,
        "top_hint": args.top,
        "max_hops": args.max_depth,
        "frontend_version": args.frontend_version,
    }
    exact = tuple(
        build_source_graph_plan(
            **common,
            operation=QueryOperation.DRIVER,
            signal_path=signal,
            recursive=True,
            include_expr=True,
            kind_filter=(),
        )
        for signal in args.signal
    )
    if any(plan.request is None for plan in exact):
        raise BenchmarkInputError("one or more exact Source Graph plans are blocked")
    union = build_source_graph_trace_plan(
        **common,
        signal_paths=args.signal,
    )
    if union.request is None:
        raise BenchmarkInputError("target-union Source Graph plan is blocked")

    chains = tuple(plan.request.scope.hierarchy_ancestors for plan in exact)
    parent_scope = args.parent_scope or _lca(chains)
    parent_chain = _ancestor_chain_to_parent(exact[0], parent_scope)
    children = LexicalHierarchyProvider(hierarchy).direct_children(
        top=exact[0].request.scope.top,
        instance_path=parent_scope,
        max_children=args.parent_max_children,
    )
    if children is None:
        raise BenchmarkInputError("parent direct children are unavailable")
    if children.truncated:
        raise BenchmarkInputError("parent direct children exceed the benchmark cap")
    if not children.paths:
        raise BenchmarkInputError("parent scope has no proved direct children")
    compact_parent = _expanded_single_endpoint_plan(
        base=exact[0],
        hierarchy_result=hierarchy,
        parent_chains=(parent_chain,),
        candidate_paths=children.paths,
        scope_expansion_anchors=(f"{parent_scope}.__semantic_session__",),
    )
    if compact_parent.request is None:
        raise BenchmarkInputError("parent-expanded Source Graph plan is blocked")
    broad_projection = compact_parent.request.artifact_identity.compile_projection
    if broad_projection is None:
        raise BenchmarkInputError("parent-expanded compile closure is unavailable")
    if len(broad_projection.ordered_inputs) > args.parent_max_inputs:
        raise BenchmarkInputError("parent compile closure exceeds the input cap")

    # The semantic-session request parses the broader proved parent closure but
    # publishes only the first exact scope. This identity is benchmark-local;
    # production adoption requires a versioned adapter receipt and runner.
    persistent_artifact = replace(
        exact[0].request.artifact_identity,
        compile_projection=broad_projection,
    )
    persistent_request = replace(
        exact[0].request,
        artifact=persistent_artifact,
    )
    return {
        "hierarchy_wall_ms": hierarchy_wall_ms,
        "hierarchy": hierarchy,
        "snapshot": snapshot,
        "exact": exact,
        "union": union,
        "compact_parent": compact_parent,
        "persistent_request": persistent_request,
        "parent_scope": parent_scope,
        "parent_child_count": len(children.paths),
    }


def _resolution_from_entry(entry: Any, *, top: str, signal: str) -> dict[str, Any]:
    started = time.perf_counter()
    resolution = entry.hierarchy_provider.resolve_scope(
        top=top,
        signal_path=signal,
    )
    return {
        "lookup_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "resolution": _resolution_summary(resolution, signal=signal),
        "artifact": _ir_fact_summary(entry.ir),
    }


def _ir_fact_summary(ir: Any) -> dict[str, Any]:
    """Hash connectivity facts while retaining only fixed/count coverage facts."""

    payload = ir.to_dict()
    semantic_payload = {
        key: payload[key]
        for key in ("definitions", "instances", "bindings", "top_instances")
    }
    coverage = ir.coverage
    return {
        "connectivity_oracle_sha256": _sha256_json(semantic_payload),
        "stats": ir.stats(),
        "coverage": {
            "status": coverage.status.value,
            "files_total": coverage.files_total,
            "files_projected": coverage.files_projected,
            "diagnostic_count": coverage.diagnostic_count,
            "blocking_diagnostic_count": coverage.blocking_diagnostic_count,
            "gap_codes": sorted({gap.code for gap in coverage.gaps}),
        },
    }


async def _run_runtime_mode(
    args: argparse.Namespace,
    plans: Mapping[str, Any],
) -> dict[str, Any]:
    from src.source_graph_runtime import (  # noqa: PLC0415
        IsolatedSourceGraphProcessRunner,
        SourceGraphRuntime,
    )

    runtime = SourceGraphRuntime(
        IsolatedSourceGraphProcessRunner(
            python_executable=os.fspath(args.frontend_python)
        )
    )
    if args.mode == "scoped":
        selected = plans["exact"]
    elif args.mode == "union":
        selected = (plans["union"],)
    elif args.mode == "compact_parent":
        selected = (plans["compact_parent"],)
    else:
        raise BenchmarkInputError("invalid one-shot benchmark mode")

    outcomes: list[dict[str, Any]] = []
    entries: list[Any] = []
    for plan in selected:
        started = time.perf_counter()
        outcome = await runtime.prepare(
            plan.request,
            timeout_seconds=args.timeout_seconds,
        )
        outcomes.append(
            {
                "prepare_wall_ms": round(
                    (time.perf_counter() - started) * 1000.0,
                    3,
                ),
                "prepare_status": outcome.status.value,
                "metrics": outcome.metrics.to_dict(),
            }
        )
        if outcome.entry is None:
            raise BenchmarkInputError(f"{args.mode} frontend prepare failed")
        entries.append(outcome.entry)

    targets: list[dict[str, Any]] = []
    for index, signal in enumerate(args.signal):
        entry = entries[index] if args.mode == "scoped" else entries[0]
        targets.append(
            _resolution_from_entry(
                entry,
                top=plans["exact"][index].request.scope.top,
                signal=signal,
            )
        )
    return {
        "prepares": outcomes,
        "targets": targets,
        "runtime": runtime.stats_snapshot(),
        "process_memory": _process_memory(),
    }


def _projection_options(
    *,
    request: Any,
    diagnostic_payload: Mapping[str, Any],
    files_projected: int,
) -> Any:
    from src.connectivity_ir import CoverageStatus  # noqa: PLC0415
    from src.slang_connectivity_projector import (  # noqa: PLC0415
        ProjectionExclusion,
        ProjectionOptions,
    )
    from src.source_graph_worker import _projection_diagnostics  # noqa: PLC0415

    source_root = Path.cwd()
    scope = request.artifact_identity.scope
    exclusions = [
        ProjectionExclusion(
            code=code,
            message=f"objective exclusion retained by build contract: {code}",
            impact=CoverageStatus.INCONCLUSIVE,
            scopes=("*",),
            constructs=(code,),
        )
        for code in scope.coverage_boundary.objective_exclusions
    ]
    return ProjectionOptions(
        source_root=source_root,
        files_total=len(request.identity.compile_inputs.ordered_inputs),
        files_projected=files_projected,
        diagnostics=_projection_diagnostics(diagnostic_payload, source_root),
        diagnostic_total=int(diagnostic_payload["total"]),
        blocking_diagnostic_total=int(
            diagnostic_payload["blocking_error_count"]
        ),
        exclusions=tuple(exclusions),
        focus_instance_paths=scope.coverage_boundary.instance_paths,
        assignment_instance_paths=scope.projection_instance_paths,
        metadata=(
            ("runtime", "persistent_parent_prototype"),
            ("scope_contract", request.contract_version),
        ),
    )


def _run_persistent_parent(
    args: argparse.Namespace,
    plans: Mapping[str, Any],
) -> dict[str, Any]:
    from pyslang import driver as driver_module  # noqa: PLC0415
    from scripts.spike_source_frontend import (  # noqa: PLC0415
        _configure_driver,
        _diagnostics_payload,
        _location,
    )
    from src.hierarchy_provider import (  # noqa: PLC0415
        ConnectivityIRHierarchyProvider,
    )
    from src.slang_connectivity_projector import (  # noqa: PLC0415
        project_slang_design,
    )
    from src.source_graph_worker import _frontend_args  # noqa: PLC0415

    session_request = plans["persistent_request"]
    artifact_request = session_request.artifact_build_request
    projection = artifact_request.identity.compile_projection
    if projection is None:
        raise BenchmarkInputError("persistent session compile closure is missing")
    rss_start_kib = _current_rss_kib()
    phases: dict[str, float] = {}
    started = time.perf_counter()
    driver = _configure_driver(driver_module, _frontend_args(artifact_request))
    phases["configure_wall_ms"] = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    if not driver.parseAllSources():
        raise BenchmarkInputError("persistent session parse failed")
    phases["parse_wall_ms"] = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    compilation = driver.createCompilation()
    phases["compilation_wall_ms"] = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    root = compilation.getRoot()
    phases["elaboration_wall_ms"] = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    diagnostics = list(compilation.getAllDiagnostics())
    diagnostic_payload = _diagnostics_payload(driver, diagnostics)
    phases["diagnostics_wall_ms"] = (time.perf_counter() - started) * 1000.0
    rss_after_session_kib = _current_rss_kib()

    targets: list[dict[str, Any]] = []
    ir_bytes_total = 0
    for exact_plan, signal in zip(plans["exact"], args.signal):
        exact_projection = exact_plan.request.artifact_identity.compile_projection
        exact_inputs = (
            exact_projection.ordered_inputs
            if exact_projection is not None
            else exact_plan.request.identity.compile_inputs.ordered_inputs
        )
        exact_frontend_inputs = {
            os.fspath(Path(path).resolve(strict=False))
            for path in exact_inputs
            if Path(path).suffix.lower() in FRONTEND_HDL_SUFFIXES
        }
        exact_diagnostics = []
        for diagnostic in diagnostics:
            location = _location(driver.sourceManager, diagnostic.location)
            file_name = location.get("file")
            if file_name is None or os.fspath(
                Path(str(file_name)).resolve(strict=False)
            ) in exact_frontend_inputs:
                exact_diagnostics.append(diagnostic)
        exact_diagnostic_payload = _diagnostics_payload(
            driver,
            exact_diagnostics,
        )
        started = time.perf_counter()
        projected = project_slang_design(
            root=root,
            source_manager=driver.sourceManager,
            frontend_version=args.frontend_version,
            options=_projection_options(
                request=exact_plan.request,
                diagnostic_payload=exact_diagnostic_payload,
                files_projected=len(exact_frontend_inputs),
            ),
        )
        projection_wall_ms = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        serialized = projected.ir.to_json_bytes()
        serialization_wall_ms = (time.perf_counter() - started) * 1000.0
        ir_bytes_total += len(serialized)
        provider = ConnectivityIRHierarchyProvider(
            projected.ir,
            design_identity="persistent_parent_prototype",
        )
        started = time.perf_counter()
        resolution = provider.resolve_scope(
            top=exact_plan.request.scope.top,
            signal_path=signal,
        )
        lookup_wall_ms = (time.perf_counter() - started) * 1000.0
        targets.append(
            {
                "projection_wall_ms": round(projection_wall_ms, 3),
                "serialization_wall_ms": round(serialization_wall_ms, 3),
                "ir_bytes": len(serialized),
                "lookup_wall_ms": round(lookup_wall_ms, 3),
                "resolution": _resolution_summary(resolution, signal=signal),
                "artifact": _ir_fact_summary(projected.ir),
            }
        )
        del provider, serialized, projected
        gc.collect()

    return {
        "session": {
            **{name: round(value, 3) for name, value in phases.items()},
            "compile_input_count": len(projection.ordered_inputs),
            "diagnostic_count": int(diagnostic_payload["total"]),
            "rss_start_kib": rss_start_kib,
            "rss_after_session_kib": rss_after_session_kib,
            "rss_end_kib": _current_rss_kib(),
            "peak_rss_kib": int(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            ),
            "serialized_ir_bytes_total": ir_bytes_total,
        },
        "targets": targets,
        "process_memory": _process_memory(),
    }


async def run_mode_async(args: argparse.Namespace) -> dict[str, Any]:
    plans = await _prepare_plans(args)
    if args.mode == "persistent_parent":
        measured = _run_persistent_parent(args, plans)
    else:
        measured = await _run_runtime_mode(args, plans)
    return {
        "benchmark": BENCHMARK_NAME,
        "mode": args.mode,
        "workload": {
            **_provider_workload(args),
            "parent_scope_depth": len(plans["parent_scope"].split(".")),
            "parent_child_count": plans["parent_child_count"],
        },
        "hierarchy_wall_ms": round(plans["hierarchy_wall_ms"], 3),
        "plans": {
            "exact": [_plan_shape(plan) for plan in plans["exact"]],
            "union": _plan_shape(plans["union"]),
            "persistent_parent": _plan_shape(
                replace(
                    plans["exact"][0],
                    request=plans["persistent_request"],
                )
            ),
            "compact_parent": _plan_shape(plans["compact_parent"]),
        },
        "measurement": measured,
    }


def run_mode(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_mode_async(args))


def _target_oracles(run: Mapping[str, Any]) -> list[str]:
    measurement = run.get("measurement")
    if not isinstance(measurement, Mapping):
        return []
    targets = measurement.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return []
    result: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        resolution = target.get("resolution")
        if isinstance(resolution, Mapping):
            digest = resolution.get("binding_oracle_sha256")
            if isinstance(digest, str):
                result.append(digest)
    return result


def _target_connectivity_oracles(run: Mapping[str, Any]) -> list[str]:
    measurement = run.get("measurement")
    if not isinstance(measurement, Mapping):
        return []
    targets = measurement.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return []
    result: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        artifact = target.get("artifact")
        if not isinstance(artifact, Mapping):
            continue
        digest = artifact.get("connectivity_oracle_sha256")
        if isinstance(digest, str):
            result.append(digest)
    return result


def compare_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_mode = {str(run.get("mode")): _target_oracles(run) for run in runs}
    oracle_sets = {tuple(value) for value in by_mode.values()}
    connectivity_by_mode = {
        str(run.get("mode")): _target_connectivity_oracles(run) for run in runs
    }
    narrow_connectivity = {
        mode: connectivity_by_mode.get(mode, [])
        for mode in ("scoped", "persistent_parent")
    }
    narrow_connectivity_sets = {
        tuple(value) for value in narrow_connectivity.values()
    }
    comparison_payload = {
        "binding": by_mode,
        "connectivity": connectivity_by_mode,
    }
    return {
        "mode_count": len(by_mode),
        "binding_oracles_by_mode": by_mode,
        "all_binding_oracles_equal": len(oracle_sets) == 1 and bool(oracle_sets),
        "connectivity_oracles_by_mode": connectivity_by_mode,
        "scoped_persistent_connectivity_equal": (
            len(narrow_connectivity_sets) == 1
            and bool(narrow_connectivity_sets)
        ),
        "connectivity_equivalence_scope": "scoped_vs_persistent_parent",
        "comparison_sha256": _sha256_json(comparison_payload),
    }


def _number(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _strategy_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a strategy run to the resource and latency decision facts.

    Compare mode intentionally omits full worker receipts by default. Besides
    making the benchmark convenient on a large SoC, this prevents changes in
    verbose internal metrics from obscuring the stable A/B decision surface.
    """

    mode = str(run.get("mode"))
    measurement = run.get("measurement")
    if not isinstance(measurement, Mapping):
        measurement = {}
    plans = run.get("plans")
    if not isinstance(plans, Mapping):
        plans = {}
    process_memory = measurement.get("process_memory")
    if not isinstance(process_memory, Mapping):
        process_memory = {}
    base: dict[str, Any] = {
        "mode": mode,
        "hierarchy_wall_ms": _number(run.get("hierarchy_wall_ms")),
        "binding_oracle_sha256": _target_oracles(run),
        "connectivity_oracle_sha256": _target_connectivity_oracles(run),
        "plans": plans,
        "process_self_peak_rss_kib": process_memory.get("self_peak_rss_kib"),
        "process_child_peak_rss_kib": process_memory.get("child_peak_rss_kib"),
    }

    targets = measurement.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        targets = []
    lookup_wall_ms = [
        _number(target.get("lookup_wall_ms"))
        for target in targets
        if isinstance(target, Mapping)
    ]
    base["target_lookup_wall_ms"] = lookup_wall_ms
    base["target_coverage"] = [
        target.get("artifact", {}).get("coverage")
        for target in targets
        if isinstance(target, Mapping)
        and isinstance(target.get("artifact"), Mapping)
    ]

    if mode == "persistent_parent":
        session = measurement.get("session")
        if not isinstance(session, Mapping):
            session = {}
        session_build_wall_ms = sum(
            _number(session.get(name))
            for name in (
                "configure_wall_ms",
                "parse_wall_ms",
                "compilation_wall_ms",
                "elaboration_wall_ms",
                "diagnostics_wall_ms",
            )
        )
        projection_wall_ms: list[float] = []
        serialization_wall_ms: list[float] = []
        ready_after_ms: list[float] = []
        cumulative = session_build_wall_ms
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            projection = _number(target.get("projection_wall_ms"))
            serialization = _number(target.get("serialization_wall_ms"))
            projection_wall_ms.append(projection)
            serialization_wall_ms.append(serialization)
            cumulative += projection + serialization
            ready_after_ms.append(round(cumulative, 3))
        base.update(
            {
                "compile_input_count": session.get("compile_input_count"),
                "semantic_session_build_wall_ms": round(
                    session_build_wall_ms, 3
                ),
                "projection_wall_ms": projection_wall_ms,
                "serialization_wall_ms": serialization_wall_ms,
                "target_ready_after_ms": ready_after_ms,
                "frontend_launch_count": 1,
                "serialized_ir_bytes_total": session.get(
                    "serialized_ir_bytes_total"
                ),
                "rss_start_kib": session.get("rss_start_kib"),
                "rss_after_session_kib": session.get(
                    "rss_after_session_kib"
                ),
                "rss_end_kib": session.get("rss_end_kib"),
                "max_worker_peak_rss_kib": session.get("peak_rss_kib"),
            }
        )
        return base

    prepares = measurement.get("prepares")
    if not isinstance(prepares, Sequence) or isinstance(prepares, (str, bytes)):
        prepares = []
    prepare_wall_ms: list[float] = []
    build_wall_ms = 0.0
    load_wall_ms = 0.0
    frontend_launch_count = 0
    serialized_ir_bytes_total = 0
    worker_peaks: list[float] = []
    for prepare in prepares:
        if not isinstance(prepare, Mapping):
            continue
        prepare_wall_ms.append(_number(prepare.get("prepare_wall_ms")))
        metrics = prepare.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        build_wall_ms += _number(metrics.get("build_wall_ms"))
        load_wall_ms += _number(metrics.get("load_wall_ms"))
        frontend_launch_count += int(_number(metrics.get("frontend_launch_count")))
        serialized_ir_bytes_total += int(_number(metrics.get("ir_bytes")))
        peak = metrics.get("rss_peak_kib")
        if isinstance(peak, (int, float)) and not isinstance(peak, bool):
            worker_peaks.append(float(peak))
    if len(prepare_wall_ms) == len(targets):
        cumulative = 0.0
        target_ready_after_ms = []
        for wall_ms in prepare_wall_ms:
            cumulative += wall_ms
            target_ready_after_ms.append(round(cumulative, 3))
    else:
        shared_ready = round(sum(prepare_wall_ms), 3)
        target_ready_after_ms = [shared_ready for _ in targets]
    runtime = measurement.get("runtime")
    if not isinstance(runtime, Mapping):
        runtime = {}
    base.update(
        {
            "prepare_wall_ms": prepare_wall_ms,
            "aggregate_prepare_wall_ms": round(sum(prepare_wall_ms), 3),
            "aggregate_build_wall_ms": round(build_wall_ms, 3),
            "aggregate_load_wall_ms": round(load_wall_ms, 3),
            "target_ready_after_ms": target_ready_after_ms,
            "frontend_launch_count": frontend_launch_count,
            "serialized_ir_bytes_total": serialized_ir_bytes_total,
            "retained_ir_bytes": runtime.get("cache_bytes"),
            "max_worker_peak_rss_kib": (
                int(max(worker_peaks)) if worker_peaks else None
            ),
        }
    )
    return base


def _child_command(args: argparse.Namespace, mode: str) -> list[str]:
    command = [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "--compile-log",
        os.fspath(args.compile_log),
        "--simulator",
        args.simulator,
        "--mode",
        mode,
        "--frontend-python",
        os.fspath(args.frontend_python),
        "--frontend-version",
        args.frontend_version,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--max-depth",
        str(args.max_depth),
        "--parent-max-children",
        str(args.parent_max_children),
        "--parent-max-inputs",
        str(args.parent_max_inputs),
    ]
    if args.top:
        command.extend(("--top", args.top))
    if args.parent_scope:
        command.extend(("--parent-scope", args.parent_scope))
    for signal in args.signal:
        command.extend(("--signal", signal))
    return command


def _run_child(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    completed = subprocess.run(
        _child_command(args, mode),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BenchmarkInputError(
            f"semantic store {mode} child failed with status "
            f"{completed.returncode}"
        )
    try:
        payload, _ = json.JSONDecoder().raw_decode(completed.stdout.lstrip())
    except json.JSONDecodeError as exc:
        raise BenchmarkInputError(
            f"semantic store {mode} child returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise BenchmarkInputError(f"semantic store {mode} result is invalid")
    return payload


def run_compare(args: argparse.Namespace) -> dict[str, Any]:
    runs = [_run_child(args, mode) for mode in MODES]
    result = {
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
        },
        "workload": _provider_workload(args),
        "strategies": [_strategy_summary(run) for run in runs],
        "comparison": compare_runs(runs),
    }
    if args.include_details:
        result["runs"] = runs
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument("--signal", action="append", required=True)
    parser.add_argument("--top")
    parser.add_argument("--parent-scope")
    parser.add_argument(
        "--simulator",
        choices=("auto", "vcs", "xcelium"),
        default="auto",
    )
    parser.add_argument("--mode", choices=("compare", *MODES), default="compare")
    parser.add_argument(
        "--frontend-python",
        type=Path,
        default=ROOT / ".venv/bin/python",
    )
    parser.add_argument("--frontend-version", default="11.0.0")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--max-depth", type=int, default=20)
    parser.add_argument("--parent-max-children", type=int, default=64)
    parser.add_argument("--parent-max-inputs", type=int, default=256)
    parser.add_argument(
        "--include-details",
        action="store_true",
        help="include verbose per-worker receipts in compare mode",
    )
    # Compatibility with the shared identity-safe workload helper.
    parser.set_defaults(max_candidate_paths=256)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    args.compile_log = args.compile_log.absolute()
    args.frontend_python = args.frontend_python.absolute()
    if not args.compile_log.is_file():
        raise BenchmarkInputError("compile log is unavailable")
    if not args.frontend_python.is_file():
        raise BenchmarkInputError("Source Graph frontend Python is unavailable")
    if len(args.signal) < 2:
        raise BenchmarkInputError("at least two target signals are required")
    if any(not signal or len(signal.split(".")) < 2 for signal in args.signal):
        raise BenchmarkInputError("each target signal must be a dotted path")
    if (
        args.timeout_seconds <= 0
        or args.max_depth < 1
        or not 1 <= args.parent_max_children <= 256
        or not 1 <= args.parent_max_inputs <= 1024
    ):
        raise BenchmarkInputError("semantic store budgets must be bounded")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        _validate_args(args)
        result = run_compare(args) if args.mode == "compare" else run_mode(args)
    except BenchmarkInputError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
