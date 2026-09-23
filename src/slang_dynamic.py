"""Typed Slang AST projection into the shared bounded expression contract."""
from __future__ import annotations

from dataclasses import replace

from .cancellation import check_cancelled
from .dynamic_evidence import Assignment, Expr, TRUE, MAX_EXPR_NODES, MAX_EXPR_DEPTH, MAX_EXPR_WIDTH
from .dynamic_selection import select_expression
from .expression_values import number

BINARY_OPS = {
    'Add':'+','Subtract':'-','Multiply':'*','Divide':'/','Mod':'%',
    'BinaryAnd':'&','BinaryOr':'|','BinaryXor':'^','BinaryXnor':'~^',
    'Equality':'==','Inequality':'!=','CaseEquality':'===','CaseInequality':'!==',
    'WildcardEquality':'==?','WildcardInequality':'!=?',
    'GreaterThanEqual':'>=','GreaterThan':'>','LessThanEqual':'<=','LessThan':'<',
    'LogicalAnd':'&&','LogicalOr':'||','LogicalImplication':'->','LogicalEquivalence':'<->',
    'LogicalShiftLeft':'<<','LogicalShiftRight':'>>','ArithmeticShiftLeft':'<<<','ArithmeticShiftRight':'>>>','Power':'**',
}
UNARY_OPS = {'Plus':'u+','Minus':'u-','BitwiseNot':'~','LogicalNot':'not',
    'BitwiseAnd':'reduce_&','BitwiseOr':'reduce_|','BitwiseXor':'reduce_^',
    'BitwiseNand':'reduce_~&','BitwiseNor':'reduce_~|','BitwiseXnor':'reduce_~^'}
FUNCTIONS = {'$signed','$unsigned','$clog2','$isunknown','$countones','$onehot','$onehot0','$countbits'}
TYPE_FUNCTIONS = {'$bits','$size','$left','$right','$low','$high','$increment','$dimensions','$unpacked_dimensions'}


def fixed_array_type(typ):
    """Bounded shape facts, without enumerating any storage elements."""
    dimensions = []
    while typ is not None and typ.canonicalType.isUnpackedArray:
        typ = typ.canonicalType
        if not typ.hasFixedRange or len(dimensions) >= 8:
            return None
        r = typ.fixedRange
        dimensions.append((int(r.left), int(r.right)))
        typ = typ.arrayElementType
    if (not dimensions or typ is None or not typ.isIntegral or
            not 1 <= int(typ.bitWidth) <= MAX_EXPR_WIDTH):
        return None
    typ = typ.canonicalType
    if typ.isPackedArray and int(typ.fixedRange.width) == int(typ.bitWidth):
        left,right = int(typ.fixedRange.left),int(typ.fixedRange.right)
    else:
        left,right = int(typ.bitWidth)-1,0
    bits = tuple(range(left,right + (-1 if left >= right else 1),-1 if left >= right else 1))
    return typ, tuple(dimensions), bits


def expression(projector,node,record,aliases,*,budget=None,depth=0):
    from .slang_connectivity_projector import _kind_name,_expression_width,_constant_expression_bits,_constant_int
    check_cancelled()
    budget = [MAX_EXPR_NODES] if budget is None else budget
    budget[0] -= 1
    kind = _kind_name(node)
    typ = getattr(node,'type',None)
    array = fixed_array_type(typ)
    width = (int(node.bitstreamWidth) if kind=='Streaming' else
             int(array[0].bitWidth) if array else _expression_width(node) or 1)
    if budget[0]<0 or depth>MAX_EXPR_DEPTH or width>MAX_EXPR_WIDTH:
        return Expr('unsupported',1,reason='dynamic_expression_limit')
    signed = bool(getattr(typ,'isSigned',False))
    two_state = typ is not None and not bool(getattr(typ,'isFourState',True))
    def visit(n):
        return expression(projector,n,record,aliases,budget=budget,depth=depth+1)
    def fail(reason='dynamic_evidence_unavailable'):
        refs = projector._template_expression_selections(node,record,aliases)
        return Expr('unsupported',width,tuple(Expr('signal',s.width,signal=s.symbol,bits=s.bits) for s in refs[:64]),reason=reason)
    if kind=='Conversion':
        operand = visit(node.operand)
        if getattr(node.conversionKind, 'name', '') == 'Propagated':
            operand = replace(operand, signed=signed)
        return Expr('cast',width,(operand,),signed=signed,two_state=two_state)
    if kind=='UnaryOp':
        op = UNARY_OPS.get(node.op.name)
        return Expr(op,width,(visit(node.operand),),signed=signed) if op else fail()
    if kind=='BinaryOp':
        op = BINARY_OPS.get(node.op.name)
        return Expr(op,width,(visit(node.left),visit(node.right)),signed=signed) if op else fail()
    if kind=='ConditionalOp' and len(node.conditions)==1:
        return Expr('mux',width,(visit(node.conditions[0].expr),visit(node.left),visit(node.right)),signed=signed)
    if kind=='Concatenation':
        return Expr('concat',width,tuple(visit(a) for a in node.operands))
    if kind=='Replication':
        return Expr('repeat',width,(visit(node.count),visit(node.concat)))
    if kind=='Streaming':
        if not node.isFixedSize or any(s.withExpr is not None for s in node.streams):
            return fail('streaming_shape_unresolved')
        parts = tuple(visit(s.operand) for s in node.streams)
        chunk = int(node.sliceSize)
        return Expr('stream_left' if chunk else 'stream_right',width,
            (Expr('concat',sum(a.width for a in parts),parts),Expr('const',32,value=number(chunk or 1).bits)))
    if kind=='Call' and bool(getattr(node,'isSystemCall',False)):
        name = str(node.subroutineName)
        if name in FUNCTIONS:
            return Expr('function',width,tuple(visit(a) for a in node.arguments),signed=signed,function=name)
        if name not in TYPE_FUNCTIONS:
            return fail()
    if kind=='Inside':
        return Expr('inside',1,(visit(node.left),*(visit(a) for a in node.rangeList)))
    if kind=='ValueRange':
        a,b = visit(node.left),visit(node.right)
        return Expr('range',max(a.width,b.width),(a,b))
    if kind=='MemberAccess':
        base = visit(node.value)
        offset = int(node.member.bitOffset)
        if offset<0 or offset+width>base.width:
            return fail('dynamic_bit_mapping_unavailable')
        return replace(select_expression(base,tuple(range(base.width-offset-width,base.width-offset))),signed=signed)
    if kind in {'ElementSelect','RangeSelect'}:
        base = visit(node.value)
        base_type = node.value.type.canonicalType
        if base_type.isUnpackedArray and kind=='ElementSelect' and fixed_array_type(base_type):
            r = base_type.fixedRange
            return Expr('array_select',width,(base,visit(node.selector)),
                        bounds=(int(r.left),int(r.right)),signed=signed,
                        two_state=not bool((array[0] if array else typ).isFourState))
        if not base_type.isIntegral:
            return fail('array_mapping_unavailable')
        bounds = base_type.fixedRange if base_type.hasFixedRange else None
        bounds = (int(bounds.left),int(bounds.right)) if bounds is not None else (base.width-1,0)
        stride = base.width//(abs(bounds[0]-bounds[1])+1)
        if kind=='ElementSelect':
            return Expr('select',width,(base,visit(node.selector)),bounds=bounds,stride=stride,signed=signed)
        right = _constant_int(node.right,record.symbol)
        if right is None:
            return fail('selection_width_unresolved')
        if node.selectionKind.name=='Simple':
            left = _constant_int(node.left,record.symbol)
            if left is None:
                return fail('selection_width_unresolved')
            start = Expr('const',64,value=number(min(left,right),64,True).bits,signed=True)
            op = 'part_up'
        else:
            start = visit(node.left)
            op = 'part_up' if node.selectionKind.name=='IndexedUp' else 'part_down'
        return Expr(op,width,(base,start),bounds=bounds,stride=stride,signed=signed)
    if array and kind in {'NamedValue','HierarchicalValue'}:
        path = projector._template_symbol_path(node,record,aliases)
        if path:
            leaf,dimensions,bits = array
            return Expr('array_ref',width,signal=path,bits=bits,declared_bits=bits,
                        dimensions=dimensions,signed=bool(leaf.isSigned),
                        two_state=not bool(leaf.isFourState))
    constant = _constant_expression_bits(node,record.symbol)
    if constant:
        return Expr('const',len(constant),value=''.join(constant),signed=signed)
    selection = projector._template_selection(node,record,aliases)
    if selection is not None:
        return Expr('signal',selection.width,signal=selection.symbol,bits=selection.bits,signed=signed,two_state=two_state)
    return fail()


def timing(projector, block, record, aliases):
    from .slang_connectivity_projector import _kind_name
    kind = block.procedureKind.name
    body = block.body
    if _kind_name(body) == "Timed":
        event = body.timing
        if _kind_name(event) == "SignalEvent" and getattr(event.edge, "name", "") in {"PosEdge", "NegEdge"}:
            clock = expression(projector, event.expr, record, aliases)
            if clock.op == "signal" and clock.width == 1 and not getattr(event, "iffCondition", None):
                return clock, "posedge" if event.edge.name == "PosEdge" else "negedge", ()
        if kind == "Always" and _kind_name(event) == "ImplicitEvent":
            return None, None, ()
        return None, None, ("temporal_context_unavailable",)
    if kind == "AlwaysComb":
        return None, None, ()
    return None, None, ("temporal_context_unavailable",)


def assignment(projector, node, record, aliases, *, process, order=0, guard=TRUE,
               clock=None, edge=None, gaps=()):
    rhs = expression(projector, node.right, record, aliases)
    nonblocking = bool(getattr(node, "isNonBlocking", False))
    if edge and not nonblocking:
        gaps = (*gaps, "sampling_order_unresolved")
    if getattr(node, "op", None) is not None or getattr(node, "timingControl", None) is not None:
        gaps = (*gaps, "dynamic_evidence_unavailable")
    return Assignment(process, order, guard, rhs, clock, edge, nonblocking, gaps)
