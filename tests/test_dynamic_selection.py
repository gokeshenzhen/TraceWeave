"""Independent bit-position oracles for projected typed expressions."""
import pytest

from src.dynamic_evidence import Expr, TRUE, evaluate
from src.dynamic_selection import select_expression


@pytest.mark.parametrize('op', ['signal', 'mux', 'concat', 'cast'])
def test_projection_keeps_order_and_four_state_lanes(op):
    source = Expr('signal', 8, signal='top.data', bits=tuple(range(12, 4, -1)),
                  declared_bits=tuple(range(12, 4, -1)))
    if op == 'signal':
        expr = source
    elif op == 'mux':
        expr = Expr('mux', 8, (TRUE, source, Expr('const', 8, value='00000000')))
    elif op == 'concat':
        expr = Expr('concat', 8, (select_expression(source, (0, 1, 2, 3)),
                                 select_expression(source, (4, 5, 6, 7))))
    else:
        expr = Expr('cast', 8, (Expr('concat', 12, (Expr('const', 4, value='1111'), source)),))
    projected = select_expression(expr, (6, 0, 4, 2))
    result = evaluate(projected, lambda e: {'value': ''.join('01xz10zx'[12 - bit] for bit in e.bits)})
    assert result.value == 'z01x'
    assert set(result.gaps) == {'value_unknown'}
    assert [bit for d in result.dependencies for bit in d['bits']] == [6, 12, 8, 10]


def test_projection_budget_and_bad_coordinate_cannot_become_a_value():
    leaf = Expr('signal', 8, signal='d', bits=tuple(range(7, -1, -1)))
    expr = Expr('mux', 8, (TRUE, leaf, leaf))
    limited = select_expression(expr, (0,), _budget=[1])
    result = evaluate(limited, lambda _: {'value': '1'})
    assert result.value is None and 'dynamic_expression_limit' in result.gaps
    with pytest.raises(ValueError, match='dynamic_bit_mapping_unavailable'):
        select_expression(leaf, (8,))
