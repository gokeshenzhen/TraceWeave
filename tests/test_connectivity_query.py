from dataclasses import replace

from src.connectivity_ir import (
    BoundaryKind,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    EdgeKind,
)
from src.connectivity_query import (
    ConnectivityQueryEngine,
    QueryConfidence,
    QueryStatus,
)
from tests.connectivity_ir_fixtures import build_deep_ir, build_hand_ir


DEEP_OUTPUT = "uart_deep_x_tb.apb_prdata[7:0]"
DEEP_INPUT = "uart_deep_x_tb.inject_x"
DEEP_LEAF = (
    "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel."
    "u_rx_fifo.u_storage_bank.u_x_cell"
)


def test_deep_driver_crosses_seven_positional_bindings_to_always_ff():
    result = ConnectivityQueryEngine(build_deep_ir()).query_driver(DEEP_OUTPUT)

    assert result.status is QueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.traversed_binding_edges == 7
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.instance_path == DEEP_LEAF
    assert match.procedure_kind == "AlwaysFF"
    assert match.boundary is BoundaryKind.SEQUENTIAL
    assert match.evidence.location.file.endswith("deep_uart_x.sv")
    assert match.evidence.location.line == 20
    assert match.confidence is QueryConfidence.CONDITIONAL
    assert len(match.traversal) == 7
    assert all(hop.binding_style.value == "positional" for hop in match.traversal)


def test_deep_load_crosses_seven_positional_bindings_to_leaf_control_use():
    result = ConnectivityQueryEngine(build_deep_ir()).query_loads(DEEP_INPUT)

    assert result.status is QueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.traversed_binding_edges == 7
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.instance_path == DEEP_LEAF
    assert match.fact_id.endswith(":data_q")
    assert match.dependency_role == "control"
    assert match.boundary is BoundaryKind.SEQUENTIAL


def test_deep_depth_limit_is_inconclusive_not_false_not_connected():
    result = ConnectivityQueryEngine(build_deep_ir()).query_driver(
        DEEP_OUTPUT,
        max_depth=6,
    )

    assert result.status is QueryStatus.INCONCLUSIVE
    assert result.coverage_status is CoverageStatus.INCONCLUSIVE
    assert result.matches == ()
    assert [gap.code for gap in result.unresolved_boundaries] == ["query_depth_limit"]


def test_hand_interface_driver_resolves_producer_always_comb_and_concat_map():
    result = ConnectivityQueryEngine(build_hand_ir()).query_driver(
        "sg_top.bus.data[15:8]"
    )

    assert result.status is QueryStatus.FOUND
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.instance_path == "sg_top.u_producer"
    assert match.kind is EdgeKind.PROCEDURAL_ASSIGN
    assert match.procedure_kind == "AlwaysComb"
    assert match.evidence.location.line == 84
    assert match.confidence is QueryConfidence.EXACT_SOURCE
    assert len(match.traversal) == 1
    assert match.traversal[0].binding_style.value == "modport"
    assert [dependency.source.bits for dependency in match.dependencies] == [
        tuple(range(7, -1, -1))
    ]
    assert match.dependencies[0].target.bits == tuple(range(15, 7, -1))


def test_hand_slice_load_reaches_only_matching_generate_lane_and_leaf_reads():
    result = ConnectivityQueryEngine(build_hand_ir()).query_loads(
        "sg_top.bus.data[7:0]"
    )

    assert result.status is QueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert {match.instance_path for match in result.matches} == {
        "sg_top.u_bridge.gen_lanes[0].u_lane.u_named"
    }
    assert {match.procedure_kind for match in result.matches} == {
        "AlwaysComb",
        "AlwaysFF",
    }
    assert all("gen_lanes[1]" not in match.instance_path for match in result.matches)
    assert all(len(match.traversal) == 3 for match in result.matches)


def test_hand_generated_output_slice_driver_selects_lane_one_continuous_assign():
    result = ConnectivityQueryEngine(build_hand_ir()).query_driver(
        "sg_top.lane_data[15:8]"
    )

    assert result.status is QueryStatus.FOUND
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.instance_path == "sg_top.u_bridge.gen_lanes[1].u_lane"
    assert match.kind is EdgeKind.CONTINUOUS_ASSIGN
    assert match.evidence.location.line == 50
    assert match.generate_scope is None


def test_hand_modport_directions_separate_ready_driver_and_consumer():
    engine = ConnectivityQueryEngine(build_hand_ir())

    driver = engine.query_driver("sg_top.bus.ready")
    loads = engine.query_loads("sg_top.bus.ready")

    assert [match.instance_path for match in driver.matches] == ["sg_top.u_bridge"]
    assert driver.matches[0].evidence.location.line == 59
    assert [match.instance_path for match in loads.matches] == ["sg_top.u_producer"]
    assert loads.matches[0].dependency_role == "control"
    assert loads.matches[0].evidence.location.line == 77


def test_complete_negative_is_not_connected():
    result = ConnectivityQueryEngine(build_hand_ir()).query_driver(
        "sg_top.runtime_force"
    )

    assert result.status is QueryStatus.NOT_CONNECTED
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.matches == ()


def test_unsupported_region_negative_is_inconclusive_and_never_exact():
    ir = build_hand_ir()
    gap = CoverageGap(
        code="runtime_force",
        message="runtime force cannot be projected from static source",
        impact=CoverageStatus.INCONCLUSIVE,
        constructs=("force",),
        scopes=("sg_top.runtime_force",),
    )
    partial = replace(
        ir,
        coverage=CoverageReport(
            status=CoverageStatus.PARTIAL,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )

    unsupported = ConnectivityQueryEngine(partial).query_driver("sg_top.runtime_force")
    supported = ConnectivityQueryEngine(partial).query_driver("sg_top.bus.valid")

    assert unsupported.status is QueryStatus.INCONCLUSIVE
    assert unsupported.coverage_status is CoverageStatus.INCONCLUSIVE
    assert unsupported.matches == ()
    assert [item.code for item in unsupported.unresolved_boundaries] == [
        "runtime_force"
    ]
    assert supported.status is QueryStatus.FOUND
    assert supported.coverage_status is CoverageStatus.COMPLETE
    assert supported.matches[0].confidence is QueryConfidence.EXACT_SOURCE


def test_global_partial_gap_downgrades_positive_match_confidence():
    ir = build_hand_ir()
    partial = replace(
        ir,
        coverage=CoverageReport(
            status=CoverageStatus.PARTIAL,
            files_total=1,
            files_projected=1,
            gaps=(
                CoverageGap(
                    code="protected_payload",
                    message="protected payload is unreadable",
                    impact=CoverageStatus.PARTIAL,
                    constructs=("protected",),
                    scopes=("*",),
                ),
            ),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )

    result = ConnectivityQueryEngine(partial).query_driver("sg_top.bus.valid")

    assert result.status is QueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.PARTIAL
    assert result.matches[0].confidence is QueryConfidence.PARTIAL
