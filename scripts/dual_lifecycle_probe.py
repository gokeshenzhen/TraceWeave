"""Real dual-native/wave lifecycle probe at a completed observation boundary.

The finite test gate delays delivery AFTER both real observe_step calls return.
Only the product sets its cancellation event. This is not mid-native preemption,
not a typed backend timeout, and not a replacement SDK/sleep/clock implementation.
All fixture inputs are read-only. See the frozen P4-S6 oracle before execution.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading
import time

from dual_trace_oracle import validate_step


async def lifecycle_chain(args, report, cases, inventory, server, counts,
                          measured, public, require, baseline_files):
    from src import cancellation, divergence_trace, npi_hierarchy_source, verdi_npi_backend
    from src.design_context import DesignContext
    from src.divergence_design_context import DivergenceDesigns
    from src.evidence_output import expand_compact_result
    from src.kdb_anchor import KdbAnchor

    out = Path(args.lifecycle_evidence_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    require(not any(out.is_relative_to(Path(c['compile_log']).parent) for c in cases.values()),
            'evidence_inside_fixture')
    kind, phase = args.lifecycle_kind, 'setup'
    handles, identities, entries = {}, {}, {}
    events = report['lifecycle_events'] = []
    observations = report['lifecycle_observations'] = {}
    state = {}
    loop = asyncio.get_running_loop()
    ready, popped = asyncio.Event(), asyncio.Event()
    main_thread = threading.get_ident()
    paths = [c['wave_path'] for c in cases.values()]
    task = None
    active = False
    final_active = False
    request_returned = False

    def save(name, value):
        with (out / name).open('x') as stream:
            json.dump(value, stream, indent=2, default=str)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())

    def event(name, **facts):
        row = dict(kind=name, monotonic_ns=time.monotonic_ns(), thread=threading.get_ident(),
                   phase=phase, **facts)
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
        try:
            result = await measured(name, lambda: public(tool, arguments))
        except BaseException as exc:
            save(name + '.exception.json', dict(type=type(exc).__name__, message=str(exc)))
            raise
        save(name + '.response.json', result)
        save(name + '.pins.json', pins())
        return result

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
        steps = [r for r in report['dynamic_steps'] if r['phase'] == name]
        require([r['side'] for r in steps] == ['a', 'b'], 'missing_dual_native_steps')
        require([r['side'] for r in report['loads'] if r['phase'] == name] == ['a', 'b'],
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

    for label, case in cases.items():
        phase = 'prepare_scan_' + label
        before = counts.copy()
        prepare_args = dict(compile_log=case['compile_log'], simulator='vcs', top_hint=case['top_hint'], timeout_sec=30)
        scan_args = dict(compile_log=case['compile_log'], simulator='vcs', analysis_mode='fast')
        save(phase + '.request.json', [dict(tool='prepare_design_context', arguments=prepare_args),
                                     dict(tool='scan_structural_risks', arguments=scan_args)])
        async def pair():
            return await asyncio.gather(public('prepare_design_context', prepare_args),
                                        public('scan_structural_risks', scan_args))
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
    baseline_name = 'p4_s6_' + kind + '_baseline'
    baseline = await invoke('trace_baseline', 'trace_divergence', request(baseline_name, 'baseline must survive'))
    full_graph(baseline, 'trace_baseline')
    before_cursors, before_parsers = cursors(), parser_state()
    anchor_ids = {s: entry.binding.anchor.identity_sha256 for s, entry in entries.items()}
    actions = deepcopy(baseline['next_actions'])
    require(len(actions) == 2 and {a['side'] for a in actions} == {'a', 'b'}
            and all(a['status'] == 'ready' for a in actions), 'baseline_actions')
    save('baseline_state.json', dict(cursors=before_cursors, parsers=before_parsers, pins=pins(), anchors=anchor_ids))

    originals = []
    def patch(owner, name, replacement):
        originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    original_walk = divergence_trace._walk
    original_observe = divergence_trace.observe_step
    original_pop = cancellation.pop_cancel_event
    original_final = DivergenceDesigns.validate_final
    original_current, original_anchor = DesignContext.current, KdbAnchor.current
    original_set, original_auto = server._cursor_store.set, server._cursor_store.auto_set
    publications = report['lifecycle_cursor_publications'] = []

    async def walk(*a, **kw):
        bound = inspect.signature(original_walk).bind(*a, **kw).arguments
        state.update(result=bound['result'], budget=bound['budget'], session=bound['observation_session'])
        return await original_walk(*a, **kw)

    def observe(*a, **kw):
        result = original_observe(*a, **kw)
        label = next(s for s in cases if cases[s]['wave_path'] == kw['wave'])
        observations[label] = deepcopy(result)
        event('real_observation_return', side=label)
        if label == 'b':
            require(active and not ready.is_set() and set(observations) == {'a', 'b'}, 'wrong_gate_stage')
            require(threading.get_ident() != main_thread and server._FSDB_WAVE_LOCK.locked(), 'gate_not_wave_worker')
            cancel = cancellation._cancel_event.get()
            require(cancel is not None and not cancel.is_set(), 'missing_live_product_event')
            require(all(v == 1 for v in pins().values()), 'gate_pin_missing')
            stage = deepcopy(state['result'])
            structures(stage, 'trace_negative')
            for s in cases:
                observation(observations[s], s)
                require('observation' not in stage['nodes'][0][s], 'observation_already_published')
            require(stage['nodes'][0]['status'] == 'pending' and not stage['findings'], 'gate_graph_stage')
            parsers = parser_state()
            require(all(not p['group_active'] and p['group_token'] is None for p in parsers.values()),
                    'resident_group_at_gate')
            budget = state['budget']
            require(time.monotonic() < budget.deadline - 1, 'gate_deadline_margin_missing')
            state['cancel'] = cancel
            report['lifecycle_worker_undrained'] = True
            state['gate_counts'] = dict(counts)
            require(counts['fsdb_abi_fsdb_event_open_v1'] == counts['fsdb_abi_fsdb_event_close_v1'],
                    'native_event_cursor_not_closed')
            save('gate_private_graph.json', stage)
            save('gate_real_observations.json', observations)
            save('gate_state.json', dict(pins=pins(), entries=entry_state(), parsers=parsers, counts=dict(counts),
                event_id=id(cancel), deadline=budget.deadline, started=budget.started))
            event('gate_ready', event_id=id(cancel), deadline=budget.deadline,
                  budget_started=budget.started, pins=pins())
            loop.call_soon_threadsafe(ready.set)
            # The original product handler alone sets this event. Finite wait is
            # a test handoff barrier, not an SDK call or a simulated timeout.
            released = cancel.wait(40)
            state['gate_released_by_product'] = released
            event('gate_return', product_event_set=released, event_id=id(cancel))
        return result

    def pop(token):
        current = cancellation._cancel_event.get()
        target = state.get('cancel')
        result = original_pop(token)
        if target is not None and current is target:
            event('target_worker_pop', event_id=id(current), locks=locks())
            report['lifecycle_worker_undrained'] = False
            loop.call_soon_threadsafe(popped.set)
        return result

    async def final(designs, *a, **kw):
        nonlocal final_active
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
        publications.append(dict(method='set', result=result.as_dict(), monotonic_ns=time.monotonic_ns()))
        return result

    def cursor_auto(*a, **kw):
        result = original_auto(*a, **kw)
        publications.append(dict(method='auto_set', result=result.as_dict(), monotonic_ns=time.monotonic_ns()))
        return result

    patch(divergence_trace, '_walk', walk)
    patch(divergence_trace, 'observe_step', observe)
    patch(cancellation, 'pop_cancel_event', pop)
    patch(DivergenceDesigns, 'validate_final', final)
    patch(DesignContext, 'current', current)
    patch(KdbAnchor, 'current', anchor_current)
    patch(server._cursor_store, 'set', cursor_set)
    patch(server._cursor_store, 'auto_set', cursor_auto)
    negative = None
    try:
        active = True
        # Before the reviewed gate, an early timeout can abandon any wave or
        # source worker. Without its exact pop receipt, fail closed: no native
        # diagnostics/atexit. A pre-gate failure is never an accepted row.
        report['lifecycle_worker_undrained'] = True
        negative_name = baseline_name if kind == 'cancel' else 'p4_s6_timeout_result'
        task = asyncio.create_task(invoke('trace_negative', 'trace_divergence',
            request(negative_name, 'cancel must not overwrite' if kind == 'cancel' else 'validated partial timeout')))
        waiter = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait([task, waiter], timeout=35, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                # Consume the original exception/result before a gate verdict.
                await task
            require(waiter in done and ready.is_set() and not task.done(), 'request_did_not_reach_gate')
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if kind == 'cancel':
            event('public_task_cancel_issue')
            require(task.cancel(), 'cancel_not_issued')
        try:
            negative = await task
        except asyncio.CancelledError:
            event('public_task_cancelled', pins=pins())
            require(kind == 'cancel', 'unexpected_timeout_task_cancel')
        else:
            event('public_task_return', pins=pins())
            require(kind == 'timeout', 'cancel_returned_normal_result')
        request_returned = True
        await asyncio.wait_for(popped.wait(), 5)
        event('drained', pins=pins(), locks=locks())
        require(state.get('gate_released_by_product') is True, 'gate_safety_cap_not_product_stop')
        require(not any(locks().values()) and all(v == 0 for v in pins().values()), 'lifecycle_resource_leak')
        session = state['session']
        require(not session.entries and session.events == session.nbytes == state['budget'].cache_bytes == 0,
                'observation_cache_not_cleared')
        require(all(not e.binding_lock.locked() and e.npi_failure is None for e in entries.values()),
                'entry_lock_or_fallback_leak')
        save('after_request_state.json', dict(cursors=cursors(), parsers=parser_state(), pins=pins(),
            entries=entry_state(), locks=locks(),
            session=dict(entries=len(session.entries), events=session.events, nbytes=session.nbytes)))
        require(parser_state() == before_parsers, 'lifecycle_changed_parser_identity')
        require([server._cursor_store.get(row['name']).as_dict() for row in before_cursors] == before_cursors,
                'baseline_cursor_changed')
        if kind == 'cancel':
            require(negative is None and cursors() == before_cursors and not publications, 'cancel_published')
            require(not [e for e in events if e['kind'].startswith('final_')], 'cancel_advanced_final')
        else:
            server.schemas.TraceDivergenceResult.model_validate(negative)
            structures(negative, 'trace_negative')
            published_contexts(negative)
            require(negative['status'] == 'partial' and negative['coverage']['truncated']
                    and negative['coverage']['gaps'] == ['budget_exhausted']
                    and negative['coverage']['checks'] == ['root_comparison', 'both_sides_driver_evidence'],
                    'timeout_coverage_oracle')
            require(negative['nodes'][0]['status'] == 'frontier' and not negative['findings']
                    and all('observation' not in negative['nodes'][0][s] for s in cases), 'timeout_uncommitted_observation')
            require(negative['frontier'] == [dict(node_id=None, reason='budget_exhausted', budget='timeout_sec'),
                    dict(node_id='n0', reason='budget_exhausted', budget='timeout_sec')], 'timeout_frontier')
            require([e['result'] for e in events if e['kind'] == 'final_return'] == [[]], 'timeout_final_failed')
            for event_name in ('final_source_current', 'final_anchor_current'):
                require([(e['side'], e['current']) for e in events if e['kind'] == event_name]
                        == [('a', True), ('b', True)], 'timeout_missing_final_validation')
            gate_exit = next(e for e in events if e['kind'] == 'gate_return')
            require(gate_exit['monotonic_ns'] / 1e9 >= state['budget'].deadline
                    and not [e for e in events if e['kind'] == 'public_task_cancel_issue'], 'not_real_deadline')
            require(len(publications) == 1 and publications[0]['result']['name'] == negative_name
                    and publications[0]['result']['time_ps'] == 20000
                    and len(cursors()) == len(before_cursors) + 1, 'timeout_cursor_contract')
            require(len(negative['next_actions']) == 2 and all(
                a['status'] == 'ready' and a['arguments']['design_handle'] == handles[a['side']]
                and a['evidence']['time_ps'] == 20000 and a['evidence']['cursor_name'] == negative_name
                for a in negative['next_actions']), 'timeout_ready_actions')
        # No further native computation was added after either gate.
        for key in ('sdk_load_design', 'get_dynamic_step', 'find_driver', 'query_source', 'fsdb_abi_total'):
            require(counts[key] == state['gate_counts'].get(key, 0), 'post_gate_native_work_' + key)
    finally:
        active = False
        async def drain():
            if task is not None and not task.done():
                event('failed_test_cleanup_cancel')
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            elif task is not None:
                await asyncio.gather(task, return_exceptions=True)
            # Drain the exact abandoned worker before observers/diagnostics.
            if state.get('cancel') is not None:
                await asyncio.wait_for(popped.wait(), 5)
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
                report['lifecycle_observers_restored'] = True
            else:
                report['lifecycle_observers_restored'] = False
                report['requires_exit_without_native_teardown'] = True
            report['request_returned_before_cleanup'] = request_returned
            save('lifecycle_events.json', events)

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
    recovered = await invoke('trace_recovered', 'trace_divergence', request('p4_s6_' + kind + '_recovered', 'same healthy handles'))
    full_graph(recovered, 'trace_recovered')
    require(parser_state() == before_parsers, 'recovery_replaced_healthy_parser')
    require(not any(locks().values()) and all(v == 0 for v in pins().values()), 'recovery_resource_leak')
    require([server._cursor_store.get(row['name']).as_dict() for row in before_cursors] == before_cursors,
            'recovery_overwrote_baseline')
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
                                entries=entry_state(), locks=locks(), cursors=cursors()))
    for row in inventory.values():
        tree = Path(row['kdb']['canonical'])
        actual = [(str(p.relative_to(tree)), hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in sorted(tree.rglob('*')) if p.is_file()]
        require(hashlib.sha256(json.dumps(actual, separators=(',', ':')).encode()).hexdigest()
                == row['kdb']['tree_content_sha256'], 'final_kdb_file_set_changed')
    save('input_hashes.json', [dict(path=p, sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest(), expected=d)
                              for p, d in baseline_files.items()])
    report['coverage'] = dict(real_dual_native_wave=True, completed_delegate_handoff_gate=True,
        lifecycle_kind=kind, original_handle_recovery=True, analysis_timeout_sec=30,
        gate_cap_sec=40, drain_cap_sec=5, native_mid_call_interruption='not_tested',
        typed_backend_identity_timeout='not_tested', license_checkout_release='unverified',
        source_wave_association='unverified', lsf='not_run')
