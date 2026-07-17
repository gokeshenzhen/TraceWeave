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
