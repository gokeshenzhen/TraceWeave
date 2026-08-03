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
    ScopeRelation,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphIdentity,
    SourceGraphScopeReceipt,
    compare_source_graph_scopes,
    compute_source_graph_build_key,
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
    return SourceGraphBuildRequest(
        identity=SourceGraphIdentity(
            compile_inputs=manifest or _manifest(),
            frontend_name="Slang/pyslang",
            frontend_version=frontend_version,
            ir_schema_version=ir_version,
            projector_version=projector_version,
            projector_schema_version=projector_schema_version,
        ),
        scope=scope or _scope(),
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
        lambda: _path_scope(to_signal="top.u_third.in", to_instance="top.u_third"),
        lambda: _path_scope(expand_assigns=True),
        lambda: _path_scope(traversal_limit=2048),
        lambda: _path_scope(output_limit=128),
    ],
)
def test_path_build_key_remains_exact_endpoint_and_semantics_specific(changed_scope):
    baseline = compute_source_graph_build_key(_request(scope=_path_scope()))
    changed = compute_source_graph_build_key(_request(scope=changed_scope()))

    assert changed.digest != baseline.digest
    assert changed.design_digest == baseline.design_digest


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
        lambda: _request(scope=_scope(max_hops=3)),
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
