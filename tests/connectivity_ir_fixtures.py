"""Hand-authored Phase 1A Connectivity IR fixtures used by pure unit tests.

These builders intentionally consume no parser, optional frontend, waveform,
NPI, license, compile artifact, or untracked checkout.  Their source anchors
refer to the tracked RTL fixtures that the optional Slang integration projects.
"""

from __future__ import annotations

from src.connectivity_ir import (
    AssignmentFact,
    BindingStyle,
    BitMapping,
    BitRange,
    BoundaryKind,
    ConnectivityIR,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    DependencyFact,
    DependencyRole,
    EdgeKind,
    InstanceDecl,
    ModportDecl,
    ModportMember,
    PortBinding,
    PortDecl,
    PortDirection,
    ResolutionKind,
    SignalDecl,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
    SymbolKind,
    selections_for_concat,
)


DEEP_RTL = "tests/fixtures/deep_x_npi/rtl/deep_uart_x.sv"
DEEP_TB = "tests/fixtures/deep_x_npi/tb/deep_x_tb.sv"
HAND_RTL = "tests/fixtures/source_graph_frontend/hand_connectivity.sv"


def _loc(path: str, line: int) -> SourceLocation:
    return SourceLocation(file=path, line=line)


def _evidence(
    path: str,
    line: int,
    construct: str,
    *,
    conditional: bool = False,
) -> SourceEvidence:
    return SourceEvidence(
        construct=construct,
        location=_loc(path, line),
        resolution=(
            ResolutionKind.CONDITIONAL if conditional else ResolutionKind.EXACT_SOURCE
        ),
        frontend="hand_oracle",
        frontend_version="1.0",
    )


def _plain_port(
    name: str,
    direction: PortDirection,
    width: int,
    ordinal: int,
    path: str,
    line: int,
) -> PortDecl:
    return PortDecl(
        name=name,
        direction=direction,
        packed_range=BitRange.from_width(width),
        ordinal=ordinal,
        location=_loc(path, line),
    )


def _signal(name: str, width: int, path: str, line: int) -> SignalDecl:
    return SignalDecl(
        name=name,
        kind=SymbolKind.VARIABLE,
        packed_range=BitRange.from_width(width),
        location=_loc(path, line),
    )


def _select(
    symbol: str,
    width: int,
    *,
    instance: str | None = None,
) -> SignalSelection:
    return SignalSelection(
        symbol=symbol,
        bits=BitRange.from_width(width).indices,
        instance_path=instance,
    )


def _slice(
    symbol: str,
    left: int,
    right: int,
    *,
    instance: str | None = None,
) -> SignalSelection:
    return SignalSelection(
        symbol=symbol,
        bits=BitRange(left, right).indices,
        instance_path=instance,
    )


def _instance_binding(
    *,
    binding_id: str,
    child: str,
    port: PortDecl,
    actual_instance: str,
    actual_symbol: str,
    path: str,
    line: int,
    style: BindingStyle,
    actual_bits: tuple[int, ...] | None = None,
) -> PortBinding:
    actual = SignalSelection(
        instance_path=actual_instance,
        symbol=actual_symbol,
        bits=actual_bits or port.packed_range.indices,
    )
    formal = SignalSelection(
        instance_path=child,
        symbol=port.name,
        bits=port.packed_range.indices,
    )
    return PortBinding(
        binding_id=binding_id,
        instance_path=child,
        port_name=port.name,
        direction=port.direction,
        style=style,
        mappings=(BitMapping(source=actual, target=formal),),
        evidence=_evidence(path, line, f"{style.value}_port_binding"),
        port_position=port.ordinal if style is BindingStyle.POSITIONAL else None,
    )


def build_deep_ir() -> ConnectivityIR:
    scalar = BitRange.scalar()
    byte = BitRange(7, 0)
    wrapper_specs = (
        ("uart_apb_bridge_deep", 81, ("pclk", "presetn", "inject_x", "bridge_prdata")),
        ("uart_16550_deep", 71, ("clk", "rst_n", "inject_x", "apb_prdata")),
        ("uart_register_file_deep", 61, ("clk", "rst_n", "inject_x", "prdata")),
        ("uart_rx_channel", 51, ("clk", "rst_n", "inject_x", "rx_data")),
        ("uart_rx_fifo", 41, ("clk", "rst_n", "inject_x", "fifo_data")),
        ("uart_fifo_storage_bank", 31, ("clk", "rst_n", "inject_x", "bank_data")),
    )
    definitions: list[DefinitionTemplate] = []
    for name, line, names in wrapper_specs:
        definitions.append(
            DefinitionTemplate(
                definition_id=name,
                name=name,
                kind=DefinitionKind.MODULE,
                location=_loc(DEEP_RTL, line),
                ports=(
                    _plain_port(
                        names[0], PortDirection.INPUT, 1, 0, DEEP_RTL, line + 1
                    ),
                    _plain_port(
                        names[1], PortDirection.INPUT, 1, 1, DEEP_RTL, line + 2
                    ),
                    _plain_port(
                        names[2], PortDirection.INPUT, 1, 2, DEEP_RTL, line + 3
                    ),
                    _plain_port(
                        names[3], PortDirection.OUTPUT, 8, 3, DEEP_RTL, line + 4
                    ),
                ),
            )
        )
    leaf_ports = (
        _plain_port("clk", PortDirection.INPUT, 1, 0, DEEP_RTL, 15),
        _plain_port("rst_n", PortDirection.INPUT, 1, 1, DEEP_RTL, 16),
        _plain_port("inject_x", PortDirection.INPUT, 1, 2, DEEP_RTL, 17),
        _plain_port("data_q", PortDirection.OUTPUT, 8, 3, DEEP_RTL, 18),
    )
    leaf_write = AssignmentFact(
        assignment_id="uart_x_storage_cell:always_ff:20:data_q",
        kind=EdgeKind.PROCEDURAL_ASSIGN,
        target=SignalSelection.template("data_q", byte),
        dependencies=(
            DependencyFact(
                source=SignalSelection.template("inject_x", scalar),
                target=SignalSelection.template("data_q", byte),
                role=DependencyRole.CONTROL,
                exact_bit_mapping=False,
                guard="inject_x",
            ),
            DependencyFact(
                source=SignalSelection.template("rst_n", scalar),
                target=SignalSelection.template("data_q", byte),
                role=DependencyRole.CONTROL,
                exact_bit_mapping=False,
                guard="!rst_n",
            ),
            DependencyFact(
                source=SignalSelection.template("clk", scalar),
                target=SignalSelection.template("data_q", byte),
                role=DependencyRole.CONTROL,
                exact_bit_mapping=False,
                guard="posedge clk",
            ),
        ),
        boundary=BoundaryKind.SEQUENTIAL,
        evidence=_evidence(DEEP_RTL, 20, "always_ff", conditional=True),
        procedure_kind="AlwaysFF",
        guard="!rst_n || inject_x || default",
    )
    definitions.append(
        DefinitionTemplate(
            definition_id="uart_x_storage_cell",
            name="uart_x_storage_cell",
            kind=DefinitionKind.MODULE,
            location=_loc(DEEP_RTL, 14),
            ports=leaf_ports,
            assignments=(leaf_write,),
        )
    )
    definitions.append(
        DefinitionTemplate(
            definition_id="uart_deep_x_tb",
            name="uart_deep_x_tb",
            kind=DefinitionKind.MODULE,
            location=_loc(DEEP_TB, 3),
            signals=(
                _signal("pclk", 1, DEEP_TB, 4),
                _signal("presetn", 1, DEEP_TB, 5),
                _signal("inject_x", 1, DEEP_TB, 6),
                _signal("apb_prdata", 8, DEEP_TB, 7),
            ),
        )
    )

    instance_specs = (
        ("uart_deep_x_tb", "uart_deep_x_tb", None, DEEP_TB, 3, None),
        (
            "uart_deep_x_tb.u_apb_bridge",
            "uart_apb_bridge_deep",
            "uart_deep_x_tb",
            DEEP_TB,
            11,
            None,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart",
            "uart_16550_deep",
            "uart_deep_x_tb.u_apb_bridge",
            DEEP_RTL,
            87,
            None,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control",
            "uart_register_file_deep",
            "uart_deep_x_tb.u_apb_bridge.u_uart",
            DEEP_RTL,
            77,
            None,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel",
            "uart_rx_channel",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control",
            DEEP_RTL,
            67,
            None,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo",
            "uart_rx_fifo",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel",
            DEEP_RTL,
            57,
            None,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank",
            "uart_fifo_storage_bank",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo",
            DEEP_RTL,
            47,
            None,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank.u_x_cell",
            "uart_x_storage_cell",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank",
            DEEP_RTL,
            37,
            None,
        ),
    )
    instances = tuple(
        InstanceDecl(
            path=path,
            name=path.rsplit(".", 1)[-1],
            definition_id=definition,
            parent_path=parent,
            location=_loc(source, line),
            generate_scope=generate,
        )
        for path, definition, parent, source, line, generate in instance_specs
    )

    chain = (
        (
            "uart_deep_x_tb.u_apb_bridge",
            "uart_deep_x_tb",
            ("pclk", "presetn", "inject_x", "apb_prdata"),
            11,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart",
            "uart_deep_x_tb.u_apb_bridge",
            ("pclk", "presetn", "inject_x", "bridge_prdata"),
            87,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control",
            "uart_deep_x_tb.u_apb_bridge.u_uart",
            ("clk", "rst_n", "inject_x", "apb_prdata"),
            77,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control",
            ("clk", "rst_n", "inject_x", "prdata"),
            67,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel",
            ("clk", "rst_n", "inject_x", "rx_data"),
            57,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo",
            ("clk", "rst_n", "inject_x", "fifo_data"),
            47,
        ),
        (
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank.u_x_cell",
            "uart_deep_x_tb.u_apb_bridge.u_uart.u_control.u_rx_channel.u_rx_fifo.u_storage_bank",
            ("clk", "rst_n", "inject_x", "bank_data"),
            37,
        ),
    )
    definition_index = {item.definition_id: item for item in definitions}
    bindings: list[PortBinding] = []
    for child, parent, actual_names, line in chain:
        child_definition = definition_index[
            next(
                instance.definition_id
                for instance in instances
                if instance.path == child
            )
        ]
        source_path = DEEP_TB if line == 11 else DEEP_RTL
        for port, actual_name in zip(child_definition.ports, actual_names):
            bindings.append(
                _instance_binding(
                    binding_id=f"{child}:{port.ordinal}:{port.name}",
                    child=child,
                    port=port,
                    actual_instance=parent,
                    actual_symbol=actual_name,
                    path=source_path,
                    line=line,
                    style=BindingStyle.POSITIONAL,
                )
            )

    return ConnectivityIR(
        frontend_name="hand_oracle",
        frontend_version="1.0",
        definitions=tuple(definitions),
        instances=instances,
        bindings=tuple(bindings),
        coverage=CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=2,
            files_projected=2,
        ),
        top_instances=("uart_deep_x_tb",),
        metadata=(("fixture", "deep_x_npi"),),
    )


def _interface_port(
    name: str,
    interface_definition: str,
    modport: str,
    ordinal: int,
    line: int,
) -> PortDecl:
    return PortDecl(
        name=name,
        direction=PortDirection.INTERFACE,
        packed_range=BitRange.scalar(),
        ordinal=ordinal,
        location=_loc(HAND_RTL, line),
        interface_definition=interface_definition,
        modport=modport,
    )


def _hand_definitions() -> tuple[DefinitionTemplate, ...]:
    byte = BitRange(7, 0)
    word = BitRange(15, 0)
    scalar = BitRange.scalar()
    bus = DefinitionTemplate(
        definition_id="sg_bus_if#WIDTH=16",
        name="sg_bus_if",
        kind=DefinitionKind.INTERFACE,
        location=_loc(HAND_RTL, 3),
        ports=(_plain_port("clk", PortDirection.INPUT, 1, 0, HAND_RTL, 3),),
        signals=(
            SignalDecl("valid", SymbolKind.INTERFACE_FIELD, scalar, _loc(HAND_RTL, 4)),
            SignalDecl("ready", SymbolKind.INTERFACE_FIELD, scalar, _loc(HAND_RTL, 5)),
            SignalDecl("data", SymbolKind.INTERFACE_FIELD, word, _loc(HAND_RTL, 6)),
        ),
        modports=(
            ModportDecl(
                name="producer",
                members=(
                    ModportMember("valid", PortDirection.OUTPUT),
                    ModportMember("data", PortDirection.OUTPUT),
                    ModportMember("ready", PortDirection.INPUT),
                ),
                location=_loc(HAND_RTL, 8),
            ),
            ModportDecl(
                name="consumer",
                members=(
                    ModportMember("valid", PortDirection.INPUT),
                    ModportMember("data", PortDirection.INPUT),
                    ModportMember("ready", PortDirection.OUTPUT),
                ),
                location=_loc(HAND_RTL, 9),
            ),
        ),
    )

    leaf_seq = AssignmentFact(
        assignment_id="sg_leaf:always_ff:19:seq_q",
        kind=EdgeKind.PROCEDURAL_ASSIGN,
        target=SignalSelection.template("seq_q", byte),
        dependencies=(
            DependencyFact(
                source=SignalSelection.template("data_i", byte),
                target=SignalSelection.template("seq_q", byte),
                guard="rst_n",
            ),
            DependencyFact(
                source=SignalSelection.template("rst_n", scalar),
                target=SignalSelection.template("seq_q", byte),
                role=DependencyRole.CONTROL,
                exact_bit_mapping=False,
                guard="!rst_n",
            ),
            DependencyFact(
                source=SignalSelection.template("clk", scalar),
                target=SignalSelection.template("seq_q", byte),
                role=DependencyRole.CONTROL,
                exact_bit_mapping=False,
                guard="posedge clk",
            ),
        ),
        boundary=BoundaryKind.SEQUENTIAL,
        evidence=_evidence(HAND_RTL, 19, "always_ff", conditional=True),
        procedure_kind="AlwaysFF",
        guard="!rst_n || rst_n",
    )
    leaf_comb_target = SignalSelection.template("comb_y", byte)
    leaf_comb = AssignmentFact(
        assignment_id="sg_leaf:always_comb:26:comb_y",
        kind=EdgeKind.PROCEDURAL_ASSIGN,
        target=leaf_comb_target,
        dependencies=selections_for_concat(
            leaf_comb_target,
            (
                _slice("seq_q", 3, 0),
                _slice("data_i", 7, 4),
            ),
        ),
        boundary=BoundaryKind.COMBINATIONAL,
        evidence=_evidence(HAND_RTL, 26, "always_comb"),
        procedure_kind="AlwaysComb",
    )
    leaf = DefinitionTemplate(
        definition_id="sg_leaf",
        name="sg_leaf",
        kind=DefinitionKind.MODULE,
        location=_loc(HAND_RTL, 12),
        ports=(
            _plain_port("clk", PortDirection.INPUT, 1, 0, HAND_RTL, 13),
            _plain_port("rst_n", PortDirection.INPUT, 1, 1, HAND_RTL, 14),
            _plain_port("data_i", PortDirection.INPUT, 8, 2, HAND_RTL, 15),
            _plain_port("seq_q", PortDirection.OUTPUT, 8, 3, HAND_RTL, 16),
            _plain_port("comb_y", PortDirection.OUTPUT, 8, 4, HAND_RTL, 17),
        ),
        assignments=(leaf_seq, leaf_comb),
    )

    lane_target = SignalSelection.template("lane_o", byte)
    lane = DefinitionTemplate(
        definition_id="sg_lane#LANE",
        name="sg_lane",
        kind=DefinitionKind.MODULE,
        location=_loc(HAND_RTL, 31),
        ports=(
            _plain_port("clk", PortDirection.INPUT, 1, 0, HAND_RTL, 34),
            _plain_port("rst_n", PortDirection.INPUT, 1, 1, HAND_RTL, 35),
            _plain_port("lane_i", PortDirection.INPUT, 8, 2, HAND_RTL, 36),
            _plain_port("lane_o", PortDirection.OUTPUT, 8, 3, HAND_RTL, 37),
        ),
        signals=(
            _signal("seq_q", 8, HAND_RTL, 39),
            _signal("comb_y", 8, HAND_RTL, 40),
        ),
        assignments=(
            AssignmentFact(
                assignment_id="sg_lane:assign:50:lane_o",
                kind=EdgeKind.CONTINUOUS_ASSIGN,
                target=lane_target,
                dependencies=selections_for_concat(
                    lane_target,
                    (
                        _slice("comb_y", 3, 0),
                        _slice("seq_q", 7, 4),
                    ),
                ),
                boundary=BoundaryKind.COMBINATIONAL,
                evidence=_evidence(HAND_RTL, 50, "continuous_assign"),
            ),
        ),
    )

    bridge = DefinitionTemplate(
        definition_id="sg_bridge",
        name="sg_bridge",
        kind=DefinitionKind.MODULE,
        location=_loc(HAND_RTL, 53),
        ports=(
            _plain_port("clk", PortDirection.INPUT, 1, 0, HAND_RTL, 54),
            _plain_port("rst_n", PortDirection.INPUT, 1, 1, HAND_RTL, 55),
            _interface_port("bus", "sg_bus_if", "consumer", 2, 56),
            _plain_port("lane_data", PortDirection.OUTPUT, 16, 3, HAND_RTL, 57),
        ),
        assignments=(
            AssignmentFact(
                assignment_id="sg_bridge:assign:59:bus.ready",
                kind=EdgeKind.CONTINUOUS_ASSIGN,
                target=SignalSelection.template("bus.ready", scalar),
                dependencies=(
                    DependencyFact(
                        source=SignalSelection.template("lane_data", word),
                        target=SignalSelection.template("bus.ready", scalar),
                        exact_bit_mapping=False,
                    ),
                ),
                boundary=BoundaryKind.COMBINATIONAL,
                evidence=_evidence(HAND_RTL, 59, "continuous_assign"),
                generate_scope=None,
            ),
        ),
    )

    producer_data_target = SignalSelection.template("bus.data", word)
    producer = DefinitionTemplate(
        definition_id="sg_producer",
        name="sg_producer",
        kind=DefinitionKind.MODULE,
        location=_loc(HAND_RTL, 71),
        ports=(
            _plain_port("clk", PortDirection.INPUT, 1, 0, HAND_RTL, 72),
            _plain_port("rst_n", PortDirection.INPUT, 1, 1, HAND_RTL, 73),
            _plain_port("seed", PortDirection.OUTPUT, 16, 2, HAND_RTL, 74),
            _interface_port("bus", "sg_bus_if", "producer", 3, 75),
        ),
        assignments=(
            AssignmentFact(
                assignment_id="sg_producer:always_ff:77:seed",
                kind=EdgeKind.PROCEDURAL_ASSIGN,
                target=SignalSelection.template("seed", word),
                dependencies=(
                    DependencyFact(
                        source=SignalSelection.template("seed", word),
                        target=SignalSelection.template("seed", word),
                        exact_bit_mapping=False,
                        guard="rst_n && bus.ready",
                    ),
                    DependencyFact(
                        source=SignalSelection.template("bus.ready", scalar),
                        target=SignalSelection.template("seed", word),
                        role=DependencyRole.CONTROL,
                        exact_bit_mapping=False,
                        guard="bus.ready",
                    ),
                    DependencyFact(
                        source=SignalSelection.template("rst_n", scalar),
                        target=SignalSelection.template("seed", word),
                        role=DependencyRole.CONTROL,
                        exact_bit_mapping=False,
                        guard="!rst_n",
                    ),
                ),
                boundary=BoundaryKind.SEQUENTIAL,
                evidence=_evidence(HAND_RTL, 77, "always_ff", conditional=True),
                procedure_kind="AlwaysFF",
                guard="!rst_n || bus.ready",
            ),
            AssignmentFact(
                assignment_id="sg_producer:always_comb:84:bus.valid",
                kind=EdgeKind.PROCEDURAL_ASSIGN,
                target=SignalSelection.template("bus.valid", scalar),
                dependencies=(
                    DependencyFact(
                        source=SignalSelection.template("rst_n", scalar),
                        target=SignalSelection.template("bus.valid", scalar),
                    ),
                ),
                boundary=BoundaryKind.COMBINATIONAL,
                evidence=_evidence(HAND_RTL, 84, "always_comb"),
                procedure_kind="AlwaysComb",
            ),
            AssignmentFact(
                assignment_id="sg_producer:always_comb:84:bus.data",
                kind=EdgeKind.PROCEDURAL_ASSIGN,
                target=producer_data_target,
                dependencies=selections_for_concat(
                    producer_data_target,
                    (
                        _slice("seed", 7, 0),
                        _slice("seed", 15, 8),
                    ),
                ),
                boundary=BoundaryKind.COMBINATIONAL,
                evidence=_evidence(HAND_RTL, 84, "always_comb"),
                procedure_kind="AlwaysComb",
            ),
        ),
    )

    top = DefinitionTemplate(
        definition_id="sg_top",
        name="sg_top",
        kind=DefinitionKind.MODULE,
        location=_loc(HAND_RTL, 90),
        signals=(
            _signal("clk", 1, HAND_RTL, 91),
            _signal("rst_n", 1, HAND_RTL, 92),
            _signal("seed", 16, HAND_RTL, 93),
            _signal("lane_data", 16, HAND_RTL, 94),
            _signal("runtime_force", 1, HAND_RTL, 94),
        ),
    )
    return bus, leaf, lane, bridge, producer, top


def _interface_member_binding(
    *,
    binding_id: str,
    child: str,
    port_name: str,
    member: str,
    direction: PortDirection,
    modport: str,
    width: int,
    line: int,
) -> PortBinding:
    return PortBinding(
        binding_id=binding_id,
        instance_path=child,
        port_name=f"{port_name}.{member}",
        direction=direction,
        style=BindingStyle.MODPORT,
        mappings=(
            BitMapping(
                source=_select(member, width, instance="sg_top.bus"),
                target=_select(f"{port_name}.{member}", width, instance=child),
            ),
        ),
        evidence=_evidence(HAND_RTL, line, "modport_binding"),
        interface_definition="sg_bus_if",
        modport=modport,
    )


def build_hand_ir() -> ConnectivityIR:
    definitions = _hand_definitions()
    instances = (
        InstanceDecl("sg_top", "sg_top", "sg_top", None, _loc(HAND_RTL, 90)),
        InstanceDecl(
            "sg_top.bus",
            "bus",
            "sg_bus_if#WIDTH=16",
            "sg_top",
            _loc(HAND_RTL, 96),
            parameterization=(("WIDTH", "16"),),
        ),
        InstanceDecl(
            "sg_top.u_bridge", "u_bridge", "sg_bridge", "sg_top", _loc(HAND_RTL, 105)
        ),
        InstanceDecl(
            "sg_top.u_bridge.gen_lanes[0].u_lane",
            "u_lane",
            "sg_lane#LANE",
            "sg_top.u_bridge",
            _loc(HAND_RTL, 62),
            generate_scope="gen_lanes[0]",
            parameterization=(("LANE", "0"),),
        ),
        InstanceDecl(
            "sg_top.u_bridge.gen_lanes[0].u_lane.u_named",
            "u_named",
            "sg_leaf",
            "sg_top.u_bridge.gen_lanes[0].u_lane",
            _loc(HAND_RTL, 42),
        ),
        InstanceDecl(
            "sg_top.u_bridge.gen_lanes[1].u_lane",
            "u_lane",
            "sg_lane#LANE",
            "sg_top.u_bridge",
            _loc(HAND_RTL, 62),
            generate_scope="gen_lanes[1]",
            parameterization=(("LANE", "1"),),
        ),
        InstanceDecl(
            "sg_top.u_bridge.gen_lanes[1].u_lane.u_named",
            "u_named",
            "sg_leaf",
            "sg_top.u_bridge.gen_lanes[1].u_lane",
            _loc(HAND_RTL, 42),
        ),
        InstanceDecl(
            "sg_top.u_producer",
            "u_producer",
            "sg_producer",
            "sg_top",
            _loc(HAND_RTL, 98),
        ),
    )
    definition_index = {item.definition_id: item for item in definitions}
    bindings: list[PortBinding] = []

    bus_clk = definition_index["sg_bus_if#WIDTH=16"].ports[0]
    bindings.append(
        _instance_binding(
            binding_id="sg_top.bus:0:clk",
            child="sg_top.bus",
            port=bus_clk,
            actual_instance="sg_top",
            actual_symbol="clk",
            path=HAND_RTL,
            line=96,
            style=BindingStyle.POSITIONAL,
        )
    )

    producer = definition_index["sg_producer"]
    for port in producer.ports[:3]:
        bindings.append(
            _instance_binding(
                binding_id=f"sg_top.u_producer:{port.ordinal}:{port.name}",
                child="sg_top.u_producer",
                port=port,
                actual_instance="sg_top",
                actual_symbol=port.name,
                path=HAND_RTL,
                line=98,
                style=BindingStyle.NAMED,
            )
        )
    for member, direction, width in (
        ("valid", PortDirection.OUTPUT, 1),
        ("data", PortDirection.OUTPUT, 16),
        ("ready", PortDirection.INPUT, 1),
    ):
        bindings.append(
            _interface_member_binding(
                binding_id=f"sg_top.u_producer:bus.{member}",
                child="sg_top.u_producer",
                port_name="bus",
                member=member,
                direction=direction,
                modport="producer",
                width=width,
                line=98,
            )
        )

    bridge = definition_index["sg_bridge"]
    for port, actual_name in zip(
        (bridge.ports[0], bridge.ports[1], bridge.ports[3]),
        ("clk", "rst_n", "lane_data"),
    ):
        bindings.append(
            _instance_binding(
                binding_id=f"sg_top.u_bridge:{port.ordinal}:{port.name}",
                child="sg_top.u_bridge",
                port=port,
                actual_instance="sg_top",
                actual_symbol=actual_name,
                path=HAND_RTL,
                line=105,
                style=BindingStyle.POSITIONAL,
            )
        )
    for member, direction, width in (
        ("valid", PortDirection.INPUT, 1),
        ("data", PortDirection.INPUT, 16),
        ("ready", PortDirection.OUTPUT, 1),
    ):
        bindings.append(
            _interface_member_binding(
                binding_id=f"sg_top.u_bridge:bus.{member}",
                child="sg_top.u_bridge",
                port_name="bus",
                member=member,
                direction=direction,
                modport="consumer",
                width=width,
                line=105,
            )
        )

    lane_definition = definition_index["sg_lane#LANE"]
    for lane_index in (0, 1):
        child = f"sg_top.u_bridge.gen_lanes[{lane_index}].u_lane"
        lane_slice = BitRange(lane_index * 8 + 7, lane_index * 8).indices
        actuals = (
            ("clk", None),
            ("rst_n", None),
            ("bus.data", lane_slice),
            ("lane_data", lane_slice),
        )
        for port, (actual_name, actual_bits) in zip(lane_definition.ports, actuals):
            bindings.append(
                _instance_binding(
                    binding_id=f"{child}:{port.ordinal}:{port.name}",
                    child=child,
                    port=port,
                    actual_instance="sg_top.u_bridge",
                    actual_symbol=actual_name,
                    actual_bits=actual_bits,
                    path=HAND_RTL,
                    line=62,
                    style=BindingStyle.POSITIONAL,
                )
            )

        leaf_child = f"{child}.u_named"
        leaf_definition = definition_index["sg_leaf"]
        for port, actual_name in zip(
            leaf_definition.ports,
            ("clk", "rst_n", "lane_i", "seq_q", "comb_y"),
        ):
            bindings.append(
                _instance_binding(
                    binding_id=f"{leaf_child}:{port.ordinal}:{port.name}",
                    child=leaf_child,
                    port=port,
                    actual_instance=child,
                    actual_symbol=actual_name,
                    path=HAND_RTL,
                    line=42,
                    style=BindingStyle.NAMED,
                )
            )

    return ConnectivityIR(
        frontend_name="hand_oracle",
        frontend_version="1.0",
        definitions=definitions,
        instances=instances,
        bindings=tuple(bindings),
        coverage=CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=1,
            files_projected=1,
        ),
        top_instances=("sg_top",),
        metadata=(("fixture", "hand_connectivity"),),
    )
