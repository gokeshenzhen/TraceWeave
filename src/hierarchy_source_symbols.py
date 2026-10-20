"""Immutable semantic declarations and a bounded static class-graph view.

The provider supplies declaration and base identities. This layer never binds
classes by a bare name, parses source text, or treats a class as a runtime UVM
component instance. Qualified names are provider facts, not synthesized paths.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re

from .cancellation import check_cancelled
from .connectivity_ir import SourceLocation
from .hierarchy_source_ir import (
    HIERARCHY_TOOL_SCHEMA_VERSION, HierarchyBudgetExceeded, HierarchyQueryBudget,
)


_SYMBOL_KINDS = {"module", "interface", "program", "package", "class", "typedef",
                 "function", "task", "variable", "parameter", "enum"}


def _gaps(values):
    values = tuple(values)
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", value)
           for value in values):
        raise ValueError("symbol gaps must be fixed labels")
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class SourceSymbol:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    location: SourceLocation | None = None
    scope_id: str | None = None
    declaration_id: str | None = None
    type_id: str | None = None
    parameterization: tuple[tuple[str, str], ...] = ()
    external: bool = False
    gaps: tuple[str, ...] = ()
    declaration_status: str = "declaration"

    def __post_init__(self):
        if not all(isinstance(value, str) and value
                   for value in (self.symbol_id, self.name, self.qualified_name)):
            raise ValueError("symbol identity and names must be nonempty")
        if self.kind not in _SYMBOL_KINDS:
            raise ValueError("unsupported declaration kind")
        if self.declaration_status not in {"declaration", "specialization", "namespace_observation"}:
            raise ValueError("unsupported declaration status")
        if self.location is not None and not isinstance(self.location, SourceLocation):
            raise ValueError("symbol locations must be immutable")
        if any(value is not None and (not isinstance(value, str) or not value)
               for value in (self.scope_id, self.declaration_id, self.type_id)):
            raise ValueError("optional symbol identities must be nonempty")
        if not isinstance(self.external, bool):
            raise ValueError("external declaration status must be explicit")
        if any(not isinstance(pair, (tuple, list)) for pair in self.parameterization):
            raise ValueError("invalid parameter evidence")
        parameters = tuple(tuple(pair) for pair in self.parameterization)
        if any(len(pair) != 2 or not all(isinstance(value, str) for value in pair) or not pair[0]
               for pair in parameters) or len({pair[0] for pair in parameters}) != len(parameters):
            raise ValueError("invalid or duplicate parameter evidence")
        object.__setattr__(self, "parameterization", parameters)
        object.__setattr__(self, "gaps", _gaps(self.gaps))

    def row(self):
        return {
            "symbol_id": self.symbol_id, "name": self.name, "qualified_name": self.qualified_name,
            "kind": self.kind, "scope_id": self.scope_id, "declaration_id": self.declaration_id,
            "type_id": self.type_id, "source_file": self.location.file if self.location else None,
            "source_line": self.location.line if self.location else None,
            "source_column": self.location.column if self.location else None,
            "parameterization": dict(self.parameterization), "external": self.external,
            "gaps": list(self.gaps), "declaration_status": self.declaration_status,
        }


@dataclass(frozen=True)
class ClassFact:
    symbol: SourceSymbol
    base_status: str
    base_symbol_id: str | None = None
    base_qualified_name: str | None = None
    gaps: tuple[str, ...] = ()
    base_value_parameters: tuple[tuple[str, str], ...] = ()
    base_parameter_status: str = "not_observed"
    base_relation: str = "declaration"
    type_parameter_bindings: tuple[tuple[str, str], ...] = ()
    base_type_parameter_bindings: tuple[tuple[str, str], ...] = ()
    type_identity_domain: str = "declaration"

    def __post_init__(self):
        if not isinstance(self.symbol, SourceSymbol) or self.symbol.kind != "class":
            raise ValueError("class fact requires an immutable class declaration")
        if self.base_status not in {"none", "resolved", "unresolved"}:
            raise ValueError("base binding status must be explicit")
        if self.base_status == "resolved":
            if not all(isinstance(value, str) and value
                       for value in (self.base_symbol_id, self.base_qualified_name)):
                raise ValueError("resolved base requires a semantic identity and qualified name")
        elif self.base_symbol_id is not None:
            raise ValueError("an unbound base cannot have a resolved identity")
        if self.base_status == "none" and self.base_qualified_name is not None:
            raise ValueError("a class with no base cannot name a base")
        if self.base_qualified_name is not None and (
            not isinstance(self.base_qualified_name, str) or not self.base_qualified_name
        ):
            raise ValueError("invalid base name")
        object.__setattr__(self, "gaps", _gaps(self.gaps))
        if any(not isinstance(pair, (tuple, list)) for pair in self.base_value_parameters):
            raise ValueError("invalid base value parameter observations")
        parameters = tuple(tuple(pair) for pair in self.base_value_parameters)
        if (len(parameters) > 128 or any(len(pair) != 2 or not all(isinstance(x, str) and x for x in pair)
                                         for pair in parameters)
                or len({pair[0] for pair in parameters}) != len(parameters)):
            raise ValueError("invalid base value parameter observations")
        if self.base_parameter_status not in {"not_observed", "observed", "partial"}:
            raise ValueError("invalid base parameter status")
        if self.base_relation not in {"declaration", "specialization"}:
            raise ValueError("invalid base relation")
        if self.type_identity_domain not in {"declaration", "artifact_canonical"}:
            raise ValueError("invalid class type identity domain")
        for field in ("type_parameter_bindings", "base_type_parameter_bindings"):
            bindings = tuple(tuple(pair) for pair in getattr(self, field))
            if (len(bindings) > 128 or any(len(pair) != 2 or
                    not all(isinstance(item, str) and item for item in pair) for pair in bindings)
                    or len({pair[0] for pair in bindings}) != len(bindings)):
                raise ValueError("invalid canonical type parameter bindings")
            object.__setattr__(self, field, bindings)
        if self.base_type_parameter_bindings and (
                self.base_status != "resolved" or self.base_parameter_status == "not_observed"):
            raise ValueError("base type bindings require a resolved base")
        if parameters and (self.base_status != "resolved" or self.base_parameter_status == "not_observed"):
            raise ValueError("base parameter observations require a resolved declaration")
        if self.base_parameter_status == "observed" and not (parameters or self.base_type_parameter_bindings):
            raise ValueError("observed base parameters require positive facts")
        object.__setattr__(self, "base_value_parameters", parameters)

    def row(self):
        return dict(self.symbol.row(), base_status=self.base_status,
                    base_symbol_id=self.base_symbol_id, base_qualified_name=self.base_qualified_name,
                    binding_gaps=list(self.gaps),
                    base_relation=self.base_relation if self.base_status == "resolved" else None,
                    base_value_parameters=dict(self.base_value_parameters),
                    base_parameter_status=self.base_parameter_status,
                    type_parameter_bindings=dict(self.type_parameter_bindings),
                    base_type_parameter_bindings=dict(self.base_type_parameter_bindings),
                    type_identity_domain=self.type_identity_domain)


def validate_class_query(*, root_class=None, depth=-1, max_nodes=5000):
    """Pure validation shared by the renderer and pre-load dispatch."""
    if root_class is not None and (not isinstance(root_class, str) or not root_class):
        raise ValueError("root_class must be a nonempty identity or name")
    if isinstance(depth, bool) or not isinstance(depth, int) or not -1 <= depth <= 256:
        raise ValueError("depth must be an integer in [-1, 256]")
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not 1 <= max_nodes <= 5000:
        raise ValueError("max_nodes must be an integer in [1, 5000]")


def query_class_hierarchy(provider, *, root_class=None, depth=-1, max_nodes=5000,
                          budget: HierarchyQueryBudget | None = None):
    """Render the provider's static class inventory with explicit work limits.

    ``open_classes(budget)`` owns a stream of ClassFact with ``complete`` and
    ``gaps`` meaningful after exhaustion. Finding derived classes can require
    an inventory across compilation scopes; that work is explicit and charged,
    even for a single root. The legacy ``total`` remains the number of rendered
    nodes. Inventory counts cover provider declarations, excluding reference-
    only base nodes. Unresolved inheritance can leave the view partial while
    the declaration inventory is complete; a query budget stop cannot.
    """
    validate_class_query(root_class=root_class, depth=depth, max_nodes=max_nodes)
    budget = budget if budget is not None else getattr(provider, "budget", None)
    budget = budget if budget is not None else HierarchyQueryBudget()
    if not isinstance(budget, HierarchyQueryBudget):
        raise TypeError("a hierarchy query budget is required")
    classes, gaps, limits = {}, set(), set()
    complete, output_truncated, error = False, False, None
    roots, emitted, candidates = [], 0, []
    stream = None
    try:
        budget.checkpoint()
        with provider.open_classes(budget) as stream:
            for fact in stream:
                budget.checkpoint()
                if not isinstance(fact, ClassFact):
                    raise ValueError("provider returned an invalid class fact")
                identity = fact.symbol.symbol_id
                if identity in classes:
                    raise ValueError("provider returned a duplicate class identity")
                budget.admit_symbol(fact)
                classes[identity] = fact
            budget.checkpoint()
            if not isinstance(stream.complete, bool):
                raise ValueError("provider inventory completeness must be explicit")
            complete = stream.complete
            gaps.update(_gaps(stream.gaps))
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        complete = False
        limits.add(exc.reason)
        if stream is not None:
            gaps.update(_gaps(stream.gaps))
    inventory_complete = complete

    # Rows and links are copied facts. A bound external reference is not a
    # declaration recovered by matching its spelling against a local class.
    nodes, children, parent = {}, defaultdict(list), {}
    for identity, fact in classes.items():
        check_cancelled()
        nodes[identity] = fact.row()
        if fact.base_status == "unresolved":
            complete = False
            gaps.add("class_base_unresolved")
        if fact.base_status == "resolved":
            parent[identity] = fact.base_symbol_id
            children[fact.base_symbol_id].append(identity)
    for fact in classes.values():
        check_cancelled()
        if (fact.base_status == "resolved" and fact.base_symbol_id in nodes
                and nodes[fact.base_symbol_id]["qualified_name"] != fact.base_qualified_name):
            raise ValueError("provider returned conflicting bound base identities")
        if fact.base_status == "resolved" and fact.base_symbol_id not in nodes:
            nodes[fact.base_symbol_id] = {
                "symbol_id": fact.base_symbol_id, "name": fact.base_qualified_name,
                "qualified_name": fact.base_qualified_name, "kind": "class",
                "source_file": None, "source_line": None, "source_column": None,
                "external": True, "reference_only": True, "gaps": ["class_base_outside_inventory"],
            }
            # An external reference is positive binding evidence. It cannot
            # assert that all ancestors or derived classes were inventoried.
            complete = False
            gaps.add("class_base_outside_inventory")

    def order(identity):
        return nodes[identity]["qualified_name"], identity

    # Detect cycles independently of output depth; otherwise a depth-zero
    # rendering could turn an invalid inheritance cycle into a clean tree.
    visited, cycle_roots = set(), []
    for identity in nodes:
        check_cancelled()
        trail, positions, cursor = [], {}, identity
        while cursor in nodes and cursor not in visited:
            check_cancelled()
            if cursor in positions:
                cycle_roots.append(min(trail[positions[cursor]:], key=order))
                gaps.add("class_inheritance_cycle")
                complete = False
                break
            positions[cursor] = len(trail)
            trail.append(cursor)
            cursor = parent.get(cursor)
        visited.update(trail)

    if root_class is not None:
        selected = [root_class] if root_class in nodes else [
            key for key, row in nodes.items() if row.get("declaration_id") == root_class]
        if not selected:
            selected = [key for key, row in nodes.items() if row["qualified_name"] == root_class]
        if not selected:
            selected = [key for key, row in nodes.items() if row["name"] == root_class]
        if len(selected) > 1:
            error = "class_name_ambiguous"
            output_truncated = len(selected) > max_nodes
            candidates = [{"symbol_id": key, "qualified_name": nodes[key]["qualified_name"]}
                          for key in sorted(selected, key=order)[:max_nodes]]
            selected = []
        elif not selected:
            error = "class_not_found" if complete else "class_unresolved"
    else:
        selected = [key for key in nodes if key not in parent] + cycle_roots

    try:
        # All loops over collected facts are bounded by max_symbols. Check the
        # shared deadline before rendering and on every emitted/traversed edge.
        budget.checkpoint()
        pending = [(key, 0, roots, None) for key in reversed(sorted(selected, key=order))]
        seen = set()
        while pending:
            budget.checkpoint()
            identity, level, target, output_parent = pending.pop()
            if identity in seen:
                # A cycle was already reported above; never create recursive
                # Python dicts or count the same declaration repeatedly.
                continue
            if emitted >= max_nodes:
                output_truncated = True
                if output_parent is not None:
                    output_parent["truncated"] = True
                for _, _, _, ancestor in pending:
                    check_cancelled()
                    if ancestor is not None:
                        ancestor["truncated"] = True
                break
            if level > budget.max_depth:
                raise HierarchyBudgetExceeded("scope_depth_limit")
            seen.add(identity)
            row = dict(nodes[identity], children=[], truncated=False)
            target.append(row)
            emitted += 1
            child_ids = sorted(children.get(identity, []), key=order)
            if depth >= 0 and level >= depth:
                if child_ids:
                    row["truncated"] = output_truncated = True
                continue
            pending.extend((key, level + 1, row["children"], row) for key in reversed(child_ids))
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        complete = False
        inventory_complete = False
        limits.add(exc.reason)
        if error == "class_not_found":
            error = "class_unresolved"
    result = {
        "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "facts_origin": provider.origin,
        "artifact_identity": provider.artifact_identity, "view": "static_class_inheritance",
        "roots": roots, "total": emitted, "output_truncated": output_truncated,
        "class_inventory_domain": getattr(provider, "class_inventory_domain", "provider_declarations"),
        "class_inventory_count": len(classes) if inventory_complete else None,
        "class_inventory_lower_bound": len(classes), "class_inventory_complete": inventory_complete,
        "coverage_status": "complete" if complete else "partial",
        "coverage_gaps": sorted(gaps), "analysis_limits": sorted(limits),
        "query_metrics": budget.metrics(),
    }
    if error is not None:
        result["error"] = error
    if candidates:
        result["candidates"] = candidates
    return result
