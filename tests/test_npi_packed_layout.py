"""Handwritten Language packed facts; no real SDK/frontend execution.

The dictionary bit identities are independent truth. In particular, member
order and widths do not establish offsets, and nested/ascending indices need
not have the same spelling as flattened aggregate indices.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy

import pytest

from src import npi_hierarchy_source as adapter
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted
from src.hierarchy_source_codec import validate_source_result
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_service import finish_hierarchy_query
from src.kdb_anchor import KdbBindingChanged
from src.npi_packed_layout import query_packed_layout, validate_packed_query
from tests.test_npi_hierarchy_source import (Capsule, NativeHandle, FakeSdk, no_real_execution,
    install_sdk, owner_guard, assert_no_live_capsules)


TOP, SIGNAL = 'test_top', 'test_top.packet'
MAPPING = {'tag': [7, 6, 5], 'payload': [4, 3, 2, 1, 0]}


class PackedHandle(NativeHandle):
    def size(self): return self._property('size')
    def packed(self): return self._property('packed')


class PackedSdk(FakeSdk):
    def __init__(self, *, top=TOP):
        super().__init__()
        self.top, self.signal = top, top + '.packet'
        self.nodes[top] = {'key': top, 'type': 'npiModule', 'name': top, 'full_name': top, 'parent': None}
        self.nodes[self.signal] = {'key': self.signal, 'type': 'npiStructVar', 'name': 'packet',
            'full_name': self.signal, 'parent': top, 'size': 8, 'Typespec': '@struct'}
        self.nodes['@struct'] = {'key': '@struct', 'type': 'npiStructTypespec', 'packed': 1}
        self.members = {'@struct': ['@tag_member', '@payload_member']}
        self.ranges = {'@tag_type': ['@tag_range'], '@payload_type': ['@payload_range']}
        for name, left in (('tag', 2), ('payload', 4)):
            self.nodes['@' + name + '_member'] = {'key': '@' + name + '_member', 'type': 'npiTypespecMember',
                'name': name, 'Typespec': '@' + name + '_type'}
            self.nodes['@' + name + '_type'] = {'key': '@' + name + '_type', 'type': 'npiLogicTypespec'}
            self.nodes['@' + name + '_range'] = {'key': '@' + name + '_range', 'type': 'npiRange',
                'LeftRange': '@' + name + '_left', 'RightRange': '@zero'}
            self.nodes['@' + name + '_left'] = {'key': '@' + name + '_left', 'type': 'npiConstant', 'value': left}
            self.nodes[self.signal + '.' + name] = {'key': self.signal + '.' + name, 'type': 'npiLogicVar',
                'name': name, 'full_name': self.signal + '.' + name, 'parent': self.signal}
        self.nodes['@zero'] = {'key': '@zero', 'type': 'npiConstant', 'value': 0}
        # Fixed ground truth; query implementation never supplies these IDs.
        self.bit_ids = {self.signal: {i: i for i in range(8)},
            self.signal + '.tag': {2: 7, 1: 6, 0: 5},
            self.signal + '.payload': {4: 4, 3: 3, 2: 2, 1: 1, 0: 0}}
        self.compare_override = None
        self.low.Typespec, self.low.TypespecMember = 'Typespec', 'TypespecMember'
        self.low.Range, self.low.LeftRange, self.low.RightRange = 'Range', 'LeftRange', 'RightRange'
        self.low.handle, self.low.handle_by_index, self.low.compare_objects = self.relation, self.bit, self.compare
        self.util.handle = PackedHandle

    def relation(self, relation, raw):
        self.validate(raw)
        assert relation in {'Typespec', 'LeftRange', 'RightRange'}
        key = raw.payload.get(relation)
        self.emit(('relation', relation, raw.payload.get('key')))
        return None if key is None else self.raw(key)

    def iterate(self, relation, reference):
        if relation not in {'TypespecMember', 'Range'}: return super().iterate(relation, reference)
        self.validate(reference)
        key = reference.payload['key']
        items = (self.members if relation == 'TypespecMember' else self.ranges).get(key, [])
        raw = None
        if items:
            raw = Capsule(self, {'relation': relation, 'reference': key, 'items': list(items), 'index': 0})
            self.iterators.append(raw)
        self.emit(('iterate', relation, key))
        return raw

    def scan(self, iterator):
        if iterator.payload['relation'] not in {'TypespecMember', 'Range'}: return super().scan(iterator)
        self.validate(iterator)
        data = iterator.payload
        index = data['index']
        raw = None
        if index == len(data['items']): iterator.valid = False
        else:
            data['index'] += 1
            raw = self.raw(data['items'][index])
        self.emit(('scan', data['relation'], data['reference'], index, raw is None))
        return raw

    def bit(self, raw, index):
        self.validate(raw)
        identity = self.bit_ids.get(raw.payload['key'], {}).get(index)
        value = None if identity is None else Capsule(self, {'type': 'npiBitSelect', 'identity': identity})
        self.emit(('bit', raw.payload['key'], index))
        return value

    def compare(self, left, right):
        self.validate(left)
        self.validate(right)
        self.emit(('compare', left.payload['identity'], right.payload['identity']))
        if self.compare_override is not None: return self.compare_override(left, right)
        return int(left.payload['identity'] == right.payload['identity'])


@contextmanager
def owner(monkeypatch, sdk, *, budget=None, degraded=False, failures=None):
    install_sdk(monkeypatch, sdk)
    with adapter.language_query_owner(top=sdk.top, artifact_identity='fixed-map-generation',
        budget=budget or HierarchyQueryBudget(), require_owner=owner_guard(sdk), degraded=degraded,
        cleanup_failed=lambda: failures.append(True) if failures is not None else None) as provider:
        yield provider


def query(provider, fields=('tag', 'payload')):
    return query_packed_layout(provider, source_signal=provider.top + '.packet', fields=list(fields), budget=provider.budget)


def test_fixed_bit_truth_and_resource_cleanup_do_not_use_a_width_heuristic(monkeypatch):
    sdk = PackedSdk()
    with owner(monkeypatch, sdk) as provider:
        result = query(provider)
        assert result['field_bits'] == MAPPING and result['declared_range'] == {'left': 7, 'right': 0}
        assert result['status'] == 'resolved' and result['coverage_status'] == result['semantic_status'] == 'complete'
        assert result['coverage_gaps'] == result['analysis_limits'] == []
        assert result['facts_origin'] == 'verdi_npi' and result['artifact_identity'] == 'fixed-map-generation'
        assert sum(row[0] == 'compare' for row in sdk.events) == 64
        validate_source_result('resolve_packed_fields', result)
    assert sdk.global_count == 1
    assert not any(row[0] == 'release' for row in sdk.events)  # fully drained iterators auto-free
    assert_no_live_capsules(sdk)


def test_member_order_is_not_offset_evidence(monkeypatch):
    sdk = PackedSdk()
    sdk.members['@struct'].reverse()
    with owner(monkeypatch, sdk) as provider: result = query(provider)
    assert result['field_bits'] == MAPPING


@pytest.mark.parametrize('left,right,truth,expected', [
    (0, 2, {0: 7, 1: 6, 2: 5}, [7, 6, 5]),
    (8, 6, {8: 7, 7: 6, 6: 5}, [7, 6, 5]),
    (-1, -3, {-1: 7, -2: 6, -3: 5}, [7, 6, 5]),
    (2, 0, {2: 5, 1: 7, 0: 6}, [5, 7, 6]),
])
def test_ranges_supply_declared_order_but_sdk_identity_supplies_nonsequential_offsets(monkeypatch, left, right, truth, expected):
    sdk = PackedSdk()
    sdk.nodes['@tag_left']['value'] = left
    sdk.nodes['@tag_right'] = {'key': '@tag_right', 'type': 'npiConstant', 'value': right}
    sdk.nodes['@tag_range']['RightRange'] = '@tag_right'
    sdk.bit_ids[SIGNAL + '.tag'] = truth
    with owner(monkeypatch, sdk) as provider: result = query(provider, ('tag',))
    assert result['field_bits'] == {'tag': expected}


def test_nested_members_require_each_actual_typespec_membership(monkeypatch):
    sdk = PackedSdk()
    sdk.members['@outer'] = ['@header_member']
    sdk.nodes['@outer'] = {'key': '@outer', 'type': 'npiStructTypespec', 'packed': 1}
    sdk.nodes['@header_member'] = {'key': '@header_member', 'type': 'npiTypespecMember', 'name': 'header', 'Typespec': '@struct'}
    sdk.nodes[SIGNAL]['Typespec'] = '@outer'
    sdk.nodes[SIGNAL + '.header'] = {'key': SIGNAL + '.header', 'type': 'npiStructVar', 'name': 'header',
        'full_name': SIGNAL + '.header', 'parent': SIGNAL, 'size': 8}
    for field in ('tag', 'payload'):
        key = SIGNAL + '.header.' + field
        sdk.nodes[key] = dict(sdk.nodes[SIGNAL + '.' + field], key=key, full_name=key, parent=SIGNAL + '.header')
        sdk.bit_ids[key] = dict(sdk.bit_ids[SIGNAL + '.' + field])
    sdk.bit_ids[SIGNAL + '.header'] = {i: i for i in range(8)}
    with owner(monkeypatch, sdk) as provider: result = query(provider, ('header', 'header.tag'))
    assert result['field_bits'] == {'header': [7, 6, 5, 4, 3, 2, 1, 0], 'header.tag': [7, 6, 5]}


@pytest.mark.parametrize('case,gap', [
    ('union', 'npi_packed_struct_type_unavailable'), ('unpacked', 'npi_packed_struct_type_unavailable'),
    ('array', 'npi_packed_aggregate_kind_unmodeled'), ('wide', 'npi_packed_identity_width_unsupported'),
    ('bool_size', 'npi_packed_size_unavailable'), ('missing_type', 'npi_packed_struct_type_unavailable'),
    ('duplicate_member', 'npi_packed_member_binding_unavailable'), ('no_member', 'npi_packed_member_binding_unavailable'),
    ('unknown_type', 'npi_packed_member_kind_unmodeled'), ('multi_dimension', 'npi_packed_member_dimensions_unmodeled'),
    ('nonconstant', 'npi_packed_range_unresolved'), ('bool_range', 'npi_packed_range_unresolved'),
    ('sentinel', 'npi_packed_range_unresolved'), ('foreign_signal', 'npi_packed_signal_binding_unavailable'),
    ('wrong_member_path', 'npi_packed_member_path_mismatch'), ('missing_bit', 'npi_packed_bit_handle_unavailable'),
    ('nonboolean_comparator', 'npi_packed_bit_identity_unavailable'), ('two_aliases', 'npi_packed_bit_identity_ambiguous'),
    ('zero_aliases', 'npi_packed_bit_identity_ambiguous'), ('repeated_bit', 'npi_packed_bit_identity_ambiguous'),
])
def test_unknown_layout_never_publishes_partial_or_guessed_fields(monkeypatch, case, gap):
    sdk = PackedSdk()
    if case == 'union': sdk.nodes['@struct']['type'] = 'npiUnionTypespec'
    elif case == 'unpacked': sdk.nodes['@struct']['packed'] = 0
    elif case == 'array': sdk.nodes[SIGNAL]['type'] = 'npiArrayVar'
    elif case == 'wide': sdk.nodes[SIGNAL]['size'] = 257
    elif case == 'bool_size': sdk.nodes[SIGNAL]['size'] = True
    elif case == 'missing_type': sdk.nodes[SIGNAL].pop('Typespec')
    elif case == 'duplicate_member': sdk.members['@struct'].append('@tag_member')
    elif case == 'no_member': sdk.members['@struct'] = ['@payload_member']
    elif case == 'unknown_type': sdk.nodes['@tag_type']['type'] = 'npiStringTypespec'
    elif case == 'multi_dimension': sdk.ranges['@tag_type'] = ['@tag_range', '@tag_range']
    elif case in {'nonconstant', 'bool_range', 'sentinel'}:
        sdk.nodes['@tag_left']['value'] = {'nonconstant': None, 'bool_range': True, 'sentinel': 'FAILED_TO_GET_VALUE'}[case]
    elif case == 'foreign_signal': sdk.nodes[SIGNAL]['parent'] = 'other'
    elif case == 'wrong_member_path': sdk.nodes[SIGNAL + '.tag']['full_name'] = 'other.tag'
    elif case == 'missing_bit': sdk.bit_ids[SIGNAL + '.tag'].pop(1)
    elif case == 'nonboolean_comparator': sdk.compare_override = lambda *_: True
    elif case == 'two_aliases': sdk.compare_override = lambda *_: 1
    elif case == 'zero_aliases': sdk.compare_override = lambda *_: 0
    elif case == 'repeated_bit': sdk.bit_ids[SIGNAL + '.tag'] = {2: 7, 1: 7, 0: 5}
    with owner(monkeypatch, sdk) as provider: result = query(provider)
    assert result['status'] == 'mapping_required' and result['field_bits'] == {} and result['declared_range'] is None
    assert result['coverage_gaps'] == [gap] and result['coverage_status'] == 'partial'
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


def test_degraded_design_cannot_publish_offset_even_with_positive_native_bits(monkeypatch):
    sdk = PackedSdk()
    with owner(monkeypatch, sdk, degraded=True) as provider: result = query(provider)
    assert result['coverage_gaps'] == ['npi_degraded_design'] and result['field_bits'] == {}
    assert not any(row[0] == 'compare' for row in sdk.events)


@pytest.mark.parametrize('settings,reason', [({'max_calls': 2}, 'native_call_limit'),
    ({'max_bytes': 256}, 'fact_byte_limit'), ({'max_depth': 1}, 'scope_depth_limit')])
def test_owner_budget_stops_return_incomplete_without_leaking_resources(monkeypatch, settings, reason):
    sdk = PackedSdk()
    if reason == 'scope_depth_limit':
        sdk.nodes[SIGNAL]['parent'] = SIGNAL + '.tag'
        sdk.nodes[SIGNAL + '.tag']['parent'] = 'other'
    with owner(monkeypatch, sdk, budget=HierarchyQueryBudget(**settings)) as provider: result = query(provider)
    assert result['field_bits'] == {} and result['declared_range'] is None
    assert result['analysis_limits'] == [reason] and result['status'] == 'mapping_required'
    assert sdk.global_count == 1
    assert_no_live_capsules(sdk)


@pytest.mark.parametrize('relation,limit', [('TypespecMember', 256), ('Range', 8)])
def test_relation_scan_cap_peeks_one_and_releases_only_live_iterator(monkeypatch, relation, limit):
    sdk = PackedSdk()
    if relation == 'TypespecMember': sdk.members['@struct'] = ['@tag_member'] * (limit + 1)
    else: sdk.ranges['@tag_type'] = ['@tag_range'] * (limit + 1)
    with owner(monkeypatch, sdk) as provider: result = query(provider)
    assert result['analysis_limits'] == ['packed_type_member_limit']
    scans = [row for row in sdk.events if row[:2] == ('scan', relation)]
    assert len(scans) == limit + 1 and not scans[-1][-1]
    releases = [row for row in sdk.events if row[:2] == ('release', relation)]
    assert len(releases) == 1 and sdk.global_count == 1
    assert_no_live_capsules(sdk)


@pytest.mark.parametrize('factory', [lambda: OperationCancelled('cancel'), lambda: CaptureAborted('input_changed'),
                                    KdbBindingChanged, lambda: TimeoutError('native timeout')])
def test_original_typed_native_failure_survives_owner_cleanup(monkeypatch, factory):
    sdk, error = PackedSdk(), factory()
    def fail(event):
        if event[0] == 'compare': raise error
    sdk.hook = fail
    with pytest.raises(type(error)) as raised:
        with owner(monkeypatch, sdk) as provider: query(provider)
    assert raised.value is error and sdk.global_count == 1
    assert_no_live_capsules(sdk)


def test_primary_timeout_not_replaced_by_global_cleanup_failure(monkeypatch):
    sdk, error, failures = PackedSdk(), TimeoutError('primary'), []
    sdk.global_rc = 0
    def fail(event):
        if event[0] == 'compare': raise error
    sdk.hook = fail
    with pytest.raises(TimeoutError) as raised:
        with owner(monkeypatch, sdk, failures=failures) as provider: query(provider)
    assert raised.value is error and sdk.global_count == 1 and failures


def test_generation_loss_never_releases_handles_belonging_to_new_design(monkeypatch):
    sdk = PackedSdk()
    def replace_generation(event):
        if event[0] == 'compare': sdk.change_generation()
    sdk.hook = replace_generation
    with pytest.raises(adapter.NpiHierarchyUnavailable):
        with owner(monkeypatch, sdk) as provider: query(provider)
    assert sdk.global_count == 0


def test_late_finish_removes_resolved_offset_proof(monkeypatch):
    sdk = PackedSdk()
    with owner(monkeypatch, sdk) as provider:
        result, budget = query(provider), provider.budget
    budget.deadline = budget.started - 1
    finish_hierarchy_query(result, budget)
    assert result['status'] == 'mapping_required' and result['field_bits'] == {} and result['declared_range'] is None
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']
    validate_source_result('resolve_packed_fields', result)


@pytest.mark.parametrize('late_cleanup', [False, True])
def test_actual_backend_query_source_owns_load_cleanup_and_final_codec(monkeypatch, tmp_path, late_cleanup):
    from types import SimpleNamespace
    from src import verdi_npi_backend as npi
    from src.design_context_runtime import prepare_design_context
    from src.hierarchy_source_request import prepare_source_query
    from src.kdb_anchor import capture_kdb_anchor
    from tests.test_npi_hierarchy_source import NoFallback
    source, log, maps = tmp_path / 'top.sv', tmp_path / 'compile.log', tmp_path / 'maps'
    source.write_text('module test_top; endmodule\n')
    log.write_text(f"Command: vcs -sverilog {source} -top test_top\nParsing design file '{source}'\nTop Level Modules:\n test_top\n")
    maps.mkdir()
    (maps / '_libFileMap').write_bytes(b'lib-A')
    (maps / '_stnElabDB').write_bytes(b'elab-A')
    context = prepare_design_context(str(log), simulator='vcs')
    request = prepare_source_query(context, 'resolve_packed_fields', {'source_signal': SIGNAL, 'fields': ['tag', 'payload']})
    sdk = PackedSdk()
    install_sdk(monkeypatch, sdk)
    monkeypatch.setattr(npi, '_import_pynpi', lambda: (sdk.system, SimpleNamespace()))
    backend = npi.VerdiNpiBackend(NoFallback())
    backend.bind_compile_context(context.compile_result())
    backend.bind_kdb_anchor(capture_kdb_anchor(str(maps)))
    if late_cleanup:
        def late(event):
            if event[0] == 'release_all': request.budget.deadline = request.budget.started - 1
        sdk.hook = late
    result = backend.query_source(request)
    assert sdk.init_count == sdk.load_count == sdk.global_count == 1
    assert result['kdb_binding']['load_status'] == 'loaded'
    if late_cleanup:
        assert result['status'] == 'mapping_required' and result['field_bits'] == {}
        assert result['analysis_limits'] == ['timeout_sec'] and result['coverage_status'] == 'partial'
    else:
        assert result['status'] == 'resolved' and result['field_bits'] == MAPPING
    validate_source_result('resolve_packed_fields', result)
    assert_no_live_capsules(sdk)


@pytest.mark.parametrize('arguments', [{}, {'source_signal': SIGNAL, 'fields': []},
    {'source_signal': SIGNAL, 'fields': ['tag', 'tag']}, {'source_signal': SIGNAL, 'fields': ['']},
    {'source_signal': '', 'fields': ['tag']}, {'source_signal': SIGNAL, 'fields': 'tag'},
    {'source_signal': SIGNAL, 'fields': ['tag'], 'extra': 1}])
def test_pure_prevalidation_has_no_native_calls(arguments):
    with pytest.raises(ValueError): validate_packed_query(arguments)
