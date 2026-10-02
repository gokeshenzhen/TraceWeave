"""Fresh-process functional probe of selected dynamic IR; not a model run."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ['TRACEWEAVE_AUTO_KDB'] = '0'
import server
from src.dynamic_observe import observe_step
from src.x_history_observe import bind_step_wave


async def main(project, output):
    cases = json.loads((Path(__file__).resolve().parents[1] / 'cases.json').read_text())
    a = cases['artifacts'][project]
    out = Path(output); out.mkdir(parents=True, exist_ok=True)
    async def call(name, **args):
        result = await server._dispatch(name, args)
        raw = result.model_dump(mode='json', exclude_none=True) if hasattr(result, 'model_dump') else result
        with (out / 'calls.jsonl').open('a') as f:
            f.write(json.dumps(dict(tool=name, arguments=args, result=raw))+'\n')
        return raw
    await call('get_diagnostic_snapshot')
    await call('get_sim_paths', verif_root=str(Path(a['compile_log']).parent),
               compile_log=a['compile_log'], sim_log=a['log_path'], wave_file=a['wave_path'])
    await asyncio.gather(*(call(n, compile_log=a['compile_log'], simulator=a['simulator'])
                          for n in ('build_tb_hierarchy', 'scan_structural_risks')))
    log = await call('parse_sim_log', log_path=a['log_path'], simulator=a['simulator'])
    if log.get('runtime_total_errors'):
        await call('sweep_handshakes', wave_path=a['wave_path'], start_time_ps=944095000,
                   end_time_ps=944535000, max_interfaces=256)
    hierarchy, snapshot = server._resolve_hierarchy_context(a['compile_log'], a['simulator'])
    config = server.get_source_graph_execution_config()
    bus = cases['scope_aliases']['system_bus']
    results = []
    for leaf in ('int_master_req[4].wdata[31:0]', 'dma_write_resp_o[0].gnt'):
        wave_signal = bus+'.'+leaf
        args = dict(signal_path=wave_signal, compile_log=a['compile_log'], wave_path=a['wave_path'])
        binding = await server._bind_wave_design_root(args, a['simulator'])
        signal = binding.to_design(wave_signal)
        plan = await server._run_in_cancellable_thread(lambda: server.build_source_graph_plan(
            compile_log=a['compile_log'], compile_result=hierarchy['compile_result'],
            hierarchy_result=hierarchy, hierarchy_snapshot_sha256=snapshot, operation='driver',
            signal_path=signal, top_hint=None, max_hops=10, frontend_version=config.frontend_version))
        outcome = await server.get_source_graph_runtime(config).prepare(plan.request, timeout_seconds=120)
        backend = server._source_graph_backend_for_plan(outcome.entry, plan)
        step = await server._run_in_cancellable_thread(lambda: backend.get_dynamic_step(signal))
        def observe():
            mapped = bind_step_wave(step, server._get_parser(a['wave_path']), binding=binding,
                                    engine=outcome.entry.query_engine)
            return mapped, observe_step(mapped, get_parser=server._get_parser,
                wave=a['wave_path'], time=944174999, history_start=944174990)
        mapped, observation = await server._run_in_wave_thread(a['wave_path'], observe)
        results.append(dict(signal=wave_signal, step=step, mapped=mapped, observation=observation,
                            artifact=outcome.entry.build_key.digest))
    (out/'selected.json').write_text(json.dumps(results, indent=2)+'\n')
    (out/'modules.json').write_text(json.dumps({n:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
        for n,m in list(sys.modules.items()) if n.startswith('src.') and getattr(m,'__file__',None)},indent=2)+'\n')
    print([(r['signal'],r['observation'].get('value'),r['observation'].get('gaps')) for r in results])


if __name__ == '__main__':
    cli=argparse.ArgumentParser();cli.add_argument('--project',required=True);cli.add_argument('--output',required=True)
    args=cli.parse_args();asyncio.run(main(args.project,args.output))
