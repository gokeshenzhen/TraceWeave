"""Bounded compile-metadata observations for native-first queries.

These records never represent a source snapshot. Only parser reads (logs and
filelists), rather than incidental source path resolution, enter validation.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import time

from .cancellation import check_cancelled
from .compile_input_capture import (
    CaptureAborted, CaptureLimits, CompileInputCapture, bind_compile_input_capture, _availability,
    validate_captured_inputs,
)
from .compile_log_parser import detect_simulator, merge_compile_results, parse_compile_log
from .compile_session_snapshot import CompileSessionSnapshot
from .design_context_runtime import _ENV_REF


NATIVE_TOOLS = frozenset({
    "explain_signal_driver", "find_signal_loads", "trace_signal_path", "trace_x_source",
    "get_tb_subtree", "find_tb_instance", "lookup_tb_files", "get_tb_file_detail",
    "get_tb_class_hierarchy", "dump_tb_section", "resolve_packed_fields",
    "analyze_failure_event", "recommend_failure_debug_next_steps",
})
ROUTE_FIELDS = frozenset({"compile_log", "supplementary_compile_logs", "simulator",
    "top_hint", "kdb_path", "backend_preference", "retry_npi", "timeout_sec"})

NATIVE_IDENTITY_ERRORS = frozenset({
    "npi_loaded_generation_changed", "npi_scope_changed_during_query",
    "npi_compile_context_mismatch", "npi_scope_parent_mismatch",
    "npi_scan_position_changed",
    "npi_namespace_parent_mismatch", "npi_class_identity_conflict",
    "npi_lsf_result_invalid", "npi_lsf_protocol_mismatch", "npi_lsf_protocol_invalid",
})


_BUSINESS_FIELDS = {
    "explain_signal_driver": {"signal_path", "wave_path", "recursive", "max_depth", "include_dependencies", "allow_bounded_bootstrap"},
    "find_signal_loads": {"signal_path", "wave_path", "max_depth", "include_expr", "kind_filter", "allow_bounded_bootstrap"},
    "trace_signal_path": {"from_signal", "to_signal", "wave_path", "expand_assigns"},
    "trace_x_source": {"wave_path", "signal_path", "time_ps", "max_depth", "mode", "history_start_ps",
        "signal_bits", "phase", "max_nodes", "max_events", "max_read_bytes"},
    "resolve_packed_fields": {"wave_path", "signal_path", "source_signal", "fields"},
    "analyze_failure_event": {"wave_path", "log_path", "failure_event", "native_time_unit"},
    "recommend_failure_debug_next_steps": {"wave_path", "log_path", "native_time_unit"},
}


def validate_native_business(name, arguments):
    if name not in _BUSINESS_FIELDS:
        from .hierarchy_source_request import validate_source_query
        validate_source_query(name, {k: v for k, v in arguments.items() if k not in ROUTE_FIELDS})
        return
    if set(arguments) - ROUTE_FIELDS - _BUSINESS_FIELDS[name]:
        raise ValueError("unsupported native query arguments")
    for key in ("recursive", "include_expr", "include_dependencies", "expand_assigns", "allow_bounded_bootstrap"):
        if key in arguments and type(arguments[key]) is not bool:
            raise ValueError("query flags must be boolean")
    if "max_depth" in arguments and (type(arguments["max_depth"]) is not int or arguments["max_depth"] < 0):
        raise ValueError("max_depth must be a nonnegative integer")
    if name == "trace_x_source":
        mode = arguments.get("mode", "snapshot")
        if mode == "history":
            from .schemas import NativeXHistoryInput
            NativeXHistoryInput.model_validate({k: v for k, v in arguments.items()
                if k not in ROUTE_FIELDS or k in {"compile_log", "simulator", "top_hint", "timeout_sec"}})
        elif mode != "snapshot" or any(k in arguments for k in (
            "history_start_ps", "signal_bits", "phase", "max_nodes", "max_events", "max_read_bytes")):
            raise ValueError("history parameters require mode=history")


def native_budget_exhausted(result):
    for key in ("traversal", "enumeration"):
        work = result.get(key) or {}
        if (work.get("state_truncated") or work.get("output_truncated") or
                set(work.get("incomplete_reasons", ())) & {
                    "work_limit", "output_limit", "dynamic_expression_limit"}):
            return True
    return False


class NativeFallback(Exception):
    """Only an allowed operation fallback may cross the source boundary."""
    def __init__(self, attempts=(), *, side=None, budget=None):
        self.attempts = list(attempts)
        self.side, self.budget = side, budget
        self.failure_events_observation = None
        super().__init__("native_source_fallback")


class NativeInputChanged(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, timeout_sec=30.0, *, started=None):
        if (isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float))
                or not math.isfinite(timeout_sec) or not 0 < timeout_sec <= 300):
            raise ValueError("timeout_sec must be finite and in (0, 300]")
        self.started = time.monotonic() if started is None else started
        self.deadline = self.started + timeout_sec

    def remaining(self, cap=300):
        check_cancelled()
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("native_query_timeout")
        return min(cap, value)


class _MetadataCapture(CompileInputCapture):
    def __init__(self, limits):
        super().__init__(limits)
        self.reading = 0
        self.environment_names = set()

    def observe_path(self, path):
        if self.reading:
            return super().observe_path(path)
        self.checkpoint()
        # Source spellings are declarations, not source-current conditions.
        return os.path.abspath(path)

    def observe_metadata_path(self, path):
        self.reading += 1
        try:
            spelling = os.path.abspath(path)
            super().observe_path(spelling)
            if not os.path.isfile(spelling):
                if (spelling not in self._unavailable and
                        len(self._records) + len(self._unavailable) >= self.limits.max_files):
                    raise CaptureAborted("file_limit")
                self._unavailable[spelling] = _availability(spelling)
        finally:
            self.reading -= 1

    def read_bytes(self, path):
        self.reading += 1
        try:
            raw = super().read_bytes(path)
            for match in _ENV_REF.finditer(raw.decode("utf-8", errors="replace")):
                self.environment_names.add(match.group(1) or match.group(2))
            return raw
        finally:
            self.reading -= 1


@dataclass(frozen=True)
class NativeQueryInputs:
    ordered_compile_logs: tuple[str, ...]
    simulator: str
    selected_top: str | None
    backend_preference: str
    requested_kdb_path: str | None
    compile_result_json: str | None
    metadata_snapshot: CompileSessionSnapshot | None
    aliases: tuple[tuple[str, str], ...]
    unavailable: tuple[tuple[str, tuple], ...]
    environment: tuple[tuple[str, str | None], ...]
    compile_metadata_identity_sha256: str | None
    identity_complete: bool
    gaps: tuple[str, ...]
    metadata_read_bytes: int

    def compile_result(self):
        return json.loads(self.compile_result_json) if self.compile_result_json is not None else None

    def current(self, *, timeout_sec=30):
        check_cancelled()
        if any(os.environ.get(k) != v for k, v in self.environment):
            return False
        return self.metadata_snapshot is None or validate_captured_inputs(
            self.metadata_snapshot, self.aliases, self.unavailable, timeout_sec=timeout_sec)

    def require_current(self, *, timeout_sec=30):
        if not self.current(timeout_sec=timeout_sec):
            raise NativeInputChanged("compile_metadata_changed")

    @property
    def fallback_key(self):
        payload = (1, self.compile_metadata_identity_sha256, self.ordered_compile_logs,
                   self.simulator, self.selected_top, self.environment)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def metadata_identity(logs, simulator, top, environment, aliases, unavailable, snapshot):
    # Identity covers consumed parser inputs. Incidental source existence,
    # realpath and diagnostic observations are deliberately outside this key.
    payload = (2, logs, simulator, top, environment, aliases, unavailable,
               [(r.path, r.size, r.sha256) for r in snapshot.records])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def read_native_inputs(arguments, *, timeout_sec=30):
    preference = arguments.get("backend_preference", "auto")
    simulator = arguments.get("simulator", "auto")
    if preference not in {"auto", "source"} or simulator not in {"auto", "vcs", "xcelium", "verilator"}:
        raise ValueError("unsupported native query policy or simulator")
    if type(arguments.get("retry_npi", False)) is not bool:
        raise ValueError("retry_npi must be boolean")
    for key in ("compile_log", "top_hint", "kdb_path"):
        value = arguments.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or "\x00" in value):
            raise ValueError(key + " must be nonempty")
    supplementary = arguments.get("supplementary_compile_logs", [])
    if (not isinstance(supplementary, list) or len(supplementary) > 16 or
            any(not isinstance(p, str) or not p or "\x00" in p for p in supplementary)):
        raise ValueError("supplementary compile logs must be an ordered list of at most 16 paths")
    log, top, kdb = (arguments.get(k) for k in ("compile_log", "top_hint", "kdb_path"))
    kdb = os.path.abspath(kdb) if kdb else None
    if log is None:
        if not kdb or not top or supplementary:
            raise ValueError("provide compile_log, or kdb_path and top_hint")
        return NativeQueryInputs((), simulator, top, preference, kdb, None, None,
                                 (), (), (), None, False, (), 0)
    logs = tuple(os.path.abspath(p) for p in (log, *supplementary))
    if len(set(map(os.path.realpath, logs))) != len(logs):
        raise ValueError("duplicate compile log")
    before = dict(os.environ)
    with _MetadataCapture(CaptureLimits(timeout_sec=timeout_sec)) as capture, bind_compile_input_capture(capture):
        results = []
        for index, path in enumerate(logs):
            requested = simulator
            if index:
                detected = detect_simulator(path)
                requested = detected if detected in {"vcs", "xcelium", "verilator"} else results[0]["simulator"]
            parsed = parse_compile_log(path, requested)
            if results and parsed["simulator"] != results[0]["simulator"]:
                raise ValueError("compile logs use different simulators")
            results.append(parsed)
        result = (merge_compile_results(results[0], results[1:], primary_log=logs[0],
                  supplementary_logs=list(logs[1:])) if len(results) > 1 else results[0])
        tops = result.get("top_modules") or []
        if top is not None and tops and top not in tops:
            raise ValueError("top hint does not match compile evidence")
        top = top or (tops[0] if len(tops) == 1 else None)
        environment = tuple((k, before.get(k)) for k in sorted(capture.environment_names))
        if any(os.environ.get(k) != v for k, v in environment):
            raise NativeInputChanged("compile_metadata_changed")
        snapshot = capture.snapshot()
        gaps = tuple(sorted(set(snapshot.issue_codes) | (
            {"compile_metadata_incomplete"} if result.get("parse_warnings") else set())))
        # The parser may report existence/realpaths of declared sources. These
        # declarations are retained, but never re-observed during validation.
        identity = metadata_identity(logs, result["simulator"], top, environment, capture.aliases,
                                     capture.unavailable_observations, snapshot)
        return NativeQueryInputs(logs, result["simulator"], top, preference, kdb,
            json.dumps(result, sort_keys=True), snapshot, capture.aliases,
            capture.unavailable_observations, environment, identity, snapshot.complete, gaps,
            capture.read_bytes_count)


class NativeFailureMemory:
    """Small metadata-only LRU; capabilities and query errors are never sticky."""
    def __init__(self):
        self.entries = OrderedDict()

    def key(self, inputs, binding):
        if binding.anchor is None:
            return None
        raw = json.dumps((binding.anchor.identity_sha256, inputs.selected_top,
            asdict(binding.execution), asdict(binding.route), binding.runtime_environment,
            binding.allow_degraded), sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest() if len(raw.encode()) <= 65536 else None

    def get(self, key, *, retry=False):
        if retry:
            self.entries.pop(key, None)
        if key in self.entries:
            self.entries.move_to_end(key)
        return self.entries.get(key)

    def note(self, key, attempts):
        if key is None:
            return
        for attempt in attempts:
            if attempt.get("reason") in {"npi_load_failed", "npi_lsf_npi_unavailable"}:
                self.entries[key] = attempt["reason"]
                self.entries.move_to_end(key)
                while len(self.entries) > 32:
                    self.entries.popitem(last=False)
