from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from src.connectivity_ir import CoverageStatus
from src.source_graph_contract import (
    BoundaryMode,
    CompileInputManifest,
    ConnectivityPathTarget,
    ConnectivityTarget,
    CoverageBoundary,
    PathHierarchyScope,
    QueryOperation,
    RequestedCone,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    ScopeRelation,
    SourceGraphArtifactIdentity,
    SourceGraphArtifactScope,
    SourceGraphArtifactScopeReceipt,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphIdentity,
    SourceGraphQueryIdentity,
    SourceGraphScopeReceipt,
    compare_source_graph_artifact_scopes,
    compare_source_graph_scopes,
    compute_source_graph_artifact_key,
    compute_source_graph_build_key,
    compute_source_graph_query_key,
)


def _fingerprint(label: str = "compile") -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _manifest(**overrides) -> CompileInputManifest:
    values = {
        "fingerprint": _fingerprint(),
        "ordered_inputs": ("rtl/pkg.sv", "rtl/top.sv"),
        "ordered_options": ("-sv", "+define+FOO=1"),
        "ordered_tops": ("top",),
        "inputs_complete": True,
        "options_complete": True,
        "tops_complete": True,
    }
    values.update(overrides)
    return CompileInputManifest(**values)


def _scope(
    *,
    operation: QueryOperation = QueryOperation.DRIVER,
    signal_path: str = "top.u_dut.u_leaf.q",
    max_hops: int = 2,
    cone_paths=("top.u_dut.u_leaf",),
    boundary_paths=("top", "top.u_dut", "top.u_dut.u_leaf"),
    boundary_mode: BoundaryMode = BoundaryMode.EXPLICIT,
    exclusions=(),
) -> SourceGraphBuildScope:
    return SourceGraphBuildScope(
        design="unit_fixture",
        top="top",
        target=ConnectivityTarget(operation=operation, signal_path=signal_path),
        hierarchy_ancestors=("top", "top.u_dut", "top.u_dut.u_leaf"),
        requested_cone=RequestedCone(
            operation=operation,
            max_hops=max_hops,
            instance_paths=tuple(cone_paths),
        ),
        coverage_boundary=CoverageBoundary(
            mode=boundary_mode,
            instance_paths=tuple(boundary_paths),
            objective_exclusions=tuple(exclusions),
        ),
    )


def _request(
    *,
    manifest: CompileInputManifest | None = None,
    scope: SourceGraphBuildScope | None = None,
    frontend_version: str = "11.0.0",
    ir_version: str = "1.0",
    projector_version: str = "1.0",
    projector_schema_version: str = "1.0",
) -> SourceGraphBuildRequest:
    build_scope = scope or _scope()
    source = SourceGraphIdentity(
        compile_inputs=manifest or _manifest(),
        frontend_name="Slang/pyslang",
        frontend_version=frontend_version,
        ir_schema_version=ir_version,
        projector_version=projector_version,
        projector_schema_version=projector_schema_version,
    )
    artifact = SourceGraphArtifactIdentity(
        source=source,
        scope=SourceGraphArtifactScope.from_build_scope(
            build_scope,
            hierarchy_snapshot_sha256=_fingerprint("hierarchy"),
        ),
        compile_snapshot_sha256=_fingerprint("compile_snapshot"),
        adapter_version="3.0",
        worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    )
    return SourceGraphBuildRequest(
        identity=source,
        scope=build_scope,
        artifact=artifact,
        query=SourceGraphQueryIdentity.from_build_scope(build_scope),
    )


def _artifact_scope(
    scope: SourceGraphBuildScope | None = None,
    *,
    hierarchy_snapshot: str | None = None,
) -> SourceGraphArtifactScope:
    return SourceGraphArtifactScope.from_build_scope(
        scope or _scope(),
        hierarchy_snapshot_sha256=hierarchy_snapshot or _fingerprint("hierarchy"),
    )


def _artifact_identity(
    scope: SourceGraphBuildScope | None = None,
    *,
    manifest: CompileInputManifest | None = None,
    hierarchy_snapshot: str | None = None,
    compile_snapshot: str | None = None,
    frontend_version: str = "11.0.0",
    adapter_version: str = "3.0",
    worker_protocol_version: str = "3.0",
) -> SourceGraphArtifactIdentity:
    return SourceGraphArtifactIdentity(
        source=SourceGraphIdentity(
            compile_inputs=manifest or _manifest(),
            frontend_name="Slang/pyslang",
            frontend_version=frontend_version,
        ),
        scope=_artifact_scope(scope, hierarchy_snapshot=hierarchy_snapshot),
        compile_snapshot_sha256=compile_snapshot or _fingerprint("compile_snapshot"),
        adapter_version=adapter_version,
        worker_protocol_version=worker_protocol_version,
    )


def _path_scope(
    *,
    from_signal: str = "top.u_left.out",
    to_signal: str = "top.u_right.in",
    from_instance: str = "top.u_left",
    to_instance: str = "top.u_right",
    expand_assigns: bool = False,
    traversal_limit: int = 4096,
    output_limit: int = 256,
) -> SourceGraphBuildScope:
    from_chain = ("top", from_instance)
    to_chain = ("top", to_instance)
    union = tuple(
        sorted(set(from_chain + to_chain), key=lambda path: (path.count("."), path))
    )
    path_hierarchy = PathHierarchyScope(
        from_ancestors=from_chain,
        to_ancestors=to_chain,
        ancestor_union=union,
        lca="top" if from_instance != to_instance else from_instance,
    )
    return SourceGraphBuildScope(
        design="unit_fixture",
        top="top",
        target=ConnectivityPathTarget(
            operation=QueryOperation.PATH,
            from_signal=from_signal,
            to_signal=to_signal,
            from_instance_path=from_instance,
            to_instance_path=to_instance,
            expand_assigns=expand_assigns,
            traversal_limit=traversal_limit,
            output_limit=output_limit,
        ),
        hierarchy_ancestors=union,
        requested_cone=RequestedCone(
            operation=QueryOperation.PATH,
            max_hops=4,
            instance_paths=(from_instance, to_instance),
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=union,
        ),
        path_hierarchy=path_hierarchy,
    )


def test_complete_request_round_trips_and_has_reusable_stable_key():
    request = _request()

    round_trip = SourceGraphBuildRequest.from_dict(request.to_dict())
    first = compute_source_graph_build_key(request)
    second = compute_source_graph_build_key(round_trip)

    assert round_trip == request
    assert first == second
    assert first.cross_request_reusable is True
    assert first.cache_key == first.digest
    assert len(first.design_digest) == 64
    assert len(first.scope_digest) == 64


def test_dual_endpoint_path_request_round_trips_with_proved_lca_scope():
    request = _request(scope=_path_scope(expand_assigns=True))

    round_trip = SourceGraphBuildRequest.from_dict(request.to_dict())
    target = round_trip.scope.target

    assert round_trip == request
    assert isinstance(target, ConnectivityPathTarget)
    assert target.operation is QueryOperation.PATH
    assert target.expand_assigns is True
    assert target.from_instance_path == "top.u_left"
    assert target.to_instance_path == "top.u_right"
    assert round_trip.scope.path_hierarchy == PathHierarchyScope(
        from_ancestors=("top", "top.u_left"),
        to_ancestors=("top", "top.u_right"),
        ancestor_union=("top", "top.u_left", "top.u_right"),
        lca="top",
    )


@pytest.mark.parametrize(
    "changed_scope",
    [
        lambda: _path_scope(to_signal="top.u_right.other"),
        lambda: _path_scope(expand_assigns=True),
        lambda: _path_scope(traversal_limit=2048),
        lambda: _path_scope(output_limit=128),
    ],
)
def test_path_build_key_excludes_target_and_query_only_semantics(changed_scope):
    baseline = compute_source_graph_build_key(_request(scope=_path_scope()))
    changed = compute_source_graph_build_key(_request(scope=changed_scope()))

    assert changed.digest == baseline.digest
    assert changed.design_digest == baseline.design_digest


def test_path_build_key_changes_when_proved_projection_scope_changes():
    baseline = compute_source_graph_build_key(_request(scope=_path_scope()))
    changed = compute_source_graph_build_key(
        _request(
            scope=_path_scope(to_signal="top.u_third.in", to_instance="top.u_third")
        )
    )

    assert changed.digest != baseline.digest


def test_path_target_supports_dotted_signal_members_without_guessing_instance():
    target = ConnectivityPathTarget(
        operation=QueryOperation.PATH,
        from_signal="top.u_left.bus.data",
        to_signal="top.u_right.bus.data",
        from_instance_path="top.u_left",
        to_instance_path="top.u_right",
    )

    assert target.from_instance_path == "top.u_left"
    assert target.to_instance_path == "top.u_right"


def test_path_target_rejects_unproved_endpoint_instance_membership():
    with pytest.raises(ValueError, match="must be within"):
        ConnectivityPathTarget(
            operation=QueryOperation.PATH,
            from_signal="top.u_left.out",
            to_signal="top.u_right.in",
            from_instance_path="top.u_other",
            to_instance_path="top.u_right",
        )


def test_path_hierarchy_rejects_different_tops_and_wrong_lca():
    with pytest.raises(ValueError, match="share one top"):
        PathHierarchyScope(
            from_ancestors=("top_a", "top_a.u_left"),
            to_ancestors=("top_b", "top_b.u_right"),
            ancestor_union=(
                "top_a",
                "top_b",
                "top_a.u_left",
                "top_b.u_right",
            ),
            lca="top_a",
        )
    with pytest.raises(ValueError, match="lowest common ancestor"):
        PathHierarchyScope(
            from_ancestors=("top", "top.u_left"),
            to_ancestors=("top", "top.u_right"),
            ancestor_union=("top", "top.u_left", "top.u_right"),
            lca="top.u_left",
        )


@pytest.mark.parametrize(
    "changed_request",
    [
        lambda: _request(
            manifest=_manifest(ordered_inputs=("rtl/top.sv", "rtl/pkg.sv"))
        ),
        lambda: _request(manifest=_manifest(fingerprint=_fingerprint("changed"))),
        lambda: _request(manifest=_manifest(ordered_options=("+define+FOO=1", "-sv"))),
        lambda: _request(manifest=_manifest(ordered_tops=("helper", "top"))),
        lambda: _request(frontend_version="11.1.0"),
        lambda: _request(ir_version="2.0"),
        lambda: _request(projector_version="1.1"),
        lambda: _request(projector_schema_version="2.0"),
    ],
)
def test_key_covers_ordered_inputs_versions_and_normalized_scope(changed_request):
    baseline = compute_source_graph_build_key(_request())
    changed = compute_source_graph_build_key(changed_request())

    assert changed.digest != baseline.digest


def test_scope_set_fields_are_normalized_before_hashing():
    left = _request(
        scope=_scope(
            cone_paths=(" top.u_dut.u_leaf ", "top.u_dut"),
            boundary_paths=("top.u_dut.u_leaf", " top ", "top.u_dut"),
        )
    )
    right = _request(
        scope=_scope(
            cone_paths=("top.u_dut", "top.u_dut.u_leaf"),
            boundary_paths=("top", "top.u_dut", "top.u_dut.u_leaf"),
        )
    )

    assert compute_source_graph_build_key(left) == compute_source_graph_build_key(right)


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        (_manifest(fingerprint=None), "compile_fingerprint_missing"),
        (_manifest(inputs_complete=False), "compile_input_order_incomplete"),
        (_manifest(options_complete=False), "compile_option_order_incomplete"),
        (_manifest(tops_complete=False), "compile_top_order_incomplete"),
    ],
)
def test_incomplete_compile_inputs_never_produce_cross_request_cache_key(
    manifest, reason
):
    key = compute_source_graph_build_key(_request(manifest=manifest))

    assert key.cross_request_reusable is False
    assert key.cache_key is None
    assert reason in key.incomplete_reasons


def test_explicit_finite_boundary_rejects_implicit_full_hierarchy_wildcard():
    with pytest.raises(ValueError, match="wildcards"):
        CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top.*",),
        )


def test_target_requires_an_instance_qualified_signal_path():
    with pytest.raises(ValueError, match="instance and signal"):
        ConnectivityTarget(operation=QueryOperation.DRIVER, signal_path="orphan")


def test_scope_requires_boundary_to_cover_ancestors_and_requested_cone():
    with pytest.raises(ValueError, match="ancestors and requested cone"):
        _scope(boundary_paths=("top.u_dut.u_leaf",))


def test_exact_and_proven_superset_scopes_are_reusable():
    requested = _scope(max_hops=2)
    superset = _scope(
        max_hops=4,
        cone_paths=("top.u_dut", "top.u_dut.u_leaf"),
        boundary_paths=(
            "top",
            "top.u_dut",
            "top.u_dut.u_leaf",
            "top.u_dut.u_other",
        ),
    )
    exact_receipt = SourceGraphScopeReceipt(
        scope=requested,
        coverage_status=CoverageStatus.COMPLETE,
    )
    superset_receipt = SourceGraphScopeReceipt(
        scope=superset,
        coverage_status=CoverageStatus.COMPLETE,
    )

    assert exact_receipt.reuse_for(requested).relation is ScopeRelation.EXACT
    assert exact_receipt.reuse_for(requested).complete_for_request is True
    match = superset_receipt.reuse_for(requested)
    assert match.relation is ScopeRelation.SUPERSET
    assert match.reusable is True
    assert match.complete_for_request is True


def test_subset_and_unscoped_gap_cannot_masquerade_as_complete():
    requested = _scope(max_hops=4)
    subset = _scope(max_hops=1)
    subset_receipt = SourceGraphScopeReceipt(
        scope=subset,
        coverage_status=CoverageStatus.COMPLETE,
    )

    match = subset_receipt.reuse_for(requested)
    assert match.relation is ScopeRelation.SUBSET
    assert match.reusable is False
    assert match.complete_for_request is False

    unscoped = replace(
        requested,
        coverage_boundary=CoverageBoundary(mode=BoundaryMode.UNSCOPED_GAP),
    )
    with pytest.raises(ValueError, match="unscoped coverage cannot be complete"):
        SourceGraphScopeReceipt(
            scope=unscoped,
            coverage_status=CoverageStatus.COMPLETE,
        )
    assert compare_source_graph_scopes(unscoped, requested) is ScopeRelation.UNPROVEN


def test_partial_exact_hit_preserves_partial_coverage_instead_of_claiming_complete():
    scope = _scope(exclusions=("uvm_runtime", "dpi_call"))
    receipt = SourceGraphScopeReceipt(
        scope=scope,
        coverage_status=CoverageStatus.PARTIAL,
        gap_codes=("unsupported_runtime_construct",),
    )

    match = receipt.reuse_for(scope)
    assert match.reusable is True
    assert match.coverage_status is CoverageStatus.PARTIAL
    assert match.complete_for_request is False
    assert match.reason == "coverage_preserved_partial"


def test_complete_receipt_rejects_explicit_objective_exclusions():
    with pytest.raises(ValueError, match="gaps or exclusions"):
        SourceGraphScopeReceipt(
            scope=_scope(exclusions=("protected_region",)),
            coverage_status=CoverageStatus.COMPLETE,
        )


def test_partial_explicit_receipt_requires_a_machine_readable_gap():
    with pytest.raises(ValueError, match="requires a gap receipt"):
        SourceGraphScopeReceipt(
            scope=_scope(),
            coverage_status=CoverageStatus.PARTIAL,
        )


def test_artifact_identity_ignores_driver_target_and_operation_but_query_does_not():
    driver_scope = _scope(
        operation=QueryOperation.DRIVER,
        signal_path="top.u_dut.u_leaf.q",
    )
    other_driver_scope = _scope(
        operation=QueryOperation.DRIVER,
        signal_path="top.u_dut.u_leaf.other_q",
    )
    loads_scope = _scope(
        operation=QueryOperation.LOADS,
        signal_path="top.u_dut.u_leaf.q",
    )

    artifact_keys = {
        compute_source_graph_artifact_key(_artifact_identity(scope)).digest
        for scope in (driver_scope, other_driver_scope, loads_scope)
    }
    query_keys = {
        compute_source_graph_query_key(
            SourceGraphQueryIdentity.from_build_scope(scope)
        ).digest
        for scope in (driver_scope, other_driver_scope, loads_scope)
    }

    assert len(artifact_keys) == 1
    assert len(query_keys) == 3


def test_artifact_query_and_scope_receipt_contracts_round_trip():
    artifact = _artifact_identity(_path_scope())
    query = SourceGraphQueryIdentity.from_build_scope(
        _scope(operation=QueryOperation.LOADS),
        include_expr=False,
        kind_filter=("rhs_expr",),
    )
    receipt = SourceGraphArtifactScopeReceipt(
        scope=artifact.scope,
        coverage_status=CoverageStatus.COMPLETE,
    )

    assert SourceGraphArtifactIdentity.from_dict(artifact.to_dict()) == artifact
    assert SourceGraphQueryIdentity.from_dict(query.to_dict()) == query
    assert SourceGraphArtifactScopeReceipt.from_dict(receipt.to_dict()) == receipt


def test_worker_build_request_serializes_artifact_without_query_identity():
    scope = _scope(signal_path="top.u_dut.u_leaf.private_target")
    request = SourceGraphBuildRequest(
        identity=_artifact_identity(scope).source,
        scope=scope,
        artifact=_artifact_identity(scope),
        query=SourceGraphQueryIdentity.from_build_scope(scope, recursive=True),
    )

    payload = request.artifact_build_request.to_dict()
    rendered = str(payload)

    assert request.artifact_build_request.__class__.from_dict(payload) == (
        request.artifact_build_request
    )
    assert set(payload) == {"contract_version", "artifact_identity"}
    assert "query_identity" not in rendered
    assert "private_target" not in rendered
    assert request.query_identity.target.signal_path.endswith("private_target")


def test_legacy_multibranch_request_is_exact_keyed_and_bounded():
    legacy_scope = _scope(
        signal_path="top.u_dut.u_leaf.q",
        cone_paths=("top.u_dut.u_leaf", "top.u_other"),
        boundary_paths=("top", "top.u_dut", "top.u_dut.u_leaf", "top.u_other"),
    )
    source = SourceGraphIdentity(
        compile_inputs=_manifest(),
        frontend_name="Slang/pyslang",
        frontend_version="11.0.0",
    )
    request = SourceGraphBuildRequest(identity=source, scope=legacy_scope)
    changed_target = SourceGraphBuildRequest(
        identity=source,
        scope=replace(
            legacy_scope,
            target=ConnectivityTarget(
                operation=QueryOperation.DRIVER,
                signal_path="top.u_dut.u_leaf.other_q",
            ),
        ),
    )

    key = compute_source_graph_build_key(request)
    artifact = request.artifact_identity

    assert key.cross_request_reusable is True
    assert key != compute_source_graph_build_key(changed_target)
    assert SourceGraphBuildRequest.from_dict(request.to_dict()) == request
    assert artifact.adapter_version == "legacy_exact_request_2_0"
    assert artifact.scope.capabilities == (QueryOperation.DRIVER,)
    assert artifact.scope.projection_instance_paths == (
        "top.u_dut.u_leaf",
        "top.u_other",
    )
    assert {
        path for chain in artifact.scope.proved_ancestor_chains for path in chain
    }.issubset(artifact.scope.coverage_boundary.instance_paths)


def test_build_request_rejects_artifact_that_does_not_cover_query_scope():
    query_scope = _scope(signal_path="top.u_dut.u_leaf.q")
    sibling_scope = SourceGraphBuildScope(
        design="unit_fixture",
        top="top",
        target=ConnectivityTarget(
            operation=QueryOperation.DRIVER,
            signal_path="top.u_other.q",
        ),
        hierarchy_ancestors=("top", "top.u_other"),
        requested_cone=RequestedCone(
            operation=QueryOperation.DRIVER,
            max_hops=2,
            instance_paths=("top.u_other",),
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top", "top.u_other"),
        ),
    )
    sibling_artifact = _artifact_identity(sibling_scope)

    with pytest.raises(ValueError, match="does not cover the query scope"):
        SourceGraphBuildRequest(
            identity=sibling_artifact.source,
            scope=query_scope,
            artifact=sibling_artifact,
            query=SourceGraphQueryIdentity.from_build_scope(query_scope),
        )


def test_driver_load_and_same_instance_path_share_one_artifact_identity():
    def single(operation: QueryOperation) -> SourceGraphBuildScope:
        return SourceGraphBuildScope(
            design="unit_fixture",
            top="top",
            target=ConnectivityTarget(
                operation=operation,
                signal_path="top.u_left.q",
            ),
            hierarchy_ancestors=("top", "top.u_left"),
            requested_cone=RequestedCone(
                operation=operation,
                max_hops=8,
                instance_paths=("top.u_left",),
            ),
            coverage_boundary=CoverageBoundary(
                mode=BoundaryMode.EXPLICIT,
                instance_paths=("top", "top.u_left"),
            ),
        )

    scopes = (
        single(QueryOperation.DRIVER),
        single(QueryOperation.LOADS),
        _path_scope(
            from_signal="top.u_left.q",
            to_signal="top.u_left.d",
            from_instance="top.u_left",
            to_instance="top.u_left",
        ),
    )

    assert (
        len(
            {
                compute_source_graph_artifact_key(_artifact_identity(scope)).digest
                for scope in scopes
            }
        )
        == 1
    )
    assert (
        len(
            {
                compute_source_graph_query_key(
                    SourceGraphQueryIdentity.from_build_scope(scope)
                ).digest
                for scope in scopes
            }
        )
        == 3
    )


def test_path_expand_and_query_caps_change_query_not_artifact_identity():
    scopes = (
        _path_scope(expand_assigns=False),
        _path_scope(expand_assigns=True),
        _path_scope(traversal_limit=2048),
        _path_scope(output_limit=128),
    )

    artifact_keys = {
        compute_source_graph_artifact_key(_artifact_identity(scope)).digest
        for scope in scopes
    }
    query_keys = {
        compute_source_graph_query_key(
            SourceGraphQueryIdentity.from_build_scope(scope)
        ).digest
        for scope in scopes
    }

    assert len(artifact_keys) == 1
    assert len(query_keys) == 4


def test_driver_query_depth_and_mapping_flags_do_not_rebuild_artifact():
    shallow = _scope(max_hops=1)
    deep = _scope(max_hops=8)
    shallow_artifact = compute_source_graph_artifact_key(_artifact_identity(shallow))
    deep_artifact = compute_source_graph_artifact_key(_artifact_identity(deep))

    shallow_query = SourceGraphQueryIdentity.from_build_scope(shallow, recursive=False)
    deep_query = SourceGraphQueryIdentity.from_build_scope(deep, recursive=True)

    assert shallow_artifact == deep_artifact
    assert compute_source_graph_query_key(shallow_query) != (
        compute_source_graph_query_key(deep_query)
    )


def test_legacy_cone_policy_fields_are_not_artifact_inputs_when_worker_ignores_them():
    baseline = _scope()
    inert_policy_change = replace(
        baseline,
        requested_cone=replace(
            baseline.requested_cone,
            cross_instance_boundaries=False,
            stop_at_sequential=False,
            include_control_dependencies=True,
        ),
    )

    assert compute_source_graph_artifact_key(_artifact_identity(baseline)) == (
        compute_source_graph_artifact_key(_artifact_identity(inert_policy_change))
    )
    assert SourceGraphQueryIdentity.from_build_scope(baseline) == (
        SourceGraphQueryIdentity.from_build_scope(inert_policy_change)
    )


@pytest.mark.parametrize(
    "changed",
    [
        lambda: _artifact_identity(
            manifest=_manifest(fingerprint=_fingerprint("changed_content"))
        ),
        lambda: _artifact_identity(frontend_version="11.1.0"),
        lambda: _artifact_identity(adapter_version="3.1"),
        lambda: _artifact_identity(worker_protocol_version="3.1"),
        lambda: _artifact_identity(
            compile_snapshot=_fingerprint("new_compile_snapshot")
        ),
        lambda: _artifact_identity(
            hierarchy_snapshot=_fingerprint("new_hierarchy_snapshot")
        ),
    ],
)
def test_artifact_affecting_content_versions_and_snapshots_change_key(changed):
    assert compute_source_graph_artifact_key(changed()) != (
        compute_source_graph_artifact_key(_artifact_identity())
    )


def test_path_ancestor_union_artifact_dominates_covered_single_endpoint():
    path_artifact = _artifact_scope(_path_scope())
    single_scope = SourceGraphBuildScope(
        design="unit_fixture",
        top="top",
        target=ConnectivityTarget(
            operation=QueryOperation.DRIVER,
            signal_path="top.u_left.out",
        ),
        hierarchy_ancestors=("top", "top.u_left"),
        requested_cone=RequestedCone(
            operation=QueryOperation.DRIVER,
            max_hops=2,
            instance_paths=("top.u_left",),
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top", "top.u_left"),
        ),
    )
    single_artifact = _artifact_scope(single_scope)

    assert (
        compare_source_graph_artifact_scopes(path_artifact, single_artifact)
        is ScopeRelation.SUPERSET
    )
    assert (
        compare_source_graph_artifact_scopes(single_artifact, path_artifact)
        is ScopeRelation.SUBSET
    )


def test_smaller_endpoint_artifact_cannot_serve_sibling_lca_path():
    path_artifact = _artifact_scope(_path_scope())
    single_artifact = SourceGraphArtifactScope(
        design="unit_fixture",
        top="top",
        hierarchy_snapshot_sha256=_fingerprint("hierarchy"),
        proved_ancestor_chains=(("top", "top.u_left"),),
        proved_lcas=("top.u_left",),
        projection_instance_paths=("top.u_left",),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top", "top.u_left"),
        ),
    )

    decision = SourceGraphArtifactScopeReceipt(
        scope=single_artifact,
        coverage_status=CoverageStatus.COMPLETE,
    ).reuse_for(path_artifact)

    assert decision.reusable is False
    assert decision.complete_for_request is False
    assert decision.relation is ScopeRelation.SUBSET


def test_artifact_capability_must_cover_requested_query_operation():
    scope = _artifact_scope()
    driver_only = replace(scope, capabilities=(QueryOperation.DRIVER,))

    assert (
        compare_source_graph_artifact_scopes(driver_only, scope) is ScopeRelation.SUBSET
    )
    assert (
        SourceGraphArtifactScopeReceipt(
            scope=driver_only,
            coverage_status=CoverageStatus.COMPLETE,
        )
        .reuse_for(scope)
        .reusable
        is False
    )


def test_artifact_dominance_blocks_different_top_and_snapshot():
    baseline = _artifact_scope()
    different_top = SourceGraphArtifactScope(
        design="unit_fixture",
        top="other_top",
        hierarchy_snapshot_sha256=_fingerprint("hierarchy"),
        proved_ancestor_chains=(("other_top", "other_top.u_leaf"),),
        proved_lcas=("other_top.u_leaf",),
        projection_instance_paths=("other_top.u_leaf",),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("other_top", "other_top.u_leaf"),
        ),
    )
    refreshed = replace(
        baseline,
        hierarchy_snapshot_sha256=_fingerprint("refreshed_hierarchy"),
    )

    assert (
        compare_source_graph_artifact_scopes(baseline, different_top)
        is ScopeRelation.DISJOINT
    )
    assert (
        compare_source_graph_artifact_scopes(baseline, refreshed)
        is ScopeRelation.UNPROVEN
    )


def test_artifact_scope_canonicalization_and_dominance_are_deterministic():
    boundary = CoverageBoundary(
        mode=BoundaryMode.EXPLICIT,
        instance_paths=("top.u_right", "top", "top.u_left"),
    )
    left = SourceGraphArtifactScope(
        design="unit_fixture",
        top="top",
        hierarchy_snapshot_sha256=_fingerprint("hierarchy"),
        proved_ancestor_chains=(
            ("top", "top.u_right"),
            ("top", "top.u_left"),
        ),
        proved_lcas=("top",),
        projection_instance_paths=("top.u_right", "top.u_left"),
        coverage_boundary=boundary,
    )
    right = SourceGraphArtifactScope(
        design="unit_fixture",
        top="top",
        hierarchy_snapshot_sha256=_fingerprint("hierarchy"),
        proved_ancestor_chains=(
            ("top", "top.u_left"),
            ("top", "top.u_right"),
        ),
        proved_lcas=("top", "top"),
        projection_instance_paths=("top.u_left", "top.u_right"),
        coverage_boundary=boundary,
    )

    assert left == right
    assert compare_source_graph_artifact_scopes(left, right) is ScopeRelation.EXACT


def test_artifact_scope_rejects_missing_or_noncanonical_lca_proof():
    kwargs = {
        "design": "unit_fixture",
        "top": "top",
        "hierarchy_snapshot_sha256": _fingerprint("hierarchy"),
        "proved_ancestor_chains": (
            ("top", "top.u_left"),
            ("top", "top.u_right"),
        ),
        "projection_instance_paths": ("top.u_left", "top.u_right"),
        "coverage_boundary": CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top", "top.u_left", "top.u_right"),
        ),
    }

    with pytest.raises(ValueError, match="exactly one proved"):
        SourceGraphArtifactScope(proved_lcas=(), **kwargs)
    with pytest.raises(ValueError, match="canonical chain intersection"):
        SourceGraphArtifactScope(proved_lcas=("top.u_left",), **kwargs)


def test_dominating_partial_artifact_preserves_non_exact_coverage():
    path_artifact = _artifact_scope(_path_scope())
    single_artifact = SourceGraphArtifactScope(
        design="unit_fixture",
        top="top",
        hierarchy_snapshot_sha256=_fingerprint("hierarchy"),
        proved_ancestor_chains=(("top", "top.u_left"),),
        proved_lcas=("top.u_left",),
        projection_instance_paths=("top.u_left",),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("top", "top.u_left"),
        ),
    )
    partial_path = replace(
        path_artifact,
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=path_artifact.coverage_boundary.instance_paths,
            objective_exclusions=("protected_region",),
        ),
    )
    partial_single = replace(
        single_artifact,
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=single_artifact.coverage_boundary.instance_paths,
            objective_exclusions=("protected_region",),
        ),
    )

    decision = SourceGraphArtifactScopeReceipt(
        scope=partial_path,
        coverage_status=CoverageStatus.INCONCLUSIVE,
        gap_codes=("frontend_diagnostic",),
    ).reuse_for(partial_single)

    assert decision.reusable is True
    assert decision.coverage_status is CoverageStatus.INCONCLUSIVE
    assert decision.complete_for_request is False
    assert decision.reason == "coverage_preserved_inconclusive"
