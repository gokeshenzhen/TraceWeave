"""Evaluation-only MCP gate/logger. Both arms run the same accepted P0 code."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root, out, arm = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
sys.path.insert(0, str(root))
os.chdir(root)
os.environ['TRACEWEAVE_AUTO_KDB'] = '0'
os.environ['TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE'] = '0'
os.environ['TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION'] = '0'
import server
from mcp.types import TextContent

ALLOWED = {'get_diagnostic_snapshot','get_sim_paths','build_tb_hierarchy','scan_structural_risks',
    'parse_sim_log','sweep_handshakes','recommend_failure_debug_next_steps','get_waveform_summary',
    'search_signals','get_signal_at_time','get_signal_transitions','get_signals_around_time',
    'get_signals_by_cycle','explain_signal_driver','find_signal_loads','trace_signal_path',
    'lookup_tb_files','get_tb_file_detail','find_tb_instance','get_tb_subtree','get_error_context',
    'inspect_handshake','verify_window'}
original_list, original_call = server.list_tools, server.call_tool
count = 0


def record():
    modules = {n:dict(path=m.__file__,sha256=hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest())
        for n,m in list(sys.modules.items()) if (n.startswith('src.') or n in {'server','config'})
        and getattr(m,'__file__',None) and Path(m.__file__).is_file()}
    (out/'loaded.json').write_text(json.dumps(dict(pid=os.getpid(),executable=sys.executable,
        head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),modules=modules),indent=2)+'\n')


@server.app.list_tools()
async def list_tools():
    items = [t.model_copy(deep=True) for t in await original_list() if t.name in ALLOWED]
    for t in items:
        if arm == 'A' and t.name == 'explain_signal_driver':
            t.inputSchema['properties'].pop('include_dependencies', None)
    (out/'tools.json').write_text(json.dumps([t.model_dump(mode='json') for t in items],indent=2)+'\n')
    return items


@server.app.call_tool()
async def call_tool(name, arguments):
    global count
    count += 1
    index = count
    start=time.monotonic()
    if count > 40 or name not in ALLOWED:
        result=[TextContent(type='text',text=json.dumps({'error':'evaluation_tool_budget_exhausted'}))]
    elif arm == 'A' and arguments.get('include_dependencies'):
        result=[TextContent(type='text',text=json.dumps({'error':'unknown_parameter_include_dependencies'}))]
    else:
        result=await original_call(name,arguments)
    blocks=[r.model_dump(mode='json') if hasattr(r,'model_dump') else r for r in result]
    with (out/'calls.jsonl').open('a') as f:
        f.write(json.dumps(dict(index=index,tool=name,arguments=arguments,result=blocks,
            elapsed=time.monotonic()-start,response_bytes=len(json.dumps(blocks).encode())))+'\n')
    record()
    return result


record()
try:
    asyncio.run(server.main())
finally:
    record()
