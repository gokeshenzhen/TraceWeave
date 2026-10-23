"""Owned, bounded regression fixture preparation. No installs or user builds.

A manifest is the publication boundary. Builds use stable absolute paths (KDB
source locations survive reuse); consumers only see validated manifests under
an exclusive per-key lock. Unpublished directories are never cache hits.
"""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'traceweave-regression-fixtures-v1\n'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def identity(paths, **parameters):
    return dict(parameters=parameters, inputs={str(Path(p).resolve()): sha(p) for p in paths})


def run(command, directory, *, timeout=120, env=None, label='build'):
    """Keep diagnostics and terminate the entire owned process group on failure."""
    command = list(map(str, command))
    start = time.monotonic()
    log = Path(directory) / (label + '.log')
    with log.open('w') as output:
        output.write(json.dumps(command) + '\n'); output.flush()
        proc = subprocess.Popen(command, cwd=directory, env=env, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
            if code:
                raise RuntimeError(f'{label} exited {code}; see {log}')
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            raise
    return dict(command=command, elapsed_sec=time.monotonic()-start,
                children_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                rss_measurement='process cumulative child high water mark')


class FixtureCache:
    def __init__(self, root=None):
        self.root = Path(root).absolute() if root else Path(tempfile.mkdtemp(prefix='traceweave-fixtures-'))
        if self.root.is_symlink():
            raise ValueError('fixture cache root must not be a symlink')
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / '.traceweave-fixtures'
        if not marker.exists():
            if any(self.root.iterdir()):
                raise ValueError('refusing nonempty cache without TraceWeave ownership marker')
            # Competing first consumers may both observe an empty directory.
            try:
                with marker.open('x') as f:
                    f.write(OWNER)
            except FileExistsError:
                pass
        if marker.read_text() != OWNER:
            raise ValueError('invalid fixture cache owner')
        self.receipts = []

    def get(self, name, inputs, build, validate=lambda path, meta: None):
        key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        directory = self.root / (name + '-' + key[:24])
        with (self.root / (key + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if directory.is_symlink():
                raise ValueError('fixture entry must not be a symlink')
            manifest = directory / 'manifest.json'
            reused = False
            if manifest.is_file():
                try:
                    meta = json.loads(manifest.read_text())
                    reused = (meta['identity'] == inputs and bool(meta['artifacts']) and
                              all((directory / p).is_file() and sha(directory / p) == h
                                  for p, h in meta['artifacts'].items()
                                  if not Path(p).is_absolute() and '..' not in Path(p).parts) and
                              all(not Path(p).is_absolute() and '..' not in Path(p).parts for p in meta['artifacts']))
                    if reused:
                        validate(directory, meta['metadata'])
                except (OSError, ValueError, KeyError, RuntimeError):
                    reused = False
            if not reused:
                # Only this exact owned key is touched. Preserve failed build logs.
                if directory.exists():
                    directory.rename(self.root / (directory.name + '-invalid-' + str(time.time_ns())))
                directory.mkdir()
                try:
                    metadata = build(directory)
                    validate(directory, metadata)
                    artifacts = {str(p.relative_to(directory)): sha(p) for p in directory.rglob('*')
                                 if p.is_file() and not p.is_symlink()}
                    if not artifacts:
                        raise RuntimeError('fixture builder produced no artifacts')
                    meta = dict(identity=inputs, artifacts=artifacts, metadata=metadata)
                    pending = directory / 'manifest.pending'
                    pending.write_text(json.dumps(meta, indent=2)+'\n')
                    pending.replace(manifest)
                except BaseException:
                    # Keep unpublished diagnostics for inspection, never as a hit.
                    raise
            receipt = dict(name=name, key=key, path=str(directory), reused=reused,
                           manifest=str(manifest), metadata=meta['metadata'],
                           disk_bytes=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file()))
            self.receipts.append(receipt)
            return directory

    def wrapper(self):
        sdk = Path(os.environ['VERDI_HOME']) / 'share/FsdbReader'
        sources = sorted((ROOT/'native/fsdb').glob('*.h')) + [ROOT/'native/fsdb/fsdb_wrapper.cpp']
        inputs = identity(sources + [sdk/'ffrAPI.h', sdk/'linux64/libnffr.so', sdk/'linux64/libnsys.so', shutil.which('g++')],
                          abi=1, flags=['-shared', '-fPIC', '-std=c++11'])
        def build(path):
            return run(['g++', '-shared', '-fPIC', '-std=c++11', '-I'+str(sdk),
                        ROOT/'native/fsdb/fsdb_wrapper.cpp', '-o', path/'wrapper.so',
                        '-L'+str(sdk/'linux64'), '-lnffr', '-lnsys', '-lz',
                        '-Wl,-rpath,'+str(sdk/'linux64')], path)
        def validate(path, meta):
            lib = ctypes.CDLL(str(path/'wrapper.so'))
            for name in ('fsdb_event_page_version',
                         'fsdb_metadata_version', 'fsdb_scope_page_version'):
                if getattr(lib, name)() != 1:
                    raise RuntimeError('wrapper ABI mismatch: '+name)
            getattr(lib, 'fsdb_event_count_v1')
        return self.get('wrapper', inputs, build, validate) / 'wrapper.so'

    def same_tick(self):
        converter = Path(os.environ['VERDI_HOME'])/'bin/vcd2fsdb'
        def build(path):
            (path/'events.vcd').write_text(
                '$timescale 100fs $end\n$scope module top $end\n'
                '$var wire 1 ! data $end\n$var wire 1 @ sparse $end\n'
                '$upscope $end\n$enddefinitions $end\n'
                '#0\n0!\n0@\n#1\n1!\n#2\nx!\n#9\nz!\n'
                '#10\n0!\n1!\nx!\n0!\n#11\n1!\n'
                '#20\nz!\n1@\n#29\n0!\n#30\n1!\n#40\n')
            result = run([converter, path/'events.vcd', '-seq_num', '-o', path/'events.fsdb'], path,
                         env=dict(os.environ, FSDB_ENV_MAX_GLITCH_NUM='0'), timeout=30)
            return dict(scale_fs=100, data_records=12, same_tick_records=4, **result)
        def validate(path, meta):
            from src.fsdb_parser import FSDBParser
            expected = [(0, '0'), (1, '1'), (2, 'x'), (9, 'z'),
                        (10, '0'), (10, '1'), (10, 'x'), (10, '0'),
                        (11, '1'), (20, 'z'), (29, '0'), (30, '1')]
            parser = FSDBParser(str(path/'events.fsdb'))
            try:
                if parser.get_summary()['scale_fs_per_tick'] != 100:
                    raise RuntimeError('same-tick timescale oracle failed')
                with parser._event_transition_group(['top.data'], 0, 4):
                    with parser._event_pages('top.data', 0, 4, max_events=3) as reader:
                        rows = []
                        for _ in range(20):
                            page = reader.read_page()
                            rows.extend((e.time_fs, e.value) for e in page.events)
                            if page.complete:
                                break
                        if rows != [(t*100, v) for t, v in expected]:
                            raise RuntimeError('same-tick raw sequence/XZ oracle failed')
            finally:
                parser.close()
        return self.get('same-tick', identity([__file__, converter], version=1), build, validate) / 'events.fsdb'


def prepare_for_tests(selected, cache_dir=None):
    """Prepare only selected capabilities; further input families are demand loaded."""
    if not any(row['layer'] in ('eda', 'stress') for row in selected):
        return []
    cache = FixtureCache(cache_dir)
    wrapper = cache.wrapper()
    os.environ['TW_COUNT_INDEX_WRAPPER'] = str(wrapper)
    from src import fsdb_parser
    fsdb_parser._WRAPPER_SO = str(wrapper)
    return cache.receipts
