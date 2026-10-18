"""One frozen synchronous dual-design boundary or trace stage. Watchdog is safety only, never a pass."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import time
import traceback

ROOT = Path(os.environ['TRACEWEAVE_PROBE_WORKSPACE']).resolve()
OUT = Path(os.environ['TRACEWEAVE_PROBE_EVIDENCE_DIR']).resolve()
ENV = dict(PYTHONDONTWRITEBYTECODE='1', TRACEWEAVE_NPI_EXECUTION='local',
    TRACEWEAVE_CONNECTIVITY_ROUTE='auto', TRACEWEAVE_AUTO_KDB='0',
    TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY='off', TRACEWEAVE_TELEMETRY='0',
    TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE='0', TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION='0')

def save(path, value):
    with path.open('x') as f:
        json.dump(value, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()

def fact(path):
    p = Path(path); s = p.stat()
    return dict(path=str(p), canonical=str(p.resolve()), sha256=sha(p), dev=s.st_dev,
        ino=s.st_ino, size=s.st_size, mode=s.st_mode, mtime_ns=s.st_mtime_ns,
        ctime_ns=s.st_ctime_ns, atime_ns=s.st_atime_ns)

def inputs(inventory):
    rows = []
    for case in inventory['cases'].values():
        for expected in [case[k] for k in ('compile_log','source','wave','sim_log')] + case['kdb']['all_files']:
            row = fact(expected['path']); row['expected_sha256'] = expected['sha256']
            rows.append(row)
    return rows

def file_sets(inventory):
    return {side: sorted(str(p.relative_to(Path(case['compile_log']['path']).parent))
        for p in Path(case['compile_log']['path']).parent.rglob('*') if p.is_file())
        for side, case in inventory['cases'].items()}

def group_live(pgid):
    rows = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            raw = (p / 'stat').read_text(); fields = raw[raw.rfind(')')+2:].split()
            if int(fields[2]) == pgid and fields[0] != 'Z': rows.append(int(p.name))
        except FileNotFoundError: pass
    return rows

def strict(path):
    def pairs(items):
        row = {}
        for k,v in items:
            if k in row: raise ValueError('duplicate key ' + k)
            row[k] = v
        return row
    def constant(value): raise ValueError('nonfinite ' + value)
    return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=constant)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--head',required=True);p.add_argument('--checkout',required=True);p.add_argument('--attempt',type=int,default=1)
    p.add_argument('--mode', choices=('sync-boundary', 'sync-trace'), required=True)
    p.add_argument('--boundary-evidence')
    a=p.parse_args();work=Path(a.checkout).resolve()
    assert subprocess.check_output(['git','-C',str(work),'rev-parse','HEAD'],text=True).strip()==a.head
    assert not subprocess.check_output(['git','-C',str(work),'status','--porcelain'],text=True).strip()
    row=OUT/f'row_{a.mode}_{a.head[:7]}_attempt{a.attempt:02d}';row.mkdir(exist_ok=False)
    inv=strict(OUT/'INPUT_INVENTORY.json');before=inputs(inv);sets=file_sets(inv)
    save(row/'inputs_before.json',before);save(row/'file_sets_before.json',sets)
    expected_inputs=len(before)
    assert expected_inputs >= 8 and all(x['sha256']==x['expected_sha256'] for x in before)
    historical=ROOT/'docs/2026-10-16-kdb-npi-roadmap/p4_s5_evidence/isolated/row_a_6be81cc_attempt01/native_assets.json'
    assets=[fact(r['path']) for r in strict(historical)['files']]
    assets.extend(fact(p) for p in sorted((ROOT/'tests/fixtures/dual_sync_npi').rglob('*')) if p.is_file())
    save(row/'native_assets.json',assets)
    argv=[str(ROOT/'.venv/bin/python'),'-B',str(work/'scripts/probe_dual_prepared_context.py'),
        '--repo',str(work),'--expected-head',a.head,'--mode',a.mode,
        '--inventory',str(OUT/'INPUT_INVENTORY.json'),
        '--mcp-evidence',str(OUT/'mcp_preflight/execution_report.json')]
    if a.boundary_evidence: argv.extend(['--boundary-evidence',a.boundary_evidence])
    started=time.monotonic();usage=resource.getrusage(resource.RUSAGE_CHILDREN);child=None;pgid=None
    receipt=dict(status='started',head=a.head,kind=a.mode,argv=argv,cwd=str(work),env=ENV,
        inherited_eda_env={k:os.environ.get(k) for k in ('VCS_HOME','VERDI_HOME','NOVAS_HOME','LD_LIBRARY_PATH','LM_LICENSE_FILE','SNPSLMD_LICENSE_FILE')},
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),watchdog_sec=180,watchdog_triggered=False)
    save(row/'provenance.started.json',receipt)
    failure=None
    try:
        with (row/'stdout.json').open('xb') as stdout,(row/'stderr.txt').open('xb') as stderr:
            child=subprocess.Popen(argv,cwd=work,env={**os.environ,**ENV},stdout=stdout,stderr=stderr,start_new_session=True)
            pgid=child.pid
            save(row/'process_started.json',dict(pid=child.pid,pgid=pgid,stat=Path(f'/proc/{child.pid}/stat').read_text()))
            try: child.wait(timeout=180)
            except subprocess.TimeoutExpired:
                receipt['watchdog_triggered']=True
                raise
        receipt['child_exit']=child.returncode
        result=strict(row/'stdout.json')
        assert child.returncode==0 and result['status']==('dual_sync_boundary_verified' if a.mode=='sync-boundary' else 'dual_sync_trace_verified')
        assert not result.get('error') and not result.get('denied') and not result.get('cleanup_errors')
        assert result['post_asyncio_threads']==['MainThread'] and result['native_observers_restored']
        assert result['sync_observers_restored'] and not result.get('lifecycle_worker_undrained')
        assert result['final_actual_file_count']==expected_inputs
        receipt['status']='ROW_COMPLETED_STOP_FOR_A_REVIEW'
    except BaseException:
        failure=traceback.format_exc();receipt.update(status='failed',failure=failure)
    finally:
        try:
            if child is not None:
                if child.poll() is None or group_live(pgid):
                    receipt['forced_cleanup']=True
                    receipt['status']='failed'
                    failure=failure or 'Owned process group required forced cleanup; never acceptance'
                    receipt['failure']=failure
                    for sig in (signal.SIGTERM,signal.SIGKILL):
                        try:os.killpg(pgid,sig)
                        except ProcessLookupError:pass
                        try:child.wait(timeout=3)
                        except subprocess.TimeoutExpired:pass
                        deadline=time.monotonic()+3
                        while group_live(pgid) and time.monotonic()<deadline:time.sleep(.05)
                        if not group_live(pgid):break
                receipt.update(child_exit=child.poll(),live_group_members=group_live(pgid))
                assert child.poll() is not None and not group_live(pgid)
            after=inputs(inv);aftersets=file_sets(inv)
            save(row/'inputs_after.json',after);save(row/'file_sets_after.json',aftersets)
            assert sets==aftersets
            keys=('path','canonical','sha256','dev','ino','size','mode','mtime_ns','ctime_ns')
            assert [[x[k] for k in keys] for x in before]==[[x[k] for k in keys] for x in after]
            assert all(sha(r['path'])==r['sha256'] for r in assets)
            receipt['input_integrity']=dict(files=expected_inputs,hash_stat_and_sets_unchanged=True,atime_excluded=True,assets_unchanged=True)
        except BaseException:
            receipt.update(status='failed',cleanup_failure=traceback.format_exc());failure=failure or receipt['cleanup_failure']
        finally:
            afterusage=resource.getrusage(resource.RUSAGE_CHILDREN)
            receipt.update(finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),wall_seconds=time.monotonic()-started,
                cost=dict(child_user_cpu_seconds=afterusage.ru_utime-usage.ru_utime,child_system_cpu_seconds=afterusage.ru_stime-usage.ru_stime,
                          child_peak_rss_kib=afterusage.ru_maxrss),next_row_started=False)
            save(row/'provenance.finished.json',receipt)
            lines=[sha(p)+'  '+str(p.relative_to(row)) for p in sorted(row.rglob('*')) if p.is_file()]
            (row/'SHA256SUMS').write_text('\n'.join(lines)+'\n')
    print(json.dumps(dict(row=str(row),**receipt),indent=2))
    return 1 if failure or receipt['status']!='ROW_COMPLETED_STOP_FOR_A_REVIEW' else 0

if __name__=='__main__':raise SystemExit(main())
