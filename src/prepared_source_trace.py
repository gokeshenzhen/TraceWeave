"""Bounded trace planning over one captured context and real semantic entries.

Textual targets are candidates only. The worker proves instances; admission
uses declarations and assignment scopes from that same immutable artifact.
"""
from __future__ import annotations

import json

from .cancellation import check_cancelled
from .compile_input_capture import CaptureAborted
from .prepared_source_connectivity import SourceQueryBudget, build_prepared_source_plan
from .source_graph_contract import SemanticScopeSearch
from .source_graph_adapter import AdapterStatus
from .source_graph_x_trace import SourceGraphTraceFallbackRequired, SourceGraphTraceScopeExpansion


class PreparedTracePlan:
    def __init__(self, context, config, deadline, max_depth):
        self.context, self.config, self.max_depth = context, config, max_depth
        self.budget = SourceQueryBudget(deadline)
        self.roots = ()
        self.depth = 0
        self.attempts = []
        self.metrics = {}

    def build(self, targets):
        check_cancelled()
        if self.budget.exhausted:
            raise CaptureAborted('source_scope_limit')
        plan = build_prepared_source_plan(context=self.context, operation='driver',
            args={'signal_path': targets[0], 'max_depth': self.max_depth}, config=self.config,
            signal_paths=tuple(targets), search=SemanticScopeSearch(purpose='trace',
                roots=self.roots, depth=self.depth, max_instances=self.budget.instances,
                max_members=self.budget.members))
        if (plan.status is AdapterStatus.BLOCKED and any(
                token in plan.receipt.blocker.code for token in ('limit', 'budget'))):
            raise CaptureAborted('source_scope_limit')
        return plan

    def observe(self, outcome):
        self.attempts.append(outcome.build_key.digest)
        for name, value in outcome.metrics.to_dict().items():
            if value is not None and (name.endswith('_ms') or name in {'actual_build_count', 'frontend_launch_count'}):
                public_name = 'prepare_total_wall_ms' if name == 'total_wall_ms' else name
                self.metrics[public_name] = self.metrics.get(public_name, 0) + value
        if outcome.entry is not None:
            self.budget.account_projection(outcome.entry)
            if dict(outcome.entry.ir.metadata).get('semantic_scope_limited') == '1':
                raise CaptureAborted('source_scope_limit')

    def expand(self, expansion):
        roots = tuple(sorted(set(self.roots) | set(expansion.semantic_roots)))
        depth = max(self.depth, expansion.semantic_depth)
        changed = (roots, depth) != (self.roots, self.depth)
        if len(roots) > 64:
            raise CaptureAborted('source_scope_limit')
        self.roots, self.depth = roots, depth
        return changed

    def receipt(self, receipt):
        receipt.update(artifact_attempt_count=len(self.attempts),
            scope_expansion_count=max(0, len(self.attempts)-1),
            attempted_artifact_fingerprints_sha256=list(self.attempts),
            semantic_instances_charged=64-self.budget.instances,
            semantic_members_charged=4096-self.budget.members)
        receipt.setdefault('metrics', {}).update(self.metrics)
        return receipt


class PreparedTraceArtifactGuard:
    def __init__(self, entry, plan):
        self.entry = entry
        self.scope = entry.artifact_scope_receipt.scope
        self.search = self.scope.semantic_search
        if self.search is None or self.search.purpose != 'trace':
            raise SourceGraphTraceFallbackRequired('source_graph_trace_capability_missing')
        self.requested = set(plan.request.scope.semantic_search.assignment_candidates)
        self.projected = set(json.loads(dict(entry.ir.metadata).get('semantic_assignment_paths', '[]')))

    def require(self, paths):
        uncovered = []
        for path in paths:
            check_cancelled()
            if not path.startswith(self.scope.top + '.'):
                raise SourceGraphTraceFallbackRequired('source_graph_trace_target_top_mismatch')
            try:
                selected = self.entry.query_engine.resolve_signal(path)
            except (KeyError, ValueError):
                if path.rpartition('.')[0] in self.requested:
                    raise SourceGraphTraceFallbackRequired('source_graph_trace_target_unresolved')
                uncovered.append(path)
                continue
            if selected.instance_path not in self.projected:
                uncovered.append(path)
        if uncovered:
            raise SourceGraphTraceScopeExpansion(uncovered)

    def input_frontier(self, paths):
        """Positive top input declarations are boundaries, never no-driver proof."""
        if not paths:
            return False
        for path in paths:
            check_cancelled()
            try:
                selected = self.entry.query_engine.resolve_signal(path)
            except (KeyError, ValueError):
                return False
            if selected.instance_path != self.scope.top:
                return False
            instance = self.entry.query_engine.instance_index[selected.instance_path]
            port = self.entry.query_engine.definition_index[instance.definition_id].port(selected.symbol)
            if port is None or port.direction.value != 'input':
                return False
        return True

    def require_frontier_children(self, paths):
        roots = set()
        for path in paths:
            check_cancelled()
            try:
                selected = self.entry.query_engine.resolve_signal(path)
            except (KeyError, ValueError):
                continue
            resolution = self.entry.hierarchy_provider.resolve_scope(top=self.scope.top, signal_path=path)
            if resolution is not None and resolution.ancestors[-1] == selected.instance_path:
                roots.add(selected.instance_path)
        if not roots:
            raise SourceGraphTraceFallbackRequired('source_graph_trace_frontier_scope_unresolved')
        depth = self.search.depth + 1 if roots <= set(self.search.roots) else max(1, self.search.depth)
        if depth <= 2:
            raise SourceGraphTraceScopeExpansion(paths, semantic_roots=tuple(sorted(roots)), semantic_depth=depth)
