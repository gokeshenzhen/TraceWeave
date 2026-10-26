"""Cold/repeated input work, partial-load rollback, and telemetry privacy."""
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from src import cancellation, operation_metrics, usage_telemetry
from src.log_parser import SimLogParser
from src.vcd_parser import VCDParser


WAVE = Path(__file__).parent / "fixtures" / "cycle_test.vcd"


@contextmanager
def measured():
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        yield metrics
    finally:
        operation_metrics.pop(token)


def test_cold_vcd_load_is_distinct_from_reusing_same_parser():
    parser = VCDParser(str(WAVE))
    with measured() as cold:
        first = parser.get_value_at_time("top_tb.data", 500)
    with measured() as warm:
        repeated = parser.get_value_at_time("top_tb.data", 500)
    with measured() as new:
        fresh = VCDParser(str(WAVE)).get_value_at_time("top_tb.data", 500)
    assert first == repeated == fresh
    for metrics in (cold, new):
        data = operation_metrics.snapshot(metrics)
        assert data["vcd_parse_calls"] == data["vcd_parse_completed"] == 1
        assert data["vcd_input_bytes"] == WAVE.stat().st_size
        assert 0 <= data["vcd_read_ms"] <= data["vcd_parse_wall_ms"]
        assert data["vcd_parse_cpu_ms"] >= 0
    assert operation_metrics.snapshot(warm) == {}


def test_cancelled_vcd_load_is_measured_and_never_published(monkeypatch):
    parser = VCDParser(str(WAVE))
    event = threading.Event()
    original = parser._parse

    def cancel_after_parse():
        original()
        event.set()

    monkeypatch.setattr(parser, "_parse", cancel_after_parse)
    token = cancellation.push_cancel_event(event)
    try:
        with measured() as attempt:
            with pytest.raises(cancellation.OperationCancelled):
                parser.get_summary()
    finally:
        cancellation.pop_cancel_event(token)
    data = operation_metrics.snapshot(attempt)
    assert data["vcd_parse_calls"] == 1
    assert data["vcd_parse_completed"] == 0
    assert not parser._parsed and parser._transitions == {}


def test_log_costs_count_repeated_full_reads_without_changing_events(tmp_path):
    path = tmp_path / "private-run.log"
    path.write_text("UVM_ERROR top_tb.sv(10) @ 10 ns: reporter [SCB] expected=1 actual=0\n")
    parser = SimLogParser(str(path), "vcs")
    with measured() as metrics:
        first = parser.parse_failure_events()
        second = parser.parse_failure_events()
    assert first == second and len(first) == 1
    data = operation_metrics.snapshot(metrics)
    assert data["log_parse_calls"] == data["log_parse_completed"] == 2
    assert data["log_input_bytes"] == path.stat().st_size * 2
    assert 0 <= data["log_read_ms"] <= data["log_parse_wall_ms"]
    assert usage_telemetry._sanitize_diagnostics(data) == data
    assert all(isinstance(value, (int, float)) for value in data.values())
    assert "private" not in str(data)


def test_read_failure_retains_attempt_without_claiming_completion(tmp_path):
    with measured() as metrics:
        with pytest.raises(FileNotFoundError):
            SimLogParser(str(tmp_path / "missing.log"), "vcs").parse_failure_events()
    data = operation_metrics.snapshot(metrics)
    assert data["log_parse_calls"] == 1 and data["log_parse_completed"] == 0
    assert "log_input_bytes" not in data


def test_input_metrics_reject_non_numeric_values_and_unknown_identity_labels():
    with measured() as metrics:
        with operation_metrics.input_parse("private/path"):
            operation_metrics.record_input_read("private/path", 12.0, 100)
        operation_metrics.set_value("vcd_parse_wall_ms", "private/path")
        operation_metrics.set_value("log_input_bytes", "private/path")
    assert operation_metrics.snapshot(metrics) == {}
    assert usage_telemetry._sanitize_diagnostics({
        "vcd_parse_wall_ms": "private/path", "log_input_bytes": float("inf"),
        "vcd_parse_calls": True, "log_parse_rss_end_kib": -1,
        "log_path": "private/path",
    }) == {}
