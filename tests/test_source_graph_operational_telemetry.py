"""End-to-end coverage for persistent Source Graph operational telemetry."""

import json

import pytest

import config
import server
from scripts.telemetry_report import render
from src import usage_telemetry


@pytest.mark.anyio
async def test_call_tool_persists_and_reports_source_graph_disk_metrics(
    monkeypatch, tmp_path
):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)

    async def dispatch_with_disk_hit(name, args):
        server.operation_metrics.set_value("source_graph_phase", "complete")
        server.operation_metrics.set_value("source_graph_cache_tier", "disk")
        server.operation_metrics.set_value(
            "source_graph_disk_validation_outcome", "hit"
        )
        server.operation_metrics.set_value("source_graph_actual_build_count", 0)
        server.operation_metrics.set_value("source_graph_frontend_launch_count", 0)
        server.operation_metrics.set_value("source_graph_disk_lookup_ms", 12.5)
        server.operation_metrics.set_value("source_graph_disk_validate_ms", 10.0)
        server.operation_metrics.set_value("source_graph_disk_hit_count", 1)
        server.operation_metrics.set_value("source_graph_disk_miss_count", 0)
        server.operation_metrics.set_value("source_graph_disk_build_skip_count", 1)
        server.operation_metrics.set_value("source_graph_disk_bytes_read", 4096)
        server.operation_metrics.set_value("source_graph_disk_entry_count", 1)
        server.operation_metrics.set_value("source_graph_disk_bytes", 8192)
        server.operation_metrics.set_value("source_graph_scope", "top.secret")
        return {"driver": None}

    monkeypatch.setattr(server, "_dispatch", dispatch_with_disk_hit)
    await server.call_tool(
        "explain_signal_driver",
        {
            "compile_log": "/private/customer/compile.log",
            "signal_path": "top.secret",
        },
    )

    text = log.read_text()
    record = json.loads(text)
    assert record["tool"] == "explain_signal_driver"
    assert record["diagnostics"] == {
        "source_graph_phase": "complete",
        "source_graph_actual_build_count": 0,
        "source_graph_cache_tier": "disk",
        "source_graph_disk_validation_outcome": "hit",
        "source_graph_frontend_launch_count": 0,
        "source_graph_disk_lookup_ms": 12.5,
        "source_graph_disk_validate_ms": 10.0,
        "source_graph_disk_hit_count": 1,
        "source_graph_disk_miss_count": 0,
        "source_graph_disk_build_skip_count": 1,
        "source_graph_disk_bytes_read": 4096,
        "source_graph_disk_entry_count": 1,
        "source_graph_disk_bytes": 8192,
    }
    assert "/private" not in text
    assert "top.secret" not in text

    report = usage_telemetry.aggregate([record])
    source_graph = report["source_graph"]
    assert source_graph["disk"]["exact_hit_rate"] == 1.0
    assert source_graph["disk"]["build_skip_count"] == 1
    assert source_graph["execution"]["frontend_launch_count"] == 0
    assert source_graph["cache_tiers"]["disk"]["calls"] == 1
    assert "hit-rate=100.0%" in render(report)
