"""Opt-in real KDB-map mutation probe for the isolated dual_trace_npi fixture.

The public trace, native steps and waveform observations are real. Delegating
observers retain unpublished evidence; only one covered elaboration-map byte
is changed. Native calls are forbidden until original bytes are restored. A
durable journal permits external recovery after process death. This separate
entry deliberately leaves the accepted source-mutation probe unchanged.
This module is a test harness, never imported by the MCP server.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import traceback

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


async def kdb_mutation_chain(args, report, cases, inventory, server, counts,
                                measured, public, require, baseline_files):
    from src import divergence_trace
    from src.design_context import DesignContext
    from src.divergence_design_context import DivergenceDesigns
    from src.evidence_output import expand_compact_result
    from src.kdb_anchor import KdbAnchor

    side = args.mutation_side
    root = Path(args.mutation_root).resolve()
    repo = Path(args.repo).resolve()
    require(not {'p4_s2_evidence', 'p4_s3_evidence'}.intersection(root.parts)
            and root.name == 'fixture' and root.parent.name == 'p4_s4_evidence'
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
    target = Path(cases[side]['kdb_path']) / '_nstnElabDB'
    require(not target.is_symlink() and stat.S_ISREG(target.lstat().st_mode)
            and target.stat().st_nlink == 1
            and target.resolve().is_relative_to(root / side), 'nonregular_or_foreign_kdb_map')
    before = file_fact(target)
    original_bytes = target.read_bytes()
    require(original_bytes.hex() == '415512580100000004030201000000001800000000000000',
            'unreviewed_kdb_map_format')
    changed_bytes = bytes([original_bytes[0] ^ 1]) + original_bytes[1:]
    offsets = [i for i, (a, b) in enumerate(zip(original_bytes, changed_bytes)) if a != b]
    require(len(original_bytes) == len(changed_bytes) and len(offsets) == 1, 'not_one_byte_mutation')
    offset = offsets[0]
    for name, data in (('original.kdb-map', original_bytes), ('mutated.kdb-map', changed_bytes)):
        with (out / name).open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    journal = dict(target=str(target), kind='kdb_elaboration_map',
                   fixture_root=str(root), side=side, map_name=target.name,
                   canonical_target=str(target.resolve()),
                   original_backup=str(out / 'original.kdb-map'),
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
    report['kdb_mutation_in_progress'] = False

    def event(kind, **facts):
        events.append(dict(index=len(events), monotonic_ns=time.monotonic_ns(),
                           phase=phase, kind=kind, **facts))

    native_keys = ('sdk_init', 'sdk_load_design', 'sdk_end', 'lang_release_all_handles',
                   'get_dynamic_step', 'find_driver', 'query_source')

    def native_counts():
        return {key: counts[key] for key in native_keys}

    def sync_directory():
        fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

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

    async def replay_action(label, actions):
        action = next(row for row in actions if row['side'] == label)
        require(action['arguments'] == dict(design_handle=handles[label], wave_path=cases[label]['wave_path'],
                                           signal_path=cases[label]['signal_path'], recursive=False), 'crossed_old_action')
        prior = counts.copy()
        raw = await invoke('action_' + label + '_old', action['tool'], action['arguments'])
        if label == side:
            require(raw.get('error_code') == 'kdb_identity_changed'
                    and not any(key in raw for key in ('driver_chain', 'driver_status', 'backend_status')),
                    'stale_action_accepted')
            require(all(counts[key] == prior[key] for key in ('sdk_init', 'sdk_load_design', 'get_dynamic_step', 'find_driver', 'query_source')),
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

    original_walk = divergence_trace._walk
    original_final = DivergenceDesigns.validate_final
    original_current = DesignContext.current
    original_anchor_current = KdbAnchor.current

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

    def anchor_current(anchor, **kw):
        if not final_active:
            return original_anchor_current(anchor, **kw)
        label = next((key for key in cases if Path(anchor.canonical_path)
                      == Path(cases[key]['kdb_path']).resolve()), None)
        event('final_anchor_current_enter', side=label)
        try:
            value = original_anchor_current(anchor, **kw)
            event('final_anchor_current_return', side=label, current=value)
            return value
        except BaseException as exc:
            event('final_anchor_current_exception', side=label, exception=type(exc).__name__)
            raise

    def anchors():
        rows = {}
        for label in cases:
            with server._design_context_store.pin(handles[label]) as entry:
                anchor = entry.binding.anchor
                require(anchor.identity_complete and anchor.artifact_status == 'elaborated_candidate'
                        and Path(anchor.canonical_path) == Path(cases[label]['kdb_path']).resolve(),
                        'unreviewed_anchor')
                record = next(row for row in anchor.map_records if row.name == '_nstnElabDB')
                expected_target = Path(cases[label]['kdb_path']) / record.name
                info = expected_target.stat()
                require(record.disposition == 'present' and record.sha256 == digest(expected_target.read_bytes())
                        and stat.S_ISREG(expected_target.lstat().st_mode)
                        and info.st_nlink == 1
                        and tuple(record.stat_identity) == (info.st_dev, info.st_ino, info.st_mode,
                            info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        and not expected_target.is_symlink()
                        and record.canonical_path == str(expected_target.resolve()), 'target_not_anchor_covered')
                rows[label] = asdict(anchor)
        return rows

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
            save('pre_injection_anchors.json', anchors())
            save('pre_injection_hashes.json', hashes())
            require(target.read_bytes() == original_bytes, 'target_changed_before_injection')
            mutation_attempted = True
            # The outer probe treats any native entry while this latch is set
            # as a test failure, never as successful product rejection.
            report['kdb_mutation_in_progress'] = True
            report['corrupt_interval_counts_before'] = native_counts()
            save('write_intent.json', dict(target=str(target), side=side,
                 original_sha256=before['sha256'], mutated_sha256=digest(changed_bytes),
                 native_counts=native_counts(), monotonic_ns=time.monotonic_ns()))
            sync_directory()
            event('kdb_write_begin', side=side)
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
            event('kdb_write_verified', side=side, sha256=after['sha256'])
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
    KdbAnchor.current = anchor_current
    try:
        await prepare('initial')
        old_handles, old_identities = handles.copy(), identities.copy()
        baseline_cursor = 'p4_s4_' + side + '_baseline'
        baseline = await invoke('trace_baseline', 'trace_divergence', request(baseline_cursor, 'baseline must survive'))
        graph(baseline, 'trace_baseline', True)
        report['baseline_anchors'] = anchors()
        save('baseline_anchors.json', report['baseline_anchors'])
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
                    and rejected['coverage']['gaps'] == ['kdb_identity_changed'], 'wrong_rejection')
            require(rejected['comparison'] == {} and rejected['nodes'] == rejected['edges'] == rejected['findings'] == []
                    and rejected.get('cursor') is None, 'stale_trace_published')
            require(rejected['frontier'] == [dict(node_id=None, reason='kdb_identity_changed')], 'stale_frontier')
            require(all(counts[key] == injection_counts[key] for key in native_keys),
                    'native_continued_after_mutation')
            validated = [(e['side'], e['current']) for e in events
                         if e['phase'] == 'trace_negative' and e['kind'] == 'final_source_current_return']
            require(validated == ([('a', True)] if side == 'a' else [('a', True), ('b', True)]),
                    'source_changed_in_kdb_test')
            checked = [(e['side'], e['current']) for e in events
                       if e['phase'] == 'trace_negative' and e['kind'] == 'final_anchor_current_return']
            require(checked == ([('a', False)] if side == 'a' else [('a', True), ('b', False)]),
                    'wrong_final_anchor_side_order')
            final_events = [e for e in events if e['phase'] == 'trace_negative']
            require(sum(e['kind'] == 'final_delegate_enter' for e in final_events) == 1
                    and [e['exception'] for e in final_events if e['kind'] == 'final_delegate_exception']
                        == ['KdbBindingChanged'], 'not_original_typed_final_rejection')
            recoveries = rejected['next_actions']
            require(len(recoveries) == 2 and {row['side'] for row in recoveries} == {'a', 'b'}, 'missing_recoveries')
            for row in recoveries:
                label = row['side']
                require(row['status'] in {'needs_context', 'needs_prerequisite'}
                        and row['evidence']['context_status'] == 'kdb_identity_changed'
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
            await replay_action(side, actions)
            cursor_after = [row.as_dict() for row in server._cursor_store.list()]
            save('cursor_after_rejection_actions.json', cursor_after)
            require(not publications and cursor_after == cursor_before, 'old_cursor_modified')
        finally:
            server._cursor_store.set, server._cursor_store.auto_set = original_set, original_auto
            original_error = sys.exc_info()[0]
            started = time.perf_counter(), time.process_time()
            try:
                if original_error is not None:
                    failure = dict(type=original_error.__name__, traceback=traceback.format_exc())
                    report['failure_before_restore'] = failure
                    try:
                        save('original_failure_before_restore.json', failure)
                    except OSError as exc:
                        report['failure_record_error'] = str(exc)
                if mutation_attempted:
                    event('restore_begin')
                    report['corrupt_interval_counts_after'] = native_counts()
                    save('corrupt_interval_counts.json', dict(
                        before=report['corrupt_interval_counts_before'], after=native_counts()))
                    require(not target.is_symlink() and target.stat().st_nlink == 1
                            and (target.stat().st_dev, target.stat().st_ino) == (before['dev'], before['ino'])
                            and digest(target.read_bytes()) in {before['sha256'], digest(changed_bytes)},
                            'restore_unknown_target_quarantine')
                    with target.open('r+b') as stream:
                        stream.write(original_bytes)
                        stream.truncate()
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(target, before['mode'])
                    require(target.read_bytes() == original_bytes, 'restore_bytes_failed')
                    save('restored_input_hashes.json', hashes())
                    os.utime(target, ns=(before['atime_ns'], before['mtime_ns']))
                    with target.open('rb') as stream:
                        os.fsync(stream.fileno())
                    restored = target.stat()
                    require((restored.st_dev, restored.st_ino, restored.st_size,
                             stat.S_IMODE(restored.st_mode), restored.st_atime_ns, restored.st_mtime_ns)
                            == tuple(before[key] for key in ('dev', 'ino', 'size', 'mode', 'atime_ns', 'mtime_ns')),
                            'restore_metadata_failed')
                    report['kdb_restoration'] = dict(sha256=digest(original_bytes), metadata_restored=True,
                        ctime_ns=restored.st_ctime_ns, ctime_restored=False, original_error=original_error is not None)
                    event('restore_completed', **report['kdb_restoration'])
                    save('restoration.json', report['kdb_restoration'])
                    report['kdb_mutation_in_progress'] = False
            except BaseException as exc:
                report['kdb_restoration_error'] = dict(type=type(exc).__name__, message=str(exc))
                if original_error is None:
                    raise
            finally:
                report['restoration_cost'] = dict(wall_ms=1000*(time.perf_counter()-started[0]),
                                                  cpu_ms=1000*(time.process_time()-started[1]))
        require(not report['kdb_mutation_in_progress'], 'kdb_restore_unconfirmed')
        require(report['corrupt_interval_counts_before'] == report['corrupt_interval_counts_after']
                and not report.get('native_fence_failed'), 'native_work_during_corrupt_interval')
        # No native operation, including switching away from a loaded design,
        # is permitted while the map is altered. Replay the independent side
        # only after complete restoration, still using its original handle.
        event('opposite_action_after_restoration')
        await replay_action('b' if side == 'a' else 'a', actions)
        cursor_after_both = [row.as_dict() for row in server._cursor_store.list()]
        save('cursor_after_both_actions.json', cursor_after_both)
        require(cursor_after_both == cursor_before, 'old_cursor_modified_after_restore')
        # The one-shot write is now disabled; transparent observation delegates remain.
        await prepare('restored')
        require(identities == old_identities, 'source_identity_changed_by_kdb_only_mutation')
        report['reprepared'] = dict(old_handles=old_handles, new_handles=handles.copy(),
                                  old_identities=old_identities, new_identities=identities.copy())
        recovered = await invoke('trace_restored', 'trace_divergence',
                                 request('p4_s4_' + side + '_restored', 'new preparation after byte restoration'))
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
        report['restored_anchors'] = anchors()
        save('restored_anchors.json', report['restored_anchors'])
        save('final_pins.json', pins())
        require(not any(lock.locked() for lock in server._wave_locks_for(
            [case['wave_path'] for case in cases.values()])), 'final_wave_lock_leaked')
        report['wave_locks_released'] = True
        report['coverage'] = dict(real_kdb_map_byte_mutation=True, mutation_side=side,
            same_size=True, same_mtime=False, raw_hash_only_detection='not_claimed',
            dual_observations_before_rejection=True, old_actions_both_sides_checked=True,
            restored_bytes_new_prepare=True, full_hierarchy='guarded_zero',
            cancellation_timeout_native_integration='not_run', source_wave_mutation='not_run',
            unchanged_side_action_timing='after_byte_restoration_before_new_prepare',
            unchanged_side_native_during_mutation='not_run_for_native_safety',
            identity_scope='bounded_elaboration_maps_not_entire_kdb',
            synchronous_deep_trace='not_run', license_checkout_release='unverified', lsf='not_run')
    finally:
        divergence_trace._walk, DivergenceDesigns.validate_final, DesignContext.current = (
            original_walk, original_final, original_current)
        KdbAnchor.current = original_anchor_current
        save('events.json', events)
        report['observation_wrappers_restored'] = True
