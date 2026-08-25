"""Production lifecycle for the optional, on-demand Source Graph runtime.

This module owns only process/session lifecycle.  It deliberately does not
select connectivity backends, map public schemas, read waveforms, or import
``pyslang``. The first eligible driver/load/path request creates one
:class:`SourceGraphRuntime`; no build or source enumeration happens at server
startup.  Process memory remains the first cache tier; an explicitly enabled
disk tier is constructed lazily and uses the existing isolated one-shot worker
contract on verified misses.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import threading

from config import SourceGraphExecutionConfig

from .source_graph_disk_cache import SourceGraphDiskCache
from .source_graph_runtime import (
    IsolatedSourceGraphProcessRunner,
    SourceGraphRuntime,
    SourceGraphWorkerRunner,
)
from .source_graph_session_runtime import PersistentSourceGraphProcessRunner


RunnerFactory = Callable[[SourceGraphExecutionConfig], SourceGraphWorkerRunner]


def _default_runner_factory(
    config: SourceGraphExecutionConfig,
) -> SourceGraphWorkerRunner:
    if config.semantic_session_enabled:
        return PersistentSourceGraphProcessRunner(
            python_executable=config.python_bin,
            idle_ttl_seconds=config.semantic_session_idle_ttl_sec,
            max_rss_kib=max(config.semantic_session_max_rss_bytes // 1024, 1),
        )
    return IsolatedSourceGraphProcessRunner(python_executable=config.python_bin)


class SourceGraphRuntimeSession:
    """Thread-safe lazy owner for one process-session runtime and memory cache."""

    def __init__(self, runner_factory: RunnerFactory = _default_runner_factory) -> None:
        self._runner_factory = runner_factory
        self._lock = threading.Lock()
        self._runtime: SourceGraphRuntime | None = None
        self._configuration_identity: tuple[object, ...] | None = None

    @property
    def created(self) -> bool:
        """Whether a runtime exists; inspecting this never creates one."""

        with self._lock:
            return self._runtime is not None

    def get(self, config: SourceGraphExecutionConfig) -> SourceGraphRuntime:
        """Return the shared runtime, creating it without starting a build.

        Configuration is process-lifetime state.  Changing worker identity or
        timeout after the first request requires an MCP server restart; silently
        replacing the runtime would discard cache/single-flight state while
        requests may still be using it.
        """

        if not config.valid:
            raise ValueError("invalid Source Graph execution configuration")
        if not config.enabled:
            raise ValueError("Source Graph execution is disabled")
        identity = (
            config.python_bin,
            config.frontend_version,
            config.timeout_sec,
            config.disk_cache_enabled,
            os.fspath(config.disk_cache_root),
            config.disk_cache_max_entries,
            config.disk_cache_max_bytes,
            config.semantic_session_enabled,
            config.semantic_session_idle_ttl_sec,
            config.semantic_session_max_rss_bytes,
            config.semantic_session_max_instances,
            config.semantic_session_max_inputs,
            tuple(sorted(config.runtime_plusarg_allowlist)),
        )
        with self._lock:
            if self._runtime is None:
                disk_cache = (
                    SourceGraphDiskCache(
                        config.disk_cache_root,
                        max_entries=config.disk_cache_max_entries,
                        max_bytes=config.disk_cache_max_bytes,
                    )
                    if config.disk_cache_enabled
                    else None
                )
                self._runtime = SourceGraphRuntime(
                    self._runner_factory(config),
                    disk_cache=disk_cache,
                )
                self._configuration_identity = identity
            elif self._configuration_identity != identity:
                raise RuntimeError(
                    "Source Graph execution configuration changed after initialization"
                )
            return self._runtime


_PROCESS_SESSION = SourceGraphRuntimeSession()


def get_source_graph_runtime(
    config: SourceGraphExecutionConfig,
) -> SourceGraphRuntime:
    """Return the process-shared, lazily-created Source Graph runtime."""

    return _PROCESS_SESSION.get(config)


def source_graph_runtime_created() -> bool:
    """Report lifecycle state without triggering optional dependency work."""

    return _PROCESS_SESSION.created


def _reset_source_graph_runtime_for_tests(
    *, runner_factory: RunnerFactory = _default_runner_factory
) -> None:
    """Replace the process session in tests after all prior flights are idle."""

    global _PROCESS_SESSION
    _PROCESS_SESSION = SourceGraphRuntimeSession(runner_factory)
