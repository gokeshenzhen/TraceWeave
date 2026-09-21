"""Request-local X/Z evidence graph over existing dynamic backend contracts."""
from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import json
import os

from .cancellation import OperationCancelled
from .divergence_budget import BudgetExceeded, TraceRestart
from .divergence_compare import file_identity, known
from .divergence_context import resolve_context
from .divergence_mapping import split_selection
from .divergence_routing import DynamicRoute
from .dynamic_observe import observe_step
from .observation_session import ObservationSession
from .schemas import BackendStatus, TraceXSourceResult, XHistoryInput
from .x_history_observe import HistoryBudget, bind_step_wave, select_step, signal_expression, unknown_interval

_HYPOTHESES = ['past_upstream_unknown_sampled', 'local_control_or_retained_state',
               'waveform_compile_binding_or_sampling_unproven']


class HistoryIdentityChanged(Exception):
    pass


def _clock_key(step):
    clock = step.get('clock')
    if not clock:
        return None
    expr = clock['expression']
    return (expr.get('signal'), tuple(expr.get('bits', ())), clock['edge'])


async def trace_x_history(services, raw, *, route_factory=DynamicRoute):
    request = XHistoryInput.model_validate(raw).model_dump(exclude_none=True)
    start = services._resolve_time(request['history_start_ps'])
    time = services._resolve_time(request['time_ps'])
    if start > time:
        raise ValueError('history window requires history_start_ps <= time_ps')
    limits = {k: request[k] for k in ('max_depth', 'max_nodes', 'max_events', 'max_read_bytes', 'timeout_sec')}
    budget = HistoryBudget(limits)
    session = ObservationSession(budget, stream_reader=budget.read_stream)
    budget.observations = session
    path = request['wave_path']
    original_wave = file_identity(path)
    evidence = dict(status='blocked', window=dict(start_ps=start, end_ps=time, phase=request['phase']),
                    context={}, nodes=[], edges=[], candidates=[], frontier=[],
                    coverage=dict(checks=[], gaps=[], limits=limits, truncated=False,
                                  true_origin_proven=False,
                                  scope='recorded history and supported dynamic statements only'))
    bound = route = None
    parser_generation = None
    context = request.get('compile_context') or {
        k: request[k] for k in ('compile_log', 'simulator', 'top_hint') if k in request}
    if os.path.realpath(context['compile_log']) != os.path.realpath(request['compile_log']):
        raise ValueError('compile_context_mismatch')

    def frontier(node, reason, **facts):
        row = dict(node_id=node, reason=reason, **facts)
        if row not in evidence['frontier']:
            evidence['frontier'].append(row)
        if reason not in evidence['coverage']['gaps']:
            evidence['coverage']['gaps'].append(reason)
        if reason == 'budget_exhausted':
            evidence['coverage']['truncated'] = True

    async def wave(fn):
        def guarded():
            nonlocal parser_generation
            result = fn()
            parser = services._get_parser(path)
            token = (id(parser), getattr(parser, '_scope_epoch', None))
            if parser_generation is not None and parser_generation != token:
                raise HistoryIdentityChanged()
            parser_generation = token
            return result
        return await budget.run(lambda: services._run_in_wave_thread(path, guarded))

    def clear_graph():
        session.clear()
        for key in ('nodes', 'edges', 'candidates', 'frontier'):
            evidence[key].clear()
        evidence['coverage'].update(checks=[], gaps=[], truncated=False)

    try:
        bound, reason = await budget.run(lambda: services._run_in_cancellable_thread(
            lambda: resolve_context(context, services._handle_store)))
        evidence['context'] = dict(compile_context=bound.context if bound else context,
                                   waveform_identity=original_wave, binding_basis='caller_supplied',
                                   waveform_compile_version_relation='not_independently_proven')
        if bound is None:
            frontier(None, reason)
        else:
            design = hashlib.sha256(json.dumps([bound.snapshot, bound.context,
                bound.hierarchy['_compile_session_snapshot'].content_fingerprint_sha256], sort_keys=True).encode()).hexdigest()
            evidence['context']['design_identity_sha256'] = design
            side = {**bound.context, 'wave_path': path, 'signal_path': split_selection(request['signal_path'])[0]}
            route = route_factory(services, 'x', side, bound, budget)
            while True:
                clear_graph()
                session.bind((design, route.stage, route.artifact))
                try:
                    await _walk(evidence, request, services, route, budget, session, design, wave, frontier)
                    break
                except TraceRestart as restart:
                    clear_graph()
                    route.receipt = {}  # A discarded attempt cannot describe the next artifact.
                    budget.restart(restart.reason)
            if evidence['status'] != 'signal_is_clean':
                evidence['status'] = 'partial' if evidence['nodes'] else 'inconclusive'
    except OperationCancelled as exc:
        raise asyncio.CancelledError from exc
    except HistoryIdentityChanged:
        clear_graph()
        evidence['status'] = 'inconclusive'
        frontier(None, 'parser_generation_changed')
    except BudgetExceeded as exc:
        evidence['status'] = 'partial' if evidence['nodes'] else 'inconclusive'
        frontier(None, 'budget_exhausted', budget=exc.limit)
    finally:
        session.clear()

    # This validation also runs after an exhausted time budget. No old graph is
    # returned under a changed compile, KDB, parser or waveform identity.
    changed = []
    if original_wave is None or file_identity(path) != original_wave:
        changed.append('waveform_changed')
    elif parser_generation is not None:
        def current_generation():
            parser = services._get_parser(path)
            return (id(parser), getattr(parser, '_scope_epoch', None))
        if parser_generation != await services._run_in_wave_thread(path, current_generation):
            changed.append('parser_generation_changed')
    if bound is not None and not await services._run_in_cancellable_thread(bound.current):
        changed.append('compile_context_changed')
    if route is not None and not route.current():
        changed.append('compile_artifact_changed')
    if changed:
        clear_graph()
        evidence['status'] = 'inconclusive'
        for reason in changed:
            frontier(None, reason)
    receipt = dict(route.receipt) if route else {}
    if route is not None and not receipt:
        receipt = dict(selected_backend=getattr(route, 'selected_backend', None),
                       attempted_backends=getattr(route, 'attempts', []),
                       current_backend_stage=route.stage, last_attempt_status='incomplete')
    if receipt.get('selected_backend'):
        receipt['backend'] = receipt['selected_backend']
    if bound is not None:
        receipt['simulator'] = bound.context['simulator']
    if changed:
        receipt.pop('artifact_sha256', None)
        receipt['evidence_discarded'] = True
    receipt = {**receipt, 'whole_trace_restart_count': budget.restarts,
               'whole_trace_restart_reasons': budget.restart_reasons, 'single_backend_provenance': True}
    evidence['context']['backend_status'] = receipt
    evidence['operation_metrics'] = budget.metrics()
    evidence['coverage']['reading_modes'] = sorted(budget.modes)
    evidence['coverage']['byte_basis'] = 'native ABI text or VCD record estimate; legacy Python value estimate'
    evidence['coverage']['restart_reasons'] = budget.restart_reasons
    for node in evidence['nodes']:
        if node['status'] == 'pending':
            node['status'] = 'frontier'
    return TraceXSourceResult.model_validate(dict(
        mode='history', start_signal=request['signal_path'], start_time_ps=time,
        trace_status=evidence['status'], trace_depth=max((n['depth'] for n in evidence['nodes']), default=0),
        max_depth=request['max_depth'], propagation_chain=[], root_cause=None,
        history=evidence, trace_restarted=bool(budget.restarts),
        backend_status={k: v for k, v in receipt.items() if k in BackendStatus.model_fields},
        analysis_guide={
            'evidence': 'Read history.nodes, edges and frontier; observed X/Z boundaries are not exclusive root causes.',
            'hypotheses': 'Check past input injection against local control/hold behavior and the waveform-to-compile binding.',
            'limits': 'Snapshot mode remains same-time only. History never expands the supplied window automatically.'}))


async def _walk(result, request, services, route, budget, session, design, wave, frontier):
    start, time = result['window']['start_ps'], result['window']['end_ps']
    path = request['wave_path']
    queue = deque([(request['signal_path'], tuple(request.get('signal_bits', ())), time, request['phase'], 0, None, None, None, ())])
    seen = {}
    artifact = None
    while queue:
        budget.check()
        signal, bits, at, phase, depth, parent, relation, consumer_clock, declared_bits = queue.popleft()

        def read():
            parser = services._get_parser(path)
            expr = signal_expression(parser, signal, bits, declared_bits)
            identity = session.identity(parser, path, expr.signal)
            stream = session.stream(services._get_parser, path, expr.signal, start, time)
            return expr, identity, unknown_interval(stream, expr, start, at, phase)
        try:
            expr, wave_identity, interval = await wave(read)
        except (KeyError, ValueError):
            frontier(parent, 'signal_or_selection_unavailable', signal=signal)
            continue
        # Include the real declaration AND ordered bits, never the display key.
        stable_wave_identity = wave_identity[1:] if wave_identity else None
        key = (design, route.stage, route.artifact, stable_wave_identity, expr.signal, expr.bits, at, phase, consumer_clock)
        if key in seen:
            frontier(parent, 'revisited_state', dependency_node_id=seen[key])
            continue
        if interval['value'] is None or interval['gaps']:
            for reason in interval['gaps']:
                frontier(parent, reason, signal=expr.signal, time_ps=at, phase=phase, interval=interval)
            continue
        if known(interval['value']):
            if parent is None:
                result['status'] = 'signal_is_clean'
                result['coverage']['checks'].append('target_value_at_requested_phase')
            else:
                frontier(parent, 'dependency_value_changed', signal=expr.signal, time_ps=at)
            continue
        budget.node()
        node_id = f"n{len(result['nodes'])}"
        seen[key] = node_id
        node = dict(id=node_id, depth=depth, signal=expr.signal, bits=list(expr.bits),
                    time_ps=at, time_fs=at * 1000, phase=phase, value=interval['value'],
                    interval=interval, status='pending')
        result['nodes'].append(node)
        if interval['start_boundary'] != 'observed_transition':
            frontier(node_id, interval['start_boundary'] or 'history_onset_unavailable')
        if parent is not None:
            result['edges'].append(dict(source=parent, target=node_id, relation=relation))
        if 'observed_unknown_intervals' not in result['coverage']['checks']:
            result['coverage']['checks'].append('observed_unknown_intervals')
        if depth >= request['max_depth']:
            frontier(node_id, 'budget_exhausted', budget='max_depth')
            continue
        # Query the whole declared source; project only through supported typed
        # expressions. This avoids confusing a dump declaration with a query slice.
        step = await budget.run(lambda: route.query(split_selection(expr.signal)[0]))
        current_artifact = (design, step.get('backend'), step.get('artifact_sha256'))
        if artifact is not None and artifact != current_artifact:
            raise TraceRestart('x', 'dynamic_artifact_changed')
        artifact = current_artifact
        session.bind(artifact)
        seen[(design, route.stage, route.artifact, stable_wave_identity, expr.signal, expr.bits, at, phase, consumer_clock)] = node_id
        try:
            step = select_step(step, expr.bits)
        except ValueError:
            step = {**step, 'branches': [], 'boundary': 'unsupported', 'complete': False,
                    'gaps': [*step.get('gaps', ()), 'dynamic_expression_limit']}
        node['structure'] = {k: step[k] for k in ('backend', 'artifact_sha256', 'boundary', 'complete',
            'bits', 'clock', 'gaps', 'sources', 'cross_check', 'traversal', 'claim_semantics', 'structural') if k in step}
        node['structure']['assignments'] = [{k: b[k] for k in ('id', 'order', 'source', 'port_hops') if k in b}
                                          for b in step.get('branches', ())]
        gaps = list(step.get('gaps', ()))
        if not step.get('complete') and 'driver_set_incomplete' not in gaps:
            gaps.append('driver_set_incomplete')
        for gap in gaps:
            frontier(node_id, gap)
        traversal = step.get('traversal') or {}
        if traversal.get('search_exhaustive') is False:
            frontier(node_id, 'driver_traversal_incomplete')
        if 'testbench_driven' in gaps or step['boundary'] == 'input':
            node['status'] = 'boundary'
            frontier(node_id, 'testbench_driven' if 'testbench_driven' in gaps else 'input_boundary')
            result['candidates'].append(dict(node_id=node_id, kind='observed_unknown_boundary',
                competing_hypotheses=_HYPOTHESES, checked=['recorded_target'],
                unchecked=['boundary_producer', 'true_first_origin']))
            continue
        if step['boundary'] == 'unsupported' or any(g != 'driver_set_incomplete' for g in gaps):
            node['status'] = 'frontier'
            continue
        clock = _clock_key(step)
        if clock and consumer_clock and clock != consumer_clock:
            frontier(node_id, 'clock_domain_crossing_unverified')
            node['status'] = 'frontier'
            continue
        obs = await wave(lambda: observe_step(bind_step_wave(step, services._get_parser(path)), get_parser=services._get_parser, wave=path,
            time=at, history_start=start, phase=phase, session=session, include_state=True))
        node['observation'] = obs
        dependencies = obs['dependencies']
        node['checked_dependencies'] = dependencies
        unsafe = [g for g in obs['gaps'] if g not in {'value_unknown', 'driver_set_incomplete'}]
        for gap in unsafe:
            frontier(node_id, gap)
        if unsafe:
            node['status'] = 'frontier'
            continue
        for check in ('selected_data_and_controls', *(['trigger_edge_and_previous_state'] if clock else [])):
            if check not in result['coverage']['checks']:
                result['coverage']['checks'].append(check)
        supported = obs['value'] == node['value'] and step.get('complete') and not gaps
        action = 'hold' if obs.get('action') == 'hold' else 'register_sample' if clock else 'combinational'
        node['relation'] = action
        node['value_relation_observed'] = obs['value'] == node['value']
        if obs['value'] != node['value']:
            frontier(node_id, 'observed_value_not_explained')
            result['candidates'].append(dict(node_id=node_id, kind='local_behavior_candidate',
                competing_hypotheses=_HYPOTHESES, checked=['selected_data_and_controls'],
                unchecked=['simulation_scheduling', 'waveform_compile_binding']))
            node['status'] = 'candidate'
            continue
        unique = {}
        for dep in dependencies:
            if dep.get('value') is None or dep.get('gaps'):
                frontier(node_id, 'dependency_evidence_incomplete', dependency=dep)
                continue
            if not known(dep['value']):
                unique[(dep['signal'], tuple(dep['bits']), dep['time_ps'], dep['phase'])] = dep
        for index, dep in enumerate(unique.values()):
            if index >= 16:
                frontier(node_id, 'budget_exhausted', budget='max_branches')
                break
            queue.append((dep['signal'], tuple(dep['bits']), dep['time_ps'], dep['phase'], depth + 1,
                          node_id, action if supported else 'candidate_' + action, clock or consumer_clock,
                          tuple(dep.get('declared_bits', ()))))
        if not unique:
            frontier(node_id, 'unknown_constant_boundary' if obs['value'] is not None and not known(obs['value']) else 'no_unknown_dependency')
        node['status'] = 'expanded' if unique else 'boundary'
