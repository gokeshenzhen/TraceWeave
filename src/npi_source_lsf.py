"""Parent-side placement of a prepared source query on the selected LSF node."""
from __future__ import annotations

import json
import time

from .cancellation import check_cancelled
from .design_context_codec import ContextTransportLimit, encode_design_context
from .hierarchy_source_ir import HierarchyBudgetExceeded
from .hierarchy_source_request import HierarchySourceRequest
from .hierarchy_source_service import finish_hierarchy_query
from .kdb_anchor import validate_query_anchor
from .npi_hierarchy_source import NpiHierarchyUnavailable
from .npi_source_worker import SOURCE_WORK_COUNTERS, validate_source_worker_result


class LsfSourceUnavailable(NpiHierarchyUnavailable):
    """An ordinary source-provider failure with a fixed placement receipt."""

    def __init__(self, reason, *, outcome=None):
        super().__init__(reason)
        self.execution_status = {
            "execution_mode": "lsf",
            "scheduler_status": outcome.scheduler_status if outcome is not None else "not_started",
            "worker_status": outcome.worker_status if outcome is not None else "not_started",
            "fallback_reason": reason,
        }


def _remaining_limits(budget):
    budget.checkpoint()
    limits = {"max_depth": budget.max_depth}
    for counter, limit, reason in SOURCE_WORK_COUNTERS.values():
        remaining = getattr(budget, limit) - getattr(budget, counter)
        if remaining < 1:
            raise HierarchyBudgetExceeded(reason)
        limits[limit] = remaining
    remaining_time = budget.deadline - time.monotonic()
    if remaining_time <= 0:
        raise HierarchyBudgetExceeded("timeout_sec")
    limits["timeout_sec"] = min(300.0, remaining_time)
    return limits


def _merge_work(budget, metrics):
    increments = []
    for metric, (counter, limit, _) in SOURCE_WORK_COUNTERS.items():
        total = getattr(budget, counter) + metrics[metric]
        if total > getattr(budget, limit):
            raise ValueError("remote work exceeds remaining parent budget")
        increments.append((counter, total))
    for counter, total in increments:
        setattr(budget, counter, total)


def query_source_over_lsf(backend, plan):
    """Run no licensed work on the parent, including on failure or timeout.

    The caller still pins and finally validates the prepared context. Parent
    catalog preparation, encoding, queue wait, worker work, result checks and
    mandatory binding validation all contribute to the original query budget.
    """
    from .npi_lsf import (
        HierarchySourceWorkerRequest, NativeSourceWorkerRequest, NPI_EXECUTION_STATUS_KEY, SourceWorkerLimits,
        _attach_execution_receipt,
    )

    check_cancelled()
    if not isinstance(plan, HierarchySourceRequest):
        raise TypeError("a prepared hierarchy/source request is required")
    plan.validate()
    backend._last_kdb_status = None
    # This return is deliberately before accessing config, binding, transport
    # or even encoding a context. It must remain true with an unusable queue.
    if not plan.needs_provider or plan.preparation_limit is not None:
        return plan.execute()
    budget = plan.budget
    try:
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        return plan._unstarted(exc.reason)
    if plan.context.backend_preference == "source":
        raise NpiHierarchyUnavailable("npi_source_only_context")
    if getattr(backend, "_bound_compile_result", None) != plan.context.compile_result():
        raise NpiHierarchyUnavailable("npi_compile_context_mismatch")
    anchor = getattr(backend, "_bound_kdb_anchor", None)
    if anchor is None:
        raise NpiHierarchyUnavailable("npi_prepared_context_required")
    validate_query_anchor(anchor)
    if not plan.context.selected_top:
        raise ValueError("an explicit selected top is required")
    if not backend._config.valid or backend._config.mode != "lsf":
        raise LsfSourceUnavailable("npi_lsf_config_invalid")

    from .hierarchy_source_request import NativeSourceRequest
    native = isinstance(plan, NativeSourceRequest)
    started = time.monotonic()
    try:
        # Reject already exhausted work budgets before copying the transport.
        _remaining_limits(budget)
        if native:
            from .native_query_codec import encode_native_inputs
            payload = encode_native_inputs(plan.context)
        else:
            payload = encode_design_context(plan.context)
        payload_bytes = len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        request = (NativeSourceWorkerRequest if native else HierarchySourceWorkerRequest)(
            kdb_path=anchor.path, top=plan.context.selected_top,
            kdb_identity_sha256=anchor.identity_sha256, kdb_identity_max_bytes=anchor.max_bytes,
            tool=plan.operation, arguments=json.loads(plan.arguments_json),
            **({"metadata_payload": payload} if native else {"context_payload": payload}),
            limits=SourceWorkerLimits(**_remaining_limits(budget)),
        )
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        return plan._unstarted(exc.reason)
    except ContextTransportLimit as exc:
        raise LsfSourceUnavailable("design_context_transport_byte_limit") from exc
    encode_ms = (time.monotonic() - started) * 1000
    parent_preparation_ms = (time.monotonic() - budget.started) * 1000
    started = time.monotonic()
    outcome = backend._execute_bound_request(request, deadline=budget.deadline)
    round_trip_ms = (time.monotonic() - started) * 1000
    check_cancelled()
    if outcome.scheduler_status == "timed_out" or outcome.fallback_reason == "npi_lsf_timeout":
        raise TimeoutError("npi_lsf_timeout")
    if outcome.result is None:
        raise LsfSourceUnavailable(outcome.fallback_reason or "npi_lsf_worker_failed", outcome=outcome)
    started = time.monotonic()
    try:
        result = validate_source_worker_result(request, outcome.result)
        binding = result.get("kdb_binding")
        if outcome.kdb_load_quality != (binding or {}).get("load_quality"):
            raise ValueError("worker load receipt disagrees with source result")
        if outcome.scheduler_status != "completed" or outcome.worker_status != "completed":
            raise ValueError("source result requires completed worker execution")
        _merge_work(budget, result["query_metrics"])
    except (TypeError, ValueError, KeyError) as exc:
        raise LsfSourceUnavailable("npi_lsf_result_invalid", outcome=outcome) from exc
    validation_ms = (time.monotonic() - started) * 1000
    result["query_metrics"].update(
        worker_query_ms=result["query_metrics"]["query_ms"],
        context_transport_encode_ms=encode_ms, context_transport_bytes=payload_bytes,
        lsf_parent_preparation_ms=parent_preparation_ms, lsf_round_trip_ms=round_trip_ms,
        lsf_result_validation_ms=validation_ms,
    )
    if binding is not None:
        backend._last_kdb_status = _attach_execution_receipt(
            result, "lsf", outcome, kdb_path=anchor.path,
        )
    else:
        # A valid worker may exhaust preparation before loading. Do not derive
        # a load claim from filesystem markers merely because it returned data.
        result[NPI_EXECUTION_STATUS_KEY] = {
            "execution_mode": "lsf", "scheduler_status": "completed", "worker_status": "completed",
        }
    return finish_hierarchy_query(result, budget)
