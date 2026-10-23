from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from src.cancellation import OperationCancelled
from src.hierarchy_source_ir import HierarchyBudgetExceeded
from src.npi_hierarchy_source import NpiHierarchySourceProvider, NpiHierarchyUnavailable
from src.npi_structural_scan import scan_native_ports, scan_budget
from src.structural_scan_continuation import ScanContinuationStore, ScanContinuationInvalid, initial_state, validate_state
from src.structural_semantic_router import combine_semantics
from src.structural_semantic_runtime import validate_semantic_options
from tests.test_npi_structural_scan import Provider, Handle, port, const


def options(**kw):
    return validate_semantic_options(dict(semantic_categories=['constant_connection', 'open_input'], **kw))[1]


def provider(opts):
    return Provider([port('a', const('1010')), port('b', const('0101'))], options=opts)


def test_resume_inside_scope_preserves_counts_without_replaying_facts():
    opts = options(semantic_scope='top.u', semantic_max_facts=1)
    p = provider(opts); first = scan_native_ports(p, opts)
    assert first['scan_progress']['status'] == 'available' and first['facts'][0]['port'] == 'a'
    before = deepcopy(first['_scan_state'])
    p = provider(opts); second = scan_native_ports(p, opts, state=first['_scan_state'])
    assert first['_scan_state'] == before  # Input is immutable to retries.
    assert second['status'] == 'complete' and second['gaps'] == []
    assert second['counts'] == {'constant_connection': 2} and second['total_facts'] == 2
    assert [f['port'] for f in second['facts']] == ['b']
    combined = combine_semantics(opts, second)
    assert combined['counts'] == second['counts'] and combined['output_truncated']
    assert combined['category_coverage'][0]['total_facts'] == 2


def test_scope_budget_resume_finishes_the_original_tree():
    opts = options(semantic_max_instances=1)
    first = scan_native_ports(provider(opts), opts)
    assert first['gaps'] == ['scope_limit'] and first['scan_progress']['completed_instances'] == 1
    final = scan_native_ports(provider(opts), opts, state=first['_scan_state'])
    assert final['status'] == 'complete' and final['scan_progress']['completed_instances'] == 2
    assert not final['_scan_state']['pending']


def test_bounded_expression_gap_does_not_block_later_ports():
    opts = options(semantic_scope='top.u'); p = provider(opts)
    ref = Handle(type='npiRefObj'); ref.data['actual_handle'] = ref
    p.child.data['Port'][0].data['high_conn_handle'] = ref
    result = scan_native_ports(p, opts)
    assert 'native_expression_depth_limit' in result['gaps']
    assert result['scan_progress']['status'] == 'complete'
    assert [f['port'] for f in result['facts']] == ['b']


def test_gap_counts_are_not_duplicated_when_a_later_port_stops():
    opts = options(semantic_scope='top.u')
    p = provider(opts); p.child.data['Port'][0].data['high_conn_handle'] = Handle(type='npiFuncCall', size=4)
    scalar = p.scalar
    def stop(h, name):
        if h is p.child.data['Port'][1] and name == 'direction':
            raise HierarchyBudgetExceeded('native_call_limit')
        return scalar(h, name)
    p.scalar = stop
    first = scan_native_ports(p, opts)
    p2 = provider(opts); p2.child.data['Port'][0] = p.child.data['Port'][0]
    final = scan_native_ports(p2, opts, state=first['_scan_state'])
    assert final['gaps'] == ['npi_expression_unmodeled']
    assert final['gap_summary'][0]['affected_ports'] == 1
    assert final['scan_progress']['status'] == 'complete'


def test_cancelled_resume_does_not_mutate_the_checkpoint():
    opts = options(semantic_scope='top.u', semantic_max_facts=1)
    first = scan_native_ports(provider(opts), opts)
    state = deepcopy(first['_scan_state']); p = provider(opts)
    p.scalar = lambda *args: (_ for _ in ()).throw(OperationCancelled())
    with pytest.raises(OperationCancelled): scan_native_ports(p, opts, state=state)
    assert state == first['_scan_state'] and not p.iterators
    assert scan_native_ports(provider(opts), opts, state=state)['status'] == 'complete'


def test_port_order_or_shortened_prefix_cannot_silently_skip_a_binding():
    opts = options(semantic_scope='top.u', semantic_max_facts=1)
    first = scan_native_ports(provider(opts), opts)
    for mutation in ['reorder', 'shorten']:
        p = provider(opts)
        p.child.data['Port'] = list(reversed(p.child.data['Port'])) if mutation == 'reorder' else []
        with pytest.raises(NpiHierarchyUnavailable, match='npi_scan_position_changed'):
            scan_native_ports(p, opts, state=first['_scan_state'])


def test_child_prefix_fingerprint_is_checked_before_resume():
    opts = options(); p = provider(opts)
    second = Handle(type='npiModule', full_name='top.v', scope_handle=p.root)
    p.root.data['InternalScope'].append(second)
    one = p.one
    def stop(h, name):
        if h is second:
            raise HierarchyBudgetExceeded('native_call_limit')
        return one(h, name)
    p.one = stop
    first = scan_native_ports(p, opts)
    assert first['_scan_state']['pending'][-1]['child_index'] == 1
    p.one = one; p.budget = scan_budget(opts)
    p.root.data['InternalScope'].reverse()
    with pytest.raises(NpiHierarchyUnavailable, match='npi_scan_position_changed'):
        scan_native_ports(p, opts, state=first['_scan_state'])


def test_expired_consumed_or_changed_tokens_cannot_fork_work(monkeypatch):
    import src.structural_scan_continuation as module
    now = [10.0]; monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    store = ScanContinuationStore(capacity=1, ttl=2)
    state = initial_state('top', {'open_input'})
    token = store.publish(None, ('key',), state)
    with pytest.raises(ScanContinuationInvalid): store.get(token, ('changed',))
    value = store.get(token, ('key',)); value['pending'].clear()
    assert store.get(token, ('key',)) == state
    next_token = store.publish(token, ('key',), state)
    with pytest.raises(ScanContinuationInvalid): store.publish(token, ('key',), state)
    now[0] = 13.0
    with pytest.raises(ScanContinuationInvalid): store.get(next_token, ('key',))


def test_frontier_validation_rejects_foreign_duplicate_and_unbounded_state():
    state = initial_state('top', {'open_input'})
    for change in ['foreign', 'duplicate', 'nested', 'count']:
        bad = deepcopy(state)
        if change == 'foreign': bad['pending'][0]['path'] = 'other'
        if change == 'duplicate': bad['pending'] *= 2
        if change == 'nested': bad['pending'][0]['children'] = [deepcopy(bad['pending'][0])]
        if change == 'count': bad['counts'] = {'open_input': -1}
        with pytest.raises(ValueError): validate_state(bad, top='top', root='top', categories={'open_input'})


def test_recycling_invalidating_capsules_never_reuses_a_released_handle(monkeypatch):
    from tests.test_npi_hierarchy_source import FakeSdk, NativeHandle, TOP
    sdk = FakeSdk()
    sdk.nodes = {TOP: dict(type='npiModule', full_name=TOP, parent=None)}
    sdk.children = {TOP: []}; sdk.parameters = {}
    paths = []
    for i in range(1400):
        parent = TOP if i < 2 else paths[(i-2)//2]
        path = parent + '.u' + str(i % 2)
        paths.append(path)
        sdk.nodes[path] = dict(type='npiModule', full_name=path, parent=parent)
        sdk.children.setdefault(parent, []).append(path)
    monkeypatch.setattr(NativeHandle, 'is_black_box', lambda h: 0, raising=False)
    sdk.low.Port = sdk.Parameter  # Empty port inventories in this owner test.
    opts = options(); budget = scan_budget(opts)
    p = NpiHierarchySourceProvider(sdk.lang, sdk.low, sdk.util, top=TOP,
        artifact_identity='kdb', budget=budget, require_owner=lambda: None)
    try:
        result = scan_native_ports(p, opts)
        assert result['status'] == 'complete' and result['instances_visited'] == 1401
        assert p.handle_count > 4096 and p.handle_batches > 1 and p.handle_peak < 16384
        assert not p.iterators
    finally:
        p.close()
    assert all(not c.valid for c in sdk.capsules)


def test_recycle_refuses_active_iterator_and_propagates_cleanup_failure():
    from tests.test_npi_hierarchy_source import FakeSdk, TOP
    sdk = FakeSdk(); opts = options()
    p = NpiHierarchySourceProvider(sdk.lang, sdk.low, sdk.util, top=TOP,
        artifact_identity='kdb', budget=scan_budget(opts), require_owner=lambda: None)
    p.iterators.add(object())
    with pytest.raises(NpiHierarchyUnavailable, match='unsafe'): p.recycle_scan_handles()
    assert sdk.global_count == 0
    p.iterators.clear(); sdk.global_rc = 0
    with pytest.raises(NpiHierarchyUnavailable, match='cleanup_unconfirmed'): p.recycle_scan_handles()
    with pytest.raises(NpiHierarchyUnavailable): p.close()
