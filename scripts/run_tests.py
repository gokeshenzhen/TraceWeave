#!/usr/bin/env python3
"""Select, prepare and run a reproducible TraceWeave regression; never install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_profiles import PROFILES, prerequisites, versions


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--profile', choices=tuple(PROFILES), default='portable')
    cli.add_argument('--list', action='store_true', help='collect a read-only selection plan')
    cli.add_argument('--check', action='store_true', help='read-only prerequisite check; no license checkout')
    cli.add_argument('--output', type=Path, help='new evidence directory (must not exist)')
    cli.add_argument('--cache-dir', type=Path, help='owned fixture cache, validated before reuse')
    cli.add_argument('--external-representative', type=Path, help='explicit read-only external project manifest')
    cli.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = cli.parse_args(argv)
    missing = prerequisites(args.profile)
    if args.check:
        print(json.dumps({'profile': args.profile, 'missing': missing,
                          'license_checked': False, 'status': 'missing' if missing else 'ready'}, indent=2))
        return 2 if missing else 0
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='traceweave-tests-'))
    if args.output:
        output.mkdir(parents=True, exist_ok=False)
    report = {'profile': args.profile, 'command': sys.argv, 'missing': missing,
              'versions': versions(),
              'layers': sorted(PROFILES[args.profile]), 'external_projects': str(args.external_representative) if args.external_representative else 'not_requested',
              'live_lsf_scheduler': 'not_requested', 'status': 'incomplete'}
    for name, command in [('head', ['git', 'rev-parse', 'HEAD']),
                          ('worktree_status', ['git', 'status', '--porcelain=v1'])]:
        report[name] = subprocess.check_output(command, cwd=ROOT, text=True).strip()
    start = time.monotonic()
    code = 2
    try:
        if missing and not args.list:
            report['failure_phase'] = 'prerequisites'
            print('Missing prerequisites: ' + ', '.join(missing), file=sys.stderr)
        else:
            command = [sys.executable, '-m', 'pytest', '-p', 'scripts.pytest_profiles',
                       '-c', str(ROOT / 'pytest.ini'), '--tw-profile', args.profile,
                       '--tw-report', str(output / 'pytest.json'), '--junitxml', str(output / 'junit.xml'),
                       '--basetemp', str(output / 'tmp')]
            if args.list:
                command += ['--collect-only', '-q']
            if args.cache_dir:
                command += ['--tw-cache-dir', str(args.cache_dir.resolve())]
            if args.external_representative:
                command += ['--tw-external-representative', str(args.external_representative.resolve())]
            extra = args.pytest_args
            if extra[:1] == ['--']:
                extra = extra[1:]
            # The runner owns completeness and report options.
            if any(x.startswith(('--tw-', '--junitxml', '--override-ini')) for x in extra):
                raise ValueError('runner report/selection options cannot be overridden')
            command += extra
            report['pytest_command'] = command
            report['partial_selection'] = bool(extra)
            env = dict(os.environ, TRACEWEAVE_TELEMETRY='0', TRACEWEAVE_NPI_EXECUTION='local')
            with (output / 'pytest.log').open('w') as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, start_new_session=True)
                try:
                    for line in process.stdout:
                        print(line, end='', flush=True)
                        log.write(line)
                    code = process.wait()
                except BaseException:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait()
                    raise
            result_path = output / 'pytest.json'
            if result_path.is_file():
                result = json.loads(result_path.read_text())
                report['pytest'] = result
                if not args.list and (not result['selected'] or result['status'] != 'passed'):
                    code = code or 1
                if result.get('preparation_error'):
                    report['failure_phase'] = 'fixtures'
                snapshots = output/'fixtures'
                snapshots.mkdir(exist_ok=True)
                for receipt in result.get('fixtures', []):
                    source = Path(receipt['manifest'])
                    target = snapshots/(receipt['name']+'-'+receipt['key'][:24])
                    if target.exists():
                        continue
                    target.mkdir()
                    shutil.copyfile(source, target/'manifest.json')
                    for log in source.parent.glob('*.log'):
                        shutil.copyfile(log, target/log.name)
            elif code == 0:
                code = 1
                report['failure_phase'] = 'missing_pytest_report'
            if code == 0:
                report['status'] = 'planned' if args.list else 'passed_selection' if extra else 'passed'
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        report.update(failure_phase='runner', error=str(exc))
        code = 2
    finally:
        report.update(exit_code=code, duration_sec=time.monotonic()-start,
                      children_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                      rss_measurement='maximum child high water mark; not sum of concurrent RSS')
        (output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(f"TraceWeave {args.profile}: {report['status']}; evidence: {output}")
    return code


if __name__ == '__main__':
    raise SystemExit(main())
