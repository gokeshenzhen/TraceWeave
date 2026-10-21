"""Topology-only codec, actual-owner and finite-budget controls.

Only the frozen independent executor runs these. Observation files precede
assertions, including exceptions for intentionally rejected artifacts.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS
import traceback

import pytest

from src.connectivity_ir import ConnectivityIR, CoverageReport, CoverageStatus
from src.slang_hierarchy_codec import decode_hierarchy, validate_hierarchy_artifact
from src.slang_hierarchy_projection import collect_hierarchy_scopes
from src.source_graph_contract import SemanticScopeSearch


def search(**kwargs):
    return SemanticScopeSearch(roots=('sg_top',), depth=8, purpose='hierarchy',
        hierarchy_mode='complete', hierarchy_aliases=(('/rtl/a.sv', '/rtl/a.sv'),), **kwargs)


def row(path='sg_top', parent=None):
    where = dict(file='/rtl/a.sv', line=1, column=1)
    definition_id = hashlib.sha256(json.dumps(['M', 'Module', where, []], sort_keys=True).encode()).hexdigest()
    return dict(path=path, parent_path=parent, name=path.rsplit('.', 1)[-1], kind='module',
        definition_id=definition_id, definition_name='M', location=dict(where),
        definition_location=dict(file='/rtl/a.sv', line=1, column=1), parameterization=[], gaps=[])


def payload(scope_search=None):
    current = scope_search or search()
    return dict(version=2, capability='instance_hierarchy', domain='selected_top_elaborated_scopes',
        selected_top='sg_top', semantic_tops=['sg_top'],
        search_sha256=hashlib.sha256(json.dumps(current.to_dict(), sort_keys=True,
                                              separators=(',', ':')).encode()).hexdigest(),
        rows=[row(), row('sg_top.child', 'sg_top')], complete_children=['sg_top', 'sg_top.child'],
        member_visits=20, retained_bytes=10000, limited=False, stop_reason=None)


def observe(root, request, action):
    (root / 'request.json').write_text(json.dumps(request, default=str) + '\n')
    try:
        result = action()
        (root / 'result.raw.json').write_text(json.dumps(result, default=str) + '\n')
        decoded = json.loads((root / 'result.raw.json').read_text())
        (root / 'result.decoded.json').write_text(json.dumps(decoded) + '\n')
        return result
    except BaseException as exc:
        (root / 'exception.json').write_text(json.dumps(dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc())) + '\n')
        raise


def test_topology_only_hierarchy_runtime_and_disk_require_actual_top_proof(tmp_path):
    from tests.test_source_graph_runtime import _request, _scope, ImmediateWorker
    from src.source_graph_contract import SourceGraphArtifactScopeReceipt, compute_source_graph_build_key
    from src.source_graph_runtime import SourceGraphRuntime, WorkerBuildResult
    from src.source_graph_disk_cache import SourceGraphDiskCache, DiskPublishOutcome, DiskValidationOutcome
    request = _request(scope=replace(_scope(), semantic_search=search()))
    ir = ConnectivityIR(frontend_name='hand_oracle', frontend_version='1.0', definitions=(),
        instances=(), bindings=(), top_instances=(), coverage=CoverageReport(CoverageStatus.COMPLETE, 1, 1),
        metadata=(('hierarchy_scopes_v2', json.dumps(payload(), sort_keys=True)),))
    receipt = SourceGraphArtifactScopeReceipt(scope=request.artifact_identity.scope,
                                             coverage_status=CoverageStatus.COMPLETE, gap_codes=())
    runtime = SourceGraphRuntime(ImmediateWorker())
    (tmp_path / 'request.json').write_text(json.dumps(request.to_dict()) + '\n')
    try:
        loaded = runtime._load_cache_entry(request, compute_source_graph_build_key(request),
                                          WorkerBuildResult.ready(ir, receipt))
        (tmp_path / 'runtime.raw.json').write_bytes(loaded.ir_json_bytes)
        disk = SourceGraphDiskCache(tmp_path / 'disk')
        published = disk.publish(request.artifact_identity, loaded.ir_json_bytes, receipt)
        (tmp_path / 'publish.raw.json').write_text(json.dumps({'outcome': published.outcome.value}) + '\n')
        hit = disk.lookup(request.artifact_identity)
        (tmp_path / 'lookup.raw.json').write_text(json.dumps({'outcome': hit.outcome.value}) + '\n')
    except BaseException as exc:
        (tmp_path / 'exception.json').write_text(json.dumps(dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc())) + '\n')
        raise
    assert loaded.ir == ir and not ir.instances and not ir.top_instances
    assert published.outcome is DiskPublishOutcome.PUBLISHED
    assert hit.outcome is DiskValidationOutcome.HIT
    assert hit.artifact.ir_json_bytes == ir.to_json_bytes()


@pytest.mark.parametrize('corruption', ['duplicate_json', 'duplicate_path', 'unknown_field',
    'wrong_version', 'wrong_capability', 'wrong_domain', 'wrong_top', 'missing_top_proof',
    'duplicate_top', 'bool_visits', 'bool_bytes', 'bool_line', 'bool_column', 'bool_limited',
    'foreign_location', 'missing_parent', 'cycle', 'foreign_top', 'unknown_complete',
    'lost_frontier', 'unknown_stop', 'wrong_digest', 'unbudgeted_payload', 'unbudgeted_rows',
    'duplicate_parameter', 'definitionless_module', 'container_definition', 'frontier_complete',
    'null_site_no_gap', 'null_definition_no_gap', 'mutated_definition', 'mutated_parameter',
    'dict_complete', 'mixed_complete', 'scalar_complete', 'duplicate_complete', 'oversize_complete', 'scalar_tops'])
def test_hierarchy_codec_rejects_corruption_before_facts(tmp_path, corruption):
    current = search()
    value = payload(current)
    first, second = value['rows']
    if corruption == 'duplicate_path': value['rows'].append(deepcopy(second))
    elif corruption == 'unknown_field': value['guessed'] = True
    elif corruption == 'wrong_version': value['version'] = True
    elif corruption == 'wrong_capability': value['capability'] = 'file_declarations'
    elif corruption == 'wrong_domain': value['domain'] = 'whole_design'
    elif corruption == 'wrong_top': value['selected_top'] = 'elsewhere'
    elif corruption == 'missing_top_proof': value['semantic_tops'] = []
    elif corruption == 'duplicate_top': value['semantic_tops'] *= 2
    elif corruption == 'bool_visits': value['member_visits'] = True
    elif corruption == 'bool_bytes': value['retained_bytes'] = True
    elif corruption == 'bool_line': second['location']['line'] = True
    elif corruption == 'bool_column': second['location']['column'] = True
    elif corruption == 'bool_limited': value['limited'] = 0
    elif corruption == 'foreign_location': second['location']['file'] = '/other.sv'
    elif corruption == 'missing_parent': second['parent_path'] = 'sg_top.missing'
    elif corruption == 'cycle': first['parent_path'] = second['path']
    elif corruption == 'foreign_top': second['path'] = 'other.child'
    elif corruption == 'unknown_complete': value['complete_children'].append('sg_top.absent')
    elif corruption == 'lost_frontier': value['limited'] = True
    elif corruption == 'unknown_stop': value.update(limited=True, stop_reason='guessed')
    elif corruption == 'wrong_digest': value['search_sha256'] = 'b' * 64
    elif corruption == 'unbudgeted_payload': value['retained_bytes'] = 1
    elif corruption == 'unbudgeted_rows': value['member_visits'] = 1
    elif corruption == 'duplicate_parameter': second['parameterization'] = [['W', '8'], ['W', '8']]
    elif corruption == 'definitionless_module': second.update(definition_id=None, definition_name=None)
    elif corruption == 'container_definition': second['kind'] = 'generate'
    elif corruption == 'null_site_no_gap': second['location'] = None
    elif corruption == 'null_definition_no_gap': second['definition_location'] = None
    elif corruption == 'mutated_definition': second['definition_name'] = 'Elsewhere'
    elif corruption == 'mutated_parameter': second['parameterization'] = [['W', '9']]
    elif corruption == 'dict_complete': value['complete_children'] = [{}]
    elif corruption == 'mixed_complete': value['complete_children'] = ['sg_top', 2]
    elif corruption == 'scalar_complete': value['complete_children'] = 'sg_top'
    elif corruption == 'duplicate_complete': value['complete_children'] = ['sg_top', 'sg_top']
    elif corruption == 'oversize_complete': value['complete_children'] = ['sg_top'] * 100
    elif corruption == 'scalar_tops': value['semantic_tops'] = 'sg_top'
    elif corruption == 'frontier_complete':
        current = replace(current, depth=1, hierarchy_mode='bounded')
        value['search_sha256'] = payload(current)['search_sha256']
    raw = json.dumps(value)
    if corruption == 'duplicate_json': raw = raw.replace('"version": 2', '"version": 2, "version": 2')
    with pytest.raises(ValueError):
        observe(tmp_path, {'search': current.to_dict(), 'raw': raw},
                lambda: decode_hierarchy(raw, current, 'sg_top'))


@pytest.mark.parametrize('mutation', ['ordinary_purpose', 'catalog_metadata', 'old_hierarchy', 'topology'])
def test_hierarchy_validator_rejects_mixed_capabilities(tmp_path, mutation):
    from tests.test_source_graph_runtime import _request
    from tests.connectivity_ir_fixtures import build_hand_ir
    scope = replace(_request().artifact_identity.scope, semantic_search=search())
    ir = ConnectivityIR(frontend_name='hand_oracle', frontend_version='1.0', definitions=(),
        instances=(), bindings=(), top_instances=(), coverage=CoverageReport(CoverageStatus.COMPLETE, 1, 1),
        metadata=(('hierarchy_scopes_v2', json.dumps(payload())),))
    if mutation == 'ordinary_purpose': scope = replace(scope, semantic_search=None)
    elif mutation == 'catalog_metadata': ir = replace(ir, metadata=(*ir.metadata, ('source_catalog_v1', '{}')))
    elif mutation == 'old_hierarchy': ir = replace(ir, metadata=(*ir.metadata, ('hierarchy_scopes_v1', '{}')))
    elif mutation == 'topology': ir = replace(build_hand_ir(), metadata=ir.metadata)
    with pytest.raises(ValueError):
        observe(tmp_path, scope.to_dict(), lambda: validate_hierarchy_artifact(ir, scope))


class Native:
    def __init__(self, path, kind='Instance', members=(), name=None):
        self.hierarchicalPath = path
        self.name = name or path.rsplit('.', 1)[-1]
        self.kind = NS(name=kind)
        self.members = list(members)
        self.location = NS(buffer=0, offset=0)
        self.isUninstantiated = False
        self.definition = NS(name='M', definitionKind=NS(name='Module'), location=self.location)
        self.body = self
        self.parameters = []
    def __iter__(self): return iter(self.members)
    def find(self, suffix): return next((item for item in self.members if item.name == suffix), None)


def collect(current, *, extra_kind=None):
    leaf = Native('sg_top.a.b')
    middle = Native('sg_top.a', members=[leaf])
    top = Native('sg_top', members=[middle])
    if extra_kind: top.members.append(Native('sg_top.extra', kind=extra_kind))
    top.members.append(Native('sg_top.signal', kind='Variable'))
    lookup = {item.hierarchicalPath: item for item in (top, middle, leaf)}
    manager = NS(getFullyOriginalLoc=lambda value: value, getFullPath=lambda _: '/rtl/a.sv',
        getFileName=lambda _: '/rtl/a.sv', getSourceText=lambda _: 'module M;\nendmodule\n')
    projector = NS(source_manager=manager, options=NS(semantic_search=current, source_root=None))
    return collect_hierarchy_scopes(projector, NS(topInstances=[top], lookupName=lookup.get))


def test_actual_owner_edges_and_signal_exclusion_complete_the_finite_domain(tmp_path):
    current = search()
    value = observe(tmp_path, current.to_dict(), lambda: collect(current))
    assert not value['limited']
    assert [item['path'] for item in value['rows']] == ['sg_top', 'sg_top.a', 'sg_top.a.b']
    assert [item['parent_path'] for item in value['rows']] == [None, 'sg_top', 'sg_top.a']
    assert value['complete_children'] == ['sg_top', 'sg_top.a', 'sg_top.a.b']
    assert decode_hierarchy(json.dumps(value), current, 'sg_top') == value


@pytest.mark.parametrize('kind', ['CheckerInstance', 'PrimitiveInstance', 'UninstantiatedDef'])
def test_unsupported_actual_scope_keeps_global_inventory_partial(tmp_path, kind):
    current = search()
    value = observe(tmp_path, current.to_dict(), lambda: collect(current, extra_kind=kind))
    assert not value['limited']
    assert value['rows'][0]['gaps'] == ['source_hierarchy_scope_kind_uncovered']
    assert 'sg_top' not in value['complete_children']
    assert all(row['path'] != 'sg_top.extra' for row in value['rows'])
    decode_hierarchy(json.dumps(value), current, 'sg_top')


@pytest.mark.parametrize('limit,reason', [('max_instances', 'source_hierarchy_scope_limit'),
    ('max_members', 'source_hierarchy_member_limit'), ('depth', 'source_hierarchy_depth_limit'),
    ('hierarchy_max_bytes', 'source_hierarchy_byte_limit')])
def test_worker_stops_with_positive_bounded_facts_and_fixed_frontier(tmp_path, limit, reason):
    current = replace(search(), **{limit: {'max_instances': 2, 'max_members': 18,
                                         'depth': 1, 'hierarchy_max_bytes': 5000}[limit]})
    value = observe(tmp_path, current.to_dict(), lambda: collect(current))
    assert value['limited'] and value['stop_reason'] == reason, value
    assert value['selected_top'] == 'sg_top' and value['rows'], value
    assert len(value['rows']) < 3 or limit == 'max_members'
    assert not set(value['complete_children']) == {item['path'] for item in value['rows']}
    decode_hierarchy(json.dumps(value), current, 'sg_top')


def test_hierarchy_identity_keeps_modes_ranges_aliases_limits_and_capabilities_separate(tmp_path, monkeypatch):
    from tests.test_source_graph_runtime import _request
    from src.source_graph_contract import compare_source_graph_artifact_scopes
    scope = replace(_request().artifact_identity.scope, semantic_search=search())
    assert SemanticScopeSearch.from_dict(search().to_dict()) == search()
    for changed in (replace(search(), max_members=5000), replace(search(), hierarchy_max_bytes=100000)):
        other = replace(scope, semantic_search=changed)
        from src.source_graph_contract import compute_source_graph_artifact_key
        identity = replace(_request().artifact_identity, scope=scope)
        assert compute_source_graph_artifact_key(replace(identity, scope=other)).digest != compute_source_graph_artifact_key(identity).digest
        assert compare_source_graph_artifact_scopes(scope, other).value == 'superset'
    for changed in (replace(search(), depth=9), replace(search(), hierarchy_mode='bounded'),
                    replace(search(), hierarchy_aliases=(('/alias/a.sv', '/rtl/a.sv'),)),
                    SemanticScopeSearch(roots=('sg_top',), depth=2)):
        assert compare_source_graph_artifact_scopes(scope, replace(scope, semantic_search=changed)).value == 'unproven'
    with pytest.raises(ValueError):
        SemanticScopeSearch(roots=('sg_top.' + '.'.join('a' for _ in range(64)),), depth=2, purpose='hierarchy')
    with pytest.raises(ValueError):
        SemanticScopeSearch(roots=('sg_top',), depth=3)  # connectivity stays tight

    # Bind runtime connectivity exclusions do not describe this strict native
    # scope-inventory capability. Exercise real planning from the same copied
    # global compile evidence, rather than filtering a provider's output gaps.
    from src import prepared_source_connectivity as planner
    from src.source_graph_adapter import SOURCE_GRAPH_ADAPTER_VERSION, _empty_digest_metrics
    from src.source_graph_contract import SourceGraphArtifactScopeReceipt
    original = replace(_request().identity.compile_inputs,
        ordered_inputs=('tests/fixtures/source_graph_frontend/hand_connectivity.sv', '/rtl/bind_aux.sv'),
        ordered_tops=('bind_top', 'sg_top'))
    context = NS(selected_top='sg_top', ordered_compile_logs=('/rtl/compile.log',),
        compile_result=lambda: {}, snapshot=object(), identity_sha256='f' * 64)
    config = NS(frontend_version='1.0', runtime_plusarg_allowlist=frozenset())
    compile_snapshot = 'e' * 64

    def invoke(label, current_search, manifest, global_exclusions, *, base=None):
        directory = tmp_path / label
        directory.mkdir()
        saved = []
        def action():
            plan = planner.build_prepared_source_plan(context=context, operation='driver',
                args={'signal_path': 'sg_top.__scope_anchor'}, config=config,
                search=current_search, base=base)
            saved.append(plan)
            return {'request': plan.request.to_dict() if plan.request is not None else None,
                    'receipt': plan.receipt.to_dict()}
        observe(directory, {'global_manifest': manifest.to_dict(),
                            'global_exclusions': global_exclusions,
                            'purpose': current_search.purpose if current_search is not None else 'connectivity',
                            'based': base is not None}, action)
        return saved[0]

    for label, manifest, global_exclusions in (
            ('bind_only', original, ('bind_semantics',)),
            ('mixed_barriers', replace(original, options_complete=False),
             ('bind_semantics', 'opaque_vhdl_boundary', 'protected_region'))):
        def copied_manifest(*args, **kwargs):
            return (manifest, (), global_exclusions, 'miss_reused_compile_session', compile_snapshot,
                    _empty_digest_metrics())
        monkeypatch.setattr(planner, '_compile_manifest', copied_manifest)
        expected_global = tuple(sorted(set(global_exclusions) |
            ({'compile_manifest_incomplete'} if not manifest.complete else set())))
        plans = {}
        for purpose, current_search in (
                ('hierarchy', search()),
                ('source_catalog', SemanticScopeSearch(purpose='source_catalog',
                    source_files=('/rtl/a.sv',), source_capabilities=('file_declarations',))),
                ('connectivity', None)):
            plan = invoke(label + '_' + purpose, current_search, manifest, global_exclusions)
            plans[purpose] = plan
            expected = tuple(code for code in expected_global if code != 'bind_semantics') if purpose == 'hierarchy' else expected_global
            assert plan.request.identity.compile_inputs is manifest
            assert plan.request.identity.compile_inputs.fingerprint == original.fingerprint
            assert plan.request.identity.compile_inputs.ordered_inputs == original.ordered_inputs
            assert plan.request.identity.compile_inputs.ordered_options == original.ordered_options
            assert plan.request.identity.compile_inputs.ordered_tops == original.ordered_tops
            assert plan.request.artifact_identity.compile_snapshot_sha256 == compile_snapshot
            assert plan.request.artifact_identity.adapter_version == SOURCE_GRAPH_ADAPTER_VERSION
            assert plan.request.scope.coverage_boundary.objective_exclusions == expected
            assert plan.request.artifact_identity.scope.coverage_boundary.objective_exclusions == expected
            assert plan.receipt.objective_exclusions == expected
            if expected:
                with pytest.raises(ValueError):
                    SourceGraphArtifactScopeReceipt(plan.request.artifact_identity.scope, CoverageStatus.COMPLETE)
            else:
                receipt = SourceGraphArtifactScopeReceipt(plan.request.artifact_identity.scope, CoverageStatus.COMPLETE)
                assert receipt.reuse_for(plan.request.artifact_identity.scope).complete_for_request
                for native_gap in ('frontend_diagnostic_unknownmodule', 'source_hierarchy_member_limit',
                                   'source_hierarchy_facts_incomplete'):
                    partial = SourceGraphArtifactScopeReceipt(plan.request.artifact_identity.scope,
                        CoverageStatus.INCONCLUSIVE, (native_gap,))
                    assert not partial.reuse_for(plan.request.artifact_identity.scope).complete_for_request
                    with pytest.raises(ValueError):
                        SourceGraphArtifactScopeReceipt(plan.request.artifact_identity.scope,
                            CoverageStatus.COMPLETE, (native_gap,))
        # A based plan must classify the inherited compile exclusions too,
        # while retaining its exact original source object and global receipt.
        def no_manifest_rebuild(*args, **kwargs):
            raise AssertionError('based capability planning rebuilt the global manifest')
        monkeypatch.setattr(planner, '_compile_manifest', no_manifest_rebuild)
        base = plans['connectivity']
        based = invoke(label + '_based_hierarchy', search(), manifest, global_exclusions, base=base)
        expected_hierarchy = tuple(code for code in expected_global if code != 'bind_semantics')
        assert based.request.identity is base.request.identity
        assert based.request.scope.coverage_boundary.objective_exclusions == expected_hierarchy
        assert based.request.artifact_identity.scope.coverage_boundary.objective_exclusions == expected_hierarchy
        assert based.receipt.objective_exclusions == expected_hierarchy
        assert base.receipt.objective_exclusions == expected_global
        assert base.request.scope.coverage_boundary.objective_exclusions == expected_global
        catalog_based = invoke(label + '_catalog_based_hierarchy', search(), manifest,
                               global_exclusions, base=plans['source_catalog'])
        assert catalog_based.request.identity is plans['source_catalog'].request.identity
        assert catalog_based.receipt.objective_exclusions == expected_hierarchy
        again = invoke(label + '_hierarchy_based_hierarchy', search(), manifest,
                       global_exclusions, base=plans['hierarchy'])
        assert again.request.identity is plans['hierarchy'].request.identity
        assert again.receipt.objective_exclusions == expected_hierarchy
        legacy_request = replace(base.request,
            scope=replace(base.request.scope, semantic_search=None),
            artifact=replace(base.request.artifact_identity,
                scope=replace(base.request.artifact_identity.scope, semantic_search=None)))
        legacy = replace(base, request=legacy_request)
        legacy_hierarchy = invoke(label + '_legacy_based_hierarchy', search(), manifest,
                                  global_exclusions, base=legacy)
        legacy_connectivity = invoke(label + '_legacy_based_connectivity', None, manifest,
                                     global_exclusions, base=legacy)
        assert legacy_hierarchy.request.identity is legacy_connectivity.request.identity is base.request.identity
        assert legacy_hierarchy.receipt.objective_exclusions == expected_hierarchy
        assert legacy_connectivity.receipt.objective_exclusions == expected_global
        # The classified hierarchy receipt cannot be promoted to any other
        # capability. A fresh call above retains the global bind exclusion.
        for purpose, current_search in (
                ('connectivity', None), ('source_catalog', plans['source_catalog'].request.scope.semantic_search),
                ('trace', SemanticScopeSearch(purpose='trace')),
                ('packed_layout', SemanticScopeSearch(purpose='packed_layout'))):
            blocked = invoke(label + '_reverse_' + purpose, current_search, manifest,
                             global_exclusions, base=plans['hierarchy'])
            assert blocked.request is None
            assert blocked.receipt.status.value == 'blocked'
            assert blocked.receipt.blocker.code == 'prepared_base_capability_mismatch'
            assert blocked.receipt.blocker.stage == 'compile_manifest'


@pytest.mark.parametrize('calls', [0, 90000, 99998])
def test_source_catalog_planning_reserves_remaining_calls_for_copy_and_render(monkeypatch, calls):
    from src import slang_hierarchy_source as source
    from src.hierarchy_source_ir import HierarchyQueryBudget
    budget = HierarchyQueryBudget()
    budget.call_count = calls
    monkeypatch.setattr(source, 'build_prepared_source_plan', lambda **kwargs: kwargs['search'])
    context = NS(selected_top='sg_top', aliases=(('/rtl/a.sv', '/rtl/a.sv'),))
    catalog = NS(files=(NS(path='/rtl/a.sv'),))
    file = source.build_instance_plan(context, 'dump_tb_section', {'section': 'files_full'},
                                     None, budget, catalog=catalog)
    classes = source.build_instance_plan(context, 'get_tb_class_hierarchy', {},
                                        None, budget, catalog=catalog)
    assert file.max_members == classes.max_members == min(50000, (100000 - calls) // 2)
    assert file.source_max_bytes == classes.source_max_bytes == 2 * 1024 * 1024
    assert file.source_max_scopes == classes.source_max_scopes == 1024
    assert file.source_capabilities == ('file_declarations',)
    assert classes.source_capabilities == ('class_inheritance', 'file_declarations')


def test_expanded_catalog_member_ceiling_does_not_expand_connectivity_or_default_work():
    current = SemanticScopeSearch(purpose='source_catalog', source_files=('/rtl/a.sv',),
                                  source_capabilities=('file_declarations',), max_members=50000)
    assert SemanticScopeSearch.from_dict(current.to_dict()) == current
    assert SemanticScopeSearch().max_members == 4096
    assert replace(current, max_members=100000).max_members == 100000
    for value in (True, 0, 100001):
        with pytest.raises(ValueError): replace(current, max_members=value)
    with pytest.raises(ValueError): SemanticScopeSearch(max_members=4097)


@pytest.mark.parametrize('kind', ['limited', 'unsupported'])
def test_worker_ir_scope_receipt_and_public_counts_share_the_hierarchy_frontier(tmp_path, monkeypatch, kind):
    from src import slang_hierarchy_projection as collector
    from src import source_graph_worker as worker
    from src.slang_connectivity_projector import ProjectionOptions, project_slang_design
    from src.source_graph_contract import SourceGraphArtifactScopeReceipt
    from src.slang_hierarchy_source import SlangHierarchyProvider
    from src.hierarchy_source_ir import HierarchyQueryBudget
    from src.hierarchy_source_service import HierarchySourceService
    from tests.test_source_graph_runtime import _request, _scope
    current = search()
    value = payload(current)
    if kind == 'limited':
        value.update(limited=True, stop_reason='source_hierarchy_member_limit')
        value['complete_children'] = ['sg_top.child']
    else:
        value['rows'][0]['gaps'] = ['source_hierarchy_scope_kind_uncovered']
        value['complete_children'] = ['sg_top.child']
    monkeypatch.setattr(collector, 'collect_hierarchy_scopes', lambda *args: value)
    request = _request(scope=replace(_scope(), semantic_search=current)).artifact_build_request
    options = ProjectionOptions(semantic_search=current, files_total=1, files_projected=1)
    projection = project_slang_design(root=object(), source_manager=None, frontend_version='1.0', options=options)
    session = worker.SemanticFrontendSession(driver=NS(sourceManager=None), compilation=object(), root=object(),
        diagnostic_payload=dict(total=0, blocking_error_count=0, items=[], items_truncated=False), frontend_version='1.0')
    monkeypatch.setattr(worker, 'project_slang_design', lambda **kwargs: projection)
    (tmp_path / 'request.json').write_text(json.dumps(request.to_dict()) + '\n')
    try:
        raw, _, scope_receipt, projection_receipt = worker.project_semantic_frontend_session(session, request)
        (tmp_path / 'worker.ir.raw.json').write_bytes(raw)
        (tmp_path / 'scope.raw.json').write_text(json.dumps(scope_receipt.to_dict()) + '\n')
        ir = ConnectivityIR.from_json_bytes(raw)
        budget = HierarchyQueryBudget()
        entry = NS(ir=ir, build_key=NS(digest='a' * 64), artifact_identity=request.identity)
        context = NS(selected_top='sg_top', snapshot=NS(records=[NS(path='/rtl/a.sv')]),
                     aliases=current.hierarchy_aliases, unavailable=())
        provider = SlangHierarchyProvider(entry, context, budget)
        result = HierarchySourceService(provider).subtree(depth=-1, count_mode='exact', budget=budget)
        (tmp_path / 'public.raw.json').write_text(json.dumps(result) + '\n')
    except BaseException as exc:
        (tmp_path / 'exception.json').write_text(json.dumps(dict(type=type(exc).__name__, message=str(exc),
            traceback=traceback.format_exc())) + '\n')
        raise
    assert ir.coverage.status is scope_receipt.coverage_status is CoverageStatus.INCONCLUSIVE
    assert projection_receipt['projection_gap_count'] == 1
    assert result['node'] is not None and result['total_descendants'] is None
    assert result['count_lower_bound'] == 1 and result['coverage_status'] == 'partial'
    assert not provider.global_complete
    if kind == 'limited': assert provider.analysis_limits == ('native_call_limit',)
    # Forging a clean outer coverage receipt may never admit this payload.
    forged = replace(ir, coverage=CoverageReport(CoverageStatus.COMPLETE, 1, 1))
    with pytest.raises(ValueError): validate_hierarchy_artifact(forged, request.scope)


@pytest.mark.parametrize('budget_kind', ['bytes', 'members'])
def test_no_actual_top_proof_is_an_explicit_unstarted_budget_blocker(tmp_path, budget_kind):
    from src.slang_hierarchy_projection import HierarchyProjectionLimit
    from src.source_graph_worker import _projection_blocker_code
    current = replace(search(), **({'hierarchy_max_bytes': 1} if budget_kind == 'bytes' else {'max_members': 1}))
    if budget_kind == 'members':
        # Two real top observations exhaust the cap before selected-top proof.
        root = NS(topInstances=[Native('other'), Native('sg_top')], lookupName=lambda _: None)
        action = lambda: collect_hierarchy_scopes(NS(options=NS(semantic_search=current, source_root=None)), root)
    else:
        action = lambda: collect(current)
    with pytest.raises(HierarchyProjectionLimit) as error:
        observe(tmp_path, current.to_dict(), action)
    assert _projection_blocker_code(error.value) == ('source_hierarchy_byte_limit' if budget_kind == 'bytes'
                                                     else 'source_hierarchy_member_limit')


def test_critical_hierarchy_frontier_survives_full_diagnostic_gap_cap(tmp_path, monkeypatch):
    from src import slang_hierarchy_projection as collector
    from src.slang_connectivity_projector import ProjectionOptions, ProjectionDiagnostic, project_slang_design
    from tests.test_source_graph_runtime import _request
    current = search()
    value = payload(current)
    value.update(limited=True, stop_reason='source_hierarchy_member_limit')
    value['rows'][0]['gaps'] = ['source_hierarchy_scope_kind_uncovered']
    value['complete_children'] = ['sg_top.child']
    monkeypatch.setattr(collector, 'collect_hierarchy_scopes', lambda *args: value)
    diagnostics = tuple(ProjectionDiagnostic(code='Controlled' + str(index),
                        message='controlled diagnostic', severity='Error') for index in range(256))
    result = project_slang_design(root=object(), source_manager=None, frontend_version='1.0',
        options=ProjectionOptions(semantic_search=current, files_total=1, files_projected=1,
            diagnostics=diagnostics, diagnostic_total=256, blocking_diagnostic_total=256))
    (tmp_path / 'ir.raw.json').write_bytes(result.ir.to_json_bytes())
    (tmp_path / 'receipt.raw.json').write_text(json.dumps(result.receipt.to_dict()) + '\n')
    scope = replace(_request().artifact_identity.scope, semantic_search=current)
    assert validate_hierarchy_artifact(result.ir, scope)
    assert result.ir.coverage.status is CoverageStatus.INCONCLUSIVE
    assert result.ir.coverage.blocking_diagnostic_count == result.ir.coverage.diagnostic_count == 256
    assert result.receipt.projection_gap_count == 257 and result.receipt.projection_gaps_truncated
    assert len(result.ir.coverage.gaps) == 257  #256 details plus the existing truncation marker
    assert result.ir.coverage.gaps[-1].code == 'projection_gaps_truncated'
    # The priority frontier remains under the tightest one-detail envelope.
    single = project_slang_design(root=object(), source_manager=None, frontend_version='1.0',
        options=ProjectionOptions(semantic_search=current, files_total=1, files_projected=1,
            diagnostics=diagnostics, max_recorded_gaps=1))
    (tmp_path / 'single.ir.raw.json').write_bytes(single.ir.to_json_bytes())
    assert validate_hierarchy_artifact(single.ir, scope)
    assert [gap.code for gap in single.ir.coverage.gaps] == [
        'source_hierarchy_member_limit', 'projection_gaps_truncated']
    assert single.receipt.projection_gaps_truncated
    assert single.receipt.projection_gap_count > len(single.ir.coverage.gaps)


def test_unsupported_scope_gap_mutation_obeys_retained_byte_frontier(tmp_path):
    baseline = collect(search())
    # Fixed envelope, actual top evidence, one20-byte coordinate buffer/two
    # newline entries, and two copied rows fit exactly before the unsupported
    # member is reached. The new gap itself needs additional retained bytes.
    required = 512 + 64 + 4 * len(json.dumps('sg_top')) + 20 + 512 + 2 * 64
    required += sum(512 + 4 * len(json.dumps(row, ensure_ascii=True, sort_keys=True))
                    for row in baseline['rows'][:2])
    current = replace(search(), hierarchy_max_bytes=required)
    value = observe(tmp_path, current.to_dict(), lambda: collect(current, extra_kind='CheckerInstance'))
    assert value['limited'] and value['stop_reason'] == 'source_hierarchy_byte_limit'
    assert len(value['rows']) == 2 and value['rows'][0]['gaps'] == []
    assert value['retained_bytes'] == required
    decode_hierarchy(json.dumps(value), current, 'sg_top')
