"""SV syntax and context sizing, checked against independent VCS vectors."""
import pytest

from src.dynamic_evidence import Expr, evaluate
from src.expression_parser import Compiler, Parser, Typed, Type
from tests.expression_oracle import cases


def compile_case(case):
    def resolve(name):
        raw = case['inputs'][name]
        width = raw['width']
        typ = Type(width, raw.get('signed',False), packed=((width-1,0),))
        return Typed(Expr('signal',width,signal=name,bits=tuple(range(width-1,-1,-1)),
                          signed=typ.signed),typ)
    return Compiler(resolve).compile(case['expression'])


@pytest.mark.parametrize('case',cases(),ids=lambda c:c['id'])
def test_simulator_vectors(case):
    compiled = compile_case(case)
    def sample(expr):
        bits = case['inputs'][expr.signal]['value']
        return {'value': ''.join(bits[len(bits)-1-i] for i in expr.bits)}
    result = evaluate(compiled.expr,sample)
    assert compiled.type.width == case['expected']['width']
    assert result.value == case['expected']['bin'], (compiled,result)


def calculate(text):
    compiled = Compiler(lambda n: {}[n]).compile(text)
    return evaluate(compiled.expr,lambda _: {}).value


def test_enclosing_context_extends_before_addition():
    assert calculate("9'(8'd255 + 8'd1)") == '100000000'
    assert calculate("9'd0 + (8'd255 + 8'd1)") == '100000000'
    assert calculate("8'd255 + 8'd1") == '00000000'
    assert calculate("8'b0 | '1") == '11111111'


@pytest.mark.parametrize('text', [
    "a = b", "a++", "a; b", "__import__('os')", "1 +", "a[3:idx]",
    "4097'b0", "0'b1", "4'b2", "{99999999{1'b1}}", "{<<0{8'b1}}",
    "foo(1)", "1 ? 0", "1'b0 /* unterminated", "()",
])
def test_invalid_and_effectful_expressions_rejected(text):
    def resolve(name):
        return Typed(Expr('signal',8,signal=name,bits=tuple(range(7,-1,-1))),
                     Type(8,packed=((7,0),)))
    with pytest.raises((ValueError, KeyError)):
        Compiler(resolve).compile(text)


def test_limits_precede_recursion_and_large_allocation():
    for text in ['('*100+'1'+')'*100, '+'.join(['1']*300), '1'*16385]:
        with pytest.raises(ValueError,match='limit'):
            calculate(text)


def test_declared_coordinates_and_member_type():
    base = Typed(Expr('signal',8,signal='bus',bits=tuple(range(-2,-10,-1))),
                 Type(8,packed=((-2,-9),)))
    compiled = Compiler(lambda _:base).compile('bus[-5]')
    assert evaluate(compiled.expr,lambda e:{'value':'1' if e.bits==(-5,) else '00010000'}).value == '1'
    field_type = Type(3,packed=((2,0),))
    aggregate = Typed(Expr('signal',8,signal='s',bits=tuple(range(7,-1,-1))),
                      Type(8,members=(('field',2,field_type),)))
    def resolve(name):
        if name != 's':
            raise KeyError(name)
        return aggregate
    compiled = Compiler(resolve).compile('s.field')
    assert evaluate(compiled.expr,lambda e:{'value':'11010100'}).value == '101'


def test_escaped_name_and_comments_are_not_code():
    tree = Parser('/* leading */ \\a+b  + 1 // tail').parse()
    assert tree.args[0].text == '\\a+b'


@pytest.mark.parametrize('bounds,expected', [((1, 0), ('1101', '0110')), ((0, 1), ('0110', '1101'))])
def test_packed_array_selection_retains_signed_member_layout(bounds, expected):
    delta = Type(4, signed=True, packed=((3, 0),))
    typ = Type(16, packed=(bounds, (7, 0)), members=(('delta', 0, delta),))
    base = Typed(Expr('signal', 16, signal='m', bits=tuple(range(15, -1, -1))), typ)
    def resolve(name):
        if name != 'm':
            raise KeyError(name)
        return base
    for index, bits in enumerate(expected):
        compiled = Compiler(resolve).compile(f'm[{index}].delta')
        assert compiled.type.signed
        def sample(expr):
            return {'value': ''.join('1010011010111101'[15 - bit] for bit in expr.bits)}
        assert evaluate(compiled.expr, sample).value == bits
    for invalid in ('m.delta', 'm[0][0].delta'):
        with pytest.raises(ValueError, match='member_type_unresolved'):
            Compiler(resolve).compile(invalid)


def test_packed_array_field_bounds_are_element_relative():
    with pytest.raises(ValueError, match='member_shape_invalid'):
        Type(16, packed=((1, 0), (7, 0)), members=(('bad', 7, Type(2)),))


@pytest.mark.parametrize('text',['++a','--a','a++','a--','$bits()','$signed()','$size(a,1,2)'])
def test_effects_and_invalid_function_arity_are_rejected(text):
    with pytest.raises(ValueError):
        Compiler(lambda _:Typed(Expr('signal',8,signal='a',bits=tuple(range(7,-1,-1))),Type(8))).compile(text)
