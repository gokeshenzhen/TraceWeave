from __future__ import annotations

from pathlib import Path
import threading

import pytest

import src.cancellation as cancellation
import src.source_graph_adapter as source_graph_adapter
from src.compile_log_parser import parse_compile_log
from src.hierarchy_handles import compute_snapshot_fingerprint
from src.source_graph_adapter import (
    AdapterStatus,
    SOURCE_GRAPH_ADAPTER_VERSION,
    build_source_graph_frontier_plan,
    build_source_graph_path_plan,
    build_source_graph_plan,
)
from src.source_graph_contract import (
    ConnectivityPathTarget,
    QueryOperation,
    compute_source_graph_build_key,
    compute_source_graph_query_key,
)


@pytest.fixture(autouse=True)
def _clean_fingerprint_cache():
    source_graph_adapter._reset_source_graph_adapter_cache_for_tests()
    yield
    source_graph_adapter._reset_source_graph_adapter_cache_for_tests()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _compile_result(
    tmp_path: Path,
    *,
    command: str,
    sources: tuple[Path, ...],
    tops: tuple[str, ...] = ("tb",),
) -> dict:
    (tmp_path / "compile.log").write_text(command + "\n", encoding="utf-8")
    return {
        "simulator": "xcelium",
        "compile_cwd": str(tmp_path),
        "compile_command": command,
        "top_modules": list(tops),
        "files": {
            "user": [
                {"path": str(path.resolve()), "type": "module", "category": "rtl"}
                for path in sources
            ],
            "filtered_count": 0,
        },
        "include_tree": {},
        "filelist_tree": {},
        "interfaces": [],
        "parse_warnings": [],
    }


def _hierarchy() -> dict:
    return {
        "component_tree": {
            "tb": {
                "dut": {
                    "module": "dut",
                    "children": {
                        "u_leaf": {"module": "leaf", "children": {}},
                    },
                }
            }
        }
    }


def _plan(
    tmp_path: Path,
    compile_result: dict,
    *,
    signal_path: str = "tb.dut.q[3:0]",
    hierarchy: dict | None = None,
):
    return build_source_graph_plan(
        compile_log=str(tmp_path / "compile.log"),
        compile_result=compile_result,
        hierarchy_result=hierarchy or _hierarchy(),
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(
            str(tmp_path / "compile.log"), "xcelium"
        ),
        operation=QueryOperation.DRIVER,
        signal_path=signal_path,
        top_hint="tb",
        max_hops=8,
        frontend_version="11.0.0",
    )


def _path_plan(
    tmp_path: Path,
    compile_result: dict,
    *,
    from_signal: str = "tb.dut.u_left.out",
    to_signal: str = "tb.dut.u_right.in",
    hierarchy: dict | None = None,
    expand_assigns: bool = False,
    top_hint: str | None = "tb",
):
    return build_source_graph_path_plan(
        compile_log=str(tmp_path / "compile.log"),
        compile_result=compile_result,
        hierarchy_result=hierarchy
        or {
            "component_tree": {
                "tb": {
                    "dut": {
                        "module": "dut",
                        "children": {
                            "u_left": {"module": "left", "children": {}},
                            "u_right": {"module": "right", "children": {}},
                        },
                    }
                }
            }
        },
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(
            str(tmp_path / "compile.log"), "xcelium"
        ),
        from_signal=from_signal,
        to_signal=to_signal,
        top_hint=top_hint,
        expand_assigns=expand_assigns,
        frontend_version="11.0.0",
    )


def _frontier_plan(
    tmp_path: Path,
    compile_result: dict,
    *,
    max_instances: int = 128,
):
    hierarchy = {
        "component_tree": {
            "tb": {
                "dut": {
                    "module": "dut",
                    "children": {
                        "u_leaf": {"module": "leaf", "children": {}},
                        "u_source": {"module": "source", "children": {}},
                    },
                }
            }
        }
    }
    return build_source_graph_frontier_plan(
        compile_log=str(tmp_path / "compile.log"),
        compile_result=compile_result,
        hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(
            str(tmp_path / "compile.log"), "xcelium"
        ),
        operation=QueryOperation.DRIVER,
        signal_path="tb.dut.u_leaf.data_i[31:0]",
        frontier_signal_paths=("tb.dut.parent_net[31:0]",),
        top_hint="tb",
        max_hops=8,
        frontend_version="11.0.0",
        max_instances=max_instances,
    )


def test_builds_complete_ordered_manifest_and_replays_every_top(tmp_path):
    child = tmp_path / "rtl" / "child.sv"
    top = tmp_path / "tb" / "top.sv"
    _write(child, "module dut; logic [3:0] q; endmodule\n")
    _write(top, "module tb; dut dut(); endmodule\n")
    _write(
        tmp_path / "design.f",
        "-define FEATURE=1\nrtl/child.sv\ntb/top.sv\n",
    )
    compile_result = _compile_result(
        tmp_path,
        command="xrun -f design.f -top bind_top -top tb",
        sources=(child, top),
        tops=("bind_top", "tb"),
    )

    plan = _plan(tmp_path, compile_result)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(child.resolve()), str(top.resolve()))
    assert "-D" in manifest.ordered_options
    assert "FEATURE=1" in manifest.ordered_options
    assert manifest.ordered_tops == ("bind_top", "tb")
    assert manifest.complete is True
    assert plan.request.scope.hierarchy_ancestors == ("tb", "tb.dut")
    assert plan.request.scope.requested_cone.instance_paths == ("tb", "tb.dut")
    assert plan.request.scope.coverage_boundary.instance_paths == ("tb", "tb.dut")
    assert plan.request.scope.coverage_boundary.objective_exclusions == (
        "bind_semantics",
    )
    assert compute_source_graph_build_key(plan.request).cross_request_reusable is True
    receipt = plan.receipt.to_dict()
    assert receipt["adapter_version"] == SOURCE_GRAPH_ADAPTER_VERSION
    assert receipt["manifest"]["complete"] is True
    assert receipt["cross_request_reusable"] is True


def test_plusargs_with_hdl_suffix_values_are_not_misfiled_as_sources(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command=(
            "xrun +define+ROM_CODE_MEM=/some/where/ROM_CODE.v "
            "+define+MODEL_SV=/lib/model.sv "
            "+define+DPI_MODEL=/lib/model.so +libext+.v+.sv "
            "top.sv -top tb"
        ),
        sources=(source,),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(source.resolve()),)
    assert "+define+ROM_CODE_MEM=/some/where/ROM_CODE.v" in (
        manifest.ordered_options
    )
    assert "+define+MODEL_SV=/lib/model.sv" in manifest.ordered_options
    assert "+define+DPI_MODEL=/lib/model.so" in manifest.ordered_options
    assert "+libext+.v+.sv" in manifest.ordered_options
    assert not any("+define+" in item for item in manifest.ordered_inputs)
    assert "native_runtime_input_excluded" not in plan.receipt.gap_codes
    assert manifest.complete is True


def test_dash_options_with_hdl_suffix_values_are_not_misfiled_as_sources(tmp_path):
    source = tmp_path / "top.sv"
    library = tmp_path / "lib" / "cells.v"
    _write(source, "module tb; logic q; endmodule\n")
    _write(library, "module cells; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command=(
            "xrun -DMODEL_SV=/lib/model.sv -vlib/cells.v "
            "-xprop=config/mode.v top.sv -top tb"
        ),
        sources=(source,),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(source.resolve()),)
    assert "-DMODEL_SV=/lib/model.sv" in manifest.ordered_options
    library_option = manifest.ordered_options.index("-v")
    assert manifest.ordered_options[library_option + 1] == str(library.resolve())
    assert not any(item.startswith("-") for item in manifest.ordered_inputs)
    assert manifest.complete is True


def test_frontier_plan_admits_only_proved_direct_sibling_scope(tmp_path):
    source = tmp_path / "top.sv"
    _write(
        source,
        "module tb; logic q; endmodule\n",
    )
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    plan = _frontier_plan(tmp_path, compile_result)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.request.scope.hierarchy_ancestors == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
    )
    artifact = plan.request.artifact_identity.scope
    assert artifact.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
        "tb.dut.u_source",
    )
    assert artifact.proved_ancestor_chains == (
        ("tb", "tb.dut"),
        ("tb", "tb.dut", "tb.dut.u_leaf"),
        ("tb", "tb.dut", "tb.dut.u_source"),
    )
    assert plan.receipt.scope_kind == "single_endpoint_expanded"
    assert plan.receipt.lca_depth == 1


def test_frontier_plan_blocks_before_exceeding_direct_child_cap(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    plan = _frontier_plan(tmp_path, compile_result, max_instances=1)

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "frontier_instance_limit"


def test_native_xrun_replay_avoids_filelist_echo_duplication_and_resolves_uvmhome(
    tmp_path,
):
    user_source = tmp_path / "rtl" / "top.sv"
    native_source = tmp_path / "dpi" / "helper.c"
    filelist = tmp_path / "design.scr"
    uvmhome = tmp_path / "xcelium" / "UVM" / "CDNS-1.2"
    uvm_pkg = uvmhome / "sv" / "src" / "uvm_pkg.sv"
    cdns_uvm_pkg = uvmhome / "additions" / "sv" / "cdns_uvm_pkg.sv"
    _write(user_source, "module tb; logic q; endmodule\n")
    _write(native_source, "void helper(void) {}\n")
    _write(uvm_pkg, "package uvm_pkg; endpackage\n")
    _write(cdns_uvm_pkg, "package cdns_uvm_pkg; endpackage\n")
    _write(filelist, "rtl/top.sv\ndpi/helper.c\n")
    log = tmp_path / "compile.log"
    _write(
        log,
        "xrun\n"
        "\t-ALLOWREDEFINITION\n"
        "\t-f design.scr\n"
        "\t\trtl/top.sv\n"
        "\t\tdpi/helper.c\n"
        "\t-uvmhome CDNS-1.2\n"
        "\t-top bind_top\n"
        "\t-top tb\n"
        f"Compiling UVM packages (uvm_pkg.sv cdns_uvm_pkg.sv) using uvmhome location {uvmhome}\n"
        "file: rtl/top.sv\n"
        "\tmodule worklib.tb:sv\n",
    )
    compile_result = parse_compile_log(str(log), "xcelium")

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
        hierarchy={"component_tree": {"tb": {}}},
    )

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (
        str(uvm_pkg.resolve()),
        str(cdns_uvm_pkg.resolve()),
        str(user_source.resolve()),
    )
    assert manifest.ordered_tops == ("bind_top", "tb")
    assert manifest.complete is True
    assert plan.receipt.cross_request_reusable is True
    assert "simulator_uvm_library_unresolved" not in plan.receipt.gap_codes
    assert set(plan.receipt.gap_codes) == {
        "definition_replacement_semantics",
        "native_runtime_input_excluded",
    }
    assert {
        "bind_semantics",
        "definition_replacement_semantics",
        "dpi_runtime",
        "uvm_dynamic_connectivity",
    } <= set(plan.receipt.objective_exclusions)


def test_content_fingerprint_changes_when_an_input_changes(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    first = _plan(tmp_path, compile_result, signal_path="tb.q")
    _write(source, "module tb; logic q; assign q = 1'b1; endmodule\n")
    _write(tmp_path / "compile.log", "xrun top.sv -top tb\nrebuilt\n")
    second = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert first.request is not None and second.request is not None
    first_fingerprint = first.request.identity.compile_inputs.fingerprint
    second_fingerprint = second.request.identity.compile_inputs.fingerprint
    assert first_fingerprint is not None and second_fingerprint is not None
    assert first_fingerprint != second_fingerprint
    assert len(first_fingerprint) == 64
    assert int(first_fingerprint, 16) >= 0


def test_content_fingerprint_cache_hits_and_invalidates_on_rebuild(
    monkeypatch, tmp_path
):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    original = source_graph_adapter._hash_file_and_scan
    calls = 0

    def tracked_hash(path, exclusions):
        nonlocal calls
        calls += 1
        return original(path, exclusions)

    monkeypatch.setattr(source_graph_adapter, "_hash_file_and_scan", tracked_hash)

    first = _plan(tmp_path, compile_result, signal_path="tb.q")
    first_calls = calls
    second = _plan(tmp_path, compile_result, signal_path="tb.q")
    _write(source, "module tb; logic q; assign q = 1'b1; endmodule\n")
    _write(tmp_path / "compile.log", "xrun top.sv -top tb\nrebuilt\n")
    third = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert first.receipt.fingerprint_cache_disposition == "miss"
    assert second.receipt.fingerprint_cache_disposition == "hit_session_snapshot"
    assert third.receipt.fingerprint_cache_disposition == "miss"
    assert first_calls == 1
    assert calls == 2
    assert first.request is not None and second.request is not None
    assert third.request is not None
    assert (
        first.request.identity.compile_inputs.fingerprint
        == second.request.identity.compile_inputs.fingerprint
    )
    assert (
        third.request.identity.compile_inputs.fingerprint
        != first.request.identity.compile_inputs.fingerprint
    )


def test_unclassified_option_forbids_cross_request_exact_reuse(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun -mystery-mode top.sv -top tb",
        sources=(source,),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.inputs_complete is True
    assert manifest.options_complete is False
    assert plan.receipt.cross_request_reusable is False
    assert "compile_option_unclassified" in plan.receipt.gap_codes
    assert (
        "unclassified_compile_option"
        in plan.request.scope.coverage_boundary.objective_exclusions
    )
    assert (
        "compile_manifest_incomplete"
        in plan.request.scope.coverage_boundary.objective_exclusions
    )


def test_common_vcs_runtime_options_preserve_exact_source_manifest(tmp_path):
    source = tmp_path / "rtl" / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command=(
            "vcs -full64 -sverilog -Mdir=simv.csrc "
            "-ntb_opts uvm-1.2 -assert svaext -xlrm uniq_prior_final "
            "'-Xcflags=-Wno-error=implicit-function-declaration "
            "-Wno-error=int-conversion' -Wl,--no-as-needed "
            "+warn=SV-NFIVC -error=IPDW -deraceclockdata "
            "-xprop=config/xprop.cfg -xprop=mmsopt rtl/top.sv -top tb"
        ),
        sources=(source,),
    )
    compile_result["simulator"] = "vcs"

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.complete is True
    assert plan.receipt.cross_request_reusable is True
    assert "compile_option_unclassified" not in plan.receipt.gap_codes
    assert set(plan.receipt.gap_codes) == {"native_runtime_input_excluded"}
    assert {"dpi_runtime", "uvm_dynamic_connectivity"} <= set(
        plan.receipt.objective_exclusions
    )


def test_dotted_symbol_suffix_is_deferred_to_exact_ir_resolution(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.missing.q",
        hierarchy={"component_tree": {"tb": {}}},
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.request.scope.hierarchy_ancestors == ("tb",)
    assert plan.request.scope.target.instance_path == "tb"
    assert plan.request.scope.target.signal_path == "tb.missing.q"


def test_scope_resolution_does_not_enumerate_siblings(tmp_path):
    class NoIterationDict(dict):
        def __iter__(self):  # pragma: no cover - failure path is the assertion
            raise AssertionError("scope adapter enumerated hierarchy siblings")

        def items(self):  # pragma: no cover - failure path is the assertion
            raise AssertionError("scope adapter enumerated hierarchy siblings")

        def values(self):  # pragma: no cover - failure path is the assertion
            raise AssertionError("scope adapter enumerated hierarchy siblings")

    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    siblings = NoIterationDict(
        {
            "dut": {"module": "dut", "children": {}},
            "unrelated": {"module": "other", "children": {}},
        }
    )

    plan = _plan(
        tmp_path,
        compile_result,
        hierarchy={"component_tree": {"tb": siblings}},
    )

    assert plan.request is not None
    assert plan.request.scope.coverage_boundary.instance_paths == ("tb", "tb.dut")


def test_objective_exclusions_are_retained_in_scope_receipt(tmp_path):
    source = tmp_path / "top.sv"
    _write(
        source,
        """
        `pragma protect begin_protected
        import "DPI-C" function void call_c();
        module tb;
          uvm_component c;
          initial force q = 1'b0;
          bind tb checker b();
          logic q;
        endmodule
        """,
    )
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    exclusions = set(plan.request.scope.coverage_boundary.objective_exclusions)
    assert {
        "uvm_dynamic_connectivity",
        "dpi_runtime",
        "procedural_force_release",
        "bind_semantics",
        "protected_region",
    } <= exclusions
    assert plan.receipt.objective_exclusions == tuple(sorted(exclusions))


def test_pre_cancelled_adapter_stops_without_building_a_fallback_plan(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    event = threading.Event()
    event.set()
    token = cancellation.push_cancel_event(event)
    try:
        with pytest.raises(cancellation.OperationCancelled):
            _plan(tmp_path, compile_result, signal_path="tb.q")
    finally:
        cancellation.pop_cancel_event(token)


def test_path_scope_same_instance_and_parent_child_are_proved(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = _hierarchy()

    same = _path_plan(
        tmp_path,
        compile_result,
        from_signal="tb.dut.a",
        to_signal="tb.dut.b",
        hierarchy=hierarchy,
    )
    parent_child = _path_plan(
        tmp_path,
        compile_result,
        from_signal="tb.dut.a",
        to_signal="tb.dut.u_leaf.q",
        hierarchy=hierarchy,
    )

    assert same.request is not None
    assert same.request.scope.path_hierarchy.lca == "tb.dut"
    assert same.request.scope.hierarchy_ancestors == ("tb", "tb.dut")
    assert parent_child.request is not None
    assert parent_child.request.scope.path_hierarchy.lca == "tb.dut"
    assert parent_child.request.scope.hierarchy_ancestors == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
    )


def test_path_sibling_scope_uses_only_proved_ancestor_union_and_lca(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    plan = _path_plan(tmp_path, compile_result, expand_assigns=True)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    target = plan.request.scope.target
    assert isinstance(target, ConnectivityPathTarget)
    assert target.from_instance_path == "tb.dut.u_left"
    assert target.to_instance_path == "tb.dut.u_right"
    assert target.expand_assigns is True
    assert plan.request.scope.path_hierarchy.lca == "tb.dut"
    assert plan.request.scope.hierarchy_ancestors == (
        "tb",
        "tb.dut",
        "tb.dut.u_left",
        "tb.dut.u_right",
    )
    assert plan.request.scope.requested_cone.instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.u_left",
        "tb.dut.u_right",
    )
    receipt = plan.receipt.to_dict()["scope"]
    assert receipt == {
        "kind": "dual_endpoint_path",
        "endpoint_count": 2,
        "ancestor_count": 4,
        "lca_depth": 1,
        "requested_cone_instance_count": 4,
        "coverage_boundary_instance_count": 4,
        "objective_exclusions": [],
    }


def test_path_different_top_is_blocked_and_dotted_suffix_is_deferred(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb_a; endmodule module tb_b; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb_a -top tb_b",
        sources=(source,),
        tops=("tb_a", "tb_b"),
    )

    different_top = _path_plan(
        tmp_path,
        compile_result,
        from_signal="tb_a.a",
        to_signal="tb_b.b",
        hierarchy={"component_tree": {"tb_a": {}, "tb_b": {}}},
        top_hint=None,
    )
    missing = _path_plan(
        tmp_path,
        compile_result,
        from_signal="tb_a.missing.a",
        to_signal="tb_a.b",
        hierarchy={"component_tree": {"tb_a": {}}},
        top_hint="tb_a",
    )

    assert different_top.receipt.blocker.code == "path_endpoint_top_mismatch"
    assert missing.status is AdapterStatus.READY
    assert missing.request is not None
    assert missing.request.scope.path_hierarchy is not None
    assert missing.request.scope.path_hierarchy.from_ancestors == ("tb_a",)
    assert different_top.receipt.to_dict()["scope"]["endpoint_count"] == 2


def test_path_scope_does_not_enumerate_unrelated_siblings(tmp_path):
    class NoIterationDict(dict):
        def __iter__(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("path adapter enumerated siblings")

        def items(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("path adapter enumerated siblings")

        def values(self):  # pragma: no cover - failure is the assertion
            raise AssertionError("path adapter enumerated siblings")

    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    children = NoIterationDict(
        {
            "u_left": {"module": "left", "children": {}},
            "u_right": {"module": "right", "children": {}},
            "unrelated": {"module": "other", "children": {}},
        }
    )
    top_children = NoIterationDict(
        {
            "dut": {"module": "dut", "children": children},
            "unrelated_top": {"module": "other", "children": {}},
        }
    )

    plan = _path_plan(
        tmp_path,
        compile_result,
        hierarchy={"component_tree": {"tb": top_children}},
    )

    assert plan.request is not None
    assert "tb.dut.unrelated" not in plan.request.scope.hierarchy_ancestors
    assert "tb.unrelated_top" not in plan.request.scope.hierarchy_ancestors


def test_path_artifact_key_ignores_endpoint_and_expand_while_query_key_changes(
    tmp_path,
):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top bind_top -top tb",
        sources=(source,),
        tops=("bind_top", "tb"),
    )

    baseline = _path_plan(tmp_path, compile_result)
    changed_endpoint = _path_plan(
        tmp_path,
        compile_result,
        to_signal="tb.dut.u_right.other",
    )
    changed_expand = _path_plan(tmp_path, compile_result, expand_assigns=True)

    assert baseline.request is not None
    assert changed_endpoint.request is not None
    assert changed_expand.request is not None
    keys = {
        compute_source_graph_build_key(plan.request).digest
        for plan in (baseline, changed_endpoint, changed_expand)
    }
    query_keys = {
        compute_source_graph_query_key(plan.request.query_identity).digest
        for plan in (baseline, changed_endpoint, changed_expand)
    }
    assert len(keys) == 1
    assert len(query_keys) == 3
    assert baseline.request.identity.compile_inputs.ordered_tops == (
        "bind_top",
        "tb",
    )
    assert baseline.receipt.fingerprint_cache_disposition == "miss"
    assert changed_endpoint.receipt.fingerprint_cache_disposition == (
        "hit_session_snapshot"
    )
    assert changed_expand.receipt.fingerprint_cache_disposition == (
        "hit_session_snapshot"
    )
