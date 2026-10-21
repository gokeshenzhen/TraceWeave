"""Primitive corruption and artifact capability controls; no frontend needed."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from src.slang_source_codec import decode_source_catalog
from src.source_graph_contract import SemanticScopeSearch


def search(**kwargs):
    return SemanticScopeSearch(purpose='source_catalog', source_files=('/rtl/a.sv',),
                               source_capabilities=('file_declarations',), **kwargs)


def payload():
    return dict(version=1, capabilities=['file_declarations'], source_files=['/rtl/a.sv'],
        semantic_tops=['sg_top'],
        declaration_domain='compilation_declarations_and_elaborated_scopes',
        usage_domain='resolved_types_imports_and_expression_references',
        member_visits=1, scope_visits=1, coordinate_bytes=70, limited=False, stop_reason=None,
        limits=dict(members=4096, scopes=1024, depth=128, coordinate_bytes=2 * 1024 * 1024),
        files={'/rtl/a.sv': dict(symbols=[dict(key='a' * 64, name='A', qualified_name='A',
            kind='module', location=dict(file='/rtl/a.sv', line=7, column=1), scope_key=None,
            declaration_key='a' * 64, type_key=None, parameterization=[], external=False,
            gaps=[], declaration_status='declaration')], inventory_gaps=[], import_gaps=[],
            usage_gaps=[], gaps=[], complete=True, has_uvm_import=False, uses_uvm=False)})


def test_catalog_codec_retains_semantic_location_and_separate_usage_coverage():
    value = payload()
    row = value['files']['/rtl/a.sv']
    row.update(uses_uvm=None, usage_gaps=['source_type_usage_domain_uncovered'],
               gaps=['source_type_usage_domain_uncovered'])
    result = decode_source_catalog(json.dumps(value), search())
    assert result == value and result['files']['/rtl/a.sv']['complete'] is True
    assert result['files']['/rtl/a.sv']['symbols'][0]['location']['line'] == 7


@pytest.mark.parametrize('corruption', [
    'bool_version', 'bool_visits', 'bool_scope', 'extra_field', 'wrong_capability',
    'wrong_files', 'wrong_domain', 'wrong_limits', 'bool_complete', 'false_with_gap',
    'null_without_gap', 'duplicate_symbol', 'foreign_location', 'bool_line',
    'bad_key', 'lost_frontier', 'missing_field', 'duplicate_json_key',
])
def test_catalog_codec_rejects_corruption_before_any_fact_is_published(corruption):
    value = payload()
    row = value['files']['/rtl/a.sv']
    if corruption == 'bool_version': value['version'] = True
    elif corruption == 'bool_visits': value['member_visits'] = True
    elif corruption == 'bool_scope': value['scope_visits'] = True
    elif corruption == 'extra_field': value['guessed'] = True
    elif corruption == 'wrong_capability': value['capabilities'] = ['connectivity']
    elif corruption == 'wrong_files': value['source_files'] = ['/other/a.sv']
    elif corruption == 'wrong_domain': value['usage_domain'] = 'all_text'
    elif corruption == 'wrong_limits': value['limits']['depth'] = 256
    elif corruption == 'bool_complete': row['complete'] = 1
    elif corruption == 'false_with_gap':
        row['usage_gaps'] = row['gaps'] = ['source_type_usage_domain_uncovered']
    elif corruption == 'null_without_gap': row['uses_uvm'] = None
    elif corruption == 'duplicate_symbol':
        value['member_visits'] = 2
        row['symbols'].append(deepcopy(row['symbols'][0]))
    elif corruption == 'foreign_location': row['symbols'][0]['location']['file'] = '/other/a.sv'
    elif corruption == 'bool_line': row['symbols'][0]['location']['line'] = True
    elif corruption == 'bad_key': row['symbols'][0]['declaration_key'] = 'bare-name'
    elif corruption == 'lost_frontier':
        value.update(limited=True, stop_reason='source_catalog_visit_limit')
    elif corruption == 'missing_field': del row['inventory_gaps']
    raw = json.dumps(value)
    if corruption == 'duplicate_json_key': raw = raw.replace('"version": 1', '"version": 1, "version": 1')
    with pytest.raises(ValueError): decode_source_catalog(raw, search())


def test_catalog_scope_identity_and_exact_isolation():
    from tests.test_source_graph_runtime import _request
    from src.source_graph_contract import compare_source_graph_artifact_scopes, compute_source_graph_artifact_key
    request = _request()
    identity = replace(request.artifact_identity,
                       scope=replace(request.artifact_identity.scope, semantic_search=search()))
    key = compute_source_graph_artifact_key(identity).digest
    assert SemanticScopeSearch.from_dict(search().to_dict()) == search()
    for changed in (search(source_max_depth=127), search(source_max_scopes=100), search(source_max_bytes=1000),
                    search(source_aliases=(('/alias/a.sv', '/rtl/a.sv'),))):
        other = replace(identity, scope=replace(identity.scope, semantic_search=changed))
        assert compute_source_graph_artifact_key(other).digest != key
        assert compare_source_graph_artifact_scopes(identity.scope, other.scope).value == (
            'unproven' if changed.source_aliases else 'superset')
    connection = replace(identity.scope, semantic_search=SemanticScopeSearch())
    assert compare_source_graph_artifact_scopes(identity.scope, connection).value == 'unproven'
    assert compare_source_graph_artifact_scopes(connection, identity.scope).value == 'unproven'


@pytest.mark.parametrize('kwargs', [dict(source_version=True), dict(source_version=2),
    dict(source_max_scopes=True), dict(source_max_depth=257), dict(source_max_bytes=True),
    dict(source_aliases=(('same', '/rtl/a.sv'), ('same', '/rtl/b.sv')))])
def test_invalid_catalog_inputs_rejected_before_worker(kwargs):
    with pytest.raises(ValueError): search(**kwargs)


def _catalog_artifact_fixture():
    from src.connectivity_ir import ConnectivityIR, CoverageReport, CoverageStatus
    from src.source_graph_contract import SourceGraphArtifactScopeReceipt
    from tests.test_source_graph_runtime import _request, _scope

    request = _request(scope=replace(_scope(), semantic_search=search()))
    ir = ConnectivityIR(frontend_name='hand_oracle', frontend_version='1.0',
        definitions=(), instances=(), bindings=(), top_instances=(),
        coverage=CoverageReport(status=CoverageStatus.COMPLETE, files_total=1, files_projected=1),
        metadata=(('source_catalog_v1', json.dumps(payload(), sort_keys=True)),))
    receipt = SourceGraphArtifactScopeReceipt(scope=request.artifact_identity.scope,
        coverage_status=ir.coverage.status, gap_codes=())
    return request, ir, receipt


def test_catalog_empty_topology_loads_with_real_top_proof_and_exact_disk_round_trip(tmp_path):
    """The catalog capability needs frontend top evidence, not invented IR instances."""
    from src.source_graph_contract import compute_source_graph_build_key, SourceGraphScopeReceipt
    from src.source_graph_disk_cache import SourceGraphDiskCache, DiskPublishOutcome, DiskValidationOutcome
    from src.source_graph_runtime import SourceGraphRuntime, WorkerBuildResult
    from tests.connectivity_ir_fixtures import build_hand_ir
    from tests.test_source_graph_runtime import _request, ImmediateWorker

    request, ir, receipt = _catalog_artifact_fixture()
    runner = ImmediateWorker()
    runtime = SourceGraphRuntime(runner)
    key = compute_source_graph_build_key(request)
    for scope_receipt in (receipt, SourceGraphScopeReceipt(scope=request.scope,
            coverage_status=ir.coverage.status, gap_codes=())):
        entry = runtime._load_cache_entry(request, key, WorkerBuildResult.ready(ir, scope_receipt))
        assert entry.ir == ir and entry.artifact_scope_receipt == receipt
        assert entry.ir.top_instances == entry.ir.instances == entry.ir.definitions == entry.ir.bindings == ()
        assert entry.query_engine.ir == ir
    store = SourceGraphDiskCache(tmp_path / 'catalog')
    assert store.publish(request.artifact_identity, ir.to_json_bytes(), receipt).outcome is DiskPublishOutcome.PUBLISHED
    hit = store.lookup(request.artifact_identity)
    assert hit.outcome is DiskValidationOutcome.HIT and hit.artifact is not None
    assert hit.artifact.ir_json_bytes == ir.to_json_bytes() and hit.artifact.scope_receipt == receipt
    reloaded = runtime._load_cache_entry(request, key, WorkerBuildResult.ready(hit.artifact.ir, receipt))
    assert reloaded.ir_json_bytes == ir.to_json_bytes()

    ordinary = _request()
    ordinary_ir = build_hand_ir()
    ordinary_receipt = replace(receipt, scope=ordinary.artifact_identity.scope)
    assert runtime._load_cache_entry(ordinary, compute_source_graph_build_key(ordinary),
        WorkerBuildResult.ready(ordinary_ir, ordinary_receipt)).ir.top_instances == ordinary_ir.top_instances
    assert runner.count == 0  # These controls never run a frontend worker.


def test_catalog_loader_rejects_wrong_capability_domain_topology_and_receipts(tmp_path):
    """Exercise both real load boundaries before the frozen frontend services run."""
    import hashlib
    from src.connectivity_ir import CoverageStatus
    from src.source_graph_contract import compute_source_graph_build_key
    from src.source_graph_disk_cache import SourceGraphDiskCache, DiskPublishOutcome, DiskValidationOutcome
    from src.source_graph_runtime import SourceGraphRuntime, WorkerBuildResult
    from tests.connectivity_ir_fixtures import build_hand_ir
    from tests.test_source_graph_disk_cache import _manifest_path, _ir_path
    from tests.test_source_graph_runtime import _request, ImmediateWorker

    request, ir, receipt = _catalog_artifact_fixture()
    runner = ImmediateWorker()
    runtime = SourceGraphRuntime(runner)
    variants = []
    for name in ('wrong_capability', 'foreign_file', 'wrong_top', 'bool_top', 'nonstr_top',
                 'duplicate_top', 'empty_top', 'missing_top', 'unbudgeted_top', 'missing_field'):
        value = payload()
        if name == 'wrong_capability': value['capabilities'] = ['connectivity']
        elif name == 'foreign_file': value['source_files'] = ['/other/a.sv']
        elif name == 'wrong_top': value['semantic_tops'] = ['wrong']
        elif name == 'bool_top': value['semantic_tops'] = [True]
        elif name == 'nonstr_top': value['semantic_tops'] = [123]
        elif name == 'duplicate_top': value['semantic_tops'] = ['sg_top', 'sg_top']
        elif name == 'empty_top': value['semantic_tops'] = ['']
        elif name == 'missing_top': value['semantic_tops'] = []
        elif name == 'unbudgeted_top': value['coordinate_bytes'] = 0
        elif name == 'missing_field': del value['semantic_tops']
        variants.append((name, request, replace(ir,
            metadata=(('source_catalog_v1', json.dumps(value, sort_keys=True)),)), receipt))
    ordinary = _request()
    ordinary_receipt = replace(receipt, scope=ordinary.artifact_identity.scope)
    variants.extend([
        ('missing_metadata', request, replace(ir, metadata=()), receipt),
        ('mixed_topology', request, replace(build_hand_ir(), metadata=ir.metadata), receipt),
        ('ordinary_missing_top', ordinary, replace(ir, metadata=()), ordinary_receipt),
        ('ordinary_catalog', ordinary, ir, ordinary_receipt),
        ('ordinary_mixed_catalog', ordinary, replace(build_hand_ir(), metadata=ir.metadata), ordinary_receipt),
        ('wrong_frontend', request, replace(ir, frontend_name='other_frontend'), receipt),
        ('wrong_frontend_version', request, replace(ir, frontend_version='other_version'), receipt),
        ('wrong_scope_receipt', request, ir, replace(receipt, scope=replace(receipt.scope,
            semantic_search=search(source_max_depth=127)))),
        ('wrong_coverage_receipt', request, ir, replace(receipt,
            coverage_status=CoverageStatus.INCONCLUSIVE, gap_codes=('fixture_coverage_gap',))),
    ])
    store = SourceGraphDiskCache(tmp_path / 'catalog')
    for name, candidate, candidate_ir, candidate_receipt in variants:
        with pytest.raises(ValueError):
            runtime._load_cache_entry(candidate, compute_source_graph_build_key(candidate),
                WorkerBuildResult.ready(candidate_ir, candidate_receipt))
        result = store.publish(candidate.artifact_identity, candidate_ir.to_json_bytes(), candidate_receipt)
        assert result.outcome is DiskPublishOutcome.INVALID_ARTIFACT, (name, result)
    with pytest.raises(ValueError, match='fingerprint'):
        runtime._load_cache_entry(request, compute_source_graph_build_key(request),
            replace(WorkerBuildResult.ready(ir, receipt), ir_fingerprint_sha256='0' * 64))

    # A rehashed persisted payload must still pass capability validation on read.
    assert store.publish(request.artifact_identity, ir.to_json_bytes(), receipt).published
    wrong = payload()
    wrong['capabilities'] = ['connectivity']
    altered = replace(ir, metadata=(('source_catalog_v1', json.dumps(wrong, sort_keys=True)),)).to_json_bytes()
    _ir_path(store, request.artifact_identity).write_bytes(altered)
    manifest_path = _manifest_path(store, request.artifact_identity)
    manifest = json.loads(manifest_path.read_bytes())
    manifest['ir'].update(sha256=hashlib.sha256(altered).hexdigest(), size_bytes=len(altered))
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
    assert store.lookup(request.artifact_identity).outcome is DiskValidationOutcome.IR_IDENTITY_MISMATCH
    assert runner.count == 0


def test_synthetic_unowned_leaf_does_not_poison_captured_inventory(monkeypatch):
    """Pure copied-scope control; fake SDK objects, no frontend/API probe.

    Unknown generated leaves lack all ownership evidence. Real unbound syntax
    and captured-owner unknowns remain gaps; instance/generate traversal must
    retain its supported declaration in every case.
    """
    from pathlib import Path
    import sys
    from types import ModuleType, SimpleNamespace as NS
    from src.slang_source_catalog import collect_source_catalog

    class Symbol:
        def __init__(self, kind, name='', *, offset=None, syntax=None, members=(), **attrs):
            self.kind, self.name = NS(name=kind), name
            self.lexicalPath = self.hierarchicalPath = name
            self.location = None if offset is None else NS(buffer='file', offset=offset)
            self.syntax, self.isType, self.isScope = syntax, False, bool(members)
            self.members = members
            self.__dict__.update(attrs)
        def __iter__(self): return iter(self.members)

    ast_module = ModuleType('pyslang.ast')
    ast_module.Symbol = Symbol
    for name in ('Expression', 'AssertionExpr', 'Constraint', 'Pattern'):
        setattr(ast_module, name, type(name, (), {}))
    ast_module.VisitAction = NS(Skip=1, Advance=0)
    package = ModuleType('pyslang')
    package.__path__, package.ast = [], ast_module
    monkeypatch.setitem(sys.modules, 'pyslang', package)
    monkeypatch.setitem(sys.modules, 'pyslang.ast', ast_module)

    request = search()
    scalar = NS(kind=NS(name='PredefinedIntegerType'), lexicalPath='', isAlias=False,
        isError=False, isArray=False, isStruct=False, isPackedUnion=False, isUnpackedUnion=False,
        isVirtualInterface=False, isCovergroup=False, isTypeRefType=False)
    scalar.canonicalType = scalar
    manager = NS(getAllBuffers=lambda: ['file'], getFullPath=lambda _: Path('/rtl/a.sv'),
        getSourceText=lambda _: 'module A;\nlogic flag;\nendmodule\n',
        getBufferKind=lambda _: NS(name='DesignFile'), getFullyOriginalLoc=lambda loc: loc,
        getFullyExpandedLoc=lambda loc: loc, getFileName=lambda _: '/rtl/a.sv', isMacroLoc=lambda _: False)
    projector = NS(source_manager=manager, options=NS(semantic_search=request,
        source_root=Path('/'), blocking_diagnostic_total=0, exclusions=()))

    for case in ('synthetic', 'unbound_syntax', 'bound_source', 'captured_owner', 'unknown_scope'):
        unknown = Symbol('UnknownCompilerLeaf', 'generated',
                         syntax=object() if case == 'unbound_syntax' else None,
                         offset=10 if case == 'bound_source' else None,
                         isScope=case == 'unknown_scope')
        declaration = Symbol('Definition', 'A', offset=7, syntax=object(), definitionKind=NS(name='Module'))
        variable = Symbol('Variable', 'A.g.flag', offset=16, syntax=object(), type=scalar, initializer=None)
        generate = Symbol('GenerateBlock', 'A.g', offset=10, syntax=object(),
                          isUninstantiated=False, members=(variable, unknown) if case == 'captured_owner' else (variable,))
        body = Symbol('InstanceBody', 'A', members=(generate,))
        top = Symbol('Instance', 'A', offset=7, syntax=object(), definition=declaration, body=body)
        unit = Symbol('CompilationUnit', members=() if case == 'captured_owner' else (unknown,))
        compilation = NS(getDefinitions=lambda: [declaration], getPackages=lambda: [])
        root = NS(compilation=compilation, compilationUnits=[unit], topInstances=[top])
        result = decode_source_catalog(json.dumps(collect_source_catalog(projector, root)), request)
        assert result['semantic_tops'] == ['A']
        row = result['files']['/rtl/a.sv']
        assert {symbol['kind'] for symbol in row['symbols']} == {'module', 'variable'}, (case, row)
        if case == 'synthetic':
            assert row['complete'] and row['gaps'] == [] and row['uses_uvm'] is False, row
        else:
            assert not row['complete'] and row['uses_uvm'] is None, (case, row)
            assert 'source_symbol_domain_uncovered' in row['gaps'], row
