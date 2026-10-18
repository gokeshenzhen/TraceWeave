"""Frozen-plan qualification in an owned, explicitly closed MCP process."""
import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import traceback

import anyio
from mcp import ClientSession, StdioServerParameters
import mcp.client.stdio as transport

ROOT = Path(os.environ['TRACEWEAVE_PROBE_WORKSPACE']).resolve()
WORK = Path(os.environ['TRACEWEAVE_PROBE_CHECKOUT'])
OUT = Path(os.environ['TRACEWEAVE_PROBE_EVIDENCE_DIR']).resolve() / 'mcp_preflight'
HEAD = os.environ['TRACEWEAVE_PROBE_HEAD']

def save(name, data):
    with (OUT / name).open('x') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def live_group(pgid):
    rows = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw = (path / 'stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                rows.append(int(path.name))
        except FileNotFoundError:
            pass
    return rows

async def main():
    assert subprocess.check_output(['git', '-C', str(WORK), 'rev-parse', 'HEAD'], text=True).strip() == HEAD
    assert not subprocess.check_output(['git', '-C', str(WORK), 'status', '--porcelain'], text=True).strip()
    plan = json.loads((OUT / 'MCP_PLAN.json').read_text())['plan']
    assert plan and len({row['id'] for row in plan}) == len(plan)
    process = None
    owned_pgid = None
    calls = []
    original_create = transport._create_platform_compatible_process
    original_terminate = transport._terminate_process_tree
    sdk_terminations = []
    started = time.monotonic()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    report = dict(status='failed', head=HEAD, started_utc=now(), calls=calls,
                  transport='owned_stdio', sdk_forced_terminations=sdk_terminations, oracle='P4_LOCAL_FROZEN_SCOPE.md')

    async def terminate(process):
        sdk_terminations.append(dict(pid=process.pid, observed_utc=now()))
        return await original_terminate(process)

    async def create(**kwargs):
        nonlocal process, owned_pgid
        process = await original_create(**kwargs)
        owned_pgid = process.pid  # SDK starts an independent POSIX session.
        assert os.getpgid(process.pid) == owned_pgid
        save('stdio_process_started.json', dict(pid=process.pid, pgid=owned_pgid,
            stat=Path(f'/proc/{process.pid}/stat').read_text(), argv=[kwargs['command'], *kwargs['args']],
            cwd=str(kwargs['cwd']), ownership='created by this exact stdio_client invocation'))
        return process

    async def call(session, row):
        t0 = time.monotonic()
        record = dict(request=row, started_utc=now())
        with anyio.fail_after(60):
            result = await session.call_tool(row['tool'], row['args'])
        record.update(finished_utc=now(), elapsed_ms=1000*(time.monotonic()-t0),
                      raw=result.model_dump(mode='json', exclude_none=True))
        save(row['id'] + '.json', record)  # Preserve raw BEFORE checking it.
        assert not result.isError
        assert len(result.content) == 1 and result.content[0].type == 'text'
        inner = json.loads(result.content[0].text)
        assert not inner.get('error') and not inner.get('error_code')
        calls.append(dict(id=row['id'], tool=row['tool'], elapsed_ms=record['elapsed_ms'],
                          raw_file=str(OUT / (row['id'] + '.json'))))
        return inner

    transport._create_platform_compatible_process = create
    transport._terminate_process_tree = terminate
    params = StdioServerParameters(command=str(ROOT / '.venv/bin/python'),
        args=['-B', str(WORK / 'server.py')], cwd=WORK,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'TRACEWEAVE_TELEMETRY': '0',
             'TRACEWEAVE_NPI_EXECUTION': 'local', 'TRACEWEAVE_AUTO_KDB': '0',
             'TRACEWEAVE_CONNECTIVITY_ROUTE': 'auto', 'TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE': '0',
             'TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION': '0',
             'TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY': 'off'})
    try:
        with (OUT / 'stdio_stderr.txt').open('x') as err:
            async with transport.stdio_client(params, errlog=err) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    with anyio.fail_after(60):
                        initialized = await session.initialize()
                    save('initialize.json', initialized.model_dump(mode='json'))
                    i = 0
                    while i < len(plan):
                        row = plan[i]
                        if row.get('parallel_group'):
                            pair = plan[i:i+2]
                            assert len(pair) == 2 and pair[0]['parallel_group'] == pair[1]['parallel_group']
                            await asyncio.gather(*(call(session, r) for r in pair))
                            i += 2
                        else:
                            await call(session, row)
                            i += 1
        assert not sdk_terminations, 'SDK forced termination is never qualification success'
        assert process is not None and process.returncode == 0
        assert not live_group(process.pid)
        report.update(status='RAW_PLAN_COMPLETE_REQUIRES_INDEPENDENT_ORACLE_REVIEW', call_count=len(calls))
    except BaseException:
        report['exception'] = traceback.format_exc()
        raise
    finally:
        transport._create_platform_compatible_process = original_create
        transport._terminate_process_tree = original_terminate
        try:
            if process is not None:
                # Also covers evidence I/O failure in create() before context setup.
                with anyio.CancelScope(shield=True):
                    if process.returncode is None or live_group(owned_pgid):
                        report['forced_cleanup'] = True
                        report['status'] = 'failed'
                        # A dead leader cannot be used with getpgid(); the saved
                        # group remains authoritative for any surviving children.
                        for sig in (signal.SIGTERM, signal.SIGKILL):
                            try:
                                os.killpg(owned_pgid, sig)
                            except ProcessLookupError:
                                pass
                            deadline = time.monotonic() + 3
                            while live_group(owned_pgid) and time.monotonic() < deadline:
                                await anyio.sleep(.1)
                            if not live_group(owned_pgid):
                                break
                        with anyio.fail_after(10):
                            await process.wait()
                save('stdio_process_finished.json', dict(pid=process.pid, exit_code=process.returncode,
                    live_group_members=live_group(owned_pgid), finished_utc=now()))
                assert not live_group(owned_pgid), 'Owned MCP group did not exit'
        except BaseException:
            report['cleanup_exception'] = traceback.format_exc()
            report['status'] = 'failed'
            raise
        finally:
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            report.update(finished_utc=now(), wall_seconds=time.monotonic()-started,
                cost=dict(child_user_cpu_seconds=after.ru_utime-usage.ru_utime,
                          child_system_cpu_seconds=after.ru_stime-usage.ru_stime,
                          child_peak_rss_kib=after.ru_maxrss))
            save('transport_execution.json', report)

if __name__ == '__main__':
    asyncio.run(main())
