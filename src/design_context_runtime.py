"""Independent compile preparation and bounded process-local context ownership.

Preparation reads compile evidence and preprocessor dependencies. It does not
enumerate HDL definitions/instances, build a hierarchy, run Slang, or probe/load
NPI. Frontend replay and KDB association remain separate validation steps.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import threading
import time
import uuid

from .cancellation import check_cancelled
from .compile_input_capture import (
    CaptureAborted, CaptureLimits, CompileInputCapture, bind_compile_input_capture,
)
from .compile_log_parser import detect_simulator, merge_compile_results, parse_compile_log
from .design_context import DesignContext, IncludeResolution
from .hdl_suffixes import VHDL_SOURCE_SUFFIXES
from .sv_preprocessor import SystemVerilogPreprocessor, extract_preprocessor_options


_SIMULATORS = {"vcs", "xcelium", "verilator"}
_ENV_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_PARTIAL_LIMITS = {"byte_limit", "file_limit", "reference_limit", "dependency_work_limit"}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _DependencyReader(SystemVerilogPreprocessor):
    def __init__(self, compile_result, capture, max_include_depth):
        self.capture = capture
        self.resolutions: dict[tuple[str, ...], IncludeResolution] = {}
        self.probe_count = 0
        self.work_bytes = 0
        self.visits = 0
        super().__init__(
            compile_result, source_loader=capture.read_text,
            max_include_depth=max_include_depth, source_cache_bytes=0,
            include_resolution_cache_entries=0,
        )

    def _canonical_path(self, path):
        # Re-observe every reference; a cached realpath must not conceal a
        # symlink replacement during capture.
        return self.capture.observe_path(path)

    def _resolve_include(self, *, parent, raw_name, include_dirs):
        self.capture.checkpoint()
        bases = (os.path.dirname(parent), *include_dirs)
        candidates = tuple(dict.fromkeys(
            [os.path.abspath(raw_name)] if os.path.isabs(raw_name)
            else [os.path.abspath(os.path.join(base, raw_name)) for base in bases]
        ))
        if candidates not in self.resolutions:
            self.probe_count += len(candidates)
            if self.probe_count > self.capture.limits.max_references:
                raise CaptureAborted("reference_limit")
        selected = spelling = None
        for candidate in candidates:
            self.capture.checkpoint()
            if os.path.isfile(candidate):
                spelling = candidate
                selected = self.capture.observe_path(candidate)
                break
        observation = IncludeResolution(candidates, selected, spelling)
        previous = self.resolutions.get(candidates)
        if previous is not None and previous != observation:
            raise CaptureAborted("input_changed")
        self.resolutions[candidates] = observation
        return selected

    def _expand_file(self, path, *, raw, include_dirs, state, depth, stack):
        self.capture.checkpoint()
        self.visits += 1
        if self.visits > self.capture.limits.max_references:
            raise CaptureAborted("dependency_work_limit")
        if depth <= self._max_include_depth:
            if raw is None:
                try:
                    raw = self.capture.read_text(path)
                except OSError:
                    state.issue("include_unreadable")
                    return ""
            # Bound logical preprocessing too, including repeated includes
            # served by the raw-byte capture. No expanded HDL is materialized.
            self.work_bytes += len(raw)
            if self.work_bytes > self.capture.limits.max_bytes:
                raise CaptureAborted("dependency_work_limit")
        result = super()._expand_file(
            path, raw=raw, include_dirs=include_dirs, state=state, depth=depth, stack=stack,
        )
        self.capture.checkpoint()
        return result


def _paths(compile_result):
    evidence = compile_result.get("compile_evidence") or {}
    units = evidence.get("ordered_compilation_units") or []
    sources = [str(item["path"]) for item in units if item.get("path")]
    # Includes and filtered simulator libraries are not interchangeable with
    # project roots. Preserve units in metadata and capture all reported input
    # bodies, including support inputs absent from files.user.
    source_set = set(sources)
    extra_sources = []
    for item in compile_result.get("files", {}).get("user", []):
        if item.get("path") and item["path"] not in source_set:
            extra_sources.append(str(item["path"]))
            source_set.add(item["path"])
    support = [str(item["path"]) for item in evidence.get("filelists", []) if item.get("path")]
    if sources:
        support.extend(extra_sources)
    else:
        sources = extra_sources
    for parent, children in (compile_result.get("include_tree") or {}).items():
        support.append(str(parent))
        support.extend(map(str, children))
    for item in evidence.get("ordered_includes", []):
        support.extend(str(item[key]) for key in ("parent", "path") if item.get(key))
    support.extend(map(str, compile_result.get("configuration_files", [])))
    return sources, list(dict.fromkeys(support)), units


def prepare_design_context(
    compile_log: str,
    *, supplementary_compile_logs=(), simulator: str = "auto",
    top_hint: str | None = None, backend_preference: str = "auto",
    limits: CaptureLimits | None = None, max_include_depth: int = 64,
) -> DesignContext:
    """Capture one exact compile observation without a full lexical tree.

    Missing source/support inputs and exhausted source budgets return explicit
    gaps. Log/option parsing must finish; timeout, cancellation and changing
    inputs always abort. A returned context is not a semantic cache key: its
    compile options still need backend-specific replay validation.
    """
    check_cancelled()
    if simulator not in {*_SIMULATORS, "auto"}:
        raise ValueError("unsupported simulator")
    if backend_preference not in {"auto", "source"}:
        raise ValueError("unsupported backend preference")
    if isinstance(supplementary_compile_logs, (str, bytes)) or len(supplementary_compile_logs) > 16:
        raise ValueError("supplementary compile logs must be an ordered sequence of at most 16 paths")
    if isinstance(max_include_depth, bool) or not isinstance(max_include_depth, int) or not 0 <= max_include_depth <= 64:
        raise ValueError("include depth must be between 0 and 64")
    if top_hint is not None and (not isinstance(top_hint, str) or not top_hint.strip()):
        raise ValueError("top hint must be nonempty")
    started = time.monotonic()
    environment_before = dict(os.environ)
    logs = tuple(os.path.abspath(path) for path in (compile_log, *supplementary_compile_logs))
    if len({os.path.realpath(path) for path in logs}) != len(logs):
        raise ValueError("duplicate compile log")
    gaps: set[str] = set()
    with CompileInputCapture(limits) as capture, bind_compile_input_capture(capture):
        results = []
        for index, path in enumerate(logs):
            capture.checkpoint()
            requested = simulator
            if index:
                detected = detect_simulator(path)
                requested = detected if detected in _SIMULATORS else results[0]["simulator"]
            result = parse_compile_log(path, requested)
            if results and result["simulator"] != results[0]["simulator"]:
                raise ValueError("compile logs use different simulators")
            results.append(result)
        compile_result = (
            merge_compile_results(results[0], results[1:], primary_log=logs[0], supplementary_logs=list(logs[1:]))
            if len(results) > 1 else results[0]
        )
        compile_result_json = _json(compile_result)
        parsed_ms = (time.monotonic() - started) * 1000
        sources, support, units = _paths(compile_result)
        evidence = compile_result.get("compile_evidence") or {}
        if not units or compile_result.get("parse_warnings"):
            gaps.add("compile_inputs_incomplete")
        tops = tuple(compile_result.get("top_modules") or ())
        if not sources and not tops and not compile_result.get("compile_command"):
            raise ValueError("compile evidence unavailable")
        selected_top = top_hint or compile_result.get("primary_top") or (tops[0] if len(tops) == 1 else None)
        if top_hint and tops and top_hint not in tops:
            raise ValueError("top hint does not match compile evidence")
        if selected_top is None or not tops:
            gaps.add("compile_tops_unresolved")
        phases = evidence.get("source_phases") or []
        if len(phases) > 1:
            # Native per-phase metadata remains intact. The small preprocessor
            # cannot establish compilation-unit macro lifetime across phases.
            gaps.add("phase_local_compile_options_unmodeled")
        reader = _DependencyReader(compile_result, capture, max_include_depth)
        dependencies_complete = True
        has_conditionals = False
        env_names: set[str] = set()
        for match in _ENV_REF.finditer(compile_result_json):
            env_names.add(match.group(1) or match.group(2))
        try:
            for path in dict.fromkeys([*support, *sources]):
                capture.checkpoint()
                try:
                    capture.read_bytes(path)
                except OSError:
                    gaps.add("compile_input_unavailable")
            for path in dict.fromkeys(sources):
                capture.checkpoint()
                if os.path.splitext(path)[1].lower() in VHDL_SOURCE_SUFFIXES:
                    gaps.add("opaque_vhdl_boundary")
                    dependencies_complete = False
                    continue
                options = extract_preprocessor_options(compile_result, path)
                if not options.complete:
                    gaps.add("compile_options_incomplete")
                    dependencies_complete = False
                try:
                    dependency = reader.collect_dependencies(path)
                except OSError:
                    gaps.add("compile_input_unavailable")
                    dependencies_complete = False
                    continue
                gaps.update(dependency.issues)
                dependencies_complete &= dependency.complete
                has_conditionals |= dependency.has_conditional_preprocessing
        except CaptureAborted as exc:
            if exc.code not in _PARTIAL_LIMITS:
                raise
            gaps.add("source_capture_" + exc.code)
            dependencies_complete = False
        if len(units) > 1 and has_conditionals:
            gaps.add("compile_unit_macro_state_unverified")
            dependencies_complete = False
        if len(phases) > 1:
            dependencies_complete = False
        # Filelist variables may not appear in the top-level command. Bind
        # them to the preparation environment, without storing unrelated env.
        captured_paths = {record.path for record in capture.records}
        for path in dict.fromkeys([*logs, *support]):
            canonical = os.path.realpath(path)
            if canonical in captured_paths:
                text = capture.read_text(path)
                for match in _ENV_REF.finditer(text):
                    env_names.add(match.group(1) or match.group(2))
        environment = tuple((name, environment_before.get(name)) for name in sorted(env_names))
        if any(os.environ.get(name) != value for name, value in environment):
            raise CaptureAborted("input_changed")
        snapshot = capture.snapshot()
        gaps.update(snapshot.issue_codes)
        for record in snapshot.records:
            if "protected_region" in record.objective_exclusions:
                gaps.add("protected_region")
                dependencies_complete = False
        dependencies_complete &= snapshot.complete
        resolutions = tuple(reader.resolutions.values())
        if not all(item.current(checkpoint=capture.checkpoint) for item in resolutions):
            raise CaptureAborted("input_changed")
        payload = {
            "version": 1, "compile_logs": logs, "compile_result": compile_result,
            "selected_top": selected_top,
            "content": [(r.path, r.size, r.sha256) for r in snapshot.records],
            "aliases": capture.aliases, "unavailable": capture.unavailable_observations,
            "includes": [(r.candidates, r.selected, r.selected_spelling) for r in resolutions],
            "environment": environment, "gaps": sorted(gaps),
        }
        identity = hashlib.sha256(_json(payload).encode()).hexdigest()
        metrics = (
            ("parse_ms", parsed_ms), ("prepare_ms", (time.monotonic() - started) * 1000),
            ("input_file_count", snapshot.file_count), ("captured_bytes", snapshot.total_bytes),
            ("read_bytes", capture.read_bytes_count), ("dependency_file_visits", reader.visits),
        )
        context = DesignContext(
            logs, results[0]["simulator"], selected_top, backend_preference,
            compile_result_json, snapshot, capture.aliases, capture.unavailable_observations,
            resolutions, environment, identity, tuple(sorted(gaps)),
            bool(dependencies_complete), metrics,
        )
        capture.checkpoint()
        if not context.metadata_current(checkpoint=capture.checkpoint):
            raise CaptureAborted("input_changed")
        return context


class ContextUnavailable(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _Entry:
    context: DesignContext
    size: int
    leases: int = 0


class DesignContextStore:
    """Bounded metadata registry. Busy contexts cannot be evicted or removed."""

    def __init__(self, max_entries: int = 8, max_bytes: int = 16 * 1024 * 1024):
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (max_entries, max_bytes)):
            raise ValueError("context store limits must be positive integers")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._generation = uuid.uuid4().hex
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def register(self, context: DesignContext) -> str:
        check_cancelled()
        if not context.current():
            raise ContextUnavailable("design_context_changed")
        size = context.estimated_bytes
        with self._lock:
            if size > self.max_bytes:
                raise ContextUnavailable("context_capacity")
            victims = []
            remaining_bytes, remaining_entries = self._bytes, len(self._entries)
            for handle, entry in self._entries.items():
                if remaining_entries < self.max_entries and remaining_bytes + size <= self.max_bytes:
                    break
                if not entry.leases:
                    victims.append(handle)
                    remaining_entries -= 1
                    remaining_bytes -= entry.size
            if remaining_entries >= self.max_entries or remaining_bytes + size > self.max_bytes:
                raise ContextUnavailable("context_capacity")
            check_cancelled()
            if not context.metadata_current():
                raise ContextUnavailable("design_context_changed")
            for victim in victims:
                self._bytes -= self._entries.pop(victim).size
            handle = "dc_" + self._generation + "_" + uuid.uuid4().hex
            self._entries[handle] = _Entry(context, size)
            self._bytes += size
            return handle

    @contextmanager
    def lease(self, handle: str):
        check_cancelled()
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None:
                raise ContextUnavailable("handle_expired")
            entry.leases += 1
            self._entries.move_to_end(handle)
        try:
            if not entry.context.current():
                raise ContextUnavailable("design_context_changed")
            check_cancelled()
            yield entry.context
        finally:
            with self._lock:
                entry.leases -= 1

    def remove(self, handle: str) -> bool:
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry.leases:
                return False
            self._bytes -= self._entries.pop(handle).size
            return True
