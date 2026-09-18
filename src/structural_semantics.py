"""Bounded facts from elaborated Slang symbols, without ConnectivityIR.

Facts are structural observations, never defect or trigger-probability verdicts.
Port bindings are per instance. Statement facts share an exact elaborated body
template; consumers resolve relative to each recorded instance binding.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import os

from .cancellation import check_cancelled
from .slang_connectivity_projector import (
    _constant_expression_bits, _definition_id, _expression_width,
    _parameterization, _unwrap_expression,
)

SEMANTIC_RULE_VERSION = "1"
SEMANTIC_CATEGORIES = ("constant_connection", "open_input", "constant_comparison")


@dataclass(frozen=True)
class ScanLimits:
    max_instances: int = 25_000
    max_facts: int = 20_000
    max_ast_nodes: int = 1_000_000
    max_width: int = 4096


class _Budget(Exception):
    pass


def _members(scope):
    for member in scope:
        check_cancelled()
        typ = type(member).__name__
        if typ == "GenerateBlockSymbol" and member.isUninstantiated:
            continue
        if typ in {"GenerateBlockSymbol", "GenerateBlockArraySymbol", "InstanceArraySymbol"}:
            yield from _members(member)
        else:
            yield member


def scan_semantic_session(session, *, scope=None, categories=SEMANTIC_CATEGORIES, limits=ScanLimits()):
    sm = session.driver.sourceManager
    facts, bindings = [], []
    counts = Counter()
    gaps = set()
    templates = {}
    template_count = 0
    nodes = 0

    def location(node):
        loc = getattr(getattr(node, "sourceRange", None), "start", None)
        if loc is None:
            loc = getattr(node, "location", None)
        if loc is None:
            return None
        try:
            return {"file": os.path.realpath(str(sm.getFileName(loc))), "line": int(sm.getLineNumber(loc))}
        except Exception:
            return None

    def text(node):
        syntax = getattr(node, "syntax", None)
        if syntax is None:
            syntax = getattr(_unwrap_expression(node), "syntax", None)
        return str(syntax).strip()[:512] if syntax is not None else ""

    def constant(expr, instance):
        if expr is None:
            return None
        width = _expression_width(expr)
        if width is None or width > limits.max_width:
            gaps.add("expression_width_unsupported")
            return None
        return _constant_expression_bits(expr, instance)

    def visit(node, fn):
        def bounded(n):
            nonlocal nodes
            nodes += 1
            if nodes > limits.max_ast_nodes:
                raise _Budget("ast_node_limit")
            check_cancelled()
            return fn(n)
        node.visit(bounded)

    def save(kind, node, **data):
        if kind not in categories:
            return
        if len(facts) >= limits.max_facts:
            raise _Budget("fact_limit")
        counts[kind] += 1
        facts.append({"kind": kind, "source": location(node), **data})

    def constant_parts(expr, instance, offset=0):
        """Contiguous constant regions in an expression, by LSB offset.

        Constant folding respects Slang's contextual sizing/sign extension.
        A mixed concat is decomposed only when its widths map exactly.
        """
        bits = constant(expr, instance)
        if bits:
            return [{"lsb_offset": offset, "width": len(bits), "bits": "".join(bits)}]
        if type(expr).__name__ == "ConversionExpression" and _expression_width(expr.operand) == _expression_width(expr):
            return constant_parts(expr.operand, instance, offset)
        if type(expr).__name__ == "ConcatenationExpression":
            parts = []
            for operand in reversed(tuple(expr.operands)):
                width = _expression_width(operand)
                if width is None or width + offset > limits.max_width:
                    gaps.add("expression_width_unsupported")
                    return []
                parts.extend(constant_parts(operand, instance, offset))
                offset += width
            return parts
        return []

    def connections(instance, path):
        for connection in instance.portConnections:
            check_cancelled()
            if getattr(getattr(connection.port, "direction", None), "name", "") != "In":
                continue
            expr = connection.expression
            port = str(connection.port.name)
            if expr is None:
                save("open_input", instance, instance=path, port=port,
                     interpretation="unconnected input; no implicit 0 or Z conclusion")
            else:
                parts = constant_parts(expr, instance)
                if parts:
                    save("constant_connection", expr, instance=path, port=port,
                         width=_expression_width(expr), constant_regions=parts,
                         expression=text(expr), origin="input_binding")

    def statements(instance, template):
        reusable = True
        for member in _members(instance.body):
            typ = type(member).__name__
            if typ not in {"ContinuousAssignSymbol", "ProceduralBlockSymbol"}:
                continue
            node = member.assignment if typ == "ContinuousAssignSymbol" else member.body
            if typ == "ContinuousAssignSymbol":
                parts = constant_parts(node.right, instance)
                if parts:
                    save("constant_connection", node, template=template,
                         target=text(node.left), width=_expression_width(node.left),
                         constant_regions=parts, origin="continuous_assignment")
            # Statement consumers are an explicitly conservative enclosing-block
            # set. The complete enclosing text preserves guards; this is not a
            # claim that every assignment is controlled by every comparison.
            consumers = set()
            comparisons = []
            def inspect(n):
                nonlocal reusable
                ntyp = type(n).__name__
                if ntyp == "HierarchicalValueExpression":
                    referenced = str(getattr(getattr(n, "symbol", None), "hierarchicalPath", ""))
                    if not referenced.startswith(str(instance.hierarchicalPath) + "."):
                        reusable = False
                if ntyp == "CallExpression" and not getattr(n, "isSystemCall", False) and _expression_width(n) and constant(n, instance):
                    reusable = False
                if ntyp == "AssignmentExpression":
                    consumers.add(text(n.left))
                if ntyp != "BinaryExpression" or n.op.name not in {"Equality", "Inequality", "CaseEquality", "CaseInequality"}:
                    return
                left, right = constant(n.left, instance), constant(n.right, instance)
                if bool(left) == bool(right):
                    return
                bits = left or right
                if len(bits) < 8:
                    return
                static = _unwrap_expression(n.left if left else n.right)
                symbol = getattr(static, "symbol", None)
                comparisons.append((n, bits, symbol, n.right if left else n.left))
            visit(node, inspect)
            for compare, bits, symbol, dynamic in comparisons:
                save("constant_comparison", compare, template=template,
                     operator=compare.op.name, width=len(bits), bits="".join(bits),
                     compared_expression=text(dynamic), constant_symbol=getattr(symbol, "name", None),
                     constant_source=location(symbol), enclosing_expression=text(node),
                     enclosing_consumers=sorted(consumers),
                     consumer_relation="direct_assignment" if typ == "ContinuousAssignSymbol" else "enclosing_block_candidates")
        return reusable

    pending = list(reversed(tuple(session.root.topInstances)))
    visited_instances = 0
    try:
        while pending:
            instance = pending.pop()
            visited_instances += 1
            if visited_instances > limits.max_instances:
                raise _Budget("instance_limit")
            path = str(instance.hierarchicalPath)
            admitted = not scope or path == scope or path.startswith(scope + ".")
            if admitted:
                # SourceLocation includes Slang's compilation buffer identity;
                # Python wrappers for the same Definition need not compare
                # equal. Type parameters conservatively keep instance templates
                # instead of equating different types by their printed names.
                parameters = _parameterization(instance)
                has_types = any(not p.isValue for p in instance.body.parameters)
                body_key = (str(instance.definition.location), parameters, path if has_types else None)
                cached = templates.get(body_key)
                if cached is None or not cached[1]:
                    template = _definition_id(instance, parameters, sm, None) + f":{template_count}"
                    template_count += 1
                    reusable = statements(instance, template)
                    templates[body_key] = (template, reusable)
                else:
                    template = cached[0]
                bindings.append({"instance": path, "template": template})
                if "." in path:
                    connections(instance, path)
            for child in reversed([m for m in _members(instance.body) if type(m).__name__ == "InstanceSymbol"]):
                child_path = str(child.hierarchicalPath)
                if not scope or child_path == scope or child_path.startswith(scope + ".") or scope.startswith(child_path + "."):
                    pending.append(child)
    except _Budget as exc:
        gaps.add(str(exc))
    diagnostics = session.diagnostic_payload
    if diagnostics.get("blocking_error_count"):
        gaps.add("frontend_diagnostics")
    if not bindings:
        gaps.add("scope_not_elaborated")
    return {
        "status": "partial" if gaps else "complete",
        "scope": scope, "categories_checked": list(categories), "gaps": sorted(gaps),
        "total_facts": len(facts), "counts": dict(counts), "facts": facts,
        "instance_bindings": bindings, "instances_visited": min(visited_instances, limits.max_instances),
        "template_count": template_count, "ast_nodes_visited": min(nodes, limits.max_ast_nodes),
        "blocking_diagnostics": int(diagnostics.get("blocking_error_count", 0)),
        "note": "Structural facts, not confirmed defects. No sequential-state inference or trigger-probability estimate.",
    }
