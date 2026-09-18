"""Bounded combinational constant propagation over an elaborated design.

This is a small abstract interpreter, not a simulator or a ConnectivityIR
builder. '?' means insufficient evidence, '!' a conflicting writer, and x/z
are actual four-state constants. All writers are inventoried before any fact
is emitted; an interrupted inventory can never prove a prefix constant.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import os

from .cancellation import check_cancelled
from .slang_connectivity_projector import (
    _constant_expression_bits, _expression_width, _selected_bits,
)
from .structural_semantics import _members


class _Stop(Exception):
    pass


@dataclass(frozen=True)
class Expr:
    kind: str
    width: int
    args: tuple


def _truth(bits):
    if "!" in bits:
        return "!"
    if "1" in bits:
        return "1"
    if "?" in bits:
        return "?"
    return "x" if any(b in "xz" for b in bits) else "0"


def _bit(op, a, b):
    if "!" in (a, b):
        return "!"
    if op == "BinaryAnd" and "0" in (a, b):
        return "0"
    if op == "BinaryOr" and "1" in (a, b):
        return "1"
    if "?" in (a, b):
        return "?"
    if any(v in "xz" for v in (a, b)):
        return "x"
    if op == "BinaryAnd":
        return str(int(a) & int(b))
    if op == "BinaryOr":
        return str(int(a) | int(b))
    return str((int(a) ^ int(b)) ^ int(op == "BinaryXnor"))


def _invert(bits):
    return "".join({"0": "1", "1": "0", "z": "x"}.get(b, b) for b in bits)


def _resize(bits, width, signed=False, four_state=True):
    bits = (bits[0] if signed else "0") * max(0, width-len(bits)) + bits[-width:]
    return bits if four_state else bits.replace("x", "0").replace("z", "0")


def _integer(bits, signed):
    value = int(bits, 2)
    return value - (1 << len(bits)) if signed and bits[0] == "1" else value


def evaluate(expr, values, tick):
    tick(expr.width)
    kind, width, args = expr.kind, expr.width, expr.args
    if kind == "constant":
        return args[0]
    if kind == "unknown":
        return "?" * width
    if kind == "signal":
        return "".join(values[args[0]])
    if kind == "select":
        value = evaluate(args[0], values, tick)
        return "".join(value[i] for i in args[1])
    if kind == "concat":
        return "".join(evaluate(a, values, tick) for a in args)
    if kind == "convert":
        return _resize(evaluate(args[0], values, tick), width, args[1], args[2])
    if kind == "mux":
        condition = _truth(evaluate(args[0], values, tick))
        if condition in "01":
            return evaluate(args[1 if condition == "1" else 2], values, tick)
        left, right = (evaluate(a, values, tick) for a in args[1:])
        if condition == "!":
            return "!" * width
        return "".join(a if a == b else "!" if "!" in (a, b) else "?" if "?" in (a, b, condition) else "x"
                       for a, b in zip(left, right))
    if kind == "unary":
        op, child = args
        value = evaluate(child, values, tick)
        if op == "Plus":
            return value
        if op == "BitwiseNot":
            return _invert(value)
        if op == "LogicalNot":
            return _invert(_truth(value))
        reductions = {"BitwiseAnd": "BinaryAnd", "BitwiseNand": "BinaryAnd",
                      "BitwiseOr": "BinaryOr", "BitwiseNor": "BinaryOr",
                      "BitwiseXor": "BinaryXor", "BitwiseXnor": "BinaryXor"}
        if op in reductions:
            result = "1" if reductions[op] == "BinaryAnd" else "0"
            for bit in value:
                result = _bit(reductions[op], result, bit)
            return _invert(result) if op in {"BitwiseNand", "BitwiseNor", "BitwiseXnor"} else result
        if all(b in "01" for b in value):
            return format((-int(value, 2)) & ((1 << width)-1), f"0{width}b")
        return ("!" if "!" in value else "?" if "?" in value else "x") * width
    op, left_expr, right_expr, signed = args
    left, right = evaluate(left_expr, values, tick), evaluate(right_expr, values, tick)
    if op in {"BinaryAnd", "BinaryOr", "BinaryXor", "BinaryXnor"}:
        return "".join(_bit(op, a, b) for a, b in zip(left, right))
    if op in {"LogicalAnd", "LogicalOr"}:
        return _bit("BinaryAnd" if op == "LogicalAnd" else "BinaryOr", _truth(left), _truth(right))
    joined = left + right
    if "!" in joined:
        return "!" * width
    if "?" in joined:
        return "?" * width
    if op in {"CaseEquality", "CaseInequality"}:
        return str(int((left == right) != (op == "CaseInequality")))
    if op in {"Equality", "Inequality"}:
        # IEEE logical equality can disprove equality using a definite unequal
        # bit even when another bit is X/Z. An X is an actual value, not a
        # conservative substitute for a proven 0.
        if any(a in "01" and b in "01" and a != b for a, b in zip(left, right)):
            return "1" if op == "Inequality" else "0"
    if any(b in "xz" for b in joined):
        return "x" * width
    a, b = _integer(left, signed), _integer(right, signed)
    if op in {"Equality", "Inequality", "LessThan", "LessThanEqual", "GreaterThan", "GreaterThanEqual"}:
        return str(int({"Equality": a == b, "Inequality": a != b,
                        "LessThan": a < b, "LessThanEqual": a <= b,
                        "GreaterThan": a > b, "GreaterThanEqual": a >= b}[op]))
    if op == "Add":
        result = a + b
    elif op == "Subtract":
        result = a - b
    else:
        result = a * b
    return format(result & ((1 << width)-1), f"0{width}b")


_BINARY = {"BinaryAnd", "BinaryOr", "BinaryXor", "BinaryXnor", "LogicalAnd", "LogicalOr",
           "Equality", "Inequality", "CaseEquality", "CaseInequality", "LessThan", "LessThanEqual",
           "GreaterThan", "GreaterThanEqual", "Add", "Subtract", "Multiply"}
_UNARY = {"Plus", "Minus", "BitwiseNot", "LogicalNot", "BitwiseAnd", "BitwiseNand",
          "BitwiseOr", "BitwiseNor", "BitwiseXor", "BitwiseXnor"}
_READ_ONLY_CALLS = {"$display", "$write", "$strobe", "$monitor", "$finish", "$stop", "$fatal",
                    "$error", "$warning", "$info", "$time", "$realtime", "$bits", "$clog2",
                    "$signed", "$unsigned", "$isunknown", "$countones", "$onehot", "$onehot0",
                    "$timeformat", "$printtimescale", "$dumpfile", "$dumpvars", "$fsdbDumpfile", "$fsdbDumpvars"}


def propagate_session(session, *, scope, categories, limits):
    engine = _Propagation(session, limits, scope, categories)
    return engine.run()


class _Propagation:
    def __init__(self, session, limits, scope, categories):
        self.session, self.limits, self.scope, self.categories = session, limits, scope, categories
        self.signals, self.owners, self.values = {}, {}, {}
        self.drivers, self.controls, self.facts = [], [], []
        self.net_port_signals = set()
        self.gaps = set()
        self.nodes = self.bits = self.target_bits = self.steps = self.instances = 0
        self.boundaries = 0
        self.inventory_complete = False

    def tick(self):
        check_cancelled()
        self.nodes += 1
        if self.nodes > self.limits.max_ast_nodes:
            raise _Stop("propagation_ast_limit")

    def step(self, cost=1):
        check_cancelled()
        self.steps += cost
        if self.steps > self.limits.max_propagation_steps:
            raise _Stop("propagation_work_limit")

    def location(self, node):
        loc = getattr(getattr(node, "sourceRange", None), "start", getattr(node, "location", None))
        if loc is None:
            return None
        sm = self.session.driver.sourceManager
        return {"file": os.path.realpath(str(sm.getFileName(loc))), "line": int(sm.getLineNumber(loc))}

    def text(self, node):
        return str(getattr(node, "syntax", None) or "").strip()[:512]

    def signal(self, symbol):
        if type(symbol).__name__ not in {"NetSymbol", "VariableSymbol"} or not symbol.type.isIntegral:
            return None
        width = int(symbol.type.bitWidth)
        if not 0 < width <= self.limits.max_width:
            self.gaps.add("propagation_width_unsupported")
            return None
        name = str(symbol.hierarchicalPath)
        if name not in self.signals:
            self.bits += width
            if self.bits > self.limits.max_propagation_bits:
                raise _Stop("propagation_bit_limit")
            self.signals[name] = symbol
            self.values[name] = ["?"] * width
            self.owners[name] = [None] * width
            if type(symbol).__name__ == "NetSymbol" and symbol.netType.netKind.name not in {"Wire", "Tri", "UWire"}:
                self.owners[name] = [-2] * width
                self.gaps.add("propagation_net_resolution_boundary")
        return name

    def positions(self, expression, instance):
        width = _expression_width(expression.value)
        if width is None or width > self.limits.max_width or not expression.value.type.isIntegral:
            return ()
        rng = expression.value.type.getBitVectorRange()
        step = -1 if rng.left > rng.right else 1
        bits = tuple(range(rng.left, rng.right + step, step))
        if len(bits) != width:
            return ()  # Packed structs / multidimensional packed arrays need a separate mapper.
        selected = _selected_bits(expression, bits, instance)
        offsets = {bit: i for i, bit in enumerate(bits)}
        return tuple(offsets[b] for b in selected)

    def expression(self, node, instance, dependencies, depth=0):
        self.tick()
        width = _expression_width(node)
        if depth > 128:
            raise _Stop("propagation_expression_depth_limit")
        if width is None or not 0 < width <= self.limits.max_width:
            self.gaps.add("propagation_expression_unsupported")
            return None
        bits = _constant_expression_bits(node, instance)
        if bits:
            return Expr("constant", width, ("".join(bits),))
        typ = type(node).__name__
        def child(n):
            return self.expression(n, instance, dependencies, depth+1)
        if typ in {"NamedValueExpression", "HierarchicalValueExpression"}:
            name = self.signal(node.symbol)
            if name:
                dependencies.add(name)
                return Expr("signal", width, (name,))
        elif typ == "ConversionExpression" and node.type.isIntegral and node.operand.type.isIntegral:
            operand = child(node.operand)
            if operand:
                return Expr("convert", width, (operand, bool(node.operand.type.isSigned), bool(node.type.isFourState)))
        elif typ in {"ElementSelectExpression", "RangeSelectExpression"}:
            operand, positions = child(node.value), self.positions(node, instance)
            if operand and len(positions) == width:
                return Expr("select", width, (operand, positions))
        elif typ == "ConcatenationExpression":
            operands = tuple(child(n) for n in node.operands)
            if all(operands) and sum(n.width for n in operands) == width:
                return Expr("concat", width, operands)
        elif typ == "UnaryExpression" and node.op.name in _UNARY:
            operand = child(node.operand)
            if operand:
                return Expr("unary", width, (node.op.name, operand))
        elif typ == "BinaryExpression" and node.op.name in _BINARY:
            left, right = child(node.left), child(node.right)
            # Slang supplies context-sized operands. Refuse an unexpected
            # mismatch rather than inventing a signed/unsigned conversion.
            if left and right and (left.width == right.width or node.op.name in {"LogicalAnd", "LogicalOr"}):
                return Expr("binary", width, (node.op.name, left, right, bool(node.left.type.isSigned and node.right.type.isSigned)))
        elif typ == "ConditionalExpression" and len(node.conditions) == 1 and node.conditions[0].pattern is None:
            operands = (child(node.conditions[0].expr), child(node.left), child(node.right))
            if all(operands) and operands[1].width == operands[2].width == width:
                return Expr("mux", width, operands)
        self.gaps.add("propagation_expression_unsupported")
        return Expr("unknown", width, ())

    def targets(self, node, instance, depth=0):
        self.tick()
        if depth > 128:
            raise _Stop("propagation_expression_depth_limit")
        typ = type(node).__name__
        if typ in {"NamedValueExpression", "HierarchicalValueExpression"}:
            name = self.signal(node.symbol)
            if name:
                return tuple((name, i) for i in range(len(self.values[name])))
        elif typ in {"ElementSelectExpression", "RangeSelectExpression"}:
            base = self.targets(node.value, instance, depth+1)
            positions = self.positions(node, instance)
            if base and positions and all(i < len(base) for i in positions):
                return tuple(base[i] for i in positions)
        elif typ == "ConcatenationExpression":
            parts = tuple(self.targets(n, instance, depth+1) for n in node.operands)
            if all(parts):
                return tuple(t for part in parts for t in part)
        return ()

    def claim(self, targets, owner):
        self.target_bits += len(targets)
        if self.target_bits > self.limits.max_propagation_bits * 2:
            raise _Stop("propagation_driver_bit_limit")
        for name, bit in targets:
            previous = self.owners[name][bit]
            self.owners[name][bit] = owner if previous is None or previous == owner == -2 else -1

    def block(self, node, instance):
        targets = self.targets(node, instance)
        if targets:
            self.claim(targets, -2)
        else:
            names = set()
            def inspect(n):
                self.tick()
                if type(n).__name__ in {"NamedValueExpression", "HierarchicalValueExpression"}:
                    name = self.signal(n.symbol)
                    if name:
                        names.add(name)
            node.visit(inspect)
            for name in names:
                self.claim(tuple((name, i) for i in range(len(self.values[name]))), -2)
            self.gaps.add("propagation_write_target_unsupported")
        self.boundaries += 1

    def driver(self, targets, node, instance, origin, source_node=None, direct_expr=None):
        dependencies = set()
        expression = direct_expr or self.expression(node, instance, dependencies)
        if direct_expr is not None and direct_expr.kind == "signal":
            dependencies.add(direct_expr.args[0])
        if not targets or expression is None or len(targets) != expression.width:
            self.gaps.add("propagation_binding_unsupported")
            if targets:
                self.claim(targets, -2)
            return
        index = len(self.drivers)
        self.claim(targets, index)
        self.drivers.append((targets, expression, dependencies, self.location(source_node or node), origin))

    def inspect_body(self, node, instance, procedural, consumers, *, continuous_root=False):
        def inspect(n):
            self.tick()
            typ = type(n).__name__
            if typ == "CallExpression":
                name = str(n.subroutineName)
                if not getattr(n, "isSystemCall", False) or name not in _READ_ONLY_CALLS:
                    # Unknown calls can write ref arguments or global state.
                    # Without a write-effect summary even other assignments
                    # cannot be proved exclusive drivers.
                    raise _Stop("propagation_call_effects_unmodeled")
            if typ == "AssignmentExpression" and not (continuous_root and n == node):
                self.block(n.left, instance)
                consumers.add(self.text(n.left))
            if typ == "UnaryExpression" and n.op.name not in _UNARY:
                self.block(n.operand, instance)
            if typ in {"ProceduralAssignStatement", "ProceduralDeassignStatement"}:
                raise _Stop("propagation_force_release_boundary")
            conditions = []
            role = None
            if typ in {"ConditionalExpression", "ConditionalStatement"}:
                if len(n.conditions) != 1 or any(c.pattern is not None for c in n.conditions):
                    self.gaps.add("propagation_condition_unsupported")
                    return
                conditions = [c.expr for c in n.conditions]
                role = "mux_select" if typ == "ConditionalExpression" else "if_guard"
            elif typ == "BinaryExpression" and n.op.name in {"Equality", "Inequality", "CaseEquality", "CaseInequality"}:
                conditions, role = [n], "comparison"
            if "constant_control" in self.categories:
                for condition in conditions:
                    deps = set()
                    expr = self.expression(condition, instance, deps)
                    if expr:
                        self.controls.append((expr, condition, str(instance.hierarchicalPath), consumers, role, procedural))
        node.visit(inspect)

    def inventory(self):
        pending = list(reversed(tuple(self.session.root.topInstances)))
        while pending:
            check_cancelled()
            instance = pending.pop()
            self.instances += 1
            if self.instances > self.limits.max_instances:
                raise _Stop("propagation_instance_limit")
            path = str(instance.hierarchicalPath)
            for member in _members(instance.body):
                self.tick()
                typ = type(member).__name__
                if typ == "InstanceSymbol":
                    pending.append(member)
                    if len(pending) + self.instances > self.limits.max_instances:
                        raise _Stop("propagation_instance_limit")
                elif typ == "PrimitiveInstanceSymbol":
                    raise _Stop("propagation_primitive_boundary")
                elif typ in {"NetAliasSymbol", "ClockingBlockSymbol", "CheckerInstanceSymbol"}:
                    raise _Stop("propagation_external_write_boundary")
                elif typ in {"NetSymbol", "VariableSymbol"} and member.initializer is not None:
                    self.inspect_body(member.initializer, instance, False, set())
                    name = self.signal(member)
                    if name:
                        targets = tuple((name, i) for i in range(len(self.values[name])))
                        if typ == "NetSymbol":
                            self.driver(targets, member.initializer, instance, "net_initializer")
                        else:
                            self.claim(targets, -2)
                            self.boundaries += 1
                elif typ in {"ContinuousAssignSymbol", "ProceduralBlockSymbol"}:
                    node = member.assignment if typ == "ContinuousAssignSymbol" else member.body
                    consumers = {self.text(node.left)} if typ == "ContinuousAssignSymbol" else set()
                    self.inspect_body(node, instance, typ == "ProceduralBlockSymbol", consumers,
                                      continuous_root=typ == "ContinuousAssignSymbol")
                    if typ == "ContinuousAssignSymbol":
                        targets = self.targets(node.left, instance)
                        if targets:
                            self.driver(targets, node.right, instance, "continuous_assignment", node)
                        else:
                            self.block(node.left, instance)
                elif typ == "PortSymbol" and "." not in path and getattr(member.direction, "name", "") in {"In", "InOut", "Ref"}:
                    name = self.signal(member.internalSymbol)
                    if name:
                        self.claim(tuple((name, i) for i in range(len(self.values[name]))), -2)
            for connection in instance.portConnections:
                self.tick()
                port, node = connection.port, connection.expression
                if type(port).__name__ != "PortSymbol":
                    raise _Stop("propagation_interface_boundary")
                name = self.signal(port.internalSymbol)
                if not name:
                    raise _Stop("propagation_port_type_boundary")
                targets = tuple((name, i) for i in range(len(self.values[name])))
                if type(port.internalSymbol).__name__ == "NetSymbol":
                    self.net_port_signals.add(name)
                direction = port.direction.name
                if direction == "In":
                    if node is not None:
                        self.inspect_body(node, instance, False, set())
                        self.driver(targets, node, instance, "input_binding")
                    # An open input remains '?', never an implicit 0 or Z.
                elif direction == "Out":
                    if node is not None:
                        if type(node).__name__ != "AssignmentExpression":
                            raise _Stop("propagation_output_binding_unsupported")
                        external = self.targets(node.left, instance)
                        if not external:
                            self.block(node.left, instance)
                        else:
                            if type(port.internalSymbol).__name__ == "NetSymbol":
                                self.net_port_signals.update(n for n, _ in external
                                    if type(self.signals[n]).__name__ == "NetSymbol")
                            # Slang's RHS is an empty argument, optionally
                            # wrapped in a sizing conversion. Recreate only
                            # ordinary integral output sizing here.
                            expr = Expr("signal", len(targets), (name,))
                            if len(external) != len(targets):
                                expr = Expr("convert", len(external), (expr, bool(port.type.isSigned), bool(node.left.type.isFourState)))
                            before = len(self.drivers)
                            self.driver(external, node, instance, "output_binding", direct_expr=expr)
                            if len(self.drivers) > before:
                                self.drivers[-1][2].add(name)
                else:
                    self.claim(targets, -2)
                    if node is not None:
                        self.block(node, instance)
                    self.gaps.add("propagation_bidirectional_boundary")
        # Collapsed net ports can carry a reverse drive when another writer is
        # introduced. A directional edge model cannot localize that conflict;
        # refuse propagation rather than leave an upstream tie falsely proven.
        if any(-1 in self.owners[name] for name in self.net_port_signals):
            raise _Stop("propagation_port_alias_conflict")
        self.inventory_complete = True

    def solve(self):
        users = defaultdict(set)
        for index, (_, _, dependencies, _, _) in enumerate(self.drivers):
            for name in dependencies:
                users[name].add(index)
        for name, owners in self.owners.items():
            for bit, owner in enumerate(owners):
                if owner == -1:
                    self.values[name][bit] = "!"
        if any("!" in value for value in self.values.values()):
            self.gaps.add("propagation_multiple_drivers")
        queue, queued = deque(range(len(self.drivers))), set(range(len(self.drivers)))
        while queue:
            self.step()
            index = queue.popleft()
            queued.remove(index)
            targets, expr, _, _, _ = self.drivers[index]
            result = evaluate(expr, self.values, self.step)
            changed = set()
            for (name, bit), value in zip(targets, result):
                if self.owners[name][bit] != index or value == "?":
                    continue
                old = self.values[name][bit]
                new = value if old == "?" else old if old == value else "!"
                if new != old:
                    self.values[name][bit] = new
                    changed.add(name)
            for name in sorted(changed):
                for user in sorted(users[name]):
                    if user not in queued:
                        queued.add(user)
                        queue.append(user)

    def admitted(self, name):
        return not self.scope or name == self.scope or name.startswith(self.scope + ".")

    def save(self, fact):
        if len(self.facts) >= self.limits.max_facts:
            raise _Stop("propagation_fact_limit")
        self.facts.append(fact)

    def emit(self):
        if "propagated_constant" in self.categories:
            for name in sorted(self.values):
                self.step()
                value = "".join(self.values[name])
                if not self.admitted(name) or not any(b in "01xz" for b in value):
                    continue
                # Only bits with a single modeled writer contribute facts.
                regions = []
                for chunk in _regions(value):
                    start, bits = chunk
                    regions.append({"lsb_offset": len(value)-start-len(bits), "width": len(bits), "bits": bits})
                drivers = sorted({o for o in self.owners[name] if o is not None and o >= 0})
                dependencies = sorted({d for i in drivers for d in self.drivers[i][2]})
                self.save({"kind": "propagated_constant", "signal": name, "width": len(value),
                           "state": "partial_known" if any(b in "?!" for b in value) else "four_state_constant" if any(b in "xz" for b in value) else "fully_known",
                           "constant_regions": regions, "source": self.location(self.signals[name]),
                           "dependencies": dependencies[:16], "dependencies_truncated": len(dependencies) > 16,
                           "driver_sources": [self.drivers[i][3] for i in drivers[:16]],
                           "driver_sources_truncated": len(drivers) > 16})
        if "constant_control" in self.categories:
            for expr, node, path, consumers, role, procedural in self.controls:
                if not self.admitted(path):
                    continue
                value = _truth(evaluate(expr, self.values, self.step))
                if value in "01":
                    self.save({"kind": "constant_control", "instance": path, "source": self.location(node),
                               "expression": self.text(node), "value": value, "role": role,
                               "enclosing_consumers": sorted(consumers)[:16],
                               "consumers_truncated": len(consumers) > 16,
                               "consumer_relation": "enclosing_block_candidates" if procedural else "direct_assignment"})

    def run(self):
        try:
            if self.session.diagnostic_payload.get("blocking_error_count"):
                raise _Stop("propagation_frontend_errors")
            self.inventory()
            self.solve()
            self.emit()
        except _Stop as exc:
            self.gaps.add(str(exc))
        return {"facts": self.facts, "receipt": {
            "status": "partial" if self.gaps else "complete", "gaps": sorted(self.gaps),
            "inventory_complete": self.inventory_complete, "instances": self.instances,
            "signal_count": len(self.signals), "signal_bits": self.bits,
            "driver_count": len(self.drivers), "driver_bits": self.target_bits,
            "ast_nodes": self.nodes, "work_steps": self.steps,
            "boundary_count": self.boundaries,
            "unknown_bits": sum(v.count("?") for v in self.values.values()),
            "conflict_bits": sum(v.count("!") for v in self.values.values()),
            "note": "Combinational steady-state facts only. Sequential/initial state and unsupported writes are boundaries; no temporal reachability or defect verdict.",
        }}


def _regions(value):
    start = None
    for index, bit in enumerate(value + "?"):
        if bit in "01xz" and start is None:
            start = index
        elif bit not in "01xz" and start is not None:
            yield start, value[start:index]
            start = None
