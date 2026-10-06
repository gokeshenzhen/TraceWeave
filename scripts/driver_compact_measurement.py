"""Presentation and fresh-process memory helpers for the driver benchmark.

This module reads repository synthetic inputs only. A captured model is never
queried again to obtain another format, and timing never includes the oracle.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.driver_evidence_codec import DecodeLimits, EncodeLimits, encode_driver_result
from src.evidence_output import expand_compact_result
from src.schemas import ExplainDriverResult


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def guards_source(count):
    body = 'q=0;\n' + '\n'.join(f"if (sel == 9'd{i}) q=data ^ 9'd{i};" for i in range(count))
    return ('module t(input logic [8:0] sel,data,output logic [8:0] q); '
            f'always_comb begin {body} end endmodule')


def query_result(backend, target, limits=None, wave=''):
    from src.source_graph_backend import _query_claim_semantics, _query_receipt
    query = backend._entry.query_engine.query_driver(target, **{'max_depth': 10, **(limits or {})})
    claim = _query_claim_semantics(query)
    result = backend._map_driver(query, wave_path=wave, recursive=True,
        requested_signal_path=target, claim_semantics=claim)
    result['_source_graph_query_receipt'] = _query_receipt(query, claim_semantics=claim)
    return result


def short_path_model(source, target, limits=None):
    """Independent in-memory frontend input, with the literal source name 'source'."""
    import pyslang
    from src.slang_connectivity_projector import SlangConnectivityProjector
    from src.connectivity_ir import ConnectivityIR
    from src.connectivity_query import ConnectivityQueryEngine
    from src.source_graph_backend import SourceGraphConnectivityBackend
    from src.hierarchy_provider import ConnectivityIRHierarchyProvider
    tree = pyslang.syntax.SyntaxTree.fromText(source)
    compilation = pyslang.ast.Compilation(); compilation.addSyntaxTree(tree)
    projection = SlangConnectivityProjector(source_manager=tree.sourceManager).project(compilation.getRoot())
    ir = ConnectivityIR.from_dict(projection.ir.to_dict())
    backend = SourceGraphConnectivityBackend(SimpleNamespace(ir=ir, query_engine=ConnectivityQueryEngine(ir),
        hierarchy_provider=ConnectivityIRHierarchyProvider(ir),
        artifact_scope_receipt=SimpleNamespace(scope=SimpleNamespace(top=target.split('.')[0]))))
    raw = query_result(backend, target, limits)
    return ExplainDriverResult.model_validate({k:v for k,v in raw.items() if not k.startswith('_')})


def encode_text(model, limits):
    return json.dumps(encode_driver_result(model, limits=limits), ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def timings(fn, repeats):
    wall, cpu = [], []
    for _ in range(repeats):
        w, c = time.perf_counter(), time.process_time()
        fn()
        wall.append((time.perf_counter() - w) * 1000)
        cpu.append((time.process_time() - c) * 1000)
    return {name: dict(median=statistics.median(values), min=min(values), max=max(values))
            for name, values in (('wall_ms', wall), ('cpu_ms', cpu))}


def rss_current_kib(field='VmRSS'):
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith(field + ':'):
            return int(line.split()[1])
    return None


def memory_sample(path, mode, limits):
    # Load/validate before starting tracemalloc; each mode runs in a fresh process.
    text = Path(path).read_text()
    value = (json.loads(text) if mode == 'decode' else ExplainDriverResult.model_validate_json(text))
    start = rss_current_kib()
    tracemalloc.start()
    if mode == 'full': output = value.model_dump_json(indent=2, exclude_none=True)
    elif mode == 'compact': output = encode_text(value, limits)
    else: output = expand_compact_result(value)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(input_resident_rss_kib=start, end_rss_kib=rss_current_kib(),
                process_peak_rss_kib=rss_current_kib('VmHWM'),
                ru_maxrss_kib_may_include_pre_exec_parent=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                python_peak_allocation_bytes=peak, output_items=len(output))


def measure_presentation(model, directory, *, repeats=50, limits=None, stem='presentation', memory=True):
    from src.driver_evidence import expand_driver_evidence
    from src.connectivity_query import ConnectivityQueryEngine
    from src.source_graph_backend import SourceGraphConnectivityBackend
    from src.source_graph_runtime import SourceGraphRuntime
    import server
    limits = limits or EncodeLimits()
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    pretty = server._serialize_result(model)
    public = json.loads(pretty)
    minified = json.dumps(public, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('presentation started additional analysis')
    with (patch.object(ConnectivityQueryEngine, 'query_driver', forbidden),
          patch.object(SourceGraphConnectivityBackend, 'find_driver', forbidden),
          patch.object(SourceGraphRuntime, 'prepare', forbidden),
          patch.object(server, '_get_parser', forbidden),
          patch.object(server, '_wave_locks_for', forbidden)):
        compact = encode_text(model, limits)
        wire = json.loads(compact)
        restored = expand_compact_result(compact)
        assert canonical(restored) == canonical(public)
        expanded_sections = []
        for section in ('driver_chain', 'bit_provenance'):
            groups = public.get(section) or []
            if groups and all('evidence_index' in group and 'statement_evidence' in group for group in groups):
                assert canonical(expand_driver_evidence(restored[section])) == canonical(expand_driver_evidence(groups))
                expanded_sections.append(section)
        timing = {
            'full': timings(lambda: server._serialize_result(model), repeats),
            'minified': timings(lambda: model.model_dump_json(exclude_none=True), repeats),
            'encode': timings(lambda: encode_text(model, limits), repeats),
            'decode_with_parse': timings(lambda: expand_compact_result(compact), repeats),
        }
        assert canonical(json.loads(server._serialize_result(model))) == canonical(public)
    paths = {}
    for name, text in (('full', pretty), ('compact', compact)):
        path = directory / f'{stem}.{name}.json'
        path.write_text(text)
        paths[name] = str(path)
    memory_rows = {}
    if memory:
        for mode in ('full', 'compact', 'decode'):
            cmd = [sys.executable, str(Path(__file__).resolve()), '--memory-mode', mode,
                   '--input', paths['compact' if mode == 'decode' else 'full'],
                   '--limits', json.dumps(asdict(limits))]
            completed = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=ROOT)
            memory_rows[mode] = json.loads(completed.stdout)
    return dict(repeats=repeats, strict_roundtrip=True,
        statement_roundtrip=True if expanded_sections else 'not_applicable',
        statement_sections_checked=expanded_sections,
        codec_extra_analysis_calls=len(calls), encode_limits=asdict(limits), decode_limits=asdict(DecodeLimits()),
        bytes={k:len(v.encode()) for k,v in [('pretty',pretty),('minified',minified),('compact',compact)]},
        characters={k:len(v) for k,v in [('pretty',pretty),('minified',minified),('compact',compact)]},
        counts=dict(chain_groups=len(public.get('driver_chain') or []),
            provenance_groups=len(public.get('bit_provenance') or []),
            chain_locations=sum(len(g.get('statement_evidence') or []) for g in public.get('driver_chain') or []),
            provenance_locations=sum(len(g.get('statement_evidence') or []) for g in public.get('bit_provenance') or []),
            bit_counts=[public.get(k) for k in ('resolved_bit_count','unresolved_bit_count','multi_driver_bit_count')]),
        traversal=public.get('traversal'),
        byte_distribution=dict(public_fields={k:len(canonical(v)) for k,v in public.items()},
            compact_result_fields={k:len(canonical(v)) for k,v in wire['result'].items()},
            tables={k:len(canonical(v)) for k,v in wire['evidence_tables'].items()},
            table_entries={k:len(v) for k,v in wire['evidence_tables'].items()}),
        timings=timing, memory=memory_rows, captures=paths,
        memory_basis='fresh exec per mode; input/model resident before tracing; VmHWM peak includes interpreter/imports/input; ru_maxrss may inherit pre-exec parent; no frontend in child')


async def capture_wire(source_path, target, directory, *, query_limits=None, repeats=1):
    """Actual local call_tool, fresh runtime + explicit recovery, then warm call.

    This is a server harness, not an AI client display/spill acceptance test.
    Source Graph is selected explicitly to avoid any licensed EDA work.
    """
    import server
    from config import SourceGraphExecutionConfig
    from src.connectivity_query import ConnectivityQueryEngine
    from src.source_graph_runtime import IsolatedSourceGraphProcessRunner, SourceGraphRuntime
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    top = target.split('.')[0]
    log = directory / 'compile.log'
    log.write_text(f"make: Entering directory '{directory}'\n"
                   f'verilator --cc --top-module {top} {source_path}\n')
    wave = directory / 'synthetic.vcd'
    width, coordinate = (32, '[1:32]') if top == 'bank' else (9, '[8:0]')
    wave.write_text(f'$timescale 1ps $end\n$scope module TOP $end\n$scope module {top} $end\n'
        f'$var wire {width} ! {target.split(".")[-1]} {coordinate} $end\n'
        '$upscope $end\n$upscope $end\n$enddefinitions $end\n#0\nb0 !\n#10\n')
    context = dict(compile_log=str(log), simulator='verilator', top_hint=top)
    args = dict(**context, compile_context=context, wave_path=str(wave), signal_path='TOP.' + target,
                recursive=True, include_dependencies=True)
    config = SourceGraphExecutionConfig(enabled=True, python_bin=sys.executable,
        frontend_version=importlib.metadata.version('pyslang'), timeout_sec=30)
    original_query = ConnectivityQueryEngine.query_driver
    def limited(self, signal_path, **kwargs):
        return original_query(self, signal_path, **{**kwargs, **(query_limits or {})})
    rows = []
    last = None
    with patch.object(server, 'get_source_graph_execution_config', lambda: config), \
         patch.object(ConnectivityQueryEngine, 'query_driver', limited):
        for repeat in range(repeats):
            server.reset_session_state()
            server._handle_store.invalidate()
            runtime = SourceGraphRuntime(IsolatedSourceGraphProcessRunner(
                python_executable=sys.executable, working_directory=ROOT, staging_directory=directory))
            observed_prepares = []
            prepare = runtime.prepare
            async def observed_prepare(*a, **kw):
                outcome = await prepare(*a, **kw)
                observed_prepares.append(outcome.metrics.to_dict())
                return outcome
            with (patch.object(server, 'get_source_graph_runtime', lambda _: runtime),
                  patch.object(runtime, 'prepare', observed_prepare)):
                for temperature in ('cold', 'warm'):
                    observed_prepares.clear()
                    captured = []
                    original = server._dispatch
                    async def capture(name, arguments):
                        result = await original(name, arguments)
                        if name == 'explain_signal_driver': captured.append(result)
                        return result
                    with patch.object(server, '_dispatch', capture):
                        start = time.perf_counter()
                        reply = await server.call_tool('explain_signal_driver', dict(args))
                        elapsed = (time.perf_counter()-start)*1000
                    assert len(captured) == 1 and isinstance(captured[0], ExplainDriverResult), reply
                    text = reply[0].text
                    wire = json.loads(text)
                    assert wire['format'] == 'traceweave.driver.compact.v1', wire
                    result = expand_compact_result(text)
                    assert canonical(result) == canonical(json.loads(server._serialize_result(captured[0])))
                    status = result['backend_status']
                    receipt = status['source_graph']
                    rows.append(dict(repeat=repeat, temperature=temperature, total_call_ms=elapsed,
                        actual_backend=status['actual_backend'], fallback_reason=status.get('fallback_reason'),
                        text_content_bytes=len(text.encode()), text_content_characters=len(text),
                        text_content_transport_bytes=len(reply[0].model_dump_json().encode()),
                        frontend_launch_count=sum(m['frontend_launch_count'] for m in observed_prepares),
                        public_frontend_launch_count=receipt['metrics'].get('frontend_launch_count'),
                        observed_prepare_count=len(observed_prepares),
                        artifact_attempt_count=receipt['artifact_attempt_count'],
                        prepare_ms=receipt['metrics'].get('prepare_total_wall_ms'),
                        query_ms=receipt['metrics'].get('query_wall_ms'),
                        worker_rss_peak_kib=receipt['metrics'].get('rss_peak_kib'),
                        hierarchy_recovery=result['hierarchy_recovery']['status'],
                        single_artifact_provenance=receipt.get('single_artifact_provenance')))
                    (directory / f'wire.{repeat}.{temperature}.compact.json').write_text(text)
                    last = captured[0]
    server.reset_session_state()
    return dict(client_status='not_verified', mode='in-process call_tool TextContent; isolated Slang worker',
        attempt_count_basis='actual post-dispatch backend_status.source_graph receipt',
        launch_count_basis='sum of observed runtime.prepare metrics; public one-shot route may omit this field',
        samples=rows), last


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory-mode', choices=['full','compact','decode'], required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--limits', default='{}')
    args = parser.parse_args()
    print(json.dumps(memory_sample(args.input, args.memory_mode, EncodeLimits(**json.loads(args.limits)))))
