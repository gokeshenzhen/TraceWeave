"""Codec resource/cancellation boundaries do not change analysis facts."""
import asyncio
from copy import deepcopy
import json
import threading

import pytest

import server
from src import cancellation, schemas
from src import driver_evidence_codec as codec
from src.evidence_output import expand_compact_result
from tests.driver_compact_fixtures import canonical, driver_fixture, public


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_cancel_during_group_copy_stops_before_consuming_remaining_groups():
    event = threading.Event()
    copied = []
    class Group(dict):
        def __iter__(self):
            return super().__iter__()
        def keys(self):
            copied.append(True)
            if len(copied) > 1:
                pytest.fail('continued copying groups after cancellation')
            event.set()
            return super().keys()
    value = dict(signal_path='t.q', wave_path='', resolved_rtl_name='q', driver_status='partial',
        driver_chain=[Group(depth=0, signal_path='t.q') for _ in range(1024)])
    token = cancellation.push_cancel_event(event)
    try:
        with pytest.raises(cancellation.OperationCancelled):
            codec.encode_driver_result(value, limits=codec.EncodeLimits(memo_bytes=0))
    finally:
        cancellation.pop_cancel_event(token)
    assert len(copied) == 1


@pytest.mark.parametrize('limits', [codec.EncodeLimits(memo_bytes=1024),
    codec.EncodeLimits(memo_entries=4), codec.EncodeLimits(table_entries=2)])
def test_exhaustion_and_oversized_single_value_keep_receipts_and_complete_evidence(limits):
    model, _ = driver_fixture('bad')
    model.driver_chain[0].statement_semantics['large'] = '证据' * 20_000
    before = public(model)
    wire = codec.encode_driver_result(model, limits=limits)
    assert canonical(expand_compact_result(wire)) == canonical(before)
    assert canonical(public(model)) == canonical(before)
    assert 'statement_semantics' in wire['result']['driver_chain'][0]
    if limits.table_entries == 2:
        assert sum(map(len, wire['evidence_tables'].values())) <= 2


@pytest.mark.parametrize('limit', [dict(assignment_limit=80), dict(evidence_limit=80),
    dict(state_limit=1), dict(max_depth=0)])
def test_low_analysis_limits_do_not_become_negative_proofs(limit):
    from tests.test_source_graph_bit_resolution import mapped
    from tests.test_source_graph_driver_processes import bank_backend
    result = mapped(bank_backend(), 'bank.S', **limit)
    model = schemas.ExplainDriverResult.model_validate({k:v for k,v in result.items() if not k.startswith('_')})
    restored = expand_compact_result(codec.encode_driver_result(model))
    assert canonical(restored) == canonical(public(model))
    assert not restored['claim_semantics']['negative_claim_allowed']
    # An evidence-only cap does not erase an independently completed structural
    # negative. Only the already-public per-bit resolution is the oracle.
    if 'assignment_limit' in limit or 'state_limit' in limit:
        assert not any(r['resolution'] == 'proved_no_driver' for r in restored['bit_provenance'])
    assert restored['traversal']['incomplete_reasons']


@pytest.mark.parametrize('source,resolution', [
    ('module t(output wire q); endmodule', 'proved_no_driver'),
    ("module t(output wire q); assign q=1'b0; endmodule", 'found'),
    ('module t(output wire q); endmodule', 'unknown'),
])
def test_three_resolutions_survive_encoding(source, resolution):
    from tests.test_dynamic_evidence import project
    from tests.test_source_graph_backend import _entry
    from tests.test_source_graph_bit_resolution import mapped
    from src.source_graph_backend import SourceGraphConnectivityBackend
    ir = project(source)._entry.ir
    if resolution == 'unknown':
        from dataclasses import replace
        from src.connectivity_ir import CoverageGap, CoverageStatus
        gap = CoverageGap(code='runtime_force_not_modeled', message='excluded writer',
                          impact=CoverageStatus.INCONCLUSIVE, scopes=('t.q',))
        ir = replace(ir, coverage=replace(ir.coverage, status=CoverageStatus.INCONCLUSIVE, gaps=(gap,)))
    b = SourceGraphConnectivityBackend(_entry(ir))
    raw = mapped(b, 't.q')
    model = schemas.ExplainDriverResult.model_validate({k:v for k,v in raw.items() if not k.startswith('_')})
    restored = expand_compact_result(codec.encode_driver_result(model))
    assert canonical(restored) == canonical(public(model))
    assert restored['bit_provenance'][0]['resolution'] == resolution


@pytest.mark.anyio
async def test_cancel_inside_codec_no_new_analysis_no_wave_lock_and_next_call(monkeypatch):
    model, _ = driver_fixture('bad')
    calls = []
    async def dispatch(*_):
        calls.append('dispatch')
        return model
    monkeypatch.setattr(server, '_dispatch', dispatch)
    def forbidden(*_, **__):
        pytest.fail('codec initiated analysis or acquired waveform locks')
    for name in ('_get_parser', 'get_source_graph_runtime', '_wave_locks_for', 'probe_verdi_backend'):
        monkeypatch.setattr(server, name, forbidden)
    original = codec.check_cancelled
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    counter = 0
    def checkpoint():
        nonlocal counter
        counter += 1
        if counter == 10:
            entered.set()
            assert release.wait(5)
        try:
            original()
        except cancellation.OperationCancelled:
            stopped.set()
            raise
    monkeypatch.setattr(codec, 'check_cancelled', checkpoint)
    task = asyncio.create_task(server.call_tool('explain_signal_driver', {}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(stopped.wait, 5)
    monkeypatch.setattr(codec, 'check_cancelled', original)
    # Independent concurrent encodes and the subsequent request remain usable.
    replies = await asyncio.gather(*(server.call_tool('explain_signal_driver', {}) for _ in range(2)))
    for reply in replies:
        assert canonical(expand_compact_result(reply[0].text)) == canonical(public(model))
    assert len(calls) == 3


def test_decoder_limits_depth_exact_bytes_nodes_rows_and_isolated_ownership():
    model, _ = driver_fixture('fixed')
    wire = codec.encode_driver_result(model)
    before = canonical(wire)
    original = public(model)
    nodes = codec._measure(original, codec.DecodeLimits()).nodes
    byte_count = len(canonical(original))
    restored = expand_compact_result(wire, limits=codec.DecodeLimits(max_nodes=nodes, max_bytes=byte_count, max_rows=1024))
    assert canonical(restored) == canonical(original)
    for limits in (codec.DecodeLimits(max_nodes=nodes-1), codec.DecodeLimits(max_bytes=byte_count-1),
                   codec.DecodeLimits(max_rows=1023), codec.DecodeLimits(max_depth=3)):
        with pytest.raises(codec.DriverCodecLimitError):
            expand_compact_result(wire, limits=limits)
    assert canonical(wire) == before
    invalid = deepcopy(wire); invalid['evidence_tables']['locations'].clear()
    with pytest.raises(ValueError):
        expand_compact_result(invalid)  # cannot borrow tables from the last successful decode


@pytest.mark.parametrize('backend,status', [('verdi_npi','partial'), ('verdi_npi','testbench_driven'),
    ('verdi_npi','resolved'), ('static','unsupported'), ('static','resolved')])
def test_other_backend_receipts_are_inline_and_not_reinterpreted(backend, status):
    model = schemas.ExplainDriverResult(signal_path='t.q', wave_path='', resolved_rtl_name='q',
        backend=backend, driver_status=status, stopped_at='unsupported_statement' if backend == 'static' else None,
        traversal=schemas.DriverTraversalReceipt(returned_fact_count=1, output_limit=32,
            output_truncated=False, visited_state_count=4096, state_limit=4096, state_truncated=True,
            search_exhaustive=False, incomplete_reasons=['work_limit']),
        cross_check=schemas.DriverLoadCrossCheck(performed=True, conflict=(status=='testbench_driven'),
            note='driver_is_load_real_driver_is_testbench' if status=='testbench_driven' else None),
        backend_status=schemas.BackendStatus(actual_backend=backend, single_backend_provenance=True,
            execution_mode='lsf' if backend=='verdi_npi' else 'local', scheduler_status='completed', worker_status='completed'))
    wire = codec.encode_driver_result(model)
    assert not any(wire['evidence_tables'].values())
    assert canonical(expand_compact_result(wire)) == canonical(public(model))


def test_model_export_occurs_once_without_materializing_get_per_group(monkeypatch):
    model, _ = driver_fixture('bad')
    expected = canonical(public(model))
    original = schemas.ExplainDriverResult.model_dump
    exported = []
    def export(self, *args, **kwargs):
        exported.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(schemas.ExplainDriverResult, 'model_dump', export)
    monkeypatch.setattr(schemas.SchemaModel, '_as_dict', lambda *_: pytest.fail('whole-model get'))
    wire = codec.encode_driver_result(model)
    assert canonical(expand_compact_result(wire)) == expected
    assert len(exported) == 1
