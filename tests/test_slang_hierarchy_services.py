"""Real finite V3 hierarchy, actual topology and semantic exports.

Independent frozen executor only. Public helper records requests, raw output,
decoded output and exceptions before any semantic assertion.
"""
from dataclasses import replace
import json
from pathlib import Path
import traceback

import pytest
import server
from src import source_graph_production
from src.hierarchy_source_ir import HierarchyQueryBudget
from tests.test_prepared_source_connectivity import isolation, anyio_backend, public, prepare
from tests.test_slang_class_services import real_positive
from tests.six_tool_hierarchy_support import (TOP, ORACLE, EXPECTED, materialize, assert_row,
    assert_tree, assert_module, assert_classes, flatten_tree)


async def call(evidence_dir, handle, tool='get_tb_subtree', /, **arguments):
    return await public(evidence_dir, tool, dict(design_handle=handle, timeout_sec=30, **arguments))


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source', 'auto'])
async def test_real_selected_top_deep_generate_array_bind_and_value_specializations(tmp_path, policy):
    handle = await prepare(tmp_path, materialize(tmp_path), policy, TOP)
    assert not source_graph_production.source_graph_runtime_created()
    modules = await call(tmp_path, handle, 'find_tb_instance', module='v3_leaf', count_mode='exact')
    real_positive(modules)
    assert_module(modules, tmp_path)
    assert {row['path'] for row in modules['hits']} == {path for path, row in EXPECTED.items() if row[3] == 'v3_leaf'}
    assert modules['backend_status']['source_graph']['metrics']['actual_build_count'] == 1
    tree = await call(tmp_path, handle, depth=-1, max_nodes=100)
    real_positive(tree)
    assert_tree(tree, tmp_path)
    assert tree['artifact_identity'] == modules['artifact_identity']
    assert tree['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
    for module, count in ORACLE['module_counts'].items():
        found = await call(tmp_path, handle, 'find_tb_instance', module=module, count_mode='exact')
        assert_module(found, tmp_path, module)
        assert found['total'] == count
        assert found['artifact_identity'] == modules['artifact_identity']
    # Same reused artifact IDs are comparable; point/local artifacts below are
    # compared by physical/path/parent facts rather than artifact-scoped IDs.
    full_rows = {row['path']: row for row in flatten_tree(tree['node'])}
    for target in (TOP + '.a.down.tail.bound_probe', TOP + '.banks[0]', TOP + '.lanes[1].unit'):
        point = await call(tmp_path, handle, 'find_tb_instance', path=target)
        assert point['total'] == 1 and point['coverage_status'] == 'complete', point
        row = assert_row(point['hits'][0], tmp_path)
        for key in ('path', 'parent', 'kind', 'module', 'source_file', 'source_line',
                    'definition_id', 'definition_file', 'definition_line', 'parameterization'):
            assert row[key] == full_rows[target][key], (row, full_rows[target])
    for target in ORACLE['absent_paths']:
        missing = await call(tmp_path, handle, 'find_tb_instance', path=target)
        if target.startswith('v3_other.'):
            assert missing['facts_origin'] == 'compile_metadata' and missing['semantic_status'] == 'unavailable', missing
            assert 'source_hierarchy_scope_unavailable' in missing['coverage_gaps'] and 'hits' not in missing
            continue
        assert missing['hits'] == [] and missing['total'] is None and missing['count_status'] == 'lower_bound', missing
        assert missing['coverage_status'] == 'partial'
    # Root-complete local traversal does not enumerate selected-top siblings.
    local = await call(tmp_path, handle, root=TOP + '.a', depth=-1, count_mode='exact')
    assert local['total_descendants'] == 3 and local['coverage_status'] == 'complete', local
    assert {row['path'] for row in flatten_tree(local['node'])} == {
        TOP + '.a', TOP + '.a.down', TOP + '.a.down.tail', TOP + '.a.down.tail.bound_probe'}
    for directory in (tmp_path / 'cache_load_evidence').iterdir():
        ir = json.loads((directory / 'ir.raw.json').read_text())
        metadata = dict(ir['metadata'])
        if 'hierarchy_scopes_v2' not in metadata: continue
        payload = json.loads(metadata['hierarchy_scopes_v2'])
        assert payload['semantic_tops'] == ORACLE['semantic_tops']
        assert ir['top_instances'] == ir['definitions'] == ir['instances'] == ir['bindings'] == []


@pytest.mark.anyio
async def test_real_count_exactness_is_independent_of_display_depth_and_limits(tmp_path):
    handle = await prepare(tmp_path, materialize(tmp_path), 'source', TOP)
    for arguments in ({'depth': 0}, {'depth': 1}, {'depth': 3}, {'depth': -1, 'max_nodes': 2}):
        result = await call(tmp_path, handle, count_mode='exact', **arguments)
        real_positive(result)
        assert result['count_status'] == 'exact' and result['total_descendants'] == 28, result
        assert result['output_truncated'] and result['coverage_status'] == 'complete', result
        assert result['analysis_limits'] == []
        assert_tree(result, tmp_path, full=False)
    deep = await call(tmp_path, handle, depth=4, max_nodes=100)
    assert_tree(deep, tmp_path)
    bounded = await call(tmp_path, handle, depth=0)
    assert bounded['count_status'] == 'lower_bound' and bounded['total_descendants'] is None
    assert bounded['count_lower_bound'] == 1 and bounded['output_truncated']
    assert bounded['analysis_limits'] == [] and bounded['coverage_status'] == 'partial'
    exact = await call(tmp_path, handle, 'find_tb_instance', module='v3_leaf', count_mode='exact', limit=1)
    assert_module(exact, tmp_path)
    assert len(exact['hits']) == 1 and exact['output_truncated'] and exact['total'] == 9
    limited = await call(tmp_path, handle, 'find_tb_instance', module='v3_leaf', limit=1)
    assert len(limited['hits']) == 1 and limited['output_truncated'] and limited['total'] is None
    assert limited['count_lower_bound'] == 2 and limited['coverage_status'] == 'partial'


@pytest.mark.anyio
async def test_real_four_sections_share_manual_facts_and_metadata_stays_frontend_free(tmp_path):
    handle = await prepare(tmp_path, materialize(tmp_path), 'source', TOP)
    for section in ('compile_result', 'include_tree', 'filelist_tree'):
        result = await call(tmp_path, handle, 'dump_tb_section', section=section)
        assert result['facts_origin'] == 'compile_metadata' and result['semantic_status'] == 'not_run', result
    assert not source_graph_production.source_graph_runtime_created()
    classes = await call(tmp_path, handle, 'get_tb_class_hierarchy')
    assert_classes(classes, tmp_path)
    detail = await call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'types.sv'))
    assert_classes(classes, tmp_path, detail)
    sections = {}
    for section in ('class_hierarchy_full', 'files_full', 'interfaces', 'component_tree_full'):
        result = await call(tmp_path, handle, 'dump_tb_section', section=section, max_items=100)
        real_positive(result)
        sections[section] = result
        assert result['analysis_limits'] == [] and not result['output_truncated'], result
        if section == 'class_hierarchy_full':
            assert_classes(result, tmp_path, detail, section=True)
            assert result['export_complete'] and result['artifact_identity'] == classes['artifact_identity']
        elif section == 'component_tree_full':
            assert_tree(result, tmp_path)
            assert result['export_complete']
        elif section == 'files_full':
            assert {Path(item['path']).name for item in result['data']} == {'types.sv', 'hierarchy.sv', 'outside.sv'}
            outside = next(item for item in result['data'] if Path(item['path']).name == 'outside.sv')
            assert ORACLE['outside_file_frontier'] in outside['semantic_gaps'], outside
            assert not result['export_complete'] and result['semantic_status'] == 'partial'
        else:
            assert {item['name']: item['source_line'] for item in result['data']} == {'v3_bus': 7, 'v3_bus_unused': 10}
            assert not result['export_complete']  # other declaration domains have explicit frontiers
        if section != 'component_tree_full':
            assert result['artifact_identity'] == classes['artifact_identity']
            assert result['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
        truncated = await call(tmp_path, handle, 'dump_tb_section', section=section, max_items=1)
        assert truncated['output_truncated'] and not truncated['export_complete'], truncated
        assert truncated['analysis_limits'] == []
        if section == 'component_tree_full': assert truncated['total_descendants'] == 28
        if section == 'class_hierarchy_full': assert truncated['class_inventory_count'] == 2
    # A filtered module lookup consumes the class-capable file subset artifact.
    files = await call(tmp_path, handle, 'lookup_tb_files', has_module='v3_leaf')
    assert files['facts_origin'] == 'slang' and len(files['matches']) == 1, files
    assert files['matches'][0]['path'] == str(tmp_path / 'hierarchy.sv')
    assert files['artifact_identity'] == classes['artifact_identity']


@pytest.mark.anyio
async def test_real_target_file_coordinates_ignore_large_confirmed_outside_buffers(tmp_path):
    log = materialize(tmp_path)
    outside = tmp_path / 'outside.sv'
    outside.write_text(outside.read_text() + '\n' * 70000)
    handle = await prepare(tmp_path, log, 'source', TOP)
    detail = await call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'types.sv'))
    real_positive(detail)
    observed = {(item['kind'], item['name'], item['source_line']) for item in detail['symbols']}
    assert {('class', 'Base', 2), ('class', 'Child', 4), ('interface', 'v3_bus', 7),
            ('interface', 'v3_bus_unused', 10)} <= observed, detail
    assert detail['analysis_limits'] == [] and all('limit' not in gap for gap in detail['semantic_gaps'])
    assert 'source_declaration_location_unbound' not in detail['semantic_gaps']
    ir = next((tmp_path / 'cache_load_evidence').glob('*/ir.raw.json'))
    payload = json.loads(dict(json.loads(ir.read_text())['metadata'])['source_catalog_v1'])
    assert not payload['limited'] and payload['coordinate_bytes'] < 100000
    assert payload['member_visits'] < 4096  # outside newlines were never indexed


@pytest.mark.anyio
async def test_real_blocking_diagnostics_preserve_hierarchy_facts_without_clean_global_negatives(tmp_path):
    log = materialize(tmp_path)
    source = tmp_path / 'hierarchy.sv'
    source.write_text(source.read_text().replace('  v3_types::Child item;',
        '  v3_types::Child item;\n  unresolved_v3_module actual_diagnostic();'))
    handle = await prepare(tmp_path, log, 'source', TOP)
    result = await call(tmp_path, handle, 'find_tb_instance', module='v3_leaf', count_mode='exact')
    real_positive(result)
    assert result['backend_status']['source_graph']['coverage_blocking_diagnostic_count'] > 0
    assert len(result['hits']) == 9 and result['total'] is None and result['coverage_status'] == 'partial', result
    assert 'source_hierarchy_frontend_diagnostics' in result['coverage_gaps']
    absent = await call(tmp_path, handle, 'find_tb_instance', module='NoSuchModule', count_mode='exact')
    assert absent['hits'] == [] and absent['total'] is None and absent['coverage_status'] == 'partial'


@pytest.mark.anyio
async def test_controlled_unstarted_worker_limit_is_public_partial_without_fallback_or_cached_facts(tmp_path, monkeypatch):
    from src.source_graph_runtime import IsolatedSourceGraphProcessRunner, WorkerBuildResult, PrepareStatus
    handle = await prepare(tmp_path, materialize(tmp_path), 'source', TOP)
    requests = []
    async def stopped(runner, request, **kwargs):
        requests.append(request)
        (tmp_path / 'controlled_worker.request.json').write_text(json.dumps(request.to_dict()) + '\n')
        result = WorkerBuildResult.failed(PrepareStatus.BUILD_FAILED,
            code='source_hierarchy_byte_limit', stage='projection', message='controlled pre-top byte frontier')
        (tmp_path / 'controlled_worker.return.raw.json').write_text(json.dumps(dict(
            status=result.status.value, blocker=result.blocker.to_dict(include_message=True))) + '\n')
        return result
    monkeypatch.setattr(IsolatedSourceGraphProcessRunner, 'run', stopped)
    result = await call(tmp_path, handle, 'find_tb_instance', module='v3_leaf', count_mode='exact')
    assert len(requests) == 1
    assert result['error'] == 'hierarchy_query_incomplete' and result['semantic_status'] == 'not_run', result
    assert result['facts_origin'] == 'compile_metadata' and result['coverage_status'] == 'partial'
    assert result['analysis_limits'] == ['fact_byte_limit']
    assert 'source_hierarchy_projection_unstarted' in result['coverage_gaps']
    assert result.get('total') is None and 'hits' not in result and 'node' not in result
    receipt = result['backend_status']['source_graph']
    assert receipt['blocker']['code'] == 'source_hierarchy_byte_limit', receipt
    assert not receipt['fallback_used']
    runtime = server.get_source_graph_runtime(server.get_source_graph_execution_config())
    assert not runtime._cache and not list((tmp_path / 'cache_load_evidence').glob('*'))


@pytest.mark.anyio
@pytest.mark.parametrize('reason,expected_limit', [
    ('source_hierarchy_scope_limit', 'scope_limit'),
    ('source_hierarchy_member_limit', 'native_call_limit'),
    ('source_hierarchy_depth_limit', 'scope_depth_limit'),
    ('source_hierarchy_byte_limit', 'fact_byte_limit')])
async def test_controlled_ready_worker_frontier_preserves_public_positive_facts_and_analysis_limit(
        tmp_path, monkeypatch, reason, expected_limit):
    import hashlib
    from src.connectivity_ir import ConnectivityIR, CoverageReport, CoverageStatus, CoverageGap
    from src.source_graph_contract import SourceGraphArtifactScopeReceipt
    from src.source_graph_runtime import IsolatedSourceGraphProcessRunner, WorkerBuildResult
    handle = await prepare(tmp_path, materialize(tmp_path), 'source', TOP)
    async def limited(runner, request, **kwargs):
        try:
            # The patched runner receives a query build request. Save it before
            # constructing controlled facts or following either identity layer.
            (tmp_path / 'controlled_worker.request.json').write_text(json.dumps(request.to_dict())+'\n')
            source = request.identity
            artifact_scope = request.artifact_identity.scope
            current = artifact_scope.semantic_search
            def controlled_row(path, parent, module, site, definition, params=(), *, site_column=8):
                where = dict(file=str(tmp_path / 'hierarchy.sv'), line=definition, column=8)
                parameters = list(map(list, params))
                identity = hashlib.sha256(json.dumps([module, 'Module', where, parameters], sort_keys=True).encode()).hexdigest()
                return dict(path=path, parent_path=parent, name=path.rsplit('.',1)[-1], kind='module',
                    definition_id=identity, definition_name=module, definition_location=where,
                    location=dict(file=str(tmp_path / 'hierarchy.sv'), line=site, column=site_column),
                    parameterization=parameters, gaps=[])
            value = dict(version=2, capability='instance_hierarchy', domain='selected_top_elaborated_scopes',
                selected_top=TOP, semantic_tops=['v3_other', TOP],
                search_sha256=hashlib.sha256(json.dumps(current.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                rows=[controlled_row(TOP,None,'v3_top',13,13),
                      controlled_row(TOP+'.low',TOP,'v3_leaf',15,1,[('W','4')], site_column=20)],
                # Both retained owners have omitted children, including the
                # bound probe under low; neither has child-exhaustion proof.
                complete_children=[], member_visits=20, retained_bytes=10000,
                limited=True, stop_reason=reason)
            ir = ConnectivityIR(frontend_name=source.frontend_name, frontend_version=source.frontend_version,
                definitions=(), instances=(), bindings=(), top_instances=(),
                coverage=CoverageReport(CoverageStatus.INCONCLUSIVE, 3, 3,
                    gaps=(CoverageGap(reason,'controlled finite projection frontier',CoverageStatus.INCONCLUSIVE),)),
                metadata=(('hierarchy_scopes_v2',json.dumps(value,sort_keys=True)),))
            receipt = SourceGraphArtifactScopeReceipt(scope=artifact_scope, coverage_status=CoverageStatus.INCONCLUSIVE,
                                                      gap_codes=(reason,))
            (tmp_path / 'controlled_worker.ir.raw.json').write_bytes(ir.to_json_bytes())
            (tmp_path / 'controlled_worker.receipt.raw.json').write_text(json.dumps(receipt.to_dict())+'\n')
            return WorkerBuildResult.ready(ir, receipt)
        except BaseException as exc:
            details = traceback.format_exc()
            try:
                (tmp_path / 'controlled_worker.exception.json').write_text(json.dumps(dict(
                    module=type(exc).__module__, type=type(exc).__name__, message=str(exc), traceback=details))+'\n')
                (tmp_path / 'controlled_worker.exception.txt').write_text(details)
            except BaseException:
                pass  # An evidence write failure must not replace the original exception.
            raise
    monkeypatch.setattr(IsolatedSourceGraphProcessRunner, 'run', limited)
    result = await call(tmp_path, handle, depth=-1, count_mode='exact')
    real_positive(result)  # real routing/codec; worker facts deliberately controlled
    assert result['node']['path'] == TOP and result['node']['children'][0]['path'] == TOP+'.low',result
    assert result['total_descendants'] is None and result['count_lower_bound'] == 1
    assert result['count_status'] == 'lower_bound' and result['coverage_status'] == 'partial'
    assert result['analysis_limits'] == [expected_limit]
    assert reason in result['backend_status']['source_graph']['coverage_gap_codes']
    assert result['backend_status']['source_graph']['coverage_status'] == 'inconclusive'
