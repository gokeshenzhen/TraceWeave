"""First-query backend selection for an immutable independent design context.

Only metadata is retained. A candidate fingerprint is not a successful load,
license receipt, source association, or permission to reuse native handles.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import hashlib
import os
import time

import config
from .cancellation import check_cancelled
from .compile_input_capture import CaptureAborted, CaptureLimits, CompileInputCapture, validate_captured_inputs
from .design_context import DesignContext
from .compile_session_snapshot import CompileSessionSnapshot
from .kdb_anchor import KdbAnchor, KdbBindingChanged, capture_kdb_anchor, validate_query_anchor
from .verdi_backend import bounded_kdb_discovery, probe_verdi_backend


_PARTIAL_PROBE = {
    "byte_limit", "file_limit", "reference_limit", "input_not_regular",
    "kdb_discovery_entry_limit", "kdb_setup_byte_limit",
}
_RUNTIME_ENV = ("VERDI_HOME", "NOVAS_HOME", "SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE")
_NATIVE_RUNTIME_ENV = (*_RUNTIME_ENV, "PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "TRACEWEAVE_NPI_WORKER")


class ExecutionBindingChanged(RuntimeError):
    def __init__(self):
        super().__init__("design_context_execution_changed")


class _BindingPolicy:
    def backend_status(self, *, npi_failure: str | None = None) -> dict:
        status = json.loads(self.status_json)
        status["kdb_load_status"] = "not_checked"
        if npi_failure is not None:
            # Keep artifact facts visible while withholding another licensed
            # attempt. New preparation is the explicit retry boundary.
            status["_npi_selection_reason"] = "npi_previous_load_failure"
            status["kdb_load_status"] = "failed"
        return status

    @property
    def npi_candidate(self) -> bool:
        return self.disposition == "candidate"

    def require_policy_current(self) -> None:
        """Cheap policy check immediately before placement; no filesystem I/O."""
        check_cancelled()
        if self.backend_preference != "source" and (
            self.execution != config.get_npi_execution_config()
            or self.route != config.get_connectivity_route_config()
            or self.allow_degraded != config.NPI_ALLOW_DEGRADED_KDB
            or any(os.environ.get(key) != value for key, value in self.runtime_environment)
        ):
            raise ExecutionBindingChanged()

    def require_current(self, *, timeout_sec: float = 5.0) -> None:
        self.require_policy_current()
        deadline = time.monotonic() + timeout_sec
        observations = getattr(self, "discovery_metadata", None)
        if observations is not None:
            if not validate_captured_inputs(*observations, timeout_sec=timeout_sec):
                raise CaptureAborted("input_changed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureAborted("timeout")
        if self.anchor is not None and not self.anchor.current(timeout_sec=remaining):
            raise KdbBindingChanged()
        self.require_policy_current()

    @property
    def estimated_bytes(self) -> int:
        return 4096 + 4 * len(json.dumps(asdict(self), default=str, ensure_ascii=False).encode())

    def receipt(self) -> dict:
        return {
            "selection": self.disposition,
            "artifact": self.anchor.receipt() if self.anchor is not None else None,
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class DesignBackendBinding(_BindingPolicy):
    context_identity_sha256: str
    backend_preference: str
    anchor: KdbAnchor | None
    status_json: str
    execution: config.NpiExecutionConfig
    route: config.ConnectivityRouteConfig
    runtime_environment: tuple[tuple[str, str | None], ...]
    allow_degraded: bool
    disposition: str
    gaps: tuple[str, ...]
    metrics: tuple[tuple[str, int | float], ...]


@dataclass(frozen=True)
class NativeQueryBinding(_BindingPolicy):
    backend_preference: str
    anchor: KdbAnchor | None
    status_json: str
    execution: config.NpiExecutionConfig
    route: config.ConnectivityRouteConfig
    runtime_environment: tuple[tuple[str, str | None], ...]
    allow_degraded: bool
    disposition: str
    gaps: tuple[str, ...]
    metrics: tuple[tuple[str, int | float], ...]
    discovery_metadata: tuple | None = None


def prepare_backend_binding(context: DesignContext, *, timeout_sec: float = 30.0) -> DesignBackendBinding:
    """Discover once in a worker; callers validate source identity and publish.

    A missing candidate is frozen too: producing a KDB later requires a new
    context. Partial probe budgets permit an honest source fallback; timeout,
    cancellation or changing inputs must abort the call instead.
    """
    values = _prepare_candidate_binding(
        compile_result=context.compile_result(),
        compile_log=context.ordered_compile_logs[0], simulator=context.simulator,
        selected_top=context.selected_top, backend_preference=context.backend_preference,
        requested_kdb_path=context.requested_kdb_path, timeout_sec=timeout_sec)
    values.pop("discovery_metadata", None)
    values["metrics"] = tuple((k, v) for k, v in values["metrics"] if k != "cached_source_read_bytes")
    return DesignBackendBinding(context.identity_sha256, **values)


def prepare_native_binding(inputs, *, timeout_sec=30):
    return NativeQueryBinding(**_prepare_candidate_binding(
        compile_result=inputs.compile_result(),
        compile_log=inputs.ordered_compile_logs[0] if inputs.ordered_compile_logs else None,
        simulator=inputs.simulator, selected_top=inputs.selected_top,
        backend_preference=inputs.backend_preference,
        requested_kdb_path=inputs.requested_kdb_path, timeout_sec=timeout_sec, prefer_native_candidate=True))


def _prepare_candidate_binding(*, compile_result, compile_log, simulator, selected_top,
                               backend_preference, requested_kdb_path, timeout_sec, prefer_native_candidate=False):
    CaptureLimits(timeout_sec=timeout_sec)
    started = time.monotonic()
    deadline = started + timeout_sec

    def remaining():
        check_cancelled()
        value = deadline - time.monotonic()
        if value <= 0:
            raise CaptureAborted("timeout")
        return value

    execution = config.get_npi_execution_config()
    route = config.get_connectivity_route_config()
    environment = tuple((key, os.environ.get(key)) for key in (
        _NATIVE_RUNTIME_ENV if prefer_native_candidate else _RUNTIME_ENV))
    allow_degraded = config.NPI_ALLOW_DEGRADED_KDB
    status = {"simulator": simulator if simulator != "auto" else "unknown", "kdb_path": None, "kdb_flow": "none",
              "kdb_validation_status": "unavailable"}
    anchor = None
    discovery_metadata = None
    gaps: set[str] = set()
    metrics = {"discovery_ms": 0.0, "discovery_read_bytes": 0,
               "cached_source_read_bytes": 0, "discovery_file_count": 0, "discovery_entry_count": 0, "anchor_ms": 0.0}
    disposition = "unavailable"
    if backend_preference == "source" or route.mode == "source_graph":
        disposition = "source_policy"
        status["_npi_selection_reason"] = "source_backend_requested"
    else:
        probe_started = time.monotonic()
        if requested_kdb_path is not None:
            status.update(kdb_path=requested_kdb_path, kdb_flow="explicit")
        else:
            with CompileInputCapture(CaptureLimits(timeout_sec=remaining())) as capture:
                with bounded_kdb_discovery(capture, top_hint=selected_top, prefer_native_candidate=prefer_native_candidate) as discovery:
                    try:
                        status = probe_verdi_backend(
                            compile_result, compile_log,
                        )
                        if not capture.validate():
                            raise CaptureAborted("input_changed")
                    except CaptureAborted as exc:
                        if exc.code not in _PARTIAL_PROBE:
                            raise
                        gaps.add("kdb_probe_" + exc.code)
                        disposition = "probe_partial"
                        status["_npi_selection_reason"] = "npi_kdb_probe_incomplete"
                    if prefer_native_candidate:
                        excluded = discovery["cached_source_paths"]
                        records = tuple(r for r in capture.records if r.path not in excluded)
                        unavailable = tuple((p, v) for p, v in capture.unavailable_observations
                                            if os.path.realpath(p) not in excluded)
                        metadata_paths = {r.path for r in records} | {os.path.realpath(p) for p, _ in unavailable}
                        aliases = tuple((p, v) for p, v in capture.aliases if v in metadata_paths)
                        if records or aliases or unavailable:
                            fingerprint = hashlib.sha256(json.dumps(
                                [r.identity_record() for r in records], sort_keys=True).encode()).hexdigest()
                            snapshot = CompileSessionSnapshot(records, not unavailable, (), fingerprint)
                            discovery_metadata = (snapshot, aliases, unavailable)
                    metrics.update(discovery_read_bytes=capture.read_bytes_count,
                                   discovery_file_count=len(capture.records),
                                   discovery_entry_count=discovery["entries"],
                                   cached_source_read_bytes=sum(capture._path_read_bytes.get(path, 0)
                                       for path in discovery["cached_source_paths"]))
        metrics["discovery_ms"] = (time.monotonic() - probe_started) * 1000
        remaining()
        if status.get("kdb_path"):
            anchor_started = time.monotonic()
            anchor = capture_kdb_anchor(status["kdb_path"], timeout_sec=remaining())
            metrics["anchor_ms"] = (time.monotonic() - anchor_started) * 1000
            gaps.update(anchor.gaps)
            try:
                validate_query_anchor(anchor)
            except ValueError:
                status["_npi_selection_reason"] = "npi_kdb_candidate_unusable"
                disposition = "unusable_candidate"
            else:
                if anchor.artifact_status == "elaboration_error" and not allow_degraded:
                    status["_npi_selection_reason"] = "npi_degraded_kdb_disabled"
                    disposition = "unusable_candidate"
                else:
                    disposition = "candidate"
            status["kdb_validation_status"] = (
                "elaboration_error" if anchor.artifact_status == "elaboration_error"
                else "usable" if disposition == "candidate" else "unavailable"
            )
    remaining()
    metrics["binding_ms"] = (time.monotonic() - started) * 1000
    return dict(backend_preference=backend_preference, anchor=anchor,
        status_json=json.dumps(status, sort_keys=True), execution=execution, route=route,
        runtime_environment=environment, allow_degraded=allow_degraded,
        disposition=disposition, gaps=tuple(sorted(gaps)), metrics=tuple(metrics.items()),
        discovery_metadata=discovery_metadata)
