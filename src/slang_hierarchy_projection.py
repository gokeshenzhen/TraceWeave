"""Finite topology-only facts copied from actual Slang owners inside the worker.

Native member / array enumeration proves each parent edge. Names can propose
an exact lookup's ancestors, but never establish an edge on their own.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
import hashlib
import json
from pathlib import Path
import re

from .cancellation import check_cancelled


class HierarchyProjectionLimit(Exception):
    """A fixed work frontier reached before actual selected-top proof."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def collect_hierarchy_scopes(projector, root):
    from .slang_connectivity_projector import _kind_name

    search = projector.options.semantic_search
    rows, symbols, complete, semantic_tops = {}, {}, set(), []
    visits = retained = 0
    reason = None
    coordinates = {}
    kinds = {"Instance", "GenerateBlock", "GenerateBlockArray", "InstanceArray"}
    aliases = dict(search.hierarchy_aliases)
    aliases.update({value: value for value in aliases.values()})

    class Limit(Exception):
        pass

    def charge():
        nonlocal visits
        check_cancelled()
        if visits >= search.max_members:
            raise Limit("source_hierarchy_member_limit")
        visits += 1

    def reserve(size):
        nonlocal retained
        if retained + size > search.hierarchy_max_bytes:
            raise Limit("source_hierarchy_byte_limit")
        retained += size

    def binding(spelling):
        if spelling in aliases:
            return aliases[spelling]
        if spelling and not Path(spelling).is_absolute():
            spelling = str((projector.options.source_root or Path.cwd()) / spelling)
        return aliases.get(spelling)

    def location(symbol):
        manager = projector.source_manager
        try:
            charge()
            original = manager.getFullyOriginalLoc(symbol.location)
            path = (binding(str(manager.getFullPath(original.buffer)))
                    or binding(str(manager.getFileName(original))))
            if path is None:
                return None
            if original.buffer not in coordinates:
                raw = manager.getSourceText(original.buffer).encode("utf-8")
                reserve(len(raw) + 512)
                starts = [0]
                offset = raw.find(b"\n")
                while offset >= 0:
                    charge()
                    reserve(64)
                    starts.append(offset + 1)
                    offset = raw.find(b"\n", offset + 1)
                coordinates[original.buffer] = (starts, len(raw))
            starts, size = coordinates[original.buffer]
            offset = int(original.offset)
            if not 0 <= offset <= size:
                return None
            line = bisect_right(starts, offset)
            return dict(file=path, line=line, column=offset - starts[line - 1] + 1)
        except (ValueError, RuntimeError):
            return None

    def children(symbol):
        kind = _kind_name(symbol)
        return iter(symbol.body if kind == "Instance" else symbol.entries if kind == "GenerateBlockArray"
                    else symbol.elements if kind == "InstanceArray" else symbol)

    def eligible(symbol):
        kind = _kind_name(symbol)
        return kind in kinds and not (kind == "GenerateBlock" and symbol.isUninstantiated)

    def unsupported(symbol):
        # These are actual hierarchical objects outside the supported scope
        # domain, rather than harmless signal/type members of a scope.
        return _kind_name(symbol) in {'CheckerInstance', 'PrimitiveInstance', 'UninstantiatedDef'}

    def parent(symbol):
        # Prefix spelling supplies candidates only; every accepted edge is
        # checked against a direct native owner member or array element.
        path = str(symbol.hierarchicalPath)
        candidates = []
        if path.endswith("]"):
            candidates.append(re.sub(r"\[-?\d+\]$", "", path))
        if "." in path:
            candidates.append(path.rpartition(".")[0])
        for candidate in candidates:
            charge()
            owner = root.lookupName(candidate)
            kind = _kind_name(owner)
            if kind not in kinds:
                continue
            if kind in {"GenerateBlockArray", "InstanceArray"}:
                for child in children(owner):
                    charge()
                    if eligible(child) and str(child.hierarchicalPath) == path:
                        return owner
            else:
                suffix = path[len(candidate) + 1:]
                if not suffix or "." in suffix or "[" in suffix:
                    continue
                charge()
                child = (owner.body if kind == "Instance" else owner).find(suffix)
                if child is not None and eligible(child) and str(child.hierarchicalPath) == path:
                    return owner
        return None

    def admit(symbol, parent_path, *, parent_proved):
        charge()
        if not eligible(symbol):
            return None
        path = str(symbol.hierarchicalPath)
        if path in rows:
            if parent_path != rows[path]["parent_path"]:
                raise ValueError("conflicting actual hierarchy parent")
            return path
        if len(rows) >= search.max_instances:
            raise Limit("source_hierarchy_scope_limit")
        definition = symbol.definition if _kind_name(symbol) == "Instance" else None
        where = location(symbol)
        definition_where = location(definition) if definition is not None else None
        parameters = []
        if definition is not None:
            for parameter in symbol.body.parameters:
                charge()
                if len(parameters) >= 128:
                    raise Limit("source_hierarchy_parameter_limit")
                value = str(parameter.value if _kind_name(parameter) == 'Parameter' else parameter.targetType.type)
                pair = [str(parameter.name), value]
                reserve(64 + 4 * len(json.dumps(pair, ensure_ascii=True)))
                parameters.append(pair)
        definition_id = (hashlib.sha256(json.dumps([str(definition.name),
            str(definition.definitionKind.name), definition_where, parameters], sort_keys=True).encode()).hexdigest()
            if definition is not None else None)
        gaps = []
        if not parent_proved:
            gaps.append("source_scope_parent_unresolved")
        if where is None:
            gaps.append("source_instance_location_unavailable")
        if definition is not None and definition_where is None:
            gaps.append("source_definition_location_unavailable")
        row = dict(path=path, name=str(symbol.name) or path.rsplit(".", 1)[-1],
            kind=(str(definition.definitionKind.name).lower() if definition is not None else
                  "instance_array" if _kind_name(symbol) == "InstanceArray" else "generate"),
            parent_path=parent_path, definition_id=definition_id,
            definition_name=str(definition.name) if definition is not None else None,
            location=where, definition_location=definition_where,
            parameterization=parameters, gaps=sorted(gaps))
        reserve(512 + 4 * len(json.dumps(row, ensure_ascii=True, sort_keys=True)))
        rows[path], symbols[path] = row, symbol
        return path

    def admit_exact(symbol):
        chain, seen = [], set()
        while symbol is not None:
            charge()
            path = str(symbol.hierarchicalPath)
            if path in seen or len(chain) > 256:
                raise Limit("source_hierarchy_depth_limit")
            seen.add(path)
            if path in rows:
                break
            chain.append(symbol)
            symbol = parent(symbol)
        parent_path = str(symbol.hierarchicalPath) if symbol is not None else None
        for symbol in reversed(chain):
            path = str(symbol.hierarchicalPath)
            parent_path = admit(symbol, parent_path,
                                parent_proved=parent_path is not None or path in semantic_tops)
        return parent_path

    selected_top = None
    try:
        reserve(512)  # fixed capability/domain/digest/counter envelope
        for instance in root.topInstances:
            charge()
            path = str(instance.hierarchicalPath)
            reserve(64 + 4 * len(json.dumps(path, ensure_ascii=True)))
            semantic_tops.append(path)
        candidates = [top for top in semantic_tops if all(path == top or path.startswith(top + ".")
                                                         for path in search.roots)]
        if len(candidates) != 1:
            raise ValueError("hierarchy roots lack one actual selected top")
        selected_top = candidates[0]
        for path in search.roots:
            charge()
            symbol = root.lookupName(path)
            if symbol is not None and eligible(symbol):
                admit_exact(symbol)
        pending = deque((path, 0) for path in search.roots if path in rows)
        expanded = set()
        while pending:
            path, level = pending.popleft()
            if path in expanded:
                continue
            expanded.add(path)
            if level >= search.depth and search.hierarchy_mode == "bounded":
                continue
            iterator = children(symbols[path])
            while True:
                charge()
                try:
                    child = next(iterator)
                except StopIteration:
                    complete.add(path)
                    break
                if not eligible(child):
                    if unsupported(child):
                        previous = rows[path]
                        updated = dict(previous, gaps=sorted(set(previous['gaps']) |
                                       {'source_hierarchy_scope_kind_uncovered'}))
                        reserve(max(0, 4 * (len(json.dumps(updated, ensure_ascii=True, sort_keys=True)) -
                                           len(json.dumps(previous, ensure_ascii=True, sort_keys=True)))))
                        rows[path] = updated
                    continue
                if level >= search.depth:
                    raise Limit("source_hierarchy_depth_limit")
                child_path = admit(child, path, parent_proved=True)
                if child_path is not None:
                    pending.append((child_path, level + 1))
    except Limit as exc:
        reason = str(exc)
    if selected_top is None:
        # Never hand the loader an invalid partial artifact with a fabricated
        # top. The worker publishes an explicit unstarted budget blocker.
        raise HierarchyProjectionLimit(reason or 'source_hierarchy_member_limit')
    for path in tuple(complete):
        if rows[path]["gaps"]:
            complete.discard(path)
    return dict(version=2, capability="instance_hierarchy", domain="selected_top_elaborated_scopes",
        selected_top=selected_top, semantic_tops=sorted(semantic_tops),
        search_sha256=hashlib.sha256(json.dumps(search.to_dict(), sort_keys=True,
                                              separators=(',', ':')).encode()).hexdigest(),
        rows=list(rows.values()), complete_children=sorted(complete), member_visits=visits,
        retained_bytes=retained, limited=reason is not None, stop_reason=reason)
