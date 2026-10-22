"""Exact NPI hierarchy/source work on an explicitly selected execution node.

The context is a captured observation, not authority to parse or rebuild it.
Shared inputs are validated before and after querying one Language owner. This
module never calls discovery, a hierarchy builder, Slang, or a fallback backend.
"""
from __future__ import annotations

import hashlib
import time

from .cancellation import OperationCancelled, check_cancelled
from .compile_input_capture import CaptureAborted
from .design_context_codec import decode_design_context
from .hierarchy_source_codec import validate_source_result
from .hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget
from .hierarchy_source_request import prepare_source_query
from .hierarchy_source_service import finish_hierarchy_query
from .kdb_anchor import KdbBindingChanged, capture_kdb_anchor, validate_query_anchor
from .npi_hierarchy_source import NpiHierarchyUnavailable


# Worker counters are constrained by the REMAINING parent budget. The parent
# also charges its own preparation; it must not replace it with these counters.
SOURCE_WORK_COUNTERS = {
    "scope_count": ("scope_count", "max_scopes", "scope_limit"),
    "native_call_count": ("call_count", "max_calls", "native_call_limit"),
    "symbol_count": ("symbol_count", "max_symbols", "symbol_limit"),
    "file_count": ("file_count", "max_files", "file_limit"),
    "estimated_fact_bytes": ("fact_bytes", "max_bytes", "fact_byte_limit"),
}


class SourceContextInvalid(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require_context(context, *, timeout_sec=30.0):
    try:
        if not context.current(timeout_sec=timeout_sec):
            raise SourceContextInvalid("context_identity_changed")
    except CaptureAborted as exc:
        code = "context_identity_timeout" if exc.code == "timeout" else "context_identity_changed"
        raise SourceContextInvalid(code) from exc


def validate_source_worker_result(request, result):
    """Reject foreign identities and other backends at both worker boundaries."""
    payload = validate_source_result(request.tool, result)
    if request.tool == "resolve_packed_fields" and "source_signal" in payload:
        if (payload["source_signal"] != request.arguments["source_signal"] or
                payload["status"] == "resolved" and set(payload["field_bits"]) != set(request.arguments["fields"])):
            raise ValueError("worker packed query mismatch")
    from .npi_lsf import NativeSourceWorkerRequest
    native = isinstance(request, NativeSourceWorkerRequest)
    if native:
        from .native_query_codec import decode_native_inputs
        inputs = decode_native_inputs(request.metadata_payload)
        identity = inputs.compile_metadata_identity_sha256 or "unavailable"
        if "context_identity_sha256" in payload:
            raise ValueError("native worker cannot claim a source context")
    else:
        identity = request.context_payload["context"]["identity_sha256"]
    if native:
        arguments = request.arguments
        if request.tool == "find_tb_instance":
            for row in payload.get("hits", ()):
                if (arguments.get("path") and row["path"] != arguments["path"] or
                    arguments.get("module") and row.get("module") != arguments["module"]):
                    raise ValueError("worker instance query mismatch")
        if request.tool == "get_tb_subtree" and "root" in payload:
            if payload["root"] != (arguments.get("root") or request.top):
                raise ValueError("worker subtree root mismatch")
        if request.tool == "dump_tb_section" and payload.get("section") != arguments["section"]:
            raise ValueError("worker section mismatch")
        if request.tool == "get_tb_file_detail" and "file_id" in payload:
            from .hierarchy_source_metadata import build_native_source_catalog
            catalog = build_native_source_catalog(inputs)
            entry = catalog.find_file(arguments["path"])
            if entry is None or payload["file_id"] != entry.file_id or payload["path"] != entry.path:
                raise ValueError("worker file mismatch")
    if not native and payload.get("context_identity_sha256") != identity:
        raise ValueError("worker source context mismatch")
    if native and "ownership_identity" in payload and payload.get("ownership_basis") != "compile_metadata":
        raise ValueError("native ownership basis mismatch")
    if payload.get("ownership_identity", identity) != identity:
        raise ValueError("worker compile ownership mismatch")
    if any(key in payload for key in ("handle", "design_handle", "design_context", "backend_status", "native_route")):
        raise ValueError("worker cannot publish parent handles or routing receipts")
    origin = payload["facts_origin"]
    if origin == "verdi_npi":
        expected = hashlib.sha256((request.kdb_identity_sha256 + "\0" + request.top).encode()).hexdigest()
        if payload["artifact_identity"] != expected or not payload.get("kdb_binding"):
            raise ValueError("worker NPI artifact mismatch")
    elif origin == "compile_metadata":
        if payload["artifact_identity"] != identity:
            raise ValueError("worker metadata artifact mismatch")
    else:
        raise ValueError("source worker cannot switch backend")
    binding = payload.get("kdb_binding")
    if binding is not None:
        if (binding["identity_sha256"] != request.kdb_identity_sha256
                or binding["identity_status"] != "complete" or binding["load_status"] != "loaded"
                or binding["artifact_status"] not in {"elaborated_candidate", "elaboration_error"}):
            raise ValueError("worker KDB load binding mismatch")
    for metric, (_, limit, _) in SOURCE_WORK_COUNTERS.items():
        if payload["query_metrics"][metric] > getattr(request.limits, limit):
            raise ValueError("worker exceeded transferred work limits")
    return payload


def _error(exc, *, stage, native=False):
    from .npi_lsf import WorkerError, WorkerUnavailable
    if isinstance(exc, OperationCancelled):
        raise exc
    if isinstance(exc, SourceContextInvalid):
        return WorkerError(error_code=exc.code, stage=stage)
    if isinstance(exc, KdbBindingChanged):
        return WorkerError(error_code="kdb_identity_changed", stage=stage)
    if isinstance(exc, CaptureAborted):
        code = "kdb_identity_timeout" if exc.code == "timeout" else "kdb_identity_changed"
        return WorkerError(error_code=code, stage=stage)
    if isinstance(exc, TimeoutError):
        return WorkerError(error_code="query_timeout", stage=stage)
    if isinstance(exc, NpiHierarchyUnavailable):
        from .native_query import NATIVE_IDENTITY_ERRORS
        if native and exc.reason in NATIVE_IDENTITY_ERRORS:
            return WorkerError(error_code="result_invalid", stage=stage)
        if exc.reason == "npi_load_failed":
            return WorkerUnavailable()
        return WorkerError(error_code="hierarchy_source_unavailable", stage=stage)
    return WorkerError(error_code="worker_internal_error", stage=stage)


def execute_source_worker(request, *, backend=None):
    from .npi_lsf import WorkerError, WorkerSuccess

    # Start before decoding or rebuilding the request-local metadata catalog.
    budget = HierarchyQueryBudget(**request.limits.model_dump())
    started = time.monotonic()
    try:
        from .npi_lsf import NativeSourceWorkerRequest
        native = isinstance(request, NativeSourceWorkerRequest)
        if native:
            from .native_query_codec import decode_native_inputs
            context = decode_native_inputs(request.metadata_payload)
        else:
            context = decode_design_context(request.context_payload)
        if context.selected_top != request.top or context.backend_preference != "auto":
            raise ValueError("worker requires the selected NPI context")
    except OperationCancelled:
        raise
    except (TypeError, ValueError, KeyError):
        return WorkerError(error_code="request_invalid", stage="request")
    decode_ms = (time.monotonic() - started) * 1000
    anchor, plan = None, None
    source_ms = anchor_ms = 0.0
    stage = "query"
    try:
        from .hierarchy_source_request import prepare_native_source_query
        plan = (prepare_native_source_query if native else prepare_source_query)(
            context, request.tool, request.arguments, budget=budget)
        try:
            budget.checkpoint()
            started = time.monotonic()
            _require_context(context, timeout_sec=min(30.0, max(0.001, budget.deadline - started)))
            source_ms += (time.monotonic() - started) * 1000
            budget.checkpoint()
            if not plan.needs_provider or plan.preparation_limit is not None:
                result = plan.execute()
            else:
                stage = "load"
                started = time.monotonic()
                anchor = capture_kdb_anchor(
                    request.kdb_path, max_bytes=request.kdb_identity_max_bytes,
                    timeout_sec=min(5.0, max(0.001, budget.deadline - started)),
                )
                if anchor.identity_sha256 != request.kdb_identity_sha256:
                    raise KdbBindingChanged()
                validate_query_anchor(anchor)
                anchor_ms += (time.monotonic() - started) * 1000
                budget.checkpoint()
                if backend is None:
                    from .verdi_npi_backend import VerdiNpiBackend
                    backend = VerdiNpiBackend()
                if native:
                    backend.bind_native_inputs(context)
                else:
                    backend.bind_compile_context(context.compile_result())
                backend.bind_kdb_anchor(anchor)
                stage = "query"
                # query_source acquires the exclusive owner before SDK load.
                result = backend.query_source(plan)
        except HierarchyBudgetExceeded as exc:
            result = plan._unstarted(exc.reason)

        # Mandatory final validation has its own bounded allowance, including
        # after analysis exhaustion. A late result is downgraded below; the
        # submitting parent may already have timed out and killed the job.
        started = time.monotonic()
        _require_context(context)
        source_ms += (time.monotonic() - started) * 1000
        if anchor is not None:
            started = time.monotonic()
            anchor.require_current()
            anchor_ms += (time.monotonic() - started) * 1000
        if not native:
            result["context_identity_sha256"] = context.identity_sha256
        result["query_metrics"].update(
            worker_context_decode_ms=decode_ms,
            **({"worker_metadata_validation_ms": source_ms} if native else {"worker_source_validation_ms": source_ms}),
            worker_kdb_validation_ms=anchor_ms,
        )
        stage = "result"
        finish_hierarchy_query(result, budget)
        started = time.monotonic()
        result = validate_source_worker_result(request, result)
        result["query_metrics"]["worker_result_validation_ms"] = (time.monotonic() - started) * 1000
        finish_hierarchy_query(result, budget)
        check_cancelled()
        quality = (result.get("kdb_binding") or {}).get("load_quality")
        return WorkerSuccess(result=result, kdb_load_quality=quality)
    except (OperationCancelled, SourceContextInvalid, KdbBindingChanged, CaptureAborted, TimeoutError) as exc:
        return _error(exc, stage=stage, native=native)
    except Exception as exc:
        # An ordinary query failure cannot hide source/KDB mutation and invite
        # a fallback on a different observation. Typed fatal errors above keep
        # their primary identity and are never replaced by this extra check.
        try:
            _require_context(context)
            if anchor is not None:
                anchor.require_current()
        except (OperationCancelled, SourceContextInvalid, KdbBindingChanged, CaptureAborted, TimeoutError) as failure:
            return _error(failure, stage=stage, native=native)
        if stage == "result" and isinstance(exc, (TypeError, ValueError, KeyError)):
            return WorkerError(error_code="result_invalid", stage="result")
        return _error(exc, stage=stage, native=native)
