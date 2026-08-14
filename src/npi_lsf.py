"""Optional LSF execution for Verdi NPI connectivity operations.

The public connectivity API stays synchronous and unchanged. In ``lsf`` mode
the server runs this module in a cancellable worker thread; this transport then
submits one short-lived, NPI-only worker through ``bsub -K``. Scheduler
and worker diagnostics are reduced to fixed labels before they leave this
module. Queue names, hostnames, paths, command lines, and native output never
enter MCP status or telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from config import NpiExecutionConfig
from . import schemas
from .cancellation import OperationCancelled, check_cancelled
from .compile_log_parser import parse_compile_log
from .connectivity_backend import StaticConnectivityBackend
from .verdi_backend import (
    kdb_has_elaboration_errors,
    probe_verdi_backend,
    read_kdb_elab_error_metadata,
)


_LOG = logging.getLogger(__name__)

NPI_WORKER_PROTOCOL_VERSION: Literal["1.0"] = "1.0"
NPI_EXECUTION_STATUS_KEY = "_npi_execution_status"
_MAX_REQUEST_BYTES = 1 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_WAIT_POLL_SEC = 0.1
_CANCEL_WAIT_SEC = 2.0
_BKILL_TIMEOUT_SEC = 5.0


class _ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _WorkerRequestBase(_ProtocolModel):
    protocol_version: Literal["1.0"] = NPI_WORKER_PROTOCOL_VERSION
    kdb_path: str = Field(min_length=1, max_length=16_384)
    top: str = Field(min_length=1, max_length=1_024)


class FindDriverWorkerRequest(_WorkerRequestBase):
    operation: Literal["find_driver"] = "find_driver"
    signal_path: str = Field(min_length=1, max_length=16_384)
    recursive: bool = False


class FindLoadsWorkerRequest(_WorkerRequestBase):
    operation: Literal["find_loads"] = "find_loads"
    signal_path: str = Field(min_length=1, max_length=16_384)
    include_expr: bool = True
    kind_filter: list[str] | None = None


class FindPathWorkerRequest(_WorkerRequestBase):
    operation: Literal["find_path"] = "find_path"
    from_signal: str = Field(min_length=1, max_length=16_384)
    to_signal: str = Field(min_length=1, max_length=16_384)
    expand_assigns: bool = False


NpiWorkerRequest = Annotated[
    FindDriverWorkerRequest | FindLoadsWorkerRequest | FindPathWorkerRequest,
    Field(discriminator="operation"),
]
_REQUEST_ADAPTER = TypeAdapter(NpiWorkerRequest)


class WorkerSuccess(_ProtocolModel):
    protocol_version: Literal["1.0"] = NPI_WORKER_PROTOCOL_VERSION
    status: Literal["ok"] = "ok"
    result: dict[str, Any]
    kdb_load_quality: Literal["clean", "degraded"] | None = None


class WorkerUnavailable(_ProtocolModel):
    protocol_version: Literal["1.0"] = NPI_WORKER_PROTOCOL_VERSION
    status: Literal["npi_unavailable"] = "npi_unavailable"
    error_code: Literal["npi_load_failed"] = "npi_load_failed"
    stage: Literal["load"] = "load"


class WorkerError(_ProtocolModel):
    protocol_version: Literal["1.0"] = NPI_WORKER_PROTOCOL_VERSION
    status: Literal["error"] = "error"
    error_code: Literal[
        "request_invalid",
        "result_invalid",
        "worker_internal_error",
    ]
    stage: Literal["request", "query", "result", "response"]


NpiWorkerResponse = Annotated[
    WorkerSuccess | WorkerUnavailable | WorkerError,
    Field(discriminator="status"),
]
_RESPONSE_ADAPTER = TypeAdapter(NpiWorkerResponse)


@dataclass(frozen=True)
class LsfExecutionResult:
    result: dict[str, Any] | None
    scheduler_status: Literal["completed", "failed", "timed_out"]
    worker_status: Literal["completed", "npi_unavailable", "failed"]
    fallback_reason: str | None = None
    kdb_load_quality: Literal["clean", "degraded"] | None = None


class LsfNpiTransport:
    """Submit one exact NPI request and return only sanitized execution facts."""

    def __init__(self, config: NpiExecutionConfig):
        self._config = config

    def execute(self, request: NpiWorkerRequest) -> LsfExecutionResult:
        if not self._config.valid or self._config.mode != "lsf":
            return _failed_execution("npi_lsf_config_invalid")
        check_cancelled()

        try:
            self._config.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self._config.staging_dir.is_dir():
                return _failed_execution("npi_lsf_worker_failed")
        except OSError:
            return _failed_execution("npi_lsf_worker_failed")

        try:
            with tempfile.TemporaryDirectory(
                prefix="request_",
                dir=str(self._config.staging_dir),
            ) as temp_dir:
                os.chmod(temp_dir, 0o700)
                return self._execute_in_dir(request, Path(temp_dir))
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "LSF NPI transport failed before completion (%s)",
                type(exc).__name__,
            )
            return _failed_execution("npi_lsf_worker_failed")

    def _execute_in_dir(
        self,
        request: NpiWorkerRequest,
        temp_dir: Path,
    ) -> LsfExecutionResult:
        request_path = temp_dir / "request.json"
        response_path = temp_dir / "response.json"
        scheduler_log = temp_dir / "scheduler.log"
        _write_private_text(
            request_path,
            _REQUEST_ADAPTER.dump_json(request).decode("utf-8"),
        )

        job_name = f"tw_npi_{secrets.token_hex(8)}"
        worker_path = Path(__file__).with_name("npi_worker.py").resolve()
        argv = [
            self._config.bsub_bin,
            *self._config.extra_args,
            "-K",
            "-q",
            self._config.queue or "",
            "-J",
            job_name,
            # LSF otherwise emails batch stdout/stderr by default. The worker
            # communicates only through its private response file, so discard
            # native output instead of leaking license text through job mail.
            "-o",
            "/dev/null",
            self._config.python_bin,
            str(worker_path),
            "--request",
            str(request_path),
            "--response",
            str(response_path),
        ]

        log_fd = os.open(
            scheduler_log,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        proc: subprocess.Popen[bytes] | None = None
        try:
            with os.fdopen(log_fd, "wb") as log_file:
                check_cancelled()
                try:
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        shell=False,
                    )
                except (OSError, ValueError) as exc:
                    _LOG.warning(
                        "LSF NPI submission failed (%s)",
                        type(exc).__name__,
                    )
                    return _failed_execution("npi_lsf_submit_failed")

                wait_status = self._wait_for_submission(proc)
                if wait_status == "timed_out":
                    self._cancel_submission(proc, job_name)
                    return LsfExecutionResult(
                        result=None,
                        scheduler_status="timed_out",
                        worker_status="failed",
                        fallback_reason="npi_lsf_timeout",
                    )
        except OperationCancelled:
            if proc is not None:
                self._cancel_submission(proc, job_name)
            raise

        response = _read_worker_response(response_path)
        if response is None:
            if proc is not None and proc.returncode == 0:
                return _worker_failed_execution()
            return _failed_execution("npi_lsf_worker_failed")
        if isinstance(response, WorkerError):
            # A valid error envelope proves that the scheduled worker started
            # and finished its protocol handling, even though it exits nonzero.
            return _worker_failed_execution()
        if proc is None or proc.returncode != 0:
            return _failed_execution("npi_lsf_worker_failed")
        if isinstance(response, WorkerSuccess):
            result = _validate_operation_result(request, response.result)
            if result is None:
                return _worker_failed_execution()
            return LsfExecutionResult(
                result=result,
                scheduler_status="completed",
                worker_status="completed",
                kdb_load_quality=response.kdb_load_quality,
            )
        if isinstance(response, WorkerUnavailable):
            return LsfExecutionResult(
                result=None,
                scheduler_status="completed",
                worker_status="npi_unavailable",
                fallback_reason="npi_lsf_npi_unavailable",
            )
        return _worker_failed_execution()

    def _wait_for_submission(
        self,
        proc: subprocess.Popen[bytes],
    ) -> Literal["completed", "timed_out"]:
        deadline = time.monotonic() + self._config.timeout_sec
        while proc.poll() is None:
            check_cancelled()
            if time.monotonic() >= deadline:
                return "timed_out"
            time.sleep(_WAIT_POLL_SEC)
        return "completed"

    def _cancel_submission(
        self,
        proc: subprocess.Popen[bytes],
        job_name: str,
    ) -> None:
        # bsub -K is only the local waiter. Cancel the uniquely named remote
        # job first, then stop the waiter. Both calls are bounded and best-effort.
        try:
            subprocess.run(
                [self._config.bkill_bin, "-J", job_name, "0"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_BKILL_TIMEOUT_SEC,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=_CANCEL_WAIT_SEC)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=_CANCEL_WAIT_SEC)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass


class LsfConnectivityBackend:
    """Connectivity backend that delegates NPI work to an LSF worker."""

    name = "verdi_npi"
    uses_external_worker = True

    def __init__(
        self,
        config: NpiExecutionConfig,
        fallback: StaticConnectivityBackend | None = None,
        transport: LsfNpiTransport | None = None,
    ):
        self.execution_mode = config.mode
        self._config = config
        self._fallback = fallback or StaticConnectivityBackend()
        self._transport = transport or LsfNpiTransport(config)
        self._last_kdb_status: dict[str, Any] | None = None

    @property
    def kdb_status(self) -> dict[str, Any] | None:
        return (
            dict(self._last_kdb_status)
            if self._last_kdb_status is not None
            else None
        )

    def find_driver(
        self,
        signal_path: str,
        wave_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        recursive: bool = False,
        max_depth: int = 10,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        self._last_kdb_status = None
        target, setup_reason = self._resolve_target(compile_log, simulator, top_hint)
        if target is None:
            return self._fallback_driver(
                signal_path,
                wave_path,
                compile_log,
                top_hint,
                recursive,
                max_depth,
                simulator,
                setup_reason,
            )
        request = FindDriverWorkerRequest(
            kdb_path=target[0],
            top=target[1],
            signal_path=signal_path,
            recursive=recursive,
        )
        outcome = self._transport.execute(request)
        if outcome.result is not None:
            result = dict(outcome.result)
            result["wave_path"] = wave_path
            self._last_kdb_status = _attach_execution_receipt(
                result,
                self.execution_mode,
                outcome,
                kdb_path=target[0],
            )
            return result
        return self._fallback_driver(
            signal_path,
            wave_path,
            compile_log,
            top_hint,
            recursive,
            max_depth,
            simulator,
            outcome.fallback_reason,
            outcome,
        )

    def find_loads(
        self,
        signal_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        max_depth: int = 1,
        include_expr: bool = True,
        kind_filter: list[str] | None = None,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        self._last_kdb_status = None
        target, setup_reason = self._resolve_target(compile_log, simulator, top_hint)
        if target is None:
            return self._fallback_loads(
                signal_path,
                compile_log,
                top_hint,
                max_depth,
                include_expr,
                kind_filter,
                simulator,
                setup_reason,
            )
        request = FindLoadsWorkerRequest(
            kdb_path=target[0],
            top=target[1],
            signal_path=signal_path,
            include_expr=include_expr,
            kind_filter=kind_filter,
        )
        outcome = self._transport.execute(request)
        if outcome.result is not None:
            result = dict(outcome.result)
            self._last_kdb_status = _attach_execution_receipt(
                result,
                self.execution_mode,
                outcome,
                kdb_path=target[0],
            )
            return result
        return self._fallback_loads(
            signal_path,
            compile_log,
            top_hint,
            max_depth,
            include_expr,
            kind_filter,
            simulator,
            outcome.fallback_reason,
            outcome,
        )

    def find_path(
        self,
        from_signal: str,
        to_signal: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        expand_assigns: bool = False,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        self._last_kdb_status = None
        target, setup_reason = self._resolve_target(compile_log, simulator, top_hint)
        if target is None:
            return self._fallback_path(
                from_signal,
                to_signal,
                expand_assigns,
                setup_reason,
            )
        request = FindPathWorkerRequest(
            kdb_path=target[0],
            top=target[1],
            from_signal=from_signal,
            to_signal=to_signal,
            expand_assigns=expand_assigns,
        )
        outcome = self._transport.execute(request)
        if outcome.result is not None:
            result = dict(outcome.result)
            self._last_kdb_status = _attach_execution_receipt(
                result,
                self.execution_mode,
                outcome,
                kdb_path=target[0],
            )
            return result
        return self._fallback_path(
            from_signal,
            to_signal,
            expand_assigns,
            outcome.fallback_reason,
            outcome,
        )

    def _resolve_target(
        self,
        compile_log: str,
        simulator: str,
        top_hint: str | None,
    ) -> tuple[tuple[str, str] | None, str]:
        if not self._config.valid:
            return None, "npi_lsf_config_invalid"
        try:
            compile_result = parse_compile_log(compile_log, simulator)
            status = probe_verdi_backend(
                compile_result,
                compile_log_path=compile_log,
            )
            kdb_path = status.get("kdb_path")
            tops = compile_result.get("top_modules") or []
            top = top_hint or (tops[0] if tops else None)
        except Exception:  # noqa: BLE001
            return None, "kdb_or_top_missing"
        if not kdb_path or not top:
            return None, "kdb_or_top_missing"
        return (str(kdb_path), str(top)), ""

    def _fallback_driver(
        self,
        signal_path: str,
        wave_path: str,
        compile_log: str,
        top_hint: str | None,
        recursive: bool,
        max_depth: int,
        simulator: str,
        reason: str | None,
        outcome: LsfExecutionResult | None = None,
    ) -> dict[str, Any]:
        result = self._fallback.find_driver(
            signal_path,
            wave_path,
            compile_log,
            top_hint=top_hint,
            recursive=recursive,
            max_depth=max_depth,
            simulator=simulator,
        )
        return _tag_fallback(
            result,
            reason or "npi_lsf_worker_failed",
            self.execution_mode,
            outcome,
        )

    def _fallback_loads(
        self,
        signal_path: str,
        compile_log: str,
        top_hint: str | None,
        max_depth: int,
        include_expr: bool,
        kind_filter: list[str] | None,
        simulator: str,
        reason: str | None,
        outcome: LsfExecutionResult | None = None,
    ) -> dict[str, Any]:
        result = self._fallback.find_loads(
            signal_path,
            compile_log,
            top_hint=top_hint,
            max_depth=max_depth,
            include_expr=include_expr,
            kind_filter=kind_filter,
            simulator=simulator,
        )
        return _tag_fallback(
            result,
            reason or "npi_lsf_worker_failed",
            self.execution_mode,
            outcome,
        )

    def _fallback_path(
        self,
        from_signal: str,
        to_signal: str,
        expand_assigns: bool,
        reason: str | None,
        outcome: LsfExecutionResult | None = None,
    ) -> dict[str, Any]:
        result = self._fallback.find_path(
            from_signal,
            to_signal,
            compile_log="",
            expand_assigns=expand_assigns,
        )
        return _tag_fallback(
            result,
            reason or "npi_lsf_worker_failed",
            self.execution_mode,
            outcome,
        )


def execute_worker_request(
    request: NpiWorkerRequest,
    *,
    backend: Any | None = None,
) -> NpiWorkerResponse:
    """Execute one request through the NPI core, never Static fallback."""

    if backend is None:
        from .verdi_npi_backend import VerdiNpiBackend  # noqa: PLC0415

        backend = VerdiNpiBackend()
    try:
        if not backend._ensure_loaded(request.kdb_path, request.top):
            return WorkerUnavailable()
        if isinstance(request, FindDriverWorkerRequest):
            result = backend._npi_find_driver(
                request.signal_path,
                "",
                request.top,
                recursive=request.recursive,
            )
        elif isinstance(request, FindLoadsWorkerRequest):
            result = backend._npi_find_loads(
                request.signal_path,
                {},
                request.kdb_path,
                request.top,
                request.include_expr,
                request.kind_filter,
            )
        else:
            result = backend._npi_find_path(
                request.from_signal,
                request.to_signal,
                expand_assigns=request.expand_assigns,
            )
            result.pop("_npi_call_error", None)
    except Exception:  # noqa: BLE001
        return WorkerError(error_code="worker_internal_error", stage="query")

    payload = _validate_operation_result(request, result)
    if payload is None:
        return WorkerError(error_code="result_invalid", stage="result")
    load_quality = getattr(backend, "kdb_load_quality", "clean")
    if load_quality not in {"clean", "degraded"}:
        load_quality = "clean"
    return WorkerSuccess(result=payload, kdb_load_quality=load_quality)


def _validate_operation_result(
    request: NpiWorkerRequest,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate and normalize an NPI-only result on both protocol boundaries."""

    try:
        if isinstance(request, FindDriverWorkerRequest):
            validated = schemas.ExplainDriverResult.model_validate(result)
            if validated.backend != "verdi_npi":
                return None
        elif isinstance(request, FindLoadsWorkerRequest):
            validated = schemas.FindSignalLoadsResult.model_validate(result)
            if validated.backend != "verdi_npi":
                return None
        else:
            validated = schemas.TraceSignalPathResult.model_validate(result)
            if validated.backend != "verdi_npi":
                return None
    except (ValidationError, TypeError, ValueError):
        return None
    return validated.model_dump(
        exclude_none=True,
        exclude={"backend_status"},
    )


def parse_worker_request_bytes(data: bytes) -> NpiWorkerRequest:
    if len(data) > _MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    raw = json.loads(data.decode("utf-8"))
    return _REQUEST_ADAPTER.validate_python(raw)


def write_worker_response(path: Path, response: NpiWorkerResponse) -> None:
    payload = _RESPONSE_ADAPTER.dump_json(response).decode("utf-8")
    if len(payload.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        response = WorkerError(error_code="result_invalid", stage="response")
        payload = _RESPONSE_ADAPTER.dump_json(response).decode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".response_",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_worker_response(path: Path) -> NpiWorkerResponse | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_RESPONSE_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _RESPONSE_ADAPTER.validate_python(raw)
    except (OSError, UnicodeError, ValueError, ValidationError, json.JSONDecodeError):
        return None


def _write_private_text(path: Path, text: str) -> None:
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _failed_execution(reason: str) -> LsfExecutionResult:
    return LsfExecutionResult(
        result=None,
        scheduler_status="failed",
        worker_status="failed",
        fallback_reason=reason,
    )


def _worker_failed_execution() -> LsfExecutionResult:
    return LsfExecutionResult(
        result=None,
        scheduler_status="completed",
        worker_status="failed",
        fallback_reason="npi_lsf_worker_failed",
    )


def _attach_execution_receipt(
    result: dict[str, Any],
    mode: str,
    outcome: LsfExecutionResult,
    *,
    kdb_path: str,
) -> dict[str, Any]:
    result[NPI_EXECUTION_STATUS_KEY] = {
        "execution_mode": mode,
        "scheduler_status": outcome.scheduler_status,
        "worker_status": outcome.worker_status,
    }
    load_quality = outcome.kdb_load_quality
    if load_quality is None:
        load_quality = (
            "degraded" if kdb_has_elaboration_errors(kdb_path) else "clean"
        )
    error_count: int | None = None
    error_log: str | None = None
    if load_quality == "degraded":
        error_count, error_log = read_kdb_elab_error_metadata(kdb_path)
    return {
        "load_quality": load_quality,
        "error_count": error_count,
        "error_log": error_log,
    }


def _tag_fallback(
    result: dict[str, Any],
    reason: str,
    mode: str,
    outcome: LsfExecutionResult | None,
) -> dict[str, Any]:
    result.setdefault("_npi_fallback_reason", reason)
    result[NPI_EXECUTION_STATUS_KEY] = {
        "execution_mode": mode,
        "scheduler_status": (
            outcome.scheduler_status if outcome is not None else "not_started"
        ),
        "worker_status": (
            outcome.worker_status if outcome is not None else "not_started"
        ),
    }
    return result
