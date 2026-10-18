"""Native-file/query_source controls against an invalidating fake SDK.

Synthetic namespace/typedef facts are handwritten; this is not SDK relation,
source-association, UVM, public six-tool or real compiler acceptance.
"""
from contextlib import nullcontext
from dataclasses import replace
import json
import subprocess
import sys
import threading
from types import SimpleNamespace as NS

import pytest

from src import hierarchy_source_ir as ir
from src import npi_hierarchy_source as native
from src import verdi_npi_backend as npi
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.compile_session_snapshot import CompileSessionSnapshot, FileContentSnapshot
from src.design_context import DesignContext
from src.hierarchy_source_files import query_file_detail
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_metadata import CompileSourceCatalog, CompileSourceFile
from src.hierarchy_source_request import prepare_source_query
from src.hierarchy_source_symbols import SourceSymbol
from src.kdb_anchor import KdbBindingChanged, capture_kdb_anchor
from tests.test_npi_hierarchy_classes import Sdk, Raw, Handle, install, owned


A, B, ALIAS, SITE = '/rtl/shared.sv', '/other/shared.sv', '/captured/rtl_link.sv', '/tb/instances.sv'
ESC = 'top.\\scope.with.dot '
FILE_GAPS = {'npi_uninstantiated_declaration_inventory_unverified',
             'npi_file_variable_inventory_unverified', 'npi_file_import_inventory_unverified',
             'npi_uvm_usage_inventory_unverified'}


def forbidden(*a, **kw):
    pytest.fail('real SDK/frontend/parse/discovery/build/fallback reached')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(native, '_OWNER_LOCK', threading.Lock())
    monkeypatch.setattr(npi, '_import_pynpi', forbidden)
    monkeypatch.setattr(npi, 'parse_compile_log', forbidden)
    monkeypatch.setattr(npi, 'probe_verdi_backend', forbidden)
    monkeypatch.setattr(npi, '_silence_native_stdio', nullcontext)
    monkeypatch.setattr(npi, '_install_shutdown_banner_silencer', lambda: None)
    monkeypatch.setattr(npi, '_NPI_INITIALIZED_IDS', set())
    monkeypatch.setattr(npi, '_NPI_ACTIVE_DESIGNS', {})
    monkeypatch.setattr(npi, '_NPI_LOAD_GENERATIONS', {})
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    previous = sys.getprofile()
    def guard(frame, event, arg):
        if event == 'call':
            path = frame.f_code.co_filename.replace('\\', '/')
            assert not any(part in path for part in ('/pynpi/', '/src/slang_',
                '/src/tb_hierarchy_builder.py', '/src/structural_scanner.py', '/src/source_graph_worker.py')), path
    sys.setprofile(guard)
    try:
        yield
    finally:
        sys.setprofile(previous)


class FileHandle(Handle):
    def def_name(self): return self.scalar('def_name')
    def def_file(self): return self.scalar('def_file')
    def def_line_no(self): return self.scalar('def_line_no')


class FileSdk(Sdk):
    """Six namespaces, eight unique declarations; A has5, B has3."""
    def __init__(self):
        super().__init__()
        def node(path, kind, *, parent=None, file=A, line=1, name=None, **kw):
            return dict(full_name=path, type=kind, parent=parent, file=file,
                        line_no=line, name=name or path.rsplit('.', 1)[-1], **kw)
        self.nodes = {
            'top': node('top', 'npiModule', file=SITE, line=40,
                        def_name='top_def', def_file=A, def_line_no=1),
            'top.u': node('top.u', 'npiModule', parent='top', file=SITE, line=41,
                          def_name='leaf', def_file=B, def_line_no=7),
            'top.bus': node('top.bus', 'npiInterface', parent='top', file=SITE, line=42,
                            def_name='bus_if', def_file=A, def_line_no=12),
            ESC: node(ESC, 'npiGenScope', parent='top', name='\\scope.with.dot ', file=SITE, line=43),
            'p': node('p', 'npiPackage', line=20),
            'p.C': node('p.C', 'npiClassDefn', parent='p', file=B, line=30),
            'T-top': node('T-top', 'npiStructTypespec', parent='top', name='packet_t', line=3),
            'T-pkg': node('T-pkg', 'npiIntTypespec', parent='p', name='packet_t', file=B, line=31),
            'T-esc': node('T-esc', 'npiStructTypespec', parent=ESC, name='\\T::part ', line=4),
        }
        for key in ('T-top', 'T-pkg', 'T-esc'):
            self.nodes[key]['full_name'] = None  # Verified native Typespec possibility.
        self.roots = ['top', 'p']
        self.children = {'top': ['top.u', 'top.bus', ESC], 'p': ['p.C']}
        self.parameters = {'top.u': [('W', 2)]}
        self.typedefs = {'top': ['T-top'], 'p': ['T-pkg'], ESC: ['T-esc']}
        self.low.Typedef = 'Typedef'
        self.util.handle = FileHandle

    def iterate(self, relation, reference):
        if relation != 'Typedef': return super().iterate(relation, reference)
        self.validate(reference)
        path = reference.data['full_name']
        items = self.typedefs.get(path, [])
        raw = Raw(self, dict(relation=relation, path=path, items=list(items), index=0)) if items else None
        if raw is not None: self.iterators.append(raw)
        self.emit(('iterate', relation, path))
        return raw


def catalog():
    return CompileSourceCatalog('compile-identity', tuple(CompileSourceFile(
        'file-' + str(i), path, (ALIAS,) if path == A else (), ('rtl',), (), ('project',), (),
        True, False, False, 'captured') for i, path in enumerate((A, B))), (), 1000, 0)


def context(*, preference='auto', selected_top='top', empty=False):
    result = dict(simulator='vcs', top_modules=['top'], files={'user': [] if empty else [
        {'path': A, 'type': 'module'}, {'path': B, 'type': 'class'}]}, include_tree={}, filelist_tree={})
    records = () if empty else tuple(FileContentSnapshot(path=path, device=1, inode=i,
        size=20, mtime_ns=7, ctime_ns=8, sha256='b' * 64) for i, path in enumerate((A, B), 1))
    return DesignContext(ordered_compile_logs=('/compile.log',), simulator='vcs', selected_top=selected_top,
        backend_preference=preference, compile_result_json=json.dumps(result),
        snapshot=CompileSessionSnapshot(records, True, (), 'c' * 64),
        aliases=() if empty else ((ALIAS, A),), unavailable=(), include_resolutions=(), environment=(),
        identity_sha256='a' * 64, gaps=(), include_resolution_complete=True, metrics=())


def harness(monkeypatch, tmp_path, *, ctx=None):
    sdk, ctx = FileSdk(), ctx or context()
    install(monkeypatch, sdk)
    monkeypatch.setattr(npi, '_import_pynpi', lambda: (sdk.system, NS()))
    directory = tmp_path / 'kdb'
    directory.mkdir()
    (directory / '_libFileMap').write_bytes(b'library')
    (directory / '_stnElabDB').write_bytes(b'elaboration')
    anchor = capture_kdb_anchor(str(directory))
    target = npi.VerdiNpiBackend(NS(find_driver=forbidden, find_loads=forbidden,
                                   find_path=forbidden, get_dynamic_step=forbidden))
    target.bind_compile_context(ctx.compile_result())
    target.bind_kdb_anchor(anchor)
    return target, sdk, ctx, anchor


def assert_closed(sdk):
    assert sdk.global_count == 1 and not any(raw.valid for raw in sdk.raws)


def observed(provider, entry):
    with provider.open_file_symbols(entry, provider.budget) as stream:
        facts = list(stream)
        return facts, stream.complete, stream.has_uvm_import, stream.uses_uvm, set(stream.gaps), stream.work_scope


def test_global_walk_maps_only_definition_file_and_actual_typedef_scope(monkeypatch):
    sdk = FileSdk()
    with owned(monkeypatch, sdk) as p:
        a, complete, imported, used, gaps, work_scope = observed(p, catalog().files[0])
        by_name = {fact.qualified_name: fact for fact in a}
        assert set(by_name) == {'top_def', 'top::packet_t', 'bus_if', ESC + '::\\T::part ', 'p'}
        assert len(a) == 5 and p.budget.scope_count == 6 and p.budget.symbol_count == 8
        assert not complete and imported is used is None
        assert FILE_GAPS <= gaps and work_scope == 'global_namespaces_bounded'
        top = by_name['top_def']
        assert top.location.file == A and top.location.line == 1
        assert top.name == top.qualified_name == 'top_def'
        assert top.declaration_status == 'specialization' and top.scope_id is top.type_id is None
        assert 'npi_definition_scope_identity_unverified' in top.gaps
        typedef = by_name['top::packet_t']
        assert typedef.kind == 'typedef' and typedef.location.line == 3 and typedef.type_id
        top_scope = p.lookup_scope('top', p.budget).fact
        assert typedef.scope_id == top_scope.scope_id
        escaped = by_name[ESC + '::\\T::part ']
        assert escaped.name == '\\T::part '
        assert escaped.scope_id == p.lookup_scope(ESC, p.budget).fact.scope_id
        assert 'npi_typedef_layout_not_projected' in escaped.gaps
    assert_closed(sdk)


def test_target_file_inventory_keeps_only_matching_facts_but_counts_namespace_work(monkeypatch):
    sdk = FileSdk()
    with owned(monkeypatch, sdk) as provider:
        provider._file_query_paths = frozenset((B,))
        facts, complete, _, _, gaps, work = observed(provider, catalog().files[1])
        assert {fact.qualified_name for fact in facts} == {'leaf', 'p.C', 'p::packet_t'}
        assert provider._file_inventory.examined_symbols == 8
        assert len(provider._file_inventory.rows) == provider.budget.symbol_count == 3
        assert provider.budget.scope_count == 6
        assert provider.budget.call_count > 0
        assert not complete and FILE_GAPS <= gaps and work == 'global_namespaces_bounded'
    assert_closed(sdk)


def test_discarded_file_symbols_still_have_a_work_limit_and_cleanup(monkeypatch):
    sdk = FileSdk()
    with owned(monkeypatch, sdk, budget=HierarchyQueryBudget(max_symbols=1)) as provider:
        provider._file_query_paths = frozenset(('/not_observed.sv',))
        with pytest.raises(ir.HierarchyBudgetExceeded, match='symbol_limit'):
            observed(provider, catalog().files[1])
        assert provider.budget.symbol_count == 0
        assert provider.budget.scope_count > 0
        assert provider._file_inventory.closed
    assert_closed(sdk)


@pytest.mark.parametrize('target_first', [False, True])
def test_filtered_symbol_identity_conflict_is_rejected_in_both_orders(monkeypatch, target_first):
    from src.connectivity_ir import SourceLocation
    from src.npi_hierarchy_files import FileInventory
    sdk = FileSdk()
    with owned(monkeypatch, sdk) as provider:
        provider._file_query_paths = frozenset((B,))
        inventory = FileInventory(provider)
        first = SourceSymbol('same-id', 'C', 'p.C', 'class', location=SourceLocation(A, 1))
        second = replace(first, location=SourceLocation(B, 2))
        observations = [(first, False), (second, False)]
        if target_first:
            observations.reverse()
        inventory.generator = (row for row in observations)
        try:
            with pytest.raises(native.NpiHierarchyUnavailable, match='npi_file_symbol_identity_conflict'):
                while inventory.read() is not None:
                    pass
            assert len(inventory.observed_fingerprints) == 1
            assert provider.budget.fact_bytes >= 256 + 4 * len('same-id') + 32
        finally:
            inventory.close()
    assert_closed(sdk)


def test_same_owner_second_file_reuses_only_copied_facts_and_close_drops_inventory(monkeypatch):
    sdk = FileSdk()
    with owned(monkeypatch, sdk) as p:
        a = observed(p, catalog().files[0])[0]
        before = list(sdk.events)
        b = observed(p, catalog().files[1])[0]
        a_again = observed(p, catalog().files[0])[0]
        assert sdk.events == before  # No repeat native read for a copied inventory.
        assert a_again == a
        assert {fact.qualified_name for fact in b} == {'leaf', 'p::packet_t', 'p.C'}
        assert all(fact.location.file == B for fact in b)
        assert all(isinstance(fact, SourceSymbol) for fact, flag in p._file_inventory.rows)
        assert p._file_inventory.exhausted and p.budget.symbol_count == 8
        assert len({fact.symbol_id for fact in a + b}) == 8
        assert a[1].symbol_id != next(f.symbol_id for f in b if f.kind == 'typedef')
    assert p._file_inventory is None and not p.active and p._top_handle is None
    assert_closed(sdk)
    assert a[0].row()['source_file'] == A


def test_file_view_alias_and_renderer_cache_have_explicit_separate_work_cost(monkeypatch):
    sdk = FileSdk()
    with owned(monkeypatch, sdk) as p:
        result = query_file_detail(catalog(), p, path=ALIAS)
        assert result['path'] == A and result['symbol_count_lower_bound'] == 5
        assert result['symbol_count'] is None and not result['symbol_inventory_complete']
        assert result['semantic_work_scope'] == 'global_namespaces_bounded'
        assert result['semantic_status'] == result['coverage_status'] == 'partial'
        assert result['query_metrics']['symbol_count'] == 13  # 8 cache facts + 5 copied file rows.
        assert result['query_metrics']['scope_count'] == 6 and result['query_metrics']['file_count'] == 1
        assert result['modules'] == ['top_def'] and result['interfaces'] == ['bus_if']
        assert all(row['source_file'] != SITE for row in result['symbols'])
    assert_closed(sdk)


@pytest.mark.parametrize('change,gap', [('wrong_parent', 'npi_typedef_binding_unavailable'),
    ('missing_name', 'npi_typedef_binding_unavailable'), ('wrong_kind', 'npi_typedef_kind_unmodeled'),
    ('missing_location', 'npi_file_symbol_ownership_unavailable')])
def test_typedef_unproved_binding_or_ownership_is_gap_not_guessed_member(monkeypatch, change, gap):
    sdk = FileSdk()
    if change == 'wrong_parent': sdk.nodes['T-top']['parent'] = 'p'
    if change == 'missing_name': sdk.nodes['T-top']['name'] = ''
    if change == 'wrong_kind': sdk.nodes['T-top']['type'] = 'npiOpaqueFutureThing'
    if change == 'missing_location': sdk.nodes['T-top']['file'] = None
    with owned(monkeypatch, sdk) as p:
        facts, _, _, _, gaps, _ = observed(p, catalog().files[0])
        assert 'top::packet_t' not in {f.qualified_name for f in facts}
        assert gap in gaps
    assert_closed(sdk)


@pytest.mark.parametrize('conflicting', [False, True])
def test_same_semantic_typedef_id_is_not_counted_twice_or_silently_changed(monkeypatch, conflicting):
    sdk = FileSdk()
    sdk.nodes['T-copy'] = dict(sdk.nodes['T-top'])
    if conflicting: sdk.nodes['T-copy']['line_no'] = 99
    sdk.typedefs['top'].append('T-copy')
    if conflicting:
        with pytest.raises(native.NpiHierarchyUnavailable, match='npi_file_symbol_identity_conflict'):
            with owned(monkeypatch, sdk) as p: observed(p, catalog().files[0])
    else:
        with owned(monkeypatch, sdk) as p:
            facts, _, _, _, gaps, _ = observed(p, catalog().files[0])
            assert len(facts) == 5 and p.budget.symbol_count == 8
            assert 'npi_file_symbol_alias_unverified' in gaps
    assert_closed(sdk)


def test_typedef_identity_separates_real_scope_and_name_components(monkeypatch):
    sdk = FileSdk()
    # Opaque native strings deliberately collide after naive 'scope::name'
    # concatenation; only the actual relationship components are authoritative.
    for parent, name, key in (('a::b', 'c', 'collision-1'), ('a', 'b::c', 'collision-2')):
        sdk.nodes[parent] = dict(type='npiPackage', full_name=parent, name=parent,
                                 parent=None, file=A, line_no=60)
        sdk.nodes[key] = dict(type='npiIntTypespec', full_name=None, name=name,
                             parent=parent, file=A, line_no=61)
        sdk.roots.append(parent)
        sdk.typedefs[parent] = [key]
    with owned(monkeypatch, sdk) as p:
        facts = observed(p, catalog().files[0])[0]
        collided = [f for f in facts if f.kind == 'typedef' and f.qualified_name == 'a::b::c']
        assert len(collided) == 2
        assert collided[0].symbol_id != collided[1].symbol_id
        assert collided[0].type_id != collided[1].type_id
        assert collided[0].scope_id != collided[1].scope_id


def test_same_definition_name_keeps_distinct_observed_specialization_ids(monkeypatch):
    sdk = FileSdk()
    sdk.nodes['top.v'] = dict(sdk.nodes['top.u'], full_name='top.v', name='v', line_no=44)
    sdk.children['top'].append('top.v')
    sdk.parameters['top.v'] = [('W', 4)]
    with owned(monkeypatch, sdk) as p:
        facts = observed(p, catalog().files[1])[0]
        leaves = [f for f in facts if f.kind == 'module']
        assert [f.name for f in leaves] == ['leaf', 'leaf']
        assert [f.parameterization for f in leaves] == [(('W', '2'),), (('W', '4'),)]
        assert leaves[0].symbol_id != leaves[1].symbol_id
        assert all(f.declaration_status == 'specialization' and f.scope_id is f.type_id is None for f in leaves)


def test_positive_uvm_base_usage_does_not_invent_import_or_complete_coverage(monkeypatch):
    sdk = FileSdk()
    sdk.nodes['uvm-base'] = dict(type='npiClassDefn', full_name='uvm_pkg.uvm_object', name='uvm_object')
    sdk.nodes['base-spec'] = dict(type='npiClassTypespec', full_name=None, name='uvm_object', class_defn='uvm-base')
    sdk.nodes['p.C']['extends'] = 'base-spec'
    with owned(monkeypatch, sdk) as p:
        _, complete, imported, used, gaps, _ = observed(p, catalog().files[1])
        assert not complete and imported is None and used is True
        assert FILE_GAPS <= gaps


def test_interrupted_consumer_closes_walk_and_does_not_resume_it_as_complete(monkeypatch):
    sdk = FileSdk()
    with owned(monkeypatch, sdk) as p:
        with p.open_file_symbols(catalog().files[0], p.budget) as stream:
            assert next(stream).name == 'top_def'
        assert p._file_inventory.closed and not p._file_inventory.exhausted
        before = list(sdk.events)
        b, complete, _, _, gaps, _ = observed(p, catalog().files[1])
        assert b == [] and not complete and 'npi_file_inventory_interrupted' in gaps
        assert sdk.events == before
        assert p.lookup_scope('top.u', p.budget).fact.definition_name == 'leaf'
    assert_closed(sdk)


@pytest.mark.parametrize('kind', ['cancel', 'capture', 'kdb', 'timeout'])
def test_primary_typed_getter_failure_survives_unconfirmed_iterator_and_global_cleanup(monkeypatch, kind):
    error = {'cancel': lambda: OperationCancelled('original'), 'capture': lambda: CaptureAborted('input_changed'),
             'kdb': KdbBindingChanged, 'timeout': lambda: TimeoutError('original')}[kind]()
    sdk = FileSdk()
    sdk.release_rc = sdk.global_rc = 0
    def hook(event):
        if event == ('get', None, 'name'): raise error
    sdk.hook = hook
    with pytest.raises(type(error)) as caught:
        with owned(monkeypatch, sdk) as p: observed(p, catalog().files[0])
    assert caught.value is error and sdk.global_count == 1
    assert not p.active and p._file_inventory is None and not p.iterators
    # RC0 leaves native reclamation unconfirmed; never assert freed capsules.


def test_lost_owner_never_releases_capsules_from_replacement_generation(monkeypatch):
    sdk = FileSdk()
    with pytest.raises(native.NpiHierarchyUnavailable, match='npi_loaded_generation_changed'):
        with owned(monkeypatch, sdk) as p:
            with p.open_file_symbols(catalog().files[0], p.budget) as stream:
                next(stream)
                sdk.generation += 1
                next(stream)
    assert sdk.global_count == 0 and not [e for e in sdk.events if e[0] == 'release']
    assert not p.active and p._file_inventory is None and not p.iterators


def test_foreign_thread_close_does_not_touch_active_file_owner(monkeypatch):
    sdk, errors = FileSdk(), []
    with owned(monkeypatch, sdk) as p:
        with p.open_file_symbols(catalog().files[0], p.budget) as stream:
            next(stream)
            inventory, before = p._file_inventory, list(sdk.events)
            def foreign():
                try: p.close()
                except BaseException as exc: errors.append(exc)
            thread = threading.Thread(target=foreign)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive() and len(errors) == 1
            assert isinstance(errors[0], native.NpiHierarchyUnavailable)
            assert sdk.events == before and p.active and p._file_inventory is inventory
            assert next(stream).qualified_name == 'top::packet_t'
    assert_closed(sdk)


@pytest.mark.parametrize('operation,args', [
    ('dump_tb_section', {'section': 'compile_result'}), ('dump_tb_section', {'section': 'include_tree'}),
    ('dump_tb_section', {'section': 'filelist_tree'}), ('get_tb_file_detail', {'path': '/missing/shared.sv'}),
    ('lookup_tb_files', {'basename': 'not-compiled.sv', 'has_module': 'leaf'}),
    ('lookup_tb_files', {'basename': 'shared.sv'}),
])
def test_metadata_nonmember_or_empty_candidate_plan_never_initializes_or_loads(monkeypatch, operation, args):
    sdk = FileSdk()
    target = npi.VerdiNpiBackend(NS())  # Intentionally unbound, source policy allowed for metadata.
    plan = prepare_source_query(context(preference='source'), operation, args)
    assert not plan.needs_provider
    result = target.query_source(plan)
    assert result['semantic_status'] == 'not_run'
    assert 'kdb_binding' not in result and target.kdb_status is None
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0


@pytest.mark.parametrize('section', ['files_full', 'interfaces'])
def test_empty_compile_catalog_exports_do_not_acquire_native_owner(monkeypatch, section):
    target = npi.VerdiNpiBackend(NS())
    plan = prepare_source_query(context(empty=True), 'dump_tb_section', {'section': section})
    assert not plan.needs_provider
    result = target.query_source(plan)
    assert result['data'] == [] and result['export_complete'] and target.kdb_status is None


@pytest.mark.parametrize('bad', ['source_policy', 'compile_mismatch', 'missing_top', 'foreign_catalog', 'invalid_args'])
def test_semantic_prevalidation_rejects_before_native_load(monkeypatch, tmp_path, bad):
    target, sdk, ctx, _ = harness(monkeypatch, tmp_path)
    if bad == 'source_policy': ctx = replace(ctx, backend_preference='source')
    if bad == 'compile_mismatch': target.bind_compile_context({'top_modules': ['different']})
    if bad == 'missing_top': ctx = replace(ctx, selected_top=None)
    plan = prepare_source_query(ctx, 'get_tb_file_detail', {'path': A})
    if bad == 'foreign_catalog': plan = replace(plan, catalog=replace(plan.catalog, context_identity='foreign'))
    if bad == 'invalid_args': plan = replace(plan, arguments_json=json.dumps({'path': A, 'extra': 1}))
    expected = native.NpiHierarchyUnavailable if bad in ('source_policy', 'compile_mismatch') else ValueError
    with pytest.raises(expected): target.query_source(plan)
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0 and not sdk.events


@pytest.mark.parametrize('operation,args', [
    ('get_tb_subtree', {'root': 'top', 'depth': -1, 'count_mode': 'exact'}),
    ('find_tb_instance', {'path': 'top.u'}), ('lookup_tb_files', {'has_module': 'leaf'}),
    ('get_tb_file_detail', {'path': ALIAS}), ('get_tb_class_hierarchy', {}),
    ('dump_tb_section', {'section': 'files_full'}),
])
def test_six_internal_source_queries_use_real_owner_control_and_native_receipt(monkeypatch, tmp_path, operation, args):
    target, sdk, ctx, _ = harness(monkeypatch, tmp_path)
    result = target.query_source(prepare_source_query(ctx, operation, args))
    assert result['facts_origin'] == 'verdi_npi' and result['context_identity_sha256'] == ctx.identity_sha256
    assert result['kdb_binding']['load_status'] == 'loaded'
    assert result['kdb_binding']['source_association'] == 'unverified'
    assert result['kdb_binding']['identity_basis'] == 'bounded_elaboration_maps'
    assert target.kdb_status['load_quality'] == 'clean'
    if operation == 'get_tb_subtree': assert result['total_descendants'] == 3 and result['coverage_status'] == 'complete'
    elif operation == 'find_tb_instance':
        assert result['hits'][0]['module'] == 'leaf' and result['hits'][0]['parameterization'] == {'W': '2'}
    elif operation == 'lookup_tb_files':
        assert [row['path'] for row in result['matches']] == [B] and result['unknown_filter_files'] == 1
        assert result['total'] is None and result['count_lower_bound'] == 1
    elif operation == 'get_tb_file_detail':
        assert result['path'] == A and result['symbol_count_lower_bound'] == 5
        assert result['semantic_work_scope'] == 'global_namespaces_bounded'
    elif operation == 'get_tb_class_hierarchy': assert result['total'] == 1 and not result['class_inventory_complete']
    else:
        assert [row['path'] for row in result['data']] == [A, B]
        assert result['query_metrics']['symbol_count'] == 16  # 8 cached + 8 copied rows.
        assert not result['export_complete']
    assert sdk.init_count == sdk.load_count == 1
    assert_closed(sdk)


@pytest.mark.parametrize('section', ['interfaces', 'component_tree_full', 'class_hierarchy_full'])
def test_semantic_section_exports_share_owner_and_honest_coverage(monkeypatch, tmp_path, section):
    target, sdk, ctx, _ = harness(monkeypatch, tmp_path)
    result = target.query_source(prepare_source_query(ctx, 'dump_tb_section', {'section': section}))
    if section == 'interfaces':
        assert [row['name'] for row in result['data']] == ['bus_if']
        assert result['total'] is None and result['count_lower_bound'] == 1 and not result['export_complete']
    elif section == 'component_tree_full':
        assert result['data']['path'] == 'top' and result['total_descendants'] == 3 and result['export_complete']
    else:
        assert result['data'][0]['qualified_name'] == 'p.C' and result['total'] == 1
        assert not result['class_inventory_complete'] and not result['export_complete']
    assert_closed(sdk)


@pytest.mark.parametrize('limits,reason,initialized', [
    ({'max_bytes': 513}, 'metadata_byte_limit', False),
    ({'max_scopes': 1}, 'scope_limit', True),
    ({'max_symbols': 1}, 'symbol_limit', True),
    ({'max_calls': 1}, 'native_call_limit', True),
])
def test_cumulative_source_plan_limits_do_not_reset_at_native_owner(monkeypatch, tmp_path, limits, reason, initialized):
    target, sdk, ctx, _ = harness(monkeypatch, tmp_path)
    plan = prepare_source_query(ctx, 'get_tb_file_detail', {'path': A}, budget=HierarchyQueryBudget(**limits))
    result = target.query_source(plan)
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == [reason]
    assert sdk.init_count == sdk.load_count == int(initialized)
    if initialized: assert_closed(sdk)
    else: assert sdk.global_count == 0 and result['semantic_status'] == 'not_run'


def test_metadata_after_native_success_does_not_inherit_loaded_receipt(monkeypatch, tmp_path):
    target, sdk, ctx, _ = harness(monkeypatch, tmp_path)
    target.query_source(prepare_source_query(ctx, 'get_tb_file_detail', {'path': A}))
    assert target.kdb_status is not None
    before = list(sdk.events)
    result = target.query_source(prepare_source_query(ctx, 'dump_tb_section', {'section': 'compile_result'}))
    assert result['semantic_status'] == 'not_run' and 'kdb_binding' not in result
    assert target.kdb_status is None and sdk.events == before
    assert_closed(sdk)


@pytest.mark.parametrize('late_phase', ['cleanup', 'final_validation'])
def test_source_query_deadline_includes_global_cleanup_and_final_anchor(monkeypatch, tmp_path, late_phase):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    target, sdk, ctx, anchor = harness(monkeypatch, tmp_path)
    if late_phase == 'cleanup':
        sdk.hook = lambda event: clock.__setitem__(0, 12.0) if event == ('global_cleanup',) else None
    else:
        original = type(anchor).require_current
        def validate(self):
            result = original(self)
            if sdk.global_count: clock[0] = 12.0
            return result
        monkeypatch.setattr(type(anchor), 'require_current', validate)
    result = target.query_source(prepare_source_query(ctx, 'get_tb_file_detail', {'path': A},
        budget=HierarchyQueryBudget(timeout_sec=1)))
    assert result['symbol_count_lower_bound'] == 5 and result['symbol_count'] is None
    assert result['analysis_limits'] == ['timeout_sec'] and result['coverage_status'] == 'partial'
    assert result['kdb_binding']['load_status'] == 'loaded'  # Actual successful fake load, separate from semantics.
    assert_closed(sdk)
