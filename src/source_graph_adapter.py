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

from collections import Counter, OrderedDict
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
from .filelist_tokenizer import tokenize_filelist
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


SOURCE_GRAPH_ADAPTER_VERSION = "3.7"
DEFAULT_SOURCE_GRAPH_FRONTIER_INSTANCE_LIMIT = 128
_FRONTEND_HDL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_OPAQUE_HDL_SUFFIXES = {".vhd", ".vhdl"}
_HDL_SUFFIXES = _FRONTEND_HDL_SUFFIXES | _OPAQUE_HDL_SUFFIXES
_NATIVE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".o", ".a", ".so"}
_ENV_REF_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_FIXED_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_FILELIST_DEPTH = 64
_MAX_FILELIST_TOKENS = 1_000_000
_MAX_ENV_INFERENCE_FILELISTS = 256
_MAX_ENV_INFERENCE_ROUNDS = 64
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
    "-Mdir",
    "-access",
    "-assert",
    "-covfile",
    "-covtest",
    "-covworkdir",
    "-errormax",
    "-input",
    "-l",
    "-log",
    "-logfile",
    "-nowarn",
    "-ntb_opts",
    "-o",
    "-seed",
    "-snapshot",
    "-svseed",
    "-uvmpackagename",
    "-work",
    "-xlrm",
    "-xprop",
    "-xmlibdirname",
    "-xmerror",
}
_NATIVE_TOOLCHAIN_VALUE_OPTIONS = {
    "-CFLAGS",
    "-LDFLAGS",
    "-Xcflags",
    "-Xldflags",
}
_IGNORED_FLAGS = {
    "+v2k",
    "+vpi",
    "-64bit",
    "-R",
    "-coverage",
    "-covoverwrite",
    "-deraceclockdata",
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
_IGNORED_OPTION_PREFIXES = (
    "+ntb_",
    "+vcs+",
    "+warn=",
    "+xm",
    "-covoverwrite",
    "-debug",
    "-error=",
)
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
class HierarchyAncestorResolution:
    """Private path-resolution evidence with a privacy-safe public summary.

    A dotted suffix after the last proved instance is not automatically a
    hierarchy failure: it may be an interface or packed member.  The candidate
    path therefore stays private and the public receipt reports only counts.
    ``missing_instance_proved`` is true only when the hierarchy scan metadata
    independently names the missing child instance.
    """

    ancestors: tuple[str, ...]
    remaining_path_segment_count: int
    stop_depth: int | None = None
    candidate_instance_path: str | None = None
    missing_instance_proved: bool = False

    def __post_init__(self) -> None:
        ancestors = tuple(str(path) for path in self.ancestors)
        if not ancestors or any(not path for path in ancestors):
            raise ValueError("hierarchy resolution requires proved ancestors")
        if self.remaining_path_segment_count < 1:
            raise ValueError("hierarchy resolution must retain a signal suffix")
        if self.candidate_instance_path is None:
            if self.stop_depth is not None or self.missing_instance_proved:
                raise ValueError(
                    "resolved hierarchy path cannot carry missing-instance evidence"
                )
        else:
            if not self.candidate_instance_path or self.stop_depth is None:
                raise ValueError(
                    "deferred hierarchy path requires a candidate and stop depth"
                )
            if self.stop_depth < 1:
                raise ValueError("hierarchy stop depth must be positive")
        object.__setattr__(self, "ancestors", ancestors)

    @property
    def status(self) -> str:
        if self.candidate_instance_path is None:
            return "resolved"
        if self.missing_instance_proved:
            return "truncated"
        return "deferred"


def _aggregate_hierarchy_resolutions(
    resolutions: Sequence[HierarchyAncestorResolution],
) -> dict[str, Any] | None:
    if not resolutions:
        return None
    statuses = {item.status for item in resolutions}
    status = next(iter(statuses)) if len(statuses) == 1 else "mixed"
    stop_depths = [
        item.stop_depth for item in resolutions if item.stop_depth is not None
    ]
    return {
        "status": status,
        "endpoint_count": len(resolutions),
        "resolved_endpoint_count": sum(
            item.status == "resolved" for item in resolutions
        ),
        "deferred_endpoint_count": sum(
            item.status == "deferred" for item in resolutions
        ),
        "truncated_endpoint_count": sum(
            item.status == "truncated" for item in resolutions
        ),
        "max_matched_instance_count": max(len(item.ancestors) for item in resolutions),
        "max_remaining_path_segment_count": max(
            item.remaining_path_segment_count for item in resolutions
        ),
        "first_stop_depth": min(stop_depths) if stop_depths else None,
        "missing_instance_proved": any(
            item.missing_instance_proved for item in resolutions
        ),
    }


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
    hierarchy_resolutions: tuple[HierarchyAncestorResolution, ...] = ()
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
            "single_endpoint_expanded",
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
        resolutions = tuple(self.hierarchy_resolutions)
        if any(
            not isinstance(item, HierarchyAncestorResolution) for item in resolutions
        ):
            raise ValueError("invalid hierarchy ancestor resolution")
        object.__setattr__(self, "hierarchy_resolutions", resolutions)
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
        hierarchy_resolution = _aggregate_hierarchy_resolutions(
            self.hierarchy_resolutions
        )
        if hierarchy_resolution is not None:
            result["scope"]["hierarchy_resolution"] = hierarchy_resolution
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

    @property
    def unprojected_instance_candidates(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.candidate_instance_path
                for item in self.receipt.hierarchy_resolutions
                if item.candidate_instance_path is not None
            )
        )


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
    env_overrides: dict[str, str] = field(default_factory=dict)
    inferred_env_names: set[str] = field(default_factory=set)
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


def _env_ref_name(match: re.Match[str]) -> str:
    rendered = match.group(0)
    return rendered[2:-1] if rendered.startswith("${") else rendered[1:]


def _expand_with_environment(value: str, overrides: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = _env_ref_name(match)
        if name in overrides:
            return overrides[name]
        return os.environ.get(name, match.group(0))

    return _ENV_REF_RE.sub(replace, os.path.expanduser(value))


def _expand_with_overrides_only(value: str, overrides: Mapping[str, str]) -> str:
    return _ENV_REF_RE.sub(
        lambda match: overrides.get(_env_ref_name(match), match.group(0)),
        os.path.expanduser(value),
    )


def _expand_token(value: str, state: _TranslationState) -> str | None:
    expanded = _expand_with_environment(value, state.env_overrides)
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


def _mark_native_toolchain_exclusion(state: _TranslationState) -> None:
    """Record C/C++ or linker inputs that cannot affect the static HDL graph."""

    state.objective_exclusions.add("dpi_runtime")
    state.gap_codes.add("native_runtime_input_excluded")


def _drop_bootstrap_library_search_options(state: _TranslationState) -> None:
    """Prevent a scoped bootstrap from reopening broad HDL libraries.

    Compile/filelist replay is still useful for exact defines and include
    options, and simulator-recorded reconciliation later replaces its source
    operands with the proved bootstrap subset.  ``-v`` and ``-y`` are
    different: they can make the frontend parse a large library outside that
    subset.  Remove only those normalized option/value pairs and keep the
    limitation explicit in coverage.
    """

    filtered: list[str] = []
    removed_support: set[Path] = set()
    index = 0
    removed = False
    while index < len(state.options):
        option = state.options[index]
        if option in {"-v", "-y"}:
            removed = True
            if option == "-v" and index + 1 < len(state.options):
                removed_support.add(Path(state.options[index + 1]))
            index += 2 if index + 1 < len(state.options) else 1
            continue
        filtered.append(option)
        index += 1
    if not removed:
        return
    state.options = filtered
    state.support_files.difference_update(removed_support)
    state.options_complete = False
    state.gap_codes.add("bootstrap_library_context_scoped")
    state.objective_exclusions.add("bootstrap_library_context_scoped")


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
        tokens = tokenize_filelist(text)
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
        if executable in {"vcs", "vlogan", "vhdlan", "xrun", "irun"}:
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

        # Simulator switches can carry values that look like source paths
        # (for example ``+define+ROM=/images/boot.v``, ``-DMODEL=foo.sv``,
        # or ``-v/lib/cells.v``).  Only a bare operand can be classified by
        # suffix; a leading ``+`` or ``-`` keeps the token in option dispatch.
        if not token.startswith(("+", "-")) and suffix in _HDL_SUFFIXES:
            _append_source(state, token, base)
            index += 1
            continue
        if not token.startswith(("+", "-")) and suffix in _NATIVE_SUFFIXES:
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
        if token in _IGNORED_FLAGS or token.startswith(_IGNORED_OPTION_PREFIXES):
            index += 1
            continue
        if token in {"-y", "-v"} and index + 1 < len(tokens):
            _append_path_option(state, token, tokens[index + 1], base)
            index += 2
            continue
        if token.startswith(("-I", "-y")) and len(token) > 2:
            _append_path_option(state, token[:2], token[2:], base)
            index += 1
            continue
        if (
            token.startswith("-v")
            and len(token) > 2
            and Path(token[2:]).suffix.lower() in _HDL_SUFFIXES
        ):
            _append_path_option(state, "-v", token[2:], base)
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
        if token == "-ntb_opts":
            if index + 1 >= len(tokens):
                _mark_unclassified(state)
                index += 1
                continue
            if tokens[index + 1].lower().startswith("uvm"):
                state.objective_exclusions.add("uvm_dynamic_connectivity")
            index += 2
            continue
        if token in _IGNORED_VALUE_OPTIONS:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if any(token.startswith(f"{option}=") for option in _IGNORED_VALUE_OPTIONS):
            index += 1
            continue
        if token == "-L" and index + 1 < len(tokens):
            _mark_native_toolchain_exclusion(state)
            index += 2
            continue
        if token.startswith("-L") or (
            token.startswith("-l") and token not in {"-l", "-licqueue"}
        ):
            _mark_native_toolchain_exclusion(state)
            index += 1
            continue
        if token in _NATIVE_TOOLCHAIN_VALUE_OPTIONS:
            _mark_native_toolchain_exclusion(state)
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if any(
            token.startswith(f"{option}=") for option in _NATIVE_TOOLCHAIN_VALUE_OPTIONS
        ) or token.startswith(("-Wl,", "-Xlinker=")):
            _mark_native_toolchain_exclusion(state)
            index += 1
            continue
        if token in {"&&", ";", "|"}:
            _mark_unclassified(state)
            index += 1
            continue
        _mark_unclassified(state)
        index += 1


def _compile_evidence(
    compile_result: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    evidence = compile_result.get("compile_evidence")
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("schema_version") != 1:
        return None
    return evidence


def _compilation_unit_records(
    compile_result: Mapping[str, Any],
    *,
    require_simulator_log: bool = False,
) -> list[Mapping[str, Any]]:
    evidence = _compile_evidence(compile_result)
    if evidence is None:
        return []
    if require_simulator_log:
        order_source = evidence.get("unit_order_source")
        bootstrap = compile_result.get("bootstrap_context")
        bounded_subset = (
            order_source == "bootstrap_subset"
            and isinstance(bootstrap, Mapping)
            and bootstrap.get("used") is True
        )
        if order_source != "simulator_log" and not bounded_subset:
            return []
    raw = evidence.get("ordered_compilation_units")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping) and item.get("path")]


def _phase_recorded_inputs(
    compile_result: Mapping[str, Any],
    *,
    source_log_index: int | None,
    suffixes: set[str],
) -> list[str]:
    """Return recorded opaque inputs belonging to one merged source phase.

    A VHDL-only phase is never replayed into Slang. Even when its source list
    came from bounded command recovery rather than simulator-emitted unit
    records, retain those files in content identity; the merge warning still
    prevents the manifest from claiming exact source order or reusable coverage.
    """

    if source_log_index is None:
        return []
    paths: list[str] = []
    for item in _compilation_unit_records(compile_result):
        if item.get("source_log_index") != source_log_index:
            continue
        if str(item.get("role") or "project") == "simulator_instrumentation":
            continue
        path = Path(str(item["path"])).resolve(strict=False)
        if path.suffix.lower() in suffixes:
            paths.append(str(path))
    return paths


def _recorded_compilation_unit_paths(
    compile_result: Mapping[str, Any],
    *,
    roles: set[str] | None = None,
    require_simulator_log: bool = False,
) -> list[str]:
    result: list[str] = []
    for item in _compilation_unit_records(
        compile_result, require_simulator_log=require_simulator_log
    ):
        role = str(item.get("role") or "project")
        if roles is not None and role not in roles:
            continue
        result.append(str(Path(str(item["path"])).resolve(strict=False)))
    return result


def _simulator_recorded_inputs(
    compile_result: Mapping[str, Any],
) -> tuple[list[str], bool]:
    records = _compilation_unit_records(compile_result, require_simulator_log=True)
    paths: list[str] = []
    instrumentation_excluded = False
    for item in records:
        role = str(item.get("role") or "project")
        if role == "simulator_instrumentation":
            instrumentation_excluded = True
            continue
        paths.append(str(Path(str(item["path"])).resolve(strict=False)))
    return paths, instrumentation_excluded


def _compile_evidence_support_paths(
    compile_result: Mapping[str, Any],
) -> set[Path]:
    evidence = _compile_evidence(compile_result)
    if evidence is None:
        return set()
    raw = evidence.get("filelists")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return set()
    result: set[Path] = set()
    for item in raw:
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        rendered = str(item["path"])
        raw_path = str(item.get("raw_path") or "")
        if _ENV_REF_RE.search(rendered) or _ENV_REF_RE.search(raw_path):
            continue
        result.add(Path(rendered).resolve(strict=False))
    return result


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


def _ordered_included_child_paths(
    compile_result: Mapping[str, Any],
) -> tuple[Path, ...]:
    evidence = _compile_evidence(compile_result)
    ordered: list[Path] = []
    if evidence is not None:
        raw = evidence.get("ordered_includes")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                if not isinstance(item, Mapping) or not item.get("path"):
                    continue
                ordered.append(Path(str(item["path"])).resolve(strict=False))
    if ordered:
        return tuple(dict.fromkeys(ordered))

    tree = compile_result.get("include_tree")
    if not isinstance(tree, Mapping):
        return ()
    for children in tree.values():
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            continue
        ordered.extend(
            Path(str(child)).resolve(strict=False) for child in children if child
        )
    return tuple(dict.fromkeys(ordered))


def _included_child_paths(compile_result: Mapping[str, Any]) -> set[Path]:
    return set(_ordered_included_child_paths(compile_result))


def _evidence_anchor_paths(compile_result: Mapping[str, Any]) -> tuple[Path, ...]:
    anchors: list[Path] = []
    for item in _compilation_unit_records(compile_result):
        anchors.append(Path(str(item["path"])).resolve(strict=False))
        if item.get("reported_path"):
            anchors.append(Path(str(item["reported_path"])).absolute())

    evidence = _compile_evidence(compile_result)
    if evidence is not None:
        includes = evidence.get("ordered_includes")
        if isinstance(includes, Sequence) and not isinstance(includes, (str, bytes)):
            for item in includes:
                if not isinstance(item, Mapping):
                    continue
                for key in ("parent", "path"):
                    if item.get(key):
                        path = Path(str(item[key])).resolve(strict=False)
                        anchors.extend((path, path.parent))
        filelists = evidence.get("filelists")
        if isinstance(filelists, Sequence) and not isinstance(filelists, (str, bytes)):
            for item in filelists:
                if not isinstance(item, Mapping) or not item.get("path"):
                    continue
                rendered = str(item["path"])
                raw_path = str(item.get("raw_path") or "")
                if not _ENV_REF_RE.search(rendered) and not _ENV_REF_RE.search(
                    raw_path
                ):
                    anchors.append(Path(rendered).resolve(strict=False))
    return tuple(dict.fromkeys(anchors))


def _iter_path_expressions(tokens: Sequence[str]):
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F"}:
            index += 2
            continue
        if token in {"-incdir", "-I", "-v", "-y"}:
            if index + 1 < len(tokens):
                yield tokens[index + 1]
            index += 2
            continue
        if token.startswith("+incdir+"):
            yield from (item for item in token[len("+incdir+") :].split("+") if item)
            index += 1
            continue
        if token.startswith(("-I", "-v", "-y")) and len(token) > 2:
            yield token[2:]
            index += 1
            continue
        if not token.startswith(("+", "-")) and _ENV_REF_RE.search(token):
            yield token
        index += 1


def _iter_filelist_references(tokens: Sequence[str]):
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F"} and index + 1 < len(tokens):
            yield token, tokens[index + 1]
            index += 2
            continue
        index += 1


def _candidate_environment_values(
    expression: str,
    *,
    base: Path,
    anchors: Sequence[Path],
) -> tuple[str, set[str]] | None:
    matches = list(_ENV_REF_RE.finditer(expression))
    if len(matches) != 1:
        return None
    match = matches[0]
    name = _env_ref_name(match)
    prefix = expression[: match.start()]
    suffix = expression[match.end() :]
    if _ENV_REF_RE.search(prefix) or _ENV_REF_RE.search(suffix):
        return None
    if not prefix and not suffix:
        return None

    candidates: set[str] = set()
    for anchor in anchors:
        rendered = str(anchor)
        if prefix and not rendered.startswith(prefix):
            continue
        if suffix and not rendered.endswith(suffix):
            continue
        end = len(rendered) - len(suffix) if suffix else len(rendered)
        candidate = rendered[len(prefix) : end]
        if not candidate or _ENV_REF_RE.search(candidate):
            continue
        expanded = prefix + candidate + suffix
        path = Path(expanded)
        if not path.is_absolute():
            path = base / path
        if path.absolute() == anchor.absolute() or path.resolve(
            strict=False
        ) == anchor.resolve(strict=False):
            candidates.add(candidate)
    return (name, candidates) if candidates else None


def _candidate_filelist_root_values(
    expression: str,
    *,
    base: Path,
    anchors: Sequence[Path],
) -> tuple[str, set[str]] | None:
    """Find a unique root candidate for ``$ROOT/path/to/file.f``.

    A VCS log does not normally print the expanded ``-f`` path.  It does print
    the files compiled from that command file, so their bounded ancestor set is
    the only local search space needed for the common project-root convention.
    Candidates are accepted only when the exact substituted command file
    exists; the later compilation-unit order check still validates its
    contents before the binding can make a manifest reusable.
    """

    matches = list(_ENV_REF_RE.finditer(expression))
    if len(matches) != 1 or matches[0].start() != 0:
        return None
    match = matches[0]
    suffix = expression[match.end() :]
    if not suffix or _ENV_REF_RE.search(suffix):
        return None

    roots: set[Path] = set()
    for index, anchor in enumerate(anchors):
        if index % 256 == 0:
            check_cancelled()
        absolute = anchor.absolute()
        start = absolute if absolute.is_dir() else absolute.parent
        roots.add(start)
        roots.update(start.parents)

    candidates: set[str] = set()
    for index, root in enumerate(sorted(roots, key=lambda item: str(item))):
        if index % 256 == 0:
            check_cancelled()
        value = str(root)
        rendered = value + suffix
        path = Path(rendered)
        if not path.is_absolute():
            path = base / path
        if path.is_file():
            candidates.add(value)
    return (_env_ref_name(match), candidates) if candidates else None


def _infer_compile_environment(
    command: str,
    *,
    command_dir: Path,
    compile_result: Mapping[str, Any],
) -> tuple[dict[str, str], set[str]]:
    """Infer only uniquely constrained path variables from local log facts.

    No filesystem search, glob, subprocess, or environment mutation is used.
    Starting from the recorded command, newly resolved command files are read
    iteratively; every accepted binding is the sole value consistent with all
    path expressions that matched simulator-reported absolute paths.
    """

    anchors = _evidence_anchor_paths(compile_result)
    if not anchors:
        return {}, set()
    try:
        command_tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        return {}, set()

    documents: list[tuple[list[str], Path]] = [(command_tokens, command_dir)]
    visited_filelists: set[Path] = set()
    bindings: dict[str, str] = {}
    inferred_names: set[str] = set()
    token_count = len(command_tokens)

    for _round in range(_MAX_ENV_INFERENCE_ROUNDS):
        constraints: dict[str, list[set[str]]] = {}
        pending_filelists: list[tuple[str, str, Path]] = []
        for tokens, base in documents:
            check_cancelled()
            for mode, raw_path in _iter_filelist_references(tokens):
                rendered = _expand_with_overrides_only(raw_path, bindings)
                candidate = _candidate_environment_values(
                    rendered,
                    base=base,
                    anchors=anchors,
                ) or _candidate_filelist_root_values(
                    rendered,
                    base=base,
                    anchors=anchors,
                )
                if candidate is not None:
                    name, values = candidate
                    if name not in bindings:
                        constraints.setdefault(name, []).append(values)
                pending_filelists.append((mode, raw_path, base))
            for expression in _iter_path_expressions(tokens):
                # Keep a process-environment value visible as a constraint
                # until the log proves it. An MCP server can inherit a stale
                # variable from another project; a unique simulator-recorded
                # binding must override that value locally rather than trust
                # ambient state by accident.
                rendered = _expand_with_overrides_only(expression, bindings)
                candidate = _candidate_environment_values(
                    rendered,
                    base=base,
                    anchors=anchors,
                )
                if candidate is None:
                    continue
                name, values = candidate
                if name not in bindings:
                    constraints.setdefault(name, []).append(values)

        changed = False
        for name, candidate_sets in constraints.items():
            common = set.intersection(*candidate_sets)
            if len(common) != 1:
                continue
            value = next(iter(common))
            if bindings.get(name) == value:
                continue
            bindings[name] = value
            inferred_names.add(name)
            changed = True

        for mode, raw_path, base in pending_filelists:
            rendered = _expand_with_environment(raw_path, bindings)
            if _ENV_REF_RE.search(rendered):
                continue
            path = Path(rendered)
            if not path.is_absolute():
                path = base / path
            path = path.resolve(strict=False)
            if path in visited_filelists or not path.is_file():
                continue
            if len(visited_filelists) >= _MAX_ENV_INFERENCE_FILELISTS:
                break
            try:
                tokens = tokenize_filelist(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except (OSError, ValueError):
                continue
            if token_count + len(tokens) > _MAX_FILELIST_TOKENS:
                break
            token_count += len(tokens)
            visited_filelists.add(path)
            token_base = command_dir if mode == "-f" else path.parent
            documents.append((tokens, token_base))
            changed = True

        if not changed:
            break
    return bindings, inferred_names


def _configured_include_dirs(state: _TranslationState) -> set[str]:
    existing: set[str] = set()
    index = 0
    while index < len(state.options):
        token = state.options[index]
        if token == "-I" and index + 1 < len(state.options):
            existing.add(str(Path(state.options[index + 1]).resolve(strict=False)))
            index += 2
            continue
        if token.startswith("+incdir+"):
            existing.update(
                str(Path(item).resolve(strict=False))
                for item in token[len("+incdir+") :].split("+")
                if item
            )
        index += 1
    return existing


def _append_include_dirs(
    state: _TranslationState,
    directories: Sequence[Path],
    *,
    after_base_options: bool = False,
) -> bool:
    existing = _configured_include_dirs(state)
    additions: list[str] = []
    for path in directories:
        directory = str(path.resolve(strict=False))
        if directory in existing or not path.is_dir():
            continue
        additions.extend(("-I", directory))
        existing.add(directory)
    if not additions:
        return False
    if after_base_options:
        index = len(_BASE_OPTIONS.get(state.simulator, ()))
        state.options[index:index] = additions
    else:
        state.options.extend(additions)
    return True


def _append_log_recovered_include_dirs(
    state: _TranslationState,
    compile_result: Mapping[str, Any],
) -> None:
    directories = [
        child.parent for child in _ordered_included_child_paths(compile_result)
    ]
    if _append_include_dirs(state, tuple(dict.fromkeys(directories))):
        state.options_complete = False
        state.gap_codes.add("compile_include_dirs_recovered_from_log")


def _append_recorded_simulator_library_context(
    state: _TranslationState,
    compile_result: Mapping[str, Any],
) -> None:
    library_paths = _recorded_compilation_unit_paths(
        compile_result,
        roles={"simulator_library"},
        require_simulator_log=True,
    )
    state.simulator_library_inputs.update(library_paths)
    _append_include_dirs(
        state,
        tuple(
            dict.fromkeys(
                Path(path).parent
                for path in library_paths
                if _is_uvm_language_input(path)
            )
        ),
        after_base_options=True,
    )


def _is_environment_recovery_warning(value: str) -> bool:
    return value.startswith(
        (
            "VCS environment-dependent source unavailable:",
            "VCS environment-dependent filelist unavailable:",
        )
    ) or (
        "$" in value
        and value.startswith(("VCS source missing:", "VCS filelist missing:"))
    )


def _is_uvm_language_input(path: str) -> bool:
    return Path(path).name.lower() in {"uvm.sv", "uvm_pkg.sv", "cdns_uvm_pkg.sv"}


def _canonicalize_expanded_xcelium_inputs(
    state: _TranslationState,
    compile_result: Mapping[str, Any],
) -> None:
    observed, instrumentation_excluded = _simulator_recorded_inputs(compile_result)
    if not observed:
        return
    counts = Counter(observed)
    consumed: Counter[str] = Counter()
    translated_observed: list[str] = []
    extras: list[str] = []
    observed_set = set(observed)
    for path in state.inputs:
        if path in observed_set and consumed[path] < counts[path]:
            translated_observed.append(path)
            consumed[path] += 1
            continue
        if (
            (path in state.simulator_library_inputs or _is_uvm_language_input(path))
            and path not in observed_set
            and path not in extras
        ):
            extras.append(path)
    if translated_observed != observed:
        state.inputs_complete = False
        state.gap_codes.add("compile_log_source_reconciliation_gap")
    state.inputs = [*extras, *observed]
    if instrumentation_excluded:
        state.gap_codes.add("simulator_instrumentation_excluded")


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
        "compile_evidence": compile_result.get("compile_evidence") or {},
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
    bootstrap = compile_result.get("bootstrap_context")
    bootstrap_used = isinstance(bootstrap, Mapping) and bootstrap.get("used") is True
    if bootstrap_used:
        state.inputs_complete = False
        state.gap_codes.add("bootstrap_compile_inputs_scoped")
        raw_exclusions = bootstrap.get("objective_exclusions")
        if isinstance(raw_exclusions, Sequence) and not isinstance(
            raw_exclusions, (str, bytes)
        ):
            for exclusion in raw_exclusions:
                rendered = str(exclusion)
                if _FIXED_LABEL_RE.fullmatch(rendered):
                    state.objective_exclusions.add(rendered)
    replay_command = str(
        compile_result.get("compile_replay_command")
        or compile_result.get("compile_command")
        or ""
    )
    evidence = _compile_evidence(compile_result)
    raw_source_phases = evidence.get("source_phases") if evidence is not None else None
    source_phases = (
        [item for item in raw_source_phases if isinstance(item, Mapping)]
        if isinstance(raw_source_phases, Sequence)
        and not isinstance(raw_source_phases, (str, bytes))
        else []
    )
    expanded_command = (
        str(evidence.get("expanded_replay_command") or "")
        if simulator == "xcelium" and evidence is not None
        else ""
    )
    phase_commands: list[tuple[str, Path, str, int | None]] = []
    if source_phases:
        for phase in source_phases:
            rendered = str(
                phase.get("expanded_replay_command")
                or phase.get("compile_replay_command")
                or phase.get("compile_command")
                or ""
            )
            phase_cwd = Path(
                str(phase.get("compile_cwd") or command_dir)
            ).resolve(strict=False)
            language = str(phase.get("language") or "unknown")
            raw_log_index = phase.get("source_log_index")
            log_index = raw_log_index if isinstance(raw_log_index, int) else None
            phase_commands.append((rendered, phase_cwd, language, log_index))
        command = _canonical_json(
            [
                {
                    "command": rendered,
                    "compile_cwd": str(phase_cwd),
                    "language": language,
                }
                for rendered, phase_cwd, language, _ in phase_commands
            ]
        ).decode("utf-8")
    else:
        command = expanded_command or replay_command
    parse_warnings = compile_result.get("parse_warnings")
    warning_items = (
        [str(item) for item in parse_warnings]
        if isinstance(parse_warnings, Sequence)
        and not isinstance(parse_warnings, (str, bytes))
        else []
    )

    if source_phases and any(
        _ENV_REF_RE.search(rendered)
        for rendered, _, language, _ in phase_commands
        if rendered and language != "vhdl"
    ):
        # Cross-phase environment inference needs a phase-local proof for each
        # command. Keep the recovered simulator unit order usable, but do not
        # claim exact option replay until every phase binding is explicit.
        state.options_complete = False
        state.gap_codes.add("compile_environment_unresolved")
    elif (
        replay_command
        and not expanded_command
        and (
            _ENV_REF_RE.search(replay_command)
            or any(_is_environment_recovery_warning(item) for item in warning_items)
        )
    ):
        bindings, inferred_names = _infer_compile_environment(
            replay_command,
            command_dir=command_dir,
            compile_result=compile_result,
        )
        state.env_overrides.update(bindings)
        state.inferred_env_names.update(inferred_names)

    if source_phases:
        frontend_phase_options: list[tuple[str, ...]] = []
        if not phase_commands or any(
            not rendered
            for rendered, _, language, _ in phase_commands
            if language != "vhdl"
        ):
            state.options_complete = False
            state.gap_codes.add("compile_command_missing")
        for rendered, phase_cwd, language, log_index in phase_commands:
            if language == "vhdl":
                state.gap_codes.add("opaque_vhdl_boundary")
                state.objective_exclusions.add("opaque_vhdl_boundary")
                for path in _phase_recorded_inputs(
                    compile_result,
                    source_log_index=log_index,
                    suffixes=_OPAQUE_HDL_SUFFIXES,
                ):
                    state.inputs.append(path)
                continue
            if not rendered:
                frontend_phase_options.append(())
                continue
            try:
                tokens = shlex.split(rendered, comments=True, posix=True)
            except ValueError:
                state.inputs_complete = False
                state.options_complete = False
                state.gap_codes.add("compile_command_parse_failed")
                continue
            if not tokens:
                continue
            previous_command_dir = state.command_dir
            state.command_dir = phase_cwd
            option_start = len(state.options)
            try:
                _translate_tokens(
                    state,
                    tokens,
                    base=phase_cwd,
                    skip_executable=True,
                )
            finally:
                state.command_dir = previous_command_dir
            frontend_phase_options.append(tuple(state.options[option_start:]))
        if len(frontend_phase_options) > 1 and any(
            options != frontend_phase_options[0]
            for options in frontend_phase_options[1:]
        ):
            # Slang consumes one global option vector, while independent
            # vlogan phases can have phase-local defines/include lookup rules.
            # Replay remains useful for positive facts, but it cannot claim an
            # exact reusable manifest when those phase-local semantics differ.
            state.options_complete = False
            state.gap_codes.add("phase_local_compile_options_unmodeled")
            state.objective_exclusions.add(
                "phase_local_compile_options_unmodeled"
            )
    elif not command:
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
            if simulator == "xcelium" and "-uvmhome" in tokens and not bootstrap_used:
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

    if bootstrap_used:
        _drop_bootstrap_library_search_options(state)

    if expanded_command:
        _canonicalize_expanded_xcelium_inputs(state, compile_result)

    recorded_inputs, instrumentation_excluded = _simulator_recorded_inputs(
        compile_result
    )
    recorded_project = _recorded_compilation_unit_paths(
        compile_result,
        roles={"project"},
        require_simulator_log=True,
    )
    recorded_simulator_libraries = _recorded_compilation_unit_paths(
        compile_result,
        roles={"simulator_library"},
        require_simulator_log=True,
    )
    recorded_instrumentation = _recorded_compilation_unit_paths(
        compile_result,
        roles={"simulator_instrumentation"},
        require_simulator_log=True,
    )
    recorded_nonproject = {
        *recorded_simulator_libraries,
        *recorded_instrumentation,
    }
    recorded_project_set = set(recorded_project)
    translated_project = [
        path
        for path in state.inputs
        if path not in recorded_nonproject
        and path not in state.simulator_library_inputs
        and not (_is_uvm_language_input(path) and path not in recorded_project_set)
    ]
    project_sequence_matches = bool(recorded_project) and (
        translated_project == recorded_project
    )

    env_unresolved = "compile_environment_unresolved" in state.gap_codes
    inference_failed_validation = bool(state.inferred_env_names) and not (
        project_sequence_matches or not recorded_project
    )
    if state.inferred_env_names and not inference_failed_validation:
        state.gap_codes.add("compile_environment_inferred_from_log")

    recorded_input_set = set(recorded_inputs)
    unrecorded_simulator_libraries = [
        path
        for path in state.inputs
        if path in state.simulator_library_inputs and path not in recorded_input_set
    ]
    reconciliation_failed = bool(recorded_project) and not project_sequence_matches
    if recorded_inputs and (
        env_unresolved or reconciliation_failed or not state.inputs
    ):
        state.inputs = [*unrecorded_simulator_libraries, *recorded_inputs]
        state.inputs_complete = False
        state.options_complete = False
        state.gap_codes.add("compile_inputs_recovered_from_simulator_log")
        if inference_failed_validation:
            state.gap_codes.add("compile_environment_inference_unverified")
        if reconciliation_failed:
            state.gap_codes.add("compile_log_source_reconciliation_gap")
        _append_log_recovered_include_dirs(state, compile_result)
    elif recorded_project:
        # Once normal replay proves the project sequence, retain the complete
        # simulator order as the source of truth. This restores injected
        # language units such as uvm_pkg.sv while excluding recorder sources.
        state.inputs = [*unrecorded_simulator_libraries, *recorded_inputs]
    elif not recorded_project:
        # Backward compatibility for caller-constructed compile_result values
        # that predate compile_evidence. This remains approximate and is never
        # used when a simulator-recorded compilation-unit sequence exists.
        reported = _reported_source_paths(compile_result)
        if not state.inputs and reported:
            state.inputs.extend(reported)
            state.inputs_complete = False
            state.gap_codes.add("compile_input_order_recovered_approximately")
        elif reported:
            include_paths = {
                str(path) for path in _included_child_paths(compile_result)
            }
            reported_direct = set(reported) - include_paths
            translated = set(state.inputs) - state.simulator_library_inputs
            if reported_direct and reported_direct != translated:
                state.inputs_complete = False
                state.gap_codes.add("compile_log_source_reconciliation_gap")

    if not bootstrap_used:
        _append_recorded_simulator_library_context(state, compile_result)
    if instrumentation_excluded:
        state.gap_codes.add("simulator_instrumentation_excluded")
    if not state.inputs:
        state.inputs_complete = False
        state.gap_codes.add("compile_inputs_empty")
    if any(Path(path).suffix.lower() in _OPAQUE_HDL_SUFFIXES for path in state.inputs):
        state.gap_codes.add("opaque_vhdl_boundary")
        state.objective_exclusions.add("opaque_vhdl_boundary")

    material_warnings = warning_items
    if state.inferred_env_names and not env_unresolved and project_sequence_matches:
        material_warnings = [
            item for item in warning_items if not _is_environment_recovery_warning(item)
        ]
    if material_warnings:
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
    state.support_files.update(_compile_evidence_support_paths(compile_result))
    state.support_files.difference_update(
        Path(path) for path in recorded_instrumentation
    )
    if tops:
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
    else:
        # A missing elaboration top blocks every Source Graph plan before a
        # reusable artifact can exist. Avoid hashing a potentially huge source
        # set merely to report that deterministic blocker; the later merged
        # hierarchy call will hash normally once a supplementary elab log
        # supplies a proved top.
        fingerprint = None
        content_complete = True

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


def _scan_proves_child_instance(
    hierarchy_result: Mapping[str, Any],
    *,
    parent_module: str | None,
    instance_name: str,
) -> bool:
    """Return true only for one unambiguous scanned parent definition."""

    if not parent_module:
        return False
    scans = hierarchy_result.get("_scan_results")
    if not isinstance(scans, Sequence) or isinstance(scans, (str, bytes)):
        return False
    parent_definition_count = 0
    child_proved = False
    for scan in scans:
        check_cancelled()
        if not isinstance(scan, Mapping):
            continue
        by_module = scan.get("module_instance_map")
        if not isinstance(by_module, Mapping) or parent_module not in by_module:
            continue
        items = by_module.get(parent_module)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        parent_definition_count += 1
        child_proved = child_proved or any(
            isinstance(item, Mapping)
            and str(item.get("instance_name") or "") == instance_name
            for item in items
        )
    return parent_definition_count == 1 and child_proved


def resolve_source_graph_hierarchy_scope(
    *,
    hierarchy_result: Mapping[str, Any],
    top: str,
    signal_path: str,
) -> HierarchyAncestorResolution | None:
    """Resolve the proved instance prefix and retain any deferred suffix fact."""

    component_tree = hierarchy_result.get("component_tree")
    if not isinstance(component_tree, Mapping):
        return None
    parts = signal_path.split(".")
    if len(parts) < 2 or parts[0] != top:
        return None
    ancestors = [top]
    children = component_tree.get(top)
    parent_module: str | None = top
    index = 1
    while index < len(parts) - 1 and isinstance(children, Mapping):
        check_cancelled()
        node = children.get(parts[index])
        if not isinstance(node, Mapping):
            break
        ancestors.append(".".join(parts[: index + 1]))
        raw_module = node.get("class") or node.get("module")
        parent_module = str(raw_module) if raw_module else None
        nested = node.get("children")
        children = nested if isinstance(nested, Mapping) else None
        index += 1
    # Once a segment is not a proved child instance, the remaining suffix is
    # deferred to the frontend/query as a signal, interface field, or packed
    # aggregate member. This never invents an instance: the artifact remains
    # bounded to the last proved ancestor and an invalid suffix is rejected by
    # exact IR declaration lookup before any fact can be returned.
    candidate_instance_path = None
    missing_instance_proved = False
    stop_depth = None
    if index < len(parts) - 1:
        candidate_instance_path = ".".join(parts[: index + 1])
        stop_depth = index
        missing_instance_proved = _scan_proves_child_instance(
            hierarchy_result,
            parent_module=parent_module,
            instance_name=parts[index],
        )
    return HierarchyAncestorResolution(
        ancestors=tuple(ancestors),
        remaining_path_segment_count=len(parts) - index,
        stop_depth=stop_depth,
        candidate_instance_path=candidate_instance_path,
        missing_instance_proved=missing_instance_proved,
    )


def resolve_source_graph_hierarchy_ancestors(
    *,
    hierarchy_result: Mapping[str, Any],
    top: str,
    signal_path: str,
) -> tuple[str, ...] | None:
    """Compatibility wrapper returning only the proved ancestor chain."""

    resolution = resolve_source_graph_hierarchy_scope(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=signal_path,
    )
    return resolution.ancestors if resolution is not None else None


def _blocked_plan(
    *,
    code: str,
    stage: str,
    manifest: CompileInputManifest | None = None,
    gaps: Sequence[str] = (),
    exclusions: Sequence[str] = (),
    scope_kind: str = "single_endpoint",
    endpoint_count: int = 1,
    hierarchy_resolutions: Sequence[HierarchyAncestorResolution] = (),
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
        hierarchy_resolutions=tuple(hierarchy_resolutions),
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
    hierarchy_resolution = resolve_source_graph_hierarchy_scope(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=normalized_signal,
    )
    if hierarchy_resolution is None:
        return _blocked_plan(
            code="hierarchy_scope_unresolved",
            stage="target_scope",
            manifest=manifest,
            gaps=(*gaps, "hierarchy_scope_unresolved"),
            exclusions=exclusions,
        )
    ancestors = hierarchy_resolution.ancestors
    if hierarchy_resolution.missing_instance_proved:
        return _blocked_plan(
            code="instance_not_in_projected_scope",
            stage="target_scope",
            manifest=manifest,
            gaps=(*gaps, "hierarchy_ancestor_chain_truncated"),
            exclusions=exclusions,
            hierarchy_resolutions=(hierarchy_resolution,),
        )

    target = ConnectivityTarget(
        operation=operation,
        signal_path=normalized_signal,
        instance_path_hint=ancestors[-1],
    )
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
        hierarchy_resolutions=(hierarchy_resolution,),
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=request,
        receipt=receipt,
    )


def resolve_source_graph_direct_children(
    *,
    hierarchy_result: Mapping[str, Any],
    top: str,
    instance_path: str,
) -> tuple[str, ...] | None:
    component_tree = hierarchy_result.get("component_tree")
    if not isinstance(component_tree, Mapping):
        return None
    children = component_tree.get(top)
    if not isinstance(children, Mapping):
        return None
    if instance_path != top:
        parts = instance_path.split(".")
        if not parts or parts[0] != top:
            return None
        for name in parts[1:]:
            node = children.get(name)
            if not isinstance(node, Mapping):
                return None
            nested = node.get("children")
            children = nested if isinstance(nested, Mapping) else {}
    candidates: list[str] = []
    for name, node in children.items():
        check_cancelled()
        if not isinstance(name, str) or not name or not isinstance(node, Mapping):
            continue
        candidate = f"{instance_path}.{name}"
        proved = resolve_source_graph_hierarchy_ancestors(
            hierarchy_result=hierarchy_result,
            top=top,
            signal_path=f"{candidate}.__traceweave_scope__",
        )
        if proved is not None and proved[-1] == candidate:
            candidates.append(candidate)
    return tuple(sorted(set(candidates)))


def build_source_graph_frontier_plan(
    *,
    compile_log: str,
    compile_result: Mapping[str, Any],
    hierarchy_result: Mapping[str, Any],
    hierarchy_snapshot_sha256: str,
    operation: QueryOperation | str,
    signal_path: str,
    frontier_signal_paths: Sequence[str],
    top_hint: str | None,
    max_hops: int,
    frontend_version: str,
    recursive: bool = False,
    include_expr: bool = True,
    kind_filter: Sequence[str] = (),
    max_instances: int = DEFAULT_SOURCE_GRAPH_FRONTIER_INSTANCE_LIMIT,
) -> SourceGraphBuildPlan:
    """Expand one single-endpoint artifact at proved dynamic frontiers.

    Each frontier is a signal segment produced by the first query, never a
    constant segment. Only direct children of its proved containing instance
    are admitted. This supports sibling producer / consumer discovery without
    a full-design walk or a name-based source guess.
    """

    if (
        not isinstance(max_instances, int)
        or isinstance(max_instances, bool)
        or max_instances < 1
    ):
        return _blocked_plan(
            code="frontier_instance_limit_invalid", stage="target_scope"
        )
    base = build_source_graph_plan(
        compile_log=compile_log,
        compile_result=compile_result,
        hierarchy_result=hierarchy_result,
        hierarchy_snapshot_sha256=hierarchy_snapshot_sha256,
        operation=operation,
        signal_path=signal_path,
        top_hint=top_hint,
        max_hops=max_hops,
        frontend_version=frontend_version,
        recursive=recursive,
        include_expr=include_expr,
        kind_filter=kind_filter,
    )
    if base.status is AdapterStatus.BLOCKED:
        return base
    assert base.request is not None
    request = base.request
    top = request.scope.top
    normalized_frontiers = tuple(
        dict.fromkeys(
            str(path).strip() for path in frontier_signal_paths if str(path).strip()
        )
    )
    if not normalized_frontiers:
        return _blocked_plan(
            code="frontier_signals_unavailable",
            stage="target_scope",
            manifest=request.identity.compile_inputs,
            gaps=base.receipt.gap_codes,
            exclusions=base.receipt.objective_exclusions,
        )

    parent_chains: list[tuple[str, ...]] = []
    candidate_paths: set[str] = set()
    for frontier in normalized_frontiers:
        check_cancelled()
        ancestors = resolve_source_graph_hierarchy_ancestors(
            hierarchy_result=hierarchy_result,
            top=top,
            signal_path=frontier,
        )
        if ancestors is None:
            return _blocked_plan(
                code="frontier_hierarchy_scope_unresolved",
                stage="target_scope",
                manifest=request.identity.compile_inputs,
                gaps=(*base.receipt.gap_codes, "frontier_hierarchy_scope_unresolved"),
                exclusions=base.receipt.objective_exclusions,
            )
        parent_chains.append(ancestors)
        children = resolve_source_graph_direct_children(
            hierarchy_result=hierarchy_result,
            top=top,
            instance_path=ancestors[-1],
        )
        if children is None:
            return _blocked_plan(
                code="frontier_children_unavailable",
                stage="target_scope",
                manifest=request.identity.compile_inputs,
                gaps=base.receipt.gap_codes,
                exclusions=base.receipt.objective_exclusions,
            )
        candidate_paths.update(children)
        if len(candidate_paths) > max_instances:
            return _blocked_plan(
                code="frontier_instance_limit",
                stage="target_scope",
                manifest=request.identity.compile_inputs,
                gaps=(*base.receipt.gap_codes, "frontier_instance_limit"),
                exclusions=base.receipt.objective_exclusions,
            )

    chains: list[tuple[str, ...]] = [request.scope.hierarchy_ancestors, *parent_chains]
    for candidate in sorted(candidate_paths):
        check_cancelled()
        ancestors = resolve_source_graph_hierarchy_ancestors(
            hierarchy_result=hierarchy_result,
            top=top,
            signal_path=f"{candidate}.__traceweave_scope__",
        )
        if ancestors is not None:
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
    expanded_request = replace(request, artifact=artifact)
    artifact_key = compute_source_graph_artifact_key(artifact)
    query_key = compute_source_graph_query_key(expanded_request.query_identity)
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=expanded_request,
        receipt=replace(
            base.receipt,
            ancestor_count=len(ancestor_union),
            requested_cone_instance_count=len(ancestor_union),
            coverage_boundary_instance_count=len(ancestor_union),
            scope_kind="single_endpoint_expanded",
            lca_depth=lca.count("."),
            cross_request_reusable=artifact_key.cross_request_reusable,
            artifact_fingerprint_sha256=artifact_key.digest,
            query_fingerprint_sha256=query_key.digest,
        ),
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
    hierarchy_resolutions: list[HierarchyAncestorResolution] = []
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
        hierarchy_resolution = resolve_source_graph_hierarchy_scope(
            hierarchy_result=hierarchy_result,
            top=top,
            signal_path=signal_path,
        )
        if hierarchy_resolution is None:
            return _blocked_plan(
                code="trace_hierarchy_scope_unresolved",
                stage="target_scope",
                manifest=manifest,
                gaps=(*base.receipt.gap_codes, "trace_hierarchy_scope_unresolved"),
                exclusions=base.receipt.objective_exclusions,
                scope_kind=scope_kind,
                endpoint_count=len(normalized_paths),
            )
        hierarchy_resolutions.append(hierarchy_resolution)
        if hierarchy_resolution.missing_instance_proved:
            return _blocked_plan(
                code="instance_not_in_projected_scope",
                stage="target_scope",
                manifest=manifest,
                gaps=(
                    *base.receipt.gap_codes,
                    "hierarchy_ancestor_chain_truncated",
                ),
                exclusions=base.receipt.objective_exclusions,
                scope_kind=scope_kind,
                endpoint_count=len(normalized_paths),
                hierarchy_resolutions=tuple(hierarchy_resolutions),
            )
        ancestors = hierarchy_resolution.ancestors
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
        hierarchy_resolutions=tuple(hierarchy_resolutions),
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

    from_resolution = resolve_source_graph_hierarchy_scope(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=normalized_from,
    )
    to_resolution = resolve_source_graph_hierarchy_scope(
        hierarchy_result=hierarchy_result,
        top=top,
        signal_path=normalized_to,
    )
    if from_resolution is None and to_resolution is None:
        code = "path_endpoint_hierarchy_unresolved"
    elif from_resolution is None:
        code = "path_from_hierarchy_unresolved"
    elif to_resolution is None:
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

    assert from_resolution is not None and to_resolution is not None
    hierarchy_resolutions = (from_resolution, to_resolution)
    if any(item.missing_instance_proved for item in hierarchy_resolutions):
        return _blocked_plan(
            code="instance_not_in_projected_scope",
            stage="target_scope",
            gaps=(*gaps, "hierarchy_ancestor_chain_truncated"),
            manifest=manifest,
            exclusions=exclusions,
            scope_kind=scope_kind,
            endpoint_count=endpoint_count,
            hierarchy_resolutions=hierarchy_resolutions,
        )
    from_ancestors = from_resolution.ancestors
    to_ancestors = to_resolution.ancestors
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
        hierarchy_resolutions=hierarchy_resolutions,
    )
    return SourceGraphBuildPlan(
        status=AdapterStatus.READY,
        request=request,
        receipt=receipt,
    )
