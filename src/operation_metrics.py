"""Per-call, privacy-safe timing metrics for long waveform operations.

The MCP request coroutine creates one :class:`OperationMetrics` object and
stores it in a ContextVar. AnyIO copies that context into the waveform worker;
both sides therefore update the same thread-safe object. Only the explicit
public whitelist returned by :func:`snapshot` reaches usage telemetry.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Iterator


_PUBLIC_FIELDS = {
    "wave_lock_wait_ms",
    "preemption_to_cancel_ms",
    "sweep_phase",
    "discover_valid_ready_ms",
    "discover_ahb_ms",
    "search_count",
    "search_total_ms",
    "search_max_ms",
    "sweep_total_ms",
    "sweep_interfaces_planned",
    "sweep_interfaces_attempted",
    "sweep_interfaces_completed",
    "sweep_unique_clocks",
    "sweep_unique_signals",
    "sweep_inspect_total_ms",
    "sweep_inspect_max_ms",
    "sweep_transition_truncated_interfaces",
    "sweep_clock_read_count",
    "sweep_clock_read_total_ms",
    "sweep_clock_read_max_ms",
    "sweep_signal_read_count",
    "sweep_signal_read_total_ms",
    "sweep_signal_read_max_ms",
    "sweep_edge_extract_total_ms",
    "sweep_value_sample_total_ms",
}
_PUBLIC_PHASES = {"discover_valid_ready", "discover_ahb", "inspect_interfaces", "complete"}
_PUBLIC_NUMERIC_FIELDS = _PUBLIC_FIELDS - {"sweep_phase"}


@dataclass
class OperationMetrics:
    values: dict[str, object] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_current: ContextVar[OperationMetrics | None] = ContextVar(
    "traceweave_operation_metrics", default=None
)


def push(metrics: OperationMetrics) -> Token:
    return _current.set(metrics)


def pop(token: Token) -> None:
    _current.reset(token)


def current() -> OperationMetrics | None:
    return _current.get()


def set_value(name: str, value: object, metrics: OperationMetrics | None = None) -> None:
    target = metrics or current()
    if target is None:
        return
    with target.lock:
        target.values[name] = value


def record_search(duration_ms: float) -> None:
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        metrics.values["search_count"] = int(metrics.values.get("search_count", 0)) + 1
        metrics.values["search_total_ms"] = (
            float(metrics.values.get("search_total_ms", 0.0)) + duration_ms
        )
        metrics.values["search_max_ms"] = max(
            float(metrics.values.get("search_max_ms", 0.0)), duration_ms
        )


def record_sweep_interface(
    duration_ms: float,
    *,
    completed: bool,
    transition_truncated: bool = False,
) -> None:
    """Record one inspect attempt without retaining interface identity.

    The aggregate is intentionally limited to counts and timings: no scope,
    signal name, waveform path, or sampled value can enter telemetry.
    """
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        metrics.values["sweep_interfaces_attempted"] = (
            int(metrics.values.get("sweep_interfaces_attempted", 0)) + 1
        )
        metrics.values["sweep_inspect_total_ms"] = (
            float(metrics.values.get("sweep_inspect_total_ms", 0.0)) + duration_ms
        )
        metrics.values["sweep_inspect_max_ms"] = max(
            float(metrics.values.get("sweep_inspect_max_ms", 0.0)), duration_ms
        )
        if completed:
            metrics.values["sweep_interfaces_completed"] = (
                int(metrics.values.get("sweep_interfaces_completed", 0)) + 1
            )
        if transition_truncated:
            metrics.values["sweep_transition_truncated_interfaces"] = (
                int(metrics.values.get("sweep_transition_truncated_interfaces", 0)) + 1
            )


def record_sweep_transition_read(kind: str, duration_ms: float) -> None:
    """Aggregate a clock/signal native read while a full sweep is active."""
    if kind not in {"clock", "signal"}:
        return
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        prefix = f"sweep_{kind}_read"
        metrics.values[f"{prefix}_count"] = (
            int(metrics.values.get(f"{prefix}_count", 0)) + 1
        )
        metrics.values[f"{prefix}_total_ms"] = (
            float(metrics.values.get(f"{prefix}_total_ms", 0.0)) + duration_ms
        )
        metrics.values[f"{prefix}_max_ms"] = max(
            float(metrics.values.get(f"{prefix}_max_ms", 0.0)), duration_ms
        )


def add_sweep_cpu_timing(kind: str, duration_ms: float) -> None:
    """Aggregate fixed-label Python-side sweep work; reject arbitrary labels."""
    field = {
        "edge_extract": "sweep_edge_extract_total_ms",
        "value_sample": "sweep_value_sample_total_ms",
    }.get(kind)
    if field is None:
        return
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values[field] = float(metrics.values.get(field, 0.0)) + duration_ms


@contextmanager
def timed_phase(phase: str, metric_name: str) -> Iterator[None]:
    set_value("sweep_phase", phase)
    started = time.perf_counter()
    try:
        yield
    finally:
        set_value(metric_name, (time.perf_counter() - started) * 1000.0)


def mark_preemption_requested(metrics: OperationMetrics | None) -> None:
    if metrics is None:
        return
    with metrics.lock:
        metrics.values.setdefault("_preemption_requested_at", time.perf_counter())


def mark_cancel_observed() -> None:
    metrics = current()
    if metrics is None:
        return
    now = time.perf_counter()
    with metrics.lock:
        requested = metrics.values.get("_preemption_requested_at")
        if isinstance(requested, (int, float)):
            metrics.values.setdefault(
                "preemption_to_cancel_ms", (now - requested) * 1000.0
            )


def snapshot(metrics: OperationMetrics | None) -> dict[str, object]:
    if metrics is None:
        return {}
    with metrics.lock:
        values = {
            key: value
            for key, value in metrics.values.items()
            if key in _PUBLIC_FIELDS
        }
    public: dict[str, object] = {}
    for key, value in values.items():
        if key == "sweep_phase" and value not in _PUBLIC_PHASES:
            continue
        if key in _PUBLIC_NUMERIC_FIELDS and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            continue
        public[key] = round(value, 1) if isinstance(value, float) else value
    return public
