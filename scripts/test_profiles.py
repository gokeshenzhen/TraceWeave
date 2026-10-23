"""Shared regression membership and read-only prerequisite checks.

Membership is per test (including its fixture closure), never inferred from
whether an SDK or a private waveform happens to exist on this machine.
"""
from __future__ import annotations

import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / 'tests/regression_catalog.json').read_text())
PROFILES = {'portable': {'portable'}, 'eda': {'portable', 'eda'},
            'stress': {'stress'}, 'all': {'portable', 'eda', 'stress'}}
EDA_FILES = {'test_fsdb_parser.py', 'test_fsdb_timescale.py', 'test_fsdb_widebus.py'}
EDA_FIXTURES = {'require_fsdb_runtime', 'native_reader_probe', 'reclaim_libraries',
                'same_tick_fsdb', 'real_kdbs', 'native_protocol'}
EDA_TESTS = {'test_native_scope_oracle', 'test_native_metadata_oracles',
             'test_real_metadata_matches_search',
             'test_real_fsdb_replacement_reopens_native_header_and_index',
             'test_native_projection_matches_fixture_stimulus'}


def membership(nodeid, *, fixtures=(), eda_marker=False):
    base = nodeid.split('[')[0]
    # Class entries intentionally cover each method, parameter entries each case.
    matches = [value for key, value in CATALOG.items()
               if base == key or base.startswith(key + '::')]
    if matches:
        return matches[0]
    if (Path(nodeid.split('::')[0]).name in EDA_FILES or
            EDA_FIXTURES.intersection(fixtures) or eda_marker or
            base.split('::')[-1] in EDA_TESTS):
        return 'eda'
    return 'portable'


def prerequisites(profile):
    """No subprocess, native import, license probe or fixture writes."""
    missing = []
    modules = ['pytest', 'mcp', 'pydantic', 'yaml']
    if profile != 'stress':
        modules += ['pyslang', 'pylibfst']
    for module in modules:
        if importlib.util.find_spec(module) is None:
            missing.append('python:' + module)
        elif module in ('pyslang', 'pylibfst'):
            required = {'pyslang': '11.0.0', 'pylibfst': '0.2.1'}[module]
            if metadata.version(module) != required:
                missing.append('python:'+module+'=='+required)
    if not shutil.which('g++'):
        missing.append('tool:g++')
    if profile != 'portable':
        verdi = Path(os.environ.get('VERDI_HOME', '/nonexistent'))
        for relative in ['share/FsdbReader/ffrAPI.h', 'share/FsdbReader/linux64/libnffr.so',
                         'share/FsdbWriter/ffwAPI.h', 'bin/vcd2fsdb']:
            if not (verdi / relative).is_file():
                missing.append('VERDI_HOME/' + relative)
        if profile != 'stress':
            if not shutil.which('vcs'):
                missing.append('tool:vcs')
            if not (verdi / 'share/NPI/python').is_dir():
                missing.append('VERDI_HOME/share/NPI/python')
    return missing


def allowed_skip(nodeid, reason):
    # Narrow platform limitations; never exempt SDK, software or fixture errors.
    rules = {
        'tests/test_fst_boundaries.py::test_permission_failure_is_distinct_from_corrupt_data': 'root can read mode-000 files',
        'tests/test_fsdb_optimization_bench.py::test_suite_affinity_is_inherited_by_worker_and_restored_on_failure': 'requires two permitted logical CPUs',
        'tests/test_fsdb_optimization_bench.py::test_guard_disables_thp_in_exec_child_without_changing_parent': 'requires Linux THP status reporting',
        'tests/test_worker_rss.py::test_exec_child_excludes_real_parent_high_water_mark': 'requires Linux /proc address-space high water marks',
        'tests/test_source_graph_session_runtime.py::test_real_released_peak_still_triggers_reported_session_limit[asyncio]': 'requires Linux address-space RSS observations',
    }
    return rules.get(nodeid) == reason


def versions():
    packages = {}
    for name in ('pytest', 'mcp', 'pydantic', 'PyYAML', 'pyslang', 'pylibfst'):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return dict(python=__import__('sys').version, packages=packages,
        tools={name: shutil.which(name) for name in ('g++', 'vcs', 'bsub')},
        installations={name: os.environ.get(name) for name in ('VERDI_HOME', 'VCS_HOME')},
        license_checked=False)
