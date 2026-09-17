"""Typed Slang AST projection for the supported dynamic evidence subset."""
from __future__ import annotations

from .dynamic_evidence import Assignment, Expr, TRUE, MAX_EXPR_NODES, MAX_EXPR_DEPTH, MAX_EXPR_WIDTH


def expression(projector, node, record, aliases, *, budget=None, depth=0):
    from .slang_connectivity_projector import _kind_name, _expression_width, _constant_expression_bits
    budget = [MAX_EXPR_NODES] if budget is None else budget
    budget[0] -= 1
    width = _expression_width(node) or 1
    if budget[0] < 0 or depth > MAX_EXPR_DEPTH or width > MAX_EXPR_WIDTH:
        return Expr("unsupported", 1, reason="dynamic_expression_limit")
    signed = bool(getattr(getattr(node, "type", None), "isSigned", False))
    def visit(n):
        return expression(projector, n, record, aliases, budget=budget, depth=depth+1)
    kind = _kind_name(node)
    constant = _constant_expression_bits(node, record.symbol)
    if constant:
        return Expr("const", len(constant), value="".join(constant), signed=signed)
    if kind == "Conversion":
        return Expr("cast", width, (visit(node.operand),), signed=signed)
    selection = projector._template_selection(node, record, aliases)
    if selection is not None:
        return Expr("signal", selection.width, signal=selection.symbol, bits=selection.bits, signed=signed)
    if kind == "UnaryOp" and node.op.name == "LogicalNot":
        return Expr("not", 1, (visit(node.operand),))
    if kind == "BinaryOp":
        op = {"LogicalAnd": "and", "LogicalOr": "or", "Equality": "eq", "Inequality": "ne"}.get(node.op.name)
        if op:
            return Expr(op, width, (visit(node.left), visit(node.right)), signed=signed)
    if kind == "ConditionalOp" and len(node.conditions) == 1:
        return Expr("mux", width, (visit(node.conditions[0].expr), visit(node.left), visit(node.right)), signed=signed)
    if kind == "Concatenation":
        return Expr("concat", width, tuple(visit(a) for a in node.operands), signed=signed)
    # Unsupported syntax retains structural dependencies without claiming values.
    refs = projector._template_expression_selections(node, record, aliases)
    return Expr("unsupported", width,
                tuple(Expr("signal", s.width, signal=s.symbol, bits=s.bits) for s in refs[:64]),
                reason="dynamic_evidence_unavailable")


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
