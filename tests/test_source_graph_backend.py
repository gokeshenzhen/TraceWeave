from __future__ import annotations

from dataclasses import replace
import hashlib

from src.connectivity_ir import CoverageStatus, ResolutionKind
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
    SourceGraphScopeReceipt,
)
from src.source_graph_runtime import SourceGraphCacheEntry
from tests.connectivity_ir_fixtures import build_hand_ir


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
            coverage_status=CoverageStatus.COMPLETE,
        ),
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
