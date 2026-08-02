import json
from dataclasses import replace

import pytest

from src.connectivity_ir import (
    BindingStyle,
    BitRange,
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    PortDirection,
    selections_for_concat,
    SignalSelection,
)
from tests.connectivity_ir_fixtures import build_deep_ir, build_hand_ir


def test_bit_range_preserves_declared_direction_and_concat_mapping():
    assert BitRange(15, 8).indices == tuple(range(15, 7, -1))
    assert BitRange(0, 7).indices == tuple(range(0, 8))

    target = SignalSelection.template("out", BitRange(7, 0))
    dependencies = selections_for_concat(
        target,
        (
            SignalSelection("upper", (3, 2, 1, 0)),
            SignalSelection("lower", (7, 6, 5, 4)),
        ),
    )

    assert dependencies[0].target.bits == (7, 6, 5, 4)
    assert dependencies[1].target.bits == (3, 2, 1, 0)


def test_hand_ir_contract_covers_required_constructs_and_template_reuse():
    ir = build_hand_ir()
    definitions = {item.name: item for item in ir.definitions}

    assert len(ir.definitions) == 6
    assert len(ir.instances) == 8
    assert definitions["sg_bus_if"].kind is DefinitionKind.INTERFACE
    assert {item.name for item in definitions["sg_bus_if"].modports} == {
        "producer",
        "consumer",
    }
    assert definitions["sg_bridge"].port("bus").modport == "consumer"
    assert definitions["sg_producer"].port("bus").modport == "producer"
    assert {item.style for item in ir.bindings} >= {
        BindingStyle.NAMED,
        BindingStyle.POSITIONAL,
        BindingStyle.MODPORT,
    }
    assert any(item.generate_scope == "gen_lanes[0]" for item in ir.instances)
    assert any(item.generate_scope == "gen_lanes[1]" for item in ir.instances)

    procedure_kinds = {
        assignment.procedure_kind
        for definition in ir.definitions
        for assignment in definition.assignments
        if assignment.procedure_kind
    }
    assert procedure_kinds == {"AlwaysComb", "AlwaysFF"}
    assert any(
        assignment.boundary.value == "sequential"
        for definition in ir.definitions
        for assignment in definition.assignments
    )
    assert ir.stats()["binding_segment_count"] > 0
    assert ir.stats()["dependency_count"] > 0


def test_deep_ir_contains_seven_positional_output_boundaries():
    ir = build_deep_ir()
    output_bindings = [
        item for item in ir.bindings if item.direction is PortDirection.OUTPUT
    ]

    assert len(ir.instances) == 8
    assert len(output_bindings) == 7
    assert all(item.style is BindingStyle.POSITIONAL for item in output_bindings)


def test_ir_serialization_roundtrip_and_fingerprint_are_deterministic():
    ir = build_hand_ir()
    payload = ir.to_json_bytes()
    decoded = json.loads(payload)
    restored = ConnectivityIR.from_json_bytes(payload)

    assert decoded["ir_version"] == "1.0"
    assert restored.to_dict() == ir.to_dict()
    assert restored.fingerprint_sha256() == ir.fingerprint_sha256()
    assert len(ir.fingerprint_sha256()) == 64

    reordered = replace(
        ir,
        definitions=tuple(reversed(ir.definitions)),
        instances=tuple(reversed(ir.instances)),
        bindings=tuple(reversed(ir.bindings)),
    )
    assert reordered.fingerprint_sha256() == ir.fingerprint_sha256()


def test_ir_rejects_complete_coverage_with_a_gap():
    with pytest.raises(ValueError, match="complete coverage"):
        CoverageReport(
            status=CoverageStatus.COMPLETE,
            files_total=1,
            files_projected=1,
            gaps=(
                CoverageGap(
                    code="runtime_force",
                    message="runtime force is not statically modeled",
                    impact=CoverageStatus.INCONCLUSIVE,
                ),
            ),
        )
