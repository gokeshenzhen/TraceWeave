"""Prepared instance queries over a capability-isolated Source Graph artifact."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

from .connectivity_ir import SourceLocation
from .hierarchy_source_ir import HierarchyScope, ScopeLookup, semantic_fact_id, HierarchyBudgetExceeded
from .prepared_source_connectivity import build_prepared_source_plan
from .source_graph_contract import SemanticScopeSearch


def capability_gap(operation, arguments):
    if operation in {"get_tb_file_detail", "lookup_tb_files"}:
        return None
    if operation not in {"get_tb_subtree", "find_tb_instance"}:
        return "source_hierarchy_capability_unsupported"
    if arguments.get("count_mode", "bounded") == "exact":
        return "source_hierarchy_exact_count_unsupported"
    if operation == "find_tb_instance" and arguments.get("path") is None:
        return "source_hierarchy_module_search_unsupported"
    if operation == "get_tb_subtree" and arguments.get("depth", 1) == -1:
        return "source_hierarchy_unlimited_depth_unsupported"
    return None


def build_instance_plan(context, operation, arguments, config, budget, *, catalog=None):
    budget.checkpoint()
    if budget.scope_count >= budget.max_scopes:
        raise HierarchyBudgetExceeded("scope_limit")
    if budget.call_count >= budget.max_calls:
        raise HierarchyBudgetExceeded("native_call_limit")
    if budget.fact_bytes >= budget.max_bytes:
        raise HierarchyBudgetExceeded("fact_byte_limit")
    path = arguments.get("path") if operation == "find_tb_instance" else arguments.get("root")
    path = path or context.selected_top
    if operation in {"get_tb_file_detail", "lookup_tb_files"}:
        if catalog is None:
            raise ValueError("source file queries require captured ownership")
        if operation == "get_tb_file_detail":
            selected = (catalog.find_file(arguments["path"], checkpoint=budget.checkpoint),)
        else:
            filters = {name: arguments.get(name) for name in ("basename", "name_contains", "path_contains")}
            selected = (catalog.select_files(**filters, checkpoint=budget.checkpoint)
                        if any(value is not None for value in filters.values()) else catalog.files)
        files = tuple(sorted(item.path for item in selected if item is not None))
        return build_prepared_source_plan(context=context, operation="driver",
            args={"signal_path": context.selected_top + ".__traceweave_source_anchor"}, config=config,
            search=SemanticScopeSearch(purpose="source_catalog", source_files=files,
                source_capabilities=("file_declarations",),
                source_aliases=context.aliases,
                source_max_scopes=min(1024, budget.max_scopes - budget.scope_count),
                source_max_depth=budget.max_depth,
                source_max_bytes=max(1, min(2 * 1024 * 1024, (budget.max_bytes - budget.fact_bytes) // 3)),
                max_members=max(1, min(4096, (budget.max_calls - budget.call_count) // 2))))
    # Reuse the captured manifest and artifact planner. This terminal is a
    # private planning anchor only: no signal query is ever executed against it.
    return build_prepared_source_plan(context=context, operation="driver",
        args={"signal_path": path + ".__traceweave_scope_anchor"}, config=config,
        search=SemanticScopeSearch(roots=(path,), depth=2, purpose="hierarchy",
            max_instances=max(1, min(64, budget.max_scopes - budget.scope_count)),
            max_members=max(1, min(4096, budget.max_calls - budget.call_count))))


class SlangHierarchyProvider:
    origin = "slang"

    def __init__(self, entry, context, budget):
        self.artifact_identity = entry.build_key.digest
        self.top = context.selected_top
        self.budget = budget
        payload = json.loads(dict(entry.ir.metadata)["hierarchy_scopes_v1"])
        self.facts, self.children = {}, {}
        self.gaps = {"source_hierarchy_bounded"}
        if entry.ir.coverage.blocking_diagnostic_count:
            self.gaps.add("source_hierarchy_frontend_diagnostics")
        if payload["limited"]:
            self.gaps.add("source_hierarchy_projection_limit")
        self.complete = set(payload["complete_children"])
        if entry.ir.coverage.blocking_diagnostic_count:
            self.complete.clear()
        captured = {record.path for record in context.snapshot.records}
        aliases = dict(context.aliases)

        def bound_location(value, gaps, role):
            if value is None:
                gaps.add("source_" + role + "_location_unavailable")
                return None
            spelling = value["file"]
            # The worker normalizes relative to its fixed launch cwd. Exact
            # captured membership is required; no basename guessing or reads.
            canonical = aliases.get(spelling, str((Path(__file__).resolve().parents[1] / spelling).absolute()))
            if canonical not in captured:
                gaps.add("source_" + role + "_location_unbound")
                return None
            return SourceLocation(canonical, value["line"], value.get("column", 0))

        for row in payload["rows"]:
            budget.checkpoint()
            gaps = set(row["gaps"])
            site = bound_location(row["location"], gaps, "instance")
            definition = (bound_location(row["definition_location"], gaps, "definition")
                          if row["definition_id"] is not None else None)
            fact = HierarchyScope(
                scope_id=semantic_fact_id(self.origin, self.artifact_identity, row["kind"], row["path"]),
                path=row["path"], name=row["name"], kind=row["kind"], parent_path=row["parent_path"],
                definition_id=row["definition_id"], definition_name=row["definition_name"],
                location=site, definition_location=definition,
                parameterization=tuple(tuple(p) for p in row["parameterization"]), gaps=tuple(gaps))
            self.facts[fact.path] = fact
            self.children.setdefault(fact.parent_path, []).append(fact)
        # Discovery and copied facts are part of this request's cumulative work.
        if payload["member_visits"] > budget.max_calls - budget.call_count:
            raise HierarchyBudgetExceeded("native_call_limit")
        budget.call_count += payload["member_visits"]

    def lookup_scope(self, path, budget):
        budget.call()
        # Even an exact miss is local/incomplete evidence, never a global
        # absence proof in a partial elaboration. Positive facts remain useful.
        return ScopeLookup(self.facts.get(path), False, tuple(sorted(self.gaps)))

    @contextmanager
    def open_children(self, scope, budget):
        budget.call()
        children = iter(self.children.get(scope.path, ()))
        complete = scope.path in self.complete

        class Stream:
            gaps = () if complete else ("source_children_not_enumerated",)
            def __init__(self):
                self.complete = False
            def __iter__(self):
                return self
            def __next__(self):
                budget.call()
                try:
                    return next(children)
                except StopIteration:
                    self.complete = complete
                    raise

        yield Stream()
