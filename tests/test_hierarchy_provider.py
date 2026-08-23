from __future__ import annotations

import pytest

from src.connectivity_ir import (
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    InstanceDecl,
    SourceLocation,
)
from src.hierarchy_provider import (
    ConnectivityIRHierarchyProvider,
    HierarchyCandidateLimitExceeded,
    HierarchyProviderKind,
    LexicalHierarchyProvider,
    NpiHierarchyProvider,
    NpiInstanceBindingFact,
    hierarchy_candidate_instance_paths,
)


def _semantic_ir() -> ConnectivityIR:
    location = SourceLocation("top.sv", 1)
    leaf_location = SourceLocation("leaf.sv", 4)
    definitions = (
        DefinitionTemplate("top", "top", DefinitionKind.MODULE, location),
        DefinitionTemplate(
            "leaf@width8", "leaf", DefinitionKind.MODULE, leaf_location
        ),
        DefinitionTemplate("cell", "cell", DefinitionKind.MODULE, leaf_location),
    )
    instances = (
        InstanceDecl("top", "top", "top", None, location),
        InstanceDecl(
            "top.g_lane[0].u_leaf",
            "u_leaf",
            "leaf@width8",
            "top",
            SourceLocation("top.sv", 10),
            generate_scope="g_lane[0]",
            parameterization=(("WIDTH", "8"),),
        ),
        InstanceDecl(
            "top.g_lane[0].u_leaf.u_cell",
            "u_cell",
            "cell",
            "top.g_lane[0].u_leaf",
            SourceLocation("leaf.sv", 8),
        ),
        InstanceDecl(
            "top.g_lane[1].u_leaf",
            "u_leaf",
            "leaf@width8",
            "top",
            SourceLocation("top.sv", 11),
            generate_scope="g_lane[1]",
            parameterization=(("WIDTH", "8"),),
        ),
    )
    return ConnectivityIR(
        frontend_name="Slang/pyslang",
        frontend_version="11.0.0",
        definitions=definitions,
        instances=instances,
        bindings=(),
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=2,
            files_projected=2,
            gaps=(
                CoverageGap(
                    code="generated_scope_partial",
                    message="fixture gap",
                    impact=CoverageStatus.INCONCLUSIVE,
                    scopes=("top.g_lane[0].u_leaf",),
                ),
            ),
        ),
        top_instances=("top",),
    )


def test_lexical_provider_resolves_proved_chain_and_edge_gaps():
    hierarchy = {
        "_hierarchy_snapshot_sha256": "a" * 64,
        "component_tree": {
            "top": {
                "u_dut": {
                    "class": "dut",
                    "source_file": "top.sv",
                    "source_line": 12,
                    "source_info_origin": "compile_log",
                    "hierarchy_gap_codes": [
                        "hierarchy_include_path_unresolved"
                    ],
                    "children": {
                        "u_leaf": {
                            "class": "leaf",
                            "source_file": "dut.sv",
                            "source_line": 7,
                            "source_info_origin": "compile_log",
                            "hierarchy_gap_codes": [],
                        }
                    },
                }
            }
        },
    }
    provider = LexicalHierarchyProvider(hierarchy)

    resolution = provider.resolve_scope(
        top="top", signal_path="top.u_dut.u_leaf.value"
    )

    assert resolution is not None
    assert resolution.provider_kind is HierarchyProviderKind.COMPILE_LEXICAL
    assert resolution.status == "resolved"
    assert resolution.ancestors == ("top", "top.u_dut", "top.u_dut.u_leaf")
    assert [item.definition_name for item in resolution.bindings] == [
        "top",
        "dut",
        "leaf",
    ]
    assert resolution.bindings[1].source_file == "top.sv"
    assert resolution.bindings[1].source_line == 12
    assert resolution.coverage_gap_codes == (
        "hierarchy_include_path_unresolved",
    )


def test_lexical_provider_retains_deferred_and_proved_missing_semantics():
    hierarchy = {
        "component_tree": {"top": {}},
        "_scan_results": [
            {
                "module_instance_map": {
                    "top": [
                        {
                            "instance_name": "u_real",
                            "module_name": "leaf",
                            "hierarchy_edge_status": "complete",
                        },
                        {
                            "instance_name": "u_generated",
                            "module_name": "leaf",
                            "hierarchy_edge_status": "unresolved_semantic",
                            "hierarchy_gap_codes": [
                                "hierarchy_generate_scope_unmodeled"
                            ],
                        },
                    ]
                }
            }
        ],
    }
    provider = LexicalHierarchyProvider(hierarchy)

    proved = provider.resolve_scope(
        top="top", signal_path="top.u_real.child.value"
    )
    deferred = provider.resolve_scope(
        top="top", signal_path="top.u_generated.child.value"
    )

    assert proved is not None and proved.missing_instance_proved is True
    assert proved.status == "truncated"
    assert deferred is not None and deferred.missing_instance_proved is False
    assert deferred.status == "deferred"
    assert "hierarchy_generate_scope_unmodeled" in deferred.gap_codes


def test_semantic_provider_resolves_generate_scope_and_specialization():
    provider = ConnectivityIRHierarchyProvider(
        _semantic_ir(), design_identity="snapshot-a"
    )

    resolution = provider.resolve_scope(
        top="top", signal_path="top.g_lane[0].u_leaf.u_cell.value"
    )

    assert resolution is not None
    assert resolution.provider_kind is HierarchyProviderKind.SLANG_IR
    assert resolution.status == "resolved"
    # Generate blocks are path atoms inside the child instance, not invented
    # parent instances.
    assert resolution.ancestors == (
        "top",
        "top.g_lane[0].u_leaf",
        "top.g_lane[0].u_leaf.u_cell",
    )
    leaf = resolution.bindings[1]
    assert leaf.definition_id == "leaf@width8"
    assert leaf.definition_name == "leaf"
    assert leaf.generate_scope == "g_lane[0]"
    assert leaf.parameterization == (("WIDTH", "8"),)
    assert "generated_scope_partial" in resolution.coverage_gap_codes


def test_semantic_scope_resolution_does_not_enumerate_instance_index():
    class LookupOnlyDict(dict):
        def __iter__(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("semantic scope lookup enumerated the design")

        def items(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("semantic scope lookup enumerated the design")

        def values(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("semantic scope lookup enumerated the design")

    ir = _semantic_ir()
    provider = ConnectivityIRHierarchyProvider(
        ir,
        design_identity="snapshot-a",
        instance_index=LookupOnlyDict(ir.instance_index),
        definition_index=ir.definition_index,
    )

    resolution = provider.resolve_scope(
        top="top", signal_path="top.g_lane[0].u_leaf.u_cell.value"
    )

    assert resolution is not None
    assert resolution.ancestors[-1] == "top.g_lane[0].u_leaf.u_cell"


def test_semantic_provider_bounds_direct_children_without_subtree_materialization():
    provider = ConnectivityIRHierarchyProvider(
        _semantic_ir(), design_identity="snapshot-a"
    )

    result = provider.direct_children(
        top="top", instance_path="top", max_children=1
    )

    assert result is not None
    assert result.available_count == 2
    assert result.truncated is True
    assert result.paths == ("top.g_lane[0].u_leaf",)
    nested = provider.direct_children(
        top="top", instance_path="top.g_lane[0].u_leaf", max_children=4
    )
    assert nested is not None
    assert nested.truncated is False
    assert nested.paths == ("top.g_lane[0].u_leaf.u_cell",)


def test_semantic_instance_ids_are_snapshot_scoped_and_deterministic():
    ir = _semantic_ir()
    first = ConnectivityIRHierarchyProvider(ir, design_identity="snapshot-a")
    same = ConnectivityIRHierarchyProvider(ir, design_identity="snapshot-a")
    changed = ConnectivityIRHierarchyProvider(ir, design_identity="snapshot-b")

    first_binding = first.lookup_instance(
        top="top", instance_path="top.g_lane[0].u_leaf"
    )
    same_binding = same.lookup_instance(
        top="top", instance_path="top.g_lane[0].u_leaf"
    )
    changed_binding = changed.lookup_instance(
        top="top", instance_path="top.g_lane[0].u_leaf"
    )

    assert first_binding is not None
    assert same_binding is not None
    assert changed_binding is not None
    assert first_binding.instance_id == same_binding.instance_id
    assert first_binding.instance_id != changed_binding.instance_id


def test_npi_candidate_paths_admit_generated_instance_without_walking():
    candidates = hierarchy_candidate_instance_paths(
        top="top",
        signal_path="top.u_dut.g_lane[3].u_leaf.value",
        max_candidates=8,
    )

    assert candidates == (
        "top",
        "top.u_dut",
        "top.u_dut.g_lane[3]",
        "top.u_dut.g_lane[3].u_leaf",
    )
    with pytest.raises(HierarchyCandidateLimitExceeded):
        hierarchy_candidate_instance_paths(
            top="top",
            signal_path="top.u_dut.g_lane[3].u_leaf.value",
            max_candidates=3,
        )


@pytest.mark.parametrize("source_line", (0, -1, True, "12"))
def test_npi_binding_fact_rejects_non_positive_integer_lines(source_line):
    with pytest.raises(ValueError, match="positive"):
        NpiInstanceBindingFact("top", "top_def", "top.sv", source_line)


def test_npi_provider_skips_generate_pseudo_parent_and_stays_partial():
    provider = NpiHierarchyProvider(
        (
            NpiInstanceBindingFact("top", "top_def", "top.sv", 1),
            NpiInstanceBindingFact("top.u_dut", "dut", "top.sv", 5),
            NpiInstanceBindingFact(
                "top.u_dut.g_lane[3].u_leaf", "leaf", "dut.sv", 20
            ),
        ),
        top="top",
        design_identity="npi-snapshot",
    )

    resolution = provider.resolve_scope(
        top="top", signal_path="top.u_dut.g_lane[3].u_leaf.value"
    )

    assert resolution is not None
    assert resolution.provider_kind is HierarchyProviderKind.VERDI_NPI
    assert resolution.ancestors == (
        "top",
        "top.u_dut",
        "top.u_dut.g_lane[3].u_leaf",
    )
    assert resolution.status == "resolved"
    assert resolution.coverage_gap_codes == (
        "npi_hierarchy_fragment_bounded",
    )
    children = provider.direct_children(
        top="top", instance_path="top.u_dut", max_children=8
    )
    assert children is not None
    assert children.paths == ("top.u_dut.g_lane[3].u_leaf",)
    assert children.truncated is True
