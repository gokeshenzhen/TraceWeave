"""Bounded projection of typed expressions by MSB-first bit positions."""
from dataclasses import replace
from itertools import groupby

from .cancellation import check_cancelled
from .dynamic_evidence import Expr, MAX_EXPR_DEPTH, MAX_EXPR_NODES


def select_expression(expr, positions, *, _budget=None, _depth=0):
    check_cancelled()
    budget = [MAX_EXPR_NODES] if _budget is None else _budget
    budget[0] -= 1
    if budget[0] < 0 or _depth > MAX_EXPR_DEPTH:
        return Expr('unsupported', len(positions), reason='dynamic_expression_limit')
    if not positions or any(i < 0 or i >= expr.width for i in positions):
        raise ValueError('dynamic_bit_mapping_unavailable')
    def select(child, indices):
        return select_expression(child, indices, _budget=budget, _depth=_depth + 1)
    if tuple(positions) == tuple(range(expr.width)):
        return expr
    if expr.op == 'signal':
        return replace(expr, width=len(positions), bits=tuple(expr.bits[i] for i in positions))
    if expr.op == 'const':
        return replace(expr, width=len(positions), value=''.join(expr.value[i] for i in positions))
    if expr.op == 'mux' and all(a.width == expr.width for a in expr.args[1:]):
        return replace(expr, width=len(positions), args=(expr.args[0], *(select(a, positions) for a in expr.args[1:])))
    if expr.op in {'concat', 'cast'} and not expr.two_state:
        if expr.op == 'concat':
            pieces = [(child, i) for child in expr.args for i in range(child.width)]
        else:
            child = expr.args[0]
            if expr.width <= child.width:
                pieces = [(child, i) for i in range(child.width - expr.width, child.width)]
            else:
                pad = (child, 0) if child.signed else (Expr('const', 1, value='0'), 0)
                pieces = [pad] * (expr.width - child.width) + [(child, i) for i in range(child.width)]
        if len(pieces) != expr.width:
            return Expr('unsupported', len(positions), reason='dynamic_bit_mapping_unavailable')
        groups = []
        for _, indices in groupby((pieces[i] for i in positions), key=lambda p: id(p[0])):
            entries = list(indices)
            groups.append(select(entries[0][0], tuple(p[1] for p in entries)))
        return Expr('concat', len(positions), tuple(groups))
    # Retain the entire typed operation: arithmetic carry and sign propagation
    # make independent narrowing of its operands unsound.
    return Expr('project', len(positions), (expr,), bits=tuple(positions))
