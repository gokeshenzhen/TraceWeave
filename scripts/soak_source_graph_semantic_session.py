#!/usr/bin/env python3
"""A/B the bounded Source Graph semantic session on an eligible SoC workload.

The query list is external so this harness contains no project-specific signal,
scope, source, or build-layout identity.  ``compare`` launches two fresh child
processes over the same ordered deep driver/load sequence:

* ``one_shot`` reproduces the current default narrow-plan worker lifecycle.
* ``persistent`` uses the production bounded semantic-context planner and
  :class:`PersistentSourceGraphProcessRunner`.

The emitted report contains only hashes, fixed status labels, counts, timings,
and resource aggregates.  Passing this single-design implementation gate never
authorizes default-on by itself: representative multi-design and operational
query-frequency evidence are still required before accepting the retained RSS
trade-off.  A smaller design may already fit the bounded adjacent artifact; in
that case the report classifies the workload as ``not_needed`` and deliberately
does not mislabel ordinary compact-IR cache hits as semantic-session reuse.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
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

from scripts.benchmark_source_graph_soc import (  # noqa: E402
    DEFAULT_FRONTEND_PYTHON,
    _distribution,
    _git_head,
    _write_json_atomic,
)


SOAK_NAME = "source_graph_semantic_session_soc_v1"
SCHEMA_VERSION = "1.0"
QUERY_SPEC_VERSION = "1.0"
MODES = ("one_shot", "persistent")
MIN_OPERATIONAL_QUERIES = 20
MAX_OPERATIONAL_QUERIES = 100
MAX_SIGNAL_PATH_CHARS = 4096
MAX_QUERY_DEPTH = 64

MIN_SEQUENCE_REDUCTION_PERCENT = 20.0
MAX_FIRST_QUERY_REGRESSION_PERCENT = 15.0
MAX_BREAK_EVEN_ORDINAL = 3
MAX_TAIL_PREPARE_P95_MS = 3_000.0
MAX_SESSION_RSS_GROWTH_KIB = 64 * 1024

_FIXED_RUNTIME_FIELDS = (
    "actual_build_count",
    "frontend_launch_count",
    "semantic_session_hit_count",
    "semantic_session_miss_count",
    "semantic_session_restart_count",
    "semantic_session_eviction_count",
    "cache_hit_count",
    "cache_miss_count",
    "cache_eviction_count",
    "cache_peak_entry_count",
    "cache_peak_bytes",
    "cache_entry_count",
    "cache_bytes",
    "timeout_count",
    "worker_failure_count",
)
_FORBIDDEN_REPORT_KEY_TOKENS = {
    "compile_log",
    "query_spec",
    "signal_path",
    "source_path",
    "scope_path",
    "instance_path",
    "cache_root",
    "exception",
    "message",
}


class SoakInputError(ValueError):
    """Raised when an operational soak is ambiguous or unsafe to compare."""


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _positive_int(value: Any, *, label: str, minimum: int = 1) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > MAX_QUERY_DEPTH
    ):
        raise SoakInputError(f"{label} must be a bounded positive integer")
    return value


def _signal_path(value: Any) -> str:
    if not isinstance(value, str):
        raise SoakInputError("query signal_path must be a string")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or "." not in normalized
        or len(normalized) > MAX_SIGNAL_PATH_CHARS
        or "\x00" in normalized
        or "*" in normalized
    ):
        raise SoakInputError("query signal_path must be a bounded exact path")
    return normalized


def _normalize_query(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SoakInputError("each query must be an object")
    operation = value.get("operation")
    allowed = {"name", "operation", "signal_path", "max_depth", "recursive"}
    if set(value) - allowed:
        raise SoakInputError("query contains unsupported fields")
    signal = _signal_path(value.get("signal_path"))
    max_depth = _positive_int(
        value.get("max_depth", 10 if operation == "driver" else 2),
        label="query max_depth",
    )
    if operation == "driver":
        recursive = value.get("recursive", True)
        if recursive is not True:
            raise SoakInputError("semantic-session driver queries must be recursive")
    elif operation == "loads":
        if "recursive" in value:
            raise SoakInputError("load queries do not accept recursive")
        if max_depth < 2:
            raise SoakInputError("semantic-session load max_depth must be at least 2")
        recursive = False
    else:
        raise SoakInputError("query operation must be driver or loads")
    name = value.get("name")
    if name is not None and (
        not isinstance(name, str) or not name.strip() or len(name) > 128
    ):
        raise SoakInputError("query name must be a bounded non-empty string")
    return {
        "operation": operation,
        "signal_path": signal,
        "max_depth": max_depth,
        "recursive": recursive,
    }


def load_query_spec(
    path: Path,
    *,
    minimum_queries: int = MIN_OPERATIONAL_QUERIES,
) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SoakInputError("query spec is unavailable or invalid") from exc
    if not isinstance(payload, Mapping):
        raise SoakInputError("query spec must be an object")
    if set(payload) != {"schema_version", "queries"}:
        raise SoakInputError("query spec fields do not match its schema")
    if payload.get("schema_version") != QUERY_SPEC_VERSION:
        raise SoakInputError("query spec version is unsupported")
    rows = payload.get("queries")
    if not isinstance(rows, list) or not (
        minimum_queries <= len(rows) <= MAX_OPERATIONAL_QUERIES
    ):
        raise SoakInputError(
            f"query spec must contain {minimum_queries} to "
            f"{MAX_OPERATIONAL_QUERIES} queries"
        )
    queries = tuple(_normalize_query(row) for row in rows)
    identities = tuple(_sha256_json(query) for query in queries)
    if len(set(identities)) != len(identities):
        raise SoakInputError("query spec contains duplicate semantic queries")
    return queries


def _query_identity(query: Mapping[str, Any]) -> str:
    return _sha256_json(dict(query))


def _result_status(operation: str, result: Mapping[str, Any]) -> str:
    if operation == "driver":
        return str(result.get("driver_status") or "unknown")
    loads = result.get("loads")
    return "found" if isinstance(loads, list) and loads else "not_found"


def _result_fact_sha256(result: Mapping[str, Any]) -> str:
    return _sha256_json(
        {
            key: value
            for key, value in result.items()
            if not str(key).startswith("_") and key != "backend"
        }
    )


def _coverage_semantics_sha256(receipt: Mapping[str, Any]) -> str:
    fields = (
        "status",
        "coverage_status",
        "unresolved_boundary_codes",
        "resolved_bit_count",
        "unresolved_bit_count",
        "constant_bit_count",
        "multi_driver_bit_count",
        "traversal_truncated",
        "output_truncated",
    )
    return _sha256_json({name: receipt.get(name) for name in fields})


def _runtime_summary(runtime: Mapping[str, Any]) -> dict[str, int | float]:
    return {
        name: runtime.get(name, 0)
        for name in _FIXED_RUNTIME_FIELDS
    }


async def _prepare_hierarchy(args: argparse.Namespace) -> tuple[dict, str, float]:
    import server  # noqa: PLC0415

    previous = os.environ.get("TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY")
    os.environ["TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"] = "off"
    started = time.perf_counter()
    try:
        await server._dispatch(
            "build_tb_hierarchy",
            {
                "compile_log": os.fspath(args.compile_log),
                "simulator": args.simulator,
            },
        )
    finally:
        if previous is None:
            os.environ.pop("TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY", None)
        else:
            os.environ["TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"] = previous
    wall_ms = (time.perf_counter() - started) * 1000.0
    hierarchy, snapshot = server._resolve_hierarchy_context(
        os.fspath(args.compile_log), args.simulator
    )
    if not isinstance(hierarchy.get("compile_result"), Mapping):
        raise SoakInputError("hierarchy lacks compile context")
    return hierarchy, snapshot, wall_ms


def _build_plan(
    args: argparse.Namespace,
    *,
    hierarchy: Mapping[str, Any],
    snapshot: str,
    query: Mapping[str, Any],
    semantic_session: bool,
) -> Any:
    from src.source_graph_adapter import (  # noqa: PLC0415
        build_source_graph_initial_plan,
    )
    from src.source_graph_contract import QueryOperation  # noqa: PLC0415

    operation = query["operation"]
    return build_source_graph_initial_plan(
        compile_log=os.fspath(args.compile_log),
        compile_result=hierarchy["compile_result"],
        hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=snapshot,
        operation=(
            QueryOperation.DRIVER
            if operation == "driver"
            else QueryOperation.LOADS
        ),
        signal_path=query["signal_path"],
        top_hint=args.top,
        max_hops=query["max_depth"],
        frontend_version=args.frontend_version,
        recursive=query["recursive"],
        include_expr=True,
        kind_filter=(),
        enable_semantic_context=semantic_session,
        semantic_context_max_instances=args.semantic_max_instances,
        semantic_context_max_inputs=args.semantic_max_inputs,
    )


def _query_entry(
    args: argparse.Namespace,
    *,
    entry: Any,
    query: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    from src.source_graph_backend import (  # noqa: PLC0415
        SourceGraphConnectivityBackend,
    )

    backend = SourceGraphConnectivityBackend(entry)
    started = time.perf_counter()
    if query["operation"] == "driver":
        result = backend.find_driver(
            query["signal_path"],
            "",
            os.fspath(args.compile_log),
            top_hint=args.top,
            recursive=True,
            max_depth=query["max_depth"],
            simulator=args.simulator,
        )
    else:
        result = backend.find_loads(
            query["signal_path"],
            os.fspath(args.compile_log),
            top_hint=args.top,
            max_depth=query["max_depth"],
            include_expr=True,
            kind_filter=None,
            simulator=args.simulator,
        )
    return result, (time.perf_counter() - started) * 1000.0


async def _run_mode_async(args: argparse.Namespace) -> dict[str, Any]:
    from src.source_graph_runtime import (  # noqa: PLC0415
        IsolatedSourceGraphProcessRunner,
        SourceGraphRuntime,
    )
    from src.source_graph_session_runtime import (  # noqa: PLC0415
        PersistentSourceGraphProcessRunner,
    )

    queries = load_query_spec(args.query_spec)
    hierarchy, snapshot, hierarchy_wall_ms = await _prepare_hierarchy(args)
    semantic_session = args.child_mode == "persistent"
    one_shot = IsolatedSourceGraphProcessRunner(
        python_executable=args.frontend_python,
    )
    persistent = (
        PersistentSourceGraphProcessRunner(
            python_executable=args.frontend_python,
            idle_ttl_seconds=args.semantic_idle_ttl,
            max_rss_kib=args.semantic_max_rss_kib,
            one_shot_runner=one_shot,
        )
        if semantic_session
        else None
    )
    runtime = SourceGraphRuntime(persistent or one_shot)
    rows: list[dict[str, Any]] = []
    cumulative_ms = 0.0
    sequence_started = time.perf_counter()
    try:
        for ordinal, query in enumerate(queries):
            plan_started = time.perf_counter()
            plan = _build_plan(
                args,
                hierarchy=hierarchy,
                snapshot=snapshot,
                query=query,
                semantic_session=semantic_session,
            )
            plan_wall_ms = (time.perf_counter() - plan_started) * 1000.0
            if plan.request is None:
                raise SoakInputError("one or more Source Graph plans are blocked")
            prepare_started = time.perf_counter()
            outcome = await runtime.prepare(
                plan.request,
                timeout_seconds=args.timeout_seconds,
            )
            prepare_wall_ms = (time.perf_counter() - prepare_started) * 1000.0
            if outcome.entry is None:
                raise SoakInputError("one or more Source Graph prepares failed")
            result, query_wall_ms = _query_entry(
                args,
                entry=outcome.entry,
                query=query,
            )
            receipt = result.get("_source_graph_query_receipt")
            if not isinstance(receipt, Mapping):
                raise SoakInputError("Source Graph query receipt is unavailable")
            operation_wall_ms = plan_wall_ms + prepare_wall_ms + query_wall_ms
            cumulative_ms += operation_wall_ms
            match_count = int(receipt.get("match_count") or 0)
            metrics = outcome.metrics
            rows.append(
                {
                    "ordinal": ordinal,
                    "query_sha256": _query_identity(query),
                    "operation": query["operation"],
                    "semantic_context_status": (
                        plan.receipt.semantic_context_status
                    ),
                    "semantic_context_instance_count": (
                        plan.receipt.semantic_context_instance_count
                    ),
                    "semantic_context_input_count": (
                        plan.receipt.semantic_context_input_count
                    ),
                    "prepare_status": outcome.status.value,
                    "result_status": _result_status(query["operation"], result),
                    "positive_fact": match_count > 0,
                    "match_count": match_count,
                    "coverage_status": receipt.get("coverage_status"),
                    "fact_sha256": _result_fact_sha256(result),
                    "coverage_semantics_sha256": (
                        _coverage_semantics_sha256(receipt)
                    ),
                    "plan_wall_ms": round(plan_wall_ms, 3),
                    "prepare_wall_ms": round(prepare_wall_ms, 3),
                    "query_wall_ms": round(query_wall_ms, 3),
                    "operation_wall_ms": round(operation_wall_ms, 3),
                    "cumulative_wall_ms": round(cumulative_ms, 3),
                    "frontend_launch_count": metrics.frontend_launch_count,
                    "semantic_session_hit_count": (
                        metrics.semantic_session_hit_count
                    ),
                    "semantic_session_miss_count": (
                        metrics.semantic_session_miss_count
                    ),
                    "semantic_session_restart_count": (
                        metrics.semantic_session_restart_count
                    ),
                    "semantic_session_eviction_count": (
                        metrics.semantic_session_eviction_count
                    ),
                    "rss_peak_kib": metrics.rss_peak_kib,
                    "rss_end_kib": metrics.rss_end_kib,
                    "ir_bytes": metrics.ir_bytes,
                }
            )
    finally:
        if persistent is not None:
            await persistent.close()
    return {
        "mode": args.child_mode,
        "query_count": len(rows),
        "hierarchy_wall_ms": round(hierarchy_wall_ms, 3),
        "sequence_wall_ms": round(
            (time.perf_counter() - sequence_started) * 1000.0,
            3,
        ),
        "queries": rows,
        "runtime": _runtime_summary(runtime.stats_snapshot()),
        "process_memory": _process_memory(),
    }


def run_mode(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_run_mode_async(args))


def _child_command(args: argparse.Namespace, mode: str) -> list[str]:
    return [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "--child-mode",
        mode,
        "--compile-log",
        os.fspath(args.compile_log),
        "--query-spec",
        os.fspath(args.query_spec),
        "--simulator",
        args.simulator,
        "--top",
        args.top,
        "--frontend-python",
        os.fspath(args.frontend_python),
        "--frontend-version",
        args.frontend_version,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--process-timeout-seconds",
        str(args.process_timeout_seconds),
        "--semantic-idle-ttl",
        str(args.semantic_idle_ttl),
        "--semantic-max-rss-kib",
        str(args.semantic_max_rss_kib),
        "--semantic-max-instances",
        str(args.semantic_max_instances),
        "--semantic-max-inputs",
        str(args.semantic_max_inputs),
    ]


def _run_child(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "0"
    environment["TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"] = "off"
    completed = subprocess.run(
        _child_command(args, mode),
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=args.process_timeout_seconds,
    )
    if completed.returncode != 0:
        raise SoakInputError(
            f"{mode} child failed with status {completed.returncode}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SoakInputError(f"{mode} child returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SoakInputError(f"{mode} child returned a non-object")
    return payload


def _percent_change(before: float, after: float) -> float | None:
    if before <= 0:
        return None
    return round((after - before) / before * 100.0, 3)


def _query_map(run: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row["query_sha256"]): row
        for row in run.get("queries", ())
        if isinstance(row, Mapping) and isinstance(row.get("query_sha256"), str)
    }


def _first_break_even_ordinal(
    one_shot_rows: Sequence[Mapping[str, Any]],
    persistent_rows: Sequence[Mapping[str, Any]],
) -> int | None:
    for ordinal, (control, experiment) in enumerate(
        zip(one_shot_rows, persistent_rows),
        start=1,
    ):
        if float(experiment.get("cumulative_wall_ms") or math.inf) <= float(
            control.get("cumulative_wall_ms") or -math.inf
        ):
            return ordinal
    return None


def _contains_forbidden_report_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(token in normalized for token in _FORBIDDEN_REPORT_KEY_TOKENS):
                return True
            if _contains_forbidden_report_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_report_key(item) for item in value)
    return False


def build_result(
    args: argparse.Namespace,
    one_shot: Mapping[str, Any],
    persistent: Mapping[str, Any],
) -> dict[str, Any]:
    control_rows = list(one_shot.get("queries", ()))
    persistent_rows = list(persistent.get("queries", ()))
    control_by_query = _query_map(one_shot)
    persistent_by_query = _query_map(persistent)
    query_keys_equal = (
        [row.get("query_sha256") for row in control_rows]
        == [row.get("query_sha256") for row in persistent_rows]
    )
    fact_equivalent = query_keys_equal and all(
        control_by_query[key].get("fact_sha256")
        == persistent_by_query[key].get("fact_sha256")
        for key in control_by_query
    )
    result_semantics_equal = query_keys_equal and all(
        (
            control_by_query[key].get("result_status"),
            control_by_query[key].get("match_count"),
        )
        == (
            persistent_by_query[key].get("result_status"),
            persistent_by_query[key].get("match_count"),
        )
        for key in control_by_query
    )
    coverage_semantics_equal = query_keys_equal and all(
        control_by_query[key].get("coverage_semantics_sha256")
        == persistent_by_query[key].get("coverage_semantics_sha256")
        for key in control_by_query
    )

    control_sequence_ms = float(one_shot.get("sequence_wall_ms") or 0.0)
    persistent_sequence_ms = float(persistent.get("sequence_wall_ms") or 0.0)
    sequence_change = _percent_change(control_sequence_ms, persistent_sequence_ms)
    sequence_reduction = (
        round(-sequence_change, 3) if sequence_change is not None else None
    )
    first_control = float(control_rows[0].get("operation_wall_ms") or 0.0)
    first_persistent = float(persistent_rows[0].get("operation_wall_ms") or 0.0)
    first_regression = _percent_change(first_control, first_persistent)
    break_even = _first_break_even_ordinal(control_rows, persistent_rows)
    tail_prepare = _distribution(
        [row.get("prepare_wall_ms") for row in persistent_rows[1:]]
    )
    persistent_rss = [
        int(row["rss_peak_kib"])
        for row in persistent_rows
        if isinstance(row.get("rss_peak_kib"), int)
    ]
    rss_growth = (
        max(persistent_rss[-1] - persistent_rss[0], 0)
        if persistent_rss
        else None
    )
    max_persistent_rss = max(persistent_rss) if persistent_rss else None
    control_runtime = one_shot.get("runtime") or {}
    persistent_runtime = persistent.get("runtime") or {}
    query_count = len(control_rows)
    persistent_context_statuses = {
        str(row.get("semantic_context_status")) for row in persistent_rows
    }
    if persistent_context_statuses == {"selected"}:
        workload_eligibility = "eligible"
    elif persistent_context_statuses == {"artifact_scope_sufficient"}:
        workload_eligibility = "not_needed_existing_artifact_scope"
    elif "selected" in persistent_context_statuses:
        workload_eligibility = "mixed"
    else:
        workload_eligibility = "ineligible"

    checks = {
        "minimum_query_count": query_count >= MIN_OPERATIONAL_QUERIES,
        "same_ordered_queries": query_keys_equal,
        "all_prepares_ready": all(
            row.get("prepare_status") == "ready"
            for row in (*control_rows, *persistent_rows)
        ),
        "all_queries_positive": all(
            row.get("positive_fact") is True
            for row in (*control_rows, *persistent_rows)
        ),
        "fact_payloads_equal": fact_equivalent,
        "result_status_and_counts_equal": result_semantics_equal,
        "persistent_context_selected": all(
            row.get("semantic_context_status") == "selected"
            for row in persistent_rows
        ),
        "one_shot_context_disabled": all(
            row.get("semantic_context_status") == "disabled"
            for row in control_rows
        ),
        "one_persistent_launch": int(
            persistent_runtime.get("frontend_launch_count") or 0
        )
        == 1,
        "one_session_miss": int(
            persistent_runtime.get("semantic_session_miss_count") or 0
        )
        == 1,
        "remaining_queries_hit_session": int(
            persistent_runtime.get("semantic_session_hit_count") or 0
        )
        == max(query_count - 1, 0),
        "no_session_restart_or_eviction": int(
            persistent_runtime.get("semantic_session_restart_count") or 0
        )
        == 0
        and int(persistent_runtime.get("semantic_session_eviction_count") or 0)
        == 0,
        "no_timeout_or_worker_failure": int(
            persistent_runtime.get("timeout_count") or 0
        )
        == 0
        and int(persistent_runtime.get("worker_failure_count") or 0) == 0,
        "sequence_reduction_gate": sequence_reduction is not None
        and sequence_reduction >= MIN_SEQUENCE_REDUCTION_PERCENT,
        "first_query_regression_gate": first_regression is not None
        and first_regression <= MAX_FIRST_QUERY_REGRESSION_PERCENT,
        "break_even_gate": break_even is not None
        and break_even <= MAX_BREAK_EVEN_ORDINAL,
        "tail_prepare_gate": tail_prepare["p95"] is not None
        and tail_prepare["p95"] <= MAX_TAIL_PREPARE_P95_MS,
        "rss_cap_gate": max_persistent_rss is not None
        and max_persistent_rss <= args.semantic_max_rss_kib,
        "rss_growth_gate": rss_growth is not None
        and rss_growth <= MAX_SESSION_RSS_GROWTH_KIB,
    }
    passed = all(checks.values())
    result = {
        "schema_version": SCHEMA_VERSION,
        "soak": SOAK_NAME,
        "repository_head": _git_head(ROOT),
        "repository_dirty": _git_dirty(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "workload": {
            "query_count": query_count,
            "semantic_session_eligibility": workload_eligibility,
            "query_sequence_sha256": _sha256_json(
                [row.get("query_sha256") for row in control_rows]
            ),
            "operation_counts": {
                operation: sum(
                    row.get("operation") == operation for row in control_rows
                )
                for operation in ("driver", "loads")
            },
        },
        "policy": {
            "fresh_process_per_mode": True,
            "current_default_is_one_shot": True,
            "semantic_session_is_guarded_opt_in": True,
            "compact_ir_cache_max_entries": 8,
            "semantic_idle_ttl_seconds": args.semantic_idle_ttl,
            "semantic_max_rss_kib": args.semantic_max_rss_kib,
            "semantic_max_instances": args.semantic_max_instances,
            "semantic_max_inputs": args.semantic_max_inputs,
            "default_on_requires_multi_design_evidence": True,
            "default_on_requires_operational_query_frequency": True,
        },
        "comparison": {
            "facts_equal": fact_equivalent,
            "result_semantics_equal": result_semantics_equal,
            "coverage_semantics_equal": coverage_semantics_equal,
            "one_shot_sequence_wall_ms": round(control_sequence_ms, 3),
            "persistent_sequence_wall_ms": round(persistent_sequence_ms, 3),
            "sequence_reduction_percent": sequence_reduction,
            "first_query_regression_percent": first_regression,
            "break_even_ordinal": break_even,
            "persistent_tail_prepare_ms": tail_prepare,
            "persistent_rss_peak_kib": _distribution(persistent_rss),
            "persistent_rss_growth_kib": rss_growth,
            "one_shot_child_peak_rss_kib": (
                one_shot.get("process_memory", {}).get("child_peak_rss_kib")
            ),
            "persistent_child_peak_rss_kib": (
                persistent.get("process_memory", {}).get("child_peak_rss_kib")
            ),
        },
        "modes": {
            "one_shot": {
                "hierarchy_wall_ms": one_shot.get("hierarchy_wall_ms"),
                "sequence_wall_ms": one_shot.get("sequence_wall_ms"),
                "runtime": control_runtime,
                "queries": control_rows,
            },
            "persistent": {
                "hierarchy_wall_ms": persistent.get("hierarchy_wall_ms"),
                "sequence_wall_ms": persistent.get("sequence_wall_ms"),
                "runtime": persistent_runtime,
                "queries": persistent_rows,
            },
        },
        "assessment": {
            "implementation_gate_passed": passed,
            "checks": checks,
            "default_on_authorized": False,
            "decision": (
                "semantic_session_soak_passed_keep_opt_in_pending_multi_design_usage"
                if passed
                else (
                    "semantic_session_not_needed_existing_artifact_scope_keep_opt_in"
                    if workload_eligibility
                    == "not_needed_existing_artifact_scope"
                    else "semantic_session_soak_not_met_keep_opt_in"
                )
            ),
            "privacy_safe_aggregate_only": True,
        },
    }
    if _contains_forbidden_report_key(result):
        raise SoakInputError("soak report contains a forbidden identity key")
    return result


def run_compare(args: argparse.Namespace) -> dict[str, Any]:
    one_shot = _run_child(args, "one_shot")
    persistent = _run_child(args, "persistent")
    return build_result(args, one_shot, persistent)


def _validate_args(args: argparse.Namespace) -> None:
    args.compile_log = args.compile_log.expanduser().resolve()
    args.query_spec = args.query_spec.expanduser().resolve()
    args.frontend_python = Path(
        os.path.abspath(os.fspath(args.frontend_python.expanduser()))
    )
    if not args.compile_log.is_file():
        raise SoakInputError("compile log is unavailable")
    if not args.query_spec.is_file():
        raise SoakInputError("query spec is unavailable")
    if not args.frontend_python.is_file() or not os.access(
        args.frontend_python, os.X_OK
    ):
        raise SoakInputError("frontend Python is unavailable")
    if not args.top or len(args.top) > 512 or "\x00" in args.top:
        raise SoakInputError("top must be a bounded non-empty name")
    if (
        not math.isfinite(args.timeout_seconds)
        or not math.isfinite(args.process_timeout_seconds)
        or not math.isfinite(args.semantic_idle_ttl)
        or min(
            args.timeout_seconds,
            args.process_timeout_seconds,
            args.semantic_idle_ttl,
        )
        <= 0
    ):
        raise SoakInputError("timeouts must be finite and positive")
    if not 1 <= args.semantic_max_instances <= 256:
        raise SoakInputError("semantic instance cap is out of range")
    if not 1 <= args.semantic_max_inputs <= 1024:
        raise SoakInputError("semantic input cap is out of range")
    if not 64 * 1024 <= args.semantic_max_rss_kib <= 8 * 1024 * 1024:
        raise SoakInputError("semantic RSS cap is out of range")
    load_query_spec(args.query_spec)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--simulator", choices=("vcs", "xcelium"), required=True)
    parser.add_argument("--top", required=True)
    parser.add_argument("--frontend-python", type=Path, default=DEFAULT_FRONTEND_PYTHON)
    parser.add_argument("--frontend-version", default="11.0.0")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--process-timeout-seconds", type=float, default=1_800.0)
    parser.add_argument("--semantic-idle-ttl", type=float, default=300.0)
    parser.add_argument("--semantic-max-rss-kib", type=int, default=768 * 1024)
    parser.add_argument("--semantic-max-instances", type=int, default=64)
    parser.add_argument("--semantic-max-inputs", type=int, default=256)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-mode", choices=MODES, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        _validate_args(args)
        result = run_mode(args) if args.child_mode else run_compare(args)
    except (SoakInputError, subprocess.TimeoutExpired) as exc:
        print(f"semantic-session soak error: {exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        _write_json_atomic(args.output, result)
    print(json.dumps(result, indent=None if args.child_mode else 2, sort_keys=True))
    if args.child_mode:
        return 0
    return 0 if result["assessment"]["implementation_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
