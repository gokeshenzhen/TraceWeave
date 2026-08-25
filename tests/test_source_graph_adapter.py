from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

import src.cancellation as cancellation
import src.source_graph_adapter as source_graph_adapter
from src.compile_log_parser import merge_compile_results, parse_compile_log
from src.hierarchy_handles import compute_snapshot_fingerprint
from src.source_graph_adapter import (
    AdapterStatus,
    SOURCE_GRAPH_ADAPTER_VERSION,
    build_source_graph_frontier_plan,
    build_source_graph_initial_plan,
    build_source_graph_path_plan,
    build_source_graph_plan,
)
from src.tb_hierarchy_builder import build_hierarchy
from src.source_graph_contract import (
    ConnectivityPathTarget,
    QueryOperation,
    SourceGraphSemanticContext,
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
    runtime_plusarg_allowlist: frozenset[str] = frozenset(),
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
        runtime_plusarg_allowlist=runtime_plusarg_allowlist,
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
    semantic_context: SourceGraphSemanticContext | None = None,
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
        semantic_context=semantic_context,
    )


def _large_adjacent_plan(
    tmp_path: Path,
    *,
    producer_count: int = 1,
    recursive: bool = True,
    max_hops: int = 8,
    operation: QueryOperation = QueryOperation.DRIVER,
    allow_adjacent: bool = True,
    enable_semantic_context: bool = False,
    semantic_context_max_instances: int = 64,
    semantic_context_max_inputs: int = 256,
):
    leaf = tmp_path / "leaf.sv"
    dut = tmp_path / "dut.sv"
    top = tmp_path / "top.sv"
    _write(leaf, "module leaf(input logic data_i); endmodule\n")
    producer_paths = tuple(
        tmp_path / f"producer_{index}.sv" for index in range(producer_count)
    )
    for index, path in enumerate(producer_paths):
        _write(
            path,
            f"module producer_{index}(output logic data_o); "
            "assign data_o = 1'b0; endmodule\n",
        )
    instances = "\n".join(
        f"producer_{index} u_source_{index}(.data_o(link_{index}));"
        for index in range(producer_count)
    )
    links = "\n".join(
        f"logic link_{index};" for index in range(producer_count)
    )
    _write(
        dut,
        "module dut;\n"
        f"{links}\n"
        f"{instances}\n"
        "leaf u_leaf(.data_i(link_0));\n"
        "endmodule\n",
    )
    _write(top, "module tb; dut dut(); endmodule\n")
    target_total = 80
    unused_count = target_total - len(producer_paths) - 3
    unused = tuple(tmp_path / f"unused_{index}.sv" for index in range(unused_count))
    for index, path in enumerate(unused):
        _write(path, f"module unused_{index}; endmodule\n")
    sources = (leaf, *producer_paths, dut, top, *unused)
    compile_result = _compile_result(
        tmp_path,
        command=f"xrun {' '.join(str(path) for path in sources)} -top tb",
        sources=sources,
    )
    hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)
    return build_source_graph_initial_plan(
        compile_log=str(tmp_path / "compile.log"),
        compile_result=hierarchy["compile_result"],
        hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(
            str(tmp_path / "compile.log"), "xcelium"
        ),
        operation=operation,
        signal_path="tb.dut.u_leaf.data_i",
        top_hint="tb",
        max_hops=max_hops,
        frontend_version="11.0.0",
        recursive=recursive,
        allow_adjacent=allow_adjacent,
        enable_semantic_context=enable_semantic_context,
        semantic_context_max_instances=semantic_context_max_instances,
        semantic_context_max_inputs=semantic_context_max_inputs,
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


def test_large_complete_manifest_projects_hierarchy_dependency_closure(tmp_path):
    dut = tmp_path / "rtl" / "dut.sv"
    top = tmp_path / "tb" / "top.sv"
    _write(dut, "module dut; logic [3:0] q; endmodule\n")
    _write(top, "module tb; dut dut(); endmodule\n")
    unrelated = tuple(tmp_path / "rtl" / f"unused_{index}.sv" for index in range(68))
    for index, path in enumerate(unrelated):
        _write(path, f"module unused_{index}; endmodule\n")
    sources = (dut, top, *unrelated)
    compile_result = _compile_result(
        tmp_path,
        command=f"xrun {' '.join(str(path) for path in sources)} -top tb",
        sources=sources,
    )
    hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)

    plan = _plan(tmp_path, compile_result, hierarchy=hierarchy)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    projection = plan.request.artifact_identity.compile_projection
    assert projection is not None
    assert projection.ordered_inputs == (str(dut.resolve()), str(top.resolve()))
    assert projection.full_input_count == 70
    assert "compile_projection_pruned_inputs" in plan.receipt.gap_codes
    assert "compile_projection_pruned_inputs" in (
        plan.request.artifact_identity.scope.coverage_boundary.objective_exclusions
    )
    receipt = plan.receipt.to_dict()["manifest"]["compile_projection"]
    assert receipt == {
        "mode": "hierarchy_dependency_closure",
        "input_count": 2,
        "excluded_input_count": 68,
        "seed_symbol_count": 2,
        "dependency_symbol_count": 0,
        "fallback_reason": None,
    }


def test_merged_source_phases_replay_options_without_fictional_command(tmp_path):
    first = tmp_path / "rtl" / "first.sv"
    second = tmp_path / "rtl" / "second.sv"
    _write(first, "module dut; logic [3:0] q; endmodule\n")
    _write(second, "module tb; dut dut(); endmodule\n")
    compile_log = tmp_path / "compile.log"
    supplement_log = tmp_path / "compile_second.log"
    elaborate_log = tmp_path / "elab.log"
    for path in (compile_log, supplement_log, elaborate_log):
        _write(path, path.name + "\n")

    def phase(command: str, source: Path | None, top: str | None = None) -> dict:
        units = (
            [{"path": str(source), "type": "module", "role": "project"}]
            if source is not None
            else []
        )
        return {
            "simulator": "vcs",
            "compile_cwd": str(tmp_path),
            "primary_top": top,
            "top_modules": [top] if top else [],
            "files": {
                "user": (
                    [
                        {
                            "path": str(source),
                            "type": "module",
                            "category": "rtl",
                        }
                    ]
                    if source is not None
                    else []
                ),
                "filtered_count": 0,
            },
            "include_tree": {},
            "filelist_tree": {},
            "interfaces": [],
            "compile_command": command,
            "parse_warnings": [],
            "compile_evidence": {
                "schema_version": 1,
                "unit_order_source": (
                    "simulator_log" if source is not None else "unavailable"
                ),
                "ordered_compilation_units": units,
                "ordered_includes": [],
                "filelists": [],
                "expanded_replay_command": None,
            },
        }

    merged = merge_compile_results(
        phase(f"vlogan +define+FIRST=1 {first}", first),
        [
            phase(f"vlogan +define+SECOND=1 {second}", second),
            phase("vcs -top tb", None, "tb"),
        ],
        primary_log=str(compile_log),
        supplementary_logs=[str(supplement_log), str(elaborate_log)],
    )

    plan = _plan(tmp_path, merged)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(first.resolve()), str(second.resolve()))
    assert "+define+FIRST=1" in manifest.ordered_options
    assert "+define+SECOND=1" in manifest.ordered_options
    assert manifest.ordered_tops == ("tb",)
    assert manifest.complete is False
    assert "phase_local_compile_options_unmodeled" in plan.receipt.gap_codes
    assert "phase_local_compile_options_unmodeled" in (
        plan.receipt.objective_exclusions
    )


def test_vhdl_inputs_are_content_identified_without_blocking_sv_manifest(tmp_path):
    sv = tmp_path / "top.sv"
    vhdl = tmp_path / "leaf.vhd"
    _write(sv, "module tb; logic q; endmodule\n")
    _write(
        vhdl,
        "entity leaf is port(i : in bit); end entity;\n"
        "architecture rtl of leaf is begin end architecture;\n",
    )
    compile_result = _compile_result(
        tmp_path,
        command=f"xrun -sv {sv} {vhdl} -top tb",
        sources=(sv, vhdl),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(sv.resolve()), str(vhdl.resolve()))
    assert manifest.complete is True
    assert "compile_option_unclassified" not in plan.receipt.gap_codes
    assert "opaque_vhdl_boundary" in plan.receipt.gap_codes
    assert "opaque_vhdl_boundary" in plan.receipt.objective_exclusions


def test_split_vhdlan_phase_does_not_replay_vhdl_only_options_into_slang(tmp_path):
    sv = tmp_path / "top.sv"
    vhdl = tmp_path / "leaf.vhd"
    compile_log = tmp_path / "sv_compile.log"
    vhdl_log = tmp_path / "vhdl_compile.log"
    elaborate_log = tmp_path / "elaborate.log"
    _write(sv, "module tb; logic q; endmodule\n")
    _write(vhdl, "entity leaf is port(i : in bit); end entity;\n")
    _write(
        compile_log,
        "Chronologic VCS simulator\n"
        f"Command: vlogan -sverilog {sv}\n"
        f"Parsing design file '{sv}'\n",
    )
    _write(
        vhdl_log,
        "Chronologic VCS simulator\n"
        f"Command: vhdlan -vhdl_vendor_semantics {vhdl}\n"
        f"Parsing design file '{vhdl}'\n",
    )
    _write(
        elaborate_log,
        "Chronologic VCS simulator\n"
        "Command: vcs -top tb\n"
        "Top Level Modules:\n"
        "       tb\n",
    )
    primary = parse_compile_log(str(compile_log), "vcs")
    supplements = [
        parse_compile_log(str(vhdl_log), "vcs"),
        parse_compile_log(str(elaborate_log), "vcs"),
    ]
    merged = merge_compile_results(
        primary,
        supplements,
        primary_log=str(compile_log),
        supplementary_logs=[str(vhdl_log), str(elaborate_log)],
    )

    plan = _plan(tmp_path, merged, signal_path="tb.q")

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(sv.resolve()), str(vhdl.resolve()))
    assert "-vhdl_vendor_semantics" not in manifest.ordered_options
    assert manifest.complete is True
    assert "opaque_vhdl_boundary" in plan.receipt.objective_exclusions


def test_command_recovered_vhdlan_input_stays_in_content_identity(tmp_path):
    sv = tmp_path / "top.sv"
    vhdl = tmp_path / "leaf.vhd"
    compile_log = tmp_path / "sv_compile.log"
    vhdl_log = tmp_path / "vhdl_compile.log"
    elaborate_log = tmp_path / "elaborate.log"
    _write(sv, "module tb; logic q; endmodule\n")
    _write(vhdl, "entity leaf is port(i : in bit); end entity;\n")
    _write(
        compile_log,
        "Chronologic VCS simulator\n"
        f"Command: vlogan -sverilog {sv}\n"
        f"Parsing design file '{sv}'\n",
    )
    # Some vhdlan transcripts preserve only the invocation, so source order is
    # conservatively command-recovered rather than simulator-log proved.
    _write(
        vhdl_log,
        "Chronologic VCS simulator\n"
        f"Command: vhdlan -work WORK {vhdl}\n",
    )
    _write(
        elaborate_log,
        "Chronologic VCS simulator\n"
        "Command: vcs -top tb\n"
        "Top Level Modules:\n"
        "       tb\n",
    )
    merged = merge_compile_results(
        parse_compile_log(str(compile_log), "vcs"),
        [
            parse_compile_log(str(vhdl_log), "vcs"),
            parse_compile_log(str(elaborate_log), "vcs"),
        ],
        primary_log=str(compile_log),
        supplementary_logs=[str(vhdl_log), str(elaborate_log)],
    )

    plan = _plan(tmp_path, merged, signal_path="tb.q")

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(sv.resolve()), str(vhdl.resolve()))
    assert manifest.fingerprint is not None
    assert manifest.complete is False
    assert "compile_log_parse_warning" in plan.receipt.gap_codes
    assert "opaque_vhdl_boundary" in plan.receipt.objective_exclusions
    assert plan.receipt.cross_request_reusable is False


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
    assert "+define+ROM_CODE_MEM=/some/where/ROM_CODE.v" in (manifest.ordered_options)
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


def test_filelist_c_style_comments_are_ignored_without_corrupting_quotes(tmp_path):
    source = tmp_path / "top.sv"
    live_library = tmp_path / "live_mem.v"
    old_line_library = tmp_path / "old_line_mem.v"
    old_block_library = tmp_path / "old_block_mem.v"
    for path in (source, live_library, old_line_library, old_block_library):
        _write(path, "module placeholder; endmodule\n")
    _write(
        tmp_path / "design.f",
        "// -v old_line_mem.v\n"
        "/* retired library:\n"
        "-v old_block_mem.v\n"
        "+define+OLD_PATH=/retired/model.v\n"
        "*/\n"
        "+define+DOC_URL='http://intranet/spec'\n"
        "+define+TEXT='a/*literal*/b'\n"
        "-v live_mem.v \\\n"
        "top.sv // active top\n",
    )
    compile_result = _compile_result(
        tmp_path,
        command="xrun -f design.f -top tb",
        sources=(source,),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(source.resolve()),)
    live_option = manifest.ordered_options.index("-v")
    assert manifest.ordered_options[live_option + 1] == str(live_library.resolve())
    assert "+define+DOC_URL=http://intranet/spec" in manifest.ordered_options
    assert "+define+TEXT=a/*literal*/b" in manifest.ordered_options
    assert not any("old_line_mem" in item for item in manifest.ordered_options)
    assert not any("old_block_mem" in item for item in manifest.ordered_options)
    assert not any("OLD_PATH" in item for item in manifest.ordered_options)
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


def test_frontier_plan_reuses_covering_semantic_context(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    expanded = _frontier_plan(tmp_path, compile_result)
    assert expanded.request is not None
    context = SourceGraphSemanticContext(
        scope=expanded.request.artifact_identity.scope,
    )

    reused = _frontier_plan(
        tmp_path,
        compile_result,
        semantic_context=context,
    )

    assert reused.request is not None
    assert reused.request.artifact_identity.semantic_context == context
    assert reused.receipt.semantic_context_status == "reused"
    assert reused.receipt.semantic_context_instance_count == 4
    assert reused.receipt.semantic_context_input_count == 1


def test_frontier_plan_drops_semantic_context_that_does_not_cover_expansion(
    tmp_path,
):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    exact = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.dut.u_leaf.data_i",
    )
    assert exact.request is not None
    narrow_context = SourceGraphSemanticContext(
        scope=exact.request.artifact_identity.scope,
    )

    expanded = _frontier_plan(
        tmp_path,
        compile_result,
        semantic_context=narrow_context,
    )

    assert expanded.request is not None
    assert expanded.request.artifact_identity.semantic_context is None
    assert expanded.receipt.semantic_context_status == "context_not_covering"


def test_large_deep_query_selects_bounded_adjacent_initial_scope(tmp_path):
    plan = _large_adjacent_plan(tmp_path)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.receipt.scope_kind == "single_endpoint_expanded"
    assert plan.request.scope.hierarchy_ancestors == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
    )
    assert plan.request.artifact_identity.scope.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
        "tb.dut.u_source_0",
    )
    assert plan.scope_expansion_anchors == (
        "tb.dut.__traceweave_initial_scope__",
    )
    projection = plan.request.artifact_identity.compile_projection
    assert projection is not None
    assert len(projection.ordered_inputs) == 4


def test_large_shallow_query_keeps_exact_ancestor_initial_scope(tmp_path):
    plan = _large_adjacent_plan(tmp_path, recursive=False, max_hops=1)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.receipt.scope_kind == "single_endpoint"
    assert plan.request.artifact_identity.scope.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
    )
    assert plan.scope_expansion_anchors == ()


def test_large_deep_load_selects_bounded_adjacent_initial_scope(tmp_path):
    plan = _large_adjacent_plan(
        tmp_path,
        operation=QueryOperation.LOADS,
        recursive=False,
        max_hops=2,
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.receipt.scope_kind == "single_endpoint_expanded"
    assert "tb.dut.u_source_0" in (
        plan.request.artifact_identity.scope.projection_instance_paths
    )


def test_initial_scope_policy_can_disable_adjacent_expansion(tmp_path):
    plan = _large_adjacent_plan(tmp_path, allow_adjacent=False)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.receipt.scope_kind == "single_endpoint"
    assert plan.scope_expansion_anchors == ()


def test_large_deep_query_rejects_costly_adjacent_compile_growth(tmp_path):
    plan = _large_adjacent_plan(tmp_path, producer_count=25)

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.receipt.scope_kind == "single_endpoint"
    assert plan.request.artifact_identity.scope.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
    )
    projection = plan.request.artifact_identity.compile_projection
    assert projection is not None
    assert len(projection.ordered_inputs) == 3
    assert plan.scope_expansion_anchors == ()


def test_large_deep_query_attaches_bounded_semantic_context(tmp_path):
    plan = _large_adjacent_plan(
        tmp_path,
        producer_count=36,
        enable_semantic_context=True,
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    artifact = plan.request.artifact_identity
    context = artifact.semantic_context
    assert context is not None
    assert artifact.scope.projection_instance_paths == (
        "tb",
        "tb.dut",
        "tb.dut.u_leaf",
    )
    assert len(context.scope.projection_instance_paths) == 39
    assert context.compile_projection is not None
    assert len(context.compile_projection.ordered_inputs) == 39
    assert plan.receipt.semantic_context_status == "selected"
    assert plan.receipt.semantic_context_instance_count == 39
    assert plan.receipt.semantic_context_input_count == 39
    assert plan.scope_expansion_anchors == ()


def test_semantic_context_respects_instance_and_input_budgets(tmp_path):
    instance_limited = _large_adjacent_plan(
        tmp_path,
        producer_count=40,
        enable_semantic_context=True,
        semantic_context_max_instances=32,
    )
    input_limited = _large_adjacent_plan(
        tmp_path,
        producer_count=25,
        enable_semantic_context=True,
        semantic_context_max_inputs=20,
    )

    assert instance_limited.request is not None
    assert instance_limited.request.artifact_identity.semantic_context is None
    assert instance_limited.receipt.semantic_context_status == "instance_limit"
    assert input_limited.request is not None
    assert input_limited.request.artifact_identity.semantic_context is None
    assert input_limited.receipt.semantic_context_status == "input_limit"


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


def test_vcs_uniquely_infers_nested_filelist_environment_and_validates_order(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    outer = project / "lists" / "outer.f"
    nested = project / "lists" / "nested.f"
    package = project / "rtl" / "pkg.sv"
    core = project / "rtl" / "core.sv"
    top = project / "tb" / "top.sv"
    for path, text in (
        (package, "package design_pkg; endpackage\n"),
        (core, "module core; endmodule\n"),
        (top, "module tb; core u_core(); endmodule\n"),
    ):
        _write(path, text)
    _write(
        outer,
        "$TW_PROJECT_ROOT/rtl/pkg.sv\n-f $TW_PROJECT_ROOT/lists/nested.f\n",
    )
    _write(
        nested,
        "$TW_RTL_ROOT/core.sv\n$TW_PROJECT_ROOT/tb/top.sv\n",
    )
    log = tmp_path / "compile.log"
    _write(
        log,
        "Chronologic VCS simulator\n"
        f"Command: vcs -f {outer} -top tb\n"
        f"Parsing design file '{package}'\n"
        f"Parsing design file '{core}'\n"
        f"Parsing design file '{top}'\n",
    )
    monkeypatch.setenv("TW_PROJECT_ROOT", str(tmp_path / "stale_project"))
    monkeypatch.setenv("TW_RTL_ROOT", str(tmp_path / "stale_rtl"))

    compile_result = parse_compile_log(str(log), "vcs")
    plan = _plan(tmp_path, compile_result, signal_path="tb.u_core.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (
        str(package.resolve()),
        str(core.resolve()),
        str(top.resolve()),
    )
    assert manifest.complete is True
    assert plan.receipt.cross_request_reusable is True
    assert "compile_environment_inferred_from_log" in plan.receipt.gap_codes
    assert "compile_environment_unresolved" not in plan.receipt.gap_codes
    assert "compile_inputs_recovered_from_simulator_log" not in plan.receipt.gap_codes
    assert "compile_log_parse_warning" not in plan.receipt.gap_codes


def test_vcs_manifest_preserves_embedded_double_slash_paths(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    outer = project / "lists" / "outer.f"
    nested = project / "lists" / "nested.f"
    source = project / "rtl" / "top.sv"
    _write(source, "module tb; endmodule\n")
    _write(
        outer,
        "-f $TW_MANIFEST_DOUBLE_SLASH_ROOT//lists/nested.f\n",
    )
    _write(
        nested,
        "$TW_MANIFEST_DOUBLE_SLASH_ROOT//rtl/top.sv\n",
    )
    monkeypatch.setenv("TW_MANIFEST_DOUBLE_SLASH_ROOT", str(project))
    log = tmp_path / "compile.log"
    _write(
        log,
        "Chronologic VCS simulator\n"
        f"Command: vcs -f {outer} -top tb\n"
        f"Parsing design file '{source}'\n",
    )

    compile_result = parse_compile_log(str(log), "vcs")
    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(source.resolve()),)
    assert manifest.complete is True
    assert manifest.fingerprint is not None
    assert plan.receipt.cross_request_reusable is True
    assert "filelist_unavailable" not in plan.receipt.gap_codes
    assert "compile_log_source_reconciliation_gap" not in plan.receipt.gap_codes


def test_vcs_infers_environment_root_from_top_level_filelist_path(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    package = project / "rtl" / "pkg.sv"
    top = project / "rtl" / "top.sv"
    filelist = project / "design.f"
    _write(package, "package design_pkg; endpackage\n")
    _write(top, "module tb; endmodule\n")
    _write(
        filelist,
        "$TW_RTL_PATH/rtl/pkg.sv\n$TW_RTL_PATH/rtl/top.sv\n",
    )
    log = tmp_path / "compile.log"
    _write(
        log,
        "Chronologic VCS simulator\n"
        "Command: vcs -f $TW_RTL_PATH/design.f -top tb\n"
        f"Parsing design file '{package}'\n"
        f"Parsing design file '{top}'\n",
    )
    monkeypatch.delenv("TW_RTL_PATH", raising=False)

    compile_result = parse_compile_log(str(log), "vcs")
    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(package.resolve()), str(top.resolve()))
    assert manifest.complete is True
    assert plan.receipt.cross_request_reusable is True
    assert "compile_environment_inferred_from_log" in plan.receipt.gap_codes
    assert "compile_inputs_recovered_from_simulator_log" not in plan.receipt.gap_codes


def test_vcs_ambiguous_root_ignores_stale_ambient_filelist_and_uses_log_order(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    actual = project / "rtl" / "top.sv"
    wrong = project / "rtl" / "wrong.sv"
    outer_filelist = project / "design.f"
    nested_filelist = project / "rtl" / "design.f"
    _write(actual, "module tb; endmodule\n")
    _write(wrong, "module wrong; endmodule\n")
    _write(outer_filelist, f"{actual}\n")
    _write(nested_filelist, f"{wrong}\n")
    log = tmp_path / "compile.log"
    _write(
        log,
        "Chronologic VCS simulator\n"
        "Command: vcs -f $TW_RTL_PATH/design.f -top tb\n"
        f"Parsing design file '{actual}'\n",
    )
    monkeypatch.setenv("TW_RTL_PATH", str(project / "rtl"))

    compile_result = parse_compile_log(str(log), "vcs")
    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(actual.resolve()),)
    assert str(wrong.resolve()) not in manifest.ordered_inputs
    assert manifest.complete is False
    assert plan.receipt.cross_request_reusable is False
    assert "compile_inputs_recovered_from_simulator_log" in plan.receipt.gap_codes
    assert "compile_log_source_reconciliation_gap" in plan.receipt.gap_codes


def test_vcs_ambiguous_filelist_root_falls_back_to_direct_units_not_files_user(
    monkeypatch, tmp_path
):
    core = tmp_path / "rtl" / "core.sv"
    independent = tmp_path / "tb" / "top.sv"
    include = tmp_path / "include" / "defs.svh"
    for path, text in (
        (core, 'module core; `include "defs.svh" endmodule\n'),
        (independent, "module tb; core u_core(); endmodule\n"),
        (include, "`define WIDTH 8\n"),
    ):
        _write(path, text)
    log = tmp_path / "compile.log"
    _write(
        log,
        "Chronologic VCS simulator\n"
        f"Command: vcs -f $TW_CFG_ROOT/design.f {independent} -top tb\n"
        f"Parsing design file '{core}'\n"
        f"Parsing included file '{include}'.\n"
        f"Back to file '{core}'.\n"
        f"Parsing design file '{independent}'\n",
    )
    monkeypatch.delenv("TW_CFG_ROOT", raising=False)

    compile_result = parse_compile_log(str(log), "vcs")
    assert len(compile_result["files"]["user"]) == 3
    plan = _plan(tmp_path, compile_result, signal_path="tb.u_core.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(core.resolve()), str(independent.resolve()))
    assert str(include.resolve()) not in manifest.ordered_inputs
    assert manifest.inputs_complete is False
    assert manifest.options_complete is False
    assert plan.receipt.cross_request_reusable is False
    assert "compile_environment_unresolved" in plan.receipt.gap_codes
    assert "compile_inputs_recovered_from_simulator_log" in plan.receipt.gap_codes
    assert "compile_include_dirs_recovered_from_log" in plan.receipt.gap_codes
    assert "compile_input_order_recovered_approximately" not in plan.receipt.gap_codes


def test_native_xrun_expanded_evidence_uses_file_records_to_remove_duplicates(
    monkeypatch, tmp_path
):
    package = tmp_path / "rtl" / "pkg.sv"
    dut = tmp_path / "rtl" / "dut.sv"
    outer = tmp_path / "outer.f"
    nested = tmp_path / "nested.f"
    include_dir = tmp_path / "include"
    for path, text in (
        (package, "package design_pkg; endpackage\n"),
        (dut, "module tb; endmodule\n"),
        (outer, "$TW_MISSING_ROOT/not_replayed.sv\n"),
        (nested, "$TW_MISSING_ROOT/not_replayed_either.sv\n"),
    ):
        _write(path, text)
    include_dir.mkdir()
    log = tmp_path / "compile.log"
    _write(
        log,
        "xrun\n"
        "\t-sv\n"
        f"\t{package}\n"
        f"\t-f {outer}\n"
        f"\t\t+incdir+{include_dir}\n"
        f"\t\t{package}\n"
        f"\t\t-f {nested}\n"
        f"\t\t\t{dut}\n"
        "\t+define+FEATURE=1\n"
        "\t-verbose\n"
        "\t-top tb\n"
        f"\t-log {log}\n"
        f"file: {package}\n"
        "\tpackage worklib.design_pkg:sv\n"
        f"file: {dut}\n"
        "\tmodule worklib.tb:sv\n",
    )
    monkeypatch.delenv("TW_MISSING_ROOT", raising=False)

    compile_result = parse_compile_log(str(log), "xcelium")
    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(package.resolve()), str(dut.resolve()))
    assert manifest.ordered_options.count("+define+FEATURE=1") == 1
    assert manifest.complete is True
    assert plan.receipt.cross_request_reusable is True
    assert "compile_environment_unresolved" not in plan.receipt.gap_codes
    assert "compile_log_source_reconciliation_gap" not in plan.receipt.gap_codes


def test_opentitan_shaped_vcs_log_keeps_project_filelist_order_and_excludes_recorders(
    monkeypatch, tmp_path
):
    work = tmp_path / "fusesoc-work"
    z_pkg = work / "src" / "z_pkg.sv"
    a_core = work / "src" / "a_core.sv"
    top = work / "src" / "tb.sv"
    include = work / "src" / "defs.svh"
    filelist = work / "design.scr"
    tool_uvm = tmp_path / "tool" / "uvm-1.2"
    uvm_pkg = tool_uvm / "uvm_pkg.sv"
    vcs_recorder = tool_uvm / "vcs" / "uvm_custom_install_vcs_recorder.sv"
    verdi_recorder = tool_uvm / "verdi" / "uvm_custom_install_verdi_recorder.sv"
    monkeypatch.setattr(
        "src.compile_log_parser.EDA_LIB_PREFIXES",
        [str(tmp_path / "tool")],
    )
    for path, text in (
        (z_pkg, "package z_pkg; endpackage\n"),
        (a_core, 'module a_core; `include "defs.svh" endmodule\n'),
        (top, "module tb; a_core u_core(); endmodule\n"),
        (include, "`define WIDTH 8\n"),
        (uvm_pkg, "package uvm_pkg; endpackage\n"),
        (vcs_recorder, "module vcs_recorder; endmodule\n"),
        (verdi_recorder, "module verdi_recorder; endmodule\n"),
    ):
        _write(path, text)
    _write(filelist, "src/z_pkg.sv\nsrc/a_core.sv\nsrc/tb.sv\n")
    log = tmp_path / "default" / "build.log"
    _write(
        log,
        "[make]: build\n"
        f"cd {work} && vcs -ntb_opts uvm-1.2 -f design.scr -top tb\n"
        "Chronologic VCS simulator\n"
        f"Parsing design file '{uvm_pkg}'\n"
        f"Parsing design file '{vcs_recorder}'\n"
        f"Parsing design file '{verdi_recorder}'\n"
        f"Parsing design file '{verdi_recorder}'\n"
        "Parsing design file 'src/z_pkg.sv'\n"
        "Parsing included file 'src/defs.svh'.\n"
        "Back to file 'src/z_pkg.sv'.\n"
        "Parsing design file 'src/a_core.sv'\n"
        "Parsing design file 'src/tb.sv'\n",
    )

    compile_result = parse_compile_log(str(log), "vcs")
    manifest, gaps, _ = source_graph_adapter._build_compile_manifest(
        str(log), compile_result
    )

    assert len(compile_result["files"]["user"]) == 4
    assert manifest.ordered_inputs == (
        str(uvm_pkg.resolve()),
        str(z_pkg.resolve()),
        str(a_core.resolve()),
        str(top.resolve()),
    )
    assert str(vcs_recorder.resolve()) not in manifest.ordered_inputs
    assert str(verdi_recorder.resolve()) not in manifest.ordered_inputs
    uvm_include_index = manifest.ordered_options.index(str(tool_uvm.resolve()))
    assert manifest.ordered_options[uvm_include_index - 1] == "-I"
    assert manifest.complete is True
    assert "compile_log_source_reconciliation_gap" not in gaps
    assert "simulator_instrumentation_excluded" in gaps
    roles = [
        item["role"]
        for item in compile_result["compile_evidence"]["ordered_compilation_units"]
    ]
    assert roles[:4] == [
        "simulator_library",
        "simulator_instrumentation",
        "simulator_instrumentation",
        "simulator_instrumentation",
    ]

    hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)
    hierarchy_snapshot = compute_snapshot_fingerprint(str(log), "vcs")
    hierarchy["_hierarchy_snapshot_sha256"] = hierarchy_snapshot
    plan = build_source_graph_plan(
        compile_log=str(log),
        compile_result=hierarchy["compile_result"],
        hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=hierarchy_snapshot,
        operation=QueryOperation.DRIVER,
        signal_path="tb.u_core.q",
        top_hint="tb",
        max_hops=8,
        frontend_version="11.0.0",
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert str(uvm_pkg.resolve()) in plan.request.identity.compile_inputs.ordered_inputs
    assert plan.receipt.fingerprint_cache_disposition == (
        "miss_reused_compile_session"
    )
    assert plan.receipt.content_digest_reuse_count == 4
    assert plan.receipt.content_digest_read_count >= 1
    assert plan.receipt.content_snapshot_conflict_count == 0


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


def test_compile_only_manifest_without_top_skips_source_hashing(monkeypatch, tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv",
        sources=(source,),
        tops=(),
    )

    monkeypatch.setattr(
        source_graph_adapter,
        "_hash_file_and_scan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("source hashing must wait for a proved top")
        ),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "compile_tops_unavailable"
    assert plan.receipt.manifest_complete is False
    assert "compile_fingerprint_missing" in (
        plan.receipt.manifest_incomplete_reasons
    )


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


def test_manifest_reuses_hierarchy_compile_session_content(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "top.sv"
    include = tmp_path / "defs.svh"
    _write(
        include,
        "task automatic drive_q; force q = 1'b1; release q; endtask\n",
    )
    _write(
        source,
        'module tb; import "DPI-C" function void f(); logic q;\n'
        '`include "defs.svh"\n'
        "endmodule\n",
    )
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = build_hierarchy(
        compile_result,
        apply_source_overlay=False,
    )
    compile_result = hierarchy["compile_result"]
    baseline = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
    )
    assert baseline.request is not None
    baseline_fingerprint = baseline.request.identity.compile_inputs.fingerprint
    baseline_exclusions = baseline.receipt.objective_exclusions
    source_graph_adapter._reset_source_graph_adapter_cache_for_tests()
    hierarchy_snapshot = compute_snapshot_fingerprint(
        str(tmp_path / "compile.log"),
        "xcelium",
    )
    hierarchy["_hierarchy_snapshot_sha256"] = hierarchy_snapshot

    calls = 0
    original = source_graph_adapter._hash_file_and_scan

    def tracked_hash(path, exclusions):
        nonlocal calls
        calls += 1
        return original(path, exclusions)

    monkeypatch.setattr(source_graph_adapter, "_hash_file_and_scan", tracked_hash)
    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    assert plan.request.identity.compile_inputs.fingerprint == baseline_fingerprint
    assert plan.receipt.objective_exclusions == baseline_exclusions
    assert calls == 0
    assert plan.receipt.fingerprint_cache_disposition == (
        "miss_reused_compile_session"
    )
    assert plan.receipt.content_digest_reuse_count == 2
    assert plan.receipt.content_digest_reuse_bytes == (
        source.stat().st_size + include.stat().st_size
    )
    assert plan.receipt.content_digest_read_count == 0
    assert plan.receipt.content_digest_read_bytes == 0
    assert plan.receipt.content_snapshot_conflict_count == 0
    assert "dpi_runtime" in plan.receipt.objective_exclusions
    assert "procedural_force_release" in plan.receipt.objective_exclusions
    manifest_receipt = plan.receipt.to_dict()["manifest"]
    assert manifest_receipt["content_digest_reuse_count"] == 2
    assert manifest_receipt["content_digest_read_count"] == 0


def test_manifest_blocks_when_hierarchy_content_snapshot_is_stale(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = build_hierarchy(
        compile_result,
        apply_source_overlay=False,
    )
    hierarchy_snapshot = compute_snapshot_fingerprint(
        str(tmp_path / "compile.log"),
        "xcelium",
    )
    hierarchy["_hierarchy_snapshot_sha256"] = hierarchy_snapshot
    _write(source, "module tb; logic q; assign q = 1'b1; endmodule\n")

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "compile_session_snapshot_changed"
    assert plan.receipt.blocker.stage == "compile_manifest"
    assert "compile_session_snapshot_changed" in plan.receipt.gap_codes
    assert plan.receipt.content_snapshot_conflict_count == 1

    path_plan = _path_plan(
        tmp_path,
        compile_result,
        hierarchy=hierarchy,
    )
    assert path_plan.status is AdapterStatus.BLOCKED
    assert path_plan.receipt.blocker is not None
    assert path_plan.receipt.blocker.code == "compile_session_snapshot_changed"
    assert path_plan.receipt.scope_kind == "dual_endpoint_path"
    assert path_plan.receipt.endpoint_count == 2
    assert path_plan.receipt.content_snapshot_conflict_count == 1


def test_manifest_blocks_incomplete_hierarchy_content_snapshot(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)
    hierarchy_snapshot = compute_snapshot_fingerprint(
        str(tmp_path / "compile.log"),
        "xcelium",
    )
    hierarchy["_hierarchy_snapshot_sha256"] = hierarchy_snapshot
    content_snapshot = hierarchy["_compile_session_snapshot"]
    hierarchy["_compile_session_snapshot"] = replace(
        content_snapshot,
        complete=False,
        issue_codes=("compile_content_changed_during_scan",),
    )
    calls = 0

    def unexpected_hash(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("an incomplete hierarchy snapshot must rebuild hierarchy")

    monkeypatch.setattr(
        source_graph_adapter,
        "_hash_file_and_scan",
        unexpected_hash,
    )

    plan = _plan(
        tmp_path,
        hierarchy["compile_result"],
        signal_path="tb.q",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "compile_session_snapshot_changed"
    assert plan.receipt.content_snapshot_conflict_count == 1
    assert plan.receipt.content_digest_reuse_count == 0
    assert plan.receipt.content_digest_read_count == 0
    assert calls == 0


def test_manifest_blocks_retargeted_compile_input_symlink(tmp_path):
    original = tmp_path / "original.sv"
    replacement = tmp_path / "replacement.sv"
    source_link = tmp_path / "top.sv"
    _write(original, "module tb; logic q; endmodule\n")
    _write(replacement, "module tb; logic q; assign q = 1'b1; endmodule\n")
    source_link.symlink_to(original)
    _write(tmp_path / "compile.log", "xrun top.sv -top tb\n")
    compile_result = {
        "simulator": "xcelium",
        "compile_cwd": str(tmp_path),
        "compile_command": "xrun top.sv -top tb",
        "top_modules": ["tb"],
        "files": {
            "user": [
                {
                    "path": str(source_link),
                    "type": "module",
                    "category": "rtl",
                }
            ],
            "filtered_count": 0,
        },
        "include_tree": {},
        "filelist_tree": {},
        "interfaces": [],
        "parse_warnings": [],
    }
    hierarchy = build_hierarchy(compile_result, apply_source_overlay=False)
    hierarchy_snapshot = compute_snapshot_fingerprint(
        str(tmp_path / "compile.log"),
        "xcelium",
    )
    hierarchy["_hierarchy_snapshot_sha256"] = hierarchy_snapshot
    content_snapshot = hierarchy["_compile_session_snapshot"]
    assert content_snapshot.current() is True

    first = _plan(
        tmp_path,
        hierarchy["compile_result"],
        signal_path="tb.q",
        hierarchy=hierarchy,
    )
    assert first.status is AdapterStatus.READY
    assert first.receipt.fingerprint_cache_disposition == (
        "miss_reused_compile_session"
    )

    source_link.unlink()
    source_link.symlink_to(replacement)
    assert content_snapshot.current() is True

    plan = _plan(
        tmp_path,
        hierarchy["compile_result"],
        signal_path="tb.q",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "compile_session_snapshot_changed"
    assert plan.receipt.content_snapshot_conflict_count == 1


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


def test_extended_systemverilog_suffixes_reconcile_and_reach_manifest(tmp_path):
    paths = {
        "top": tmp_path / "rtl" / "top.sv",
        "header": tmp_path / "rtl" / "defs.svh",
        "include": tmp_path / "rtl" / "checks.svi",
        "assertions": tmp_path / "rtl" / "assertions.sva",
        "library": tmp_path / "rtl" / "checker_library.svl",
        "protected": tmp_path / "rtl" / "protected_model.svp",
    }
    _write(paths["top"], "module tb; logic q; endmodule\n")
    _write(paths["header"], "`define WIDTH 8\n")
    _write(paths["include"], "package checks_pkg; endpackage\n")
    _write(paths["assertions"], "module assertions; endmodule\n")
    _write(paths["library"], "package checker_library; endpackage\n")
    _write(paths["protected"], "`protected\nopaque payload\n")
    ordered = (
        paths["top"],
        paths["header"],
        paths["include"],
        paths["assertions"],
        paths["library"],
        paths["protected"],
        paths["include"],
    )
    _write(
        tmp_path / "design.f",
        "\n".join(str(path.relative_to(tmp_path)) for path in ordered) + "\n",
    )
    private_plusarg = "+TW_PRIVATE+MODE"
    compile_result = _compile_result(
        tmp_path,
        command=(
            f"vcs -f design.f -diag reports {private_plusarg} -top tb"
        ),
        sources=ordered,
    )
    compile_result["simulator"] = "vcs"
    compile_result["compile_evidence"] = {
        "schema_version": 1,
        "unit_order_source": "simulator_log",
        "ordered_compilation_units": [
            {"path": str(path.resolve()), "role": "project"} for path in ordered
        ],
        "ordered_includes": [],
        "filelists": [],
    }

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
        hierarchy={"component_tree": {"tb": {}}},
        runtime_plusarg_allowlist=frozenset({private_plusarg}),
    )

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == tuple(str(path.resolve()) for path in ordered)
    assert manifest.complete is True
    assert plan.receipt.cross_request_reusable is True
    assert "compile_log_source_reconciliation_gap" not in plan.receipt.gap_codes
    assert "compile_option_unclassified" not in plan.receipt.gap_codes
    assert "protected_region" in plan.receipt.objective_exclusions
    assert "-diag" not in manifest.ordered_options
    assert "reports" not in manifest.ordered_options
    assert private_plusarg not in manifest.ordered_options
    assert private_plusarg not in json.dumps(plan.receipt.to_dict())


def test_runtime_plusarg_allowlist_is_exact_and_manifest_cache_scoped(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic q; endmodule\n")
    private_plusarg = "+TW_PRIVATE+MODE"
    compile_result = _compile_result(
        tmp_path,
        command=f"xrun {private_plusarg} top.sv -top tb",
        sources=(source,),
    )

    allowed = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
        hierarchy={"component_tree": {"tb": {}}},
        runtime_plusarg_allowlist=frozenset({private_plusarg}),
    )
    disallowed = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.q",
        hierarchy={"component_tree": {"tb": {}}},
    )

    assert allowed.request is not None
    assert allowed.request.identity.compile_inputs.complete is True
    assert allowed.receipt.cross_request_reusable is True
    assert disallowed.request is not None
    assert disallowed.request.identity.compile_inputs.options_complete is False
    assert disallowed.receipt.cross_request_reusable is False
    assert "compile_option_unclassified" in disallowed.receipt.gap_codes


def test_unknown_option_and_arbitrary_filelist_suffix_remain_fail_closed(tmp_path):
    source = tmp_path / "top.sv"
    unrelated = tmp_path / "payload.design_data"
    _write(source, "module tb; logic q; endmodule\n")
    _write(unrelated, "not an HDL compilation unit\n")
    _write(tmp_path / "design.f", "top.sv\npayload.design_data\n")
    compile_result = _compile_result(
        tmp_path,
        command="vcs -f design.f -mystery-mode -top tb",
        sources=(source,),
    )

    plan = _plan(tmp_path, compile_result, signal_path="tb.q")

    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(source.resolve()),)
    assert str(unrelated.resolve()) not in manifest.ordered_inputs
    assert manifest.options_complete is False
    assert plan.receipt.cross_request_reusable is False
    assert "compile_option_unclassified" in plan.receipt.gap_codes


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
        "hierarchy_resolution": {
            "status": "resolved",
            "endpoint_count": 2,
            "resolved_endpoint_count": 2,
            "deferred_endpoint_count": 0,
            "truncated_endpoint_count": 0,
            "max_matched_instance_count": 3,
            "max_remaining_path_segment_count": 1,
            "first_stop_depth": None,
            "missing_instance_proved": False,
        },
    }


def test_path_scope_unions_gap_codes_from_both_ancestor_chains(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = {
        "component_tree": {
            "tb": {
                "left": {
                    "module": "left",
                    "children": {},
                    "hierarchy_gap_codes": [
                        "hierarchy_include_path_unresolved"
                    ],
                },
                "right": {
                    "module": "right",
                    "children": {},
                    "hierarchy_gap_codes": [
                        "hierarchy_definition_ambiguous"
                    ],
                },
            }
        }
    }

    plan = _path_plan(
        tmp_path,
        compile_result,
        from_signal="tb.left.out",
        to_signal="tb.right.in",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.READY
    expected = {
        "hierarchy_include_path_unresolved",
        "hierarchy_definition_ambiguous",
    }
    assert expected <= set(plan.receipt.gap_codes)
    assert expected <= set(plan.receipt.objective_exclusions)


def test_single_endpoint_receipt_exposes_deferred_hierarchy_suffix(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.iface.req.valid",
        hierarchy={"component_tree": {"tb": {}}},
    )

    assert plan.status is AdapterStatus.READY
    assert plan.unprojected_instance_candidates == ("tb.iface",)
    resolution = plan.receipt.to_dict()["scope"]["hierarchy_resolution"]
    assert resolution == {
        "status": "deferred",
        "endpoint_count": 1,
        "resolved_endpoint_count": 0,
        "deferred_endpoint_count": 1,
        "truncated_endpoint_count": 0,
        "max_matched_instance_count": 1,
        "max_remaining_path_segment_count": 3,
        "first_stop_depth": 1,
        "missing_instance_proved": False,
    }
    assert "hierarchy_ancestor_chain_truncated" not in plan.receipt.gap_codes


def test_hierarchy_edge_gaps_flow_to_source_graph_coverage(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = {
        "component_tree": {
            "tb": {
                "dut": {
                    "module": "dut",
                    "children": {},
                    "hierarchy_gap_codes": [
                        "hierarchy_instance_array_unexpanded",
                        "hierarchy_parameter_specialization_unmodeled",
                    ],
                }
            }
        }
    }

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.dut.value",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.READY
    assert "hierarchy_instance_array_unexpanded" in plan.receipt.gap_codes
    assert "hierarchy_instance_array_unexpanded" in (
        plan.receipt.objective_exclusions
    )
    assert "hierarchy_instance_array_unexpanded" in (
        plan.request.scope.coverage_boundary.objective_exclusions
    )
    assert "hierarchy_parameter_specialization_unmodeled" not in (
        plan.receipt.gap_codes
    )


def test_root_module_gap_flows_to_source_graph_coverage(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic value; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = {
        "component_tree": {"tb": {}},
        "_scan_results": [
            {
                "hierarchy_module_gap_map": {
                    "tb": ["hierarchy_macro_compound_unsupported"]
                }
            }
        ],
    }

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.value",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.READY
    assert "hierarchy_macro_compound_unsupported" in plan.receipt.gap_codes
    assert "hierarchy_macro_compound_unsupported" in (
        plan.receipt.objective_exclusions
    )
    assert "hierarchy_macro_compound_unsupported" in (
        plan.request.scope.coverage_boundary.objective_exclusions
    )


def test_duplicate_top_definition_flows_to_source_graph_coverage(tmp_path):
    source = tmp_path / "top.sv"
    _write(source, "module tb; logic value; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = {
        "component_tree": {"tb": {}},
        "_scan_results": [
            {
                "structural_modules": ["tb"],
                "structural_interfaces": [],
                "module_instance_map": {"tb": []},
            },
            {
                "structural_modules": ["tb"],
                "structural_interfaces": [],
                "module_instance_map": {"tb": []},
            },
        ],
    }

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.value",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.READY
    assert "hierarchy_definition_ambiguous" in plan.receipt.gap_codes
    assert "hierarchy_definition_ambiguous" in (
        plan.receipt.objective_exclusions
    )


def test_unresolved_generate_candidate_is_deferred_not_proved_missing(
    tmp_path,
):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = {
        "component_tree": {"tb": {}},
        "_scan_results": [
            {
                "module_instance_map": {
                    "tb": [
                        {
                            "module_name": "leaf",
                            "instance_name": "u_generated",
                            "hierarchy_edge_status": "unresolved_semantic",
                            "hierarchy_gap_codes": [
                                "hierarchy_generate_scope_unmodeled"
                            ],
                        }
                    ]
                }
            }
        ],
    }

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.u_generated.value",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.READY
    resolution = plan.receipt.to_dict()["scope"]["hierarchy_resolution"]
    assert resolution["status"] == "deferred"
    assert resolution["missing_instance_proved"] is False
    assert "hierarchy_generate_scope_unmodeled" in plan.receipt.gap_codes
    assert "hierarchy_generate_scope_unmodeled" in (
        plan.receipt.objective_exclusions
    )


def test_proved_missing_child_blocks_as_instance_outside_projected_scope(
    tmp_path,
):
    source = tmp_path / "top.sv"
    _write(source, "module tb; endmodule\n")
    compile_result = _compile_result(
        tmp_path,
        command="xrun top.sv -top tb",
        sources=(source,),
    )
    hierarchy = {
        "component_tree": {"tb": {}},
        "_scan_results": [
            {
                "module_instance_map": {
                    "tb": [
                        {
                            "module_name": "dut",
                            "instance_name": "u_dut",
                        }
                    ]
                }
            }
        ],
    }

    plan = _plan(
        tmp_path,
        compile_result,
        signal_path="tb.u_dut.u_leaf.target_sig",
        hierarchy=hierarchy,
    )

    assert plan.status is AdapterStatus.BLOCKED
    assert plan.receipt.blocker is not None
    assert plan.receipt.blocker.code == "instance_not_in_projected_scope"
    assert "hierarchy_ancestor_chain_truncated" in plan.receipt.gap_codes
    resolution = plan.receipt.to_dict()["scope"]["hierarchy_resolution"]
    assert resolution["status"] == "truncated"
    assert resolution["first_stop_depth"] == 1
    assert resolution["missing_instance_proved"] is True


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
