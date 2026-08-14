from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from config import SourceGraphExecutionConfig
import server
from src import operation_metrics
from src.cancellation import OperationCancelled
from src.connectivity_ir import CoverageGap, CoverageReport, CoverageStatus
import src.connectivity_backend as connectivity_backend
from src.source_graph_backend import SourceGraphConnectivityBackend
from src.source_graph_disk_cache import SourceGraphDiskCache
from src.source_graph_runtime import PrepareStatus, SourceGraphRuntime
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME
from tests.connectivity_ir_fixtures import DEEP_RTL, DEEP_TB, build_deep_ir
from tests.test_source_graph_public_routing import (
    ReadyWorker,
    SequenceWorker,
)


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


def _deep_ir(*, coverage: CoverageReport | None = None):
    ir = replace(
        build_deep_ir(),
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version="11.0.0",
    )
    return replace(ir, coverage=coverage) if coverage is not None else ir


def _leaf_path() -> str:
    return (
        "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel."
        "u_rx_fifo.u_storage_bank.u_x_cell"
    )


def _component_hierarchy() -> dict:
    return {
        "uart_deep_x_tb": {
            "u_apb_bridge": {
                "module": "uart_apb_bridge_deep",
                "children": {
                    "u_uart": {
                        "module": "uart_16550_deep",
                        "children": {
                            "u_control": {
                                "module": "uart_register_file_deep",
                                "children": {
                                    "u_rx_channel": {
                                        "module": "uart_rx_channel",
                                        "children": {
                                            "u_rx_fifo": {
                                                "module": "uart_rx_fifo",
                                                "children": {
                                                    "u_storage_bank": {
                                                        "module": (
                                                            "uart_fifo_storage_bank"
                                                        ),
                                                        "children": {
                                                            "u_x_cell": {
                                                                "module": (
                                                                    "uart_x_storage_cell"
                                                                ),
                                                                "children": {},
                                                            }
                                                        },
                                                    }
                                                },
                                            }
                                        },
                                    }
                                },
                            }
                        },
                    }
                },
            }
        }
    }


def _write_deep_vcd(path: Path) -> None:
    path.write_text(
        """\
$timescale 1ps $end
$scope module uart_deep_x_tb $end
$var wire 8 % apb_prdata $end
$scope module u_apb_bridge $end
$scope module u_uart $end
$scope module u_control $end
$scope module u_rx_channel $end
$scope module u_rx_fifo $end
$scope module u_storage_bank $end
$scope module u_x_cell $end
$var wire 8 ! data_q $end
$var wire 1 \" inject_x $end
$var wire 1 # rst_n $end
$var wire 1 $ clk $end
$upscope $end
$upscope $end
$upscope $end
$upscope $end
$upscope $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
bxxxxxxxx %
bxxxxxxxx !
x\"
1#
0$
""",
        encoding="utf-8",
    )


def _install_context(tmp_path: Path) -> tuple[str, str]:
    repo = Path(server.__file__).resolve().parent
    rtl = repo / DEEP_RTL
    tb = repo / DEEP_TB
    compile_log = tmp_path / "compile.log"
    command = f"xrun {rtl} {tb} -top uart_deep_x_tb"
    compile_log.write_text(command + "\n", encoding="utf-8")
    compile_result = {
        "simulator": "xcelium",
        "compile_cwd": str(tmp_path),
        "compile_command": command,
        "top_modules": ["uart_deep_x_tb"],
        "files": {
            "user": [
                {"path": str(rtl), "type": "module", "category": "rtl"},
                {"path": str(tb), "type": "module", "category": "tb"},
            ],
            "filtered_count": 0,
        },
        "include_tree": {},
        "filelist_tree": {},
        "interfaces": [],
        "parse_warnings": [],
    }
    hierarchy = {
        "compile_result": compile_result,
        "component_tree": _component_hierarchy(),
    }
    handle = server.compute_handle(str(compile_log), "xcelium")
    server._handle_store.register(handle, hierarchy)
    wave = tmp_path / "deep.vcd"
    _write_deep_vcd(wave)
    return str(compile_log), str(wave)


def _trace_args(
    compile_log: str,
    wave_path: str,
    *,
    signal_path: str | None = None,
) -> dict:
    return {
        "signal_path": signal_path or f"{_leaf_path()}.data_q",
        "wave_path": wave_path,
        "compile_log": compile_log,
        "time_ps": 0,
        "simulator": "xcelium",
        "top_hint": "uart_deep_x_tb",
        "max_depth": 8,
    }


class TraceStaticBackend:
    name = "static"
    uses_external_worker = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def find_driver(self, **kwargs):
        signal_path = kwargs["signal_path"]
        self.calls.append(signal_path)
        leaf = _leaf_path()
        if signal_path.endswith(("data_q", "apb_prdata")):
            return {
                "signal_path": signal_path,
                "wave_path": kwargs["wave_path"],
                "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
                "resolved_module": "static_root",
                "driver_status": "resolved",
                "driver_kind": "assign",
                "source_file": "static_root.sv",
                "source_line": 1,
                "expression_summary": "static root",
                "upstream_signals": [f"{leaf}.inject_x"],
                "confidence": "low",
                "recursive": False,
                "backend": "static",
            }
        return {
            "signal_path": signal_path,
            "wave_path": kwargs["wave_path"],
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "resolved_module": "static_leaf",
            "driver_status": "partial",
            "driver_kind": "unknown",
            "source_file": "static_leaf.sv",
            "source_line": 2,
            "expression_summary": "static leaf",
            "upstream_signals": [],
            "confidence": "low",
            "recursive": False,
            "backend": "static",
        }


def _patch_route(
    monkeypatch,
    *,
    runtime: SourceGraphRuntime | None,
    static: TraceStaticBackend,
    npi_backend=None,
    with_kdb: bool = False,
) -> None:
    monkeypatch.setattr(server, "_check_prerequisites", lambda name, args: None)
    monkeypatch.setattr(
        server,
        "_safe_probe_backend",
        lambda *args: {
            "simulator": "xcelium",
            "backend": "static",
            "parser_match": "approximate",
            "kdb_path": "/private/kdb.elab++" if with_kdb else None,
            "kdb_flow": "vcs_two_step" if with_kdb else "none",
            "kdb_validation_status": "usable" if with_kdb else "unavailable",
            "kdb_hint": None,
        },
    )
    monkeypatch.setattr(server, "get_source_graph_execution_config", _source_config)
    if runtime is None:
        monkeypatch.setattr(
            server,
            "get_source_graph_runtime",
            lambda config: (_ for _ in ()).throw(
                AssertionError("Source Graph runtime must not be called")
            ),
        )
    else:
        monkeypatch.setattr(server, "get_source_graph_runtime", lambda config: runtime)
    monkeypatch.setattr(
        connectivity_backend, "StaticConnectivityBackend", lambda: static
    )
    monkeypatch.setattr(
        connectivity_backend,
        "select_backend",
        lambda status, *, fallback=None: npi_backend or fallback,
    )


@pytest.mark.anyio
async def test_source_graph_multi_node_trace_builds_once_and_repeats_exact_warm(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    worker = ReadyWorker(ir=_deep_ir())
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    first = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    second = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert first.trace_status == "driver_unresolved"
    assert [node.signal_path for node in first.propagation_chain] == [
        f"{_leaf_path()}.data_q",
        f"{_leaf_path()}.inject_x",
    ]
    assert first.backend_status.actual_backend == "source_graph"
    assert first.backend_status.single_backend_provenance is True
    receipt = first.backend_status.source_graph
    assert receipt.single_artifact_provenance is True
    assert receipt.final_artifact_scope_match is True
    assert receipt.query_count == 2
    assert receipt.attempted_query_count == 2
    assert len(set(receipt.query_fingerprints_sha256)) == 2
    assert receipt.metrics.actual_build_count == 1
    assert receipt.artifact_attempt_count == 1
    assert receipt.scope_expansion_count == 0
    assert second.backend_status.source_graph.artifact_reuse == "exact_hit"
    assert second.backend_status.source_graph.metrics.actual_build_count == 0
    assert worker.calls == 1
    assert static.calls == []


@pytest.mark.anyio
async def test_trusted_npi_terminal_result_skips_source_graph_and_static(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    static = TraceStaticBackend()

    class TerminalNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def __init__(self):
            self.calls = 0

        def find_driver(self, **kwargs):
            self.calls += 1
            return {
                "signal_path": kwargs["signal_path"],
                "wave_path": kwargs["wave_path"],
                "driver_status": "testbench_driven",
                "driver_kind": None,
                "source_file": None,
                "upstream_signals": [],
                "backend": "verdi_npi",
            }

    npi = TerminalNpi()
    _patch_route(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )

    result = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert result.trace_status == "testbench_driven"
    assert result.backend_status.actual_backend == "verdi_npi"
    assert result.backend_status.source_graph is None
    assert result.backend_status.whole_trace_restart_count == 0
    assert npi.calls == 1
    assert static.calls == []


@pytest.mark.anyio
async def test_explicit_source_graph_route_skips_npi_for_trace_with_usable_kdb(
    monkeypatch, tmp_path
):
    policy_selector = connectivity_backend.select_backend
    compile_log, wave = _install_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker(ir=_deep_ir()))
    static = TraceStaticBackend()

    class MustNotRunNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def __init__(self):
            self.calls = 0

        def find_driver(self, **kwargs):
            del kwargs
            self.calls += 1
            raise AssertionError("explicit Source Graph route must not call NPI")

    npi = MustNotRunNpi()
    _patch_route(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=npi,
        with_kdb=True,
    )
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    monkeypatch.setattr(connectivity_backend, "select_backend", policy_selector)

    result = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    status = result.backend_status
    assert status.kdb_validation_status == "usable"
    assert status.connectivity_route == "source_graph"
    assert status.connectivity_route_error is None
    assert status.selected_backend == "source_graph"
    assert status.actual_backend == "source_graph"
    assert status.fallback_reason == "npi_skipped_by_policy"
    assert status.single_backend_provenance is True
    assert [
        (item.backend, item.status, item.reason) for item in status.attempted_backends
    ] == [
        ("verdi_npi", "skipped", "npi_skipped_by_policy"),
        ("source_graph", "success", None),
    ]
    assert status.source_graph.single_artifact_provenance is True
    assert npi.calls == 0
    assert static.calls == []


@pytest.mark.anyio
async def test_npi_internal_fallback_discards_partial_chain_and_restarts_source_graph(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker(ir=_deep_ir()))
    static = TraceStaticBackend()
    npi_calls: list[str] = []

    class MidTraceFallbackNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def find_driver(self, **kwargs):
            signal_path = kwargs["signal_path"]
            npi_calls.append(signal_path)
            if signal_path.endswith("data_q"):
                return {
                    "signal_path": signal_path,
                    "wave_path": kwargs["wave_path"],
                    "driver_status": "resolved",
                    "driver_kind": "assign",
                    "source_file": "npi_partial_must_be_discarded.sv",
                    "source_line": 9,
                    "expression_summary": "npi partial",
                    "upstream_signals": [f"{_leaf_path()}.inject_x"],
                    "backend": "verdi_npi",
                }
            return {
                "signal_path": signal_path,
                "wave_path": kwargs["wave_path"],
                "driver_status": "deferred",
                "upstream_signals": [],
                "backend": "source_graph_deferred",
                "_connectivity_fallback_deferred": True,
                "_npi_fallback_reason": "npi_load_failed",
            }

    _patch_route(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=MidTraceFallbackNpi(),
        with_kdb=True,
    )

    result = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert npi_calls == [
        f"{_leaf_path()}.data_q",
        f"{_leaf_path()}.inject_x",
    ]
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.fallback_reason == "npi_load_failed"
    assert result.backend_status.whole_trace_restart_reasons == [
        "npi_internal_fallback"
    ]
    assert result.trace_restarted is True
    assert result.propagation_chain[0].source_file == DEEP_RTL
    assert all(
        node.source_file != "npi_partial_must_be_discarded.sv"
        for node in result.propagation_chain
    )
    assert [item.backend for item in result.backend_status.attempted_backends] == [
        "verdi_npi",
        "source_graph",
    ]
    assert static.calls == []


@pytest.mark.anyio
async def test_degraded_npi_incomplete_driver_discards_chain_and_restarts(
    monkeypatch,
    tmp_path,
):
    compile_log, wave = _install_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker(ir=_deep_ir()))
    static = TraceStaticBackend()
    npi_calls: list[str] = []

    class DegradedNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False
        kdb_status = {
            "load_quality": "degraded",
            "error_count": 8,
            "error_log": "/private/kdb/elabcomLog/compiler.log",
        }

        def find_driver(self, **kwargs):
            signal_path = kwargs["signal_path"]
            npi_calls.append(signal_path)
            if signal_path.endswith("data_q"):
                return {
                    "signal_path": signal_path,
                    "wave_path": kwargs["wave_path"],
                    "driver_status": "resolved",
                    "driver_kind": "assign",
                    "source_file": "degraded_partial_must_be_discarded.sv",
                    "source_line": 9,
                    "expression_summary": "positive degraded NPI edge",
                    "upstream_signals": [f"{_leaf_path()}.inject_x"],
                    "backend": "verdi_npi",
                }
            return {
                "signal_path": signal_path,
                "wave_path": kwargs["wave_path"],
                "driver_status": "unsupported",
                "unsupported_reason": "signal_path_unresolved_in_npi",
                "upstream_signals": [],
                "backend": "verdi_npi",
            }

    _patch_route(
        monkeypatch,
        runtime=runtime,
        static=static,
        npi_backend=DegradedNpi(),
        with_kdb=True,
    )

    result = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert npi_calls == [
        f"{_leaf_path()}.data_q",
        f"{_leaf_path()}.inject_x",
    ]
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.kdb_degraded is True
    assert result.backend_status.kdb_error_count == 8
    assert result.backend_status.fallback_reason == (
        "npi_degraded_result_inconclusive"
    )
    assert result.backend_status.whole_trace_restart_reasons == [
        "npi_degraded_inconclusive"
    ]
    assert result.backend_status.attempted_backends[0].coverage_status == "partial"
    assert result.trace_restarted is True
    assert all(
        node.source_file != "degraded_partial_must_be_discarded.sv"
        for node in result.propagation_chain
    )
    assert static.calls == []


@pytest.mark.anyio
async def test_scope_expansion_restarts_source_graph_with_one_final_artifact(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    worker = ReadyWorker(ir=_deep_ir())
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch(
        "trace_x_source",
        _trace_args(
            compile_log,
            wave,
            signal_path="uart_deep_x_tb.apb_prdata",
        ),
    )

    receipt = result.backend_status.source_graph
    assert result.backend_status.actual_backend == "source_graph"
    assert result.backend_status.whole_trace_restart_reasons == [
        "source_graph_scope_expansion"
    ]
    assert result.trace_restarted is True
    assert receipt.single_artifact_provenance is True
    assert receipt.final_artifact_scope_match is True
    assert receipt.artifact_attempt_count == 2
    assert receipt.scope_expansion_count == 1
    assert receipt.metrics.actual_build_count == 2
    assert receipt.query_count == 2
    assert receipt.attempted_query_count == 3
    assert len(set(receipt.attempted_artifact_fingerprints_sha256)) == 2
    assert receipt.final_artifact_fingerprint_sha256 == (
        receipt.selected_artifact_fingerprint_sha256
    )
    assert worker.calls == 2

    covered = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    covered_receipt = covered.backend_status.source_graph
    assert covered.backend_status.actual_backend == "source_graph"
    assert covered_receipt.artifact_reuse == "dominating_hit"
    assert covered_receipt.scope_match.relation == "superset"
    assert covered_receipt.final_artifact_fingerprint_sha256 == (
        receipt.final_artifact_fingerprint_sha256
    )
    assert worker.calls == 2


@pytest.mark.anyio
async def test_trace_scope_expansion_respects_frontier_instance_cap(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    worker = ReadyWorker(ir=_deep_ir())
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)
    monkeypatch.setattr(
        server,
        "get_source_graph_execution_config",
        lambda: replace(_source_config(), frontier_max_instances=1),
    )

    result = await server._dispatch(
        "trace_x_source",
        _trace_args(
            compile_log,
            wave,
            signal_path="uart_deep_x_tb.apb_prdata",
        ),
    )

    assert result.backend_status.actual_backend == "static"
    assert result.backend_status.fallback_reason == (
        "source_graph_frontier_instance_limit"
    )
    assert result.backend_status.whole_trace_restart_reasons == [
        "source_graph_to_static"
    ]
    receipt = result.backend_status.source_graph
    assert receipt.blocker.code == "frontier_instance_limit"
    assert receipt.artifact_attempt_count == 1
    assert receipt.scope_expansion_count == 0
    assert worker.calls == 1
    assert static.calls == [
        "uart_deep_x_tb.apb_prdata",
        f"{_leaf_path()}.inject_x",
    ]


@pytest.mark.anyio
async def test_fresh_runtime_x_trace_restarts_using_only_exact_disk_artifacts(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    cache_root = tmp_path / "source-graph-cache"
    static = TraceStaticBackend()
    cold_worker = ReadyWorker(ir=_deep_ir())
    cold_runtime = SourceGraphRuntime(
        cold_worker,
        disk_cache=SourceGraphDiskCache(cache_root),
    )
    _patch_route(monkeypatch, runtime=cold_runtime, static=static)
    args = _trace_args(
        compile_log,
        wave,
        signal_path="uart_deep_x_tb.apb_prdata",
    )

    cold = await server._dispatch("trace_x_source", args)
    disk_worker = ReadyWorker(ir=_deep_ir())
    disk_runtime = SourceGraphRuntime(
        disk_worker,
        disk_cache=SourceGraphDiskCache(cache_root),
    )
    monkeypatch.setattr(server, "get_source_graph_runtime", lambda config: disk_runtime)
    disk = await server._dispatch("trace_x_source", args)

    receipt = disk.backend_status.source_graph
    assert cold.backend_status.actual_backend == "source_graph"
    assert cold_worker.calls == 2
    assert disk.backend_status.actual_backend == "source_graph"
    assert disk.backend_status.single_backend_provenance is True
    assert disk.trace_restarted is True
    assert disk.backend_status.whole_trace_restart_reasons == [
        "source_graph_scope_expansion"
    ]
    assert receipt.single_artifact_provenance is True
    assert receipt.final_artifact_scope_match is True
    assert receipt.artifact_attempt_count == 2
    assert receipt.scope_expansion_count == 1
    assert receipt.artifact_reuse == "disk_exact_hit"
    assert receipt.cache_tier == "disk"
    assert receipt.disk_validation_outcome == "hit"
    assert receipt.metrics.actual_build_count == 0
    assert receipt.metrics.frontend_launch_count == 0
    assert receipt.metrics.disk_hit_count == 2
    assert receipt.metrics.disk_build_skip_count == 2
    assert disk_worker.calls == 0
    assert static.calls == []


@pytest.mark.anyio
async def test_inconclusive_negative_discards_source_chain_and_restarts_static(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    gap = CoverageGap(
        code="protected_payload",
        message="inject source is outside the proved objective",
        impact=CoverageStatus.INCONCLUSIVE,
        constructs=("protected",),
        scopes=(f"{_leaf_path()}.inject_x",),
    )
    coverage = CoverageReport(
        status=CoverageStatus.PARTIAL,
        files_total=2,
        files_projected=2,
        gaps=(gap,),
    )
    runtime = SourceGraphRuntime(ReadyWorker(ir=_deep_ir(coverage=coverage)))
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    result = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert result.backend_status.actual_backend == "static"
    assert result.backend_status.fallback_reason == (
        "source_graph_trace_query_inconclusive"
    )
    assert result.backend_status.whole_trace_restart_reasons == [
        "source_graph_to_static"
    ]
    assert [node.source_file for node in result.propagation_chain] == [
        "static_root.sv",
        "static_leaf.sv",
    ]
    receipt = result.backend_status.source_graph
    assert receipt.query_statuses == ["found", "inconclusive"]
    assert receipt.inconclusive_negative_count == 1
    assert receipt.fallback_used is True
    assert static.calls == [
        f"{_leaf_path()}.data_q",
        f"{_leaf_path()}.inject_x",
    ]


@pytest.mark.anyio
async def test_source_graph_build_failure_restarts_static_and_retry_is_safe(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    static = TraceStaticBackend()
    worker = SequenceWorker([PrepareStatus.BUILD_FAILED, _deep_ir()])
    runtime = SourceGraphRuntime(worker)
    _patch_route(monkeypatch, runtime=runtime, static=static)

    failed = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert failed.backend_status.actual_backend == "static"
    assert failed.backend_status.source_graph.prepare_status == "build_failed"
    assert failed.backend_status.whole_trace_restart_reasons == [
        "source_graph_to_static"
    ]
    assert runtime.stats_snapshot()["cache_entry_count"] == 0

    retry_static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=retry_static)
    retried = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert retried.backend_status.actual_backend == "source_graph"
    assert worker.calls == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 1
    assert retry_static.calls == []


@pytest.mark.anyio
async def test_source_graph_query_runs_outside_wave_lock_and_wave_reads_hold_it(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker(ir=_deep_ir()))
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)
    wave_lock = server._wave_locks_for([wave])[0]
    original = SourceGraphConnectivityBackend.find_driver
    query_calls = 0

    def checked_query(self, *args, **kwargs):
        nonlocal query_calls
        assert not wave_lock.locked()
        query_calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SourceGraphConnectivityBackend, "find_driver", checked_query)
    parser = server.VCDParser(wave)
    original_value = parser.get_value_at_time

    def checked_value(*args, **kwargs):
        assert wave_lock.locked()
        return original_value(*args, **kwargs)

    monkeypatch.setattr(parser, "get_value_at_time", checked_value)
    monkeypatch.setattr(server, "_get_parser", lambda path: parser)

    result = await server._dispatch("trace_x_source", _trace_args(compile_log, wave))

    assert result.backend_status.actual_backend == "source_graph"
    assert query_calls == 2
    assert wave_lock.locked() is False


@pytest.mark.anyio
async def test_cold_trace_build_is_single_flight_and_does_not_block_light_calls(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    worker = ReadyWorker(ir=_deep_ir(), release=release, entered=entered)
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    tasks = [
        asyncio.create_task(
            server._dispatch("trace_x_source", _trace_args(compile_log, wave))
        )
        for _ in range(2)
    ]
    assert await asyncio.to_thread(entered.wait, 2)
    assert server._wave_locks_for([wave])[0].locked() is False
    started = time.perf_counter()
    light = await server._dispatch("cursor_list", {})
    elapsed = time.perf_counter() - started
    for _ in range(200):
        if runtime.stats_snapshot()["coalesced_waiter_count"] == 1:
            break
        await asyncio.sleep(0.005)
    release.set()
    results = await asyncio.gather(*tasks)

    assert isinstance(light.cursors, list)
    assert elapsed < 0.5
    assert worker.calls == 1
    assert runtime.stats_snapshot()["actual_build_count"] == 1
    assert runtime.stats_snapshot()["coalesced_waiter_count"] == 1
    assert {item.backend_status.actual_backend for item in results} == {"source_graph"}
    assert static.calls == []


@pytest.mark.anyio
async def test_one_cancelled_trace_waiter_does_not_cancel_survivor(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    cancelled = threading.Event()
    worker = ReadyWorker(
        ir=_deep_ir(),
        release=release,
        entered=entered,
        cancelled=cancelled,
    )
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    first = asyncio.create_task(
        server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    )
    second = asyncio.create_task(
        server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    for _ in range(200):
        if runtime.stats_snapshot()["coalesced_waiter_count"] == 1:
            break
        await asyncio.sleep(0.005)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    survivor = await second

    assert survivor.backend_status.actual_backend == "source_graph"
    assert worker.calls == 1
    assert cancelled.is_set() is False
    assert static.calls == []


@pytest.mark.anyio
async def test_sole_cancelled_trace_waiter_stops_worker_without_fallback(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    release = asyncio.Event()
    entered = threading.Event()
    cancelled = threading.Event()
    worker = ReadyWorker(
        ir=_deep_ir(),
        release=release,
        entered=entered,
        cancelled=cancelled,
    )
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    task = asyncio.create_task(
        server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(cancelled.wait, 2)
    await runtime.wait_idle()

    assert runtime.stats_snapshot()["cache_entry_count"] == 0
    assert static.calls == []


@pytest.mark.anyio
async def test_cached_query_cancellation_does_not_fallback_or_pollute_artifact(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    worker = ReadyWorker(ir=_deep_ir())
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)
    args = _trace_args(compile_log, wave)

    ready = await server._dispatch("trace_x_source", args)
    original = SourceGraphConnectivityBackend.find_driver

    def cancelled_query(self, *query_args, **query_kwargs):
        del self, query_args, query_kwargs
        raise OperationCancelled("query cancelled")

    monkeypatch.setattr(SourceGraphConnectivityBackend, "find_driver", cancelled_query)
    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("trace_x_source", args)
    monkeypatch.setattr(SourceGraphConnectivityBackend, "find_driver", original)
    warm = await server._dispatch("trace_x_source", args)

    assert ready.backend_status.actual_backend == "source_graph"
    assert warm.backend_status.source_graph.artifact_reuse == "exact_hit"
    assert worker.calls == 1
    assert runtime.stats_snapshot()["cache_entry_count"] == 1
    assert static.calls == []


@pytest.mark.anyio
async def test_npi_cancellation_never_enters_source_graph_or_static(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    static = TraceStaticBackend()

    class CancelledNpi:
        name = "verdi_npi"
        execution_mode = "local"
        uses_external_worker = False

        def find_driver(self, **kwargs):
            del kwargs
            raise OperationCancelled("cancelled")

    _patch_route(
        monkeypatch,
        runtime=None,
        static=static,
        npi_backend=CancelledNpi(),
        with_kdb=True,
    )

    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    assert static.calls == []


@pytest.mark.anyio
async def test_adapter_cancellation_never_enters_runtime_or_static(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=None, static=static)

    def cancelled_adapter(**kwargs):
        del kwargs
        raise OperationCancelled("adapter cancelled")

    monkeypatch.setattr(server, "build_source_graph_trace_plan", cancelled_adapter)

    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    assert static.calls == []


@pytest.mark.anyio
async def test_wave_sampling_cancellation_after_prepare_never_falls_back(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    worker = ReadyWorker(ir=_deep_ir())
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)

    class CancelledParser:
        def get_value_at_time(self, signal_path, time_ps):
            del signal_path, time_ps
            raise OperationCancelled("wave cancelled")

    monkeypatch.setattr(server, "_get_parser", lambda path: CancelledParser())

    with pytest.raises(asyncio.CancelledError):
        await server._dispatch("trace_x_source", _trace_args(compile_log, wave))
    assert worker.calls == 1
    assert static.calls == []


@pytest.mark.anyio
async def test_scope_restart_cancellation_discards_partial_trace_without_fallback(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    worker = ReadyWorker(ir=_deep_ir())
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)
    original = server.build_source_graph_trace_plan
    adapter_calls = 0

    def cancel_second_plan(**kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        if adapter_calls == 2:
            raise OperationCancelled("restart cancelled")
        return original(**kwargs)

    monkeypatch.setattr(server, "build_source_graph_trace_plan", cancel_second_plan)

    with pytest.raises(asyncio.CancelledError):
        await server._dispatch(
            "trace_x_source",
            _trace_args(
                compile_log,
                wave,
                signal_path="uart_deep_x_tb.apb_prdata",
            ),
        )
    assert adapter_calls == 2
    assert worker.calls == 1
    assert static.calls == []


@pytest.mark.anyio
async def test_trace_metrics_are_identity_free_numeric_or_fixed_labels(
    monkeypatch, tmp_path
):
    compile_log, wave = _install_context(tmp_path)
    runtime = SourceGraphRuntime(ReadyWorker(ir=_deep_ir()))
    static = TraceStaticBackend()
    _patch_route(monkeypatch, runtime=runtime, static=static)
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    try:
        await server._dispatch("trace_x_source", _trace_args(compile_log, wave))
        operation_metrics.set_value(
            "source_graph_trace_signal", f"{_leaf_path()}.data_q"
        )
    finally:
        operation_metrics.pop(token)

    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["source_graph_phase"] == "complete"
    assert snapshot["source_graph_trace_query_count"] == 2
    assert snapshot["source_graph_trace_artifact_attempt_count"] == 1
    assert "source_graph_trace_signal" not in snapshot
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
    serialized = str(snapshot)
    assert _leaf_path() not in serialized
    assert compile_log not in serialized
    assert wave not in serialized
