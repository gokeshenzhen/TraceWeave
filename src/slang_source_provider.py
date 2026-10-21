"""File-bound copied facts from one capability-checked Slang artifact."""
from __future__ import annotations

from contextlib import contextmanager

from .connectivity_ir import SourceLocation
from .hierarchy_source_ir import semantic_fact_id
from .hierarchy_source_symbols import SourceSymbol, ClassFact
from .semantic_query_view import semantic_query_view


class SlangSourceProvider:
    origin = "slang"

    def __init__(self, entry, context, budget, source_files, *, capabilities=("file_declarations",)):
        self.artifact_identity = entry.build_key.digest
        self.top, self.budget = context.selected_top, budget
        search = entry.artifact_scope_receipt.scope.semantic_search
        if (search is None or search.purpose != "source_catalog"
                or not set(capabilities).issubset(search.source_capabilities)):
            raise ValueError("source catalog artifact capability or scope mismatch")
        budget.checkpoint()
        view = semantic_query_view(entry, budget.checkpoint)
        view.bind_ownership(context, budget.checkpoint)
        payload = view.payload
        if any(path not in payload['files'] for path in source_files):
            raise ValueError('source catalog artifact capability or scope mismatch')
        self.identity = context.identity_sha256
        self.files = payload['files']
        self.classes = None
        if 'class_inheritance' in capabilities:
            self.class_inventory_domain = payload['classes']['domain']
            self.class_complete = payload['classes']['complete']
            self.class_gaps = payload['classes']['gaps']
            self.classes = payload['classes']['rows']

    def _id(self, key, *, stable=False, kind='declaration'):
        return (semantic_fact_id(self.origin, self.identity if stable else
                                 self.artifact_identity, kind, key) if key is not None else None)

    def _symbol(self, item, *, class_row=False):
        return SourceSymbol(
            symbol_id=self._id(item['key']), name=item['name'],
            qualified_name=item['qualified_name'], kind='class' if class_row else item['kind'],
            location=SourceLocation(**item['location']),
            scope_id=self._id(item['scope_key'], stable=True),
            declaration_id=self._id(item['declaration_key'], stable=True),
            type_id=self._id(item['type_key'], **({'kind': 'canonical_type'} if class_row else {'stable': True})),
            parameterization=item['parameterization'], gaps=item['gaps'],
            declaration_status=item['declaration_status'],
            **({} if class_row else {'external': item['external']}))

    def _class(self, row):
        def typed(values):
            return tuple((name, self._id(key, kind='canonical_type')) for name, key in values)
        return ClassFact(symbol=self._symbol(row, class_row=True), base_status=row['base_status'],
            base_symbol_id=self._id(row['base_key']), base_qualified_name=row['base_qualified_name'],
            gaps=row['gaps'], base_value_parameters=row['base_value_parameters'],
            base_parameter_status=row['base_parameter_status'], base_relation=row['base_relation'] or 'declaration',
            type_parameter_bindings=typed(row['type_parameter_bindings']),
            base_type_parameter_bindings=typed(row['base_type_parameter_bindings']),
            type_identity_domain='artifact_canonical')

    @contextmanager
    def open_file_symbols(self, entry, budget):
        budget.call()
        row = self.files.get(entry.path)
        if row is None:
            raise ValueError("source catalog does not cover requested file")
        iterator = iter(row["symbols"])
        convert = self._symbol
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
                    return convert(next(iterator))
                except StopIteration:
                    self.complete = row["complete"]
                    raise
        yield Stream()

    @contextmanager
    def open_classes(self, budget):
        budget.call()
        if self.classes is None:
            raise ValueError('source catalog has no requested class capability')
        iterator = iter(self.classes)
        convert = self._class
        complete, gaps = self.class_complete, self.class_gaps
        class Stream:
            def __init__(self):
                self.complete, self.gaps = False, gaps
            def __iter__(self):
                return self
            def __next__(self):
                budget.call()
                try:
                    return convert(next(iterator))
                except StopIteration:
                    self.complete = complete
                    raise
        yield Stream()
