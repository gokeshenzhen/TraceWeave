"""Internal scoped Source Graph preparation runtime.

The runtime owns an in-process session cache, same-key single-flight, and an
isolated child process runner for the optional source frontend. The production
connectivity router imports it through a lazy lifecycle owner; import/startup
does not create a runtime or build. It never selects or invokes the Legacy
Static backend and never acquires a waveform lock.
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import Enum
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Protocol

from .connectivity_ir import ConnectivityIR, CoverageStatus
from .connectivity_query import ConnectivityQueryEngine
from .source_graph_contract import (
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    ScopeRelation,
    ScopeReuseDecision,
    SourceGraphArtifactScopeReceipt,
    SourceGraphBuildKey,
    SourceGraphBuildRequest,
    SourceGraphScopeReceipt,
    compute_source_graph_build_key,
)
from .source_graph_disk_cache import (
    DiskArtifact,
    DiskCacheCancelled,
    DiskPublishOutcome,
    DiskValidationOutcome,
    SourceGraphDiskCache,
)


# One real cold build admission across every runtime object in this process.
# Acquisition is polled from the event loop, so waiting never blocks it and a
# cancelled request cannot strand a background lock-acquisition thread.
_PROCESS_COLD_BUILD_LOCK = threading.Lock()
DEFAULT_SOURCE_GRAPH_CACHE_MAX_ENTRIES = 8
DEFAULT_SOURCE_GRAPH_CACHE_MAX_BYTES = 512 * 1024 * 1024
MIN_SOURCE_GRAPH_TIMEOUT_SECONDS = 0.001
MAX_SOURCE_GRAPH_TIMEOUT_SECONDS = 86_400.0


class PrepareStatus(str, Enum):
    READY = "ready"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    BUILD_FAILED = "build_failed"
    WORKER_CRASH = "worker_crash"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INVALID_RESPONSE = "invalid_response"


class CacheDisposition(str, Enum):
    HIT_EXACT = "hit_exact"
    HIT_SUPERSET = "hit_superset"
    MISS = "miss"
    BYPASS_INCOMPLETE_KEY = "bypass_incomplete_key"
    BYPASS_CAPACITY = "bypass_capacity"


class FlightDisposition(str, Enum):
    NONE = "none"
    BUILDER = "builder"
    COALESCED = "coalesced"


class CacheTier(str, Enum):
    MEMORY = "memory"
    DISK = "disk"
    BUILD = "build"


class CacheLookupReason(str, Enum):
    EXACT_ARTIFACT = "exact_artifact"
    DOMINATING_ARTIFACT = "dominating_artifact"
    NO_CACHED_ARTIFACT = "no_cached_artifact"
    ARTIFACT_SEMANTICS_MISMATCH = "artifact_semantics_mismatch"
    CACHED_SCOPE_NOT_DOMINATING = "cached_scope_not_dominating"
    IDENTITY_NOT_REUSABLE = "identity_not_reusable"
    SAME_ARTIFACT_INFLIGHT = "same_artifact_inflight"
    ARTIFACT_EXCEEDS_CACHE_CAPACITY = "artifact_exceeds_cache_capacity"
    CANCELLED_BEFORE_LOOKUP = "cancelled_before_lookup"


def _fixed_label(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must not be empty")
    if not value.isascii() or not all(
        char.islower() or char.isdigit() or char == "_" for char in value
    ):
        raise ValueError(f"{label} must be a fixed lowercase label")
    return value


def _round_ms(value: float) -> float:
    return round(max(value, 0.0), 6)


def _validated_timeout_seconds(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be a finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be a finite number") from None
    if (
        not math.isfinite(timeout)
        or timeout < MIN_SOURCE_GRAPH_TIMEOUT_SECONDS
        or timeout > MAX_SOURCE_GRAPH_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "timeout_seconds must be between 0.001 and 86400 seconds"
        )
    return timeout


def _exact_flight_key(
    build_key: SourceGraphBuildKey,
    effective_timeout_sec: float,
) -> str:
    """Return an exact process-local preparation identity.

    The artifact digest covers every available source/snapshot/scope input,
    including explicit incompleteness flags.  Timeout affects worker behavior,
    so it participates in flight identity without entering persistent cache
    identity.
    """

    return f"{build_key.digest}:{effective_timeout_sec.hex()}"


@dataclass(frozen=True)
class InternalBuildBlocker:
    code: str
    stage: str
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _fixed_label(self.code, "blocker code"))
        object.__setattr__(self, "stage", _fixed_label(self.stage, "blocker stage"))

    def to_dict(self, *, include_message: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "stage": self.stage}
        if include_message and self.message:
            result["message"] = self.message
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InternalBuildBlocker:
        message = value.get("message")
        return cls(
            code=str(value["code"]),
            stage=str(value["stage"]),
            message=str(message) if message is not None else None,
        )


@dataclass(frozen=True)
class WorkerResourceMetrics:
    wall_time_ms: float = 0.0
    cpu_time_ms: float | None = None
    rss_start_kib: int | None = None
    rss_peak_kib: int | None = None
    rss_end_kib: int | None = None
    ir_bytes: int = 0
    cancel_to_exit_ms: float | None = None

    def __post_init__(self) -> None:
        numeric = (
            self.wall_time_ms,
            self.cpu_time_ms,
            self.rss_start_kib,
            self.rss_peak_kib,
            self.rss_end_kib,
            self.ir_bytes,
            self.cancel_to_exit_ms,
        )
        if any(value is not None and value < 0 for value in numeric):
            raise ValueError("worker metrics must not be negative")

    def to_dict(self) -> dict[str, int | float]:
        result: dict[str, int | float] = {
            "wall_time_ms": _round_ms(self.wall_time_ms),
            "ir_bytes": self.ir_bytes,
        }
        for name in (
            "cpu_time_ms",
            "rss_start_kib",
            "rss_peak_kib",
            "rss_end_kib",
            "cancel_to_exit_ms",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = _round_ms(value) if name.endswith("_ms") else value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkerResourceMetrics:
        def optional_float(name: str) -> float | None:
            item = value.get(name)
            return None if item is None else float(item)

        def optional_int(name: str) -> int | None:
            item = value.get(name)
            return None if item is None else int(item)

        return cls(
            wall_time_ms=float(value.get("wall_time_ms", 0.0)),
            cpu_time_ms=optional_float("cpu_time_ms"),
            rss_start_kib=optional_int("rss_start_kib"),
            rss_peak_kib=optional_int("rss_peak_kib"),
            rss_end_kib=optional_int("rss_end_kib"),
            ir_bytes=int(value.get("ir_bytes", 0)),
            cancel_to_exit_ms=optional_float("cancel_to_exit_ms"),
        )


@dataclass(frozen=True)
class WorkerBuildResult:
    status: PrepareStatus
    ir_json_bytes: bytes | None = None
    ir_fingerprint_sha256: str | None = None
    scope_receipt: SourceGraphScopeReceipt | SourceGraphArtifactScopeReceipt | None = (
        None
    )
    blocker: InternalBuildBlocker | None = None
    metrics: WorkerResourceMetrics = field(default_factory=WorkerResourceMetrics)
    projection_receipt: Mapping[str, Any] | None = None
    fallback_used: bool = False

    def __post_init__(self) -> None:
        status = PrepareStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.fallback_used:
            raise ValueError("Source Graph worker results must never use fallback")
        if status is PrepareStatus.READY:
            if (
                self.ir_json_bytes is None
                or self.ir_fingerprint_sha256 is None
                or self.scope_receipt is None
            ):
                raise ValueError("ready worker result requires IR and scope receipt")
            if self.blocker is not None:
                raise ValueError("ready worker result must not carry a blocker")
        elif self.blocker is None:
            raise ValueError("non-ready worker result requires a blocker")

    @classmethod
    def ready(
        cls,
        ir: ConnectivityIR,
        scope_receipt: SourceGraphScopeReceipt | SourceGraphArtifactScopeReceipt,
        *,
        metrics: WorkerResourceMetrics | None = None,
        projection_receipt: Mapping[str, Any] | None = None,
    ) -> WorkerBuildResult:
        payload = ir.to_json_bytes()
        return cls(
            status=PrepareStatus.READY,
            ir_json_bytes=payload,
            ir_fingerprint_sha256=ir.fingerprint_sha256(),
            scope_receipt=scope_receipt,
            metrics=metrics or WorkerResourceMetrics(ir_bytes=len(payload)),
            projection_receipt=projection_receipt,
        )

    @classmethod
    def failed(
        cls,
        status: PrepareStatus,
        *,
        code: str,
        stage: str,
        message: str | None = None,
        metrics: WorkerResourceMetrics | None = None,
    ) -> WorkerBuildResult:
        if status is PrepareStatus.READY:
            raise ValueError("failed worker result cannot have ready status")
        return cls(
            status=status,
            blocker=InternalBuildBlocker(code=code, stage=stage, message=message),
            metrics=metrics or WorkerResourceMetrics(),
        )


class SourceGraphWorkerRunner(Protocol):
    async def run(
        self,
        request: SourceGraphBuildRequest,
        *,
        timeout_seconds: float,
        cancel_event: asyncio.Event,
    ) -> WorkerBuildResult: ...


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()


async def _wait_for_atomic_response(path: Path) -> None:
    """Wait until the worker's atomic response rename becomes observable."""

    while not path.is_file():
        await asyncio.sleep(0.05)


class IsolatedSourceGraphProcessRunner:
    """Launch one frontend build in a fresh child interpreter."""

    def __init__(
        self,
        *,
        python_executable: str | Path = sys.executable,
        worker_script: str | Path | None = None,
        working_directory: str | Path | None = None,
        staging_directory: str | Path | None = None,
    ) -> None:
        self.python_executable = Path(python_executable)
        self.worker_script = (
            Path(worker_script)
            if worker_script
            else Path(__file__).with_name("source_graph_worker.py")
        )
        self.working_directory = (
            Path(working_directory)
            if working_directory is not None
            else Path(__file__).resolve().parents[1]
        )
        self.staging_directory = (
            Path(staging_directory) if staging_directory is not None else None
        )

    async def run(
        self,
        request: SourceGraphBuildRequest,
        *,
        timeout_seconds: float,
        cancel_event: asyncio.Event,
    ) -> WorkerBuildResult:
        timeout_seconds = _validated_timeout_seconds(timeout_seconds)
        if cancel_event.is_set():
            return WorkerBuildResult.failed(
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="admission",
            )

        started = time.perf_counter()
        temp_parent = (
            str(self.staging_directory) if self.staging_directory is not None else None
        )
        try:
            temp = tempfile.TemporaryDirectory(
                prefix="traceweave-source-graph-", dir=temp_parent
            )
        except OSError as exc:
            return WorkerBuildResult.failed(
                PrepareStatus.DEPENDENCY_BLOCKED,
                code="worker_staging_unavailable",
                stage="worker_setup",
                message=f"{type(exc).__name__}: {exc}",
            )

        try:
            temp_path = Path(temp.name)
            os.chmod(temp_path, stat.S_IRWXU)
            request_path = temp_path / "request.json"
            response_path = temp_path / "response.json"
            request_payload = {
                "protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
                "request": request.artifact_build_request.to_dict(),
            }
            request_path.write_text(
                json.dumps(request_payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(request_path, stat.S_IRUSR | stat.S_IWUSR)
            command = [
                str(self.python_executable),
                str(self.worker_script),
                "--request",
                str(request_path),
                "--response",
                str(response_path),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.working_directory),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                return WorkerBuildResult.failed(
                    PrepareStatus.DEPENDENCY_BLOCKED,
                    code="frontend_python_unavailable",
                    stage="worker_start",
                    message=f"{type(exc).__name__}: {exc}",
                    metrics=WorkerResourceMetrics(
                        wall_time_ms=(time.perf_counter() - started) * 1000
                    ),
                )

            wait_task = asyncio.create_task(process.wait())
            cancel_task = asyncio.create_task(cancel_event.wait())
            response_task = asyncio.create_task(
                _wait_for_atomic_response(response_path)
            )
            response_completed_before_exit = False
            try:
                try:
                    done, _ = await asyncio.wait(
                        {wait_task, cancel_task, response_task},
                        timeout=timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    cancel_started = time.perf_counter()
                    await asyncio.shield(_terminate_process_group(process))
                    cancel_to_exit_ms = (time.perf_counter() - cancel_started) * 1000
                    return WorkerBuildResult.failed(
                        PrepareStatus.CANCELLED,
                        code="request_cancelled",
                        stage="worker_process",
                        metrics=WorkerResourceMetrics(
                            wall_time_ms=(time.perf_counter() - started) * 1000,
                            cancel_to_exit_ms=cancel_to_exit_ms,
                        ),
                    )
                if cancel_task in done and cancel_event.is_set():
                    cancel_started = time.perf_counter()
                    await _terminate_process_group(process)
                    cancel_to_exit_ms = (time.perf_counter() - cancel_started) * 1000
                    return WorkerBuildResult.failed(
                        PrepareStatus.CANCELLED,
                        code="request_cancelled",
                        stage="worker_process",
                        metrics=WorkerResourceMetrics(
                            wall_time_ms=(time.perf_counter() - started) * 1000,
                            cancel_to_exit_ms=cancel_to_exit_ms,
                        ),
                    )
                response_ready = response_task in done or response_path.is_file()
                if response_ready and wait_task not in done:
                    # The worker publishes only after projection, serialization,
                    # fingerprinting, and fsync, using an atomic rename.  Treat
                    # that file as a second completion signal.  On very large
                    # Slang builds the interpreter can finish publishing while
                    # its asyncio child-exit notification is delayed; waiting
                    # solely on process.wait() would misreport a successful
                    # build as a timeout.  Give normal interpreter shutdown a
                    # short grace period, keep cancellation authoritative, then
                    # reap a lingering process group before decoding.
                    grace_done, _ = await asyncio.wait(
                        {wait_task, cancel_task},
                        timeout=1.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_task in grace_done and cancel_event.is_set():
                        cancel_started = time.perf_counter()
                        await _terminate_process_group(process)
                        cancel_to_exit_ms = (
                            time.perf_counter() - cancel_started
                        ) * 1000
                        return WorkerBuildResult.failed(
                            PrepareStatus.CANCELLED,
                            code="request_cancelled",
                            stage="worker_process",
                            metrics=WorkerResourceMetrics(
                                wall_time_ms=(time.perf_counter() - started) * 1000,
                                cancel_to_exit_ms=cancel_to_exit_ms,
                            ),
                        )
                    if wait_task not in grace_done:
                        await _terminate_process_group(process)
                        response_completed_before_exit = True
                elif wait_task not in done:
                    cancel_started = time.perf_counter()
                    await _terminate_process_group(process)
                    cancel_to_exit_ms = (time.perf_counter() - cancel_started) * 1000
                    return WorkerBuildResult.failed(
                        PrepareStatus.TIMED_OUT,
                        code="worker_timeout",
                        stage="worker_process",
                        metrics=WorkerResourceMetrics(
                            wall_time_ms=(time.perf_counter() - started) * 1000,
                            cancel_to_exit_ms=cancel_to_exit_ms,
                        ),
                    )
            finally:
                for task in (wait_task, cancel_task, response_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    wait_task,
                    cancel_task,
                    response_task,
                    return_exceptions=True,
                )

            elapsed_ms = (time.perf_counter() - started) * 1000
            if process.returncode != 0 and not response_path.is_file():
                return WorkerBuildResult.failed(
                    PrepareStatus.WORKER_CRASH,
                    code="worker_exit_failure",
                    stage="worker_process",
                    message=f"worker exited with status {process.returncode}",
                    metrics=WorkerResourceMetrics(wall_time_ms=elapsed_ms),
                )
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
                result = self._decode_response(payload)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                json.JSONDecodeError,
            ) as exc:
                return WorkerBuildResult.failed(
                    PrepareStatus.INVALID_RESPONSE,
                    code="worker_response_invalid",
                    stage="worker_response",
                    message=f"{type(exc).__name__}: {exc}",
                    metrics=WorkerResourceMetrics(wall_time_ms=elapsed_ms),
                )
            if (
                process.returncode != 0
                and result.status is PrepareStatus.READY
                and not response_completed_before_exit
            ):
                return WorkerBuildResult.failed(
                    PrepareStatus.INVALID_RESPONSE,
                    code="worker_exit_status_invalid",
                    stage="worker_response",
                    message=(
                        "worker returned a ready response with nonzero exit status "
                        f"{process.returncode}"
                    ),
                    metrics=WorkerResourceMetrics(wall_time_ms=elapsed_ms),
                )
            metrics = replace(result.metrics, wall_time_ms=elapsed_ms)
            return replace(result, metrics=metrics)
        finally:
            temp.cleanup()

    @staticmethod
    def _decode_response(payload: Mapping[str, Any]) -> WorkerBuildResult:
        if payload.get("protocol_version") != SOURCE_GRAPH_WORKER_PROTOCOL_VERSION:
            raise ValueError("worker protocol version mismatch")
        if payload.get("fallback_used") is not False:
            raise ValueError("worker response lacks the no-fallback receipt")
        status = PrepareStatus(payload["status"])
        metrics_value = payload.get("metrics") or {}
        if not isinstance(metrics_value, Mapping):
            raise ValueError("worker metrics must be an object")
        metrics = WorkerResourceMetrics.from_dict(metrics_value)
        if status is PrepareStatus.READY:
            encoded = payload.get("ir_json_base64")
            receipt_value = payload.get("scope_receipt")
            if not isinstance(encoded, str) or not isinstance(receipt_value, Mapping):
                raise ValueError("ready response lacks IR or scope receipt")
            ir_bytes = base64.b64decode(encoded, validate=True)
            return WorkerBuildResult(
                status=status,
                ir_json_bytes=ir_bytes,
                ir_fingerprint_sha256=str(payload["ir_fingerprint_sha256"]),
                scope_receipt=SourceGraphArtifactScopeReceipt.from_dict(receipt_value),
                metrics=replace(metrics, ir_bytes=len(ir_bytes)),
                projection_receipt=(
                    payload.get("projection_receipt")
                    if isinstance(payload.get("projection_receipt"), Mapping)
                    else None
                ),
            )
        blocker_value = payload.get("blocker")
        if not isinstance(blocker_value, Mapping):
            raise ValueError("failed response lacks blocker")
        return WorkerBuildResult(
            status=status,
            blocker=InternalBuildBlocker.from_dict(blocker_value),
            metrics=metrics,
        )


@dataclass(frozen=True)
class SourceGraphCacheEntry:
    build_key: SourceGraphBuildKey
    scope_receipt: SourceGraphScopeReceipt | SourceGraphArtifactScopeReceipt
    artifact_scope_receipt: SourceGraphArtifactScopeReceipt
    ir: ConnectivityIR
    query_engine: ConnectivityQueryEngine
    ir_json_bytes: bytes
    ir_fingerprint_sha256: str
    ir_bytes: int
    cache_bytes: int

    @property
    def coverage_status(self) -> CoverageStatus:
        return self.artifact_scope_receipt.coverage_status


@dataclass(frozen=True)
class SourceGraphPrepareMetrics:
    cache_disposition: CacheDisposition
    flight_disposition: FlightDisposition
    total_wall_ms: float
    cache_tier: CacheTier = CacheTier.BUILD
    disk_validation_outcome: str = "disabled"
    admission_wait_ms: float = 0.0
    build_wall_ms: float = 0.0
    load_wall_ms: float = 0.0
    actual_build_count: int = 0
    coalesced_waiter_count: int = 0
    cancel_to_exit_ms: float | None = None
    worker_cpu_ms: float | None = None
    rss_start_kib: int | None = None
    rss_peak_kib: int | None = None
    rss_end_kib: int | None = None
    ir_bytes: int = 0
    cache_bytes: int = 0
    cache_entry_count: int = 0
    cache_peak_entry_count: int = 0
    cache_peak_bytes: int = 0
    cache_eviction_count: int = 0
    cache_oversize_bypass_count: int = 0
    frontend_launch_count: int = 0
    disk_lookup_wall_ms: float = 0.0
    disk_read_wall_ms: float = 0.0
    disk_validate_wall_ms: float = 0.0
    disk_publish_wall_ms: float = 0.0
    disk_write_wall_ms: float = 0.0
    disk_eviction_wall_ms: float = 0.0
    disk_hit_count: int = 0
    disk_miss_count: int = 0
    disk_corrupt_count: int = 0
    disk_build_skip_count: int = 0
    disk_bytes_read: int = 0
    disk_bytes_written: int = 0
    disk_entry_count: int = 0
    disk_bytes: int = 0
    disk_eviction_count: int = 0

    def to_dict(self) -> dict[str, int | float | str]:
        result: dict[str, int | float | str] = {
            "cache_disposition": self.cache_disposition.value,
            "flight_disposition": self.flight_disposition.value,
            "cache_tier": self.cache_tier.value,
            "disk_validation_outcome": self.disk_validation_outcome,
            "total_wall_ms": _round_ms(self.total_wall_ms),
            "admission_wait_ms": _round_ms(self.admission_wait_ms),
            "build_wall_ms": _round_ms(self.build_wall_ms),
            "load_wall_ms": _round_ms(self.load_wall_ms),
            "actual_build_count": self.actual_build_count,
            "coalesced_waiter_count": self.coalesced_waiter_count,
            "ir_bytes": self.ir_bytes,
            "cache_bytes": self.cache_bytes,
            "cache_entry_count": self.cache_entry_count,
            "cache_peak_entry_count": self.cache_peak_entry_count,
            "cache_peak_bytes": self.cache_peak_bytes,
            "cache_eviction_count": self.cache_eviction_count,
            "cache_oversize_bypass_count": self.cache_oversize_bypass_count,
            "frontend_launch_count": self.frontend_launch_count,
            "disk_lookup_wall_ms": _round_ms(self.disk_lookup_wall_ms),
            "disk_read_wall_ms": _round_ms(self.disk_read_wall_ms),
            "disk_validate_wall_ms": _round_ms(self.disk_validate_wall_ms),
            "disk_publish_wall_ms": _round_ms(self.disk_publish_wall_ms),
            "disk_write_wall_ms": _round_ms(self.disk_write_wall_ms),
            "disk_eviction_wall_ms": _round_ms(self.disk_eviction_wall_ms),
            "disk_hit_count": self.disk_hit_count,
            "disk_miss_count": self.disk_miss_count,
            "disk_corrupt_count": self.disk_corrupt_count,
            "disk_build_skip_count": self.disk_build_skip_count,
            "disk_bytes_read": self.disk_bytes_read,
            "disk_bytes_written": self.disk_bytes_written,
            "disk_entry_count": self.disk_entry_count,
            "disk_bytes": self.disk_bytes,
            "disk_eviction_count": self.disk_eviction_count,
        }
        for name in (
            "cancel_to_exit_ms",
            "worker_cpu_ms",
            "rss_start_kib",
            "rss_peak_kib",
            "rss_end_kib",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = _round_ms(value) if name.endswith("_ms") else value
        return result


@dataclass(frozen=True)
class SourceGraphPrepareOutcome:
    status: PrepareStatus
    build_key: SourceGraphBuildKey
    metrics: SourceGraphPrepareMetrics
    cache_lookup_reason: CacheLookupReason
    effective_timeout_sec: float
    entry: SourceGraphCacheEntry | None = None
    blocker: InternalBuildBlocker | None = None
    scope_match: ScopeReuseDecision | None = None
    fallback_used: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effective_timeout_sec",
            _validated_timeout_seconds(self.effective_timeout_sec),
        )
        if self.fallback_used:
            raise ValueError("Source Graph prepare must never use fallback")
        if self.status is PrepareStatus.READY and self.entry is None:
            raise ValueError("ready prepare outcome requires a cache entry")
        if self.status is not PrepareStatus.READY and self.blocker is None:
            raise ValueError("failed prepare outcome requires a blocker")

    @property
    def coverage_status(self) -> CoverageStatus | None:
        return self.entry.coverage_status if self.entry is not None else None

    def to_receipt(self, *, include_blocker_message: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "build_key": self.build_key.to_dict(),
            "cache_lookup_reason": self.cache_lookup_reason.value,
            "effective_timeout_sec": self.effective_timeout_sec,
            "fallback_used": False,
            "metrics": self.metrics.to_dict(),
        }
        if self.entry is not None:
            result["ir"] = {
                "fingerprint_sha256": self.entry.ir_fingerprint_sha256,
                "ir_bytes": self.entry.ir_bytes,
                "cache_bytes": self.entry.cache_bytes,
            }
            result["coverage"] = {
                "status": self.entry.coverage_status.value,
                "scope_receipt": self.entry.artifact_scope_receipt.to_dict(),
            }
        if self.scope_match is not None:
            result["scope_match"] = {
                "relation": self.scope_match.relation.value,
                "reusable": self.scope_match.reusable,
                "complete_for_request": self.scope_match.complete_for_request,
                "reason": self.scope_match.reason,
            }
        if self.blocker is not None:
            result["blocker"] = self.blocker.to_dict(
                include_message=include_blocker_message
            )
        return result


@dataclass
class _Flight:
    flight_key: str
    request: SourceGraphBuildRequest
    build_key: SourceGraphBuildKey
    cache_disposition: CacheDisposition
    cache_lookup_reason: CacheLookupReason
    effective_timeout_sec: float
    worker_cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[SourceGraphPrepareOutcome] | None = None
    waiter_count: int = 0
    coalesced_waiter_count: int = 0
    actual_build_count: int = 0
    cancel_requested_at: float | None = None
    cache_tier: CacheTier = CacheTier.BUILD
    disk_validation_outcome: str = "disabled"
    disk_lookup_wall_ms: float = 0.0
    disk_read_wall_ms: float = 0.0
    disk_validate_wall_ms: float = 0.0
    disk_publish_wall_ms: float = 0.0
    disk_write_wall_ms: float = 0.0
    disk_eviction_wall_ms: float = 0.0
    disk_hit_count: int = 0
    disk_miss_count: int = 0
    disk_corrupt_count: int = 0
    disk_build_skip_count: int = 0
    disk_bytes_read: int = 0
    disk_bytes_written: int = 0
    disk_entry_count: int = 0
    disk_bytes: int = 0
    disk_eviction_count: int = 0


class SourceGraphRuntime:
    """Process-session Source Graph cache and cold-build coordinator."""

    def __init__(
        self,
        worker_runner: SourceGraphWorkerRunner,
        *,
        max_cache_entries: int = DEFAULT_SOURCE_GRAPH_CACHE_MAX_ENTRIES,
        max_cache_bytes: int = DEFAULT_SOURCE_GRAPH_CACHE_MAX_BYTES,
        disk_cache: SourceGraphDiskCache | None = None,
    ) -> None:
        if (
            not isinstance(max_cache_entries, int)
            or isinstance(max_cache_entries, bool)
            or max_cache_entries < 1
        ):
            raise ValueError("max_cache_entries must be a positive integer")
        if (
            not isinstance(max_cache_bytes, int)
            or isinstance(max_cache_bytes, bool)
            or max_cache_bytes < 1
        ):
            raise ValueError("max_cache_bytes must be a positive integer")
        self._worker_runner = worker_runner
        self._disk_cache = disk_cache
        self._max_cache_entries = max_cache_entries
        self._max_cache_bytes = max_cache_bytes
        self._cache: OrderedDict[str, SourceGraphCacheEntry] = OrderedDict()
        self._inflight: dict[str, _Flight] = {}
        self._state_lock = asyncio.Lock()
        self._stats: dict[str, int | float] = {
            "actual_build_count": 0,
            "frontend_launch_count": 0,
            "cache_hit_count": 0,
            "cache_miss_count": 0,
            "cache_bypass_count": 0,
            "cache_eviction_count": 0,
            "cache_oversize_bypass_count": 0,
            "cache_peak_entry_count": 0,
            "cache_peak_bytes": 0,
            "coalesced_waiter_count": 0,
            "cancelled_waiter_count": 0,
            "timeout_count": 0,
            "worker_failure_count": 0,
            "last_cancel_to_exit_ms": 0.0,
            "disk_lookup_count": 0,
            "disk_hit_count": 0,
            "disk_miss_count": 0,
            "disk_corrupt_count": 0,
            "disk_build_skip_count": 0,
            "disk_publish_count": 0,
            "disk_publish_failure_count": 0,
            "disk_bytes_read": 0,
            "disk_bytes_written": 0,
            "disk_entry_count": 0,
            "disk_bytes": 0,
            "disk_eviction_count": 0,
            "disk_lookup_wall_ms": 0.0,
            "disk_read_wall_ms": 0.0,
            "disk_validate_wall_ms": 0.0,
            "disk_publish_wall_ms": 0.0,
            "disk_write_wall_ms": 0.0,
            "disk_eviction_wall_ms": 0.0,
        }

    async def prepare(
        self,
        request: SourceGraphBuildRequest,
        *,
        timeout_seconds: float = 120.0,
        cancel_event: asyncio.Event | None = None,
    ) -> SourceGraphPrepareOutcome:
        effective_timeout_sec = _validated_timeout_seconds(timeout_seconds)
        started = time.perf_counter()
        build_key = compute_source_graph_build_key(request)
        if cancel_event is not None and cancel_event.is_set():
            self._stats["cancelled_waiter_count"] += 1
            return self._standalone_cancelled_outcome(
                build_key,
                started,
                (
                    CacheDisposition.MISS
                    if build_key.cross_request_reusable
                    else CacheDisposition.BYPASS_INCOMPLETE_KEY
                ),
                effective_timeout_sec,
            )

        async with self._state_lock:
            if build_key.cross_request_reusable:
                cached = self._find_cached_locked(request, build_key)
                if cached is not None:
                    entry, disposition, match = cached
                    self._stats["cache_hit_count"] += 1
                    return self._cache_hit_outcome(
                        build_key,
                        entry,
                        disposition,
                        match,
                        started,
                        effective_timeout_sec,
                    )
                cache_disposition = CacheDisposition.MISS
                cache_lookup_reason = self._cache_miss_reason_locked(build_key)
                self._stats["cache_miss_count"] += 1
            else:
                cache_disposition = CacheDisposition.BYPASS_INCOMPLETE_KEY
                cache_lookup_reason = CacheLookupReason.IDENTITY_NOT_REUSABLE
                self._stats["cache_bypass_count"] += 1

            # Incomplete identities remain ineligible for memory/disk reuse,
            # but exact overlapping requests may share only the live worker.
            # A completed flight is removed immediately and cannot serve a
            # later request as a cache hit.
            flight_key = _exact_flight_key(
                build_key,
                effective_timeout_sec,
            )

            flight = self._inflight.get(flight_key)
            if flight is not None and flight.task is not None and flight.task.done():
                self._inflight.pop(flight_key, None)
                flight = None
            created = flight is None
            if flight is None:
                flight = _Flight(
                    flight_key=flight_key,
                    request=request,
                    build_key=build_key,
                    cache_disposition=cache_disposition,
                    cache_lookup_reason=cache_lookup_reason,
                    effective_timeout_sec=effective_timeout_sec,
                    waiter_count=1,
                )
                self._inflight[flight_key] = flight
                flight.task = asyncio.create_task(
                    self._execute_flight(flight)
                )
            else:
                flight.waiter_count += 1
                flight.coalesced_waiter_count += 1
                self._stats["coalesced_waiter_count"] += 1

        role = FlightDisposition.BUILDER if created else FlightDisposition.COALESCED
        assert flight.task is not None
        try:
            if cancel_event is None:
                shared_outcome = await asyncio.shield(flight.task)
            else:
                shared_outcome = await self._wait_with_explicit_cancel(
                    flight,
                    cancel_event,
                    started=started,
                    role=role,
                )
                if shared_outcome.status is PrepareStatus.CANCELLED:
                    return shared_outcome
        except asyncio.CancelledError:
            await self._release_waiter(flight, cancelled=True)
            return self._cancelled_waiter_outcome(flight, started, role)
        else:
            await self._release_waiter(flight, cancelled=False)
            return self._adapt_for_waiter(shared_outcome, flight, started, role)

    async def _wait_with_explicit_cancel(
        self,
        flight: _Flight,
        cancel_event: asyncio.Event,
        *,
        started: float,
        role: FlightDisposition,
    ) -> SourceGraphPrepareOutcome:
        assert flight.task is not None
        shared = asyncio.shield(flight.task)
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {shared, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if shared in done:
                return await shared
            shared.cancel()
            sole = await self._release_waiter(flight, cancelled=True)
            if sole:
                completed = await asyncio.shield(flight.task)
                return self._cancelled_waiter_outcome(
                    flight,
                    started,
                    role,
                    cancel_to_exit_ms=completed.metrics.cancel_to_exit_ms,
                )
            return self._cancelled_waiter_outcome(flight, started, role)
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _release_waiter(self, flight: _Flight, *, cancelled: bool) -> bool:
        async with self._state_lock:
            if flight.waiter_count > 0:
                flight.waiter_count -= 1
            sole_cancel = (
                cancelled
                and flight.waiter_count == 0
                and flight.task is not None
                and not flight.task.done()
            )
            if cancelled:
                self._stats["cancelled_waiter_count"] += 1
            if sole_cancel:
                flight.cancel_requested_at = time.perf_counter()
                flight.worker_cancel_event.set()
            return sole_cancel

    async def _execute_flight(
        self,
        flight: _Flight,
    ) -> SourceGraphPrepareOutcome:
        try:
            return await self._execute_flight_body(flight)
        finally:
            await self._remove_flight(flight)

    async def _execute_flight_body(
        self,
        flight: _Flight,
    ) -> SourceGraphPrepareOutcome:
        request = flight.request
        key = flight.build_key
        disk_outcome = await self._prepare_exact_disk_hit(flight)
        if disk_outcome is not None:
            return disk_outcome
        admission_started = time.perf_counter()
        admitted = False
        try:
            while not admitted:
                if flight.worker_cancel_event.is_set():
                    return self._flight_failure(
                        flight,
                        PrepareStatus.CANCELLED,
                        code="request_cancelled",
                        stage="admission",
                        admission_wait_ms=(time.perf_counter() - admission_started)
                        * 1000,
                    )
                admitted = _PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
                if not admitted:
                    await asyncio.sleep(0.005)

            admission_wait_ms = (time.perf_counter() - admission_started) * 1000
            if flight.worker_cancel_event.is_set():
                return self._flight_failure(
                    flight,
                    PrepareStatus.CANCELLED,
                    code="request_cancelled",
                    stage="admission",
                    admission_wait_ms=admission_wait_ms,
                )

            flight.actual_build_count = 1
            async with self._state_lock:
                self._stats["actual_build_count"] += 1
                self._stats["frontend_launch_count"] += 1
            build_started = time.perf_counter()
            try:
                worker = await self._worker_runner.run(
                    request,
                    timeout_seconds=flight.effective_timeout_sec,
                    cancel_event=flight.worker_cancel_event,
                )
            except Exception as exc:
                worker = WorkerBuildResult.failed(
                    PrepareStatus.WORKER_CRASH,
                    code="worker_runner_exception",
                    stage="worker_process",
                    message=f"{type(exc).__name__}: {exc}",
                )
            build_wall_ms = (time.perf_counter() - build_started) * 1000
        finally:
            if admitted:
                _PROCESS_COLD_BUILD_LOCK.release()

        if worker.status is not PrepareStatus.READY:
            async with self._state_lock:
                if worker.status is PrepareStatus.TIMED_OUT:
                    self._stats["timeout_count"] += 1
                elif worker.status is not PrepareStatus.CANCELLED:
                    self._stats["worker_failure_count"] += 1
                if worker.metrics.cancel_to_exit_ms is not None:
                    self._stats["last_cancel_to_exit_ms"] = (
                        worker.metrics.cancel_to_exit_ms
                    )
            return self._worker_failure_outcome(
                flight,
                worker,
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
            )

        if flight.worker_cancel_event.is_set():
            return self._flight_failure(
                flight,
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="worker_response",
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
                worker_metrics=worker.metrics,
            )

        load_started = time.perf_counter()
        try:
            entry = await asyncio.to_thread(
                self._load_cache_entry,
                request,
                key,
                worker,
            )
        except Exception as exc:
            return self._flight_failure(
                flight,
                PrepareStatus.INVALID_RESPONSE,
                code="worker_payload_invalid",
                stage="cache_load",
                message=f"{type(exc).__name__}: {exc}",
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
                load_wall_ms=(time.perf_counter() - load_started) * 1000,
                worker_metrics=worker.metrics,
            )
        load_wall_ms = (time.perf_counter() - load_started) * 1000

        if flight.worker_cancel_event.is_set():
            return self._flight_failure(
                flight,
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="cache_publish",
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
                load_wall_ms=load_wall_ms,
                worker_metrics=worker.metrics,
            )

        if self._disk_cache is not None and key.cross_request_reusable:
            try:
                await self._publish_disk_entry(flight, entry)
            except DiskCacheCancelled:
                return self._flight_failure(
                    flight,
                    PrepareStatus.CANCELLED,
                    code="request_cancelled",
                    stage="disk_publish",
                    admission_wait_ms=admission_wait_ms,
                    build_wall_ms=build_wall_ms,
                    load_wall_ms=load_wall_ms,
                    worker_metrics=worker.metrics,
                )

        if key.cross_request_reusable:
            async with self._state_lock:
                if entry.cache_bytes > self._max_cache_bytes:
                    flight.cache_disposition = CacheDisposition.BYPASS_CAPACITY
                    flight.cache_lookup_reason = (
                        CacheLookupReason.ARTIFACT_EXCEEDS_CACHE_CAPACITY
                    )
                    self._stats["cache_bypass_count"] += 1
                    self._stats["cache_oversize_bypass_count"] += 1
                else:
                    self._publish_cache_entry_locked(key.digest, entry)
        scope_match = entry.artifact_scope_receipt.reuse_for(
            request.artifact_identity.scope
        )
        return SourceGraphPrepareOutcome(
            status=PrepareStatus.READY,
            build_key=key,
            cache_lookup_reason=flight.cache_lookup_reason,
            effective_timeout_sec=flight.effective_timeout_sec,
            entry=entry,
            scope_match=scope_match,
            metrics=self._prepare_metrics(
                flight,
                total_wall_ms=admission_wait_ms + build_wall_ms + load_wall_ms,
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
                load_wall_ms=load_wall_ms,
                worker_metrics=worker.metrics,
                ir_bytes=entry.ir_bytes,
                cache_bytes=entry.cache_bytes,
            ),
        )

    async def _prepare_exact_disk_hit(
        self,
        flight: _Flight,
    ) -> SourceGraphPrepareOutcome | None:
        disk_cache = self._disk_cache
        if disk_cache is None:
            flight.disk_validation_outcome = "disabled"
            return None
        if not flight.build_key.cross_request_reusable:
            flight.disk_validation_outcome = "not_checked"
            return None
        if flight.worker_cancel_event.is_set():
            return self._flight_failure(
                flight,
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="disk_lookup",
            )

        lookup_started = time.perf_counter()
        try:
            disk_result = await asyncio.to_thread(
                disk_cache.lookup,
                flight.request.artifact_identity,
                cancelled=flight.worker_cancel_event.is_set,
            )
        except DiskCacheCancelled:
            return self._flight_failure(
                flight,
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="disk_lookup",
                load_wall_ms=(time.perf_counter() - lookup_started) * 1000,
            )
        except Exception:  # noqa: BLE001
            disk_result = None

        if disk_result is None:
            flight.disk_validation_outcome = DiskValidationOutcome.IO_ERROR.value
            flight.disk_lookup_wall_ms = (time.perf_counter() - lookup_started) * 1000
        else:
            flight.disk_validation_outcome = disk_result.outcome.value
            flight.disk_lookup_wall_ms = disk_result.lookup_wall_ms
            flight.disk_read_wall_ms = disk_result.read_wall_ms
            flight.disk_validate_wall_ms = disk_result.validate_wall_ms
            flight.disk_bytes_read = disk_result.bytes_read

        async with self._state_lock:
            self._stats["disk_lookup_count"] += 1
            self._stats["disk_lookup_wall_ms"] += flight.disk_lookup_wall_ms
            self._stats["disk_read_wall_ms"] += flight.disk_read_wall_ms
            self._stats["disk_validate_wall_ms"] += flight.disk_validate_wall_ms
            self._stats["disk_bytes_read"] += flight.disk_bytes_read

        if disk_result is None or not disk_result.hit:
            flight.disk_miss_count = 1
            if disk_result is not None and disk_result.corrupt:
                flight.disk_corrupt_count = 1
            async with self._state_lock:
                self._stats["disk_miss_count"] += 1
                self._stats["disk_corrupt_count"] += flight.disk_corrupt_count
            return None

        assert disk_result.artifact is not None
        if flight.worker_cancel_event.is_set():
            return self._flight_failure(
                flight,
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="disk_validate",
            )
        load_started = time.perf_counter()
        try:
            entry = await asyncio.to_thread(
                self._cache_entry_from_disk,
                flight,
                disk_result.artifact,
            )
        except Exception:  # noqa: BLE001
            flight.disk_validation_outcome = (
                DiskValidationOutcome.IR_SCHEMA_MISMATCH.value
            )
            flight.disk_miss_count = 1
            flight.disk_corrupt_count = 1
            async with self._state_lock:
                self._stats["disk_miss_count"] += 1
                self._stats["disk_corrupt_count"] += 1
            return None
        load_wall_ms = (time.perf_counter() - load_started) * 1000
        if flight.worker_cancel_event.is_set():
            return self._flight_failure(
                flight,
                PrepareStatus.CANCELLED,
                code="request_cancelled",
                stage="disk_load",
                load_wall_ms=load_wall_ms,
            )

        flight.cache_tier = CacheTier.DISK
        flight.disk_hit_count = 1
        flight.disk_build_skip_count = 1
        flight.disk_entry_count = 1
        flight.disk_bytes = disk_result.artifact.entry_bytes
        async with self._state_lock:
            self._stats["disk_hit_count"] += 1
            self._stats["disk_build_skip_count"] += 1
            self._stats["disk_entry_count"] = max(
                int(self._stats["disk_entry_count"]), 1
            )
            self._stats["disk_bytes"] = max(
                int(self._stats["disk_bytes"]), disk_result.artifact.entry_bytes
            )
            if entry.cache_bytes > self._max_cache_bytes:
                flight.cache_disposition = CacheDisposition.BYPASS_CAPACITY
                flight.cache_lookup_reason = (
                    CacheLookupReason.ARTIFACT_EXCEEDS_CACHE_CAPACITY
                )
                self._stats["cache_bypass_count"] += 1
                self._stats["cache_oversize_bypass_count"] += 1
            else:
                self._publish_cache_entry_locked(entry.build_key.digest, entry)

        scope_match = entry.artifact_scope_receipt.reuse_for(
            flight.request.artifact_identity.scope
        )
        return SourceGraphPrepareOutcome(
            status=PrepareStatus.READY,
            build_key=flight.build_key,
            cache_lookup_reason=flight.cache_lookup_reason,
            effective_timeout_sec=flight.effective_timeout_sec,
            entry=entry,
            scope_match=scope_match,
            metrics=self._prepare_metrics(
                flight,
                total_wall_ms=flight.disk_lookup_wall_ms + load_wall_ms,
                load_wall_ms=load_wall_ms,
                ir_bytes=entry.ir_bytes,
                cache_bytes=entry.cache_bytes,
            ),
        )

    @staticmethod
    def _cache_entry_from_disk(
        flight: _Flight, artifact: DiskArtifact
    ) -> SourceGraphCacheEntry:
        if artifact.artifact_key.digest != flight.build_key.digest:
            raise ValueError("disk artifact key does not match flight")
        if artifact.identity != flight.request.artifact_identity:
            raise ValueError("disk artifact identity does not match flight")
        engine = ConnectivityQueryEngine(artifact.ir)
        return SourceGraphCacheEntry(
            build_key=flight.build_key,
            scope_receipt=artifact.scope_receipt,
            artifact_scope_receipt=artifact.scope_receipt,
            ir=artifact.ir,
            query_engine=engine,
            ir_json_bytes=artifact.ir_json_bytes,
            ir_fingerprint_sha256=artifact.ir_fingerprint_sha256,
            ir_bytes=artifact.ir_bytes,
            cache_bytes=artifact.ir_bytes,
        )

    async def _publish_disk_entry(
        self,
        flight: _Flight,
        entry: SourceGraphCacheEntry,
    ) -> None:
        assert self._disk_cache is not None
        if flight.worker_cancel_event.is_set():
            raise DiskCacheCancelled("cancelled before disk publish")
        publish_started = time.perf_counter()
        try:
            publish_result = await asyncio.to_thread(
                self._disk_cache.publish,
                flight.request.artifact_identity,
                entry.ir_json_bytes,
                entry.artifact_scope_receipt,
                cancelled=flight.worker_cancel_event.is_set,
            )
        except DiskCacheCancelled:
            raise
        except Exception:  # noqa: BLE001
            flight.disk_publish_wall_ms = (time.perf_counter() - publish_started) * 1000
            async with self._state_lock:
                self._stats["disk_publish_wall_ms"] += flight.disk_publish_wall_ms
                self._stats["disk_publish_failure_count"] += 1
            return
        flight.disk_publish_wall_ms = publish_result.publish_wall_ms
        flight.disk_write_wall_ms = publish_result.write_wall_ms
        flight.disk_eviction_wall_ms = publish_result.eviction_wall_ms
        flight.disk_bytes_written = (
            publish_result.entry_bytes if publish_result.write_wall_ms > 0 else 0
        )
        if publish_result.published:
            flight.disk_entry_count = publish_result.disk_entry_count
            flight.disk_bytes = publish_result.disk_bytes
        flight.disk_eviction_count = publish_result.eviction_count
        async with self._state_lock:
            self._stats["disk_publish_wall_ms"] += flight.disk_publish_wall_ms
            self._stats["disk_write_wall_ms"] += flight.disk_write_wall_ms
            self._stats["disk_eviction_wall_ms"] += flight.disk_eviction_wall_ms
            self._stats["disk_bytes_written"] += flight.disk_bytes_written
            if publish_result.published:
                self._stats["disk_entry_count"] = flight.disk_entry_count
                self._stats["disk_bytes"] = flight.disk_bytes
            self._stats["disk_eviction_count"] += flight.disk_eviction_count
            if publish_result.outcome in {
                DiskPublishOutcome.PUBLISHED,
                DiskPublishOutcome.ALREADY_PRESENT,
            }:
                self._stats["disk_publish_count"] += 1
            else:
                self._stats["disk_publish_failure_count"] += 1

    async def _remove_flight(self, flight: _Flight) -> None:
        async with self._state_lock:
            if self._inflight.get(flight.flight_key) is flight:
                self._inflight.pop(flight.flight_key, None)

    def _load_cache_entry(
        self,
        request: SourceGraphBuildRequest,
        key: SourceGraphBuildKey,
        worker: WorkerBuildResult,
    ) -> SourceGraphCacheEntry:
        assert worker.ir_json_bytes is not None
        assert worker.ir_fingerprint_sha256 is not None
        assert worker.scope_receipt is not None
        ir = ConnectivityIR.from_json_bytes(worker.ir_json_bytes)
        fingerprint = ir.fingerprint_sha256()
        if fingerprint != worker.ir_fingerprint_sha256:
            raise ValueError("worker IR fingerprint mismatch")
        artifact_identity = request.artifact_identity
        if ir.frontend_name != artifact_identity.source.frontend_name:
            raise ValueError("worker frontend name mismatch")
        if ir.frontend_version != artifact_identity.source.frontend_version:
            raise ValueError("worker frontend version mismatch")
        if ir.ir_version != artifact_identity.source.ir_schema_version:
            raise ValueError("worker IR schema version mismatch")
        if artifact_identity.scope.top not in ir.top_instances:
            raise ValueError("worker IR does not contain the requested top")
        if isinstance(worker.scope_receipt, SourceGraphArtifactScopeReceipt):
            if worker.scope_receipt.scope != artifact_identity.scope:
                raise ValueError("worker artifact scope receipt does not match request")
            artifact_scope_receipt = worker.scope_receipt
        else:
            if worker.scope_receipt.scope != request.scope:
                raise ValueError("worker scope receipt does not match request")
            artifact_scope_receipt = SourceGraphArtifactScopeReceipt(
                scope=artifact_identity.scope,
                coverage_status=worker.scope_receipt.coverage_status,
                gap_codes=worker.scope_receipt.gap_codes,
            )
        if worker.scope_receipt.coverage_status is not ir.coverage.status:
            raise ValueError("worker scope receipt coverage differs from IR")
        engine = ConnectivityQueryEngine(ir)
        return SourceGraphCacheEntry(
            build_key=key,
            scope_receipt=worker.scope_receipt,
            artifact_scope_receipt=artifact_scope_receipt,
            ir=ir,
            query_engine=engine,
            ir_json_bytes=worker.ir_json_bytes,
            ir_fingerprint_sha256=fingerprint,
            ir_bytes=len(worker.ir_json_bytes),
            cache_bytes=len(worker.ir_json_bytes),
        )

    async def wait_idle(self) -> None:
        while True:
            async with self._state_lock:
                tasks = [
                    flight.task
                    for flight in self._inflight.values()
                    if flight.task is not None and not flight.task.done()
                ]
            if not tasks:
                return
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks), return_exceptions=True
            )

    def stats_snapshot(self) -> dict[str, int | float]:
        result = dict(self._stats)
        result["cache_entry_count"] = len(self._cache)
        result["cache_bytes"] = sum(entry.cache_bytes for entry in self._cache.values())
        result["inflight_count"] = len(self._inflight)
        result["cache_max_entries"] = self._max_cache_entries
        result["cache_max_bytes"] = self._max_cache_bytes
        result["disk_cache_max_entries"] = (
            self._disk_cache.max_entries if self._disk_cache is not None else 0
        )
        result["disk_cache_max_bytes"] = (
            self._disk_cache.max_bytes if self._disk_cache is not None else 0
        )
        return result

    def _publish_cache_entry_locked(
        self, key: str, entry: SourceGraphCacheEntry
    ) -> None:
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while (
            len(self._cache) > self._max_cache_entries
            or sum(item.cache_bytes for item in self._cache.values())
            > self._max_cache_bytes
        ):
            self._cache.popitem(last=False)
            self._stats["cache_eviction_count"] += 1
        current_bytes = sum(item.cache_bytes for item in self._cache.values())
        self._stats["cache_peak_entry_count"] = max(
            int(self._stats["cache_peak_entry_count"]), len(self._cache)
        )
        self._stats["cache_peak_bytes"] = max(
            int(self._stats["cache_peak_bytes"]), current_bytes
        )

    def _find_cached_locked(
        self,
        request: SourceGraphBuildRequest,
        build_key: SourceGraphBuildKey,
    ) -> tuple[SourceGraphCacheEntry, CacheDisposition, ScopeReuseDecision] | None:
        exact = self._cache.get(build_key.digest)
        if exact is not None:
            match = exact.artifact_scope_receipt.reuse_for(
                request.artifact_identity.scope
            )
            if match.relation is ScopeRelation.EXACT and match.reusable:
                self._cache.move_to_end(build_key.digest)
                return exact, CacheDisposition.HIT_EXACT, match
        candidates: list[
            tuple[int, int, str, SourceGraphCacheEntry, ScopeReuseDecision]
        ] = []
        for entry in self._cache.values():
            if entry.build_key.design_digest != build_key.design_digest:
                continue
            match = entry.artifact_scope_receipt.reuse_for(
                request.artifact_identity.scope
            )
            if match.relation is not ScopeRelation.SUPERSET or not match.reusable:
                continue
            scope = entry.artifact_scope_receipt.scope
            candidates.append(
                (
                    len(scope.projection_instance_paths),
                    len(scope.coverage_boundary.instance_paths),
                    entry.build_key.digest,
                    entry,
                    match,
                )
            )
        if not candidates:
            return None
        _, _, _, entry, match = min(candidates, key=lambda item: item[:3])
        self._cache.move_to_end(entry.build_key.digest)
        return entry, CacheDisposition.HIT_SUPERSET, match

    def _cache_miss_reason_locked(
        self, build_key: SourceGraphBuildKey
    ) -> CacheLookupReason:
        if not self._cache:
            return CacheLookupReason.NO_CACHED_ARTIFACT
        if any(
            entry.build_key.design_digest == build_key.design_digest
            for entry in self._cache.values()
        ):
            return CacheLookupReason.CACHED_SCOPE_NOT_DOMINATING
        return CacheLookupReason.ARTIFACT_SEMANTICS_MISMATCH

    def _cache_hit_outcome(
        self,
        build_key: SourceGraphBuildKey,
        entry: SourceGraphCacheEntry,
        disposition: CacheDisposition,
        match: ScopeReuseDecision,
        started: float,
        effective_timeout_sec: float,
    ) -> SourceGraphPrepareOutcome:
        return SourceGraphPrepareOutcome(
            status=PrepareStatus.READY,
            build_key=build_key,
            cache_lookup_reason=(
                CacheLookupReason.EXACT_ARTIFACT
                if disposition is CacheDisposition.HIT_EXACT
                else CacheLookupReason.DOMINATING_ARTIFACT
            ),
            effective_timeout_sec=effective_timeout_sec,
            entry=entry,
            scope_match=match,
            metrics=SourceGraphPrepareMetrics(
                cache_disposition=disposition,
                flight_disposition=FlightDisposition.NONE,
                total_wall_ms=(time.perf_counter() - started) * 1000,
                cache_tier=CacheTier.MEMORY,
                disk_validation_outcome="not_checked",
                ir_bytes=entry.ir_bytes,
                cache_bytes=entry.cache_bytes,
                **self._cache_metric_fields(),
            ),
        )

    def _cache_metric_fields(self) -> dict[str, int]:
        return {
            "cache_entry_count": len(self._cache),
            "cache_peak_entry_count": int(self._stats["cache_peak_entry_count"]),
            "cache_peak_bytes": int(self._stats["cache_peak_bytes"]),
            "cache_eviction_count": int(self._stats["cache_eviction_count"]),
            "cache_oversize_bypass_count": int(
                self._stats["cache_oversize_bypass_count"]
            ),
        }

    def _adapt_for_waiter(
        self,
        outcome: SourceGraphPrepareOutcome,
        flight: _Flight,
        started: float,
        role: FlightDisposition,
    ) -> SourceGraphPrepareOutcome:
        metrics = replace(
            outcome.metrics,
            cache_disposition=flight.cache_disposition,
            flight_disposition=role,
            total_wall_ms=(time.perf_counter() - started) * 1000,
            actual_build_count=(
                flight.actual_build_count if role is FlightDisposition.BUILDER else 0
            ),
            frontend_launch_count=(
                flight.actual_build_count if role is FlightDisposition.BUILDER else 0
            ),
            coalesced_waiter_count=flight.coalesced_waiter_count,
        )
        return replace(
            outcome,
            metrics=metrics,
            cache_lookup_reason=(
                CacheLookupReason.SAME_ARTIFACT_INFLIGHT
                if role is FlightDisposition.COALESCED
                else outcome.cache_lookup_reason
            ),
        )

    def _prepare_metrics(
        self,
        flight: _Flight,
        *,
        total_wall_ms: float,
        admission_wait_ms: float = 0.0,
        build_wall_ms: float = 0.0,
        load_wall_ms: float = 0.0,
        worker_metrics: WorkerResourceMetrics | None = None,
        ir_bytes: int = 0,
        cache_bytes: int = 0,
    ) -> SourceGraphPrepareMetrics:
        worker_metrics = worker_metrics or WorkerResourceMetrics()
        return SourceGraphPrepareMetrics(
            cache_disposition=flight.cache_disposition,
            flight_disposition=FlightDisposition.BUILDER,
            total_wall_ms=total_wall_ms,
            cache_tier=flight.cache_tier,
            disk_validation_outcome=flight.disk_validation_outcome,
            admission_wait_ms=admission_wait_ms,
            build_wall_ms=build_wall_ms,
            load_wall_ms=load_wall_ms,
            actual_build_count=flight.actual_build_count,
            coalesced_waiter_count=flight.coalesced_waiter_count,
            cancel_to_exit_ms=worker_metrics.cancel_to_exit_ms,
            worker_cpu_ms=worker_metrics.cpu_time_ms,
            rss_start_kib=worker_metrics.rss_start_kib,
            rss_peak_kib=worker_metrics.rss_peak_kib,
            rss_end_kib=worker_metrics.rss_end_kib,
            ir_bytes=ir_bytes or worker_metrics.ir_bytes,
            cache_bytes=cache_bytes,
            frontend_launch_count=flight.actual_build_count,
            disk_lookup_wall_ms=flight.disk_lookup_wall_ms,
            disk_read_wall_ms=flight.disk_read_wall_ms,
            disk_validate_wall_ms=flight.disk_validate_wall_ms,
            disk_publish_wall_ms=flight.disk_publish_wall_ms,
            disk_write_wall_ms=flight.disk_write_wall_ms,
            disk_eviction_wall_ms=flight.disk_eviction_wall_ms,
            disk_hit_count=flight.disk_hit_count,
            disk_miss_count=flight.disk_miss_count,
            disk_corrupt_count=flight.disk_corrupt_count,
            disk_build_skip_count=flight.disk_build_skip_count,
            disk_bytes_read=flight.disk_bytes_read,
            disk_bytes_written=flight.disk_bytes_written,
            disk_entry_count=flight.disk_entry_count,
            disk_bytes=flight.disk_bytes,
            disk_eviction_count=flight.disk_eviction_count,
            **self._cache_metric_fields(),
        )

    def _worker_failure_outcome(
        self,
        flight: _Flight,
        worker: WorkerBuildResult,
        *,
        admission_wait_ms: float,
        build_wall_ms: float,
    ) -> SourceGraphPrepareOutcome:
        assert worker.blocker is not None
        return SourceGraphPrepareOutcome(
            status=worker.status,
            build_key=flight.build_key,
            cache_lookup_reason=flight.cache_lookup_reason,
            effective_timeout_sec=flight.effective_timeout_sec,
            blocker=worker.blocker,
            metrics=self._prepare_metrics(
                flight,
                total_wall_ms=admission_wait_ms + build_wall_ms,
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
                worker_metrics=worker.metrics,
            ),
        )

    def _flight_failure(
        self,
        flight: _Flight,
        status: PrepareStatus,
        *,
        code: str,
        stage: str,
        message: str | None = None,
        admission_wait_ms: float = 0.0,
        build_wall_ms: float = 0.0,
        load_wall_ms: float = 0.0,
        worker_metrics: WorkerResourceMetrics | None = None,
    ) -> SourceGraphPrepareOutcome:
        return SourceGraphPrepareOutcome(
            status=status,
            build_key=flight.build_key,
            cache_lookup_reason=flight.cache_lookup_reason,
            effective_timeout_sec=flight.effective_timeout_sec,
            blocker=InternalBuildBlocker(code=code, stage=stage, message=message),
            metrics=self._prepare_metrics(
                flight,
                total_wall_ms=admission_wait_ms + build_wall_ms + load_wall_ms,
                admission_wait_ms=admission_wait_ms,
                build_wall_ms=build_wall_ms,
                load_wall_ms=load_wall_ms,
                worker_metrics=worker_metrics,
            ),
        )

    def _cancelled_waiter_outcome(
        self,
        flight: _Flight,
        started: float,
        role: FlightDisposition,
        *,
        cancel_to_exit_ms: float | None = None,
    ) -> SourceGraphPrepareOutcome:
        return SourceGraphPrepareOutcome(
            status=PrepareStatus.CANCELLED,
            build_key=flight.build_key,
            cache_lookup_reason=flight.cache_lookup_reason,
            effective_timeout_sec=flight.effective_timeout_sec,
            blocker=InternalBuildBlocker(code="request_cancelled", stage="waiter"),
            metrics=SourceGraphPrepareMetrics(
                cache_disposition=flight.cache_disposition,
                flight_disposition=role,
                total_wall_ms=(time.perf_counter() - started) * 1000,
                cache_tier=flight.cache_tier,
                disk_validation_outcome=flight.disk_validation_outcome,
                actual_build_count=(
                    flight.actual_build_count
                    if role is FlightDisposition.BUILDER
                    else 0
                ),
                coalesced_waiter_count=flight.coalesced_waiter_count,
                cancel_to_exit_ms=cancel_to_exit_ms,
                frontend_launch_count=(
                    flight.actual_build_count
                    if role is FlightDisposition.BUILDER
                    else 0
                ),
                disk_lookup_wall_ms=flight.disk_lookup_wall_ms,
                disk_read_wall_ms=flight.disk_read_wall_ms,
                disk_validate_wall_ms=flight.disk_validate_wall_ms,
                disk_publish_wall_ms=flight.disk_publish_wall_ms,
                disk_write_wall_ms=flight.disk_write_wall_ms,
                disk_eviction_wall_ms=flight.disk_eviction_wall_ms,
                disk_hit_count=flight.disk_hit_count,
                disk_miss_count=flight.disk_miss_count,
                disk_corrupt_count=flight.disk_corrupt_count,
                disk_build_skip_count=flight.disk_build_skip_count,
                disk_bytes_read=flight.disk_bytes_read,
                disk_bytes_written=flight.disk_bytes_written,
                disk_entry_count=flight.disk_entry_count,
                disk_bytes=flight.disk_bytes,
                disk_eviction_count=flight.disk_eviction_count,
            ),
        )

    @staticmethod
    def _standalone_cancelled_outcome(
        build_key: SourceGraphBuildKey,
        started: float,
        disposition: CacheDisposition,
        effective_timeout_sec: float,
    ) -> SourceGraphPrepareOutcome:
        return SourceGraphPrepareOutcome(
            status=PrepareStatus.CANCELLED,
            build_key=build_key,
            cache_lookup_reason=CacheLookupReason.CANCELLED_BEFORE_LOOKUP,
            effective_timeout_sec=effective_timeout_sec,
            blocker=InternalBuildBlocker(code="request_cancelled", stage="admission"),
            metrics=SourceGraphPrepareMetrics(
                cache_disposition=disposition,
                flight_disposition=FlightDisposition.NONE,
                total_wall_ms=(time.perf_counter() - started) * 1000,
                cache_tier=CacheTier.BUILD,
                disk_validation_outcome="not_checked",
            ),
        )
