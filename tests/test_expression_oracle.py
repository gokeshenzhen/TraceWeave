"""Independent simulator checks for the versioned expression vectors."""
import os
import shutil
import subprocess

import pytest

from tests.expression_oracle import cases, check_output, render


@pytest.mark.eda_integration
def test_expression_vectors_with_vcs(tmp_path):
    if os.environ.get('TRACEWEAVE_TEST_EXPRESSION_ORACLE') != '1':
        pytest.skip('opt in with TRACEWEAVE_TEST_EXPRESSION_ORACLE=1')
    assert shutil.which('vcs'), 'VCS required for explicitly requested oracle'
    source = tmp_path / 'oracle.sv'
    source.write_text(render())
    build = subprocess.run(['vcs', '-full64', '-sverilog', '-timescale=1ns/1ps',
                            str(source), '-top', 'expression_oracle', '-o', 'simv'],
                           cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run([str(tmp_path / 'simv'), '-no_save'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    check_output(result.stdout)


def test_oracle_vectors_have_identified_operator_coverage():
    vectors = cases()
    assert len({c['id'] for c in vectors}) == len(vectors)
    assert {group for c in vectors for group in c['coverage']} == {
        f'E{i:02}' for i in range(1, 15)}
    for case in vectors:
        assert len(case['expected']['bin']) == case['expected']['width']
        assert set(case['expected']['bin']) <= set('01xz')
