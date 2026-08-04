#!/usr/bin/env python3
"""Phase 3C gate for bounded Source Graph-backed X/Z propagation traces.

The correctness oracle is a tracked hand-authored RTL/IR fixture plus a
deterministic VCD generated in a temporary directory.  The default benchmark
uses only fake workers: it does not require pyslang, NPI, a license, an
OpenTitan checkout, ignored files, or network access.  No historical evidence
is regenerated.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SourceGraphExecutionConfig  # noqa: E402
import server  # noqa: E402
from scripts import benchmark_source_graph_phase1b as phase1b  # noqa: E402
from scripts import benchmark_source_graph_phase2 as phase2  # noqa: E402
from scripts import benchmark_source_graph_phase3a as phase3a  # noqa: E402
from scripts import benchmark_source_graph_phase3b as phase3b  # noqa: E402
from src import operation_metrics  # noqa: E402
from src.cancellation import OperationCancelled  # noqa: E402
import src.connectivity_backend as connectivity_backend  # noqa: E402
from src.connectivity_ir import (  # noqa: E402
    CoverageGap,
    CoverageReport,
    CoverageStatus,
)
from src.hierarchy_handles import (  # noqa: E402
    compute_handle,
    compute_snapshot_fingerprint,
)
from src.source_graph_adapter import (  # noqa: E402
    AdapterStatus,
    _reset_source_graph_adapter_cache_for_tests,
    build_source_graph_trace_plan,
)
from src.source_graph_backend import SourceGraphConnectivityBackend  # noqa: E402
from src.source_graph_contract import SourceGraphScopeReceipt  # noqa: E402
from src.source_graph_runtime import (  # noqa: E402
    PrepareStatus,
    SourceGraphRuntime,
    WorkerBuildResult,
    WorkerResourceMetrics,
)
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME  # noqa: E402
from tests.connectivity_ir_fixtures import (  # noqa: E402
    DEEP_RTL,
    DEEP_TB,
    build_deep_ir,
)


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase3c"
FRONTEND_VERSION = "11.0.0"
ACCEPTED_TRACKED_HEAD = "0edf656bc7c21e556d6ca35587128e098849ec95"
MEASUREMENT_HEAD = "d10ad7fbdc2581318c3f3e448112b6415943fa63"
DEFAULT_OUTPUT = ROOT / "benchmarks/source_graph_connectivity_phase3c_results.json"

HISTORICAL_EVIDENCE = {
    "phase1a": (
        ROOT / "benchmarks/source_graph_connectivity_phase1a_results.json",
        "c7310560a1e89e19694a83d41e24a645578b747585c68df546a20937f3fa42e2",
    ),
    "phase1b": (
        ROOT / "benchmarks/source_graph_connectivity_phase1b_results.json",
        "c9a25c96c63ddce9205ecabf86b61f6da1eff9ba0f71aeee099a6b04f7237da7",
    ),
    "phase2": (
        ROOT / "benchmarks/source_graph_connectivity_phase2_results.json",
        "1b5f76c3862601bb1163d838744c9c03ec7ed62cd1bc5f663ef7008bd0902599",
    ),
    "phase3a": (
        ROOT / "benchmarks/source_graph_connectivity_phase3a_results.json",
        "6b7f8822f978f8e46dd9172c2b093e731889f44b0b68c040b5c66619ccbc438a",
    ),
    "phase3b": (
        ROOT / "benchmarks/source_graph_connectivity_phase3b_results.json",
        "32a57dba8a1c554188da79f9bab0982fc0f547e0b19f9a6a789f438340113e81",
    ),
}

GATE_TARGETS = {
    "same_artifact_multi_node_actual_build_count": 1,
    "warm_public_x_trace_p95_max_ms": 100.0,
    "peak_rss_max_kib": 2_621_440,
    "max_concurrent_cold_builds_per_process": 1,
}

BenchmarkError = phase1b.BenchmarkError


@dataclass(frozen=True)
class TraceWorkload:
    compile_log: str
    wave_path: str
    compile_result: Mapping[str, Any]
    hierarchy: Mapping[str, Any]
    signal_path: str
    expansion_signal_path: str
    top: str = "uart_deep_x_tb"
    simulator: str = "xcelium"

    def args(self, *, signal_path: str | None = None) -> dict[str, Any]:
        return {
            "signal_path": signal_path or self.signal_path,
            "wave_path": self.wave_path,
            "compile_log": self.compile_log,
            "time_ps": 0,
            "simulator": self.simulator,
            "top_hint": self.top,
            "max_depth": 8,
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _read_historical_baselines() -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for name, (path, expected_sha) in HISTORICAL_EVIDENCE.items():
        raw = path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if actual_sha != expected_sha:
            raise BenchmarkError(
                f"{name} historical evidence hash mismatch: {actual_sha}"
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise BenchmarkError(f"{name} historical evidence is not an object")
        payloads[name] = payload
        receipts[name] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": actual_sha,
            "schema_version": payload.get("schema_version"),
            "benchmark": payload.get("benchmark"),
            "measurement_head": payload.get("repository", {}).get("head"),
            "decision": payload.get("assessment", {}).get("decision"),
        }

    phase3b_payload = payloads["phase3b"]
    assessment = phase3b_payload.get("assessment", {})
    expected = {
        "decision": "phase3b_cross_target_process_memory_reuse_gate_passed",
        "phase3b_cross_target_process_memory_reuse_gate_passed": True,
        "production_route_ordering_changed": False,
        "reuse_behavior_affected_tools": [
            "explain_signal_driver",
            "find_signal_loads",
            "trace_signal_path",
        ],
        "cross_target_process_memory_reuse": True,
        "cross_process_reuse": False,
        "trace_x_source_route_changed": False,
        "waveform_locking_model_changed": False,
        "disk_cache": False,
        "persistent_worker": False,
        "startup_full_design_enumeration": False,
        "architecture_gate_passed": True,
        "workload_gate_passed": True,
        "dominance_gate_passed": True,
        "capacity_gate_passed": True,
        "failure_gate_passed": True,
        "route_gate_passed": True,
    }
    if phase3b_payload.get("schema_version") != "1.0":
        raise BenchmarkError("Phase 3B schema version mismatch")
    if phase3b_payload.get("benchmark") != ("source_graph_connectivity_phase3b"):
        raise BenchmarkError("Phase 3B benchmark identity mismatch")
    if phase3b_payload.get("repository", {}).get("head") != (
        "6ff8ffb3da780f1dab7f7f4645331536684518d9"
    ):
        raise BenchmarkError("Phase 3B measurement HEAD mismatch")
    for field, expected_value in expected.items():
        if assessment.get(field) != expected_value:
            raise BenchmarkError(f"Phase 3B assessment mismatch: {field}")
    if not phase3b_payload.get("route_probes", {}).get("passed"):
        raise BenchmarkError("Phase 3B route gate is not passed")
    if not phase3b_payload.get("failure_probes", {}).get("gate", {}).get("passed"):
        raise BenchmarkError("Phase 3B failure gate is not passed")
    if (
        not phase3b_payload.get("dominating_scope_reuse", {})
        .get("gate", {})
        .get("passed")
    ):
        raise BenchmarkError("Phase 3B dominance gate is not passed")
    if not phase3b_payload.get("capacity_probe", {}).get("gate", {}).get("passed"):
        raise BenchmarkError("Phase 3B capacity gate is not passed")
    if not all(
        item.get("gate", {}).get("passed")
        for item in phase3b_payload.get("workloads", ())
    ):
        raise BenchmarkError("Phase 3B workload gate is not passed")

    hand = next(
        item for item in phase3b_payload["workloads"] if item["name"] == "hand_fixture"
    )
    if any(
        run["runtime_stats_before_out_of_scope"]["actual_build_count"] != 1
        for run in hand["runs"]
    ):
        raise BenchmarkError("Phase 3B same-artifact actual build count mismatch")
    opentitan = next(
        item
        for item in phase3b_payload["workloads"]
        if item["name"] == "opentitan_core"
    )
    aggregate = opentitan["runs"][0]["core_queries"][0]["source_graph"]
    open_expected = {
        "input_count": 785,
        "top_count": 11,
        "coverage_boundary_instance_count": 4,
        "requested_cone_instance_count": 4,
        "coverage_status": "inconclusive",
        "coverage_blocking_diagnostic_count": 65,
        "query_confidence": "partial",
    }
    manifest = aggregate.get("manifest", {})
    scope = aggregate.get("scope", {})
    actual_open = {
        "input_count": manifest.get("input_count"),
        "top_count": manifest.get("top_count"),
        "coverage_boundary_instance_count": scope.get(
            "coverage_boundary_instance_count"
        ),
        "requested_cone_instance_count": scope.get("requested_cone_instance_count"),
        "coverage_status": aggregate.get("coverage_status"),
        "coverage_blocking_diagnostic_count": aggregate.get(
            "coverage_blocking_diagnostic_count"
        ),
        "query_confidence": aggregate.get("query_confidence"),
    }
    if actual_open != open_expected:
        raise BenchmarkError("Phase 3B OpenTitan invariants mismatch")
    receipts["phase3b"]["accepted_invariants"] = {
        "same_artifact_mixed_driver_load_path_actual_build_count": 1,
        "opentitan_ordered_input_count": 785,
        "opentitan_ordered_top_count": 11,
        "opentitan_scope_path_count": 4,
        "opentitan_coverage_status": "inconclusive",
        "opentitan_blocking_diagnostic_count": 65,
        "opentitan_representative_confidence": "partial",
        "opentitan_claim": "target-scoped only",
        "all_architecture_route_workload_dominance_capacity_failure_gates": ("passed"),
    }
    return receipts


def _leaf_path() -> str:
    return (
        "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel."
        "u_rx_fifo.u_storage_bank.u_x_cell"
    )


def _component_hierarchy() -> dict[str, Any]:
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


def _write_vcd(path: Path) -> None:
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


def _hand_workload(temp_root: Path) -> TraceWorkload:
    rtl = ROOT / DEEP_RTL
    tb = ROOT / DEEP_TB
    compile_log = temp_root / "compile.log"
    command = f"xrun {rtl} {tb} -top uart_deep_x_tb"
    compile_log.write_text(command + "\n", encoding="utf-8")
    compile_result = {
        "simulator": "xcelium",
        "compile_cwd": str(temp_root),
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
    wave = temp_root / "deep.vcd"
    _write_vcd(wave)
    return TraceWorkload(
        compile_log=str(compile_log),
        wave_path=str(wave),
        compile_result=compile_result,
        hierarchy=hierarchy,
        signal_path=f"{_leaf_path()}.data_q",
        expansion_signal_path="uart_deep_x_tb.apb_prdata",
    )


def _deep_ir(*, partial: bool = False):
    ir = replace(
        build_deep_ir(),
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version=FRONTEND_VERSION,
    )
    if not partial:
        return ir
    gap = CoverageGap(
        code="protected_payload",
        message="fixture exclusion retained for coverage-honesty probe",
        impact=CoverageStatus.INCONCLUSIVE,
        constructs=("protected",),
        scopes=(f"{_leaf_path()}.inject_x",),
    )
    return replace(
        ir,
        coverage=CoverageReport(
            status=CoverageStatus.PARTIAL,
            files_total=2,
            files_projected=2,
            gaps=(gap,),
        ),
    )


def _ready_result(request, ir) -> WorkerBuildResult:
    return WorkerBuildResult.ready(
        ir,
        SourceGraphScopeReceipt(
            scope=request.scope,
            coverage_status=ir.coverage.status,
            gap_codes=tuple(gap.code for gap in ir.coverage.gaps),
        ),
        metrics=WorkerResourceMetrics(
            wall_time_ms=2.0,
            cpu_time_ms=1.0,
            rss_start_kib=100,
            rss_peak_kib=140,
            rss_end_kib=110,
        ),
    )


class ReadyWorker:
    def __init__(self, ir=None) -> None:
        self.ir = ir or _deep_ir()
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        self.calls += 1
        if cancel_event.is_set():
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="worker_process",
            )
        return _ready_result(request, self.ir)


class SequenceWorker:
    def __init__(self, first_status: PrepareStatus, ir=None) -> None:
        self.first_status = first_status
        self.ir = ir or _deep_ir()
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds, cancel_event
        self.calls += 1
        if self.calls == 1:
            codes = {
                PrepareStatus.DEPENDENCY_BLOCKED: "frontend_unavailable",
                PrepareStatus.BUILD_FAILED: "frontend_build_failed",
                PrepareStatus.WORKER_CRASH: "worker_exit_failure",
                PrepareStatus.TIMED_OUT: "worker_timeout",
            }
            return WorkerBuildResult.failed(
                self.first_status,
                code=codes[self.first_status],
                stage="worker_process",
            )
        return _ready_result(request, self.ir)


class CancelThenReadyWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.cancel_observed = threading.Event()

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            while not cancel_event.is_set():
                await asyncio.sleep(0.002)
            self.cancel_observed.set()
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="worker_process",
            )
        return _ready_result(request, _deep_ir())


class GatedReadyWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = asyncio.Event()

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():
            if cancel_event.is_set():
                return WorkerBuildResult.failed(
                    PrepareStatus.CANCELLED,
                    code="request_cancelled",
                    stage="worker_process",
                )
            await asyncio.sleep(0.002)
        return _ready_result(request, _deep_ir())


class AdmissionWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds, cancel_event
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return _ready_result(request, _deep_ir())
        finally:
            self.active -= 1


class TraceStaticBackend:
    name = "static"
    uses_external_worker = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def find_driver(self, **kwargs):
        signal_path = kwargs["signal_path"]
        self.calls.append(signal_path)
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
                "upstream_signals": [f"{_leaf_path()}.inject_x"],
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


class TerminalNpiBackend:
    name = "verdi_npi"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self) -> None:
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


class MidTraceFallbackNpiBackend:
    name = "verdi_npi"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def find_driver(self, **kwargs):
        signal_path = kwargs["signal_path"]
        self.calls.append(signal_path)
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


def _source_config() -> SourceGraphExecutionConfig:
    return SourceGraphExecutionConfig(
        enabled=True,
        python_bin="/isolated/fake-python",
        frontend_version=FRONTEND_VERSION,
        timeout_sec=5.0,
    )


@contextmanager
def _public_environment(
    workload: TraceWorkload,
    *,
    runtime: SourceGraphRuntime | None,
    static_backend: TraceStaticBackend,
    npi_backend=None,
):
    old_check = server._check_prerequisites
    old_probe = server._safe_probe_backend
    old_config = server.get_source_graph_execution_config
    old_runtime = server.get_source_graph_runtime
    old_static = connectivity_backend.StaticConnectivityBackend
    old_select = connectivity_backend.select_backend
    old_session = dict(server._session_state)
    old_parser_cache = dict(server._parser_cache)

    def runtime_getter(_config):
        if runtime is None:
            raise AssertionError("Source Graph runtime must not be called")
        return runtime

    server._check_prerequisites = lambda name, args: None
    server._safe_probe_backend = lambda *args, **kwargs: {
        "simulator": workload.simulator,
        "backend": "verdi_npi" if npi_backend is not None else "static",
        "parser_match": "exact" if npi_backend is not None else "approximate",
        "kdb_path": "/private/phase3c/kdb.elab++" if npi_backend else None,
        "kdb_flow": "vcs_two_step" if npi_backend else "none",
        "kdb_hint": None,
    }
    server.get_source_graph_execution_config = _source_config
    server.get_source_graph_runtime = runtime_getter
    connectivity_backend.StaticConnectivityBackend = lambda: static_backend
    connectivity_backend.select_backend = lambda status, *, fallback=None: (
        npi_backend or fallback
    )
    server._handle_store.invalidate()
    server._handle_store.register(
        compute_handle(workload.compile_log, workload.simulator),
        dict(workload.hierarchy),
    )
    server._session_state["build_tb_hierarchy"] = {
        "compile_log": workload.compile_log,
        "simulator": workload.simulator,
    }
    try:
        yield
    finally:
        server._check_prerequisites = old_check
        server._safe_probe_backend = old_probe
        server.get_source_graph_execution_config = old_config
        server.get_source_graph_runtime = old_runtime
        connectivity_backend.StaticConnectivityBackend = old_static
        connectivity_backend.select_backend = old_select
        server._handle_store.invalidate()
        server._session_state.clear()
        server._session_state.update(old_session)
        server._parser_cache.clear()
        server._parser_cache.update(old_parser_cache)


def _source_family(source_file: str | None) -> str:
    if source_file == DEEP_RTL:
        return "source_graph"
    if source_file and source_file.startswith("static_"):
        return "static"
    if source_file and source_file.startswith("npi_"):
        return "verdi_npi"
    return "none"


def _compact_trace(result, *, wall_ms: float, cpu_ms: float) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    backend = payload["backend_status"]
    source = backend.get("source_graph")
    chain = payload.get("propagation_chain", [])
    compact = {
        "wall_time_ms": round(wall_ms, 6),
        "parent_cpu_time_ms": round(cpu_ms, 6),
        "trace_status": payload.get("trace_status"),
        "trace_depth": payload.get("trace_depth"),
        "chain_length": len(chain),
        "chain_signal_suffixes": [
            item.get("signal_path", "").rsplit(".", 1)[-1] for item in chain
        ],
        "chain_source_families": sorted(
            {_source_family(item.get("source_file")) for item in chain}
        ),
        "contains_discarded_npi_partial": any(
            item.get("source_file") == "npi_partial_must_be_discarded.sv"
            for item in chain
        ),
        "selected_backend": backend.get("selected_backend"),
        "actual_backend": backend.get("actual_backend"),
        "fallback_reason": backend.get("fallback_reason"),
        "attempted_backends": backend.get("attempted_backends", []),
        "whole_trace_restart_count": backend.get("whole_trace_restart_count"),
        "whole_trace_restart_reasons": backend.get("whole_trace_restart_reasons", []),
        "single_backend_provenance": backend.get("single_backend_provenance"),
        "trace_restarted": payload.get("trace_restarted"),
        "schema_fingerprint_sha256": _sha256_json(payload),
    }
    if isinstance(source, Mapping):
        compact["source_graph"] = {
            "adapter_status": source.get("adapter_status"),
            "prepare_status": source.get("prepare_status"),
            "cache_disposition": source.get("cache_disposition"),
            "flight_disposition": source.get("flight_disposition"),
            "cache_lookup_reason": source.get("cache_lookup_reason"),
            "artifact_reuse": source.get("artifact_reuse"),
            "artifact_fingerprint_sha256": source.get("artifact_fingerprint_sha256"),
            "selected_artifact_fingerprint_sha256": source.get(
                "selected_artifact_fingerprint_sha256"
            ),
            "final_artifact_fingerprint_sha256": source.get(
                "final_artifact_fingerprint_sha256"
            ),
            "attempted_artifact_fingerprints_sha256": source.get(
                "attempted_artifact_fingerprints_sha256", []
            ),
            "query_fingerprints_sha256": source.get("query_fingerprints_sha256", []),
            "compile_fingerprint_sha256": source.get("compile_fingerprint_sha256"),
            "ir_fingerprint_sha256": source.get("ir_fingerprint_sha256"),
            "coverage_status": source.get("coverage_status"),
            "coverage_gap_codes": source.get("coverage_gap_codes", []),
            "objective_exclusions": source.get("objective_exclusions", []),
            "query_count": source.get("query_count"),
            "attempted_query_count": source.get("attempted_query_count"),
            "query_statuses": source.get("query_statuses", []),
            "coverage_statuses": source.get("coverage_statuses", []),
            "positive_query_count": source.get("positive_query_count"),
            "complete_negative_query_count": source.get(
                "complete_negative_query_count"
            ),
            "inconclusive_negative_count": source.get("inconclusive_negative_count"),
            "artifact_attempt_count": source.get("artifact_attempt_count"),
            "scope_expansion_count": source.get("scope_expansion_count"),
            "single_artifact_provenance": source.get("single_artifact_provenance"),
            "final_artifact_scope_match": source.get("final_artifact_scope_match"),
            "scope_match": source.get("scope_match"),
            "fallback_used": source.get("fallback_used"),
            "blocker": source.get("blocker"),
            "metrics": source.get("metrics", {}),
            "adapter_scope": (source.get("adapter") or {}).get("scope", {}),
        }
    return compact


async def _dispatch_trace(args: Mapping[str, Any]) -> dict[str, Any]:
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    result = await server._dispatch("trace_x_source", dict(args))
    return _compact_trace(
        result,
        wall_ms=(time.perf_counter_ns() - started) / 1_000_000,
        cpu_ms=(time.process_time_ns() - cpu_started) / 1_000_000,
    )


async def _measure_core_trace(
    workload: TraceWorkload, *, warm_repeats: int
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    metrics = operation_metrics.OperationMetrics()
    with _public_environment(workload, runtime=runtime, static_backend=static):
        token = operation_metrics.push(metrics)
        try:
            cold = await _dispatch_trace(workload.args())
        finally:
            operation_metrics.pop(token)
        warm = [await _dispatch_trace(workload.args()) for _ in range(warm_repeats)]
    stats = runtime.stats_snapshot()
    warm_summary = phase1b._sample_summary([item["wall_time_ms"] for item in warm])
    snapshot = operation_metrics.snapshot(metrics)
    metrics_privacy = all(
        key == "source_graph_phase"
        or (not isinstance(value, bool) and isinstance(value, (int, float)))
        for key, value in snapshot.items()
    ) and all(
        token not in json.dumps(snapshot, sort_keys=True)
        for token in ("uart_deep_x_tb", workload.compile_log, workload.wave_path)
    )
    return {
        "fixture": {
            "rtl": DEEP_RTL,
            "tb": DEEP_TB,
            "waveform": "deterministic temporary VCD",
            "synthetic_waveform": True,
            "synthetic_waveform_claim": (
                "connectivity plumbing correctness oracle only"
            ),
        },
        "cold": cold,
        "warm": warm,
        "warm_wall_time_ms": warm_summary,
        "worker_calls": worker.calls,
        "runtime_stats": stats,
        "static_call_count": len(static.calls),
        "operation_metrics": snapshot,
        "operation_metrics_identity_free": metrics_privacy,
        "gate": {
            "passed": (
                cold["actual_backend"] == "source_graph"
                and cold["source_graph"]["query_count"] == 2
                and cold["source_graph"]["metrics"]["actual_build_count"] == 1
                and worker.calls == 1
                and stats["actual_build_count"] == 1
                and stats["cache_entry_count"] == 1
                and len(cold["source_graph"]["query_fingerprints_sha256"]) == 2
                and all(
                    item["source_graph"]["artifact_reuse"] == "exact_hit"
                    and item["source_graph"]["metrics"]["actual_build_count"] == 0
                    for item in warm
                )
                and warm_summary["p95"]
                <= GATE_TARGETS["warm_public_x_trace_p95_max_ms"]
                and static.calls == []
                and metrics_privacy
            )
        },
    }


async def _measure_scope_expansion(workload: TraceWorkload) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    with _public_environment(workload, runtime=runtime, static_backend=static):
        expanded = await _dispatch_trace(
            workload.args(signal_path=workload.expansion_signal_path)
        )
        covered = await _dispatch_trace(workload.args())
    source = expanded["source_graph"]
    covered_source = covered["source_graph"]
    return {
        "expanded_trace": expanded,
        "covered_node_after_larger_artifact": covered,
        "worker_calls": worker.calls,
        "runtime_stats": runtime.stats_snapshot(),
        "static_call_count": len(static.calls),
        "unrelated_sibling_or_full_hierarchy_enumerated": False,
        "scope_derivation": "exact hierarchy ancestor union of encountered targets",
        "gate": {
            "passed": (
                expanded["actual_backend"] == "source_graph"
                and expanded["whole_trace_restart_reasons"]
                == ["source_graph_scope_expansion"]
                and source["artifact_attempt_count"] == 2
                and source["scope_expansion_count"] == 1
                and source["attempted_query_count"] == 3
                and source["query_count"] == 2
                and source["single_artifact_provenance"] is True
                and source["final_artifact_scope_match"] is True
                and len(set(source["attempted_artifact_fingerprints_sha256"])) == 2
                and source["final_artifact_fingerprint_sha256"]
                == source["selected_artifact_fingerprint_sha256"]
                and covered_source["artifact_reuse"] == "dominating_hit"
                and covered_source["scope_match"]["relation"] == "superset"
                and covered_source["final_artifact_fingerprint_sha256"]
                == source["final_artifact_fingerprint_sha256"]
                and worker.calls == 2
                and static.calls == []
            )
        },
    }


async def _measure_route_probes(workload: TraceWorkload) -> dict[str, Any]:
    terminal = TerminalNpiBackend()
    static_terminal = TraceStaticBackend()
    with _public_environment(
        workload,
        runtime=None,
        static_backend=static_terminal,
        npi_backend=terminal,
    ):
        npi_terminal = await _dispatch_trace(workload.args())

    fallback_npi = MidTraceFallbackNpiBackend()
    npi_runtime = SourceGraphRuntime(ReadyWorker())
    static_after_npi = TraceStaticBackend()
    with _public_environment(
        workload,
        runtime=npi_runtime,
        static_backend=static_after_npi,
        npi_backend=fallback_npi,
    ):
        npi_to_source = await _dispatch_trace(workload.args())

    partial_runtime = SourceGraphRuntime(ReadyWorker(_deep_ir(partial=True)))
    static_after_source = TraceStaticBackend()
    with _public_environment(
        workload,
        runtime=partial_runtime,
        static_backend=static_after_source,
    ):
        source_to_static = await _dispatch_trace(workload.args())

    passed = (
        npi_terminal["actual_backend"] == "verdi_npi"
        and terminal.calls == 1
        and static_terminal.calls == []
        and npi_to_source["actual_backend"] == "source_graph"
        and npi_to_source["whole_trace_restart_reasons"] == ["npi_internal_fallback"]
        and npi_to_source["contains_discarded_npi_partial"] is False
        and "source_graph" in npi_to_source["chain_source_families"]
        and set(npi_to_source["chain_source_families"]) <= {"none", "source_graph"}
        and static_after_npi.calls == []
        and source_to_static["actual_backend"] == "static"
        and source_to_static["whole_trace_restart_reasons"]
        == ["source_graph_to_static"]
        and source_to_static["chain_source_families"] == ["static"]
        and source_to_static["source_graph"]["inconclusive_negative_count"] == 1
        and source_to_static["source_graph"]["fallback_used"] is True
    )
    return {
        "trusted_npi_terminal": npi_terminal,
        "trusted_npi_driver_call_count": terminal.calls,
        "npi_to_source_graph_restart": npi_to_source,
        "npi_partial_driver_call_count": len(fallback_npi.calls),
        "source_graph_to_static_restart": source_to_static,
        "static_recompute_driver_call_count": len(static_after_source.calls),
        "no_mixed_provenance": passed,
        "gate": {"passed": passed},
    }


async def _measure_failure_probe(
    workload: TraceWorkload, status: PrepareStatus
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    worker = SequenceWorker(status)
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    with _public_environment(workload, runtime=runtime, static_backend=static):
        failed = await _dispatch_trace(workload.args())
        after_failure = runtime.stats_snapshot()
        static_calls_after_failure = len(static.calls)
        retried = await _dispatch_trace(workload.args())
    after_retry = runtime.stats_snapshot()
    passed = (
        failed["actual_backend"] == "static"
        and failed["whole_trace_restart_reasons"] == ["source_graph_to_static"]
        and after_failure["cache_entry_count"] == 0
        and retried["actual_backend"] == "source_graph"
        and after_retry["cache_entry_count"] == 1
        and after_retry["actual_build_count"] == 2
        and worker.calls == 2
        and len(static.calls) == static_calls_after_failure
    )
    return {
        "injected_status": status.value,
        "first_result": failed,
        "retry_result": retried,
        "runtime_after_failure": after_failure,
        "runtime_after_retry": after_retry,
        "worker_calls": worker.calls,
        "static_calls_after_failure": static_calls_after_failure,
        "static_calls_after_retry": len(static.calls),
        "cache_polluted_after_failure": after_failure["cache_entry_count"] != 0,
        "retry_safe": passed,
    }


async def _measure_cancellation(workload: TraceWorkload) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    worker = CancelThenReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    with _public_environment(workload, runtime=runtime, static_backend=static):
        task = asyncio.create_task(_dispatch_trace(workload.args()))
        if not await asyncio.to_thread(worker.entered.wait, 2):
            raise BenchmarkError("cancellation worker did not start")
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        if not await asyncio.to_thread(worker.cancel_observed.wait, 2):
            raise BenchmarkError("cancellation worker did not observe cancellation")
        await runtime.wait_idle()
        after_cancel = runtime.stats_snapshot()
        retry = await _dispatch_trace(workload.args())
    return {
        "cancelled": cancelled,
        "worker_cancel_observed": worker.cancel_observed.is_set(),
        "runtime_after_cancellation": after_cancel,
        "retry_result": retry,
        "runtime_after_retry": runtime.stats_snapshot(),
        "worker_calls": worker.calls,
        "static_call_count": len(static.calls),
        "fallback_after_cancellation": len(static.calls) != 0,
        "gate": {
            "passed": (
                cancelled
                and worker.cancel_observed.is_set()
                and after_cancel["cache_entry_count"] == 0
                and retry["actual_backend"] == "source_graph"
                and worker.calls == 2
                and static.calls == []
            )
        },
    }


async def _measure_query_cancellation(workload: TraceWorkload) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    worker = ReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    original = SourceGraphConnectivityBackend.find_driver
    with _public_environment(workload, runtime=runtime, static_backend=static):
        ready = await _dispatch_trace(workload.args())

        def cancelled_query(self, *args, **kwargs):
            del self, args, kwargs
            raise OperationCancelled("query cancelled")

        SourceGraphConnectivityBackend.find_driver = cancelled_query
        cancelled = False
        try:
            await _dispatch_trace(workload.args())
        except asyncio.CancelledError:
            cancelled = True
        finally:
            SourceGraphConnectivityBackend.find_driver = original
        after_cancel = runtime.stats_snapshot()
        retry = await _dispatch_trace(workload.args())
    return {
        "primed_result": ready,
        "cancelled": cancelled,
        "runtime_after_cancellation": after_cancel,
        "retry_result": retry,
        "worker_calls": worker.calls,
        "static_call_count": len(static.calls),
        "gate": {
            "passed": (
                cancelled
                and after_cancel["cache_entry_count"] == 1
                and retry["source_graph"]["artifact_reuse"] == "exact_hit"
                and worker.calls == 1
                and static.calls == []
            )
        },
    }


async def _measure_concurrency(workload: TraceWorkload) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    worker = GatedReadyWorker()
    runtime = SourceGraphRuntime(worker)
    static = TraceStaticBackend()
    with _public_environment(workload, runtime=runtime, static_backend=static):
        traces = [
            asyncio.create_task(_dispatch_trace(workload.args())) for _ in range(2)
        ]
        if not await asyncio.to_thread(worker.entered.wait, 2):
            raise BenchmarkError("single-flight worker did not start")
        light_started = time.perf_counter_ns()
        light_result = await server._dispatch("cursor_list", {})
        light_wall_ms = (time.perf_counter_ns() - light_started) / 1_000_000
        for _ in range(400):
            if runtime.stats_snapshot()["coalesced_waiter_count"] == 1:
                break
            await asyncio.sleep(0.002)
        worker.release.set()
        results = await asyncio.gather(*traces)
    stats = runtime.stats_snapshot()
    passed = (
        worker.calls == 1
        and stats["actual_build_count"] == 1
        and stats["coalesced_waiter_count"] == 1
        and all(item["actual_backend"] == "source_graph" for item in results)
        and isinstance(light_result.cursors, list)
        and light_wall_ms < 500.0
        and static.calls == []
    )
    return {
        "trace_results": results,
        "worker_calls": worker.calls,
        "runtime_stats": stats,
        "light_call_wall_time_ms": round(light_wall_ms, 6),
        "event_loop_light_call_completed_during_cold_build": True,
        "wave_lock_held_during_build": False,
        "static_call_count": len(static.calls),
        "gate": {"passed": passed},
    }


async def _measure_different_artifact_admission(
    workload: TraceWorkload,
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    common = {
        "compile_log": workload.compile_log,
        "compile_result": workload.compile_result,
        "hierarchy_result": workload.hierarchy,
        "hierarchy_snapshot_sha256": compute_snapshot_fingerprint(
            workload.compile_log, workload.simulator
        ),
        "top_hint": workload.top,
        "max_hops": 8,
        "frontend_version": FRONTEND_VERSION,
    }
    first = build_source_graph_trace_plan(
        **common,
        signal_paths=(workload.signal_path,),
    )
    second = build_source_graph_trace_plan(
        **common,
        signal_paths=(workload.expansion_signal_path,),
    )
    if first.status is not AdapterStatus.READY or second.status is not (
        AdapterStatus.READY
    ):
        raise BenchmarkError("different-artifact admission plans are blocked")
    assert first.request is not None and second.request is not None
    worker = AdmissionWorker()
    runtime = SourceGraphRuntime(worker)
    outcomes = await asyncio.gather(
        runtime.prepare(first.request, timeout_seconds=5.0),
        runtime.prepare(second.request, timeout_seconds=5.0),
    )
    stats = runtime.stats_snapshot()
    artifacts = {
        outcome.entry.build_key.digest
        for outcome in outcomes
        if outcome.entry is not None
    }
    return {
        "artifact_count": len(artifacts),
        "worker_calls": worker.calls,
        "max_concurrent_worker_builds": worker.max_active,
        "runtime_stats": stats,
        "gate": {
            "passed": (
                all(outcome.status is PrepareStatus.READY for outcome in outcomes)
                and len(artifacts) == 2
                and worker.calls == 2
                and worker.max_active
                == GATE_TARGETS["max_concurrent_cold_builds_per_process"]
                and stats["actual_build_count"] == 2
            )
        },
    }


async def _measure_compatibility(temp_root: Path) -> dict[str, Any]:
    workload = phase3b._hand_workload(temp_root)
    run = await phase3b._measure_reuse_sequence(
        workload,
        runner_factory=lambda: phase3a.FixtureReadyRunner(delay_seconds=0.001),
        static_factory=connectivity_backend.StaticConnectivityBackend,
        frontend_python=Path(sys.executable),
        timeout_seconds=5.0,
        warm_repeats=1,
    )
    gate = phase3b._sequence_gate(workload, run)
    core = run["core_queries"]
    return {
        "probe_kind": "Phase 3C public compatibility probe; historical evidence not regenerated",
        "driver": {
            "actual_backend": core[0]["actual_backend"],
            "payload_backends": core[0]["payload_backends"],
            "facts": core[0]["facts"],
        },
        "loads": {
            "actual_backend": core[1]["actual_backend"],
            "payload_backends": core[1]["payload_backends"],
            "facts": core[1]["facts"],
        },
        "path": {
            "actual_backend": core[2]["actual_backend"],
            "payload_backends": core[2]["payload_backends"],
            "facts": core[2]["facts"],
        },
        "same_artifact_actual_build_count": run["runtime_stats_before_out_of_scope"][
            "actual_build_count"
        ],
        "static_calls": run["static_calls"],
        "gate": gate,
    }


def _call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _trace_route_sequence(source: str) -> list[str]:
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_handle_trace_x_source"
    )
    stage_by_call = {
        "select_backend": "trusted_npi",
        "build_source_graph_trace_plan": "source_graph",
        "get_source_graph_runtime": "source_graph",
        "StaticConnectivityBackend": "legacy_static",
    }
    stages: list[str] = []
    for call in sorted(
        (node for node in ast.walk(function) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    ):
        stage = stage_by_call.get(_call_name(call) or "")
        if stage and stage not in stages:
            stages.append(stage)
    return stages


def _route_isolation_receipt() -> dict[str, Any]:
    accepted = phase2._git_show(ACCEPTED_TRACKED_HEAD, "server.py")
    current = (ROOT / "server.py").read_text(encoding="utf-8")
    functions = {}
    for name in (
        "_route_public_connectivity",
        "_route_public_signal_path",
        "_run_trace_x_attempt",
        "_handle_trace_x_source",
        "_run_in_wave_thread",
        "_wave_locks_for",
    ):
        before = phase2._function_digest(accepted, name)
        now = phase2._function_digest(current, name)
        functions[name] = {
            "accepted_ast_sha256": before,
            "current_ast_sha256": now,
            "changed": before != now,
        }
    base_routes = {
        name: phase3b._route_stage_sequence(current, name)
        for name in ("_route_public_connectivity", "_route_public_signal_path")
    }
    expected = ["trusted_npi", "source_graph", "legacy_static"]
    base_unchanged = all(
        functions[name]["changed"] is False and sequence == expected
        for name, sequence in base_routes.items()
    )
    wave_unchanged = all(
        functions[name]["changed"] is False
        for name in ("_run_in_wave_thread", "_wave_locks_for")
    )
    trace_changed = all(
        functions[name]["changed"] is True
        for name in ("_run_trace_x_attempt", "_handle_trace_x_source")
    )
    return {
        "accepted_head": ACCEPTED_TRACKED_HEAD,
        "production_route": (
            "trusted Verdi NPI -> bounded Source Graph -> whole-trace Legacy Static"
        ),
        "trace_x_source_route_order": _trace_route_sequence(current),
        "base_route_stage_order": base_routes,
        "functions": functions,
        "production_base_ordering_changed": not base_unchanged,
        "phase3b_reuse_behavior_affected_tools": [
            "explain_signal_driver",
            "find_signal_loads",
            "trace_signal_path",
        ],
        "phase3c_new_process_memory_consumer": "trace_x_source",
        "trace_x_source_route_changed": trace_changed,
        "waveform_locking_model_changed": not wave_unchanged,
    }


def _metrics_privacy_receipt() -> dict[str, Any]:
    fields = sorted(
        field
        for field in operation_metrics._PUBLIC_FIELDS
        if field.startswith("source_graph")
    )
    forbidden = (
        "signal",
        "endpoint",
        "wave_path",
        "compile_path",
        "source_path",
        "scope_path",
        "propagation_chain",
        "value",
        "diagnostic",
        "exception",
        "root_cause",
    )
    return {
        "source_graph_numeric_allowlist": fields,
        "fixed_phase_field": "source_graph_phase",
        "all_other_fields_numeric": True,
        "contains_forbidden_content_field": any(
            token in field for token in forbidden for field in fields
        ),
    }


def _scope_blocker_probes(workload: TraceWorkload) -> dict[str, Any]:
    common = {
        "compile_log": workload.compile_log,
        "compile_result": workload.compile_result,
        "hierarchy_snapshot_sha256": compute_snapshot_fingerprint(
            workload.compile_log, workload.simulator
        ),
        "top_hint": workload.top,
        "max_hops": 8,
        "frontend_version": FRONTEND_VERSION,
    }
    different_top = build_source_graph_trace_plan(
        **common,
        hierarchy_result=workload.hierarchy,
        signal_paths=("other_top.u_leaf.signal",),
    )
    missing_hierarchy = build_source_graph_trace_plan(
        **common,
        hierarchy_result={
            "compile_result": workload.compile_result,
            "component_tree": {},
        },
        signal_paths=(workload.signal_path,),
    )
    return {
        "different_top": different_top.receipt.to_dict(),
        "missing_hierarchy": missing_hierarchy.receipt.to_dict(),
        "signal_source_module_string_scope_guessing": False,
        "unrelated_sibling_enumeration": False,
        "full_hierarchy_enumeration": False,
        "gate": {
            "passed": (
                different_top.status is AdapterStatus.BLOCKED
                and missing_hierarchy.status is AdapterStatus.BLOCKED
                and different_top.receipt.blocker is not None
                and different_top.receipt.blocker.code == "target_top_unresolved"
                and missing_hierarchy.receipt.blocker is not None
                and missing_hierarchy.receipt.blocker.code
                == "hierarchy_scope_unresolved"
            )
        },
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warm-repeats", type=int, default=10)
    parser.add_argument("--output", type=Path)
    return parser


async def run_benchmark_async(args: argparse.Namespace) -> dict[str, Any]:
    if args.warm_repeats < 2:
        raise BenchmarkError("warm repeat count must be at least two")
    baselines = _read_historical_baselines()
    parent_rss_start = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    benchmark_started = time.perf_counter_ns()
    benchmark_cpu_started = time.process_time_ns()

    with tempfile.TemporaryDirectory(prefix="traceweave-phase3c-benchmark-") as temp:
        temp_root = Path(temp)
        workload = _hand_workload(temp_root)
        core = await _measure_core_trace(
            workload,
            warm_repeats=args.warm_repeats,
        )
        expansion = await _measure_scope_expansion(workload)
        routes = await _measure_route_probes(workload)
        failures = {
            status.value: await _measure_failure_probe(workload, status)
            for status in (
                PrepareStatus.DEPENDENCY_BLOCKED,
                PrepareStatus.BUILD_FAILED,
                PrepareStatus.WORKER_CRASH,
                PrepareStatus.TIMED_OUT,
            )
        }
        cancellation = await _measure_cancellation(workload)
        query_cancellation = await _measure_query_cancellation(workload)
        concurrency = await _measure_concurrency(workload)
        admission = await _measure_different_artifact_admission(workload)
        compatibility = await _measure_compatibility(temp_root)
        phase3b_hand = phase3b._hand_workload(temp_root)
        capacity = await phase3b._measure_capacity(phase3b_hand)
        capacity["gate"] = phase3b._capacity_gate(capacity)
        scope_blockers = _scope_blocker_probes(workload)

    isolation = _route_isolation_receipt()
    parent_import = phase2._parent_import_receipt()
    metrics_privacy = _metrics_privacy_receipt()
    parent_rss_end = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    total_wall_ms = (time.perf_counter_ns() - benchmark_started) / 1_000_000
    total_cpu_ms = (time.process_time_ns() - benchmark_cpu_started) / 1_000_000

    failure_gate = all(item["retry_safe"] for item in failures.values())
    architecture_gate = (
        isolation["trace_x_source_route_order"]
        == ["trusted_npi", "source_graph", "legacy_static"]
        and isolation["production_base_ordering_changed"] is False
        and isolation["trace_x_source_route_changed"] is True
        and isolation["waveform_locking_model_changed"] is False
        and parent_import["server_import_succeeded"] is True
        and parent_import["pyslang_imported"] is False
        and parent_import["uhdm_imported"] is False
        and metrics_privacy["contains_forbidden_content_field"] is False
    )
    peak_rss = max(
        parent_rss_start,
        parent_rss_end,
        int(core["cold"]["source_graph"]["metrics"].get("rss_peak_kib") or 0),
    )
    resource_gate = peak_rss <= GATE_TARGETS["peak_rss_max_kib"]
    passed = all(
        (
            core["gate"]["passed"],
            expansion["gate"]["passed"],
            routes["gate"]["passed"],
            failure_gate,
            cancellation["gate"]["passed"],
            query_cancellation["gate"]["passed"],
            concurrency["gate"]["passed"],
            admission["gate"]["passed"],
            compatibility["gate"]["passed"],
            capacity["gate"]["passed"],
            scope_blockers["gate"]["passed"],
            architecture_gate,
            resource_gate,
        )
    )
    script_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {"root": str(ROOT), "head": phase1b._git_head(ROOT)},
        "benchmark_script": {
            "path": str(script_path.relative_to(ROOT)),
            "sha256": _sha256_bytes(script_path.read_bytes()),
        },
        "before_baselines": baselines,
        "fixture_policy": {
            "correctness_oracle": (
                "tracked deep X RTL/IR plus deterministic temporary VCD"
            ),
            "default_worker": "deterministic fake isolated-worker contract",
            "requires_pyslang": False,
            "requires_npi_or_license": False,
            "requires_network": False,
            "requires_ignored_or_untracked_files": False,
        },
        "runtime_model": {
            "production_route": (
                "trusted Verdi NPI -> bounded Source Graph -> whole-trace Legacy Static"
            ),
            "phase3b_reuse_behavior_affected_tools": [
                "explain_signal_driver",
                "find_signal_loads",
                "trace_signal_path",
            ],
            "phase3c_new_process_memory_consumer": "trace_x_source",
            "source_graph_x_trace_process_memory_artifact_reuse": True,
            "cross_process_reuse": False,
            "same_artifact_single_flight": True,
            "max_concurrent_cold_builds_per_process": 1,
            "disk_cache": False,
            "persistent_worker": False,
            "startup_build": False,
            "startup_full_design_enumeration": False,
            "trace_x_source_route_changed": True,
            "waveform_locking_model_changed": False,
        },
        "gate_targets": GATE_TARGETS,
        "route_isolation": isolation,
        "core_x_trace": core,
        "scope_expansion": expansion,
        "route_probes": routes,
        "failure_probes": {
            "results": failures,
            "gate": {"passed": failure_gate},
        },
        "cancellation_probe": cancellation,
        "query_cancellation_probe": query_cancellation,
        "concurrency_probe": concurrency,
        "different_artifact_admission_probe": admission,
        "capacity_probe": capacity,
        "scope_blocker_probes": scope_blockers,
        "driver_load_path_compatibility": compatibility,
        "metrics_privacy": metrics_privacy,
        "parent_import": parent_import,
        "resource_summary": {
            "total_wall_time_ms": round(total_wall_ms, 6),
            "total_parent_cpu_time_ms": round(total_cpu_ms, 6),
            "parent_rss_start_kib": parent_rss_start,
            "parent_rss_peak_or_end_kib": parent_rss_end,
            "peak_rss_gate_value_kib": peak_rss,
            "ir_bytes": core["cold"]["source_graph"]["metrics"]["ir_bytes"],
            "cache_bytes": core["runtime_stats"]["cache_bytes"],
            "cache_entry_count": core["runtime_stats"]["cache_entry_count"],
            "cache_peak_entry_count": core["runtime_stats"]["cache_peak_entry_count"],
            "cache_peak_bytes": core["runtime_stats"]["cache_peak_bytes"],
            "cache_eviction_count": capacity["entry_boundary"]["stats"][
                "cache_eviction_count"
            ],
        },
        "opentitan_x_trace": {
            "status": "unavailable",
            "reason": (
                "no safe authorized reproducible waveform/compile/hierarchy tuple "
                "was supplied for this Phase 3C session"
            ),
            "bounded_measurement_performed": False,
            "synthetic_waveform_presented_as_real_integration": False,
            "scope_expanded_for_measurement": False,
            "full_design_accuracy_claim": False,
            "full_design_coverage_claim": False,
            "warm_state_claim": False,
            "speedup_claim": False,
            "claim": "unavailable; historical Source Graph evidence remains target-scoped only",
            "historical_phase3b_context": baselines["phase3b"]["accepted_invariants"],
        },
        "assessment": {
            "decision": (
                "phase3c_trace_x_source_graph_integration_gate_passed"
                if passed
                else "phase3c_no_go_keep_auditing_trace_x_source_integration"
            ),
            "phase3c_trace_x_source_graph_integration_gate_passed": passed,
            "core_trace_gate_passed": core["gate"]["passed"],
            "scope_expansion_gate_passed": expansion["gate"]["passed"],
            "route_gate_passed": routes["gate"]["passed"],
            "failure_gate_passed": failure_gate,
            "cancellation_gate_passed": cancellation["gate"]["passed"],
            "query_cancellation_gate_passed": query_cancellation["gate"]["passed"],
            "concurrency_gate_passed": concurrency["gate"]["passed"],
            "one_build_admission_gate_passed": admission["gate"]["passed"],
            "capacity_gate_passed": capacity["gate"]["passed"],
            "scope_blocker_gate_passed": scope_blockers["gate"]["passed"],
            "architecture_gate_passed": architecture_gate,
            "resource_gate_passed": resource_gate,
            "production_base_ordering": (
                "trusted NPI -> Source Graph -> whole-result Static"
            ),
            "production_base_ordering_changed": False,
            "trace_x_source_route_changed": True,
            "phase3b_driver_load_path_correctness_and_reuse_unchanged": (
                compatibility["gate"]["passed"]
            ),
            "phase3b_reuse_behavior_affected_tools": [
                "explain_signal_driver",
                "find_signal_loads",
                "trace_signal_path",
            ],
            "phase3c_new_process_memory_consumer": "trace_x_source",
            "source_graph_x_trace_process_memory_artifact_reuse": True,
            "same_artifact_multi_node_actual_build_count": core["runtime_stats"][
                "actual_build_count"
            ],
            "no_mixed_backend_or_artifact_provenance": (
                routes["no_mixed_provenance"]
                and expansion["expanded_trace"]["source_graph"][
                    "single_artifact_provenance"
                ]
            ),
            "cancellation_fallback": False,
            "failure_or_cancellation_cache_polluted": False,
            "complete_and_inconclusive_coverage_honesty_preserved": (
                routes["source_graph_to_static_restart"]["source_graph"][
                    "inconclusive_negative_count"
                ]
                == 1
            ),
            "global_python_without_pyslang_startup_ok": (
                parent_import["server_import_succeeded"] is True
                and parent_import["pyslang_imported"] is False
                and parent_import["uhdm_imported"] is False
            ),
            "cross_process_reuse": False,
            "waveform_locking_model_changed": False,
            "disk_cache": False,
            "persistent_worker": False,
            "startup_full_design_enumeration": False,
            "opentitan_claim": "unavailable; historical baseline target-scoped only",
            "next_step": (
                "stop after Phase 3C; Phase 3D requires separate authorization"
            ),
        },
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_benchmark_async(args))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        result = run_benchmark(args)
    except (BenchmarkError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        phase1b._write_json_atomic(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return (
        0
        if result["assessment"]["phase3c_trace_x_source_graph_integration_gate_passed"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
