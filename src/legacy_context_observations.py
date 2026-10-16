"""Verify old hierarchy source tokens without constructing a lexical tree.

Historical tokens hash the files actually read by the old preprocessor,
including stat identity. They are not the independent context's content key
and do not prove a complete include closure or waveform/source association.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
import time

from .compile_input_capture import (
    CaptureAborted, CaptureLimits, CompileInputCapture, _availability,
    bind_compile_input_capture,
)
from .compile_session_snapshot import (
    CompileSessionSnapshot, CompileSessionSnapshotBuilder, _canonical_json,
)
from .design_context import DesignContext
from .hdl_suffixes import is_protected_systemverilog_path
from .hierarchy_recovery import RecoveryBlocked
from .sv_preprocessor import SystemVerilogPreprocessor


class _HistoricalDependencies(SystemVerilogPreprocessor):
    """Use the original include policy; bound logical work as well as I/O."""

    def __init__(self, compile_result, capture, loader):
        self.capture = capture
        self.observations: dict[str, tuple] = {}
        self.visits = self.work_chars = 0
        super().__init__(compile_result, source_loader=loader,
                         source_cache_bytes=0, include_resolution_cache_entries=0)

    def _canonical_path(self, path):
        return self.capture.observe_path(path)

    def _observe_lookup(self, path):
        self.capture.checkpoint()
        spelling = os.path.abspath(path)
        self.capture.observe_path(spelling)
        observation = _availability(spelling)
        previous = self.observations.get(spelling)
        if previous is not None and previous != observation:
            raise CaptureAborted("input_changed")
        if previous is None and len(self.observations) >= self.capture.limits.max_references:
            raise CaptureAborted("reference_limit")
        self.observations[spelling] = observation

    def _resolve_include(self, *, parent, raw_name, include_dirs):
        # Observe every candidate that the base policy can consider. The base
        # implementation retains ownership of its exact-log-first precedence,
        # including ambiguous basename handling. New preparation uses a
        # different search policy, so its observations alone are insufficient.
        candidates = [p for p in self._exact_includes.get(parent, ())
                      if os.path.basename(p) == os.path.basename(raw_name)]
        candidates.extend([raw_name] if os.path.isabs(raw_name) else
                          [os.path.join(base, raw_name)
                           for base in (os.path.dirname(parent), *include_dirs)])
        for candidate in candidates:
            self._observe_lookup(candidate)
        resolved = super()._resolve_include(parent=parent, raw_name=raw_name,
                                            include_dirs=include_dirs)
        for candidate in candidates:
            self._observe_lookup(candidate)
        return resolved

    def _expand_file(self, path, *, raw, include_dirs, state, depth, stack):
        self.capture.checkpoint()
        self.visits += 1
        if self.visits > self.capture.limits.max_references:
            raise CaptureAborted("dependency_work_limit")
        if depth <= self._max_include_depth:
            if raw is None:
                try:
                    raw = self._load(path)
                except OSError:
                    state.issue("include_unreadable")
                    return ""
            self.work_chars += len(raw)
            if self.work_chars > self.capture.limits.max_bytes:
                raise CaptureAborted("dependency_work_limit")
        result = super()._expand_file(path, raw=raw, include_dirs=include_dirs,
                                      state=state, depth=depth, stack=stack)
        self.capture.checkpoint()
        return result


def _merge_observations(first, second):
    merged = dict(first)
    for key, value in second:
        if key in merged and merged[key] != value:
            raise CaptureAborted("input_changed")
        merged[key] = value
    return tuple(sorted(merged.items()))


def verify_legacy_source_observations(
    context: DesignContext, expected_sha256: str, *, limits: CaptureLimits,
) -> DesignContext:
    """Return immutable current observations bound to the old source token.

    No expanded source, definitions, instances, scan results or native handles
    are retained. All old-only inputs and lookup observations remain part of
    subsequent ``context.current()`` checks. The composite identity is replayed
    through the old compile_context contract, not plain prepare_design_context.
    """
    started = time.monotonic()
    compile_result = context.compile_result()
    with CompileInputCapture(limits) as capture, bind_compile_input_capture(capture):
        builder = CompileSessionSnapshotBuilder(indexed_reader=capture.read_source_content)
        reader = _HistoricalDependencies(compile_result, capture, builder.read_text)
        gaps = set(context.gaps)
        dependencies_complete = True
        for entry in compile_result.get("files", {}).get("user", ()):
            capture.checkpoint()
            path = entry["path"]
            try:
                os.stat(path)
                if is_protected_systemverilog_path(path):
                    builder.read_text(path)
                else:
                    dependency = reader.collect_dependencies(path)
                    gaps.update(dependency.issues)
                    dependencies_complete &= dependency.complete
            except OSError:
                builder.mark_issue("compile_source_unavailable")
        historical = builder.finish()
        if (not historical.complete
                or historical.content_fingerprint_sha256 != expected_sha256):
            raise RecoveryBlocked("compile_context_changed")
        extra = capture.snapshot()
        records = context.snapshot.record_map()
        for record in extra.records:
            if record.path in records and records[record.path] != record:
                raise CaptureAborted("input_changed")
            records[record.path] = record
        if len(records) > limits.max_files:
            raise CaptureAborted("file_limit")
        if sum(record.size for record in records.values()) > limits.max_bytes:
            raise CaptureAborted("byte_limit")
        ordered = tuple(sorted(records.values(), key=lambda record: record.path))
        issues = tuple(sorted(set(context.snapshot.issue_codes) | set(extra.issue_codes)))
        snapshot = CompileSessionSnapshot(
            ordered, context.snapshot.complete and extra.complete, issues,
            hashlib.sha256(_canonical_json([r.identity_record() for r in ordered])).hexdigest(),
        )
        aliases = _merge_observations(context.aliases, capture.aliases)
        unavailable = _merge_observations(context.unavailable, capture.unavailable_observations)
        lookups = _merge_observations(context.path_observations, reader.observations.items())
        if max(len(aliases), len(unavailable), len(lookups)) > limits.max_references:
            raise CaptureAborted("reference_limit")
        identity = hashlib.sha256(_canonical_json({
            "version": 1, "prepared_context": context.identity_sha256,
            "historical_source": expected_sha256,
            "snapshot": snapshot.content_fingerprint_sha256,
            "aliases": aliases, "unavailable": unavailable, "lookups": lookups,
        })).hexdigest()
        updated = replace(
            context, snapshot=snapshot, aliases=aliases, unavailable=unavailable,
            path_observations=lookups, identity_sha256=identity,
            gaps=tuple(sorted(gaps | set(issues))),
            include_resolution_complete=context.include_resolution_complete and dependencies_complete,
            metrics=(*context.metrics,
                     ("legacy_source_files_read", historical.file_count),
                     ("legacy_source_bytes_read", capture.read_bytes_count),
                     ("legacy_dependency_file_visits", reader.visits),
                     ("legacy_dependency_work_chars", reader.work_chars),
                     ("legacy_verification_ms", (time.monotonic() - started) * 1000)),
        )
        if not updated.metadata_current(checkpoint=capture.checkpoint):
            raise CaptureAborted("input_changed")
        return updated
