"""Small four-state abstract vectors. '?' denotes dynamic, not an RTL X."""

from itertools import product


BITWISE = {'npiBitAndOp': 'and', 'npiBitOrOp': 'or', 'npiBitXorOp': 'xor', 'npiBitXNorOp': 'xnor'}
REDUCTIONS = {'npiUnaryAndOp': 'and', 'npiUnaryNandOp': 'nand', 'npiUnaryOrOp': 'or',
              'npiUnaryNorOp': 'nor', 'npiUnaryXorOp': 'xor', 'npiUnaryXNorOp': 'xnor'}


def possibilities(bit):
    return '01xz' if bit == '?' else bit


def joined(bits):
    values = set(bits)
    return next(iter(values)) if len(values) == 1 else '?'


def invert(bit):
    return {'0': '1', '1': '0'}.get(bit, 'x')


def binary_bit(op, a, b):
    if op == 'and':
        return '0' if '0' in (a, b) else '1' if a == b == '1' else 'x'
    if op == 'or':
        return '1' if '1' in (a, b) else '0' if a == b == '0' else 'x'
    bit = str(int(a != b)) if a in '01' and b in '01' else 'x'
    return invert(bit) if op == 'xnor' else bit


def truth(bits):
    if '1' in bits:
        return '1'
    if '?' in bits:
        return '01x' if not set(bits) & set('xz') else '1x'
    return 'x' if set(bits) & set('xz') else '0'


def operate(op, inputs):
    """Sound constant-bit transfer functions; return None for unmodeled forms.

    Binary bitwise and conditional branches must already have equal widths
    and the language-defined common signedness. Logical operands are unsized.
    """
    if op in BITWISE and len(inputs) == 2 and len(inputs[0]) == len(inputs[1]):
        return ''.join(joined(binary_bit(BITWISE[op], x, y)
            for x, y in product(possibilities(a), possibilities(b))) for a, b in zip(*inputs))
    if op == 'npiBitNegOp' and len(inputs) == 1:
        return ''.join(joined(invert(x) for x in possibilities(bit)) for bit in inputs[0])
    if op == 'npiNotOp' and len(inputs) == 1:
        return joined(invert(x) for x in truth(inputs[0]))
    if op in {'npiLogAndOp', 'npiLogOrOp'} and len(inputs) == 2:
        return joined(binary_bit('and' if op == 'npiLogAndOp' else 'or', a, b)
                      for a, b in product(truth(inputs[0]), truth(inputs[1])))
    if op in REDUCTIONS and len(inputs) == 1:
        kind = REDUCTIONS[op]
        negated = kind in {'nand', 'nor', 'xnor'}
        base = {'nand': 'and', 'nor': 'or', 'xnor': 'xor'}.get(kind, kind)
        states = {'1' if base == 'and' else '0'}
        for bit in inputs[0]:
            states = {binary_bit(base, a, b) for a, b in product(states, possibilities(bit))}
        return joined(invert(x) if negated else x for x in states)
    if op == 'npiConditionOp' and len(inputs) == 3 and len(inputs[1]) == len(inputs[2]):
        def conditional(c, a, b):
            return a if c == '1' else b if c == '0' else a if a == b else 'x'
        return ''.join(joined(conditional(c, x, y)
            for c, x, y in product(truth(inputs[0]), possibilities(a), possibilities(b)))
            for a, b in zip(inputs[1], inputs[2]))
    return None


def vector(size, regions):
    bits = ['?'] * size
    for row in regions:
        end = size - row['lsb_offset']
        bits[end-row['width']:end] = row['bits']
    return ''.join(bits)


def value(bits):
    regions, end = [], len(bits)
    while end:
        if bits[end-1] == '?':
            end -= 1
            continue
        start = end - 1
        while start and bits[start-1] != '?':
            start -= 1
        regions.append(dict(lsb_offset=len(bits)-end, width=end-start, bits=bits[start:end]))
        end = start
    return len(bits), regions, '?' not in bits
