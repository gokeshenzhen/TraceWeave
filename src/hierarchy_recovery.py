"""Bounded, live-only sharing for explicit hierarchy context recovery.

No handles, session caches or workflow state are published here. The server
validates each result and is the only writer of the handle registry.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os

from .cancellation import check_cancelled
from .compile_session_snapshot import SourceContentLimitExceeded, read_source_content


class RecoveryBlocked(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RecoveryLimits:
    timeout_sec: float = 30.0
    max_source_files: int = 4096
    max_source_bytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        if (not math.isfinite(self.timeout_sec) or self.timeout_sec <= 0
                or self.max_source_files < 1 or self.max_source_bytes < 1):
            raise ValueError("invalid hierarchy recovery limits")


class BoundedSourceReader:
    """Count include reads as well as manifest files; retain no source text."""
    def __init__(self, limits: RecoveryLimits):
        self.limits = limits
        self.paths = set()
        self.bytes_read = 0

    def read(self, path):
        check_cancelled()
        canonical = os.path.realpath(path)
        if canonical not in self.paths and len(self.paths) >= self.limits.max_source_files:
            raise RecoveryBlocked("hierarchy_recovery_source_file_limit")
        remaining = self.limits.max_source_bytes - self.bytes_read
        if os.stat(canonical).st_size > remaining:
            raise RecoveryBlocked("hierarchy_recovery_source_byte_limit")
        try:
            content = read_source_content(canonical, max_bytes=remaining)
        except SourceContentLimitExceeded as exc:
            raise RecoveryBlocked("hierarchy_recovery_source_byte_limit") from exc
        self.paths.add(canonical)
        self.bytes_read += content.snapshot.size if content.snapshot else len(content.text.encode())
        return content


@dataclass
class _Flight:
    task: asyncio.Task
    waiters: int = 0


class HierarchyRecoveryRuntime:
    """One admitted build, at most four exact live flights, no persistent cache."""
    def __init__(self):
        self._flights = {}
        self._admission = asyncio.Semaphore(1)

    async def run(self, key, build, *, timeout_sec):
        flight = self._flights.get(key)
        coalesced = flight is not None
        if flight is None:
            if len(self._flights) >= 4:
                raise RecoveryBlocked("hierarchy_recovery_busy")

            async def execute():
                # The build's deadline starts once, including admission. Later
                # waiters cannot prolong work by joining the same live flight.
                async with asyncio.timeout(timeout_sec):
                    async with self._admission:
                        return await build()

            flight = _Flight(asyncio.create_task(execute()))
            self._flights[key] = flight
            flight.task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task), coalesced
        finally:
            flight.waiters -= 1
            if not flight.waiters:
                self._flights.pop(key, None)
                if not flight.task.done():
                    # The server's cancellable thread arms its cooperative token.
                    flight.task.cancel()
