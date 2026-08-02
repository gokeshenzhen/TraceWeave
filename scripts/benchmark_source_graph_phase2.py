#!/usr/bin/env python3
"""Benchmark Phase 2 public Source Graph driver/load production routing.

The default workload is a tracked hand fixture.  OpenTitan is opt-in and uses
only an existing compile log plus a pre-resolved four-path target ancestor
chain; it never enumerates the full hierarchy and never claims full-design
accuracy or speedup.  Optional pyslang stays in the isolated worker process.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SourceGraphExecutionConfig  # noqa: E402
import server  # noqa: E402
from scripts import benchmark_source_graph_phase1b as phase1b  # noqa: E402
import src.connectivity_backend as connectivity_backend  # noqa: E402
from src.compile_log_parser import parse_compile_log  # noqa: E402
from src.hierarchy_handles import compute_handle  # noqa: E402
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME  # noqa: E402
from src.source_graph_adapter import (  # noqa: E402
    _reset_source_graph_adapter_cache_for_tests,
)
from src.source_graph_runtime import (  # noqa: E402
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    SourceGraphRuntime,
    SourceGraphWorkerRunner,
    WorkerBuildResult,
)


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase2"
FRONTEND_VERSION = "11.0.0"
ACCEPTED_PHASE1B_HEAD = "f377abff886631322605d6d3e572641014d97413"
DEFAULT_FRONTEND_PYTHON = Path("/tmp/traceweave-phase0b-pyslang-11.0.0/bin/python")
DEFAULT_PHASE1A_EVIDENCE = (
    ROOT / "benchmarks/source_graph_connectivity_phase1a_results.json"
)
DEFAULT_PHASE1B_EVIDENCE = (
    ROOT / "benchmarks/source_graph_connectivity_phase1b_results.json"
)
PHASE1A_EVIDENCE_SHA256 = (
    "c7310560a1e89e19694a83d41e24a645578b747585c68df546a20937f3fa42e2"
)
PHASE1B_EVIDENCE_SHA256 = (
    "c9a25c96c63ddce9205ecabf86b61f6da1eff9ba0f71aeee099a6b04f7237da7"
)
DEFAULT_OPENTITAN_COMPILE_LOG = Path(
    "/tmp/traceweave-phase0b-opentitan-dvsim/phase0b-cold/"
    "chip_earlgrey_asic-sim-xcelium/default/fusesoc-work/xrun.log"
)
GATE_TARGETS = {
    "opentitan_public_cold_p50_max_ms": 15_000.0,
    "warm_public_prepare_query_p95_max_ms": 100.0,
    "peak_rss_max_kib": 2_621_440,
    "same_key_actual_build_count": 1,
}
REQUIRED_PHASE1B_OPENTITAN_EXCLUSIONS = {
    "bind_semantics_incomplete",
    "dpi_runtime_not_modeled",
    "procedural_force_not_modeled",
    "protected_payload_not_modeled",
    "uvm_dynamic_connectivity_not_modeled",
}
REQUIRED_OPENTITAN_OBSERVED_BOUNDARY_FACTS = {
    "bind_semantics",
    "definition_replacement_semantics",
    "dpi_runtime",
    "procedural_force_release",
    "uvm_dynamic_connectivity",
}
REQUIRED_PRODUCTION_MARKER_EXCLUSIONS = {
    "bind_semantics",
    "dpi_runtime",
    "procedural_force_release",
    "protected_region",
    "uvm_dynamic_connectivity",
}


BenchmarkError = phase1b.BenchmarkError
RunnerFactory = Callable[[], SourceGraphWorkerRunner]
StaticFactory = Callable[[], Any]


class FailureFactory(Protocol):
    def __call__(self) -> SourceGraphWorkerRunner: ...


@dataclass(frozen=True)
class FailureRunnerFactories:
    build_failure: FailureFactory
    crash: FailureFactory
    timeout: FailureFactory
    cancellation: FailureFactory


@dataclass(frozen=True)
class PublicWorkload:
    name: str
    compile_log: str
    simulator: str
    compile_result: dict[str, Any]
    hierarchy: dict[str, Any]
    top: str
    driver_signal: str
    load_signal: str
    scope_claim: str

    def driver_args(self) -> dict[str, Any]:
        return {
            "signal_path": self.driver_signal,
            "wave_path": "/unused/source_graph_benchmark.fsdb",
            "compile_log": self.compile_log,
            "simulator": self.simulator,
            "top_hint": self.top,
            "recursive": True,
            "max_depth": 64,
        }

    def load_args(self) -> dict[str, Any]:
        return {
            "signal_path": self.load_signal,
            "compile_log": self.compile_log,
            "simulator": self.simulator,
            "top_hint": self.top,
            "max_depth": 64,
            "include_expr": True,
        }


class _FirstThenRunner:
    def __init__(self, first: SourceGraphWorkerRunner, then: SourceGraphWorkerRunner):
        self.first = first
        self.then = then
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.calls += 1
        runner = self.first if self.calls == 1 else self.then
        return await runner.run(
            request,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )


class _TimeoutOverrideRunner:
    def __init__(self, delegate: SourceGraphWorkerRunner, timeout_seconds: float):
        self.delegate = delegate
        self.timeout_seconds = timeout_seconds

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        return await self.delegate.run(
            request,
            timeout_seconds=self.timeout_seconds,
            cancel_event=cancel_event,
        )


class _FailedRunner:
    def __init__(self, status: PrepareStatus, code: str):
        self.status = status
        self.code = code
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del request, timeout_seconds, cancel_event
        self.calls += 1
        return WorkerBuildResult.failed(
            self.status,
            code=self.code,
            stage="worker_process",
        )


class _FakeNpiBackend:
    name = "verdi_npi"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self) -> None:
        self.driver_calls = 0
        self.load_calls = 0

    def find_driver(self, **kwargs):
        self.driver_calls += 1
        return {
            "signal_path": kwargs["signal_path"],
            "wave_path": kwargs["wave_path"],
            "resolved_rtl_name": "lane_data",
            "resolved_module": "sg_top",
            "resolved_instance_path": "sg_top",
            "driver_status": "resolved",
            "driver_kind": "continuous_assign",
            "source_file": "fixture/hand_connectivity.sv",
            "source_line": 1,
            "source_info_origin": "npi",
            "expression_summary": None,
            "upstream_signals": [],
            "confidence": "exact",
            "recursive": kwargs["recursive"],
            "driver_chain": None,
            "chain_summary": None,
            "backend": "verdi_npi",
        }

    def find_loads(self, **kwargs):
        self.load_calls += 1
        return {
            "signal_path": kwargs["signal_path"],
            "resolved_rtl_name": "data[7:0]",
            "resolved_module": "sg_bus",
            "resolved_instance_path": "sg_top.bus",
            "loads": [],
            "completeness": "exact",
            "stopped_at": "no_npi_loads",
            "unsupported_reason": None,
            "backend": "verdi_npi",
        }


class _CountingStaticBackend:
    name = "static"
    uses_external_worker = False

    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.driver_calls = 0
        self.load_calls = 0

    def find_driver(self, **kwargs):
        self.driver_calls += 1
        return self.delegate.find_driver(**kwargs)

    def find_loads(self, **kwargs):
        self.load_calls += 1
        return self.delegate.find_loads(**kwargs)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        action="append",
        choices=("hand_fixture", "opentitan_core"),
        help="Repeat to select workloads (default: tracked hand fixture)",
    )
    parser.add_argument("--frontend-python", type=Path, default=DEFAULT_FRONTEND_PYTHON)
    parser.add_argument(
        "--opentitan-compile-log", type=Path, default=DEFAULT_OPENTITAN_COMPILE_LOG
    )
    parser.add_argument("--cold-repeats", type=int, default=3)
    parser.add_argument("--warm-repeats", type=int, default=100)
    parser.add_argument("--concurrent-requests", type=int, default=4)
    parser.add_argument("--worker-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--failure-timeout-seconds", type=float, default=0.001)
    parser.add_argument("--cancellation-delay-seconds", type=float, default=0.01)
    parser.add_argument(
        "--phase1a-evidence", type=Path, default=DEFAULT_PHASE1A_EVIDENCE
    )
    parser.add_argument(
        "--phase1b-evidence", type=Path, default=DEFAULT_PHASE1B_EVIDENCE
    )
    parser.add_argument("--output", type=Path)
    return parser


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _read_historical_baselines(
    phase1a_path: Path, phase1b_path: Path
) -> dict[str, Any]:
    phase1a_hash = phase1b._sha256_file(phase1a_path)
    phase1b_hash = phase1b._sha256_file(phase1b_path)
    if phase1a_hash != PHASE1A_EVIDENCE_SHA256:
        raise BenchmarkError(
            "Phase 1A evidence hash mismatch; stop without regeneration"
        )
    if phase1b_hash != PHASE1B_EVIDENCE_SHA256:
        raise BenchmarkError(
            "Phase 1B evidence hash mismatch; stop without regeneration"
        )
    phase1a_payload = json.loads(phase1a_path.read_text(encoding="utf-8"))
    phase1b_payload = json.loads(phase1b_path.read_text(encoding="utf-8"))
    phase1a_expected = {
        "schema_version": "1.0",
        "benchmark": "source_graph_connectivity_phase1a",
    }
    if any(
        phase1a_payload.get(key) != value for key, value in phase1a_expected.items()
    ):
        raise BenchmarkError("Phase 1A schema/benchmark mismatch")
    if phase1a_payload.get("repository", {}).get("head") != (
        "1d4f43e8c9745d03630f89014026f4595c391409"
    ):
        raise BenchmarkError("Phase 1A measurement HEAD mismatch")
    if (
        phase1a_payload.get("assessment", {}).get("decision")
        != ("go_for_production_integration_review")
        or phase1a_payload.get("assessment", {}).get("production_route_changed")
        is not False
    ):
        raise BenchmarkError("Phase 1A assessment mismatch")

    if (
        phase1b_payload.get("schema_version") != "1.0"
        or phase1b_payload.get("benchmark") != "source_graph_connectivity_phase1b"
    ):
        raise BenchmarkError("Phase 1B schema/benchmark mismatch")
    if phase1b_payload.get("repository", {}).get("head") != (
        "b1ad0da31f27cf9120fb22faff6e0a7c69101f40"
    ):
        raise BenchmarkError("Phase 1B measurement HEAD mismatch")
    assessment = phase1b_payload.get("assessment", {})
    if assessment.get("decision") != (
        "phase1b_internal_gate_passed_await_production_integration_approval"
    ):
        raise BenchmarkError("Phase 1B decision mismatch")
    for field, expected in (
        ("phase1b_internal_gate_passed", True),
        ("production_route_changed", False),
        ("public_production_integration_performed", False),
    ):
        if assessment.get(field) is not expected:
            raise BenchmarkError(f"Phase 1B assessment field mismatch: {field}")
    workloads = {item["name"]: item for item in phase1b_payload.get("workloads", ())}
    if set(workloads) != {"deep_x_npi", "hand_fixture", "opentitan_core"}:
        raise BenchmarkError("Phase 1B workload set mismatch")
    if not all(
        item.get("gate", {}).get("passed") is True for item in workloads.values()
    ):
        raise BenchmarkError("Phase 1B workload gate mismatch")
    opentitan = workloads["opentitan_core"]
    aggregate = opentitan["aggregate"]
    if aggregate["cold_prepare_wall_ms"]["p50"] > 15_000.0:
        raise BenchmarkError("Phase 1B OpenTitan cold gate mismatch")
    if any(
        aggregate["warm_prepare_queries"][operation]["wall_latency_ms"]["p95"] > 100.0
        for operation in ("driver", "load")
    ):
        raise BenchmarkError("Phase 1B warm gate mismatch")
    if aggregate["peak_rss_kib"]["max"] > 2_621_440:
        raise BenchmarkError("Phase 1B memory gate mismatch")
    if aggregate["coverage_statuses"] != ["inconclusive"]:
        raise BenchmarkError("Phase 1B coverage mismatch")
    if aggregate["blocking_diagnostic_counts"] != [65]:
        raise BenchmarkError("Phase 1B blocking diagnostic mismatch")
    if aggregate["representative_query_confidences"] != ["partial"]:
        raise BenchmarkError("Phase 1B query confidence mismatch")
    scope = opentitan["scope"]
    if len(scope["coverage_boundary"]["instance_paths"]) != 5:
        raise BenchmarkError("Phase 1B coverage boundary mismatch")
    if len(scope["requested_cone"]["instance_paths"]) != 2:
        raise BenchmarkError("Phase 1B requested cone mismatch")
    if set(scope["coverage_boundary"]["objective_exclusions"]) != (
        REQUIRED_PHASE1B_OPENTITAN_EXCLUSIONS
    ):
        raise BenchmarkError("Phase 1B objective exclusion mismatch")
    if any(
        item["concurrent_same_key"]["actual_build_count"] != 1
        for item in workloads.values()
    ):
        raise BenchmarkError("Phase 1B single-flight mismatch")
    probes = phase1b_payload["failure_probes"]
    if probes["gate"]["passed"] is not True:
        raise BenchmarkError("Phase 1B failure gate mismatch")
    for kind in ("crash", "timeout", "cancellation"):
        result = probes["results"][kind]
        if (
            result["cache_entry_count_after_failure"] != 0
            or result["failure"]["fallback_used"] is not False
            or result["retry"]["status"] != "ready"
        ):
            raise BenchmarkError(f"Phase 1B {kind} failure contract mismatch")
    return {
        "phase1a": {
            "sha256": phase1a_hash,
            "measurement_head": phase1a_payload["repository"]["head"],
            "decision": phase1a_payload["assessment"]["decision"],
            "production_route_changed": False,
        },
        "phase1b": {
            "sha256": phase1b_hash,
            "measurement_head": phase1b_payload["repository"]["head"],
            "decision": assessment["decision"],
            "production_route_changed": False,
            "public_production_integration_performed": False,
            "opentitan_reference_scope": {
                "coverage_boundary_path_count": 5,
                "requested_assignment_cone_path_count": 2,
                "coverage_status": "inconclusive",
                "blocking_diagnostic_count": 65,
                "representative_query_confidence": "partial",
                "objective_exclusions": sorted(REQUIRED_PHASE1B_OPENTITAN_EXCLUSIONS),
            },
        },
    }


def _hand_workload(temp_root: Path) -> PublicWorkload:
    source = ROOT / "tests/fixtures/source_graph_frontend/hand_connectivity.sv"
    compile_log = temp_root / "hand_compile.log"
    command = f"xrun {source} -top sg_top"
    compile_log.write_text(command + "\n", encoding="utf-8")
    compile_result = {
        "simulator": "xcelium",
        "compile_cwd": str(ROOT),
        "compile_command": command,
        "compile_replay_command": command,
        "top_modules": ["sg_top"],
        "files": {
            "user": [
                {
                    "path": str(source),
                    "type": "module",
                    "category": "rtl",
                }
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
    return PublicWorkload(
        name="hand_fixture",
        compile_log=str(compile_log),
        simulator="xcelium",
        compile_result=compile_result,
        hierarchy=hierarchy,
        top="sg_top",
        driver_signal="sg_top.u_producer.seed",
        load_signal="sg_top.u_producer.seed",
        scope_claim="tracked eight-instance hand fixture; not a production accuracy claim",
    )


def _coverage_boundary_workload(temp_root: Path) -> PublicWorkload:
    source = ROOT / "tests/fixtures/source_graph_frontend/hand_connectivity.sv"
    markers = ROOT / "tests/fixtures/source_graph_frontend/coverage_boundaries.sv"
    compile_log = temp_root / "coverage_boundary_compile.log"
    command = f"xrun {source} {markers} -top sg_top"
    compile_log.write_text(command + "\n", encoding="utf-8")
    compile_result = {
        "simulator": "xcelium",
        "compile_cwd": str(ROOT),
        "compile_command": command,
        "compile_replay_command": command,
        "top_modules": ["sg_top"],
        "files": {
            "user": [
                {"path": str(source), "type": "module", "category": "rtl"},
                {"path": str(markers), "type": "module", "category": "rtl"},
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
    return PublicWorkload(
        name="coverage_boundary_probe",
        compile_log=str(compile_log),
        simulator="xcelium",
        compile_result=compile_result,
        hierarchy=hierarchy,
        top="sg_top",
        driver_signal="sg_top.u_producer.seed",
        load_signal="sg_top.u_producer.seed",
        scope_claim=(
            "tracked marker fixture proving public UVM/DPI/force/bind/protected "
            "coverage receipts; not an accuracy or performance workload"
        ),
    )


def _opentitan_workload(compile_log: Path) -> PublicWorkload:
    if not compile_log.is_file():
        raise BenchmarkError(f"OpenTitan compile log unavailable: {compile_log}")
    compile_result = parse_compile_log(str(compile_log), "xcelium")
    leaf = {"module": "rv_core_ibex", "children": {}}
    top = {"module": "top_earlgrey", "children": {"u_rv_core_ibex": leaf}}
    dut = {"module": "chip_earlgrey_asic", "children": {"top_earlgrey": top}}
    hierarchy = {
        "compile_result": compile_result,
        "component_tree": {"tb": {"dut": dut}},
    }
    signal = "tb.dut.top_earlgrey.u_rv_core_ibex.fatal_intg_event"
    return PublicWorkload(
        name="opentitan_core",
        compile_log=str(compile_log),
        simulator="xcelium",
        compile_result=compile_result,
        hierarchy=hierarchy,
        top="tb",
        driver_signal=signal,
        load_signal=signal,
        scope_claim=(
            "target-specific public adapter scope: four ancestor paths and one "
            "assignment-bearing cone path; not Phase 1A/1B five-path/two-cone "
            "scope and not a full-design accuracy or speedup claim"
        ),
    )


def _fixed_probe(with_kdb: bool) -> dict[str, Any]:
    return {
        "simulator": "xcelium",
        "backend": "verdi_npi" if with_kdb else "static",
        "parser_match": "exact" if with_kdb else "approximate",
        "kdb_path": "/private/routing_probe/kdb.elab++" if with_kdb else None,
        "kdb_flow": "vcs_two_step" if with_kdb else "none",
        "kdb_hint": None,
    }


@contextmanager
def _public_environment(
    workload: PublicWorkload,
    *,
    runtime: SourceGraphRuntime | None,
    config: SourceGraphExecutionConfig,
    static_backend: _CountingStaticBackend,
    npi_backend: _FakeNpiBackend | None = None,
):
    old_probe = server._safe_probe_backend
    old_config = server.get_source_graph_execution_config
    old_runtime = server.get_source_graph_runtime
    old_static = connectivity_backend.StaticConnectivityBackend
    old_select = connectivity_backend.select_backend
    old_session = dict(server._session_state)
    counters = {"runtime_get_count": 0}

    def runtime_getter(_config):
        counters["runtime_get_count"] += 1
        if runtime is None:
            raise AssertionError("Source Graph runtime must not be selected")
        return runtime

    server._safe_probe_backend = lambda *args, **kwargs: _fixed_probe(
        npi_backend is not None
    )
    server.get_source_graph_execution_config = lambda: config
    server.get_source_graph_runtime = runtime_getter
    connectivity_backend.StaticConnectivityBackend = lambda: static_backend
    if npi_backend is not None:
        connectivity_backend.select_backend = lambda status, *, fallback=None: (
            npi_backend
        )
    server._handle_store.invalidate()
    server._handle_store.register(
        compute_handle(workload.compile_log, workload.simulator),
        workload.hierarchy,
    )
    server._session_state["build_tb_hierarchy"] = {
        "compile_log": workload.compile_log,
        "simulator": workload.simulator,
    }
    try:
        yield counters
    finally:
        server._safe_probe_backend = old_probe
        server.get_source_graph_execution_config = old_config
        server.get_source_graph_runtime = old_runtime
        connectivity_backend.StaticConnectivityBackend = old_static
        connectivity_backend.select_backend = old_select
        server._handle_store.invalidate()
        server._session_state.clear()
        server._session_state.update(old_session)


def _static_counter(static_factory: StaticFactory) -> _CountingStaticBackend:
    return _CountingStaticBackend(static_factory())


def _payload_backends(payload: Mapping[str, Any], operation: str) -> list[str]:
    values = []
    if payload.get("backend"):
        values.append(str(payload["backend"]))
    field = "driver_chain" if operation == "driver" else "loads"
    items = payload.get(field) or ()
    for item in items:
        if isinstance(item, Mapping) and item.get("backend"):
            values.append(str(item["backend"]))
    return sorted(set(values))


def _compact_result(
    result: Any, operation: str, wall_ms: float, cpu_ms: float
) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    backend_status = payload["backend_status"]
    source_graph = backend_status.get("source_graph")
    compact = {
        "wall_time_ms": round(wall_ms, 6),
        "parent_cpu_time_ms": round(cpu_ms, 6),
        "backend": payload.get("backend"),
        "selected_backend": backend_status.get("selected_backend"),
        "attempted_backend": backend_status.get("attempted_backend"),
        "actual_backend": backend_status.get("actual_backend"),
        "fallback_reason": backend_status.get("fallback_reason"),
        "attempted_backends": backend_status.get("attempted_backends", []),
        "payload_backends": _payload_backends(payload, operation),
        "schema_fingerprint_sha256": _sha256_json(payload),
    }
    if source_graph is not None:
        adapter = source_graph.get("adapter") or {}
        compact["source_graph"] = {
            "adapter_status": source_graph.get("adapter_status"),
            "prepare_status": source_graph.get("prepare_status"),
            "cache_disposition": source_graph.get("cache_disposition"),
            "flight_disposition": source_graph.get("flight_disposition"),
            "coverage_status": source_graph.get("coverage_status"),
            "coverage_files_total": source_graph.get("coverage_files_total", 0),
            "coverage_files_projected": source_graph.get("coverage_files_projected", 0),
            "coverage_diagnostic_count": source_graph.get(
                "coverage_diagnostic_count", 0
            ),
            "coverage_blocking_diagnostic_count": source_graph.get(
                "coverage_blocking_diagnostic_count", 0
            ),
            "coverage_gap_count": source_graph.get("coverage_gap_count", 0),
            "coverage_gap_codes": source_graph.get("coverage_gap_codes", []),
            "objective_exclusions": source_graph.get("objective_exclusions", []),
            "query_status": source_graph.get("query_status"),
            "query_confidence": source_graph.get("query_confidence"),
            "query_match_count": source_graph.get("query_match_count", 0),
            "build_key_sha256": source_graph.get("build_key_sha256"),
            "compile_fingerprint_sha256": source_graph.get(
                "compile_fingerprint_sha256"
            ),
            "ir_fingerprint_sha256": source_graph.get("ir_fingerprint_sha256"),
            "fallback_used": source_graph.get("fallback_used", False),
            "blocker": source_graph.get("blocker"),
            "metrics": source_graph.get("metrics", {}),
            "manifest": adapter.get("manifest", {}),
            "scope": adapter.get("scope", {}),
            "gap_codes": adapter.get("gap_codes", []),
            "cross_request_reusable": adapter.get("cross_request_reusable"),
        }
    return compact


async def _dispatch_timed(operation: str, workload: PublicWorkload):
    tool = "explain_signal_driver" if operation == "driver" else "find_signal_loads"
    args = workload.driver_args() if operation == "driver" else workload.load_args()
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    result = await server._dispatch(tool, args)
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    return result, wall_ms, cpu_ms


async def _measure_public_operation(
    workload: PublicWorkload,
    operation: str,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    cold_repeats: int,
    warm_repeats: int,
) -> dict[str, Any]:
    runs = []
    warm_wall: list[float] = []
    warm_cpu: list[float] = []
    for _ in range(cold_repeats):
        _reset_source_graph_adapter_cache_for_tests()
        runtime = SourceGraphRuntime(runner_factory())
        static = _static_counter(static_factory)
        config = SourceGraphExecutionConfig(
            enabled=True,
            python_bin=str(frontend_python),
            frontend_version=FRONTEND_VERSION,
            timeout_sec=timeout_seconds,
        )
        with _public_environment(
            workload,
            runtime=runtime,
            config=config,
            static_backend=static,
        ) as counters:
            cold_result, wall_ms, cpu_ms = await _dispatch_timed(operation, workload)
            cold = _compact_result(cold_result, operation, wall_ms, cpu_ms)
            warm_results = []
            for _ in range(warm_repeats):
                warm_result, current_wall, current_cpu = await _dispatch_timed(
                    operation, workload
                )
                warm_wall.append(current_wall)
                warm_cpu.append(current_cpu)
                warm_results.append(
                    _compact_result(
                        warm_result,
                        operation,
                        current_wall,
                        current_cpu,
                    )
                )
        runs.append(
            {
                "cold": cold,
                "warm": {
                    "sample_count": len(warm_results),
                    "all_source_graph": all(
                        item["actual_backend"] == "source_graph"
                        for item in warm_results
                    ),
                    "cache_dispositions": dict(
                        Counter(
                            item["source_graph"]["cache_disposition"]
                            for item in warm_results
                        )
                    ),
                    "manifest_cache_dispositions": dict(
                        Counter(
                            item["source_graph"]["manifest"].get(
                                "fingerprint_cache_disposition"
                            )
                            for item in warm_results
                        )
                    ),
                },
                "runtime_stats": runtime.stats_snapshot(),
                "runtime_get_count": counters["runtime_get_count"],
                "static_calls": static.driver_calls + static.load_calls,
            }
        )
    cold_items = [run["cold"] for run in runs]
    source_receipts = [item["source_graph"] for item in cold_items]
    return {
        "cold_runs": runs,
        "aggregate": {
            "cold_wall_time_ms": phase1b._sample_summary(
                [item["wall_time_ms"] for item in cold_items]
            ),
            "cold_parent_cpu_time_ms": phase1b._sample_summary(
                [item["parent_cpu_time_ms"] for item in cold_items]
            ),
            "warm_wall_time_ms": phase1b._sample_summary(warm_wall),
            "warm_parent_cpu_time_ms": phase1b._sample_summary(warm_cpu),
            "worker_cpu_ms": phase1b._sample_summary(
                [
                    receipt["metrics"].get("worker_cpu_ms")
                    for receipt in source_receipts
                    if receipt["metrics"].get("worker_cpu_ms") is not None
                ]
            ),
            "peak_rss_kib": phase1b._sample_summary(
                [
                    receipt["metrics"].get("rss_peak_kib")
                    for receipt in source_receipts
                    if receipt["metrics"].get("rss_peak_kib") is not None
                ]
            ),
            "ir_bytes": phase1b._sample_summary(
                [receipt["metrics"].get("ir_bytes", 0) for receipt in source_receipts]
            ),
            "cache_bytes": phase1b._sample_summary(
                [
                    receipt["metrics"].get("cache_bytes", 0)
                    for receipt in source_receipts
                ]
            ),
            "actual_build_counts": sorted(
                {
                    receipt["metrics"].get("actual_build_count", 0)
                    for receipt in source_receipts
                }
            ),
            "actual_backends": sorted({item["actual_backend"] for item in cold_items}),
            "payload_backend_sets": sorted(
                {tuple(item["payload_backends"]) for item in cold_items}
            ),
            "coverage_statuses": sorted(
                {receipt["coverage_status"] for receipt in source_receipts}
            ),
            "blocking_diagnostic_counts": sorted(
                {
                    receipt["coverage_blocking_diagnostic_count"]
                    for receipt in source_receipts
                }
            ),
            "query_confidences": sorted(
                {receipt["query_confidence"] for receipt in source_receipts}
            ),
            "query_statuses": sorted(
                {receipt["query_status"] for receipt in source_receipts}
            ),
            "build_fingerprints": sorted(
                {receipt["build_key_sha256"] for receipt in source_receipts}
            ),
            "compile_fingerprints": sorted(
                {receipt["compile_fingerprint_sha256"] for receipt in source_receipts}
            ),
            "ir_fingerprints": sorted(
                {receipt["ir_fingerprint_sha256"] for receipt in source_receipts}
            ),
            "manifest_input_counts": sorted(
                {receipt["manifest"].get("input_count") for receipt in source_receipts}
            ),
            "manifest_top_counts": sorted(
                {receipt["manifest"].get("top_count") for receipt in source_receipts}
            ),
            "coverage_boundary_path_counts": sorted(
                {
                    receipt["scope"].get("coverage_boundary_instance_count")
                    for receipt in source_receipts
                }
            ),
            "requested_cone_path_counts": sorted(
                {
                    receipt["scope"].get("requested_cone_instance_count")
                    for receipt in source_receipts
                }
            ),
            "coverage_fact_codes": sorted(
                {
                    code
                    for receipt in source_receipts
                    for code in (
                        receipt["coverage_gap_codes"]
                        + receipt["objective_exclusions"]
                        + receipt["gap_codes"]
                    )
                }
            ),
            "all_static_calls_zero": all(run["static_calls"] == 0 for run in runs),
        },
    }


async def _measure_concurrent_same_key(
    workload: PublicWorkload,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    request_count: int,
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    runtime = SourceGraphRuntime(runner_factory())
    static = _static_counter(static_factory)
    config = SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=timeout_seconds,
    )
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    with _public_environment(
        workload,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        results = await asyncio.gather(
            *(_dispatch_timed("driver", workload) for _ in range(request_count))
        )
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    compact = [
        _compact_result(result, "driver", observed_wall, observed_cpu)
        for result, observed_wall, observed_cpu in results
    ]
    stats = runtime.stats_snapshot()
    return {
        "request_count": request_count,
        "wall_time_ms": round(wall_ms, 6),
        "parent_cpu_time_ms": round(cpu_ms, 6),
        "actual_build_count": stats["actual_build_count"],
        "coalesced_waiter_count": stats["coalesced_waiter_count"],
        "cache_entry_count": stats["cache_entry_count"],
        "actual_backends": dict(Counter(item["actual_backend"] for item in compact)),
        "payload_backend_sets": sorted(
            {tuple(item["payload_backends"]) for item in compact}
        ),
        "flight_dispositions": dict(
            Counter(item["source_graph"]["flight_disposition"] for item in compact)
        ),
        "manifest_cache_dispositions": dict(
            Counter(
                item["source_graph"]["manifest"].get("fingerprint_cache_disposition")
                for item in compact
            )
        ),
        "static_calls": static.driver_calls + static.load_calls,
    }


async def _measure_npi_success_probe(
    workload: PublicWorkload,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    npi = _FakeNpiBackend()
    static = _static_counter(static_factory)
    config = SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=60.0,
    )
    with _public_environment(
        workload,
        runtime=None,
        config=config,
        static_backend=static,
        npi_backend=npi,
    ) as counters:
        driver, driver_wall, driver_cpu = await _dispatch_timed("driver", workload)
        loads, loads_wall, loads_cpu = await _dispatch_timed("loads", workload)
    return {
        "probe_model": (
            "deterministic trusted-result routing probe; validates public NPI "
            "precedence/schema without requiring an NPI license or accuracy claim"
        ),
        "driver": _compact_result(driver, "driver", driver_wall, driver_cpu),
        "loads": _compact_result(loads, "loads", loads_wall, loads_cpu),
        "npi_driver_calls": npi.driver_calls,
        "npi_load_calls": npi.load_calls,
        "source_graph_runtime_get_count": counters["runtime_get_count"],
        "static_calls": static.driver_calls + static.load_calls,
    }


async def _measure_dependency_fallback_probe(
    workload: PublicWorkload,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(
        _FailedRunner(PrepareStatus.DEPENDENCY_BLOCKED, "frontend_unavailable")
    )
    static = _static_counter(static_factory)
    config = SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=60.0,
    )
    with _public_environment(
        workload,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        result, wall_ms, cpu_ms = await _dispatch_timed("driver", workload)
    return {
        "result": _compact_result(result, "driver", wall_ms, cpu_ms),
        "runtime_stats": runtime.stats_snapshot(),
        "static_calls": static.driver_calls,
    }


async def _measure_coverage_boundary_probe(
    workload: PublicWorkload,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Exercise construct exclusions through the complete public driver route."""

    _reset_source_graph_adapter_cache_for_tests()
    runtime = SourceGraphRuntime(runner_factory())
    static = _static_counter(static_factory)
    config = SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=timeout_seconds,
    )
    with _public_environment(
        workload,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        result, wall_ms, cpu_ms = await _dispatch_timed("driver", workload)
    return {
        "probe_model": workload.scope_claim,
        "result": _compact_result(result, "driver", wall_ms, cpu_ms),
        "runtime_stats": runtime.stats_snapshot(),
        "static_calls": static.driver_calls,
    }


async def _measure_failure_case(
    kind: str,
    workload: PublicWorkload,
    runner: SourceGraphWorkerRunner,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    cancellation_delay_seconds: float,
) -> dict[str, Any]:
    _reset_source_graph_adapter_cache_for_tests()
    runtime = SourceGraphRuntime(runner)
    static = _static_counter(static_factory)
    config = SourceGraphExecutionConfig(
        enabled=True,
        python_bin=str(frontend_python),
        frontend_version=FRONTEND_VERSION,
        timeout_sec=timeout_seconds,
    )
    failure: dict[str, Any]
    with _public_environment(
        workload,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        if kind == "cancellation":
            task = asyncio.create_task(_dispatch_timed("driver", workload))
            await asyncio.sleep(cancellation_delay_seconds)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                failure = {
                    "status": "cancelled",
                    "fallback_used": False,
                    "actual_backend": None,
                }
            else:
                raise BenchmarkError("cancelled public request unexpectedly returned")
        else:
            result, wall_ms, cpu_ms = await _dispatch_timed("driver", workload)
            failure = _compact_result(result, "driver", wall_ms, cpu_ms)
        cleanup_deadline = time.monotonic() + 10.0
        while runtime.stats_snapshot()["inflight_count"]:
            if time.monotonic() >= cleanup_deadline:
                raise BenchmarkError(f"{kind} flight cleanup exceeded 10 seconds")
            await asyncio.sleep(0.005)
        stats_after_failure = runtime.stats_snapshot()
        static_calls_after_failure = static.driver_calls
        retry_result, retry_wall, retry_cpu = await _dispatch_timed("driver", workload)
        retry = _compact_result(retry_result, "driver", retry_wall, retry_cpu)
    return {
        "failure": failure,
        "cache_entry_count_after_failure": stats_after_failure["cache_entry_count"],
        "inflight_count_after_failure": stats_after_failure["inflight_count"],
        "static_calls_after_failure": static_calls_after_failure,
        "retry": retry,
        "cache_entry_count_after_retry": runtime.stats_snapshot()["cache_entry_count"],
    }


async def _measure_failure_probes(
    workload: PublicWorkload,
    factories: FailureRunnerFactories,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    cancellation_delay_seconds: float,
) -> dict[str, Any]:
    return {
        kind: await _measure_failure_case(
            kind,
            workload,
            factory(),
            static_factory=static_factory,
            frontend_python=frontend_python,
            timeout_seconds=timeout_seconds,
            cancellation_delay_seconds=cancellation_delay_seconds,
        )
        for kind, factory in (
            ("build_failure", factories.build_failure),
            ("crash", factories.crash),
            ("timeout", factories.timeout),
            ("cancellation", factories.cancellation),
        )
    }


def _git_show(head: str, path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "show", f"{head}:{path}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError(f"cannot read accepted route source: {exc}") from exc
    return result.stdout


def _dispatch_branch_digest(source: str, tool_name: str) -> str:
    tree = ast.parse(source)
    dispatch = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch"
        ),
        None,
    )
    if dispatch is None:
        raise BenchmarkError("server._dispatch AST unavailable")
    for node in ast.walk(dispatch):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comparison = node.test
        if (
            isinstance(comparison.left, ast.Name)
            and comparison.left.id == "name"
            and len(comparison.comparators) == 1
            and isinstance(comparison.comparators[0], ast.Constant)
            and comparison.comparators[0].value == tool_name
        ):
            normalized = ast.dump(
                ast.Module(body=node.body, type_ignores=[]),
                include_attributes=False,
            )
            return hashlib.sha256(normalized.encode()).hexdigest()
    raise BenchmarkError(f"dispatch branch unavailable: {tool_name}")


def _function_digest(source: str, function_name: str) -> str:
    tree = ast.parse(source)
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == function_name
        ),
        None,
    )
    if node is None:
        raise BenchmarkError(f"server function unavailable: {function_name}")
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def _route_isolation_receipt() -> dict[str, Any]:
    accepted = _git_show(ACCEPTED_PHASE1B_HEAD, "server.py")
    current = (ROOT / "server.py").read_text(encoding="utf-8")
    branches = {}
    for tool in (
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
        "trace_x_source",
    ):
        before = _dispatch_branch_digest(accepted, tool)
        after = _dispatch_branch_digest(current, tool)
        branches[tool] = {
            "accepted_ast_sha256": before,
            "current_ast_sha256": after,
            "changed": before != after,
        }
    locking = {}
    for function_name in ("_run_in_wave_thread", "_wave_locks_for"):
        before = _function_digest(accepted, function_name)
        after = _function_digest(current, function_name)
        locking[function_name] = {
            "accepted_ast_sha256": before,
            "current_ast_sha256": after,
            "changed": before != after,
        }
    return {
        "accepted_head": ACCEPTED_PHASE1B_HEAD,
        "dispatch_branches": branches,
        "waveform_locking_functions": locking,
        "driver_load_route_changed_only": (
            branches["explain_signal_driver"]["changed"]
            and branches["find_signal_loads"]["changed"]
            and not branches["trace_signal_path"]["changed"]
            and not branches["trace_x_source"]["changed"]
            and not any(item["changed"] for item in locking.values())
        ),
    }


def _parent_import_receipt() -> dict[str, Any]:
    command = (
        "import sys; import server; "
        "print(int('pyslang' in sys.modules), int('uhdm' in sys.modules))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", command],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError(f"parent import probe failed: {exc}") from exc
    fields = result.stdout.strip().split()
    return {
        "server_import_succeeded": True,
        "pyslang_imported": fields != ["0", "0"] and fields[0] == "1",
        "uhdm_imported": fields != ["0", "0"] and fields[-1] == "1",
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    expected: Any,
    actual: Any,
) -> None:
    checks.append(
        {"name": name, "passed": bool(passed), "expected": expected, "actual": actual}
    )


def _workload_gate(workload: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    name = workload["name"]
    for operation in ("driver", "loads"):
        aggregate = workload["operations"][operation]["aggregate"]
        _check(
            checks,
            f"{operation}_source_graph_selected",
            aggregate["actual_backends"] == ["source_graph"],
            expected=["source_graph"],
            actual=aggregate["actual_backends"],
        )
        _check(
            checks,
            f"{operation}_no_mixed_provenance",
            aggregate["payload_backend_sets"] == [("source_graph",)],
            expected=[("source_graph",)],
            actual=aggregate["payload_backend_sets"],
        )
        _check(
            checks,
            f"{operation}_fresh_build_once",
            aggregate["actual_build_counts"] == [1],
            expected=[1],
            actual=aggregate["actual_build_counts"],
        )
        warm_p95 = aggregate["warm_wall_time_ms"]["p95"]
        _check(
            checks,
            f"{operation}_warm_public_p95",
            warm_p95 is not None
            and warm_p95 <= GATE_TARGETS["warm_public_prepare_query_p95_max_ms"],
            expected=f"<={GATE_TARGETS['warm_public_prepare_query_p95_max_ms']}ms",
            actual=warm_p95,
        )
        _check(
            checks,
            f"{operation}_query_found",
            aggregate["query_statuses"] == ["found"],
            expected=["found"],
            actual=aggregate["query_statuses"],
        )
        _check(
            checks,
            f"{operation}_legacy_static_unused",
            aggregate["all_static_calls_zero"],
            expected=True,
            actual=aggregate["all_static_calls_zero"],
        )
    concurrent = workload["concurrent_same_key"]
    _check(
        checks,
        "same_key_public_actual_build_count",
        concurrent["actual_build_count"] == 1,
        expected=1,
        actual=concurrent["actual_build_count"],
    )
    _check(
        checks,
        "same_key_public_all_source_graph",
        concurrent["actual_backends"] == {"source_graph": concurrent["request_count"]},
        expected={"source_graph": concurrent["request_count"]},
        actual=concurrent["actual_backends"],
    )
    if name == "opentitan_core":
        for operation in ("driver", "loads"):
            aggregate = workload["operations"][operation]["aggregate"]
            cold_p50 = aggregate["cold_wall_time_ms"]["p50"]
            peak = aggregate["peak_rss_kib"]["max"]
            _check(
                checks,
                f"opentitan_{operation}_cold_public_p50",
                cold_p50 is not None
                and cold_p50 <= GATE_TARGETS["opentitan_public_cold_p50_max_ms"],
                expected=f"<={GATE_TARGETS['opentitan_public_cold_p50_max_ms']}ms",
                actual=cold_p50,
            )
            _check(
                checks,
                f"opentitan_{operation}_peak_rss",
                peak is not None and peak <= GATE_TARGETS["peak_rss_max_kib"],
                expected=f"<={GATE_TARGETS['peak_rss_max_kib']}KiB",
                actual=peak,
            )
            _check(
                checks,
                f"opentitan_{operation}_coverage_inconclusive",
                aggregate["coverage_statuses"] == ["inconclusive"],
                expected=["inconclusive"],
                actual=aggregate["coverage_statuses"],
            )
            _check(
                checks,
                f"opentitan_{operation}_blocking_diagnostics",
                aggregate["blocking_diagnostic_counts"] == [65],
                expected=[65],
                actual=aggregate["blocking_diagnostic_counts"],
            )
            _check(
                checks,
                f"opentitan_{operation}_query_partial",
                aggregate["query_confidences"] == ["partial"],
                expected=["partial"],
                actual=aggregate["query_confidences"],
            )
            _check(
                checks,
                f"opentitan_{operation}_production_manifest_shape",
                aggregate["manifest_input_counts"] == [785]
                and aggregate["manifest_top_counts"] == [11],
                expected={"inputs": [785], "tops": [11]},
                actual={
                    "inputs": aggregate["manifest_input_counts"],
                    "tops": aggregate["manifest_top_counts"],
                },
            )
            _check(
                checks,
                f"opentitan_{operation}_target_specific_scope",
                aggregate["coverage_boundary_path_counts"] == [4]
                and aggregate["requested_cone_path_counts"] == [1],
                expected={"boundary": [4], "cone": [1]},
                actual={
                    "boundary": aggregate["coverage_boundary_path_counts"],
                    "cone": aggregate["requested_cone_path_counts"],
                },
            )
            actual_boundary = set(aggregate["coverage_fact_codes"])
            _check(
                checks,
                f"opentitan_{operation}_boundary_facts_explicit",
                REQUIRED_OPENTITAN_OBSERVED_BOUNDARY_FACTS <= actual_boundary,
                expected=sorted(REQUIRED_OPENTITAN_OBSERVED_BOUNDARY_FACTS),
                actual=sorted(actual_boundary),
            )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _failure_gate(probes: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for kind, expected in (
        ("build_failure", "build_failed"),
        ("crash", "worker_crash"),
        ("timeout", "timed_out"),
    ):
        probe = probes[kind]
        receipt = probe["failure"]["source_graph"]
        _check(
            checks,
            f"{kind}_structured_status",
            receipt["prepare_status"] == expected,
            expected=expected,
            actual=receipt["prepare_status"],
        )
        _check(
            checks,
            f"{kind}_legacy_static_fallback",
            probe["failure"]["actual_backend"] == "static"
            and receipt["fallback_used"] is True,
            expected={"actual_backend": "static", "fallback_used": True},
            actual={
                "actual_backend": probe["failure"]["actual_backend"],
                "fallback_used": receipt["fallback_used"],
            },
        )
    cancelled = probes["cancellation"]
    _check(
        checks,
        "cancellation_stops_without_fallback",
        cancelled["failure"]
        == {
            "status": "cancelled",
            "fallback_used": False,
            "actual_backend": None,
        }
        and cancelled["static_calls_after_failure"] == 0,
        expected={"cancelled": True, "fallback_used": False, "static_calls": 0},
        actual={
            "failure": cancelled["failure"],
            "static_calls": cancelled["static_calls_after_failure"],
        },
    )
    for kind, probe in probes.items():
        _check(
            checks,
            f"{kind}_failure_does_not_pollute_cache",
            probe["cache_entry_count_after_failure"] == 0
            and probe["inflight_count_after_failure"] == 0,
            expected={"cache": 0, "inflight": 0},
            actual={
                "cache": probe["cache_entry_count_after_failure"],
                "inflight": probe["inflight_count_after_failure"],
            },
        )
        _check(
            checks,
            f"{kind}_safe_public_retry",
            probe["retry"]["actual_backend"] == "source_graph"
            and probe["cache_entry_count_after_retry"] == 1,
            expected={"actual_backend": "source_graph", "cache": 1},
            actual={
                "actual_backend": probe["retry"]["actual_backend"],
                "cache": probe["cache_entry_count_after_retry"],
            },
        )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


async def run_benchmark_async(
    args: argparse.Namespace,
    *,
    runner_factory: RunnerFactory | None = None,
    failure_factories: FailureRunnerFactories | None = None,
    static_factory: StaticFactory | None = None,
) -> dict[str, Any]:
    if args.cold_repeats < 1 or args.warm_repeats < 1:
        raise BenchmarkError("cold/warm repeat counts must be positive")
    if args.concurrent_requests < 2:
        raise BenchmarkError("concurrent request count must be at least two")
    if (
        min(
            args.worker_timeout_seconds,
            args.failure_timeout_seconds,
            args.cancellation_delay_seconds,
        )
        <= 0
    ):
        raise BenchmarkError("timeout/cancellation values must be positive")
    baselines = _read_historical_baselines(args.phase1a_evidence, args.phase1b_evidence)
    process_factory: RunnerFactory = runner_factory or (
        lambda: IsolatedSourceGraphProcessRunner(
            python_executable=args.frontend_python,
            working_directory=ROOT,
        )
    )
    original_static_class = connectivity_backend.StaticConnectivityBackend
    selected_static_factory: StaticFactory = static_factory or original_static_class
    if failure_factories is None:
        missing_worker = ROOT / "scripts/__missing_source_graph_phase2_worker__.py"
        if missing_worker.exists():
            raise BenchmarkError("reserved missing-worker path unexpectedly exists")
        failure_factories = FailureRunnerFactories(
            build_failure=lambda: _FirstThenRunner(
                _FailedRunner(PrepareStatus.BUILD_FAILED, "frontend_build_failed"),
                process_factory(),
            ),
            crash=lambda: _FirstThenRunner(
                IsolatedSourceGraphProcessRunner(
                    python_executable=args.frontend_python,
                    worker_script=missing_worker,
                    working_directory=ROOT,
                ),
                process_factory(),
            ),
            timeout=lambda: _FirstThenRunner(
                _TimeoutOverrideRunner(process_factory(), args.failure_timeout_seconds),
                process_factory(),
            ),
            cancellation=lambda: _FirstThenRunner(process_factory(), process_factory()),
        )

    with tempfile.TemporaryDirectory(prefix="traceweave-phase2-benchmark-") as temp:
        temp_root = Path(temp)
        hand = _hand_workload(temp_root)
        coverage_boundary = _coverage_boundary_workload(temp_root)
        selected = args.workload or ["hand_fixture"]
        workload_map = {"hand_fixture": hand}
        if "opentitan_core" in selected:
            workload_map["opentitan_core"] = _opentitan_workload(
                args.opentitan_compile_log
            )

        npi_probe = await _measure_npi_success_probe(
            hand,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
        )
        dependency_fallback = await _measure_dependency_fallback_probe(
            hand,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
        )
        coverage_boundary_probe = await _measure_coverage_boundary_probe(
            coverage_boundary,
            runner_factory=process_factory,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
            timeout_seconds=args.worker_timeout_seconds,
        )
        workloads = []
        for name in selected:
            workload = workload_map[name]
            operations = {
                operation: await _measure_public_operation(
                    workload,
                    operation,
                    runner_factory=process_factory,
                    static_factory=selected_static_factory,
                    frontend_python=args.frontend_python,
                    timeout_seconds=args.worker_timeout_seconds,
                    cold_repeats=args.cold_repeats,
                    warm_repeats=args.warm_repeats,
                )
                for operation in ("driver", "loads")
            }
            concurrent = await _measure_concurrent_same_key(
                workload,
                runner_factory=process_factory,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
                timeout_seconds=args.worker_timeout_seconds,
                request_count=args.concurrent_requests,
            )
            item = {
                "name": name,
                "scope_claim": workload.scope_claim,
                "operations": operations,
                "concurrent_same_key": concurrent,
            }
            item["gate"] = _workload_gate(item)
            workloads.append(item)

        failures = await _measure_failure_probes(
            hand,
            failure_factories,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
            timeout_seconds=args.worker_timeout_seconds,
            cancellation_delay_seconds=args.cancellation_delay_seconds,
        )
        failure_gate = _failure_gate(failures)

    route_isolation = _route_isolation_receipt()
    parent_import = _parent_import_receipt()
    global_checks: list[dict[str, Any]] = []
    npi_results = (npi_probe["driver"], npi_probe["loads"])
    _check(
        global_checks,
        "npi_success_precedes_source_graph_and_static",
        all(item["actual_backend"] == "verdi_npi" for item in npi_results)
        and npi_probe["source_graph_runtime_get_count"] == 0
        and npi_probe["static_calls"] == 0,
        expected={"backend": "verdi_npi", "source_graph": 0, "static": 0},
        actual={
            "backends": [item["actual_backend"] for item in npi_results],
            "source_graph": npi_probe["source_graph_runtime_get_count"],
            "static": npi_probe["static_calls"],
        },
    )
    fallback_result = dependency_fallback["result"]
    _check(
        global_checks,
        "dependency_blocker_routes_to_legacy_static",
        fallback_result["actual_backend"] == "static"
        and fallback_result["source_graph"]["prepare_status"] == "dependency_blocked"
        and fallback_result["source_graph"]["fallback_used"] is True,
        expected={"prepare": "dependency_blocked", "actual": "static"},
        actual={
            "prepare": fallback_result["source_graph"]["prepare_status"],
            "actual": fallback_result["actual_backend"],
        },
    )
    _check(
        global_checks,
        "driver_load_only_route_change",
        route_isolation["driver_load_route_changed_only"],
        expected=True,
        actual=route_isolation["driver_load_route_changed_only"],
    )
    boundary_result = coverage_boundary_probe["result"]
    boundary_receipt = boundary_result["source_graph"]
    boundary_exclusions = set(boundary_receipt["objective_exclusions"])
    _check(
        global_checks,
        "construct_exclusions_preserved_by_public_receipt",
        boundary_result["actual_backend"] == "source_graph"
        and boundary_result["payload_backends"] == ["source_graph"]
        and boundary_receipt["coverage_status"] == "inconclusive"
        and boundary_receipt["query_confidence"] == "partial"
        and REQUIRED_PRODUCTION_MARKER_EXCLUSIONS <= boundary_exclusions
        and coverage_boundary_probe["static_calls"] == 0,
        expected={
            "actual_backend": "source_graph",
            "coverage_status": "inconclusive",
            "query_confidence": "partial",
            "objective_exclusions": sorted(REQUIRED_PRODUCTION_MARKER_EXCLUSIONS),
            "static_calls": 0,
        },
        actual={
            "actual_backend": boundary_result["actual_backend"],
            "payload_backends": boundary_result["payload_backends"],
            "coverage_status": boundary_receipt["coverage_status"],
            "query_confidence": boundary_receipt["query_confidence"],
            "objective_exclusions": sorted(boundary_exclusions),
            "static_calls": coverage_boundary_probe["static_calls"],
        },
    )
    _check(
        global_checks,
        "parent_import_has_no_optional_frontend",
        parent_import["server_import_succeeded"]
        and not parent_import["pyslang_imported"]
        and not parent_import["uhdm_imported"],
        expected={"server": True, "pyslang": False, "uhdm": False},
        actual=parent_import,
    )
    workload_gates_passed = bool(workloads) and all(
        item["gate"]["passed"] for item in workloads
    )
    passed = (
        workload_gates_passed
        and failure_gate["passed"]
        and all(item["passed"] for item in global_checks)
    )
    script_path = Path(__file__).resolve()
    decision = (
        "phase2_public_driver_load_gate_passed"
        if passed
        else "phase2_no_go_keep_auditing_public_driver_load_route"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {"root": str(ROOT), "head": phase1b._git_head(ROOT)},
        "benchmark_script": {
            "path": str(script_path.relative_to(ROOT)),
            "sha256": phase1b._sha256_file(script_path),
        },
        "before_baselines": baselines,
        "frontend": {
            "name": SLANG_FRONTEND_NAME,
            "required_version": FRONTEND_VERSION,
            "interpreter": str(args.frontend_python),
            "parent_import_requires_pyslang": False,
            "dependency_model": "optional pinned isolated one-shot worker",
        },
        "runtime_model": {
            "production_route": (
                "trusted Verdi NPI -> bounded on-demand Source Graph -> Legacy Static"
            ),
            "process_shared_lazy_runtime": True,
            "compile_session_manifest_cache": "bounded process memory",
            "ir_cache": "process-session memory only",
            "disk_cache": False,
            "persistent_worker": False,
            "startup_build": False,
            "same_key_single_flight": True,
            "max_concurrent_cold_builds_per_process": 1,
        },
        "measurement_policy": {
            "cold": "fresh manifest cache, runtime, and isolated worker per repeat",
            "warm": "whole public dispatch with compile-session and exact IR memory hits",
            "concurrent": "simultaneous identical public driver requests",
            "npi_probe": npi_probe["probe_model"],
            "failure": (
                "public crash/timeout fallback and cancellation no-fallback, then "
                "same-runtime retry"
            ),
            "coverage_boundary_probe": coverage_boundary_probe["probe_model"],
            "opentitan_scope": (
                "four ancestor paths / one target cone path only; not full-design "
                "accuracy, coverage, or speedup; protected-source exclusion is "
                "proved separately because this manifest has no detected protected "
                "input marker"
            ),
        },
        "gate_targets": GATE_TARGETS,
        "route_probes": {
            "npi_success": npi_probe,
            "source_graph_dependency_to_legacy_static": dependency_fallback,
            "construct_coverage_boundary": coverage_boundary_probe,
            "scope_isolation": route_isolation,
            "parent_import": parent_import,
            "gate": {
                "passed": all(item["passed"] for item in global_checks),
                "checks": global_checks,
            },
        },
        "workloads": workloads,
        "failure_probes": {
            "workload": "hand_fixture",
            "results": failures,
            "gate": failure_gate,
        },
        "assessment": {
            "decision": decision,
            "phase2_public_driver_load_gate_passed": passed,
            "workload_gates_passed": workload_gates_passed,
            "failure_gate_passed": failure_gate["passed"],
            "route_gate_passed": all(item["passed"] for item in global_checks),
            "production_route_changed": True,
            "production_route_changed_tools": [
                "explain_signal_driver",
                "find_signal_loads",
            ],
            "trace_signal_path_route_changed": False,
            "trace_x_source_route_changed": False,
            "waveform_locking_model_changed": False,
            "public_production_integration_performed": True,
            "coverage_claim": "target_scoped_only_partial_or_inconclusive_preserved",
            "next_step": (
                "stop after Phase 2; do not enter path, X-trace, disk-cache, or "
                "persistent-worker scope"
            ),
        },
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_benchmark_async(args))


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        result = run_benchmark(args)
    except (BenchmarkError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _write_json_atomic(args.output, result)
    return 0 if result["assessment"]["phase2_public_driver_load_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
