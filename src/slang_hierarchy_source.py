"""Prepared hierarchy/source queries over capability-isolated Slang artifacts."""
from __future__ import annotations

from contextlib import contextmanager

from .connectivity_ir import SourceLocation
from .hierarchy_source_ir import HierarchyScope, ScopeLookup, semantic_fact_id, HierarchyBudgetExceeded
from .prepared_source_connectivity import build_prepared_source_plan
from .source_graph_contract import SemanticScopeSearch
from .semantic_query_view import semantic_query_view


_LIMITS = {'source_hierarchy_member_limit': 'native_call_limit',
           'source_hierarchy_scope_limit': 'scope_limit',
           'source_hierarchy_depth_limit': 'scope_depth_limit',
           'source_hierarchy_byte_limit': 'fact_byte_limit',
           'source_hierarchy_parameter_limit': 'parameter_binding_limit'}


def capability_gap(operation, arguments):
    if operation in {'get_tb_file_detail', 'lookup_tb_files', 'get_tb_class_hierarchy',
                     'get_tb_subtree', 'find_tb_instance'}:
        return None
    if operation == 'dump_tb_section' and arguments.get('section') in {
            'files_full', 'interfaces', 'class_hierarchy_full', 'component_tree_full'}:
        return None
    return 'source_hierarchy_capability_unsupported'


def build_instance_plan(context, operation, arguments, config, budget, *, catalog=None):
    budget.checkpoint()
    if budget.scope_count >= budget.max_scopes:
        raise HierarchyBudgetExceeded('scope_limit')
    if budget.call_count >= budget.max_calls:
        raise HierarchyBudgetExceeded('native_call_limit')
    if budget.fact_bytes >= budget.max_bytes:
        raise HierarchyBudgetExceeded('fact_byte_limit')
    section = arguments.get('section') if operation == 'dump_tb_section' else None
    source_operation = operation in {'get_tb_file_detail', 'lookup_tb_files', 'get_tb_class_hierarchy'}
    if source_operation or section in {'files_full', 'interfaces', 'class_hierarchy_full'}:
        if catalog is None:
            raise ValueError('source file queries require captured ownership')
        if operation == 'get_tb_file_detail':
            selected = (catalog.find_file(arguments['path'], checkpoint=budget.checkpoint),)
        elif operation == 'lookup_tb_files':
            filters = {name: arguments.get(name) for name in ('basename', 'name_contains', 'path_contains')}
            selected = (catalog.select_files(**filters, checkpoint=budget.checkpoint)
                        if any(value is not None for value in filters.values()) else catalog.files)
        else:
            selected = catalog.files
        files = tuple(sorted(item.path for item in selected if item is not None))
        class_capable = operation == 'get_tb_class_hierarchy' or section == 'class_hierarchy_full'
        return build_prepared_source_plan(context=context, operation='driver',
            args={'signal_path': context.selected_top + '.__traceweave_source_anchor'}, config=config,
            search=SemanticScopeSearch(purpose='source_catalog', source_files=files,
                source_capabilities=(('class_inheritance', 'file_declarations')
                                     if class_capable else ('file_declarations',)),
                source_aliases=context.aliases,
                source_max_scopes=min(1024, budget.max_scopes - budget.scope_count),
                source_max_depth=budget.max_depth,
                source_max_bytes=max(1, min(2 * 1024 * 1024, (budget.max_bytes - budget.fact_bytes) // 3)),
                max_members=max(1, min(50000, (budget.max_calls - budget.call_count) // 2))))
    # Top-global module searches and tree exports share one complete domain.
    # An exact/unlimited local subtree completes that actual root only; it
    # never needs to enumerate unrelated selected-top siblings.
    full = (section == 'component_tree_full' or operation == 'get_tb_subtree' and arguments.get('count_mode') == 'exact'
            or operation == 'find_tb_instance' and arguments.get('path') is None
            or operation == 'get_tb_subtree' and arguments.get('depth', 1) == -1)
    global_domain = section == 'component_tree_full' or operation == 'find_tb_instance' and arguments.get('path') is None
    path = (context.selected_top if global_domain else
            arguments.get('path') if operation == 'find_tb_instance' else arguments.get('root'))
    path = path or context.selected_top
    depth = (budget.max_depth if full else 0 if operation == 'find_tb_instance' else
             min(budget.max_depth, arguments.get('depth', 1) + 1))
    return build_prepared_source_plan(context=context, operation='driver',
        args={'signal_path': path + '.__traceweave_scope_anchor'}, config=config,
        search=SemanticScopeSearch(roots=(path,), depth=depth, purpose='hierarchy',
            hierarchy_mode='complete' if full else 'bounded', hierarchy_aliases=context.aliases,
            hierarchy_max_bytes=max(1, min(2 * 1024 * 1024, (budget.max_bytes - budget.fact_bytes) // 3)),
            max_instances=max(1, min(10000, (budget.max_scopes - budget.scope_count) // 2)),
            max_members=max(1, min(50000, (budget.max_calls - budget.call_count) // 2))))


class SlangHierarchyProvider:
    origin = 'slang'

    def __init__(self, entry, context, budget):
        self.artifact_identity = entry.build_key.digest
        self.top, self.budget = context.selected_top, budget
        budget.checkpoint()
        view = semantic_query_view(entry, budget.checkpoint)
        view.bind_ownership(context, budget.checkpoint)
        self.view = view
        scope, payload = view.scope, view.payload
        self.facts, self.children = view.by_path, view.children
        self.gaps = set()
        if entry.ir.coverage.blocking_diagnostic_count:
            self.gaps.add('source_hierarchy_frontend_diagnostics')
        if entry.ir.coverage.gaps:
            self.gaps.add('source_hierarchy_context_incomplete')
        if payload['limited']:
            self.gaps.add('source_hierarchy_projection_limit')
        self.analysis_limits = (() if payload['stop_reason'] is None else (_LIMITS[payload['stop_reason']],))
        self.complete = frozenset() if self.gaps else view.complete_children
        self.global_complete = (scope.semantic_search.hierarchy_mode == 'complete'
            and scope.semantic_search.roots == (self.top,) and not self.gaps
            and bool(self.facts) and len(self.facts) == len(self.complete))

    def _fact(self, row):
        if row is None:
            return None
        return HierarchyScope(
            scope_id=semantic_fact_id(self.origin, self.artifact_identity, row['kind'], row['path']),
            path=row['path'], name=row['name'], kind=row['kind'], parent_path=row['parent_path'],
            definition_id=row['definition_id'], definition_name=row['definition_name'],
            location=SourceLocation(**row['location']) if row['location'] is not None else None,
            definition_location=(SourceLocation(**row['definition_location'])
                                 if row['definition_location'] is not None else None),
            parameterization=row['parameterization'], gaps=row['gaps'])

    def lookup_scope(self, path, budget):
        budget.call()
        fact = self._fact(self.facts.get(path))
        complete = not self.gaps and (fact is not None or self.global_complete)
        gaps = self.gaps | ({'source_scope_not_in_projected_domain'} if fact is None and not complete else set())
        return ScopeLookup(fact, complete, tuple(sorted(gaps)))

    @contextmanager
    def open_children(self, scope, budget):
        budget.call()
        children = iter(self.children.get(scope.path, ()))
        convert = self._fact
        complete = scope.path in self.complete

        class Stream:
            gaps = () if complete else ('source_children_not_enumerated',)
            def __init__(self):
                self.complete = False
            def __iter__(self):
                return self
            def __next__(self):
                budget.call()
                try:
                    return convert(next(children))
                except StopIteration:
                    self.complete = complete
                    raise
        yield Stream()
