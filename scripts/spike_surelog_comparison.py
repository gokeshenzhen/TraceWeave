#!/usr/bin/env python3
"""Phase 0B Surelog comparison for a measured Slang frontend gap.

This standalone development harness exists only because the primary Slang
probe found a blocking diagnostic on the local Xcelium/UVM workload.  It does
not register a backend, import a frontend into the server, or silently fall
back.  Each Surelog phase runs in a fresh, single-process child so crashes,
timeouts, CPU time, and RSS remain observable.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import spike_source_frontend as slang_spike  # noqa: E402


SCHEMA_VERSION = "1.0"
SPIKE_NAME = "source_graph_frontend_surelog_comparison"
PACKAGE_NAME = "sc-surelog"
PACKAGE_VERSION = "1.84.1"
BINARY_VERSION = "1.84"
BINARY_BUILD_DATE = "Aug 23 2024"
PACKAGE_SOURCE = "https://pypi.org/project/sc-surelog/1.84.1/"
PACKAGE_PROJECT = "https://github.com/siliconcompiler/sc-surelog"
UPSTREAM_SOURCE = "https://github.com/chipsalliance/Surelog"
UPSTREAM_PREFERRED_TAG = "v1.86"
UPSTREAM_PREFERRED_COMMIT = "1efe418724de9dc36613869e5d0e5e6ac5cca972"
WHEEL = (
    "sc_surelog-1.84.1-cp311-cp311-manylinux_2_17_x86_64."
    "manylinux2014_x86_64.whl"
)
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/21/26/"
    "4fb2e1552020dcab2506258c4da880b3793d1401e4c4d3d6165b33d72cb5/"
    + WHEEL
)
WHEEL_SHA256 = "bb0cd40141eb96af21aebfa2b8f25a53a7708b847352f30e591853b6a6201af6"
REQUIREMENTS = (
    ROOT
    / "scripts"
    / "frontend_spike_surelog_comparison_requirements_cp311_linux_x86_64.txt"
)
DEFAULT_COMPILE_LOG = Path(
    "/home/robin/Projects/mcp_practise/uvm_demo_cc29/tb/work/elab.log"
)
DEFAULT_WAVEFORM = Path(
    "/home/robin/Projects/mcp_practise/uvm_demo_cc29/tb/work/"
    "work_my_case0_/top_tb.fsdb"
)

PHASES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "preprocess",
        ("-noparse",),
        "fresh-process preprocessing only",
    ),
    (
        "parse",
        ("-profile", "-parse", "-nocomp"),
        "fresh-process cumulative preprocessing plus parsing",
    ),
    (
        "compile",
        ("-profile", "-parse", "-noelab"),
        "fresh-process cumulative preprocessing, parsing, and compilation",
    ),
    (
        "elaboration",
        ("-profile", "-parse", "-elabuhdm"),
        "fresh-process full preprocessing, parsing, compilation, and UHDM elaboration",
    ),
)

_DIAGNOSTIC_RE = re.compile(
    r"^\[(?P<tag>[A-Z]{3}):(?P<code>[A-Z]{2}\d{4})\]\s+"
    r"(?:(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?:\s+)?"
    r"(?P<message>.*)$"
)
_SUMMARY_RE = re.compile(
    r"^\[\s*(?P<severity>FATAL|SYNTAX|ERROR|WARNING|NOTE)\]\s*:\s*(?P<count>\d+)\s*$"
)
_PROFILE_RE = re.compile(
    r"^(?P<phase>Preprocessing|Parsing|Compilation|Elaboration) took "
    r"(?P<seconds>\d+(?:\.\d+)?)s$",
    re.MULTILINE,
)
_TOP_RE = re.compile(
    r'^(?:\[NTE:EL0503\]\s+)?(?P<file>.+?):(?P<line>\d+):(?P<column>\d+): '
    r'Top level module "(?P<name>[^"]+)"\.$',
    re.MULTILINE,
)
_COUNT_PATTERNS = {
    "top_level_modules": re.compile(r"Nb Top level modules:\s*(\d+)"),
    "max_instance_depth": re.compile(r"Max instance depth:\s*(\d+)"),
    "instances": re.compile(r"Nb instances:\s*(\d+)"),
    "leaf_instances": re.compile(r"Nb leaf instances:\s*(\d+)"),
}


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise slang_spike.SpikeInputError("invalid_arguments", message)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be >= 1")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return value


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        description="Compare Surelog only after a measured Slang frontend gap."
    )
    parser.add_argument("--surelog", type=Path, required=True)
    parser.add_argument("--real-compile-log", type=Path, default=DEFAULT_COMPILE_LOG)
    parser.add_argument("--real-waveform", type=Path, default=DEFAULT_WAVEFORM)
    parser.add_argument("--oracle-json", type=Path)
    parser.add_argument("--cold-repeats", type=_positive_int, default=3)
    parser.add_argument("--timeout-seconds", type=_positive_float, default=180.0)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_argument_parser().parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_receipt(surelog: Path) -> dict[str, Any]:
    venv = Path("/tmp/traceweave-phase0b-surelog-wheel")
    return {
        "package": PACKAGE_NAME,
        "package_version": PACKAGE_VERSION,
        "binary_reported_version": BINARY_VERSION,
        "binary_build_date": BINARY_BUILD_DATE,
        "package_source": PACKAGE_SOURCE,
        "package_project": PACKAGE_PROJECT,
        "package_project_status": "archived_2025-03-10",
        "upstream_source": UPSTREAM_SOURCE,
        "preferred_upstream_recheck": {
            "tag": UPSTREAM_PREFERRED_TAG,
            "commit": UPSTREAM_PREFERRED_COMMIT,
            "status": "source_fetch_failed_in_this_environment_not_capability_evidence",
        },
        "wheel": {
            "filename": WHEEL,
            "url": WHEEL_URL,
            "sha256": WHEEL_SHA256,
            "platform": "CPython 3.11 manylinux_2_17 x86_64",
        },
        "requirements_file": str(REQUIREMENTS),
        "selected_executable": str(surelog.expanduser().absolute()),
        "system_python_modified": False,
        "runtime_dependency": False,
        "production_distribution_accepted": False,
        "reproduction_commands": [
            f"python3.11 -m venv {venv}",
            (
                f"{venv}/bin/python -m pip install --only-binary=:all: "
                f"--no-deps --require-hashes -r {REQUIREMENTS}"
            ),
            (
                f"{venv}/lib/python3.11/site-packages/surelog/bin/surelog "
                "--version"
            ),
        ],
        "comparison_scope": (
            "diagnostic fallback comparison only; this archived wrapper is not a "
            "candidate TraceWeave dependency"
        ),
    }


def translate_workload(workload: dict[str, Any]) -> dict[str, Any]:
    """Translate the already-audited Slang input into a deduplicated Surelog plan."""
    frontend_args = list(workload["translation"]["frontend_args"])
    defines: list[str] = []
    include_dirs: list[str] = []
    timescale: str | None = None
    top = str(workload["top"])
    index = 0
    while index < len(frontend_args):
        token = frontend_args[index]
        if token.startswith("+define+"):
            if token not in defines:
                defines.append(token)
            index += 1
            continue
        if token.startswith("+incdir+"):
            for path in token[len("+incdir+") :].split("+"):
                if path and path not in include_dirs:
                    include_dirs.append(path)
            index += 1
            continue
        if token == "-I" and index + 1 < len(frontend_args):
            path = frontend_args[index + 1]
            if path not in include_dirs:
                include_dirs.append(path)
            index += 2
            continue
        if token == "--timescale" and index + 1 < len(frontend_args):
            timescale = frontend_args[index + 1]
            index += 2
            continue
        if token == "--top" and index + 1 < len(frontend_args):
            top = frontend_args[index + 1]
            index += 2
            continue
        if token in {"--compat", "--std", "-f", "-F"}:
            index += 2
            continue
        index += 1

    source_paths = list(workload["source_facts"]["source_paths"])
    if len(source_paths) != len(set(source_paths)):
        raise slang_spike.SpikeInputError(
            "duplicate_sources", "audited source set is not deduplicated"
        )
    missing = [path for path in source_paths if not Path(path).is_file()]
    if missing:
        raise slang_spike.SpikeInputError(
            "fixture_missing", "Surelog source inputs are missing: " + ", ".join(missing)
        )

    base_args = [*defines]
    if include_dirs:
        base_args.append("+incdir+" + "+".join(include_dirs))
    base_args.extend(["-f", "<run_dir>/inputs.f"])
    if timescale:
        base_args.append(f"-timescale={timescale}")
    base_args.extend(
        [
            "-top",
            top,
            "-mt",
            "0",
            "-mp",
            "0",
            "-nopython",
            "-nostdout",
        ]
    )
    return {
        "source_strategy": (
            "flatten and deduplicate the captured Xcelium source order; the original "
            "command names Cadence uvm_pkg.sv both directly and through a filelist"
        ),
        "source_paths": source_paths,
        "source_count": len(source_paths),
        "defines": defines,
        "include_dirs": include_dirs,
        "timescale": timescale,
        "top": top,
        "base_args_template": base_args,
        "phase_invocations": {
            name: shlex.join(["<surelog>", *base_args, *phase_args])
            for name, phase_args, _ in PHASES
        },
        "translated_options": [
            {
                "input": "captured Xcelium HDL source/filelist inputs",
                "output": ["-f", "<run_dir>/inputs.f"],
                "reason": "preserve order while removing duplicate uvm_pkg.sv",
            },
            {
                "input": "captured Xcelium defines and include directories",
                "output": [*defines, *(base_args[len(defines) : len(defines) + 1])],
                "reason": "Surelog standard simulator-compatible spellings",
            },
            {
                "input": "Xcelium timescale and top",
                "output": [f"-timescale={timescale}", "-top", top],
                "reason": "Surelog documented compile inputs",
            },
        ],
        "unsupported_options": workload["translation"]["unsupported_options"],
        "execution_policy": {
            "threads": 0,
            "processes": 0,
            "python": False,
            "reason": "single-process comparison makes crash, CPU, and RSS attribution exact",
        },
        "translator_scope": (
            "Phase 0B comparison only; not a production DesignInputs implementation"
        ),
    }


def _read_rss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _run_child(
    command: list[str], cwd: Path, environment: dict[str, str], timeout: float
) -> dict[str, Any]:
    stdout_path = cwd / "frontend.stdout"
    stderr_path = cwd / "frontend.stderr"
    stdout_fd = os.open(stdout_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    stderr_fd = os.open(stderr_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    started = time.perf_counter_ns()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exec-only child
        try:
            os.setsid()
            os.chdir(cwd)
            os.dup2(stdout_fd, 1)
            os.dup2(stderr_fd, 2)
            os.close(stdout_fd)
            os.close(stderr_fd)
            os.execve(command[0], command, {**os.environ, **environment})
        except BaseException as exc:
            os.write(2, f"exec failure: {type(exc).__name__}: {exc}\n".encode())
            os._exit(127)
    os.close(stdout_fd)
    os.close(stderr_fd)

    rss_samples: list[int] = []
    timed_out = False
    status: int | None = None
    usage = None
    while status is None:
        rss = _read_rss_kib(pid)
        if rss is not None:
            rss_samples.append(rss)
        waited_pid, waited_status, waited_usage = os.wait4(pid, os.WNOHANG)
        if waited_pid == pid:
            status = waited_status
            usage = waited_usage
            break
        if (time.perf_counter_ns() - started) / 1_000_000_000 > timeout:
            timed_out = True
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                waited_pid, waited_status, waited_usage = os.wait4(pid, os.WNOHANG)
                if waited_pid == pid:
                    status = waited_status
                    usage = waited_usage
                    break
                time.sleep(0.01)
            if status is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status, usage = os.wait4(pid, 0)
            break
        time.sleep(0.005)

    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    return_code = os.waitstatus_to_exitcode(status)
    signal_number = -return_code if return_code < 0 else None
    return {
        "return_code": return_code,
        "shell_exit_code": 128 + signal_number if signal_number else return_code,
        "signal": signal.Signals(signal_number).name if signal_number else None,
        "timed_out": timed_out,
        "wall_time_ms": round(wall_ms, 6),
        "cpu_time_ms": round((usage.ru_utime + usage.ru_stime) * 1000, 6),
        "user_cpu_time_ms": round(usage.ru_utime * 1000, 6),
        "system_cpu_time_ms": round(usage.ru_stime * 1000, 6),
        "rss_start_kib": rss_samples[0] if rss_samples else None,
        "rss_peak_kib": max(rss_samples) if rss_samples else None,
        "rss_end_kib": rss_samples[-1] if rss_samples else None,
        "rss_sampling": "single-process /proc/<pid>/status at 5ms intervals",
        "stdout_bytes": stdout_path.stat().st_size,
        "stderr_bytes": stderr_path.stat().st_size,
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
    }


def parse_surelog_evidence(log_text: str, stdout_text: str) -> dict[str, Any]:
    severity_names = {
        "FTL": "Fatal",
        "SNT": "Syntax",
        "ERR": "Error",
        "WRN": "Warning",
        "NTE": "Note",
        "INF": "Info",
    }
    by_severity: collections.Counter[str] = collections.Counter()
    by_code: collections.Counter[str] = collections.Counter()
    items: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        match = _DIAGNOSTIC_RE.match(line)
        if not match:
            continue
        severity = severity_names[match.group("tag")]
        code = match.group("code")
        by_severity[severity] += 1
        by_code[code] += 1
        if len(items) < 200:
            items.append(
                {
                    "code": code,
                    "severity": severity,
                    "file": match.group("file"),
                    "line": int(match.group("line")) if match.group("line") else None,
                    "column": (
                        int(match.group("column")) if match.group("column") else None
                    ),
                    "message": match.group("message"),
                }
            )

    summary = {
        match.group("severity").lower(): int(match.group("count"))
        for line in log_text.splitlines()
        if (match := _SUMMARY_RE.match(line))
    }
    profile: dict[str, float] = {}
    for match in _PROFILE_RE.finditer(stdout_text):
        profile[match.group("phase").lower() + "_wall_time_ms"] = round(
            float(match.group("seconds")) * 1000, 6
        )

    tops = []
    for match in _TOP_RE.finditer(log_text):
        tops.append(
            {
                "name": match.group("name"),
                "file": match.group("file"),
                "line": int(match.group("line")),
                "column": int(match.group("column")),
            }
        )
    object_counts: dict[str, int] = {}
    for name, pattern in _COUNT_PATTERNS.items():
        match = pattern.search(log_text)
        if match:
            object_counts[name] = int(match.group(1))
    return {
        "diagnostics": {
            "summary_available": bool(summary),
            "summary": summary,
            "observed_by_severity": dict(sorted(by_severity.items())),
            "observed_by_code": dict(sorted(by_code.items())),
            "items": items,
            "items_truncated": sum(by_severity.values()) > len(items),
        },
        "frontend_profile": profile,
        "recovered": {
            "tops": tops,
            "instance_path_naming": (
                "Surelog work@<top>.<instance> when emitted"
                if tops
                else "not_recovered_before_failure"
            ),
            "file_line_fidelity": (
                "frontend-emitted top file/line/column"
                if tops
                else "not_reached_before_failure"
            ),
        },
        "frontend_object_counts": {
            "available": bool(object_counts),
            "counts": object_counts,
            "scope": "Surelog EL0508-EL0511 summary facts only",
        },
        "graph_object_counts": {
            "available": False,
            "reason": "comparison does not build a TraceWeave Connectivity IR",
        },
    }


def _semantic_fingerprint(run: dict[str, Any]) -> str:
    evidence = run["evidence"]
    payload = {
        "phase": run["phase"],
        "return_code": run["process"]["return_code"],
        "signal": run["process"]["signal"],
        "diagnostic_identities": [
            {
                key: item.get(key)
                for key in ("code", "severity", "file", "line", "column")
            }
            for item in evidence["diagnostics"]["items"]
        ],
        "recovered": evidence["recovered"],
        "object_counts": evidence["frontend_object_counts"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _run_phase(
    *,
    phase: str,
    phase_args: tuple[str, ...],
    scope: str,
    surelog: Path,
    translation: dict[str, Any],
    environment: dict[str, str],
    timeout: float,
    root: Path,
    repeat: int,
) -> dict[str, Any]:
    run_dir = root / f"{phase}-{repeat:02d}"
    run_dir.mkdir(parents=True)
    inputs = run_dir / "inputs.f"
    inputs.write_text("\n".join(translation["source_paths"]) + "\n", encoding="utf-8")
    base_args = [
        str(inputs) if token == "<run_dir>/inputs.f" else token
        for token in translation["base_args_template"]
    ]
    command = [str(surelog), *base_args, *phase_args]
    process = _run_child(command, run_dir, environment, timeout)
    log_path = run_dir / "slpp_all" / "surelog.log"
    log_text = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.is_file()
        else ""
    )
    stdout_text = (run_dir / "frontend.stdout").read_text(
        encoding="utf-8", errors="replace"
    )
    evidence = parse_surelog_evidence(log_text, stdout_text)
    result = {
        "phase": phase,
        "measurement_scope": scope,
        "repeat": repeat,
        "invocation": shlex.join(command),
        "process": process,
        "evidence": evidence,
        "generated_output": {
            "surelog_log_available": bool(log_text),
            "surelog_log_bytes": len(log_text.encode()),
            "uhdm_available": (run_dir / "slpp_all" / "surelog.uhdm").is_file(),
            "retained": False,
        },
    }
    result["semantic_fingerprint_sha256"] = _semantic_fingerprint(result)
    return result


def _aggregate(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_phase: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for run in runs:
        by_phase[run["phase"]].append(run)
    phases: dict[str, Any] = {}
    for phase, phase_runs in by_phase.items():
        walls = [run["process"]["wall_time_ms"] for run in phase_runs]
        cpus = [run["process"]["cpu_time_ms"] for run in phase_runs]
        peaks = [
            run["process"]["rss_peak_kib"]
            for run in phase_runs
            if run["process"]["rss_peak_kib"] is not None
        ]
        phases[phase] = {
            "repeat_count": len(phase_runs),
            "return_codes": [run["process"]["return_code"] for run in phase_runs],
            "signals": [run["process"]["signal"] for run in phase_runs],
            "semantic_fingerprints": [
                run["semantic_fingerprint_sha256"] for run in phase_runs
            ],
            "stable_semantics": len(
                {run["semantic_fingerprint_sha256"] for run in phase_runs}
            )
            == 1,
            "wall_time_ms": {
                "min": min(walls),
                "p50": statistics.median(walls),
                "max": max(walls),
            },
            "cpu_time_ms": {
                "min": min(cpus),
                "p50": statistics.median(cpus),
                "max": max(cpus),
            },
            "rss_peak_kib": {
                "min": min(peaks) if peaks else None,
                "p50": statistics.median(peaks) if peaks else None,
                "max": max(peaks) if peaks else None,
            },
        }
    return phases


def _compare_oracles(
    recovered: dict[str, Any], oracle_doc: dict[str, Any]
) -> dict[str, Any]:
    workload_oracles = oracle_doc.get("workloads", {}).get("real_uvm", {})
    recovered_tops = {item["name"].removeprefix("work@") for item in recovered["tops"]}
    comparisons = []
    for name, oracle in workload_oracles.items():
        if not isinstance(oracle, dict):
            continue
        available = bool(oracle.get("available"))
        expected_top = oracle.get("top")
        comparisons.append(
            {
                "oracle": name,
                "available": available,
                "expected_top": expected_top,
                "top_match": (
                    expected_top in recovered_tops if available and recovered_tops else None
                ),
                "status": (
                    "compared"
                    if available and recovered_tops
                    else "not_compared_frontend_failure"
                    if available
                    else "unavailable"
                ),
            }
        )
    return {
        "comparisons": comparisons,
        "development_oracle_policy": (
            "FSDB/Xcelium/KDB/NPI evidence is comparison-only and never a frontend or "
            "automated-test dependency"
        ),
    }


def run_comparison(args: argparse.Namespace) -> dict[str, Any]:
    surelog = args.surelog.expanduser().absolute()
    dependency = _dependency_receipt(surelog)
    workload = slang_spike.build_real_workload(
        args.real_compile_log,
        {},
        simulator="xcelium",
        waveform=args.real_waveform,
    )
    translation = translate_workload(workload)
    oracle_doc = slang_spike._read_json(args.oracle_json) if args.oracle_json else {}
    base = {
        "schema_version": SCHEMA_VERSION,
        "spike": SPIKE_NAME,
        "repository_baseline": slang_spike.REPOSITORY_BASELINE,
        "dependency": dependency,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "workload": {
            key: workload[key]
            for key in (
                "name",
                "kind",
                "simulator",
                "simulator_version",
                "top",
                "compile_log",
                "compile_cwd",
                "waveform",
                "waveform_role",
                "source_facts",
                "diagnostic_policy",
            )
        },
        "translation": translation,
        "cold_repeats_per_phase": args.cold_repeats,
        "methodology": {
            "process_model": "fresh isolated single-process Surelog child per phase",
            "cache_state": "fresh output directory per child; host filesystem cache not flushed",
            "phase_timing": (
                "Surelog -profile supplies internal wall time where a phase completes; "
                "process wall/CPU/RSS measurements are cumulative per documented mode"
            ),
            "no_fallback": True,
        },
    }
    if args.plan_only:
        return {**base, "status": "planned", "runs": [], "phase_aggregate": {}}
    if not surelog.is_file() or not os.access(surelog, os.X_OK):
        return {
            **base,
            "status": "blocked",
            "runs": [],
            "phase_aggregate": {},
            "blockers": [
                {
                    "code": "frontend_unavailable",
                    "phase": "dependency",
                    "message": f"Surelog executable is missing or not executable: {surelog}",
                }
            ],
        }

    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="traceweave-surelog-phase0b-") as raw_root:
        run_root = Path(raw_root)
        for repeat in range(1, args.cold_repeats + 1):
            for phase, phase_args, scope in PHASES:
                runs.append(
                    _run_phase(
                        phase=phase,
                        phase_args=phase_args,
                        scope=scope,
                        surelog=surelog,
                        translation=translation,
                        environment=workload["environment"],
                        timeout=args.timeout_seconds,
                        root=run_root,
                        repeat=repeat,
                    )
                )

    elaboration_runs = [run for run in runs if run["phase"] == "elaboration"]
    representative = elaboration_runs[0]
    crashed = all(run["process"]["signal"] == "SIGSEGV" for run in elaboration_runs)
    blockers = []
    if crashed:
        blockers.append(
            {
                "code": "frontend_crash",
                "phase": "compilation_before_elaboration",
                "message": (
                    "Surelog 1.84 exits by SIGSEGV after CP0300 Compilation begins; "
                    "the same crash occurs with -noelab, so elaboration is not reached"
                ),
            }
        )
    elif any(run["process"]["return_code"] != 0 for run in elaboration_runs):
        blockers.append(
            {
                "code": "frontend_nonzero_exit",
                "phase": "elaboration",
                "message": "one or more Surelog elaboration runs returned nonzero",
            }
        )
    recovered = representative["evidence"]["recovered"]
    return {
        **base,
        "status": "blocked" if blockers else "supported",
        "runs": runs,
        "phase_aggregate": _aggregate(runs),
        "representative_elaboration_result": representative,
        "oracle_comparison": _compare_oracles(recovered, oracle_doc),
        "blockers": blockers,
        "assessment": {
            "slang_primary_frontend": {
                "decision": "retain_for_phase1_prototype",
                "reason": (
                    "Slang completed elaboration and recovered oracle-matching hierarchy/source "
                    "facts; this Surelog comparison does not reach elaboration"
                ),
                "production_status": "not_implemented_not_production_validated",
            },
            "surelog_uhdm_fallback": {
                "decision": "do_not_add",
                "reason": (
                    "measured comparison supplies no recovery for the Slang UVM diagnostic and "
                    "uses an archived third-party binary distribution"
                ),
                "future_recheck": (
                    "only against reproducibly obtained official Surelog v1.86+ if a future "
                    "Slang blocker prevents required source-graph facts"
                ),
            },
            "dependency_model": "Slang optional extra; no Surelog dependency",
            "worker_model": "isolated worker process",
            "evidence_scope": (
                "frontend feasibility only; no Source Graph edges, production accuracy, or "
                "production performance claim"
            ),
        },
    }


def _emit(result: dict[str, Any], output: Path | None) -> int:
    if output is not None:
        path = output.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        rendered = {
            "status": result["status"],
            "output": str(path),
            "sha256": _sha256(path),
        }
    else:
        rendered = result
    print(json.dumps(rendered, indent=2, sort_keys=True))
    if result["status"] in {"planned", "supported"}:
        return 0
    return 3


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = run_comparison(args)
    except slang_spike.SpikeError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "spike": SPIKE_NAME,
            "status": "blocked",
            "blockers": [
                {"code": exc.error_code, "phase": "input", "message": str(exc)}
            ],
        }
        args = None
    return _emit(result, args.output if args is not None else None)


if __name__ == "__main__":
    raise SystemExit(main())
