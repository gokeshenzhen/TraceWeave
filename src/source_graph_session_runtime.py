"""Bounded parent-side lifecycle for one persistent Source Graph frontend."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from typing import Any

from .source_graph_contract import (
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphBuildRequest,
    compute_source_graph_semantic_context_key,
)
from .source_graph_runtime import (
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    WorkerBuildResult,
    WorkerResourceMetrics,
    _terminate_process_group,
    _validated_timeout_seconds,
    _wait_for_atomic_response,
)


DEFAULT_SEMANTIC_SESSION_IDLE_TTL_SECONDS = 60.0
DEFAULT_SEMANTIC_SESSION_MAX_RSS_KIB = 768 * 1024
MIN_SEMANTIC_SESSION_IDLE_TTL_SECONDS = 0.01
MAX_SEMANTIC_SESSION_IDLE_TTL_SECONDS = 3_600.0
MIN_SEMANTIC_SESSION_MAX_RSS_KIB = 64 * 1024
MAX_SEMANTIC_SESSION_MAX_RSS_KIB = 8 * 1024 * 1024
_RSS_POLL_SECONDS = 0.05


def _process_rss_kib(pid: int) -> int | None:
    try:
        with Path(f"/proc/{pid}/status").open(encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


async def _wait_for_rss_limit(pid: int, max_rss_kib: int) -> int:
    while True:
        rss_kib = _process_rss_kib(pid)
        if rss_kib is not None and rss_kib > max_rss_kib:
            return rss_kib
        await asyncio.sleep(_RSS_POLL_SECONDS)


class PersistentSourceGraphProcessRunner:
    """Keep one exact semantic context in an isolated child process.

    Requests without a complete bounded semantic context retain the historical
    one-shot path.  A context change, timeout, cancellation, RSS breach,
    protocol error, or worker crash destroys the whole child before returning.
    """

    def __init__(
        self,
        *,
        python_executable: str | Path,
        worker_script: str | Path | None = None,
        working_directory: str | Path | None = None,
        staging_directory: str | Path | None = None,
        idle_ttl_seconds: float = DEFAULT_SEMANTIC_SESSION_IDLE_TTL_SECONDS,
        max_rss_kib: int = DEFAULT_SEMANTIC_SESSION_MAX_RSS_KIB,
        one_shot_runner: IsolatedSourceGraphProcessRunner | None = None,
    ) -> None:
        if not (
            MIN_SEMANTIC_SESSION_IDLE_TTL_SECONDS
            <= idle_ttl_seconds
            <= MAX_SEMANTIC_SESSION_IDLE_TTL_SECONDS
        ):
            raise ValueError("semantic session idle TTL is out of range")
        if (
            not isinstance(max_rss_kib, int)
            or isinstance(max_rss_kib, bool)
            or not MIN_SEMANTIC_SESSION_MAX_RSS_KIB
            <= max_rss_kib
            <= MAX_SEMANTIC_SESSION_MAX_RSS_KIB
        ):
            raise ValueError("semantic session RSS limit is out of range")
        self.python_executable = Path(python_executable)
        self.worker_script = (
            Path(worker_script)
            if worker_script is not None
            else Path(__file__).with_name("source_graph_session_worker.py")
        )
        self.working_directory = (
            Path(working_directory)
            if working_directory is not None
            else Path(__file__).resolve().parents[1]
        )
        self.staging_directory = (
            Path(staging_directory) if staging_directory is not None else None
        )
        self.idle_ttl_seconds = float(idle_ttl_seconds)
        self.max_rss_kib = max_rss_kib
        self._one_shot = one_shot_runner or IsolatedSourceGraphProcessRunner(
            python_executable=python_executable,
            working_directory=working_directory,
            staging_directory=staging_directory,
        )
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._temporary: Any | None = None
        self._directory: Path | None = None
        self._context_digest: str | None = None
        self._last_used = 0.0
        self._generation = 0
        self._idle_task: asyncio.Task[None] | None = None
        self._pending_restart_count = 0
        self._pending_eviction_count = 0

    @property
    def active(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def run(
        self,
        request: SourceGraphBuildRequest,
        *,
        timeout_seconds: float,
        cancel_event: asyncio.Event,
    ) -> WorkerBuildResult:
        timeout = _validated_timeout_seconds(timeout_seconds)
        context_key = compute_source_graph_semantic_context_key(
            request.artifact_identity
        )
        if not context_key.cross_request_reusable:
            return await self._one_shot.run(
                request,
                timeout_seconds=timeout,
                cancel_event=cancel_event,
            )
        if cancel_event.is_set():
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="admission",
            )

        async with self._lock:
            self._cancel_idle_locked()
            now = time.monotonic()
            if self.active and now - self._last_used >= self.idle_ttl_seconds:
                await self._terminate_locked()
                self._pending_eviction_count += 1
            if self.active and self._context_digest != context_key.digest:
                await self._terminate_locked()
                self._pending_restart_count += 1
            if not self.active:
                started = await self._start_locked(context_key.digest)
                if isinstance(started, WorkerBuildResult):
                    return started
                session_started = True
            else:
                session_started = False
            result = await self._run_locked(
                request,
                timeout_seconds=timeout,
                cancel_event=cancel_event,
                session_started=session_started,
            )
            if result.status is PrepareStatus.READY:
                self._last_used = time.monotonic()
                self._schedule_idle_locked()
            else:
                await self._terminate_locked()
            return result

    async def close(self) -> None:
        async with self._lock:
            self._cancel_idle_locked()
            await self._terminate_locked()

    async def _start_locked(
        self,
        context_digest: str,
    ) -> bool | WorkerBuildResult:
        parent = (
            os.fspath(self.staging_directory)
            if self.staging_directory is not None
            else None
        )
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix="traceweave-source-graph-session-",
                dir=parent,
            )
            directory = Path(temporary.name)
            os.chmod(directory, stat.S_IRWXU)
            process = await asyncio.create_subprocess_exec(
                os.fspath(self.python_executable),
                os.fspath(self.worker_script),
                "--directory",
                os.fspath(directory),
                cwd=os.fspath(self.working_directory),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            if "temporary" in locals():
                temporary.cleanup()
            return WorkerBuildResult.failed(
                PrepareStatus.DEPENDENCY_BLOCKED,
                code="frontend_python_unavailable",
                stage="worker_start",
                message=f"{type(exc).__name__}: {exc}",
            )
        self._temporary = temporary
        self._directory = directory
        self._process = process
        self._context_digest = context_digest
        self._generation += 1
        return True

    async def _run_locked(
        self,
        request: SourceGraphBuildRequest,
        *,
        timeout_seconds: float,
        cancel_event: asyncio.Event,
        session_started: bool,
    ) -> WorkerBuildResult:
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        assert self._directory is not None
        process = self._process
        token = secrets.token_hex(16)
        request_path = self._directory / f"{token}.request.json"
        response_path = self._directory / f"{token}.response.json"
        payload = {
            "protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
            "request": request.artifact_build_request.to_dict(),
        }
        request_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(request_path, stat.S_IRUSR | stat.S_IWUSR)
        started = time.perf_counter()
        rss_start = _process_rss_kib(process.pid)
        restart_count = self._pending_restart_count
        eviction_count = self._pending_eviction_count
        self._pending_restart_count = 0
        self._pending_eviction_count = 0
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel_event.wait())
        response_task = asyncio.create_task(_wait_for_atomic_response(response_path))
        rss_task = asyncio.create_task(
            _wait_for_rss_limit(process.pid, self.max_rss_kib)
        )
        try:
            command = json.dumps(
                {"command": "build", "token": token},
                sort_keys=True,
                separators=(",", ":"),
            )
            self._process.stdin.write((command + "\n").encode("utf-8"))
            await self._process.stdin.drain()
            try:
                done, _ = await asyncio.wait(
                    {wait_task, cancel_task, response_task, rss_task},
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                return await self._cancelled_result(
                    started=started,
                    rss_start=rss_start,
                    session_started=session_started,
                    restart_count=restart_count,
                    eviction_count=eviction_count,
                )
            if cancel_task in done and cancel_event.is_set():
                return await self._cancelled_result(
                    started=started,
                    rss_start=rss_start,
                    session_started=session_started,
                    restart_count=restart_count,
                    eviction_count=eviction_count,
                )
            if rss_task in done:
                observed_rss = rss_task.result()
                await self._terminate_locked()
                return WorkerBuildResult.failed(
                    PrepareStatus.BUILD_FAILED,
                    code="semantic_session_rss_limit",
                    stage="worker_process",
                    metrics=self._parent_metrics(
                        started=started,
                        rss_start=rss_start,
                        rss_end=observed_rss,
                        session_started=session_started,
                        restart_count=restart_count,
                        eviction_count=eviction_count + 1,
                    ),
                )
            if response_task not in done:
                if wait_task in done:
                    await self._terminate_locked()
                    return WorkerBuildResult.failed(
                        PrepareStatus.WORKER_CRASH,
                        code="worker_exit_failure",
                        stage="worker_process",
                        metrics=self._parent_metrics(
                            started=started,
                            rss_start=rss_start,
                            rss_end=None,
                            session_started=session_started,
                            restart_count=restart_count,
                            eviction_count=eviction_count,
                        ),
                    )
                await self._terminate_locked()
                return WorkerBuildResult.failed(
                    PrepareStatus.TIMED_OUT,
                    code="worker_timeout",
                    stage="worker_process",
                    metrics=self._parent_metrics(
                        started=started,
                        rss_start=rss_start,
                        rss_end=None,
                        session_started=session_started,
                        restart_count=restart_count,
                        eviction_count=eviction_count,
                    ),
                )
            try:
                acknowledgement = await asyncio.wait_for(
                    process.stdout.readline(), timeout=1.0
                )
                if acknowledgement.decode("ascii").strip() != token:
                    raise ValueError("semantic session acknowledgement mismatch")
                response = json.loads(response_path.read_text(encoding="utf-8"))
                result = IsolatedSourceGraphProcessRunner._decode_response(response)
            except (
                OSError,
                TimeoutError,
                UnicodeError,
                ValueError,
                TypeError,
                KeyError,
            ) as exc:
                await self._terminate_locked()
                return WorkerBuildResult.failed(
                    PrepareStatus.INVALID_RESPONSE,
                    code="worker_response_invalid",
                    stage="worker_response",
                    message=f"{type(exc).__name__}: {exc}",
                    metrics=self._parent_metrics(
                        started=started,
                        rss_start=rss_start,
                        rss_end=None,
                        session_started=session_started,
                        restart_count=restart_count,
                        eviction_count=eviction_count,
                    ),
                )
            reported_peak = result.metrics.rss_peak_kib
            if reported_peak is not None and reported_peak > self.max_rss_kib:
                await self._terminate_locked()
                return WorkerBuildResult.failed(
                    PrepareStatus.BUILD_FAILED,
                    code="semantic_session_rss_limit",
                    stage="worker_process",
                    metrics=self._parent_metrics(
                        started=started,
                        rss_start=rss_start,
                        rss_end=reported_peak,
                        session_started=session_started,
                        restart_count=restart_count,
                        eviction_count=eviction_count + 1,
                    ),
                )
            return replace(
                result,
                metrics=replace(
                    result.metrics,
                    wall_time_ms=(time.perf_counter() - started) * 1000.0,
                    frontend_launch_count=1 if session_started else 0,
                    semantic_session_hit_count=0 if session_started else 1,
                    semantic_session_miss_count=1 if session_started else 0,
                    semantic_session_restart_count=restart_count,
                    semantic_session_eviction_count=eviction_count,
                ),
            )
        except asyncio.CancelledError:
            return await self._cancelled_result(
                started=started,
                rss_start=rss_start,
                session_started=session_started,
                restart_count=restart_count,
                eviction_count=eviction_count,
            )
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            await self._terminate_locked()
            return WorkerBuildResult.failed(
                PrepareStatus.WORKER_CRASH,
                code="worker_exit_failure",
                stage="worker_process",
                message=f"{type(exc).__name__}: {exc}",
                metrics=self._parent_metrics(
                    started=started,
                    rss_start=rss_start,
                    rss_end=None,
                    session_started=session_started,
                    restart_count=restart_count,
                    eviction_count=eviction_count,
                ),
            )
        finally:
            for task in (wait_task, cancel_task, response_task, rss_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                wait_task,
                cancel_task,
                response_task,
                rss_task,
                return_exceptions=True,
            )
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)

    async def _cancelled_result(
        self,
        *,
        started: float,
        rss_start: int | None,
        session_started: bool,
        restart_count: int,
        eviction_count: int,
    ) -> WorkerBuildResult:
        cancel_started = time.perf_counter()
        await self._terminate_locked()
        return WorkerBuildResult.failed(
            PrepareStatus.CANCELLED,
            code="request_cancelled",
            stage="worker_process",
            metrics=replace(
                self._parent_metrics(
                    started=started,
                    rss_start=rss_start,
                    rss_end=None,
                    session_started=session_started,
                    restart_count=restart_count,
                    eviction_count=eviction_count,
                ),
                cancel_to_exit_ms=(time.perf_counter() - cancel_started) * 1000.0,
            ),
        )

    def _parent_metrics(
        self,
        *,
        started: float,
        rss_start: int | None,
        rss_end: int | None,
        session_started: bool,
        restart_count: int,
        eviction_count: int,
    ) -> WorkerResourceMetrics:
        return WorkerResourceMetrics(
            wall_time_ms=(time.perf_counter() - started) * 1000.0,
            rss_start_kib=rss_start,
            rss_peak_kib=rss_end,
            rss_end_kib=rss_end,
            frontend_launch_count=1 if session_started else 0,
            semantic_session_hit_count=0 if session_started else 1,
            semantic_session_miss_count=1 if session_started else 0,
            semantic_session_restart_count=restart_count,
            semantic_session_eviction_count=eviction_count,
        )

    def _cancel_idle_locked(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_idle_locked(self) -> None:
        self._cancel_idle_locked()
        generation = self._generation
        self._idle_task = asyncio.create_task(self._idle_reaper(generation))

    async def _idle_reaper(self, generation: int) -> None:
        try:
            await asyncio.sleep(self.idle_ttl_seconds)
            while True:
                async with self._lock:
                    if generation != self._generation or not self.active:
                        return
                    remaining = self.idle_ttl_seconds - (
                        time.monotonic() - self._last_used
                    )
                    if remaining <= 0:
                        await self._terminate_locked()
                        self._pending_eviction_count += 1
                        return
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

    async def _terminate_locked(self) -> None:
        self._cancel_idle_locked()
        process = self._process
        self._process = None
        self._context_digest = None
        self._generation += 1
        if process is not None:
            await _terminate_process_group(process)
        temporary = self._temporary
        self._temporary = None
        self._directory = None
        if temporary is not None:
            temporary.cleanup()
