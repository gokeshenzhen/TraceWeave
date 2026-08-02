#!/usr/bin/env python3
"""Phase 1A benchmark for the internal minimal Source Graph prototype.

The benchmark is deliberately separate from the Phase 0A Legacy Static and
Phase 0B broad frontend evidence harnesses.  A fresh pinned Python worker builds
Slang's frontend objects, projects the narrow connectivity IR, serializes it,
and measures successful warm driver/load queries.  Nothing in this file is
registered as an MCP tool or production backend.
"""

from __future__ import annotations

import argparse
from collections import Counter
import contextlib
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.spike_source_frontend import (  # noqa: E402
    _configure_driver,
    _diagnostics_payload,
    _parse_name_values,
    build_deep_workload,
    build_hand_workload,
    build_real_workload,
)
from src.connectivity_ir import (  # noqa: E402
    ConnectivityIR,
    CoverageStatus,
    SourceLocation,
)
from src.connectivity_query import (  # noqa: E402
    ConnectivityQueryEngine,
    QueryStatus,
)
from src.slang_connectivity_projector import (  # noqa: E402
    ProjectionDiagnostic,
    ProjectionExclusion,
    ProjectionOptions,
    project_slang_design,
)


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "source_graph_connectivity_phase1a"
FRONTEND_NAME = "Slang/pyslang"
FRONTEND_VERSION = "11.0.0"
DEFAULT_FRONTEND_PYTHON = Path("/tmp/traceweave-phase0b-pyslang-11.0.0/bin/python")
DEFAULT_OPENTITAN_ORACLE = (
    ROOT / "benchmarks" / "source_graph_frontend_phase0b_opentitan_oracles.json"
)
WORKLOAD_NAMES = ("deep_x_npi", "hand_fixture", "opentitan_core")
PHASE0B_REFERENCE = {
    "cold_worker_wall_p50_ms": 54_120.388241,
    "broad_object_extraction_wall_p50_ms": 49_193.00829,
    "peak_rss_p50_kib": 2_137_532,
    "role": "accepted Phase 0B broad feasibility evidence; not query latency",
}
GATE_TARGETS = {
    "cold_prepare_preferred_max_ms": 15_000.0,
    "cold_prepare_stop_threshold_ms": 30_000.0,
    "warm_successful_query_p95_max_ms": 100.0,
    "peak_rss_max_kib": 2_621_440,
}
OPENTITAN_FOCUS_PATHS = (
    "tb",
    "tb.dut",
    "tb.dut.top_earlgrey",
    "tb.dut.top_earlgrey.u_rv_core_ibex",
    "tb.dut.top_earlgrey.u_xbar_main",
)
OPENTITAN_ASSIGNMENT_PATHS = OPENTITAN_FOCUS_PATHS[-2:]
MAX_RESULT_DIAGNOSTIC_ITEMS = 100
MAX_RESULT_GAP_ITEMS = 40
MAX_RESULT_QUERY_MATCHES = 20


class BenchmarkError(RuntimeError):
    """A benchmark input, worker, or output contract is invalid."""


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        action="append",
        choices=WORKLOAD_NAMES,
        help="Workload to run; repeat to select multiple (default: all available)",
    )
    parser.add_argument(
        "--frontend-python",
        type=Path,
        default=DEFAULT_FRONTEND_PYTHON,
        help="Pinned CPython 3.11 interpreter containing pyslang 11.0.0",
    )
    parser.add_argument("--cold-repeats", type=int, default=3)
    parser.add_argument("--query-repeats", type=int, default=100)
    parser.add_argument("--worker-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--real-compile-log", type=Path)
    parser.add_argument(
        "--real-simulator", choices=("auto", "vcs", "xcelium"), default="auto"
    )
    parser.add_argument("--real-waveform", type=Path)
    parser.add_argument("--real-env", action="append", default=[])
    parser.add_argument(
        "--opentitan-oracle",
        type=Path,
        default=DEFAULT_OPENTITAN_ORACLE,
        help="Manual-only Phase 0B OpenTitan oracle receipt",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker-spec", type=Path, help=argparse.SUPPRESS)
    return parser


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _round_ms(value: float) -> float:
    return round(value, 6)


def _read_proc_rss_kib() -> dict[str, int | None]:
    values: dict[str, int | None] = {"current": None, "high_water": None}
    try:
        with open("/proc/self/status", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    values["current"] = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    values["high_water"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return values


def _measure_phase(fn: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    rss_start = _read_proc_rss_kib()
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    value = fn()
    wall_ms = (time.perf_counter_ns() - wall_start) / 1_000_000
    cpu_ms = (time.process_time_ns() - cpu_start) / 1_000_000
    rss_end = _read_proc_rss_kib()
    return value, {
        "wall_time_ms": _round_ms(wall_ms),
        "cpu_time_ms": _round_ms(cpu_ms),
        "rss_start_kib": rss_start["current"],
        "rss_peak_kib": rss_end["high_water"],
        "rss_end_kib": rss_end["current"],
    }


@contextlib.contextmanager
def _temporary_environment(values: Mapping[str, str]):
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _sample_summary(
    values: Sequence[float], *, include_samples: bool = True
) -> dict[str, Any]:
    rounded = [_round_ms(value) for value in values]
    return {
        "count": len(values),
        "min": _round_ms(min(values)) if values else None,
        "p50": _round_ms(statistics.median(values)) if values else None,
        "p95": (
            _round_ms(_percentile(values, 0.95))
            if _percentile(values, 0.95) is not None
            else None
        ),
        "max": _round_ms(max(values)) if values else None,
        "samples": rounded if include_samples else None,
    }


def _git_head(path: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _deep_queries() -> list[dict[str, Any]]:
    leaf = (
        "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel."
        "u_rx_fifo.u_storage_bank.u_x_cell"
    )
    return [
        {
            "name": "seven_level_output_driver",
            "operation": "driver",
            "signal": "uart_deep_x_tb.apb_prdata[7:0]",
            "expected": {
                "status": "found",
                "instance_path": leaf,
                "file_suffix": "tests/fixtures/deep_x_npi/rtl/deep_uart_x.sv",
                "line": 22,
                "boundary": "sequential",
                "procedure_kind": "AlwaysFF",
                "traversal_depth": 7,
            },
        },
        {
            "name": "seven_level_control_load",
            "operation": "load",
            "signal": "uart_deep_x_tb.inject_x",
            "expected": {
                "status": "found",
                "instance_path": leaf,
                "file_suffix": "tests/fixtures/deep_x_npi/rtl/deep_uart_x.sv",
                "line": 24,
                "boundary": "sequential",
                "procedure_kind": "AlwaysFF",
                "traversal_depth": 7,
            },
        },
    ]


def _hand_queries() -> list[dict[str, Any]]:
    source = "tests/fixtures/source_graph_frontend/hand_connectivity.sv"
    return [
        {
            "name": "generated_positional_slice_driver",
            "operation": "driver",
            "signal": "sg_top.lane_data[15:8]",
            "expected": {
                "status": "found",
                "instance_path": "sg_top.u_bridge.gen_lanes[1].u_lane",
                "file_suffix": source,
                "line": 50,
                "boundary": "combinational",
                "traversal_depth": 2,
            },
        },
        {
            "name": "modport_concat_driver",
            "operation": "driver",
            "signal": "sg_top.bus.data[15:8]",
            "expected": {
                "status": "found",
                "instance_path": "sg_top.u_producer",
                "file_suffix": source,
                "line": 86,
                "boundary": "combinational",
                "procedure_kind": "AlwaysComb",
                "traversal_depth": 1,
            },
        },
        {
            "name": "always_ff_named_output_driver",
            "operation": "driver",
            "signal": "sg_top.seed",
            "expected": {
                "status": "found",
                "instance_path": "sg_top.u_producer",
                "file_suffix": source,
                "line": 79,
                "boundary": "sequential",
                "procedure_kind": "AlwaysFF",
                "traversal_depth": 1,
            },
        },
        {
            "name": "modport_positional_named_slice_load",
            "operation": "load",
            "signal": "sg_top.bus.data[15:8]",
            "expected": {
                "status": "found",
                "instance_path": "sg_top.u_bridge.gen_lanes[1].u_lane.u_named",
                "file_suffix": source,
                "line": 23,
                "boundary": "sequential",
                "procedure_kind": "AlwaysFF",
                "traversal_depth": 2,
            },
        },
    ]


def _opentitan_queries() -> list[dict[str, Any]]:
    core = "tb.dut.top_earlgrey.u_rv_core_ibex"
    xbar = "tb.dut.top_earlgrey.u_xbar_main"
    core_file = "lowrisc_earlgrey_ip_rv_core_ibex_0.1/rtl/rv_core_ibex.sv"
    xbar_file = "lowrisc_top_earlgrey_xbar_main_0.1/rtl/autogen/xbar_main.sv"
    return [
        {
            "name": "core_local_driver",
            "operation": "driver",
            "signal": f"{core}.fatal_intg_event",
            "expected": {
                "status": "found",
                "instance_path": core,
                "file_suffix": core_file,
                "line": 265,
                "boundary": "combinational",
                "traversal_depth": 0,
                "confidence": "partial",
            },
        },
        {
            "name": "core_local_load",
            "operation": "load",
            "signal": f"{core}.fatal_intg_event",
            "expected": {
                "status": "found",
                "instance_path": core,
                "file_suffix": core_file,
                "line": 873,
                "boundary": "combinational",
                "traversal_depth": 0,
                "confidence": "partial",
            },
        },
        {
            "name": "xbar_local_driver",
            "operation": "driver",
            "signal": f"{xbar}.unused_scanmode",
            "expected": {
                "status": "found",
                "instance_path": xbar,
                "file_suffix": xbar_file,
                "line": 207,
                "boundary": "combinational",
                "traversal_depth": 0,
                "confidence": "partial",
            },
        },
        {
            "name": "xbar_local_load",
            "operation": "load",
            "signal": f"{xbar}.scanmode_i",
            "expected": {
                "status": "found",
                "instance_path": xbar,
                "file_suffix": xbar_file,
                "line": 207,
                "boundary": "combinational",
                "traversal_depth": 0,
                "confidence": "partial",
            },
        },
    ]


def _location_oracles(name: str) -> list[dict[str, Any]]:
    if name == "deep_x_npi":
        return [
            {
                "definition": "uart_x_storage_cell",
                "file_suffix": "tests/fixtures/deep_x_npi/rtl/deep_uart_x.sv",
                "line": 14,
            }
        ]
    if name == "hand_fixture":
        source = "tests/fixtures/source_graph_frontend/hand_connectivity.sv"
        return [
            {"definition": "sg_bus_if", "file_suffix": source, "line": 3},
            {"definition": "sg_leaf", "file_suffix": source, "line": 12},
        ]
    return [
        {
            "definition": "tb",
            "file_suffix": "lowrisc_dv_top_earlgrey_chip_sim_0.1/tb/tb.sv",
            "line": 5,
        },
        {
            "definition": "chip_earlgrey_asic",
            "file_suffix": (
                "lowrisc_systems_chip_earlgrey_asic_0.1/rtl/autogen/"
                "chip_earlgrey_asic.sv"
            ),
            "line": 12,
        },
        {
            "definition": "top_earlgrey",
            "file_suffix": (
                "lowrisc_systems_top_earlgrey_0.1/rtl/autogen/top_earlgrey.sv"
            ),
            "line": 11,
        },
        {
            "definition": "rv_core_ibex",
            "file_suffix": "lowrisc_earlgrey_ip_rv_core_ibex_0.1/rtl/rv_core_ibex.sv",
            "line": 13,
        },
        {
            "definition": "xbar_main",
            "file_suffix": (
                "lowrisc_top_earlgrey_xbar_main_0.1/rtl/autogen/xbar_main.sv"
            ),
            "line": 128,
        },
    ]


def _query_oracles(name: str) -> list[dict[str, Any]]:
    return {
        "deep_x_npi": _deep_queries,
        "hand_fixture": _hand_queries,
        "opentitan_core": _opentitan_queries,
    }[name]()


def _read_opentitan_oracle(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        oracle = payload["workloads"]["real_uvm"]
        annotated = oracle["annotated_source"]
        elaborated = oracle["xcelium_elaboration"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"invalid OpenTitan oracle {path}: {exc}") from exc
    expected = list(annotated["expected_instance_paths"])
    if expected != list(elaborated["expected_instance_paths"]):
        raise BenchmarkError("OpenTitan annotated and Xcelium hierarchy oracles differ")
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_instance_paths": expected,
        "opentitan_commit": annotated["evidence"]["opentitan_commit"],
        "xcelium_simulator": elaborated["evidence"]["simulator"],
        "artifact_role": "manual benchmark input; never loaded by automated tests",
    }


def build_workload_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = args.workload or ["deep_x_npi", "hand_fixture"]
    specs: list[dict[str, Any]] = []
    for name in selected:
        if name == "deep_x_npi":
            workload = build_deep_workload()
            expected_paths = workload["manual_oracle"]["expected_instance_paths"]
            source_root = ROOT
            focus_paths: tuple[str, ...] = ()
            assignment_paths: tuple[str, ...] = ()
            oracle_receipt = {
                "type": "tracked_source_hand_annotation",
                "path": None,
                "tracked_inputs_only": True,
            }
        elif name == "hand_fixture":
            workload = build_hand_workload()
            expected_paths = workload["manual_oracle"]["expected_instance_paths"]
            source_root = ROOT
            focus_paths = ()
            assignment_paths = ()
            oracle_receipt = {
                "type": "tracked_json_hand_annotation",
                "path": str(
                    ROOT / "tests/fixtures/source_graph_frontend/hand_oracle.json"
                ),
                "tracked_inputs_only": True,
            }
        else:
            if args.real_compile_log is None:
                raise BenchmarkError(
                    "--real-compile-log is required for opentitan_core"
                )
            workload = build_real_workload(
                args.real_compile_log,
                _parse_name_values(args.real_env),
                simulator=args.real_simulator,
                waveform=args.real_waveform,
            )
            oracle_receipt = _read_opentitan_oracle(args.opentitan_oracle)
            expected_paths = oracle_receipt["expected_instance_paths"]
            if tuple(expected_paths) != OPENTITAN_FOCUS_PATHS:
                raise BenchmarkError(
                    "OpenTitan focus paths differ from the accepted Phase 0B oracle"
                )
            source_root = Path(workload["compile_cwd"])
            focus_paths = OPENTITAN_FOCUS_PATHS
            assignment_paths = OPENTITAN_ASSIGNMENT_PATHS

        spec = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "workload": name,
            "top": workload["top"],
            "frontend_args": workload["translation"]["frontend_args"],
            "environment": workload.get("environment") or {},
            "source_root": str(source_root.resolve()),
            "source_facts": workload["source_facts"],
            "translation_receipt": {
                "simulator": workload["simulator"],
                "unsupported_options": workload["translation"]["unsupported_options"],
                "translator_scope": workload["translation"]["translator_scope"],
            },
            "expected_instance_paths": expected_paths,
            "expected_locations": _location_oracles(name),
            "query_oracles": _query_oracles(name),
            "focus_instance_paths": list(focus_paths),
            "assignment_instance_paths": list(assignment_paths),
            "oracle_receipt": oracle_receipt,
            "query_repeats": args.query_repeats,
        }
        spec["input_fingerprint_sha256"] = _sha256(
            {
                key: value
                for key, value in spec.items()
                if key not in {"environment", "oracle_receipt"}
            }
        )
        specs.append(spec)
    return specs


def _normalize_location(
    item: Mapping[str, Any], source_root: Path
) -> SourceLocation | None:
    file_name = item.get("file")
    line = item.get("line")
    if not file_name or not line:
        return None
    path = Path(str(file_name))
    try:
        normalized = path.resolve().relative_to(source_root.resolve()).as_posix()
    except (OSError, ValueError):
        normalized = path.as_posix()
    return SourceLocation(
        file=normalized,
        line=int(line),
        column=max(int(item.get("column") or 0), 0),
    )


def _projection_diagnostics(
    payload: Mapping[str, Any], source_root: Path
) -> tuple[ProjectionDiagnostic, ...]:
    return tuple(
        ProjectionDiagnostic(
            code=str(item["code"]),
            severity=str(item["severity"]),
            message=str(item["message"]),
            location=_normalize_location(item, source_root),
            constructs=("frontend_diagnostic",),
            scopes=("*",),
        )
        for item in payload.get("items", ())
    )


def _projection_exclusions(spec: Mapping[str, Any]) -> tuple[ProjectionExclusion, ...]:
    if spec["workload"] != "opentitan_core":
        return ()
    exclusions = [
        ProjectionExclusion(
            code="uvm_dynamic_connectivity_not_modeled",
            message="UVM class and virtual-interface runtime connectivity is outside the static projector",
            scopes=("*",),
            constructs=("uvm", "dynamic_testbench"),
        ),
        ProjectionExclusion(
            code="dpi_runtime_not_modeled",
            message="native DPI implementation and simulator system-task behavior is not projected",
            scopes=("*",),
            constructs=("dpi", "runtime_system_task"),
        ),
        ProjectionExclusion(
            code="procedural_force_not_modeled",
            message="runtime force and release connectivity is not represented as an exact source edge",
            scopes=("*",),
            constructs=("force", "release"),
        ),
        ProjectionExclusion(
            code="bind_semantics_incomplete",
            message="unresolved bind targets remain outside the focused DUT/core projection",
            scopes=("*",),
            constructs=("bind",),
        ),
        ProjectionExclusion(
            code="protected_payload_not_modeled",
            message="unreadable protected payload cannot contribute exact source connectivity",
            scopes=("*",),
            constructs=("protected_source",),
        ),
    ]
    by_impact = Counter(
        str(item.get("impact") or "unspecified")
        for item in spec["translation_receipt"].get("unsupported_options", ())
    )
    for impact, count in sorted(by_impact.items()):
        exclusions.append(
            ProjectionExclusion(
                code=f"compile_option_exclusion:{impact}",
                message=f"{count} translated compile inputs have impact {impact}",
                scopes=("*",),
                constructs=("compile_input",),
            )
        )
    return tuple(exclusions)


def _diagnostic_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    items = list(payload.get("items", ()))[:MAX_RESULT_DIAGNOSTIC_ITEMS]
    semantic = {
        "total": payload.get("total"),
        "blocking_error_count": payload.get("blocking_error_count"),
        "by_effective_severity": payload.get("by_effective_severity", {}),
        "by_code": payload.get("by_code", {}),
        "items": items,
    }
    return {
        **semantic,
        "items_truncated": bool(payload.get("items_truncated")),
        "explicitly_suppressed_unknown_system_count": payload.get(
            "explicitly_suppressed_unknown_system_count", 0
        ),
        "semantic_fingerprint_sha256": _sha256(semantic),
    }


def _coverage_receipt(ir: ConnectivityIR) -> dict[str, Any]:
    by_code = Counter(gap.code for gap in ir.coverage.gaps)
    items = [asdict(gap) for gap in ir.coverage.gaps[:MAX_RESULT_GAP_ITEMS]]
    return {
        "status": ir.coverage.status.value,
        "files_total": ir.coverage.files_total,
        "files_projected": ir.coverage.files_projected,
        "diagnostic_count": ir.coverage.diagnostic_count,
        "blocking_diagnostic_count": ir.coverage.blocking_diagnostic_count,
        "gap_count": len(ir.coverage.gaps),
        "by_code": dict(sorted(by_code.items())),
        "items": _enum_values(items),
        "items_truncated": len(items) < len(ir.coverage.gaps),
    }


def _enum_values(value: Any) -> Any:
    from enum import Enum

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enum_values(item) for item in value]
    return value


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    expected: Any,
    actual: Any,
) -> None:
    checks.append(
        {
            "name": name,
            "passed": bool(passed),
            "expected": expected,
            "actual": actual,
        }
    )


def _match_satisfies(match: Any, expected: Mapping[str, Any]) -> bool:
    location = match.evidence.location
    return all(
        (
            (
                "instance_path" not in expected
                or match.instance_path == expected["instance_path"]
            ),
            (
                "file_suffix" not in expected
                or location.file.endswith(str(expected["file_suffix"]))
            ),
            "line" not in expected or location.line == expected["line"],
            (
                "boundary" not in expected
                or match.boundary.value == expected["boundary"]
            ),
            (
                "procedure_kind" not in expected
                or match.procedure_kind == expected["procedure_kind"]
            ),
            (
                "traversal_depth" not in expected
                or len(match.traversal) == expected["traversal_depth"]
            ),
            (
                "confidence" not in expected
                or match.confidence.value == expected["confidence"]
            ),
        )
    )


def _compact_query_result(result: Any) -> dict[str, Any]:
    matches = [
        {
            "instance_path": match.instance_path,
            "fact_id": match.fact_id,
            "kind": match.kind.value,
            "target": match.target.path(include_bits=True),
            "source": (
                match.source.path(include_bits=True)
                if match.source is not None
                else None
            ),
            "dependency_role": match.dependency_role,
            "dependency_count": len(match.dependencies),
            "dependency_sources": [
                dependency.source.path(include_bits=True)
                for dependency in match.dependencies
            ],
            "boundary": match.boundary.value,
            "procedure_kind": match.procedure_kind,
            "guard": match.guard,
            "generate_scope": match.generate_scope,
            "evidence": _enum_values(asdict(match.evidence)),
            "confidence": match.confidence.value,
            "traversal_depth": len(match.traversal),
            "traversal": [
                {
                    "edge_kind": hop.edge_kind.value,
                    "binding_style": hop.binding_style.value,
                    "binding_id": hop.binding_id,
                    "evidence": _enum_values(asdict(hop.evidence)),
                }
                for hop in match.traversal
            ],
        }
        for match in result.matches[:MAX_RESULT_QUERY_MATCHES]
    ]
    unresolved = [
        {"code": gap.code, "impact": gap.impact.value, "scopes": list(gap.scopes)}
        for gap in result.unresolved_boundaries[:MAX_RESULT_GAP_ITEMS]
    ]
    return {
        "operation": result.operation,
        "signal": result.signal.path(include_bits=True),
        "status": result.status.value,
        "coverage_status": result.coverage_status.value,
        "match_count": len(result.matches),
        "matches": matches,
        "matches_truncated": len(matches) < len(result.matches),
        "unresolved_boundary_count": len(result.unresolved_boundaries),
        "unresolved_boundaries": unresolved,
        "unresolved_boundaries_truncated": (
            len(unresolved) < len(result.unresolved_boundaries)
        ),
        "traversed_binding_edges": result.traversed_binding_edges,
        "max_depth": result.max_depth,
    }


def _feature_checks(
    ir: ConnectivityIR, workload: str, checks: list[dict[str, Any]]
) -> None:
    if workload == "deep_x_npi":
        _check(
            checks,
            "deep_definition_template_reuse_and_hierarchy",
            len(ir.instances) == 8,
            expected=8,
            actual=len(ir.instances),
        )
        positional = sum(binding.style.value == "positional" for binding in ir.bindings)
        _check(
            checks,
            "deep_positional_bindings_present",
            positional >= 28,
            expected=">=28",
            actual=positional,
        )
        return
    if workload == "opentitan_core":
        _check(
            checks,
            "opentitan_coverage_is_not_false_complete",
            ir.coverage.status is CoverageStatus.INCONCLUSIVE,
            expected="inconclusive",
            actual=ir.coverage.status.value,
        )
        _check(
            checks,
            "opentitan_blocking_diagnostics_preserved",
            ir.coverage.blocking_diagnostic_count == 65,
            expected=65,
            actual=ir.coverage.blocking_diagnostic_count,
        )
        required_gaps = {
            "uvm_dynamic_connectivity_not_modeled",
            "dpi_runtime_not_modeled",
            "procedural_force_not_modeled",
            "bind_semantics_incomplete",
            "protected_payload_not_modeled",
        }
        actual_gaps = {gap.code for gap in ir.coverage.gaps}
        _check(
            checks,
            "opentitan_runtime_exclusions_explicit",
            required_gaps <= actual_gaps,
            expected=sorted(required_gaps),
            actual=sorted(required_gaps & actual_gaps),
        )
        return

    styles = {binding.style.value for binding in ir.bindings}
    _check(
        checks,
        "hand_named_positional_modport_bindings",
        {"named", "positional", "modport"} <= styles,
        expected=["modport", "named", "positional"],
        actual=sorted(styles),
    )
    generated = {item.generate_scope for item in ir.instances if item.generate_scope}
    _check(
        checks,
        "hand_generate_instances",
        {"gen_lanes[0]", "gen_lanes[1]"} <= generated,
        expected=["gen_lanes[0]", "gen_lanes[1]"],
        actual=sorted(generated),
    )
    procedure_kinds = {
        assignment.procedure_kind
        for definition in ir.definitions
        for assignment in definition.assignments
        if assignment.procedure_kind
    }
    _check(
        checks,
        "hand_always_comb_and_ff",
        {"AlwaysComb", "AlwaysFF"} <= procedure_kinds,
        expected=["AlwaysComb", "AlwaysFF"],
        actual=sorted(procedure_kinds),
    )
    interface_definitions = [
        definition
        for definition in ir.definitions
        if definition.kind.value == "interface"
    ]
    _check(
        checks,
        "hand_interface_modports",
        any(len(definition.modports) >= 2 for definition in interface_definitions),
        expected=">=2 modports",
        actual=sum(len(item.modports) for item in interface_definitions),
    )
    interface = next(
        (
            definition
            for definition in interface_definitions
            if definition.name == "sg_bus_if"
        ),
        None,
    )
    interface_data = interface.direct_signal_range("data") if interface else None
    modport_directions = {
        f"{modport.name}.{member.name}": member.direction.value
        for modport in (interface.modports if interface else ())
        for member in modport.members
    }
    _check(
        checks,
        "hand_direction_and_bit_ranges",
        interface_data is not None
        and interface_data.width == 16
        and modport_directions.get("producer.data") == "output"
        and modport_directions.get("consumer.data") == "input",
        expected={
            "data_width": 16,
            "producer.data": "output",
            "consumer.data": "input",
        },
        actual={
            "data_width": interface_data.width if interface_data else None,
            "producer.data": modport_directions.get("producer.data"),
            "consumer.data": modport_directions.get("consumer.data"),
        },
    )
    exact_partial_maps = [
        dependency
        for definition in ir.definitions
        for assignment in definition.assignments
        for dependency in assignment.dependencies
        if dependency.exact_bit_mapping
        and dependency.source.bits != dependency.target.bits
    ]
    _check(
        checks,
        "hand_slice_concat_dependencies",
        bool(exact_partial_maps),
        expected=">=1 exact remapped dependency",
        actual=len(exact_partial_maps),
    )


def _run_correctness(
    ir: ConnectivityIR,
    engine: ConnectivityQueryEngine,
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], Any]]]:
    checks: list[dict[str, Any]] = []
    actual_paths = {instance.path for instance in ir.instances}
    expected_paths = set(spec["expected_instance_paths"])
    _check(
        checks,
        "expected_instance_paths",
        expected_paths <= actual_paths,
        expected=sorted(expected_paths),
        actual=sorted(expected_paths & actual_paths),
    )
    for oracle in spec["expected_locations"]:
        candidates = [
            definition.location
            for definition in ir.definitions
            if definition.name == oracle["definition"]
        ]
        matched = any(
            location.file.endswith(oracle["file_suffix"])
            and location.line == oracle["line"]
            for location in candidates
        )
        _check(
            checks,
            f"definition_location:{oracle['definition']}",
            matched,
            expected={
                "file_suffix": oracle["file_suffix"],
                "line": oracle["line"],
            },
            actual=[asdict(item) for item in candidates],
        )
    _feature_checks(ir, str(spec["workload"]), checks)

    query_results: list[tuple[dict[str, Any], Any]] = []
    for query in spec["query_oracles"]:
        try:
            result = (
                engine.query_driver(query["signal"])
                if query["operation"] == "driver"
                else engine.query_loads(query["signal"])
            )
            passed = result.status.value == query["expected"]["status"] and any(
                _match_satisfies(match, query["expected"]) for match in result.matches
            )
            actual = _compact_query_result(result)
        except Exception as exc:
            result = None
            passed = False
            actual = {"exception": f"{type(exc).__name__}: {exc}"}
        _check(
            checks,
            f"query:{query['name']}",
            passed,
            expected=query["expected"],
            actual=actual,
        )
        if result is not None:
            query_results.append((query, result))

    serialized = ir.to_json_bytes()
    restored = ConnectivityIR.from_json_bytes(serialized)
    _check(
        checks,
        "ir_roundtrip_fingerprint",
        restored.fingerprint_sha256() == ir.fingerprint_sha256(),
        expected=ir.fingerprint_sha256(),
        actual=restored.fingerprint_sha256(),
    )
    return {
        "all_passed": all(check["passed"] for check in checks),
        "passed_count": sum(check["passed"] for check in checks),
        "check_count": len(checks),
        "checks": checks,
    }, query_results


def _measure_warm_queries(
    engine: ConnectivityQueryEngine,
    query_results: Sequence[tuple[dict[str, Any], Any]],
    repeats: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for operation in ("driver", "load"):
        successful = [
            query
            for query, initial in query_results
            if query["operation"] == operation and initial.status is QueryStatus.FOUND
        ]
        wall_samples: list[float] = []
        cpu_samples: list[float] = []

        def run_group() -> None:
            for _ in range(repeats):
                for query in successful:
                    wall_started = time.perf_counter_ns()
                    cpu_started = time.process_time_ns()
                    current = (
                        engine.query_driver(query["signal"])
                        if operation == "driver"
                        else engine.query_loads(query["signal"])
                    )
                    cpu_samples.append(
                        (time.process_time_ns() - cpu_started) / 1_000_000
                    )
                    wall_samples.append(
                        (time.perf_counter_ns() - wall_started) / 1_000_000
                    )
                    if current.status is not QueryStatus.FOUND:
                        raise BenchmarkError(
                            f"warm {operation} query stopped returning found: "
                            f"{query['signal']}"
                        )

        _, phase = _measure_phase(run_group)
        result[operation] = {
            **phase,
            "successful_query_count": len(successful),
            "excluded_noop_count": sum(
                query["operation"] == operation
                and initial.status is not QueryStatus.FOUND
                for query, initial in query_results
            ),
            "repeat_count": repeats,
            "wall_latency_ms": _sample_summary(wall_samples),
            "cpu_latency_ms": _sample_summary(cpu_samples),
            "sample_policy": "only status=found queries; unsupported/no-op excluded",
        }
    return result


def run_worker(spec: Mapping[str, Any]) -> dict[str, Any]:
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    rss_start = _read_proc_rss_kib()
    phases: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []

    def import_frontend() -> tuple[Any, Any, str]:
        import pyslang
        from pyslang import driver as driver_module

        return pyslang, driver_module, importlib.metadata.version("pyslang")

    try:
        imported, phases["frontend_import"] = _measure_phase(import_frontend)
        _pyslang, driver_module, version = imported
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "workload": spec.get("workload"),
            "status": "blocked",
            "blockers": [
                {
                    "code": "frontend_unavailable",
                    "phase": "frontend_import",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
            "phases": phases,
        }
    if version != FRONTEND_VERSION:
        blockers.append(
            {
                "code": "frontend_version_mismatch",
                "phase": "frontend_import",
                "message": f"expected {FRONTEND_VERSION}, found {version}",
            }
        )

    def build_frontend() -> tuple[Any, Any, dict[str, Any]]:
        driver = _configure_driver(driver_module, spec["frontend_args"])
        if not driver.parseAllSources():
            raise BenchmarkError("Driver.parseAllSources returned false")
        compilation = driver.createCompilation()
        root = compilation.getRoot()
        diagnostics = list(compilation.getAllDiagnostics())
        return driver, root, _diagnostics_payload(driver, diagnostics)

    try:
        with _temporary_environment(spec.get("environment") or {}):
            built, phases["frontend_build"] = _measure_phase(build_frontend)
        driver, root, diagnostic_payload = built
        source_root = Path(str(spec["source_root"]))
        projection_options = ProjectionOptions(
            source_root=source_root,
            files_total=int(spec["source_facts"]["source_count"]),
            files_projected=int(spec["source_facts"]["existing_source_count"]),
            diagnostics=_projection_diagnostics(diagnostic_payload, source_root),
            diagnostic_total=int(diagnostic_payload["total"]),
            blocking_diagnostic_total=int(diagnostic_payload["blocking_error_count"]),
            exclusions=_projection_exclusions(spec),
            focus_instance_paths=tuple(spec.get("focus_instance_paths") or ()),
            assignment_instance_paths=tuple(
                spec.get("assignment_instance_paths") or ()
            ),
            metadata=(("workload", str(spec["workload"])),),
        )
        projection, phases["minimal_projection"] = _measure_phase(
            lambda: project_slang_design(
                root=root,
                source_manager=driver.sourceManager,
                frontend_version=version,
                options=projection_options,
            )
        )
        ir = projection.ir
        serialized, phases["serialization"] = _measure_phase(ir.to_json_bytes)
        engine, phases["query_index_build"] = _measure_phase(
            lambda: ConnectivityQueryEngine(ir)
        )
        correctness, initial_queries = _run_correctness(ir, engine, spec)
        phases["warm_queries"] = _measure_warm_queries(
            engine,
            initial_queries,
            int(spec["query_repeats"]),
        )
    except Exception as exc:
        blockers.append(
            {
                "code": "worker_measurement_failed",
                "phase": "measurement",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        rss_end = _read_proc_rss_kib()
        return {
            "schema_version": SCHEMA_VERSION,
            "benchmark": BENCHMARK_NAME,
            "workload": spec.get("workload"),
            "status": "blocked",
            "frontend": {"name": FRONTEND_NAME, "version": version},
            "input_fingerprint_sha256": spec.get("input_fingerprint_sha256"),
            "blockers": blockers,
            "phases": phases,
            "process_rss_kib": {
                "start": rss_start["current"],
                "peak": rss_end["high_water"],
                "end": rss_end["current"],
            },
        }

    rss_end = _read_proc_rss_kib()
    overall_wall_ms = (time.perf_counter_ns() - started_wall) / 1_000_000
    overall_cpu_ms = (time.process_time_ns() - started_cpu) / 1_000_000
    if not correctness["all_passed"]:
        blockers.append(
            {
                "code": "correctness_oracle_mismatch",
                "phase": "correctness",
                "message": "one or more hierarchy, source, feature, or query checks failed",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "workload": spec["workload"],
        "status": "measured" if not blockers else "measured_with_blockers",
        "frontend": {"name": FRONTEND_NAME, "version": version},
        "input_fingerprint_sha256": spec["input_fingerprint_sha256"],
        "phases": phases,
        "whole_worker": {
            "wall_time_ms": _round_ms(overall_wall_ms),
            "cpu_time_ms": _round_ms(overall_cpu_ms),
        },
        "process_rss_kib": {
            "start": rss_start["current"],
            "peak": rss_end["high_water"],
            "end": rss_end["current"],
        },
        "diagnostics": _diagnostic_receipt(diagnostic_payload),
        "projection_receipt": projection.receipt.to_dict(),
        "coverage": _coverage_receipt(ir),
        "ir": {
            "version": ir.ir_version,
            "stats": ir.stats(),
            "node_count": ir.stats()["node_count"],
            "edge_count": ir.stats()["edge_count"],
            "serialized_cache_bytes": len(serialized),
            "cache_scope": (
                "measurement-only serialized IR bytes; no production cache implemented"
            ),
            "fingerprint_sha256": ir.fingerprint_sha256(),
        },
        "correctness": correctness,
        "blockers": blockers,
    }


def _validate_worker_result(result: Mapping[str, Any], workload: str) -> None:
    if result.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkError("worker result schema version mismatch")
    if result.get("benchmark") != BENCHMARK_NAME:
        raise BenchmarkError("worker result benchmark name mismatch")
    if result.get("workload") != workload:
        raise BenchmarkError("worker result workload mismatch")
    if result.get("status") in {"measured", "measured_with_blockers"}:
        required = {
            "frontend_build",
            "minimal_projection",
            "serialization",
            "query_index_build",
            "warm_queries",
        }
        missing = required - set(result.get("phases", {}))
        if missing:
            raise BenchmarkError(
                f"worker result lacks measured phases: {sorted(missing)}"
            )
        fingerprint = result.get("ir", {}).get("fingerprint_sha256", "")
        if len(fingerprint) != 64:
            raise BenchmarkError("worker IR fingerprint is missing or malformed")


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def _launch_worker(
    frontend_python: Path,
    spec: Mapping[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="traceweave-phase1a-") as temp_dir:
        spec_path = Path(temp_dir) / "worker-spec.json"
        spec_path.write_bytes(_json_bytes(spec))
        command = [
            str(frontend_python),
            str(Path(__file__).resolve()),
            "--worker-spec",
            str(spec_path),
        ]
        started = time.perf_counter_ns()
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            return {
                "schema_version": SCHEMA_VERSION,
                "benchmark": BENCHMARK_NAME,
                "workload": spec["workload"],
                "status": "blocked",
                "worker_process_wall_time_ms": _round_ms(elapsed_ms),
                "blockers": [
                    {
                        "code": "worker_timeout",
                        "phase": "worker_process",
                        "message": f"worker exceeded {timeout_seconds:g}s and was reaped",
                    }
                ],
                "worker_model": {
                    "isolated_process": True,
                    "timeout_reaped": True,
                    "fallback_used": False,
                },
            }
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        try:
            result = json.loads(stdout)
            _validate_worker_result(result, str(spec["workload"]))
        except (AttributeError, TypeError, json.JSONDecodeError, BenchmarkError):
            result = None
        if result is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "benchmark": BENCHMARK_NAME,
                "workload": spec["workload"],
                "status": "blocked",
                "worker_process_wall_time_ms": _round_ms(elapsed_ms),
                "blockers": [
                    {
                        "code": "worker_exit_failure",
                        "phase": "worker_process",
                        "message": (
                            f"worker exited with status {process.returncode} "
                            "without a valid structured result"
                        ),
                    }
                ],
                "worker_stderr_tail": stderr[-4000:],
                "worker_model": {
                    "isolated_process": True,
                    "timeout_reaped": False,
                    "fallback_used": False,
                },
            }
        result["worker_process_wall_time_ms"] = _round_ms(elapsed_ms)
        result["worker_exit_status"] = process.returncode
        result["worker_stderr_tail"] = stderr[-4000:]
        result["worker_model"] = {
            "isolated_process": True,
            "timeout_reaped": False,
            "fallback_used": False,
        }
        return result


def _aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    measured = [
        run for run in runs if str(run.get("status", "")).startswith("measured")
    ]
    worker_wall = [float(run["worker_process_wall_time_ms"]) for run in measured]
    peak_rss = [
        float(run["process_rss_kib"]["peak"])
        for run in measured
        if run.get("process_rss_kib", {}).get("peak") is not None
    ]
    fingerprints = [run["ir"]["fingerprint_sha256"] for run in measured]
    warm: dict[str, Any] = {}
    for operation in ("driver", "load"):
        wall = [
            float(sample)
            for run in measured
            for sample in run["phases"]["warm_queries"][operation]["wall_latency_ms"][
                "samples"
            ]
        ]
        cpu = [
            float(sample)
            for run in measured
            for sample in run["phases"]["warm_queries"][operation]["cpu_latency_ms"][
                "samples"
            ]
        ]
        warm[operation] = {
            "wall_latency_ms": _sample_summary(wall, include_samples=False),
            "cpu_latency_ms": _sample_summary(cpu, include_samples=False),
            "successful_samples_only": True,
        }
    return {
        "requested_run_count": len(runs),
        "measured_run_count": len(measured),
        "cold_worker_wall_ms": _sample_summary(worker_wall, include_samples=True),
        "peak_rss_kib": _sample_summary(peak_rss, include_samples=True),
        "warm_queries": warm,
        "ir_fingerprints": fingerprints,
        "ir_fingerprint_stable": bool(fingerprints) and len(set(fingerprints)) == 1,
        "correctness_all_runs": bool(measured)
        and all(run["correctness"]["all_passed"] for run in measured),
    }


def _workload_gate(name: str, aggregate: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "all_requested_runs_measured",
        aggregate["measured_run_count"] == aggregate["requested_run_count"],
        expected=aggregate["requested_run_count"],
        actual=aggregate["measured_run_count"],
    )
    _check(
        checks,
        "correctness_all_runs",
        bool(aggregate["correctness_all_runs"]),
        expected=True,
        actual=aggregate["correctness_all_runs"],
    )
    _check(
        checks,
        "stable_ir_fingerprint",
        bool(aggregate["ir_fingerprint_stable"]),
        expected=True,
        actual=aggregate["ir_fingerprint_stable"],
    )
    for operation in ("driver", "load"):
        p95 = aggregate["warm_queries"][operation]["wall_latency_ms"]["p95"]
        _check(
            checks,
            f"warm_{operation}_p95",
            p95 is not None and p95 <= GATE_TARGETS["warm_successful_query_p95_max_ms"],
            expected=f"<={GATE_TARGETS['warm_successful_query_p95_max_ms']}ms",
            actual=p95,
        )
    if name == "opentitan_core":
        cold = aggregate["cold_worker_wall_ms"]["p50"]
        peak = aggregate["peak_rss_kib"]["p50"]
        _check(
            checks,
            "opentitan_cold_prepare_p50",
            cold is not None and cold <= GATE_TARGETS["cold_prepare_preferred_max_ms"],
            expected=f"<={GATE_TARGETS['cold_prepare_preferred_max_ms']}ms",
            actual=cold,
        )
        _check(
            checks,
            "opentitan_peak_rss_p50",
            peak is not None and peak <= GATE_TARGETS["peak_rss_max_kib"],
            expected=f"<={GATE_TARGETS['peak_rss_max_kib']}KiB",
            actual=peak,
        )
    return {
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if args.cold_repeats < 1 or args.query_repeats < 1:
        raise BenchmarkError("repeat counts must be positive")
    if args.worker_timeout_seconds <= 0:
        raise BenchmarkError("worker timeout must be positive")
    specs = build_workload_specs(args)
    workloads: list[dict[str, Any]] = []
    for spec in specs:
        runs = [
            _launch_worker(
                args.frontend_python,
                spec,
                args.worker_timeout_seconds,
            )
            for _ in range(args.cold_repeats)
        ]
        aggregate = _aggregate_runs(runs)
        workloads.append(
            {
                "name": spec["workload"],
                "input_fingerprint_sha256": spec["input_fingerprint_sha256"],
                "source_facts": spec["source_facts"],
                "oracle_receipt": spec["oracle_receipt"],
                "projection_scope": {
                    "focus_instance_paths": spec["focus_instance_paths"],
                    "assignment_instance_paths": spec["assignment_instance_paths"],
                    "coverage_policy": (
                        "focused OpenTitan scope is globally inconclusive"
                        if spec["focus_instance_paths"]
                        else "complete tracked fixture projection required"
                    ),
                },
                "runs": runs,
                "aggregate": aggregate,
                "gate": _workload_gate(str(spec["workload"]), aggregate),
            }
        )
    required = {"deep_x_npi", "hand_fixture", "opentitan_core"}
    measured_names = {item["name"] for item in workloads}
    all_required_present = required <= measured_names
    all_required_measured = all_required_present and all(
        item["aggregate"]["measured_run_count"]
        == item["aggregate"]["requested_run_count"]
        for item in workloads
        if item["name"] in required
    )
    workload_gates_pass = all(item["gate"]["passed"] for item in workloads)
    decision = (
        "go_for_production_integration_review"
        if all_required_measured and workload_gates_pass
        else "no_go_or_incomplete_keep_npi_legacy_route"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "root": str(ROOT),
            "head": _git_head(ROOT),
        },
        "frontend": {
            "name": FRONTEND_NAME,
            "required_version": FRONTEND_VERSION,
            "interpreter": str(args.frontend_python),
            "dependency_model": "optional pinned isolated environment",
        },
        "worker_model": {
            "cold_build": "one fresh isolated process per repeat",
            "max_concurrent_cold_builds": 1,
            "persistent_worker_measured": False,
            "production_cache_implemented": False,
            "fallback_used": False,
            "timeout_seconds": args.worker_timeout_seconds,
        },
        "measurement_policy": {
            "cold_prepare_gate_metric": "parent-observed fresh worker process wall",
            "phase_metrics": "worker perf_counter/process_time and /proc RSS",
            "warm_query_metric": "successful status=found calls only; result serialization excluded",
            "cache_bytes": "deterministic serialized internal IR bytes",
            "phase0b_comparison": PHASE0B_REFERENCE,
        },
        "gate_targets": GATE_TARGETS,
        "workloads": workloads,
        "assessment": {
            "decision": decision,
            "all_required_workloads_present": all_required_present,
            "all_required_workloads_measured": all_required_measured,
            "all_measured_workload_gates_passed": workload_gates_pass,
            "production_route_changed": False,
            "next_step": (
                "await explicit approval before any production integration"
                if decision == "go_for_production_integration_review"
                else "retain NPI -> Legacy Static and inspect recorded blockers"
            ),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.worker_spec is not None:
        try:
            spec = json.loads(args.worker_spec.read_text(encoding="utf-8"))
            result = run_worker(spec)
        except Exception as exc:
            result = {
                "schema_version": SCHEMA_VERSION,
                "benchmark": BENCHMARK_NAME,
                "workload": None,
                "status": "blocked",
                "blockers": [
                    {
                        "code": "worker_bootstrap_failure",
                        "phase": "worker_bootstrap",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                ],
            }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0 if str(result.get("status", "")).startswith("measured") else 2

    try:
        result = run_benchmark(args)
    except Exception as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
