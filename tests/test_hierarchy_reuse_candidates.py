"""Controlled hierarchy coverage across unused assignment hints; saved before assert.

Native ownership coverage is also exercised by the unchanged fresh sequences.
No SDK interfaces are fabricated here: payloads use the existing valid codec fixture.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import traceback

import pytest

from src.connectivity_ir import ConnectivityIR
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.slang_hierarchy_source import SlangHierarchyProvider
from src.source_graph_contract import compute_source_graph_build_key
from src.source_graph_disk_cache import SourceGraphDiskCache
from src.source_graph_runtime import SourceGraphRuntime, WorkerBuildResult
from tests.test_semantic_query_reuse import fixture, load, context, saved_outcome
from tests.test_slang_hierarchy_catalog import observe
from tests.test_source_graph_runtime import _request, _scope, ImmediateWorker


def producer(tmp, *, limited=False):
    # Factory evidence starts before constructing its source fixture. These are
    # control inputs, distinct from the serialized runtime request saved below.
    (tmp / 'producer.control.json').write_text(json.dumps(dict(kind='hierarchy', limited=limited)))
    try:
        request, ready = fixture('hierarchy', limited=limited)
        (tmp / 'source.request.json').write_text(json.dumps(request.to_dict()))
        (tmp / 'source.ir.raw.json').write_bytes(ready.ir_json_bytes)
        (tmp / 'source.ready.json').write_text(json.dumps(dict(status=ready.status.value,
            ir_fingerprint_sha256=ready.ir_fingerprint_sha256,
            scope_receipt=ready.scope_receipt.to_dict(), metrics=ready.metrics.to_dict())))
        decoded = ConnectivityIR.from_json_bytes(ready.ir_json_bytes)
        search = replace(request.scope.semantic_search, assignment_candidates=('sg_top',))
        request = _request(scope=replace(request.scope, semantic_search=search))
        (tmp / 'producer.request.json').write_text(json.dumps(request.to_dict()))
        payload = json.loads(dict(decoded.metadata)['hierarchy_scopes_v2'])
        payload['search_sha256'] = hashlib.sha256(json.dumps(search.to_dict(), sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        ir = replace(decoded, metadata=(('hierarchy_scopes_v2', json.dumps(payload, sort_keys=True)),))
        (tmp / 'producer.ir.raw.json').write_bytes(ir.to_json_bytes())
        ready = WorkerBuildResult.ready(ir, replace(ready.scope_receipt, scope=request.artifact_identity.scope))
        (tmp / 'producer.ready.json').write_text(json.dumps(dict(status=ready.status.value,
            ir_fingerprint_sha256=ready.ir_fingerprint_sha256,
            scope_receipt=ready.scope_receipt.to_dict(), metrics=ready.metrics.to_dict())))
        return request, ready
    except BaseException as exc:
        (tmp / 'producer.exception.json').write_text(json.dumps(dict(
            type=type(exc).__name__, message=str(exc))))
        (tmp / 'producer.exception.txt').write_text(traceback.format_exc())
        raise


def asked(request, *, root='sg_top.child', hint='sg_top.child', foreign=False, complete=False):
    paths = ('sg_top', root) + (('sg_top.foreign',) if foreign else ())
    search = replace(request.scope.semantic_search, roots=(root,), assignment_candidates=(hint,),
                     depth=8 if complete else 0, hierarchy_mode='complete' if complete else 'bounded')
    return _request(scope=replace(_scope(signal_path=root + '.signal', ancestors=('sg_top', root),
        cone_paths=paths, boundary_paths=paths), semantic_search=search,
        elaboration_candidates=('sg_top.foreign',) if foreign else ()))


def decision(entry, query):
    admitted, stage = entry.semantic_view._hierarchy_reuse_decision(query.artifact_identity.scope)
    match = entry.reuse_for(query.artifact_identity.scope)
    return dict(reusable=match.reusable, complete=match.complete_for_request,
                private_decision=dict(admitted=admitted, stage=stage,
                    producer_candidates=list(entry.semantic_view.search.assignment_candidates),
                    requested_candidates=list(query.scope.semantic_search.assignment_candidates),
                    requested_roots=list(query.scope.semantic_search.roots)))


@pytest.mark.parametrize('path', ['memory', 'registration', 'disk_loaded'])
def test_same_budget_nested_root_and_candidate_use_actual_stored_fact(tmp_path, path):
    request, ready = producer(tmp_path)
    query = asked(request)
    async def action():
        if path == 'registration':
            worker = ImmediateWorker()
            runtime = SourceGraphRuntime(worker)
            published = await runtime.publish_prepared(request, ready)
            (tmp_path / 'registration.raw.json').write_text(json.dumps(dict(published=published)))
            original = await runtime.lookup_prepared(request)
        else:
            store = SourceGraphDiskCache(tmp_path / 'disk') if path == 'disk_loaded' else None
            worker = ImmediateWorker([ready])
            runtime = SourceGraphRuntime(worker, disk_cache=store)
            built = await runtime.prepare(request); saved_outcome(tmp_path, 'built', built)
            original = built.entry
            if store is not None:
                worker = ImmediateWorker()
                runtime = SourceGraphRuntime(worker, disk_cache=store)
                restored = await runtime.prepare(request); saved_outcome(tmp_path, 'disk', restored)
                original = restored.entry
        current = await runtime.prepare(query); saved_outcome(tmp_path, 'query', current)
        prepared = await runtime.lookup_prepared(query)
        return dict(same_entry=original is current.entry is prepared,
            same_producer=current.entry.build_key.digest == compute_source_graph_build_key(request).digest,
            raw_unchanged=current.entry.ir_json_bytes == ready.ir_json_bytes,
            current_builds=current.metrics.actual_build_count, worker_count=worker.count,
            decision=decision(current.entry, query))
    actual = observe(tmp_path, dict(producer=request.to_dict(), consumer=query.to_dict(), path=path),
                     lambda: asyncio.run(action()))
    assert actual['same_entry'] and actual['same_producer'] and actual['raw_unchanged']
    assert actual['current_builds'] == 0 and actual['worker_count'] == (1 if path == 'memory' else 0)
    assert actual['decision']['reusable'] and actual['decision']['complete']
    for key in ('max_instances', 'max_members', 'hierarchy_max_bytes'):
        assert getattr(request.scope.semantic_search, key) == getattr(query.scope.semantic_search, key)


def test_unused_missing_assignment_hint_does_not_restrict_hierarchy_fact(tmp_path):
    request, ready = producer(tmp_path)
    entry = load(request, ready, tmp_path)
    query = asked(request, hint='sg_top.not_a_native_scope')
    actual = observe(tmp_path, query.to_dict(), lambda: decision(entry, query))
    assert actual['reusable'] and actual['complete']


@pytest.mark.parametrize('boundary', ['missing_root', 'foreign_projection', 'partial_complete_negative'])
def test_actual_uncovered_domain_cannot_become_complete_from_candidate_hint(tmp_path, boundary):
    request, ready = producer(tmp_path, limited=boundary == 'partial_complete_negative')
    entry = load(request, ready, tmp_path)
    query = asked(request, root='sg_top.missing' if boundary == 'missing_root' else 'sg_top.child',
        hint='sg_top', foreign=boundary == 'foreign_projection', complete=boundary == 'partial_complete_negative')
    def action():
        result = decision(entry, query)
        if boundary == 'partial_complete_negative':
            provider = SlangHierarchyProvider(entry, context(), HierarchyQueryBudget())
            absent = provider.lookup_scope('sg_top.missing', HierarchyQueryBudget())
            result.update(negative_complete=absent.complete, gaps=absent.gaps)
        return result
    actual = observe(tmp_path, query.to_dict(), action)
    assert not actual['reusable'] and not actual['private_decision']['admitted']
    if boundary == 'partial_complete_negative':
        assert not actual['negative_complete'] and actual['gaps']


def test_partial_known_positive_with_new_candidate_keeps_unknown_negative(tmp_path):
    request, ready = producer(tmp_path, limited=True)
    entry = load(request, ready, tmp_path)
    query = asked(request)
    def action():
        result = decision(entry, query)
        budget = HierarchyQueryBudget()
        provider = SlangHierarchyProvider(entry, context(), budget)
        present = provider.lookup_scope('sg_top.child', budget)
        absent = provider.lookup_scope('sg_top.missing', budget)
        result.update(positive=present.fact is not None, negative_complete=absent.complete, gaps=absent.gaps)
        return result
    actual = observe(tmp_path, query.to_dict(), action)
    assert actual['reusable'] and actual['positive'] and not actual['complete']
    assert not actual['negative_complete'] and actual['gaps']
