"""Read-only real-case functional replay; explicitly NOT a model A/B runner."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

STUDY = Path(__file__).resolve().parents[1]
ROOT = STUDY.parents[1]
CASES = json.loads((STUDY / 'cases.json').read_text())


async def run(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    a = CASES['artifacts'][args.project]
    env = dict(os.environ, TRACEWEAVE_AUTO_KDB='0')
    params = StdioServerParameters(command=str(ROOT / '.venv/bin/python'),
        args=[str(Path(__file__).with_name('server_entry.py')), str(ROOT), str(out / 'loaded.json')], env=env)
    with (out / 'server.stderr').open('w') as err:
        async with stdio_client(params, errlog=err) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=180)) as session:
                await session.initialize()
                schema = (await session.list_tools()).model_dump(mode='json')
                (out / 'tools.json').write_text(json.dumps(schema, indent=2) + '\n')

                async def call(name, **arguments):
                    started = time.monotonic()
                    result = await session.call_tool(name, arguments)
                    raw = result.model_dump(mode='json')
                    try:
                        payload = json.loads(result.content[0].text)
                    except (ValueError, AttributeError):
                        payload = {'error': str(result.content)}
                    row = dict(tool=name, arguments=arguments, result=payload,
                        elapsed=time.monotonic()-started, isError=result.isError,
                        response_bytes=len(json.dumps(raw).encode()))
                    with (out / 'calls.jsonl').open('a') as f:
                        f.write(json.dumps(row) + '\n')
                    print(name, round(row['elapsed'], 3),
                          {k: payload[k] for k in ('error', 'coverage_status', 'driver_status', 'num_cycles_returned') if k in payload}, flush=True)
                    return payload

                await call('get_diagnostic_snapshot')
                await call('get_sim_paths', verif_root=str(Path(a['compile_log']).parent),
                           compile_log=a['compile_log'], sim_log=a['log_path'], wave_file=a['wave_path'])
                await asyncio.gather(*[call(name, compile_log=a['compile_log'], simulator=a['simulator'])
                    for name in ('build_tb_hierarchy', 'scan_structural_risks')])
                log = await call('parse_sim_log', log_path=a['log_path'], simulator=a['simulator'])
                if log.get('runtime_total_errors', 0):
                    await call('sweep_handshakes', wave_path=a['wave_path'],
                        start_time_ps=944095000, end_time_ps=944535000, max_interfaces=256)
                await call('get_waveform_summary', wave_path=a['wave_path'])
                if args.project in ('x1', 'x2', 'x3') and 'window' in args.origins.split(','):
                    bus = CASES['scope_aliases']['system_bus']
                    dma = CASES['scope_aliases']['dma']
                    signals = [bus + '.' + leaf for leaf in (
                        'dma_write_req_i[0].wdata[31:0]', 'int_master_req[4].wdata[31:0]',
                        'int_master_resp[4].gnt', 'dma_write_resp_o[0].gnt')]
                    for phase in ('before', 'after'):
                        await call('get_signals_by_cycle', wave_path=a['wave_path'],
                            clock_path=dma + '.clk_cg', signal_paths=signals,
                            start_time_ps=944175000, num_cycles=1, sample_phase=phase,
                            cycle_index_origin='window')
                if args.project in ('ot', 'x1'):
                    scope = CASES['scope_aliases']['uart_tx' if args.project == 'ot' else 'dma']
                    clock = scope + ('.clk_i' if args.project == 'ot' else '.clk_cg')
                    leaves = (['tx_q', 'tx_d', 'tick_baud_q', 'bit_cnt_q[3:0]'] if args.project == 'ot'
                              else ['dma_state_q[31:0]', 'dma_state_d[31:0]', 'dma_done', 'data_out_req', 'data_out_gnt'])
                    for origin in args.origins.split(','):
                        for phase in ('before', 'after'):
                            kw = {} if origin == 'global' else dict(cycle_index_origin=origin)
                            await call('get_signals_by_cycle', wave_path=a['wave_path'], clock_path=clock,
                                signal_paths=[scope + '.' + leaf for leaf in leaves],
                                start_time_ps=7074050 if args.project == 'ot' else 944095000,
                                num_cycles=5 if args.project == 'ot' else 9, sample_phase=phase, **kw)
                            if args.project == 'x1' and origin == 'window':
                                await call('get_signals_by_cycle', wave_path=a['wave_path'], clock_path=clock,
                                    signal_paths=[scope + '.' + leaf for leaf in leaves[:3]],
                                    start_time_ps=944475000, end_time_ps=944535000, sample_phase=phase, **kw)
                    for at in ([7145477,7145479] if args.project == 'ot' else [944514999,944515001]):
                        for leaf in leaves:
                            await call('get_signal_at_time', wave_path=a['wave_path'],
                                       signal_path=scope + '.' + leaf, time_ps=at)
                if args.drivers:
                    paths = ([CASES['scope_aliases']['uart_tx'] + '.' + x for x in ('tx_q','tx_d')] if args.project == 'ot'
                        else [CASES['scope_aliases']['system_bus'] + '.' + x for x in
                              ('int_master_req[4].wdata[31:0]', 'dma_write_resp_o[0].gnt')]
                        + [CASES['scope_aliases']['dma'] + '.dma_state_q[31:0]'])
                    for path in paths:
                        await call('explain_signal_driver', signal_path=path, compile_log=a['compile_log'],
                                   simulator=a['simulator'], wave_path=a['wave_path'],
                                   **(dict(include_dependencies=True) if args.dependencies else {}))
                    if args.project in ('x1', 'x2', 'x3'):
                        bus = CASES['scope_aliases']['system_bus']
                        common = dict(wave_path=a['wave_path'], compile_log=a['compile_log'], simulator=a['simulator'])
                        await call('find_signal_loads', signal_path=bus + '.dma_write_req_i[0].wdata[31:0]', **common)
                        await call('trace_signal_path', from_signal=bus + '.dma_write_req_i[0].wdata[31:0]',
                                   to_signal=bus + '.int_master_req[4].wdata[31:0]', expand_assigns=True, **common)
                        await call('trace_x_source', signal_path=paths[-1], time_ps=944175000, **common)
    print('saved', out, flush=True)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--project', choices=tuple(CASES['artifacts']), required=True)
    cli.add_argument('--output', required=True)
    cli.add_argument('--origins', default='global')
    cli.add_argument('--drivers', action='store_true')
    cli.add_argument('--dependencies', action='store_true')
    asyncio.run(run(cli.parse_args()))
