"""Backend-neutral, bounded expressions and single-driver observations.

Only elaborated, typed facts enter this contract. RTL text is never evaluated.
Missing evidence and four-state unknowns are distinct from a false condition.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Callable

from .cancellation import check_cancelled
from .divergence_compare import bit_value, known
from .expression_values import Value, BINARY, binary, unary, conditional, builtin, stream

DYNAMIC_VERSION = "1.0"
MAX_EXPR_NODES = 256
MAX_EXPR_DEPTH = 32
MAX_EXPR_WIDTH = 4096


@dataclass(frozen=True)
class Expr:
    op: str
    width: int
    args: tuple[Expr, ...] = ()
    signal: str | None = None
    bits: tuple[int, ...] = ()
    declared_bits: tuple[int, ...] = ()
    value: str | None = None
    signed: bool = False
    reason: str | None = None
    bounds: tuple[int, int] | None = None
    stride: int = 1
    function: str | None = None
    two_state: bool = False
    # Unbased unsized literals retain their fill behavior through width context.
    fill: bool = False

    def __post_init__(self):
        if not 1 <= self.width <= MAX_EXPR_WIDTH:
            raise ValueError("dynamic_expression_width_limit")
        if self.op not in ({"signal", "const", "not", "and", "or", "eq", "ne",
                           "mux", "concat", "cast", "unsupported", "u+", "u-", "~",
                           "reduce_&", "reduce_~&", "reduce_|", "reduce_~|",
                           "reduce_^", "reduce_~^", "reduce_^~", "select",
                           "part_up", "part_down", "project", "function",
                           "repeat", "stream_left", "stream_right", "inside", "range"} | BINARY):
            raise ValueError("dynamic_expression_operator_invalid")
        arity = {"signal": 0, "const": 0, "not": 1, "and": 2, "or": 2,
                 "eq": 2, "ne": 2, "mux": 3, "cast": 1}
        if self.op in arity and len(self.args) != arity[self.op]:
            raise ValueError("dynamic_expression_arity_invalid")
        if self.op == "signal" and (not self.signal or len(self.bits) != self.width):
            raise ValueError("dynamic_signal_selection_invalid")
        if self.op == "const" and (not self.value or len(self.value) != self.width
                                    or any(c not in "01xz" for c in self.value)):
            raise ValueError("dynamic_constant_invalid")
        if self.fill and (self.op != "const" or len(set(self.value)) != 1):
            raise ValueError("dynamic_fill_invalid")
        if type(self.stride) is not int or not 1 <= self.stride <= MAX_EXPR_WIDTH:
            raise ValueError("dynamic_selection_stride_invalid")
        if self.bounds is not None and (len(self.bounds) != 2 or
                any(type(i) is not int or abs(i) >= 2**63 for i in self.bounds)):
            raise ValueError("dynamic_selection_bounds_invalid")
        count = (2 if self.op in BINARY | {"select", "part_up", "part_down", "repeat",
                                          "stream_left", "stream_right", "range"} else
                 1 if self.op.startswith("reduce_") or self.op in {"u+", "u-", "~", "project"} else None)
        if count is not None and len(self.args) != count:
            raise ValueError("dynamic_expression_arity_invalid")
        if self.op == "project" and (len(self.bits) != self.width or
                any(type(i) is not int or not 0 <= i < self.args[0].width for i in self.bits)):
            raise ValueError("dynamic_bit_mapping_unavailable")
        if self.op == "function":
            functions = {"$signed", "$unsigned", "$clog2", "$isunknown", "$countones",
                         "$onehot", "$onehot0", "$countbits"}
            if self.function not in functions or (len(self.args) < 2 if self.function == "$countbits"
                                                  else len(self.args) != 1):
                raise ValueError("dynamic_function_invalid")
        if self.op == "inside" and len(self.args) < 2:
            raise ValueError("dynamic_expression_arity_invalid")
        if self.op in {"select", "part_up", "part_down"}:
            extent = abs(self.bounds[0] - self.bounds[1]) + 1 if self.bounds else self.args[0].width // self.stride
            if (extent * self.stride != self.args[0].width or self.width % self.stride or
                    self.op == "select" and self.width != self.stride):
                raise ValueError("dynamic_selection_shape_invalid")

    @classmethod
    def from_dict(cls, raw: dict, *, _budget=None, _depth=0):
        budget = [MAX_EXPR_NODES] if _budget is None else _budget
        budget[0] -= 1
        if budget[0] < 0 or _depth > MAX_EXPR_DEPTH:
            raise ValueError("dynamic_expression_limit")
        return cls(**{**raw, "bits": tuple(raw.get("bits", ())),
                      "declared_bits": tuple(raw.get("declared_bits", ())),
                      "bounds": tuple(raw["bounds"]) if raw.get("bounds") is not None else None,
                      "args": tuple(cls.from_dict(a, _budget=budget, _depth=_depth + 1)
                                    for a in raw.get("args", ()))})

    def bind(self, scope: str) -> Expr:
        return replace(self, signal=f"{scope}.{self.signal}" if self.signal else None,
                       args=tuple(a.bind(scope) for a in self.args))


TRUE = Expr("const", 1, value="1")
FALSE = Expr("const", 1, value="0")


@dataclass(frozen=True)
class Assignment:
    """One statement, with polarity-preserving path guard and process priority."""
    process: str
    order: int
    guard: Expr
    value: Expr
    clock: Expr | None = None
    edge: str | None = None
    nonblocking: bool = False
    gaps: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict):
        return cls(**{**raw, "guard": Expr.from_dict(raw["guard"]),
                      "value": Expr.from_dict(raw["value"]),
                      "clock": Expr.from_dict(raw["clock"]) if raw.get("clock") else None,
                      "gaps": tuple(raw.get("gaps", ()))})


@dataclass
class Evaluation:
    value: str | None
    dependencies: list[dict]
    gaps: list[str]
    branches: list[dict]
    # Exact ordered passthrough only; equal values do not prove a hold.
    reference: tuple[str, tuple[int, ...]] | None = None

    @property
    def truth(self) -> str:
        if self.value is None:
            return "unsupported"
        if "1" in self.value:
            return "true"
        return "false" if known(self.value) else "unknown"


def evaluate(expr: Expr, sample: Callable[[Expr], dict], role="data") -> Evaluation:
    """Evaluate with conservative four-state logic and retain selected dependencies."""
    budget = MAX_EXPR_NODES

    def walk(node: Expr, use: str, depth=0) -> Evaluation:
        nonlocal budget
        check_cancelled()
        budget -= 1
        if budget < 0 or depth > MAX_EXPR_DEPTH:
            return Evaluation(None, [], ["dynamic_expression_limit"], [])
        if node.op == "const":
            return Evaluation(node.value, [], [], [])
        if node.op == "signal":
            fact = sample(node)
            value = bit_value(fact.get("value"), node.width)
            gaps = list(fact.get("gaps", ()))
            if value is None:
                gaps.append("signal_not_dumped")
            elif not known(value):
                gaps.append("value_unknown")
            return Evaluation(value, [{**fact, "signal": node.signal, "bits": list(node.bits),
                                       "declared_bits": list(node.declared_bits),
                                       "width": node.width, "role": use, "value": value}], gaps, [],
                              (node.signal, node.bits))
        if node.op == "mux":
            cond = walk(node.args[0], "control", depth+1)
            selected = 1 if cond.truth == "true" else 2 if cond.truth == "false" else None
            children = [walk(node.args[i], use, depth+1) for i in
                        ((selected,) if selected else (1, 2))]
            value = children[0].value if selected else None
            if selected is None and cond.value is not None and all(c.value is not None for c in children):
                value = conditional(Value(cond.value),
                    Value(children[0].value, node.args[1].signed),
                    Value(children[1].value, node.args[2].signed), width=node.width).bits
            width_gaps = []
            if any(a.width != node.width for a in node.args[1:]):
                value = None
                width_gaps.append("expression_width_unresolved")
            return Evaluation(value, cond.dependencies + [d for c in children for d in c.dependencies],
                              cond.gaps + [g for c in children for g in c.gaps]
                              + ([] if selected else ["guard_unresolved"]) + width_gaps,
                              cond.branches + [{"condition": asdict(node.args[0]), "state": cond.truth,
                                                 "selected": selected}] +
                              [b for c in children for b in c.branches],
                              children[0].reference if selected else None)
        if node.op in {"select", "part_up", "part_down"}:
            base, index_node = node.args
            index = walk(index_node, "index", depth+1)
            if index.value is None:
                return Evaluation(None, index.dependencies, index.gaps, index.branches)
            if not known(index.value):
                return Evaluation("x" * node.width, index.dependencies,
                                  list(dict.fromkeys(index.gaps + ["index_unknown"])), index.branches)
            at = Value(index.value, index_node.signed).integer
            left, right = node.bounds or (base.width // node.stride - 1, 0)
            direction = -1 if left >= right else 1
            count = node.width // node.stride
            if node.op == "select":
                indices = [at]
            else:
                low = at if node.op == "part_up" else at - count + 1
                indices = list(range(low, low + count))
                if direction == -1:
                    indices.reverse()
            positions = [((i-left)*direction)*node.stride + j
                         if min(left, right) <= i <= max(left, right) else None
                         for i in indices for j in range(node.stride)]
            if len(positions) != node.width:
                return Evaluation(None, index.dependencies, ["expression_width_unresolved"], index.branches)
            if base.op == "signal" and all(p is not None for p in positions):
                selected_node = replace(base, width=node.width,
                    bits=tuple(base.bits[p] for p in positions), signed=node.signed)
                data = walk(selected_node, use, depth+1)
                value, reference = data.value, data.reference
            else:
                data = walk(base, use, depth+1)
                value = None if data.value is None else "".join(
                    data.value[p] if p is not None else "x" for p in positions)
                reference = None
            gaps = index.gaps + data.gaps
            if any(p is None for p in positions):
                gaps.append("index_out_of_range")
            return Evaluation(value, index.dependencies + data.dependencies,
                              list(dict.fromkeys(gaps)), index.branches + data.branches, reference)
        if node.op == "inside":
            subject = walk(node.args[0], use, depth+1)
            observations = [subject]
            matches = []
            for item in node.args[1:]:
                endpoints = item.args if item.op == "range" else (item,)
                observed = [walk(a, use, depth+1) for a in endpoints]
                observations.extend(observed)
                if subject.value is None or any(o.value is None for o in observed):
                    matches.append(None)
                    continue
                target = Value(subject.value, node.args[0].signed)
                inputs = [Value(o.value, a.signed) for o, a in zip(observed, endpoints)]
                if item.op == "range":
                    matches.append(binary("&&", binary(">=", target, inputs[0]),
                                           binary("<=", target, inputs[1])).bits)
                else:
                    matches.append(binary("==?", target, inputs[0]).bits)
            value = ("1" if "1" in matches else None if None in matches else
                     "x" if "x" in matches else "0")
            return Evaluation(value, [d for o in observations for d in o.dependencies],
                list(dict.fromkeys(g for o in observations for g in o.gaps)),
                [b for o in observations for b in o.branches])
        children = [walk(a, "control" if node.op in {"not","and","or","eq","ne"} else use,
                         depth+1) for a in node.args]
        deps = [d for c in children for d in c.dependencies]
        gaps = [g for c in children for g in c.gaps]
        branches = [b for c in children for b in c.branches]
        values = [c.value for c in children]
        value = None
        reference = None
        if node.op == "unsupported":
            gaps.append(node.reason or "dynamic_evidence_unavailable")
        elif node.op == "not":
            value = unary("!", Value(values[0])).bits if values[0] is not None else None
        elif node.op in {"and", "or"}:
            truths = [c.truth for c in children]
            decisive = "false" if node.op == "and" else "true"
            if all(v is not None for v in values):
                value = binary("&&" if node.op == "and" else "||", *(Value(v) for v in values)).bits
            elif decisive in truths:
                value = "0" if node.op == "and" else "1"
            elif all(t == ("true" if node.op == "and" else "false") for t in truths):
                value = "1" if node.op == "and" else "0"
            elif "unsupported" not in truths:
                value = "x"
        elif node.op in {"eq", "ne"}:
            if node.args[0].width != node.args[1].width or node.args[0].signed != node.args[1].signed:
                gaps.append("operand_type_unresolved")
            elif all(v is not None for v in values):
                value = binary("==" if node.op == "eq" else "!=",
                    Value(values[0], node.args[0].signed), Value(values[1], node.args[1].signed)).bits
        elif node.op == "concat" and all(v is not None for v in values):
            value = "".join(values)
            refs = [c.reference for c in children]
            if refs and all(r is not None and r[0] == refs[0][0] for r in refs):
                reference = (refs[0][0], tuple(b for r in refs for b in r[1]))
        elif node.op == "cast" and values[0] is not None:
            value = Value(values[0], node.args[0].signed).resize(node.width,
                signed=node.signed, two_state=node.two_state).bits
            ref = children[0].reference
            if ref and node.width <= node.args[0].width and not node.two_state:
                reference = (ref[0], ref[1][-node.width:])
        elif node.op == "project" and values[0] is not None:
            value = "".join(values[0][i] for i in node.bits)
            if children[0].reference:
                name, bits = children[0].reference
                reference = name, tuple(bits[i] for i in node.bits)
        elif all(v is not None for v in values):
            operands = [Value(v, a.signed) for v, a in zip(values, node.args)]
            if node.op in BINARY:
                value = binary(node.op, *operands, width=node.width).bits
                if node.op in {"/", "%"} and operands[1].known and operands[1].integer == 0:
                    gaps.append("division_by_zero")
            elif node.op in {"u+", "u-", "~"} or node.op.startswith("reduce_"):
                op = node.op[7:] if node.op.startswith("reduce_") else node.op.lstrip("u")
                value = unary(op, operands[0]).bits
            elif node.op == "function":
                value = builtin(node.function, operands).resize(node.width, signed=node.signed).bits
            elif node.op == "repeat":
                if operands[0].known and 0 < operands[0].integer <= MAX_EXPR_WIDTH:
                    count = operands[0].integer
                    if count * operands[1].width != node.width:
                        gaps.append("expression_width_unresolved")
                    else:
                        value = operands[1].bits * count
                else:
                    gaps.append("expression_repeat_count_invalid")
            elif node.op in {"stream_left", "stream_right"}:
                if operands[1].known:
                    value = stream(operands[0], "<<" if node.op == "stream_left" else ">>",
                                   operands[1].integer).bits
                else:
                    gaps.append("expression_stream_size_invalid")
        if value is not None and len(value) != node.width:
            value = None
            gaps.append("expression_width_unresolved")
        return Evaluation(value, deps, list(dict.fromkeys(gaps)), branches, reference)

    return walk(expr, role)


def unsupported_step(signal: str, backend: str, reason: str, **extra) -> dict:
    return dict(version=DYNAMIC_VERSION, signal=signal, backend=backend, width=None,
                boundary="unsupported", complete=False, branches=[], clock=None,
                gaps=[reason], **extra)


def expression_gaps(expr: Expr) -> list[str]:
    pending = [(expr, 0)]
    count = 0
    gaps = []
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_EXPR_NODES or depth > MAX_EXPR_DEPTH:
            return ["dynamic_expression_limit"]
        if node.op == "unsupported":
            gaps.append(node.reason or "dynamic_evidence_unavailable")
        pending.extend((a, depth + 1) for a in node.args)
    return list(dict.fromkeys(gaps))


def validate_step(raw: dict) -> dict:
    """Validate the private LSF boundary without accepting executable text."""
    if raw.get("version") != DYNAMIC_VERSION or raw.get("backend") not in {"verdi_npi", "source_graph", "static"}:
        raise ValueError("dynamic_step_version_invalid")
    if raw.get("boundary") not in {"combinational", "sequential", "input", "constant", "unsupported"}:
        raise ValueError("dynamic_step_boundary_invalid")
    if len(raw.get("branches", ())) > 64:
        raise ValueError("dynamic_step_branch_limit")
    for branch in raw.get("branches", ()):
        Expr.from_dict(branch["guard"])
        Expr.from_dict(branch["value"])
    if raw.get("state"):
        state = Expr.from_dict(raw["state"])
        if state.op != "signal" or state.bits != tuple(raw.get("bits", ())):
            raise ValueError("dynamic_step_state_invalid")
    if raw.get("clock"):
        Expr.from_dict(raw["clock"]["expression"])
        if raw["clock"]["edge"] not in {"posedge", "negedge"}:
            raise ValueError("dynamic_step_clock_invalid")
    controls = raw.get('async_controls', [])
    if not isinstance(controls, list) or len(controls) > 2:
        raise ValueError('dynamic_async_controls_invalid')
    if controls:
        clock = Expr.from_dict(raw['clock']['expression']) if raw.get('clock') else None
        if (raw.get('boundary') != 'sequential' or raw.get('complete') is not False
            or 'async_control_value_unmodeled' not in raw.get('gaps', ())
            or clock is None or clock.op != 'signal' or clock.width != 1):
            raise ValueError('dynamic_async_controls_invalid')
        for control in controls:
            expr = Expr.from_dict(control['expression'])
            active = control.get('active_value')
            if (expr.op != 'signal' or expr.width != 1 or control.get('kind') not in {'reset', 'set'}
                or active not in {'0', '1'} or control.get('assertion_edge') != ('posedge' if active == '1' else 'negedge')):
                raise ValueError('dynamic_async_controls_invalid')
    return raw
