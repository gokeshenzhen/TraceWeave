#!/usr/bin/env python3
"""Opt-in boundary and identity-chain probes over reviewed real dual designs.

Run only after A reviews MCP preflight. Raw dynamic gaps are observations,
not converted to success or forced fallback. This is in-process public-call
acceptance plus diagnostic SDK evidence, not stdio transport acceptance.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True
import argparse
import asyncio
from collections import Counter
import hashlib
import inspect
import json
import os
from pathlib import Path
import resource
import threading
import time
import traceback
from copy import deepcopy

MAIN = Path(__file__).resolve().parents[1]
ENV = dict(PYTHONDONTWRITEBYTECODE='1', TRACEWEAVE_NPI_EXECUTION='local',
           TRACEWEAVE_CONNECTIVITY_ROUTE='auto', TRACEWEAVE_AUTO_KDB='0',
           TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY='off', TRACEWEAVE_TELEMETRY='0',
           TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE='0', TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION='0')


class GuardFailure(BaseException):
    pass


def require(condition, code):
    if not condition:
        raise GuardFailure(code)


def head_at(repo):
    gitdir = repo / '.git'
    if gitdir.is_file():
        gitdir = (repo / gitdir.read_text().strip().removeprefix('gitdir: ')).resolve()
    common = gitdir
    if (gitdir / 'commondir').is_file():
        common = (gitdir / (gitdir / 'commondir').read_text().strip()).resolve()
    head = (gitdir / 'HEAD').read_text().strip()
    if not head.startswith('ref: '):
        return head
    ref = head.removeprefix('ref: ')
    for base in (gitdir, common):
        if (base / ref).is_file():
            return (base / ref).read_text().strip()
    for line in (common / 'packed-refs').read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            return fields[0]
    raise GuardFailure('head_unavailable')


def sample():
    return time.perf_counter(), time.process_time()


async def run(args, report):
    repo = Path(args.repo).resolve()
    require(head_at(repo) == args.expected_head, 'unexpected_head')
    inventory_bytes = Path(args.inventory).read_bytes()
    inventory = json.loads(inventory_bytes)['cases']
    cases = {label.lower(): dict(compile_log=row['compile_log']['path'],
        wave_path=row['wave']['path'], top_hint=row['top'], signal_path=row['signal'],
        kdb_path=row['kdb']['canonical']) for label, row in inventory.items()}
    require(set(cases) == {'a', 'b'}, 'two_cases_required')
    require(Path(cases['a']['kdb_path']).resolve() != Path(cases['b']['kdb_path']).resolve(),
            'aliased_kdb_inputs')
    report['inventory_sha256'] = hashlib.sha256(inventory_bytes).hexdigest()
    require(all(os.environ.get(k) == v for k, v in ENV.items()), 'explicit_environment_required')
    evidence = Path(args.mcp_evidence).read_bytes()
    json.loads(evidence)
    report['mcp_evidence'] = dict(path=str(Path(args.mcp_evidence).resolve()),
                                sha256=hashlib.sha256(evidence).hexdigest(),
                                interpretation='A-reviewed external MCP prerequisite; not local SDK proof')
    counts, denied, selected, systems, loads = Counter(), Counter(), {}, {}, []
    baseline_files = {}
    for label, row in inventory.items():
        for kind in ('compile_log', 'source', 'wave'):
            p = Path(row[kind]['path'])
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            require(digest == row[kind]['sha256'], 'inventory_file_changed_' + label + '_' + kind)
            baseline_files[str(p.resolve())] = digest
        tree = Path(row['kdb']['canonical'])
        actual = [(str(p.relative_to(tree)), hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in sorted(tree.rglob('*')) if p.is_file()]
        require(hashlib.sha256(json.dumps(actual, separators=(',', ':')).encode()).hexdigest()
                == row['kdb']['tree_content_sha256'], 'inventory_kdb_changed_' + label)
        for relative, digest in actual:
            baseline_files[str((tree / relative).resolve())] = digest
    report.update(counts=counts, denied=denied, loads=loads, phases={}, results={}, inputs=cases)
    phase = 'import'
    main_thread = threading.get_ident()

    def reject(code):
        denied[code] += 1
        raise GuardFailure(code)

    def profile(frame, event, arg):
        if (event != 'call' or not frame.f_code.co_flags & inspect.CO_NEWLOCALS
                or not frame.f_code.co_filename.startswith(str(repo) + os.sep)):
            return
        module, name = Path(frame.f_code.co_filename).name, frame.f_code.co_name
        qualified = frame.f_code.co_qualname
        if ((module == 'tb_hierarchy_builder.py' and name in {
                'build_hierarchy', 'build_component_tree', '_walk_component_tree', '_walk_children'})
                or module == 'bounded_hierarchy_bootstrap.py' and name == 'build_bounded_connectivity_context'
                or module == 'analyzer.py' and name == '_load_hierarchy'):
            reject('full_hierarchy')
        if module == 'kdb_builder.py' and name in {'build_kdb', '_run_phase', '_write_build_script'}:
            reject('licensed_build')
        if (module in {'source_graph_worker.py', 'structural_semantic_worker.py',
                       'slang_connectivity_projector.py'} and not name.startswith('<')):
            reject('semantic_frontend')
        if module == 'connectivity_backend.py' and qualified.startswith('StaticConnectivityBackend.'):
            reject('static_fallback')
        if module == 'fsdb_parser.py' and qualified.startswith('FSDBParser.'):
            if args.mode == 'boundary':
                reject('unexpected_waveform_read')
            if name in {'__init__', '_open', '_ensure_open', 'get_transitions', 'get_value_at_time',
                        'get_signal_at_time', 'get_signal_transitions', 'get_transitions_capped',
                        'get_signal_declaration', 'get_signal_width', '_event_pages'}:
                counts['wave_read_profile_events'] += 1
                if not runtime_server:
                    reject('wave_before_server_import')
                locks = runtime_server._wave_locks_for([case['wave_path'] for case in cases.values()])
                if threading.get_ident() == main_thread or not all(lock.locked() for lock in locks):
                    reject('wave_outside_worker_lock')
        if module == 'server.py' and name in {'_validate_design_source', '_validate_design_binding',
                                             '_select_prepared_backend', '_query_prepared_hierarchy'}:
            counts['source_lock_check_profile_events'] += 1
            if runtime_server and any(lock.locked() for lock in runtime_server._wave_locks_for(
                    [case['wave_path'] for case in cases.values()])):
                reject('source_under_wave_lock')
        if module == 'verdi_npi_backend.py' and name in {'find_driver', 'get_dynamic_step', 'query_source'}:
            counts[name] += 1
        if module in {'compile_log_parser.py', 'design_backend_binding.py', 'verdi_backend.py'} and name in {
                'parse_compile_log', 'prepare_backend_binding', 'probe_verdi_backend'}:
            counts[name] += 1

    def audit(event, values):
        if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork'}:
            reject('external_process')
        if event == 'import' and str(values[0]).split('.')[0] == 'pyslang':
            reject('semantic_frontend')

    runtime_server = None
    sys.path.insert(0, str(repo))
    sys.addaudithook(audit)
    sys.setprofile(profile)
    threading.setprofile(profile)
    import_started = sample()
    try:
        import server
        runtime_server = server
        report['phases']['import'] = dict(wall_ms=1000*(time.perf_counter()-import_started[0]),
            cpu_ms=1000*(time.process_time()-import_started[1]),
            ru_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        from src import verdi_npi_backend as npi
        require(Path(server.__file__).resolve() == repo / 'server.py', 'foreign_server_import')
        original_import, original_select = npi._import_pynpi, server._select_prepared_backend

        def imported():
            modules = original_import()
            if modules is not None and id(modules[0]) not in systems:
                sdk = modules[0]
                systems[id(sdk)] = sdk
                for name in ('init', 'load_design', 'end'):
                    original = getattr(sdk, name)
                    def counted(*a, _name=name, _fn=original, **kw):
                        counts['sdk_' + _name] += 1
                        require(threading.get_ident() == main_thread, 'native_not_main_thread')
                        require(not any(lock.locked() for lock in server._wave_locks_for(
                            [case['wave_path'] for case in cases.values()])), 'native_under_wave_lock')
                        if _name == 'load_design':
                            loads.append(dict(phase=phase, arguments=a, kwargs=kw))
                            label = phase.split('_')[1]
                            argv = a[0]
                            require(argv[argv.index('-top') + 1] == cases[label]['top_hint']
                                and Path(argv[argv.index('-dbdir') + 1]).resolve()
                                    == Path(cases[label]['kdb_path']).resolve().parent,
                                'native_load_crossed_design')
                        return _fn(*a, **kw)
                    setattr(sdk, name, counted)
                from pynpi import lang
                original_release = lang.release_all_handles
                def released(*a, _fn=original_release, **kw):
                    counts['lang_release_all_handles'] += 1
                    return _fn(*a, **kw)
                lang.release_all_handles = released
            return modules

        def select(entry, deferred):
            result = original_select(entry, deferred)
            if getattr(result[0], 'name', None) == 'verdi_npi':
                selected[entry.context.identity_sha256] = result[0]
            return result

        npi._import_pynpi, server._select_prepared_backend = imported, select
        handles, identities = {}, {}

        async def measured(name, operation):
            nonlocal phase
            phase, before, started = name, counts.copy(), sample()
            try:
                result = await operation()
                report['results'][name] = result
                return result
            finally:
                wall, cpu = started
                report['phases'][name] = dict(wall_ms=1000*(time.perf_counter()-wall),
                    cpu_ms=1000*(time.process_time()-cpu),
                    ru_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    counts=dict(counts-before))

        async def public(name, arguments):
            blocks = await server.call_tool(name, arguments)
            require(len(blocks) == 1 and blocks[0].type == 'text', 'unexpected_public_envelope')
            return json.loads(blocks[0].text)

        for label, case in cases.items():
            prepared = await measured('prepare_' + label, lambda case=case: public(
                'prepare_design_context', dict(compile_log=case['compile_log'],
                    simulator='vcs', top_hint=case['top_hint'], timeout_sec=30)))
            require('error' not in prepared, 'prepare_error_' + label)
            require(prepared['semantic_status'] == 'not_run' and prepared['kdb_status'] == 'not_checked',
                    'prepare_claimed_native_' + label)
            require(counts['sdk_init'] == counts['sdk_load_design'] == 0, 'prepare_started_native')
            handles[label], identities[label] = prepared['design_handle'], prepared['context_identity_sha256']
        require(handles['a'] != handles['b'] and identities['a'] != identities['b'], 'aliased_contexts')
        if args.mode == 'chain':
            await chain(args, report, cases, inventory, server, counts, handles, identities,
                        measured, public)
            require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
                        for p, digest in baseline_files.items()), 'final_actual_file_changed')
            report['final_actual_file_count'] = len(baseline_files)
            report['status'] = 'chain_subset_verified'
            return
        for label, case in cases.items():
            result = await measured('driver_' + label, lambda label=label, case=case: public(
                'explain_signal_driver', dict(design_handle=handles[label], wave_path=case['wave_path'],
                    signal_path=case['signal_path'], recursive=False, output_format='full')))
            require(result.get('backend_status', {}).get('actual_backend') == 'verdi_npi',
                    'driver_not_native_' + label)
            require(identities[label] in selected, 'native_selection_not_observed_' + label)
        for label, case in cases.items():
            backend = selected[identities[label]]
            with server._design_context_store.pin(handles[label]) as entry:
                anchor = entry.binding.anchor
                expected = Path(case['kdb_path'])
                require(anchor is not None and Path(anchor.canonical_path) == expected.resolve(),
                        'foreign_kdb_' + label)
                entry.binding.require_current()
                async def dynamic():
                    return await server._call_connectivity_backend(backend, lambda: backend.get_dynamic_step(
                        signal_path=case['signal_path'], compile_log=case['compile_log'],
                        top_hint=case['top_hint'], simulator='vcs'))
                step = await measured('dynamic_' + label, dynamic)
                require(step.get('backend') == 'verdi_npi', 'foreign_dynamic_source_' + label)
                entry.binding.require_current()
                report.setdefault('identities', {})[label] = dict(context=identities[label],
                    design_handle=handles[label], anchor=anchor.receipt(),
                    loaded_top=getattr(backend, '_loaded_top', None),
                    loaded_kdb=getattr(backend, '_loaded_kdb', None))
                terminal = {'testbench_driven', 'multiple_driver_candidates', 'dynamic_expression_limit'}
                report.setdefault('dynamic_trace_gate', {})[label] = dict(
                    complete=step.get('complete'), gaps=step.get('gaps'),
                    retained_native_boundary=bool(step.get('complete') or set(step.get('gaps', ())) & terminal),
                    interpretation='gate only; not public trace_divergence acceptance')
        report['status'] = 'boundary_observed'
    finally:
        report['product_completion_counts'] = dict(counts)
        phase = 'diagnostic_cleanup'
        if runtime_server is not None and args.mode == 'chain':
            for case in cases.values():
                path = case['wave_path']
                cached = runtime_server._parser_cache.get(path)
                if cached is None:
                    continue
                parser = cached[1]
                def close_parser(parser=parser):
                    before = bool(parser._handle)
                    parser.close()
                    return dict(was_open=before, still_open=bool(parser._handle))
                try:
                    report.setdefault('diagnostic_parser_close', {})[path] = (
                        await runtime_server._run_in_wave_thread(path, close_parser))
                except Exception as exc:
                    report.setdefault('cleanup_errors', []).append(type(exc).__name__)
        for sdk in systems.values():
            try:
                report.setdefault('diagnostic_end_returns', []).append(sdk.end())
            except Exception as exc:
                report.setdefault('cleanup_errors', []).append(type(exc).__name__)
        report['counts'] = dict(counts)
        report['counts_note'] = 'SDK init/load/end are not license checkout/release counts; end is diagnostic only.'
        report['denied'] = dict(denied)
        report['final_head'] = head_at(repo)
        sys.setprofile(None)
        threading.setprofile(None)
        require(not report.get('cleanup_errors'), 'diagnostic_cleanup_failed')


async def chain(args, report, cases, inventory, server, counts, handles, identities, measured, public):
    """Public source/driver/diff chain; no direct diagnostic dynamic query."""
    require(bool(args.boundary_evidence), 'chain_requires_reviewed_native_boundary')
    raw = Path(args.boundary_evidence).read_bytes()
    boundary = json.loads(raw)
    require(boundary.get('status') == 'boundary_observed', 'unaccepted_boundary')
    require(boundary.get('inventory_sha256') == report['inventory_sha256'], 'foreign_boundary_inventory')
    require(boundary.get('mcp_evidence', {}).get('sha256') == report['mcp_evidence']['sha256'],
            'foreign_boundary_wave_preflight')
    require(set(boundary.get('dynamic_trace_gate', {})) == {'a', 'b'}, 'incomplete_boundary_gate')
    report['native_boundary_evidence'] = dict(path=str(Path(args.boundary_evidence).resolve()),
        sha256=hashlib.sha256(raw).hexdigest(), expected_head=boundary.get('expected_head'),
        gates=boundary['dynamic_trace_gate'])
    anchor_ids = {}

    def native_receipt(result, label):
        status = result.get('backend_status', {})
        require(status.get('actual_backend') == 'verdi_npi', 'not_native_' + label)
        receipt = status.get('design_context', {})
        require(receipt.get('design_handle') == handles[label]
                and receipt.get('context_identity_sha256') == identities[label], 'foreign_context_' + label)
        artifact = receipt.get('artifact', {})
        require(artifact.get('load_status') == 'loaded'
                and artifact.get('source_association') == 'unverified', 'dishonest_artifact_' + label)
        with server._design_context_store.pin(handles[label]) as entry:
            require(entry.binding is not None and entry.binding.anchor is not None, 'binding_missing_' + label)
            anchor = entry.binding.anchor
            require(Path(anchor.canonical_path) == Path(cases[label]['kdb_path']).resolve(), 'foreign_anchor_' + label)
            require(artifact.get('identity_sha256') == anchor.identity_sha256, 'foreign_artifact_' + label)
            anchor_ids[label] = anchor.identity_sha256

    async def source(label, kind, iteration):
        case = cases[label]
        tool = 'find_tb_instance' if kind == 'instance' else 'get_tb_file_detail'
        path = case['top_hint'] if kind == 'instance' else inventory[label.upper()]['source']['path']
        result = await measured(kind + '_' + label + '_' + iteration,
            lambda: public(tool, dict(design_handle=handles[label], path=path)))
        require(result.get('facts_origin') == 'verdi_npi', 'source_origin_' + label)
        require(result.get('design_handle') == handles[label]
                and result.get('context_identity_sha256') == identities[label], 'source_identity_' + label)
        native_receipt(result, label)
        if kind == 'instance':
            server.schemas.SourceInstanceResult.model_validate(result)
            require(len(result['hits']) == 1 and result['hits'][0]['path'] == path
                    and result['hits'][0]['module'] == case['top_hint'], 'exact_top_mismatch_' + label)
        else:
            server.schemas.SourceFileDetailResult.model_validate(result)
            require(Path(result['path']).resolve() == Path(path).resolve()
                    and result['source_status'] == 'captured', 'source_member_mismatch_' + label)
            require(all(row.get('source_file') is None
                        or row['source_file'] in {result['path'], *result['aliases']}
                        for row in result.get('symbols') or []), 'foreign_source_symbol_' + label)
        return result

    def driver_facts(result):
        # Exact exclusions: public receipts/timing and query presentation costs
        # are retained raw; driver semantic/coverage/traversal facts are compared.
        value = deepcopy(result)
        value.pop('backend_status', None)
        value.pop('operation_metrics', None)
        return value

    first_driver = {}
    async def driver(label, iteration, arguments=None):
        case = cases[label]
        arguments = dict(arguments) if arguments is not None else dict(
            design_handle=handles[label], wave_path=case['wave_path'],
            signal_path=case['signal_path'], recursive=False)
        arguments['output_format'] = 'full'
        result = await measured('driver_' + label + '_' + iteration,
            lambda: public('explain_signal_driver', arguments))
        native_receipt(result, label)
        require(result.get('signal_path') == case['signal_path'], 'driver_target_mismatch_' + label)
        if label not in first_driver:
            first_driver[label] = driver_facts(result)
        else:
            require(driver_facts(result) == first_driver[label], 'repeated_driver_facts_changed_' + label)
        return result

    # Both Language owners and Netlist driver calls see interleaved designs.
    await source('a', 'instance', 'first')
    await source('b', 'instance', 'first')
    await source('a', 'file', 'first')
    await source('b', 'file', 'first')
    for iteration in ('first', 'repeat'):
        await driver('a', iteration)
        await driver('b', iteration)
    require(anchor_ids['a'] != anchor_ids['b'], 'aliased_bound_artifacts')

    diff_args = dict(wave_path_a=cases['a']['wave_path'], signal_a=cases['a']['signal_path'],
        wave_path_b=cases['b']['wave_path'], signal_b=cases['b']['signal_path'],
        context_a=dict(design_handle=handles['a']), context_b=dict(design_handle=handles['b']),
        start_time_ps=0, end_time_ps=30000, cursor_name='p4_s1_reset_diff')
    before_native = counts['sdk_load_design']
    diff = await measured('diff_first', lambda: public('diff_first_divergence', diff_args))
    server.schemas.DiffFirstDivergenceResult.model_validate(diff)
    require(diff.get('diverged') is True and diff.get('first_divergence_time_ps') == 26000
            and diff.get('value_a') == '0' and diff.get('value_b') == '1', 'rtl_mcp_diff_oracle_mismatch')
    require(counts['sdk_load_design'] == before_native, 'diff_loaded_native_for_actions')
    require(diff.get('cursor', {}).get('time_ps') == 26000, 'diff_cursor_mismatch')
    actions = diff.get('next_actions', [])
    require(len(actions) == 2 and {row['side'] for row in actions} == {'a', 'b'}, 'missing_two_actions')
    for action in actions:
        label, call = action['side'], action['arguments']
        require(action.get('status') == 'ready' and action.get('prerequisite_calls') == [], 'action_not_ready')
        require(call == dict(design_handle=handles[label], wave_path=cases[label]['wave_path'],
                            signal_path=cases[label]['signal_path'], recursive=False), 'cross_side_action')
        require(action.get('evidence', {}).get('context_identity_sha256') == identities[label],
                'foreign_action_identity')
        await driver(label, 'action_replay', call)
        await source('b' if label == 'a' else 'a', 'instance', 'after_action_' + label)

    gates = boundary['dynamic_trace_gate']
    require(all(row.get('retained_native_boundary') is False
                and row.get('gaps') == ['dynamic_evidence_unavailable']
                for row in gates.values()), 'different_boundary_requires_new_review')
    report['trace_divergence'] = dict(status='trace_unverified', executed=False,
        reason='reviewed_real_reset_dynamic_evidence_unavailable',
        observed_gaps={side: row['gaps'] for side, row in gates.items()})

    # An actual already-open FSDB parser generation change between observation
    # and final recheck must reject the public diff, before any cursor publish.
    original_wave_request = server._run_in_wave_request
    original_set = server._cursor_store.set
    original_auto_set = server._cursor_store.auto_set
    publication_attempts, injected = [], False
    cursor_before = [row.as_dict() for row in server._cursor_store.list()]
    b_wave = cases['b']['wave_path']
    before_token = None

    def forbidden_publish(*a, **kw):
        publication_attempts.append(dict(arguments=a, kwargs=kw))
        raise GuardFailure('cursor_publication_after_parser_invalidation')

    async def changed_wave(paths, fn, **kwargs):
        nonlocal injected, before_token
        result = await original_wave_request(paths, fn, **kwargs)
        if not injected:
            injected = True
            def mutate():
                parser = server._get_parser(b_wave)
                before = (id(parser), id(getattr(parser, '_scope_epoch', None)))
                parser._scope_epoch = object()
                return before, (id(parser), id(parser._scope_epoch))
            before_token, after_token = await server._run_in_wave_thread(b_wave, mutate)
            report['parser_generation_injection'] = dict(before=before_token, after=after_token,
                interpretation='Actual loaded B FSDB parser epoch changed under wave lock; no file mutation.')
        return result

    server._run_in_wave_request = changed_wave
    server._cursor_store.set, server._cursor_store.auto_set = forbidden_publish, forbidden_publish
    try:
        rejected = await measured('diff_parser_changed', lambda: public('diff_first_divergence',
            dict(diff_args, cursor_name='p4_s1_must_not_publish')))
        require(injected and 'parser_generation_changed' in json.dumps(rejected), 'parser_change_not_rejected')
        require('diverged' not in rejected and not rejected.get('cursor')
                and not rejected.get('next_actions'), 'stale_diff_facts_published')
        require(not publication_attempts and [row.as_dict() for row in server._cursor_store.list()] == cursor_before,
                'stale_diff_cursor_published')
        for label in cases:
            with server._design_context_store.pin(handles[label]) as entry:
                require(entry.leases == 1, 'rejected_diff_leaked_pin_' + label)
    finally:
        server._run_in_wave_request = original_wave_request
        server._cursor_store.set, server._cursor_store.auto_set = original_set, original_auto_set
    # B is still usable after the failed comparison; this request uses the new
    # generation consistently. No native work is needed for the wave-only diff.
    resumed = await measured('diff_after_parser_rejection', lambda: public('diff_first_divergence',
        dict(diff_args, cursor_name='p4_s1_after_parser_change')))
    require(resumed.get('diverged') is True and resumed.get('first_divergence_time_ps') == 26000,
            'consistent_new_parser_generation_unusable')
    await source('b', 'instance', 'final')
    await driver('a', 'final')

    async def final_identities():
        rows = {}
        for label, case in cases.items():
            with server._design_context_store.pin(handles[label]) as entry:
                await server._validate_design_source(entry.context, timeout_sec=30)
                await server._validate_design_binding(entry.binding, timeout_sec=5)
                def token():
                    parser = server._get_parser(case['wave_path'])
                    return dict(parser_id=id(parser), parser_epoch_id=id(getattr(parser, '_scope_epoch', None)),
                                waveform_stat=server._get_wave_signature(case['wave_path']))
                rows[label] = dict(context_identity=entry.context.identity_sha256,
                    artifact=entry.binding.anchor.receipt(),
                    wave=await server._run_in_wave_thread(case['wave_path'], token))
                require(rows[label]['context_identity'] == identities[label], 'final_foreign_context_' + label)
        return rows
    report['final_identity_validation'] = await measured('final_identity_validation', final_identities)
    for label in cases:
        with server._design_context_store.pin(handles[label]) as entry:
            require(entry.leases == 1, 'final_request_leaked_pin_' + label)
    report['coverage'] = dict(real_native_source=True, real_native_drivers=True,
        real_wave_diff_and_action_replay=True, real_parser_generation_rejection=True,
        source_kdb_wave_current=True, actual_source_kdb_wave_byte_mutation='not_run',
        cancellation_timeout_native_integration='not_run', license_checkout_release='unverified',
        full_hierarchy='guarded_zero')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=str(MAIN))
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--mcp-evidence', required=True)
    parser.add_argument('--inventory', required=True,
                        help='Reviewed private JSON with cases A/B, file paths and KDB identities')
    parser.add_argument('--mode', choices=('boundary', 'chain'), default='boundary')
    parser.add_argument('--boundary-evidence', help='Reviewed real boundary JSON; required for chain mode')
    args = parser.parse_args()
    report = dict(status='failed', scope='bounded real dual identity chain, not deep trace/LSF/license release',
                  mode=args.mode,
                  expected_head=args.expected_head, python=sys.version,
                  environment={key: os.environ.get(key) for key in ENV})
    output_fd = os.dup(1)
    sys.stdout.flush()
    os.dup2(2, 1)
    code, started = 0, sample()
    try:
        asyncio.run(run(args, report))
    except BaseException as exc:
        report['error'] = dict(type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
        code = 1
    finally:
        report['total'] = dict(wall_ms=1000*(time.perf_counter()-started[0]),
            cpu_ms=1000*(time.process_time()-started[1]),
            ru_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Keep native fd1 directed to private stderr through C atexit. JSON is
        # written only through the saved stdout descriptor, so a native banner
        # can never be silently treated as acceptable JSON trailing data.
        with os.fdopen(output_fd, 'w') as output:
            print(json.dumps(report, indent=2, default=str), file=output, flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
