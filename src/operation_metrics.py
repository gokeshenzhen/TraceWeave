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
from typing import Iterator, Mapping


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
    "sweep_clock_reuse_hits",
    "sweep_signal_reuse_hits",
    "sweep_native_group_count",
    "sweep_native_group_signal_total",
    "sweep_native_group_signal_max",
    "sweep_native_group_load_call_count",
    "sweep_native_group_load_total_ms",
    "sweep_native_group_load_max_ms",
    "sweep_native_group_fallback_count",
    "sweep_native_group_unsupported_count",
    "sweep_native_group_oversized_count",
    "sweep_native_group_begin_error_count",
    "sweep_native_profiled_read_count",
    "sweep_native_standalone_load_call_count",
    "sweep_native_standalone_load_total_ms",
    "sweep_native_standalone_load_max_ms",
    "sweep_native_fallback_signal_total",
    "sweep_native_lookup_total_ms",
    "sweep_native_add_signal_total_ms",
    "sweep_native_load_total_ms",
    "sweep_native_load_max_ms",
    "sweep_native_create_handle_total_ms",
    "sweep_native_seek_total_ms",
    "sweep_native_traverse_format_total_ms",
    "sweep_native_free_handle_total_ms",
    "sweep_native_unload_total_ms",
    "sweep_native_transition_count",
    "sweep_native_output_bytes",
    "sweep_native_truncated_calls",
    "sweep_rss_start_kib",
    "sweep_rss_peak_kib",
    "sweep_rss_end_kib",
    "sweep_rss_peak_delta_kib",
    "sweep_cached_signal_results_peak",
    "sweep_cached_transition_count_peak",
    "sweep_sample_edges_total",
    "sweep_sample_edges_max",
    "sweep_sample_values_total",
    "sweep_sample_values_max",
    "sweep_path_resolution_total_ms",
    "sweep_sample_lookup_total_ms",
    "sweep_sample_materialize_total_ms",
    "sweep_protocol_scan_total_ms",
    "sweep_write_data_scan_total_ms",
    "sweep_group_pack_count",
    "sweep_group_pack_clock_total",
    "sweep_group_chunk_count",
    "sweep_result_build_ms",
    "sweep_result_serialize_ms",
    "sweep_result_bytes",
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


def add_sweep_execution_timing(kind: str, duration_ms: float) -> None:
    """Aggregate a fixed execution phase without accepting identity labels."""
    field = {
        "path_resolution": "sweep_path_resolution_total_ms",
        "sample_lookup": "sweep_sample_lookup_total_ms",
        "sample_materialize": "sweep_sample_materialize_total_ms",
        "protocol_scan": "sweep_protocol_scan_total_ms",
        "write_data_scan": "sweep_write_data_scan_total_ms",
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


def record_sweep_reuse_hit(kind: str) -> None:
    """Count fixed-kind cache reuse without retaining signal identity."""
    field = {
        "clock": "sweep_clock_reuse_hits",
        "signal": "sweep_signal_reuse_hits",
    }.get(kind)
    if field is None:
        return
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values[field] = int(metrics.values.get(field, 0)) + 1


def _ns_to_ms(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) / 1_000_000.0


def record_sweep_native_group_begin(profile: Mapping[str, object]) -> None:
    """Aggregate one identity-free native group-load receipt."""
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        signal_count = int(profile.get("signal_count", 0) or 0)
        metrics.values["sweep_native_group_count"] = (
            int(metrics.values.get("sweep_native_group_count", 0)) + 1
        )
        metrics.values["sweep_native_group_signal_total"] = (
            int(metrics.values.get("sweep_native_group_signal_total", 0))
            + signal_count
        )
        metrics.values["sweep_native_group_signal_max"] = max(
            int(metrics.values.get("sweep_native_group_signal_max", 0)),
            signal_count,
        )
        _add_native_duration_locked(metrics, "lookup", profile.get("lookup_ns"))
        _add_native_duration_locked(
            metrics, "add_signal", profile.get("add_signal_ns")
        )
        load_ms = _add_native_duration_locked(
            metrics, "load", profile.get("load_ns")
        )
        metrics.values["sweep_native_group_load_call_count"] = (
            int(metrics.values.get("sweep_native_group_load_call_count", 0)) + 1
        )
        metrics.values["sweep_native_group_load_total_ms"] = (
            float(metrics.values.get("sweep_native_group_load_total_ms", 0.0))
            + load_ms
        )
        metrics.values["sweep_native_group_load_max_ms"] = max(
            float(metrics.values.get("sweep_native_group_load_max_ms", 0.0)),
            load_ms,
        )
        metrics.values["sweep_native_load_max_ms"] = max(
            float(metrics.values.get("sweep_native_load_max_ms", 0.0)), load_ms
        )


def record_sweep_native_group_end(profile: Mapping[str, object]) -> None:
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        _add_native_duration_locked(metrics, "unload", profile.get("unload_ns"))


def record_sweep_native_transition(
    profile: Mapping[str, object], *, standalone_load: bool = False
) -> None:
    """Aggregate one profiled transition call without its signal identity."""
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values["sweep_native_profiled_read_count"] = (
            int(metrics.values.get("sweep_native_profiled_read_count", 0)) + 1
        )
        for name in (
            "lookup", "add_signal", "load", "create_handle", "seek",
            "traverse_format", "free_handle", "unload",
        ):
            duration_ms = _add_native_duration_locked(
                metrics, name, profile.get(f"{name}_ns")
            )
            if name == "load":
                metrics.values["sweep_native_load_max_ms"] = max(
                    float(metrics.values.get("sweep_native_load_max_ms", 0.0)),
                    duration_ms,
                )
                if standalone_load:
                    metrics.values["sweep_native_standalone_load_call_count"] = (
                        int(metrics.values.get(
                            "sweep_native_standalone_load_call_count", 0
                        )) + 1
                    )
                    metrics.values["sweep_native_standalone_load_total_ms"] = (
                        float(metrics.values.get(
                            "sweep_native_standalone_load_total_ms", 0.0
                        )) + duration_ms
                    )
                    metrics.values["sweep_native_standalone_load_max_ms"] = max(
                        float(metrics.values.get(
                            "sweep_native_standalone_load_max_ms", 0.0
                        )),
                        duration_ms,
                    )
        metrics.values["sweep_native_transition_count"] = (
            int(metrics.values.get("sweep_native_transition_count", 0))
            + int(profile.get("transition_count", 0) or 0)
        )
        metrics.values["sweep_native_output_bytes"] = (
            int(metrics.values.get("sweep_native_output_bytes", 0))
            + int(profile.get("output_bytes", 0) or 0)
        )
        if int(profile.get("truncated", 0) or 0):
            metrics.values["sweep_native_truncated_calls"] = (
                int(metrics.values.get("sweep_native_truncated_calls", 0)) + 1
            )


def _add_native_duration_locked(
    metrics: OperationMetrics, name: str, value_ns: object
) -> float:
    duration_ms = _ns_to_ms(value_ns)
    field = f"sweep_native_{name}_total_ms"
    metrics.values[field] = float(metrics.values.get(field, 0.0)) + duration_ms
    return duration_ms


def record_sweep_native_group_fallback(
    reason: str, *, signal_count: int = 0
) -> None:
    """Count a fixed fallback reason; arbitrary labels are rejected."""
    reason_field = {
        "unsupported": "sweep_native_group_unsupported_count",
        "oversized": "sweep_native_group_oversized_count",
        "begin_error": "sweep_native_group_begin_error_count",
    }.get(reason)
    if reason_field is None:
        return
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values["sweep_native_group_fallback_count"] = (
            int(metrics.values.get("sweep_native_group_fallback_count", 0)) + 1
        )
        metrics.values[reason_field] = int(metrics.values.get(reason_field, 0)) + 1
        metrics.values["sweep_native_fallback_signal_total"] = (
            int(metrics.values.get("sweep_native_fallback_signal_total", 0))
            + max(0, int(signal_count))
        )


def record_sweep_group_pack(*, clock_count: int, chunked: bool = False) -> None:
    """Record bounded scheduler shape; identities are deliberately absent."""
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values["sweep_group_pack_count"] = (
            int(metrics.values.get("sweep_group_pack_count", 0)) + 1
        )
        metrics.values["sweep_group_pack_clock_total"] = (
            int(metrics.values.get("sweep_group_pack_clock_total", 0))
            + max(0, int(clock_count))
        )
        if chunked:
            metrics.values["sweep_group_chunk_count"] = (
                int(metrics.values.get("sweep_group_chunk_count", 0)) + 1
            )


def record_sweep_cache_peak(entries: int, transitions: int) -> None:
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values["sweep_cached_signal_results_peak"] = max(
            int(metrics.values.get("sweep_cached_signal_results_peak", 0)),
            int(entries),
        )
        metrics.values["sweep_cached_transition_count_peak"] = max(
            int(metrics.values.get("sweep_cached_transition_count_peak", 0)),
            int(transitions),
        )


def record_sweep_sampling_shape(edge_count: int, signal_count: int) -> None:
    metrics = current()
    if metrics is None:
        return
    edge_count = max(0, int(edge_count))
    value_count = edge_count * max(0, int(signal_count))
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        metrics.values["sweep_sample_edges_total"] = (
            int(metrics.values.get("sweep_sample_edges_total", 0)) + edge_count
        )
        metrics.values["sweep_sample_edges_max"] = max(
            int(metrics.values.get("sweep_sample_edges_max", 0)), edge_count
        )
        metrics.values["sweep_sample_values_total"] = (
            int(metrics.values.get("sweep_sample_values_total", 0)) + value_count
        )
        metrics.values["sweep_sample_values_max"] = max(
            int(metrics.values.get("sweep_sample_values_max", 0)), value_count
        )


def read_process_rss_kib() -> int | None:
    """Return current Linux RSS without retaining process/environment identity."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def record_sweep_rss(*, phase: str) -> None:
    if phase not in {"start", "sample", "end"}:
        return
    rss_kib = read_process_rss_kib()
    if rss_kib is None:
        return
    metrics = current()
    if metrics is None:
        return
    with metrics.lock:
        if metrics.values.get("_sweep_active") is not True:
            return
        if phase == "start":
            metrics.values["sweep_rss_start_kib"] = rss_kib
        metrics.values["sweep_rss_peak_kib"] = max(
            int(metrics.values.get("sweep_rss_peak_kib", 0)), rss_kib
        )
        if phase == "end":
            metrics.values["sweep_rss_end_kib"] = rss_kib
            start = metrics.values.get("sweep_rss_start_kib")
            if isinstance(start, int):
                metrics.values["sweep_rss_peak_delta_kib"] = max(
                    0, int(metrics.values["sweep_rss_peak_kib"]) - start
                )


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
