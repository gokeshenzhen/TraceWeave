"""
x_trace.py
X/Z 溯源：从一个波形信号和时刻出发，追踪含 X/Z 的上游传播链。
"""

from __future__ import annotations

import inspect
import re
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from config import DEFAULT_X_TRACE_MAX_DEPTH, X_TRACE_MAX_BRANCH_FANOUT
from .cancellation import OperationCancelled


DriverLookup = Callable[
    [str],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
ValueLookup = Callable[
    [str, int],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
UpstreamLookup = Callable[
    [list[str], str, int],
    list[dict[str, Any]] | Awaitable[list[dict[str, Any]]],
]
ScopeGuard = Callable[[list[str]], None | Awaitable[None]]


async def trace_x_source(
    wave_path: str,
    signal_path: str,
    time_ps: int,
    compile_log: str,
    parser=None,
    top_hint: str | None = None,
    max_depth: int = DEFAULT_X_TRACE_MAX_DEPTH,
    simulator: str = "auto",
    *,
    driver_lookup: DriverLookup | None = None,
    value_lookup: ValueLookup | None = None,
    upstream_lookup: UpstreamLookup | None = None,
    upstream_scope_guard: ScopeGuard | None = None,
) -> dict[str, Any]:
    """Trace X/Z propagation using injected wave and connectivity lookups.

    The core deliberately knows nothing about Static, NPI, LSF, or wave locks.
    ``server.py`` composes those execution concerns around the three callbacks,
    so a potentially blocking connectivity query never runs while an FSDB/VCD
    parser lock is held.

    ``parser`` remains as a compatibility/test adapter for wave reads. Server
    dispatch supplies lock-aware async callbacks instead.
    """
    del wave_path, compile_log, top_hint, simulator
    if driver_lookup is None:
        raise ValueError("trace_x_source requires a driver_lookup callback")
    if parser is None and value_lookup is None:
        raise ValueError("trace_x_source requires parser or value_lookup")
    if parser is None and upstream_lookup is None:
        raise ValueError("trace_x_source requires parser or upstream_lookup")

    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    queue: deque[tuple[str, int, dict[str, Any] | None]] = deque(
        [(signal_path, 0, None)]
    )

    while queue:
        current_signal, depth, cached_value = queue.popleft()
        if current_signal in visited:
            continue
        if depth > max_depth:
            chain.append(
                {
                    "depth": depth,
                    "signal_path": current_signal,
                    "trace_stop_reason": "depth_limit_reached",
                }
            )
            continue
        visited.add(current_signal)

        try:
            value_result = (
                cached_value
                if cached_value is not None
                else await _read_signal_value(
                    parser,
                    value_lookup,
                    current_signal,
                    time_ps,
                )
            )
        except OperationCancelled:
            raise
        except Exception:
            chain.append(
                {
                    "depth": depth,
                    "signal_path": current_signal,
                    "trace_stop_reason": "signal_not_in_waveform",
                }
            )
            continue

        if not has_x_or_z(value_result):
            continue

        driver = await _maybe_await(driver_lookup(current_signal))
        node = _build_chain_node(current_signal, depth, value_result, driver)
        chain.append(node)

        driver_status = driver.get("driver_status")
        if driver_status != "resolved":
            node["trace_stop_reason"] = (
                "testbench_driven"
                if driver_status == "testbench_driven"
                else "driver_unresolved"
            )
            continue

        if driver.get("driver_kind") == "instance_ports":
            node["trace_stop_reason"] = "instance_ports_listed"
            continue

        x_upstream: list[str] = []
        clean_upstream: list[str] = []
        unresolved_upstream: list[str] = []
        skipped_signals: list[str] = []

        upstream_names = list(driver.get("upstream_signals", []))
        if not upstream_names:
            # A backend may resolve the originating process/cell without
            # exposing waveform-sampleable dependency signals. Stop honestly
            # instead of implying that the X/Z cone was fully exhausted.
            node["trace_stop_reason"] = "no_upstream_candidates"
            continue
        observations = (
            await _read_upstream_values(
                parser,
                upstream_lookup,
                upstream_names,
                current_signal,
                time_ps,
            )
            if upstream_names
            else []
        )
        for observation in observations:
            upstream_name = str(observation["name"])
            full_path = observation.get("path")
            if full_path is None or full_path in visited:
                unresolved_upstream.append(upstream_name)
                continue
            upstream_value = observation.get("value")
            if not isinstance(upstream_value, dict):
                unresolved_upstream.append(upstream_name)
                continue

            if has_x_or_z(upstream_value):
                x_upstream.append(full_path)
                if len(x_upstream) <= X_TRACE_MAX_BRANCH_FANOUT:
                    queue.append((full_path, depth + 1, upstream_value))
                else:
                    skipped_signals.append(full_path)
            else:
                clean_upstream.append(full_path)

        if x_upstream and upstream_scope_guard is not None:
            await _maybe_await(upstream_scope_guard(x_upstream))

        if x_upstream:
            node["x_upstream_signals"] = x_upstream[:X_TRACE_MAX_BRANCH_FANOUT]
        if clean_upstream:
            node["clean_upstream_signals"] = clean_upstream
        if unresolved_upstream:
            node["unresolved_signals"] = unresolved_upstream
        if skipped_signals:
            node["skipped_signals"] = skipped_signals
        if (
            clean_upstream
            and not x_upstream
            and not unresolved_upstream
            and not skipped_signals
        ):
            node["trace_stop_reason"] = "traced_to_clean_leaf"

    trace_status = _determine_trace_status(chain, signal_path)
    return {
        "start_signal": signal_path,
        "start_time_ps": time_ps,
        "trace_status": trace_status,
        "trace_depth": max((item.get("depth", 0) for item in chain), default=0),
        "max_depth": max_depth,
        "propagation_chain": chain,
        "root_cause": _identify_root_cause(chain),
        "analysis_guide": _generate_analysis_guide(chain, trace_status),
    }


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _read_signal_value(
    parser,
    value_lookup: ValueLookup | None,
    signal_path: str,
    time_ps: int,
) -> dict[str, Any]:
    if value_lookup is not None:
        result = await _maybe_await(value_lookup(signal_path, time_ps))
    else:
        result = parser.get_value_at_time(signal_path, time_ps)
    if not isinstance(result, dict):
        raise TypeError("value_lookup must return a mapping")
    return result


async def _read_upstream_values(
    parser,
    upstream_lookup: UpstreamLookup | None,
    upstream_names: list[str],
    current_signal_path: str,
    time_ps: int,
) -> list[dict[str, Any]]:
    if upstream_lookup is not None:
        result = await _maybe_await(
            upstream_lookup(upstream_names, current_signal_path, time_ps)
        )
    else:
        result = inspect_upstream_values(
            parser,
            upstream_names,
            current_signal_path,
            time_ps,
        )
    if not isinstance(result, list):
        raise TypeError("upstream_lookup must return a list")
    return result


def has_x_or_z(value_result: dict[str, Any]) -> bool:
    value = value_result.get("value")
    candidates: list[str] = []
    if isinstance(value, dict):
        for key in ("raw", "bin"):
            item = value.get(key)
            if item is not None:
                candidates.append(str(item))
    elif value is not None:
        candidates.append(str(value))
    return any(re.search(r"[xXzZ]", candidate) for candidate in candidates)


def _build_chain_node(
    signal_path: str,
    depth: int,
    value_result: dict[str, Any],
    driver: dict[str, Any],
) -> dict[str, Any]:
    node = {
        "depth": depth,
        "signal_path": signal_path,
        "value_at_time": _summarize_value(value_result),
        "has_x": True,
        "module": driver.get("resolved_module"),
        "source_file": driver.get("source_file"),
        "source_line": driver.get("source_line"),
        "driver_status": driver.get("driver_status"),
        "driver_kind": driver.get("driver_kind"),
        "driver_expression": driver.get("expression_summary"),
        "driver_confidence": driver.get("confidence"),
        "claim_semantics": driver.get("claim_semantics"),
        "unsupported_reason": driver.get("unsupported_reason"),
    }
    if driver.get("instance_port_connections"):
        node["instance_port_connections"] = driver["instance_port_connections"]
    if driver.get("cross_check"):
        node["cross_check"] = driver["cross_check"]
    return node


def _summarize_value(value_result: dict[str, Any]) -> str:
    value = value_result.get("value")
    if isinstance(value, dict):
        for key in ("raw", "bin", "hex"):
            if value.get(key) is not None:
                return str(value[key])
    return str(value)


def inspect_upstream_values(
    parser,
    upstream_names: list[str],
    current_signal_path: str,
    time_ps: int,
) -> list[dict[str, Any]]:
    """Resolve and sample one driver's upstream candidates under one wave lock."""
    observations: list[dict[str, Any]] = []
    for upstream_name in upstream_names:
        full_path = _resolve_signal_full_path(
            parser,
            upstream_name,
            current_signal_path,
        )
        if full_path is None:
            observations.append(
                {
                    "name": upstream_name,
                    "path": None,
                    "value": None,
                }
            )
            continue
        try:
            upstream_value = parser.get_value_at_time(full_path, time_ps)
        except OperationCancelled:
            raise
        except Exception:
            upstream_value = None
        observations.append(
            {
                "name": upstream_name,
                "path": full_path,
                "value": upstream_value,
            }
        )
    return observations


def _resolve_signal_full_path(
    parser, signal_name: str, current_signal_path: str
) -> str | None:
    instance_path = ".".join(current_signal_path.split(".")[:-1])
    candidates = []
    if "." in signal_name:
        # Graph/NPI-style providers may already return a waveform-qualified
        # hierarchy path. Static currently returns local RTL identifiers.
        candidates.append(signal_name)
    local_candidate = f"{instance_path}.{signal_name}"
    if local_candidate not in candidates:
        candidates.append(local_candidate)
    for candidate in candidates:
        try:
            parser.get_value_at_time(candidate, 0)
            return candidate
        except OperationCancelled:
            raise
        except Exception:
            pass

    try:
        results = parser.search_signals(signal_name, max_results=10)
    except OperationCancelled:
        raise
    except Exception:
        return None

    matches = results.get("results") or results.get("matches") or []
    for item in matches:
        path = item.get("path")
        if path and path.startswith(instance_path + "."):
            return path
    return matches[0].get("path") if matches else None


def _determine_trace_status(chain: list[dict[str, Any]], start_signal: str) -> str:
    if not chain:
        return "signal_is_clean"
    last = chain[-1]
    if last.get("trace_stop_reason") == "signal_not_in_waveform":
        return "signal_not_in_waveform"
    if last.get("trace_stop_reason") == "instance_ports_listed":
        return "instance_ports_listed"
    if last.get("trace_stop_reason") == "testbench_driven":
        return "testbench_driven"
    if last.get("trace_stop_reason") == "depth_limit_reached":
        return "depth_limit_reached"
    if last.get("trace_stop_reason") == "driver_unresolved":
        return "driver_unresolved"
    if last.get("trace_stop_reason") == "traced_to_clean_leaf":
        return "traced_to_clean_leaf"
    if any(item.get("signal_path") == start_signal for item in chain):
        return "traced_partial_chain"
    return "signal_is_clean"


def _identify_root_cause(chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    for node in reversed(chain):
        if node.get("trace_stop_reason") in {
            "instance_ports_listed",
            "driver_unresolved",
            "testbench_driven",
        }:
            return {
                "signal_path": node.get("signal_path"),
                "driver_kind": node.get("driver_kind"),
                "stop_reason": node.get("trace_stop_reason"),
                "source_file": node.get("source_file"),
                "source_line": node.get("source_line"),
            }
    return None


def _generate_analysis_guide(
    chain: list[dict[str, Any]], trace_status: str
) -> dict[str, str]:
    if trace_status == "signal_is_clean":
        return {
            "step1": "Signal is clean at the requested time; choose another signal or failure timestamp.",
        }

    last = chain[-1] if chain else {}
    if last.get("trace_stop_reason") == "instance_ports_listed":
        connections = last.get("instance_port_connections", [])
        conn_text = ", ".join(item["connected_expression"] for item in connections[:8])
        return {
            "step1": f"Signal {last.get('signal_path')} is driven by {len(connections)} instance port connections.",
            "step2": f"Check bit-range continuity for gaps or overlaps: {conn_text}",
            "step3": "Use explain_signal_driver on the listed upstream instance outputs for deeper analysis.",
        }

    if last.get("trace_stop_reason") == "driver_unresolved":
        return {
            "step1": "The selected connectivity backend could not resolve a traceable driver for this node.",
            "step2": "Inspect unsupported_reason/source evidence, then retry from a more local signal if needed.",
        }

    if last.get("trace_stop_reason") == "testbench_driven":
        return {
            "step1": "NPI found no genuine RTL driver; the signal is driven from testbench or behavioral code.",
            "step2": "Inspect the UVM driver/BFM or virtual-interface clocking block; do not attribute the signal to a landed DUT load register.",
        }

    if last.get("trace_stop_reason") == "no_upstream_candidates":
        return {
            "step1": "The backend resolved the driver but did not expose waveform-sampleable upstream candidates, so this chain is partial.",
            "step2": "Inspect the reported driver location or call explain_signal_driver with recursive=true for structural boundary evidence.",
        }

    if trace_status == "signal_not_in_waveform":
        return {
            "step1": "The requested signal path was not found in the selected waveform.",
            "step2": "Verify the signal hierarchy path or choose a waveform that contains this scope.",
        }

    if trace_status == "traced_to_clean_leaf":
        return {
            "step1": "Trace reached a leaf whose upstream signals are clean at the requested time.",
            "step2": "Focus on the last X-bearing node before this leaf or inspect timing around the divergence point.",
        }

    return {
        "step1": "Inspect the propagation_chain from shallow to deep nodes and focus on X-bearing upstream signals first.",
        "step2": "If multiple upstream signals carry X, prioritize those closest to DUT state or instance boundaries.",
    }
