"""Completeness failures must survive pytest's otherwise-successful exit."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from scripts import test_profiles

ROOT = Path(__file__).resolve().parents[1]


def invoke(tmp_path, source):
    test = tmp_path / 'test_sample.py'
    test.write_text(source)
    output = tmp_path / 'evidence'
    proc = subprocess.run([sys.executable, str(ROOT/'scripts/run_tests.py'),
                           '--output', str(output), '--', '-p', 'scripts.pytest_profiles',
                           str(test), '-q'], cwd=ROOT, capture_output=True, text=True, timeout=30)
    return proc, json.loads((output/'report.json').read_text())


@pytest.mark.parametrize('source,passed', [
    ('def test_ok(): assert True\n', True),
    ('def test_bad(): assert False\n', False),
    ('import pytest\ndef test_skip(): pytest.skip("missing required fixture")\n', False),
    ('import pytest\npytest.importorskip("traceweave_missing_dependency_for_test")\n', False),
    ('# empty selected suite\n', False),
    ('raise RuntimeError("broken collection")\n', False),
])
def test_runner_propagates_incomplete_suites(tmp_path, source, passed):
    proc, report = invoke(tmp_path, source)
    assert (proc.returncode == 0) is passed, proc.stdout + proc.stderr
    assert (report['status'] == 'passed_selection') is passed
    assert (tmp_path/'evidence/pytest.log').exists()


def test_selection_keeps_only_five_original_scale_cases():
    catalog = test_profiles.CATALOG
    assert sum(v == 'stress' for v in catalog.values()) == 5
    assert test_profiles.membership('tests/test_fsdb_point_window.py::test_native_point_resource_lifecycle[success]') == 'portable'
    assert test_profiles.membership('tests/test_fsdb_point_window.py::test_lazy_narrow_read_loads_only_a_fraction_of_multisession_trace') == 'stress'
    assert test_profiles.membership('tests/test_fsdb_point_window.py::test_real_reader_restores_an_existing_view') == 'eda'
    assert test_profiles.membership('tests/test_x.py::test_x', fixtures=['require_fsdb_runtime']) == 'eda'


def test_readonly_checks_separate_stress_from_vcs(monkeypatch):
    monkeypatch.setenv('VERDI_HOME', '/nonexistent/traceweave-sdk')
    monkeypatch.setattr(test_profiles.shutil, 'which', lambda name: None if name == 'vcs' else '/usr/bin/g++')
    assert 'tool:vcs' in test_profiles.prerequisites('eda')
    assert 'tool:vcs' not in test_profiles.prerequisites('stress')
    assert not any('VERDI_HOME' in s for s in test_profiles.prerequisites('portable'))
    original = test_profiles.importlib.util.find_spec
    monkeypatch.setattr(test_profiles.importlib.util, 'find_spec', lambda name: None if name == 'pylibfst' else original(name))
    assert 'python:pylibfst' in test_profiles.prerequisites('portable')
