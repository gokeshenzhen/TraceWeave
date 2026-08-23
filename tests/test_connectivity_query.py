from dataclasses import replace

import pytest

from src import cancellation
from src.connectivity_ir import (
    AssignmentFact,
    BindingSourceKind,
    BindingStyle,
    BitMapping,
    BitRange,
    BoundaryKind,
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    DependencyFact,
    EdgeKind,
    InstanceDecl,
    PackedMemberDecl,
    PortBinding,
    PortDecl,
    PortDirection,
    SignalDecl,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
    SymbolKind,
)
from src.connectivity_query import (
    ConnectivityQueryEngine,
    PathQueryStatus,
    QueryConfidence,
    QueryStatus,
    _longest_instance_prefix,
)
from scripts.benchmark_connectivity_query_indexes import build_wide_load_ir
from tests.connectivity_ir_fixtures import build_deep_ir, build_hand_ir


DEEP_OUTPUT = "uart_deep_x_tb.apb_prdata[7:0]"
DEEP_INPUT = "uart_deep_x_tb.inject_x"
DEEP_LEAF = (
    "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel."
    "u_rx_fifo.u_storage_bank.u_x_cell"
)


def test_packed_member_resolution_preserves_ascending_field_indices():
    ir = build_hand_ir()
    definitions = tuple(
        replace(
            definition,
            packed_members=(
                PackedMemberDecl(
                    name="lane_data.asc",
                    aggregate="lane_data",
                    packed_range=BitRange(0, 7),
                    aggregate_bits=tuple(range(15, 7, -1)),
                    location=definition.location,
                ),
            ),
        )
        if definition.name == "sg_top"
        else definition
        for definition in ir.definitions
    )
    engine = ConnectivityQueryEngine(replace(ir, definitions=definitions))

    resolved = engine.resolve_signal("sg_top.lane_data.asc[0:3]")

    assert resolved.symbol == "lane_data"
    assert resolved.bits == (15, 14, 13, 12)


def test_instance_prefix_lookup_probes_path_depth_not_design_size():
    class CountingPaths:
        def __init__(self):
            self.paths = {"top", *(f"top.u{index:05d}" for index in range(30_000))}
            self.probes: list[str] = []

        def __contains__(self, candidate: object) -> bool:
            assert isinstance(candidate, str)
            self.probes.append(candidate)
            return candidate in self.paths

    paths = CountingPaths()

    resolved = _longest_instance_prefix(
        "top.u12345.bus.payload.member",
        paths,
    )

    assert resolved == "top.u12345"
    assert paths.probes == [
        "top.u12345.bus.payload",
        "top.u12345.bus",
        "top.u12345",
    ]


def test_instance_prefix_lookup_preserves_trailing_separator_rejection():
    assert _longest_instance_prefix("top.", {"top"}) is None
    assert _longest_instance_prefix("top.u.", {"top", "top.u"}) == "top"


def test_instance_prefix_resolution_keeps_dotted_symbols_below_deep_instance():
    resolved = ConnectivityQueryEngine(build_hand_ir()).resolve_signal(
        "sg_top.u_bridge.gen_lanes[0].u_lane.u_named.data_i"
    )

    assert resolved.instance_path == (
        "sg_top.u_bridge.gen_lanes[0].u_lane.u_named"
    )
    assert resolved.symbol == "data_i"


def test_wide_load_query_preserves_ordered_bit_mapping():
    result = ConnectivityQueryEngine(build_wide_load_ir(4_096)).query_loads(
        "top.payload"
    )

    expected_bits = tuple(range(4_095, -1, -1))
    assert result.status is QueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.resolved_bits == expected_bits
    assert len(result.matches) == 1
    assert result.matches[0].source.bits == expected_bits
    assert result.matches[0].covered_signal.bits == expected_bits


def _build_scalar_path_ir(
    *edges: tuple[str, str, str, BoundaryKind],
    signals: tuple[str, ...] = (),
    coverage: CoverageReport | None = None,
) -> ConnectivityIR:
    names = sorted(
        {
            *signals,
            *(source for _, source, _, _ in edges),
            *(target for _, _, target, _ in edges),
        }
    )
    location = SourceLocation(file="tests/test_connectivity_query.py", line=1)
    assignments = []
    for line, (edge_id, source, target, boundary) in enumerate(edges, start=10):
        sequential = boundary is BoundaryKind.SEQUENTIAL
        assignments.append(
            AssignmentFact(
                assignment_id=edge_id,
                kind=(
                    EdgeKind.PROCEDURAL_ASSIGN
                    if sequential
                    else EdgeKind.CONTINUOUS_ASSIGN
                ),
                target=SignalSelection.template(target, BitRange.scalar()),
                dependencies=(
                    DependencyFact(
                        source=SignalSelection.template(source, BitRange.scalar()),
                        target=SignalSelection.template(target, BitRange.scalar()),
                    ),
                ),
                boundary=boundary,
                evidence=SourceEvidence(
                    construct="path_test_edge",
                    location=replace(location, line=line),
                    frontend="path_test",
                    frontend_version="1.0",
                ),
                procedure_kind="AlwaysFF" if sequential else None,
            )
        )
    definition = DefinitionTemplate(
        definition_id="path_top",
        name="path_top",
        kind=DefinitionKind.MODULE,
        location=location,
        signals=tuple(
            SignalDecl(name, SymbolKind.NET, BitRange.scalar(), location)
            for name in names
        ),
        assignments=tuple(assignments),
    )
    return ConnectivityIR(
        frontend_name="path_test",
        frontend_version="1.0",
        definitions=(definition,),
        instances=(InstanceDecl("path_top", "path_top", "path_top", None, location),),
        bindings=(),
        coverage=coverage
        or CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=1,
            files_projected=1,
        ),
        top_instances=("path_top",),
    )


def _build_segmented_binding_ir(
    *,
    unresolved_prefix: bool = False,
    duplicate_payload_driver: bool = False,
) -> ConnectivityIR:
    location = SourceLocation(file="tests/segmented_binding.sv", line=1)
    payload_bits = BitRange(23, 0)
    data_bits = BitRange(31, 0)
    assignments = [
        AssignmentFact(
            assignment_id="top:assign:payload",
            kind=EdgeKind.CONTINUOUS_ASSIGN,
            target=SignalSelection.template("payload", payload_bits),
            dependencies=(
                DependencyFact(
                    source=SignalSelection.template("seed", payload_bits),
                    target=SignalSelection.template("payload", payload_bits),
                ),
            ),
            boundary=BoundaryKind.COMBINATIONAL,
            evidence=SourceEvidence(
                construct="continuous_assignment",
                location=replace(location, line=5),
                frontend="segmented_test",
            ),
        )
    ]
    if duplicate_payload_driver:
        assignments.append(
            replace(
                assignments[0],
                assignment_id="top:assign:payload_duplicate",
                evidence=replace(
                    assignments[0].evidence,
                    location=replace(location, line=6),
                ),
            )
        )
    top_definition = DefinitionTemplate(
        definition_id="top",
        name="top",
        kind=DefinitionKind.MODULE,
        location=location,
        signals=(
            SignalDecl("seed", SymbolKind.NET, payload_bits, location),
            SignalDecl("payload", SymbolKind.NET, payload_bits, location),
        ),
        assignments=tuple(assignments),
    )
    leaf_definition = DefinitionTemplate(
        definition_id="leaf",
        name="leaf",
        kind=DefinitionKind.MODULE,
        location=replace(location, line=10),
        ports=(
            PortDecl(
                "data_i",
                PortDirection.INPUT,
                data_bits,
                0,
                replace(location, line=11),
            ),
        ),
    )
    target_prefix = SignalSelection("data_i", tuple(range(31, 23, -1)), "top.u")
    prefix = BitMapping(
        source=None,
        target=target_prefix,
        source_kind=(
            BindingSourceKind.UNRESOLVED
            if unresolved_prefix
            else BindingSourceKind.CONSTANT
        ),
        constant_bits=() if unresolved_prefix else ("0",) * 8,
        unresolved_reason="dynamic_prefix" if unresolved_prefix else None,
    )
    binding = PortBinding(
        binding_id="top.u:0:data_i",
        instance_path="top.u",
        port_name="data_i",
        direction=PortDirection.INPUT,
        style=BindingStyle.NAMED,
        mappings=(
            prefix,
            BitMapping(
                source=SignalSelection("payload", payload_bits.indices, "top"),
                target=SignalSelection("data_i", payload_bits.indices, "top.u"),
            ),
        ),
        evidence=SourceEvidence(
            construct="named_port_binding",
            location=replace(location, line=20),
            frontend="segmented_test",
        ),
    )
    return ConnectivityIR(
        frontend_name="segmented_test",
        frontend_version="1",
        definitions=(top_definition, leaf_definition),
        instances=(
            InstanceDecl("top", "top", "top", None, location),
            InstanceDecl("top.u", "u", "leaf", "top", replace(location, line=20)),
        ),
        bindings=(binding,),
        coverage=CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=1,
            files_projected=1,
        ),
        top_instances=("top",),
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


def test_segmented_driver_resolves_constant_and_dynamic_bits_independently():
    engine = ConnectivityQueryEngine(_build_segmented_binding_ir())

    full = engine.query_driver("top.u.data_i[31:0]")
    crossing = engine.query_driver("top.u.data_i[27:20]")

    assert full.status is QueryStatus.FOUND
    assert full.coverage_status is CoverageStatus.COMPLETE
    assert full.resolved_bits == tuple(range(31, -1, -1))
    assert full.unresolved_bits == ()
    assert full.constant_bits == tuple(range(31, 23, -1))
    assert {match.kind for match in full.matches} == {
        EdgeKind.CONSTANT_DRIVER,
        EdgeKind.CONTINUOUS_ASSIGN,
    }
    constant = next(
        match for match in full.matches if match.kind is EdgeKind.CONSTANT_DRIVER
    )
    dynamic = next(
        match for match in full.matches if match.kind is EdgeKind.CONTINUOUS_ASSIGN
    )
    assert constant.covered_signal.bits == tuple(range(31, 23, -1))
    assert constant.constant_bits == ("0",) * 8
    assert dynamic.covered_signal.bits == tuple(range(23, -1, -1))

    assert crossing.resolved_bits == tuple(range(27, 19, -1))
    assert crossing.constant_bits == (27, 26, 25, 24)
    assert [match.covered_signal.bits for match in crossing.matches] == [
        (23, 22, 21, 20),
        (27, 26, 25, 24),
    ]


def test_segmented_driver_keeps_unresolved_and_multi_driver_bits_honest():
    partial = ConnectivityQueryEngine(
        _build_segmented_binding_ir(unresolved_prefix=True)
    ).query_driver("top.u.data_i")
    multiple = ConnectivityQueryEngine(
        _build_segmented_binding_ir(duplicate_payload_driver=True)
    ).query_driver("top.u.data_i")

    assert partial.status is QueryStatus.FOUND
    assert partial.coverage_status is CoverageStatus.INCONCLUSIVE
    assert partial.resolved_bits == tuple(range(23, -1, -1))
    assert partial.unresolved_bits == tuple(range(31, 23, -1))
    assert {gap.code for gap in partial.unresolved_boundaries} == {
        "driver_bits_unresolved",
        "port_segment_unresolved",
    }

    assert multiple.unresolved_bits == ()
    assert multiple.multi_driver_bits == tuple(range(23, -1, -1))
    assert (
        len(
            [
                match
                for match in multiple.matches
                if match.kind is EdgeKind.CONTINUOUS_ASSIGN
            ]
        )
        == 2
    )


def test_dynamic_query_frontier_excludes_terminal_constant_segment():
    ir = _build_segmented_binding_ir()
    top = ir.definitions[0]
    without_payload_driver = replace(top, assignments=())
    result = ConnectivityQueryEngine(
        replace(ir, definitions=(without_payload_driver, *ir.definitions[1:]))
    ).query_driver("top.u.data_i")

    assert result.constant_bits == tuple(range(31, 23, -1))
    assert result.unresolved_bits == tuple(range(23, -1, -1))
    assert [
        (item.signal.path(), item.query_target.bits) for item in result.frontiers
    ] == [("top.payload", tuple(range(23, -1, -1)))]


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
    assert result.matches[0].positive_fact_confidence is QueryConfidence.EXACT_SOURCE


def test_path_same_signal_and_overlapping_slice_are_alias_equivalent():
    engine = ConnectivityQueryEngine(build_hand_ir())

    same = engine.query_path("sg_top.seed", "sg_top.seed")
    overlapping = engine.query_path("sg_top.seed", "sg_top.seed[7:0]")

    for result in (same, overlapping):
        assert result.status is PathQueryStatus.FOUND
        assert result.coverage_status is CoverageStatus.COMPLETE
        assert result.endpoint_alias_equivalent is True
        assert result.path == ()


@pytest.mark.parametrize(
    ("from_signal", "to_signal", "expected_style"),
    [
        ("sg_top.clk", "sg_top.u_producer.clk", "named"),
        ("sg_top.clk", "sg_top.u_bridge.clk", "positional"),
        (
            "sg_top.u_producer.bus.data[15:8]",
            "sg_top.bus.data[15:8]",
            "modport",
        ),
    ],
)
def test_path_direct_named_positional_and_modport_bindings(
    from_signal, to_signal, expected_style
):
    result = ConnectivityQueryEngine(build_hand_ir()).query_path(from_signal, to_signal)

    assert result.status is PathQueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert len(result.path) == 1
    assert result.path[0].binding_style.value == expected_style
    assert result.path[0].evidence.location.file.endswith("hand_connectivity.sv")


def test_path_interface_binding_style_is_structurally_traversable():
    ir = build_hand_ir()
    bindings = tuple(
        replace(binding, style=BindingStyle.INTERFACE, modport=None)
        if binding.binding_id == "sg_top.u_producer:bus.data"
        else binding
        for binding in ir.bindings
    )

    result = ConnectivityQueryEngine(replace(ir, bindings=bindings)).query_path(
        "sg_top.u_producer.bus.data[15:8]",
        "sg_top.bus.data[15:8]",
    )

    assert result.status is PathQueryStatus.FOUND
    assert result.path[0].edge_kind is EdgeKind.INTERFACE_BIND
    assert result.path[0].binding_style is BindingStyle.INTERFACE


def test_path_multi_hop_preserves_interface_and_exact_slice_concat_mappings():
    result = ConnectivityQueryEngine(build_hand_ir()).query_path(
        "sg_top.u_producer.seed[7:0]",
        "sg_top.u_bridge.gen_lanes[1].u_lane.u_named.data_i",
    )

    assert result.status is PathQueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert [edge.edge_kind for edge in result.path] == [
        EdgeKind.PROCEDURAL_ASSIGN,
        EdgeKind.INTERFACE_BIND,
        EdgeKind.INTERFACE_BIND,
        EdgeKind.PORT_BIND_INPUT,
        EdgeKind.PORT_BIND_INPUT,
    ]
    assert [
        edge.binding_style.value if edge.binding_style else None for edge in result.path
    ] == [
        None,
        "modport",
        "modport",
        "positional",
        "named",
    ]
    assert all(edge.exact_bit_mapping for edge in result.path)
    assert result.path[0].source.bits == tuple(range(7, -1, -1))
    assert result.path[0].target.bits == tuple(range(15, 7, -1))
    assert result.path[-1].target.bits == tuple(range(7, -1, -1))


def test_path_continuous_assignment_has_real_source_evidence():
    result = ConnectivityQueryEngine(build_hand_ir()).query_path(
        "sg_top.u_bridge.gen_lanes[0].u_lane.comb_y[3:0]",
        "sg_top.u_bridge.gen_lanes[0].u_lane.lane_o[7:4]",
    )

    assert result.status is PathQueryStatus.FOUND
    assert [edge.edge_kind for edge in result.path] == [EdgeKind.CONTINUOUS_ASSIGN]
    assert result.path[0].edge_id == "sg_lane:assign:50:lane_o"
    assert result.path[0].evidence.location.line == 50


def test_path_inexact_bit_dependency_is_found_with_partial_coverage():
    result = ConnectivityQueryEngine(build_hand_ir()).query_path(
        "sg_top.u_bridge.lane_data",
        "sg_top.u_bridge.bus.ready",
    )

    assert result.status is PathQueryStatus.FOUND
    assert result.coverage_status is CoverageStatus.PARTIAL
    assert len(result.path) == 1
    assert result.path[0].exact_bit_mapping is False
    assert [gap.code for gap in result.unresolved_boundaries] == [
        "path_bit_mapping_inexact"
    ]


def test_path_expand_assigns_is_presentation_only_and_preserves_connectivity():
    engine = ConnectivityQueryEngine(build_hand_ir())
    args = (
        "sg_top.u_producer.seed[7:0]",
        "sg_top.u_bridge.gen_lanes[1].u_lane.u_named.data_i",
    )

    folded = engine.query_path(*args, expand_assigns=False)
    expanded = engine.query_path(*args, expand_assigns=True)

    assert folded.status is expanded.status is PathQueryStatus.FOUND
    assert folded.path == expanded.path
    assert folded.expand_assigns is False
    assert expanded.expand_assigns is True
    assert any(edge.edge_kind is EdgeKind.PROCEDURAL_ASSIGN for edge in expanded.path)


def test_path_shortest_hop_tie_break_and_serialization_are_deterministic():
    ir = _build_scalar_path_ir(
        ("edge-c-d", "c", "d", BoundaryKind.COMBINATIONAL),
        ("edge-a-c", "a", "c", BoundaryKind.COMBINATIONAL),
        ("edge-b-d", "b", "d", BoundaryKind.COMBINATIONAL),
        ("edge-a-b", "a", "b", BoundaryKind.COMBINATIONAL),
    )
    engine = ConnectivityQueryEngine(ir)

    first = engine.query_path("path_top.a", "path_top.d")
    second = engine.query_path("path_top.a", "path_top.d")

    assert [edge.edge_id for edge in first.path] == ["edge-a-b", "edge-b-d"]
    assert first.to_dict() == second.to_dict()


def test_path_cycle_detection_terminates_with_complete_not_connected():
    result = ConnectivityQueryEngine(
        _build_scalar_path_ir(
            ("edge-a-b", "a", "b", BoundaryKind.COMBINATIONAL),
            ("edge-b-a", "b", "a", BoundaryKind.COMBINATIONAL),
            signals=("d",),
        )
    ).query_path("path_top.a", "path_top.d")

    assert result.status is PathQueryStatus.NOT_CONNECTED
    assert result.coverage_status is CoverageStatus.COMPLETE
    assert result.visited_state_count == 2


@pytest.mark.parametrize(
    ("from_signal", "to_signal", "status", "gap_codes"),
    [
        (
            "path_top.missing",
            "path_top.b",
            PathQueryStatus.FROM_UNRESOLVED,
            ["path_from_endpoint_unresolved"],
        ),
        (
            "path_top.a",
            "path_top.missing",
            PathQueryStatus.TO_UNRESOLVED,
            ["path_to_endpoint_unresolved"],
        ),
        (
            "path_top.left_missing",
            "path_top.right_missing",
            PathQueryStatus.ENDPOINTS_UNRESOLVED,
            ["path_from_endpoint_unresolved", "path_to_endpoint_unresolved"],
        ),
    ],
)
def test_path_endpoint_resolution_statuses_are_distinct(
    from_signal, to_signal, status, gap_codes
):
    result = ConnectivityQueryEngine(
        _build_scalar_path_ir(signals=("a", "b"))
    ).query_path(from_signal, to_signal)

    assert result.status is status
    assert result.coverage_status is CoverageStatus.INCONCLUSIVE
    assert [gap.code for gap in result.unresolved_boundaries] == gap_codes


def test_path_complete_negative_and_partial_no_path_are_not_confused():
    complete_ir = _build_scalar_path_ir(signals=("a", "d"))
    gap = CoverageGap(
        code="objective_exclusion",
        message="unsupported construct can affect the source endpoint",
        impact=CoverageStatus.PARTIAL,
        constructs=("dpi",),
        scopes=("path_top.a",),
    )
    partial_ir = replace(
        complete_ir,
        coverage=CoverageReport(
            status=CoverageStatus.PARTIAL,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
            diagnostic_count=1,
            blocking_diagnostic_count=1,
        ),
    )

    complete = ConnectivityQueryEngine(complete_ir).query_path(
        "path_top.a", "path_top.d"
    )
    partial = ConnectivityQueryEngine(partial_ir).query_path("path_top.a", "path_top.d")

    assert complete.status is PathQueryStatus.NOT_CONNECTED
    assert complete.coverage_status is CoverageStatus.COMPLETE
    assert partial.status is PathQueryStatus.INCONCLUSIVE
    assert partial.coverage_status is CoverageStatus.PARTIAL
    assert [item.code for item in partial.unresolved_boundaries] == [
        "objective_exclusion"
    ]


def test_path_sequential_boundary_is_inconclusive_and_not_traversed():
    result = ConnectivityQueryEngine(
        _build_scalar_path_ir(
            ("edge-a-b", "a", "b", BoundaryKind.SEQUENTIAL),
        )
    ).query_path("path_top.a", "path_top.b")

    assert result.status is PathQueryStatus.INCONCLUSIVE
    assert result.coverage_status is CoverageStatus.INCONCLUSIVE
    assert result.path == ()
    assert [gap.code for gap in result.unresolved_boundaries] == ["sequential_boundary"]


def test_path_traversal_and_output_caps_are_loud():
    engine = ConnectivityQueryEngine(
        _build_scalar_path_ir(
            ("edge-a-b", "a", "b", BoundaryKind.COMBINATIONAL),
            ("edge-b-c", "b", "c", BoundaryKind.COMBINATIONAL),
        )
    )

    traversal = engine.query_path("path_top.a", "path_top.c", traversal_limit=1)
    output = engine.query_path("path_top.a", "path_top.c", output_limit=1)

    assert traversal.status is PathQueryStatus.TRUNCATED
    assert traversal.traversal_truncated is True
    assert [gap.code for gap in traversal.unresolved_boundaries] == [
        "path_traversal_limit"
    ]
    assert output.status is PathQueryStatus.TRUNCATED
    assert output.output_truncated is True
    assert output.path == ()
    assert [gap.code for gap in output.unresolved_boundaries] == ["path_output_limit"]


def test_path_checks_cancellation_during_graph_traversal(monkeypatch):
    calls = 0

    def cancel_during_walk():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise cancellation.OperationCancelled("cancelled in path walk")

    monkeypatch.setattr("src.connectivity_query.check_cancelled", cancel_during_walk)
    engine = ConnectivityQueryEngine(
        _build_scalar_path_ir(
            ("edge-a-b", "a", "b", BoundaryKind.COMBINATIONAL),
            ("edge-b-c", "b", "c", BoundaryKind.COMBINATIONAL),
            ("edge-c-d", "c", "d", BoundaryKind.COMBINATIONAL),
        )
    )

    with pytest.raises(cancellation.OperationCancelled):
        engine.query_path("path_top.a", "path_top.d")
    assert calls == 4


def _build_high_fanout_ir(count: int) -> ConnectivityIR:
    return _build_scalar_path_ir(
        *(
            (f"fanout-{index:05d}", "source", "sink", BoundaryKind.COMBINATIONAL)
            for index in range(count)
        )
    )


def test_load_query_default_match_cap_is_stable_and_inconclusive():
    ir = _build_high_fanout_ir(300)
    reordered = replace(
        ir,
        definitions=(
            replace(
                ir.definitions[0],
                assignments=tuple(reversed(ir.definitions[0].assignments)),
            ),
        ),
    )

    first = ConnectivityQueryEngine(ir).query_loads("path_top.source")
    second = ConnectivityQueryEngine(reordered).query_loads("path_top.source")

    assert first.status is QueryStatus.FOUND
    assert first.coverage_status is CoverageStatus.INCONCLUSIVE
    assert len(first.matches) == 256
    assert [match.fact_id for match in first.matches] == [
        match.fact_id for match in second.matches
    ]
    assert first.matches[0].fact_id == "fanout-00000"
    assert first.matches[-1].fact_id == "fanout-00255"
    assert first.visited_state_count == 1
    assert first.inspected_edge_count == 257
    assert first.match_limit == 256
    assert first.match_truncated is True
    assert first.truncated is True
    assert [gap.code for gap in first.unresolved_boundaries] == [
        "query_match_limit"
    ]
    assert all(
        match.positive_fact_confidence is QueryConfidence.EXACT_SOURCE
        for match in first.matches
    )
    assert all(match.confidence is QueryConfidence.PARTIAL for match in first.matches)


def test_driver_and_load_edge_limit_never_produces_complete_enumeration():
    engine = ConnectivityQueryEngine(_build_high_fanout_ir(8))

    driver = engine.query_driver(
        "path_top.sink",
        edge_limit=3,
        match_limit=8,
    )
    loads = engine.query_loads(
        "path_top.source",
        edge_limit=3,
        match_limit=8,
    )

    for result in (driver, loads):
        assert result.status is QueryStatus.FOUND
        assert result.coverage_status is CoverageStatus.INCONCLUSIVE
        assert len(result.matches) == 3
        assert result.inspected_edge_count == 3
        assert result.edge_truncated is True
        assert result.match_truncated is False
        assert [gap.code for gap in result.unresolved_boundaries] == [
            "query_edge_limit"
        ]


def test_driver_and_load_state_limit_is_loud():
    engine = ConnectivityQueryEngine(build_deep_ir())

    driver = engine.query_driver(DEEP_OUTPUT, state_limit=2)
    loads = engine.query_loads(DEEP_INPUT, state_limit=2)

    for result in (driver, loads):
        assert result.status is QueryStatus.INCONCLUSIVE
        assert result.coverage_status is CoverageStatus.INCONCLUSIVE
        assert result.matches == ()
        assert result.visited_state_count == 2
        assert result.state_truncated is True
        assert [gap.code for gap in result.unresolved_boundaries] == [
            "query_state_limit"
        ]


@pytest.mark.parametrize(
    ("operation", "signal_path"),
    (("driver", "path_top.sink"), ("loads", "path_top.source")),
)
def test_driver_and_load_queries_check_cancellation_during_walk(
    monkeypatch, operation, signal_path
):
    calls = 0

    def cancel_during_walk():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise cancellation.OperationCancelled("cancelled in connectivity walk")

    monkeypatch.setattr("src.connectivity_query.check_cancelled", cancel_during_walk)
    engine = ConnectivityQueryEngine(_build_high_fanout_ir(8))

    with pytest.raises(cancellation.OperationCancelled):
        getattr(engine, f"query_{operation}")(signal_path)
    assert calls == 4


@pytest.mark.parametrize(
    "limit_name",
    ("state_limit", "edge_limit", "match_limit", "frontier_limit"),
)
@pytest.mark.parametrize("invalid", (0, -1, True))
def test_driver_and_load_query_limits_require_positive_integers(
    limit_name, invalid
):
    engine = ConnectivityQueryEngine(_build_high_fanout_ir(1))

    for operation, signal_path in (
        (engine.query_driver, "path_top.sink"),
        (engine.query_loads, "path_top.source"),
    ):
        with pytest.raises(ValueError, match=limit_name):
            operation(signal_path, **{limit_name: invalid})
