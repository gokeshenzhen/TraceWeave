"""Source plans tied to one immutable prepared design, without a lexical tree.

Candidate prefixes request exact semantic lookup; only the worker's real
Instance objects prove hierarchy. All queries retain the normal manifest,
artifact identity, cache, single-flight and isolated-worker contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
import time

from .slang_connectivity_projector import SLANG_FRONTEND_NAME
from .cancellation import check_cancelled
from .compile_input_capture import CaptureAborted
from .source_graph_adapter import (
    AdapterStatus, SourceGraphAdapterReceipt, SourceGraphBuildPlan,
    SOURCE_GRAPH_ADAPTER_VERSION, _blocked_plan, _compile_manifest,
)
from .source_graph_contract import (
    BoundaryMode, ConnectivityPathTarget, ConnectivityTarget, CoverageBoundary,
    PathHierarchyScope, QueryOperation, RequestedCone, SemanticScopeSearch,
    SourceGraphArtifactIdentity, SourceGraphArtifactScope, SourceGraphBuildRequest,
    SourceGraphBuildScope, SourceGraphIdentity, SourceGraphQueryIdentity,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION, compute_source_graph_artifact_key,
    compute_source_graph_query_key,
)

_PREFIX = re.compile(r"[a-zA-Z_$][\w$]*(?:\[-?\d+\])*(?:\.[a-zA-Z_$][\w$]*(?:\[-?\d+\])*)*")


@dataclass
class SourceQueryBudget:
    deadline: float
    instances: int = 64
    members: int = 4096
    edges: int = 16384
    states: int = 4096
    assignments: int = 16384
    matches: int = 256
    frontiers: int = 4096
    evidence: int = 4096

    def remaining(self):
        check_cancelled()
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("prepared source query deadline")
        return value

    def account_projection(self, entry):
        metadata = dict(entry.ir.metadata)
        self.instances -= int(metadata.get("semantic_instance_count", len(entry.ir.instances)))
        self.members -= int(metadata.get("semantic_member_visits", 0))
    def account_query(self, query):
        self.edges -= int(query.get("traversed_edge_count", query.get("inspected_edge_count", 0)))
        self.states -= int(query.get("visited_state_count", 0))
        self.assignments -= int(query.get("inspected_assignment_count", 0))
        self.matches -= int(query.get("match_count", 0))
        self.frontiers -= len(query.get("expansion_frontiers", ()))
        self.evidence -= int(query.get("retained_evidence_count", 0))

    @property
    def exhausted(self):
        return min(self.instances, self.members, self.edges, self.states, self.assignments, self.matches, self.frontiers, self.evidence) <= 0


def build_prepared_source_plan(*, context, operation, args, config,
                               search=None, base=None, signal_paths=None):
    """One shared planner for one/two endpoints; no mutable global hierarchy."""
    check_cancelled()
    operation = QueryOperation(operation)
    top = context.selected_top
    signals = tuple(signal_paths) if signal_paths is not None else (
        (args["from_signal"], args["to_signal"]) if operation is QueryOperation.PATH
        else (args["signal_path"],))
    if not signals or len(signals) > 64:
        return _blocked_plan(code="semantic_scope_candidate_limit", stage="target_scope")
    prefixes = set()
    parents = set()
    for signal in signals:
        if not isinstance(signal, str) or "." not in signal:
            return _blocked_plan(code="signal_path_unscoped", stage="target_scope")
        parent = signal.strip().rpartition(".")[0]
        parents.add(parent)
        parts = parent.split(".")
        if len(parts) > 64 or not _PREFIX.fullmatch(parent):
            return _blocked_plan(code="semantic_scope_candidate_limit", stage="target_scope")
        if not top or parts[0] != top:
            return _blocked_plan(code="target_top_unresolved", stage="target_scope")
        prefixes.update(".".join(parts[:i]) for i in range(2, len(parts) + 1))
    if len(prefixes) + 1 > 64:
        return _blocked_plan(code="semantic_scope_candidate_limit", stage="target_scope")
    search = replace(search or SemanticScopeSearch(), assignment_candidates=tuple(sorted(parents)))
    if base is None:
        manifest, gaps, exclusions, disposition, snapshot, digests = _compile_manifest(
            context.ordered_compile_logs[0], context.compile_result(),
            content_snapshot=context.snapshot,
            runtime_plusarg_allowlist=config.runtime_plusarg_allowlist)
        if set(gaps) & {"compile_session_snapshot_changed", "compile_snapshot_changed"}:
            raise CaptureAborted("input_changed")
        if not manifest.ordered_inputs or top not in manifest.ordered_tops:
            return _blocked_plan(code="compile_context_unavailable", stage="compile_manifest",
                                 manifest=manifest, gaps=gaps, exclusions=exclusions)
        if not manifest.complete:
            exclusions = tuple(sorted({*exclusions, "compile_manifest_incomplete"}))
        # Incomplete manifests keep their normal non-reusable key and bounded
        # handoff; a captured context is not permission to promote completeness.
        source = SourceGraphIdentity(compile_inputs=manifest, frontend_name=SLANG_FRONTEND_NAME,
                                     frontend_version=config.frontend_version)
    else:
        source = base.request.identity
        manifest = source.compile_inputs
        gaps, exclusions = base.receipt.gap_codes, base.receipt.objective_exclusions
        disposition = base.receipt.fingerprint_cache_disposition
        snapshot = base.request.artifact_identity.compile_snapshot_sha256
        digests = {name: 0 for name in ("content_digest_reuse_count", "content_digest_reuse_bytes",
                   "content_digest_read_count", "content_digest_read_bytes", "content_snapshot_conflict_count")}
        base_search = base.request.scope.semantic_search
        if (base_search is not None and base_search.purpose == "hierarchy"
                and search.purpose != "hierarchy"):
            # A hierarchy receipt has capability-specific effective exclusions,
            # not the original global connectivity exclusions. Other purposes
            # need fresh compile evidence rather than inheriting that receipt.
            return _blocked_plan(code="prepared_base_capability_mismatch", stage="compile_manifest",
                manifest=manifest, gaps=gaps, exclusions=exclusions,
                fingerprint_cache_disposition=disposition, digest_metrics=digests)
    if search.purpose == "hierarchy":
        # The topology-only capability observes bind attachments through real
        # native owner membership. The compile manifest retains this exclusion
        # for connectivity's unresolved runtime signal semantics; it does not
        # exclude the supported elaborated-scope inventory. All other compile
        # barriers, native diagnostics and hierarchy frontiers remain active.
        exclusions = tuple(code for code in exclusions if code != "bind_semantics")
    paths = (top, *sorted(prefixes))
    if operation is QueryOperation.PATH:
        target = ConnectivityPathTarget(operation, *signals, top, top,
                                        expand_assigns=args.get("expand_assigns", False))
        hierarchy = PathHierarchyScope((top,), (top,), (top,), top)
    else:
        target = ConnectivityTarget(operation, signals[0], top)
        hierarchy = None
    scope = SourceGraphBuildScope(
        design=f"compile_{manifest.fingerprint[:24]}" if manifest.fingerprint else "compile_incomplete_manifest",
        top=top, target=target, hierarchy_ancestors=(top,),
        requested_cone=RequestedCone(operation=operation,
            max_hops=args.get("max_depth", 10 if operation is QueryOperation.DRIVER else 1),
            instance_paths=paths, cross_instance_boundaries=True,
            stop_at_sequential=True, include_control_dependencies=False),
        coverage_boundary=CoverageBoundary(BoundaryMode.EXPLICIT, paths, tuple(exclusions)),
        path_hierarchy=hierarchy, elaboration_candidates=tuple(sorted(prefixes)),
        semantic_search=search)
    artifact = SourceGraphArtifactIdentity(
        source=source, scope=SourceGraphArtifactScope.from_build_scope(
            scope, hierarchy_snapshot_sha256=context.identity_sha256),
        compile_snapshot_sha256=snapshot or context.identity_sha256,
        adapter_version=SOURCE_GRAPH_ADAPTER_VERSION,
        worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        snapshots_complete=(snapshot is not None if base is None else base.request.artifact_identity.snapshots_complete))
    query = SourceGraphQueryIdentity.from_build_scope(scope,
        recursive=args.get("recursive", False), include_expr=args.get("include_expr", True),
        kind_filter=tuple(args.get("kind_filter") or ()))
    request = SourceGraphBuildRequest(identity=source, scope=scope, artifact=artifact, query=query)
    key = compute_source_graph_artifact_key(artifact)
    receipt = SourceGraphAdapterReceipt(status=AdapterStatus.READY,
        input_count=len(manifest.ordered_inputs), option_count=len(manifest.ordered_options),
        top_count=len(manifest.ordered_tops), manifest_complete=manifest.complete,
        manifest_incomplete_reasons=manifest.incomplete_reasons, gap_codes=tuple(gaps),
        objective_exclusions=tuple(exclusions), ancestor_count=1,
        requested_cone_instance_count=len(paths), coverage_boundary_instance_count=len(paths),
        scope_kind=("dual_endpoint_path" if operation is QueryOperation.PATH else
                    "multi_endpoint_trace" if len(signals) > 1 else "single_endpoint"),
        endpoint_count=len(signals), cross_request_reusable=key.cross_request_reusable,
        artifact_fingerprint_sha256=key.digest, query_fingerprint_sha256=compute_source_graph_query_key(query).digest,
        snapshot_identity_complete=artifact.snapshots_complete,
        fingerprint_cache_disposition=disposition, compile_projection_input_count=len(manifest.ordered_inputs),
        semantic_context_status="exact_semantic_candidates", **digests)
    return SourceGraphBuildPlan(status=AdapterStatus.READY, request=request, receipt=receipt)


def semantic_expansion_root(entry, signals):
    """Choose the actual semantic LCA, never a dotted field-name guess."""
    provider = entry.hierarchy_provider
    top = entry.artifact_scope_receipt.scope.top
    chains = []
    for signal in signals:
        check_cancelled()
        # A positive symbol resolution is required before enlarging a scope.
        try:
            entry.query_engine.resolve_signal(signal)
        except (KeyError, ValueError):
            return None
        resolution = provider.resolve_scope(top=top, signal_path=signal)
        if resolution is None:
            return None
        chains.append(resolution.ancestors)
    common = list(chains[0])
    for chain in chains[1:]:
        common = common[:next((i for i, (a, b) in enumerate(zip(common, chain)) if a != b), min(len(common), len(chain)))]
    return common[-1] if common else None
