from __future__ import annotations

import asyncio
from dataclasses import replace
import threading
import time

import pytest

from config import SourceGraphExecutionConfig
import server
from src import operation_metrics
from src.cancellation import OperationCancelled
from src.connectivity_ir import CoverageGap, CoverageReport, CoverageStatus
import src.connectivity_backend as connectivity_backend
from src.source_graph_contract import SourceGraphScopeReceipt
from src.source_graph_backend import SourceGraphConnectivityBackend
from src.source_graph_disk_cache import SourceGraphDiskCache
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME
from src.source_graph_runtime import (
    PrepareStatus,
    SourceGraphRuntime,
    WorkerBuildResult,
    WorkerResourceMetrics,
)
from tests.connectivity_ir_fixtures import build_hand_ir


@pytest.fixture(autouse=True)
def _clean_hierarchy_store():
    server._handle_store.invalidate()
    yield
    server._handle_store.invalidate()


def _source_config() -> SourceGraphExecutionConfig:
    return SourceGraphExecutionConfig(
        enabled=True,
        python_bin="/isolated/fake-python",
        frontend_version="11.0.0",
        timeout_sec=5.0,
    )


def _probe(*, with_kdb: bool = False) -> dict:
    return {
        "simulator": "xcelium",
        "backend": "static",
        "parser_match": "approximate",
        "kdb_path": "/private/kdb.elab++" if with_kdb else None,
        "kdb_flow": "vcs_two_step" if with_kdb else "none",
        "kdb_validation_status": "usable" if with_kdb else "unavailable",
        "kdb_hint": None,
    }


def _install_source_context(tmp_path) -> tuple[str, str]:
    source = (
        server.os.path.dirname(server.__file__)
        + "/tests/fixtures/source_graph_frontend/hand_connectivity.sv"
    )
    compile_log = tmp_path / "compile.log"
    compile_log.write_text("xrun\n", encoding="utf-8")
    compile_result = {
        "simulator": "xcelium",
        "compile_cwd": str(tmp_path),
        "compile_command": f"xrun {source} -top sg_top",
        "top_modules": ["sg_top"],
        "files": {
            "user": [
                {"path": source, "type": "module", "category": "rtl"},
            ],
            "filtered_count": 0,
        },
        "include_tree": {},
        "filelist_tree": {},
        "interfaces": ["sg_bus"],
        "parse_warnings": [],
    }
    hierarchy = {
        "compile_result": compile_result,
        "component_tree": {
            "sg_top": {
                "bus": {"module": "sg_bus", "children": {}},
                "u_producer": {"module": "sg_producer", "children": {}},
            }
        },
    }
    handle = server.compute_handle(str(compile_log), "xcelium")
    server._handle_store.register(handle, hierarchy)
    return str(compile_log), source


def _production_ir(*, coverage: CoverageReport | None = None):
    ir = replace(
        build_hand_ir(),
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version="11.0.0",
    )
    return replace(ir, coverage=coverage) if coverage is not None else ir


class ReadyWorker:
    def __init__(
        self,
        *,
        ir=None,
        release: asyncio.Event | None = None,
        entered: threading.Event | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        self.ir = ir or _production_ir()
        self.release = release
        self.entered = entered
        self.cancelled = cancelled
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        while self.release is not None and not self.release.is_set():
            if cancel_event.is_set():
                if self.cancelled is not None:
                    self.cancelled.set()
                return WorkerBuildResult.failed(
                    PrepareStatus.CANCELLED,
                    code="request_cancelled",
                    stage="worker_process",
                )
            await asyncio.sleep(0.005)
        gaps = tuple(gap.code for gap in self.ir.coverage.gaps)
        receipt = SourceGraphScopeReceipt(
            scope=request.scope,
            coverage_status=self.ir.coverage.status,
            gap_codes=gaps,
        )
        return WorkerBuildResult.ready(
            self.ir,
            receipt,
            metrics=WorkerResourceMetrics(
                wall_time_ms=2.0,
                cpu_time_ms=1.0,
                rss_start_kib=100,
                rss_peak_kib=140,
                rss_end_kib=110,
            ),
        )


class FailedWorker:
    def __init__(self, status: PrepareStatus) -> None:
        self.status = status
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del request, timeout_seconds, cancel_event
        self.calls += 1
        return WorkerBuildResult.failed(
            self.status,
            code={
                PrepareStatus.DEPENDENCY_BLOCKED: "frontend_unavailable",
                PrepareStatus.BUILD_FAILED: "frontend_build_failed",
                PrepareStatus.WORKER_CRASH: "worker_exit_failure",
                PrepareStatus.TIMED_OUT: "worker_timeout",
            }[self.status],
            stage="worker_process",
        )


class SequenceWorker:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds, cancel_event
        index = self.calls
        self.calls += 1
        item = self.results[index]
        if isinstance(item, PrepareStatus):
            return WorkerBuildResult.failed(
                item,
                code="frontend_build_failed",
                stage="projection",
            )
        receipt = SourceGraphScopeReceipt(
            scope=request.scope,
            coverage_status=item.coverage.status,
            gap_codes=tuple(gap.code for gap in item.coverage.gaps),
        )
        return WorkerBuildResult.ready(item, receipt)


class TrackingStaticBackend:
    name = "static"
    uses_external_worker = False

    def __init__(self) -> None:
        self.driver_calls = 0
        self.load_calls = 0
        self.path_calls = 0

    def find_driver(self, **kwargs):
        self.driver_calls += 1
        return {
            "signal_path": kwargs["signal_path"],
            "wave_path": kwargs["wave_path"],
            "resolved_rtl_name": kwargs["signal_path"].rsplit(".", 1)[-1],
            "resolved_module": "legacy_static_module",
            "resolved_instance_path": kwargs["signal_path"].rsplit(".", 1)[0],
            "driver_status": "partial",
            "driver_kind": "unknown",
            "source_file": None,
            "source_line": None,
            "expression_summary": None,
            "upstream_signals": [],
            "confidence": "low",
            "recursive": kwargs["recursive"],
            "driver_chain": None,
            "chain_summary": None,
            "backend": "static",
        }

    def find_loads(self, **kwargs):
        self.load_calls += 1
        return {
            "signal_path": kwargs["signal_path"],
            "resolved_rtl_name": kwargs["signal_path"].rsplit(".", 1)[-1],
            "resolved_module": "legacy_static_module",
            "resolved_instance_path": kwargs["signal_path"].rsplit(".", 1)[0],
            "loads": [],
            "completeness": "shallow_only",
            "stopped_at": "no_static_load_found",
            "unsupported_reason": None,
        }

    def find_path(self, **kwargs):
        self.path_calls += 1
        return {
            "from_signal": kwargs["from_signal"],
            "to_signal": kwargs["to_signal"],
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": kwargs["expand_assigns"],
            "unsupported_reason": "static_backend_no_path_api",
        }


class FakeNpiBackend:
    name = "verdi_npi"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls = 0

    def find_driver(self, **kwargs):
        self.calls += 1
        return {
            **self.result,
            "signal_path": kwargs["signal_path"],
            "wave_path": kwargs["wave_path"],
        }

    def find_loads(self, **kwargs):
        self.calls += 1
        return {**self.result, "signal_path": kwargs["signal_path"]}

    def find_path(self, **kwargs):
        self.calls += 1
        return {
            **self.result,
            "from_signal": kwargs["from_signal"],
            "to_signal": kwargs["to_signal"],
            "expand_assigns": kwargs["expand_assigns"],
        }


def _patch_common(
    monkeypatch,
    *,
    runtime: SourceGraphRuntime | None,
    static: TrackingStaticBackend,
    npi_backend=None,
    with_kdb: bool = False,
):
    monkeypatch.setattr(server, "_check_prerequisites", lambda name, args: None)
    monkeypatch.setattr(
        server, "_safe_probe_backend", lambda *args: _probe(with_kdb=with_kdb)
    )
    monkeypatch.setattr(server, "get_source_graph_execution_config", _source_config)
    if runtime is not None:
        monkeypatch.setattr(server, "get_source_graph_runtime", lambda config: runtime)
    else:
        monkeypatch.setattr(
            server,
            "get_source_graph_runtime",
            lambda config: (_ for _ in ()).throw(
                AssertionError("Source Graph runtime must not be called")
            ),
        )
    monkeypatch.setattr(
        connectivity_backend, "StaticConnectivityBackend", lambda: static
    )
    if npi_backend is None:
        monkeypatch.setattr(
            connectivity_backend,
            "select_backend",
            lambda status, *, fallback=None: fallback,
        )
    else:
        monkeypatch.setattr(
            connectivity_backend,
            "select_backend",
            lambda status, *, fallback=None: npi_backend,
        )


def _driver_args(compile_log: str, signal: str = "sg_top.lane_data[15:8]") -> dict:
    return {
        "signal_path": signal,
        "wave_path": "/private/wave.fsdb",
        "compile_log": compile_log,
        "simulator": "xcelium",
        "top_hint": "sg_top",
        "recursive": True,
        "max_depth": 8,
    }


def _load_args(compile_log: str) -> dict:
    return {
        "signal_path": "sg_top.bus.data[7:0]",
        "compile_log": compile_log,
        "simulator": "xcelium",
        "top_hint": "sg_top",
        "max_depth": 8,
        "include_expr": True,
    }


def _path_args(
    compile_log: str,
    *,
    from_signal: str = "sg_top.u_producer.seed[7:0]",
    to_signal: str = "sg_top.bus.data[15:8]",
    expand_assigns: bool = True,
) -> dict:
    return {
        "from_signal": from_signal,
        "to_signal": to_signal,
        "compile_log": compile_log,
        "simulator": "xcelium",
        "top_hint": "sg_top",
        "expand_assigns": expand_assigns,
    }


@pytest.mark.anyio
async def test_npi_success_skips_source_graph_and_static(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    static = TrackingStaticBackend()
    npi = FakeNpiBackend(
        {
            "resolved_rtl_name": "q",
            "resolved_module": "dut",
            "resolved_instance_path": "sg_top",
            "driver_status": "resolved",
            "driver_kind": "always_ff",
            "source_file": "rtl/dut.sv",
            "source_line": 12,
            "upstream_signals": [],
            "confidence": "exact",
            "recursive": True,
            "driver_chain": None,
            "chain_summary": None,
            "backend": "verdi_npi",
        }
    )
    _patch_common(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "verdi_npi"
    assert result.backend_status.actual_backend == "verdi_npi"
    assert result.backend_status.single_backend_provenance is True
    assert [item.backend for item in result.backend_status.attempted_backends] == [
        "verdi_npi"
    ]
    assert result.backend_status.source_graph is None
    assert npi.calls == 1
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_npi_load_success_skips_source_graph_and_static(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    static = TrackingStaticBackend()
    npi = FakeNpiBackend(
        {
            "resolved_rtl_name": "data[7:0]",
            "resolved_module": "sg_bus",
            "resolved_instance_path": "sg_top.bus",
            "loads": [],
            "completeness": "exact",
            "stopped_at": "no_npi_loads",
            "unsupported_reason": None,
            "backend": "verdi_npi",
        }
    )
    _patch_common(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("find_signal_loads", _load_args(compile_log))

    assert result.backend == "verdi_npi"
    assert result.backend_status.actual_backend == "verdi_npi"
    assert result.backend_status.source_graph is None
    assert npi.calls == 1
    assert static.load_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["driver", "loads", "path"])
async def test_explicit_source_graph_route_skips_npi_with_usable_kdb(
    monkeypatch, tmp_path, operation
):
    policy_selector = connectivity_backend.select_backend
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    npi = FakeNpiBackend({})
    _patch_common(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setattr(connectivity_backend, "select_backend", policy_selector)

    if operation == "driver":
        result = await server._dispatch(
            "explain_signal_driver", _driver_args(compile_log)
        )
    elif operation == "loads":
        result = await server._dispatch("find_signal_loads", _load_args(compile_log))
    else:
        result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    status = result.backend_status
    assert result.backend == "source_graph"
    assert status.kdb_validation_status == "usable"
    assert status.connectivity_route == "source_graph"
    assert status.connectivity_route_error is None
    assert status.selected_backend == "source_graph"
    assert status.actual_backend == "source_graph"
    assert status.fallback_reason == "npi_skipped_by_policy"
    assert [
        (item.backend, item.status, item.reason) for item in status.attempted_backends
    ] == [
        ("verdi_npi", "skipped", "npi_skipped_by_policy"),
        ("source_graph", "success", None),
    ]
    assert npi.calls == 0
    assert static.driver_calls + static.load_calls + static.path_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["driver", "loads"])
async def test_npi_unavailable_routes_public_driver_and_loads_to_source_graph(
    monkeypatch, tmp_path, tool
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    if tool == "driver":
        result = await server._dispatch(
            "explain_signal_driver", _driver_args(compile_log)
        )
        assert result.driver_status == "resolved"
        assert result.source_info_origin == "source_graph"
        assert result.confidence == "exact"
        payload_backends = {hop.backend for hop in result.driver_chain or []}
        assert payload_backends <= {"source_graph"}
    else:
        result = await server._dispatch("find_signal_loads", _load_args(compile_log))
        assert result.loads
        assert {item.backend for item in result.loads} == {"source_graph"}
        assert {item.source_info_origin for item in result.loads} == {"source_graph"}
        assert result.completeness == "exact"

    assert result.backend == "source_graph"
    assert result.backend_status.selected_backend == "source_graph"
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.single_backend_provenance is True
    assert result.backend_status.source_graph.prepare_status == "ready"
    assert result.backend_status.source_graph.query_status == "found"
    assert result.backend_status.source_graph.coverage_status == "complete"
    assert result.backend_status.source_graph.coverage_files_total == 1
    assert result.backend_status.source_graph.coverage_files_projected == 1
    assert result.backend_status.source_graph.coverage_diagnostic_count == 0
    assert result.backend_status.source_graph.coverage_blocking_diagnostic_count == 0
    assert result.backend_status.source_graph.coverage_gap_count == 0
    assert len(result.backend_status.source_graph.build_key_sha256) == 64
    assert len(result.backend_status.source_graph.compile_fingerprint_sha256) == 64
    assert len(result.backend_status.source_graph.ir_fingerprint_sha256) == 64
    assert static.driver_calls + static.load_calls == 0


@pytest.mark.anyio
async def test_warm_public_request_reuses_compile_snapshot_and_ir(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    first = await server._dispatch("explain_signal_driver", _driver_args(compile_log))
    second = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert first.backend_status.source_graph.cache_disposition == "miss"
    assert second.backend_status.source_graph.cache_disposition == "hit_exact"
    assert (
        second.backend_status.source_graph.adapter["manifest"][
            "fingerprint_cache_disposition"
        ]
        == "hit_session_snapshot"
    )
    assert worker.calls == 1
    assert static.driver_calls == 0
    source_payload = first.model_dump(mode="json")["backend_status"]["source_graph"]
    assert "cache_tier" not in source_payload
    assert "disk_validation_outcome" not in source_payload
    assert "frontend_launch_count" not in source_payload["metrics"]
    assert all(not key.startswith("disk_") for key in source_payload["metrics"])


@pytest.mark.anyio
async def test_fresh_runtime_public_driver_load_and_path_use_exact_disk_artifact(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    store_root = tmp_path / "source-graph-cache"
    signal = "sg_top.u_producer.seed[7:0]"
    static = TrackingStaticBackend()
    cold_worker = ReadyWorker()
    cold_runtime = SourceGraphRuntime(
        cold_worker,
        disk_cache=SourceGraphDiskCache(store_root),
    )
    _patch_common(monkeypatch, runtime=cold_runtime, static=static)

    cold = await server._dispatch(
        "explain_signal_driver", _driver_args(compile_log, signal)
    )
    disk_worker = ReadyWorker()
    disk_runtime = SourceGraphRuntime(
        disk_worker,
        disk_cache=SourceGraphDiskCache(store_root),
    )
    monkeypatch.setattr(server, "get_source_graph_runtime", lambda config: disk_runtime)
    load_args = _load_args(compile_log)
    load_args["signal_path"] = signal
    path_args = _path_args(
        compile_log,
        from_signal=signal,
        to_signal=signal,
    )

    driver = await server._dispatch(
        "explain_signal_driver", _driver_args(compile_log, signal)
    )
    loads = await server._dispatch("find_signal_loads", load_args)
    path = await server._dispatch("trace_signal_path", path_args)

    assert cold.backend_status.source_graph.cache_tier == "build"
    assert cold.backend_status.source_graph.artifact_reuse == "cold"
    disk_receipt = driver.backend_status.source_graph
    assert driver.backend == "source_graph"
    assert driver.source_info_origin == "source_graph"
    assert disk_receipt.cache_disposition == "miss"
    assert disk_receipt.cache_tier == "disk"
    assert disk_receipt.disk_validation_outcome == "hit"
    assert disk_receipt.artifact_reuse == "disk_exact_hit"
    assert disk_receipt.metrics.actual_build_count == 0
    assert disk_receipt.metrics.frontend_launch_count == 0
    assert loads.backend == "source_graph"
    assert {item.backend for item in loads.loads} == {"source_graph"}
    assert path.backend == "source_graph"
    assert path.found is True
    assert {hop.backend for hop in path.path} == {"source_graph"}
    assert loads.backend_status.source_graph.cache_tier == "memory"
    assert path.backend_status.source_graph.cache_tier == "memory"
    assert cold_worker.calls == 1
    assert disk_worker.calls == 0
    assert static.driver_calls + static.load_calls + static.path_calls == 0


@pytest.mark.anyio
async def test_npi_failure_uses_source_graph_before_static(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    npi = FakeNpiBackend(
        {
            "resolved_rtl_name": "lane_data",
            "driver_status": "deferred",
            "recursive": True,
            "backend": "source_graph_deferred",
            "_npi_fallback_reason": "npi_load_failed",
            "_connectivity_fallback_deferred": True,
        }
    )
    _patch_common(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "source_graph"
    assert result.backend_status.selected_backend == "verdi_npi"
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.fallback_reason == "npi_load_failed"
    assert [item.status for item in result.backend_status.attempted_backends] == [
        "failed",
        "success",
    ]
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_raised_npi_failure_still_routes_to_source_graph(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()

    class RaisingNpiBackend:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def find_driver(self, **kwargs):
            del kwargs
            raise RuntimeError("private NPI diagnostic")

    _patch_common(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=RaisingNpiBackend(),
        with_kdb=True,
    )

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "source_graph"
    assert result.backend_status.fallback_reason == "npi_query_failed"
    assert result.backend_status.attempted_backends[0].reason == "npi_query_failed"
    assert "private NPI diagnostic" not in result.model_dump_json()
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_npi_backend_initialization_failure_still_tries_source_graph(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static, with_kdb=True)
    monkeypatch.setattr(
        connectivity_backend,
        "select_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private init")),
    )

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "source_graph"
    assert result.backend_status.attempted_backends[0].status == "failed"
    assert result.backend_status.attempted_backends[0].reason == (
        "npi_backend_initialization_failed"
    )
    assert "private init" not in result.model_dump_json()
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_npi_cancellation_never_continues_to_source_graph_or_static(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    static = TrackingStaticBackend()

    class CancelledNpiBackend:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def find_driver(self, **kwargs):
            del kwargs
            raise OperationCancelled("cancelled")

    _patch_common(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=CancelledNpiBackend(),
        with_kdb=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert static.driver_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prepare_status", "expected_reason", "attempt_status"),
    [
        (
            PrepareStatus.DEPENDENCY_BLOCKED,
            "source_graph_dependency_blocked",
            "failed",
        ),
        (PrepareStatus.BUILD_FAILED, "source_graph_build_failed", "failed"),
        (PrepareStatus.WORKER_CRASH, "source_graph_worker_crash", "failed"),
        (PrepareStatus.TIMED_OUT, "source_graph_timed_out", "timed_out"),
    ],
)
async def test_source_graph_prepare_failures_fall_back_to_legacy_static(
    monkeypatch,
    tmp_path,
    prepare_status,
    expected_reason,
    attempt_status,
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = FailedWorker(prepare_status)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "static"
    assert result.resolved_module == "legacy_static_module"
    assert result.backend_status.actual_backend == "static"
    assert result.backend_status.single_backend_provenance is True
    assert result.backend_status.attempted_backend == "source_graph"
    assert result.backend_status.fallback_reason == expected_reason
    assert result.backend_status.source_graph.prepare_status == prepare_status.value
    assert result.backend_status.source_graph.fallback_used is True
    assert result.backend_status.attempted_backends[-2].status == attempt_status
    assert static.driver_calls == 1
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_partial_positive_stays_partial_and_does_not_fall_back(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    coverage = CoverageReport(
        status=CoverageStatus.PARTIAL,
        files_total=1,
        files_projected=1,
        gaps=(
            CoverageGap(
                code="protected_payload",
                message="protected payload unavailable",
                impact=CoverageStatus.PARTIAL,
                constructs=("protected",),
                scopes=("*",),
            ),
        ),
    )
    runtime = SourceGraphRuntime(ReadyWorker(ir=_production_ir(coverage=coverage)))
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "source_graph"
    assert result.confidence == "partial"
    assert result.backend_status.source_graph.coverage_status == "partial"
    assert result.backend_status.source_graph.query_confidence == "partial"
    assert "protected_payload" in (
        result.backend_status.source_graph.coverage_gap_codes
    )
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_complete_not_connected_and_inconclusive_no_match_are_distinct(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    signal = "sg_top.runtime_force"

    complete_runtime = SourceGraphRuntime(ReadyWorker())
    complete_static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=complete_runtime, static=complete_static)
    complete = await server._dispatch(
        "explain_signal_driver", _driver_args(compile_log, signal)
    )

    assert complete.backend == "source_graph"
    assert complete.driver_status == "not_connected"
    assert complete.confidence == "exact"
    assert complete.backend_status.source_graph.query_status == "not_connected"
    assert complete_static.driver_calls == 0

    gap = CoverageGap(
        code="runtime_force",
        message="runtime force is outside the objective",
        impact=CoverageStatus.INCONCLUSIVE,
        constructs=("force",),
        scopes=(signal,),
    )
    coverage = CoverageReport(
        status=CoverageStatus.PARTIAL,
        files_total=1,
        files_projected=1,
        gaps=(gap,),
    )
    inconclusive_runtime = SourceGraphRuntime(
        ReadyWorker(ir=_production_ir(coverage=coverage))
    )
    inconclusive_static = TrackingStaticBackend()
    _patch_common(
        monkeypatch,
        runtime=inconclusive_runtime,
        static=inconclusive_static,
    )
    inconclusive = await server._dispatch(
        "explain_signal_driver", _driver_args(compile_log, signal)
    )

    assert inconclusive.backend == "static"
    assert inconclusive.driver_status == "partial"
    assert inconclusive.backend_status.source_graph.query_status == "inconclusive"
    assert inconclusive.backend_status.source_graph.coverage_status == "inconclusive"
    assert inconclusive.backend_status.fallback_reason == (
        "source_graph_coverage_inconclusive"
    )
    assert inconclusive_static.driver_calls == 1


@pytest.mark.anyio
async def test_explicit_mixed_npi_provenance_is_discarded(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    npi = FakeNpiBackend(
        {
            "resolved_rtl_name": "lane_data",
            "resolved_module": "dut",
            "resolved_instance_path": "sg_top",
            "driver_status": "resolved",
            "driver_kind": "always_ff",
            "source_file": "dut.sv",
            "source_line": 5,
            "confidence": "exact",
            "recursive": True,
            "backend": "verdi_npi",
            "driver_chain": [
                {
                    "depth": 0,
                    "signal_path": "sg_top.lane_data",
                    "backend": "static",
                }
            ],
        }
    )
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "source_graph"
    assert {hop.backend for hop in result.driver_chain or []} <= {"source_graph"}
    assert result.backend_status.attempted_backends[0].status == "inconclusive"
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_mixed_source_graph_provenance_is_rejected_as_a_whole(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    class MixedSourceBackend:
        name = "source_graph"
        uses_external_worker = False

        def __init__(self, entry):
            del entry

        def find_driver(self, **kwargs):
            return {
                "signal_path": kwargs["signal_path"],
                "wave_path": kwargs["wave_path"],
                "resolved_rtl_name": "lane_data",
                "driver_status": "resolved",
                "recursive": True,
                "backend": "source_graph",
                "driver_chain": [
                    {
                        "depth": 0,
                        "signal_path": kwargs["signal_path"],
                        "backend": "static",
                    }
                ],
                "_source_graph_query_receipt": {
                    "status": "found",
                    "coverage_status": "complete",
                    "confidence": "exact",
                    "match_count": 1,
                    "unresolved_boundary_codes": [],
                    "traversed_binding_edges": 0,
                    "max_depth": 8,
                },
            }

    monkeypatch.setattr(server, "SourceGraphConnectivityBackend", MixedSourceBackend)

    result = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert result.backend == "static"
    assert result.backend_status.actual_backend == "static"
    assert result.backend_status.source_graph.blocker.code == (
        "mixed_provenance_rejected"
    )
    assert result.backend_status.fallback_reason == (
        "source_graph_mixed_provenance_rejected"
    )
    assert static.driver_calls == 1


@pytest.mark.anyio
async def test_same_key_public_concurrent_requests_build_once(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker = ReadyWorker(release=release, entered=entered)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    tasks = [
        asyncio.create_task(
            server._dispatch("explain_signal_driver", _driver_args(compile_log))
        )
        for _ in range(4)
    ]
    assert await asyncio.to_thread(entered.wait, 2)
    for _ in range(200):
        if runtime.stats_snapshot()["coalesced_waiter_count"] == 3:
            break
        await asyncio.sleep(0.005)
    release.set()
    results = await asyncio.gather(*tasks)

    assert worker.calls == 1
    assert runtime.stats_snapshot()["actual_build_count"] == 1
    assert runtime.stats_snapshot()["coalesced_waiter_count"] == 3
    assert {result.backend for result in results} == {"source_graph"}
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_one_public_waiter_cancel_does_not_cancel_other_waiter(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    cancelled = threading.Event()
    worker = ReadyWorker(release=release, entered=entered, cancelled=cancelled)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    first = asyncio.create_task(
        server._dispatch("explain_signal_driver", _driver_args(compile_log))
    )
    second = asyncio.create_task(
        server._dispatch("explain_signal_driver", _driver_args(compile_log))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    for _ in range(200):
        if runtime.stats_snapshot()["coalesced_waiter_count"] == 1:
            break
        await asyncio.sleep(0.005)
    assert runtime.stats_snapshot()["coalesced_waiter_count"] == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    surviving = await second

    assert surviving.backend == "source_graph"
    assert worker.calls == 1
    assert cancelled.is_set() is False
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_sole_public_waiter_cancel_terminates_worker_without_fallback(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    cancelled = threading.Event()
    worker = ReadyWorker(release=release, entered=entered, cancelled=cancelled)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    task = asyncio.create_task(
        server._dispatch("explain_signal_driver", _driver_args(compile_log))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(cancelled.wait, 2)
    await runtime.wait_idle()

    assert runtime.stats_snapshot()["cache_entry_count"] == 0
    assert static.driver_calls == 0


@pytest.mark.anyio
async def test_public_failure_does_not_poison_cache_and_retry_is_ready(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = SequenceWorker([PrepareStatus.BUILD_FAILED, _production_ir()])
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    failed = await server._dispatch("explain_signal_driver", _driver_args(compile_log))
    retried = await server._dispatch("explain_signal_driver", _driver_args(compile_log))

    assert failed.backend == "static"
    assert retried.backend == "source_graph"
    assert worker.calls == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_cold_public_build_does_not_block_light_call_or_hold_wave_lock(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker = ReadyWorker(release=release, entered=entered)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    wave_lock = server._wave_locks_for(["/private/wave.fsdb"])[0]

    task = asyncio.create_task(
        server._dispatch("explain_signal_driver", _driver_args(compile_log))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    assert wave_lock.locked() is False
    started = time.perf_counter()
    light = await server._dispatch("cursor_list", {})
    elapsed = time.perf_counter() - started
    release.set()
    result = await task

    assert isinstance(light.cursors, list)
    assert elapsed < 0.5
    assert result.backend == "source_graph"
    assert wave_lock.locked() is False


@pytest.mark.anyio
async def test_source_graph_operation_metrics_are_numeric_or_fixed_labels_only(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        await server._dispatch("explain_signal_driver", _driver_args(compile_log))
        operation_metrics.set_value("source_graph_signal", "sg_top.secret")
    finally:
        operation_metrics.pop(token)

    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["source_graph_phase"] == "complete"
    assert "source_graph_signal" not in snapshot
    assert "source_graph_cache_tier" not in snapshot
    assert "source_graph_disk_validation_outcome" not in snapshot
    fixed_labels = {
        "source_graph_phase",
        "source_graph_cache_tier",
        "source_graph_disk_validation_outcome",
    }
    assert all(
        key in fixed_labels
        or (not isinstance(value, bool) and isinstance(value, (int, float)))
        for key, value in snapshot.items()
    )
    assert all("signal" not in key and "path" not in key for key in snapshot)


@pytest.mark.anyio
async def test_driver_load_path_and_expand_toggle_reuse_same_bounded_artifact(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    signal = "sg_top.u_producer.seed[7:0]"
    driver_args = _driver_args(compile_log, signal)
    load_args = _load_args(compile_log)
    load_args["signal_path"] = signal
    path_args = _path_args(
        compile_log,
        from_signal=signal,
        to_signal=signal,
        expand_assigns=True,
    )

    driver = await server._dispatch("explain_signal_driver", driver_args)
    loads = await server._dispatch("find_signal_loads", load_args)
    path = await server._dispatch("trace_signal_path", path_args)
    toggled = await server._dispatch(
        "trace_signal_path", {**path_args, "expand_assigns": False}
    )

    results = (driver, loads, path, toggled)
    receipts = [result.backend_status.source_graph for result in results]
    assert {result.backend for result in results} == {"source_graph"}, [
        (
            result.backend,
            result.backend_status.source_graph.query_status,
            result.backend_status.fallback_reason,
        )
        for result in results
    ]
    assert path.found is True
    assert toggled.found is True
    assert worker.calls == 1
    assert runtime.stats_snapshot()["actual_build_count"] == 1
    assert [receipt.artifact_reuse for receipt in receipts] == [
        "cold",
        "exact_hit",
        "exact_hit",
        "exact_hit",
    ]
    assert [receipt.cache_lookup_reason for receipt in receipts] == [
        "no_cached_artifact",
        "exact_artifact",
        "exact_artifact",
        "exact_artifact",
    ]
    assert len({receipt.artifact_fingerprint_sha256 for receipt in receipts}) == 1
    assert len({receipt.query_fingerprint_sha256 for receipt in receipts}) == 4
    assert all(receipt.scope_match.relation == "exact" for receipt in receipts)
    assert static.driver_calls + static.load_calls + static.path_calls == 0


@pytest.mark.anyio
async def test_cancelled_cached_query_does_not_fallback_or_pollute_artifact(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    args = _driver_args(compile_log, "sg_top.u_producer.seed[7:0]")

    ready = await server._dispatch("explain_signal_driver", args)
    original = SourceGraphConnectivityBackend.find_driver

    def cancelled_query(self, *query_args, **query_kwargs):
        del self, query_args, query_kwargs
        raise OperationCancelled("query cancelled")

    monkeypatch.setattr(
        SourceGraphConnectivityBackend,
        "find_driver",
        cancelled_query,
    )
    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("explain_signal_driver", args)
    monkeypatch.setattr(SourceGraphConnectivityBackend, "find_driver", original)
    warm = await server._dispatch("explain_signal_driver", args)

    assert ready.backend == "source_graph"
    assert warm.backend == "source_graph"
    assert warm.backend_status.source_graph.artifact_reuse == "exact_hit"
    assert worker.calls == 1
    assert runtime.stats_snapshot()["cache_entry_count"] == 1
    assert static.driver_calls + static.load_calls + static.path_calls == 0


@pytest.mark.anyio
async def test_larger_path_artifact_dominates_endpoint_but_reverse_is_cold(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    cross_scope_path = _path_args(compile_log)

    path = await server._dispatch("trace_signal_path", cross_scope_path)
    endpoint = await server._dispatch(
        "explain_signal_driver",
        _driver_args(compile_log, "sg_top.u_producer.seed[7:0]"),
    )

    assert path.backend == "source_graph"
    assert endpoint.backend == "source_graph"
    assert worker.calls == 1
    receipt = endpoint.backend_status.source_graph
    assert receipt.artifact_reuse == "dominating_hit"
    assert receipt.cache_lookup_reason == "dominating_artifact"
    assert receipt.scope_match.relation == "superset"
    assert receipt.artifact_fingerprint_sha256 != (
        receipt.selected_artifact_fingerprint_sha256
    )

    second_worker = ReadyWorker()
    second_runtime = SourceGraphRuntime(second_worker)
    _patch_common(monkeypatch, runtime=second_runtime, static=TrackingStaticBackend())
    await server._dispatch(
        "explain_signal_driver",
        _driver_args(compile_log, "sg_top.u_producer.seed[7:0]"),
    )
    larger = await server._dispatch("trace_signal_path", cross_scope_path)

    assert larger.backend == "source_graph"
    assert larger.backend_status.source_graph.artifact_reuse == "cold"
    assert (
        larger.backend_status.source_graph.cache_lookup_reason
        == "cached_scope_not_dominating"
    )
    assert second_worker.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "npi_result",
    [
        {
            "found": True,
            "hops": 1,
            "path": [
                {
                    "index": 0,
                    "net_path": "sg_top.u_producer.seed[7:0]",
                    "scope_inst": "sg_top.u_producer",
                    "is_endpoint": True,
                },
                {
                    "index": 1,
                    "net_path": "sg_top.bus.data[15:8]",
                    "scope_inst": "sg_top.bus",
                    "is_endpoint": True,
                },
            ],
            "unsupported_reason": None,
        },
        {
            "found": False,
            "hops": 0,
            "path": [],
            "unsupported_reason": "not_connected",
        },
    ],
)
async def test_path_npi_authoritative_results_skip_source_graph_and_static(
    monkeypatch, tmp_path, npi_result
):
    compile_log, _ = _install_source_context(tmp_path)
    static = TrackingStaticBackend()
    npi = FakeNpiBackend(npi_result)
    _patch_common(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.backend == "verdi_npi"
    assert result.backend_status.actual_backend == "verdi_npi"
    assert result.backend_status.source_graph is None
    assert [item.backend for item in result.backend_status.attempted_backends] == [
        "verdi_npi"
    ]
    assert npi.calls == 1
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_npi_unavailable_routes_to_source_graph_found(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.found is True
    assert result.backend == "source_graph"
    assert result.hops == 2
    assert [hop.net_path for hop in result.path] == [
        "sg_top.u_producer.seed[7:0]",
        "sg_top.u_producer.bus.data[15:8]",
        "sg_top.bus.data[15:8]",
    ]
    assert {hop.backend for hop in result.path} == {"source_graph"}
    assert result.path[1].edge_kind == "procedural_assign"
    assert result.path[1].edge_id == "sg_producer:always_comb:84:bus.data"
    assert result.backend_status.selected_backend == "source_graph"
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.single_backend_provenance is True
    receipt = result.backend_status.source_graph
    assert receipt.prepare_status == "ready"
    assert receipt.query_status == "found"
    assert receipt.path_edge_count == 2
    assert receipt.coverage_status == "complete"
    assert receipt.fallback_used is False
    assert static.path_calls == 0
    assert worker.calls == 1


@pytest.mark.anyio
async def test_path_internal_npi_fallback_defers_static_until_after_source_graph(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    npi = FakeNpiBackend(
        {
            "found": False,
            "hops": 0,
            "path": [],
            "unsupported_reason": "connectivity_fallback_deferred",
            "_npi_fallback_reason": "npi_load_failed",
            "backend": "source_graph_deferred",
        }
    )
    _patch_common(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.backend == "source_graph"
    assert result.backend_status.selected_backend == "verdi_npi"
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.fallback_reason == "npi_load_failed"
    assert [item.backend for item in result.backend_status.attempted_backends] == [
        "verdi_npi",
        "source_graph",
    ]
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_source_graph_complete_negative_is_authoritative(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch(
        "trace_signal_path",
        _path_args(
            compile_log,
            from_signal="sg_top.runtime_force",
            to_signal="sg_top.seed",
            expand_assigns=False,
        ),
    )

    assert result.found is False
    assert result.unsupported_reason == "not_connected"
    assert result.backend == "source_graph"
    assert result.backend_status.source_graph.query_status == "not_connected"
    assert result.backend_status.source_graph.coverage_status == "complete"
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_source_graph_partial_positive_remains_usable(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    gap = CoverageGap(
        code="scoped_gap",
        message="bounded projection retains a scoped gap",
        impact=CoverageStatus.PARTIAL,
        scopes=("sg_top.u_producer",),
    )
    partial = replace(
        _production_ir(),
        coverage=CoverageReport(
            status=CoverageStatus.PARTIAL,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )
    runtime = SourceGraphRuntime(ReadyWorker(ir=partial))
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.found is True
    assert result.backend == "source_graph"
    assert result.backend_status.source_graph.query_status == "found"
    assert result.backend_status.source_graph.coverage_status == "partial"
    assert result.backend_status.source_graph.query_confidence == "partial"
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_source_graph_inconclusive_negative_falls_back_to_static(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    gap = CoverageGap(
        code="objective_exclusion",
        message="runtime behavior can affect this endpoint",
        impact=CoverageStatus.INCONCLUSIVE,
        scopes=("sg_top.runtime_force",),
    )
    inconclusive = replace(
        _production_ir(),
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )
    runtime = SourceGraphRuntime(ReadyWorker(ir=inconclusive))
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch(
        "trace_signal_path",
        _path_args(
            compile_log,
            from_signal="sg_top.runtime_force",
            to_signal="sg_top.seed",
        ),
    )

    assert result.found is False
    assert result.unsupported_reason == "static_backend_no_path_api"
    assert result.backend == "static"
    assert result.backend_status.actual_backend == "static"
    assert result.backend_status.fallback_reason == (
        "source_graph_coverage_inconclusive"
    )
    receipt = result.backend_status.source_graph
    assert receipt.query_status == "inconclusive"
    assert receipt.coverage_status == "inconclusive"
    assert receipt.coverage_gap_codes == ["objective_exclusion"]
    assert receipt.fallback_used is True
    assert static.path_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prepare_status", "reason", "attempt_status"),
    [
        (
            PrepareStatus.DEPENDENCY_BLOCKED,
            "source_graph_dependency_blocked",
            "failed",
        ),
        (PrepareStatus.BUILD_FAILED, "source_graph_build_failed", "failed"),
        (PrepareStatus.WORKER_CRASH, "source_graph_worker_crash", "failed"),
        (PrepareStatus.TIMED_OUT, "source_graph_timed_out", "timed_out"),
    ],
)
async def test_path_source_graph_prepare_failures_use_static_whole_result(
    monkeypatch, tmp_path, prepare_status, reason, attempt_status
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(FailedWorker(prepare_status))
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.backend == "static"
    assert result.unsupported_reason == "static_backend_no_path_api"
    assert result.backend_status.fallback_reason == reason
    source_attempt = result.backend_status.attempted_backends[-2]
    assert source_attempt.backend == "source_graph"
    assert source_attempt.status == attempt_status
    assert result.backend_status.source_graph.fallback_used is True
    assert static.path_calls == 1


@pytest.mark.anyio
async def test_path_npi_cancellation_never_falls_back(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    static = TrackingStaticBackend()

    class CancellingNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def find_path(self, **kwargs):
            del kwargs
            raise OperationCancelled("cancelled")

    _patch_common(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=CancellingNpi(),
        with_kdb=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("trace_signal_path", _path_args(compile_log))
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_mixed_source_graph_provenance_is_rejected_as_whole(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    class MixedBackend:
        name = "source_graph"

        def __init__(self, entry):
            del entry

        def find_path(self, **kwargs):
            return {
                "from_signal": kwargs["from_signal"],
                "to_signal": kwargs["to_signal"],
                "found": True,
                "hops": 1,
                "path": [
                    {
                        "index": 0,
                        "net_path": kwargs["from_signal"],
                        "is_endpoint": True,
                        "backend": "source_graph",
                    },
                    {
                        "index": 1,
                        "net_path": kwargs["to_signal"],
                        "is_endpoint": True,
                        "backend": "static",
                    },
                ],
                "expand_assigns": kwargs["expand_assigns"],
                "unsupported_reason": None,
                "backend": "source_graph",
                "_source_graph_query_receipt": {
                    "status": "found",
                    "coverage_status": "complete",
                    "confidence": "exact",
                    "match_count": 1,
                    "unresolved_boundary_codes": [],
                    "path_edge_count": 1,
                },
            }

    monkeypatch.setattr(server, "SourceGraphConnectivityBackend", MixedBackend)

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.backend == "static"
    assert result.path == []
    assert result.backend_status.fallback_reason == (
        "source_graph_mixed_provenance_rejected"
    )
    assert result.backend_status.source_graph.blocker.code == (
        "mixed_provenance_rejected"
    )
    assert static.path_calls == 1


@pytest.mark.anyio
async def test_concurrent_exact_path_requests_single_flight_one_build(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker = ReadyWorker(release=release, entered=entered)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    tasks = [
        asyncio.create_task(
            server._dispatch("trace_signal_path", _path_args(compile_log))
        )
        for _ in range(4)
    ]
    assert await asyncio.to_thread(entered.wait, 2)
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(*tasks)

    assert {result.backend for result in results} == {"source_graph"}
    assert worker.calls == 1
    assert (
        sum(
            result.backend_status.source_graph.metrics.actual_build_count
            for result in results
        )
        == 1
    )
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_waiter_cancellation_does_not_cancel_surviving_waiter(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker = ReadyWorker(release=release, entered=entered)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    cancelled = asyncio.create_task(
        server._dispatch("trace_signal_path", _path_args(compile_log))
    )
    surviving = asyncio.create_task(
        server._dispatch("trace_signal_path", _path_args(compile_log))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    await asyncio.sleep(0.05)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    result = await surviving

    assert result.backend == "source_graph"
    assert worker.calls == 1
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_cold_path_build_does_not_block_light_call_or_hold_wave_lock(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker = ReadyWorker(release=release, entered=entered)
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    wave_lock = server._wave_locks_for(["/private/wave.fsdb"])[0]

    task = asyncio.create_task(
        server._dispatch("trace_signal_path", _path_args(compile_log))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    assert wave_lock.locked() is False
    started = time.perf_counter()
    light = await server._dispatch("cursor_list", {})
    elapsed = time.perf_counter() - started
    release.set()
    result = await task

    assert isinstance(light.cursors, list)
    assert elapsed < 0.5
    assert result.backend == "source_graph"
    assert wave_lock.locked() is False


@pytest.mark.anyio
async def test_path_operation_metrics_exclude_endpoint_and_path_content(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        await server._dispatch("trace_signal_path", _path_args(compile_log))
        operation_metrics.set_value("source_graph_path", "sg_top.secret")
    finally:
        operation_metrics.pop(token)

    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["source_graph_phase"] == "complete"
    assert "source_graph_path" not in snapshot
    fixed_labels = {
        "source_graph_phase",
        "source_graph_cache_tier",
        "source_graph_disk_validation_outcome",
    }
    assert all(
        key in fixed_labels
        or (not isinstance(value, bool) and isinstance(value, (int, float)))
        for key, value in snapshot.items()
    )
    assert all(
        label not in key
        for key in snapshot
        for label in ("compile", "diagnostic", "endpoint", "path", "scope", "signal")
    )


@pytest.mark.anyio
async def test_raised_npi_path_failure_still_routes_to_source_graph(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()

    class ExplodingNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def find_path(self, **kwargs):
            del kwargs
            raise RuntimeError("private failure detail")

    _patch_common(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=ExplodingNpi(),
        with_kdb=True,
    )

    result = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert result.backend == "source_graph"
    assert result.backend_status.fallback_reason == "npi_query_failed"
    assert static.path_calls == 0


@pytest.mark.anyio
async def test_path_adapter_blocker_preserved_when_static_is_final(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker())
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch(
        "trace_signal_path",
        _path_args(
            compile_log,
            from_signal="sg_top.missing.a",
            to_signal="sg_top.seed",
        ),
    )

    assert result.backend == "static"
    assert result.unsupported_reason == "static_backend_no_path_api"
    assert result.backend_status.source_graph.adapter_status == "blocked"
    assert result.backend_status.source_graph.blocker.code == (
        "path_from_hierarchy_unresolved"
    )
    assert result.backend_status.source_graph.fallback_used is True
    assert static.path_calls == 1


@pytest.mark.anyio
async def test_path_build_failure_does_not_poison_retry(monkeypatch, tmp_path):
    compile_log, _ = _install_source_context(tmp_path)
    worker = SequenceWorker([PrepareStatus.BUILD_FAILED, _production_ir()])
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    failed = await server._dispatch("trace_signal_path", _path_args(compile_log))
    retried = await server._dispatch("trace_signal_path", _path_args(compile_log))

    assert failed.backend == "static"
    assert retried.backend == "source_graph"
    assert worker.calls == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_sole_path_waiter_cancellation_terminates_worker_without_fallback(
    monkeypatch, tmp_path
):
    compile_log, _ = _install_source_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker_cancelled = threading.Event()
    worker = ReadyWorker(
        release=release,
        entered=entered,
        cancelled=worker_cancelled,
    )
    runtime = SourceGraphRuntime(worker)
    static = TrackingStaticBackend()
    _patch_common(monkeypatch, runtime=runtime, static=static)

    task = asyncio.create_task(
        server._dispatch("trace_signal_path", _path_args(compile_log))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(worker_cancelled.wait, 2)
    await runtime.wait_idle()

    assert runtime.stats_snapshot()["cache_entry_count"] == 0
    assert runtime.stats_snapshot()["inflight_count"] == 0
    assert static.path_calls == 0
