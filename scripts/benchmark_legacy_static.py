#!/usr/bin/env python3
"""Reproducible correctness/performance baseline for Legacy Static connectivity.

The default workload is the locally generated ``deep_x_npi`` compile log.  It
does not open the waveform and it never probes or invokes Verdi/NPI: the
``wave_path`` is carried only because ``explain_signal_driver`` includes that
identity in its existing result contract.

Two execution modes are measured separately:

* cold process: one query in each fresh Python interpreter.  Query-only timing
  is reported alongside end-to-end process wall time (startup/import included).
* same process: repeated queries against one ``StaticConnectivityBackend``
  instance.  Legacy Static is intentionally stateless, so this exposes the
  current re-parse/re-scan cost on every driver/load query.

The JSON result contains local workload identities by design.  It is benchmark
output, not privacy-safe operation telemetry; this script never writes to
``src.operation_metrics`` or usage telemetry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.compile_log_parser import parse_compile_log
from src.connectivity_backend import StaticConnectivityBackend


SCHEMA_VERSION = "1.0"
BENCHMARK_NAME = "legacy_static_connectivity"
OUTCOME_TAXONOMY = ("resolved", "partial", "unsupported")
OPERATIONS = (
    "explain_signal_driver",
    "find_signal_loads",
    "trace_signal_path",
)

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "deep_x_npi"
DEFAULT_COMPILE_LOG = DEFAULT_FIXTURE / "work" / "compile.log"
DEFAULT_WAVE_PATH = DEFAULT_FIXTURE / "work" / "deep_x.fsdb"
DEFAULT_TOP = "uart_deep_x_tb"
DEFAULT_DRIVER_SIGNAL = f"{DEFAULT_TOP}.apb_prdata[7:0]"
DEFAULT_LOAD_SIGNAL = DEFAULT_DRIVER_SIGNAL
DEFAULT_PATH_FROM = f"{DEFAULT_TOP}.pclk"
DEFAULT_PATH_TO = DEFAULT_DRIVER_SIGNAL


class BenchmarkError(RuntimeError):
    """Base class for a machine-classifiable benchmark failure."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class BenchmarkInputError(BenchmarkError):
    """The requested workload cannot establish a real Static baseline."""


class BenchmarkExecutionError(BenchmarkError):
    """A child process or connectivity query failed."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BenchmarkInputError("invalid_arguments", message)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return value


def _nonempty(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        description=(
            "Measure the current Legacy Static driver/load/path behavior in fresh "
            "processes and through repeated same-process queries."
        )
    )
    parser.add_argument("--compile-log", type=Path, default=DEFAULT_COMPILE_LOG)
    parser.add_argument(
        "--wave-path",
        type=Path,
        default=DEFAULT_WAVE_PATH,
        help="Identity-only for the Static driver result; the file is not opened.",
    )
    parser.add_argument("--simulator", choices=("auto", "vcs", "xcelium"), default="vcs")
    parser.add_argument("--top", type=_nonempty, default=DEFAULT_TOP)
    parser.add_argument("--driver-signal", type=_nonempty, default=DEFAULT_DRIVER_SIGNAL)
    parser.add_argument("--load-signal", type=_nonempty, default=DEFAULT_LOAD_SIGNAL)
    parser.add_argument("--path-from", type=_nonempty, default=DEFAULT_PATH_FROM)
    parser.add_argument("--path-to", type=_nonempty, default=DEFAULT_PATH_TO)
    parser.add_argument("--driver-max-depth", type=_positive_int, default=20)
    parser.add_argument("--load-max-depth", type=_positive_int, default=1)
    parser.add_argument("--cold-repeats", type=_positive_int, default=7)
    parser.add_argument("--same-process-repeats", type=_positive_int, default=50)
    parser.add_argument("--child-timeout-seconds", type=_positive_float, default=30.0)

    # Private child protocol.  Keeping it on the same executable guarantees the
    # cold and repeated measurements import exactly the same implementation.
    parser.add_argument(
        "--_worker-mode", choices=("single", "series"), help=argparse.SUPPRESS
    )
    parser.add_argument("--_operation", choices=OPERATIONS, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-repeats", type=_positive_int, help=argparse.SUPPRESS)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_argument_parser().parse_args(argv)


def _resolved_path(path: Path) -> Path:
    return path.expanduser().resolve()


def validate_workload(args: argparse.Namespace) -> dict[str, Any]:
    compile_log = _resolved_path(args.compile_log)
    if not compile_log.is_file() or compile_log.stat().st_size == 0:
        raise BenchmarkInputError(
            "fixture_missing",
            "Legacy Static benchmark compile log is missing or empty: "
            f"{compile_log}. Generate the local fixture with "
            f"{DEFAULT_FIXTURE / 'run.sh'} or pass --compile-log.",
        )

    try:
        compile_result = parse_compile_log(str(compile_log), args.simulator)
    except Exception as exc:
        raise BenchmarkInputError(
            "fixture_invalid",
            f"could not parse benchmark compile log {compile_log}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc

    source_entries = compile_result.get("files", {}).get("user", [])
    source_paths = [
        _resolved_path(Path(entry["path"]))
        for entry in source_entries
        if isinstance(entry, dict) and entry.get("path")
    ]
    if not source_paths:
        warnings = compile_result.get("parse_warnings") or []
        suffix = f" Parser warnings: {warnings}" if warnings else ""
        raise BenchmarkInputError(
            "fixture_invalid",
            f"compile log recovered no source files: {compile_log}.{suffix}",
        )

    missing_sources = [
        path for path in source_paths if not path.is_file() or path.stat().st_size == 0
    ]
    if missing_sources:
        rendered = ", ".join(str(path) for path in missing_sources)
        raise BenchmarkInputError(
            "fixture_missing",
            f"compile-log source fixture(s) are missing or empty: {rendered}",
        )

    recovered_tops = list(compile_result.get("top_modules") or [])
    if recovered_tops and args.top not in recovered_tops:
        raise BenchmarkInputError(
            "fixture_invalid",
            f"requested top {args.top!r} is not in compile-log tops {recovered_tops!r}",
        )

    wave_path = _resolved_path(args.wave_path)
    return {
        "compile_log": str(compile_log),
        "compile_log_bytes": compile_log.stat().st_size,
        "wave_path": str(wave_path),
        "wave_path_role": "identity_only_not_opened",
        "simulator": args.simulator,
        "top": args.top,
        "driver_signal": args.driver_signal,
        "load_signal": args.load_signal,
        "path_from": args.path_from,
        "path_to": args.path_to,
        "driver_recursive": False,
        "driver_max_depth": args.driver_max_depth,
        "load_max_depth_requested": args.load_max_depth,
        "load_max_depth_effective": 1,
        "source_paths": [str(path) for path in source_paths],
        "source_count": len(source_paths),
        "source_bytes_total": sum(path.stat().st_size for path in source_paths),
        "recovered_tops": recovered_tops,
        "parse_warning_count": len(compile_result.get("parse_warnings") or []),
    }


def _read_proc_rss_kib() -> dict[str, int | None]:
    values: dict[str, int | None] = {"current": None, "high_water": None}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    values["current"] = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    values["high_water"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return values


def _round_ms(value: float) -> float:
    return round(value, 6)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _path_aliases(workload: dict[str, Any]) -> dict[str, str]:
    aliases = {
        workload["compile_log"]: "<compile_log>",
        workload["wave_path"]: "<wave_path>",
    }
    for index, path in enumerate(workload["source_paths"]):
        aliases[path] = f"<source:{index:03d}:{Path(path).name}>"
    return aliases


def _normalize_result_paths(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_result_paths(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_result_paths(item, aliases) for item in value]
    if isinstance(value, str):
        return aliases.get(value, value)
    return value


def result_artifact(result: dict[str, Any], workload: dict[str, Any]) -> dict[str, Any]:
    serialized = _canonical_json_bytes(result)
    normalized = _normalize_result_paths(result, _path_aliases(workload))
    fingerprint = hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()
    return {
        "size_bytes": len(serialized),
        "fingerprint_sha256": fingerprint,
    }


def _source_locations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for item in items:
        file_path = item.get("source_file")
        line = item.get("source_line")
        if file_path is None and line is None:
            continue
        key = (file_path, line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "line": line})
    return locations


def summarize_correctness(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    if operation == "explain_signal_driver":
        chain = list(result.get("driver_chain") or [])
        depth = max((int(hop.get("depth", 0)) for hop in chain), default=0)
        locations = _source_locations([result, *chain])
        driver_status = result.get("driver_status")
        outcome = (
            "resolved"
            if driver_status == "resolved"
            else "unsupported" if driver_status == "unsupported" else "partial"
        )
        return {
            "outcome": outcome,
            "depth": depth,
            "source_file": result.get("source_file"),
            "source_line": result.get("source_line"),
            "source_locations": locations,
            "coverage": {
                "engine": "legacy_static_regex",
                "scope": "single_hop",
                "driver_kind": result.get("driver_kind"),
                "driver_status": driver_status,
                "confidence": result.get("confidence"),
                "stopped_at": result.get("stopped_at"),
                "chain_hops": len(chain),
            },
            "unsupported_reason": result.get("unsupported_reason"),
        }

    if operation == "find_signal_loads":
        loads = list(result.get("loads") or [])
        unsupported_reason = result.get("unsupported_reason")
        if unsupported_reason:
            outcome = "unsupported"
        elif result.get("completeness") == "exact":
            outcome = "resolved"
        else:
            outcome = "partial"
        locations = _source_locations(loads)
        first_location = locations[0] if locations else {"file": None, "line": None}
        return {
            "outcome": outcome,
            "depth": 1,
            "source_file": first_location["file"],
            "source_line": first_location["line"],
            "source_locations": locations,
            "coverage": {
                "engine": "legacy_static_regex",
                "completeness": result.get("completeness"),
                "max_depth_effective": 1,
                "loads_returned": len(loads),
                "stopped_at": result.get("stopped_at"),
            },
            "unsupported_reason": unsupported_reason,
        }

    if operation == "trace_signal_path":
        path = list(result.get("path") or [])
        unsupported_reason = result.get("unsupported_reason")
        outcome = (
            "resolved"
            if result.get("found")
            else "unsupported" if unsupported_reason else "partial"
        )
        locations = _source_locations(path)
        first_location = locations[0] if locations else {"file": None, "line": None}
        return {
            "outcome": outcome,
            "depth": int(result.get("hops", 0)),
            "source_file": first_location["file"],
            "source_line": first_location["line"],
            "source_locations": locations,
            "coverage": {
                "engine": "legacy_static_regex",
                "found": bool(result.get("found")),
                "path_nodes": len(path),
                "hops": int(result.get("hops", 0)),
                "expand_assigns": bool(result.get("expand_assigns")),
            },
            "unsupported_reason": unsupported_reason,
        }

    raise BenchmarkExecutionError("unknown_operation", f"unknown operation: {operation}")


def _operation_runner(
    backend: StaticConnectivityBackend,
    operation: str,
    workload: dict[str, Any],
) -> Callable[[], dict[str, Any]]:
    if operation == "explain_signal_driver":
        return lambda: backend.find_driver(
            signal_path=workload["driver_signal"],
            wave_path=workload["wave_path"],
            compile_log=workload["compile_log"],
            top_hint=workload["top"],
            recursive=False,
            max_depth=workload["driver_max_depth"],
            simulator=workload["simulator"],
        )
    if operation == "find_signal_loads":
        return lambda: backend.find_loads(
            signal_path=workload["load_signal"],
            compile_log=workload["compile_log"],
            top_hint=workload["top"],
            max_depth=workload["load_max_depth_requested"],
            simulator=workload["simulator"],
        )
    if operation == "trace_signal_path":
        return lambda: backend.find_path(
            workload["path_from"],
            workload["path_to"],
            compile_log=workload["compile_log"],
            top_hint=workload["top"],
            expand_assigns=False,
            simulator=workload["simulator"],
        )
    raise BenchmarkExecutionError("unknown_operation", f"unknown operation: {operation}")


def run_same_process_series(
    operation: str,
    repeats: int,
    workload: dict[str, Any],
) -> dict[str, Any]:
    backend = StaticConnectivityBackend()
    query = _operation_runner(backend, operation, workload)
    process_rss_start = _read_proc_rss_kib()
    series_wall_started = time.perf_counter_ns()
    series_cpu_started = time.process_time_ns()
    samples: list[dict[str, Any]] = []
    baseline_result: dict[str, Any] | None = None

    for _ in range(repeats):
        rss_before = _read_proc_rss_kib()
        wall_started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        result = query()
        cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
        wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
        rss_after = _read_proc_rss_kib()
        artifact = result_artifact(result, workload)
        samples.append(
            {
                "wall_time_ms": _round_ms(wall_ms),
                "cpu_time_ms": _round_ms(cpu_ms),
                "rss_start_kib": rss_before["current"],
                "rss_peak_kib": rss_after["high_water"],
                "rss_end_kib": rss_after["current"],
                **artifact,
            }
        )
        if baseline_result is None:
            baseline_result = result

    series_cpu_ms = (time.process_time_ns() - series_cpu_started) / 1_000_000
    series_wall_ms = (time.perf_counter_ns() - series_wall_started) / 1_000_000
    process_rss_end = _read_proc_rss_kib()
    assert baseline_result is not None
    return {
        "operation": operation,
        "repeat_count": repeats,
        "series_wall_time_ms": _round_ms(series_wall_ms),
        "series_cpu_time_ms": _round_ms(series_cpu_ms),
        "process_rss_kib": {
            "start": process_rss_start["current"],
            "peak": process_rss_end["high_water"],
            "end": process_rss_end["current"],
        },
        "samples": samples,
        "correctness": summarize_correctness(operation, baseline_result),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = percentile / 100.0 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * fraction)


def _distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(ordered),
        "min": _round_ms(ordered[0]),
        "p50": _round_ms(_percentile(ordered, 50)),
        "p95": _round_ms(_percentile(ordered, 95)),
        "max": _round_ms(ordered[-1]),
    }


def _optional_distribution(values: list[int | None]) -> dict[str, Any]:
    supported = [int(value) for value in values if value is not None]
    if not supported:
        return {
            "supported": False,
            "count": len(values),
            "min": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    distribution = _distribution([float(value) for value in supported])
    return {"supported": len(supported) == len(values), **distribution}


def _summarize_operation(
    workers: list[dict[str, Any]],
    process_wall_times_ms: list[float],
    process_cpu_times_ms: list[float],
) -> dict[str, Any]:
    samples = [sample for worker in workers for sample in worker["samples"]]
    fingerprints = [sample["fingerprint_sha256"] for sample in samples]
    sizes = [int(sample["size_bytes"]) for sample in samples]
    correctness = [worker["correctness"] for worker in workers]
    stable = (
        len(set(fingerprints)) == 1
        and len(set(sizes)) == 1
        and all(item == correctness[0] for item in correctness)
    )
    return {
        "sample_count": len(samples),
        "query_wall_time_ms": _distribution(
            [sample["wall_time_ms"] for sample in samples]
        ),
        "query_cpu_time_ms": _distribution(
            [sample["cpu_time_ms"] for sample in samples]
        ),
        "process_wall_time_ms": _distribution(process_wall_times_ms),
        "process_cpu_time_ms": _distribution(process_cpu_times_ms),
        "rss_kib": {
            "start": _optional_distribution(
                [worker["process_rss_kib"]["start"] for worker in workers]
            ),
            "peak": _optional_distribution(
                [worker["process_rss_kib"]["peak"] for worker in workers]
            ),
            "end": _optional_distribution(
                [worker["process_rss_kib"]["end"] for worker in workers]
            ),
        },
        "series_wall_time_ms": _distribution(
            [worker["series_wall_time_ms"] for worker in workers]
        ),
        "series_cpu_time_ms": _distribution(
            [worker["series_cpu_time_ms"] for worker in workers]
        ),
        "result": {
            "size_bytes": sizes[0],
            "fingerprint_sha256": fingerprints[0],
            "stable_across_samples": stable,
            "fingerprint_policy": (
                "sha256 of sorted compact JSON after exact compile/wave/source paths "
                "are replaced with deterministic aliases"
            ),
            "correctness": correctness[0],
        },
        "samples": samples,
    }


def _worker_command(
    args: argparse.Namespace,
    operation: str,
    mode: str,
    repeats: int,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--compile-log",
        str(_resolved_path(args.compile_log)),
        "--wave-path",
        str(_resolved_path(args.wave_path)),
        "--simulator",
        args.simulator,
        "--top",
        args.top,
        "--driver-signal",
        args.driver_signal,
        "--load-signal",
        args.load_signal,
        "--path-from",
        args.path_from,
        "--path-to",
        args.path_to,
        "--driver-max-depth",
        str(args.driver_max_depth),
        "--load-max-depth",
        str(args.load_max_depth),
        "--_worker-mode",
        mode,
        "--_operation",
        operation,
        "--_worker-repeats",
        str(repeats),
    ]


def _invoke_worker(
    args: argparse.Namespace,
    operation: str,
    mode: str,
    repeats: int,
) -> tuple[dict[str, Any], float, float]:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    child_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter_ns()
    try:
        process = subprocess.run(
            _worker_command(args, operation, mode, repeats),
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=args.child_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkExecutionError(
            "worker_timeout",
            f"{mode} worker for {operation} exceeded "
            f"{args.child_timeout_seconds:g}s",
        ) from exc
    process_wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    child_usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    process_cpu_ms = 1_000.0 * (
        child_usage_after.ru_utime
        + child_usage_after.ru_stime
        - child_usage_before.ru_utime
        - child_usage_before.ru_stime
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no child output"
        raise BenchmarkExecutionError(
            "worker_failed",
            f"{mode} worker for {operation} exited {process.returncode}: {detail}",
        )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise BenchmarkExecutionError(
            "worker_protocol_error",
            f"{mode} worker for {operation} returned invalid JSON: {exc}",
        ) from exc
    return payload, _round_ms(process_wall_ms), _round_ms(process_cpu_ms)


def _environment_receipt() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "machine": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "rss_source": "/proc/self/status VmRSS/VmHWM (KiB)",
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    workload = validate_workload(args)
    cold_workers: dict[str, list[dict[str, Any]]] = {
        operation: [] for operation in OPERATIONS
    }
    cold_process_wall: dict[str, list[float]] = {
        operation: [] for operation in OPERATIONS
    }
    cold_process_cpu: dict[str, list[float]] = {
        operation: [] for operation in OPERATIONS
    }

    # Alternate operation order per repeat to reduce a fixed thermal/order bias.
    for repeat in range(args.cold_repeats):
        order = OPERATIONS if repeat % 2 == 0 else tuple(reversed(OPERATIONS))
        for operation in order:
            worker, process_wall_ms, process_cpu_ms = _invoke_worker(
                args, operation, "single", 1
            )
            cold_workers[operation].append(worker)
            cold_process_wall[operation].append(process_wall_ms)
            cold_process_cpu[operation].append(process_cpu_ms)

    same_process_workers: dict[str, list[dict[str, Any]]] = {}
    same_process_wall: dict[str, list[float]] = {}
    same_process_cpu: dict[str, list[float]] = {}
    for operation in OPERATIONS:
        worker, process_wall_ms, process_cpu_ms = _invoke_worker(
            args, operation, "series", args.same_process_repeats
        )
        same_process_workers[operation] = [worker]
        same_process_wall[operation] = [process_wall_ms]
        same_process_cpu[operation] = [process_cpu_ms]

    cold = {
        operation: _summarize_operation(
            cold_workers[operation],
            cold_process_wall[operation],
            cold_process_cpu[operation],
        )
        for operation in OPERATIONS
    }
    same_process = {
        operation: _summarize_operation(
            same_process_workers[operation],
            same_process_wall[operation],
            same_process_cpu[operation],
        )
        for operation in OPERATIONS
    }
    stable = all(
        cold[operation]["result"]["stable_across_samples"]
        and same_process[operation]["result"]["stable_across_samples"]
        and cold[operation]["result"]["fingerprint_sha256"]
        == same_process[operation]["result"]["fingerprint_sha256"]
        and cold[operation]["result"]["correctness"]
        == same_process[operation]["result"]["correctness"]
        for operation in OPERATIONS
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "status": "ok" if stable else "unstable_results",
        "backend": "static",
        "backend_selection": "forced_direct_no_probe_no_npi",
        "correctness_outcome_taxonomy": list(OUTCOME_TAXONOMY),
        "environment": _environment_receipt(),
        "workload": {
            **workload,
            "cold_repeats": args.cold_repeats,
            "same_process_repeats": args.same_process_repeats,
            "operations": list(OPERATIONS),
        },
        "methodology": {
            "cold_process": (
                "one query per fresh interpreter; query timing excludes process "
                "startup/import; process_wall_time_ms and process_cpu_time_ms "
                "include them"
            ),
            "same_process": (
                "all repeats for one operation share one interpreter and one "
                "StaticConnectivityBackend instance"
            ),
            "rss": (
                "start/end are current VmRSS; peak is process-lifetime VmHWM; "
                "fresh workers isolate each cold query and each repeated series"
            ),
            "telemetry": (
                "benchmark JSON may contain workload identities; no values are "
                "written to privacy-safe operation metrics or usage telemetry"
            ),
        },
        "modes": {
            "cold_process": {"operations": cold},
            "same_process": {"operations": same_process},
        },
        "stable_across_modes": stable,
    }


def _error_payload(exc: BenchmarkError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "status": "error",
        "error_code": exc.error_code,
        "message": str(exc),
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args._worker_mode is not None:
            if args._operation is None or args._worker_repeats is None:
                raise BenchmarkInputError(
                    "invalid_arguments",
                    "private worker mode requires --_operation and --_worker-repeats",
                )
            workload = validate_workload(args)
            payload = run_same_process_series(
                args._operation, args._worker_repeats, workload
            )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0

        result = run_benchmark(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["stable_across_modes"] else 3
    except BenchmarkError as exc:
        print(
            json.dumps(_error_payload(exc), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        wrapped = BenchmarkExecutionError(
            "benchmark_failed", f"{type(exc).__name__}: {exc}"
        )
        print(
            json.dumps(_error_payload(wrapped), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
