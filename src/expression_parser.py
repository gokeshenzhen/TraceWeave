"""Portable, bounded SV integral-expression syntax and type/context lowering."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re

from .cancellation import check_cancelled
from .dynamic_evidence import Expr, evaluate, MAX_EXPR_NODES, MAX_EXPR_DEPTH, MAX_EXPR_WIDTH
from .expression_values import BINARY, COMPARISONS, LOGICAL, LEFT_TYPED, number

MAX_TEXT = 16384
_TOKEN = re.compile(r"""\s+|//[^\n]*|/\*.*?\*/|(?:[0-9][0-9_]*)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+|'[01xXzZ]|[0-9][0-9_]*|\\[^\s]+|[a-zA-Z_$][a-zA-Z_0-9$]*|===|!==|==\?|!=\?|<<<|>>>|<->|\*\*|&&|\|\||<<|>>|<=|>=|==|!=|~&|~\||~\^|\^~|\+:|-:|::|->|[(){}\[\],.?:'+*/%&|^~!<>-]""", re.S)
_PREC = {'<->': 1, '->': 1, '||': 3, '&&': 4, '|': 5, '^': 6, '~^': 6, '^~': 6,
         '&': 7, '==': 8, '!=': 8, '===': 8, '!==': 8, '==?': 8, '!=?': 8,
         '<': 9, '<=': 9, '>': 9, '>=': 9, 'inside': 9,
         '<<': 10, '>>': 10, '<<<': 10, '>>>': 10, '+': 11, '-': 11,
         '*': 12, '/': 12, '%': 12, '**': 13}
_PREFIX = {'+', '-', '!', '~', '&', '~&', '|', '~|', '^', '~^', '^~'}


@dataclass(frozen=True)
class Syntax:
    op: str
    args: tuple = ()
    text: str = ''


class Parser:
    def __init__(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError('expression_text_limit')
        self.tokens = []
        position = 0
        for match in _TOKEN.finditer(text):
            if match.start() != position:
                raise ValueError(f'expression_invalid_token_at_{position}')
            position = match.end()
            token = match[0]
            if token.isspace() or token.startswith(('//', '/*')):
                continue
            self.tokens.append(token)
            if len(self.tokens) > MAX_EXPR_NODES * 8:
                raise ValueError('dynamic_expression_limit')
        if position != len(text):
            raise ValueError(f'expression_invalid_token_at_{position}')
        self.tokens.append('<end>')
        self.index = self.nodes = 0

    def peek(self):
        return self.tokens[self.index]

    def take(self, expected=None):
        token = self.peek()
        if expected is not None and token != expected:
            raise ValueError(f'expression_expected_{expected}')
        if token == '<end>':
            raise ValueError('expression_unexpected_end')
        self.index += 1
        return token

    def node(self, op, args=(), text=''):
        self.nodes += 1
        if self.nodes > MAX_EXPR_NODES:
            raise ValueError('dynamic_expression_limit')
        return Syntax(op, tuple(args), text)

    def expression(self, minimum=0, depth=0):
        check_cancelled()
        if depth > MAX_EXPR_DEPTH:
            raise ValueError('dynamic_expression_limit')
        token = self.take()
        if token in _PREFIX:
            node = self.node('unary', (self.expression(14, depth+1),), token)
        elif token == '(':
            node = self.expression(0, depth+1)
            self.take(')')
        elif token == '{':
            if self.peek() in {'<<', '>>'}:
                direction = self.take()
                size = self.node('literal', text='1') if self.peek() == '{' else self.expression(14, depth+1)
                self.take('{')
                children = self.list('}', depth+1)
                self.take('}')
                node = self.node('stream', (self.node('concat', children), size), direction)
            else:
                first = self.expression(0, depth+1)
                if self.peek() == '{':
                    self.take('{')
                    children = self.list('}', depth+1)
                    self.take('}')
                    node = self.node('repeat', (first, self.node('concat', children)))
                else:
                    children = [first]
                    while self.peek() == ',':
                        self.take(',')
                        children.append(self.expression(0, depth+1))
                    self.take('}')
                    node = self.node('concat', children)
        elif token[0].isdigit() or token.startswith("'"):
            node = self.node('literal', text=token)
        elif token.startswith('\\') or re.fullmatch(r'[a-zA-Z_$][a-zA-Z_0-9$]*', token):
            node = self.node('name', text=token)
        else:
            raise ValueError('expression_operand_expected')
        while True:
            token = self.peek()
            if token == "'" and node.op in {'literal', 'name'}:
                self.take("'")
                self.take('(')
                child = self.expression(0, depth+1)
                self.take(')')
                node = self.node('cast', (child,), node.text)
            elif token == '(' and node.op == 'name':
                self.take('(')
                node = self.node('call', self.list(')', depth+1), node.text)
            elif token in {'.', '::'}:
                sep = self.take()
                name = self.take()
                if not (name.startswith('\\') or re.fullmatch(r'[a-zA-Z_$][a-zA-Z_0-9$]*', name)):
                    raise ValueError('expression_member_expected')
                node = self.node('member', (node,), sep + name)
            elif token == '[':
                self.take('[')
                start = self.expression(0, depth+1)
                if self.peek() in {':', '+:', '-:'}:
                    kind = self.take()
                    end = self.expression(0, depth+1)
                    node = self.node('part', (node, start, end), kind)
                else:
                    node = self.node('index', (node, start))
                self.take(']')
            else:
                break
        while True:
            op = self.peek()
            if op == '?' and minimum <= 2:
                self.take('?')
                lhs = self.expression(0, depth+1)
                self.take(':')
                rhs = self.expression(2, depth+1)
                node = self.node('mux', (node, lhs, rhs))
                continue
            precedence = _PREC.get(op, -1)
            if precedence < minimum:
                break
            self.take()
            if op == 'inside':
                self.take('{')
                items = []
                while self.peek() != '}':
                    if self.peek() == '[':
                        self.take('[')
                        lo = self.expression(0, depth+1)
                        self.take(':')
                        hi = self.expression(0, depth+1)
                        self.take(']')
                        items.append(self.node('range', (lo,hi)))
                    else:
                        items.append(self.expression(0, depth+1))
                    if self.peek() != ',':
                        break
                    self.take(',')
                self.take('}')
                if not items:
                    raise ValueError('expression_empty_set')
                node = self.node('inside', (node, *items))
            else:
                rhs = self.expression(precedence if op in {'->', '<->'} else precedence+1, depth+1)
                node = self.node('binary', (node, rhs), op)
        return node

    def list(self, end, depth):
        result = []
        if self.peek() != end:
            result.append(self.expression(0, depth))
            while self.peek() == ',':
                self.take(',')
                result.append(self.expression(0, depth))
        self.take(end)
        if not result:
            raise ValueError('expression_empty_arguments')
        return tuple(result)

    def parse(self):
        node = self.expression()
        if self.peek() != '<end>':
            raise ValueError('expression_trailing_input')
        return node


@dataclass(frozen=True)
class Type:
    width: int
    signed: bool = False
    two_state: bool = False
    packed: tuple[tuple[int, int], ...] = ()
    unpacked: tuple[tuple[int, int], ...] = ()
    # Fields: (name, least-significant bit offset, Type).
    members: tuple = ()

    def __post_init__(self):
        if type(self.width) is not int or not 1 <= self.width <= MAX_EXPR_WIDTH:
            raise ValueError('expression_width_limit')
        dims = self.packed + self.unpacked
        if len(dims) > 8 or any(len(r) != 2 or any(type(i) is not int or abs(i) >= 2**63 for i in r) for r in dims):
            raise ValueError('expression_dimensions_invalid')
        if self.packed and math.prod(abs(a-b)+1 for a,b in self.packed) != self.width:
            raise ValueError('expression_packed_shape_invalid')
        if self.members and any(offset < 0 or offset + member.width > self.width
                                for _,offset,member in self.members):
            raise ValueError('expression_member_shape_invalid')

    @property
    def dimensions(self):
        return self.unpacked + self.packed


@dataclass(frozen=True)
class Typed:
    expr: Expr
    type: Type
    fill: bool = False


def _plain(width, signed=False, two_state=False):
    return Type(width, signed, two_state, ((width-1,0),) if width > 1 else ())


def literal(text):
    raw = text.replace('_', '').lower()
    if re.fullmatch("'[01xz]", raw):
        return Typed(Expr('const', 1, value=raw[1], fill=True), _plain(1), True)
    match = re.fullmatch(r"([0-9]*)'(s?)([bodh])([0-9a-fxz?]+)", raw)
    if match:
        digits, signed, base, payload = match.groups()
        radix = {'b':2,'o':8,'d':10,'h':16}[base]
        if any(c not in '0123456789abcdef'[:radix] + 'xz?' for c in payload):
            raise ValueError('expression_literal_invalid')
        if base == 'd':
            if any(c in 'xz?' for c in payload):
                if len(payload) != 1:
                    raise ValueError('expression_literal_invalid')
                bits = 'z' if payload == '?' else payload
            else:
                bits = bin(int(payload, radix))[2:]
        else:
            digit_width = {'b':1,'o':3,'h':4}[base]
            bits = ''.join(('z' if c == '?' else c) * digit_width if c in 'xz?'
                           else format(int(c,radix), f'0{digit_width}b') for c in payload)
        width = int(digits) if digits else max(32,len(bits))
        if not 1 <= width <= MAX_EXPR_WIDTH:
            raise ValueError('expression_width_limit')
        bits = bits[-width:].rjust(width, bits[0] if bits[0] in 'xz' else '0')
        return Typed(Expr('const', width, value=bits, signed=bool(signed)), _plain(width,bool(signed)))
    if not raw.isdigit() or len(raw) > 1233:
        raise ValueError('expression_literal_invalid')
    integer = int(raw)
    width = max(32, integer.bit_length())
    value = number(integer, width, True)
    return Typed(Expr('const', width, value=value.bits, signed=True), _plain(width,True))


_CONTEXT_OPS = {'+', '-', '*', '/', '%', '&', '|', '^', '~^', '^~'}


def context(value, width, signed):
    """Propagate an enclosing width before evaluating context-determined nodes."""
    node = value.expr
    if node.fill:
        return Typed(Expr('const', width, value=node.value[0] * width, signed=signed, fill=True), _plain(width,signed), True)
    if node.op in _CONTEXT_OPS:
        args = tuple(context(Typed(a, _plain(a.width,a.signed)), width, signed).expr for a in node.args)
        node = replace(node, width=width, signed=signed, args=args)
    elif node.op in LEFT_TYPED:
        left = context(Typed(node.args[0], _plain(node.args[0].width,node.args[0].signed)), width,signed).expr
        node = replace(node, width=width, signed=signed, args=(left,node.args[1]))
    elif node.op in {'u+', 'u-', '~'}:
        child = context(Typed(node.args[0], _plain(node.args[0].width,node.args[0].signed)),width,signed).expr
        node = replace(node, width=width, signed=signed, args=(child,))
    elif node.op == 'mux':
        args = tuple(context(Typed(a, _plain(a.width,a.signed)),width,signed).expr for a in node.args[1:])
        node = replace(node, width=width, signed=signed, args=(node.args[0],*args))
    else:
        # Common-operand unsigned conversion must happen before widening.
        child = replace(node, signed=signed)
        node = child if width == child.width else Expr('cast', width, (child,), signed=signed)
    return Typed(node, _plain(width,signed))


def constant(value):
    pending = [value.expr]
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        if count > MAX_EXPR_NODES or node.op in {'signal','unsupported','array','array_select'}:
            raise ValueError('expression_constant_required')
        pending.extend(node.args)
    result = evaluate(value.expr, lambda _: {})
    if result.value is None or any(c in result.value for c in 'xz'):
        raise ValueError('expression_known_constant_required')
    from .expression_values import Value
    return Value(result.value,value.type.signed).integer


def static_name(node):
    if node.op == 'name':
        return node.text
    if node.op == 'member':
        base = static_name(node.args[0])
        return base + node.text if base is not None else None
    if node.op == 'index' and node.args[1].op == 'literal':
        base = static_name(node.args[0])
        if base is not None and node.args[1].text.isdecimal():
            return base + '[' + node.args[1].text + ']'
    return None


class Compiler:
    """resolve(name) returns a Typed declaration, never an executable value."""
    def __init__(self, resolve, types=None):
        self.resolve = resolve
        self.types = types or {}
        self.count = 0

    def compile(self, text):
        result = self.lower(Parser(text).parse())
        # Validate expanded trees as well as the original syntax budget.
        from dataclasses import asdict
        Expr.from_dict(asdict(result.expr))
        if result.type.unpacked:
            raise ValueError('expression_array_requires_selection')
        return result

    def lower(self, node, depth=0):
        check_cancelled()
        self.count += 1
        if self.count > MAX_EXPR_NODES or depth > MAX_EXPR_DEPTH:
            raise ValueError('dynamic_expression_limit')
        def lower(n):
            return self.lower(n, depth+1)
        path = static_name(node)
        if path is not None:
            try:
                return self.resolve(path)
            except KeyError:
                if node.op == 'name':
                    raise ValueError('expression_signal_unresolved: ' + path) from None
        if node.op == 'literal':
            return literal(node.text)
        if node.op == 'unary':
            value = lower(node.args[0])
            if value.type.unpacked:
                raise ValueError('expression_array_requires_selection')
            op = 'u' + node.text if node.text in {'+','-'} else 'not' if node.text == '!' else (
                node.text if node.text == '~' else 'reduce_' + node.text)
            typ = _plain(1) if op == 'not' or op.startswith('reduce_') else _plain(value.type.width,value.type.signed)
            return Typed(Expr(op,typ.width,(value.expr,),signed=typ.signed),typ)
        if node.op == 'binary':
            a,b = map(lower,node.args)
            if a.type.unpacked or b.type.unpacked:
                raise ValueError('expression_array_requires_selection')
            op = node.text
            if op in LOGICAL:
                return Typed(Expr(op,1,(a.expr,b.expr)),_plain(1))
            if op in LEFT_TYPED:
                width,signed = a.type.width,a.type.signed
            else:
                width,signed = max(a.type.width,b.type.width),a.type.signed and b.type.signed
                a,b = context(a,width,signed),context(b,width,signed)
            typ = _plain(1) if op in COMPARISONS else _plain(width,signed)
            return Typed(Expr(op,typ.width,(a.expr,b.expr),signed=typ.signed),typ)
        if node.op == 'mux':
            cond,a,b = map(lower,node.args)
            if cond.type.unpacked or a.type.unpacked or b.type.unpacked:
                raise ValueError('expression_array_requires_selection')
            width,signed = max(a.type.width,b.type.width),a.type.signed and b.type.signed
            a,b = context(a,width,signed),context(b,width,signed)
            return Typed(Expr('mux',width,(cond.expr,a.expr,b.expr),signed=signed),_plain(width,signed))
        if node.op == 'concat':
            children = [lower(a) for a in node.args]
            if any(c.type.unpacked for c in children):
                raise ValueError('expression_concat_operand_invalid')
            width = sum(c.type.width for c in children)
            typ = _plain(width)
            return Typed(Expr('concat',width,tuple(c.expr for c in children)),typ)
        if node.op in {'repeat','stream'}:
            first,second = map(lower,node.args)
            count = constant(first if node.op == 'repeat' else second)
            if not 1 <= count <= MAX_EXPR_WIDTH:
                raise ValueError('expression_constant_size_invalid')
            width = count*second.type.width if node.op == 'repeat' else first.type.width
            op = 'repeat' if node.op == 'repeat' else 'stream_left' if node.text == '<<' else 'stream_right'
            typ = _plain(width)
            return Typed(Expr(op,width,(first.expr,second.expr)),typ)
        if node.op == 'cast':
            value = lower(node.args[0])
            target = node.text
            builtins = {'byte':(8,True,False), 'shortint':(16,True,False), 'int':(32,True,False),
                        'longint':(64,True,False), 'integer':(32,True,False),
                        'bit':(1,False,True), 'logic':(1,False,False), 'reg':(1,False,False),
                        'time':(64,False,False)}
            # SV integer atom types byte/shortint/int/longint are two-state.
            if target in {'byte','shortint','int','longint'}:
                width,signed,_ = builtins[target]
                typ = _plain(width,signed,True)
            elif target in builtins:
                typ = _plain(*builtins[target])
            elif target in {'signed','unsigned'}:
                typ = replace(value.type,signed=target == 'signed')
            elif target.replace('_','').isdigit():
                typ = _plain(int(target.replace('_','')),value.type.signed)
            elif target in self.types:
                typ = self.types[target]
            else:
                raise ValueError('expression_type_unresolved')
            if typ.unpacked or value.type.unpacked:
                raise ValueError('expression_array_cast_unsupported')
            # A sized/type cast supplies width context, but its signedness
            # applies to the completed conversion, not to its operand.
            inner = context(value,max(value.type.width,typ.width),value.type.signed)
            return Typed(Expr('cast',typ.width,(inner.expr,),signed=typ.signed,two_state=typ.two_state),typ)
        if node.op in {'index','part'}:
            base = lower(node.args[0])
            index = lower(node.args[1])
            if index.type.unpacked:
                raise ValueError('expression_index_not_integral')
            if base.type.unpacked:
                if node.op != 'index':
                    raise ValueError('expression_unpacked_slice_unsupported')
                typ = replace(base.type,unpacked=base.type.unpacked[1:])
                return Typed(Expr('array_select',typ.width,(base.expr,index.expr),
                    bounds=base.type.unpacked[0],signed=typ.signed,two_state=typ.two_state),typ)
            bounds = base.type.packed[0] if base.type.packed else (base.type.width-1,0)
            extent = abs(bounds[0]-bounds[1])+1
            stride = base.type.width//extent
            if node.op == 'index':
                typ = Type(stride,False,base.type.two_state,base.type.packed[1:])
                return Typed(Expr('select',stride,(base.expr,index.expr),bounds=bounds,stride=stride),typ)
            amount = lower(node.args[2])
            count = constant(amount)
            if node.text == ':':
                start = constant(index)
                if (start < count) != (bounds[0] < bounds[1]) and start != count:
                    raise ValueError('expression_part_direction_invalid')
                low = min(start,count)
                count = abs(start-count)+1
                index = Typed(Expr('const',64,value=number(low,64,True).bits,signed=True),_plain(64,True))
                op = 'part_up'
            else:
                op = 'part_up' if node.text == '+:' else 'part_down'
            if not 1 <= count <= MAX_EXPR_WIDTH//stride:
                raise ValueError('expression_part_width_invalid')
            width = count*stride
            return Typed(Expr(op,width,(base.expr,index.expr),bounds=bounds,stride=stride),_plain(width))
        if node.op == 'member':
            base = lower(node.args[0])
            name = node.text[1:]
            member = next((m for m in base.type.members if m[0] == name),None)
            if member is None or base.type.unpacked:
                raise ValueError('expression_member_type_unresolved')
            _,offset,typ = member
            positions = tuple(range(base.type.width-offset-typ.width,base.type.width-offset))
            return Typed(Expr('project',typ.width,(base.expr,),bits=positions,signed=typ.signed),typ)
        if node.op in {'inside','range'}:
            children = [lower(a) for a in node.args]
            width = 1 if node.op == 'inside' else max(c.type.width for c in children)
            return Typed(Expr(node.op,width,tuple(c.expr for c in children)),_plain(width))
        if node.op == 'call':
            args = [lower(a) for a in node.args]
            name = node.text
            if name in {'$bits','$dimensions','$unpacked_dimensions','$left','$right','$low','$high','$size','$increment'}:
                typ = args[0].type
                dims = typ.dimensions
                if name == '$bits' and len(args) == 1:
                    value = typ.width * math.prod(abs(a-b)+1 for a,b in typ.unpacked)
                elif name in {'$dimensions','$unpacked_dimensions'} and len(args) == 1:
                    value = len(dims if name == '$dimensions' else typ.unpacked)
                elif name not in {'$bits','$dimensions','$unpacked_dimensions'} and len(args) in {1,2}:
                    dim = constant(args[1]) if len(args) == 2 else 1
                    if not 1 <= dim <= len(dims):
                        return Typed(Expr('const',32,value='x'*32,signed=True),_plain(32,True))
                    left,right = dims[dim-1]
                    value = {'$left':left,'$right':right,'$low':min(left,right),'$high':max(left,right),
                             '$size':abs(left-right)+1,'$increment':1 if left>=right else -1}[name]
                else:
                    raise ValueError('expression_function_arity_invalid')
                return Typed(Expr('const',32,value=number(value,32,True).bits,signed=True),_plain(32,True))
            if any(a.type.unpacked for a in args):
                raise ValueError('expression_array_requires_selection')
            if name in {'$signed','$unsigned'}:
                typ = replace(args[0].type,signed=name == '$signed')
            elif name in {'$onehot','$onehot0','$isunknown'}:
                typ = _plain(1)
            else:
                typ = _plain(32,True)
            return Typed(Expr('function',typ.width,tuple(a.expr for a in args),
                              signed=typ.signed,function=name),typ)
        raise ValueError('expression_syntax_unsupported')
