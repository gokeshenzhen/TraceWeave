"""Handwritten compile ownership facts; no filesystem or HDL/native frontend.

Snapshots below are invented immutable observations, not product-generated
expected results. They establish metadata semantics only, never elaboration,
actual source availability, semantic declarations or KDB association.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import re
import sys
import threading

import pytest

from src import hierarchy_source_ir as ir
from src import hierarchy_source_metadata as metadata
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.compile_session_snapshot import CompileSessionSnapshot, FileContentSnapshot
from src.design_context import DesignContext, IncludeResolution
from src.hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget


V1 = '/project/release_v1/rtl/shared.sv'
V2 = '/project/release_v2/rtl/shared.sv'
ALIAS = '/work/aliases/shared_alias.sv'
ALIAS_V2 = '/work/stale/shared.sv'
TOOL = '/vendor/uvm_pkg.sv'
UNCAPTURED = '/project/rtl/not_captured.sv'
REPORTED = '/project/includes/reported.svh'
MISSING = '/project/includes/missing.svh'
ORDERED = '/project/includes/ordered.svh'
DEPENDENCY = '/project/includes/dependency.svh'
DEPENDENCY_ALIAS = '/work/headers/dependency_link.svh'
SUPPORT = ('/support/compile.log', '/support/compile.f', '/support/setup.cfg', '/support/log_named_as_source.sv')
IDENTITY = 'a' * 64


def source_record(path, size=17):
    return FileContentSnapshot(path=path, device=11, inode=12, size=size,
                               mtime_ns=13, ctime_ns=14, sha256='b' * 64)


def context(*, result=None, captured=(), aliases=(), unavailable=(), resolutions=(),
            gaps=(), include_complete=True, identity=IDENTITY):
    if result is None:
        result = {}
    return DesignContext(
        ordered_compile_logs=(SUPPORT[0],), simulator='vcs', selected_top='top',
        backend_preference='auto', compile_result_json=json.dumps(result),
        snapshot=CompileSessionSnapshot(tuple(source_record(path) for path in captured),
                                        not unavailable, () if not unavailable else ('compile_input_unavailable',), 'c' * 64),
        aliases=tuple(aliases), unavailable=tuple(unavailable), include_resolutions=tuple(resolutions),
        environment=(), identity_sha256=identity, gaps=tuple(gaps),
        include_resolution_complete=include_complete, metrics=(),
    )


def compile_record():
    return {
        'compile_cwd': '/work', 'simulator': 'vcs', 'top_modules': ['top'],
        'files': {'user': [
            {'path': 'aliases/shared_alias.sv', 'category': 'rtl', 'type': 'module'},
            {'path': V2, 'category': 'tb', 'type': 'interface'},
            {'path': V1, 'category': 'shared', 'type': 'compiler_unknown'},
            {'path': UNCAPTURED, 'category': 'rtl', 'type': 'module'},
        ]},
        'compile_evidence': {
            'ordered_compilation_units': [
                {'path': V1, 'role': 'project', 'type': 'SystemVerilog'},
                {'path': V2, 'role': 'project', 'type': 'SystemVerilog'},
                {'path': TOOL, 'role': 'tool_library', 'type': 'package'},
            ],
            'ordered_includes': [{'parent': V1, 'path': ORDERED}, {'parent': V2, 'path': REPORTED}],
        },
        'include_tree': {V1: [REPORTED, MISSING, REPORTED]},
        'filelist_tree': {SUPPORT[1]: {'children': ['/support/nested.f'], 'options': ['+define+MODE=1']}},
        'warnings': ['recorded warning only'],
    }


def fixture_context():
    return context(
        result=compile_record(), captured=(V1, V2, TOOL, REPORTED, ORDERED, DEPENDENCY, *SUPPORT),
        aliases=((ALIAS, V1), (ALIAS_V2, V2), (DEPENDENCY_ALIAS, DEPENDENCY)),
        unavailable=((MISSING, (MISSING, 2)),),
        resolutions=(
            IncludeResolution((DEPENDENCY_ALIAS,), DEPENDENCY, DEPENDENCY_ALIAS),
            IncludeResolution((REPORTED,), REPORTED, REPORTED),
            IncludeResolution(('/unresolved/search/no_header.svh',), None, None),
        ),
        gaps=('compile_inputs_incomplete', 'protected_region'), include_complete=False,
    )


@contextmanager
def no_io_or_frontend(monkeypatch):
    """Restore before pytest renders failures; even a live realpath is forbidden."""
    original_import, previous_profile = builtins.__import__, sys.getprofile()
    def forbidden(*a, **kw):
        raise AssertionError('metadata operation attempted filesystem/current/frontend work')
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        names = [name, *(str(item) for item in (fromlist or ()))]
        assert not any(any(piece in {'pynpi', 'pyslang', 'tb_hierarchy_builder', 'structural_scanner'} or
                                  piece.startswith(('slang_', 'source_graph_worker')) for piece in item.split('.'))
                       for item in names), 'metadata operation imported a semantic/native builder'
        return original_import(name, globals, locals, fromlist, level)
    def guarded_calls(frame, event, arg):
        if event == 'call':
            path = frame.f_code.co_filename.replace('\\', '/')
            assert not any(part in path for part in (
                '/pynpi/', '/src/slang_', '/src/tb_hierarchy_builder.py',
                '/src/structural_scanner.py', '/src/source_graph_worker.py',
            )), 'metadata operation executed a semantic/native builder'
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(builtins, '__import__', guarded_import)
        for name in ('open', 'stat', 'lstat', 'scandir', 'listdir', 'readlink'):
            patch.setattr(os, name, forbidden)
        patch.setattr(os.path, 'realpath', forbidden)
        for name in ('open', 'read_text', 'read_bytes', 'stat', 'lstat', 'resolve'):
            patch.setattr(Path, name, forbidden)
        patch.setattr(DesignContext, 'current', forbidden)
        patch.setattr(DesignContext, 'metadata_current', forbidden)
        patch.setattr(FileContentSnapshot, 'current', forbidden)
        patch.setattr(IncludeResolution, 'current', forbidden)
        sys.setprofile(guarded_calls)
        try:
            yield
        finally:
            sys.setprofile(previous_profile)


def build(monkeypatch, ctx, **kwargs):
    with no_io_or_frontend(monkeypatch):
        return metadata.build_compile_source_catalog(ctx, **kwargs)


def test_handwritten_source_inventory_keeps_versions_and_excludes_all_support_records(monkeypatch):
    ctx = fixture_context()
    catalog = build(monkeypatch, ctx)
    assert [item.path for item in catalog.files] == [V1, V2, UNCAPTURED, TOOL, REPORTED, MISSING, ORDERED, DEPENDENCY]
    assert catalog.context_identity == IDENTITY
    assert catalog.gaps == ('compile_inputs_incomplete', 'include_inventory_incomplete')
    assert catalog.metadata_bytes > 0 and catalog.preparation_ms >= 0
    assert all(path not in [item.path for item in catalog.files] for path in SUPPORT)
    with no_io_or_frontend(monkeypatch):
        v1, v2 = catalog.find_file(V1), catalog.find_file(V2)
        alias_v1, alias_v2 = catalog.find_file(ALIAS), catalog.find_file(ALIAS_V2)
    assert v1 is alias_v1 and v2 is alias_v2 and v1 is not v2
    assert v1.categories == ('rtl', 'shared')
    assert v1.compiler_types == ('SystemVerilog', 'compiler_unknown', 'module')
    assert v1.roles == ('project',) and v1.is_compilation_unit
    assert v1.aliases == tuple(sorted(('aliases/shared_alias.sv', ALIAS, V1)))
    assert v2.compiler_types == ('SystemVerilog', 'interface')
    assert v2.categories == ('tb',)
    assert re.fullmatch(r'hs_[0-9a-f]{64}', v1.file_id)
    assert v1.file_id != v2.file_id


def test_captured_unavailable_and_not_captured_are_distinct_without_live_filesystem_checks(monkeypatch):
    catalog = build(monkeypatch, fixture_context())
    with no_io_or_frontend(monkeypatch):
        states = {path: catalog.find_file(path).source_status for path in (V1, TOOL, REPORTED, MISSING, UNCAPTURED)}
    assert states == {V1: 'captured', TOOL: 'captured', REPORTED: 'captured',
                      MISSING: 'unavailable', UNCAPTURED: 'not_captured'}


def test_compiler_include_edges_and_preparation_dependency_inventory_are_independent(monkeypatch):
    catalog = build(monkeypatch, fixture_context())
    with no_io_or_frontend(monkeypatch):
        a, b = catalog.find_file(V1), catalog.find_file(V2)
        reported, missing = catalog.find_file(REPORTED), catalog.find_file(MISSING)
        dependency = catalog.find_file(DEPENDENCY_ALIAS)
        unresolved = catalog.find_file('/unresolved/search/no_header.svh')
    assert a.includes == (REPORTED, MISSING, ORDERED)
    assert b.includes == (REPORTED,)
    assert reported.is_reported_include and reported.is_dependency_include
    assert missing.is_reported_include and not missing.is_dependency_include
    assert not dependency.is_reported_include and dependency.is_dependency_include
    assert dependency.includes == ()
    assert DEPENDENCY not in a.includes and DEPENDENCY not in b.includes
    assert unresolved is None
    assert a.row()['include_origin'] == 'compile_log'
    assert missing.source_status == 'unavailable'
    assert dependency.source_status == 'captured'


def test_compiler_types_are_hints_not_semantic_types_or_declaration_inventory(monkeypatch):
    catalog = build(monkeypatch, fixture_context())
    row = catalog.find_file(V1).row()
    assert row['ownership_origin'] == 'compile_metadata'
    assert row['compiler_file_types'] == ['SystemVerilog', 'compiler_unknown', 'module']
    assert not any(key in row for key in ('file_type', 'semantic_file_type', 'modules', 'classes', 'declarations'))
    assert catalog.find_file(TOOL).roles == ('tool_library',)
    assert catalog.find_file(TOOL).is_compilation_unit


def test_noncompiled_version_is_not_resolved_but_basename_suggestions_keep_both_versions(monkeypatch):
    catalog = build(monkeypatch, fixture_context())
    with no_io_or_frontend(monkeypatch):
        missing = catalog.find_file('/outside/shared.sv')
        suggested = catalog.suggest_files('/outside/shared.sv')
        one = catalog.suggest_files('/outside/shared.sv', limit=1)
        alias_basename = catalog.suggest_files('/outside/shared_alias.sv')
        absent = catalog.suggest_files('/outside/no_match.sv')
    assert missing is None
    assert suggested == [V1, V2] and one == [V1]
    assert alias_basename == [V1]
    assert absent == []


@pytest.mark.parametrize('filters,expected', [
    ({'basename': 'shared.sv'}, [V1, V2]),
    ({'basename': 'shared_alias.sv'}, [V1]),
    ({'name_contains': 'shared', 'path_contains': 'release_v2'}, [V2]),
    ({'basename': 'shared.sv', 'name_contains': 'shared', 'path_contains': 'release_v1'}, [V1]),
    ({'basename': 'shared_alias.sv', 'path_contains': 'release_v2'}, []),
    ({'basename': 'shared_alias.sv', 'path_contains': 'release_v1'}, [V1]),
    ({'path_contains': '/vendor/'}, [TOOL]),
])
def test_filters_are_and_conditions_over_observed_canonical_and_alias_spellings(monkeypatch, filters, expected):
    catalog = build(monkeypatch, fixture_context())
    with no_io_or_frontend(monkeypatch):
        actual = [item.path for item in catalog.select_files(**filters)]
    assert actual == expected


@pytest.mark.parametrize('filters', [
    {}, {'basename': 1}, {'name_contains': False}, {'path_contains': []},
    {'basename': ''}, {'name_contains': ''}, {'path_contains': ''},
    {'basename': 'shared.sv', 'name_contains': ''},
    {'name_contains': 'shared', 'path_contains': ''},
    {'basename': '', 'path_contains': 'release_v1'},
])
def test_filter_is_required_and_nonstring_filters_are_rejected(monkeypatch, filters):
    catalog = build(monkeypatch, fixture_context())
    with no_io_or_frontend(monkeypatch):
        with pytest.raises(ValueError):
            list(catalog.select_files(**filters))


def test_only_captured_dotdot_alias_may_establish_canonical_membership(monkeypatch):
    observed = '/work/links/../observed.sv'
    result = {'compile_cwd': '/work', 'files': {'user': [
        {'path': observed}, {'path': 'links/../observed.sv'},
        {'path': '/work/links/../unobserved.sv'}, {'path': '/work/./unobserved.sv'},
    ]}}
    ctx = context(result=result, aliases=((observed, V1),), captured=(V1,))
    catalog = build(monkeypatch, ctx)
    assert [item.path for item in catalog.files] == [V1]
    assert catalog.gaps == ('compile_source_path_unresolved',)
    with no_io_or_frontend(monkeypatch):
        assert catalog.find_file(observed) is catalog.find_file(V1)
        assert catalog.find_file('/work/unobserved.sv') is None
        assert catalog.find_file('/work/never_observed_link.sv') is None


@pytest.mark.parametrize('cwd', [None, 'relative/cwd', 3])
def test_unresolved_relative_paths_are_not_made_absolute_using_ambient_cwd(monkeypatch, cwd):
    result = {'compile_cwd': cwd, 'files': {'user': [{'path': 'rtl/a.sv'}, {'path': '/safe.sv'}]}}
    catalog = build(monkeypatch, context(result=result))
    assert [item.path for item in catalog.files] == ['/safe.sv']
    assert catalog.gaps == ('compile_source_path_unresolved',)


def test_identity_changes_file_ids_but_source_bodies_are_never_part_of_catalog(monkeypatch):
    first_context = fixture_context()
    first = build(monkeypatch, first_context)
    again = build(monkeypatch, first_context)
    changed = build(monkeypatch, replace(first_context, identity_sha256='d' * 64))
    assert first.find_file(V1).file_id == again.find_file(V1).file_id
    assert first.find_file(V1).file_id != changed.find_file(V1).file_id
    assert not any(hasattr(first, name) for name in ('bodies', 'source_text', 'native_handle', 'hierarchy'))


def test_catalog_records_and_rows_are_immutable_independent_copies(monkeypatch):
    ctx = fixture_context()
    before = ctx.compile_result_json
    catalog = build(monkeypatch, ctx)
    item = catalog.find_file(V1)
    with pytest.raises(FrozenInstanceError):
        item.path = '/changed.sv'
    with pytest.raises(FrozenInstanceError):
        catalog.context_identity = 'changed'
    row = item.row()
    row['aliases'].append('/unobserved.sv')
    row['compile_categories'].clear()
    row['includes'].append('/invented.svh')
    row2 = item.row()
    assert '/unobserved.sv' not in row2['aliases']
    assert row2['compile_categories'] == ['rtl', 'shared']
    assert row2['includes'] == [REPORTED, MISSING, ORDERED]
    assert ctx.compile_result_json == before


@pytest.mark.parametrize('section', ['compile_result', 'include_tree', 'filelist_tree'])
def test_three_record_exports_are_exact_fresh_metadata_copies_with_honest_coverage(monkeypatch, section):
    ctx = fixture_context()
    expected_result = {
        'compile_result': compile_record(),
        'include_tree': {V1: [REPORTED, MISSING, REPORTED]},
        'filelist_tree': {SUPPORT[1]: {'children': ['/support/nested.f'], 'options': ['+define+MODE=1']}},
    }[section]
    # Expected data is the hand-written input record, never a product export.
    before = ctx.compile_result_json
    with no_io_or_frontend(monkeypatch):
        first = metadata.export_compile_metadata(ctx, section)
        second = metadata.export_compile_metadata(ctx, section)
    assert first == {
        'section': section, 'data': expected_result, 'facts_origin': 'compile_metadata',
        'artifact_identity': IDENTITY, 'semantic_status': 'not_run',
        'coverage_domain': 'reported_compile_record', 'record_export_complete': True,
        'coverage_status': 'partial', 'coverage_gaps': ['compile_inputs_incomplete', 'protected_region'],
    }
    first['data'].clear()
    first['coverage_gaps'].append('invented_gap')
    assert second['data'] == expected_result
    with no_io_or_frontend(monkeypatch):
        third = metadata.export_compile_metadata(ctx, section)
    assert third == second
    assert ctx.compile_result_json == before


def test_empty_complete_record_export_does_not_claim_semantics_or_inventory_from_snapshot(monkeypatch):
    ctx = context(captured=SUPPORT)
    catalog = build(monkeypatch, ctx)
    with no_io_or_frontend(monkeypatch):
        exported = metadata.export_compile_metadata(ctx, 'include_tree')
    assert catalog.files == () and catalog.gaps == () and catalog.metadata_bytes == 0
    assert exported['data'] == {} and exported['coverage_status'] == 'complete'
    assert exported['semantic_status'] == 'not_run'
    assert exported['coverage_domain'] == 'reported_compile_record'


@pytest.mark.parametrize('section', ['component_tree', 'files_full', 'classes', 'types', 'unknown'])
def test_semantic_sections_are_not_inferred_from_compile_metadata(monkeypatch, section):
    with no_io_or_frontend(monkeypatch):
        with pytest.raises(ValueError):
            metadata.export_compile_metadata(fixture_context(), section)


def test_file_and_byte_limits_use_unique_members_and_independent_exact_byte_boundary(monkeypatch):
    # Hand count JSON lengths: ["file", "/a.sv", "<64 chars>"] = 85;
    # "/a.sv" = 7. Two accounting records cost (128+4*85)+(128+4*7)=624.
    one = context(result={'files': {'user': [{'path': '/a.sv'}, {'path': '/a.sv'}]}})
    catalog = build(monkeypatch, one, max_files=1, max_bytes=624)
    assert len(catalog.files) == 1 and catalog.metadata_bytes == 624
    with pytest.raises(HierarchyBudgetExceeded) as caught:
        build(monkeypatch, one, max_bytes=623)
    assert caught.value.reason == 'metadata_byte_limit'
    two = context(result={'files': {'user': [{'path': '/a.sv'}, {'path': '/b.sv'}]}})
    with pytest.raises(HierarchyBudgetExceeded) as caught:
        build(monkeypatch, two, max_files=1)
    assert caught.value.reason == 'metadata_file_limit'
    assert one.compile_result()['files']['user'] == [{'path': '/a.sv'}, {'path': '/a.sv'}]


@pytest.mark.parametrize('kwargs', [
    {'max_files': 0}, {'max_files': True}, {'max_files': 1.5}, {'max_files': 100001},
    {'max_bytes': 0}, {'max_bytes': False}, {'max_bytes': 64 * 1024 * 1024 + 1},
])
def test_invalid_catalog_budgets_are_rejected(monkeypatch, kwargs):
    with no_io_or_frontend(monkeypatch):
        with pytest.raises(ValueError):
            metadata.build_compile_source_catalog(context(), **kwargs)


@pytest.mark.parametrize('limit', [0, True, 5001])
def test_invalid_suggestion_limits_do_not_scan_catalog(monkeypatch, limit):
    catalog = build(monkeypatch, fixture_context())
    with no_io_or_frontend(monkeypatch):
        with pytest.raises(ValueError):
            catalog.suggest_files('/else/shared.sv', limit=limit)


def test_real_contextvar_cancellation_is_observed_without_touching_context_or_files(monkeypatch):
    ctx, event = fixture_context(), threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            build(monkeypatch, ctx)
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize('operation', ['build', 'find', 'suggest', 'select', 'export'])
@pytest.mark.parametrize('error', [OperationCancelled('cancel'), CaptureAborted('input_changed'), TimeoutError('typed timeout')])
def test_checkpoint_typed_failures_propagate_unchanged_without_metadata_mutation(monkeypatch, operation, error):
    ctx = fixture_context()
    catalog = build(monkeypatch, ctx)
    before = ctx.compile_result_json
    def checkpoint():
        raise error
    with no_io_or_frontend(monkeypatch):
        with pytest.raises(type(error)) as caught:
            if operation == 'build':
                metadata.build_compile_source_catalog(ctx, checkpoint=checkpoint)
            elif operation == 'find':
                catalog.find_file(V1, checkpoint=checkpoint)
            elif operation == 'suggest':
                catalog.suggest_files('/else/shared.sv', checkpoint=checkpoint)
            elif operation == 'select':
                list(catalog.select_files(basename='shared.sv', checkpoint=checkpoint))
            else:
                metadata.export_compile_metadata(ctx, 'compile_result', checkpoint=checkpoint)
    assert caught.value is error
    assert ctx.compile_result_json == before


def test_mid_build_and_post_copy_cancellation_do_not_publish_or_change_input(monkeypatch):
    ctx, before = fixture_context(), fixture_context().compile_result_json
    error = OperationCancelled('late checkpoint')
    for operation, threshold in [('build', 5), ('export', 2)]:
        calls = []
        def checkpoint():
            calls.append(1)
            if len(calls) == threshold:
                raise error
        with no_io_or_frontend(monkeypatch):
            with pytest.raises(OperationCancelled) as caught:
                if operation == 'build':
                    metadata.build_compile_source_catalog(ctx, checkpoint=checkpoint)
                else:
                    metadata.export_compile_metadata(ctx, 'compile_result', checkpoint=checkpoint)
        assert caught.value is error and len(calls) == threshold
        assert ctx.compile_result_json == before


def test_caller_deadline_budget_is_observed_by_metadata_checkpoint(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    budget, ctx = HierarchyQueryBudget(timeout_sec=1), fixture_context()
    calls = []
    def checkpoint():
        calls.append(1)
        if len(calls) == 2:
            clock[0] = 12.0
        budget.checkpoint()
    with pytest.raises(HierarchyBudgetExceeded) as caught:
        build(monkeypatch, ctx, checkpoint=checkpoint)
    assert caught.value.reason == 'timeout_sec'


def test_context_copy_failure_is_not_converted_into_empty_inventory(monkeypatch):
    ctx = fixture_context()
    error = TimeoutError('context metadata unavailable')
    def fail_copy(self):
        raise error
    monkeypatch.setattr(DesignContext, 'compile_result', fail_copy)
    for operation in ('build', 'export'):
        with no_io_or_frontend(monkeypatch):
            with pytest.raises(TimeoutError) as caught:
                if operation == 'build':
                    metadata.build_compile_source_catalog(ctx)
                else:
                    metadata.export_compile_metadata(ctx, 'include_tree')
        assert caught.value is error
