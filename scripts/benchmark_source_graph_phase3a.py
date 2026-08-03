#!/usr/bin/env python3
"""Benchmark Phase 3A public Source Graph signal-path production routing.

The tracked hand fixture is the correctness oracle. OpenTitan is opt-in and
uses one pre-resolved instance plus two signals from a single continuous
assignment; it never enumerates the full design or claims full-design accuracy.
The optional pyslang frontend remains isolated in a one-shot worker process.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
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
from scripts import benchmark_source_graph_phase2 as phase2  # noqa: E402
import src.connectivity_backend as connectivity_backend  # noqa: E402
from src.compile_log_parser import parse_compile_log  # noqa: E402
from src.connectivity_ir import (  # noqa: E402
    CoverageGap,
    CoverageReport,
    CoverageStatus,
)
from src.hierarchy_handles import compute_handle  # noqa: E402
from src.slang_connectivity_projector import SLANG_FRONTEND_NAME  # noqa: E402
from src.source_graph_adapter import (  # noqa: E402
    _reset_source_graph_adapter_cache_for_tests,
)
from src.source_graph_contract import SourceGraphScopeReceipt  # noqa: E402
from src.source_graph_runtime import (  # noqa: E402
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    SourceGraphRuntime,
    SourceGraphWorkerRunner,
    WorkerBuildResult,
    WorkerResourceMetrics,
)
from tests.connectivity_ir_fixtures import build_hand_ir  # noqa: E402


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase3a"
FRONTEND_VERSION = "11.0.0"
ACCEPTED_PHASE2_HEAD = "102f5a6339b399138c795e2cb917a0ef462b3c8d"
MEASUREMENT_HEAD = "1a9fac908caa865821d46b60130edf1edf3abde1"
DEFAULT_FRONTEND_PYTHON = Path("/tmp/traceweave-phase0b-pyslang-11.0.0/bin/python")
DEFAULT_PHASE2_EVIDENCE = (
    ROOT / "benchmarks/source_graph_connectivity_phase2_results.json"
)
PHASE1A_EVIDENCE_SHA256 = (
    "c7310560a1e89e19694a83d41e24a645578b747585c68df546a20937f3fa42e2"
)
PHASE1B_EVIDENCE_SHA256 = (
    "c9a25c96c63ddce9205ecabf86b61f6da1eff9ba0f71aeee099a6b04f7237da7"
)
PHASE2_EVIDENCE_SHA256 = (
    "1b5f76c3862601bb1163d838744c9c03ec7ed62cd1bc5f663ef7008bd0902599"
)
DEFAULT_OPENTITAN_COMPILE_LOG = Path(
    "/tmp/traceweave-phase0b-opentitan-dvsim/phase0b-cold/"
    "chip_earlgrey_asic-sim-xcelium/default/fusesoc-work/xrun.log"
)
GATE_TARGETS = {
    "opentitan_scoped_cold_p50_max_ms": 15_000.0,
    "warm_exact_path_public_p95_max_ms": 100.0,
    "peak_rss_max_kib": 2_621_440,
    "same_key_actual_build_count": 1,
}


BenchmarkError = phase1b.BenchmarkError
RunnerFactory = Callable[[], SourceGraphWorkerRunner]
StaticFactory = Callable[[], Any]


class FailureFactory(Protocol):
    def __call__(self) -> SourceGraphWorkerRunner: ...


@dataclass(frozen=True)
class FailureRunnerFactories:
    dependency: FailureFactory
    build_failure: FailureFactory
    crash: FailureFactory
    timeout: FailureFactory
    cancellation: FailureFactory


@dataclass(frozen=True)
class PathWorkload:
    name: str
    compile_log: str
    simulator: str
    compile_result: dict[str, Any]
    hierarchy: dict[str, Any]
    top: str
    from_signal: str
    to_signal: str
    expected_path: tuple[str, ...]
    expand_assigns: bool
    scope_claim: str

    def path_args(self, **overrides: Any) -> dict[str, Any]:
        result = {
            "from_signal": self.from_signal,
            "to_signal": self.to_signal,
            "compile_log": self.compile_log,
            "simulator": self.simulator,
            "top_hint": self.top,
            "expand_assigns": self.expand_assigns,
        }
        result.update(overrides)
        return result


class FixtureReadyRunner:
    """Tracked-IR fake worker used only by automated tests and fact probes."""

    def __init__(self, ir=None, *, delay_seconds: float = 0.0) -> None:
        self.ir = ir or replace(
            build_hand_ir(),
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version=FRONTEND_VERSION,
        )
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        del timeout_seconds
        self.calls += 1
        if self.delay_seconds:
            deadline = time.monotonic() + self.delay_seconds
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    return WorkerBuildResult.failed(
                        PrepareStatus.CANCELLED,
                        code="request_cancelled",
                        stage="fixture_worker",
                    )
                await asyncio.sleep(0.001)
        if cancel_event.is_set():
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="fixture_worker",
            )
        gaps = tuple(gap.code for gap in self.ir.coverage.gaps)
        return WorkerBuildResult.ready(
            self.ir,
            SourceGraphScopeReceipt(
                scope=request.scope,
                coverage_status=self.ir.coverage.status,
                gap_codes=gaps,
            ),
            metrics=WorkerResourceMetrics(
                wall_time_ms=2.0,
                cpu_time_ms=1.0,
                rss_start_kib=100,
                rss_peak_kib=160,
                rss_end_kib=120,
                ir_bytes=len(self.ir.to_json_bytes()),
            ),
        )


class _FailedRunner:
    def __init__(self, status: PrepareStatus, code: str) -> None:
        self.status = status
        self.code = code

    async def run(self, request, *, timeout_seconds, cancel_event):
        del request, timeout_seconds, cancel_event
        return WorkerBuildResult.failed(
            self.status,
            code=self.code,
            stage="worker_process",
        )


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


class _FakeNpiPathBackend:
    name = "verdi_npi"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.path_calls = 0

    def find_path(self, **kwargs):
        self.path_calls += 1
        if not self.connected:
            return {
                "from_signal": kwargs["from_signal"],
                "to_signal": kwargs["to_signal"],
                "found": False,
                "hops": 0,
                "path": [],
                "expand_assigns": kwargs["expand_assigns"],
                "unsupported_reason": "not_connected",
                "backend": "verdi_npi",
            }
        return {
            "from_signal": kwargs["from_signal"],
            "to_signal": kwargs["to_signal"],
            "found": True,
            "hops": 1,
            "path": [
                {
                    "index": 0,
                    "net_path": kwargs["from_signal"],
                    "scope_inst": "sg_top.u_producer",
                    "source_file": "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
                    "source_line": 84,
                    "is_endpoint": True,
                    "source_info_origin": "npi",
                    "backend": "verdi_npi",
                },
                {
                    "index": 1,
                    "net_path": kwargs["to_signal"],
                    "scope_inst": "sg_top.bus",
                    "source_file": "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
                    "source_line": 98,
                    "is_endpoint": True,
                    "source_info_origin": "npi",
                    "backend": "verdi_npi",
                },
            ],
            "expand_assigns": kwargs["expand_assigns"],
            "unsupported_reason": None,
            "backend": "verdi_npi",
        }


class _CountingStaticBackend:
    name = "static"
    uses_external_worker = False

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.driver_calls = 0
        self.load_calls = 0
        self.path_calls = 0

    def find_driver(self, **kwargs):
        self.driver_calls += 1
        return self.delegate.find_driver(**kwargs)

    def find_loads(self, **kwargs):
        self.load_calls += 1
        return self.delegate.find_loads(**kwargs)

    def find_path(self, **kwargs):
        self.path_calls += 1
        return self.delegate.find_path(**kwargs)

    @property
    def total_calls(self) -> int:
        return self.driver_calls + self.load_calls + self.path_calls


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
    parser.add_argument("--warm-repeats", type=int, default=50)
    parser.add_argument("--concurrent-requests", type=int, default=4)
    parser.add_argument("--worker-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--failure-timeout-seconds", type=float, default=0.001)
    parser.add_argument("--cancellation-delay-seconds", type=float, default=0.01)
    parser.add_argument("--phase2-evidence", type=Path, default=DEFAULT_PHASE2_EVIDENCE)
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


def _read_phase2_baseline(path: Path) -> dict[str, Any]:
    digest = phase1b._sha256_file(path)
    if digest != PHASE2_EVIDENCE_SHA256:
        raise BenchmarkError(
            "Phase 2 evidence hash mismatch; stop without regeneration"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("benchmark") != "source_graph_connectivity_phase2"
        or payload.get("repository", {}).get("head")
        != "8b40dc2b7b597f152fe5d0a152d3afe71c8ff05e"
    ):
        raise BenchmarkError("Phase 2 schema, benchmark, or measurement HEAD mismatch")
    assessment = payload.get("assessment", {})
    required_assessment = {
        "decision": "phase2_public_driver_load_gate_passed",
        "phase2_public_driver_load_gate_passed": True,
        "production_route_changed_tools": [
            "explain_signal_driver",
            "find_signal_loads",
        ],
        "trace_signal_path_route_changed": False,
        "trace_x_source_route_changed": False,
        "waveform_locking_model_changed": False,
    }
    if any(assessment.get(key) != value for key, value in required_assessment.items()):
        raise BenchmarkError("Phase 2 assessment mismatch")
    runtime = payload.get("runtime_model", {})
    if any(
        runtime.get(key) is not False
        for key in ("disk_cache", "persistent_worker", "startup_build")
    ):
        raise BenchmarkError("Phase 2 runtime model mismatch")
    if payload.get("route_probes", {}).get("gate", {}).get("passed") is not True:
        raise BenchmarkError("Phase 2 route gate mismatch")
    if payload.get("failure_probes", {}).get("gate", {}).get("passed") is not True:
        raise BenchmarkError("Phase 2 failure gate mismatch")
    workloads = {item["name"]: item for item in payload.get("workloads", ())}
    opentitan = workloads.get("opentitan_core")
    if opentitan is None or opentitan.get("gate", {}).get("passed") is not True:
        raise BenchmarkError("Phase 2 OpenTitan workload mismatch")
    for operation in ("driver", "loads"):
        aggregate = opentitan["operations"][operation]["aggregate"]
        expected = {
            "manifest_input_counts": [785],
            "manifest_top_counts": [11],
            "coverage_boundary_path_counts": [4],
            "requested_cone_path_counts": [1],
            "coverage_statuses": ["inconclusive"],
            "blocking_diagnostic_counts": [65],
            "query_confidences": ["partial"],
        }
        if any(aggregate.get(key) != value for key, value in expected.items()):
            raise BenchmarkError(f"Phase 2 OpenTitan {operation} receipt mismatch")
    concurrent = opentitan["concurrent_same_key"]
    if concurrent.get("actual_build_count") != 1:
        raise BenchmarkError("Phase 2 single-flight mismatch")
    for probe in payload["failure_probes"]["results"].values():
        if (
            probe.get("cache_entry_count_after_failure") != 0
            or probe.get("inflight_count_after_failure") != 0
            or probe.get("retry", {}).get("actual_backend") != "source_graph"
        ):
            raise BenchmarkError("Phase 2 failure cleanup/retry mismatch")
    before = payload.get("before_baselines", {})
    if (
        before.get("phase1a", {}).get("sha256") != PHASE1A_EVIDENCE_SHA256
        or before.get("phase1b", {}).get("sha256") != PHASE1B_EVIDENCE_SHA256
    ):
        raise BenchmarkError("Phase 2 historical baseline chain mismatch")
    return {
        "sha256": digest,
        "measurement_head": payload["repository"]["head"],
        "decision": assessment["decision"],
        "production_route_changed_tools": assessment["production_route_changed_tools"],
        "trace_signal_path_route_changed": False,
        "trace_x_source_route_changed": False,
        "waveform_locking_model_changed": False,
        "opentitan_reference": {
            "ordered_input_count": 785,
            "ordered_top_count": 11,
            "coverage_boundary_path_count": 4,
            "requested_cone_path_count": 1,
            "coverage_status": "inconclusive",
            "blocking_diagnostic_count": 65,
            "representative_confidence": "partial",
        },
    }


def _hand_workload(temp_root: Path) -> PathWorkload:
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
            "user": [{"path": str(source), "type": "module", "category": "rtl"}],
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
                "bus": {"module": "sg_bus_if", "children": {}},
                "u_producer": {"module": "sg_producer", "children": {}},
            }
        },
    }
    return PathWorkload(
        name="hand_fixture",
        compile_log=str(compile_log),
        simulator="xcelium",
        compile_result=compile_result,
        hierarchy=hierarchy,
        top="sg_top",
        from_signal="sg_top.u_producer.seed[7:0]",
        to_signal="sg_top.bus.data[15:8]",
        expected_path=(
            "sg_top.u_producer.seed[7:0]",
            "sg_top.u_producer.bus.data[15:8]",
            "sg_top.bus.data[15:8]",
        ),
        expand_assigns=True,
        scope_claim=(
            "tracked hand fixture and exact endpoint oracle; bounded ancestor-union "
            "scope, not a production full-design accuracy claim"
        ),
    )


def _opentitan_workload(compile_log: Path) -> PathWorkload:
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
    prefix = "tb.dut.top_earlgrey.u_rv_core_ibex"
    return PathWorkload(
        name="opentitan_core",
        compile_log=str(compile_log),
        simulator="xcelium",
        compile_result=compile_result,
        hierarchy=hierarchy,
        top="tb",
        from_signal=f"{prefix}.ibus_intg_err",
        to_signal=f"{prefix}.fatal_intg_event",
        expected_path=(
            f"{prefix}.ibus_intg_err[0]",
            f"{prefix}.fatal_intg_event[0]",
        ),
        expand_assigns=True,
        scope_claim=(
            "bounded representative path from the tracked compile session: one "
            "continuous assignment inside a pre-resolved rv_core_ibex instance; "
            "target-scoped only, not full-design path accuracy, coverage, or speedup"
        ),
    )


def _fixed_probe(with_kdb: bool) -> dict[str, Any]:
    return {
        "simulator": "xcelium",
        "backend": "verdi_npi" if with_kdb else "static",
        "parser_match": "exact" if with_kdb else "approximate",
        "kdb_path": "/private/phase3a_probe/kdb.elab++" if with_kdb else None,
        "kdb_flow": "vcs_two_step" if with_kdb else "none",
        "kdb_hint": None,
    }


@contextmanager
def _public_environment(
    workload: PathWorkload,
    *,
    runtime: SourceGraphRuntime | None,
    config: SourceGraphExecutionConfig,
    static_backend: _CountingStaticBackend,
    npi_backend: _FakeNpiPathBackend | None = None,
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


def _static_counter(factory: StaticFactory) -> _CountingStaticBackend:
    return _CountingStaticBackend(factory())


def _source_graph_receipt(source: Mapping[str, Any]) -> dict[str, Any]:
    adapter = source.get("adapter") or {}
    return {
        "adapter_status": source.get("adapter_status"),
        "prepare_status": source.get("prepare_status"),
        "cache_disposition": source.get("cache_disposition"),
        "flight_disposition": source.get("flight_disposition"),
        "coverage_status": source.get("coverage_status"),
        "coverage_files_total": source.get("coverage_files_total", 0),
        "coverage_files_projected": source.get("coverage_files_projected", 0),
        "coverage_diagnostic_count": source.get("coverage_diagnostic_count", 0),
        "coverage_blocking_diagnostic_count": source.get(
            "coverage_blocking_diagnostic_count", 0
        ),
        "coverage_gap_count": source.get("coverage_gap_count", 0),
        "coverage_gap_codes": source.get("coverage_gap_codes", []),
        "objective_exclusions": source.get("objective_exclusions", []),
        "query_status": source.get("query_status"),
        "query_confidence": source.get("query_confidence"),
        "query_match_count": source.get("query_match_count", 0),
        "path_edge_count": source.get("path_edge_count", 0),
        "traversed_edge_count": source.get("traversed_edge_count", 0),
        "visited_state_count": source.get("visited_state_count", 0),
        "traversal_limit": source.get("traversal_limit"),
        "output_limit": source.get("output_limit"),
        "traversal_truncated": source.get("traversal_truncated", False),
        "output_truncated": source.get("output_truncated", False),
        "endpoint_alias_equivalent": source.get("endpoint_alias_equivalent", False),
        "expand_assigns": source.get("expand_assigns"),
        "build_key_sha256": source.get("build_key_sha256"),
        "compile_fingerprint_sha256": source.get("compile_fingerprint_sha256"),
        "ir_fingerprint_sha256": source.get("ir_fingerprint_sha256"),
        "fallback_used": source.get("fallback_used", False),
        "blocker": source.get("blocker"),
        "metrics": source.get("metrics", {}),
        "manifest": adapter.get("manifest", {}),
        "scope": adapter.get("scope", {}),
        "gap_codes": adapter.get("gap_codes", []),
        "cross_request_reusable": adapter.get("cross_request_reusable"),
    }


def _compact_path_result(result: Any, wall_ms: float, cpu_ms: float) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    status = payload["backend_status"]
    source = status.get("source_graph")
    payload_backends = {payload.get("backend")}
    payload_backends.update(
        hop.get("backend")
        for hop in payload.get("path", ())
        if isinstance(hop, Mapping) and hop.get("backend")
    )
    payload_backends.discard(None)
    compact = {
        "wall_time_ms": round(wall_ms, 6),
        "parent_cpu_time_ms": round(cpu_ms, 6),
        "from_signal": payload["from_signal"],
        "to_signal": payload["to_signal"],
        "endpoint_pair_fingerprint_sha256": _sha256_json(
            {
                "from_signal": payload["from_signal"],
                "to_signal": payload["to_signal"],
                "expand_assigns": payload["expand_assigns"],
            }
        ),
        "found": payload["found"],
        "hops": payload["hops"],
        "path": payload.get("path", []),
        "path_net_paths": [hop["net_path"] for hop in payload.get("path", ())],
        "expand_assigns": payload["expand_assigns"],
        "direction_note": payload["direction_note"],
        "unsupported_reason": payload.get("unsupported_reason"),
        "backend": payload.get("backend"),
        "selected_backend": status.get("selected_backend"),
        "attempted_backend": status.get("attempted_backend"),
        "actual_backend": status.get("actual_backend"),
        "fallback_reason": status.get("fallback_reason"),
        "attempted_backends": status.get("attempted_backends", []),
        "payload_backends": sorted(payload_backends),
        "schema_fingerprint_sha256": _sha256_json(payload),
    }
    if source is not None:
        compact["source_graph"] = _source_graph_receipt(source)
    return compact


async def _dispatch_path_timed(workload: PathWorkload, **overrides: Any):
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    result = await server._dispatch(
        "trace_signal_path", workload.path_args(**overrides)
    )
    wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    return result, wall_ms, cpu_ms


def _summary(values: Sequence[int | float]) -> dict[str, Any]:
    return phase1b._sample_summary(list(values))


async def _measure_workload(
    workload: PathWorkload,
    *,
    runner_factory: RunnerFactory,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    cold_repeats: int,
    warm_repeats: int,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
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
            result, wall_ms, cpu_ms = await _dispatch_path_timed(workload)
            cold = _compact_path_result(result, wall_ms, cpu_ms)
            warm_results = []
            for _ in range(warm_repeats):
                current, current_wall, current_cpu = await _dispatch_path_timed(
                    workload
                )
                warm_wall.append(current_wall)
                warm_cpu.append(current_cpu)
                warm_results.append(
                    _compact_path_result(current, current_wall, current_cpu)
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
                    "all_found": all(item["found"] for item in warm_results),
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
                "static_calls": static.total_calls,
            }
        )
    cold_items = [run["cold"] for run in runs]
    receipts = [item["source_graph"] for item in cold_items]
    aggregate = {
        "cold_wall_time_ms": _summary([item["wall_time_ms"] for item in cold_items]),
        "cold_parent_cpu_time_ms": _summary(
            [item["parent_cpu_time_ms"] for item in cold_items]
        ),
        "warm_wall_time_ms": _summary(warm_wall),
        "warm_parent_cpu_time_ms": _summary(warm_cpu),
        "worker_cpu_ms": _summary(
            [
                receipt["metrics"]["worker_cpu_ms"]
                for receipt in receipts
                if receipt["metrics"].get("worker_cpu_ms") is not None
            ]
        ),
        "peak_rss_kib": _summary(
            [
                receipt["metrics"]["rss_peak_kib"]
                for receipt in receipts
                if receipt["metrics"].get("rss_peak_kib") is not None
            ]
        ),
        "ir_bytes": _summary(
            [receipt["metrics"].get("ir_bytes", 0) for receipt in receipts]
        ),
        "cache_bytes": _summary(
            [receipt["metrics"].get("cache_bytes", 0) for receipt in receipts]
        ),
        "actual_build_counts": sorted(
            {receipt["metrics"].get("actual_build_count", 0) for receipt in receipts}
        ),
        "actual_backends": sorted({item["actual_backend"] for item in cold_items}),
        "payload_backend_sets": sorted(
            {tuple(item["payload_backends"]) for item in cold_items}
        ),
        "found_values": sorted({item["found"] for item in cold_items}),
        "path_net_path_sets": sorted(
            {tuple(item["path_net_paths"]) for item in cold_items}
        ),
        "coverage_statuses": sorted(
            {receipt["coverage_status"] for receipt in receipts}
        ),
        "query_statuses": sorted({receipt["query_status"] for receipt in receipts}),
        "query_confidences": sorted(
            {receipt["query_confidence"] for receipt in receipts}
        ),
        "blocking_diagnostic_counts": sorted(
            {receipt["coverage_blocking_diagnostic_count"] for receipt in receipts}
        ),
        "build_fingerprints": sorted(
            {receipt["build_key_sha256"] for receipt in receipts}
        ),
        "compile_fingerprints": sorted(
            {receipt["compile_fingerprint_sha256"] for receipt in receipts}
        ),
        "ir_fingerprints": sorted(
            {receipt["ir_fingerprint_sha256"] for receipt in receipts}
        ),
        "endpoint_pair_fingerprints": sorted(
            {item["endpoint_pair_fingerprint_sha256"] for item in cold_items}
        ),
        "manifest_input_counts": sorted(
            {receipt["manifest"].get("input_count") for receipt in receipts}
        ),
        "manifest_top_counts": sorted(
            {receipt["manifest"].get("top_count") for receipt in receipts}
        ),
        "coverage_boundary_path_counts": sorted(
            {
                receipt["scope"].get("coverage_boundary_instance_count")
                for receipt in receipts
            }
        ),
        "requested_cone_path_counts": sorted(
            {
                receipt["scope"].get("requested_cone_instance_count")
                for receipt in receipts
            }
        ),
        "coverage_fact_codes": sorted(
            {
                code
                for receipt in receipts
                for code in (
                    receipt["coverage_gap_codes"]
                    + receipt["objective_exclusions"]
                    + receipt["gap_codes"]
                )
            }
        ),
        "all_static_calls_zero": all(run["static_calls"] == 0 for run in runs),
    }
    return {"cold_runs": runs, "aggregate": aggregate}


async def _measure_concurrent_same_key(
    workload: PathWorkload,
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
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    with _public_environment(
        workload,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        raw = await asyncio.gather(
            *(_dispatch_path_timed(workload) for _ in range(request_count))
        )
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    compact = [
        _compact_path_result(result, observed_wall, observed_cpu)
        for result, observed_wall, observed_cpu in raw
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
        "build_fingerprints": sorted(
            {item["source_graph"]["build_key_sha256"] for item in compact}
        ),
        "flight_dispositions": dict(
            Counter(item["source_graph"]["flight_disposition"] for item in compact)
        ),
        "static_calls": static.total_calls,
    }


async def _measure_npi_probe(
    workload: PathWorkload,
    *,
    connected: bool,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    npi = _FakeNpiPathBackend(connected=connected)
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
        result, wall_ms, cpu_ms = await _dispatch_path_timed(workload)
    return {
        "probe_model": "deterministic trusted NPI routing probe; no license claim",
        "result": _compact_path_result(result, wall_ms, cpu_ms),
        "npi_path_calls": npi.path_calls,
        "source_graph_runtime_get_count": counters["runtime_get_count"],
        "static_calls": static.total_calls,
    }


def _inconclusive_fixture_ir():
    base = replace(
        build_hand_ir(),
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version=FRONTEND_VERSION,
    )
    gap = CoverageGap(
        code="objective_exclusion",
        message="runtime behavior remains outside the structural objective",
        impact=CoverageStatus.INCONCLUSIVE,
        constructs=("runtime",),
        scopes=("*",),
    )
    return replace(
        base,
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )


async def _measure_negative_probe(
    workload: PathWorkload,
    *,
    inconclusive: bool,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    runner = FixtureReadyRunner(_inconclusive_fixture_ir() if inconclusive else None)
    runtime = SourceGraphRuntime(runner)
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
        result, wall_ms, cpu_ms = await _dispatch_path_timed(
            workload,
            from_signal="sg_top.runtime_force",
            to_signal="sg_top.seed",
            expand_assigns=False,
        )
    return {
        "probe_model": (
            "tracked IR inconclusive no-path oracle"
            if inconclusive
            else "tracked IR coverage-complete not-connected oracle"
        ),
        "result": _compact_path_result(result, wall_ms, cpu_ms),
        "runtime_stats": runtime.stats_snapshot(),
        "static_calls": static.total_calls,
    }


async def _measure_target_specific_probe(
    workload: PathWorkload,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    runner = FixtureReadyRunner()
    runtime = SourceGraphRuntime(runner)
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
        first = _compact_path_result(*(await _dispatch_path_timed(workload)))
        repeated = _compact_path_result(*(await _dispatch_path_timed(workload)))
        changed_pair = _compact_path_result(
            *(
                await _dispatch_path_timed(
                    workload,
                    from_signal="sg_top.u_producer.rst_n",
                    to_signal="sg_top.bus.valid",
                )
            )
        )
        changed_expand = _compact_path_result(
            *(await _dispatch_path_timed(workload, expand_assigns=False))
        )
    keys = {
        "first": first["source_graph"]["build_key_sha256"],
        "repeated": repeated["source_graph"]["build_key_sha256"],
        "changed_pair": changed_pair["source_graph"]["build_key_sha256"],
        "changed_expand": changed_expand["source_graph"]["build_key_sha256"],
    }
    return {
        "probe_model": (
            "Phase 3A target-specific identity: exact repeat may warm; changed "
            "endpoint pair or semantic option remains cold"
        ),
        "build_keys": keys,
        "cache_dispositions": {
            "first": first["source_graph"]["cache_disposition"],
            "repeated": repeated["source_graph"]["cache_disposition"],
            "changed_pair": changed_pair["source_graph"]["cache_disposition"],
            "changed_expand": changed_expand["source_graph"]["cache_disposition"],
        },
        "runtime_stats": runtime.stats_snapshot(),
        "worker_calls": runner.calls,
        "static_calls": static.total_calls,
        "cross_target_reuse_implemented": False,
    }


async def _measure_failure_case(
    kind: str,
    workload: PathWorkload,
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
    with _public_environment(
        workload,
        runtime=runtime,
        config=config,
        static_backend=static,
    ):
        if kind == "cancellation":
            task = asyncio.create_task(_dispatch_path_timed(workload))
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
                raise BenchmarkError("cancelled path request unexpectedly returned")
        else:
            result, wall_ms, cpu_ms = await _dispatch_path_timed(workload)
            failure = _compact_path_result(result, wall_ms, cpu_ms)
        await runtime.wait_idle()
        after_failure = runtime.stats_snapshot()
        static_after_failure = static.total_calls
        retry_result, retry_wall, retry_cpu = await _dispatch_path_timed(workload)
        retry = _compact_path_result(retry_result, retry_wall, retry_cpu)
    return {
        "failure": failure,
        "cache_entry_count_after_failure": after_failure["cache_entry_count"],
        "inflight_count_after_failure": after_failure["inflight_count"],
        "static_calls_after_failure": static_after_failure,
        "retry": retry,
        "cache_entry_count_after_retry": runtime.stats_snapshot()["cache_entry_count"],
    }


async def _measure_failure_probes(
    workload: PathWorkload,
    factories: FailureRunnerFactories,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
    timeout_seconds: float,
    cancellation_delay_seconds: float,
) -> dict[str, Any]:
    pairs = (
        ("dependency", factories.dependency),
        ("build_failure", factories.build_failure),
        ("crash", factories.crash),
        ("timeout", factories.timeout),
        ("cancellation", factories.cancellation),
    )
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
        for kind, factory in pairs
    }


async def _measure_driver_load_unchanged_probe(
    workload: PathWorkload,
    *,
    static_factory: StaticFactory,
    frontend_python: Path,
) -> dict[str, Any]:
    runtime = SourceGraphRuntime(FixtureReadyRunner())
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
        driver = await server._dispatch(
            "explain_signal_driver",
            {
                "signal_path": "sg_top.u_producer.seed",
                "wave_path": "/unused/phase3a.fsdb",
                "compile_log": workload.compile_log,
                "simulator": workload.simulator,
                "top_hint": workload.top,
                "recursive": True,
                "max_depth": 64,
            },
        )
        loads = await server._dispatch(
            "find_signal_loads",
            {
                "signal_path": "sg_top.u_producer.seed",
                "compile_log": workload.compile_log,
                "simulator": workload.simulator,
                "top_hint": workload.top,
                "max_depth": 64,
                "include_expr": True,
            },
        )
    return {
        "driver_actual_backend": driver.backend_status.actual_backend,
        "loads_actual_backend": loads.backend_status.actual_backend,
        "driver_payload_backend": driver.backend,
        "loads_payload_backend": loads.backend,
        "static_calls": static.total_calls,
        "runtime_stats": runtime.stats_snapshot(),
    }


def _route_isolation_receipt() -> dict[str, Any]:
    accepted = phase2._git_show(ACCEPTED_PHASE2_HEAD, "server.py")
    current = (ROOT / "server.py").read_text(encoding="utf-8")
    branches = {}
    for tool in (
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
        "trace_x_source",
    ):
        before = phase2._dispatch_branch_digest(accepted, tool)
        after = phase2._dispatch_branch_digest(current, tool)
        branches[tool] = {
            "accepted_ast_sha256": before,
            "current_ast_sha256": after,
            "changed": before != after,
        }
    functions = {}
    for function_name in (
        "_route_public_connectivity",
        "_run_trace_x_attempt",
        "_handle_trace_x_source",
        "_run_in_wave_thread",
        "_wave_locks_for",
    ):
        before = phase2._function_digest(accepted, function_name)
        after = phase2._function_digest(current, function_name)
        functions[function_name] = {
            "accepted_ast_sha256": before,
            "current_ast_sha256": after,
            "changed": before != after,
        }
    changed_tools = [tool for tool, item in branches.items() if item["changed"]]
    return {
        "accepted_head": ACCEPTED_PHASE2_HEAD,
        "dispatch_branches": branches,
        "functions": functions,
        "production_route_changed_tools": changed_tools,
        "phase3a_isolated": (
            changed_tools == ["trace_signal_path"]
            and not any(item["changed"] for item in functions.values())
        ),
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


def _workload_gate(item: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    aggregate = item["path_operation"]["aggregate"]
    concurrent = item["concurrent_same_key"]
    _check(
        checks,
        "source_graph_found",
        aggregate["actual_backends"] == ["source_graph"]
        and aggregate["found_values"] == [True]
        and aggregate["query_statuses"] == ["found"],
        expected={"backend": ["source_graph"], "found": [True]},
        actual={
            "backend": aggregate["actual_backends"],
            "found": aggregate["found_values"],
            "query_status": aggregate["query_statuses"],
        },
    )
    _check(
        checks,
        "tracked_endpoint_oracle",
        aggregate["path_net_path_sets"] == [tuple(item["expected_path"])],
        expected=[tuple(item["expected_path"])],
        actual=aggregate["path_net_path_sets"],
    )
    _check(
        checks,
        "single_provenance",
        aggregate["payload_backend_sets"] == [("source_graph",)]
        and aggregate["all_static_calls_zero"],
        expected={"payload": [("source_graph",)], "static": 0},
        actual={
            "payload": aggregate["payload_backend_sets"],
            "static_zero": aggregate["all_static_calls_zero"],
        },
    )
    _check(
        checks,
        "cold_build_and_fingerprints",
        aggregate["actual_build_counts"] == [1]
        and all(
            len(value) == 64
            for field in (
                "build_fingerprints",
                "compile_fingerprints",
                "ir_fingerprints",
                "endpoint_pair_fingerprints",
            )
            for value in aggregate[field]
        ),
        expected={"actual_build_counts": [1], "fingerprint_length": 64},
        actual={
            "actual_build_counts": aggregate["actual_build_counts"],
            "fingerprints": {
                field: aggregate[field]
                for field in (
                    "build_fingerprints",
                    "compile_fingerprints",
                    "ir_fingerprints",
                    "endpoint_pair_fingerprints",
                )
            },
        },
    )
    _check(
        checks,
        "warm_exact_path_latency",
        aggregate["warm_wall_time_ms"]["p95"] is not None
        and aggregate["warm_wall_time_ms"]["p95"]
        <= GATE_TARGETS["warm_exact_path_public_p95_max_ms"],
        expected=f"<={GATE_TARGETS['warm_exact_path_public_p95_max_ms']}ms",
        actual=aggregate["warm_wall_time_ms"]["p95"],
    )
    _check(
        checks,
        "same_key_single_flight",
        concurrent["actual_build_count"] == 1
        and concurrent["actual_backends"]
        == {"source_graph": concurrent["request_count"]}
        and concurrent["payload_backend_sets"] == [("source_graph",)]
        and concurrent["static_calls"] == 0,
        expected={"actual_build_count": 1, "backend": "source_graph"},
        actual=concurrent,
    )
    peak = aggregate["peak_rss_kib"]["max"]
    _check(
        checks,
        "peak_rss",
        peak is not None and peak <= GATE_TARGETS["peak_rss_max_kib"],
        expected=f"<={GATE_TARGETS['peak_rss_max_kib']}KiB",
        actual=peak,
    )
    if item["name"] == "opentitan_core":
        _check(
            checks,
            "opentitan_scoped_cold_latency",
            aggregate["cold_wall_time_ms"]["p50"] is not None
            and aggregate["cold_wall_time_ms"]["p50"]
            <= GATE_TARGETS["opentitan_scoped_cold_p50_max_ms"],
            expected=f"<={GATE_TARGETS['opentitan_scoped_cold_p50_max_ms']}ms",
            actual=aggregate["cold_wall_time_ms"]["p50"],
        )
        _check(
            checks,
            "opentitan_bounded_manifest_scope",
            aggregate["manifest_input_counts"] == [785]
            and aggregate["manifest_top_counts"] == [11]
            and aggregate["coverage_boundary_path_counts"] == [4]
            and aggregate["requested_cone_path_counts"] == [4],
            expected={"inputs": [785], "tops": [11], "boundary": [4], "cone": [4]},
            actual={
                "inputs": aggregate["manifest_input_counts"],
                "tops": aggregate["manifest_top_counts"],
                "boundary": aggregate["coverage_boundary_path_counts"],
                "cone": aggregate["requested_cone_path_counts"],
            },
        )
        _check(
            checks,
            "opentitan_coverage_honesty",
            aggregate["coverage_statuses"] == ["inconclusive"]
            and aggregate["blocking_diagnostic_counts"] == [65]
            and aggregate["query_confidences"] == ["partial"],
            expected={
                "coverage": ["inconclusive"],
                "blocking": [65],
                "confidence": ["partial"],
            },
            actual={
                "coverage": aggregate["coverage_statuses"],
                "blocking": aggregate["blocking_diagnostic_counts"],
                "confidence": aggregate["query_confidences"],
            },
        )
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def _failure_gate(probes: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    expected_status = {
        "dependency": "dependency_blocked",
        "build_failure": "build_failed",
        "crash": "worker_crash",
        "timeout": "timed_out",
    }
    for kind, prepare_status in expected_status.items():
        probe = probes[kind]
        receipt = probe["failure"]["source_graph"]
        _check(
            checks,
            f"{kind}_structured_static_fallback",
            probe["failure"]["actual_backend"] == "static"
            and probe["failure"]["unsupported_reason"] == "static_backend_no_path_api"
            and receipt["prepare_status"] == prepare_status
            and receipt["fallback_used"] is True,
            expected={"actual": "static", "prepare": prepare_status},
            actual={
                "actual": probe["failure"]["actual_backend"],
                "prepare": receipt["prepare_status"],
                "fallback_used": receipt["fallback_used"],
            },
        )
    cancelled = probes["cancellation"]
    _check(
        checks,
        "cancellation_no_fallback",
        cancelled["failure"]
        == {"status": "cancelled", "fallback_used": False, "actual_backend": None}
        and cancelled["static_calls_after_failure"] == 0,
        expected={"cancelled": True, "static_calls": 0},
        actual={
            "failure": cancelled["failure"],
            "static_calls": cancelled["static_calls_after_failure"],
        },
    )
    for kind, probe in probes.items():
        _check(
            checks,
            f"{kind}_cleanup_and_retry",
            probe["cache_entry_count_after_failure"] == 0
            and probe["inflight_count_after_failure"] == 0
            and probe["retry"]["actual_backend"] == "source_graph"
            and probe["retry"]["found"] is True
            and probe["cache_entry_count_after_retry"] == 1,
            expected={"cache": 0, "inflight": 0, "retry": "source_graph"},
            actual={
                "cache": probe["cache_entry_count_after_failure"],
                "inflight": probe["inflight_count_after_failure"],
                "retry": probe["retry"]["actual_backend"],
            },
        )
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


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
    baseline = _read_phase2_baseline(args.phase2_evidence)
    process_factory: RunnerFactory = runner_factory or (
        lambda: IsolatedSourceGraphProcessRunner(
            python_executable=args.frontend_python,
            working_directory=ROOT,
        )
    )
    original_static = connectivity_backend.StaticConnectivityBackend
    selected_static_factory = static_factory or original_static
    if failure_factories is None:
        missing_worker = ROOT / "scripts/__missing_source_graph_phase3a_worker__.py"
        if missing_worker.exists():
            raise BenchmarkError("reserved missing-worker path unexpectedly exists")
        failure_factories = FailureRunnerFactories(
            dependency=lambda: _FirstThenRunner(
                _FailedRunner(PrepareStatus.DEPENDENCY_BLOCKED, "frontend_unavailable"),
                process_factory(),
            ),
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

    with tempfile.TemporaryDirectory(prefix="traceweave-phase3a-benchmark-") as temp:
        temp_root = Path(temp)
        hand = _hand_workload(temp_root)
        selected = args.workload or ["hand_fixture"]
        workloads = {"hand_fixture": hand}
        if "opentitan_core" in selected:
            workloads["opentitan_core"] = _opentitan_workload(
                args.opentitan_compile_log
            )

        route_probes = {
            "npi_success": await _measure_npi_probe(
                hand,
                connected=True,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "npi_complete_not_connected": await _measure_npi_probe(
                hand,
                connected=False,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "source_graph_complete_not_connected": await _measure_negative_probe(
                hand,
                inconclusive=False,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "source_graph_inconclusive_to_static": await _measure_negative_probe(
                hand,
                inconclusive=True,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "target_specific_identity": await _measure_target_specific_probe(
                hand,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
            "driver_load_unchanged": await _measure_driver_load_unchanged_probe(
                hand,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
            ),
        }
        measured_workloads = []
        for name in selected:
            workload = workloads[name]
            path_operation = await _measure_workload(
                workload,
                runner_factory=process_factory,
                static_factory=selected_static_factory,
                frontend_python=args.frontend_python,
                timeout_seconds=args.worker_timeout_seconds,
                cold_repeats=args.cold_repeats,
                warm_repeats=args.warm_repeats,
            )
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
                "expected_path": list(workload.expected_path),
                "path_operation": path_operation,
                "concurrent_same_key": concurrent,
            }
            item["gate"] = _workload_gate(item)
            measured_workloads.append(item)

        failures = await _measure_failure_probes(
            hand,
            failure_factories,
            static_factory=selected_static_factory,
            frontend_python=args.frontend_python,
            timeout_seconds=args.worker_timeout_seconds,
            cancellation_delay_seconds=args.cancellation_delay_seconds,
        )
        failure_gate = _failure_gate(failures)

    isolation = _route_isolation_receipt()
    parent_import = phase2._parent_import_receipt()
    route_checks: list[dict[str, Any]] = []
    npi_success = route_probes["npi_success"]
    _check(
        route_checks,
        "trusted_npi_precedence",
        npi_success["result"]["actual_backend"] == "verdi_npi"
        and npi_success["result"]["found"] is True
        and npi_success["source_graph_runtime_get_count"] == 0
        and npi_success["static_calls"] == 0,
        expected={"actual": "verdi_npi", "source_graph": 0, "static": 0},
        actual=npi_success,
    )
    npi_negative = route_probes["npi_complete_not_connected"]
    _check(
        route_checks,
        "npi_authoritative_negative_unchanged",
        npi_negative["result"]["actual_backend"] == "verdi_npi"
        and npi_negative["result"]["unsupported_reason"] == "not_connected"
        and npi_negative["source_graph_runtime_get_count"] == 0,
        expected={"actual": "verdi_npi", "reason": "not_connected"},
        actual=npi_negative,
    )
    complete_negative = route_probes["source_graph_complete_not_connected"]
    _check(
        route_checks,
        "source_graph_complete_negative",
        complete_negative["result"]["actual_backend"] == "source_graph"
        and complete_negative["result"]["unsupported_reason"] == "not_connected"
        and complete_negative["result"]["source_graph"]["coverage_status"] == "complete"
        and complete_negative["static_calls"] == 0,
        expected={"actual": "source_graph", "reason": "not_connected"},
        actual=complete_negative,
    )
    inconclusive = route_probes["source_graph_inconclusive_to_static"]
    _check(
        route_checks,
        "inconclusive_negative_to_static",
        inconclusive["result"]["actual_backend"] == "static"
        and inconclusive["result"]["unsupported_reason"] == "static_backend_no_path_api"
        and inconclusive["result"]["source_graph"]["query_status"] == "inconclusive"
        and inconclusive["result"]["source_graph"]["fallback_used"] is True
        and inconclusive["static_calls"] == 1,
        expected={"actual": "static", "query": "inconclusive"},
        actual=inconclusive,
    )
    identity = route_probes["target_specific_identity"]
    keys = identity["build_keys"]
    _check(
        route_checks,
        "target_specific_identity_no_cross_target_reuse",
        keys["first"] == keys["repeated"]
        and len({keys["first"], keys["changed_pair"], keys["changed_expand"]}) == 3
        and identity["runtime_stats"]["actual_build_count"] == 3
        and identity["worker_calls"] == 3
        and identity["cross_target_reuse_implemented"] is False,
        expected={"exact_repeat_same": True, "distinct_builds": 3},
        actual=identity,
    )
    unchanged = route_probes["driver_load_unchanged"]
    _check(
        route_checks,
        "driver_load_public_route_unchanged",
        unchanged["driver_actual_backend"] == "source_graph"
        and unchanged["loads_actual_backend"] == "source_graph"
        and unchanged["static_calls"] == 0,
        expected={"driver": "source_graph", "loads": "source_graph"},
        actual=unchanged,
    )
    _check(
        route_checks,
        "phase3a_ast_isolation",
        isolation["phase3a_isolated"],
        expected={"changed_tools": ["trace_signal_path"], "other_functions": False},
        actual=isolation,
    )
    _check(
        route_checks,
        "parent_import_optional_frontend_free",
        parent_import["server_import_succeeded"]
        and not parent_import["pyslang_imported"]
        and not parent_import["uhdm_imported"],
        expected={"server": True, "pyslang": False, "uhdm": False},
        actual=parent_import,
    )
    route_gate = {
        "passed": all(check["passed"] for check in route_checks),
        "checks": route_checks,
    }
    workload_gate = bool(measured_workloads) and all(
        item["gate"]["passed"] for item in measured_workloads
    )
    passed = workload_gate and route_gate["passed"] and failure_gate["passed"]
    decision = (
        "phase3a_trace_signal_path_gate_passed"
        if passed
        else "phase3a_no_go_keep_auditing_trace_signal_path"
    )
    script_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {"root": str(ROOT), "head": phase1b._git_head(ROOT)},
        "benchmark_script": {
            "path": str(script_path.relative_to(ROOT)),
            "sha256": phase1b._sha256_file(script_path),
        },
        "before_baselines": {
            "phase1a": {"sha256": PHASE1A_EVIDENCE_SHA256},
            "phase1b": {"sha256": PHASE1B_EVIDENCE_SHA256},
            "phase2": baseline,
        },
        "frontend": {
            "name": SLANG_FRONTEND_NAME,
            "required_version": FRONTEND_VERSION,
            "interpreter": str(args.frontend_python),
            "parent_import_requires_pyslang": False,
            "dependency_model": "optional pinned isolated one-shot worker",
        },
        "runtime_model": {
            "production_route": (
                "trusted Verdi NPI -> bounded dual-endpoint Source Graph -> "
                "Legacy Static structured unsupported"
            ),
            "process_shared_lazy_runtime": True,
            "compile_session_manifest_cache": "bounded process memory",
            "ir_cache": "target-specific process-session memory only",
            "same_exact_path_can_warm": True,
            "different_endpoint_pair_may_be_cold": True,
            "cross_target_reuse_implemented": False,
            "disk_cache": False,
            "persistent_worker": False,
            "startup_build": False,
            "startup_full_design_enumeration": False,
            "same_key_single_flight": True,
            "max_concurrent_cold_builds_per_process": 1,
            "waveform_locking_model_changed": False,
        },
        "measurement_policy": {
            "cold": "fresh adapter manifest cache and Source Graph runtime per repeat",
            "warm": "repeated exact public endpoint pair with process-memory hits",
            "concurrent": "simultaneous identical public path requests",
            "failure": (
                "dependency/build/crash/timeout whole-result Static fallback; "
                "cancellation no-fallback; same-runtime safe retry"
            ),
            "correctness_oracle": "tracked hand IR and RTL fixture",
            "opentitan_claim": "bounded representative target-scoped only",
        },
        "gate_targets": GATE_TARGETS,
        "route_probes": {
            **route_probes,
            "scope_isolation": isolation,
            "parent_import": parent_import,
            "gate": route_gate,
        },
        "workloads": measured_workloads,
        "failure_probes": {"results": failures, "gate": failure_gate},
        "assessment": {
            "decision": decision,
            "phase3a_trace_signal_path_gate_passed": passed,
            "workload_gate_passed": workload_gate,
            "route_gate_passed": route_gate["passed"],
            "failure_gate_passed": failure_gate["passed"],
            "production_route_changed": True,
            "production_route_changed_tools": ["trace_signal_path"],
            "driver_load_route_changed": False,
            "trace_x_source_route_changed": False,
            "cross_target_reuse_implemented": False,
            "waveform_locking_model_changed": False,
            "disk_cache": False,
            "persistent_worker": False,
            "startup_full_design_enumeration": False,
            "coverage_claim": "target_scoped_only_partial_or_inconclusive_preserved",
            "opentitan_claim": "target-scoped only; no full-design path claim",
            "next_step": "stop after Phase 3A and await explicit Phase 3B authorization",
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
    return 0 if result["assessment"]["phase3a_trace_signal_path_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
