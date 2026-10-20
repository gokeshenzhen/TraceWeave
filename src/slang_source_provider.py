"""File-bound copied facts from one capability-checked Slang artifact."""
from __future__ import annotations

from contextlib import contextmanager

from .connectivity_ir import SourceLocation
from .hierarchy_source_ir import HierarchyBudgetExceeded, semantic_fact_id
from .hierarchy_source_symbols import SourceSymbol
from .slang_source_codec import decode_source_catalog


class SlangSourceProvider:
    origin = "slang"

    def __init__(self, entry, context, budget, source_files):
        self.artifact_identity = entry.build_key.digest
        self.top, self.budget = context.selected_top, budget
        search = entry.artifact_scope_receipt.scope.semantic_search
        if (search is None or search.purpose != "source_catalog"
                or search.source_capabilities != ("file_declarations",)
                or tuple(source_files) != search.source_files):
            raise ValueError("source catalog artifact capability or scope mismatch")
        raw = dict(entry.ir.metadata).get("source_catalog_v1")
        if raw is None:
            raise ValueError("source catalog artifact has no declaration projection")
        budget.reserve_bytes(512 + 4 * len(raw))
        payload = decode_source_catalog(raw, search, checkpoint=budget.checkpoint)
        budget.reserve_bytes(payload['coordinate_bytes'])
        visits = payload["member_visits"]
        if not isinstance(visits, int) or not 0 <= visits <= budget.max_calls - budget.call_count:
            raise HierarchyBudgetExceeded("native_call_limit")
        budget.call_count += visits
        if payload['scope_visits'] > budget.max_scopes - budget.scope_count:
            raise HierarchyBudgetExceeded('scope_limit')
        budget.scope_count += payload['scope_visits']
        captured = {record.path for record in context.snapshot.records}
        aliases = dict(context.aliases)
        unavailable = {aliases.get(path, path) for path, _ in context.unavailable}
        if any(aliases.get(spelling) != path for spelling, path in search.source_aliases):
            raise ValueError('source catalog aliases differ from captured ownership')
        self.files = {}
        for path, row in payload["files"].items():
            budget.checkpoint()
            if path not in captured and path not in unavailable:
                raise ValueError("source catalog file is outside captured inputs")
            if path not in captured and (row['symbols'] or row['complete']
                    or row['uses_uvm'] is False or row['has_uvm_import'] is False):
                raise ValueError('unavailable source cannot supply declarations or complete negatives')
            symbols = []
            for item in row["symbols"]:
                budget.checkpoint()
                where = item["location"]
                canonical = aliases.get(where["file"], where["file"])
                if canonical != path or canonical not in captured:
                    raise ValueError("source catalog declaration belongs to another file")
                def fact_id(key, *, stable=False):
                    return (semantic_fact_id(self.origin, context.identity_sha256 if stable else
                                             self.artifact_identity, "declaration", key)
                            if key is not None else None)
                symbols.append(SourceSymbol(
                    symbol_id=fact_id(item["key"]), name=item["name"],
                    qualified_name=item["qualified_name"], kind=item["kind"],
                    location=SourceLocation(canonical, where["line"], where["column"]),
                    scope_id=fact_id(item["scope_key"], stable=True),
                    declaration_id=fact_id(item["declaration_key"], stable=True),
                    type_id=fact_id(item["type_key"], stable=True),
                    parameterization=tuple(tuple(pair) for pair in item["parameterization"]),
                    external=item["external"], gaps=tuple(item["gaps"]),
                    declaration_status=item["declaration_status"]))
            self.files[path] = dict(row, symbols=tuple(symbols))

    @contextmanager
    def open_file_symbols(self, entry, budget):
        budget.call()
        row = self.files.get(entry.path)
        if row is None:
            raise ValueError("source catalog does not cover requested file")
        iterator = iter(row["symbols"])
        class Stream:
            complete = False
            gaps = tuple(row["gaps"])
            has_uvm_import = row["has_uvm_import"]
            uses_uvm = row["uses_uvm"]
            work_scope = "global_namespaces_bounded"
            def __iter__(self):
                return self
            def __next__(self):
                budget.call()
                try:
                    return next(iterator)
                except StopIteration:
                    self.complete = row["complete"]
                    raise
        yield Stream()
