#!/usr/bin/env python3
"""Phase 0B feasibility probe for a source-elaborating SystemVerilog frontend.

This is deliberately a standalone development harness.  It does not register
an MCP tool, alter connectivity routing, or provide a production backend.  The
parent process never imports pyslang; every measured run starts a fresh worker
using an explicitly selected Python interpreter.  That keeps the optional
native frontend out of TraceWeave's base runtime and makes import/build RSS and
process wall time observable.

The probe emits one machine-readable JSON document.  A failed frontend run is
also represented as JSON with explicit blockers; there is no implicit fallback
to Legacy Static.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import re
import resource
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "1.0"
SPIKE_NAME = "source_graph_frontend_feasibility"
FRONTEND_NAME = "Slang/pyslang"
FRONTEND_PACKAGE = "pyslang"
FRONTEND_VERSION = "11.0.0"
FRONTEND_SOURCE = "https://pypi.org/project/pyslang/11.0.0/"
FRONTEND_UPSTREAM_COMMIT = "7ddf4059f79eff508dd486eb42fd650cdf320d52"
FRONTEND_WHEEL = (
    "pyslang-11.0.0-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
)
FRONTEND_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/ac/bc/"
    "a8830bdbbe30ac04b8a545c5e630707c99da3bed348ea8a96d2591e7e79d/" + FRONTEND_WHEEL
)
FRONTEND_WHEEL_BYTES = 6_530_751
FRONTEND_WHEEL_SHA256 = (
    "b0ddf4fa29b72e0386d43863c4e7b5d53a4b0e5db6f7038c0f47f2e3e97a88bc"
)
REPOSITORY_BASELINE = "02cc7c11f62daf6a9fdf8fcee3eb40c98afbdea0"
FRONTEND_REQUIREMENTS = (
    ROOT / "scripts" / "frontend_spike_requirements_cp311_linux_x86_64.txt"
)

HAND_DIR = ROOT / "tests" / "fixtures" / "source_graph_frontend"
HAND_SOURCE = HAND_DIR / "hand_connectivity.sv"
HAND_ORACLE = HAND_DIR / "hand_oracle.json"
DEEP_DIR = ROOT / "tests" / "fixtures" / "deep_x_npi"
DEEP_RTL = DEEP_DIR / "rtl" / "deep_uart_x.sv"
DEEP_TB = DEEP_DIR / "tb" / "deep_x_tb.sv"
DEEP_COMPILE_LOG = DEEP_DIR / "work" / "compile.log"
DEEP_FSDB = DEEP_DIR / "work" / "deep_x.fsdb"
DEFAULT_REAL_COMPILE_LOG = Path(
    "/home/robin/Projects/mcp_practise/uvm_demo_cc20/tb/comp.log"
)

WORKLOAD_NAMES = ("deep_x_npi", "hand_fixture", "real_uvm")
REAL_SIMULATORS = ("auto", "vcs", "xcelium")
HDL_SUFFIXES = {".v", ".vh", ".sv", ".svh", ".sva", ".svp"}
_DESIGN_FILE_RE = re.compile(r"Parsing design file\s+['\"]([^'\"]+)['\"]")
_ENV_REF_RE = re.compile(
    r"\$(?:\((?P<paren>[A-Za-z_][A-Za-z0-9_]*)\)|"
    r"\{(?P<brace>[A-Za-z_][A-Za-z0-9_]*)\}|"
    r"(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)


class SpikeError(RuntimeError):
    """Machine-classifiable probe error."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SpikeInputError(SpikeError):
    """A workload or dependency input is unusable."""


class SpikeExecutionError(SpikeError):
    """A frontend worker failed to obey the JSON protocol."""


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SpikeInputError("invalid_arguments", message)


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


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        description="Measure pyslang feasibility without changing production routing."
    )
    parser.add_argument(
        "--frontend-python",
        type=Path,
        default=Path(sys.executable),
        help="Python interpreter containing the pinned pyslang package.",
    )
    parser.add_argument(
        "--workload",
        action="append",
        choices=WORKLOAD_NAMES,
        help="Workload to run; repeatable. The default runs all three.",
    )
    parser.add_argument(
        "--real-compile-log", type=Path, default=DEFAULT_REAL_COMPILE_LOG
    )
    parser.add_argument(
        "--real-simulator",
        choices=REAL_SIMULATORS,
        default="auto",
        help="Simulator syntax used by the real compile log (default: detect).",
    )
    parser.add_argument(
        "--real-waveform",
        type=Path,
        help="Optional local FSDB/VCD oracle path; the frontend worker never opens it.",
    )
    parser.add_argument(
        "--real-env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Explicit environment substitution used by the real VCS filelist.",
    )
    parser.add_argument(
        "--oracle-json",
        type=Path,
        help="Optional development-only FSDB/KDB/NPI evidence; never required by tests.",
    )
    parser.add_argument("--cold-repeats", type=_positive_int, default=3)
    parser.add_argument("--child-timeout-seconds", type=_positive_float, default=180.0)
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Emit translated invocations without importing or running a frontend.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Also write the complete JSON result to this path.",
    )
    parser.add_argument("--_worker-spec", type=Path, help=argparse.SUPPRESS)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_argument_parser().parse_args(argv)


def _round_ms(value: float) -> float:
    return round(value, 6)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpikeInputError(
            "invalid_json",
            f"cannot read JSON input {path}: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise SpikeInputError("invalid_json", f"expected a JSON object in {path}")
    return value


def _require_nonempty_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise SpikeInputError(
            "fixture_missing", f"{label} is missing or empty: {resolved}"
        )
    return resolved


def _parse_name_values(raw_values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in raw_values:
        name, separator, value = raw.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise SpikeInputError(
                "invalid_environment",
                f"expected NAME=VALUE for --real-env, got {raw!r}",
            )
        if not value:
            raise SpikeInputError(
                "invalid_environment", f"empty value for --real-env {name}"
            )
        result[name] = value
    return result


def _extract_env_refs(text: str) -> set[str]:
    return {
        next(group for group in match.groups() if group is not None)
        for match in _ENV_REF_RE.finditer(text)
    }


def _expand_with_env(value: str, environment: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = next(group for group in match.groups() if group is not None)
        return environment.get(name, match.group(0))

    return _ENV_REF_RE.sub(replace, value)


def _read_command_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def _extract_direct_design_files(log_path: Path) -> list[Path]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    seen: set[Path] = set()
    result: list[Path] = []
    for raw in _DESIGN_FILE_RE.findall(text):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = log_path.parent / path
        path = path.resolve()
        if path.suffix.lower() not in HDL_SUFFIXES or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _detect_real_simulator(log_path: Path) -> str:
    prefix = log_path.read_text(encoding="utf-8", errors="replace")[:32_768].lower()
    if re.search(r"(?:^|\n)\s*(?:tool:\s*)?(?:xrun|irun)(?:\(64\))?\b", prefix):
        return "xcelium"
    if "chronologic vcs" in prefix or re.search(
        r"(?:^|\n)\s*(?:command:\s*)?vcs\b", prefix
    ):
        return "vcs"
    raise SpikeInputError(
        "simulator_unknown",
        f"cannot detect VCS or Xcelium from real compile log: {log_path}",
    )


def _extract_xcelium_invocation(log_path: Path) -> tuple[str, list[Path]]:
    """Recover the leading xrun option block without consuming later diagnostics."""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"\s*(?:xrun|irun)\s*", line)
        ),
        None,
    )
    if start is None:
        raise SpikeInputError(
            "compile_log_invalid",
            f"Xcelium log has no leading xrun/irun option block: {log_path}",
        )

    block: list[tuple[int, str]] = []
    base_indent: int | None = None
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 or stripped == "|":
            break
        if base_indent is None:
            base_indent = indent
        block.append((indent, stripped))

    if not block or base_indent is None:
        raise SpikeInputError(
            "compile_log_invalid",
            f"Xcelium log has an empty xrun/irun option block: {log_path}",
        )

    top_level_tokens: list[str] = ["xrun"]
    expanded_sources: list[Path] = []
    for indent, rendered in block:
        try:
            tokens = shlex.split(rendered)
        except ValueError as exc:
            raise SpikeInputError(
                "compile_command_invalid",
                f"cannot tokenize Xcelium option line {rendered!r}: {exc}",
            ) from exc
        if indent == base_indent:
            top_level_tokens.extend(tokens)
        for token in tokens:
            if Path(token).suffix.lower() not in HDL_SUFFIXES:
                continue
            path = Path(token).expanduser()
            if not path.is_absolute():
                path = log_path.parent / path
            expanded_sources.append(path.resolve())

    return shlex.join(top_level_tokens), expanded_sources


def _extract_xcelium_uvm_library(log_path: Path) -> dict[str, Any] | None:
    """Resolve the simulator-provided UVM sources recorded by xrun itself."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"Compiling UVM packages \((?P<packages>[^)]+)\) using uvmhome "
        r"location (?P<location>[^\r\n]+)",
        text,
    )
    if match is None:
        return None

    location = Path(match.group("location").strip()).expanduser().resolve()
    search_dirs = [location / "sv" / "src", location / "additions" / "sv"]
    sources: list[Path] = []
    missing: list[str] = []
    for package_name in shlex.split(match.group("packages")):
        source = next(
            (directory / package_name for directory in search_dirs if (directory / package_name).is_file()),
            None,
        )
        if source is None:
            missing.append(package_name)
        else:
            sources.append(source.resolve())
    if missing:
        raise SpikeInputError(
            "simulator_library_missing",
            "xrun recorded simulator-provided UVM packages that are unavailable: "
            + ", ".join(missing),
        )
    return {
        "name": "xcelium_uvmhome",
        "source": "xrun_log_uvmhome_record",
        "location": str(location),
        "include_dirs": [str(path.resolve()) for path in search_dirs if path.is_dir()],
        "sources": [str(path) for path in sources],
    }


def _real_project_root(log_path: Path) -> Path:
    for candidate in (log_path.parent, *log_path.parents):
        if (candidate / "dut").is_dir() and (candidate / "tb").is_dir():
            return candidate
    return log_path.parent.parent


def _real_compile_cwd(log_path: Path) -> Path:
    project_root = _real_project_root(log_path)
    tb_dir = project_root / "tb"
    return tb_dir if tb_dir.is_dir() else log_path.parent


def _infer_real_environment(
    compile_log: Path, command: str, source_paths: Sequence[Path]
) -> tuple[dict[str, str], dict[str, str]]:
    environment: dict[str, str] = {}
    provenance: dict[str, str] = {}
    project_root = _real_project_root(compile_log)
    if (project_root / "dut").is_dir() and (project_root / "tb").is_dir():
        environment["TB_DIR"] = str(project_root)
        provenance["TB_DIR"] = "inferred_from_compile_log_parent"

    for token in shlex.split(command):
        path = Path(token)
        if path.name in {"uvm.sv", "uvm_pkg.sv"} and path.parent.name == "src":
            environment["UVM_HOME"] = str(path.parent.parent.resolve())
            provenance["UVM_HOME"] = "inferred_from_absolute_uvm_source"
            break

    marker = f"{os.sep}rtl{os.sep}verilog{os.sep}"
    for path in source_paths:
        rendered = str(path)
        if marker in rendered:
            prefix = rendered.split(marker, 1)[0]
            environment["DUT_SRC_DIR"] = prefix + f"{os.sep}rtl{os.sep}verilog"
            provenance["DUT_SRC_DIR"] = "inferred_from_compile_log_source_record"
            break
    return environment, provenance


def _discover_filelist_requirements(
    command: str, cwd: Path, environment: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Return filelists visited and unresolved environment variable names."""
    pending: list[Path] = []
    tokens = shlex.split(command)
    for index, token in enumerate(tokens[:-1]):
        if token in {"-f", "-F"}:
            raw = _expand_with_env(tokens[index + 1], environment)
            path = Path(raw)
            if not path.is_absolute():
                path = cwd / path
            pending.append(path.resolve())

    visited: list[str] = []
    unresolved: set[str] = set()
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        visited.append(str(path))
        if not path.is_file():
            continue
        text = _read_command_file(path)
        unresolved.update(_extract_env_refs(text) - environment.keys())
        expanded = _expand_with_env(text, environment)
        try:
            file_tokens = shlex.split(expanded, comments=False)
        except ValueError:
            continue
        for index, token in enumerate(file_tokens[:-1]):
            if token not in {"-f", "-F"}:
                continue
            nested = Path(file_tokens[index + 1])
            if not nested.is_absolute():
                nested = cwd / nested if token == "-f" else path.parent / nested
            pending.append(nested.resolve())
    return sorted(visited), sorted(unresolved)


def _discover_filelist_sources(
    command: str, cwd: Path, environment: dict[str, str]
) -> list[Path]:
    pending: list[tuple[Path, str]] = []
    tokens = shlex.split(command)
    for index, token in enumerate(tokens[:-1]):
        if token not in {"-f", "-F"}:
            continue
        raw = _expand_with_env(tokens[index + 1], environment)
        path = Path(raw)
        if not path.is_absolute():
            path = cwd / path
        pending.append((path.resolve(), token))

    sources: list[Path] = []
    seen_sources: set[Path] = set()
    seen_filelists: set[Path] = set()
    while pending:
        path, mode = pending.pop()
        if path in seen_filelists or not path.is_file():
            continue
        seen_filelists.add(path)
        expanded = _expand_with_env(_read_command_file(path), environment)
        try:
            file_tokens = shlex.split(expanded, comments=True)
        except ValueError:
            continue
        index = 0
        while index < len(file_tokens):
            token = file_tokens[index]
            if token in {"-f", "-F"} and index + 1 < len(file_tokens):
                nested = Path(file_tokens[index + 1])
                if not nested.is_absolute():
                    nested = cwd / nested if token == "-f" else path.parent / nested
                pending.append((nested.resolve(), token))
                index += 2
                continue
            if Path(token).suffix.lower() in HDL_SUFFIXES:
                source = Path(token).expanduser()
                if not source.is_absolute():
                    source = cwd / source if mode == "-f" else path.parent / source
                source = source.resolve()
                if source not in seen_sources:
                    seen_sources.add(source)
                    sources.append(source)
            index += 1
    return sources


def _direct_command_sources(
    command: str, cwd: Path, environment: dict[str, str]
) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for token in shlex.split(_expand_with_env(command, environment)):
        if Path(token).suffix.lower() not in HDL_SUFFIXES:
            continue
        path = Path(token).expanduser()
        if not path.is_absolute():
            path = cwd / path
        path = path.resolve()
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _simulator_version_from_log(log_path: Path, simulator: str) -> str | None:
    text = log_path.read_text(encoding="utf-8", errors="replace")[:16_384]
    if simulator == "xcelium":
        match = re.search(r"(?:xrun|irun)(?:\(64\))?:\s*([^:\n]+)", text)
    else:
        match = re.search(r"Compiler version\s*=\s*([^\n]+)", text)
        if match is None:
            match = re.search(r"VCS[^\n]*?([A-Z]-\d{4}\.\d+(?:-[A-Za-z0-9.-]+)?)", text)
    return match.group(1).strip() if match else None


def _record_translation(
    records: list[dict[str, Any]],
    input_option: str,
    output_options: Sequence[str],
    reason: str,
) -> None:
    records.append(
        {
            "input": input_option,
            "output": list(output_options),
            "reason": reason,
        }
    )


def _record_unsupported(
    records: list[dict[str, Any]], option: str, reason: str, impact: str
) -> None:
    records.append({"option": option, "reason": reason, "impact": impact})


def translate_vcs_invocation(
    command: str,
    *,
    top: str,
    fallback_sources: Sequence[Path],
) -> dict[str, Any]:
    """Translate only what the Phase 0B probe needs from one VCS command.

    This intentionally is not a production DesignInputs parser.  It preserves
    Slang-compatible filelists, sources, defines, include paths and library
    extensions, while classifying simulator/runtime/C-DPI switches instead of
    forwarding unknown arguments.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise SpikeInputError(
            "compile_command_invalid", f"cannot tokenize VCS command: {exc}"
        ) from exc
    if tokens and Path(tokens[0]).name in {"vcs", "vlogan"}:
        tokens = tokens[1:]

    frontend_args = [
        "--compat",
        "vcs",
        "--enable-legacy-protect",
        "--single-unit",
        "-Wno-unknown-sys-name",
    ]
    translated: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    _record_translation(
        translated,
        "simulator=vcs",
        ["--compat", "vcs"],
        "enable documented VCS compatibility behavior",
    )
    _record_translation(
        translated,
        "VCS legacy protected UVM source",
        ["--enable-legacy-protect"],
        "parse legacy `protected/`endprotected envelopes",
    )
    _record_translation(
        translated,
        "VCS compilation-unit semantics",
        ["--single-unit"],
        "preserve macro guards and source order across the VCS file list",
    )
    _record_translation(
        translated,
        "external simulator runtime system tasks",
        ["-Wno-unknown-sys-name"],
        "record but do not reject FSDB/VCS runtime hooks that do not define HDL connectivity",
    )

    source_input_seen = False
    top_seen = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        suffix = Path(token).suffix.lower()
        if suffix in HDL_SUFFIXES:
            frontend_args.append(token)
            source_input_seen = True
            _record_translation(translated, token, [token], "HDL source input")
            index += 1
            continue
        if suffix in {".c", ".cc", ".cpp", ".o", ".a", ".so"}:
            _record_unsupported(
                unsupported,
                token,
                "C/C++/binary DPI input is outside source connectivity elaboration",
                "external_runtime_not_modeled",
            )
            index += 1
            continue
        if token.startswith("+define+"):
            definitions = token[len("+define+") :].split("+")
            rejected_redefinitions = {"define"}
            removed = [
                definition
                for definition in definitions
                if definition in rejected_redefinitions
            ]
            retained = [
                definition
                for definition in definitions
                if definition not in rejected_redefinitions
            ]
            outputs = ["+define+" + "+".join(retained)] if retained else []
            frontend_args.extend(outputs)
            _record_translation(
                translated,
                token,
                outputs,
                (
                    "Slang accepts this VCS-compatible spelling"
                    if not removed
                    else (
                        "remove the compiler-directive name that Slang rejects as a "
                        f"macro {removed!r}; retain the remaining VCS macro definitions"
                    )
                ),
            )
            index += 1
            continue
        if token.startswith("+incdir+"):
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang accepts this VCS-compatible spelling"
            )
            index += 1
            continue
        if token.startswith("+libext+"):
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang supports VCS library extensions"
            )
            index += 1
            continue
        if token == "-sverilog":
            frontend_args.extend(["--std", "1800-2017"])
            _record_translation(
                translated,
                token,
                ["--std", "1800-2017"],
                "select the SystemVerilog revision used by this probe",
            )
            index += 1
            continue
        if token.startswith("-timescale="):
            value = token.split("=", 1)[1]
            frontend_args.extend(["--timescale", value])
            _record_translation(
                translated, token, ["--timescale", value], "default time scale"
            )
            index += 1
            continue
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend([token, value])
            source_input_seen = True
            _record_translation(
                translated,
                f"{token} {value}",
                [token, value],
                "Slang command files support nested filelists and environment expansion",
            )
            index += 2
            continue
        if token == "-top" and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend(["--top", value])
            top_seen = top_seen or value == top
            _record_translation(
                translated, f"-top {value}", ["--top", value], "explicit top"
            )
            index += 2
            continue
        if token in {"-I", "-D", "-U", "-y", "-v"} and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend([token, value])
            _record_translation(
                translated,
                f"{token} {value}",
                [token, value],
                "Slang-compatible compile input",
            )
            index += 2
            continue
        if token.startswith(("-I", "-D", "-U")) and len(token) > 2:
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang-compatible compile input"
            )
            index += 1
            continue
        if token in {"-l", "-o", "-Mdir", "-work", "-CFLAGS", "-LDFLAGS"}:
            value = tokens[index + 1] if index + 1 < len(tokens) else None
            rendered = f"{token} {value}" if value is not None else token
            reason = (
                "simulator output/log/build option"
                if token not in {"-CFLAGS", "-LDFLAGS"}
                else "native DPI compiler/linker option"
            )
            _record_unsupported(unsupported, rendered, reason, "frontend_irrelevant")
            index += 2 if value is not None else 1
            continue
        if token in {
            "-full64",
            "-lca",
            "-kdb",
            "-kdb=only",
            "+v2k",
            "+vpi",
            "-R",
        } or token.startswith(("-debug", "+ntb_", "+vcs+")):
            _record_unsupported(
                unsupported,
                token,
                "VCS execution, debug, KDB, or runtime option",
                "frontend_irrelevant",
            )
            index += 1
            continue
        if token.startswith(("+", "-")):
            _record_unsupported(
                unsupported,
                token,
                "unclassified vendor option was not forwarded",
                "requires_workload_review",
            )
            index += 1
            continue
        _record_unsupported(
            unsupported,
            token,
            "non-option token was not recognized as HDL source",
            "requires_workload_review",
        )
        index += 1

    if not source_input_seen:
        for path in fallback_sources:
            rendered = str(path)
            frontend_args.append(rendered)
            _record_translation(
                translated,
                "compile-log design-file record",
                [rendered],
                "fallback because the captured command had no direct HDL/filelist input",
            )
    if not top_seen:
        frontend_args.extend(["--top", top])
        _record_translation(
            translated,
            "compile-log recovered top",
            ["--top", top],
            "the captured VCS command did not name the elaboration top",
        )

    return {
        "original_invocation": command,
        "translated_options": translated,
        "unsupported_options": unsupported,
        "frontend_args": frontend_args,
        "frontend_invocation": (
            "pyslang.driver.Driver.parseCommandLine("
            + repr(shlex.join(frontend_args))
            + ")"
        ),
        "translator_scope": (
            "Phase 0B probe only; not a production DesignInputs implementation"
        ),
    }


def translate_xcelium_invocation(
    command: str,
    *,
    top: str,
    fallback_sources: Sequence[Path],
    xcelium_uvm_library: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate the Phase 0B-relevant portion of an xrun invocation."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise SpikeInputError(
            "compile_command_invalid", f"cannot tokenize Xcelium command: {exc}"
        ) from exc
    if tokens and Path(tokens[0]).name in {"xrun", "irun"}:
        tokens = tokens[1:]

    frontend_args = [
        "--compat",
        "all",
        "--enable-legacy-protect",
        "--single-unit",
        "-Wno-unknown-sys-name",
    ]
    uvm_frontend_args: list[str] = []
    if xcelium_uvm_library is not None:
        for include_dir in xcelium_uvm_library.get("include_dirs", []):
            uvm_frontend_args.extend(["-I", str(include_dir)])
        uvm_frontend_args.extend(
            str(path) for path in xcelium_uvm_library.get("sources", [])
        )
        # Xcelium compiles its implicit UVM library before user compilation
        # units even when the rendered option block prints -uvmhome later.
        frontend_args.extend(uvm_frontend_args)
    translated: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    _record_translation(
        translated,
        "simulator=xcelium",
        ["--compat", "all"],
        (
            "Slang 11 has no xcelium-specific compatibility value; use its "
            "documented aggregate vendor-compatibility profile"
        ),
    )
    _record_translation(
        translated,
        "legacy protected source envelopes",
        ["--enable-legacy-protect"],
        "parse legacy `protected/`endprotected envelopes when present",
    )
    _record_translation(
        translated,
        "Xcelium compilation-unit source ordering",
        ["--single-unit"],
        "preserve macro guards and source order across the captured file list",
    )
    _record_translation(
        translated,
        "external simulator runtime system tasks",
        ["-Wno-unknown-sys-name"],
        "record but do not reject FSDB/Xcelium runtime hooks outside HDL connectivity",
    )

    source_input_seen = False
    top_seen = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        suffix = Path(token).suffix.lower()
        if suffix in HDL_SUFFIXES:
            frontend_args.append(token)
            source_input_seen = True
            _record_translation(translated, token, [token], "HDL source input")
            index += 1
            continue
        if suffix in {".c", ".cc", ".cpp", ".o", ".a", ".so"}:
            _record_unsupported(
                unsupported,
                token,
                "C/C++/binary DPI input is outside source connectivity elaboration",
                "external_runtime_not_modeled",
            )
            index += 1
            continue
        if token.startswith("+define+"):
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang accepts this vendor spelling"
            )
            index += 1
            continue
        if token.startswith("+incdir+"):
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang accepts this vendor spelling"
            )
            index += 1
            continue
        if token.startswith("+libext+"):
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang supports library extensions"
            )
            index += 1
            continue
        if token == "-sv":
            frontend_args.extend(["--std", "1800-2017"])
            _record_translation(
                translated,
                token,
                ["--std", "1800-2017"],
                "select the SystemVerilog revision used by this probe",
            )
            index += 1
            continue
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend([token, value])
            source_input_seen = True
            _record_translation(
                translated,
                f"{token} {value}",
                [token, value],
                "Slang command files support nested filelists and environment expansion",
            )
            index += 2
            continue
        if token == "-incdir" and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend(["-I", value])
            _record_translation(
                translated,
                f"-incdir {value}",
                ["-I", value],
                "translate Xcelium include-directory spelling",
            )
            index += 2
            continue
        if token == "-define" and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend(["-D", value])
            _record_translation(
                translated,
                f"-define {value}",
                ["-D", value],
                "translate Xcelium macro-definition spelling",
            )
            index += 2
            continue
        if token == "-timescale" and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend(["--timescale", value])
            _record_translation(
                translated,
                f"-timescale {value}",
                ["--timescale", value],
                "default time scale",
            )
            index += 2
            continue
        if token == "-top" and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend(["--top", value])
            top_seen = top_seen or value == top
            _record_translation(
                translated, f"-top {value}", ["--top", value], "explicit top"
            )
            index += 2
            continue
        if token in {"-I", "-D", "-U", "-y", "-v"} and index + 1 < len(tokens):
            value = tokens[index + 1]
            frontend_args.extend([token, value])
            _record_translation(
                translated,
                f"{token} {value}",
                [token, value],
                "Slang-compatible compile input",
            )
            index += 2
            continue
        if token.startswith(("-I", "-D", "-U")) and len(token) > 2:
            frontend_args.append(token)
            _record_translation(
                translated, token, [token], "Slang-compatible compile input"
            )
            index += 1
            continue
        if token == "-disable_sem2009":
            _record_unsupported(
                unsupported,
                token,
                (
                    "Xcelium pre-2009 compatibility semantics have no exact Slang "
                    "switch; hierarchy and source fidelity require oracle validation"
                ),
                "semantic_compatibility_oracle_required",
            )
            index += 1
            continue
        if token == "-uvmhome" and index + 1 < len(tokens):
            value = tokens[index + 1]
            if xcelium_uvm_library is None:
                _record_unsupported(
                    unsupported,
                    f"-uvmhome {value}",
                    (
                        "Xcelium's implicit UVM source library was not recorded in "
                        "the compile log and cannot be silently approximated"
                    ),
                    "simulator_source_library_unresolved",
                )
            else:
                _record_translation(
                    translated,
                    f"-uvmhome {value}",
                    uvm_frontend_args,
                    (
                        "expand the simulator-provided UVM package sources and include "
                        "directories recorded by xrun"
                    ),
                )
            index += 2
            continue
        if token == "-ALLOWREDEFINITION":
            _record_unsupported(
                unsupported,
                token,
                (
                    "Xcelium root-definition replacement semantics have no exact "
                    "Slang switch; elaborated hierarchy requires oracle validation"
                ),
                "semantic_compatibility_oracle_required",
            )
            index += 1
            continue
        if token in {"-errormax", "-nowarn", "-xmerror", "-xprop"}:
            value = tokens[index + 1] if index + 1 < len(tokens) else None
            rendered = f"{token} {value}" if value is not None else token
            _record_unsupported(
                unsupported,
                rendered,
                "Xcelium diagnostic or simulation-runtime policy",
                "frontend_irrelevant",
            )
            index += 2 if value is not None else 1
            continue
        if token in {
            "-access",
            "-input",
            "-log",
            "-l",
            "-snapshot",
            "-xmlibdirname",
            "-work",
            "-uvmpackagename",
            "-covworkdir",
            "-covtest",
            "-seed",
            "-svseed",
        }:
            value = tokens[index + 1] if index + 1 < len(tokens) else None
            rendered = f"{token} {value}" if value is not None else token
            _record_unsupported(
                unsupported,
                rendered,
                "Xcelium library, output, debug, coverage, or runtime option",
                "frontend_irrelevant",
            )
            index += 2 if value is not None else 1
            continue
        if token in {
            "-64bit",
            "-elaborate",
            "-mess",
            "-messages",
            "-notimingchecks",
            "-nospecify",
            "-quiet",
            "-status",
            "-verbose",
        } or token.startswith(("+xm", "-coverage", "-covoverwrite")):
            _record_unsupported(
                unsupported,
                token,
                "Xcelium execution, timing, diagnostics, coverage, or runtime option",
                "frontend_irrelevant",
            )
            index += 1
            continue
        if token in {
            "-licqueue",
            "-enable_strict_timescale",
            "-nocopyright",
            "-enable_abv_asrtctrl_enh",
            "-xverbose",
        }:
            _record_unsupported(
                unsupported,
                token,
                "Xcelium license, diagnostics, assertion, or simulation-runtime policy",
                "frontend_irrelevant",
            )
            index += 1
            continue
        if token == "-L" and index + 1 < len(tokens):
            value = tokens[index + 1]
            _record_unsupported(
                unsupported,
                f"-L {value}",
                "native DPI linker search path is outside HDL source connectivity",
                "external_runtime_not_modeled",
            )
            index += 2
            continue
        if token.startswith("-L") or (
            token.startswith("-l") and token not in {"-licqueue"}
        ):
            _record_unsupported(
                unsupported,
                token,
                "native DPI linker option is outside HDL source connectivity",
                "external_runtime_not_modeled",
            )
            index += 1
            continue
        if token.startswith(("+", "-")):
            _record_unsupported(
                unsupported,
                token,
                "unclassified Xcelium option was not forwarded",
                "requires_workload_review",
            )
            index += 1
            continue
        _record_unsupported(
            unsupported,
            token,
            "non-option token was not recognized as HDL source",
            "requires_workload_review",
        )
        index += 1

    if not source_input_seen:
        for path in fallback_sources:
            rendered = str(path)
            frontend_args.append(rendered)
            _record_translation(
                translated,
                "compile-log source record",
                [rendered],
                "fallback because the captured command had no HDL/filelist input",
            )
    if not top_seen:
        frontend_args.extend(["--top", top])
        _record_translation(
            translated,
            "compile-log recovered top",
            ["--top", top],
            "the captured Xcelium command did not name the elaboration top",
        )

    return {
        "original_invocation": command,
        "translated_options": translated,
        "unsupported_options": unsupported,
        "frontend_args": frontend_args,
        "frontend_invocation": (
            "pyslang.driver.Driver.parseCommandLine("
            + repr(shlex.join(frontend_args))
            + ")"
        ),
        "translator_scope": (
            "Phase 0B probe only; not a production DesignInputs implementation"
        ),
    }


def _source_facts(paths: Sequence[Path]) -> dict[str, Any]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        item = path.expanduser().resolve()
        if item in seen:
            continue
        seen.add(item)
        resolved.append(item)
    missing = [str(path) for path in resolved if not path.is_file()]
    existing = [path for path in resolved if path.is_file()]
    return {
        "source_count": len(resolved),
        "existing_source_count": len(existing),
        "missing_sources": missing,
        "source_bytes_total": sum(path.stat().st_size for path in existing),
        "source_paths": [str(path) for path in resolved],
        "source_set_origin": "frontend workload input records",
    }


def _deep_oracle() -> dict[str, Any]:
    top = "uart_deep_x_tb"
    paths = [
        top,
        f"{top}.u_apb_bridge",
        f"{top}.u_apb_bridge.u_uart",
        f"{top}.u_apb_bridge.u_uart.u_control",
        f"{top}.u_apb_bridge.u_uart.u_control.u_rx_channel",
        f"{top}.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo",
        f"{top}.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank",
        f"{top}.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank.u_x_cell",
    ]
    return {
        "oracle_type": "tracked_source_hand_annotation",
        "top": top,
        "expected_instance_paths": paths,
        "expected_source_locations": [
            {
                "kind": "definition",
                "name": "uart_x_storage_cell",
                "file_suffix": "tests/fixtures/deep_x_npi/rtl/deep_uart_x.sv",
                "line": 14,
            },
            {
                "kind": "procedural_block",
                "procedure_kind": "AlwaysFF",
                "file_suffix": "tests/fixtures/deep_x_npi/rtl/deep_uart_x.sv",
                "line": 20,
            },
        ],
    }


def _load_compile_result(log_path: Path, simulator: str) -> dict[str, Any]:
    from src.compile_log_parser import parse_compile_log

    try:
        return parse_compile_log(str(log_path), simulator)
    except Exception as exc:
        raise SpikeInputError(
            "compile_log_invalid",
            f"cannot parse compile log {log_path}: {type(exc).__name__}: {exc}",
        ) from exc


def build_deep_workload() -> dict[str, Any]:
    rtl = _require_nonempty_file(DEEP_RTL, "deep_x_npi RTL")
    tb = _require_nonempty_file(DEEP_TB, "deep_x_npi testbench")
    top = "uart_deep_x_tb"
    if DEEP_COMPILE_LOG.is_file() and DEEP_COMPILE_LOG.stat().st_size:
        compile_result = _load_compile_result(DEEP_COMPILE_LOG.resolve(), "vcs")
        command = str(compile_result.get("compile_command") or "")
        compile_log_status = "available_local_ignored_artifact"
    else:
        command = (
            f"vcs -full64 -sverilog -timescale=1ns/1ps -debug_access+all "
            f"-kdb {shlex.quote(str(rtl))} {shlex.quote(str(tb))} "
            f"-top {top} -o simv -l compile.log"
        )
        compile_log_status = "missing_using_tracked_run_script_equivalent"
    translation = translate_vcs_invocation(command, top=top, fallback_sources=[rtl, tb])
    return {
        "name": "deep_x_npi",
        "kind": "seven_level_positional_hierarchy",
        "simulator": "vcs",
        "top": top,
        "compile_log": str(DEEP_COMPILE_LOG.resolve()),
        "compile_log_status": compile_log_status,
        "waveform": str(DEEP_FSDB.resolve()),
        "waveform_role": "development_oracle_only_not_opened_by_frontend_worker",
        "environment": {},
        "environment_provenance": {},
        "source_facts": _source_facts([rtl, tb]),
        "translation": translation,
        "manual_oracle": _deep_oracle(),
        "diagnostic_policy": {
            "suppressed_option": "unknown-sys-name",
            "reason": "FSDB dump tasks are external simulator runtime hooks",
            "risk": "connectivity implemented inside external system calls is not modeled",
        },
    }


def build_hand_workload() -> dict[str, Any]:
    source = _require_nonempty_file(HAND_SOURCE, "hand-written frontend fixture")
    oracle = _read_json(_require_nonempty_file(HAND_ORACLE, "hand oracle"))
    top = str(oracle["top"])
    command = (
        f"vcs -full64 -sverilog -timescale=1ns/1ps +define+HAND_SPIKE=1 "
        f"-debug_access+all {shlex.quote(str(source))} -top {top}"
    )
    return {
        "name": "hand_fixture",
        "kind": "annotated_language_feature_fixture",
        "simulator": "vcs",
        "top": top,
        "compile_log": None,
        "compile_log_status": "not_applicable_hand_written_invocation",
        "waveform": None,
        "waveform_role": "not_required",
        "environment": {},
        "environment_provenance": {},
        "source_facts": _source_facts([source]),
        "translation": translate_vcs_invocation(
            command, top=top, fallback_sources=[source]
        ),
        "manual_oracle": oracle,
        "diagnostic_policy": {
            "suppressed_option": "unknown-sys-name",
            "reason": "same explicit probe profile across workloads",
            "risk": "fixture contains no external system calls",
        },
    }


def build_real_workload(
    compile_log: Path,
    explicit_environment: dict[str, str],
    simulator: str = "auto",
    waveform: Path | None = None,
) -> dict[str, Any]:
    log_path = _require_nonempty_file(compile_log, "real compile log")
    selected_simulator = (
        _detect_real_simulator(log_path) if simulator == "auto" else simulator
    )
    compile_result = _load_compile_result(log_path, selected_simulator)
    xcelium_sources: list[Path] = []
    xcelium_uvm_library: dict[str, Any] | None = None
    if selected_simulator == "xcelium":
        command, xcelium_sources = _extract_xcelium_invocation(log_path)
        xcelium_uvm_library = _extract_xcelium_uvm_library(log_path)
        if xcelium_uvm_library is not None:
            xcelium_sources.extend(
                Path(path) for path in xcelium_uvm_library.get("sources", [])
            )
    else:
        command = str(compile_result.get("compile_command") or "")
    if not command:
        raise SpikeInputError(
            "compile_log_invalid",
            f"real compile log has no captured command: {log_path}",
        )
    tops = list(compile_result.get("top_modules") or [])
    if not tops:
        raise SpikeInputError(
            "compile_log_invalid",
            f"real compile log recovered no top module: {log_path}",
        )
    top = str(tops[0])
    compile_cwd = _real_compile_cwd(log_path)
    source_paths = [*_extract_direct_design_files(log_path), *xcelium_sources]
    inferred, provenance = _infer_real_environment(log_path, command, source_paths)
    environment = {**inferred, **explicit_environment}
    for name in explicit_environment:
        provenance[name] = "explicit_cli"
    filelists, unresolved = _discover_filelist_requirements(
        command, compile_cwd, environment
    )
    if unresolved:
        raise SpikeInputError(
            "missing_environment",
            "real compile filelists reference unresolved environment variables: "
            + ", ".join(unresolved),
        )
    source_paths.extend(_direct_command_sources(command, compile_cwd, environment))
    source_paths.extend(_discover_filelist_sources(command, compile_cwd, environment))
    facts = _source_facts(source_paths)
    if facts["existing_source_count"] == 0:
        raise SpikeInputError(
            "fixture_missing",
            "real compile log has no existing HDL inputs",
        )
    translation = (
        translate_xcelium_invocation(
            command,
            top=top,
            fallback_sources=source_paths,
            xcelium_uvm_library=xcelium_uvm_library,
        )
        if selected_simulator == "xcelium"
        else translate_vcs_invocation(command, top=top, fallback_sources=source_paths)
    )
    waveform_path = (
        str(waveform.expanduser().resolve())
        if waveform is not None
        else str(log_path.parent / "top_tb.fsdb")
    )
    simulator_label = "Xcelium" if selected_simulator == "xcelium" else "VCS"
    return {
        "name": "real_uvm",
        "kind": f"local_{selected_simulator}_uvm_package_vendor_options",
        "simulator": selected_simulator,
        "simulator_version": _simulator_version_from_log(log_path, selected_simulator),
        "top": top,
        "compile_log": str(log_path),
        "compile_log_status": "available_local_real_workload",
        "compile_cwd": str(compile_cwd),
        "waveform": waveform_path,
        "waveform_role": "development_oracle_only_not_opened_by_frontend_worker",
        "environment": environment,
        "environment_provenance": provenance,
        "filelists": filelists,
        "simulator_source_libraries": (
            [xcelium_uvm_library] if xcelium_uvm_library is not None else []
        ),
        "source_facts": facts,
        "translation": translation,
        "manual_oracle": {
            "oracle_type": f"{simulator_label}_compile_log",
            "top": top,
            "expected_instance_paths": [top],
            "expected_source_locations": [],
            "simulator_version": _simulator_version_from_log(
                log_path, selected_simulator
            ),
        },
        "diagnostic_policy": {
            "suppressed_option": "unknown-sys-name",
            "reason": (
                f"{simulator_label} UVM and testbench sources call vendor atomics, stack helpers, "
                "and FSDB dump tasks that are external to HDL connectivity"
            ),
            "risk": (
                "DPI/system-call behavior is excluded; any connectivity created only "
                "by those calls remains unsupported"
            ),
            "excluded_inputs": {
                "native_dpi_implementations": (
                    "C/C++/object/shared-library inputs are recorded but not parsed"
                ),
                "runtime_system_tasks": (
                    "unknown simulator system-task diagnostics are suppressed only; "
                    "HDL arguments and surrounding processes remain parsed"
                ),
                "protected_source": (
                    "legacy envelopes are enabled; encrypted payloads that the frontend "
                    "cannot decode must remain an explicit blocker, never a fallback"
                ),
            },
        },
    }


def build_workloads(args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = args.workload or list(WORKLOAD_NAMES)
    explicit_environment = _parse_name_values(args.real_env)
    builders: dict[str, Callable[[], dict[str, Any]]] = {
        "deep_x_npi": build_deep_workload,
        "hand_fixture": build_hand_workload,
        "real_uvm": lambda: build_real_workload(
            args.real_compile_log,
            explicit_environment,
            simulator=args.real_simulator,
            waveform=args.real_waveform,
        ),
    }
    result: list[dict[str, Any]] = []
    for name in selected:
        try:
            result.append(builders[name]())
        except SpikeError as exc:
            result.append(
                {
                    "name": name,
                    "status": "blocked",
                    "blockers": [
                        {"code": exc.error_code, "message": str(exc), "phase": "input"}
                    ],
                }
            )
    return result


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


def _measure_phase(fn: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
    rss_start = _read_proc_rss_kib()
    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    value = fn()
    cpu_ms = (time.process_time_ns() - cpu_start) / 1_000_000
    wall_ms = (time.perf_counter_ns() - wall_start) / 1_000_000
    rss_end = _read_proc_rss_kib()
    return value, {
        "wall_time_ms": _round_ms(wall_ms),
        "cpu_time_ms": _round_ms(cpu_ms),
        "rss_start_kib": rss_start["current"],
        "rss_peak_kib": rss_end["high_water"],
        "rss_end_kib": rss_end["current"],
    }


@contextlib.contextmanager
def _discard_process_stdout():
    """Temporarily discard Python and C++ stdout used by runPreprocessor()."""
    sys.stdout.flush()
    saved = os.dup(1)
    sink = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink, 1)
        yield
        ctypes.CDLL(None).fflush(None)
        sys.stdout.flush()
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(sink)


@contextlib.contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


def _configure_driver(driver_module: Any, frontend_args: Sequence[str]) -> Any:
    driver = driver_module.Driver()
    driver.addStandardArgs()
    options = driver_module.CommandLineOptions()
    options.ignoreProgramName = True
    options.expandEnvVars = True
    options.supportsComments = True
    command = shlex.join(list(frontend_args))
    parsed = bool(driver.parseCommandLine(command, options))
    processed = bool(driver.processOptions()) if parsed else False
    errors = list(driver.sourceLoader.errors)
    if not parsed or not processed or errors:
        raise SpikeExecutionError(
            "frontend_option_failure",
            "frontend rejected translated options: "
            + json.dumps(
                {"parsed": parsed, "processed": processed, "errors": errors},
                ensure_ascii=False,
            ),
        )
    return driver


def _location(source_manager: Any, location: Any) -> dict[str, Any]:
    try:
        file_name = str(source_manager.getFileName(location))
        line = int(source_manager.getLineNumber(location))
        column = int(source_manager.getColumnNumber(location))
    except Exception:
        return {"file": None, "line": None, "column": None}
    if file_name:
        path = Path(file_name)
        if not path.is_absolute():
            candidate = (ROOT / path).resolve()
            if candidate.exists():
                file_name = str(candidate)
    return {"file": file_name or None, "line": line, "column": column}


def _diag_code_name(code: Any) -> str:
    rendered = str(code)
    if rendered.startswith("DiagCode(") and rendered.endswith(")"):
        return rendered[len("DiagCode(") : -1]
    return rendered


def _diagnostics_payload(driver: Any, diagnostics: Sequence[Any]) -> dict[str, Any]:
    engine = driver.diagEngine
    source_manager = driver.sourceManager
    by_severity: collections.Counter[str] = collections.Counter()
    by_code: collections.Counter[str] = collections.Counter()
    items: list[dict[str, Any]] = []
    suppressed_unknown = 0
    for diagnostic in diagnostics:
        code = _diag_code_name(diagnostic.code)
        try:
            severity = engine.getSeverity(diagnostic.code, diagnostic.location).name
        except Exception:
            severity = "Unknown"
        by_severity[severity] += 1
        by_code[code] += 1
        option = engine.getOptionName(diagnostic.code)
        if option == "unknown-sys-name" and severity == "Ignored":
            suppressed_unknown += 1
        if severity in {"Error", "Fatal", "Warning"} or option == "unknown-sys-name":
            if len(items) < 100:
                items.append(
                    {
                        "code": code,
                        "option": option or None,
                        "severity": severity,
                        "message": engine.formatMessage(diagnostic),
                        **_location(source_manager, diagnostic.location),
                    }
                )
    blocking = by_severity["Error"] + by_severity["Fatal"]
    return {
        "total": len(diagnostics),
        "blocking_error_count": blocking,
        "by_effective_severity": dict(sorted(by_severity.items())),
        "by_code": dict(sorted(by_code.items())),
        "items": items,
        "items_truncated": len(items)
        < sum(
            count
            for severity, count in by_severity.items()
            if severity in {"Error", "Fatal", "Warning"}
        )
        + suppressed_unknown,
        "explicitly_suppressed_unknown_system_count": suppressed_unknown,
        "suppression_receipt": {
            "option": "-Wno-unknown-sys-name",
            "scope": "external simulator runtime hooks only",
            "not_modeled": "DPI/system-task behavior",
        },
    }


def _extract_frontend_objects(
    pyslang: Any, ast: Any, compilation: Any, root: Any, driver: Any
) -> dict[str, Any]:
    source_manager = driver.sourceManager
    definitions: list[dict[str, Any]] = []
    for definition in compilation.getDefinitions():
        definitions.append(
            {
                "name": definition.name,
                "definition_kind": definition.definitionKind.name,
                "instance_count": int(definition.instanceCount),
                **_location(source_manager, definition.location),
            }
        )
    definitions.sort(
        key=lambda item: (item["name"], item["file"] or "", item["line"] or 0)
    )

    instances: list[dict[str, Any]] = []
    procedural_blocks: list[dict[str, Any]] = []
    symbol_counts: collections.Counter[str] = collections.Counter()
    procedural_kind_counts: collections.Counter[str] = collections.Counter()
    port_connection_count = 0

    def count_symbol(symbol: Any) -> None:
        symbol_counts[symbol.kind.name] += 1

    def record_instance(instance: Any) -> None:
        nonlocal port_connection_count
        count_symbol(instance)
        try:
            connections = len(instance.portConnections)
        except Exception:
            connections = 0
        port_connection_count += connections
        if len(instances) < 1000:
            instances.append(
                {
                    "path": instance.hierarchicalPath,
                    "name": instance.name,
                    "definition": instance.definition.name,
                    "instance_kind": "interface" if instance.isInterface else "module",
                    "port_connection_count": connections,
                    "instance_location": _location(source_manager, instance.location),
                    "definition_location": _location(
                        source_manager, instance.definition.location
                    ),
                }
            )

    def record_procedural(block: Any) -> None:
        count_symbol(block)
        kind = block.procedureKind.name
        procedural_kind_counts[kind] += 1
        if len(procedural_blocks) < 1000:
            procedural_blocks.append(
                {
                    "procedure_kind": kind,
                    "path": block.hierarchicalPath,
                    **_location(source_manager, block.location),
                }
            )

    interesting = [
        ast.SymbolKind.ContinuousAssign,
        ast.SymbolKind.GenerateBlock,
        ast.SymbolKind.GenerateBlockArray,
        ast.SymbolKind.InstanceArray,
        ast.SymbolKind.InterfacePort,
        ast.SymbolKind.Modport,
        ast.SymbolKind.Net,
        ast.SymbolKind.Package,
        ast.SymbolKind.Port,
        ast.SymbolKind.Variable,
    ]
    lookup_table: dict[Any, Callable[[Any], None]] = {
        kind: count_symbol for kind in interesting
    }
    lookup_table[ast.SymbolKind.Instance] = record_instance
    lookup_table[ast.SymbolKind.ProceduralBlock] = record_procedural
    root.visit(lookup_table=lookup_table)
    instances.sort(key=lambda item: item["path"])
    procedural_blocks.sort(
        key=lambda item: (item["file"] or "", item["line"] or 0, item["path"])
    )

    buffer_counts: collections.Counter[str] = collections.Counter()
    for buffer_id in source_manager.getAllBuffers():
        buffer_counts[source_manager.getBufferKind(buffer_id).name] += 1
    top_instances = [instance.hierarchicalPath for instance in root.topInstances]
    return {
        "tops": top_instances,
        "instances": instances,
        "instances_truncated": symbol_counts["Instance"] > len(instances),
        "definitions": definitions,
        "procedural_blocks": procedural_blocks,
        "procedural_blocks_truncated": (
            symbol_counts["ProceduralBlock"] > len(procedural_blocks)
        ),
        "object_counts": {
            "syntax_trees": len(driver.syntaxTrees),
            "source_buffers": len(source_manager.getAllBuffers()),
            "source_buffers_by_kind": dict(sorted(buffer_counts.items())),
            "compilation_units": len(compilation.getCompilationUnits()),
            "definitions": len(definitions),
            "packages": len(compilation.getPackages()),
            "symbols_by_kind": dict(sorted(symbol_counts.items())),
            "procedural_blocks_by_kind": dict(sorted(procedural_kind_counts.items())),
            "port_connections": port_connection_count,
            "dpi_exports": len(compilation.getDPIExports()),
        },
        "graph_object_counts": {
            "available": False,
            "reason": (
                "Phase 0B does not build a TraceWeave Connectivity IR; reporting "
                "only objects directly exposed by the frontend"
            ),
        },
        "count_scope": (
            "facts exposed by pyslang's syntax trees, source manager, compilation, "
            "and elaborated symbol visitor; not inferred graph edges"
        ),
    }


def run_frontend_worker(spec: dict[str, Any]) -> dict[str, Any]:
    overall_rss = _read_proc_rss_kib()
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    phases: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []

    def import_frontend() -> tuple[Any, Any, Any, str]:
        import importlib.metadata
        import pyslang
        import pyslang.ast as ast
        from pyslang import driver as driver_module

        return pyslang, ast, driver_module, importlib.metadata.version("pyslang")

    try:
        imported, phases["frontend_import"] = _measure_phase(import_frontend)
        pyslang, ast, driver_module, version = imported
    except Exception as exc:
        end = _read_proc_rss_kib()
        return {
            "status": "blocked",
            "frontend": {"name": FRONTEND_NAME, "version": None},
            "blockers": [
                {
                    "code": "frontend_unavailable",
                    "phase": "frontend_import",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
            "phases": phases,
            "process_rss_kib": {
                "start": overall_rss["current"],
                "peak": end["high_water"],
                "end": end["current"],
            },
        }

    if version != FRONTEND_VERSION:
        blockers.append(
            {
                "code": "frontend_version_mismatch",
                "phase": "frontend_import",
                "message": f"expected pyslang {FRONTEND_VERSION}, found {version}",
            }
        )

    frontend_args = list(spec["translation"]["frontend_args"])
    with _temporary_environment(dict(spec.get("environment") or {})):
        try:
            preprocessor_driver, phases["preprocess_setup"] = _measure_phase(
                lambda: _configure_driver(driver_module, frontend_args)
            )

            def preprocess() -> bool:
                with _discard_process_stdout():
                    return bool(
                        preprocessor_driver.runPreprocessor(
                            pyslang.PreprocessOutputFlags(0)
                        )
                    )

            preprocess_ok, phases["preprocess"] = _measure_phase(preprocess)
            if not preprocess_ok:
                blockers.append(
                    {
                        "code": "preprocess_failed",
                        "phase": "preprocess",
                        "message": "Driver.runPreprocessor returned false",
                    }
                )

            parse_driver, phases["parse_setup"] = _measure_phase(
                lambda: _configure_driver(driver_module, frontend_args)
            )
            parse_ok, phases["parse"] = _measure_phase(parse_driver.parseAllSources)
            if not parse_ok:
                blockers.append(
                    {
                        "code": "parse_failed",
                        "phase": "parse",
                        "message": "Driver.parseAllSources returned false",
                    }
                )

            def elaborate() -> tuple[Any, Any, list[Any]]:
                compilation = parse_driver.createCompilation()
                root = compilation.getRoot()
                diagnostics = list(compilation.getAllDiagnostics())
                return compilation, root, diagnostics

            elaborated, phases["elaboration"] = _measure_phase(elaborate)
            compilation, root, diagnostics = elaborated
            diagnostic_payload = _diagnostics_payload(parse_driver, diagnostics)
            recovered, phases["object_extraction"] = _measure_phase(
                lambda: _extract_frontend_objects(
                    pyslang, ast, compilation, root, parse_driver
                )
            )
        except SpikeError as exc:
            blockers.append(
                {"code": exc.error_code, "phase": "frontend", "message": str(exc)}
            )
            diagnostic_payload = {
                "total": 0,
                "blocking_error_count": 0,
                "by_effective_severity": {},
                "by_code": {},
                "items": [],
                "items_truncated": False,
                "explicitly_suppressed_unknown_system_count": 0,
            }
            recovered = {
                "tops": [],
                "instances": [],
                "definitions": [],
                "procedural_blocks": [],
                "object_counts": {},
            }
        except Exception as exc:
            blockers.append(
                {
                    "code": "frontend_exception",
                    "phase": "frontend",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            diagnostic_payload = {
                "total": 0,
                "blocking_error_count": 0,
                "by_effective_severity": {},
                "by_code": {},
                "items": [],
                "items_truncated": False,
                "explicitly_suppressed_unknown_system_count": 0,
            }
            recovered = {
                "tops": [],
                "instances": [],
                "definitions": [],
                "procedural_blocks": [],
                "object_counts": {},
            }

    if diagnostic_payload["blocking_error_count"]:
        blockers.append(
            {
                "code": "blocking_frontend_diagnostics",
                "phase": "elaboration",
                "message": (
                    f"{diagnostic_payload['blocking_error_count']} effective error/fatal "
                    "diagnostics remain"
                ),
            }
        )
    expected_top = spec.get("top")
    if expected_top and expected_top not in recovered.get("tops", []):
        blockers.append(
            {
                "code": "top_not_recovered",
                "phase": "elaboration",
                "message": f"expected top {expected_top!r}, got {recovered.get('tops', [])!r}",
            }
        )

    end_rss = _read_proc_rss_kib()
    overall_cpu_ms = (time.process_time_ns() - cpu_started) / 1_000_000
    overall_wall_ms = (time.perf_counter_ns() - wall_started) / 1_000_000
    suppressed = diagnostic_payload.get("explicitly_suppressed_unknown_system_count", 0)
    status = (
        "blocked"
        if blockers
        else ("supported_with_explicit_exclusions" if suppressed else "supported")
    )
    return {
        "status": status,
        "frontend": {
            "name": FRONTEND_NAME,
            "package": FRONTEND_PACKAGE,
            "version": version,
            "source": FRONTEND_SOURCE,
        },
        "invocation": {
            "api": "pyslang.driver.Driver",
            "arguments": frontend_args,
            "rendered": spec["translation"]["frontend_invocation"],
            "environment": dict(spec.get("environment") or {}),
            "working_directory": str(Path.cwd().resolve()),
        },
        "phase_measurements": phases,
        "phase_semantics": {
            "preprocess": (
                "independent Driver.runPreprocessor pass; emitted text discarded"
            ),
            "parse": (
                "Driver.parseAllSources; includes preprocessing again because pyslang "
                "does not expose a reusable preprocessed-token handoff in this driver API"
            ),
            "elaboration": ("createCompilation + getRoot + getAllDiagnostics"),
            "object_extraction": "read-only elaborated symbol/count projection",
        },
        "worker_wall_time_ms": _round_ms(overall_wall_ms),
        "worker_cpu_time_ms": _round_ms(overall_cpu_ms),
        "process_rss_kib": {
            "start": overall_rss["current"],
            "peak": end_rss["high_water"],
            "end": end_rss["current"],
        },
        "diagnostics": diagnostic_payload,
        "recovered": recovered,
        "blockers": blockers,
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _diagnostic_semantic_identities(
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    fields = ("code", "option", "severity", "file", "line", "column")
    identities = [
        {field: item.get(field) for field in fields}
        for item in (diagnostics.get("items") or [])
    ]
    return sorted(
        identities,
        key=lambda item: tuple(
            "" if item[field] is None else str(item[field]) for field in fields
        ),
    )


def _semantic_projection(worker: dict[str, Any]) -> dict[str, Any]:
    diagnostics = worker.get("diagnostics") or {}
    return {
        "status": worker.get("status"),
        "frontend": worker.get("frontend"),
        # Ignore purely advisory diagnostic aggregate jitter when deciding
        # whether recovered connectivity semantics are stable.  Blocking
        # counts and every emitted error/warning/unknown-system identity remain
        # part of this projection. Formatted messages can name whichever generic
        # specialization Slang visited first, so they remain in the evidence but
        # are not cache identity. Full diagnostic stability is reported separately.
        "diagnostics": {
            "blocking_error_count": diagnostics.get("blocking_error_count"),
            "explicitly_suppressed_unknown_system_count": diagnostics.get(
                "explicitly_suppressed_unknown_system_count"
            ),
            "item_identities": _diagnostic_semantic_identities(diagnostics),
            "items_truncated": diagnostics.get("items_truncated"),
        },
        "recovered": worker.get("recovered"),
        "blockers": worker.get("blockers"),
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    rank = percentile / 100.0 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * fraction)


def _distribution(values: Sequence[float | int]) -> dict[str, Any]:
    numbers = [float(value) for value in values]
    if not numbers:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(numbers),
        "min": _round_ms(min(numbers)),
        "p50": _round_ms(_percentile(numbers, 50)),
        "p95": _round_ms(_percentile(numbers, 95)),
        "max": _round_ms(max(numbers)),
    }


def _optional_distribution(values: Sequence[int | None]) -> dict[str, Any]:
    present = [value for value in values if value is not None]
    result = _distribution(present)
    return {"supported": len(present) == len(values), **result}


def _source_location_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    file_name = actual.get("file")
    suffix = expected.get("file_suffix")
    if suffix and (not file_name or not str(file_name).endswith(str(suffix))):
        return False
    if expected.get("line") is not None and actual.get("line") != expected["line"]:
        return False
    if expected.get("name") is not None and actual.get("name") != expected["name"]:
        return False
    if (
        expected.get("procedure_kind") is not None
        and actual.get("procedure_kind") != expected["procedure_kind"]
    ):
        return False
    return True


def _compare_one_oracle(
    recovered: dict[str, Any], oracle: dict[str, Any], oracle_name: str
) -> dict[str, Any]:
    actual_tops = set(recovered.get("tops") or [])
    actual_paths = {item["path"] for item in recovered.get("instances") or []}
    expected_top = oracle.get("top") or oracle.get("expected_top")
    expected_paths = set(oracle.get("expected_instance_paths") or [])
    signal_paths = list(oracle.get("signal_paths") or [])
    signal_scopes = {
        path.rsplit(".", 1)[0] if "." in path else path for path in signal_paths
    }
    missing_locations: list[dict[str, Any]] = []
    for expected in (
        oracle.get("expected_source_locations") or oracle.get("source_locations") or []
    ):
        candidates = (
            recovered.get("procedural_blocks") or []
            if expected.get("kind") == "procedural_block"
            else recovered.get("definitions") or []
        )
        if not any(_source_location_matches(actual, expected) for actual in candidates):
            missing_locations.append(expected)
    missing_paths = sorted(expected_paths - actual_paths)
    missing_signal_scopes = sorted(signal_scopes - actual_paths)
    top_matches = expected_top is None or expected_top in actual_tops
    matches = (
        top_matches
        and not missing_paths
        and not missing_signal_scopes
        and not missing_locations
    )
    return {
        "oracle": oracle_name,
        "available": True,
        "matches": matches,
        "expected_top": expected_top,
        "recovered_tops": sorted(actual_tops),
        "missing_instance_paths": missing_paths,
        "signal_paths_checked": signal_paths,
        "missing_signal_parent_scopes": missing_signal_scopes,
        "missing_source_locations": missing_locations,
    }


def compare_oracles(
    recovered: dict[str, Any],
    manual_oracle: dict[str, Any] | None,
    supplemental: dict[str, Any] | None,
) -> dict[str, Any]:
    comparisons: list[dict[str, Any]] = []
    if manual_oracle:
        comparisons.append(_compare_one_oracle(recovered, manual_oracle, "manual"))
    if supplemental:
        for name, value in sorted(supplemental.items()):
            if isinstance(value, dict) and value.get("available", True):
                comparisons.append(_compare_one_oracle(recovered, value, name))
            elif isinstance(value, dict):
                comparisons.append(
                    {
                        "oracle": name,
                        "available": False,
                        "matches": None,
                        "blocker": value.get("blocker"),
                    }
                )
    available = [item for item in comparisons if item.get("available")]
    return {
        "comparisons": comparisons,
        "all_available_oracles_match": bool(available)
        and all(bool(item.get("matches")) for item in available),
        "development_oracle_policy": (
            "FSDB/KDB/NPI evidence is optional input to this harness and is never "
            "a frontend or automated-test dependency"
        ),
    }


def _worker_command(frontend_python: Path, spec_path: Path) -> list[str]:
    return [
        str(frontend_python),
        str(Path(__file__).resolve()),
        "--_worker-spec",
        str(spec_path),
    ]


def _invoke_worker(
    frontend_python: Path, spec: dict[str, Any], timeout: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    configured_cwd = spec.get("compile_cwd")
    worker_cwd = (
        Path(str(configured_cwd)).expanduser().resolve()
        if configured_cwd is not None
        else ROOT
    )
    if not worker_cwd.is_dir():
        raise SpikeExecutionError(
            "worker_cwd_missing",
            f"frontend worker compile working directory is unavailable: {worker_cwd}",
        )
    with tempfile.TemporaryDirectory(prefix="traceweave-frontend-spike-") as tmp:
        spec_path = Path(tmp) / "workload.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter_ns()
        try:
            process = subprocess.run(
                _worker_command(frontend_python, spec_path),
                cwd=worker_cwd,
                env={**os.environ, "PYTHONHASHSEED": "0"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpikeExecutionError(
                "worker_timeout", f"frontend worker exceeded {timeout:g}s"
            ) from exc
        wall_ms = (time.perf_counter_ns() - started) / 1_000_000
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_ms = 1_000.0 * (
            usage_after.ru_utime
            + usage_after.ru_stime
            - usage_before.ru_utime
            - usage_before.ru_stime
        )
        receipt = {
            "process_wall_time_ms": _round_ms(wall_ms),
            "process_cpu_time_ms": _round_ms(cpu_ms),
            "returncode": process.returncode,
        }
        if process.returncode != 0:
            detail = (
                process.stderr.strip() or process.stdout.strip() or "no child output"
            )
            raise SpikeExecutionError(
                "worker_failed",
                f"frontend worker exited {process.returncode}: {detail}",
            )
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise SpikeExecutionError(
                "worker_protocol_error",
                f"frontend worker returned invalid JSON: {exc}; stderr={process.stderr!r}",
            ) from exc
        return payload, receipt


def _summarize_workload(
    spec: dict[str, Any],
    workers: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    supplemental_oracle: dict[str, Any] | None,
) -> dict[str, Any]:
    fingerprints = [
        hashlib.sha256(_canonical_json(_semantic_projection(worker))).hexdigest()
        for worker in workers
    ]
    diagnostic_fingerprints = [
        hashlib.sha256(_canonical_json(worker.get("diagnostics") or {})).hexdigest()
        for worker in workers
    ]
    representative = workers[0]
    phase_names = sorted(
        {
            phase
            for worker in workers
            for phase in (worker.get("phase_measurements") or {})
        }
    )
    phase_summary: dict[str, Any] = {}
    for phase in phase_names:
        values = [
            worker["phase_measurements"][phase]
            for worker in workers
            if phase in worker.get("phase_measurements", {})
        ]
        phase_summary[phase] = {
            "wall_time_ms": _distribution([item["wall_time_ms"] for item in values]),
            "cpu_time_ms": _distribution([item["cpu_time_ms"] for item in values]),
            "rss_start_kib": _optional_distribution(
                [item["rss_start_kib"] for item in values]
            ),
            "rss_peak_kib": _optional_distribution(
                [item["rss_peak_kib"] for item in values]
            ),
            "rss_end_kib": _optional_distribution(
                [item["rss_end_kib"] for item in values]
            ),
        }
    oracle_comparison = compare_oracles(
        representative.get("recovered") or {},
        spec.get("manual_oracle"),
        supplemental_oracle,
    )
    stable = len(set(fingerprints)) == 1
    stable_full_diagnostics = len(set(diagnostic_fingerprints)) == 1
    diagnostic_codes = sorted(
        {
            code
            for worker in workers
            for code in (worker.get("diagnostics") or {}).get("by_code", {})
        }
    )
    varying_diagnostic_codes: dict[str, Any] = {}
    for code in diagnostic_codes:
        values = [
            int((worker.get("diagnostics") or {}).get("by_code", {}).get(code, 0))
            for worker in workers
        ]
        if min(values) != max(values):
            varying_diagnostic_codes[code] = _distribution(values)
    blockers = [
        blocker for worker in workers for blocker in (worker.get("blockers") or [])
    ]
    if blockers:
        status = "blocked"
    elif not oracle_comparison["all_available_oracles_match"]:
        status = "oracle_mismatch"
    elif representative.get("status") == "supported_with_explicit_exclusions":
        status = "supported_with_explicit_exclusions"
    else:
        status = "supported"
    samples = []
    for worker, receipt, fingerprint, diagnostic_fingerprint in zip(
        workers, receipts, fingerprints, diagnostic_fingerprints
    ):
        samples.append(
            {
                **receipt,
                "worker_wall_time_ms": worker.get("worker_wall_time_ms"),
                "worker_cpu_time_ms": worker.get("worker_cpu_time_ms"),
                "process_rss_kib": worker.get("process_rss_kib"),
                "semantic_fingerprint_sha256": fingerprint,
                "full_diagnostic_fingerprint_sha256": diagnostic_fingerprint,
            }
        )
    return {
        **spec,
        "status": status,
        "cold_process": {
            "sample_count": len(workers),
            "stable_semantics": stable,
            "diagnostic_stability": {
                "stable_full_payload": stable_full_diagnostics,
                "interpretation": (
                    "full diagnostics include advisory/ignored lint aggregates; "
                    "semantic stability still includes all blocking counts and "
                    "emitted error/warning/unknown-system items"
                ),
                "total": _distribution(
                    [
                        int((worker.get("diagnostics") or {}).get("total", 0))
                        for worker in workers
                    ]
                ),
                "blocking_error_count": _distribution(
                    [
                        int(
                            (worker.get("diagnostics") or {}).get(
                                "blocking_error_count", 0
                            )
                        )
                        for worker in workers
                    ]
                ),
                "suppressed_unknown_system_count": _distribution(
                    [
                        int(
                            (worker.get("diagnostics") or {}).get(
                                "explicitly_suppressed_unknown_system_count", 0
                            )
                        )
                        for worker in workers
                    ]
                ),
                "varying_by_code": varying_diagnostic_codes,
            },
            "process_wall_time_ms": _distribution(
                [item["process_wall_time_ms"] for item in receipts]
            ),
            "process_cpu_time_ms": _distribution(
                [item["process_cpu_time_ms"] for item in receipts]
            ),
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
            "phase_measurements": phase_summary,
            "samples": samples,
        },
        "representative_frontend_result": representative,
        "oracle_comparison": oracle_comparison,
        "blockers": blockers,
    }


def _dependency_receipt(frontend_python: Path) -> dict[str, Any]:
    env_path = "/tmp/traceweave-phase0b-pyslang-11.0.0"
    return {
        "package": FRONTEND_PACKAGE,
        "version": FRONTEND_VERSION,
        "source": FRONTEND_SOURCE,
        "upstream_commit": FRONTEND_UPSTREAM_COMMIT,
        "wheel": {
            "filename": FRONTEND_WHEEL,
            "url": FRONTEND_WHEEL_URL,
            "bytes": FRONTEND_WHEEL_BYTES,
            "sha256": FRONTEND_WHEEL_SHA256,
            "platform": "CPython 3.11 manylinux_2_27/2_28 x86_64",
        },
        "requirements_file": str(FRONTEND_REQUIREMENTS),
        # Do not resolve this path: venv ``bin/python`` is normally a symlink to
        # the base interpreter, and following it drops the venv's site-packages.
        "selected_interpreter": str(frontend_python),
        "isolation": "fresh child processes using a /tmp virtual environment",
        "reproduction_commands": [
            f"python3.11 -m venv {env_path}",
            (
                f"{env_path}/bin/python -m pip install --only-binary=:all: "
                f"--require-hashes -r {FRONTEND_REQUIREMENTS}"
            ),
        ],
        "system_python_modified": False,
    }


def _environment_receipt() -> dict[str, Any]:
    return {
        "orchestrator_python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "machine": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "rss_source": "/proc/self/status VmRSS/VmHWM (KiB)",
        "orchestrator_frontend_availability": {
            "pyslang": importlib.util.find_spec("pyslang") is not None,
            "uhdm": importlib.util.find_spec("uhdm") is not None,
            "interpretation": "package presence only; absence is not frontend incompatibility",
        },
    }


def _assessment(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_name = {str(result.get("name")): result for result in results}
    required_present = all(name in by_name for name in WORKLOAD_NAMES)
    accepted_statuses = {"supported", "supported_with_explicit_exclusions"}
    key_gaps: list[dict[str, Any]] = []
    for name in WORKLOAD_NAMES:
        result = by_name.get(name)
        if result is None:
            key_gaps.append({"workload": name, "reason": "not_measured"})
            continue
        if result.get("status") not in accepted_statuses:
            key_gaps.append(
                {
                    "workload": name,
                    "reason": f"status={result.get('status')}",
                    "blockers": result.get("blockers") or [],
                }
            )
        elif not result.get("oracle_comparison", {}).get(
            "all_available_oracles_match", False
        ):
            key_gaps.append({"workload": name, "reason": "oracle_mismatch"})
        elif not result.get("cold_process", {}).get("stable_semantics", False):
            key_gaps.append(
                {"workload": name, "reason": "connectivity_semantics_unstable"}
            )

    slang_supported = required_present and not key_gaps
    return {
        "evidence_scope": (
            "Phase 0B frontend feasibility only: parsing, elaborated hierarchy, "
            "source locations, diagnostics, and frontend-exposed object counts; no "
            "Source Graph edges or production accuracy claim"
        ),
        "key_frontend_gaps": key_gaps,
        "slang_primary_frontend": {
            "decision": (
                "recommended_for_phase_1_prototype"
                if slang_supported
                else "not_yet_supported_by_complete_phase_0b_evidence"
            ),
            "production_status": "not_implemented_not_production_validated",
        },
        "surelog_uhdm_comparison": {
            "performed": False,
            "fallback_needed": False if slang_supported else None,
            "reason": (
                "Slang recovered all required workload tops, annotated hierarchy "
                "paths, and source locations without a key frontend blocker"
                if slang_supported
                else "required workload coverage is incomplete or has a recorded gap"
            ),
        },
        "dependency_model": {
            "recommendation": "optional_extra",
            "base_dependency": False,
            "bundled_artifact": False,
            "reason": (
                "pyslang is a native, platform-specific wheel and source connectivity "
                "is additive to TraceWeave's existing log/wave functionality"
            ),
        },
        "cold_frontend_worker_model": {
            "recommendation": "isolated_worker_process",
            "reason": (
                "preserves hard timeout/crash and RSS isolation for a native frontend; "
                "future session caching can keep a successful worker warm"
            ),
        },
        "cache_fingerprint_contract": {
            "recommendation": "semantic_identity_not_full_diagnostic_payload",
            "includes": [
                "frontend name and version",
                "blocking diagnostic count",
                "diagnostic code/severity/file/line/column/option identities",
                "suppressed unknown-system count",
                "recovered frontend objects",
                "explicit blockers",
            ],
            "excludes": [
                "formatted diagnostic message text",
                "advisory or ignored diagnostic aggregate counts",
                "timing and RSS measurements",
            ],
            "reason": (
                "generic specialization order can change formatted diagnostic text "
                "and advisory counts without changing source location, blocker, or "
                "recovered connectivity facts"
            ),
        },
        "open_questions": [
            "large-design cold build and steady-state cache measurements",
            "vendor protected-source and DPI/system-task exclusion policy",
            "Xcelium-specific option translation on a representative local workload",
            "frontend wheel availability on every supported deployment platform",
        ],
    }


def run_spike(args: argparse.Namespace) -> dict[str, Any]:
    # Preserve a virtual-environment launcher as such.  Path.resolve() follows
    # ``bin/python`` to the base interpreter and silently loses the isolated
    # package environment that this spike is explicitly measuring.
    frontend_python = Path(
        os.path.abspath(os.path.expanduser(os.fspath(args.frontend_python)))
    )
    workloads = build_workloads(args)
    oracle_doc = (
        _read_json(args.oracle_json.expanduser().resolve()) if args.oracle_json else {}
    )
    supplemental_by_workload = oracle_doc.get("workloads", oracle_doc)
    base = {
        "schema_version": SCHEMA_VERSION,
        "spike": SPIKE_NAME,
        "frontend": {
            "name": FRONTEND_NAME,
            "package": FRONTEND_PACKAGE,
            "version": FRONTEND_VERSION,
            "source": FRONTEND_SOURCE,
        },
        "dependency": _dependency_receipt(frontend_python),
        "environment": _environment_receipt(),
        "harness": {
            "script": str(Path(__file__).resolve()),
            "repository_baseline": REPOSITORY_BASELINE,
            "selected_workloads": [workload["name"] for workload in workloads],
            "child_timeout_seconds": args.child_timeout_seconds,
            "oracle_input": (
                str(args.oracle_json.expanduser().resolve())
                if args.oracle_json
                else None
            ),
        },
        "methodology": {
            "execution": (
                "one workload per fresh child interpreter; the parent does not import pyslang"
            ),
            "preprocess_parse": (
                "preprocess is an independent runPreprocessor probe; parseAllSources "
                "necessarily preprocesses again"
            ),
            "rss": (
                "start/end are VmRSS and peak is process-lifetime VmHWM; each cold "
                "sample exits after one workload"
            ),
            "oracles": (
                "tracked hand annotation is mandatory; FSDB/KDB/NPI evidence is "
                "development-only supplemental input"
            ),
            "production_claim": (
                "feasibility data only; no Source Graph backend or routing is implemented"
            ),
        },
    }
    if args.plan_only:
        return {**base, "status": "planned", "workloads": workloads}

    if not frontend_python.is_file() or not os.access(frontend_python, os.X_OK):
        raise SpikeInputError(
            "frontend_python_missing",
            f"frontend interpreter is missing or not executable: {frontend_python}",
        )

    results: list[dict[str, Any]] = []
    for spec in workloads:
        if spec.get("status") == "blocked":
            results.append(spec)
            continue
        workers: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        try:
            for _ in range(args.cold_repeats):
                worker, receipt = _invoke_worker(
                    frontend_python, spec, args.child_timeout_seconds
                )
                workers.append(worker)
                receipts.append(receipt)
            supplemental = (
                supplemental_by_workload.get(spec["name"])
                if isinstance(supplemental_by_workload, dict)
                else None
            )
            results.append(_summarize_workload(spec, workers, receipts, supplemental))
        except SpikeError as exc:
            results.append(
                {
                    **spec,
                    "status": "blocked",
                    "blockers": [
                        {
                            "code": exc.error_code,
                            "phase": "worker",
                            "message": str(exc),
                        }
                    ],
                }
            )

    statuses = [result.get("status") for result in results]
    if all(
        status in {"supported", "supported_with_explicit_exclusions"}
        for status in statuses
    ):
        status = "ok"
    elif any(status == "blocked" for status in statuses):
        status = "blocked"
    else:
        status = "partial"
    return {
        **base,
        "status": status,
        "workload_count": len(results),
        "cold_repeats_per_workload": args.cold_repeats,
        "workloads": results,
        "assessment": _assessment(results),
    }


def _error_payload(exc: SpikeError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "spike": SPIKE_NAME,
        "status": "error",
        "error_code": exc.error_code,
        "message": str(exc),
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args._worker_spec is not None:
            spec = _read_json(args._worker_spec.expanduser().resolve())
            print(
                json.dumps(
                    run_frontend_worker(spec), ensure_ascii=False, sort_keys=True
                )
            )
            return 0
        result = run_spike(args)
        rendered = (
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if args.output is None:
            print(rendered, end="")
        else:
            output_path = Path(
                os.path.abspath(os.path.expanduser(os.fspath(args.output)))
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "output": str(output_path),
                        "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0 if result["status"] in {"ok", "planned"} else 3
    except SpikeError as exc:
        print(
            json.dumps(_error_payload(exc), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        wrapped = SpikeExecutionError("spike_failed", f"{type(exc).__name__}: {exc}")
        print(
            json.dumps(_error_payload(wrapped), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
