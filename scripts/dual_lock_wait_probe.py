"""Real global-FSDB-lock waiter cancellation behind a retained dual trace.

The finite test gate delays delivery AFTER both real observe_step calls return.
A separate test event releases the retained holder; product alone cancels the
waiter. This is not mid-native preemption,
not a typed backend timeout, and not a replacement SDK/sleep/clock implementation.
All fixture inputs are read-only. See the frozen P4-S7 oracle before execution.
"""
from __future__ import annotations

import ast
import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from dual_trace_oracle import validate_step


ROLE = ContextVar("p4_s7_request_role", default="setup")
WORK = ContextVar("p4_s7_work_id", default=None)


async def lock_wait_chain(args, report, cases, inventory, server, counts,
                          measured, public, require, baseline_files):
    from src import cancellation, divergence_trace, npi_hierarchy_source, verdi_npi_backend
    from src.design_context import DesignContext
    from src.divergence_design_context import DivergenceDesigns
    from src.evidence_output import expand_compact_result
    from src.kdb_anchor import KdbAnchor

    out = Path(args.lock_wait_evidence_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    require(not any(out.is_relative_to(Path(c['compile_log']).parent) for c in cases.values()),
            'evidence_inside_fixture')
    prepared_waiter = args.lock_wait_prepared
    prefix = 'p4_s8' if prepared_waiter else 'p4_s7'
    phase = 'setup'
    handles, identities, entries = {}, {}, {}
    events = report['lock_wait_events'] = []
    observations = report['lock_wait_observations'] = {}
    state = {}
    sessions = {}
    loop = asyncio.get_running_loop()
    ready, queued = asyncio.Event(), asyncio.Event()
    waiter_popped, holder_popped = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    main_thread = threading.get_ident()
    holder_task = waiter_task = None
    owned_tasks = []
    final_active = False
    original_lock = server._FSDB_WAVE_LOCK

    def save(name, value):
        with (out / name).open('x') as stream:
            json.dump(value, stream, indent=2, default=str)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())

    def event(name, **facts):
        row = dict(kind=name, monotonic_ns=time.monotonic_ns(), thread=threading.get_ident(),
                   phase=phase, role=ROLE.get(), work_id=WORK.get(), **facts)
        events.append(row)
        return row

    def pins():
        return {label: entry.leases for label, entry in entries.items()}

    def entry_state():
        return {label: dict(leases=entry.leases, binding_locked=entry.binding_lock.locked(),
                            npi_failure=entry.npi_failure) for label, entry in entries.items()}

    def cursors():
        return deepcopy([row.as_dict() for row in server._cursor_store.list()])

    def parser_state():
        result = {}
        for label, case in cases.items():
            parser = server._parser_cache[case['wave_path']][1]
            result[label] = dict(object_id=id(parser), epoch_id=id(parser._scope_epoch),
                signature=list(server._get_wave_signature(case['wave_path'])),
                open=bool(parser._handle), group_active=parser._transition_group_active,
                group_token=parser._transition_group_token)
        return result

    def locks():
        return dict(fsdb=server._FSDB_WAVE_LOCK.locked(), active=server._FSDB_ACTIVE is not None,
                    source=npi_hierarchy_source._OWNER_LOCK.locked(),
                    fan_in=verdi_npi_backend._NPI_FAN_IN_CALLBACK_LOCK.locked())

    async def invoke(name, tool, arguments):
        nonlocal phase
        phase = name
        save(name + '.request.json', dict(tool=tool, arguments=arguments))
        token = ROLE.set(name)
        try:
            try:
                result = await measured(name, lambda: public(tool, arguments))
            except BaseException as exc:
                event('public_exception', type=type(exc).__name__, message=str(exc), pins=pins())
                save(name + '.exception.json', dict(type=type(exc).__name__, message=str(exc)))
                raise
            event('public_return', pins=pins())
            save(name + '.response.json', result)
            save(name + '.pins.json', pins())
            return result
        finally:
            ROLE.reset(token)

    def request(cursor, note):
        return dict(side_a=dict(design_handle=handles['a'], wave_path=cases['a']['wave_path'],
                               signal_path=cases['a']['signal_path']),
                    side_b=dict(design_handle=handles['b'], wave_path=cases['b']['wave_path'],
                               signal_path=cases['b']['signal_path']),
                    start_time_ps=5000, end_time_ps=35000, reference_side='a',
                    comparison=dict(mode='event'), signal_pairs=[],
                    scope_pairs=[dict(a=cases['a']['top_hint'], b=cases['b']['top_hint'])],
                    limits=dict(max_depth=4, max_nodes=16, max_branches=8, timeout_sec=30),
                    cursor_name=cursor, cursor_note=note)

    def comparison(result):
        comp = result['comparison']
        require(comp['diverged'] and comp['first_divergence_time_ps'] == 20000
                and comp['earliest_difference_proven'] and comp['value_a'] == '00110011'
                and comp['value_b'] == '11001100', 'root_wave_oracle')
        require(comp['coverage_status'] == 'partial' and
                {r['reason'] for r in comp['coverage_gaps']} == {'stopped_at_difference'},
                'tail_coverage_hidden')

    def structures(result, name):
        comparison(result)
        require(len(result['nodes']) == 1 and not result['edges'], 'root_shape')
        steps = [r for r in report['dynamic_steps'] if r.get('role') == name]
        require([r['side'] for r in steps] == ['a', 'b'], 'missing_dual_native_steps')
        require([r['side'] for r in report['loads'] if r.get('role') == name] == ['a', 'b'],
                'wrong_dual_load_order')
        for label, mask, value in [('a', 15, '00110011'), ('b', 240, '11001100')]:
            row = next(r for r in steps if r['side'] == label)
            require(row['loaded_top'] == cases[label]['top_hint'] and
                    Path(row['loaded_kdb']).resolve() == Path(cases[label]['kdb_path']).resolve(),
                    'native_binding_crossed')
            validate_step(row['result'], cases[label], inventory[label.upper()]['source']['path'], mask, require)
            fact = result['nodes'][0][label]
            require(tuple(fact['bits']) == tuple(range(7, -1, -1)) and fact['value'] == value
                    and fact['structure']['backend'] == 'verdi_npi'
                    and fact['structure']['complete'] and not fact['structure']['gaps'], 'structure_oracle')

    def observation(obs, label):
        value = '00110011' if label == 'a' else '11001100'
        require(obs['complete'] and obs['value'] == value and not obs['gaps']
                and obs['sampling_time_ps'] == obs['observation_time_ps'] == 20000
                and obs['sampling_phase'] == obs['observation_phase'] == 'after'
                and obs['trigger_time_ps'] is None, 'observation_oracle_' + label)
        top = cases[label]['top_hint']
        expected = {top + '.data': ('00111100', list(range(7, -1, -1))), top + '.mode': ('1', [0])}
        found = {}
        for dep in obs['dependencies']:
            require(dep['signal'] in expected and dep['time_ps'] == 20000
                    and dep['phase'] == 'after' and not dep['gaps'], 'dependency_identity')
            bits, declared = expected[dep['signal']]
            require(dep['value'] == ''.join(bits[declared.index(i)] for i in dep['bits']), 'dependency_value')
            found.setdefault(dep['signal'], set()).update(dep['bits'])
        require(found == {top + '.data': set(range(8)), top + '.mode': {0}}, 'opposite_side_incomplete')

    def published_contexts(result):
        for label in cases:
            context = result['contexts'][label]
            dc, receipt = context['design_context'], context['backend_status']
            require(dc['design_handle'] == handles[label] and dc['context_identity_sha256'] == identities[label],
                    'public_context_crossed')
            require(receipt['actual_backend'] == receipt['selected_backend'] == 'verdi_npi'
                    and receipt['execution_mode'] == 'local' and not receipt.get('fallback_reason')
                    and receipt['single_backend_provenance'] and receipt['whole_trace_restart_count'] == 0,
                    'public_native_provenance')
            require(dc['artifact']['source_association'] == 'unverified' and
                    context['waveform_compile_version_relation'] == 'not_independently_proven', 'association_upgrade')

    def full_graph(result, name):
        server.schemas.TraceDivergenceResult.model_validate(result)
        structures(result, name)
        published_contexts(result)
        require(result['status'] == 'complete' and not result['coverage']['gaps']
                and not result['frontier'] and not result['coverage']['truncated'], 'recovery_not_complete')
        require(len(result['findings']) == 1 and result['findings'][0]['kind'] == 'local_logic_candidate'
                and result['findings'][0]['checked_sides'] == ['a', 'b']
                and result['findings'][0]['competing_explanations'] and result['findings'][0]['limitations'],
                'local_finding_oracle')
        for label in cases:
            observation(result['nodes'][0][label]['observation'], label)

    originals = []
    workers = report['lock_wait_workers'] = {}
    workers_guard = threading.Lock()
    token_work = {}
    publications = report['lock_wait_cursor_publications'] = []
    original_walk = divergence_trace._walk
    original_observe = divergence_trace.observe_step
    original_wave = server._run_in_wave_thread
    original_source = server._run_in_cancellable_thread
    original_push, original_pop = cancellation.push_cancel_event, cancellation.pop_cancel_event
    original_preempt = server._preempt_lower_priority_fsdb
    original_final = DivergenceDesigns.validate_final
    original_current, original_anchor = DesignContext.current, KdbAnchor.current
    original_set, original_auto = server._cursor_store.set, server._cursor_store.auto_set
    original_active = server._set_active_fsdb
    original_mark = server.operation_metrics.mark_cancel_observed
    original_pin_all, original_exit = DivergenceDesigns.pin_all, DivergenceDesigns.__exit__
    original_session_init = divergence_trace.ObservationSession.__init__
    pin_events = report['lock_wait_pin_owners'] = []

    # Locate the ORIGINAL post-failed-acquire path, not a timing approximation.
    source_bytes = Path(server.__file__).read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    require(source_sha == 'bc4605fb2ab72d8861ca3b1aa83c41c7a3226b387cfc0ff2c440d59a5e328c7b',
            'unreviewed_wave_worker_source')
    wave_ast = next(n for n in ast.parse(source_bytes).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_in_wave_thread')
    loops = [n for n in ast.walk(wave_ast) if isinstance(n, ast.While)
             and isinstance(n.test, ast.UnaryOp) and isinstance(n.test.op, ast.Not)
             and isinstance(n.test.operand, ast.Call)
             and isinstance(n.test.operand.func, ast.Attribute) and n.test.operand.func.attr == 'acquire']
    require(len(loops) == 1, 'ambiguous_acquire_loop')
    queue_calls = [n for n in ast.walk(loops[0]) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == '_preempt_lower_priority_fsdb']
    raises = [n for n in ast.walk(loops[0]) if isinstance(n, ast.Raise)]
    worker_codes = [c for c in original_wave.__code__.co_consts
                    if inspect.iscode(c) and c.co_name == '_worker']
    require(len(queue_calls) == len(raises) == len(worker_codes) == 1, 'ambiguous_queue_witness')
    queue_line, raise_line, worker_code = queue_calls[0].lineno, raises[0].lineno, worker_codes[0]
    source_codes = [c for c in original_source.__code__.co_consts if inspect.iscode(c) and c.co_name == '_worker']
    require(len(source_codes) == 1, 'ambiguous_source_worker')
    marks = [n for n in ast.walk(loops[0]) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == 'mark_cancel_observed']
    require(len(marks) == 1, 'ambiguous_cancel_observation')
    mark_line = marks[0].lineno
    save('queue_source_identity.json', dict(server_sha256=source_sha, queue_line=queue_line,
         cancellation_raise_line=raise_line, worker_qualname=worker_code.co_qualname,
         poll_seconds=server._WAVE_LOCK_POLL_S))
    require(server._WAVE_LOCK_POLL_S == .2, 'poll_changed')

    def patch(owner, name, replacement):
        originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def pending():
        with workers_guard:
            return [key for key, row in workers.items() if not row.get('popped')]

    async def run_owned(kind, original, *a, **kw):
        with workers_guard:
            key = len(workers) + 1
            workers[key] = dict(kind=kind, role=ROLE.get(), submitted_ns=time.monotonic_ns())
        report['lifecycle_worker_undrained'] = True
        token = WORK.set(key)
        try:
            if kind == 'wave':
                paths, body = a
                def observed_body():
                    event('wave_body_enter')
                    return body()
                return await original(paths, observed_body, **kw)
            return await original(*a, **kw)
        finally:
            workers[key]['await_finished_ns'] = time.monotonic_ns()
            WORK.reset(token)
            report['lifecycle_worker_undrained'] = bool(pending())

    async def wave(paths, body, **kw):
        return await run_owned('wave', original_wave, paths, body, **kw)

    async def source(body):
        return await run_owned('source', original_source, body)

    def push(cancel):
        token = original_push(cancel)
        frame = sys._getframe(1)
        key = WORK.get() if frame.f_code in (worker_code, source_codes[0]) else None
        with workers_guard:
            if key is None:
                key = len(workers) + 1
                workers[key] = dict(kind='auxiliary_scope', role=ROLE.get(),
                    submitted_ns=time.monotonic_ns(), parent_work=WORK.get(), caller=frame.f_code.co_qualname)
            token_work[id(token)] = key
            row = workers[key]
            row.update(event_id=id(cancel), thread=threading.get_ident(), pushed_ns=time.monotonic_ns())
        report['lifecycle_worker_undrained'] = True
        event('worker_push', event_id=id(cancel), registered_work_id=key)
        if ROLE.get() == 'waiter' and row['kind'] == 'wave':
            state['waiter_event'] = cancel
            state['waiter_work'] = key
        return token

    def pop(token):
        cancel = cancellation._cancel_event.get()
        key = token_work[id(token)]
        frame = sys._getframe(1)
        exc = sys.exception()
        trace = []
        tb = exc.__traceback__ if exc is not None else None
        while tb is not None:
            trace.append(dict(function=tb.tb_frame.f_code.co_qualname, line=tb.tb_lineno,
                              original_wave_worker=tb.tb_frame.f_code is worker_code))
            tb = tb.tb_next
        facts = dict(event_id=id(cancel), product_event_set=cancel.is_set(),
            exception_type=type(exc).__name__ if exc is not None else None,
            exception_message=str(exc) if exc is not None else None,
            original_operation_cancelled=isinstance(exc, cancellation.OperationCancelled),
            traceback=trace, caller_original_worker=frame.f_code is worker_code,
            acquired_count=len(frame.f_locals.get('acquired', [])),
            owns_fsdb=frame.f_locals.get('owns_fsdb', False))
        result = original_pop(token)  # Must reset even when later oracle checks fail.
        facts['same_active_exception_after_reset'] = sys.exception() is exc
        facts['context_reset'] = cancellation._cancel_event.get() is not cancel
        facts['locks'] = locks()
        event('worker_pop', **facts)
        with workers_guard:
            workers[key].update(popped=True, popped_ns=time.monotonic_ns(), pop=facts)
            token_work.pop(id(token))
        report['lifecycle_worker_undrained'] = bool(pending())
        if cancel is state.get('waiter_event'):
            loop.call_soon_threadsafe(waiter_popped.set)
        if cancel is state.get('holder_event'):
            loop.call_soon_threadsafe(holder_popped.set)
        return result

    def preempt(cancel, priority):
        result = original_preempt(cancel, priority)
        frame = sys._getframe(1)
        if ROLE.get() == 'waiter' and frame.f_code is worker_code and frame.f_lineno == queue_line:
            if not state.get('queue_seen'):
                active = server._FSDB_ACTIVE
                require(cancel is state.get('waiter_event') and cancel is cancellation._cancel_event.get()
                        and cancel is not state.get('holder_event') and not cancel.is_set(), 'wrong_waiter_event')
                require(threading.get_ident() != main_thread and frame.f_locals['lock'] is original_lock
                        and server._FSDB_WAVE_LOCK is original_lock
                        and frame.f_locals['locks'] == [original_lock]
                        and frame.f_locals['acquired'] == [] and not frame.f_locals['owns_fsdb'],
                        'not_original_failed_acquire')
                require(original_lock.locked() and active is not None
                        and active[0] is state['holder_event'] and not active[0].is_set()
                        and active[1] == priority == server._WAVE_PRIORITY_INTERACTIVE,
                        'holder_not_retained_interactive')
                require(not release.is_set(), 'holder_released_before_queue')
                require(all(v == (2 if prepared_waiter else 1) for v in pins().values()),
                        'queued_request_lease_count')
                state['queue_seen'] = True
                event('queue_witness', event_id=id(cancel), holder_event_id=id(active[0]),
                      caller_line=frame.f_lineno, acquired_count=0, owns_fsdb=False,
                      priority=priority, pins=pins(), counts=dict(counts))
                loop.call_soon_threadsafe(queued.set)
        return result

    def mark_cancel(*a, **kw):
        result = original_mark(*a, **kw)
        frame = sys._getframe(1)
        if ROLE.get() == 'waiter' and frame.f_code is worker_code and frame.f_lineno == mark_line:
            cancel = cancellation._cancel_event.get()
            event('waiter_original_cancel_observed', event_id=id(cancel), product_event_set=cancel.is_set(),
                  acquired_count=len(frame.f_locals['acquired']), owns_fsdb=frame.f_locals['owns_fsdb'])
        return result

    def set_active(cancel, priority, metrics=None):
        result = original_active(cancel, priority, metrics)
        event('set_active_fsdb', event_id=id(cancel), priority=priority)
        return result

    async def walk(*a, **kw):
        bound = inspect.signature(original_walk).bind(*a, **kw).arguments
        sessions[ROLE.get()] = (bound['observation_session'], bound['budget'])
        if ROLE.get() != 'holder':
            return await original_walk(*a, **kw)
        state.update(result=bound['result'], budget=bound['budget'], session=bound['observation_session'])
        return await original_walk(*a, **kw)

    def observe(*a, **kw):
        result = original_observe(*a, **kw)
        if ROLE.get() != 'holder':
            return result
        label = next(s for s in cases if cases[s]['wave_path'] == kw['wave'])
        observations[label] = deepcopy(result)
        event('real_observation_return', side=label)
        if label == 'b':
            require(ROLE.get() == 'holder' and not ready.is_set() and set(observations) == {'a', 'b'},
                    'wrong_holder_gate_stage')
            cancel = cancellation._cancel_event.get()
            require(threading.get_ident() != main_thread and original_lock.locked()
                    and cancel is not None and not cancel.is_set(), 'missing_live_holder')
            require(server._FSDB_ACTIVE[0] is cancel and all(v == 1 for v in pins().values()), 'holder_pin_or_owner')
            stage = deepcopy(state['result'])
            structures(stage, 'holder')
            for s in cases:
                observation(observations[s], s)
                require('observation' not in stage['nodes'][0][s], 'observation_already_published')
            require(stage['nodes'][0]['status'] == 'pending' and not stage['findings'], 'holder_graph_stage')
            require(all(not p['group_active'] and p['group_token'] is None for p in parser_state().values()),
                    'holder_resident_group')
            require(counts['fsdb_abi_fsdb_event_open_v1'] == counts['fsdb_abi_fsdb_event_close_v1'],
                    'holder_event_cursor_open')
            require(not locks()['source'] and not locks()['fan_in'], 'holder_native_owner_lock')
            require(all(row.get('returned') and row.get('delegate_finished_ns')
                        for row in report['fsdb_abi_calls']), 'holder_ffr_delegate_inflight')
            require(counts['get_dynamic_step'] == len(report['dynamic_steps']),
                    'holder_dynamic_delegate_inflight')
            state['holder_event'] = cancel
            save('gate_private_graph.json', stage)
            save('gate_real_observations.json', observations)
            save('gate_state.json', dict(pins=pins(), entries=entry_state(), parsers=parser_state(),
                 event_id=id(cancel), counts=dict(counts), deadline=state['budget'].deadline,
                 all_ffr_delegates_finished=True, all_dynamic_delegates_returned=True,
                 native_main_thread_serialized=True, native_owner_locks_clear=True))
            require(state['budget'].deadline - time.monotonic() >= 22, 'holder_gate_margin')
            state['gate_counts'] = dict(counts)
            if prepared_waiter:
                report['prepared_waiter_safe_holder_event'] = id(cancel)
                report['prepared_waiter_gate_active'] = True
            state['gate_parser_calls'] = len(report['lock_wait_parser_calls'])
            event('holder_gate_ready', event_id=id(cancel), pins=pins(), deadline=state['budget'].deadline)
            loop.call_soon_threadsafe(ready.set)
            # This is a separate test event. Never arm either product event.
            state['gate_released'] = release.wait(20)
            report['prepared_waiter_gate_active'] = False
            event('holder_gate_return', released=state['gate_released'], product_event_set=cancel.is_set())
        return result

    async def final(designs, *a, **kw):
        nonlocal final_active
        if ROLE.get() != 'holder':
            event('nonholder_final_enter', designs_id=id(designs))
            return await original_final(designs, *a, **kw)
        final_active = True
        event('final_enter', pins=pins(), entries=entry_state())
        try:
            result = await original_final(designs, *a, **kw)
            event('final_return', result=deepcopy(result), pins=pins(), entries=entry_state())
            return result
        finally:
            final_active = False

    def current(context, *a, **kw):
        result = original_current(context, *a, **kw)
        if final_active:
            event('final_source_current', side=next(s for s in entries if entries[s].context is context), current=result)
        return result

    def anchor_current(anchor, *a, **kw):
        result = original_anchor(anchor, *a, **kw)
        if final_active:
            event('final_anchor_current', side=next(s for s in entries if entries[s].binding.anchor is anchor), current=result)
        return result

    def cursor_set(*a, **kw):
        result = original_set(*a, **kw)
        if ROLE.get() not in {'holder', 'waiter'}:
            return result
        publications.append(dict(method='set', role=ROLE.get(), result=result.as_dict(), monotonic_ns=time.monotonic_ns()))
        return result

    def cursor_auto(*a, **kw):
        result = original_auto(*a, **kw)
        if ROLE.get() not in {'holder', 'waiter'}:
            return result
        publications.append(dict(method='auto_set', role=ROLE.get(), result=result.as_dict(), monotonic_ns=time.monotonic_ns()))
        return result

    async def completed(task, timeout, code):
        done, _ = await asyncio.wait([task], timeout=timeout)
        require(task in done, code)  # asyncio.wait never implicitly cancels task.
        return await task

    async def reach(marker, task, timeout, code):
        waiting = asyncio.create_task(marker.wait())
        try:
            done, _ = await asyncio.wait([task, waiting], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task  # Preserve original failure instead of replacing it.
            require(waiting in done and marker.is_set() and not task.done(), code)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    def pin_all(designs):
        before = pins()
        result = original_pin_all(designs)
        pin_events.append(dict(kind='acquire', role=ROLE.get(), designs_id=id(designs),
            task_id=id(asyncio.current_task()), before=before, after=pins(),
            entries={label: id(designs.entries[handles[label]]) for label in cases}))
        return result

    def exit_designs(designs, *exc):
        before = pins()
        try:
            return original_exit(designs, *exc)
        finally:
            pin_events.append(dict(kind='release', role=ROLE.get(), designs_id=id(designs),
                task_id=id(asyncio.current_task()), before=before, after=pins(),
                exception_type=exc[0].__name__ if exc and exc[0] else None))

    def session_init(session, *a, **kw):
        original_session_init(session, *a, **kw)
        sessions[ROLE.get()] = (session, session.budget)

    if prepared_waiter:
        patch(DivergenceDesigns, 'pin_all', pin_all)
        patch(DivergenceDesigns, '__exit__', exit_designs)
        patch(divergence_trace.ObservationSession, '__init__', session_init)
    patch(server, '_run_in_wave_thread', wave)
    patch(server, '_run_in_cancellable_thread', source)
    patch(cancellation, 'push_cancel_event', push)
    patch(cancellation, 'pop_cancel_event', pop)
    patch(server, '_preempt_lower_priority_fsdb', preempt)
    patch(server, '_set_active_fsdb', set_active)
    patch(server.operation_metrics, 'mark_cancel_observed', mark_cancel)
    patch(divergence_trace, '_walk', walk)
    patch(divergence_trace, 'observe_step', observe)
    patch(DivergenceDesigns, 'validate_final', final)
    patch(DesignContext, 'current', current)
    patch(KdbAnchor, 'current', anchor_current)
    patch(server._cursor_store, 'set', cursor_set)
    patch(server._cursor_store, 'auto_set', cursor_auto)
    succeeded = False
    try:
        for label, case in cases.items():
            phase = 'prepare_scan_' + label
            before = counts.copy()
            prepare_args = dict(compile_log=case['compile_log'], simulator='vcs', top_hint=case['top_hint'], timeout_sec=30)
            scan_args = dict(compile_log=case['compile_log'], simulator='vcs', analysis_mode='fast')
            save(phase + '.request.json', [dict(tool='prepare_design_context', arguments=prepare_args),
                                         dict(tool='scan_structural_risks', arguments=scan_args)])
            async def pair():
                pair_tasks = [asyncio.create_task(public('prepare_design_context', prepare_args)),
                              asyncio.create_task(public('scan_structural_risks', scan_args))]
                owned_tasks.extend(pair_tasks)
                return await asyncio.gather(*pair_tasks)
            prepared, scan = await measured(phase, pair)
            save(phase + '.response.json', [prepared, scan])
            require(not prepared.get('error') and not scan.get('error') and
                    prepared['semantic_status'] == 'not_run' and prepared['kdb_status'] == 'not_checked'
                    and scan['lexical_coverage_status'] == 'complete' and scan['semantic']['status'] == 'not_run',
                    'prepare_scan_oracle')
            require(all(counts[k] == before[k] for k in ('sdk_init', 'sdk_load_design', 'get_dynamic_step')),
                    'prepare_started_native')
            handles[label], identities[label] = prepared['design_handle'], prepared['context_identity_sha256']
            with server._design_context_store.pin(handles[label]) as entry:
                entries[label] = entry
        require(handles['a'] != handles['b'] and identities['a'] != identities['b'], 'aliased_designs')
        baseline_name = prefix + '_baseline'
        baseline = await invoke('trace_baseline', 'trace_divergence', request(baseline_name, 'baseline must survive'))
        full_graph(baseline, 'trace_baseline')
        before_cursors, before_parsers = cursors(), parser_state()
        anchor_ids = {s: entry.binding.anchor.identity_sha256 for s, entry in entries.items()}
        actions = deepcopy(baseline['next_actions'])
        require(len(actions) == 2 and {a['side'] for a in actions} == {'a', 'b'}
                and all(a['status'] == 'ready' for a in actions), 'baseline_actions')
        save('baseline_state.json', dict(cursors=before_cursors, parsers=before_parsers, pins=pins(), anchors=anchor_ids))

        holder_task = asyncio.create_task(invoke('holder', 'trace_divergence',
            request(prefix + '_holder', 'retained holder completes after queued waiter cancellation')))
        owned_tasks.append(holder_task)
        await reach(ready, holder_task, 5, 'holder_did_not_reach_gate')
        waiter_tool = 'trace_divergence' if prepared_waiter else 'get_signal_at_time'
        waiter_args = (request(prefix + '_waiter', 'cancel before first wave comparison')
                       if prepared_waiter else dict(wave_path=cases['b']['wave_path'],
                           signal_path=cases['b']['signal_path'], time_ps=20000))
        waiter_task = asyncio.create_task(invoke('waiter', waiter_tool, waiter_args))
        owned_tasks.append(waiter_task)
        await reach(queued, waiter_task, 5, 'waiter_did_not_reach_failed_acquire')
        require(not holder_task.done() and not release.is_set() and original_lock.locked(), 'holder_not_waiting')
        event('waiter_cancel_issue', event_id=id(state['waiter_event']))
        require(waiter_task.cancel(), 'waiter_cancel_not_issued')
        try:
            await completed(waiter_task, 3, 'waiter_public_did_not_finish')
        except asyncio.CancelledError:
            event('waiter_cancel_received')
        else:
            require(False, 'waiter_returned_normal_response')
        await asyncio.wait_for(waiter_popped.wait(), 3)
        row = workers[state['waiter_work']]
        receipt = row['pop']
        require(receipt['original_operation_cancelled'] and receipt['same_active_exception_after_reset']
                and receipt['exception_message'] == 'tool call cancelled while waiting for wave lock'
                and receipt['caller_original_worker'] and receipt['product_event_set'] and receipt['context_reset']
                and receipt['acquired_count'] == 0 and not receipt['owns_fsdb']
                and any(t['original_wave_worker'] and t['line'] == raise_line for t in receipt['traceback']),
                'missing_original_lock_wait_cancellation')
        marks_seen = [e for e in events if e['kind'] == 'waiter_original_cancel_observed']
        require(len(marks_seen) == 1 and marks_seen[0]['product_event_set']
                and marks_seen[0]['event_id'] == id(state['waiter_event'])
                and marks_seen[0]['acquired_count'] == 0 and not marks_seen[0]['owns_fsdb'],
                'missing_original_cancel_observation')
        require(not [r for r in report['lock_wait_parser_calls'] if r['role'] == 'waiter'], 'waiter_touched_parser')
        require(not [r for r in report['fsdb_abi_calls'] if r.get('role') == 'waiter'], 'waiter_called_ffr')
        require(not [r for r in report['loads'] + report.get('dynamic_steps', []) if r.get('role') == 'waiter'],
                'waiter_called_npi')
        require(not [e for e in events if e['role'] == 'waiter' and e['kind'] in
                     ('wave_body_enter', 'set_active_fsdb', 'final_enter', 'nonholder_final_enter')], 'waiter_entered_body_or_owned_lock')
        protected = {key for key in set(counts) | set(state['gate_counts'])
                     if key != 'source_lock_check_profile_events'} if prepared_waiter else set(counts) | set(state['gate_counts'])
        require(all(counts[key] == state['gate_counts'].get(key, 0) for key in protected)
                and len(report['lock_wait_parser_calls']) == state['gate_parser_calls'],
                'native_or_parser_work_while_holder_gated')
        require(not holder_task.done() and not state['holder_event'].is_set()
                and original_lock.locked() and server._FSDB_ACTIVE[0] is state['holder_event']
                and all(v == 1 for v in pins().values()), 'waiter_cancel_disrupted_holder')
        require(cursors() == before_cursors and not publications and parser_state() == before_parsers,
                'waiter_changed_publication_or_parser')
        if prepared_waiter:
            waiter_leases = [row for row in pin_events if row['role'] == 'waiter']
            holder_leases = [row for row in pin_events if row['role'] == 'holder']
            require(len(waiter_leases) == 2 and len(holder_leases) == 1, 'lease_owner_event_count')
            acquired, released = waiter_leases
            require(acquired['kind'] == 'acquire' and released['kind'] == 'release'
                    and acquired['designs_id'] == released['designs_id'] != holder_leases[0]['designs_id']
                    and acquired['task_id'] == released['task_id'] != holder_leases[0]['task_id']
                    and acquired['before'] == released['after'] == {'a': 1, 'b': 1}
                    and acquired['after'] == released['before'] == {'a': 2, 'b': 2}
                    and released['exception_type'] == 'CancelledError'
                    and acquired['entries'] == {label: id(entries[label]) for label in cases},
                    'prepared_waiter_own_leases_not_released')
            require(report.get('prepared_waiter_nonowner_source_checks'), 'nonowner_source_work_unobserved')
            waiter_session, waiter_budget = sessions['waiter']
            require(not waiter_session.entries and waiter_session.events == waiter_session.nbytes
                    == waiter_budget.cache_bytes == 0, 'waiter_session_leaked')
            save('prepared_waiter_lease_receipt.json', dict(waiter=waiter_leases, holder=holder_leases,
                 source_checks=report['prepared_waiter_nonowner_source_checks']))
        save('after_waiter_drain.json', dict(pins=pins(), entries=entry_state(), locks=locks(), cursors=cursors(),
             parsers=parser_state(), counts=dict(counts), waiter_owned_pins=0, pending_workers=pending()))
        event('holder_release_issue')
        release.set()
        holder = await completed(holder_task, 15, 'holder_did_not_finish')
        await asyncio.wait_for(holder_popped.wait(), 5)
        require(state.get('gate_released') is True and not state['holder_event'].is_set(), 'holder_gate_cap_or_cancel')
        full_graph(holder, 'holder')
        require(not pending() and not any(locks().values()) and all(v == 0 for v in pins().values()),
                'holder_resource_leak')
        require([e['result'] for e in events if e['kind'] == 'final_return'] == [[]], 'holder_final_failed')
        for name in ('final_source_current', 'final_anchor_current'):
            require([(e['side'], e['current']) for e in events if e['kind'] == name] == [('a', True), ('b', True)],
                    'holder_final_validation_incomplete')
        require(len(publications) == 1 and publications[0]['role'] == 'holder'
                and publications[0]['result']['name'] == prefix + '_holder'
                and publications[0]['result']['time_ps'] == 20000
                and len(cursors()) == len(before_cursors) + 1, 'holder_cursor_contract')
        require(len(holder['next_actions']) == 2 and {a['side'] for a in holder['next_actions']} == {'a', 'b'}
                and all(a['status'] == 'ready' and a['arguments']['design_handle'] == handles[a['side']]
                and a['evidence']['time_ps'] == 20000 and a['evidence']['cursor_name'] == prefix + '_holder'
                for a in holder['next_actions']), 'holder_ready_actions')
        session = state['session']
        require(not session.entries and session.events == session.nbytes == state['budget'].cache_bytes == 0,
                'holder_observation_cache_not_cleared')
        require(parser_state() == before_parsers and all(not e.binding_lock.locked() and e.npi_failure is None
                for e in entries.values()), 'holder_entry_or_parser_changed')
        require([server._cursor_store.get(r['name']).as_dict() for r in before_cursors] == before_cursors,
                'baseline_cursor_changed')
        save('after_holder.json', dict(pins=pins(), entries=entry_state(), locks=locks(), parsers=parser_state(),
             cursors=cursors(), session=dict(entries=len(session.entries), events=session.events, nbytes=session.nbytes)))
        for label in ('a', 'b'):
            action = next(a for a in actions if a['side'] == label)
            require(action['arguments'] == dict(design_handle=handles[label], wave_path=cases[label]['wave_path'],
                        signal_path=cases[label]['signal_path'], recursive=False), 'crossed_old_action')
            before = counts.copy()
            raw = await invoke('old_action_' + label, action['tool'], action['arguments'])
            result = expand_compact_result(raw)
            save('old_action_' + label + '.expanded.json', result)
            require(result['backend_status']['actual_backend'] == 'verdi_npi' and result['driver_status'] == 'resolved',
                    'old_action_not_native')
            require(result['source_file'] == inventory[label.upper()]['source']['path']
                    and result['source_line'] == 8 and result['signal_path'] == cases[label]['signal_path']
                    and result['resolved_module'] == cases[label]['top_hint']
                    and result['backend_status']['design_context']['design_handle'] == handles[label],
                    'old_action_source_identity')
            require(action['arguments']['design_handle'] == handles[label]
                    and counts['sdk_load_design'] - before['sdk_load_design'] == 1
                    and counts['find_driver'] - before['find_driver'] == 1
                    and counts['fsdb_abi_total'] == before['fsdb_abi_total'], 'old_action_work')
        recovered = await invoke('trace_recovered', 'trace_divergence', request(prefix + '_recovered', 'same healthy handles'))
        full_graph(recovered, 'trace_recovered')
        require(parser_state() == before_parsers, 'recovery_replaced_healthy_parser')
        require(not any(locks().values()) and all(v == 0 for v in pins().values()), 'recovery_resource_leak')
        require([server._cursor_store.get(row['name']).as_dict() for row in before_cursors] == before_cursors,
                'recovery_overwrote_baseline')
        session_states = {name: dict(entries=len(session.entries), events=session.events,
            nbytes=session.nbytes, cache_bytes=budget.cache_bytes) for name, (session, budget) in sessions.items()}
        require(set(session_states) == ({'trace_baseline', 'holder', 'trace_recovered', 'waiter'}
                if prepared_waiter else {'trace_baseline', 'holder', 'trace_recovered'})
                and all(not any(row.values()) for row in session_states.values()), 'trace_session_cleanup')
        final_ids = {}
        for label, entry in entries.items():
            async def validate(entry=entry):
                await server._validate_design_source(entry.context, timeout_sec=30)
                await server._validate_design_binding(entry.binding, timeout_sec=5)
            await measured('final_identity_' + label, validate)
            require(entry.context.identity_sha256 == identities[label] and
                    entry.binding.anchor.identity_sha256 == anchor_ids[label] and entry.npi_failure is None,
                    'final_identity_or_failure_changed')
            final_ids[label] = dict(handle=handles[label], source=identities[label], anchor=anchor_ids[label], leases=entry.leases)
        save('final_state.json', dict(identities=final_ids, parsers=parser_state(), pins=pins(),
                                    entries=entry_state(), locks=locks(), cursors=cursors(), sessions=session_states))
        for row in inventory.values():
            tree = Path(row['kdb']['canonical'])
            actual = [(str(p.relative_to(tree)), hashlib.sha256(p.read_bytes()).hexdigest())
                      for p in sorted(tree.rglob('*')) if p.is_file()]
            require(hashlib.sha256(json.dumps(actual, separators=(',', ':')).encode()).hexdigest()
                    == row['kdb']['tree_content_sha256'], 'final_kdb_file_set_changed')
        save('input_hashes.json', [dict(path=p, sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest(), expected=d)
                                  for p, d in baseline_files.items()])
        require(counts['fsdb_abi_fsdb_event_open_v1'] == counts['fsdb_abi_fsdb_event_close_v1'],
                'final_native_event_cursor_imbalance')
        report['coverage'] = dict(real_dual_native_wave=True, original_failed_lock_acquire=True,
            queued_public_point_cancel=not prepared_waiter, waiter_owned_design_pins=2 if prepared_waiter else 0, holder_continuity=True,
            original_handle_recovery=True, holder_gate_sec=20, analysis_timeout_sec=30,
            native_mid_call_interruption='not_tested', wire_cancellation='not_tested',
            queued_prepared_trace_pins='verified' if prepared_waiter else 'not_tested', typed_backend_identity_timeout='not_tested',
            license_checkout_release='unverified', source_wave_association='unverified', lsf='not_run')
        succeeded = True
    except BaseException as exc:
        report['lock_wait_primary_failure'] = dict(type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
        save('primary_failure.json', report['lock_wait_primary_failure'])
        raise
    finally:
        if not succeeded:
            event('failed_test_release')
            release.set()
        async def drain():
            tasks = list(owned_tasks)
            for task in tasks:
                if not task.done():
                    event('failed_test_task_cancel')
                    task.cancel()
            if tasks:
                done, _ = await asyncio.wait(tasks, timeout=5)
                for task in done:
                    # Retrieve exceptions; never convert them to acceptance.
                    if not task.cancelled():
                        task.exception()
            deadline = time.monotonic() + 5
            while pending() and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            report['lifecycle_worker_undrained'] = bool(pending()) or any(not t.done() for t in tasks)
        cleanup = asyncio.create_task(drain())
        try:
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup)
                raise
        finally:
            if not report.get('lifecycle_worker_undrained'):
                for owner, name, original in reversed(originals):
                    setattr(owner, name, original)
                report['lock_wait_observers_restored'] = True
            else:
                report['lock_wait_observers_restored'] = False
                report['requires_exit_without_native_teardown'] = True
            save('lock_wait_events.json', events)
            save('lock_wait_workers.json', workers)
            save('lock_wait_pin_owners.json', pin_events)
        require(not report.get('lifecycle_worker_undrained'), 'owned_workers_undrained')
