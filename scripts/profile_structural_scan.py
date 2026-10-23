#!/usr/bin/env python3
"""Opt-in structural-scan profiling through an isolated stdio MCP server.

Only this benchmark's child process is instrumented. Normal servers, licensed
execution placement, budgets and scan algorithms are unchanged. Reports contain
fixed phase names, numeric aggregates and hashes, never argument/RTL text.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import ExitStack
from contextvars import ContextVar
import functools
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import resource
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PHASES = frozenset({
    'handler', 'dispatch', 'compile_parse', 'compile_snapshot', 'source_index_key',
    'source_index', 'source_index_release', 'identity_capture', 'preprocessor_options',
    'identity_memo', 'identity_validate', 'file_validate', 'include_resolve',
    'lexical_cache', 'lexical_rules', 'semantic', 'native_metadata', 'native_binding',
    'native_metadata_validate', 'native_binding_validate', 'native_query', 'native_core',
    'source_supplement', 'query_context', 'source_cache', 'combine',
    'result_model', 'output_budget', 'serialize',
})
_active: ContextVar[PageProfile | None] = ContextVar('structural_profile', default=None)
_stack: ContextVar[tuple[str, ...]] = ContextVar('structural_profile_stack', default=())


class PageProfile:
    """Bounded aggregate bins; inclusive timings must not be added to parents."""
    def __init__(self):
        self.rows = defaultdict(lambda: dict(count=0, total_ns=0, max_ns=0,
                                            errors=0, hits=0, logical_bytes=0))
        self.lock = threading.Lock()

    def record(self, phase, parent, elapsed_ns, *, error=False, hit=False, logical_bytes=0):
        if phase not in PHASES or parent not in PHASES | {'root'}:
            raise ValueError('unknown profile phase')
        with self.lock:
            row = self.rows[(phase, parent)]
            row['count'] += 1
            row['total_ns'] += elapsed_ns
            row['max_ns'] = max(row['max_ns'], elapsed_ns)
            row['errors'] += int(error)
            row['hits'] += int(hit)
            row['logical_bytes'] += logical_bytes

    def snapshot(self):
        with self.lock:
            return [dict(phase=phase, parent=parent, count=v['count'],
                         total_ms=v['total_ns'] / 1e6, max_ms=v['max_ns'] / 1e6,
                         errors=v['errors'], hits=v['hits'], logical_bytes=v['logical_bytes'])
                    for (phase, parent), v in sorted(self.rows.items())]


def timed(function, phase):
    if phase not in PHASES:
        raise ValueError('unknown profile phase')

    def enter():
        previous = _stack.get()
        return previous[-1] if previous else 'root', _stack.set((*previous, phase)), time.perf_counter_ns()

    def finish(profiler, parent, token, started, args, result, failed):
        elapsed = time.perf_counter_ns() - started
        _stack.reset(token)
        hit = phase == 'identity_memo' and result is not None and not failed
        logical = (args[0].size if phase == 'file_validate' and result is True and not failed else 0)
        profiler.record(phase, parent, elapsed, error=failed, hit=hit, logical_bytes=logical)

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(*args, **kwargs):
            profiler = _active.get()
            if profiler is None:
                return await function(*args, **kwargs)
            parent, token, started = enter()
            result, failed = None, True
            try:
                result = await function(*args, **kwargs)
                failed = False
                return result
            finally:
                finish(profiler, parent, token, started, args, result, failed)
        return asynchronous

    @functools.wraps(function)
    def synchronous(*args, **kwargs):
        profiler = _active.get()
        if profiler is None:
            return function(*args, **kwargs)
        parent, token, started = enter()
        result, failed = None, True
        try:
            result = function(*args, **kwargs)
            failed = False
            return result
        finally:
            finish(profiler, parent, token, started, args, result, failed)
    return synchronous


def install(stack, server):
    from src import design_identity as identity
    from src import structural_native_runtime as native
    from src import structural_scan_runtime as lexical
    from src import structural_semantic_runtime as source
    from src import npi_structural_scan as core
    from src import native_query, design_backend_binding, compile_source_runtime

    bindings = [
        (server, '_dispatch', 'dispatch'),
        (server, '_parse_merged_compile_context', 'compile_parse'),
        (server, 'compute_snapshot_fingerprint', 'compile_snapshot'),
        (server, 'compile_source_index_key', 'source_index_key'),
        (server, 'scan_structural_risks', 'lexical_rules'),
        (server, '_structural_query_context', 'query_context'),
        (server, '_enforce_output_budget', 'output_budget'),
        (server, '_serialize_result', 'serialize'),
        (identity, 'extract_preprocessor_options', 'preprocessor_options'),
        (identity.DesignIdentityReader, 'capture', 'identity_capture'),
        (identity.DesignIdentityReader, '_cached_identity', 'identity_memo'),
        (identity.DesignIdentity, 'current', 'identity_validate'),
        (identity, '_content_current', 'file_validate'),
        (identity, '_resolve', 'include_resolve'),
        (lexical.StructuralScanRuntime, 'run', 'lexical_cache'),
        (source.SemanticScanRuntime, 'scan', 'source_cache'),
        (native, 'read_native_inputs', 'native_metadata'),
        (native, 'prepare_native_binding', 'native_binding'),
        (native_query.NativeQueryInputs, 'require_current', 'native_metadata_validate'),
        (design_backend_binding._BindingPolicy, 'require_current', 'native_binding_validate'),
        (core, 'scan_native_ports', 'native_core'),
        (native, 'combine_semantics', 'combine'),
        (compile_source_runtime.CompileSourceIndexRuntime, 'acquire', 'source_index'),
        (compile_source_runtime.CompileSourceIndexLease, 'release', 'source_index_release'),
    ]
    for owner, name, phase in bindings:
        stack.enter_context(patch.object(owner, name, timed(getattr(owner, name), phase)))

    # This callback is local to dispatch; instrument it at the existing runtime
    # boundary without changing any arguments, guard calls or deadlines.
    original = native.StructuralNativeRuntime.scan
    @functools.wraps(original)
    async def semantic(*args, **kwargs):
        kwargs['source_scan'] = timed(kwargs['source_scan'], 'source_supplement')
        return await original(*args, **kwargs)
    stack.enter_context(patch.object(native.StructuralNativeRuntime, 'scan', timed(semantic, 'semantic')))
    model = server.schemas.ScanStructuralRisksResult
    original_model = inspect.getattr_static(model, 'model_validate')
    stack.enter_context(patch.object(model, 'model_validate', classmethod(timed(original_model.__func__, 'result_model'))))


def write_private(path, payload):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream, indent=2)
    os.replace(temporary, path)


async def serve(output, enabled):
    import server
    from mcp.types import CallToolRequest
    from src import structural_native_runtime as native
    pages, oracle = [], hashlib.sha256()
    fact_count = query_count = 0
    original_query = native.query_native_scan
    def query(*args, **kwargs):
        nonlocal fact_count, query_count
        result = original_query(*args, **kwargs)
        query_count += 1
        for fact in result.get('facts', []):
            oracle.update(json.dumps(fact, sort_keys=True, separators=(',', ':')).encode())
            oracle.update(b'\n')
            fact_count += 1
        return result

    original_handler = server.app.request_handlers[CallToolRequest]
    async def handle(request):
        if request.params.name != 'scan_structural_risks':
            return await original_handler(request)
        profiler = PageProfile()
        token = _active.set(profiler if enabled else None)
        try:
            return await timed(original_handler, 'handler')(request)
        finally:
            _active.reset(token)
            pages.append(profiler.snapshot())
            write_private(output, dict(profile_enabled=enabled, timing='inclusive_by_parent_do_not_sum_nested_rows',
                pages=pages, local_native_queries=query_count,
                native_fact_count=fact_count, native_facts_sha256=oracle.hexdigest()))

    with ExitStack() as stack:
        if enabled:
            install(stack, server)
        stack.enter_context(patch.dict(server.app.request_handlers, {CallToolRequest: handle}))
        # The oracle wrapper is common to control/profile. It observes copied
        # facts only, after the native owner has already closed.
        stack.enter_context(patch.object(native, 'query_native_scan', timed(query, 'native_query') if enabled else query))
        await server.main()


def receipt(result):
    semantic = result['semantic']
    groups = [g for g in semantic.get('result_groups', []) if g['backend'] == 'verdi_npi']
    native = groups[0] if groups else {}
    progress = native.get('scan_progress', {})
    categories = {'constant_connection', 'open_input', 'constant_comparison', 'propagated_constant', 'constant_control'}
    statuses = {'complete', 'partial', 'not_run', 'unavailable', 'unsupported', 'failed'}
    coverage = [dict(category=c['category'], status=c['status'], total_facts=c['total_facts'],
                     gaps_sha256=hashlib.sha256(json.dumps(c['gaps']).encode()).hexdigest())
                for c in semantic['category_coverage'] if c['category'] in categories and c['status'] in statuses]
    metric_names = {'native_traversal_ms', 'native_call_count', 'native_port_count', 'native_handle_count',
        'native_handle_peak', 'native_handle_batches', 'native_instance_count', 'design_load_ms',
        'query_ms', 'language_cleanup_ms', 'semantic_total_ms'}
    metrics = {k: v for k, v in semantic.get('metrics', {}).items()
               if k in metric_names and type(v) in {int, float} and math.isfinite(v) and v >= 0}
    return dict(status=semantic['status'] if semantic['status'] in statuses else 'unknown',
                counts={k: v for k, v in semantic['counts'].items() if k in categories and type(v) is int},
                category_coverage=coverage,
                completed_instances=progress.get('completed_instances'), pending_scopes=progress.get('pending_scopes'),
                lexical_files=result['files_scanned'], lexical_risks=result['total_risks'],
                kdb_identity_sha256=native.get('kdb_binding', {}).get('identity_sha256'),
                native_metrics=metrics)


async def run(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'run.json').exists() or (output / 'profile.json').exists():
        raise ValueError('output directory already contains a run')
    environment = {**os.environ, 'TRACEWEAVE_TELEMETRY': '0', 'TRACEWEAVE_AUTO_KDB': '0',
                   'TRACEWEAVE_CACHE_DIR': str(output / 'cache')}
    child = ['--serve', '--profile-output', str(output / 'profile.json')] + (['--profile'] if args.profile else [])
    parameters = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()), *child],
                                      env=environment, cwd=str(ROOT))
    call = dict(compile_log=os.path.abspath(args.compile_log), simulator=args.simulator, top_hint=args.top,
                semantic_scope=args.scope, analysis_mode=args.mode,
                semantic_categories=['constant_connection', 'open_input'],
                semantic_timeout_sec=15, semantic_max_instances=args.initial_instances)
    rows, last_completed, identity = [], 0, None
    with (output / 'server.stderr').open('w') as stderr:
        async with stdio_client(parameters, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for page in range(args.max_pages):
                    started = time.perf_counter()
                    response = await session.call_tool('scan_structural_risks', call)
                    elapsed = (time.perf_counter() - started) * 1000
                    if response.isError:
                        raise RuntimeError('MCP scan failed; inspect the local server log')
                    result = json.loads(response.content[0].text)
                    if 'semantic' not in result or result['semantic'].get('termination_reason'):
                        raise RuntimeError('scan terminated; inspect the local server log')
                    row = dict(page=page, wall_ms=elapsed, result=receipt(result))
                    rows.append(row)
                    if row['result']['kdb_identity_sha256']:
                        identity = identity or row['result']['kdb_identity_sha256']
                        if identity != row['result']['kdb_identity_sha256']:
                            raise RuntimeError('native identity changed between pages')
                    write_private(output / 'run.json', dict(profile_enabled=args.profile, pages=rows,
                        wall_ms_total=sum(r['wall_ms'] for r in rows),
                        peak_child_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss))
                    semantic = result['semantic']
                    if not semantic.get('continuation'):
                        if row['result']['pending_scopes']:
                            raise RuntimeError('native frontier is blocked')
                        break
                    completed = row['result']['completed_instances']
                    if completed <= last_completed and semantic['counts'] == (rows[-2]['result']['counts'] if page else {}):
                        raise RuntimeError('continuation made no progress')
                    last_completed = completed
                    call = {**call, 'semantic_continuation': semantic['continuation'],
                            'semantic_max_instances': args.instances}
                else:
                    raise RuntimeError('max-pages reached before frontier completion')
    payload = dict(profile_enabled=args.profile, pages=rows,
                   wall_ms_total=sum(r['wall_ms'] for r in rows),
                   peak_child_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    write_private(output / 'run.json', payload)
    print(json.dumps(dict(calls=len(rows), wall_sec=payload['wall_ms_total'] / 1000,
                         peak_child_rss_kib=payload['peak_child_rss_kib'])))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--profile-output', help=argparse.SUPPRESS)
    p.add_argument('--compile-log')
    p.add_argument('--simulator', default='vcs')
    p.add_argument('--top')
    p.add_argument('--scope')
    p.add_argument('--mode', choices=['fast', 'auto', 'deep'], default='auto')
    p.add_argument('--profile', action='store_true')
    p.add_argument('--output')
    p.add_argument('--initial-instances', type=int, default=1000)
    p.add_argument('--instances', type=int, default=25000)
    p.add_argument('--max-pages', type=int, default=120)
    return p


if __name__ == '__main__':
    args = parser().parse_args()
    if args.serve:
        if not args.profile_output:
            parser().error('--profile-output is required for the child server')
        asyncio.run(serve(Path(args.profile_output), args.profile))
    else:
        if not args.compile_log or not args.output:
            parser().error('--compile-log and --output are required')
        if not (1 <= args.initial_instances <= 100000 and 1 <= args.instances <= 100000
                and 1 <= args.max_pages <= 1000):
            parser().error('invalid instance/page budget')
        asyncio.run(run(args))
