import pytest

import server
from src import structural_native_runtime as native
from src.cancellation import OperationCancelled
from src.npi_structural_scan import scan_native_ports
from tests.test_structural_native_routing import setup
from tests.test_npi_structural_scan import Provider, port, const


def install_page_query(monkeypatch, calls):
    def query(backend, options, *, top, budget, state=None):
        calls.append('page')
        p = Provider([port('a', const('1010')), port('b', const('0101'))], options=options)
        p.budget = budget
        r = scan_native_ports(p, options, state=state)
        r.pop('query_metrics')
        return r
    monkeypatch.setattr(native, 'query_native_scan', query)
    return query


@pytest.mark.anyio
async def test_public_resume_consumes_token_and_retains_cumulative_counts(setup, monkeypatch):
    args, calls, _ = setup
    install_page_query(monkeypatch, calls)
    args = dict(args, semantic_scope='top.u', semantic_categories=['constant_connection', 'open_input'], semantic_max_facts=1)
    first = await server._dispatch('scan_structural_risks', args)
    token = first.semantic.continuation
    assert token and first.semantic.suggested_next_action['arguments']['semantic_continuation'] == token
    assert server._extract_structural_scan_summary(first)['semantic_continuation'] == token
    final = await server._dispatch('scan_structural_risks', dict(args, semantic_continuation=token))
    assert final.semantic.status == 'complete' and final.semantic.counts == {'constant_connection': 2}
    assert final.semantic.total_facts == 2 and [f['port'] for f in final.semantic.facts] == ['b']
    assert final.semantic.output_truncated and final.semantic.continuation is None
    stale = await server._dispatch('scan_structural_risks', dict(args, semantic_continuation=token))
    assert stale.semantic.termination_reason == 'native_scan_continuation_expired_or_changed'
    assert calls.count('page') == 2


@pytest.mark.anyio
async def test_resume_scope_or_kdb_change_never_advances_fallback(setup, monkeypatch):
    args, calls, _ = setup
    install_page_query(monkeypatch, calls)
    args = dict(args, semantic_scope='top.u', semantic_categories=['constant_connection', 'open_input'], semantic_max_facts=1)
    first = await server._dispatch('scan_structural_risks', args)
    async def forbidden(**kw): pytest.fail('changed continuation advanced source fallback')
    monkeypatch.setattr(server._semantic_scan_runtime, 'scan', forbidden)
    different = await server._dispatch('scan_structural_risks', dict(args, semantic_scope='top', semantic_continuation=first.semantic.continuation))
    assert different.semantic.termination_reason == 'native_scan_continuation_expired_or_changed'
    binding = native.prepare_native_binding(None); binding.anchor.identity_sha256 = 'changed'
    changed = await server._dispatch('scan_structural_risks', dict(args, semantic_continuation=first.semantic.continuation))
    assert changed.semantic.termination_reason == 'native_scan_continuation_expired_or_changed'
    assert calls.count('page') == 1


@pytest.mark.anyio
async def test_cancelled_page_keeps_token_retryable(setup, monkeypatch):
    args, calls, _ = setup
    query = install_page_query(monkeypatch, calls)
    args = dict(args, semantic_scope='top.u', semantic_categories=['constant_connection', 'open_input'], semantic_max_facts=1)
    first = await server._dispatch('scan_structural_risks', args)
    def cancel(*a, **kw): raise OperationCancelled()
    monkeypatch.setattr(native, 'query_native_scan', cancel)
    with pytest.raises(OperationCancelled):
        await server._dispatch('scan_structural_risks', dict(args, semantic_continuation=first.semantic.continuation))
    monkeypatch.setattr(native, 'query_native_scan', query)
    final = await server._dispatch('scan_structural_risks', dict(args, semantic_continuation=first.semantic.continuation))
    assert final.semantic.status == 'complete' and final.semantic.total_facts == 2


@pytest.mark.anyio
async def test_tiny_output_budget_keeps_resume_token_and_coverage(setup, monkeypatch):
    args, calls, _ = setup
    install_page_query(monkeypatch, calls)
    monkeypatch.setattr(server.schemas, 'TOKEN_BUDGET_SOFT_LIMIT', 2500)
    first = await server._dispatch('scan_structural_risks', dict(args, semantic_scope='top.u',
        semantic_categories=['constant_connection', 'open_input'], semantic_max_facts=1))
    assert first.payload_bytes <= 2500 and first.semantic.continuation
    assert all(c.status == 'partial' for c in first.semantic.category_coverage)
