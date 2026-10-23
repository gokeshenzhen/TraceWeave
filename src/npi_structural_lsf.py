"""Versioned structural-scan worker boundary on the existing NPI transport."""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter

from .cancellation import OperationCancelled, check_cancelled
from .compile_input_capture import CaptureAborted
from .hierarchy_source_ir import HierarchyBudgetExceeded
from .kdb_anchor import KdbBindingChanged, capture_kdb_anchor, validate_query_anchor
from .native_query import NativeInputChanged
from .native_query_codec import decode_native_inputs, encode_native_inputs
from .npi_hierarchy_source import NpiHierarchyUnavailable
from .npi_structural_scan import CAPABILITY_VERSION, query_native_scan, scan_budget
from .structural_semantic_runtime import validate_semantic_options
from .structural_semantic_router import NPI_CATEGORIES
from .structural_driver_facts import DRIVER_CATEGORIES, validate_driver_fact


def validate_scan_options(options):
    if not isinstance(options, dict) or set(options) != {'scope', 'categories', 'limits', 'timeout_sec', 'max_rss_mib'}:
        raise ValueError('invalid scan options')
    args = {'semantic_scope': options['scope'], 'semantic_categories': options['categories'],
            'semantic_timeout_sec': options['timeout_sec'], 'semantic_max_rss_mib': options['max_rss_mib']}
    limits = dict(options['limits'])
    if limits.pop('max_width', None) != 4096:
        raise ValueError('invalid scan width limit')
    args.update({'semantic_' + k: v for k, v in limits.items()})
    _, expected = validate_semantic_options(args)
    if expected != options:
        raise ValueError('noncanonical scan options')
    return options


def _fingerprint(request):
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


def validate_scan_worker_result(request, result):
    """Validate identity, requested domain, work bounds and positive fact shape."""
    if not isinstance(result, dict) or len(json.dumps(result).encode()) > 8 * 1024 * 1024:
        raise ValueError('invalid native scan result')
    expected = hashlib.sha256((request.kdb_identity_sha256 + '\0' + request.top).encode()).hexdigest()
    if (result.get('request_fingerprint') != _fingerprint(request)
            or result.get('artifact_identity') != expected or result.get('selected_top') != request.top
            or result.get('scope') != request.options['scope']
            or result.get('capability_version') != CAPABILITY_VERSION):
        raise ValueError('worker scan identity mismatch')
    allowed = {'status', 'scope', 'artifact_identity', 'capability_version', 'categories_checked',
               'category_results', 'gaps', 'facts', 'total_facts', 'counts', 'instances_visited',
               'analysis_limits', 'coverage_status', 'limitations', 'metrics', 'kdb_binding',
               'request_fingerprint', 'selected_top', 'gap_summary', '_scan_state', 'scan_progress'}
    if set(result) != allowed or result['status'] not in {'complete', 'partial'}:
        raise ValueError('invalid scan receipt fields')
    from .schemas import StructuralGapSummary
    summary = result['gap_summary']
    if not isinstance(summary, list) or len(summary) > 64:
        raise ValueError('invalid scan gap summary')
    for row in summary:
        StructuralGapSummary.model_validate(row)
        if row['affected_ports'] > row['occurrences'] or row['reason'] not in result['gaps']:
            raise ValueError('inconsistent scan gap summary')
    if sum(len(row['examples']) for row in summary) > 16:
        raise ValueError('unbounded scan gap examples')
    binding = result['kdb_binding']
    if (binding.get('identity_sha256') != request.kdb_identity_sha256 or binding.get('load_status') != 'loaded'
            or binding.get('identity_status') != 'complete' or binding.get('load_quality') not in {'clean', 'degraded'}
            or binding.get('source_association') != 'unverified'
            or binding.get('artifact_status') not in {'elaborated_candidate', 'elaboration_error'}
            or binding.get('identity_basis') != 'bounded_elaboration_maps'):
        raise ValueError('invalid scan load binding')
    categories = set(request.options['categories'])
    if set(result['category_results']) != categories or set(result['categories_checked']) != categories & NPI_CATEGORIES:
        raise ValueError('worker scan categories mismatch')
    for category, row in result['category_results'].items():
        if (set(row) != {'status', 'gaps'} or row['status'] not in {'complete', 'partial', 'unsupported'}
                or not isinstance(row['gaps'], list) or row['status'] == 'complete' and row['gaps']
                or category not in result['categories_checked'] and row['status'] != 'unsupported'):
            raise ValueError('invalid category coverage')
    if result['status'] == 'complete' and (result['gaps'] or result['analysis_limits'] or
            any(row['status'] != 'complete' for row in result['category_results'].values())):
        raise ValueError('invalid complete scan')
    from .structural_scan_continuation import validate_state
    from .schemas import StructuralScanProgress
    state = validate_state(result['_scan_state'], top=request.top,
        root=request.options['scope'] or request.top, categories=categories)
    progress = result['scan_progress']
    StructuralScanProgress.model_validate(progress)
    if (progress['completed_instances'] != state['completed_instances']
            or progress['pending_scopes'] != len(state['pending'])
            or progress['cumulative_counts'] != state['counts']
            or (progress['status'] == 'complete') != (not state['pending'])
            or result['counts'] != state['counts'] or result['total_facts'] != sum(state['counts'].values())):
        raise ValueError('inconsistent scan progress')
    facts = result['facts']
    if not isinstance(facts, list) or progress['page_facts'] != len(facts) or len(facts) > request.options['limits']['max_facts']:
        raise ValueError('invalid fact count')
    root = request.options['scope'] or request.top
    for fact in facts:
        if (fact.get('kind') not in categories & NPI_CATEGORIES
                or not isinstance(fact.get('instance'), str)
                or not (fact['instance'] == root or fact['instance'].startswith(root + '.'))
                or fact.get('source_association') != 'unverified'):
            raise ValueError('invalid native fact scope')
        if fact['kind'] in DRIVER_CATEGORIES:
            validate_driver_fact(fact)
            from .npi_structural_drivers import writer_identity
            if len(fact['target_bits']) != 1:
                raise ValueError('native scan facts must preserve the current bit position')
            for driver in fact['drivers']:
                name = driver.get('signal_path')
                if (not isinstance(name, str) or not 0 < len(name) <= 16384
                        or driver['driver_id'] != hashlib.sha256(writer_identity(name).encode()).hexdigest()
                        or driver.get('kind') not in {'instance_port','always_ff','always_comb','assign'}):
                    raise ValueError('invalid native structural writer identity')
            continue
        if (fact['instance'] == request.top or not isinstance(fact.get('port'), str) or not fact['port']
                or fact.get('origin') != 'input_binding'):
            raise ValueError('invalid native port fact')
        if fact['kind'] == 'constant_connection':
            width = fact.get('width')
            if type(width) is not int or not 1 <= width <= 4096 or not fact.get('constant_regions'):
                raise ValueError('invalid native port width')
            covered = set()
            for region in fact['constant_regions']:
                offset, n, bits = (region.get(k) for k in ('lsb_offset', 'width', 'bits'))
                if (type(offset) is not int or type(n) is not int or n < 1 or offset < 0 or offset + n > width
                        or not isinstance(bits, str) or len(bits) != n or set(bits) - set('01xz')
                        or covered & set(range(offset, offset+n))):
                    raise ValueError('invalid native constant region')
                covered.update(range(offset, offset+n))
        elif fact.get('connection_status') != 'explicit_empty' or not fact.get('source'):
            raise ValueError('invalid empty-actual evidence')
    counts = Counter((request.scan_state or {}).get('counts', {}))
    counts.update(f['kind'] for f in facts)
    if result['counts'] != dict(counts):
        raise ValueError('worker scan counters mismatch')
    metrics = result['metrics']
    allowed_metrics = {'native_port_count', 'native_traversal_ms', 'scope_count', 'native_call_count',
        'symbol_count', 'file_count', 'estimated_fact_bytes', 'query_ms', 'native_handle_count',
        'native_iterator_release_count', 'language_cleanup_ms', 'design_load_ms', 'native_driver_bit_count',
        'query_with_cleanup_validation_ms', 'native_instance_count', 'native_handle_peak', 'native_handle_batches'}
    if (set(metrics) - allowed_metrics or any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in metrics.values())
            or type(result['instances_visited']) is not int or result['instances_visited'] != metrics.get('native_instance_count')):
        raise ValueError('invalid scan work metrics')
    for key, maximum in [('native_instance_count', request.options['limits']['max_instances']),
                         ('native_call_count', 100000), ('native_port_count', 100000),
                         ('estimated_fact_bytes', 8 * 1024 * 1024), ('native_handle_count', 100000),
                         ('native_handle_peak', 16385), ('native_handle_batches', 100001)]:
        if type(metrics.get(key)) is not int or not 0 <= metrics[key] <= maximum:
            raise ValueError('worker scan exceeded work budget')
    if categories & DRIVER_CATEGORIES and (type(metrics.get('native_driver_bit_count')) is not int
            or not 0 <= metrics['native_driver_bit_count'] <= 100000):
        raise ValueError('invalid native driver work count')
    if metrics['native_handle_count'] >= 16385 and metrics['native_handle_batches'] <= 1 and (result['status'] != 'partial' or 'native_handle_limit' not in result['gaps']):
        raise ValueError('unreported native handle stop')
    return result


def execute_scan_worker(request, *, backend=None):
    from .npi_lsf import WorkerSuccess, WorkerError, WorkerUnavailable
    from .native_query import NATIVE_IDENTITY_ERRORS
    budget = scan_budget(request.options)
    def remaining():
        budget.checkpoint()
        return budget.deadline - time.monotonic()
    try:
        inputs = decode_native_inputs(request.metadata_payload)
        inputs.require_current(timeout_sec=remaining())
        anchor = capture_kdb_anchor(request.kdb_path, max_bytes=request.kdb_identity_max_bytes, timeout_sec=remaining())
        if anchor.identity_sha256 != request.kdb_identity_sha256:
            raise KdbBindingChanged()
        validate_query_anchor(anchor)
        if backend is None:
            from .verdi_npi_backend import VerdiNpiBackend
            backend = VerdiNpiBackend()
        backend.bind_native_inputs(inputs); backend.bind_kdb_anchor(anchor)
        extra = {'state': request.scan_state} if request.scan_state is not None else {}
        result = query_native_scan(backend, request.options, top=request.top, budget=budget, **extra)
        inputs.require_current(timeout_sec=remaining())
        if not anchor.current(timeout_sec=remaining()):
            raise KdbBindingChanged()
        result.update(request_fingerprint=_fingerprint(request), selected_top=request.top)
        validate_scan_worker_result(request, result)
        remaining()
        return WorkerSuccess(result=result, kdb_load_quality=backend.kdb_load_quality)
    except OperationCancelled:
        raise
    except KdbBindingChanged:
        return WorkerError(error_code='kdb_identity_changed', stage='query')
    except NativeInputChanged:
        return WorkerError(error_code='context_identity_changed', stage='query')
    except CaptureAborted as exc:
        return WorkerError(error_code='context_identity_timeout' if exc.code == 'timeout' else 'context_identity_changed', stage='query')
    except NpiHierarchyUnavailable as exc:
        try:
            inputs.require_current(timeout_sec=remaining())
            if not anchor.current(timeout_sec=remaining()):
                return WorkerError(error_code='kdb_identity_changed', stage='query')
        except OperationCancelled:
            raise
        except (TimeoutError, HierarchyBudgetExceeded):
            return WorkerError(error_code='query_timeout', stage='query')
        except Exception:
            return WorkerError(error_code='context_identity_changed', stage='query')
        # Identity and timeout must never invite a parent fallback.
        if exc.reason in NATIVE_IDENTITY_ERRORS:
            return WorkerError(error_code='result_invalid', stage='query')
        return WorkerUnavailable()
    except (TimeoutError, HierarchyBudgetExceeded):
        return WorkerError(error_code='query_timeout', stage='query')
    except Exception:
        # A failing API must not hide concurrent metadata/KDB changes.
        try:
            inputs.require_current(timeout_sec=remaining())
            if not anchor.current(timeout_sec=remaining()):
                return WorkerError(error_code='kdb_identity_changed', stage='query')
        except OperationCancelled:
            raise
        except (TimeoutError, HierarchyBudgetExceeded):
            return WorkerError(error_code='query_timeout', stage='query')
        except Exception:
            return WorkerError(error_code='context_identity_changed', stage='query')
        return WorkerError(error_code='result_invalid', stage='result')


def query_scan_over_lsf(backend, inputs, options, budget, *, state=None):
    from .npi_lsf import StructuralScanWorkerRequest
    budget.checkpoint()
    options = {**options, 'timeout_sec': min(options['timeout_sec'], budget.deadline-time.monotonic())}
    anchor = backend._bound_kdb_anchor
    request = StructuralScanWorkerRequest(kdb_path=anchor.path, top=inputs.selected_top,
        kdb_identity_sha256=anchor.identity_sha256, kdb_identity_max_bytes=anchor.max_bytes,
        metadata_payload=encode_native_inputs(inputs), options=options, scan_state=state)
    outcome = backend._execute_bound_request(request, deadline=budget.deadline)
    check_cancelled(); budget.checkpoint()
    execution = dict(execution_mode='lsf', scheduler_status=outcome.scheduler_status, worker_status=outcome.worker_status)
    if outcome.result is None:
        error = NpiHierarchyUnavailable(outcome.fallback_reason or 'npi_lsf_worker_failed')
        error.execution_status = dict(execution, fallback_reason=error.reason)
        raise error
    result = validate_scan_worker_result(request, outcome.result)
    if (outcome.scheduler_status != 'completed' or outcome.worker_status != 'completed'
            or outcome.kdb_load_quality != result['kdb_binding']['load_quality']):
        raise ValueError('incomplete structural worker receipt')
    result = dict(result)
    result.pop('request_fingerprint'); result.pop('selected_top')
    result['native_execution'] = execution
    return result
