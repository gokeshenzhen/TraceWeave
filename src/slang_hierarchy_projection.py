"""Bounded primitive scope facts copied inside the existing Slang worker.

Only actual semantic scopes supply parents and sites. Connectivity instance
locations retain their legacy compatibility fallback; these source facts do not.
"""
from __future__ import annotations

from dataclasses import asdict

from .cancellation import check_cancelled


def collect_hierarchy_scopes(projector, root):
    from .slang_connectivity_projector import _kind_name, _parameterization, _definition_id

    search = projector.options.semantic_search
    rows, symbols, instances, complete = {}, {}, {}, set()
    visits, limited = 0, False
    kinds = {"Instance", "GenerateBlock", "GenerateBlockArray", "InstanceArray"}

    class Limit(Exception):
        pass

    def charge():
        nonlocal visits
        check_cancelled()
        if visits >= search.max_members:
            raise Limit
        visits += 1

    def location(symbol):
        value = projector._location(symbol.location)
        return asdict(value) if value is not None else None

    def parent(symbol):
        scope = symbol.parentScope
        if _kind_name(scope) == "InstanceBody":
            scope = scope.parentInstance
        return scope if _kind_name(scope) in kinds else None

    def admit(symbol, ancestors=0):
        charge()
        kind = _kind_name(symbol)
        if kind not in kinds or (kind == "GenerateBlock" and symbol.isUninstantiated):
            return None
        path = str(symbol.hierarchicalPath)
        if path in rows:
            return path
        if len(rows) >= search.max_instances or ancestors >= 64:
            raise Limit
        owner = parent(symbol)
        parent_path = admit(owner, ancestors + 1) if owner is not None else None
        if len(rows) >= search.max_instances:
            raise Limit
        definition = symbol.definition if kind == "Instance" else None
        parameters = _parameterization(symbol) if definition is not None else ()
        definition_id = (_definition_id(symbol, parameters, projector.source_manager,
                         projector.options.source_root) if definition is not None else None)
        semantic_kind = (str(definition.definitionKind.name).lower() if definition is not None
                         else "instance_array" if kind == "InstanceArray" else "generate")
        rows[path] = dict(path=path, name=str(symbol.name) or path.rsplit(".", 1)[-1],
            kind=semantic_kind, parent_path=parent_path, definition_id=definition_id,
            definition_name=str(definition.name) if definition is not None else None,
            location=location(symbol), definition_location=location(definition) if definition is not None else None,
            parameterization=parameters, gaps=[])
        symbols[path] = symbol
        if kind == "Instance":
            instances[path] = symbol
        return path

    try:
        for path in projector.options.focus_instance_paths:
            charge()
            symbol = root.lookupName(path)
            if symbol is not None:
                admit(symbol)
        pending = [(path, search.depth) for path in search.roots if path in symbols]
        expanded = set()
        while pending:
            path, depth = pending.pop(0)
            if path in expanded or depth < 1:
                continue
            expanded.add(path)
            symbol = symbols[path]
            kind = _kind_name(symbol)
            members = iter(symbol.body if kind == "Instance" else
                           symbol.entries if kind == "GenerateBlockArray" else
                           symbol.elements if kind == "InstanceArray" else symbol)
            while True:
                charge()
                try:
                    child = next(members)
                except StopIteration:
                    complete.add(path)
                    break
                if _kind_name(child) not in kinds:
                    continue
                child_path = admit(child)
                if child_path is not None:
                    if rows[child_path]["parent_path"] != path:
                        # Some frontend container representations omit an
                        # intermediate owner. Never manufacture that edge.
                        rows[path]["gaps"].append("source_scope_parent_unresolved")
                        actual_parent = rows[child_path]["parent_path"]
                        if actual_parent in rows:
                            rows[actual_parent]["gaps"].append("source_scope_parent_unresolved")
                        complete.discard(path)
                        complete.discard(actual_parent)
                    if depth > 1:
                        pending.append((child_path, depth - 1))
    except Limit:
        limited = True
    for path in tuple(complete):
        if rows[path]["gaps"]:
            complete.discard(path)
    return instances, dict(rows=list(rows.values()), complete_children=sorted(complete),
                           member_visits=visits, limited=limited)
