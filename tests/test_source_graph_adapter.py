from __future__ import annotations

from pathlib import Path
import threading

import pytest

import src.cancellation as cancellation
from src.compile_log_parser import parse_compile_log
from src.source_graph_adapter import (
    AdapterStatus,
    SOURCE_GRAPH_ADAPTER_VERSION,
    build_source_graph_plan,
)
from src.source_graph_contract import QueryOperation, compute_source_graph_build_key


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
        operation=QueryOperation.DRIVER,
        signal_path=signal_path,
        top_hint="tb",
        max_hops=8,
        frontend_version="11.0.0",
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
    assert plan.request.scope.requested_cone.instance_paths == ("tb.dut",)
    assert plan.request.scope.coverage_boundary.instance_paths == ("tb", "tb.dut")
    assert plan.request.scope.coverage_boundary.objective_exclusions == (
        "bind_semantics",
    )
    assert compute_source_graph_build_key(plan.request).cross_request_reusable is True
    receipt = plan.receipt.to_dict()
    assert receipt["adapter_version"] == SOURCE_GRAPH_ADAPTER_VERSION
    assert receipt["manifest"]["complete"] is True
    assert receipt["cross_request_reusable"] is True


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
    second = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert first.request is not None and second.request is not None
    first_fingerprint = first.request.identity.compile_inputs.fingerprint
    second_fingerprint = second.request.identity.compile_inputs.fingerprint
    assert first_fingerprint is not None and second_fingerprint is not None
    assert first_fingerprint != second_fingerprint
    assert len(first_fingerprint) == 64
    assert int(first_fingerprint, 16) >= 0


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


def test_unprovable_hierarchy_scope_is_a_structured_blocker(tmp_path):
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

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.request is None
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "hierarchy_scope_unresolved"
    assert plan.receipt.to_dict()["blocker"] == {
        "code": "hierarchy_scope_unresolved",
        "stage": "target_scope",
    }


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
