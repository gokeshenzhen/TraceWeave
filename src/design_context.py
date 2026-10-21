"""Immutable compile/source observations independent of a hierarchy result.

These records describe preparation, not elaboration, semantic coverage or a
verified association with a KDB. They never own source bodies or frontend state.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
import json
import os
import time

from .cancellation import check_cancelled
from .compile_input_capture import CaptureAborted, CaptureLimits, _availability, validate_captured_inputs
from .compile_session_snapshot import CompileSessionSnapshot


@dataclass(frozen=True)
class IncludeResolution:
    candidates: tuple[str, ...]
    selected: str | None
    selected_spelling: str | None

    def current(self, *, checkpoint=check_cancelled) -> bool:
        for candidate in self.candidates:
            checkpoint()
            if os.path.isfile(candidate):
                return (candidate == self.selected_spelling
                        and os.path.realpath(candidate) == self.selected)
        return self.selected is None


@dataclass(frozen=True)
class DesignContext:
    ordered_compile_logs: tuple[str, ...]
    simulator: str
    selected_top: str | None
    backend_preference: str
    compile_result_json: str
    snapshot: CompileSessionSnapshot
    aliases: tuple[tuple[str, str], ...]
    unavailable: tuple[tuple[str, tuple], ...]
    include_resolutions: tuple[IncludeResolution, ...]
    environment: tuple[tuple[str, str | None], ...]
    identity_sha256: str
    gaps: tuple[str, ...]
    include_resolution_complete: bool
    metrics: tuple[tuple[str, int | float], ...]
    replay_environment: tuple[tuple[str, str], ...] = ()
    requested_kdb_path: str | None = None
    # Extra lookup observations made when verifying a historical source token.
    # They include readable candidates, unlike negative input observations.
    path_observations: tuple[tuple[str, tuple], ...] = ()

    @cached_property
    def semantic_ownership_index(self):
        """Captured metadata only; shared by queries, never a frontend load."""
        paths, aliases, unavailable = set(), {}, set()
        for record in self.snapshot.records:
            check_cancelled()
            paths.add(record.path)
        for spelling, canonical in self.aliases:
            check_cancelled()
            aliases[spelling] = canonical
        for spelling, _ in self.unavailable:
            check_cancelled()
            unavailable.add(aliases.get(spelling, spelling))
        return frozenset(paths), MappingProxyType(aliases), frozenset(unavailable)

    def compile_result(self) -> dict:
        """A mutable caller copy; nested metadata cannot alter this context."""
        return json.loads(self.compile_result_json)

    def metadata_current(self, *, checkpoint=check_cancelled) -> bool:
        checkpoint()
        for name, value in self.environment:
            checkpoint()
            if os.environ.get(name) != value:
                return False
        for spelling, canonical in self.aliases:
            checkpoint()
            if os.path.realpath(spelling) != canonical:
                return False
        for spelling, observation in self.unavailable:
            checkpoint()
            if _availability(spelling) != observation:
                return False
        for spelling, observation in self.path_observations:
            checkpoint()
            if _availability(spelling) != observation:
                return False
        for record in self.snapshot.records:
            checkpoint()
            if not record.current():
                return False
        return all(item.current(checkpoint=checkpoint) for item in self.include_resolutions)

    def current(self, *, timeout_sec: float = 30.0) -> bool:
        CaptureLimits(timeout_sec=timeout_sec)
        deadline = time.monotonic() + timeout_sec

        def checkpoint():
            check_cancelled()
            if time.monotonic() >= deadline:
                raise CaptureAborted("timeout")

        if not self.metadata_current(checkpoint=checkpoint):
            return False
        checkpoint()
        return (
            validate_captured_inputs(
                self.snapshot, self.aliases, self.unavailable,
                timeout_sec=deadline - time.monotonic(),
            )
            and self.metadata_current(checkpoint=checkpoint)
        )

    @property
    def estimated_bytes(self) -> int:
        # Conservative accounting of retained metadata; no source-text bytes.
        payload = (
            self.compile_result_json,
            tuple(r.identity_record() for r in self.snapshot.records),
            self.aliases, self.unavailable,
            tuple((r.candidates, r.selected, r.selected_spelling) for r in self.include_resolutions),
            self.environment, self.replay_environment, self.gaps, self.requested_kdb_path,
            self.path_observations,
        )
        return (4096 + 4 * len(json.dumps(payload, ensure_ascii=False).encode())
                + 256 * (len(self.snapshot.records) + len(self.aliases) + len(self.unavailable)))
