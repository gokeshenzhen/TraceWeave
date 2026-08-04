"""Conservative production inputs for scoped Source Graph connectivity builds.

The adapter translates only local compile-log facts that can be replayed by
the isolated Slang worker.  It never starts a worker and never walks an
elaborated design.  Hierarchy scope is resolved by following the requested
signal through the already-built ``component_tree`` one child at a time. Path
requests follow two such chains, require one proved top, and project only their
ancestor union; no sibling or full-design enumeration is performed.

Incomplete compile inputs can still produce a diagnostic request, but the
existing build contract makes its key non-reusable.  Unprovable target/top
scope is a structured blocker rather than an implicit full-design scan.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import threading
from typing import Any

from .cancellation import check_cancelled
from .slang_connectivity_projector import SLANG_FRONTEND_NAME
from .source_graph_contract import (
    BoundaryMode,
    CompileInputManifest,
    ConnectivityPathTarget,
    ConnectivityTarget,
    CoverageBoundary,
    PathHierarchyScope,
    QueryOperation,
    RequestedCone,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactIdentity,
    SourceGraphArtifactScope,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphIdentity,
    SourceGraphQueryIdentity,
    compute_source_graph_artifact_key,
    compute_source_graph_query_key,
)


SOURCE_GRAPH_ADAPTER_VERSION = "3.0"
_HDL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_NATIVE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".o", ".a", ".so"}
_ENV_REF_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_FIXED_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_FILELIST_DEPTH = 64
_MAX_FILELIST_TOKENS = 1_000_000
_MANIFEST_CACHE_MAX_ENTRIES = 8

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
    scope_kind: str = "single_endpoint"
    endpoint_count: int = 1
    lca_depth: int | None = None
    cross_request_reusable: bool = False
    artifact_fingerprint_sha256: str | None = None
    query_fingerprint_sha256: str | None = None
    snapshot_identity_complete: bool = False
    fingerprint_cache_disposition: str | None = None
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
        if self.scope_kind not in {
            "single_endpoint",
            "dual_endpoint_path",
            "multi_endpoint_trace",
        }:
            raise ValueError("invalid adapter scope kind")
        if self.endpoint_count < 1:
            raise ValueError("adapter endpoint count must be positive")
        if (self.scope_kind == "single_endpoint" and self.endpoint_count != 1) or (
            self.scope_kind == "dual_endpoint_path" and self.endpoint_count != 2
        ):
            raise ValueError("adapter endpoint count does not match scope kind")
        if self.lca_depth is not None and self.lca_depth < 0:
            raise ValueError("adapter LCA depth must not be negative")
        if self.fingerprint_cache_disposition not in {
            None,
            "miss",
            "hit_session_snapshot",
        }:
            raise ValueError("invalid fingerprint cache disposition")
        for field_name in (
            "artifact_fingerprint_sha256",
            "query_fingerprint_sha256",
        ):
            value = getattr(self, field_name)
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"invalid {field_name}")

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
                "fingerprint_cache_disposition": self.fingerprint_cache_disposition,
            },
            "scope": {
                "kind": self.scope_kind,
                "endpoint_count": self.endpoint_count,
                "ancestor_count": self.ancestor_count,
                "lca_depth": self.lca_depth,
                "requested_cone_instance_count": self.requested_cone_instance_count,
                "coverage_boundary_instance_count": self.coverage_boundary_instance_count,
                "objective_exclusions": list(self.objective_exclusions),
            },
            "gap_codes": list(self.gap_codes),
            "cross_request_reusable": self.cross_request_reusable,
            "artifact_fingerprint_sha256": self.artifact_fingerprint_sha256,
            "query_fingerprint_sha256": self.query_fingerprint_sha256,
            "snapshot_identity_complete": self.snapshot_identity_complete,
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


@dataclass(frozen=True)
class _ManifestCacheEntry:
    manifest: CompileInputManifest
    gap_codes: tuple[str, ...]
    objective_exclusions: tuple[str, ...]


_MANIFEST_CACHE: OrderedDict[str, _ManifestCacheEntry] = OrderedDict()
_MANIFEST_CACHE_LOCK = threading.Lock()
_MANIFEST_BUILD_LOCK = threading.Lock()


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


def _stat_record(path: Path) -> dict[str, int | str] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
    }


def _content_stat_key(
    *,
    inputs: Sequence[str],
    support_files: set[Path],
) -> str | None:
    input_records: list[dict[str, int | str]] = []
    for index, rendered in enumerate(inputs):
        if index % 256 == 0:
            check_cancelled()
        record = _stat_record(Path(rendered))
        if record is None:
            return None
        input_records.append(record)
    input_paths = {Path(item).resolve(strict=False) for item in inputs}
    support_records: list[dict[str, int | str]] = []
    for path in sorted(support_files - input_paths, key=lambda item: str(item)):
        record = _stat_record(path)
        if record is None:
            return None
        support_records.append(record)
    return hashlib.sha256(
        _canonical_json(
            {
                "ordered_inputs": input_records,
                "support_files": support_records,
            }
        )
    ).hexdigest()


def _manifest_snapshot_key(
    compile_log: str,
    compile_result: Mapping[str, Any],
) -> str | None:
    log_path = Path(compile_log).resolve(strict=False)
    log_record = _stat_record(log_path)
    if log_record is None:
        return None
    files = compile_result.get("files")
    user = files.get("user") if isinstance(files, Mapping) else ()
    user_records = []
    if isinstance(user, Sequence) and not isinstance(user, (str, bytes)):
        user_records = [
            {
                "path": str(item.get("path") or ""),
                "type": str(item.get("type") or ""),
                "category": str(item.get("category") or ""),
            }
            for item in user
            if isinstance(item, Mapping)
        ]
    payload = {
        "adapter_version": SOURCE_GRAPH_ADAPTER_VERSION,
        "compile_log": log_record,
        "simulator": str(compile_result.get("simulator") or ""),
        "compile_cwd": str(compile_result.get("compile_cwd") or ""),
        "compile_command": str(compile_result.get("compile_command") or ""),
        "compile_replay_command": str(
            compile_result.get("compile_replay_command") or ""
        ),
        "top_modules": list(compile_result.get("top_modules") or ()),
        "user_files": user_records,
        "include_tree": compile_result.get("include_tree") or {},
        "filelist_tree": compile_result.get("filelist_tree") or {},
        "parse_warnings": list(compile_result.get("parse_warnings") or ()),
    }
    try:
        return hashlib.sha256(_canonical_json(payload)).hexdigest()
    except (TypeError, ValueError):
        return None


def _lookup_manifest_cache(key: str | None) -> _ManifestCacheEntry | None:
    if key is None:
        return None
    with _MANIFEST_CACHE_LOCK:
        entry = _MANIFEST_CACHE.get(key)
        if entry is not None:
            _MANIFEST_CACHE.move_to_end(key)
        return entry


def _publish_manifest_cache(key: str, entry: _ManifestCacheEntry) -> None:
    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE[key] = entry
        _MANIFEST_CACHE.move_to_end(key)
        while len(_MANIFEST_CACHE) > _MANIFEST_CACHE_MAX_ENTRIES:
            _MANIFEST_CACHE.popitem(last=False)


def _reset_source_graph_adapter_cache_for_tests() -> None:
    """Clear process-local manifest memoization after all calls are idle."""

    with _MANIFEST_CACHE_LOCK:
        _MANIFEST_CACHE.clear()


def _build_compile_manifest(
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
    before_content = _content_stat_key(
        inputs=state.inputs,
        support_files=state.support_files,
    )
    fingerprint, content_complete = _compile_fingerprint(
        simulator=simulator,
        command=command,
        inputs=state.inputs,
        options=state.options,
        tops=tops,
        support_files=state.support_files,
        exclusions=state.objective_exclusions,
    )
    after_content = _content_stat_key(
        inputs=state.inputs,
        support_files=state.support_files,
    )
    if before_content is None or after_content != before_content:
        fingerprint = None
        content_complete = False
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


def _compile_manifest(
    compile_log: str,
    compile_result: Mapping[str, Any],
) -> tuple[
    CompileInputManifest,
    tuple[str, ...],
    tuple[str, ...],
    str,
    str | None,
]:
    """Return a content-hashed manifest for one immutable compile session.

    The hierarchy handle and this cache share the same lifetime boundary: the
    compile-log snapshot.  A rebuild changes the log metadata and therefore
    invalidates the memoized manifest.  The first request still hashes every
    source/support input and rejects a snapshot whose file metadata changes
    during that scan; later requests reuse that exact process-session snapshot
    instead of re-walking hundreds of source paths.
    """

    snapshot_key = _manifest_snapshot_key(compile_log, compile_result)
    cached = _lookup_manifest_cache(snapshot_key)
    if cached is not None:
        return (
            cached.manifest,
            cached.gap_codes,
            cached.objective_exclusions,
            "hit_session_snapshot",
            snapshot_key,
        )

    while not _MANIFEST_BUILD_LOCK.acquire(timeout=0.05):
        check_cancelled()
    try:
        snapshot_key = _manifest_snapshot_key(compile_log, compile_result)
        cached = _lookup_manifest_cache(snapshot_key)
        if cached is not None:
            return (
                cached.manifest,
                cached.gap_codes,
                cached.objective_exclusions,
                "hit_session_snapshot",
                snapshot_key,
            )
        manifest, gaps, exclusions = _build_compile_manifest(
            compile_log, compile_result
        )
        after_key = _manifest_snapshot_key(compile_log, compile_result)
        if snapshot_key is not None and after_key != snapshot_key:
            manifest = CompileInputManifest(
                fingerprint=None,
                ordered_inputs=manifest.ordered_inputs,
                ordered_options=manifest.ordered_options,
                ordered_tops=manifest.ordered_tops,
                inputs_complete=False,
                options_complete=manifest.options_complete,
                tops_complete=manifest.tops_complete,
            )
            gaps = tuple(sorted({*gaps, "compile_snapshot_changed"}))
        elif snapshot_key is not None and manifest.complete:
            _publish_manifest_cache(
                snapshot_key,
                _ManifestCacheEntry(
                    manifest=manifest,
                    gap_codes=gaps,
                    objective_exclusions=exclusions,
                ),
            )
        stable_snapshot = (
            snapshot_key
            if snapshot_key is not None and after_key == snapshot_key
            else None
        )
        return manifest, gaps, exclusions, "miss", stable_snapshot
    finally:
        _MANIFEST_BUILD_LOCK.release()


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


def resolve_source_graph_hierarchy_ancestors(
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
    scope_kind: str = "single_endpoint",
    endpoint_count: int = 1,
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
        scope_kind=scope_kind,
        endpoint_count=endpoint_count,
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
    hierarchy_snapshot_sha256: str,
    operation: QueryOperation | str,
    signal_path: str,
    top_hint: str | None,
    max_hops: int,
    frontend_version: str,
    recursive: bool = False,
    include_expr: bool = True,
    kind_filter: Sequence[str] = (),
) -> SourceGraphBuildPlan:
    """Build one bounded driver/load request or a fixed structured blocker."""

    check_cancelled()
    try:
        operation = QueryOperation(operation)
    except ValueError:
        return _blocked_plan(code="operation_unsupported", stage="target_scope")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 0:
        return _blocked_plan(code="max_hops_invalid", stage="target_scope")
    normalized_hierarchy_snapshot = str(hierarchy_snapshot_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_hierarchy_snapshot):
        return _blocked_plan(
            code="hierarchy_snapshot_unavailable", stage="target_scope"
        )
    normalized_signal = str(signal_path).strip()
    if not normalized_signal or "." not in normalized_signal:
        return _blocked_plan(code="signal_path_unscoped", stage="target_scope")

    (
        manifest,
        gaps,
        exclusions,
        fingerprint_cache_disposition,
        compile_snapshot,
    ) = _compile_manifest(compile_log, compile_result)
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
    ancestors = resolve_source_graph_hierarchy_ancestors(
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
    # The proved ancestor chain is the canonical bounded projection.  It lets
    # driver/load and same-chain path queries share one artifact without adding
    # siblings or descendants; every admitted path came directly from the
    # hierarchy handle.
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
            instance_paths=boundary_paths,
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
    source_identity = SourceGraphIdentity(
        compile_inputs=manifest,
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version=frontend_version,
    )
    artifact_scope = SourceGraphArtifactScope.from_build_scope(
        scope,
        hierarchy_snapshot_sha256=normalized_hierarchy_snapshot,
    )
    stable_compile_snapshot = (
        compile_snapshot
        or hashlib.sha256(
            _canonical_json(
                {
                    "unproved_compile_snapshot": True,
                    "manifest": manifest.to_dict(),
                }
            )
        ).hexdigest()
    )
    artifact_identity = SourceGraphArtifactIdentity(
        source=source_identity,
        scope=artifact_scope,
        compile_snapshot_sha256=stable_compile_snapshot,
        adapter_version=SOURCE_GRAPH_ADAPTER_VERSION,
        worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        snapshots_complete=compile_snapshot is not None,
    )
    query_identity = SourceGraphQueryIdentity.from_build_scope(
        scope,
        recursive=recursive,
        include_expr=include_expr,
        kind_filter=kind_filter,
    )
    request = SourceGraphBuildRequest(
        identity=source_identity,
        scope=scope,
        artifact=artifact_identity,
        query=query_identity,
    )
    artifact_key = compute_source_graph_artifact_key(artifact_identity)
    query_key = compute_source_graph_query_key(query_identity)
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
        requested_cone_instance_count=len(boundary_paths),
        coverage_boundary_instance_count=len(boundary_paths),
        cross_request_reusable=artifact_key.cross_request_reusable,
        artifact_fingerprint_sha256=artifact_key.digest,
        query_fingerprint_sha256=query_key.digest,
        snapshot_identity_complete=artifact_identity.snapshots_complete,
        fingerprint_cache_disposition=fingerprint_cache_disposition,
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=request,
        receipt=receipt,
    )


def _trace_scope_lca(chains: Sequence[tuple[str, ...]]) -> str:
    common = chains[0]
    for chain in chains[1:]:
        prefix_length = 0
        for left, right in zip(common, chain):
            if left != right:
                break
            prefix_length += 1
        common = common[:prefix_length]
    if not common:
        raise ValueError("trace hierarchy chains must share one proved top")
    return common[-1]


def build_source_graph_trace_plan(
    *,
    compile_log: str,
    compile_result: Mapping[str, Any],
    hierarchy_result: Mapping[str, Any],
    hierarchy_snapshot_sha256: str,
    signal_paths: Sequence[str],
    top_hint: str | None,
    max_hops: int,
    frontend_version: str,
) -> SourceGraphBuildPlan:
    """Build one artifact for an exact, hierarchy-proved X-trace target union.

    ``signal_paths`` contains only waveform targets already encountered by the
    trace. Every target is resolved by direct child lookup through the cached
    hierarchy. The artifact projects the union of those proved ancestor chains;
    it never admits a sibling, descendant, filename, or module-name inference.

    The request query remains rooted at the first signal. Additional targets
    affect only artifact identity, which lets independent per-node driver
    queries reuse one prepared IR while preserving QueryIdentity separation.
    """

    check_cancelled()
    if isinstance(signal_paths, (str, bytes)):
        normalized_paths: tuple[str, ...] = ()
    else:
        normalized_paths = tuple(
            dict.fromkeys(
                str(path).strip() for path in signal_paths if str(path).strip()
            )
        )
    scope_kind = "multi_endpoint_trace"
    if not normalized_paths:
        return _blocked_plan(
            code="trace_targets_unavailable",
            stage="target_scope",
            scope_kind=scope_kind,
            endpoint_count=1,
        )

    base = build_source_graph_plan(
        compile_log=compile_log,
        compile_result=compile_result,
        hierarchy_result=hierarchy_result,
        hierarchy_snapshot_sha256=hierarchy_snapshot_sha256,
        operation=QueryOperation.DRIVER,
        signal_path=normalized_paths[0],
        top_hint=top_hint,
        max_hops=max_hops,
        frontend_version=frontend_version,
        recursive=True,
    )
    if base.status is AdapterStatus.BLOCKED:
        return replace(
            base,
            receipt=replace(
                base.receipt,
                scope_kind=scope_kind,
                endpoint_count=len(normalized_paths),
            ),
        )

    assert base.request is not None
    request = base.request
    manifest = request.identity.compile_inputs
    top = request.scope.top
    chains: list[tuple[str, ...]] = []
    for signal_path in normalized_paths:
        check_cancelled()
        if signal_path.split(".", 1)[0] != top:
            return _blocked_plan(
                code="trace_target_top_mismatch",
                stage="target_scope",
                manifest=manifest,
                gaps=base.receipt.gap_codes,
                exclusions=base.receipt.objective_exclusions,
                scope_kind=scope_kind,
                endpoint_count=len(normalized_paths),
            )
        ancestors = resolve_source_graph_hierarchy_ancestors(
            hierarchy_result=hierarchy_result,
            top=top,
            signal_path=signal_path,
        )
        if ancestors is None:
            return _blocked_plan(
                code="trace_hierarchy_scope_unresolved",
                stage="target_scope",
                manifest=manifest,
                gaps=(*base.receipt.gap_codes, "trace_hierarchy_scope_unresolved"),
                exclusions=base.receipt.objective_exclusions,
                scope_kind=scope_kind,
                endpoint_count=len(normalized_paths),
            )
        chains.append(ancestors)

    unique_chains = tuple(dict.fromkeys(chains))
    ancestor_union = tuple(
        sorted(
            set().union(*(set(chain) for chain in unique_chains)),
            key=lambda path: (path.count("."), path),
        )
    )
    lca = _trace_scope_lca(unique_chains)
    boundary = CoverageBoundary(
        mode=BoundaryMode.EXPLICIT,
        instance_paths=ancestor_union,
        objective_exclusions=request.scope.coverage_boundary.objective_exclusions,
    )
    base_artifact = request.artifact_identity
    artifact_scope = SourceGraphArtifactScope(
        design=base_artifact.scope.design,
        top=top,
        hierarchy_snapshot_sha256=base_artifact.scope.hierarchy_snapshot_sha256,
        proved_ancestor_chains=unique_chains,
        proved_lcas=(lca,),
        projection_instance_paths=ancestor_union,
        coverage_boundary=boundary,
        capabilities=base_artifact.scope.capabilities,
    )
    artifact = replace(base_artifact, scope=artifact_scope)
    trace_request = replace(request, artifact=artifact)
    artifact_key = compute_source_graph_artifact_key(artifact)
    query_key = compute_source_graph_query_key(trace_request.query_identity)
    receipt = replace(
        base.receipt,
        ancestor_count=len(ancestor_union),
        requested_cone_instance_count=len(ancestor_union),
        coverage_boundary_instance_count=len(ancestor_union),
        scope_kind=scope_kind,
        endpoint_count=len(normalized_paths),
        lca_depth=lca.count("."),
        cross_request_reusable=artifact_key.cross_request_reusable,
        artifact_fingerprint_sha256=artifact_key.digest,
        query_fingerprint_sha256=query_key.digest,
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=trace_request,
        receipt=receipt,
    )


def _path_ancestor_union(
    from_ancestors: tuple[str, ...],
    to_ancestors: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {*from_ancestors, *to_ancestors},
            key=lambda path: (path.count("."), path),
        )
    )


def _path_lca(
    from_ancestors: tuple[str, ...],
    to_ancestors: tuple[str, ...],
) -> str:
    common = from_ancestors[0]
    for left, right in zip(from_ancestors, to_ancestors):
        if left != right:
            break
        common = left
    return common


def build_source_graph_path_plan(
    *,
    compile_log: str,
    compile_result: Mapping[str, Any],
    hierarchy_result: Mapping[str, Any],
    hierarchy_snapshot_sha256: str,
    from_signal: str,
    to_signal: str,
    top_hint: str | None,
    expand_assigns: bool,
    frontend_version: str,
) -> SourceGraphBuildPlan:
    """Build one bounded, dual-endpoint structural path request.

    Each endpoint instance must be found by direct child lookup in the cached
    hierarchy. Only the two ancestor chains and their LCA are admitted to the
    projection, so an unrelated sibling cannot enter the request implicitly.
    """

    check_cancelled()
    scope_kind = "dual_endpoint_path"
    endpoint_count = 2
    normalized_from = str(from_signal).strip()
    normalized_to = str(to_signal).strip()
    if not normalized_from or "." not in normalized_from:
        return _blocked_plan(
            code="path_from_signal_unscoped",
            stage="target_scope",
            scope_kind=scope_kind,
            endpoint_count=endpoint_count,
        )
    if not normalized_to or "." not in normalized_to:
        return _blocked_plan(
            code="path_to_signal_unscoped",
            stage="target_scope",
            scope_kind=scope_kind,
            endpoint_count=endpoint_count,
        )
    if not isinstance(expand_assigns, bool):
        return _blocked_plan(
            code="path_expand_assigns_invalid",
            stage="target_scope",
            scope_kind=scope_kind,
            endpoint_count=endpoint_count,
        )

    normalized_hierarchy_snapshot = str(hierarchy_snapshot_sha256).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_hierarchy_snapshot):
        return _blocked_plan(
            code="hierarchy_snapshot_unavailable",
            stage="target_scope",
            scope_kind=scope_kind,
            endpoint_count=endpoint_count,
        )

    (
        manifest,
        gaps,
        exclusions,
        fingerprint_cache_disposition,
        compile_snapshot,
    ) = _compile_manifest(compile_log, compile_result)
    if not manifest.complete:
        exclusions = tuple(sorted({*exclusions, "compile_manifest_incomplete"}))
    blocker_kwargs = {
        "manifest": manifest,
        "gaps": gaps,
        "exclusions": exclusions,
        "scope_kind": scope_kind,
        "endpoint_count": endpoint_count,
    }
    if not manifest.ordered_inputs:
        return _blocked_plan(
            code="compile_inputs_unavailable",
            stage="compile_manifest",
            **blocker_kwargs,
        )
    if not manifest.ordered_tops:
        return _blocked_plan(
            code="compile_tops_unavailable",
            stage="compile_manifest",
            **blocker_kwargs,
        )

    from_root = normalized_from.split(".", 1)[0]
    to_root = normalized_to.split(".", 1)[0]
    if from_root != to_root:
        return _blocked_plan(
            code="path_endpoint_top_mismatch",
            stage="target_scope",
            **blocker_kwargs,
        )
    top = _selected_top(
        signal_path=normalized_from,
        tops=manifest.ordered_tops,
        top_hint=top_hint,
    )
    if (
        top is None
        or _selected_top(
            signal_path=normalized_to,
            tops=manifest.ordered_tops,
            top_hint=top_hint,
        )
        != top
    ):
        return _blocked_plan(
            code="path_endpoint_top_unresolved",
            stage="target_scope",
            **blocker_kwargs,
        )

    from_ancestors = resolve_source_graph_hierarchy_ancestors(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=normalized_from,
    )
    to_ancestors = resolve_source_graph_hierarchy_ancestors(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=normalized_to,
    )
    if from_ancestors is None and to_ancestors is None:
        code = "path_endpoint_hierarchy_unresolved"
    elif from_ancestors is None:
        code = "path_from_hierarchy_unresolved"
    elif to_ancestors is None:
        code = "path_to_hierarchy_unresolved"
    else:
        code = None
    if code is not None:
        return _blocked_plan(
            code=code,
            stage="target_scope",
            gaps=(*gaps, code),
            manifest=manifest,
            exclusions=exclusions,
            scope_kind=scope_kind,
            endpoint_count=endpoint_count,
        )

    assert from_ancestors is not None and to_ancestors is not None
    ancestor_union = _path_ancestor_union(from_ancestors, to_ancestors)
    lca = _path_lca(from_ancestors, to_ancestors)
    target = ConnectivityPathTarget(
        operation=QueryOperation.PATH,
        from_signal=normalized_from,
        to_signal=normalized_to,
        from_instance_path=from_ancestors[-1],
        to_instance_path=to_ancestors[-1],
        expand_assigns=expand_assigns,
    )
    path_hierarchy = PathHierarchyScope(
        from_ancestors=from_ancestors,
        to_ancestors=to_ancestors,
        ancestor_union=ancestor_union,
        lca=lca,
    )
    scope = SourceGraphBuildScope(
        design=(
            f"compile_{manifest.fingerprint[:24]}"
            if manifest.fingerprint
            else "compile_incomplete_manifest"
        ),
        top=top,
        target=target,
        hierarchy_ancestors=ancestor_union,
        requested_cone=RequestedCone(
            operation=QueryOperation.PATH,
            max_hops=max(len(from_ancestors), len(to_ancestors)) - 1,
            instance_paths=ancestor_union,
            cross_instance_boundaries=True,
            stop_at_sequential=True,
            include_control_dependencies=False,
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=ancestor_union,
            objective_exclusions=tuple(exclusions),
        ),
        path_hierarchy=path_hierarchy,
    )
    source_identity = SourceGraphIdentity(
        compile_inputs=manifest,
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version=frontend_version,
    )
    artifact_scope = SourceGraphArtifactScope.from_build_scope(
        scope,
        hierarchy_snapshot_sha256=normalized_hierarchy_snapshot,
    )
    stable_compile_snapshot = (
        compile_snapshot
        or hashlib.sha256(
            _canonical_json(
                {
                    "unproved_compile_snapshot": True,
                    "manifest": manifest.to_dict(),
                }
            )
        ).hexdigest()
    )
    artifact_identity = SourceGraphArtifactIdentity(
        source=source_identity,
        scope=artifact_scope,
        compile_snapshot_sha256=stable_compile_snapshot,
        adapter_version=SOURCE_GRAPH_ADAPTER_VERSION,
        worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        snapshots_complete=compile_snapshot is not None,
    )
    query_identity = SourceGraphQueryIdentity.from_build_scope(scope)
    request = SourceGraphBuildRequest(
        identity=source_identity,
        scope=scope,
        artifact=artifact_identity,
        query=query_identity,
    )
    artifact_key = compute_source_graph_artifact_key(artifact_identity)
    query_key = compute_source_graph_query_key(query_identity)
    receipt = SourceGraphAdapterReceipt(
        status=AdapterStatus.READY,
        input_count=len(manifest.ordered_inputs),
        option_count=len(manifest.ordered_options),
        top_count=len(manifest.ordered_tops),
        manifest_complete=manifest.complete,
        manifest_incomplete_reasons=manifest.incomplete_reasons,
        gap_codes=gaps,
        objective_exclusions=tuple(exclusions),
        ancestor_count=len(ancestor_union),
        requested_cone_instance_count=len(ancestor_union),
        coverage_boundary_instance_count=len(ancestor_union),
        scope_kind=scope_kind,
        endpoint_count=endpoint_count,
        lca_depth=lca.count("."),
        cross_request_reusable=artifact_key.cross_request_reusable,
        artifact_fingerprint_sha256=artifact_key.digest,
        query_fingerprint_sha256=query_key.digest,
        snapshot_identity_complete=artifact_identity.snapshots_complete,
        fingerprint_cache_disposition=fingerprint_cache_disposition,
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=request,
        receipt=receipt,
    )
