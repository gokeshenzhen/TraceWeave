"""Privacy and timing tests for per-call waveform operation metrics."""

import time

from src import operation_metrics


def test_snapshot_exposes_only_public_fields():
    metrics = operation_metrics.OperationMetrics()
    operation_metrics.set_value("search_count", 3, metrics)
    operation_metrics.set_value("signal_path", "top.secret", metrics)
    operation_metrics.set_value("scope", "top.customer", metrics)
    operation_metrics.set_value("sweep_phase", "top.secret", metrics)
    operation_metrics.set_value("wave_lock_wait_ms", "top.secret", metrics)

    assert operation_metrics.snapshot(metrics) == {"search_count": 3}


def test_search_aggregate_has_count_total_and_max_only():
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        operation_metrics.record_search(2.25)
        operation_metrics.record_search(5.75)
    finally:
        operation_metrics.pop(token)

    assert operation_metrics.snapshot(metrics) == {
        "search_count": 2,
        "search_total_ms": 8.0,
        "search_max_ms": 5.8,
    }


def test_preemption_latency_is_published_only_after_cancel_observed():
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        operation_metrics.mark_preemption_requested(metrics)
        assert "preemption_to_cancel_ms" not in operation_metrics.snapshot(metrics)
        time.sleep(0.001)
        operation_metrics.mark_cancel_observed()
    finally:
        operation_metrics.pop(token)

    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["preemption_to_cancel_ms"] >= 0
    assert "_preemption_requested_at" not in snapshot


def test_sweep_interface_metrics_are_aggregate_and_privacy_safe():
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        operation_metrics.set_value("sweep_interfaces_planned", 3)
        operation_metrics.set_value("sweep_unique_clocks", 1)
        operation_metrics.set_value("sweep_unique_signals", 9)
        operation_metrics.record_sweep_interface(12.25, completed=True)
        operation_metrics.record_sweep_interface(
            7.75, completed=True, transition_truncated=True
        )
        operation_metrics.record_sweep_interface(4.0, completed=False)
        operation_metrics.set_value("interface_name", "top.customer.secret")
    finally:
        operation_metrics.pop(token)

    assert operation_metrics.snapshot(metrics) == {
        "sweep_interfaces_planned": 3,
        "sweep_unique_clocks": 1,
        "sweep_unique_signals": 9,
        "sweep_interfaces_attempted": 3,
        "sweep_inspect_total_ms": 24.0,
        "sweep_inspect_max_ms": 12.2,
        "sweep_interfaces_completed": 2,
        "sweep_transition_truncated_interfaces": 1,
    }


def test_sweep_subphase_timings_require_fixed_labels_and_active_sweep():
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        operation_metrics.record_sweep_transition_read("clock", 99.0)
        operation_metrics.set_value("_sweep_active", True)
        operation_metrics.record_sweep_transition_read("clock", 2.0)
        operation_metrics.record_sweep_transition_read("signal", 5.0)
        operation_metrics.record_sweep_transition_read("top.secret", 1000.0)
        operation_metrics.add_sweep_cpu_timing("edge_extract", 1.5)
        operation_metrics.add_sweep_cpu_timing("value_sample", 3.5)
        operation_metrics.add_sweep_cpu_timing("top.secret", 1000.0)
        operation_metrics.record_sweep_reuse_hit("clock")
        operation_metrics.record_sweep_reuse_hit("signal")
        operation_metrics.record_sweep_reuse_hit("top.secret")
    finally:
        operation_metrics.pop(token)

    assert operation_metrics.snapshot(metrics) == {
        "sweep_clock_read_count": 1,
        "sweep_clock_read_total_ms": 2.0,
        "sweep_clock_read_max_ms": 2.0,
        "sweep_signal_read_count": 1,
        "sweep_signal_read_total_ms": 5.0,
        "sweep_signal_read_max_ms": 5.0,
        "sweep_edge_extract_total_ms": 1.5,
        "sweep_value_sample_total_ms": 3.5,
        "sweep_clock_reuse_hits": 1,
        "sweep_signal_reuse_hits": 1,
    }
