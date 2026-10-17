"""Bounded hierarchy queries over one semantic provider and artifact.

Instance and static class operations share the provider/identity boundary.
File and type operations use that boundary as they are added. This module
neither builds a lexical tree nor loads a frontend. It
does not change the legacy full-result handle tools or their exact counts.
"""
from __future__ import annotations

from contextlib import ExitStack
from typing import ContextManager, Iterator, Protocol

from .hierarchy_source_ir import (
    HIERARCHY_TOOL_SCHEMA_VERSION, FactsOrigin, HierarchyBudgetExceeded,
    HierarchyQueryBudget, HierarchyScope, ScopeLookup,
)


class ScopeStream(Protocol):
    # Meaningful after StopIteration. Unsupported/incomplete enumeration must
    # not set complete merely because the native iterator was absent.
    complete: bool
    gaps: tuple[str, ...]

    def __iter__(self) -> Iterator[HierarchyScope]: ...

    def __next__(self) -> HierarchyScope: ...


class HierarchySourceProvider(Protocol):
    origin: FactsOrigin
    artifact_identity: str
    top: str

    def lookup_scope(self, path: str, budget: HierarchyQueryBudget) -> ScopeLookup: ...

    def open_children(self, scope: HierarchyScope, budget: HierarchyQueryBudget
                      ) -> ContextManager[ScopeStream]: ...


def _count_mode(value):
    if value not in {"bounded", "exact"}:
        raise ValueError("count_mode must be bounded or exact")


def _output_limit(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5000:
        raise ValueError(f"{name} must be an integer in [1, 5000]")


def validate_hierarchy_query(operation: str, arguments: dict):
    """Validate before a provider starts licensed or frontend work."""
    allowed = {
        "get_tb_subtree": {"root", "depth", "max_nodes", "count_mode"},
        "find_tb_instance": {"path", "module", "limit", "count_mode"},
        "get_tb_class_hierarchy": {"root_class", "depth", "max_nodes"},
    }
    if operation not in allowed or not isinstance(arguments, dict) or set(arguments) - allowed[operation]:
        raise ValueError("unsupported hierarchy query or arguments")
    _count_mode(arguments.get("count_mode", "bounded"))
    if operation == "get_tb_subtree":
        _output_limit(arguments.get("max_nodes", 500), "max_nodes")
        depth = arguments.get("depth", 1)
        if isinstance(depth, bool) or not isinstance(depth, int) or not -1 <= depth <= 256:
            raise ValueError("depth must be an integer in [-1, 256]")
        if not isinstance(arguments.get("root", ""), str):
            raise ValueError("root must be a string")
    elif operation == "find_tb_instance":
        _output_limit(arguments.get("limit", 100), "limit")
        path, module = arguments.get("path"), arguments.get("module")
        if (path is None) == (module is None) or not isinstance(path or module, str) or not (path or module):
            raise ValueError("provide exactly one nonempty path or module")
    else:
        from .hierarchy_source_symbols import validate_class_query
        validate_class_query(**arguments)


def finish_hierarchy_query(result: dict, budget: HierarchyQueryBudget) -> dict:
    """Account for mandatory provider cleanup and final identity validation.

    Cleanup runs even after the deadline. A late finish may retain observed
    positive facts, but cannot publish complete coverage or a negative proof.
    Cancellation and an operation's own TimeoutError still propagate.
    """
    try:
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        result["coverage_status"] = "partial"
        result["analysis_limits"] = sorted(set(result["analysis_limits"]) | {exc.reason})
        if "field_bits" in result:
            result.update(status="mapping_required", field_bits={}, declared_range=None)
        if "export_complete" in result:
            result["export_complete"] = False
            result["warning"] = "section export is incomplete; inspect coverage and output limits"
        if result.get("semantic_status") == "complete":
            result["semantic_status"] = "partial"
        if result.get("view") == "static_class_inheritance":
            # Class total is the number rendered, not a global exact count.
            result["class_inventory_count"] = None
            result["class_inventory_complete"] = False
            if result.get("error") == "class_not_found":
                result["error"] = "class_unresolved"
        elif "symbol_inventory_complete" in result:
            result["symbol_inventory_complete"] = False
            result["symbol_count"] = None
        else:
            if "count_status" in result:
                result["count_status"] = "lower_bound"
            for key in ("total", "total_descendants"):
                if key in result:
                    result[key] = None
            if result.get("error") == "instance_not_found":
                result["error"] = "instance_unresolved"
            if result.get("error") == "file_not_in_compile_set":
                result["error"] = "file_query_incomplete"
    result["query_metrics"].update(budget.metrics())
    return result


class HierarchySourceService:
    def __init__(self, provider: HierarchySourceProvider):
        self.provider = provider

    def class_hierarchy(self, *, root_class=None, depth=-1, max_nodes=5000, budget=None):
        from .hierarchy_source_symbols import query_class_hierarchy
        return query_class_hierarchy(self.provider, root_class=root_class, depth=depth,
                                     max_nodes=max_nodes, budget=budget)

    def _query_budget(self, requested):
        # A native provider's owner budget starts before loading/importing.
        # Omitting the service argument must not reset that deadline or create
        # a second incompatible budget. Stateless IR providers can use a fresh
        # budget; an explicitly supplied budget remains the caller's choice.
        budget = requested if requested is not None else getattr(self.provider, "budget", None)
        if budget is None:
            return HierarchyQueryBudget()
        if not isinstance(budget, HierarchyQueryBudget):
            raise TypeError("a hierarchy query budget is required")
        return budget

    def _envelope(self, *, count_mode, domain, count, complete, gaps, limits,
                  output_truncated, budget):
        return {
            "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "facts_origin": self.provider.origin,
            "artifact_identity": self.provider.artifact_identity,
            "count_mode": count_mode, "count_domain": domain,
            "count_status": "exact" if complete else "lower_bound",
            "count_lower_bound": count, "coverage_status": "complete" if complete else "partial",
            "coverage_gaps": sorted(gaps), "analysis_limits": sorted(limits),
            "output_truncated": output_truncated, "query_metrics": budget.metrics(),
        }

    def _lookup(self, path, budget):
        budget.checkpoint()
        result = self.provider.lookup_scope(path, budget)
        budget.checkpoint()
        if result.fact is not None and result.fact.path != path:
            raise ValueError("provider exact lookup returned a different scope")
        return result

    def subtree(self, *, root: str = "", depth: int = 1, max_nodes: int = 500,
                count_mode: str = "bounded", budget: HierarchyQueryBudget | None = None):
        """Browse a scope; only exact mode traverses undisplayed descendants.

        max_nodes includes the root. At a depth/output boundary, one child may
        be observed to distinguish a proved omitted edge from an empty scope.
        That observation contributes to the lower bound, never to an exact
        total. No native enumeration continues merely to fill a total field.
        """
        validate_hierarchy_query("get_tb_subtree", dict(root=root, depth=depth,
                                 max_nodes=max_nodes, count_mode=count_mode))
        budget = self._query_budget(budget)
        root = root or self.provider.top
        gaps, limits, seen = set(), set(), set()
        count = emitted = 0
        complete, truncated = True, False
        rendered = None
        error = None

        def node(fact):
            return dict(fact.row(), children=[], child_count=None,
                        child_count_lower_bound=0, truncated=False)

        try:
            lookup = self._lookup(root, budget)
            gaps.update(lookup.gaps)
            complete &= lookup.complete
            if lookup.fact is None:
                complete = lookup.complete
                error = "instance_not_found" if complete else "instance_unresolved"
            else:
                budget.admit(lookup.fact, 0)
                seen.add(root)
                rendered, emitted = node(lookup.fact), 1
                # Each frame owns its own context manager. Closing a subtree
                # releases its iterator promptly; outer finally closes all
                # live ancestors on cancellation, timeout or provider failure.
                with ExitStack() as all_resources:
                    frames = []

                    def push(fact, output, level):
                        owner = ExitStack()
                        all_resources.callback(owner.close)
                        stream = owner.enter_context(self.provider.open_children(fact, budget))
                        frames.append([fact, output, level, stream, owner, 0])

                    push(lookup.fact, rendered, 0)
                    while frames:
                        budget.checkpoint()
                        parent, output, level, stream, owner, child_count = frames[-1]
                        try:
                            child = next(stream)
                        except StopIteration:
                            budget.checkpoint()
                            gaps.update(stream.gaps)
                            complete &= stream.complete
                            if output is not None and stream.complete:
                                output["child_count"] = child_count
                            owner.close()
                            frames.pop()
                            continue
                        budget.checkpoint()
                        if child.parent_path != parent.path or child.path in seen:
                            raise ValueError("provider returned a duplicate or incorrectly parented scope")
                        budget.admit(child, level + 1)
                        seen.add(child.path)
                        count += 1
                        frames[-1][5] += 1
                        if output is not None:
                            output["child_count_lower_bound"] += 1
                        visible = (output is not None and (depth < 0 or level < depth)
                                   and emitted < max_nodes)
                        child_output = node(child) if visible else None
                        if visible:
                            output["children"].append(child_output)
                            emitted += 1
                        else:
                            truncated = True
                            if output is not None:
                                output["truncated"] = True
                            if count_mode == "bounded":
                                complete = False
                                gaps.add("descendants_not_enumerated")
                                # A depth frontier only ends this parent's
                                # iterator. Other visible siblings remain
                                # browsable. An output cap stops the request.
                                owner.close()
                                frames.pop()
                                if emitted >= max_nodes:
                                    break
                                continue
                        push(child, child_output, level + 1)
            budget.checkpoint()
        except HierarchyBudgetExceeded as exc:
            complete = False
            limits.add(exc.reason)
        result = self._envelope(count_mode=count_mode, domain="descendants_of_root", count=count,
                                complete=complete, gaps=gaps, limits=limits,
                                output_truncated=truncated, budget=budget)
        result.update(root=root, node=rendered, total_descendants=count if complete else None,
                      truncated=truncated)
        if error is not None:
            result["error"] = "instance_unresolved" if not complete else error
        return result

    def find_instances(self, *, path: str | None = None, module: str | None = None,
                       limit: int = 100, count_mode: str = "bounded",
                       budget: HierarchyQueryBudget | None = None):
        validate_hierarchy_query("find_tb_instance", dict(path=path, module=module,
                                 limit=limit, count_mode=count_mode))
        budget = self._query_budget(budget)
        gaps, limits, seen = set(), set(), set()
        hits, count, complete, truncated = [], 0, True, False

        def observe(fact, depth):
            nonlocal count, truncated
            if fact.path in seen:
                raise ValueError("provider returned a duplicate scope")
            budget.admit(fact, depth)
            seen.add(fact.path)
            if path is not None or (fact.kind in {"module", "interface", "program"}
                                    and fact.definition_name == module):
                count += 1
                if len(hits) < limit:
                    hits.append(fact.row())
                else:
                    truncated = True

        try:
            lookup = self._lookup(path if path is not None else self.provider.top, budget)
            gaps.update(lookup.gaps)
            complete &= lookup.complete
            if lookup.fact is None:
                complete = lookup.complete
            else:
                observe(lookup.fact, 0)
                # An exact path never opens a child iterator or scans siblings.
                if path is None:
                    with ExitStack() as resources:
                        frames = []

                        def push(fact, level):
                            owner = ExitStack()
                            resources.callback(owner.close)
                            stream = owner.enter_context(self.provider.open_children(fact, budget))
                            frames.append((fact, level, stream, owner))

                        push(lookup.fact, 0)
                        while frames:
                            budget.checkpoint()
                            parent, level, stream, owner = frames[-1]
                            try:
                                child = next(stream)
                            except StopIteration:
                                budget.checkpoint()
                                gaps.update(stream.gaps)
                                complete &= stream.complete
                                owner.close()
                                frames.pop()
                                continue
                            if child.parent_path != parent.path:
                                raise ValueError("provider returned an incorrectly parented scope")
                            observe(child, level + 1)
                            if truncated and count_mode == "bounded":
                                complete = False
                                gaps.add("matching_instances_not_enumerated")
                                break
                            push(child, level + 1)
            budget.checkpoint()
        except HierarchyBudgetExceeded as exc:
            complete = False
            limits.add(exc.reason)
        result = self._envelope(count_mode=count_mode,
                                domain="exact_path" if path is not None else "matching_instances_under_top",
                                count=count, complete=complete, gaps=gaps, limits=limits,
                                output_truncated=truncated, budget=budget)
        result.update(hits=hits, total=count if complete else None, truncated=truncated)
        return result
