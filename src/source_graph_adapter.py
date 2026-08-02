"""Conservative production inputs for scoped Source Graph driver/load builds.

The adapter translates only local compile-log facts that can be replayed by
the isolated Slang worker.  It never starts a worker and never walks an
elaborated design.  Hierarchy scope is resolved by following the requested
signal through the already-built ``component_tree`` one child at a time; the
requested assignment cone is the target instance and the explicit coverage
boundary is that instance plus its ancestors.

Incomplete compile inputs can still produce a diagnostic request, but the
existing build contract makes its key non-reusable.  Unprovable target/top
scope is a structured blocker rather than an implicit full-design scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
from typing import Any

from .cancellation import check_cancelled
from .slang_connectivity_projector import SLANG_FRONTEND_NAME
from .source_graph_contract import (
    BoundaryMode,
    CompileInputManifest,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    RequestedCone,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphIdentity,
    compute_source_graph_build_key,
)


SOURCE_GRAPH_ADAPTER_VERSION = "1.1"
_HDL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_NATIVE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".o", ".a", ".so"}
_ENV_REF_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_FIXED_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_FILELIST_DEPTH = 64
_MAX_FILELIST_TOKENS = 1_000_000

_BASE_OPTIONS = {
    "vcs": (
        "--compat",
        "vcs",
        "--enable-legacy-protect",
        "--single-unit",
        "-Wno-unknown-sys-name",
    ),
    "xcelium": (
        "--compat",
        "all",
        "--enable-legacy-protect",
        "--single-unit",
        "-Wno-unknown-sys-name",
    ),
}

_IGNORED_VALUE_OPTIONS = {
    "-CFLAGS",
    "-LDFLAGS",
    "-Mdir",
    "-access",
    "-covfile",
    "-covtest",
    "-covworkdir",
    "-errormax",
    "-input",
    "-l",
    "-log",
    "-logfile",
    "-nowarn",
    "-o",
    "-seed",
    "-snapshot",
    "-svseed",
    "-uvmpackagename",
    "-work",
    "-xprop",
    "-xmlibdirname",
    "-xmerror",
}
_IGNORED_FLAGS = {
    "+v2k",
    "+vpi",
    "-64bit",
    "-R",
    "-coverage",
    "-covoverwrite",
    "-elaborate",
    "-enable_abv_asrtctrl_enh",
    "-enable_strict_timescale",
    "-full64",
    "-kdb",
    "-kdb=only",
    "-lca",
    "-licqueue",
    "-mess",
    "-messages",
    "-nocopyright",
    "-nospecify",
    "-notimingchecks",
    "-quiet",
    "-status",
    "-verbose",
    "-xverbose",
}
_SEMANTIC_GAP_OPTIONS = {
    "-ALLOWREDEFINITION": "definition_replacement_semantics",
    "-disable_sem2009": "pre_2009_semantics",
}

_SOURCE_MARKERS: tuple[tuple[re.Pattern[bytes], str], ...] = (
    (re.compile(rb"\b(?:import|export)\s*\"DPI", re.IGNORECASE), "dpi_runtime"),
    (
        re.compile(rb"\b(?:force|release)\b", re.IGNORECASE),
        "procedural_force_release",
    ),
    (re.compile(rb"\bbind\b", re.IGNORECASE), "bind_semantics"),
    (
        re.compile(rb"(?:`pragma\s+protect|`protect|`protected)", re.IGNORECASE),
        "protected_region",
    ),
    (
        re.compile(rb"\b(?:uvm_pkg|uvm_[a-z0-9_]+)\b", re.IGNORECASE),
        "uvm_dynamic_connectivity",
    ),
)
_XCELIUM_UVM_RECORD_RE = re.compile(
    r"Compiling UVM packages \((?P<packages>[^)]+)\) using uvmhome "
    r"location (?P<location>[^\r\n]+)"
)


class AdapterStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"


def _fixed_label(value: str, label: str) -> str:
    if not isinstance(value, str) or not _FIXED_LABEL_RE.fullmatch(value):
        raise ValueError(f"{label} must be a fixed snake_case label")
    return value


@dataclass(frozen=True)
class SourceGraphAdapterBlocker:
    code: str
    stage: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _fixed_label(self.code, "blocker code"))
        object.__setattr__(self, "stage", _fixed_label(self.stage, "blocker stage"))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage}


@dataclass(frozen=True)
class SourceGraphAdapterReceipt:
    status: AdapterStatus
    input_count: int = 0
    option_count: int = 0
    top_count: int = 0
    manifest_complete: bool = False
    manifest_incomplete_reasons: tuple[str, ...] = ()
    gap_codes: tuple[str, ...] = ()
    objective_exclusions: tuple[str, ...] = ()
    ancestor_count: int = 0
    requested_cone_instance_count: int = 0
    coverage_boundary_instance_count: int = 0
    cross_request_reusable: bool = False
    blocker: SourceGraphAdapterBlocker | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AdapterStatus(self.status))
        for field_name in (
            "manifest_incomplete_reasons",
            "gap_codes",
            "objective_exclusions",
        ):
            values = tuple(sorted(set(getattr(self, field_name))))
            for value in values:
                _fixed_label(value, field_name)
            object.__setattr__(self, field_name, values)
        if self.status is AdapterStatus.BLOCKED and self.blocker is None:
            raise ValueError("blocked adapter receipt requires a blocker")
        if self.status is AdapterStatus.READY and self.blocker is not None:
            raise ValueError("ready adapter receipt must not carry a blocker")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "adapter_version": SOURCE_GRAPH_ADAPTER_VERSION,
            "status": self.status.value,
            "manifest": {
                "input_count": self.input_count,
                "option_count": self.option_count,
                "top_count": self.top_count,
                "complete": self.manifest_complete,
                "incomplete_reasons": list(self.manifest_incomplete_reasons),
            },
            "scope": {
                "ancestor_count": self.ancestor_count,
                "requested_cone_instance_count": self.requested_cone_instance_count,
                "coverage_boundary_instance_count": self.coverage_boundary_instance_count,
                "objective_exclusions": list(self.objective_exclusions),
            },
            "gap_codes": list(self.gap_codes),
            "cross_request_reusable": self.cross_request_reusable,
        }
        if self.blocker is not None:
            result["blocker"] = self.blocker.to_dict()
        return result


@dataclass(frozen=True)
class SourceGraphBuildPlan:
    status: AdapterStatus
    request: SourceGraphBuildRequest | None
    receipt: SourceGraphAdapterReceipt

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AdapterStatus(self.status))
        if self.status is AdapterStatus.READY and self.request is None:
            raise ValueError("ready Source Graph plan requires a request")
        if self.status is AdapterStatus.BLOCKED and self.request is not None:
            raise ValueError("blocked Source Graph plan must not carry a request")


@dataclass
class _TranslationState:
    simulator: str
    command_dir: Path
    inputs: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    command_tops: list[str] = field(default_factory=list)
    simulator_library_inputs: set[str] = field(default_factory=set)
    support_files: set[Path] = field(default_factory=set)
    active_filelists: set[Path] = field(default_factory=set)
    visited_filelists: set[Path] = field(default_factory=set)
    gap_codes: set[str] = field(default_factory=set)
    objective_exclusions: set[str] = field(default_factory=set)
    inputs_complete: bool = True
    options_complete: bool = True
    uvmhome_resolved: bool = False
    token_count: int = 0


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _expand_token(value: str, state: _TranslationState) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(value))
    if _ENV_REF_RE.search(expanded):
        state.gap_codes.add("compile_environment_unresolved")
        state.inputs_complete = False
        state.options_complete = False
        return None
    return expanded


def _resolve_path(
    value: str,
    base: Path,
    state: _TranslationState,
) -> Path | None:
    expanded = _expand_token(value, state)
    if expanded is None or "\x00" in expanded:
        return None
    path = Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _append_top(state: _TranslationState, value: str) -> None:
    if value and value not in state.command_tops:
        state.command_tops.append(value)


def _append_source(
    state: _TranslationState,
    token: str,
    base: Path,
) -> None:
    path = _resolve_path(token, base, state)
    if path is None:
        return
    state.inputs.append(str(path))


def _append_path_option(
    state: _TranslationState,
    option: str,
    value: str,
    base: Path,
) -> None:
    path = _resolve_path(value, base, state)
    if path is None:
        return
    state.options.extend((option, str(path)))
    if option == "-v":
        state.support_files.add(path)
    elif option == "-y":
        # Directory contents cannot be fingerprinted without an unbounded
        # enumeration.  Preserve the option for a best-effort build but forbid
        # exact cross-request reuse.
        state.gap_codes.add("library_directory_content_unproven")
        state.options_complete = False
        state.objective_exclusions.add("library_directory_semantics")


def _translate_plus_incdir(
    state: _TranslationState,
    token: str,
    base: Path,
) -> None:
    values: list[str] = []
    for item in token[len("+incdir+") :].split("+"):
        if not item:
            continue
        path = _resolve_path(item, base, state)
        if path is not None:
            values.append(str(path))
    if values:
        state.options.append("+incdir+" + "+".join(values))


def _mark_unclassified(state: _TranslationState) -> None:
    state.options_complete = False
    state.gap_codes.add("compile_option_unclassified")
    state.objective_exclusions.add("unclassified_compile_option")


def _translate_filelist(
    state: _TranslationState,
    raw_path: str,
    *,
    base: Path,
    relative_mode: str,
    depth: int,
) -> None:
    if depth > _MAX_FILELIST_DEPTH:
        state.inputs_complete = False
        state.gap_codes.add("filelist_depth_exceeded")
        return
    path = _resolve_path(raw_path, base, state)
    if path is None:
        return
    state.support_files.add(path)
    if path in state.active_filelists:
        state.inputs_complete = False
        state.gap_codes.add("filelist_cycle")
        return
    if path in state.visited_filelists:
        # A repeated command file has compile-order meaning, so replay it; only
        # recursion cycles are blocked.
        pass
    if not path.is_file():
        state.inputs_complete = False
        state.gap_codes.add("filelist_unavailable")
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tokens = shlex.split(text, comments=True, posix=True)
    except (OSError, ValueError):
        state.inputs_complete = False
        state.gap_codes.add("filelist_parse_failed")
        return
    state.active_filelists.add(path)
    state.visited_filelists.add(path)
    try:
        token_base = state.command_dir if relative_mode == "-f" else path.parent
        _translate_tokens(
            state,
            tokens,
            base=token_base,
            depth=depth,
            skip_executable=False,
        )
    finally:
        state.active_filelists.remove(path)


def _translate_tokens(
    state: _TranslationState,
    tokens: Sequence[str],
    *,
    base: Path,
    depth: int = 0,
    skip_executable: bool,
) -> None:
    index = 0
    if skip_executable and tokens:
        executable = Path(tokens[0]).name
        if executable in {"vcs", "vlogan", "xrun", "irun"}:
            index = 1
        else:
            state.options_complete = False
            state.gap_codes.add("compile_command_unrecognized")

    while index < len(tokens):
        check_cancelled()
        state.token_count += 1
        if state.token_count > _MAX_FILELIST_TOKENS:
            state.inputs_complete = False
            state.options_complete = False
            state.gap_codes.add("compile_token_limit_exceeded")
            return
        token = tokens[index]
        suffix = Path(token).suffix.lower()

        if suffix in _HDL_SUFFIXES:
            _append_source(state, token, base)
            index += 1
            continue
        if suffix in _NATIVE_SUFFIXES:
            state.objective_exclusions.add("dpi_runtime")
            state.gap_codes.add("native_runtime_input_excluded")
            index += 1
            continue
        if token in {"-f", "-F"}:
            if index + 1 >= len(tokens):
                state.inputs_complete = False
                state.gap_codes.add("filelist_argument_missing")
                return
            _translate_filelist(
                state,
                tokens[index + 1],
                base=base,
                relative_mode=token,
                depth=depth + 1,
            )
            index += 2
            continue
        if token.startswith("+define+"):
            definitions = token[len("+define+") :].split("+")
            retained = [item for item in definitions if item and item != "define"]
            if len(retained) != len([item for item in definitions if item]):
                state.gap_codes.add("macro_definition_approximated")
                state.objective_exclusions.add("macro_semantics_approximated")
            if retained:
                state.options.append("+define+" + "+".join(retained))
            index += 1
            continue
        if token.startswith("+incdir+"):
            _translate_plus_incdir(state, token, base)
            index += 1
            continue
        if token.startswith("+libext+"):
            state.options.append(token)
            index += 1
            continue
        if token in {"-sverilog", "-sv"}:
            state.options.extend(("--std", "1800-2017"))
            index += 1
            continue
        if token.startswith("-timescale="):
            state.options.extend(("--timescale", token.split("=", 1)[1]))
            index += 1
            continue
        if token == "-timescale" and index + 1 < len(tokens):
            state.options.extend(("--timescale", tokens[index + 1]))
            index += 2
            continue
        if token in {"-top", "--top"} and index + 1 < len(tokens):
            _append_top(state, tokens[index + 1])
            index += 2
            continue
        if token.startswith(("-top=", "--top=")):
            _append_top(state, token.split("=", 1)[1])
            index += 1
            continue
        if token in {"-incdir", "-I"} and index + 1 < len(tokens):
            path = _resolve_path(tokens[index + 1], base, state)
            if path is not None:
                state.options.extend(("-I", str(path)))
            index += 2
            continue
        if token == "-define" and index + 1 < len(tokens):
            state.options.extend(("-D", tokens[index + 1]))
            index += 2
            continue
        if token in {"-D", "-U"} and index + 1 < len(tokens):
            state.options.extend((token, tokens[index + 1]))
            index += 2
            continue
        if token in {"-y", "-v"} and index + 1 < len(tokens):
            _append_path_option(state, token, tokens[index + 1], base)
            index += 2
            continue
        if token.startswith(("-I", "-y", "-v")) and len(token) > 2:
            _append_path_option(state, token[:2], token[2:], base)
            index += 1
            continue
        if token.startswith(("-D", "-U")) and len(token) > 2:
            state.options.append(token)
            index += 1
            continue
        if token in _SEMANTIC_GAP_OPTIONS:
            code = _SEMANTIC_GAP_OPTIONS[token]
            state.gap_codes.add(code)
            state.objective_exclusions.add(code)
            index += 1
            continue
        if token == "-uvmhome":
            state.objective_exclusions.add("uvm_dynamic_connectivity")
            if index + 1 >= len(tokens):
                state.gap_codes.add("simulator_uvm_library_unresolved")
                state.inputs_complete = False
                index += 1
                continue
            if not state.uvmhome_resolved:
                state.gap_codes.add("simulator_uvm_library_unresolved")
                state.inputs_complete = False
            index += 2
            continue
        if token == "-L" and index + 1 < len(tokens):
            state.objective_exclusions.add("dpi_runtime")
            state.gap_codes.add("native_runtime_input_excluded")
            index += 2
            continue
        if token.startswith("-L") or (
            token.startswith("-l") and token not in {"-l", "-licqueue"}
        ):
            state.objective_exclusions.add("dpi_runtime")
            state.gap_codes.add("native_runtime_input_excluded")
            index += 1
            continue
        if token in _IGNORED_VALUE_OPTIONS:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if token in _IGNORED_FLAGS or token.startswith(
            ("-debug", "+ntb_", "+vcs+", "+xm", "-covoverwrite")
        ):
            index += 1
            continue
        if token in {"&&", ";", "|"}:
            _mark_unclassified(state)
            index += 1
            continue
        _mark_unclassified(state)
        index += 1


def _reported_source_paths(compile_result: Mapping[str, Any]) -> list[str]:
    files = compile_result.get("files")
    if not isinstance(files, Mapping):
        return []
    user = files.get("user")
    if not isinstance(user, Sequence) or isinstance(user, (str, bytes)):
        return []
    result: list[str] = []
    for item in user:
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        result.append(str(Path(str(item["path"])).resolve(strict=False)))
    return result


def _include_support_paths(compile_result: Mapping[str, Any]) -> set[Path]:
    tree = compile_result.get("include_tree")
    if not isinstance(tree, Mapping):
        return set()
    result: set[Path] = set()
    for parent, children in tree.items():
        if parent:
            result.add(Path(str(parent)).resolve(strict=False))
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            result.update(
                Path(str(child)).resolve(strict=False) for child in children if child
            )
    return result


def _included_child_paths(compile_result: Mapping[str, Any]) -> set[Path]:
    tree = compile_result.get("include_tree")
    if not isinstance(tree, Mapping):
        return set()
    result: set[Path] = set()
    for children in tree.values():
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            continue
        result.update(
            Path(str(child)).resolve(strict=False) for child in children if child
        )
    return result


def _extract_xcelium_uvm_library(
    compile_log: str,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Recover only simulator-provided UVM sources explicitly recorded by xrun."""

    match: re.Match[str] | None = None
    try:
        with Path(compile_log).open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                if line_number % 256 == 0:
                    check_cancelled()
                match = _XCELIUM_UVM_RECORD_RE.search(line)
                if match is not None:
                    break
    except OSError:
        return None
    if match is None:
        return None

    location = Path(match.group("location").strip()).expanduser().resolve(strict=False)
    search_dirs = (location / "sv" / "src", location / "additions" / "sv")
    sources: list[str] = []
    try:
        package_names = shlex.split(match.group("packages"), posix=True)
    except ValueError:
        return None
    for package_name in package_names:
        source = next(
            (
                directory / package_name
                for directory in search_dirs
                if (directory / package_name).is_file()
            ),
            None,
        )
        if source is None:
            return None
        sources.append(str(source.resolve(strict=False)))
    include_dirs = tuple(
        str(directory.resolve(strict=False))
        for directory in search_dirs
        if directory.is_dir()
    )
    return include_dirs, tuple(sources)


def _ordered_tops(
    compile_result: Mapping[str, Any],
    state: _TranslationState,
) -> tuple[tuple[str, ...], bool]:
    raw = compile_result.get("top_modules")
    reported = (
        [str(item) for item in raw if item]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else []
    )
    reported = list(dict.fromkeys(reported))
    command = list(dict.fromkeys(state.command_tops))
    if command:
        ordered = command + [item for item in reported if item not in command]
        complete = not reported or set(command) == set(reported)
    else:
        ordered = reported
        # Elaboration logs report top modules independently of the invocation;
        # that list is authoritative when command-line -top is absent.
        complete = bool(reported)
    return tuple(ordered), complete


def _hash_file_and_scan(
    path: Path,
    exclusions: set[str],
) -> tuple[str, int] | None:
    digest = hashlib.sha256()
    size = 0
    tail = b""
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                check_cancelled()
                digest.update(chunk)
                size += len(chunk)
                sample = tail + chunk
                for pattern, code in _SOURCE_MARKERS:
                    if pattern.search(sample):
                        exclusions.add(code)
                tail = sample[-256:]
    except OSError:
        return None
    return digest.hexdigest(), size


def _compile_fingerprint(
    *,
    simulator: str,
    command: str,
    inputs: Sequence[str],
    options: Sequence[str],
    tops: Sequence[str],
    support_files: set[Path],
    exclusions: set[str],
) -> tuple[str | None, bool]:
    input_records: list[dict[str, Any]] = []
    readable = True
    for rendered in inputs:
        path = Path(rendered)
        fact = _hash_file_and_scan(path, exclusions)
        if fact is None:
            readable = False
            continue
        digest, size = fact
        input_records.append({"path": rendered, "bytes": size, "sha256": digest})

    support_records: list[dict[str, Any]] = []
    input_paths = {Path(item).resolve(strict=False) for item in inputs}
    for path in sorted(support_files - input_paths, key=lambda item: str(item)):
        fact = _hash_file_and_scan(path, exclusions)
        if fact is None:
            readable = False
            continue
        digest, size = fact
        support_records.append({"path": str(path), "bytes": size, "sha256": digest})
    if not readable or len(input_records) != len(inputs):
        return None, False
    payload = {
        "adapter_version": SOURCE_GRAPH_ADAPTER_VERSION,
        "simulator": simulator,
        "compile_command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "ordered_inputs": input_records,
        "ordered_options": list(options),
        "ordered_tops": list(tops),
        "support_files": support_records,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest(), True


def _compile_manifest(
    compile_log: str,
    compile_result: Mapping[str, Any],
) -> tuple[CompileInputManifest, tuple[str, ...], tuple[str, ...]]:
    simulator = str(compile_result.get("simulator") or "").lower()
    if simulator not in _BASE_OPTIONS:
        simulator = "unknown"
    raw_cwd = compile_result.get("compile_cwd") or Path(compile_log).parent
    command_dir = Path(str(raw_cwd)).resolve(strict=False)
    state = _TranslationState(simulator=simulator, command_dir=command_dir)
    state.options.extend(_BASE_OPTIONS.get(simulator, ()))
    command = str(
        compile_result.get("compile_replay_command")
        or compile_result.get("compile_command")
        or ""
    )
    if not command:
        state.options_complete = False
        state.gap_codes.add("compile_command_missing")
    else:
        try:
            tokens = shlex.split(command, comments=True, posix=True)
        except ValueError:
            tokens = ()
            state.inputs_complete = False
            state.options_complete = False
            state.gap_codes.add("compile_command_parse_failed")
        if tokens:
            if simulator == "xcelium" and "-uvmhome" in tokens:
                uvm_library = _extract_xcelium_uvm_library(compile_log)
                if uvm_library is not None:
                    include_dirs, sources = uvm_library
                    for include_dir in include_dirs:
                        state.options.extend(("-I", include_dir))
                    state.inputs.extend(sources)
                    state.simulator_library_inputs.update(sources)
                    # Kept as a private translation-state fact: it controls
                    # whether the later -uvmhome token is a resolved input or
                    # a conservative manifest blocker, and is never serialized.
                    state.uvmhome_resolved = True
            _translate_tokens(
                state,
                tokens,
                base=command_dir,
                skip_executable=True,
            )

    reported = _reported_source_paths(compile_result)
    if not state.inputs and reported:
        state.inputs.extend(reported)
        state.inputs_complete = False
        state.gap_codes.add("compile_input_order_recovered_approximately")
    elif reported:
        include_paths = {str(path) for path in _included_child_paths(compile_result)}
        reported_direct = set(reported) - include_paths
        translated = set(state.inputs) - state.simulator_library_inputs
        if reported_direct and reported_direct != translated:
            state.inputs_complete = False
            state.gap_codes.add("compile_log_source_reconciliation_gap")
    if not state.inputs:
        state.inputs_complete = False
        state.gap_codes.add("compile_inputs_empty")

    parse_warnings = compile_result.get("parse_warnings")
    if (
        isinstance(parse_warnings, Sequence)
        and not isinstance(parse_warnings, (str, bytes))
        and parse_warnings
    ):
        state.inputs_complete = False
        state.options_complete = False
        state.gap_codes.add("compile_log_parse_warning")

    tops, tops_complete = _ordered_tops(compile_result, state)
    if not tops:
        state.gap_codes.add("compile_tops_empty")
    elif len(tops) > 1:
        # Multiple elaboration tops are replayed in full.  They frequently
        # include bind tops, whose runtime attachment semantics remain an
        # explicit objective exclusion for exact coverage claims.
        state.objective_exclusions.add("bind_semantics")

    state.support_files.update(_include_support_paths(compile_result))
    fingerprint, content_complete = _compile_fingerprint(
        simulator=simulator,
        command=command,
        inputs=state.inputs,
        options=state.options,
        tops=tops,
        support_files=state.support_files,
        exclusions=state.objective_exclusions,
    )
    if not content_complete:
        state.inputs_complete = False
        state.gap_codes.add("compile_content_unavailable")

    manifest = CompileInputManifest(
        fingerprint=fingerprint,
        ordered_inputs=tuple(state.inputs),
        ordered_options=tuple(state.options),
        ordered_tops=tops,
        inputs_complete=state.inputs_complete and content_complete,
        options_complete=state.options_complete,
        tops_complete=tops_complete,
    )
    return (
        manifest,
        tuple(sorted(state.gap_codes)),
        tuple(sorted(state.objective_exclusions)),
    )


def _selected_top(
    *,
    signal_path: str,
    tops: Sequence[str],
    top_hint: str | None,
) -> str | None:
    root = signal_path.split(".", 1)[0]
    if top_hint:
        return top_hint if top_hint == root and top_hint in tops else None
    return root if root in tops else None


def _hierarchy_ancestors(
    *,
    hierarchy_result: Mapping[str, Any],
    top: str,
    signal_path: str,
) -> tuple[str, ...] | None:
    component_tree = hierarchy_result.get("component_tree")
    if not isinstance(component_tree, Mapping):
        return None
    parts = signal_path.split(".")
    if len(parts) < 2 or parts[0] != top:
        return None
    ancestors = [top]
    children = component_tree.get(top)
    index = 1
    while index < len(parts) - 1 and isinstance(children, Mapping):
        check_cancelled()
        node = children.get(parts[index])
        if not isinstance(node, Mapping):
            break
        ancestors.append(".".join(parts[: index + 1]))
        nested = node.get("children")
        children = nested if isinstance(nested, Mapping) else None
        index += 1
    # The build contract treats everything before the final symbol as an
    # instance path.  Require every such segment to exist in the cached source
    # hierarchy; otherwise a dotted interface/struct member could be guessed as
    # an instance and create a dishonest scope boundary.
    if index != len(parts) - 1:
        return None
    return tuple(ancestors)


def _blocked_plan(
    *,
    code: str,
    stage: str,
    manifest: CompileInputManifest | None = None,
    gaps: Sequence[str] = (),
    exclusions: Sequence[str] = (),
) -> SourceGraphBuildPlan:
    blocker = SourceGraphAdapterBlocker(code=code, stage=stage)
    receipt = SourceGraphAdapterReceipt(
        status=AdapterStatus.BLOCKED,
        input_count=len(manifest.ordered_inputs) if manifest else 0,
        option_count=len(manifest.ordered_options) if manifest else 0,
        top_count=len(manifest.ordered_tops) if manifest else 0,
        manifest_complete=manifest.complete if manifest else False,
        manifest_incomplete_reasons=(manifest.incomplete_reasons if manifest else ()),
        gap_codes=tuple(gaps),
        objective_exclusions=tuple(exclusions),
        blocker=blocker,
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.BLOCKED,
        request=None,
        receipt=receipt,
    )


def build_source_graph_plan(
    *,
    compile_log: str,
    compile_result: Mapping[str, Any],
    hierarchy_result: Mapping[str, Any],
    operation: QueryOperation | str,
    signal_path: str,
    top_hint: str | None,
    max_hops: int,
    frontend_version: str,
) -> SourceGraphBuildPlan:
    """Build one bounded driver/load request or a fixed structured blocker."""

    check_cancelled()
    try:
        operation = QueryOperation(operation)
    except ValueError:
        return _blocked_plan(code="operation_unsupported", stage="target_scope")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 0:
        return _blocked_plan(code="max_hops_invalid", stage="target_scope")
    normalized_signal = str(signal_path).strip()
    if not normalized_signal or "." not in normalized_signal:
        return _blocked_plan(code="signal_path_unscoped", stage="target_scope")

    manifest, gaps, exclusions = _compile_manifest(compile_log, compile_result)
    if not manifest.complete:
        exclusions = tuple(sorted({*exclusions, "compile_manifest_incomplete"}))
    if not manifest.ordered_inputs:
        return _blocked_plan(
            code="compile_inputs_unavailable",
            stage="compile_manifest",
            manifest=manifest,
            gaps=gaps,
            exclusions=exclusions,
        )
    if not manifest.ordered_tops:
        return _blocked_plan(
            code="compile_tops_unavailable",
            stage="compile_manifest",
            manifest=manifest,
            gaps=gaps,
            exclusions=exclusions,
        )

    top = _selected_top(
        signal_path=normalized_signal,
        tops=manifest.ordered_tops,
        top_hint=top_hint,
    )
    if top is None:
        return _blocked_plan(
            code="target_top_unresolved",
            stage="target_scope",
            manifest=manifest,
            gaps=gaps,
            exclusions=exclusions,
        )
    ancestors = _hierarchy_ancestors(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=normalized_signal,
    )
    if ancestors is None:
        return _blocked_plan(
            code="hierarchy_scope_unresolved",
            stage="target_scope",
            manifest=manifest,
            gaps=(*gaps, "hierarchy_scope_unresolved"),
            exclusions=exclusions,
        )

    target = ConnectivityTarget(operation=operation, signal_path=normalized_signal)
    # The deepest source-hierarchy instance is the only assignment-bearing
    # cone.  Its already-resolved ancestors are included as binding skeletons.
    # This is finite by construction and never enumerates siblings/descendants.
    target_instance = ancestors[-1]
    boundary_paths = tuple(dict.fromkeys(ancestors))
    scope = SourceGraphBuildScope(
        design=(
            f"compile_{manifest.fingerprint[:24]}"
            if manifest.fingerprint
            else "compile_incomplete_manifest"
        ),
        top=top,
        target=target,
        hierarchy_ancestors=ancestors,
        requested_cone=RequestedCone(
            operation=operation,
            max_hops=max_hops,
            instance_paths=(target_instance,),
            cross_instance_boundaries=True,
            stop_at_sequential=True,
            include_control_dependencies=False,
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=boundary_paths,
            objective_exclusions=tuple(exclusions),
        ),
    )
    request = SourceGraphBuildRequest(
        identity=SourceGraphIdentity(
            compile_inputs=manifest,
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version=frontend_version,
        ),
        scope=scope,
    )
    build_key = compute_source_graph_build_key(request)
    receipt = SourceGraphAdapterReceipt(
        status=AdapterStatus.READY,
        input_count=len(manifest.ordered_inputs),
        option_count=len(manifest.ordered_options),
        top_count=len(manifest.ordered_tops),
        manifest_complete=manifest.complete,
        manifest_incomplete_reasons=manifest.incomplete_reasons,
        gap_codes=gaps,
        objective_exclusions=tuple(exclusions),
        ancestor_count=len(ancestors),
        requested_cone_instance_count=1,
        coverage_boundary_instance_count=len(boundary_paths),
        cross_request_reusable=build_key.cross_request_reusable,
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=request,
        receipt=receipt,
    )
