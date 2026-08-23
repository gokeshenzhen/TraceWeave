#!/usr/bin/env python3
"""Compare NPI and one bounded Source Graph artifact on an SoC query corpus.

The providers run in fresh child processes and never enter the production
fallback chain.  The report is identity-free: query text, signal paths, source
paths, expressions, and hierarchy names are represented only by SHA-256 facts.
This is an opt-in development benchmark, not an MCP tool or routing oracle.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
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


BENCHMARK_NAME = "soc_connectivity_differential_v1"
CORPUS_SCHEMA_VERSION = "1.0"
MAX_CORPUS_QUERIES = 64
MAX_QUERY_DEPTH = 64
MAX_SIGNAL_PATH_CHARS = 4096
_NPI_OVERLAY_ENV = "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY"
_INCOMPLETE_REASON_LABELS = {
    "output_limit",
    "work_limit",
    "depth_limit",
    "coverage_incomplete",
    "backend_degraded",
}


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
    """Normalize common absolute and KDB-relative source spellings."""

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


def _process_memory() -> dict[str, int]:
    return {
        "self_peak_rss_kib": int(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        ),
        "child_peak_rss_kib": int(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        ),
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


def _bounded_int(value: Any, *, label: str, default: int) -> int:
    if value is None:
        value = default
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_QUERY_DEPTH
    ):
        raise BenchmarkInputError(f"{label} must be positive and bounded")
    return value


def _boolean(value: Any, *, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise BenchmarkInputError(f"{label} must be boolean")
    return value


def _signal_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise BenchmarkInputError(f"{label} must be a dotted signal path")
    normalized = value.strip()
    if (
        not normalized
        or "." not in normalized
        or len(normalized) > MAX_SIGNAL_PATH_CHARS
        or "\x00" in normalized
        or "*" in normalized
    ):
        raise BenchmarkInputError(f"{label} must be a bounded exact signal path")
    return normalized


def _normalize_query(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BenchmarkInputError("each corpus query must be an object")
    operation = value.get("operation")
    if operation == "driver":
        allowed = {"name", "operation", "signal_path", "recursive", "max_depth"}
        query = {
            "operation": operation,
            "signal_path": _signal_path(
                value.get("signal_path"), label="driver signal_path"
            ),
            "recursive": _boolean(
                value.get("recursive"), label="recursive", default=True
            ),
            "max_depth": _bounded_int(
                value.get("max_depth"), label="driver max_depth", default=10
            ),
        }
    elif operation == "loads":
        allowed = {"name", "operation", "signal_path", "max_depth"}
        query = {
            "operation": operation,
            "signal_path": _signal_path(
                value.get("signal_path"), label="loads signal_path"
            ),
            "max_depth": _bounded_int(
                value.get("max_depth"), label="loads max_depth", default=1
            ),
        }
    elif operation == "path":
        allowed = {
            "name",
            "operation",
            "from_signal",
            "to_signal",
            "expand_assigns",
        }
        query = {
            "operation": operation,
            "from_signal": _signal_path(
                value.get("from_signal"), label="path from_signal"
            ),
            "to_signal": _signal_path(
                value.get("to_signal"), label="path to_signal"
            ),
            "expand_assigns": _boolean(
                value.get("expand_assigns"),
                label="expand_assigns",
                default=False,
            ),
        }
    else:
        raise BenchmarkInputError("query operation must be driver, loads, or path")
    if set(value) - allowed:
        raise BenchmarkInputError("corpus query contains unsupported fields")
    name = value.get("name")
    if name is not None and (
        not isinstance(name, str) or not name.strip() or len(name) > 128
    ):
        raise BenchmarkInputError("query name must be a bounded non-empty string")
    return query


def load_corpus(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError("query corpus is unavailable or invalid") from exc
    if not isinstance(payload, Mapping):
        raise BenchmarkInputError("query corpus must be an object")
    if set(payload) != {"schema_version", "queries"}:
        raise BenchmarkInputError("query corpus fields do not match its schema")
    if payload.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise BenchmarkInputError("query corpus schema version is unsupported")
    rows = payload.get("queries")
    if (
        not isinstance(rows, Sequence)
        or isinstance(rows, (str, bytes))
        or not 1 <= len(rows) <= MAX_CORPUS_QUERIES
    ):
        raise BenchmarkInputError("query corpus size must be positive and bounded")
    queries = tuple(_normalize_query(row) for row in rows)
    identities = tuple(_sha256_json(query) for query in queries)
    if len(set(identities)) != len(identities):
        raise BenchmarkInputError("query corpus contains duplicate semantic queries")
    return queries


def _kind_family(value: Any) -> str:
    kind = str(value or "").lower()
    if "constant" in kind:
        return "constant"
    if any(token in kind for token in ("testbench", "clocking", "uvm")):
        return "testbench"
    if any(
        token in kind
        for token in ("sequential", "register", "nonblocking", "always_ff")
    ):
        return "sequential"
    if any(token in kind for token in ("port", "binding", "input", "output", "inout")):
        return "port"
    if any(
        token in kind
        for token in ("assign", "rhs", "procedural", "combinational", "operator")
    ):
        return "combinational"
    return "other"


def _fact_anchor(
    row: Mapping[str, Any],
    *,
    scope_keys: Sequence[str],
    kind_keys: Sequence[str],
) -> tuple[str, str, str, str | None]:
    source = _source_identity(
        str(row.get("source_file")) if row.get("source_file") else None
    )
    line_value = row.get("source_line")
    line = (
        line_value
        if isinstance(line_value, int) and not isinstance(line_value, bool)
        else None
    )
    kind = next((row.get(key) for key in kind_keys if row.get(key)), None)
    family = _kind_family(kind)
    if source is not None:
        return (
            _sha256_json(
                {
                    "evidence": "source",
                    "source": source,
                    "line": line,
                    "kind": family,
                }
            ),
            "source",
            _sha256_json({"evidence": "source", "source": source, "line": line}),
            _sha256_text(source),
        )
    scope = next((str(row.get(key)) for key in scope_keys if row.get(key)), "")
    return (
        _sha256_json({"evidence": "scope", "scope": scope, "kind": family}),
        "scope",
        _sha256_json({"evidence": "scope", "scope": scope}),
        None,
    )


def _driver_rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    chain = result.get("driver_chain")
    if isinstance(chain, Sequence) and not isinstance(chain, (str, bytes)):
        rows = [row for row in chain if isinstance(row, Mapping)]
        if rows:
            return rows
    if result.get("driver_status") in {"resolved", "partial"} and any(
        result.get(key)
        for key in ("source_file", "source_line", "driver_kind", "resolved_instance_path")
    ):
        return [result]
    return []


def _load_rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    loads = result.get("loads")
    if not isinstance(loads, Sequence) or isinstance(loads, (str, bytes)):
        return []
    return [row for row in loads if isinstance(row, Mapping)]


def _path_rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    path = result.get("path")
    if not isinstance(path, Sequence) or isinstance(path, (str, bytes)):
        return []
    return [row for row in path if isinstance(row, Mapping)]


def _fixed_query_status(
    operation: str,
    result: Mapping[str, Any],
    *,
    positive: bool,
    exhaustive: bool,
) -> str:
    if operation == "driver":
        status = result.get("driver_status")
        if status == "testbench_driven":
            return "testbench_driven"
        if positive:
            return "found" if status == "resolved" else "found_partial"
        if status == "not_connected":
            return "not_connected"
        return "inconclusive" if status == "partial" else "unresolved"
    if operation == "loads":
        if positive:
            return "found"
        if exhaustive or result.get("stopped_at") == "not_connected":
            return "not_connected"
        stopped = str(result.get("stopped_at") or "")
        return "unresolved" if "unresolved" in stopped else "inconclusive"
    if bool(result.get("found")):
        return "found"
    reason = result.get("unsupported_reason")
    if reason == "not_connected":
        return "not_connected"
    if reason in {"from_not_found", "to_not_found", "source_graph_endpoints_unresolved"}:
        return "unresolved"
    return "inconclusive"


def _resource_bounds(
    operation: str,
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Copy only privacy-safe numeric/fixed-label query-bound facts."""

    if operation == "driver":
        raw = result.get("traversal")
        count_key = "returned_fact_count"
        kind = "driver_traversal"
    elif operation == "loads":
        raw = result.get("enumeration")
        count_key = "returned_count"
        kind = "load_enumeration"
    else:
        return None
    if not isinstance(raw, Mapping):
        return None

    receipt: dict[str, Any] = {"kind": kind}
    for key in (
        count_key,
        "output_limit",
        "visited_state_count",
        "state_limit",
        "callback_observed_count",
        "callback_pruned_count",
    ):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            receipt[key] = value
    for key in ("output_truncated", "state_truncated", "search_exhaustive"):
        value = raw.get(key)
        if isinstance(value, bool):
            receipt[key] = value
    reasons = raw.get("incomplete_reasons")
    if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)):
        receipt["incomplete_reasons"] = [
            reason
            for reason in reasons
            if isinstance(reason, str) and reason in _INCOMPLETE_REASON_LABELS
        ]
    return receipt


def normalize_query_result(
    provider: str,
    query: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    npi_load_quality: str | None = None,
) -> dict[str, Any]:
    """Reduce a provider result to identity-free, cross-provider facts."""

    operation = str(query["operation"])
    receipt = result.get("_source_graph_query_receipt")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    claim = receipt.get("claim_semantics")
    claim = claim if isinstance(claim, Mapping) else result.get("claim_semantics")
    claim = claim if isinstance(claim, Mapping) else {}
    enumeration = result.get("enumeration")
    enumeration = enumeration if isinstance(enumeration, Mapping) else {}
    traversal = result.get("traversal")
    traversal = traversal if isinstance(traversal, Mapping) else {}
    if provider == "source_graph":
        exhaustive = bool(claim.get("exhaustive_search"))
        coverage = str(
            receipt.get("coverage_status")
            or claim.get("global_coverage_status")
            or "unknown"
        )
    else:
        if operation == "loads":
            exhaustive = bool(enumeration.get("search_exhaustive"))
        elif operation == "driver":
            exhaustive = bool(traversal.get("search_exhaustive"))
        elif operation == "path":
            exhaustive = bool(
                result.get("unsupported_reason") == "not_connected"
                and npi_load_quality == "clean"
            )
        else:
            exhaustive = False
        coverage = (
            "degraded"
            if npi_load_quality == "degraded"
            else "complete" if exhaustive else "partial"
        )

    if operation == "driver":
        rows = _driver_rows(result)
        scope_keys = ("resolved_instance_path", "signal_path")
        kind_keys = ("driver_kind",)
        positive = bool(rows)
    elif operation == "loads":
        rows = _load_rows(result)
        scope_keys = ("load_path",)
        kind_keys = ("kind",)
        positive = bool(rows)
    else:
        rows = _path_rows(result)
        scope_keys = ("scope_inst", "net_path")
        kind_keys = ("edge_kind",)
        positive = bool(result.get("found"))

    anchored = [
        _fact_anchor(row, scope_keys=scope_keys, kind_keys=kind_keys) for row in rows
    ]
    anchors = sorted({anchor for anchor, _, _, _ in anchored})
    evidence_loci = sorted({locus for _, _, locus, _ in anchored})
    source_files = sorted(
        {source_file for _, _, _, source_file in anchored if source_file is not None}
    )
    evidence_counts = Counter(kind for _, kind, _, _ in anchored)
    return {
        "query_sha256": _sha256_json(dict(query)),
        "operation": operation,
        "status": _fixed_query_status(
            operation,
            result,
            positive=positive,
            exhaustive=exhaustive,
        ),
        "positive": positive,
        "fact_count": len(rows),
        "distinct_fact_count": len(anchors),
        "fact_anchor_sha256": anchors,
        "evidence_locus_sha256": evidence_loci,
        "source_file_sha256": source_files,
        "source_evidence_count": evidence_counts["source"],
        "scope_evidence_count": evidence_counts["scope"],
        "search_exhaustive": exhaustive,
        "coverage_status": coverage,
        "resource_bounds": _resource_bounds(operation, result),
        "path_hops": (
            int(result.get("hops", 0))
            if operation == "path" and isinstance(result.get("hops", 0), int)
            else None
        ),
    }


def _empty_query_summary(
    query: Mapping[str, Any],
    *,
    status: str,
    coverage_status: str = "unavailable",
) -> dict[str, Any]:
    return {
        "query_sha256": _sha256_json(dict(query)),
        "operation": query["operation"],
        "status": status,
        "positive": False,
        "fact_count": 0,
        "distinct_fact_count": 0,
        "fact_anchor_sha256": [],
        "evidence_locus_sha256": [],
        "source_file_sha256": [],
        "source_evidence_count": 0,
        "scope_evidence_count": 0,
        "search_exhaustive": False,
        "coverage_status": coverage_status,
        "resource_bounds": None,
        "path_hops": None,
    }


def _provider_workload(args: argparse.Namespace) -> dict[str, Any]:
    counts = Counter(query["operation"] for query in args.queries)
    stat_result = args.compile_log.stat()
    return {
        "simulator": args.simulator,
        "compile_log_bytes": stat_result.st_size,
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_sha256": _sha256_json(list(args.queries)),
        "query_count": len(args.queries),
        "operation_counts": {
            operation: counts.get(operation, 0)
            for operation in ("driver", "loads", "path")
        },
        "query_sha256": [_sha256_json(query) for query in args.queries],
        "top_sha256": _sha256_text(args.top) if args.top else None,
    }


def _npi_query(
    backend: Any,
    query: Mapping[str, Any],
    *,
    compile_result: dict[str, Any],
    kdb_path: str,
    top: str,
) -> dict[str, Any]:
    operation = query["operation"]
    if operation == "driver":
        return backend._npi_find_driver(  # noqa: SLF001
            query["signal_path"],
            "",
            top,
            recursive=query["recursive"],
        )
    if operation == "loads":
        return backend._npi_find_loads(  # noqa: SLF001
            query["signal_path"],
            compile_result,
            kdb_path,
            top,
            False,
            None,
        )
    return backend._npi_find_path(  # noqa: SLF001
        query["from_signal"],
        query["to_signal"],
        expand_assigns=query["expand_assigns"],
    )


def run_npi(args: argparse.Namespace) -> dict[str, Any]:
    from src.compile_log_parser import parse_compile_log  # noqa: PLC0415
    from src.verdi_npi_backend import VerdiNpiBackend  # noqa: PLC0415

    started = time.perf_counter()
    parse_started = time.perf_counter()
    compile_result = parse_compile_log(os.fspath(args.compile_log), args.simulator)
    parse_wall_ms = (time.perf_counter() - parse_started) * 1000.0
    backend = VerdiNpiBackend()
    kdb_path = backend._kdb_path_from(  # noqa: SLF001
        compile_result, os.fspath(args.compile_log)
    )
    top = args.top or backend._top_from(compile_result)  # noqa: SLF001
    if not kdb_path or not top:
        raise BenchmarkInputError("NPI benchmark requires a KDB and elaborated top")
    load_started = time.perf_counter()
    if not backend._ensure_loaded(kdb_path, top):  # noqa: SLF001
        raise BenchmarkInputError("NPI benchmark could not load the selected design")
    load_wall_ms = (time.perf_counter() - load_started) * 1000.0

    queries: list[dict[str, Any]] = []
    for query in args.queries:
        query_started = time.perf_counter()
        try:
            raw = _npi_query(
                backend,
                query,
                compile_result=compile_result,
                kdb_path=kdb_path,
                top=top,
            )
        except Exception:  # noqa: BLE001
            summary = _empty_query_summary(query, status="provider_error")
        else:
            summary = normalize_query_result(
                "npi",
                query,
                raw,
                npi_load_quality=backend.kdb_load_quality,
            )
        summary["query_wall_ms"] = round(
            (time.perf_counter() - query_started) * 1000.0, 3
        )
        queries.append(summary)
    return {
        "benchmark": BENCHMARK_NAME,
        "provider": "npi",
        "workload": _provider_workload(args),
        "setup": {
            "status": "completed",
            "load_quality": backend.kdb_load_quality,
            "parse_wall_ms": round(parse_wall_ms, 3),
            "design_load_wall_ms": round(load_wall_ms, 3),
        },
        "queries": queries,
        "total_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "process_memory": _process_memory(),
    }


def _source_graph_plan(
    query: Mapping[str, Any],
    *,
    args: argparse.Namespace,
    compile_result: Mapping[str, Any],
    hierarchy: Mapping[str, Any],
    snapshot: str,
) -> Any:
    from src.source_graph_adapter import (  # noqa: PLC0415
        build_source_graph_initial_plan,
        build_source_graph_path_plan,
    )
    from src.source_graph_contract import QueryOperation  # noqa: PLC0415

    operation = query["operation"]
    if operation == "path":
        return build_source_graph_path_plan(
            compile_log=os.fspath(args.compile_log),
            compile_result=compile_result,
            hierarchy_result=hierarchy,
            hierarchy_snapshot_sha256=snapshot,
            from_signal=query["from_signal"],
            to_signal=query["to_signal"],
            top_hint=args.top,
            expand_assigns=query["expand_assigns"],
            frontend_version=args.frontend_version,
        )
    return build_source_graph_initial_plan(
        compile_log=os.fspath(args.compile_log),
        compile_result=compile_result,
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
        recursive=bool(query.get("recursive", False)),
        include_expr=operation == "driver",
        kind_filter=(),
    )


def _source_graph_query(
    backend: Any,
    query: Mapping[str, Any],
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    operation = query["operation"]
    if operation == "driver":
        return backend.find_driver(
            query["signal_path"],
            "",
            os.fspath(args.compile_log),
            top_hint=args.top,
            recursive=query["recursive"],
            max_depth=query["max_depth"],
            simulator=args.simulator,
        )
    if operation == "loads":
        return backend.find_loads(
            query["signal_path"],
            os.fspath(args.compile_log),
            top_hint=args.top,
            max_depth=query["max_depth"],
            include_expr=False,
            kind_filter=None,
            simulator=args.simulator,
        )
    return backend.find_path(
        query["from_signal"],
        query["to_signal"],
        os.fspath(args.compile_log),
        top_hint=args.top,
        expand_assigns=query["expand_assigns"],
        simulator=args.simulator,
    )


async def _run_source_graph_async(args: argparse.Namespace) -> dict[str, Any]:
    import server  # noqa: PLC0415
    from src.source_graph_backend import (  # noqa: PLC0415
        SourceGraphConnectivityBackend,
        SourceGraphQueryBlocked,
    )
    from src.source_graph_runtime import (  # noqa: PLC0415
        IsolatedSourceGraphProcessRunner,
        SourceGraphRuntime,
    )

    started = time.perf_counter()
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
    queries: list[dict[str, Any]] = []
    for query in args.queries:
        plan_started = time.perf_counter()
        plan = _source_graph_plan(
            query,
            args=args,
            compile_result=compile_result,
            hierarchy=hierarchy,
            snapshot=snapshot,
        )
        planning_wall_ms = (time.perf_counter() - plan_started) * 1000.0
        row = _empty_query_summary(query, status="plan_blocked")
        row["planning_wall_ms"] = round(planning_wall_ms, 3)
        row["plan_status"] = plan.status.value
        if plan.request is None:
            blocker = plan.receipt.blocker
            row["plan_blocker"] = blocker.code if blocker is not None else None
            queries.append(row)
            continue

        projection = plan.request.artifact_identity.compile_projection
        row["projection_input_count"] = (
            len(projection.ordered_inputs) if projection is not None else None
        )
        row["projection_instance_count"] = len(
            plan.request.artifact_identity.scope.projection_instance_paths
        )
        prepare_started = time.perf_counter()
        outcome = await runtime.prepare(
            plan.request,
            timeout_seconds=args.timeout_seconds,
        )
        row["prepare_wall_ms"] = round(
            (time.perf_counter() - prepare_started) * 1000.0, 3
        )
        row["prepare_status"] = outcome.status.value
        row["prepare_metrics"] = outcome.metrics.to_dict()
        if outcome.entry is None:
            row["status"] = "prepare_failed"
            row["prepare_blocker"] = (
                outcome.blocker.code if outcome.blocker is not None else None
            )
            queries.append(row)
            continue

        backend = SourceGraphConnectivityBackend(outcome.entry)
        backend.set_unprojected_instance_candidates(
            plan.unprojected_instance_candidates
        )
        query_started = time.perf_counter()
        try:
            raw = _source_graph_query(backend, query, args=args)
        except SourceGraphQueryBlocked as exc:
            summary = _empty_query_summary(query, status="query_blocked")
            summary["query_blocker"] = exc.code
        except Exception:  # noqa: BLE001
            summary = _empty_query_summary(query, status="provider_error")
        else:
            summary = normalize_query_result("source_graph", query, raw)
        summary["query_wall_ms"] = round(
            (time.perf_counter() - query_started) * 1000.0, 3
        )
        for key in (
            "planning_wall_ms",
            "plan_status",
            "projection_input_count",
            "projection_instance_count",
            "prepare_wall_ms",
            "prepare_status",
            "prepare_metrics",
        ):
            summary[key] = row[key]
        queries.append(summary)

    return {
        "benchmark": BENCHMARK_NAME,
        "provider": "source_graph",
        "workload": _provider_workload(args),
        "setup": {
            "status": "completed",
            "hierarchy_wall_ms": round(hierarchy_wall_ms, 3),
        },
        "queries": queries,
        "runtime": runtime.stats_snapshot(),
        "total_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "process_memory": _process_memory(),
    }


def run_source_graph(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_run_source_graph_async(args))


def _query_map(run: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = run.get("queries")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    return {
        str(row["query_sha256"]): row
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("query_sha256"), str)
    }


def compare_provider_runs(
    npi: Mapping[str, Any],
    source_graph: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare identity-free query facts without changing production policy."""

    npi_queries = _query_map(npi)
    source_graph_queries = _query_map(source_graph)
    keys = sorted(set(npi_queries) | set(source_graph_queries))
    counts = {
        "query_missing_npi": 0,
        "query_missing_source_graph": 0,
        "positive_status_mismatch": 0,
        "common_fact": 0,
        "common_evidence_locus": 0,
        "common_source_file": 0,
        "coverage_explained_npi_only_fact": 0,
        "unexpected_npi_only_fact": 0,
        "source_graph_only_fact": 0,
        "path_reachability_mismatch": 0,
        "path_hop_count_mismatch": 0,
    }
    comparisons: list[dict[str, Any]] = []
    for key in keys:
        left = npi_queries.get(key)
        right = source_graph_queries.get(key)
        if left is None:
            counts["query_missing_npi"] += 1
            comparisons.append({"query_sha256": key, "status": "missing_npi"})
            continue
        if right is None:
            counts["query_missing_source_graph"] += 1
            comparisons.append(
                {"query_sha256": key, "status": "missing_source_graph"}
            )
            continue
        operation = str(left.get("operation"))
        positive_mismatch = bool(left.get("positive")) != bool(right.get("positive"))
        if positive_mismatch:
            counts["positive_status_mismatch"] += 1
        if operation == "path":
            if positive_mismatch:
                counts["path_reachability_mismatch"] += 1
            hop_mismatch = bool(
                left.get("positive")
                and right.get("positive")
                and left.get("path_hops") != right.get("path_hops")
            )
            if hop_mismatch:
                counts["path_hop_count_mismatch"] += 1
            comparisons.append(
                {
                    "query_sha256": key,
                    "operation": operation,
                    "positive_status_match": not positive_mismatch,
                    "path_hop_count_match": not hop_mismatch,
                }
            )
            continue

        left_facts = {
            value
            for value in left.get("fact_anchor_sha256", ())
            if isinstance(value, str)
        }
        right_facts = {
            value
            for value in right.get("fact_anchor_sha256", ())
            if isinstance(value, str)
        }
        common = left_facts & right_facts
        npi_only = left_facts - right_facts
        source_graph_only = right_facts - left_facts
        source_graph_exhaustive = bool(right.get("search_exhaustive"))
        counts["common_fact"] += len(common)
        left_loci = {
            value
            for value in left.get("evidence_locus_sha256", ())
            if isinstance(value, str)
        }
        right_loci = {
            value
            for value in right.get("evidence_locus_sha256", ())
            if isinstance(value, str)
        }
        left_files = {
            value
            for value in left.get("source_file_sha256", ())
            if isinstance(value, str)
        }
        right_files = {
            value
            for value in right.get("source_file_sha256", ())
            if isinstance(value, str)
        }
        common_loci = len(left_loci & right_loci)
        common_files = len(left_files & right_files)
        counts["common_evidence_locus"] += common_loci
        counts["common_source_file"] += common_files
        if source_graph_exhaustive:
            counts["unexpected_npi_only_fact"] += len(npi_only)
            coverage_explained = 0
            unexpected = len(npi_only)
        else:
            counts["coverage_explained_npi_only_fact"] += len(npi_only)
            coverage_explained = len(npi_only)
            unexpected = 0
        counts["source_graph_only_fact"] += len(source_graph_only)
        comparisons.append(
            {
                "query_sha256": key,
                "operation": operation,
                "positive_status_match": not positive_mismatch,
                "common_fact_count": len(common),
                "common_evidence_locus_count": common_loci,
                "common_source_file_count": common_files,
                "coverage_explained_npi_only_fact_count": coverage_explained,
                "unexpected_npi_only_fact_count": unexpected,
                "source_graph_only_fact_count": len(source_graph_only),
                "source_graph_search_exhaustive": source_graph_exhaustive,
            }
        )
    return {
        "query_count": len(keys),
        "counts": counts,
        "queries": comparisons,
        "interpretation": "offline_measurement_only",
    }


def _child_command(args: argparse.Namespace, provider: str) -> list[str]:
    command = [
        sys.executable,
        os.fspath(Path(__file__).resolve()),
        "--compile-log",
        os.fspath(args.compile_log),
        "--corpus",
        os.fspath(args.corpus),
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
    ]
    if args.top:
        command.extend(("--top", args.top))
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
    source_graph = _run_child(args, "source_graph")
    return {
        "benchmark": BENCHMARK_NAME,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_head": _git_head(),
            "git_dirty": _git_dirty(),
        },
        "workload": _provider_workload(args),
        "providers": {"npi": npi, "source_graph": source_graph},
        "comparison": compare_provider_runs(npi, source_graph),
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-log", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--top")
    parser.add_argument(
        "--simulator",
        choices=("auto", "vcs", "xcelium"),
        default="auto",
    )
    parser.add_argument(
        "--provider",
        choices=("compare", "npi", "source_graph"),
        default="compare",
    )
    parser.add_argument(
        "--frontend-python",
        type=Path,
        default=ROOT / ".venv/bin/python",
    )
    parser.add_argument("--frontend-version", default="11.0.0")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    args.compile_log = args.compile_log.absolute()
    args.corpus = args.corpus.absolute()
    args.frontend_python = args.frontend_python.absolute()
    if not args.compile_log.is_file():
        raise BenchmarkInputError("compile log is unavailable")
    if not args.corpus.is_file():
        raise BenchmarkInputError("query corpus is unavailable")
    if not args.frontend_python.is_file():
        raise BenchmarkInputError("Source Graph frontend Python is unavailable")
    if (
        not math.isfinite(args.timeout_seconds)
        or args.timeout_seconds <= 0
        or args.timeout_seconds > 3600
    ):
        raise BenchmarkInputError("provider timeout must be positive and bounded")
    if args.top is not None and (
        not args.top.strip() or len(args.top) > 512 or "\x00" in args.top
    ):
        raise BenchmarkInputError("top must be a bounded non-empty name")
    args.queries = load_corpus(args.corpus)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        _validate_args(args)
        if args.provider == "compare":
            result = run_compare(args)
        elif args.provider == "npi":
            result = run_npi(args)
        else:
            result = run_source_graph(args)
    except BenchmarkInputError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
