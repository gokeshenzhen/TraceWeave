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
    assert result.backend_status.source_graph.prepare_status == "ready"
    assert result.backend_status.source_graph.query_status == "found"
    assert result.backend_status.source_graph.coverage_status == "complete"
    assert len(result.backend_status.source_graph.build_key_sha256) == 64
    assert len(result.backend_status.source_graph.compile_fingerprint_sha256) == 64
    assert len(result.backend_status.source_graph.ir_fingerprint_sha256) == 64
    assert static.driver_calls + static.load_calls == 0


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
    assert all(
        key == "source_graph_phase"
        or (not isinstance(value, bool) and isinstance(value, (int, float)))
        for key, value in snapshot.items()
    )
    assert all("signal" not in key and "path" not in key for key in snapshot)
