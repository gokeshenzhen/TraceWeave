"""Opt-in real source mutation probe for the isolated dual_trace_npi fixture.

The public trace, native steps and waveform observations are real. Delegating
observers retain unpublished evidence; only the explicitly selected source byte
is changed. A durable journal permits external recovery after process death.
This module is a test harness, never imported by the MCP server.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

from dual_trace_oracle import validate_step


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_fact(path):
    before = path.stat()
    data = path.read_bytes()
    return dict(path=str(path), sha256=digest(data), dev=before.st_dev,
                ino=before.st_ino, size=before.st_size, mode=stat.S_IMODE(before.st_mode),
                atime_ns=before.st_atime_ns, mtime_ns=before.st_mtime_ns,
                ctime_ns=before.st_ctime_ns)


async def source_mutation_chain(args, report, cases, inventory, server, counts,
                                measured, public, require, baseline_files):
    from src import divergence_trace
    from src.design_context import DesignContext
    from src.divergence_design_context import DivergenceDesigns
    from src.evidence_output import expand_compact_result

    side = args.mutation_side
    root = Path(args.mutation_root).resolve()
    repo = Path(args.repo).resolve()
    require('p4_s2_evidence' not in root.parts
            and not root.is_relative_to(repo / 'tests'), 'sealed_mutation_root')
    paths = {label: Path(inventory[label.upper()]['source']['path']).resolve() for label in cases}
    require(paths['a'] != paths['b'], 'aliased_mutation_sources')
    for label, path in paths.items():
        require(path == root / label / (label + '.sv')
                and Path(cases[label]['compile_log']).resolve().parent == path.parent
                and Path(cases[label]['wave_path']).resolve().parent == path.parent
                and Path(cases[label]['kdb_path']).resolve().is_relative_to(path.parent),
                'mutation_outside_authorized_fixture')
        require(path.read_bytes() == (repo / 'tests/fixtures/dual_trace_npi' / (label + '.sv')).read_bytes(),
                'mutation_source_not_fixture_template')
    out = Path(args.mutation_evidence_dir).resolve()
    require(not out.is_relative_to(root), 'evidence_inside_mutated_fixture')
    out.mkdir(parents=True, exist_ok=False)

    def save(name, value):
        with (out / name).open('x') as stream:
            json.dump(value, stream, indent=2, default=str)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())

    events = report['mutation_events'] = []
    phase = 'setup'
    final_active = False
    injected = False
    mutation_attempted = False
    injection_counts = None
    handles, identities, all_handles = {}, {}, []
    walks = report['unpublished_graphs'] = {}
    report['unpublished_graphs_note'] = 'Private pre-validation observations; rejected graphs are never public results.'
    report['mutation_side'] = side
    report['mutation_evidence_dir'] = str(out)
    target = paths[side]
    before = file_fact(target)
    original_bytes = target.read_bytes()
    old, new = (b"8'h0f", b"8'h0e") if side == 'a' else (b"8'hf0", b"8'hf1")
    require(original_bytes.count(old) == 1, 'mutation_token_not_unique')
    changed_bytes = original_bytes.replace(old, new, 1)
    offsets = [i for i, (a, b) in enumerate(zip(original_bytes, changed_bytes)) if a != b]
    require(len(original_bytes) == len(changed_bytes) and len(offsets) == 1, 'not_one_byte_mutation')
    offset = offsets[0]
    for name, data in (('original.sv', original_bytes), ('mutated.sv', changed_bytes)):
        with (out / name).open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    journal = dict(source=str(target), original_backup=str(out / 'original.sv'),
                   original=before, mutated_sha256=digest(changed_bytes), offset=offset,
                   old_byte=original_bytes[offset], new_byte=changed_bytes[offset],
                   recovery='After child exit only: restore original bytes in place, fsync, restore mode/atime/mtime; ctime cannot be restored.')
    save('recovery_journal.json', journal)
    # Persist directory entries too before the first possible source write.
    directory = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    report['mutation_journal'] = journal

    def event(kind, **facts):
        events.append(dict(index=len(events), monotonic_ns=time.monotonic_ns(),
                           phase=phase, kind=kind, **facts))

    def hashes(expected_mutation=False):
        rows = []
        for name, expected in baseline_files.items():
            actual = digest(Path(name).read_bytes())
            if expected_mutation and Path(name) == target:
                expected = digest(changed_bytes)
            rows.append(dict(path=name, expected_sha256=expected, actual_sha256=actual))
        require(all(row['expected_sha256'] == row['actual_sha256'] for row in rows),
                'unexpected_input_bytes_changed')
        return rows

    def pins():
        rows = {}
        for handle in all_handles:
            with server._design_context_store.pin(handle) as entry:
                rows[handle] = dict(leases_excluding_inspector=entry.leases - 1,
                                    npi_failure=entry.npi_failure)
                require(entry.leases == 1 and entry.npi_failure is None, 'pin_or_failure_state_leaked')
        return rows

    async def invoke(name, tool, arguments):
        nonlocal phase
        phase = name
        report.setdefault('requests', {})[name] = deepcopy(arguments)
        save(name + '.request.json', dict(tool=tool, arguments=arguments))
        result = await measured(name, lambda: public(tool, arguments))
        save(name + '.response.json', result)
        save(name + '.pins.json', pins())
        return result

    async def prepare(iteration):
        for label, case in cases.items():
            before_native = counts.copy()
            arguments = dict(compile_log=case['compile_log'], simulator='vcs',
                             top_hint=case['top_hint'], timeout_sec=30)

            async def pair():
                return await asyncio.gather(
                    invoke('prepare_' + label + '_' + iteration, 'prepare_design_context', arguments),
                    invoke('scan_' + label + '_' + iteration, 'scan_structural_risks',
                           dict(compile_log=case['compile_log'], simulator='vcs', analysis_mode='fast')))

            prepared, scan = await measured('prepare_scan_' + label + '_' + iteration, pair)
            require(not prepared.get('error') and not scan.get('error'), 'prepare_or_scan_error')
            require(prepared['semantic_status'] == 'not_run' and prepared['kdb_status'] == 'not_checked',
                    'prepare_claimed_semantics')
            require(scan['lexical_coverage_status'] == 'complete'
                    and scan['semantic']['status'] == 'not_run', 'unexpected_scan_coverage')
            require(all(counts[key] == before_native[key] for key in ('sdk_init', 'sdk_load_design', 'get_dynamic_step')),
                    'prepare_scan_started_native')
            old_handle = handles.get(label)
            handles[label], identities[label] = prepared['design_handle'], prepared['context_identity_sha256']
            require(handles[label] != old_handle, 'recovery_reused_old_handle')
            all_handles.append(handles[label])
        require(handles['a'] != handles['b'] and identities['a'] != identities['b'], 'aliased_contexts')

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

    def graph(result, name, published):
        comp = result['comparison']
        require(comp['diverged'] is True and comp['first_divergence_time_ps'] == 20000
                and comp['earliest_difference_proven'] is True
                and comp['value_a'] == '00110011' and comp['value_b'] == '11001100', 'root_wave_oracle')
        require(comp['coverage_status'] == 'partial'
                and {row['reason'] for row in comp['coverage_gaps']} == {'stopped_at_difference'},
                'tail_coverage_hidden')
        require(len(result['nodes']) == 1 and not result['edges'] and not result['frontier']
                and not result['coverage']['gaps'], 'unexpected_local_graph')
        require(len(result['findings']) == 1
                and result['findings'][0]['kind'] == 'local_logic_candidate'
                and result['findings'][0]['checked_sides'] == ['a', 'b']
                and result['findings'][0]['competing_explanations']
                and result['findings'][0]['limitations'], 'overclaimed_finding')
        steps = [row for row in report['dynamic_steps'] if row['phase'] == name]
        require([row['side'] for row in steps] == ['a', 'b'], 'missing_dual_native_steps')
        require([row['side'] for row in report['loads'] if row['phase'] == name] == ['a', 'b'],
                'unexpected_trace_loads')
        for label, value, mask in (('a', '00110011', 15), ('b', '11001100', 240)):
            case = cases[label]
            raw = next(row for row in steps if row['side'] == label)
            require(raw['loaded_top'] == case['top_hint']
                    and Path(raw['loaded_kdb']).resolve() == Path(case['kdb_path']).resolve(), 'crossed_native_design')
            validate_step(raw['result'], case, str(paths[label]), mask, require)
            node = result['nodes'][0][label]
            structure, observation = node['structure'], node['observation']
            # The private walk retains tuple selections; public JSON uses lists.
            # Compare the same ordered coordinates without changing the oracle.
            require(tuple(node['bits']) == tuple(range(7, -1, -1)) and node['value'] == value
                    and structure['backend'] == 'verdi_npi' and structure['complete'] is True
                    and not structure['gaps'] and structure['boundary'] == 'combinational', 'structure_oracle')
            require(observation['value'] == value and observation['complete'] is True
                    and observation['sampling_time_ps'] == observation['observation_time_ps'] == 20000
                    and observation['sampling_phase'] == observation['observation_phase'] == 'after'
                    and observation['trigger_time_ps'] is None and not observation['gaps'], 'wave_observation_oracle')
            expected = {case['top_hint'] + '.data': ('00111100', list(range(7, -1, -1))),
                        case['top_hint'] + '.mode': ('1', [0])}
            found = {}
            for dep in observation['dependencies']:
                require(dep['signal'] in expected and dep['time_ps'] == 20000
                        and dep['phase'] == 'after' and not dep['gaps'], 'dependency_identity')
                val, bits = expected[dep['signal']]
                require(dep['value'] == ''.join(val[bits.index(bit)] for bit in dep['bits']), 'opposite_side_values')
                found.setdefault(dep['signal'], set()).update(dep['bits'])
            require(found == {name: set(bits) for name, (_, bits) in expected.items()}, 'missing_dependencies')
            if published:
                ctx = result['contexts'][label]
                dc, backend = ctx['design_context'], ctx['backend_status']
                require(dc['design_handle'] == handles[label]
                        and dc['context_identity_sha256'] == identities[label], 'crossed_context')
                require(backend['actual_backend'] == backend['selected_backend'] == 'verdi_npi'
                        and backend['execution_mode'] == 'local' and not backend.get('fallback_reason')
                        and backend['single_backend_provenance'] is True
                        and backend['whole_trace_restart_count'] == 0
                        and backend['kdb_load_status'] == 'loaded'
                        and backend['artifact_sha256'] == structure['artifact_sha256'], 'trace_backend_receipt')
                require(dc['artifact']['source_association'] == 'unverified'
                        and ctx['waveform_compile_version_relation'] == 'not_independently_proven', 'association_upgraded')
        require(result['nodes'][0]['a']['structure']['artifact_sha256']
                != result['nodes'][0]['b']['structure']['artifact_sha256'], 'aliased_graph_artifacts')
        if published:
            server.schemas.TraceDivergenceResult.model_validate(result)
            require(result['status'] == 'complete' and result['cursor']['time_ps'] == 20000
                    and not result['coverage']['truncated'] and not result['coverage']['restart_reasons'],
                    'positive_trace_status')

    original_walk = divergence_trace._walk
    original_final = DivergenceDesigns.validate_final
    original_current = DesignContext.current

    async def walk(result, *a, **kw):
        returned = await original_walk(result, *a, **kw)
        require(phase not in walks, 'unexpected_graph_restart')
        walks[phase] = deepcopy(result)
        save(phase + '.unpublished.json', walks[phase])
        event('walk_observations_completed', checked_sides=['a', 'b'])
        return returned

    def current(context, **kw):
        if not final_active:
            return original_current(context, **kw)
        label = next((key for key in cases if context.identity_sha256 == identities[key]), None)
        event('final_source_current_enter', side=label)
        try:
            value = original_current(context, **kw)
            event('final_source_current_return', side=label, current=value)
            return value
        except BaseException as exc:
            event('final_source_current_exception', side=label, exception=type(exc).__name__)
            raise

    async def final(designs, *a, **kw):
        nonlocal final_active, injected, mutation_attempted, injection_counts
        event('final_enter')
        require(set(designs.entries) == set(handles.values()), 'unexpected_final_handles')
        require(not any(lock.locked() for lock in server._wave_locks_for(
            [case['wave_path'] for case in cases.values()])), 'mutation_under_wave_lock')
        if phase == 'trace_negative':
            require(not injected and phase in walks, 'late_injection_not_reached')
            graph(walks[phase], phase, False)
            require(all(entry.leases == 1 for entry in designs.entries.values()), 'missing_trace_pins')
            save('pre_injection_hashes.json', hashes())
            require(target.read_bytes() == original_bytes, 'target_changed_before_injection')
            mutation_attempted = True
            event('source_write_begin', side=side)
            with target.open('r+b') as stream:
                stream.seek(offset)
                stream.write(changed_bytes[offset:offset + 1])
                stream.flush()
                os.fsync(stream.fileno())
            after = file_fact(target)
            require(target.read_bytes() == changed_bytes and after['sha256'] != before['sha256']
                    and all(after[key] == before[key] for key in ('dev', 'ino', 'size', 'mode')),
                    'actual_byte_mutation_not_proven')
            report['mutation_after'] = after
            save('mutation_after.json', after)
            save('mutated_input_hashes.json', hashes(True))
            event('source_write_verified', side=side, sha256=after['sha256'])
            injected = True
            injection_counts = counts.copy()
        final_active = True
        event('final_delegate_enter')
        try:
            result = await original_final(designs, *a, **kw)
            event('final_delegate_return', result=result)
            return result
        except BaseException as exc:
            event('final_delegate_exception', exception=type(exc).__name__, code=getattr(exc, 'code', None))
            raise
        finally:
            final_active = False

    divergence_trace._walk, DivergenceDesigns.validate_final, DesignContext.current = walk, final, current
    try:
        await prepare('initial')
        old_handles, old_identities = handles.copy(), identities.copy()
        baseline_cursor = 'p4_s3_' + side + '_baseline'
        baseline = await invoke('trace_baseline', 'trace_divergence', request(baseline_cursor, 'baseline must survive'))
        graph(baseline, 'trace_baseline', True)
        actions = deepcopy(baseline['next_actions'])
        require(len(actions) == 2 and {row['side'] for row in actions} == {'a', 'b'}
                and all(row['status'] == 'ready' and row['tool'] == 'explain_signal_driver' for row in actions),
                'baseline_actions_missing')
        cursor_before = deepcopy([row.as_dict() for row in server._cursor_store.list()])
        save('cursor_before.json', cursor_before)
        original_set, original_auto = server._cursor_store.set, server._cursor_store.auto_set
        publications = report['negative_cursor_publications'] = []

        def observed_set(*a, **kw):
            publications.append(dict(method='set', arguments=a, kwargs=kw))
            return original_set(*a, **kw)

        def observed_auto(*a, **kw):
            publications.append(dict(method='auto_set', arguments=a, kwargs=kw))
            return original_auto(*a, **kw)

        server._cursor_store.set, server._cursor_store.auto_set = observed_set, observed_auto
        try:
            rejected = await invoke('trace_negative', 'trace_divergence', request(baseline_cursor, 'must never overwrite'))
            server.schemas.TraceDivergenceResult.model_validate(rejected)
            require(injected and rejected['status'] == 'inconclusive'
                    and rejected['coverage']['gaps'] == ['design_context_changed'], 'wrong_rejection')
            require(rejected['comparison'] == {} and rejected['nodes'] == rejected['edges'] == rejected['findings'] == []
                    and rejected.get('cursor') is None, 'stale_trace_published')
            require(rejected['frontier'] == [dict(node_id=None, reason='design_context_changed')], 'stale_frontier')
            require(all(counts[key] == injection_counts[key] for key in
                        ('sdk_init', 'sdk_load_design', 'get_dynamic_step', 'find_driver', 'query_source')),
                    'native_continued_after_mutation')
            validated = [(e['side'], e['current']) for e in events
                         if e['phase'] == 'trace_negative' and e['kind'] == 'final_source_current_return']
            require(validated == ([('a', False)] if side == 'a' else [('a', True), ('b', False)]),
                    'wrong_final_side_order')
            recoveries = rejected['next_actions']
            require(len(recoveries) == 2 and {row['side'] for row in recoveries} == {'a', 'b'}, 'missing_recoveries')
            for row in recoveries:
                label = row['side']
                require(row['status'] in {'needs_context', 'needs_prerequisite'}
                        and row['evidence']['context_status'] == 'design_context_changed'
                        and row['evidence'].get('time_ps') is None
                        and 'cursor_name' not in row['evidence']
                        and 'design_handle' not in row['arguments'], 'stale_recovery_action')
                calls = row['prerequisite_calls']
                require({call['tool'] for call in calls} == {'prepare_design_context', 'scan_structural_risks'}
                        and len(calls) == 2 and all(not call['depends_on'] for call in calls)
                        and len({call['parallel_group'] for call in calls}) == 1, 'incorrect_recovery_workflow')
                call = next(call for call in calls if call['tool'] == 'prepare_design_context')
                require(call['arguments']['compile_log'] == cases[label]['compile_log']
                        and set(call['arguments']) <= {'compile_log', 'simulator', 'supplementary_compile_logs',
                                                     'top_hint', 'backend_preference', 'kdb_path'},
                        'stale_recovery_identity')
            for label in ('a', 'b'):
                action = next(row for row in actions if row['side'] == label)
                require(action['arguments'] == dict(design_handle=handles[label], wave_path=cases[label]['wave_path'],
                                                   signal_path=cases[label]['signal_path'], recursive=False), 'crossed_old_action')
                prior = counts.copy()
                raw = await invoke('action_' + label + '_old', action['tool'], action['arguments'])
                if label == side:
                    require(raw.get('error_code') == 'design_context_changed'
                            and not any(key in raw for key in ('driver_chain', 'driver_status', 'backend_status')),
                            'stale_action_accepted')
                    require(all(counts[key] == prior[key] for key in ('sdk_load_design', 'find_driver', 'query_source')),
                            'stale_action_started_native')
                else:
                    result = expand_compact_result(raw) if raw.get('format') else raw
                    status = result.get('backend_status', {})
                    require(status.get('actual_backend') == 'verdi_npi'
                            and result.get('driver_status') == 'resolved'
                            and result.get('signal_path') == cases[label]['signal_path']
                            and result.get('resolved_module') == cases[label]['top_hint']
                            and Path(result.get('source_file', '')).resolve() == paths[label]
                            and result.get('source_line') == 8
                            and status['design_context']['design_handle'] == handles[label], 'opposite_action_invalid')
                    require(counts['find_driver'] - prior['find_driver'] == 1
                            and counts['sdk_load_design'] - prior['sdk_load_design'] == 1, 'opposite_action_not_native')
            cursor_after = [row.as_dict() for row in server._cursor_store.list()]
            save('cursor_after_rejection_actions.json', cursor_after)
            require(not publications and cursor_after == cursor_before, 'old_cursor_modified')
        finally:
            server._cursor_store.set, server._cursor_store.auto_set = original_set, original_auto
            original_error = sys.exc_info()[0]
            started = time.perf_counter(), time.process_time()
            try:
                if mutation_attempted:
                    event('restore_begin')
                    with target.open('r+b') as stream:
                        stream.write(original_bytes)
                        stream.truncate()
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(target, before['mode'])
                    require(target.read_bytes() == original_bytes, 'restore_bytes_failed')
                    save('restored_input_hashes.json', hashes())
                    os.utime(target, ns=(before['atime_ns'], before['mtime_ns']))
                    restored = target.stat()
                    require((restored.st_dev, restored.st_ino, restored.st_size,
                             stat.S_IMODE(restored.st_mode), restored.st_atime_ns, restored.st_mtime_ns)
                            == tuple(before[key] for key in ('dev', 'ino', 'size', 'mode', 'atime_ns', 'mtime_ns')),
                            'restore_metadata_failed')
                    report['source_restoration'] = dict(sha256=digest(original_bytes), metadata_restored=True,
                        ctime_ns=restored.st_ctime_ns, ctime_restored=False, original_error=original_error is not None)
                    event('restore_completed', **report['source_restoration'])
                    save('restoration.json', report['source_restoration'])
            except BaseException as exc:
                report['source_restoration_error'] = dict(type=type(exc).__name__, message=str(exc))
                if original_error is None:
                    raise
            finally:
                report['restoration_cost'] = dict(wall_ms=1000*(time.perf_counter()-started[0]),
                                                  cpu_ms=1000*(time.process_time()-started[1]))
        # The one-shot write is now disabled; transparent observation delegates remain.
        await prepare('restored')
        report['reprepared'] = dict(old_handles=old_handles, new_handles=handles.copy(),
                                  old_identities=old_identities, new_identities=identities.copy())
        recovered = await invoke('trace_restored', 'trace_divergence',
                                 request('p4_s3_' + side + '_restored', 'new preparation after byte restoration'))
        graph(recovered, 'trace_restored', True)
        require([server._cursor_store.get(row['name']).as_dict() for row in cursor_before] == cursor_before,
                'recovery_overwrote_old_cursor')

        async def final_identities():
            rows = {}
            for label in cases:
                with server._design_context_store.pin(handles[label]) as entry:
                    await server._validate_design_source(entry.context, timeout_sec=30)
                    await server._validate_design_binding(entry.binding, timeout_sec=5)
                    rows[label] = dict(context=entry.context.identity_sha256,
                                       artifact=entry.binding.anchor.receipt(),
                                       snapshot=[record.identity_record() for record in entry.context.snapshot.records])
                    require(Path(entry.binding.anchor.canonical_path) == Path(cases[label]['kdb_path']).resolve(),
                            'recovered_foreign_binding')
            return rows

        report['final_identity_validation'] = await measured('final_identity_validation', final_identities)
        save('final_identity_validation.json', report['final_identity_validation'])
        save('final_pins.json', pins())
        require(not any(lock.locked() for lock in server._wave_locks_for(
            [case['wave_path'] for case in cases.values()])), 'final_wave_lock_leaked')
        report['wave_locks_released'] = True
        report['coverage'] = dict(real_source_byte_mutation=True, mutation_side=side,
            same_size=True, same_mtime=False, raw_hash_only_detection='not_claimed',
            dual_observations_before_rejection=True, old_actions_both_sides_checked=True,
            restored_bytes_new_prepare=True, full_hierarchy='guarded_zero',
            cancellation_timeout_native_integration='not_run', kdb_wave_mutation='not_run',
            synchronous_deep_trace='not_run', license_checkout_release='unverified', lsf='not_run')
    finally:
        divergence_trace._walk, DivergenceDesigns.validate_final, DesignContext.current = (
            original_walk, original_final, original_current)
        save('events.json', events)
        report['observation_wrappers_restored'] = True
