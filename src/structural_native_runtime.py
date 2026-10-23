"""Native-first structural orchestration under one semantic request deadline."""
from __future__ import annotations

import time

import anyio
import config

from .cancellation import OperationCancelled, check_cancelled
from .compile_input_capture import CaptureAborted
from .connectivity_backend import select_backend
from .design_backend_binding import ExecutionBindingChanged, prepare_native_binding
from .design_context_runtime import ContextUnavailable
from .kdb_anchor import KdbBindingChanged
from .native_query import NATIVE_IDENTITY_ERRORS, NativeInputChanged, RequestBudget, read_native_inputs
from .npi_hierarchy_source import NpiHierarchyUnavailable
from .npi_structural_scan import CAPABILITY_VERSION, query_native_scan, scan_budget
from .structural_semantic_router import NPI_CATEGORIES, combine_semantics, missing_categories
from .structural_scan_continuation import ScanContinuationInvalid, ScanContinuationStore


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
        self.continuations = ScanContinuationStore()

    async def scan(self, *, mode, options, compile_log, simulator, source_scan,
                   run_thread, top_hint=None, metadata_guard=None, continuation=None):
        if mode == 'fast':
            return dict(status='not_run', gaps=['fast_mode'])
        budget = RequestBudget(options['timeout_sec'])
        inputs = binding = native = source = None
        attempts = []
        execution = config.get_npi_execution_config()
        route = config.get_connectivity_route_config()
        backend = key = None
        state = continuation_key = None

        async def validate():
            budget.remaining()
            if continuation is not None and continuation_key is not None:
                self.continuations.check(continuation, continuation_key)
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
                if set(options['categories']) & NPI_CATEGORIES:
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
                            continuation_key = (key, getattr(inputs, 'compile_metadata_identity_sha256', None),
                                tuple(getattr(inputs, 'ordered_compile_logs', ())), options['scope'],
                                tuple(options['categories']), CAPABILITY_VERSION)
                            if continuation is not None:
                                state = self.continuations.get(continuation, continuation_key)
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
                                    extra = {'state': state} if state is not None else {}
                                    native = await run_thread(lambda: query_scan_over_lsf(backend, inputs, options, native_budget, **extra))
                                else:
                                    # Preserve the existing synchronous native-owner boundary.
                                    extra = {'state': state} if state is not None else {}
                                    native = query_native_scan(backend, options, top=inputs.selected_top, budget=native_budget, **extra)
                                    native['native_execution'] = dict(execution_mode='local', worker_status='completed', scheduler_status='not_applicable')
                                await anyio.lowlevel.checkpoint()
                                await validate()
                            except NpiHierarchyUnavailable as exc:
                                if exc.reason in NATIVE_IDENTITY_ERRORS:
                                    raise NativeInputChanged(exc.reason) from exc
                                await validate()
                                native = unavailable(options, exc.reason, execution=getattr(exc, 'execution_status', None))
                            except (OperationCancelled, KdbBindingChanged, NativeInputChanged, ExecutionBindingChanged, ContextUnavailable, CaptureAborted, TimeoutError):
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
                if continuation is not None and (native is None or '_scan_state' not in native):
                    raise ScanContinuationInvalid('native_scan_continuation_unavailable')
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
                # No token is consumed or published before final deadline,
                # identity and policy checks. Cancellation preserves retries.
                budget.remaining()
                if (native or {}).get('scan_progress', {}).get('status') == 'available':
                    result['continuation'] = self.continuations.publish(
                        continuation, continuation_key, native['_scan_state'])
                    arguments = dict(compile_log=compile_log, simulator=simulator, analysis_mode=mode,
                        semantic_categories=options['categories'], semantic_timeout_sec=options['timeout_sec'],
                        semantic_max_rss_mib=options['max_rss_mib'], semantic_continuation=result['continuation'])
                    arguments.update({'semantic_' + k: v for k, v in options['limits'].items() if k != 'max_width'})
                    if top_hint is not None:
                        arguments['top_hint'] = top_hint
                    if options['scope'] is not None:
                        arguments['semantic_scope'] = options['scope']
                    result['suggested_next_action'] = dict(tool='scan_structural_risks', arguments=arguments)
                elif continuation is not None:
                    self.continuations.discard(continuation)
                return result
        except OperationCancelled:
            raise
        except KdbBindingChanged:
            reason = 'kdb_identity_changed'
        except NativeInputChanged:
            reason = 'compile_metadata_or_native_identity_changed'
        except ExecutionBindingChanged:
            reason = 'npi_execution_policy_changed'
        except ContextUnavailable as exc:
            reason = exc.code
        except CaptureAborted as exc:
            reason = 'semantic_timeout' if exc.code == 'timeout' else 'compile_metadata_changed' if exc.code == 'input_changed' else 'semantic_capture_' + exc.code
        except TimeoutError:
            reason = 'semantic_timeout'
        except ScanContinuationInvalid as exc:
            reason = str(exc)
        # Terminal failures discard cross-identity/late facts and never start source fallback.
        self._backend_key = self._backend = None
        result = combine_semantics(options, unavailable(options, reason), attempts=attempts,
                                   elapsed_ms=(time.monotonic()-budget.started)*1000)
        result['termination_reason'] = reason
        return result
