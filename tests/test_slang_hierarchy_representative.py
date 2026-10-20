"""Opt-in counted compile-only smoke at the preselected OpenTitan UART target.

This file is enabled only in the frozen V3 representative stage. It performs
no simulation, build, installation or waveform/log analysis. Historical
constructed wave/log paths remain attribution, not evidence for this run.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
from src import source_graph_production
from tests.test_prepared_source_connectivity import isolation, anyio_backend, public

SELECTION = json.loads((Path(__file__).parent / 'fixtures/six_tool_hierarchy/representative.json').read_text())


@pytest.mark.anyio
@pytest.mark.skipif(os.environ.get('TRACEWEAVE_V3_REPRESENTATIVE') != '1', reason='opt-in frozen V3 representative stage')
async def test_fixed_opentitan_uart_deep_local_hierarchy_and_physical_file_declaration(tmp_path):
    selected = SELECTION
    compile_log = selected['paths']['compile_log']
    # Explicit source-only scope: do not discover the historical constructed
    # log/VCD. Preparation and the lexical scan share the fixed compile log.
    prepared, scan = await asyncio.gather(
        public(tmp_path, 'prepare_design_context', dict(compile_log=compile_log,
            simulator='vcs', backend_preference='source', top_hint='tb', timeout_sec=30)),
        public(tmp_path, 'scan_structural_risks', dict(compile_log=compile_log,
            simulator='vcs', analysis_mode='fast')))
    (tmp_path / 'representative_boundary.json').write_text(json.dumps(dict(
        selection=selected, scope='compile_only_source_queries', historical_blocking_diagnostics=79,
        actual_simulation=False, actual_waveform=False, scan=scan)) + '\n')
    assert prepared['selected_top'] == 'tb' and prepared['semantic_status'] == 'not_run', prepared
    assert not source_graph_production.source_graph_runtime_created()
    handle = prepared['design_handle']
    async def call(tool, **args):
        return await public(tmp_path, tool, dict(design_handle=handle, timeout_sec=30, **args))
    point = await call('find_tb_instance', path=selected['target'])
    tree = await call('get_tb_subtree', root=selected['target'], depth=3, max_nodes=100)
    exact_local = await call('get_tb_subtree', root=selected['target'], depth=0, max_nodes=1, count_mode='exact')
    detail = await call('get_tb_file_detail', path=selected['source'])
    (tmp_path / 'representative_query_summary.raw.json').write_text(json.dumps(dict(
        point=point, tree=tree, exact_local=exact_local, file=detail)) + '\n')
    assert point['facts_origin'] == tree['facts_origin'] == exact_local['facts_origin'] == detail['facts_origin'] == 'slang'
    assert len(point['hits']) == 1, point
    row = point['hits'][0]
    assert row['path'] == selected['target'] and row['module'] == 'uart_core', row
    assert row['source_file'] == selected['site'] and row['source_line'] == selected['site_line'], row
    assert row['definition_file'] == selected['source'] and row['definition_line'] == selected['line'], row
    for value in (tree, exact_local):
        assert value['node']['path'] == selected['target']
        assert value['node']['definition_file'] == selected['source'] and value['node']['definition_line'] == selected['line']
        assert value['count_lower_bound'] > 0 and value['total_descendants'] is None, value
        assert value['coverage_status'] == 'partial'  # retain actual blocking frontend diagnostics
        assert value['analysis_limits'] == [], value
    assert tree['node']['children'] and exact_local['output_truncated']
    declarations = [item for item in detail['symbols'] if item['kind'] == 'module' and item['name'] == 'uart_core']
    assert len(declarations) == 1, detail
    assert declarations[0]['source_file'] == selected['source'] and declarations[0]['source_line'] == 10
    observations = []
    for directory in (tmp_path / 'cache_load_evidence').iterdir():
        ir = json.loads((directory / 'ir.raw.json').read_text())
        observations.append(dict(directory=directory.name, diagnostic_count=ir['coverage']['diagnostic_count'],
            blocking_count=ir['coverage']['blocking_diagnostic_count'], gaps=ir['coverage']['gaps']))
    (tmp_path / 'actual_frontend_diagnostics.raw.json').write_text(json.dumps(observations) + '\n')
    assert len(observations) == 4
    # Count is observed, not forced to equal the historical79. The complete
    # original diagnostic prefix remains in each saved worker IR above.
    assert all(row['blocking_count'] > 0 and row['gaps'] for row in observations), observations
    assert all(value['backend_status']['source_graph']['coverage_blocking_diagnostic_count'] > 0
               for value in (point, tree, exact_local, detail))
