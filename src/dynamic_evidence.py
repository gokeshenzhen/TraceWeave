"""Backend-neutral, bounded expressions and single-driver observations.

Only elaborated, typed facts enter this contract. RTL text is never evaluated.
Missing evidence and four-state unknowns are distinct from a false condition.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Callable

from .cancellation import check_cancelled
from .divergence_compare import bit_value, known

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

    def __post_init__(self):
        if not 1 <= self.width <= MAX_EXPR_WIDTH:
            raise ValueError("dynamic_expression_width_limit")
        if self.op not in {"signal", "const", "not", "and", "or", "eq", "ne",
                           "mux", "concat", "cast", "unsupported"}:
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

    @classmethod
    def from_dict(cls, raw: dict, *, _budget=None, _depth=0):
        budget = [MAX_EXPR_NODES] if _budget is None else _budget
        budget[0] -= 1
        if budget[0] < 0 or _depth > MAX_EXPR_DEPTH:
            raise ValueError("dynamic_expression_limit")
        return cls(**{**raw, "bits": tuple(raw.get("bits", ())),
                      "declared_bits": tuple(raw.get("declared_bits", ())),
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
            return Evaluation(value, cond.dependencies + [d for c in children for d in c.dependencies],
                              cond.gaps + [g for c in children for g in c.gaps]
                              + ([] if selected else ["guard_unresolved"]),
                              cond.branches + [{"condition": asdict(node.args[0]), "state": cond.truth,
                                                 "selected": selected}] +
                              [b for c in children for b in c.branches],
                              children[0].reference if selected else None)
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
            value = {"true": "0", "false": "1", "unknown": "x"}.get(children[0].truth)
        elif node.op in {"and", "or"}:
            truths = [c.truth for c in children]
            decisive = "false" if node.op == "and" else "true"
            if decisive in truths:
                value = "0" if node.op == "and" else "1"
            elif all(t == ("true" if node.op == "and" else "false") for t in truths):
                value = "1" if node.op == "and" else "0"
            elif "unsupported" not in truths:
                value = "x"
        elif node.op in {"eq", "ne"}:
            if node.args[0].width != node.args[1].width or node.args[0].signed != node.args[1].signed:
                gaps.append("operand_type_unresolved")
            elif all(known(v) for v in values):
                value = str(int((values[0] == values[1]) == (node.op == "eq")))
            elif all(v is not None for v in values):
                value = "x"
        elif node.op == "concat" and all(v is not None for v in values):
            value = "".join(values)
            refs = [c.reference for c in children]
            if refs and all(r is not None and r[0] == refs[0][0] for r in refs):
                reference = (refs[0][0], tuple(b for r in refs for b in r[1]))
        elif node.op == "cast" and values[0] is not None:
            source = values[0]
            pad = source[0] if node.args[0].signed else "0"
            value = source[-node.width:].rjust(node.width, pad)
            ref = children[0].reference
            if ref and node.width <= node.args[0].width:
                reference = (ref[0], ref[1][-node.width:])
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
    return raw
