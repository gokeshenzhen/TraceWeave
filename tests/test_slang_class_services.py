"""Real isolated Slang class services; independent executor only.

This is a finite compile-only type model, not a UVM runtime topology test.
Public helpers save requests/raw/decoded responses/exceptions before assertions.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import time

import pytest
import server
from src import source_graph_production, slang_hierarchy_source
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_symbols import query_class_hierarchy
from src.prepared_source_connectivity import build_prepared_source_plan
from src.slang_source_provider import SlangSourceProvider
from src.source_graph_contract import SemanticScopeSearch
from tests.test_prepared_source_connectivity import isolation, anyio_backend, public, prepare
from tests.six_tool_class_support import (materialize, assert_class_oracle, flatten, named,
                                           segments, TOP, ORACLE)


async def call(root, handle, tool='get_tb_class_hierarchy', **arguments):
    return await public(root, tool, dict(design_handle=handle, timeout_sec=30, **arguments))


def real_positive(result):
    assert result['facts_origin'] == 'slang', result
    status = result['backend_status']
    assert status['actual_backend'] == 'source_graph' and status['single_backend_provenance']
    assert status['source_graph']['single_artifact_provenance']


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source', 'auto'])
async def test_real_scoped_classes_type_and_value_specializations_aliases_and_file_identity(tmp_path, policy):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, policy, TOP)
    assert not source_graph_production.source_graph_runtime_created()
    first = await call(tmp_path, handle)
    real_positive(first)
    assert_class_oracle(first, tmp_path)
    assert first['backend_status']['source_graph']['metrics']['actual_build_count'] == 1
    again = await call(tmp_path, handle)
    assert_class_oracle(again, tmp_path)
    assert again['artifact_identity'] == first['artifact_identity']
    assert again['roots'] == first['roots']
    assert again['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
    details = {}
    for name in ('external.sv', 'classes.sv'):
        detail = await call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / name))
        real_positive(detail)
        assert detail['artifact_identity'] == first['artifact_identity'], detail
        assert detail['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
        details[name] = detail
    rows = assert_class_oracle(first, tmp_path, details=details)
    beta_same = named(rows, 'beta::Same')
    for root in ('beta::Same', beta_same['symbol_id'], beta_same['declaration_id']):
        result = await call(tmp_path, handle, root_class=root)
        assert result['total'] == 1 and result['roots'][0]['symbol_id'] == beta_same['symbol_id'], result
    ambiguous = await call(tmp_path, handle, root_class='Same')
    assert ambiguous['error'] == 'class_name_ambiguous' and len(ambiguous['candidates']) == 2
    absent = await call(tmp_path, handle, root_class='NoSuchClass')
    assert absent['error'] == 'class_unresolved' and absent['coverage_status'] == 'partial'
    limited = await call(tmp_path, handle, root_class='alpha::Same', depth=0)
    assert limited['total'] == 1 and limited['output_truncated']
    capped = await call(tmp_path, handle, max_nodes=2)
    assert capped['total'] == 2 and capped['output_truncated']
    assert capped['class_inventory_lower_bound'] == first['class_inventory_lower_bound']


@pytest.mark.anyio
async def test_real_class_capability_does_not_reuse_file_or_hierarchy_only_inventory(tmp_path):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    file = await call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'classes.sv'))
    hierarchy = await call(tmp_path, handle, 'find_tb_instance', path=TOP)
    classes = await call(tmp_path, handle)
    for result in (file, hierarchy, classes):
        real_positive(result)
        assert result['backend_status']['source_graph']['metrics']['actual_build_count'] == 1
    assert len({result['artifact_identity'] for result in (file, hierarchy, classes)}) == 3
    assert_class_oracle(classes, tmp_path)


@pytest.mark.anyio
async def test_real_bound_external_base_is_reference_only_in_a_narrow_catalog(tmp_path):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    budget = HierarchyQueryBudget()
    with server._design_context_store.pin(handle) as entry:
        context = entry.context
        search = SemanticScopeSearch(purpose='source_catalog', source_files=(str(tmp_path / 'classes.sv'),),
            source_capabilities=('class_inheritance', 'file_declarations'), source_aliases=context.aliases)
        config = server.get_source_graph_execution_config()
        plan = build_prepared_source_plan(context=context, operation='driver',
            args={'signal_path': TOP + '.__traceweave_source_anchor'}, config=config, search=search)
        # This direct real provider control deliberately narrows the requested
        # inventory, while the frontend still compiles both captured files.
        (tmp_path / 'external.request.json').write_text(json.dumps(plan.request.to_dict()) + '\n')
        try:
            outcome = await server.get_source_graph_runtime(config).prepare(plan.request, timeout_seconds=30)
            (tmp_path / 'external.outcome.raw.json').write_text(json.dumps(outcome.to_receipt()) + '\n')
            provider = SlangSourceProvider(outcome.entry, context, budget, search.source_files,
                                          capabilities=search.source_capabilities)
            result = query_class_hierarchy(provider, budget=budget)
            raw = json.dumps(result)
            (tmp_path / 'external.result.raw.json').write_text(raw + '\n')
            decoded = json.loads(raw)
            (tmp_path / 'external.result.decoded.json').write_text(json.dumps(decoded) + '\n')
        except BaseException as exc:
            import traceback
            (tmp_path / 'external.exception.json').write_text(json.dumps({
                'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}) + '\n')
            raise
    rows = flatten(decoded)
    external = named(rows, 'external_pkg::Base')
    assert external['external'] and external['reference_only']
    assert external['source_file'] is external['source_line'] is None
    child = named(rows, 'beta::External')
    assert child['base_symbol_id'] == external['symbol_id'] and child['base_status'] == 'resolved'
    assert decoded['class_inventory_lower_bound'] == 22 and decoded['total'] == 23
    assert 'class_base_outside_inventory' in decoded['coverage_gaps']


@pytest.mark.anyio
async def test_real_missing_base_and_frontend_diagnostics_stay_unresolved(tmp_path):
    log = materialize(tmp_path, missing=True)
    handle = await prepare(tmp_path, log, 'source', 'missing_class_top')
    result = await call(tmp_path, handle)
    real_positive(result)
    row = named(flatten(result), 'broken_pkg::MissingChild')
    assert row['source_line'] == 2 and row['source_file'] == str(tmp_path / 'missing.sv')
    assert row['base_status'] == 'unresolved' and row['base_symbol_id'] is None
    assert result['coverage_status'] == 'partial'
    assert 'class_base_unresolved' in result['coverage_gaps']
    # Independent max review: a complete finite declaration inventory is
    # separate from an unresolved inheritance relation. Both saved attempts
    # observed zero SDK diagnostics here; never fabricate a missing-base error.
    expected = ORACLE['missing']
    assert result['class_inventory_complete'] is expected['class_inventory_complete']
    assert result['class_inventory_count'] == result['class_inventory_lower_bound'] == expected['class_inventory_count']
    receipt = result['backend_status']['source_graph']
    assert receipt['coverage_diagnostic_count'] == expected['diagnostic_count']
    assert receipt['coverage_blocking_diagnostic_count'] == expected['blocking_diagnostic_count']
    assert 'source_class_frontend_diagnostics' not in result['coverage_gaps']


@pytest.mark.anyio
async def test_real_unknown_module_diagnostic_preserves_class_facts_and_incomplete_inventory(tmp_path):
    expected = ORACLE['diagnostic']
    log = materialize(tmp_path, diagnostic=True)
    handle = await prepare(tmp_path, log, 'source', expected['top'])
    result = await call(tmp_path, handle)
    real_positive(result)
    receipt = result['backend_status']['source_graph']
    assert receipt['coverage_diagnostic_count'] > 0
    assert receipt['coverage_blocking_diagnostic_count'] > 0
    assert 'frontend_diagnostic_unknownmodule' in receipt['coverage_gap_codes']
    native_gaps = []
    for path in (tmp_path / 'cache_load_evidence').glob('*/ir.raw.json'):
        saved_ir = json.loads(path.read_text())
        native_gaps.extend(gap for gap in saved_ir['coverage']['gaps']
                           if gap['code'] == 'frontend_diagnostic:UnknownModule')
    assert len(native_gaps) == 1, native_gaps
    where = native_gaps[0]['location']
    diagnostic_file = Path(where['file'])
    if not diagnostic_file.is_absolute():
        diagnostic_file = Path(server.__file__).parent / diagnostic_file
    assert diagnostic_file.resolve() == (tmp_path / expected['file']).resolve()
    assert where['line'] == expected['module_site_line']
    assert expected['undeclared_module'] in native_gaps[0]['message']
    assert 'source_class_frontend_diagnostics' in result['coverage_gaps']
    assert result['coverage_status'] == 'partial' and result['class_inventory_complete'] is False
    assert result['class_inventory_count'] is None
    assert result['class_inventory_lower_bound'] == expected['class_inventory_lower_bound']
    rows = flatten(result)
    for qualified, line, base in expected['classes']:
        row = named(rows, qualified)
        assert row['source_file'] == str(tmp_path / expected['file']) and row['source_line'] == line
        if base is None:
            assert row['base_status'] == 'none'
        else:
            assert row['base_status'] == 'resolved' and row['base_symbol_id'] == named(rows, base)['symbol_id']
    detail = await call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / expected['file']))
    real_positive(detail)
    assert detail['artifact_identity'] == result['artifact_identity']
    assert detail['symbol_inventory_complete'] is False
    assert detail['contains_uvm'] is None and detail['has_uvm_import'] is None
    for row in rows:
        matches = [symbol for symbol in detail['symbols'] if symbol['kind'] == 'class'
                   and symbol['source_line'] == row['source_line']]
        assert len(matches) == 1 and matches[0]['declaration_id'] == row['declaration_id']


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source', 'auto'])
async def test_partial_capture_class_query_is_unavailable_without_worker_or_false_absence(tmp_path, policy):
    log = materialize(tmp_path)
    log.write_text(log.read_text().replace(' -top ', f' {tmp_path / "unavailable.sv"} -top ')
                   + f"Parsing design file '{tmp_path / 'unavailable.sv'}'\n")
    handle = await prepare(tmp_path, log, policy, TOP)
    result = await call(tmp_path, handle)
    assert result['facts_origin'] == 'compile_metadata' and result['semantic_status'] == 'unavailable'
    assert result['error'] == 'semantic_provider_unavailable' and 'roots' not in result
    assert 'source_capture_incomplete' in result['coverage_gaps']
    assert not source_graph_production.source_graph_runtime_created()


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['ordinary', 'identity', 'deadline', 'cancel'])
async def test_controlled_npi_class_failure_preserves_typed_boundaries(tmp_path, monkeypatch, failure):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'auto', TOP)
    original = server._select_prepared_backend
    calls = []
    class Controlled:
        name, uses_external_worker = 'verdi_npi', False
        def query_source(self, plan):
            calls.append(plan.operation)
            if failure == 'identity': raise CaptureAborted('input_changed')
            if failure == 'deadline': raise TimeoutError('controlled deadline')
            if failure == 'cancel': raise OperationCancelled()
            raise RuntimeError('controlled ordinary NPI failure, no SDK')
    def select(entry, fallback):
        _, status, _ = original(entry, fallback)
        return Controlled(), status, None
    monkeypatch.setattr(server, '_select_prepared_backend', select)
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError): await call(tmp_path, handle)
    else:
        result = await call(tmp_path, handle)
        if failure == 'ordinary':
            real_positive(result)
            assert_class_oracle(result, tmp_path)
        else:
            assert result['error_code'] == {
                'identity': 'design_context_changed', 'deadline': 'design_context_timeout'}[failure]
            assert 'roots' not in result
    assert calls == ['get_tb_class_hierarchy']
    if failure != 'ordinary': assert not source_graph_production.source_graph_runtime_created()


@pytest.mark.anyio
async def test_class_original_deadline_includes_planning(tmp_path, monkeypatch):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    original = slang_hierarchy_source.build_instance_plan
    def slow(*args, **kwargs):
        time.sleep(.08)
        return original(*args, **kwargs)
    monkeypatch.setattr(slang_hierarchy_source, 'build_instance_plan', slow)
    result = await public(tmp_path, 'get_tb_class_hierarchy', dict(design_handle=handle, timeout_sec=.04))
    assert result['error_code'] == 'design_context_timeout' and 'roots' not in result
    await asyncio.sleep(.1)


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['source', 'policy'])
async def test_class_final_source_or_policy_change_discards_result(tmp_path, monkeypatch, change):
    from src import slang_source_provider
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    original = slang_source_provider.SlangSourceProvider
    class Changed(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if change == 'policy':
                monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
            else:
                source = tmp_path / 'classes.sv'
                before = source.stat()
                source.write_text(source.read_text().replace('payload', 'changed'))
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(slang_source_provider, 'SlangSourceProvider', Changed)
    result = await call(tmp_path, handle)
    assert result['error_code'] == {'source': 'design_context_changed',
                                  'policy': 'design_context_execution_changed'}[change]
    assert 'roots' not in result
