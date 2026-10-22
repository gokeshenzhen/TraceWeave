"""Native-first structural orchestration under one semantic request deadline."""
from __future__ import annotations

import time

import anyio
import config

from .cancellation import OperationCancelled, check_cancelled
from .compile_input_capture import CaptureAborted
from .connectivity_backend import select_backend
from .design_backend_binding import ExecutionBindingChanged, prepare_native_binding
from .kdb_anchor import KdbBindingChanged
from .native_query import NATIVE_IDENTITY_ERRORS, NativeInputChanged, RequestBudget, read_native_inputs
from .npi_hierarchy_source import NpiHierarchyUnavailable
from .npi_structural_scan import query_native_scan, scan_budget
from .structural_semantic_router import NPI_PORT_CATEGORIES, combine_semantics, missing_categories


def unavailable(options, reason, *, status='unavailable', execution=None):
    return dict(status=status, scope=options['scope'], facts=[], total_facts=0,
                categories_checked=[], gaps=[reason],
                category_results={c: {'status': status, 'gaps': [reason]} for c in options['categories']},
                native_execution=execution or {})


class StructuralNativeRuntime:
    """Retain at most one validated local backend; never retain Language handles.

    The synchronous local owner stays on the dispatch thread. Only discovery,
    validation and LSF transport use cancellable workers, without a wave lock.
    A cached loaded design still requires fresh metadata/KDB/policy validation.
    """
    def __init__(self):
        self._backend_key = self._backend = None

    async def scan(self, *, mode, options, compile_log, simulator, source_scan,
                   run_thread, top_hint=None, metadata_guard=None):
        if mode == 'fast':
            return dict(status='not_run', gaps=['fast_mode'])
        budget = RequestBudget(options['timeout_sec'])
        inputs = binding = native = source = None
        attempts = []
        execution = config.get_npi_execution_config()
        route = config.get_connectivity_route_config()
        backend = key = None

        async def validate():
            budget.remaining()
            if config.get_npi_execution_config() != execution or config.get_connectivity_route_config() != route:
                raise ExecutionBindingChanged()
            if inputs is not None:
                await run_thread(lambda: inputs.require_current(timeout_sec=budget.remaining()))
            if binding is not None:
                await run_thread(lambda: binding.require_current(timeout_sec=budget.remaining(5)))
            if metadata_guard is not None:
                metadata_guard()
            budget.remaining()

        try:
            with anyio.fail_after(budget.remaining()):
                if set(options['categories']) & NPI_PORT_CATEGORIES:
                    try:
                        inputs = await run_thread(lambda: read_native_inputs(
                            dict(compile_log=compile_log, simulator=simulator, top_hint=top_hint),
                            timeout_sec=budget.remaining()))
                    except (OSError, ValueError):
                        native = unavailable(options, 'npi_compile_metadata_unavailable')
                    if inputs is None:
                        pass
                    elif inputs.selected_top is None:
                        native = unavailable(options, 'npi_top_unresolved')
                    else:
                        binding = await run_thread(lambda: prepare_native_binding(inputs, timeout_sec=budget.remaining()))
                        await validate()
                        if not binding.npi_candidate:
                            reason = binding.backend_status().get('_npi_selection_reason', 'npi_kdb_unavailable')
                            native = unavailable(options, reason)
                        else:
                            key = (binding.anchor.identity_sha256, inputs.selected_top,
                                   binding.execution, binding.route, binding.runtime_environment, binding.allow_degraded)
                            if self._backend_key == key:
                                backend = self._backend
                            else:
                                backend = select_backend(binding.backend_status())
                            backend.bind_native_inputs(inputs); backend.bind_kdb_anchor(binding.anchor)
                            native_budget = scan_budget(options, deadline=budget.deadline)
                            try:
                                await validate()
                                if execution.mode == 'lsf' or not execution.valid:
                                    from .npi_structural_lsf import query_scan_over_lsf
                                    native = await run_thread(lambda: query_scan_over_lsf(backend, inputs, options, native_budget))
                                else:
                                    # Preserve the existing synchronous native-owner boundary.
                                    native = query_native_scan(backend, options, top=inputs.selected_top, budget=native_budget)
                                    native['native_execution'] = dict(execution_mode='local', worker_status='completed', scheduler_status='not_applicable')
                                await anyio.lowlevel.checkpoint()
                                await validate()
                            except NpiHierarchyUnavailable as exc:
                                if exc.reason in NATIVE_IDENTITY_ERRORS:
                                    raise NativeInputChanged(exc.reason) from exc
                                await validate()
                                native = unavailable(options, exc.reason, execution=getattr(exc, 'execution_status', None))
                            except (OperationCancelled, KdbBindingChanged, NativeInputChanged, ExecutionBindingChanged, CaptureAborted, TimeoutError):
                                raise
                            except ValueError as exc:
                                if execution.mode == 'lsf':
                                    raise NativeInputChanged('npi_lsf_result_invalid') from exc
                                await validate()
                                native = unavailable(options, 'npi_scan_failed')
                            except Exception:
                                await validate()
                                native = unavailable(options, 'npi_scan_failed')
                    if inputs is not None:
                        native['selected_top'] = inputs.selected_top
                        native['checked_scope'] = ((options['scope'] or inputs.selected_top)
                            if native['status'] == 'complete' or native.get('instances_visited', 0) > 0 else None)
                    attempts.append(dict(backend='verdi_npi', status=native['status'], reason=(native.get('gaps') or ['completed'])[0]))
                else:
                    native = unavailable(options, 'npi_category_unsupported', status='unsupported')
                    attempts.append(dict(backend='verdi_npi', status='not_run', reason='npi_category_unsupported'))
                await validate()
                missing = missing_categories(options['categories'], native)
                if missing:
                    source = await source_scan(missing, budget.deadline, validate)
                    await validate()
                    attempts.append(dict(backend='slang', status=source['status'], reason=(source.get('gaps') or ['completed'])[0]))
                await anyio.lowlevel.checkpoint()
                await validate()
                result = combine_semantics(options, native, source, attempts=attempts,
                                           elapsed_ms=(time.monotonic()-budget.started)*1000)
                if binding is not None:
                    result.setdefault('metrics', {}).update(dict(binding.metrics))
                # Publish loaded-backend reuse only after every final validation.
                if backend is not None and native['status'] in {'complete', 'partial'} and execution.mode == 'local':
                    self._backend_key, self._backend = key, backend
                elif backend is not None and self._backend is backend:
                    # Cleanup/load failures may poison this Python backend.
                    # A later request gets a fresh bounded native attempt.
                    self._backend_key = self._backend = None
                return result
        except OperationCancelled:
            raise
        except KdbBindingChanged:
            reason = 'kdb_identity_changed'
        except NativeInputChanged:
            reason = 'compile_metadata_or_native_identity_changed'
        except ExecutionBindingChanged:
            reason = 'npi_execution_policy_changed'
        except CaptureAborted as exc:
            reason = 'semantic_timeout' if exc.code == 'timeout' else 'compile_metadata_changed' if exc.code == 'input_changed' else 'semantic_capture_' + exc.code
        except TimeoutError:
            reason = 'semantic_timeout'
        # Terminal failures discard cross-identity/late facts and never start source fallback.
        self._backend_key = self._backend = None
        result = combine_semantics(options, unavailable(options, reason), attempts=attempts,
                                   elapsed_ms=(time.monotonic()-budget.started)*1000)
        result['termination_reason'] = reason
        return result
