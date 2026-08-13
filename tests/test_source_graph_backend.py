from __future__ import annotations

from dataclasses import replace
import hashlib

from src.connectivity_ir import (
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    ResolutionKind,
)
from src.connectivity_query import ConnectivityQueryEngine, QueryConfidence
from src.source_graph_backend import (
    SourceGraphConnectivityBackend,
    _public_confidence,
)
from src.source_graph_contract import (
    BoundaryMode,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    RequestedCone,
    SourceGraphBuildKey,
    SourceGraphBuildScope,
    SourceGraphArtifactScope,
    SourceGraphArtifactScopeReceipt,
    SourceGraphScopeReceipt,
)
from src.source_graph_runtime import SourceGraphCacheEntry
from tests.connectivity_ir_fixtures import build_hand_ir
from tests.test_connectivity_query import _build_segmented_binding_ir


def _entry(ir=None) -> SourceGraphCacheEntry:
    ir = ir or build_hand_ir()
    scope = SourceGraphBuildScope(
        design="source_graph_backend_fixture",
        top="sg_top",
        target=ConnectivityTarget(
            operation=QueryOperation.DRIVER,
            signal_path="sg_top.lane_data[15:8]",
        ),
        hierarchy_ancestors=("sg_top",),
        requested_cone=RequestedCone(
            operation=QueryOperation.DRIVER,
            max_hops=8,
            instance_paths=("sg_top",),
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=("sg_top",),
        ),
    )
    payload = ir.to_json_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    artifact_receipt = SourceGraphArtifactScopeReceipt(
        scope=SourceGraphArtifactScope.from_build_scope(
            scope,
            hierarchy_snapshot_sha256=hashlib.sha256(b"hierarchy").hexdigest(),
        ),
        coverage_status=ir.coverage.status,
        gap_codes=tuple(gap.code for gap in ir.coverage.gaps),
    )
    return SourceGraphCacheEntry(
        build_key=SourceGraphBuildKey(
            digest=digest,
            design_digest=digest,
            scope_digest=digest,
            cross_request_reusable=True,
            incomplete_reasons=(),
        ),
        scope_receipt=SourceGraphScopeReceipt(
            scope=scope,
            coverage_status=ir.coverage.status,
            gap_codes=tuple(gap.code for gap in ir.coverage.gaps),
        ),
        artifact_scope_receipt=artifact_receipt,
        ir=ir,
        query_engine=ConnectivityQueryEngine(ir),
        ir_json_bytes=payload,
        ir_fingerprint_sha256=ir.fingerprint_sha256(),
        ir_bytes=len(payload),
        cache_bytes=len(payload),
    )


def test_driver_mapping_uses_only_ir_source_facts():
    result = SourceGraphConnectivityBackend(_entry()).find_driver(
        signal_path="sg_top.lane_data[15:8]",
        wave_path="wave.fsdb",
        compile_log="compile.log",
        recursive=True,
        max_depth=8,
    )

    assert result["driver_status"] == "resolved"
    assert result["backend"] == "source_graph"
    assert result["source_info_origin"] == "source_graph"
    assert result["source_file"]
    assert result["source_line"] > 0
    assert result["expression_summary"] is None
    assert {hop["backend"] for hop in result["driver_chain"]} == {"source_graph"}
    assert result["_source_graph_query_receipt"]["status"] == "found"


def test_segmented_driver_mapping_reports_composite_bit_provenance():
    result = SourceGraphConnectivityBackend(
        _entry(_build_segmented_binding_ir())
    ).find_driver(
        signal_path="top.u.data_i[27:20]",
        wave_path="wave.fsdb",
        compile_log="compile.log",
        recursive=True,
        max_depth=8,
    )

    assert result["driver_status"] == "resolved"
    assert result["driver_kind"] == "composite_port_binding"
    assert result["resolved_bit_count"] == 8
    assert result["unresolved_bit_count"] == 0
    assert result["multi_driver_bit_count"] == 0
    assert [item["target_path"] for item in result["bit_provenance"]] == [
        "top.u.data_i[23:20]",
        "top.u.data_i[27:24]",
    ]
    constant = next(
        item for item in result["bit_provenance"] if item["source_kind"] == "constant"
    )
    dynamic = next(
        item for item in result["bit_provenance"] if item["source_kind"] == "signal"
    )
    assert constant["constant_value"] == "4'b0000"
    assert dynamic["source_path"] == "top.payload[23:20]"
    receipt = result["_source_graph_query_receipt"]
    assert receipt["queried_bit_count"] == 8
    assert receipt["resolved_bit_count"] == 8
    assert receipt["expansion_frontiers"] == []


def test_load_mapping_preserves_terminal_assignment_evidence():
    result = SourceGraphConnectivityBackend(_entry()).find_loads(
        signal_path="sg_top.bus.data[7:0]",
        compile_log="compile.log",
        max_depth=8,
    )

    assert result["loads"]
    assert {item["kind"] for item in result["loads"]} == {"rhs_expr"}
    assert {item["backend"] for item in result["loads"]} == {"source_graph"}
    assert all(item["expr"] is None for item in result["loads"])
    assert result["completeness"] == "exact"


def test_conditional_and_partial_confidence_mapping_is_explicit():
    assert _public_confidence(QueryConfidence.EXACT_SOURCE) == "exact"
    assert _public_confidence(QueryConfidence.CONDITIONAL) == "conditional"
    assert _public_confidence(QueryConfidence.PARTIAL) == "partial"

    ir = build_hand_ir()
    definitions = tuple(
        replace(
            definition,
            assignments=tuple(
                replace(
                    assignment,
                    evidence=replace(
                        assignment.evidence,
                        resolution=ResolutionKind.CONDITIONAL,
                    ),
                )
                for assignment in definition.assignments
            ),
        )
        for definition in ir.definitions
    )
    conditional_ir = replace(ir, definitions=definitions)
    result = SourceGraphConnectivityBackend(_entry(conditional_ir)).find_driver(
        signal_path="sg_top.lane_data[15:8]",
        wave_path="wave.fsdb",
        compile_log="compile.log",
        max_depth=8,
    )

    assert result["confidence"] == "conditional"


def test_complete_negative_maps_to_not_connected_without_source_guessing():
    result = SourceGraphConnectivityBackend(_entry()).find_driver(
        signal_path="sg_top.runtime_force",
        wave_path="wave.fsdb",
        compile_log="compile.log",
        max_depth=8,
    )

    assert result["driver_status"] == "not_connected"
    assert result["confidence"] == "exact"
    assert result["source_file"] is None
    assert result["unsupported_reason"] is None
    assert result["_source_graph_query_receipt"]["status"] == "not_connected"


def test_path_mapping_returns_only_ir_endpoints_and_edge_evidence():
    result = SourceGraphConnectivityBackend(_entry()).find_path(
        from_signal="sg_top.u_producer.seed[7:0]",
        to_signal="sg_top.u_bridge.gen_lanes[1].u_lane.u_named.data_i",
        compile_log="compile.log",
        expand_assigns=True,
    )

    assert result["found"] is True
    assert result["backend"] == "source_graph"
    assert result["hops"] == 5
    assert len(result["path"]) == 6
    assert result["path"][0]["net_path"] == "sg_top.u_producer.seed[7:0]"
    assert result["path"][-1]["net_path"].endswith("u_named.data_i[7:0]")
    assert {hop["backend"] for hop in result["path"]} == {"source_graph"}
    assert {hop["source_info_origin"] for hop in result["path"]} == {"source_graph"}
    assignment = result["path"][1]
    assert assignment["edge_kind"] == "procedural_assign"
    assert assignment["edge_id"] == "sg_producer:always_comb:84:bus.data"
    assert assignment["source_file"].endswith("hand_connectivity.sv")
    receipt = result["_source_graph_query_receipt"]
    assert receipt["status"] == "found"
    assert receipt["coverage_status"] == "complete"
    assert receipt["path_edge_count"] == 5


def test_path_expand_assigns_changes_evidence_visibility_not_connectivity():
    backend = SourceGraphConnectivityBackend(_entry())
    kwargs = {
        "from_signal": "sg_top.u_producer.seed[7:0]",
        "to_signal": "sg_top.u_bridge.gen_lanes[1].u_lane.u_named.data_i",
        "compile_log": "compile.log",
    }

    folded = backend.find_path(**kwargs, expand_assigns=False)
    expanded = backend.find_path(**kwargs, expand_assigns=True)

    assert folded["found"] is expanded["found"] is True
    assert folded["hops"] == expanded["hops"] == 5
    assert [hop["net_path"] for hop in folded["path"]] == [
        hop["net_path"] for hop in expanded["path"]
    ]
    assert folded["path"][1]["edge_kind"] is None
    assert folded["path"][1]["edge_id"] is None
    assert expanded["path"][1]["edge_kind"] == "procedural_assign"
    assert expanded["path"][1]["edge_id"] is not None


def test_path_complete_negative_and_partial_positive_map_distinctly():
    complete = SourceGraphConnectivityBackend(_entry()).find_path(
        from_signal="sg_top.runtime_force",
        to_signal="sg_top.seed",
        compile_log="compile.log",
    )
    gap = CoverageGap(
        code="scoped_projection_gap",
        message="projection is intentionally bounded",
        impact=CoverageStatus.PARTIAL,
        scopes=("sg_top.u_producer",),
    )
    partial_ir = replace(
        build_hand_ir(),
        coverage=CoverageReport(
            status=CoverageStatus.PARTIAL,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )
    partial = SourceGraphConnectivityBackend(_entry(partial_ir)).find_path(
        from_signal="sg_top.u_producer.seed[7:0]",
        to_signal="sg_top.u_bridge.gen_lanes[1].u_lane.u_named.data_i",
        compile_log="compile.log",
    )

    assert complete["found"] is False
    assert complete["unsupported_reason"] == "not_connected"
    assert complete["_source_graph_query_receipt"]["coverage_status"] == "complete"
    assert partial["found"] is True
    assert partial["unsupported_reason"] is None
    assert partial["_source_graph_query_receipt"]["coverage_status"] == "partial"
    assert partial["_source_graph_query_receipt"]["confidence"] == "partial"


def test_path_inconclusive_negative_preserves_gap_in_query_receipt():
    gap = CoverageGap(
        code="objective_exclusion",
        message="runtime behavior can affect the endpoint",
        impact=CoverageStatus.INCONCLUSIVE,
        scopes=("sg_top.runtime_force",),
    )
    ir = replace(
        build_hand_ir(),
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )

    result = SourceGraphConnectivityBackend(_entry(ir)).find_path(
        from_signal="sg_top.runtime_force",
        to_signal="sg_top.seed",
        compile_log="compile.log",
    )

    assert result["found"] is False
    assert result["unsupported_reason"] == "source_graph_query_inconclusive"
    assert result["_source_graph_query_receipt"]["status"] == "inconclusive"
    assert result["_source_graph_query_receipt"]["unresolved_boundary_codes"] == [
        "objective_exclusion"
    ]
