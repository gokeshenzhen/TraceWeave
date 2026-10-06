"""The compact wire is a reversible encoding of one already validated result."""
from copy import deepcopy
import json

import pytest

from src.driver_evidence import expand_driver_evidence
from src.evidence_output import expand_compact_result, serialize_compact_result
from tests.driver_compact_fixtures import canonical, driver_fixture, public

FORMAT = 'traceweave.driver.compact.v1'


@pytest.fixture(scope='module', params=['bad', 'fixed', 'guards301'])
def captured(request):
    model, backend = driver_fixture(request.param)
    return request.param, model, backend


def encode(value):
    return json.loads(serialize_compact_result('explain_signal_driver', value))


def test_same_public_result_and_every_statement_roundtrip(captured):
    kind, model, backend = captured
    before = public(model)
    wire = encode(model)
    saved = canonical(wire)
    assert wire['format'] == FORMAT
    expanded = expand_compact_result(wire)
    assert canonical(expanded) == canonical(before)
    for section in ('driver_chain', 'bit_provenance'):
        assert canonical(expand_driver_evidence(expanded[section])) == canonical(expand_driver_evidence(before[section]))
    assert canonical(public(model)) == canonical(before)
    assert canonical(wire) == saved
    assert canonical(encode(model)) == saved
    if kind != 'guards301':
        counts = [expanded[k] for k in ('resolved_bit_count', 'unresolved_bit_count', 'multi_driver_bit_count')]
        assert counts == ([32, 0, 0] if kind == 'fixed' else [31, 1, 1])
        assert len(expanded['driver_chain']) == 8
        assert sum(len(g['statement_evidence']) for g in expanded['driver_chain']) == 512
        assert sum(len(g['statement_evidence']) for g in expanded['bit_provenance']) == (512 if kind == 'fixed' else 640)
        assert len(canonical(wire)) <= .35 * len(canonical(before))
    else:
        assert len(expanded['driver_chain']) == 301
        assert not wire['evidence_tables']['statement_evidence'], 'singleton locations must stay inline'
        before_dynamic = backend.get_dynamic_step('t.q')
        assert len(before_dynamic['branches']) == 301
        assert [b['order'] for b in before_dynamic['branches']] == list(range(301))
        assert len({b['id'] for b in before_dynamic['branches']}) == 301
        assert canonical(backend.get_dynamic_step('t.q')) == canonical(before_dynamic)


def test_exact_types_presence_repeated_locations_and_independent_arrays():
    value = public(driver_fixture('fixed')[0])
    rows = value['driver_chain'] + value['bit_provenance']
    for i, row in enumerate(rows):
        row['statement_semantics']['codec_test'] = dict(null=None, boolean=True, integer=1, real=1.0, text='证据', bits=[7, 2, 7, 0])
        row['statement_evidence'] = [dict(source_line=9, source_column=0, evidence_index=6),
            dict(source_line=9, evidence_index=1), dict(source_line=9, source_column=21, evidence_index=6)] * 32
        row['statement_count'] = 96
        if i % 2:
            row['statement_evidence'] = list(reversed(row['statement_evidence']))
            row['statement_semantics']['codec_test']['integer'] = True
    before = canonical(value)
    wire = encode(value)
    expanded = expand_compact_result(wire)
    assert canonical(expanded) == before
    assert canonical(value) == before
    for section in ('driver_chain', 'bit_provenance'):
        assert canonical(expand_driver_evidence(expanded[section])) == canonical(expand_driver_evidence(value[section]))
    expanded['driver_chain'][0]['statement_evidence'][0]['source_line'] = 999
    expanded['driver_chain'][0]['statement_semantics']['codec_test']['bits'].append(999)
    assert canonical(expanded['driver_chain'][2]) == canonical(value['driver_chain'][2])
    assert canonical(expand_compact_result(wire)) == before


@pytest.fixture
def encoded():
    return encode(driver_fixture('fixed')[0])


def referenced(wire):
    return next(r for r in wire['result']['driver_chain'] if 'statement_evidence_ref' in r)


@pytest.mark.parametrize('bad_id', [-1, True, '0', 1.0, 999999, None])
@pytest.mark.parametrize('field', ['statement_evidence_ref', 'statement_semantics_ref'])
def test_rejects_invalid_reference_ids(encoded, field, bad_id):
    referenced(encoded)[field] = bad_id
    with pytest.raises(ValueError):
        expand_compact_result(encoded)


@pytest.mark.parametrize('damage', ['version', 'tool', 'extra_table', 'extra_path', 'inline_ref',
    'bad_column', 'wide_row', 'line_bool', 'index_bool', 'length', 'count', 'dangling_pair', 'bad_table'])
def test_rejects_malformed_closure(encoded, damage):
    tables = encoded['evidence_tables']
    row = referenced(encoded)
    if damage == 'version': encoded['format'] = 'traceweave.driver.compact.v9'
    elif damage == 'tool': encoded['tool'] = 'trace_x_source'
    elif damage == 'extra_table': tables['arbitrary'] = []
    elif damage == 'extra_path': encoded['result']['statement_semantics_ref'] = 0
    elif damage == 'inline_ref': row['statement_evidence'] = []
    elif damage == 'bad_column': tables['locations'][0]['columns'][0] = 'source_file'
    elif damage == 'wide_row': tables['locations'][0]['rows'][0].append(0)
    elif damage == 'line_bool': tables['locations'][0]['rows'][0][0] = True
    elif damage == 'index_bool': tables['evidence_indices'][0][0] = True
    elif damage == 'length': tables['evidence_indices'][0].pop()
    elif damage == 'count': row['statement_count'] += 1
    elif damage == 'dangling_pair': tables['statement_evidence'][0]['locations_ref'] = 9999
    elif damage == 'bad_table': tables['locations'] = {}
    with pytest.raises(ValueError):
        expand_compact_result(encoded)


def test_raw_json_entry_rejects_duplicate_keys_and_nonfinite(encoded):
    text = json.dumps(encoded)
    assert canonical(expand_compact_result(text)) == canonical(expand_compact_result(encoded))
    for bad in ('{"format":"other",' + text[1:], text.replace('"source_line": 3', '"source_line": NaN', 1)):
        with pytest.raises(ValueError):
            expand_compact_result(bad)


def test_empty_missing_and_explicit_null_are_not_invented():
    value = dict(signal_path='t.q', wave_path='', resolved_rtl_name='q', driver_status='unknown',
        dependency_context={'explicit_null': None}, driver_chain=[], bit_provenance=None)
    assert canonical(expand_compact_result(encode(value))) == canonical(value)


def test_model_projection_matches_public_nonfinite_json_values():
    model, _ = driver_fixture('fixed')
    model.dependency_context = {'values': [float('nan'), float('inf'), -0.0]}
    assert canonical(expand_compact_result(encode(model))) == canonical(public(model))


def test_optimization_capacity_keeps_complete_inline_evidence():
    from src.driver_evidence_codec import EncodeLimits, encode_driver_result
    model, _ = driver_fixture('bad')
    for limits in (EncodeLimits(memo_bytes=0), EncodeLimits(memo_entries=1), EncodeLimits(table_entries=0)):
        wire = encode_driver_result(model, limits=limits)
        assert canonical(expand_compact_result(wire)) == canonical(public(model))
        assert sum(map(len, wire['evidence_tables'].values())) <= (1 if limits.memo_entries == 1 else 0)


def test_decoder_preflights_amplification_before_copy(encoded, monkeypatch):
    from src import driver_evidence_codec as codec
    row = referenced(encoded)
    encoded['result']['driver_chain'] = [deepcopy(row) for _ in range(100)]
    original = codec._walk
    def no_allocate(value, budget, depth=0, *, copy=False):
        assert not copy, 'expansion was allocated before rejecting its size'
        return original(value, budget, depth, copy=copy)
    monkeypatch.setattr(codec, '_walk', no_allocate)
    for limits in (codec.DecodeLimits(max_rows=1000), codec.DecodeLimits(max_bytes=100_000),
                   codec.DecodeLimits(max_nodes=20_000)):
        with pytest.raises(codec.DriverCodecLimitError):
            expand_compact_result(encoded, limits=limits)


def test_encoder_and_decoder_observe_prearmed_cancel(encoded):
    import threading
    from src.cancellation import push_cancel_event, pop_cancel_event, OperationCancelled
    event = threading.Event(); event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            encode(encoded['result'])
        with pytest.raises(OperationCancelled):
            expand_compact_result(encoded)
    finally:
        pop_cancel_event(token)
