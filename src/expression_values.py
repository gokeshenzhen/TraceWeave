"""Bounded four-state integral operations shared by queries and RTL evidence.

Values have a declared width; Python integers are only intermediate storage.
Type/context propagation belongs to the typed-expression builder.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import xor

from .cancellation import check_cancelled

MAX_WIDTH = 4096


@dataclass(frozen=True)
class Value:
    bits: str
    signed: bool = False

    def __post_init__(self):
        if not 1 <= len(self.bits) <= MAX_WIDTH or any(c not in '01xz' for c in self.bits):
            raise ValueError('expression_value_invalid')

    @property
    def width(self):
        return len(self.bits)

    @property
    def known(self):
        return not any(c in self.bits for c in 'xz')

    @property
    def truth(self):
        return '1' if '1' in self.bits else '0' if self.known else 'x'

    @property
    def integer(self):
        if not self.known:
            raise ValueError('expression_value_unknown')
        n = int(self.bits, 2)
        return n - (1 << self.width) if self.signed and self.bits[0] == '1' else n

    def resize(self, width, *, signed=None, sign_extend=None, two_state=False):
        _width(width)
        extend = self.signed if sign_extend is None else sign_extend
        bits = self.bits[-width:].rjust(width, self.bits[0] if extend else '0')
        if two_state:
            bits = bits.replace('x', '0').replace('z', '0')
        return Value(bits, self.signed if signed is None else signed)

    def public(self):
        return dict(bin=self.bits,
                    hex=f'0x{int(self.bits, 2):0{(self.width + 3)//4}x}' if self.known else None,
                    dec=self.integer if self.known else None)


def _width(width):
    if type(width) is not int or not 1 <= width <= MAX_WIDTH:
        raise ValueError('expression_width_limit')


def number(n, width=32, signed=False):
    _width(width)
    return Value(format(n & ((1 << width) - 1), f'0{width}b'), signed)


def unknown(width, signed=False):
    _width(width)
    return Value('x' * width, signed)


def _logic(op, a, b):
    if op == '&&':
        return '0' if '0' in (a, b) else '1' if a == b == '1' else 'x'
    if op == '||':
        return '1' if '1' in (a, b) else '0' if a == b == '0' else 'x'
    raise ValueError('expression_operator_invalid')


def _invert(bit):
    return {'0': '1', '1': '0'}.get(bit, 'x')


def unary(op, value):
    check_cancelled()
    if op == '+':
        return value
    if op == '-':
        return number(-value.integer, value.width, value.signed) if value.known else unknown(value.width, value.signed)
    if op == '!':
        return Value(_invert(value.truth))
    if op == '~':
        return Value(''.join(_invert(c) for c in value.bits), value.signed)
    if op in {'&', '~&'}:
        result = '0' if '0' in value.bits else '1' if value.known else 'x'
    elif op in {'|', '~|'}:
        result = value.truth
    elif op in {'^', '~^', '^~'}:
        result = str(reduce(xor, map(int, value.bits), 0)) if value.known else 'x'
    else:
        raise ValueError('expression_operator_invalid')
    return Value(_invert(result) if op in {'~&', '~|', '~^', '^~'} else result)


COMPARISONS = frozenset({'==', '!=', '===', '!==', '==?', '!=?', '<', '<=', '>', '>='})
LOGICAL = frozenset({'&&', '||', '->', '<->'})
LEFT_TYPED = frozenset({'<<', '>>', '<<<', '>>>', '**'})
BINARY = COMPARISONS | LOGICAL | LEFT_TYPED | {'+', '-', '*', '/', '%', '&', '|', '^', '~^', '^~'}


def binary(op, lhs, rhs, *, width=None):
    check_cancelled()
    if op not in BINARY:
        raise ValueError('expression_operator_invalid')
    if op in LOGICAL:
        a, b = lhs.truth, rhs.truth
        if op == '->':
            return Value(_logic('||', _invert(a), b))
        if op == '<->':
            return Value(str(int(a == b)) if a in '01' and b in '01' else 'x')
        return Value(_logic(op, a, b))
    common = max(lhs.width, rhs.width) if op not in LEFT_TYPED else lhs.width
    if op not in COMPARISONS:
        common = max(common, width or common)
    signed = lhs.signed if op in LEFT_TYPED else lhs.signed and rhs.signed
    a = lhs.resize(common, signed=signed, sign_extend=signed)
    b = rhs if op in LEFT_TYPED else rhs.resize(common, signed=signed, sign_extend=signed)
    out_width = 1 if op in COMPARISONS else width or common
    _width(out_width)
    if op in {'===', '!==', '==', '!=', '==?', '!=?'}:
        pairs = list(zip(a.bits, b.bits))
        if op in {'===', '!=='}:
            result = '1' if a.bits == b.bits else '0'
        else:
            if op in {'==?', '!=?'}:
                pairs = [(x, y) for x, y in pairs if y not in 'xz']
            if any(x in '01' and y in '01' and x != y for x, y in pairs):
                result = '0'
            elif any(x in 'xz' or y in 'xz' for x, y in pairs):
                result = 'x'
            else:
                result = '1'
        return Value(_invert(result) if op in {'!=', '!==', '!=?'} else result)
    if op in {'&', '|', '^', '~^', '^~'}:
        bits = []
        for x, y in zip(a.bits, b.bits):
            if op in {'&', '|'}:
                bit = _logic('&&' if op == '&' else '||',
                             x if x in '01' else 'x', y if y in '01' else 'x')
            else:
                bit = str(int(x) ^ int(y)) if x in '01' and y in '01' else 'x'
                if op != '^':
                    bit = _invert(bit)
            bits.append(bit)
        return Value(''.join(bits), signed).resize(out_width)
    if op in {'<<', '>>', '<<<', '>>>'}:
        if not b.known:
            return unknown(out_width, signed)
        # The shift count is interpreted as unsigned, including signed negatives.
        amount = min(int(b.bits, 2), common)
        if op in {'<<', '<<<'}:
            bits = a.bits[amount:] + '0' * amount
        else:
            pad = a.bits[0] if op == '>>>' and signed else '0'
            bits = pad * amount + a.bits[:common-amount]
        return Value(bits, signed).resize(out_width)
    if not a.known or not b.known:
        return unknown(out_width, signed if op not in COMPARISONS else False)
    x, y = a.integer, b.integer
    if op in {'<', '<=', '>', '>='}:
        return Value(str(int({'<': x < y, '<=': x <= y, '>': x > y, '>=': x >= y}[op])))
    if op in {'/', '%'}:
        if y == 0:
            return unknown(out_width, signed)
        quotient = (abs(x) // abs(y)) * (-1 if (x < 0) != (y < 0) else 1)
        result = quotient if op == '/' else x - quotient * y
    elif op == '**':
        if y < 0:
            if x == 0:
                return unknown(out_width, signed)
            result = 1 if x == 1 else (-1 if y % 2 else 1) if x == -1 else 0
        else:
            # Modular exponentiation never constructs an unbounded x**y.
            result = pow(x, y, 1 << out_width)
    else:
        result = {'+': lambda: x + y, '-': lambda: x - y, '*': lambda: x * y}[op]()
    check_cancelled()
    return number(result, out_width, signed)


def conditional(condition, lhs, rhs, *, width=None):
    width = width or max(lhs.width, rhs.width)
    signed = lhs.signed and rhs.signed
    lhs = lhs.resize(width, signed=signed, sign_extend=signed)
    rhs = rhs.resize(width, signed=signed, sign_extend=signed)
    if condition.truth in '01':
        return lhs if condition.truth == '1' else rhs
    return Value(''.join(a if a == b else 'x' for a, b in zip(lhs.bits, rhs.bits)), signed)


def concatenate(values):
    _width(sum(v.width for v in values))
    return Value(''.join(v.bits for v in values))


def stream(value, direction, size=1):
    if type(size) is not int or not 1 <= size <= MAX_WIDTH:
        raise ValueError('expression_stream_size_invalid')
    if direction == '>>':
        return Value(value.bits)
    if direction != '<<':
        raise ValueError('expression_operator_invalid')
    # Group from the right; a final partial group stays intact.
    return Value(''.join(value.bits[max(0, end-size):end]
                         for end in range(value.width, 0, -size)))


def builtin(name, args):
    check_cancelled()
    if not args:
        raise ValueError('expression_function_arity_invalid')
    v = args[0]
    if name in {'$signed', '$unsigned'} and len(args) == 1:
        return Value(v.bits, name == '$signed')
    if name == '$isunknown' and len(args) == 1:
        return number(int(not v.known), 1)
    if name in {'$onehot', '$onehot0'} and len(args) == 1:
        return number(int(v.bits.count('1') == 1 if name == '$onehot' else v.bits.count('1') <= 1), 1)
    if name == '$countones' and len(args) == 1:
        return number(v.bits.count('1'), 32, True)
    if name == '$countbits' and len(args) > 1:
        states = {a.bits[-1] for a in args[1:]}
        return number(sum(c in states for c in v.bits), 32, True)
    if name == '$clog2' and len(args) == 1:
        if not v.known:
            return unknown(32, True)
        n = int(v.bits, 2)
        return number((n - 1).bit_length() if n > 0 else 0, 32, True)
    raise ValueError('expression_function_invalid')
