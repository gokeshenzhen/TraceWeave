"""Four-state/type tests with independent simulator-checked expected vectors."""
from dataclasses import asdict
import itertools
import threading

import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.dynamic_evidence import Expr, evaluate
from src.dynamic_selection import select_expression
from src.expression_values import Value, binary, unary, number, conditional, stream, builtin
from tests.expression_oracle import cases


@pytest.mark.parametrize('case', [c for c in cases() if c['id'] in {
    'shift_left', 'shift_right', 'arithmetic_right', 'signed_division', 'signed_remainder',
    'division_zero', 'power', 'relational', 'mixed_signed_relation', 'equality_known_mismatch',
    'case_equality', 'wildcard_equality', 'bit_and', 'bit_xor', 'logic_decisive',
    'logic_implication', 'logic_equivalence'}], ids=lambda c: c['id'])
def test_binary_against_simulator_vectors(case):
    left, op, right = case['expression'].split()
    def value(name):
        raw = case['inputs'][name]
        return Value(raw['value'], raw.get('signed', False))
    result = binary(op, value(left), value(right))
    assert result.bits == case['expected']['bin']
    assert result.width == case['expected']['width']


@pytest.mark.parametrize('a,b', itertools.product('01xz', repeat=2))
def test_bitwise_truth_tables(a, b):
    assert binary('&', Value(a), Value(b)).bits == (
        '0' if '0' in (a,b) else '1' if a == b == '1' else 'x')
    assert binary('|', Value(a), Value(b)).bits == (
        '1' if '1' in (a,b) else '0' if a == b == '0' else 'x')
    assert binary('===', Value(a), Value(b)).bits == str(int(a == b))


def test_context_width_precedes_arithmetic_and_mixed_sign_extension():
    assert binary('+', number(255, 8), number(1, 8), width=9).bits == '100000000'
    assert binary('+', number(-1, 4, True), number(1, 8)).integer == 16
    assert binary('+', number(-1, 4, True), number(1, 8, True)).integer == 0
    assert Value('1xz0', True).resize(8).bits == '11111xz0'
    assert Value('1xz0').resize(4, two_state=True).bits == '1000'


def test_unknown_mux_merges_equal_bits_without_unique_source():
    assert conditional(Value('x'), Value('1010'), Value('1000')).bits == '10x0'
    expr = Expr('mux', 4, (Expr('const', 1, value='x'),
        Expr('const', 4, value='1010'), Expr('const', 4, value='1000')))
    result = evaluate(expr, lambda _: {})
    assert result.value == '10x0' and result.reference is None
    assert 'guard_unresolved' in result.gaps


def test_functions_keep_four_state_information():
    value = Value('10xz1100')
    assert builtin('$countones', [value]).integer == 3
    assert builtin('$countbits', [value, Value('x'), Value('z'), Value('x')]).integer == 2
    assert builtin('$isunknown', [value]).bits == '1'
    assert unary('~', value).bits == '01xx0011'
    assert stream(Value('1011001'), '<<', 3).bits == '0010111'


def test_dynamic_selection_tracks_index_and_only_selected_data():
    source = Expr('signal', 8, signal='a', bits=tuple(range(15, 7, -1)),
                  declared_bits=tuple(range(15, 7, -1)))
    index = Expr('signal', 4, signal='idx', bits=(3,2,1,0))
    expr = Expr('select', 1, (source, index), bounds=(15,8))
    seen = []
    def sample(node):
        seen.append((node.signal, node.bits))
        return {'value': '1011' if node.signal == 'idx' else '1'}
    result = evaluate(Expr.from_dict(asdict(expr)), sample)
    assert result.value == '1'
    assert seen == [('idx', (3,2,1,0)), ('a', (11,))]
    assert result.dependencies[0]['role'] == 'index'
    assert result.reference == ('a', (11,))


def test_unknown_index_does_not_invent_selected_data():
    source = Expr('signal', 8, signal='a', bits=tuple(range(7,-1,-1)))
    expr = Expr('select', 1, (source, Expr('const', 3, value='x00')))
    result = evaluate(expr, lambda _: pytest.fail('no unique selected data'))
    assert result.value == 'x' and 'index_unknown' in result.gaps


def test_arithmetic_projection_preserves_carry():
    expr = Expr('+', 8, (Expr('const', 8, value='00001111'), Expr('const', 8, value='00000001')))
    projected = select_expression(expr, (3,))
    assert evaluate(projected, lambda _: {}).value == '1'
    assert projected.args[0].width == 8


def test_budget_and_cancellation_apply_before_expensive_operations():
    with pytest.raises(ValueError, match='width'):
        number(1, 100000000)
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            binary('**', number(2,4096), number(2**4095,4096))
    finally:
        pop_cancel_event(token)
