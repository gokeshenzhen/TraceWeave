"""Prepared packed-field routing with real text/VCD and fake Language facts.

Only handwritten SDK bit identities are used. Loaded labels are control
evidence; the separate existing-P0 public probe must establish real NPI use.
The VCD is deliberately authored as a declaration fixture, not simulator data.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import json
import threading

import pytest

import server
from src import connectivity_backend, npi_hierarchy_source as adapter
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.design_context_codec import encode_design_context
from src.design_context_runtime import ContextUnavailable
from src.hierarchy_source_ir import HierarchyBudgetExceeded
from src.kdb_anchor import KdbBindingChanged
from src.npi_lsf import HierarchySourceWorkerRequest, SourceWorkerLimits, parse_worker_request_bytes
from src.npi_source_worker import validate_source_worker_result
from tests.test_npi_hierarchy_source import install_sdk, owner_guard, assert_no_live_capsules
from tests.test_npi_packed_layout import PackedSdk
from tests.test_public_design_instance import anyio_backend, isolate, forbidden, prepare, project, public
from tests.test_failure_design_context import ServerClock


ORIGINAL_GET_PARSER = server._get_parser
FIELDS = {'tag': {'path': 'top.packet[7:0]', 'bits': [7, 6, 5]},
          'payload': {'path': 'top.packet[7:0]', 'bits': [4, 3, 2, 1, 0]}}


class Controls:
    def __init__(self, monkeypatch):
        self.monkeypatch, self.thread = monkeypatch, threading.get_ident()
        self.sdk = PackedSdk(top='top')
        self.requests, self.wave_threads, self.raw_results = [], [], []
        self.before = self.after = self.transform = None
        self.wave_active = False

    def select(self, status, *, fallback):
        assert not self.wave_active and threading.get_ident() == self.thread
        controls = self
        class Backend:
            name, uses_external_worker, kdb_status = 'verdi_npi', False, None
            def bind_compile_context(self, metadata): self.metadata = deepcopy(metadata)
            def bind_kdb_anchor(self, anchor): self.anchor = anchor
            def query_source(self, request):
                assert not controls.wave_active and threading.get_ident() == controls.thread
                assert self.metadata == request.context.compile_result()
                controls.requests.append(request)
                if controls.before: controls.before(request)
                install_sdk(controls.monkeypatch, controls.sdk)
                identity = hashlib.sha256((self.anchor.identity_sha256 + '\0' + request.context.selected_top).encode()).hexdigest()
                with adapter.language_query_owner(top=request.context.selected_top, artifact_identity=identity,
                    budget=request.budget, require_owner=owner_guard(controls.sdk), cleanup_failed=forbidden) as provider:
                    value = request.execute(provider)
                self.kdb_status = {'load_quality': 'clean'}
                if hasattr(request.context, 'identity_sha256'):
                    value['context_identity_sha256'] = request.context.identity_sha256
                value['kdb_binding'] = dict(self.anchor.receipt(), load_status='loaded', load_quality='clean')
                if controls.after: controls.after(request)
                if controls.transform: controls.transform(value)
                controls.raw_results.append(deepcopy(value))
                return value
        return Backend()


@pytest.fixture
def controls(monkeypatch, tmp_path):
    value = Controls(monkeypatch)
    value.source, value.log, value.maps = project(tmp_path)
    value.source.write_text('module top; typedef struct packed {logic [2:0] tag; logic [4:0] payload;} packet_t; packet_t packet; endmodule\n')
    value.wave = tmp_path / 'declaration.vcd'
    value.wave.write_text('$timescale 1ps $end\n$scope module top $end\n$var wire 8 ! packet [7:0] $end\n'
        '$upscope $end\n$enddefinitions $end\n#0\nb00000000 !\n')
    monkeypatch.setattr(connectivity_backend, 'select_backend', value.select)
    monkeypatch.setattr(server, '_resolve_source_packed_fields', forbidden)
    original_wave = server._run_in_wave_request
    async def wave_request(paths, fn, **kw):
        def checked():
            assert threading.get_ident() != value.thread
            assert all(lock.locked() for lock in server._wave_locks_for([paths] if isinstance(paths, str) else paths))
            value.wave_active = True
            value.wave_threads.append(threading.get_ident())
            try: return fn()
            finally: value.wave_active = False
        return await original_wave(paths, checked, **kw)
    monkeypatch.setattr(server, '_run_in_wave_request', wave_request)
    def get_parser(path):
        assert value.wave_active and threading.get_ident() != value.thread
        return ORIGINAL_GET_PARSER(path)
    monkeypatch.setattr(server, '_get_parser', get_parser)
    return value


async def query(controls, **extra):
    return await public('resolve_packed_fields', wave_path=str(controls.wave), signal_path='top.packet[7:0]',
        source_signal='top.packet', fields=['tag', 'payload'], **extra)


@pytest.mark.anyio
@pytest.mark.parametrize('automatic', [False, True])
async def test_actual_owner_engine_projects_hand_bit_truth_then_checks_real_dump(controls, automatic):
    selector = {'compile_log': str(controls.log)} if automatic else {'design_handle': await prepare(controls.log)}
    result = await query(controls, **selector)
    assert result['status'] == 'resolved' and result['fields'] == FIELDS
    assert result['evidence']['layout_source'] == 'verdi_npi_bit_identity'
    assert result['evidence']['source_signal'] == 'top.packet'
    assert result['evidence']['declared_range'] == {'left': 7, 'right': 0}
    assert result['evidence']['native_query']['field_bits'] == {'tag': [7, 6, 5], 'payload': [4, 3, 2, 1, 0]}
    assert result['backend_status']['actual_backend'] == 'verdi_npi'
    assert (result['native_route']['source_association'] if automatic else
        result['backend_status']['design_context']['artifact']['source_association']) == 'unverified'
    assert controls.wave_threads and controls.sdk.global_count == 1
    assert_no_live_capsules(controls.sdk)
    assert server._result_cache['build_tb_hierarchy'] is None and server._session_state['build_tb_hierarchy'] is None


@pytest.mark.anyio
async def test_explicit_design_never_borrows_most_recent_legacy_hierarchy(controls, monkeypatch):
    handle = await prepare(controls.log)
    monkeypatch.setattr(server, '_resolve_hierarchy_context', forbidden)
    server._result_cache['build_tb_hierarchy'] = {'compile_result': {'primary_top': 'foreign'}}
    result = await query(controls, design_handle=handle)
    assert result['status'] == 'resolved' and result['fields'] == FIELDS


@pytest.mark.anyio
async def test_legacy_packed_orchestrator_keeps_its_source_graph_path(controls, monkeypatch):
    # The legacy compatibility entry is an explicit view, never fabricated NPI.
    legacy, entered = {'compile_result': {'primary_top': 'top'}}, []
    monkeypatch.setattr(server, '_resolve_hierarchy_context', lambda *_: (legacy, 'historical-snapshot'))
    monkeypatch.setattr(server, '_handle_prepare_design_context', forbidden)
    monkeypatch.setattr(server, '_check_prerequisites', lambda *_: None)
    async def source(args, simulator):
        assert '_prepared_design' not in args and not controls.wave_active
        entered.append('legacy-source')
        return {'status': 'resolved', 'fields': deepcopy(FIELDS), 'evidence': {
            'declared_range': {'left': 7, 'right': 0}, 'layout_source': 'control_source_graph'}}, None
    monkeypatch.setattr(server, '_resolve_source_packed_fields', source)
    result = (await server._dispatch_inner('resolve_packed_fields', dict(compile_log=str(controls.log),
        simulator='vcs', wave_path=str(controls.wave), signal_path='top.packet[7:0]',
        source_signal='top.packet', fields=['tag', 'payload']))).model_dump(exclude_none=True)
    assert entered == ['legacy-source'] and result['status'] == 'resolved' and result['fields'] == FIELDS
    assert controls.requests == [] and controls.sdk.global_count == 0


@pytest.mark.anyio
async def test_unsupported_native_type_may_use_separate_source_graph_facts(controls, monkeypatch):
    handle = await prepare(controls.log)
    controls.sdk.nodes['@struct']['type'] = 'npiUnionTypespec'
    entered = []
    async def source(args, simulator):
        assert not controls.wave_active and args['_prepared_design'].context.selected_top == 'top'
        entered.append('source')
        return {'status': 'resolved', 'fields': deepcopy(FIELDS), 'evidence': {
            'declared_range': {'left': 7, 'right': 0}, 'layout_source': 'control_source_graph'}}, None
    monkeypatch.setattr(server, '_resolve_source_packed_fields', source)
    result = await query(controls, design_handle=handle)
    assert entered == ['source'] and result['fields'] == FIELDS
    assert result['backend_status']['actual_backend'] == 'source_graph'
    assert result['evidence']['layout_source'] == 'control_source_graph'
    assert result['evidence']['native_query']['status'] == 'mapping_required'
    assert result['evidence']['native_query']['field_bits'] == {}


@pytest.mark.anyio
async def test_source_policy_skips_all_native_owner_work(controls, monkeypatch):
    handle = await prepare(controls.log, backend_preference='source')
    monkeypatch.setattr(connectivity_backend, 'select_backend', forbidden)
    entered = []
    async def source(args, simulator):
        assert not controls.wave_active
        entered.append(True)
        return {'status': 'resolved', 'fields': deepcopy(FIELDS), 'evidence': {
            'declared_range': {'left': 7, 'right': 0}, 'layout_source': 'control_source_graph'}}, None
    monkeypatch.setattr(server, '_resolve_source_packed_fields', source)
    result = await query(controls, design_handle=handle)
    assert entered == [True] and result['status'] == 'resolved'
    assert controls.requests == [] and controls.sdk.global_count == controls.sdk.load_count == 0
    assert result['backend_status']['actual_backend'] == 'source_graph'


@pytest.mark.anyio
@pytest.mark.parametrize('fields', [[], ['tag', 'tag'], [''], [7]])
async def test_invalid_fields_reject_before_native_selection(controls, monkeypatch, fields):
    handle = await prepare(controls.log)
    monkeypatch.setattr(connectivity_backend, 'select_backend', forbidden)
    result = await public('resolve_packed_fields', design_handle=handle, wave_path=str(controls.wave),
        signal_path='top.packet[7:0]', source_signal='top.packet', fields=fields)
    assert result['error_code'] == 'invalid_packed_layout_arguments'
    assert controls.requests == controls.wave_threads == []


@pytest.mark.anyio
@pytest.mark.parametrize('reason', ['native_call_limit', 'fact_byte_limit', 'packed_type_member_limit'])
async def test_native_budget_stop_never_advances_source_fallback(controls, reason):
    handle = await prepare(controls.log)
    controls.before = lambda *_: (_ for _ in ()).throw(HierarchyBudgetExceeded(reason))
    result = await query(controls, design_handle=handle)
    assert result['status'] == 'mapping_required' and result.get('fields', {}) == {} and result['reasons'] == [reason]
    assert len(controls.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('field', ['source_signal', 'field_names', 'artifact', 'context', 'kdb'])
async def test_schema_valid_foreign_native_result_cannot_publish_selections(controls, field):
    handle = await prepare(controls.log)
    def transform(value):
        if field == 'source_signal': value['source_signal'] = 'foreign.packet'
        elif field == 'field_names': value['field_bits']['foreign'] = value['field_bits'].pop('tag')
        elif field == 'artifact': value['artifact_identity'] = 'f' * 64
        elif field == 'context': value['context_identity_sha256'] = 'f' * 64
        else: value['kdb_binding']['identity_sha256'] = 'f' * 64
    controls.transform = transform
    result = await query(controls, design_handle=handle)
    assert 'error' in result and result.get('fields', {}) == {}
    assert controls.sdk.global_count == 1 and not controls.wave_threads


@pytest.mark.anyio
async def test_dump_range_mismatch_discards_native_fields_without_guessed_width_projection(controls):
    handle = await prepare(controls.log)
    controls.wave.write_text(controls.wave.read_text().replace('packet [7:0]', 'packet [15:8]'))
    result = await query(controls, design_handle=handle)
    assert result['status'] == 'mapping_required' and result.get('fields', {}) == {}
    assert result['reasons'] and len(controls.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('factory,code', [(lambda: CaptureAborted('input_changed'), 'design_context_changed'),
    (KdbBindingChanged, 'kdb_identity_changed'), (lambda: TimeoutError('native timeout'), 'design_context_timeout'),
    (lambda: ContextUnavailable('design_context_changed'), 'design_context_changed')])
async def test_original_typed_failure_never_becomes_mapping_required_or_source_fallback(controls, factory, code):
    handle = await prepare(controls.log)
    error = factory()
    controls.before = lambda *_: (_ for _ in ()).throw(error)
    result = await query(controls, design_handle=handle)
    assert result['error_code'] == code and result.get('fields', {}) == {}
    assert len(controls.requests) == 1 and not controls.wave_threads


@pytest.mark.anyio
async def test_cancellation_releases_pin_without_source_fallback(controls):
    handle = await prepare(controls.log)
    controls.before = lambda *_: (_ for _ in ()).throw(OperationCancelled('cancel'))
    with pytest.raises(asyncio.CancelledError): await query(controls, design_handle=handle)
    assert server._design_context_store.remove(handle) and not controls.wave_threads


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['source', 'map', 'reset'])
async def test_final_identity_failure_discards_completed_mapping(controls, change):
    handle = await prepare(controls.log)
    def changed(request):
        if change == 'source': controls.source.write_text(controls.source.read_text() + '// changed\n')
        elif change == 'map': (controls.maps / '_libFileMap').write_bytes(b'map-B')
        else: server.reset_session_state()
    controls.after = changed
    result = await query(controls, design_handle=handle)
    assert result['error_code'] == {'source': 'design_context_changed', 'map': 'kdb_identity_changed',
                                   'reset': 'design_context_generation_changed'}[change]
    assert result.get('fields', {}) == {}


@pytest.mark.anyio
async def test_native_metadata_is_part_of_original_packed_deadline(controls, monkeypatch):
    from src import native_dispatch, native_query
    clock, original, entered = ServerClock(), native_dispatch.read_native_inputs, []
    def read(*a, **kw):
        value = original(*a, **kw)
        entered.append(value)
        clock.offset = 2
        return value
    monkeypatch.setattr(native_dispatch, 'time', clock)
    monkeypatch.setattr(native_query, 'time', clock)
    monkeypatch.setattr(native_dispatch, 'read_native_inputs', read)
    result = await query(controls, compile_log=str(controls.log), timeout_sec=1)
    assert len(entered) == 1 and result['error_code'] == 'native_query_timeout' and controls.requests == []


@pytest.mark.anyio
async def test_late_final_validation_cannot_publish_resolved_mapping(controls, monkeypatch):
    handle = await prepare(controls.log)
    clock, original, entered = ServerClock(), server._validate_design_binding, []
    async def final(*a, **kw):
        await original(*a, **kw)
        if controls.requests:
            entered.append(True)
            clock.offset = 10
    monkeypatch.setattr(server, 'time', clock)
    monkeypatch.setattr(server, '_validate_design_binding', final)
    result = await query(controls, design_handle=handle, timeout_sec=1)
    assert entered == [True] and result['error_code'] == 'design_context_timeout'
    assert result.get('fields', {}) == {} and server._design_context_store.remove(handle)


@pytest.mark.anyio
async def test_lsf_request_and_result_codec_keep_exact_packed_association(controls):
    handle = await prepare(controls.log)
    result = await query(controls, design_handle=handle)
    assert result['status'] == 'resolved'
    with server._design_context_store.pin(handle) as entry:
        request = HierarchySourceWorkerRequest(kdb_path=entry.binding.anchor.canonical_path, top='top',
            kdb_identity_sha256=entry.binding.anchor.identity_sha256,
            tool='resolve_packed_fields', arguments={'source_signal': 'top.packet', 'fields': ['tag', 'payload']},
            context_payload=encode_design_context(entry.context), limits=SourceWorkerLimits(timeout_sec=30.0,
                max_scopes=10000, max_calls=100000, max_bytes=16 * 1024 * 1024, max_depth=256,
                max_symbols=100000, max_files=100000))
    decoded = parse_worker_request_bytes(request.model_dump_json().encode())
    assert decoded == request and decoded.tool == 'resolve_packed_fields'
    raw = controls.raw_results[0]
    assert validate_source_worker_result(decoded, raw)['field_bits'] == {'tag': [7, 6, 5], 'payload': [4, 3, 2, 1, 0]}
    for key in ('source_signal', 'field_bits'):
        foreign = deepcopy(raw)
        if key == 'source_signal': foreign[key] = 'foreign.packet'
        else: foreign[key] = {'foreign': [7]}
        with pytest.raises(ValueError): validate_source_worker_result(decoded, foreign)
