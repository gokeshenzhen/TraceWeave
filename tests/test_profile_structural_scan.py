import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace

import pytest

from scripts import profile_structural_scan as profile
from tests.test_structural_native_routing import setup


def test_nested_timings_are_inclusive_and_retain_no_arguments(monkeypatch):
    clock = iter([0, 10, 40, 60])
    monkeypatch.setattr(profile.time, 'perf_counter_ns', lambda: next(clock))
    child = profile.timed(lambda value: value, 'preprocessor_options')
    parent = profile.timed(lambda value: child(value), 'identity_capture')
    result = object()
    measurement = profile.PageProfile()
    token = profile._active.set(measurement)
    try:
        assert parent(result) is result
    finally:
        profile._active.reset(token)
    rows = measurement.snapshot()
    assert [(r['phase'], r['parent'], r['count']) for r in rows] == [
        ('identity_capture', 'root', 1), ('preprocessor_options', 'identity_capture', 1)]
    assert rows[0]['total_ms'] == 60 / 1e6
    assert rows[1]['total_ms'] == 30 / 1e6
    assert profile._stack.get() == ()


@pytest.mark.anyio
async def test_workers_inherit_parent_and_cancelled_requests_restore_context():
    def worker(): return 17
    async def outer(): return await asyncio.to_thread(profile.timed(worker, 'identity_validate'))
    measurement = profile.PageProfile()
    token = profile._active.set(measurement)
    try:
        assert await profile.timed(outer, 'lexical_cache')() == 17
        error = asyncio.CancelledError('private value')
        async def cancel(): raise error
        with pytest.raises(asyncio.CancelledError) as caught:
            await profile.timed(cancel, 'semantic')()
        assert caught.value is error
    finally:
        profile._active.reset(token)
    rows = measurement.snapshot()
    assert next(r for r in rows if r['phase'] == 'identity_validate')['parent'] == 'lexical_cache'
    assert next(r for r in rows if r['phase'] == 'semantic')['errors'] == 1
    assert 'private value' not in json.dumps(rows) and profile._stack.get() == ()


def test_file_bytes_and_memo_hits_are_numeric_only():
    measurement = profile.PageProfile()
    token = profile._active.set(measurement)
    try:
        record = SimpleNamespace(size=123, path='/customer/private.sv')
        assert profile.timed(lambda r: True, 'file_validate')(record)
        assert not profile.timed(lambda r: False, 'file_validate')(record)
        assert profile.timed(lambda: object(), 'identity_memo')() is not None
        assert profile.timed(lambda: None, 'identity_memo')() is None
    finally:
        profile._active.reset(token)
    rows = measurement.snapshot()
    file = next(r for r in rows if r['phase'] == 'file_validate')
    assert file['count'] == 2 and file['logical_bytes'] == 123
    assert next(r for r in rows if r['phase'] == 'identity_memo')['hits'] == 1
    assert 'customer' not in json.dumps(rows)
    with pytest.raises(ValueError): profile.timed(lambda: None, '/customer/name')


@pytest.mark.anyio
async def test_profile_exercises_public_wrapper_without_changing_coverage(setup):
    import server
    args, _, _ = setup
    unmeasured = json.loads((await server.call_tool('scan_structural_risks', args))[0].text)
    measurement = profile.PageProfile()
    with ExitStack() as stack:
        profile.install(stack, server)
        token = profile._active.set(measurement)
        try:
            measured = json.loads((await profile.timed(server.call_tool, 'handler')(
                'scan_structural_risks', args))[0].text)
        finally:
            profile._active.reset(token)
    assert unmeasured['semantic']['category_coverage'] == measured['semantic']['category_coverage']
    assert unmeasured['semantic']['facts'] == measured['semantic']['facts']
    rows = measurement.snapshot()
    for phase in ['compile_parse', 'identity_capture', 'preprocessor_options',
                  'lexical_cache', 'semantic', 'source_supplement', 'source_cache',
                  'result_model', 'output_budget', 'serialize']:
        assert any(r['phase'] == phase for r in rows), phase
    assert not any(str(args['compile_log']) in str(row) for row in rows)


def test_receipt_omits_names_and_unapproved_metrics():
    value = dict(files_scanned=2, total_risks=0, semantic=dict(status='partial',
        counts={'constant_connection': 3, 'private_design': 1},
        metrics={'native_call_count': 10, 'native_handle_count': 'private_design',
                 'private_path': '/customer/design'},
        category_coverage=[dict(category='constant_connection', status='partial', total_facts=3,
                                gaps=['private_design'])], result_groups=[]))
    result = profile.receipt(value)
    assert result['native_metrics'] == {'native_call_count': 10}
    assert result['counts'] == {'constant_connection': 3}
    assert 'private' not in json.dumps(result) and 'customer' not in json.dumps(result)


def test_private_report_permissions(tmp_path):
    path = tmp_path / 'profile.json'
    profile.write_private(path, {'count': 1})
    assert json.loads(path.read_text()) == {'count': 1}
    assert path.stat().st_mode & 0o777 == 0o600
