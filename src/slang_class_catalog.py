"""Worker-local finite class inventory and canonical type binding identities.

Only declaration facts and actually encountered ClassTypes are collected. This
module never asks the frontend to invent a default or enumerate parameter sets.
Printed names and values are observations; Type.isMatching is the only equality
test for canonical type identities. Native owners live until projection ends.
"""
from __future__ import annotations

import hashlib
import json


CLASS_DOMAIN = "declarations_and_referenced_specializations"


class ClassCollector:
    def __init__(self, *, charge, identity, source, enqueue, kind_name, files, max_depth, depth_limit,
                 parameter_limit, reserve):
        self.charge, self.identity, self.source = charge, identity, source
        self.enqueue, self.kind_name, self.files = enqueue, kind_name, files
        self.max_depth, self.depth_limit = max_depth, depth_limit
        self.parameter_limit = parameter_limit
        self.reserve = reserve
        self.types, self.rows, self.declarations = [], {}, {}
        self.gaps = set()

    def type_key(self, value):
        """Intern actual canonical types, preserving nominal type distinctions."""
        self.charge()
        if value is None:
            return None
        value = value.canonicalType
        if value.isError:
            self.gaps.add("source_class_type_binding_unresolved")
            return None
        for key, owner in self.types:
            self.charge()
            if value.isMatching(owner):
                return key
        key = hashlib.sha256(("canonical_type:" + str(len(self.types))).encode()).hexdigest()
        self.reserve(256)
        self.types.append((key, value))
        return key

    def declaration(self, symbol, scope_key=None):
        kind = self.kind_name(symbol)
        if kind not in {"ClassType", "GenericClassDef"}:
            return
        if kind == "ClassType":
            key = self.observe_type(symbol)
            if key in self.rows and scope_key is not None:
                self.rows[key]["scope_key"] = scope_key
            return
        where = self.source(symbol)
        if where is None or where["file"] not in self.files:
            return
        key = self.identity(symbol, where)
        if key is None:
            self.gaps.add('source_class_declaration_identity_unbound')
            return
        self.declarations[key] = symbol
        row = dict(key=key, declaration_key=key, type_key=None,
            name=str(symbol.name), qualified_name=str(symbol.lexicalPath) or str(symbol.name),
            location=where, scope_key=scope_key, declaration_status="declaration", parameterization=[],
            type_parameter_bindings=[], base_status="unresolved" if self.has_extends(symbol) else "none",
            base_key=None, base_qualified_name=None, base_relation=None, base_value_parameters=[],
            base_type_parameter_bindings=[], base_parameter_status="not_observed",
            gaps=["source_generic_class_binding_not_instantiated"])
        if key not in self.rows:
            self.reserve(512 + 4 * len(json.dumps(row, ensure_ascii=True)))
            self.rows[key] = row
        self.gaps.add("source_generic_class_binding_not_instantiated")
        if scope_key is not None:
            self.rows[key]["scope_key"] = scope_key

    @staticmethod
    def has_extends(symbol):
        syntax = symbol.syntax
        # Syntax evidence distinguishes no inheritance from an unresolved
        # explicit clause. Never recover a base by matching its spelling.
        return syntax is not None and getattr(syntax, "extendsClause", None) is not None

    def observe_type(self, value, depth=0):
        """Copy one encountered class and its bound base/type parameters."""
        self.charge()
        if depth > self.max_depth:
            self.depth_limit()
        if value is None:
            return None
        value = value.canonicalType
        if value.isError or self.kind_name(value) != "ClassType":
            return None
        type_key = self.type_key(value)
        generic = value.genericClass
        declaration = generic if generic is not None else value
        where = self.source(declaration)
        declaration_key = self.identity(declaration, where) if where is not None else None
        if where is not None and declaration_key is None:
            self.gaps.add('source_class_declaration_identity_unbound')
        specialized = generic is not None
        # A nested non-generic class inside two different specializations can
        # share its declaration location and lexical path while being two
        # nominal types. Every actual class node therefore uses canonical
        # identity; declaration identity remains a separate field.
        key = type_key
        if key in self.rows:
            return key
        name = str(declaration.name)
        qualified = str(declaration.lexicalPath) or name
        bound = where is not None and where["file"] in self.files and declaration_key is not None
        # An out-of-domain base remains a positive reference, not a recovered
        # declaration. Retain the canonical owner for links and type equality.
        if not bound:
            return key
        if specialized:
            self.declaration(generic)
        self.declarations[declaration_key] = declaration
        row = dict(key=key, declaration_key=declaration_key,
            type_key=type_key, name=name, qualified_name=qualified,
            location=where, scope_key=None,
            declaration_status="specialization" if specialized else "declaration",
            parameterization=[], type_parameter_bindings=[],
            base_status="unresolved" if self.has_extends(declaration) else "none", base_key=None,
            base_qualified_name=None, base_relation=None, base_value_parameters=[],
            base_type_parameter_bindings=[], base_parameter_status="not_observed",
            gaps=["source_class_binding_incomplete"])
        self.reserve(512 + 4 * len(json.dumps(row, ensure_ascii=True)))
        # Insert before following recursive type arguments or the base chain.
        self.rows[key] = row
        if specialized:
            self.enqueue(value, declaration_key, where["file"], depth + 1,
                         ("class_type", type_key))
        values, typed, _, parameters_complete = self.parameters(value, depth)
        row["parameterization"] = values
        row["type_parameter_bindings"] = typed
        if not parameters_complete:
            row["gaps"].append("source_class_type_binding_unresolved")
        base = value.baseClass
        if base is None:
            if self.has_extends(declaration):
                row["base_status"] = "unresolved"
                row["gaps"].append("source_class_base_unresolved")
            row["gaps"].remove("source_class_binding_incomplete")
            return key
        base = base.canonicalType
        if base.isError or self.kind_name(base) != "ClassType":
            row["base_status"] = "unresolved"
            row["gaps"].append("source_class_base_unresolved")
            row["gaps"].remove("source_class_binding_incomplete")
            return key
        base_key = self.observe_type(base, depth + 1)
        base_decl = base.genericClass if base.genericClass is not None else base
        row.update(base_status="resolved", base_key=base_key,
                   base_qualified_name=str(base_decl.lexicalPath) or str(base_decl.name),
                   base_relation="specialization" if base.genericClass is not None else "declaration")
        base_values, base_typed, constants, base_parameters_complete = self.parameters(base, depth + 1)
        row["base_value_parameters"] = constants
        row["base_type_parameter_bindings"] = base_typed
        row["base_parameter_status"] = ("partial" if not base_parameters_complete else
                                        "observed" if base_values else "not_observed")
        if not base_parameters_complete:
            row["gaps"].append("source_class_type_binding_unresolved")
        row["gaps"].remove("source_class_binding_incomplete")
        return key

    def parameters(self, value, depth):
        values, typed, constants, complete = [], [], [], True
        for member in value:
            self.charge()
            kind = self.kind_name(member)
            if kind not in {"Parameter", "TypeParameter"} or not member.isPortParam:
                continue
            if len(values) >= 128:
                self.parameter_limit()
            name = str(member.name)
            if kind == "TypeParameter":
                target = member.targetType.type
                key = self.type_key(target)
                pair = [name, str(target)]
                self.reserve(128 + 4 * len(json.dumps(pair, ensure_ascii=True)))
                values.append(pair)
                if key is None:
                    complete = False
                    self.gaps.add("source_class_type_binding_unresolved")
                else:
                    self.reserve(128 + 4 * len(name))
                    typed.append([name, key])
                self.observe_type(target, depth + 1)
            else:
                pair = [name, str(member.value)]
                self.reserve(128 + 4 * len(json.dumps(pair, ensure_ascii=True)))
                values.append(pair)
                constants.append(pair)
        return values, typed, constants, complete

    def payload(self, *, limited, diagnostics, context_incomplete):
        gaps = set(self.gaps)
        if limited:
            gaps.add("source_class_inventory_limited")
        if diagnostics:
            gaps.add("source_class_frontend_diagnostics")
        if context_incomplete:
            gaps.add("source_class_context_incomplete")
        for row in self.rows.values():
            row["gaps"] = sorted(set(row["gaps"]))
            if row["declaration_status"] == "specialization":
                row["scope_key"] = self.rows.get(row["declaration_key"], {}).get("scope_key")
        return dict(domain=CLASS_DOMAIN, rows=list(self.rows.values()),
                    type_keys=[key for key, _ in self.types], gaps=sorted(gaps),
                    complete=not gaps)
