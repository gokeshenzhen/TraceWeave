"""Controlled valid-codec/runtime oracles for semantic reuse, saved before assert.

No synthetic SDK interfaces: these are immutable artifact/query contract tests.
Native coverage remains in all existing hierarchy/catalog/identity cases.
"""
import asyncio
from dataclasses import replace
import json
import hashlib
import traceback
from threading import Event
from types import SimpleNamespace as NS

import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.connectivity_ir import ConnectivityIR, CoverageReport, CoverageStatus, CoverageGap
from src.hierarchy_source_ir import HierarchyQueryBudget, HierarchyBudgetExceeded
from src.slang_hierarchy_source import SlangHierarchyProvider
from src.slang_source_provider import SlangSourceProvider
from src.source_graph_contract import SourceGraphArtifactScopeReceipt, compute_source_graph_build_key
from src.source_graph_runtime import SourceGraphRuntime, WorkerBuildResult
from src.source_graph_disk_cache import SourceGraphDiskCache
from tests.test_source_graph_runtime import _request, _scope, ImmediateWorker, ControlledWorker
from tests.test_slang_hierarchy_catalog import search as hierarchy_search, payload as hierarchy_payload, row, observe
from tests.test_slang_source_catalog import search as catalog_search, payload as catalog_payload


def saved_outcome(root, label, outcome):
    value = dict(status=outcome.status.value, build_key=outcome.build_key.to_dict(),
        blocker=vars(outcome.blocker) if outcome.blocker is not None else None,
        metrics=vars(outcome.metrics), scope_match=vars(outcome.scope_match) if outcome.scope_match else None)
    (root / (label + '.outcome.raw.json')).write_text(json.dumps(value, default=str) + '\n')
    decoded = json.loads((root / (label + '.outcome.raw.json')).read_text())
    (root / (label + '.outcome.decoded.json')).write_text(json.dumps(decoded) + '\n')
    if outcome.entry is not None:
        (root / (label + '.ir.raw.json')).write_bytes(outcome.entry.ir_json_bytes)
        (root / (label + '.receipt.raw.json')).write_text(json.dumps(
            outcome.entry.artifact_scope_receipt.to_dict()) + '\n')


def context():
    return NS(selected_top='sg_top', identity_sha256='c' * 64,
        snapshot=NS(records=(NS(path='/rtl/a.sv'),)),
        aliases=(('/rtl/a.sv', '/rtl/a.sv'),), unavailable=())


def fixture(kind='hierarchy', *, low=False, limited=False, large=False, root=None, label='runtime'):
    if kind == 'hierarchy':
        search = hierarchy_search(max_instances=128 if low else 5000,
            max_members=4096 if low else 50000, hierarchy_max_bytes=349525 if low else 2097152)
        value = hierarchy_payload(search)
        if large:
            value['rows'] = [row()] + [row(f'sg_top.child{index}', 'sg_top') for index in range(100)]
            value['complete_children'] = sorted(item['path'] for item in value['rows'])
            value.update(member_visits=49000, retained_bytes=1000000)
        if limited:
            value.update(limited=True, stop_reason='source_hierarchy_member_limit', complete_children=[])
        metadata = 'hierarchy_scopes_v2'
        reason = 'source_hierarchy_member_limit'
    else:
        search = catalog_search(max_members=4096 if low else 50000,
            source_max_scopes=128 if low else 1024, source_max_bytes=349525 if low else 2097152)
        value = catalog_payload()
        value['limits'].update(members=search.max_members, scopes=search.source_max_scopes,
                               coordinate_bytes=search.source_max_bytes)
        if large:
            value.update(member_visits=49000, scope_visits=1000, coordinate_bytes=1000000)
        if limited:
            reason = 'source_catalog_visit_limit'
            value.update(limited=True, stop_reason=reason)
            file = value['files']['/rtl/a.sv']
            file.update(inventory_gaps=[reason], import_gaps=[reason], usage_gaps=[reason],
                        gaps=[reason], complete=False, has_uvm_import=None, uses_uvm=None)
        metadata = 'source_catalog_v1'
        reason = 'source_catalog_visit_limit'
    request = _request(label=label, scope=replace(_scope(), semantic_search=search))
    gaps = ((CoverageGap(code=reason, message='bounded producer', impact=CoverageStatus.INCONCLUSIVE),)
            if limited else ())
    ir = ConnectivityIR(frontend_name='hand_oracle', frontend_version='1.0', definitions=(),
        instances=(), bindings=(), top_instances=(),
        coverage=CoverageReport(CoverageStatus.INCONCLUSIVE if limited else CoverageStatus.COMPLETE,
                                1, 1, gaps=gaps),
        metadata=((metadata, json.dumps(value, sort_keys=True)),))
    receipt = SourceGraphArtifactScopeReceipt(request.artifact_identity.scope, ir.coverage.status,
                                             (reason,) if limited else ())
    ready = WorkerBuildResult.ready(ir, receipt)
    if root is not None:
        stem = 'producer-' + hashlib.sha256(ready.ir_json_bytes).hexdigest()[:16]
        (root / (stem + '.request.json')).write_text(json.dumps(request.to_dict()) + '\n')
        (root / (stem + '.ir.raw.json')).write_bytes(ready.ir_json_bytes)
        (root / (stem + '.receipt.raw.json')).write_text(json.dumps(receipt.to_dict()) + '\n')
    return request, ready


def load(request, result, root):
    (root / 'load.request.json').write_text(json.dumps(request.to_dict()) + '\n')
    (root / 'load.ir.raw.json').write_bytes(result.ir_json_bytes)
    (root / 'load.receipt.raw.json').write_text(json.dumps(result.scope_receipt.to_dict()) + '\n')
    try:
        return SourceGraphRuntime(ImmediateWorker())._load_cache_entry(
            request, compute_source_graph_build_key(request), result)
    except BaseException as exc:
        (root / 'load.exception.json').write_text(json.dumps(dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc())) + '\n')
        raise


def consumer(request, *, low):
    current = request.scope.semantic_search
    if current.purpose == 'hierarchy':
        changed = replace(current, max_instances=128 if low else 5000,
            max_members=4096 if low else 50000, hierarchy_max_bytes=349525 if low else 2097152)
    else:
        changed = replace(current, max_members=4096 if low else 50000,
            source_max_scopes=128 if low else 1024, source_max_bytes=349525 if low else 2097152)
    return _request(scope=replace(request.scope, semantic_search=changed))


@pytest.mark.parametrize('kind', ['hierarchy', 'catalog'])
@pytest.mark.parametrize('producer_low', [False, True])
def test_cross_budget_memory_registration_and_prepared_share_original_producer(tmp_path, kind, producer_low):
    request, ready = fixture(kind, low=producer_low, root=tmp_path)
    asked = consumer(request, low=not producer_low)
    worker = ImmediateWorker([ready])
    runtime = SourceGraphRuntime(worker)
    async def action():
        first = await runtime.prepare(request)
        saved_outcome(tmp_path, 'first', first)
        second = await runtime.prepare(asked)
        saved_outcome(tmp_path, 'second', second)
        prepared = await runtime.lookup_prepared(asked)
        registered = SourceGraphRuntime(ImmediateWorker())
        published = await registered.publish_prepared(request, ready)
        registered_hit = await registered.lookup_prepared(asked)
        observed = dict(first=first.entry.build_key.digest, second=second.entry.build_key.digest,
            builds=worker.count, current_builds=second.metrics.actual_build_count,
            same_entry=first.entry is second.entry is prepared,
            registered=published and registered_hit is not None,
            producer_search=second.entry.artifact_identity.scope.semantic_search.to_dict(),
            raw_unchanged=second.entry.ir_json_bytes == ready.ir_json_bytes,
            cache_bytes=second.entry.cache_bytes, raw_bytes=second.entry.ir_bytes)
        return observed
    actual = observe(tmp_path, dict(producer=request.to_dict(), consumer=asked.to_dict()),
                     lambda: asyncio.run(action()))
    assert actual['same_entry'] and actual['registered'] and actual['builds'] == 1 and actual['current_builds'] == 0
    assert actual['first'] == actual['second'] == compute_source_graph_build_key(request).digest
    assert actual['producer_search'] == request.scope.semantic_search.to_dict() and actual['raw_unchanged']
    assert actual['cache_bytes'] > actual['raw_bytes']


@pytest.mark.parametrize('kind', ['hierarchy', 'catalog'])
def test_exact_disk_loaded_producer_then_cross_budget_memory_reuse(tmp_path, kind):
    request, ready = fixture(kind, root=tmp_path)
    asked = consumer(request, low=True)
    store = SourceGraphDiskCache(tmp_path / 'disk')
    async def action():
        original = SourceGraphRuntime(ImmediateWorker([ready]), disk_cache=store)
        built = await original.prepare(request)
        saved_outcome(tmp_path, 'built', built)
        worker = ImmediateWorker()
        restored = SourceGraphRuntime(worker, disk_cache=store)
        hit = await restored.prepare(request)
        saved_outcome(tmp_path, 'disk', hit)
        reused = await restored.prepare(asked)
        saved_outcome(tmp_path, 'reused', reused)
        exact_miss = store.lookup(asked.artifact_identity)
        observed = dict(tier=hit.metrics.cache_tier.value, builds=worker.count,
            same_entry=hit.entry is reused.entry, producer_id=hit.entry.build_key.digest,
            original_id=built.entry.build_key.digest, disk_consumer=exact_miss.outcome.value,
            raw_unchanged=reused.entry.ir_json_bytes == ready.ir_json_bytes)
        return observed
    actual = observe(tmp_path, dict(producer=request.to_dict(), consumer=asked.to_dict()),
                     lambda: asyncio.run(action()))
    assert actual['tier'] == 'disk' and actual['builds'] == 0 and actual['same_entry']
    assert actual['producer_id'] == actual['original_id'] and actual['raw_unchanged']
    assert actual['disk_consumer'] != 'hit'


@pytest.mark.parametrize('kind', ['hierarchy', 'catalog'])
def test_large_historical_work_does_not_charge_small_current_fact_query(tmp_path, monkeypatch, kind):
    request, ready = fixture(kind, large=True, root=tmp_path)
    entry = load(request, ready, tmp_path)
    budget = HierarchyQueryBudget(max_calls=4, max_scopes=2, max_bytes=4096)
    def forbidden(*args, **kwargs):
        raise AssertionError('a warm reader must not decode the whole producer again')
    monkeypatch.setattr('src.semantic_query_view.validate_hierarchy_artifact', forbidden)
    monkeypatch.setattr('src.semantic_query_view.validate_source_catalog_artifact', forbidden)
    def action():
        if kind == 'hierarchy':
            provider = SlangHierarchyProvider(entry, context(), budget)
            fact = provider.lookup_scope('sg_top', budget).fact
            budget.admit(fact, 0)
        else:
            provider = SlangSourceProvider(entry, context(), budget, ('/rtl/a.sv',))
            with provider.open_file_symbols(NS(path='/rtl/a.sv'), budget) as stream:
                fact = next(stream)
                budget.admit_symbol(fact)
        return dict(fact=fact.row(), metrics=budget.metrics(), cache_bytes=entry.cache_bytes,
                    raw_bytes=entry.ir_bytes, original=entry.ir_json_bytes == ready.ir_json_bytes)
    actual = observe(tmp_path, request.to_dict(), action)
    assert actual['metrics']['native_call_count'] <= 2 and actual['metrics']['estimated_fact_bytes'] <= 4096
    assert actual['cache_bytes'] > 4096 and actual['original']
    assert actual['fact']['source_file'] == '/rtl/a.sv'


@pytest.mark.parametrize('kind', ['hierarchy', 'catalog'])
@pytest.mark.parametrize('boundary', ['calls', 'bytes', 'cancel', 'deadline'])
def test_current_query_boundaries_remain_enforced_without_cold_rebuild(tmp_path, kind, boundary):
    request, ready = fixture(kind, root=tmp_path)
    entry = load(request, ready, tmp_path)
    budget = HierarchyQueryBudget(max_calls=1, max_bytes=1 if boundary == 'bytes' else 4096)
    provider = (SlangHierarchyProvider(entry, context(), budget) if kind == 'hierarchy' else
                SlangSourceProvider(entry, context(), budget, ('/rtl/a.sv',)))
    def action():
        event, token = Event(), None
        if boundary == 'cancel':
            event.set(); token = push_cancel_event(event)
        if boundary == 'deadline':
            budget.deadline = budget.started - 1
        try:
            if kind == 'hierarchy':
                fact = provider.lookup_scope('sg_top', budget).fact
                budget.admit(fact, 0)
                provider.lookup_scope('sg_top.child', budget)
            else:
                with provider.open_file_symbols(NS(path='/rtl/a.sv'), budget) as stream:
                    if boundary == 'bytes':
                        budget.max_calls = 2
                    fact = next(stream)
                    budget.admit_symbol(fact)
        finally:
            if token is not None:
                pop_cancel_event(token)
    expected = OperationCancelled if boundary == 'cancel' else HierarchyBudgetExceeded
    with pytest.raises(expected) as caught:
        observe(tmp_path, dict(request=request.to_dict(), boundary=boundary), action)
    if boundary != 'cancel':
        assert caught.value.reason == {'calls': 'native_call_limit', 'bytes': 'fact_byte_limit',
                                      'deadline': 'timeout_sec'}[boundary]


@pytest.mark.parametrize('kind', ['hierarchy', 'catalog'])
def test_partial_positive_is_reusable_without_complete_negative(tmp_path, kind):
    request, ready = fixture(kind, limited=True, root=tmp_path)
    entry = load(request, ready, tmp_path)
    asked = consumer(request, low=True)
    budget = HierarchyQueryBudget()
    def action():
        match = entry.reuse_for(asked.artifact_identity.scope)
        if kind == 'hierarchy':
            provider = SlangHierarchyProvider(entry, context(), budget)
            present = provider.lookup_scope('sg_top.child', budget)
            absent = provider.lookup_scope('sg_top.missing', budget)
            positive, negative_complete = present.fact is not None, absent.complete
            gaps = absent.gaps
        else:
            provider = SlangSourceProvider(entry, context(), budget, ('/rtl/a.sv',))
            with provider.open_file_symbols(NS(path='/rtl/a.sv'), budget) as stream:
                facts = list(stream)
                positive, negative_complete, gaps = bool(facts), stream.complete, stream.gaps
        return dict(reusable=match.reusable, complete=match.complete_for_request,
                    positive=positive, negative_complete=negative_complete, gaps=gaps)
    actual = observe(tmp_path, asked.to_dict(), action)
    assert actual['reusable'] and actual['positive'] and not actual['complete'] and not actual['negative_complete']
    assert actual['gaps']


def test_actual_native_parent_index_proves_wider_hierarchy_domain(tmp_path):
    request, ready = fixture(root=tmp_path)
    entry = load(request, ready, tmp_path)
    asked = _request(scope=replace(_scope(signal_path='sg_top.child.x', ancestors=('sg_top', 'sg_top.child'),
        cone_paths=('sg_top', 'sg_top.child'), boundary_paths=('sg_top', 'sg_top.child')),
        semantic_search=replace(request.scope.semantic_search, roots=('sg_top.child',), depth=0,
                                hierarchy_mode='bounded', max_members=1)))
    missing = replace(asked.artifact_identity.scope,
        semantic_search=replace(asked.scope.semantic_search, roots=('sg_top.missing',)))
    actual = observe(tmp_path, asked.to_dict(), lambda: dict(
        match=vars(entry.reuse_for(asked.artifact_identity.scope)),
        missing=entry.reuse_for(missing).reusable))
    assert actual['match']['reusable'] and actual['match']['complete_for_request'] and not actual['missing']


@pytest.mark.parametrize('kind', ['hierarchy', 'catalog'])
def test_wrong_design_capability_alias_or_policy_never_reuses(tmp_path, kind):
    request, ready = fixture(kind, root=tmp_path)
    entry = load(request, ready, tmp_path)
    scope = request.artifact_identity.scope
    changed_search = replace(scope.semantic_search, **(
        {'hierarchy_aliases': (('/alias/a.sv', '/rtl/a.sv'),)} if kind == 'hierarchy'
        else {'source_aliases': (('/alias/a.sv', '/rtl/a.sv'),)}))
    from src.source_graph_contract import SemanticScopeSearch
    rejected = [replace(scope, design='different'), replace(scope, semantic_search=changed_search),
                replace(scope, semantic_search=SemanticScopeSearch()),
                replace(scope, coverage_boundary=replace(scope.coverage_boundary,
                                                        objective_exclusions=('source_context_incomplete',)))]
    if kind == 'catalog':
        rejected.append(replace(scope, semantic_search=replace(scope.semantic_search,
            source_capabilities=('class_inheritance', 'file_declarations'))))
    actual = observe(tmp_path, request.to_dict(), lambda: [entry.reuse_for(item).reusable for item in rejected])
    assert not any(actual)


def test_retained_view_participates_in_runtime_capacity(tmp_path):
    request, ready = fixture(large=True, root=tmp_path)
    entry = load(request, ready, tmp_path)
    worker = ImmediateWorker([ready])
    runtime = SourceGraphRuntime(worker, max_cache_bytes=entry.ir_bytes)
    async def action():
        outcome = await runtime.prepare(request)
        saved_outcome(tmp_path, 'capacity', outcome)
        healthy, healthy_ready = fixture(root=tmp_path, label='healthy-retained-entry')
        registered = SourceGraphRuntime(ImmediateWorker(), max_cache_bytes=entry.ir_bytes)
        healthy_published = await registered.publish_prepared(healthy, healthy_ready)
        large_published = await registered.publish_prepared(request, ready)
        return dict(disposition=outcome.metrics.cache_disposition.value,
                    healthy_published=healthy_published, large_published=large_published,
                    healthy_preserved=await registered.lookup_prepared(healthy) is not None,
                    prepared=await runtime.lookup_prepared(request) is not None,
                    cache_bytes=outcome.entry.cache_bytes, raw_bytes=outcome.entry.ir_bytes)
    actual = observe(tmp_path, request.to_dict(), lambda: asyncio.run(action()))
    assert actual['disposition'] == 'bypass_capacity' and not actual['prepared']
    assert actual['cache_bytes'] > actual['raw_bytes']
    assert actual['healthy_published'] and not actual['large_published'] and actual['healthy_preserved']


def test_captured_ancestor_positive_does_not_need_descendant_completion(tmp_path):
    request, _ = fixture(root=tmp_path)
    deep = replace(request.scope.semantic_search, roots=('sg_top.child',), depth=0, hierarchy_mode='bounded')
    value = hierarchy_payload(deep)
    value['complete_children'] = []
    request = _request(scope=replace(request.scope, semantic_search=deep))
    ir = ConnectivityIR(frontend_name='hand_oracle', frontend_version='1.0', definitions=(), instances=(),
        bindings=(), top_instances=(), coverage=CoverageReport(CoverageStatus.COMPLETE, 1, 1),
        metadata=(('hierarchy_scopes_v2', json.dumps(value, sort_keys=True)),))
    receipt = SourceGraphArtifactScopeReceipt(request.artifact_identity.scope, CoverageStatus.COMPLETE)
    entry = load(request, WorkerBuildResult.ready(ir, receipt), tmp_path)
    ancestor = replace(request.artifact_identity.scope,
        semantic_search=replace(deep, roots=('sg_top',), max_members=1))
    wider = replace(ancestor, semantic_search=replace(ancestor.semantic_search, depth=2))
    actual = observe(tmp_path, request.to_dict(), lambda: dict(
        positive=entry.reuse_for(ancestor).reusable, unproved_children=entry.reuse_for(wider).reusable))
    assert actual['positive'] and not actual['unproved_children']


def test_same_scope_different_content_or_compiler_policy_is_a_cache_miss(tmp_path):
    request, ready = fixture(root=tmp_path)
    async def action():
        runtime = SourceGraphRuntime(ImmediateWorker([ready]))
        await runtime.prepare(request)
        changed = [_request(label='other-design', scope=request.scope)]
        identity = replace(request.artifact_identity, adapter_version='different_policy')
        changed.append(replace(request, artifact=identity))
        return [await runtime.lookup_prepared(item) is not None for item in changed]
    actual = observe(tmp_path, request.to_dict(), lambda: asyncio.run(action()))
    assert actual == [False, False]


def test_first_waiter_context_cancellation_does_not_cancel_shared_view_load(tmp_path):
    request, ready = fixture(root=tmp_path)
    async def action():
        worker = ControlledWorker(ready)
        runtime = SourceGraphRuntime(worker)
        waiter_cancel, caller_cancel = asyncio.Event(), Event()
        token = push_cancel_event(caller_cancel)
        try:
            first = asyncio.create_task(runtime.prepare(request, cancel_event=waiter_cancel))
        finally:
            pop_cancel_event(token)
        await asyncio.wait_for(worker.started.wait(), 2)
        second = asyncio.create_task(runtime.prepare(request))
        await asyncio.sleep(0)  # second attaches before cancellation of the first waiter
        caller_cancel.set()
        waiter_cancel.set()
        first_result = await asyncio.wait_for(first, 2)
        saved_outcome(tmp_path, 'cancelled_waiter', first_result)
        worker.release.set()
        second_result = await asyncio.wait_for(second, 2)
        saved_outcome(tmp_path, 'surviving_waiter', second_result)
        return dict(first=first_result.status.value, second=second_result.status.value,
                    worker_cancelled=worker.cancel_seen, builds=worker.count,
                    view=second_result.entry.semantic_view is not None if second_result.entry else False)
    actual = observe(tmp_path, request.to_dict(), lambda: asyncio.run(action()))
    assert actual == dict(first='cancelled', second='ready', worker_cancelled=False, builds=1, view=True)
