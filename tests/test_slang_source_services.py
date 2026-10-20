"""Real isolated Slang file service and captured-context lifecycle acceptance.

The executor alone runs these. Namespace fixtures are not UVM runtime proof.
All expected declarations/lines originate from the hand-written fixture table.
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
from src import source_graph_production, source_graph_runtime
from src import slang_hierarchy_source
from tests.test_prepared_source_connectivity import isolation, anyio_backend, public, prepare
from tests.six_tool_support import materialize, assert_file_oracle, ORACLE, UNITS, TOP, segments


def real_positive(result):
    assert result.get('facts_origin') == 'slang', result
    assert result['backend_status']['actual_backend'] == 'source_graph', result
    assert result['backend_status']['single_backend_provenance'] is True
    assert result['backend_status']['source_graph']['single_artifact_provenance'] is True


async def file_call(tmp, handle, name, **args):
    return await public(tmp, name, dict(design_handle=handle, timeout_sec=30, **args))


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['source', 'auto'])
async def test_real_catalog_facts_filters_reuse_and_captured_parent_identities(tmp_path, policy):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, policy, TOP)
    assert not source_graph_production.source_graph_runtime_created()
    metadata = await file_call(tmp_path, handle, 'lookup_tb_files', basename='unit.sv')
    assert metadata['total'] == 2 and metadata['facts_origin'] == 'compile_metadata', metadata
    for section in ('compile_result', 'include_tree', 'filelist_tree'):
        value = await file_call(tmp_path, handle, 'dump_tb_section', section=section)
        assert value['semantic_status'] == 'not_run', value
    missing = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'uncompiled/unit.sv'))
    assert missing['error'] == 'file_not_in_compile_set'
    assert set(missing['did_you_mean']) == {str(tmp_path / 'a/unit.sv'), str(tmp_path / 'b/unit.sv')}
    assert not source_graph_production.source_graph_runtime_created()
    plain = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'plain.sv'))
    real_positive(plain)
    assert plain['backend_status']['source_graph']['metrics']['actual_build_count'] == 1
    reusable = await file_call(tmp_path, handle, 'lookup_tb_files', basename='plain.sv', has_module='plain_leaf')
    assert reusable['total'] == 1 and reusable['artifact_identity'] == plain['artifact_identity'], reusable
    assert reusable['backend_status']['source_graph']['metrics']['actual_build_count'] == 0
    details = {'plain.sv': plain}
    for name in ORACLE['file_predicates']:
        if name == 'plain.sv': continue
        value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / name))
        real_positive(value)
        details[name] = value
    assert details['p2.sv']['artifact_identity'] != plain['artifact_identity']
    assert details['p2.sv']['backend_status']['source_graph']['metrics']['actual_build_count'] == 1
    for name, value in details.items(): assert_file_oracle(value, name, tmp_path, all_details=details)
    # Both source versions and the uninstantiated declaration are positive;
    # the missing body is never smuggled into a complete global negative.
    modules = await file_call(tmp_path, handle, 'lookup_tb_files', basename='unit.sv', file_type='module', count_mode='exact', limit=1)
    assert modules['total'] == 2 and modules['count_status'] == 'exact' and modules['output_truncated'], modules
    assert modules['unknown_filter_files'] == 0
    for filters, expected in [
        ({'contains_uvm': True}, {'uvm_pkg.sv', 'p1.sv', 'p2.sv', 'usage.sv', 'alias_use.sv', 'nested_use.sv', 'top.sv'}),
        ({'contains_uvm': False}, {'included.svh', 'plain.sv', 'a/unit.sv', 'b/unit.sv', 'empty.sv'}),
        ({'has_module': 'version_a'}, {'a/unit.sv'}),
        ({'file_type': 'class'}, {'uvm_pkg.sv', 'p1.sv', 'included.svh', 'p2.sv'}),
        ({'contains_uvm': False, 'file_type': 'module'}, {'plain.sv', 'a/unit.sv', 'b/unit.sv'}),
    ]:
        result = await file_call(tmp_path, handle, 'lookup_tb_files', **filters, count_mode='exact')
        paths = {str(Path(row['path']).relative_to(tmp_path)) for row in result['matches']}
        assert paths == expected, (filters, result)
        if filters.get('contains_uvm') is not None or filters.get('has_module') == 'version_a':
            expected_unknown = 2 if filters.get('has_module') == 'version_a' else 1
            assert result['unknown_filter_files'] == expected_unknown and result['total'] is None, result


@pytest.mark.anyio
async def test_real_exact_alias_owns_canonical_declarations(tmp_path):
    log = materialize(tmp_path, alias=True)
    handle = await prepare(tmp_path, log, 'source', TOP)
    result = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'a/observed_alias.sv'))
    real_positive(result)
    assert_file_oracle(result, 'a/unit.sv', tmp_path)
    assert str(tmp_path / 'a/observed_alias.sv') in result['aliases']
    missing = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'a/../a/unit.sv'))
    assert missing['error'] == 'file_not_in_compile_set'


@pytest.mark.anyio
async def test_real_unprocessed_language_is_unknown_not_complete_empty(tmp_path):
    log = materialize(tmp_path, include_opaque=True)
    handle = await prepare(tmp_path, log, 'source', TOP)
    result = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'opaque.vhd'))
    real_positive(result)
    assert result['symbols'] == [] and result['symbol_inventory_complete'] is False, result
    assert result['contains_uvm'] is None and result['has_uvm_import'] is None
    assert result['file_type'] is None and 'source_file_not_processed_by_frontend' in result['coverage_gaps']


@pytest.mark.anyio
async def test_unavailable_compiled_member_retains_metadata(tmp_path):
    log = materialize(tmp_path)
    absent = tmp_path / 'missing.sv'
    text = log.read_text().replace(' -top ', f' {absent} -top ')
    text += f"Parsing design file '{absent}'\n"
    log.write_text(text)
    handle = await prepare(tmp_path, log, 'source', TOP)
    result = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(absent))
    assert result['path'] == str(absent) and result['source_status'] == 'unavailable', result
    assert result['semantic_status'] in {'unavailable', 'partial'} and result['coverage_status'] == 'partial'
    assert result['contains_uvm'] is None and result['symbol_inventory_complete'] is False
    assert result.get('symbols') in (None, [])


@pytest.mark.anyio
async def test_file_source_same_size_restored_mtime_invalidates_context(tmp_path):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'plain.sv'))
    real_positive(value)
    source = tmp_path / 'plain.sv'
    before = source.stat()
    source.write_text(source.read_text().replace('plain_leaf', 'other_leaf'))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(source))
    assert value.get('error_code') == 'design_context_changed' and 'symbols' not in value, value


@pytest.mark.anyio
async def test_file_request_one_deadline_includes_planning(tmp_path, monkeypatch):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    original = slang_hierarchy_source.build_instance_plan
    def slow(*args, **kw):
        time.sleep(.08)
        return original(*args, **kw)
    monkeypatch.setattr(slang_hierarchy_source, 'build_instance_plan', slow)
    value = await public(tmp_path, 'get_tb_file_detail', dict(design_handle=handle,
                         path=str(tmp_path / 'plain.sv'), timeout_sec=.04))
    assert value.get('error_code') == 'design_context_timeout' and 'symbols' not in value, value
    await asyncio.sleep(.1)


@pytest.mark.anyio
async def test_file_final_execution_policy_change_discards_semantic_result(tmp_path, monkeypatch):
    from src import slang_source_provider
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'source', TOP)
    original = slang_source_provider.SlangSourceProvider
    class Changed(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    monkeypatch.setattr(slang_source_provider, 'SlangSourceProvider', Changed)
    value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'plain.sv'))
    assert value.get('error_code') == 'design_context_execution_changed' and 'symbols' not in value, value


@pytest.mark.anyio
async def test_file_ordinary_npi_failure_uses_one_real_source_artifact(tmp_path, monkeypatch):
    log = materialize(tmp_path)
    handle = await prepare(tmp_path, log, 'auto', TOP)
    original = server._select_prepared_backend
    calls = []
    class Controlled:
        name, uses_external_worker = 'verdi_npi', False
        def query_source(self, plan):
            calls.append(plan.operation)
            raise RuntimeError('controlled unavailable NPI; never SDK execution')
    def select(entry, fallback):
        _, status, _ = original(entry, fallback)
        return Controlled(), status, None
    monkeypatch.setattr(server, '_select_prepared_backend', select)
    value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(tmp_path / 'plain.sv'))
    real_positive(value)
    assert calls == ['get_tb_file_detail']
    assert_file_oracle(value, 'plain.sv', tmp_path)
    assert any(row['backend'] == 'verdi_npi' and row['status'] == 'failed'
               for row in value['backend_status']['attempted_backends'])


@pytest.mark.anyio
async def test_macro_only_uvm_caller_cannot_be_a_complete_negative(tmp_path):
    log = materialize(tmp_path)
    header = tmp_path / 'macro.svh'
    header.write_text('`define REPORT_CALL uvm_pkg::report()\n')
    source = tmp_path / 'top.sv'
    source.write_text('`include "macro.svh"\nmodule service_top;\n'
                      '  int value;\n  initial value = `REPORT_CALL;\nendmodule\n')
    log.write_text(log.read_text().replace(f"Parsing design file '{source}'\n",
        f"Parsing design file '{source}'\nParsing included file '{header}'.\n"))
    handle = await prepare(tmp_path, log, 'source', TOP)
    value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(source))
    real_positive(value)
    assert value['contains_uvm'] is None and value['has_uvm_import'] is None, value
    assert 'source_macro_usage_domain_uncovered' in value['coverage_gaps'], value
    assert value['coverage_status'] == 'partial'


@pytest.mark.anyio
async def test_line_directive_cannot_replace_physical_file_or_line(tmp_path):
    log = materialize(tmp_path)
    source = tmp_path / 'top.sv'
    source.write_text('`line 800 "pretend.sv" 0\nmodule service_top;\n  logic marker;\nendmodule\n')
    handle = await prepare(tmp_path, log, 'source', TOP)
    value = await file_call(tmp_path, handle, 'get_tb_file_detail', path=str(source))
    real_positive(value)
    rows = {row['name']: row for row in value['symbols']}
    assert rows['service_top']['source_file'] == str(source) and rows['service_top']['source_line'] == 2, value
    assert rows['marker']['source_file'] == str(source) and rows['marker']['source_line'] == 3, value
