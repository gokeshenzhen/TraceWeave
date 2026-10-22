"""Native-first orchestration over the existing public query engines."""
from __future__ import annotations

from dataclasses import dataclass
import time

import anyio

from .compile_input_capture import CaptureAborted
from .design_backend_binding import ExecutionBindingChanged, prepare_native_binding
from .design_context_runtime import ContextUnavailable
from .hierarchy_source_codec import validate_source_result
from .hierarchy_source_ir import HierarchyQueryBudget
from .hierarchy_source_request import HIERARCHY_SOURCE_TOOLS, prepare_native_source_query
from .hierarchy_source_service import finish_hierarchy_query
from .kdb_anchor import KdbBindingChanged
from .native_query import (
    NativeFallback, NativeInputChanged, RequestBudget, ROUTE_FIELDS, read_native_inputs, validate_native_business,
)


@dataclass
class NativeEntry:
    context: object
    binding: object
    npi_failure: str | None = None
    native: bool = True
    deadline: float | None = None
    metrics: dict | None = None


def mark_native_locations(result):
    """Raw native paths remain evidence without claiming local content binding."""
    classes = "roots" in result or result.get("section") == "class_hierarchy_full"
    pending = list(result.get("hits", ()))
    if result.get("node") is not None:
        pending.append(result["node"])
    pending.extend(result.get("roots", ()))
    if result.get("section") in {"component_tree_full", "class_hierarchy_full"}:
        data = result.get("data")
        pending.extend(data if isinstance(data, list) else [data] if data else [])
    while pending:
        row = pending.pop()
        gaps = set(row.get("gaps", ()))
        if row.get("source_file"):
            gaps.add("npi_class_location_unbound" if classes else "npi_instance_location_unbound")
        if row.get("definition_file"):
            gaps.add("npi_definition_location_unbound")
        if row.get("source_file") or row.get("definition_file"):
            gaps.add("native_source_content_unverified")
        row["gaps"] = sorted(gaps)
        pending.extend(row.get("children", ()))


async def dispatch_native_query(s, name, arguments):
    started = time.monotonic()
    metrics = dict.fromkeys(s.schemas.NativeRouteMetrics.model_fields, 0)
    inputs = binding = None
    source_status, handle, source_identity = "not_required", None, None
    budget = None
    store = s._design_context_store
    runtime, memory = s._source_fallback_runtime, s._native_failure_memory

    def receipt():
        metrics["total_ms"] = (time.monotonic() - started) * 1000
        return s.schemas.NativeRouteReceipt(
            input_validation=("kdb_only" if inputs and not inputs.ordered_compile_logs else
                "compile_metadata_and_kdb" if binding and binding.anchor else "compile_metadata_only"),
            compile_metadata_identity_sha256=inputs.compile_metadata_identity_sha256 if inputs else None,
            kdb_identity_sha256=binding.anchor.identity_sha256 if binding and binding.anchor else None,
            source_context_status=source_status, source_design_handle=handle,
            source_context_identity_sha256=source_identity, metrics=metrics)

    def attach(result):
        route = receipt()
        code = result.get("error_code") if isinstance(result, dict) else getattr(result, "error_code", None)
        if inputs and inputs.ordered_compile_logs and code in {"design_context_changed", "handle_expired"}:
            recovery = {"suggested_call": {"tool": "prepare_design_context", "arguments": {
                "compile_log": inputs.ordered_compile_logs[0],
                "supplementary_compile_logs": list(inputs.ordered_compile_logs[1:]),
                "simulator": inputs.simulator, "top_hint": inputs.selected_top,
                "backend_preference": "source"}}}
            if isinstance(result, dict):
                result["fallback"] = recovery
            else:
                result = result.model_copy(update={"fallback": recovery})
        if isinstance(result, dict):
            result["native_route"] = route.model_dump(mode="json")
            return result
        return result.model_copy(update={"native_route": route})

    async def validate():
        budget.remaining()
        phase = time.monotonic()
        await s._run_in_cancellable_thread(lambda: inputs.require_current(timeout_sec=budget.remaining()))
        metrics["metadata_ms"] += (time.monotonic() - phase) * 1000
        phase = time.monotonic()
        if binding:
            await s._run_in_cancellable_thread(lambda: binding.require_current(timeout_sec=budget.remaining(5)))
        metrics["kdb_validation_ms"] += (time.monotonic() - phase) * 1000
        s._require_design_store_owner(store)
        budget.remaining()

    try:
        budget = RequestBudget(arguments.get("timeout_sec", 30), started=started)
        validate_native_business(name, arguments)
        with anyio.fail_after(budget.remaining()):
            phase = time.monotonic()
            inputs = await s._run_in_cancellable_thread(
                lambda: read_native_inputs(arguments, timeout_sec=budget.remaining()))
            metrics["metadata_ms"] = (time.monotonic() - phase) * 1000
            business = {k: v for k, v in arguments.items() if k not in ROUTE_FIELDS}
            if name == "trace_x_source":
                for key in ("time_ps", "history_start_ps"):
                    if key in business:
                        business[key] = s._resolve_time(business[key])
            plan = None
            if name in HIERARCHY_SOURCE_TOOLS:
                if not inputs.ordered_compile_logs and (name in {"lookup_tb_files", "get_tb_file_detail"}
                    or name == "dump_tb_section" and arguments.get("section") in {
                        "compile_result", "include_tree", "filelist_tree", "files_full", "interfaces"}):
                    return attach(s.schemas.ToolErrorResult(error="compile_log is required for compile ownership.",
                        error_code="compile_metadata_required"))
                hierarchy_budget = HierarchyQueryBudget(timeout_sec=budget.remaining())
                hierarchy_budget.started, hierarchy_budget.deadline = started, budget.deadline
                plan = prepare_native_source_query(inputs, name, business, budget=hierarchy_budget)
                if not plan.needs_provider:
                    await validate()
                    raw = plan.execute()
                    await validate()
                    finish_hierarchy_query(raw, hierarchy_budget)
                    raw = validate_source_result(name, raw)
                    budget.remaining()
                    return attach(raw)
            if inputs.selected_top is None:
                return attach(s.schemas.ToolErrorResult(error="Select an unambiguous top_hint.",
                    error_code="compile_tops_unresolved"))
            if name in {"analyze_failure_event", "recommend_failure_debug_next_steps"} and inputs.simulator == "auto":
                return attach(s.schemas.ToolErrorResult(
                    error="KDB-only log consumers require an explicit simulator.",
                    error_code="simulator_required"))
            phase = time.monotonic()
            binding = await s._run_in_cancellable_thread(
                lambda: prepare_native_binding(inputs, timeout_sec=budget.remaining()))
            binding_metrics = dict(binding.metrics)
            metrics["kdb_discovery_ms"] = binding_metrics["discovery_ms"]
            metrics["kdb_validation_ms"] = binding_metrics["anchor_ms"]
            metrics["cached_kdb_source_read_bytes"] = binding_metrics.get("cached_source_read_bytes", 0)
            failure_key = memory.key(inputs, binding)
            entry = NativeEntry(inputs, binding, memory.get(failure_key, retry=arguments.get("retry_npi", False)), deadline=budget.deadline, metrics=metrics)
            await validate()
            resolved = {**business, "compile_log": inputs.ordered_compile_logs[0] if inputs.ordered_compile_logs else None,
                "simulator": inputs.simulator, "top_hint": inputs.selected_top,
                "_native_query": entry, "_design_query_started": started}
            if name != "trace_x_source" or arguments.get("mode", "snapshot") == "history":
                resolved["timeout_sec"] = arguments.get("timeout_sec", 30)
            prior = []
            phase = time.monotonic()
            try:
                if name in HIERARCHY_SOURCE_TOOLS:
                    result = await s._query_prepared_hierarchy(entry, name, business, plan.budget, plan=plan)
                else:
                    result = await s._dispatch_inner(name, resolved)
            except NativeFallback as fallback:
                await validate()
                prior = fallback.attempts
                trace_budget = fallback.budget if name == "trace_x_source" else None
                if trace_budget is not None:
                    trace_budget.restart("npi_source_context_fallback")
                memory.note(failure_key, prior)
                if not inputs.ordered_compile_logs:
                    source_status = "unavailable"
                    return attach(s.schemas.ToolErrorResult(error="Supply compile_log for source fallback.",
                        error_code="source_context_required"))
                phase = time.monotonic()
                handle, source_status, count, read_bytes = await runtime.resolve(
                    inputs, store, budget, s._run_in_cancellable_thread, s._require_design_store_owner)
                metrics.update(source_prepare_ms=(time.monotonic() - phase) * 1000,
                    source_prepare_count=count, source_capture_read_bytes=read_bytes)
                # Pin before the next await; strict dispatch retains its own pin
                # through source execution, cleanup and final content checks.
                with store.pin(handle) as source:
                    source_identity = source.context.identity_sha256
                    await validate()
                    source_args = {**business, "design_handle": handle,
                        "timeout_sec": arguments.get("timeout_sec", 30)}
                    if name == "trace_x_source" and arguments.get("mode", "snapshot") == "snapshot":
                        source_args.pop("timeout_sec")
                    result = await s._dispatch_design_query(name, source_args, started=started, deadline=budget.deadline,
                        trace_budget=trace_budget, publish=False,
                        failure_events_observation=fallback.failure_events_observation)
                    status = result.get("backend_status") if isinstance(result, dict) else getattr(result, "backend_status", None)
                    if status is not None:
                        if not isinstance(status, dict):
                            status = status.model_dump(mode="json", exclude_unset=True)
                        status["attempted_backends"] = prior + [a for a in status.get("attempted_backends", ()) if a["backend"] != "verdi_npi"]
                        status["selected_backend"] = "verdi_npi" if binding.npi_candidate else "source_graph"
                        if name == "trace_x_source" and trace_budget is None:
                            status["whole_trace_restart_count"] = status.get("whole_trace_restart_count", 0) + 1
                            status["whole_trace_restart_reasons"] = ["npi_source_context_fallback", *status.get("whole_trace_restart_reasons", [])]
                            result = result.model_copy(update={"trace_restarted": True})
                        if isinstance(result, dict):
                            result["backend_status"] = status
                        else:
                            result = result.model_copy(update={"backend_status": s.schemas.BackendStatus.model_validate(status)})
                            if getattr(result, "history", None) is not None:
                                result.history.context["backend_status"] = dict(status)
                        query_receipt = status.get("design_context") or {}
                        rm = query_receipt.get("metrics") or {}
                        metrics["source_validation_ms"] = rm.get("source_validation_ms", 0) + rm.get("final_validation_ms", 0)
            except (ValueError, TypeError, KeyError):
                await validate()
                raise
            await validate()
            await anyio.lowlevel.checkpoint()
            s._require_design_store_owner(store)
            budget.remaining()
            code = result.get("error_code") if isinstance(result, dict) else getattr(result, "error_code", None)
            if code == "design_context_changed":
                source_status = "changed"
            elif code == "handle_expired":
                source_status = "expired"
            if name == "recommend_failure_debug_next_steps" and not code:
                # Publication follows all native and source identity checks.
                s._result_cache[name] = result
                provenance = s._build_result_provenance(name, arguments, result)
                provenance.update(simulator=inputs.simulator,
                    compile_log=inputs.ordered_compile_logs[0] if inputs.ordered_compile_logs else None,
                    compile_metadata_identity_sha256=inputs.compile_metadata_identity_sha256,
                    design_handle=handle, context_identity_sha256=source_identity)
                s._result_provenance[name] = provenance
            return attach(result)
    except KdbBindingChanged:
        code = "kdb_identity_changed"
    except NativeInputChanged:
        code = "compile_metadata_changed"
    except ExecutionBindingChanged:
        code = "design_context_execution_changed"
    except ContextUnavailable as exc:
        code = exc.code
        if code == "handle_expired":
            source_status = "expired"
        elif code == "design_context_changed":
            source_status = "changed"
        elif code in {"context_capacity", "source_fallback_busy", "source_context_required"}:
            source_status = "unavailable"
    except CaptureAborted as exc:
        code = "compile_metadata_changed" if exc.code == "input_changed" else "native_query_" + exc.code
    except TimeoutError:
        code = "native_query_timeout"
    except OSError:
        code = "compile_metadata_unavailable"
    except (ValueError, TypeError, KeyError) as exc:
        code = "invalid_native_query_arguments"
    return attach(s.schemas.ToolErrorResult(error="Native query did not complete: " + code, error_code=code))
