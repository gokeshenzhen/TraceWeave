"""Process-session single-flight lifecycle for transient compile source text."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import threading
from typing import Any, Mapping, Sequence

from . import cancellation
from .compile_source_index import CompileSourceIndex


COMPILE_SOURCE_RUNTIME_VERSION = "1"
RuntimeKey = tuple[str, int, int]


def compile_source_index_key(
    *,
    compile_snapshot_sha256: str,
    compile_result: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    files = compile_result.get("files")
    user_files = files.get("user", ()) if isinstance(files, Mapping) else ()
    paths = tuple(
        dict.fromkeys(
            os.path.realpath(str(item.get("path")))
            for item in user_files
            if isinstance(item, Mapping) and item.get("path")
        )
    )
    payload = {
        "version": COMPILE_SOURCE_RUNTIME_VERSION,
        "compile_snapshot_sha256": compile_snapshot_sha256,
        "simulator": str(compile_result.get("simulator") or "auto"),
        "ordered_paths": list(paths),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return digest, paths


@dataclass
class _Flight:
    key: RuntimeKey
    paths: tuple[str, ...]
    max_bytes: int
    max_files: int
    cancel_event: threading.Event
    waiter_count: int = 1
    task: asyncio.Task[CompileSourceIndex] | None = None


@dataclass
class _Session:
    index: CompileSourceIndex
    holder_count: int = 0


class CompileSourceIndexLease:
    def __init__(
        self,
        runtime: "CompileSourceIndexRuntime",
        key: RuntimeKey,
        index: CompileSourceIndex,
        disposition: str,
    ) -> None:
        self._runtime = runtime
        self._key = key
        self.index = index
        self.disposition = disposition
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._runtime.release(self._key, self.index)


class CompileSourceIndexRuntime:
    """Coalesce exact concurrent source-index builds and drop final raw text."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[RuntimeKey, _Flight] = {}
        self._sessions: dict[RuntimeKey, _Session] = {}
        self._build_count = 0
        self._coalesced_waiter_count = 0
        self._cancelled_waiter_count = 0

    async def acquire(
        self,
        *,
        key: str,
        paths: Sequence[str],
        max_bytes: int,
        max_files: int,
        create_if_missing: bool = True,
    ) -> CompileSourceIndexLease | None:
        runtime_key = (key, int(max_bytes), int(max_files))
        with self._lock:
            session = self._sessions.get(runtime_key)
            if session is not None:
                session.holder_count += 1
                return CompileSourceIndexLease(
                    self,
                    runtime_key,
                    session.index,
                    "hit_active_session",
                )
            flight = self._flights.get(runtime_key)
            disposition = "miss_build"
            if flight is None:
                if not create_if_missing:
                    return None
                flight = _Flight(
                    key=runtime_key,
                    paths=tuple(paths),
                    max_bytes=max_bytes,
                    max_files=max_files,
                    cancel_event=threading.Event(),
                )
                self._flights[runtime_key] = flight
                self._build_count += 1
                flight.task = asyncio.create_task(self._execute_flight(flight))
            else:
                flight.waiter_count += 1
                self._coalesced_waiter_count += 1
                disposition = "coalesced"
        assert flight.task is not None
        try:
            index = await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            self._cancel_waiter(flight)
            raise
        except BaseException:
            self._fail_waiter(flight)
            raise
        with self._lock:
            if flight.waiter_count > 0:
                flight.waiter_count -= 1
            session = self._sessions.get(runtime_key)
            if session is None:
                session = _Session(index=index)
                self._sessions[runtime_key] = session
            session.holder_count += 1
            if flight.waiter_count == 0:
                if self._flights.get(runtime_key) is flight:
                    self._flights.pop(runtime_key, None)
        return CompileSourceIndexLease(
            self,
            runtime_key,
            index,
            disposition,
        )

    def _cancel_waiter(self, flight: _Flight) -> None:
        close_index = None
        with self._lock:
            if flight.waiter_count > 0:
                flight.waiter_count -= 1
            self._cancelled_waiter_count += 1
            if flight.waiter_count != 0:
                return
            if self._flights.get(flight.key) is flight:
                self._flights.pop(flight.key, None)
            session = self._sessions.get(flight.key)
            if session is None:
                flight.cancel_event.set()
                if flight.task is not None and flight.task.done():
                    try:
                        close_index = flight.task.result()
                    except BaseException:
                        close_index = None
            elif session.holder_count == 0:
                self._sessions.pop(flight.key, None)
                close_index = session.index
        if close_index is not None:
            close_index.close()

    def _fail_waiter(self, flight: _Flight) -> None:
        """Drop bookkeeping after an unexpected shared task failure."""

        with self._lock:
            if flight.waiter_count > 0:
                flight.waiter_count -= 1
            if flight.waiter_count == 0 and self._flights.get(flight.key) is flight:
                self._flights.pop(flight.key, None)

    async def _execute_flight(self, flight: _Flight) -> CompileSourceIndex:
        def build() -> CompileSourceIndex:
            token = cancellation.push_cancel_event(flight.cancel_event)
            try:
                index = CompileSourceIndex(
                    max_bytes=flight.max_bytes,
                    max_files=flight.max_files,
                )
                index.preload(flight.paths)
                return index
            except (OSError, MemoryError, cancellation.OperationCancelled):
                fallback = CompileSourceIndex(max_bytes=0, max_files=0)
                fallback.preload(())
                return fallback
            finally:
                cancellation.pop_cancel_event(token)

        index = await asyncio.to_thread(build)
        orphaned = False
        with self._lock:
            orphaned = self._flights.get(flight.key) is not flight
        if orphaned:
            index.close()
        return index

    async def release(self, key: RuntimeKey, index: CompileSourceIndex) -> None:
        close_index = False
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.index is not index:
                return
            if session.holder_count > 0:
                session.holder_count -= 1
            flight = self._flights.get(key)
            if session.holder_count == 0 and (
                flight is None or flight.waiter_count == 0
            ):
                self._sessions.pop(key, None)
                close_index = True
        if close_index:
            index.close()

    def metrics_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "compile_source_runtime_build_count": self._build_count,
                "compile_source_runtime_coalesced_waiter_count": (
                    self._coalesced_waiter_count
                ),
                "compile_source_runtime_cancelled_waiter_count": (
                    self._cancelled_waiter_count
                ),
                "compile_source_runtime_inflight_count": len(self._flights),
                "compile_source_runtime_active_session_count": len(
                    self._sessions
                ),
            }
