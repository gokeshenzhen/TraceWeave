"""First-query backend selection for an immutable independent design context.

Only metadata is retained. A candidate fingerprint is not a successful load,
license receipt, source association, or permission to reuse native handles.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import time

import config
from .cancellation import check_cancelled
from .compile_input_capture import CaptureAborted, CaptureLimits, CompileInputCapture
from .design_context import DesignContext
from .kdb_anchor import KdbAnchor, KdbBindingChanged, capture_kdb_anchor, validate_query_anchor
from .verdi_backend import bounded_kdb_discovery, probe_verdi_backend


_PARTIAL_PROBE = {
    "byte_limit", "file_limit", "reference_limit", "input_not_regular",
    "kdb_discovery_entry_limit", "kdb_setup_byte_limit",
}
_RUNTIME_ENV = ("VERDI_HOME", "NOVAS_HOME", "SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE")


class ExecutionBindingChanged(RuntimeError):
    def __init__(self):
        super().__init__("design_context_execution_changed")


@dataclass(frozen=True)
class DesignBackendBinding:
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
        if self.anchor is not None and not self.anchor.current(timeout_sec=timeout_sec):
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


def prepare_backend_binding(context: DesignContext, *, timeout_sec: float = 30.0) -> DesignBackendBinding:
    """Discover once in a worker; callers validate source identity and publish.

    A missing candidate is frozen too: producing a KDB later requires a new
    context. Partial probe budgets permit an honest source fallback; timeout,
    cancellation or changing inputs must abort the call instead.
    """
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
    environment = tuple((key, os.environ.get(key)) for key in _RUNTIME_ENV)
    allow_degraded = config.NPI_ALLOW_DEGRADED_KDB
    status = {"simulator": context.simulator, "kdb_path": None, "kdb_flow": "none",
              "kdb_validation_status": "unavailable"}
    anchor = None
    gaps: set[str] = set()
    metrics = {"discovery_ms": 0.0, "discovery_read_bytes": 0,
               "discovery_file_count": 0, "discovery_entry_count": 0, "anchor_ms": 0.0}
    disposition = "unavailable"
    if context.backend_preference == "source" or route.mode == "source_graph":
        disposition = "source_policy"
        status["_npi_selection_reason"] = "source_backend_requested"
    else:
        probe_started = time.monotonic()
        if context.requested_kdb_path is not None:
            status.update(kdb_path=context.requested_kdb_path, kdb_flow="explicit")
        else:
            with CompileInputCapture(CaptureLimits(timeout_sec=remaining())) as capture:
                with bounded_kdb_discovery(capture, top_hint=context.selected_top) as discovery:
                    try:
                        status = probe_verdi_backend(
                            context.compile_result(), context.ordered_compile_logs[0],
                        )
                        if not capture.validate():
                            raise CaptureAborted("input_changed")
                    except CaptureAborted as exc:
                        if exc.code not in _PARTIAL_PROBE:
                            raise
                        gaps.add("kdb_probe_" + exc.code)
                        disposition = "probe_partial"
                        status["_npi_selection_reason"] = "npi_kdb_probe_incomplete"
                    metrics.update(discovery_read_bytes=capture.read_bytes_count,
                                   discovery_file_count=len(capture.records),
                                   discovery_entry_count=discovery["entries"])
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
    return DesignBackendBinding(
        context.identity_sha256, context.backend_preference, anchor,
        json.dumps(status, sort_keys=True), execution, route, environment,
        allow_degraded, disposition, tuple(sorted(gaps)), tuple(metrics.items()),
    )
