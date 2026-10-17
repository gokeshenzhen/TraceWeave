#!/usr/bin/env python3
"""One real cc31 stdio MCP sequence; review before running, no compilation.

The client spawns one instrumented real server. Server guards prohibit child
processes/Python writes except its private report; native SDK lock I/O remains
SDK-owned. stdout is one JSON report; stderr belongs in a private /tmp log.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True
import argparse
import asyncio
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import resource
import tempfile
import threading
import time

MAIN = Path(__file__).resolve().parents[1]
TARGET = 'top_tb.u0'
ENV = {'PYTHONDONTWRITEBYTECODE': '1', 'TRACEWEAVE_NPI_EXECUTION': 'local',
       'TRACEWEAVE_CONNECTIVITY_ROUTE': 'auto', 'TRACEWEAVE_AUTO_KDB': '0',
       'TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY': 'off', 'TRACEWEAVE_TELEMETRY': '0',
       'TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE': '0', 'TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION': '0'}


class ProbeFailure(BaseException):
    pass


def require(value, reason):
    if not value:
        raise ProbeFailure(reason)


def head_at(repo):
    directory = repo / '.git'
    if directory.is_file():
        directory = (repo / directory.read_text().strip().removeprefix('gitdir: ')).resolve()
    common = directory
    if (directory / 'commondir').exists():
        common = (directory / (directory / 'commondir').read_text().strip()).resolve()
    value = (directory / 'HEAD').read_text().strip()
    if not value.startswith('ref: '):
        return value
    ref = value.removeprefix('ref: ')
    for base in (directory, common):
        if (base / ref).is_file():
            return (base / ref).read_text().strip()
    for line in (common / 'packed-refs').read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    raise ProbeFailure('head_unavailable')


def sample():
    return time.perf_counter(), time.process_time()


def costs(start):
    return {'wall_ms': (time.perf_counter() - start[0]) * 1000,
            'cpu_ms': (time.process_time() - start[1]) * 1000,
            'ru_maxrss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


class Guards:
    def __init__(self, repo, report_path):
        self.repo, self.report_path, self.phase = str(repo), str(report_path), 'load_server'
        # fdopen of the protocol's duplicated stdout does not open a user file.
        # Permit only descriptors pointing to our inherited output channels.
        self.output_fd_identities = {
            (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in (1, 2)
        }
        self.denied = Counter(dict.fromkeys(('full_build', 'lexical_bootstrap_or_scan', 'slang',
            'static_or_legacy_context', 'automatic_kdb_build', 'external_process', 'python_write',
            'eager_language_list', 'waveform', 'repeat_parse_or_probe'), 0))
        self.calls = Counter(dict.fromkeys(('init', 'load_design', 'end', 'parse_compile_log',
            'prepare_design_context', 'probe_verdi_backend', 'prepare_backend_binding',
            'capture_kdb_anchor', 'select_backend', 'query_source', 'lookup_scope', 'open_children',
            'lang.handle_by_name', 'lang.iterate', 'lang.scan', 'lang.release_handle',
            'lang.release_all_handles'), 0))

    def reject(self, key):
        self.denied[key] += 1
        raise ProbeFailure(key)

    def audit(self, event, args):
        if event == 'import' and str(args[0]).split('.')[0] == 'pyslang':
            self.reject('slang')
        if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn',
                     'os.spawn', 'os.fork', 'os.forkpty'}:
            self.reject('external_process')
        if event == 'open':
            name, mode, flags = args
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            output_fd = (type(name) is int and
                         (os.fstat(name).st_dev, os.fstat(name).st_ino) in self.output_fd_identities)
            if write and not output_fd and name not in {self.report_path, os.devnull}:
                self.reject('python_write')
        if event in {'os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.link', 'os.symlink',
                     'os.chmod', 'os.chown', 'os.utime', 'os.truncate'}:
            self.reject('python_write')

    def profile(self, frame, event, arg):
        if event != 'call':
            return
        code = frame.f_code
        # Python reports class-body execution as a call while importing model
        # definitions. Only function frames represent the work guarded here;
        # actual pyslang imports remain independently blocked by the audit hook.
        if not code.co_flags & inspect.CO_NEWLOCALS:
            return
        module, name, qualified = Path(code.co_filename).name, code.co_name, code.co_qualname
        if '/pynpi/' in code.co_filename.replace('\\', '/'):
            if name in {'get_1m_list', 'get_top_inst_list', 'list_all_module_define', 'mod_define_get_inst'}:
                self.reject('eager_language_list')
            return
        if not code.co_filename.startswith(self.repo + os.sep) or name.startswith('<'):
            return
        if module == 'tb_hierarchy_builder.py' and name in {'build_hierarchy', 'build_component_tree'}:
            self.reject('full_build')
        if ((module == 'tb_hierarchy_builder.py' and name in {'scan_sv_file', 'scan_sv_text', 'scan_preprocessed_sv', '_scan_user_files'})
                or module == 'bounded_hierarchy_bootstrap.py' and name == 'build_bounded_connectivity_context'
                or module == 'sv_preprocessor.py' and name in {'preprocess', '_expand_hierarchy_macro_line'}
                or module == 'structural_scanner.py' and name in {'scan_structural_risks', '_scan_file'}):
            self.reject('lexical_bootstrap_or_scan')
        if module == 'kdb_builder.py' and name in {'build_kdb', '_run_phase', '_write_build_script'}:
            self.reject('automatic_kdb_build')
        if ((module.startswith('slang_') and name not in {'__init__'})
                or module in {'source_graph_worker.py', 'structural_semantic_worker.py'}
                   and name in {'execute', 'execute_build', 'build_session', 'create_semantic_frontend_session'}
                or module in {'source_graph_runtime.py', 'source_graph_session_runtime.py'}
                   and qualified.startswith(('SourceGraphRuntime.', 'IsolatedSourceGraphProcessRunner.', 'PersistentSourceGraphProcessRunner.'))):
            self.reject('slang')
        if ((module == 'connectivity_backend.py' and qualified.startswith('StaticConnectivityBackend.') and name.startswith('find_'))
                or module == 'handle_tools.py' and name == 'find_tb_instance'
                or module in {'signal_driver.py', 'signal_load.py'} and name in {'explain_signal_driver', 'find_signal_loads'}
                or module == 'divergence_context.py' and name == 'resolve_context'):
            self.reject('static_or_legacy_context')
        if module in {'fsdb_parser.py', 'vcd_parser.py', 'fst_parser.py'} and qualified.startswith(('FSDBParser.', 'VCDParser.', 'FstParser.')):
            self.reject('waveform')
        if name in {'parse_compile_log', 'prepare_design_context', 'probe_verdi_backend', 'prepare_backend_binding',
                    'capture_kdb_anchor', 'select_backend'}:
            self.calls[name] += 1
            if self.phase == 'repeat_instance' and name in {'parse_compile_log', 'prepare_design_context',
                                                          'probe_verdi_backend', 'prepare_backend_binding'}:
                self.reject('repeat_parse_or_probe')
        if module == 'verdi_npi_backend.py' and qualified == 'VerdiNpiBackend.query_source':
            self.calls['query_source'] += 1
        if module == 'npi_hierarchy_source.py' and qualified.startswith('NpiHierarchySourceProvider.') and name in {'lookup_scope', 'open_children'}:
            self.calls[name] += 1


def server_process(args):
    repo, report_path = Path(args.repo).resolve(), Path(args.server_report).resolve()
    kdb = Path(args.project).resolve() / 'simv.daidir/kdb.elab++'
    guard, captures, sdk = Guards(repo, report_path), [], None
    context = binding = last_reply_finished = None
    report = {'status': 'failed', 'head': head_at(repo), 'phases': {}, 'calls': [],
              'environment': {k: os.environ.get(k) for k in ENV}}
    require(report['head'] == args.expected_head, 'unexpected_head')
    require(all(os.environ.get(k) == v for k, v in ENV.items()), 'explicit_environment_required')
    sys.path.insert(0, str(repo))
    sys.addaudithook(guard.audit)
    sys.setprofile(guard.profile)
    threading.setprofile(guard.profile)
    started = sample()
    import server
    from mcp import types
    from src import compile_input_capture, npi_hierarchy_source, verdi_npi_backend as npi
    require(Path(server.__file__).resolve() == repo / 'server.py', 'wrong_server_import')
    for module_name in ('config', 'src.design_context_runtime', 'src.verdi_npi_backend', 'src.hierarchy_source_service'):
        require(Path(sys.modules[module_name].__file__).resolve().is_relative_to(repo), 'wrong_checkout_import')
    report['phases']['load_server'] = costs(started)
    original_init = compile_input_capture.CompileInputCapture.__init__
    def capture_init(self, *a, **kw):
        original_init(self, *a, **kw)
        captures.append(self)
    compile_input_capture.CompileInputCapture.__init__ = capture_init
    original_import = npi._import_pynpi
    def imported():
        nonlocal sdk
        modules = original_import()
        if modules is not None and sdk is None:
            sdk = modules[0]
            from pynpi import lang, lang_l0
            for module, names, prefix in ((sdk, ('init', 'load_design', 'end'), ''),
                (lang_l0, ('handle_by_name', 'iterate', 'scan', 'release_handle'), 'lang.'),
                (lang, ('release_all_handles',), 'lang.')):
                for name in names:
                    original = getattr(module, name)
                    def counted(*a, _fn=original, _key=prefix + name, **kw):
                        guard.calls[_key] += 1
                        return _fn(*a, **kw)
                    setattr(module, name, counted)
        return modules
    npi._import_pynpi, npi._silence_native_stdio = imported, nullcontext
    original_handler = server.app.request_handlers[types.CallToolRequest]
    async def observed(req):
        nonlocal last_reply_finished
        index = len(report['calls'])
        phase = ('prepare', 'first_instance', 'repeat_instance')[index] if index < 3 else 'unexpected_call'
        guard.phase, started, before = phase, sample(), guard.calls.copy()
        try:
            response = await original_handler(req)
            payload = json.loads(response.root.content[0].text)
            report['calls'].append({'phase': phase, 'tool': req.params.name,
                                    'arguments': req.params.arguments, 'result': payload})
            return response
        finally:
            report['phases'][phase] = dict(costs(started), api_counts={k: guard.calls[k] - before[k] for k in guard.calls})
            if phase == 'repeat_instance':
                last_reply_finished = sample()
    server.app.request_handlers[types.CallToolRequest] = observed
    try:
        asyncio.run(server.main())
        if last_reply_finished is not None:
            report['phases']['post_repeat_reply_to_product_stdio_close'] = costs(last_reply_finished)
        report['product_stdio_close'] = {'api_counts': dict(guard.calls),
            'note': 'Actual product transport-close counters before any diagnostic SDK oracle or harness end; no claim of native-session release.'}
        require([row['tool'] for row in report['calls']] == ['prepare_design_context', 'find_tb_instance', 'find_tb_instance'], 'public_sequence_incomplete')
        prepared, first, repeat = [row['result'] for row in report['calls']]
        server.schemas.PrepareDesignContextResult.model_validate(prepared)
        for result in (first, repeat):
            server.schemas.SourceInstanceResult.model_validate(result)
            require(result['facts_origin'] == 'verdi_npi' and result['backend_status']['actual_backend'] == 'verdi_npi', 'not_real_npi')
            require(len(result['hits']) == 1 and result['hits'][0]['path'] == TARGET, 'exact_target_missing')
        handle = prepared['design_handle']
        for index, result in enumerate((first, repeat)):
            receipt = result['backend_status']['design_context']
            require(result['design_handle'] == handle and receipt['binding_reused'] is bool(index), 'binding_receipt_mismatch')
            require(receipt['context_identity_sha256'] == prepared['context_identity_sha256'], 'context_identity_mismatch')
            require(receipt['artifact']['load_status'] == 'loaded' and receipt['artifact']['source_association'] == 'unverified', 'invalid_load_evidence')
        excluded = ['backend_status', 'design_context', 'query_metrics']
        def facts(value):
            value = deepcopy(value)
            for key in excluded:
                value.pop(key, None)
            return value
        report['equality'] = {'excluded_runtime_fields': excluded, 'first_repeat': facts(first) == facts(repeat)}
        require(report['equality']['first_repeat'], 'repeat_semantic_difference')
        with server._design_context_store.pin(handle) as entry:
            context, binding = entry.context, entry.binding
            require(binding is not None and binding.anchor is not None, 'binding_missing')
            require(Path(binding.anchor.canonical_path) == kdb.resolve(), 'unexpected_candidate')
            report['input_scale'] = {'files': context.snapshot.file_count, 'raw_bytes': context.snapshot.total_bytes,
                                     'source_gaps': list(context.gaps), 'include_resolution_complete': context.include_resolution_complete}
            # Separate diagnostic oracle after both public requests. No reload,
            # eager list, product projector, retained Language handle or timing blend.
            guard.phase, started = 'independent_exact_sdk_oracle', sample()
            before = guard.calls.copy()
            require(npi_hierarchy_source._OWNER_LOCK.acquire(blocking=False), 'oracle_owner_busy')
            try:
                from pynpi import lang
                def copied_oracle():
                    target = lang.handle_by_name(TARGET, None)
                    require(target is not None, 'oracle_target_missing')
                    parent = target.scope_handle()
                    return {'path': target.full_name(), 'kind': target.type(),
                            'parent': parent.full_name() if parent is not None else None,
                            'module': target.def_name(), 'source_file': target.file(), 'source_line': target.line_no(),
                            'definition_file': target.def_file(), 'definition_line': target.def_line_no()}
                primary = None
                try:
                    oracle = copied_oracle()
                except BaseException as exc:
                    primary = exc
                    raise
                finally:
                    try:
                        cleanup = lang.release_all_handles()
                        require(cleanup == 1, 'oracle_cleanup_unconfirmed')
                    except BaseException as exc:
                        report['oracle_cleanup_failure'] = type(exc).__name__
                        if primary is None:
                            raise
            finally:
                npi_hierarchy_source._OWNER_LOCK.release()
            report['phases'][guard.phase] = dict(costs(started), api_counts={k: guard.calls[k] - before[k] for k in guard.calls})
            report['independent_sdk_oracle'] = oracle
            require(oracle['path'] == TARGET and oracle['parent'] == 'top_tb' and oracle['module'] == 'des'
                    and oracle['kind'] == 'npiModule', 'archived_positive_scope_mismatch')
            hit = first['hits'][0]
            for field in ('path', 'parent', 'module'):
                require(hit[field] == oracle[field], 'native_scope_fact_mismatch')
            for filename, line in (('source_file', 'source_line'), ('definition_file', 'definition_line')):
                valid = isinstance(oracle[filename], str) and bool(oracle[filename]) and type(oracle[line]) is int and oracle[line] >= 1
                require(hit[filename] == (oracle[filename] if valid else None)
                        and hit[line] == (oracle[line] if valid else None), 'native_location_mismatch')
            require(hit['definition_id'] and hit['definition_file'] and type(hit['definition_line']) is int, 'positive_definition_location_missing')
            guard.phase, started = 'final_identity_before_end', sample()
            report['final_context_current'] = context.current()
            report['final_anchor_current'] = binding.anchor.current()
            binding.require_current()
            report['phases'][guard.phase] = costs(started)
            require(report['final_context_current'] and report['final_anchor_current'], 'final_identity_changed')
        prepare_counts = report['phases']['prepare']['api_counts']
        require(all(prepare_counts[k] == 0 for k in ('init', 'load_design', 'probe_verdi_backend', 'select_backend')), 'prepare_did_native_work')
        require(guard.calls['init'] > 0 and guard.calls['load_design'] > 0 and guard.calls['query_source'] == 2, 'native_calls_missing')
        require(guard.calls['open_children'] == 0, 'exact_path_walked_siblings')
        report['status'] = 'ok'
    except BaseException as exc:
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
    finally:
        guard.phase, started = 'diagnostic_harness_end_owned_sdk', sample()
        before = guard.calls.copy()
        try:
            if sdk is not None and guard.calls['init']:
                sdk.end()
                npi._NPI_INITIALIZED_IDS.discard(id(sdk))
                npi._NPI_ACTIVE_DESIGNS.pop(id(sdk), None)
        except BaseException as exc:
            report['status'], report['end_failure'] = 'failed', type(exc).__name__
        report['phases'][guard.phase] = dict(costs(started), api_counts={k: guard.calls[k] - before[k] for k in guard.calls})
        report['diagnostic_end_note'] = 'Extra harness cleanup, NOT a product normal-close/release or license checkout measurement.'
        if context is not None and binding is not None:
            guard.phase, started = 'identity_after_diagnostic_end', sample()
            try:
                report['after_end_context_current'] = context.current()
                report['after_end_anchor_current'] = binding.anchor.current()
                binding.require_policy_current()
                if not report['after_end_context_current'] or not report['after_end_anchor_current']:
                    report['status'] = 'failed'
            except BaseException as exc:
                report['status'], report['after_end_failure'] = 'failed', type(exc).__name__
            report['phases'][guard.phase] = costs(started)
        report['captures'] = {'count': len(captures), 'body_count': sum(c.body_count for c in captures),
                              'resident_bytes': sum(c.resident_bytes for c in captures)}
        if report['captures']['body_count'] or report['captures']['resident_bytes'] or any(guard.denied.values()):
            report['status'] = 'failed'
        report['api_counts'], report['guard_counts'] = dict(guard.calls), dict(guard.denied)
        report['all_guard_counts_zero'] = not any(guard.denied.values())
        report['final_head'] = head_at(repo)
        if report['final_head'] != args.expected_head:
            report['status'] = 'failed'
        report_path.write_text(json.dumps(report, indent=2))


async def client(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from datetime import timedelta
    require(all(os.environ.get(k) == v for k, v in ENV.items()), 'explicit_environment_required')
    repo = Path(args.repo).resolve()
    require(head_at(repo) == args.expected_head, 'unexpected_head')
    report = {'probe': 'p3_cc31_real_stdio_instance_v1', 'status': 'failed', 'head': args.expected_head,
              'client_phases': {}, 'calls': [], 'environment': {k: os.environ.get(k) for k in ENV},
              'operations': ['MCP initialize/list_tools', 'prepare_design_context', 'exact instance',
                             'repeat exact instance', 'stdio EOF', 'independent SDK oracle/final identity/end'],
              'scope': {'timing': 'instrumented single sample; not acceleration/OS cold or checkout evidence',
                        'rss': 'Linux per-process high-water KiB, not memory allocation deltas',
                        'native_io': 'Python writes blocked in server except private report; SDK-native lock I/O not intercepted',
                        'not_tested': ['other five tools/consumers', 'source fallback', 'LSF', 'waveforms',
                                       'source/KDB association', 'license checkout counts', 'persistent session/concurrency'],
                        'baseline': 'P0 archived cc31 top_tb.u0/module des/parent top_tb; old lexical instance line is not Native definition location',
                        'oracle': 'separate exact SDK lookup after public sequence, counted/timed separately; no product projector used'}}
    started_total = sample()
    # Preserve the child evidence even if protocol setup or a public call fails.
    # The runner archives this private directory; do not silently delete it.
    evidence = Path(tempfile.mkdtemp(prefix='traceweave-p3-instance-', dir='/tmp'))
    report['private_evidence_dir'] = str(evidence)
    server_report = evidence / 'server.json'
    stderr_path = evidence / 'server.stderr'
    close_started = None
    try:
        params = StdioServerParameters(command=sys.executable,
            args=['-B', str(Path(__file__).resolve()), '--server', '--repo', str(repo),
                  '--project', str(Path(args.project).resolve()),
                  '--expected-head', args.expected_head, '--server-report', str(server_report)],
            cwd=str(repo), env=dict(os.environ))
        with stderr_path.open('w+') as errors:
            started_spawn = sample()
            try:
                async with stdio_client(params, errlog=errors) as (read, write):
                    report['client_phases']['spawn_and_connect'] = costs(started_spawn)
                    try:
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                            started = sample()
                            initialized = await session.initialize()
                            tools = await session.list_tools()
                            report['client_phases']['initialize_and_list'] = costs(started)
                            report['protocol_version'] = initialized.protocolVersion
                            report['listed_tools'] = {t.name: t.model_dump(mode='json') for t in tools.tools
                                                      if t.name in {'prepare_design_context', 'find_tb_instance'}}
                            require(set(report['listed_tools']) == {'prepare_design_context', 'find_tb_instance'}, 'missing_public_tools')
                            async def call(phase, name, arguments):
                                started = sample()
                                response = await session.call_tool(name, arguments)
                                report['client_phases'][phase] = costs(started)
                                require(not response.isError, 'mcp_error_response')
                                require(len(response.content) == 1 and response.content[0].type == 'text', 'unexpected_content')
                                value = json.loads(response.content[0].text)
                                report['calls'].append({'phase': phase, 'tool': name, 'arguments': arguments,
                                                        'result': value, 'raw_mcp': response.model_dump(mode='json')})
                                require('error' not in value, 'public_tool_error')
                                return value
                            prepared = await call('prepare', 'prepare_design_context', {
                                'compile_log': str(Path(args.project).resolve() / 'comp.log'), 'simulator': 'vcs'})
                            require(prepared['semantic_status'] == 'not_run' and prepared['kdb_status'] == 'not_checked', 'prepare_claim_invalid')
                            arguments = {'design_handle': prepared['design_handle'], 'path': TARGET}
                            await call('first_instance', 'find_tb_instance', arguments)
                            await call('repeat_instance', 'find_tb_instance', arguments)
                            close_started = sample()
                    finally:
                        if close_started is None:
                            close_started = sample()
            finally:
                if close_started is not None:
                    report['client_phases']['stdio_close_including_child_diagnostics'] = costs(close_started)
        require(server_report.exists(), 'server_report_missing_after_shutdown')
        report['server'] = json.loads(server_report.read_text())
        require(report['server']['status'] == 'ok', 'server_acceptance_failed')
        report['status'] = 'ok'
    except BaseException as exc:
        report['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
    finally:
        if server_report.exists():
            report['raw_server_report'] = server_report.read_text()
            try:
                report['server'] = json.loads(report['raw_server_report'])
            except ValueError:
                report['status'] = 'failed'
        if stderr_path.exists():
            report['private_stderr'] = stderr_path.read_text()
    report['client_process_total'] = costs(started_total)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--repo', default=str(MAIN))
    cli.add_argument('--expected-head', required=True)
    cli.add_argument('--project', required=True, help='Existing cc31 tb directory; never compiled or modified.')
    cli.add_argument('--server', action='store_true', help=argparse.SUPPRESS)
    cli.add_argument('--server-report', help=argparse.SUPPRESS)
    arguments = cli.parse_args()
    if arguments.server:
        server_process(arguments)
    else:
        raise SystemExit(asyncio.run(client(arguments)))
