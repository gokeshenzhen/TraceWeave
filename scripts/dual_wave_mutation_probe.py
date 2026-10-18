"""Opt-in real waveform-byte mutation probe for the isolated dual_trace_npi fixture.

A dedicated FSDB lifecycle experiment: finish both real observations, close
only the changed reader while bytes are healthy, verify descriptor isolation,
then change one byte under the global wave lock. Final validation must reject
without native access. Restore before actions, parser rebuild or diagnostics.
This module is a test harness, never imported by the MCP server.
"""
from __future__ import annotations

import asyncio
import fcntl
import threading
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


# Keep closed readers alive through dirty-error handling; main uses os._exit
# after durable output if restoration failed, so no destructor can reach FFR.
_KEEPALIVE = []
FSDB_ABI = (
    'fsdb_batch_window_transitions', 'fsdb_begin_lazy_transition_group_v1',
    'fsdb_begin_transition_group', 'fsdb_close', 'fsdb_end_transition_group',
    'fsdb_event_close_v1', 'fsdb_event_count_index_v1', 'fsdb_event_count_v1',
    'fsdb_event_end_tick_v1', 'fsdb_event_open_v1', 'fsdb_event_page_v1',
    'fsdb_event_page_version', 'fsdb_get_end_time', 'fsdb_get_loaded_transitions',
    'fsdb_get_multi_signals_around_time', 'fsdb_get_scale_info', 'fsdb_get_signal_count',
    'fsdb_get_signal_metadata_v1', 'fsdb_get_summary_paths_v1', 'fsdb_get_transitions',
    'fsdb_get_transitions_profiled', 'fsdb_get_value_at_time', 'fsdb_metadata_version',
    'fsdb_open', 'fsdb_reader_status', 'fsdb_scope_page_v1', 'fsdb_scope_page_version',
    'fsdb_search_signals',
)


def file_references(target, exclusions):
    """Observe same-UID readers; unknown unreadable processes block the write.

    Exact pre-reviewed OS credential/session services and other UIDs are
    explicit exclusions, never evidence of absence. This is not an atomic
    guarantee against a noncooperating external process opening the file.
    """
    info = target.stat()
    fds, mappings, skipped, checked, errors = [], [], [], [], []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            uid = process.stat().st_uid
            if uid != os.getuid():
                continue
            raw = (process / 'stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            comm = (process / 'comm').read_text().strip()
            identity = dict(pid=int(process.name), uid=uid, starttime=fields[19], comm=comm)
            if fields[0] == 'Z':
                skipped.append(dict(**identity, reason='zombie_no_running_userspace'))
                continue
            if any(all(identity[k] == row[k] for k in identity) for row in exclusions):
                skipped.append(dict(**identity, reason='reviewed_credential_service_outside_task'))
                continue
            for path in (process / 'fd').iterdir():
                try:
                    value = path.stat()
                except FileNotFoundError:
                    continue
                if (value.st_dev, value.st_ino) == (info.st_dev, info.st_ino):
                    try:
                        spelling = os.readlink(path)
                    except FileNotFoundError:
                        spelling = 'vanished_after_positive_inode_match'
                    fds.append(dict(**identity, fd=path.name, target=spelling))
            for line in (process / 'maps').read_text().splitlines():
                parts = line.split(maxsplit=5)
                major, minor = (int(value, 16) for value in parts[3].split(':'))
                if (os.makedev(major, minor), int(parts[4])) == (info.st_dev, info.st_ino):
                    mappings.append(dict(**identity, mapping=line))
            again = (process / 'stat').read_text()
            current = again[again.rfind(')') + 2:].split()
            if current[19] != identity['starttime']:
                errors.append(dict(pid=int(process.name), reason='pid_reused_during_scan'))
            else:
                checked.append(identity)
        except FileNotFoundError:
            if process.exists():
                errors.append(dict(pid=int(process.name), reason='incomplete_live_process_inventory'))
        except OSError as exc:
            errors.append(dict(pid=int(process.name), reason=type(exc).__name__))
    return dict(dev=info.st_dev, ino=info.st_ino, fds=fds, mappings=mappings,
                checked=checked, excluded=skipped, errors=errors, other_uids='not_checked')


def install_wave_observers(server, report, counts, patches, reject, require, phase, main_thread, *, role=None):
    """Observe actual ABI delegates, preserving ctypes signature assignments."""
    from src import fsdb_parser

    original_setup, original_load = fsdb_parser._setup, fsdb_parser._load_wrapper
    original_parser = server._get_parser
    native_handles = {}
    report['fsdb_abi_calls'] = []
    report['fsdb_abi_exports'] = list(FSDB_ABI)

    class NativeCall:
        def __init__(self, fn, name):
            object.__setattr__(self, '_fn', fn)
            object.__setattr__(self, '_name', name)

        def __getattr__(self, name):
            return getattr(self._fn, name)

        def __setattr__(self, name, value):
            # _setup writes these AFTER proxy installation. Forwarding is
            # mandatory: otherwise fsdb_open defaults to int and loses bits.
            setattr(self._fn, name, value)

        def __call__(self, *args, **kwargs):
            if report.get('wave_mutation_in_progress'):
                reject('fsdb_abi_during_wave_mutation')
            require(threading.get_ident() != main_thread and server._FSDB_WAVE_LOCK.locked(),
                    'fsdb_abi_outside_worker_lock')
            counts['fsdb_abi_total'] += 1
            counts['fsdb_abi_' + self._name] += 1
            path = (os.fsdecode(args[0]) if self._name == 'fsdb_open'
                    else native_handles.get(args[0]) if args else None)
            row = dict(index=len(report['fsdb_abi_calls']), phase=phase(), api=self._name,
                       path=path, delegate_started_ns=time.monotonic_ns())
            if role is not None:
                from src.cancellation import _cancel_event
                row.update(role=role(), thread=threading.get_ident(), event_id=id(_cancel_event.get()))
            report['fsdb_abi_calls'].append(row)
            try:
                result = self._fn(*args, **kwargs)
                row['returned'] = True
                if self._name == 'fsdb_open' and result:
                    native_handles[result] = path
                elif self._name == 'fsdb_close':
                    native_handles.pop(args[0], None)
                return result
            finally:
                row['delegate_finished_ns'] = time.monotonic_ns()

    def setup(lib):
        if report.get('wave_mutation_in_progress'):
            reject('fsdb_setup_during_wave_mutation')
        for name in FSDB_ABI:
            fn = getattr(lib, name)  # This probe requires the reviewed wrapper.
            patches.append((lib, name, fn))
            setattr(lib, name, NativeCall(fn, name))
        original_setup(lib)
        require(all(isinstance(getattr(lib, name), NativeCall) for name in FSDB_ABI),
                'unobserved_fsdb_export')

    def load():
        if report.get('wave_mutation_in_progress'):
            reject('fsdb_wrapper_load_during_wave_mutation')
        return original_load()

    def get_parser(path):
        if report.get('wave_mutation_in_progress'):
            allowed = report['wave_dirty_unchanged']
            cached = server._parser_cache.get(path)
            if (path != allowed['path'] or cached is None
                    or list(cached[0]) != allowed['signature']
                    or list(server._get_wave_signature(path)) != allowed['signature']
                    or id(cached[1]) != allowed['object_id']
                    or id(cached[1]._scope_epoch) != allowed['epoch_id']):
                reject('parser_replacement_during_wave_mutation')
            result = original_parser(path)
            require(result is cached[1], 'dirty_cache_hit_replaced_parser')
            report.setdefault('dirty_unchanged_cache_hits', []).append(
                dict(path=path, object_id=id(result), epoch_id=id(result._scope_epoch)))
            return result
        return original_parser(path)

    patches.extend(((fsdb_parser, '_setup', original_setup),
                    (fsdb_parser, '_load_wrapper', original_load),
                    (server, '_get_parser', original_parser)))
    fsdb_parser._setup, fsdb_parser._load_wrapper = setup, load
    server._get_parser = get_parser


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_fact(path):
    before = path.stat()
    data = path.read_bytes()
    return dict(path=str(path), sha256=digest(data), dev=before.st_dev,
                ino=before.st_ino, size=before.st_size, mode=stat.S_IMODE(before.st_mode),
                atime_ns=before.st_atime_ns, mtime_ns=before.st_mtime_ns,
                ctime_ns=before.st_ctime_ns)


async def wave_mutation_chain(args, report, cases, inventory, server, counts,
                                measured, public, require, baseline_files):
    from src import divergence_trace
    from src.design_context import DesignContext
    from src.divergence_design_context import DivergenceDesigns
    from src.evidence_output import expand_compact_result
    from src.kdb_anchor import KdbAnchor
    from src.divergence_compare import file_identity

    side = args.mutation_side
    root = Path(args.mutation_root).resolve()
    authorized = Path('/home/robin/Projects/mcp/TraceWeave/docs/2026-10-16-kdb-npi-roadmap/p4_s5_evidence/isolated/fixture')
    require(root == authorized, 'foreign_isolated_wave_fixture')
    exclusion_path = root.parent.parent / 'READER_SCAN_EXCLUSIONS.json'
    exclusion_bytes = exclusion_path.read_bytes()
    exclusions = json.loads(exclusion_bytes)['exclusions']
    report['reader_exclusions_sha256'] = digest(exclusion_bytes)
    repo = Path(args.repo).resolve()
    require(not {'p4_s2_evidence', 'p4_s3_evidence', 'p4_s4_evidence'}.intersection(root.parts)
            and root.name == 'fixture' and root.parent.name == 'isolated'
            and root.parent.parent.name == 'p4_s5_evidence'
            and not root.is_relative_to(repo / 'tests'), 'sealed_mutation_root')
    require(args.fixture_lock_fd is not None, 'outer_fixture_lock_required')
    lock_stat = os.fstat(args.fixture_lock_fd)
    expected_lock = (root.parent / 'fixture.execution.lock').stat()
    require((lock_stat.st_dev, lock_stat.st_ino) == (expected_lock.st_dev, expected_lock.st_ino),
            'foreign_fixture_lock')
    fcntl.flock(args.fixture_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    target = Path(cases[side]['wave_path'])
    require(target == root / side / 'trace.fsdb', 'foreign_wave_target')
    other_target = Path(cases['b' if side == 'a' else 'a']['wave_path'])
    require((target.stat().st_dev, target.stat().st_ino) !=
            (other_target.stat().st_dev, other_target.stat().st_ino), 'aliased_wave_inodes')
    require(not target.is_symlink() and stat.S_ISREG(target.lstat().st_mode)
            and target.stat().st_nlink == 1
            and target.resolve().is_relative_to(root / side), 'nonregular_or_foreign_wave')
    before = file_fact(target)
    original_bytes = target.read_bytes()
    require(len(original_bytes) >= 16 and original_bytes[:16].hex() == '000000001c3ed9480403020100000000',
            'unreviewed_wave_header')
    changed_bytes = bytes([original_bytes[0] ^ 1]) + original_bytes[1:]
    offsets = [i for i, (a, b) in enumerate(zip(original_bytes, changed_bytes)) if a != b]
    require(len(original_bytes) == len(changed_bytes) and len(offsets) == 1, 'not_one_byte_mutation')
    offset = offsets[0]
    for name, data in (('original.fsdb', original_bytes), ('mutated.fsdb', changed_bytes)):
        with (out / name).open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    journal = dict(target=str(target), kind='fsdb_waveform',
                   fixture_root=str(root), side=side, wave_name=target.name,
                   canonical_target=str(target.resolve()),
                   original_backup=str(out / 'original.fsdb'),
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
    report['wave_mutation_in_progress'] = False

    def event(kind, **facts):
        events.append(dict(index=len(events), monotonic_ns=time.monotonic_ns(),
                           phase=phase, kind=kind, **facts))

    native_keys = ('fsdb_abi_total', 'sdk_init', 'sdk_load_design', 'sdk_end', 'lang_release_all_handles',
                   'get_dynamic_step', 'find_driver', 'query_source')

    def native_counts():
        return {key: counts[key] for key in native_keys}

    def sync_directory():
        for directory in (out, out.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
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
        result = expand_compact_result(raw) if raw.get('format') else raw
        status = result.get('backend_status', {})
        require(status.get('actual_backend') == 'verdi_npi'
                and result.get('driver_status') == 'resolved'
                and result.get('signal_path') == cases[label]['signal_path']
                and result.get('resolved_module') == cases[label]['top_hint']
                and Path(result.get('source_file', '')).resolve() == paths[label]
                and result.get('source_line') == 8
                and status['design_context']['design_handle'] == handles[label], 'old_action_invalid')
        require(counts['find_driver'] - prior['find_driver'] == 1
                and counts['sdk_load_design'] - prior['sdk_load_design'] == 1
                and counts['fsdb_abi_total'] == prior['fsdb_abi_total'], 'old_action_not_native_or_read_wave')

    restore_had_error = False

    def restore_file():
        require(threading.current_thread() is not threading.main_thread()
                and server._FSDB_WAVE_LOCK.locked(), 'wave_restore_outside_worker_lock')
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
        report['wave_restoration'] = dict(sha256=digest(original_bytes), metadata_restored=True,
            ctime_ns=restored.st_ctime_ns, ctime_restored=False, original_error=restore_had_error)
        event('restore_completed', **report['wave_restoration'])
        save('restoration.json', report['wave_restoration'])
        fsync_path_directory(target.parent)
        sync_directory()
        report['wave_mutation_in_progress'] = False

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
                rows[label] = asdict(anchor)
        return rows

    def parser_facts():
        rows = {}
        for label, case in cases.items():
            cached = server._parser_cache.get(case['wave_path'])
            require(cached is not None, 'missing_real_parser')
            signature, parser = cached
            _KEEPALIVE.append(parser)
            rows[label] = dict(object_id=id(parser), epoch_id=id(parser._scope_epoch),
                open=bool(parser._handle), group_active=parser._transition_group_active,
                group_token_present=parser._transition_group_token is not None,
                cache_signature=signature, parser_file_identity=parser._file_identity,
                current_file_identity=file_identity(case['wave_path']),
                metadata_count=len(parser._metadata_cache), count_cache_count=len(parser._transition_count_cache),
                index_count=len(parser._transition_indexes), header_present=parser._header is not None,
                summary_present=parser._summary_paths is not None)
        return rows

    def fsync_path_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def preclose_and_mutate(designs):
        nonlocal injected, mutation_attempted, injection_counts
        require(threading.current_thread() is not threading.main_thread()
                and server._FSDB_WAVE_LOCK.locked(), 'wave_write_outside_worker_lock')
        initial = parser_facts()
        for label, case in cases.items():
            parser = server._parser_cache[case['wave_path']][1]
            require(designs.parser_generations[case['wave_path']] == (id(parser), parser._scope_epoch)
                    and initial[label]['open'] and not initial[label]['group_active']
                    and not initial[label]['group_token_present']
                    and designs.wave_ids[label] == file_identity(case['wave_path']),
                    'unsettled_parser_before_mutation')
        require(target.read_bytes() == original_bytes, 'target_changed_before_close')
        save('parsers_before_preclose.json', initial)
        parser = server._parser_cache[str(target)][1]
        old_epoch = parser._scope_epoch
        _KEEPALIVE.append(old_epoch)  # Prevent address reuse in epoch-ID evidence.
        before_close = counts['fsdb_abi_fsdb_close']
        event('healthy_parser_close_enter', side=side)
        parser.close()
        event('healthy_parser_close_return', side=side)
        closed = parser_facts()
        other = 'b' if side == 'a' else 'a'
        require(counts['fsdb_abi_fsdb_close'] == before_close + 1
                and not parser._handle and parser._scope_epoch is not old_epoch
                and not parser._transition_group_active and parser._transition_group_token is None
                and not parser._metadata_cache and not parser._transition_count_cache
                and not parser._transition_indexes and parser._header is None
                and parser._summary_paths is None and parser._file_identity is None
                and not any(k.startswith('_cached_clock_') for k in vars(parser))
                and closed[other] == initial[other], 'preclose_not_isolated')
        isolation = file_references(target, exclusions)
        save('preclose_file_references.json', isolation)
        require(not isolation['fds'] and not isolation['mappings'] and not isolation['errors'],
                'live_or_unknown_wave_reference_before_write')
        save('parsers_after_preclose.json', closed)
        report['wave_dirty_target'] = str(target)
        report['wave_dirty_unchanged'] = dict(path=cases[other]['wave_path'],
            object_id=closed[other]['object_id'], epoch_id=closed[other]['epoch_id'],
            signature=list(server._get_wave_signature(cases[other]['wave_path'])))
        save('write_intent.json', dict(target=str(target), side=side,
            original_sha256=before['sha256'], mutated_sha256=digest(changed_bytes),
            native_counts=native_counts(), monotonic_ns=time.monotonic_ns()))
        sync_directory()
        fsync_path_directory(target.parent)
        mutation_attempted = True
        report['wave_mutation_in_progress'] = True
        report['corrupt_interval_counts_before'] = native_counts()
        event('wave_write_begin', side=side)
        with target.open('r+b') as stream:
            stream.seek(offset)
            stream.write(changed_bytes[offset:offset + 1])
            stream.flush()
            os.fsync(stream.fileno())
        fsync_path_directory(target.parent)
        after = file_fact(target)
        require(target.read_bytes() == changed_bytes and after['sha256'] != before['sha256']
                and all(after[key] == before[key] for key in ('dev', 'ino', 'size', 'mode'))
                and after['mtime_ns'] != before['mtime_ns']
                and after['ctime_ns'] != before['ctime_ns']
                and file_identity(str(target)) != designs.wave_ids[side], 'actual_byte_mutation_not_proven')
        report['mutation_after'] = after
        save('mutation_after.json', after)
        save('mutated_input_hashes.json', hashes(True))
        save('parsers_during_mutation.json', parser_facts())
        event('wave_write_verified', side=side, sha256=after['sha256'])
        injected = True
        injection_counts = counts.copy()

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
            await server._run_in_wave_thread([c['wave_path'] for c in cases.values()],
                                             lambda: preclose_and_mutate(designs))
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
        baseline_cursor = 'p4_s5_' + side + '_baseline'
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
                    and rejected['coverage']['gaps'] == ['waveform_changed']
                    and rejected['coverage']['checks'] == []
                    and not rejected['coverage']['truncated']
                    and all(rejected['contexts'][label]['status'] == 'waveform_changed'
                            for label in cases), 'wrong_rejection')
            require(rejected['comparison'] == {} and rejected['nodes'] == rejected['edges'] == rejected['findings'] == []
                    and rejected.get('cursor') is None, 'stale_trace_published')
            require(rejected['frontier'] == [dict(node_id=None, reason='waveform_changed')], 'stale_frontier')
            require(all(counts[key] == injection_counts[key] for key in native_keys),
                    'native_continued_after_mutation')
            validated = [(e['side'], e['current']) for e in events
                         if e['phase'] == 'trace_negative' and e['kind'] == 'final_source_current_return']
            require(validated == [('a', True), ('b', True)],
                    'source_changed_in_wave_test')
            checked = [(e['side'], e['current']) for e in events
                       if e['phase'] == 'trace_negative' and e['kind'] == 'final_anchor_current_return']
            require(checked == [('a', True), ('b', True)],
                    'wrong_final_anchor_side_order')
            final_events = [e for e in events if e['phase'] == 'trace_negative']
            require(sum(e['kind'] == 'final_delegate_enter' for e in final_events) == 1
                    and not [e for e in final_events if e['kind'] == 'final_delegate_exception']
                    and [e['result'] for e in final_events if e['kind'] == 'final_delegate_return']
                        == [[(side, 'waveform_changed')]], 'not_original_wave_final_return')
            recoveries = rejected['next_actions']
            require(len(recoveries) == 2 and {row['side'] for row in recoveries} == {'a', 'b'}, 'missing_recoveries')
            for row in recoveries:
                label = row['side']
                require(row['status'] == 'needs_prerequisite'
                        and row['evidence']['context_status'] == 'waveform_changed'
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
            cursor_after = [row.as_dict() for row in server._cursor_store.list()]
            save('cursor_after_rejection_actions.json', cursor_after)
            require(not publications and cursor_after == cursor_before, 'old_cursor_modified')
        finally:
            server._cursor_store.set, server._cursor_store.auto_set = original_set, original_auto
            original_error = sys.exc_info()[0]
            restore_had_error = original_error is not None
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
                    await server._run_in_wave_thread(str(target), restore_file)
            except BaseException as exc:
                report['wave_restoration_error'] = dict(type=type(exc).__name__, message=str(exc))
                if original_error is None:
                    raise
            finally:
                report['restoration_cost'] = dict(wall_ms=1000*(time.perf_counter()-started[0]),
                                                  cpu_ms=1000*(time.process_time()-started[1]))
        require(not report['wave_mutation_in_progress'], 'wave_restore_unconfirmed')
        require(report['corrupt_interval_counts_before'] == report['corrupt_interval_counts_after']
                and not report.get('native_fence_failed'), 'native_work_during_corrupt_interval')
        require(len(report.get('dirty_unchanged_cache_hits', [])) == 1,
                'final_did_not_use_unchanged_parser_only')
        # VCS driver actions bind source/KDB, not wave identity. Both original
        # handles remain valid, but neither action is executed on damaged bytes.
        event('old_actions_after_restoration', order=['a', 'b'])
        server._cursor_store.set, server._cursor_store.auto_set = observed_set, observed_auto
        try:
            await replay_action('a', actions)
            await replay_action('b', actions)
            require(not publications, 'old_action_published_cursor')
        finally:
            server._cursor_store.set, server._cursor_store.auto_set = original_set, original_auto
        cursor_after_both = [row.as_dict() for row in server._cursor_store.list()]
        save('cursor_after_both_actions.json', cursor_after_both)
        require(cursor_after_both == cursor_before, 'old_cursor_modified_after_restore')
        # The one-shot write is now disabled; transparent observation delegates remain.
        await prepare('restored')
        require(identities == old_identities, 'source_identity_changed_by_wave_only_mutation')
        report['reprepared'] = dict(old_handles=old_handles, new_handles=handles.copy(),
                                  old_identities=old_identities, new_identities=identities.copy())
        recovered = await invoke('trace_restored', 'trace_divergence',
                                 request('p4_s5_' + side + '_restored', 'new preparation after byte restoration'))
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
        require(report['restored_anchors'] == report['baseline_anchors'], 'wave_mutation_changed_kdb_anchor')
        recovered_parsers = parser_facts()
        initial_parsers = json.loads((out / 'parsers_before_preclose.json').read_text())
        other = 'b' if side == 'a' else 'a'
        require(recovered_parsers[side]['object_id'] != initial_parsers[side]['object_id']
                and recovered_parsers[side]['epoch_id'] != initial_parsers[side]['epoch_id']
                and recovered_parsers[side]['open']
                and recovered_parsers[side]['parser_file_identity'] == file_identity(str(target))
                and recovered_parsers[other]['object_id'] == initial_parsers[other]['object_id']
                and recovered_parsers[other]['epoch_id'] == initial_parsers[other]['epoch_id'],
                'restoration_did_not_replace_changed_parser')
        require(all(not reader._handle for reader in _KEEPALIVE
                    if id(reader) == initial_parsers[side]['object_id']),
                'old_changed_parser_reopened')
        for label in cases:
            calls = [r for r in report['fsdb_abi_calls'] if r['phase'] == 'trace_restored'
                     and r.get('path') == cases[label]['wave_path']]
            require(any(r['api'] in {'fsdb_event_page_v1', 'fsdb_get_value_at_time',
                'fsdb_get_loaded_transitions', 'fsdb_get_transitions_profiled'} for r in calls),
                'restored_side_without_real_read')
            require(sum(r['api'] == 'fsdb_open' for r in calls) == int(label == side),
                'wrong_recovery_reopen_count')
        save('parsers_recovered.json', recovered_parsers)
        save('restored_anchors.json', report['restored_anchors'])
        save('final_pins.json', pins())
        require(not any(lock.locked() for lock in server._wave_locks_for(
            [case['wave_path'] for case in cases.values()])), 'final_wave_lock_leaked')
        report['wave_locks_released'] = True
        report['coverage'] = dict(real_wave_byte_mutation=True, mutation_side=side,
            same_size=True, same_mtime=False, raw_hash_only_detection='not_claimed',
            dual_observations_before_rejection=True, old_actions_both_sides_checked=True,
            old_actions_binding='source_and_kdb_not_wave_identity_for_these_vcs_paths',
            old_actions_timing='both_after_restoration_before_new_prepare',
            dirty_actions='not_run_for_native_safety', changed_parser_preclosed_on_healthy_bytes=True,
            live_reader_over_damaged_file='not_tested', restored_actual_reopen_and_reads=True,
            restored_bytes_new_prepare=True, full_hierarchy='guarded_zero',
            cancellation_timeout_native_integration='not_run',
            identity_scope='wave_stat_identity_not_content_hash',
            synchronous_deep_trace='not_run', license_checkout_release='unverified', lsf='not_run')

    finally:
        divergence_trace._walk, DivergenceDesigns.validate_final, DesignContext.current = (
            original_walk, original_final, original_current)
        KdbAnchor.current = original_anchor_current
        save('events.json', events)
        report['observation_wrappers_restored'] = True
