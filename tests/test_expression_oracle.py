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
    from scripts.test_fixtures import current_cache
    path = current_cache().vcs_design('expression-vectors', {'oracle.sv': render()},
                                      'expression_oracle', simulate=True, kdb=False)
    check_output((path/'simulation.log').read_text())


def test_oracle_vectors_have_identified_operator_coverage():
    vectors = cases()
    assert len({c['id'] for c in vectors}) == len(vectors)
    assert {group for c in vectors for group in c['coverage']} == {
        f'E{i:02}' for i in range(1, 15)}
    for case in vectors:
        assert len(case['expected']['bin']) == case['expected']['width']
        assert set(case['expected']['bin']) <= set('01xz')
