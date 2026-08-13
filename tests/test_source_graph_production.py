from __future__ import annotations

import subprocess
import sys

import pytest

from config import (
    ConnectivityRouteConfig,
    SourceGraphExecutionConfig,
    get_connectivity_route_config,
    get_source_graph_execution_config,
)
from src.source_graph_production import SourceGraphRuntimeSession


class NeverRunWorker:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):  # pragma: no cover
        self.calls += 1
        raise AssertionError("lifecycle creation must not start a build")


def _config(**changes) -> SourceGraphExecutionConfig:
    values = {
        "enabled": True,
        "python_bin": "/isolated/python",
        "frontend_version": "11.0.0",
        "timeout_sec": 120.0,
    }
    values.update(changes)
    return SourceGraphExecutionConfig(**values)


def test_runtime_session_is_lazy_and_shared_without_starting_a_build():
    worker = NeverRunWorker()
    factory_calls = 0

    def factory(config):
        nonlocal factory_calls
        factory_calls += 1
        assert config.python_bin == "/isolated/python"
        return worker

    session = SourceGraphRuntimeSession(factory)

    assert session.created is False
    first = session.get(_config())
    second = session.get(_config())

    assert first is second
    assert session.created is True
    assert factory_calls == 1
    assert worker.calls == 0


def test_runtime_session_rejects_invalid_disabled_or_mutated_configuration():
    session = SourceGraphRuntimeSession(lambda _config: NeverRunWorker())

    with pytest.raises(ValueError, match="invalid"):
        session.get(_config(error_code="source_graph_execution_config_invalid"))
    with pytest.raises(ValueError, match="disabled"):
        session.get(_config(enabled=False))

    session.get(_config())
    with pytest.raises(RuntimeError, match="changed after initialization"):
        session.get(_config(python_bin="/different/python"))


def test_opt_in_disk_store_is_lazy_and_part_of_session_identity(tmp_path):
    cache_root = tmp_path / "cache"
    session = SourceGraphRuntimeSession(lambda _config: NeverRunWorker())
    config = _config(
        disk_cache_enabled=True,
        disk_cache_root=cache_root,
        disk_cache_max_entries=3,
        disk_cache_max_bytes=4096,
    )

    runtime = session.get(config)

    assert not cache_root.exists()
    assert runtime.stats_snapshot()["disk_cache_max_entries"] == 3
    assert runtime.stats_snapshot()["disk_cache_max_bytes"] == 4096
    with pytest.raises(RuntimeError, match="changed after initialization"):
        session.get(
            _config(
                disk_cache_enabled=True,
                disk_cache_root=cache_root,
                disk_cache_max_entries=4,
                disk_cache_max_bytes=4096,
            )
        )


def test_source_graph_execution_config_is_namespaced_and_validated(monkeypatch):
    # Keep the default-value assertion independent of developer shell settings.
    monkeypatch.delenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE", raising=False)
    monkeypatch.delenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES", raising=False)
    monkeypatch.delenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES", raising=False)
    monkeypatch.delenv("TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_ROUNDS", raising=False)
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH", "1")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_PYTHON", "/tmp/pinned/bin/python")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_FRONTEND_VERSION", "11.0.0")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_TIMEOUT", "7.5")

    config = get_source_graph_execution_config()

    assert config == SourceGraphExecutionConfig(
        enabled=True,
        python_bin="/tmp/pinned/bin/python",
        frontend_version="11.0.0",
        timeout_sec=7.5,
    )

    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_TIMEOUT", "not-a-number")
    assert get_source_graph_execution_config().error_code == (
        "source_graph_execution_config_invalid"
    )


def test_source_graph_frontier_limits_are_namespaced_and_bounded(monkeypatch):
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES", "257")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_ROUNDS", "7")

    config = get_source_graph_execution_config()

    assert config.valid
    assert config.frontier_max_instances == 257
    assert config.frontier_max_rounds == 7

    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES", "4097")
    assert get_source_graph_execution_config().error_code == (
        "source_graph_frontier_config_invalid"
    )

    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES", "bad")
    assert get_source_graph_execution_config().error_code == (
        "source_graph_frontier_config_invalid"
    )


def test_connectivity_route_is_explicit_and_invalid_values_preserve_auto(monkeypatch):
    monkeypatch.delenv("TRACEWEAVE_CONNECTIVITY_ROUTE", raising=False)
    assert get_connectivity_route_config() == ConnectivityRouteConfig()

    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    assert get_connectivity_route_config() == ConnectivityRouteConfig(
        mode="source_graph"
    )

    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "npi-off")
    assert get_connectivity_route_config() == ConnectivityRouteConfig(
        error_code="connectivity_route_config_invalid"
    )


def test_disk_cache_config_is_namespaced_opt_in_and_bounded(monkeypatch, tmp_path):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("TRACEWEAVE_CACHE_DIR", str(cache_root))
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE", "1")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES", "5")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES", "123456")

    config = get_source_graph_execution_config()

    assert config.valid
    assert config.disk_cache_enabled is True
    assert config.disk_cache_root == cache_root
    assert config.disk_cache_max_entries == 5
    assert config.disk_cache_max_bytes == 123456
    assert not cache_root.exists()

    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES", "0")
    assert get_source_graph_execution_config().error_code == (
        "source_graph_disk_cache_config_invalid"
    )


def test_disabled_disk_cache_ignores_invalid_optional_capacity(monkeypatch):
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE", "0")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES", "-1")
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES", "bad")

    config = get_source_graph_execution_config()

    assert config.valid
    assert config.disk_cache_enabled is False
    assert config.disk_cache_max_entries == 8
    assert config.disk_cache_max_bytes == 512 * 1024 * 1024


def test_server_startup_does_not_require_or_import_pyslang():
    script = """
import importlib.util
import sys
assert importlib.util.find_spec('pyslang') is None
import server
assert 'pyslang' not in sys.modules
print('ok')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "ok"
