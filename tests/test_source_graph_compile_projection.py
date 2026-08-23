from __future__ import annotations

from src.source_graph_compile_projection import (
    COMPILE_PROJECTION_GAP,
    plan_source_graph_compile_projection,
)
from src.source_graph_contract import CompileInputManifest


def _manifest(inputs: tuple[str, ...], *, tops: tuple[str, ...] = ("top",)):
    return CompileInputManifest(
        fingerprint="a" * 64,
        ordered_inputs=inputs,
        ordered_options=("--compat", "vcs"),
        ordered_tops=tops,
        inputs_complete=True,
        options_complete=True,
        tops_complete=True,
    )


def _record(
    path: str,
    *,
    modules: tuple[str, ...] = (),
    packages: tuple[str, ...] = (),
    imports: tuple[str, ...] = (),
    qualifiers: tuple[str, ...] = (),
    macro_definitions: tuple[str, ...] = (),
    macro_undefinitions: tuple[str, ...] = (),
    conditional_macros: tuple[str, ...] = (),
    macro_uses: tuple[str, ...] = (),
    has_macro_undefineall: bool = False,
):
    return {
        "path": path,
        "modules": list(modules),
        "interfaces": [],
        "structural_modules": list(modules),
        "structural_interfaces": [],
        "packages": list(packages),
        "package_imports": list(imports),
        "package_qualifiers": list(qualifiers),
        "macro_definitions": list(macro_definitions),
        "macro_undefinitions": list(macro_undefinitions),
        "conditional_macros": list(conditional_macros),
        "macro_uses": list(macro_uses),
        "has_macro_undefineall": has_macro_undefineall,
    }


def _large_fixture():
    inputs = tuple(f"/design/unit_{index}.sv" for index in range(70))
    records = [
        _record(inputs[0], macro_definitions=("TARGET_WIDTH",)),
        _record(inputs[1], packages=("types_pkg",)),
        _record(
            inputs[2],
            modules=("top",),
            imports=("types_pkg",),
            macro_uses=("TARGET_WIDTH",),
        ),
        _record(inputs[3], modules=("child",), qualifiers=("types_pkg",)),
        _record(inputs[4], modules=("leaf",)),
        *(_record(path, modules=(f"unrelated_{index}",)) for index, path in enumerate(inputs[5:])),
    ]
    hierarchy = {
        "component_tree": {
            "top": {
                "u_child": {
                    "class": "child",
                    "children": {"u_leaf": {"class": "leaf"}},
                }
            }
        },
        "_scan_results": records,
    }
    return inputs, hierarchy


def test_large_hierarchy_uses_ordered_dependency_closure():
    inputs, hierarchy = _large_fixture()

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.applied is True
    assert decision.projection is not None
    assert decision.projection.ordered_inputs == inputs[:5]
    assert decision.input_count == 5
    assert decision.excluded_input_count == 65
    assert decision.seed_symbol_count == 3
    assert decision.dependency_symbol_count == 2
    assert decision.gap_codes == (COMPILE_PROJECTION_GAP,)
    assert decision.fallback_reason is None


def test_compile_tops_seed_bind_definition_without_elaborating_siblings():
    inputs, hierarchy = _large_fixture()
    hierarchy["_scan_results"][5] = _record(
        inputs[5], modules=("top_bind",)
    )

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs, tops=("top_bind", "top")),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.projection is not None
    assert decision.projection.ordered_inputs == inputs[:6]


def test_small_manifest_keeps_full_replay():
    inputs, hierarchy = _large_fixture()
    inputs = inputs[:8]

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child"),
    )

    assert decision.applied is False
    assert decision.input_count == len(inputs)
    assert decision.fallback_reason == "below_input_threshold"
    assert decision.gap_codes == ()


def test_unresolved_import_falls_back_to_full_manifest():
    inputs, hierarchy = _large_fixture()
    hierarchy["_scan_results"][3]["package_imports"] = ["missing_pkg"]

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.applied is False
    assert decision.input_count == len(inputs)
    assert decision.fallback_reason == "package_dependency_unresolved"


def test_replay_only_tool_package_can_be_recovered_by_basename():
    original_inputs, hierarchy = _large_fixture()
    inputs = list(original_inputs)
    inputs[5] = "/tools/uvm_pkg.sv"
    hierarchy["_scan_results"] = [
        record
        for record in hierarchy["_scan_results"]
        if record["path"] != original_inputs[5]
    ]
    hierarchy["_scan_results"][2]["package_imports"].append("uvm_pkg")

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(tuple(inputs)),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.projection is not None
    assert decision.projection.ordered_inputs == tuple(inputs[:6])


def test_scanned_basename_without_package_definition_is_not_promoted():
    original_inputs, hierarchy = _large_fixture()
    inputs = list(original_inputs)
    inputs[5] = "/design/missing_pkg.sv"
    hierarchy["_scan_results"][5] = _record(inputs[5], modules=("helper",))
    hierarchy["_scan_results"][2]["package_imports"].append("missing_pkg")

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(tuple(inputs)),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.applied is False
    assert decision.fallback_reason == "package_dependency_unresolved"


def test_qualified_non_package_scope_does_not_block_projection():
    inputs, hierarchy = _large_fixture()
    hierarchy["_scan_results"][3]["package_qualifiers"].append("some_class")

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.projection is not None
    assert decision.projection.ordered_inputs == inputs[:5]


def test_duplicate_compile_input_keeps_full_replay():
    inputs, hierarchy = _large_fixture()
    duplicate_inputs = (*inputs, inputs[-1])

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(duplicate_inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child"),
    )

    assert decision.applied is False
    assert decision.fallback_reason == "duplicate_compile_inputs"


def test_ambiguous_seed_definition_keeps_full_replay():
    inputs, hierarchy = _large_fixture()
    hierarchy["_scan_results"][5] = _record(inputs[5], modules=("child",))

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child"),
    )

    assert decision.applied is False
    assert decision.fallback_reason == "seed_definition_ambiguous"


def test_conditional_macro_and_undef_mutations_join_the_closure():
    inputs, hierarchy = _large_fixture()
    hierarchy["_scan_results"][0] = _record(
        inputs[0], macro_definitions=("FEATURE",)
    )
    hierarchy["_scan_results"][1] = _record(
        inputs[1],
        packages=("types_pkg",),
        macro_undefinitions=("FEATURE",),
    )
    hierarchy["_scan_results"][2] = _record(
        inputs[2],
        modules=("top",),
        imports=("types_pkg",),
        conditional_macros=("FEATURE",),
    )

    decision = plan_source_graph_compile_projection(
        manifest=_manifest(inputs),
        hierarchy_result=hierarchy,
        top="top",
        instance_paths=("top", "top.u_child", "top.u_child.u_leaf"),
    )

    assert decision.projection is not None
    assert decision.projection.ordered_inputs == inputs[:5]
    assert decision.dependency_symbol_count == 2
