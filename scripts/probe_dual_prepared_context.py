#!/usr/bin/env python3
"""Opt-in real dual-KDB boundary probe, no waveform reads or rebuilds.

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
            reject('unexpected_waveform_read')
        if module == 'verdi_npi_backend.py' and name in {'find_driver', 'get_dynamic_step'}:
            counts[name] += 1
        if module in {'compile_log_parser.py', 'design_backend_binding.py', 'verdi_backend.py'} and name in {
                'parse_compile_log', 'prepare_backend_binding', 'probe_verdi_backend'}:
            counts[name] += 1

    def audit(event, values):
        if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork'}:
            reject('external_process')
        if event == 'import' and str(values[0]).split('.')[0] == 'pyslang':
            reject('semantic_frontend')

    sys.path.insert(0, str(repo))
    sys.addaudithook(audit)
    sys.setprofile(profile)
    threading.setprofile(profile)
    try:
        import server
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
                        return _fn(*a, **kw)
                    setattr(sdk, name, counted)
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
        phase = 'diagnostic_cleanup'
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=str(MAIN))
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--mcp-evidence', required=True)
    parser.add_argument('--inventory', required=True,
                        help='Reviewed private JSON with cases A/B, file paths and KDB identities')
    args = parser.parse_args()
    report = dict(status='failed', scope='native reset boundary only, not deep trace/LSF/license release',
                  expected_head=args.expected_head, python=sys.version,
                  environment={key: os.environ.get(key) for key in ENV})
    output_fd = os.dup(1)
    sys.stdout.flush()
    os.dup2(2, 1)
    code = 0
    try:
        asyncio.run(run(args, report))
    except BaseException as exc:
        report['error'] = dict(type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
        code = 1
    finally:
        os.dup2(output_fd, 1)
        os.close(output_fd)
        print(json.dumps(report, indent=2, default=str), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
