from __future__ import annotations

import subprocess
import sys

import pytest

from config import SourceGraphExecutionConfig, get_source_graph_execution_config
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


def test_source_graph_execution_config_is_namespaced_and_validated(monkeypatch):
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


def test_parent_lifecycle_import_does_not_require_or_import_pyslang():
    script = """
import importlib.util
import sys
assert importlib.util.find_spec('pyslang') is None
import src.source_graph_production
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
