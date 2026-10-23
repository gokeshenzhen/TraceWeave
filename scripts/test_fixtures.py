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
import threading
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
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.getsignal(signal.SIGTERM)
            def interrupted(signum, frame):
                raise KeyboardInterrupt('fixture preparation terminated')
            signal.signal(signal.SIGTERM, interrupted)
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
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
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
        cache_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(cache_fd, fcntl.LOCK_EX)
            if not marker.exists():
                if any(self.root.iterdir()):
                    raise ValueError('refusing nonempty cache without TraceWeave ownership marker')
                with marker.open('x') as f:
                    f.write(OWNER)
            if marker.read_text() != OWNER:
                raise ValueError('invalid fixture cache owner')
        finally:
            os.close(cache_fd)
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

    def legacy_parser(self):
        converter = Path(os.environ['VERDI_HOME'])/'bin/vcd2fsdb'
        source = ROOT/'tests/fixtures/parser_legacy.vcd'
        def build(path):
            return run([converter, source, '-seq_num', '-o', path/'parser.fsdb'], path, timeout=30)
        return self.get('legacy-parser', identity([source, converter], version=1), build) / 'parser.fsdb'

    def shape_probe(self):
        sdk = Path(os.environ['VERDI_HOME'])/'share/FsdbReader'
        source = ROOT/'scripts/fsdb_optimization_probe.cpp'
        native = ROOT/'native/fsdb'
        inputs = identity([source, native/'fsdb_wrapper.cpp'] + sorted(native.glob('*.h')) +
                          [sdk/'ffrAPI.h', sdk/'linux64/libnffr.so', shutil.which('g++')], version=1)
        def build(path):
            include = path/'include.cpp'
            include.write_text('static unsigned long long bench_reclaims=0, bench_reclaim_ns=0;\n'
                               '#include "'+str(native/'fsdb_wrapper.cpp')+'"\n')
            return run(['g++', '-O0', '-std=c++11', '-I'+str(sdk), '-I'+str(native),
                        '-DTRACEWEAVE_BENCH_WRAPPER="'+str(include)+'"', source,
                        '-L'+str(sdk/'linux64'), '-lnffr', '-lnsys', '-lz', '-ldl',
                        '-Wl,-rpath,'+str(sdk/'linux64'), '-o', path/'probe'], path)
        return self.get('shape-probe', inputs, build)/'probe'

    def inspect(self, path, signal_path, records, *, multisession=False, output_dir=None):
        probe = self.shape_probe()
        output_dir = output_dir or path.parent
        result = run([probe, path, signal_path, '0', '-1', str(records), '1', 'count'],
                     output_dir, timeout=180, label='shape')
        rows = [json.loads(line) for line in (output_dir/'shape.log').read_text().splitlines()
                if line.startswith('{')]
        metadata = next(r for r in rows if r['phase'] == 'metadata')
        count = next(r for r in rows if r['phase'] == 'count')
        if multisession and metadata['sessions'] < 2:
            raise RuntimeError('fixture needs multiple physical FFR sessions')
        return dict(shape=metadata, count=count, validation=result)

    def activity(self, steps):
        from scripts.benchmark_waveform_reads import generate
        converter = Path(os.environ['VERDI_HOME'])/'bin/vcd2fsdb'
        inputs = identity([ROOT/'scripts/benchmark_waveform_reads.py', converter],
                          steps=steps, writer_memory_mib=4, seq_num=True)
        def build(path):
            start = time.monotonic()
            generate(path/'activity.vcd', steps)
            conversion = run([converter, path/'activity.vcd', '-seq_num', '-o', path/'activity.fsdb'], path,
                             env=dict(os.environ, FSDB_ENV_WRITER_MEM_LIMIT='4'), timeout=600)
            info = self.inspect(path/'activity.fsdb', 'top.data', steps+1, multisession=True)
            from src.fsdb_parser import FSDBParser
            parser = FSDBParser(str(path/'activity.fsdb'))
            try:
                for step in (0, steps//2, steps-1, steps):
                    got = parser.get_value_at_time('top.data', step*10)['value']['bin']
                    if got != f'{step*2654435761 & 0xffffffff:032b}':
                        raise RuntimeError('activity mathematical value oracle failed')
                if parser.get_summary()['scale_fs_per_tick'] != 1000:
                    raise RuntimeError('activity scale oracle failed')
            finally:
                parser.close()
            # Retain the reproducible source, but avoid caching a second huge copy.
            vcd_bytes = (path/'activity.vcd').stat().st_size
            (path/'activity.vcd').unlink()
            return dict(steps=steps, data_records=steps+1, clock_posedges=steps//2,
                        signals=4, generated_vcd_bytes=vcd_bytes,
                        elapsed_sec=time.monotonic()-start, conversion=conversion, **info)
        return self.get('activity-'+str(steps), inputs, build)/'activity.fsdb'

    def writer_fixture(self, name):
        specifications = {'wide_256_reclaim': ('wide256', 600000, 0, 128),
                          'protocol_small': ('protocol', 256, 32, 128),
                          'protocol_complete': ('protocol', 32768, 4096, 128),
                          'protocol_large': ('protocol', 131072, 4096, 128)}
        mode, steps, sync, memory = specifications[name]
        sdk = Path(os.environ['VERDI_HOME'])/'share/FsdbWriter'
        source = ROOT/'scripts/fsdb_optimization_fixture.cpp'
        inputs = identity([source, sdk/'ffwAPI.h', sdk/'linux64/libnffw.so', shutil.which('g++')],
                          mode=mode, steps=steps, sync=sync, memory=memory)
        def build(path):
            compilation = run(['g++', '-O2', '-std=c++11', '-I'+str(sdk), source,
                               '-L'+str(sdk/'linux64'), '-lnffw', '-ldl',
                               '-Wl,-rpath,'+str(sdk/'linux64'), '-o', path/'writer'], path)
            generation = run([path/'writer', path/'wave.fsdb', mode, steps, sync, memory], path, timeout=300,
                             label='generation')
            signal_path = 'top.data[255:0]' if mode == 'wide256' else 'top.fast.clk'
            info = self.inspect(path/'wave.fsdb', signal_path, steps+1 if mode == 'wide256' else steps*2+1,
                                multisession=mode == 'wide256')
            return dict(mode=mode, steps=steps, compilation=compilation, generation=generation, **info)
        return self.get(name, inputs, build)/'wave.fsdb'

    def wave(self, variable, generate, *, signal_path=None, records=None, scale_fs=1000,
             multisession=False, sample=None):
        """Legacy path overrides remain read-only, with the same semantic contract."""
        raw = os.environ.get(variable)
        if not raw:
            return generate()
        external = Path(raw).resolve(strict=True)
        if not external.is_file():
            raise ValueError(variable+' must be a waveform file')
        def validate(path):
            metadata = self.inspect(external, signal_path, records, multisession=multisession,
                                    output_dir=path) if signal_path else {}
            from src.fsdb_parser import FSDBParser
            parser = FSDBParser(str(external))
            try:
                if parser.get_summary()['scale_fs_per_tick'] != scale_fs:
                    raise RuntimeError(variable+' timescale mismatch')
                if sample:
                    sample(parser)
            finally:
                parser.close()
            return dict(external=str(external), sha256=sha(external), **metadata)
        self.get('override-'+variable.lower(), identity([external], variable=variable), validate)
        return external

    def vcs_design(self, name, sources, top, *, simulate=False, kdb=True):
        vcs = Path(shutil.which('vcs'))
        inputs = identity([vcs], sources=sources, top=top, simulate=simulate, kdb=kdb,
                          cache_root=str(self.root.resolve()), vcs_home=os.environ.get('VCS_HOME'),
                          verdi_home=os.environ.get('VERDI_HOME'), flags='full64+sverilog+1ns/1ps+debug_access+all')
        def build(path):
            files = []
            for filename, text in sources.items():
                source = path/filename
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(text); files.append(source)
            command = [vcs, '-full64', '-sverilog', '-timescale=1ns/1ps', '-debug_access+all']
            if kdb:
                command += ['-kdb']
            command += files + ['-top', top, '-o', 'simv', '-l', 'compile.log']
            compilation = run(command, path, timeout=90)
            if kdb and not (path/'simv.daidir/kdb.elab++').is_dir():
                raise RuntimeError('VCS did not produce the required KDB')
            simulation = run([path/'simv', '-no_save', '-l', 'sim.log'], path, timeout=30,
                             label='simulation') if simulate else None
            return dict(compilation=compilation, simulation=simulation,
                        actual_simulation=simulate, live_scheduler=False)
        return self.get(name, inputs, build)


def prepare_for_tests(selected, cache_dir=None):
    """Prepare only selected capabilities; further input families are demand loaded."""
    if not any(row['layer'] in ('eda', 'stress') for row in selected):
        return []
    global _current_cache
    cache = _current_cache = FixtureCache(cache_dir)
    wrapper = cache.wrapper()
    os.environ['TW_COUNT_INDEX_WRAPPER'] = str(wrapper)
    from src import fsdb_parser
    fsdb_parser._WRAPPER_SO = str(wrapper)
    if any('test_fsdb_parser.py::' in r['nodeid'] for r in selected):
        os.environ['TW_LEGACY_PARSER_FSDB'] = str(cache.legacy_parser())
    fixtures = {name for row in selected for name in row.get('fixtures', [])}
    if 'same_tick_fsdb' in fixtures:
        os.environ['TW_SAME_TICK_FSDB'] = str(cache.same_tick())
    million = ('test_multiblock_points_preserve_following_reads', 'test_real_reader_restores_an_existing_view',
               'test_interleaved_reads_release_selection_and_residency',
               'test_lazy_windows_match_generator_across_sessions_in_both_directions',
               'test_lazy_around_keeps_full_requested_history_and_center',
               'test_lazy_event_group_cancellation_releases_loaded_and_untouched_signals')
    if any(row['nodeid'].split('::')[-1].split('[')[0] in million for row in selected):
        os.environ['TW_POINT_READ_FSDB'] = str(cache.wave('TW_POINT_READ_FSDB', lambda: cache.activity(1_000_000),
            signal_path='top.data', records=1_000_001, multisession=True))
    if any(row['layer'] == 'stress' for row in selected):
        os.environ['TW_LAZY_LARGE_FSDB'] = str(cache.wave('TW_LAZY_LARGE_FSDB', lambda: cache.activity(10_000_000),
            signal_path='top.data', records=10_000_001, multisession=True))
    for var, name, needed in [
        ('TW_SESSION_RECLAIM_FSDB', 'wide_256_reclaim', 'test_real_multisession_wide_counter_count_and_values'),
        ('TW_P2B_PROTOCOL_FSDB', 'protocol_small', 'native_protocol'),
        ('TW_P2B_COMPLETE_FSDB', 'protocol_complete', 'TW_P2B_COMPLETE_FSDB'),
        ('TW_P2B_LARGE_FSDB', 'protocol_large', 'TW_P2B_LARGE_FSDB')]:
        if needed in fixtures or any(needed in row['nodeid'] for row in selected):
            os.environ[var] = str(cache.wave(var, lambda: cache.writer_fixture(name),
                signal_path='top.data[255:0]' if name == 'wide_256_reclaim' else 'top.fast.clk',
                records=600001 if name == 'wide_256_reclaim' else
                    {'protocol_small': 513, 'protocol_complete': 65537, 'protocol_large': 262145}[name],
                multisession=name == 'wide_256_reclaim'))
    if any('real_verdi_runtime' in r.get('fixtures', []) for r in selected):
        sources = {'native_connections.sv': (ROOT/'tests/fixtures/native_connections.sv').read_text()}
        path = cache.vcs_design('native-connections', sources, 'top_tb')
        os.environ['TW_NPI_COMPILE_LOG'] = str(path/'compile.log')
    if any('test_deep_x_npi_integration.py::' in r['nodeid'] for r in selected):
        base = ROOT/'tests/fixtures/deep_x_npi'
        sources = {name: (base/name).read_text() for name in ('rtl/deep_uart_x.sv', 'tb/deep_x_tb.sv')}
        path = cache.vcs_design('deep-x', sources, 'uart_deep_x_tb', simulate=True)
        if not (path/'deep_x.fsdb').is_file():
            raise RuntimeError('deep-X simulation did not produce its FSDB')
        os.environ['TW_DEEP_X_WORK'] = str(path)
    return cache.receipts


_current_cache = None


def current_cache():
    if _current_cache is None:
        raise RuntimeError('real EDA fixture requested outside an explicit EDA profile')
    return _current_cache
