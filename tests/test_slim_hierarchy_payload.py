"""Tests for the slim-payload helpers in tb_hierarchy_builder.

Phase 2 of the build_tb_hierarchy compactification: helpers are pure
functions over a full hierarchy dict, so the tests construct minimal
fixtures and verify each helper independently. Server wiring is phase 3.
"""

from __future__ import annotations

from src.tb_hierarchy_builder import (
    HANDLE_TOOL_NAMES,
    build_slim_payload,
    compute_stats,
    detect_ambiguous_basenames,
    extract_tree_skeleton,
)


def _scan(path: str, source_text: str = "", **extra) -> dict:
    base = {
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "type": "module",
        "source_text": source_text,
        "classes": [],
        "class_extends": {},
        "modules": [],
        "interfaces": [],
        "creates": [],
        "module_instances": [],
        "virtual_interfaces": [],
    }
    base.update(extra)
    return base


def _make_full_result(component_tree=None, files_user=None, scan_results=None,
                     interfaces=None, class_hierarchy=None, top_module="top",
                     compile_command="vcs -f files.f"):
    return {
        "project": {
            "top_module": top_module,
            "source_root": "/proj",
            "simulator": "vcs",
        },
        "files": {},
        "component_tree": component_tree or {},
        "class_hierarchy": class_hierarchy or [],
        "interfaces": interfaces or [],
        "compile_result": {
            "simulator": "vcs",
            "top_modules": [top_module],
            "files": {"user": files_user or [], "filtered_count": 0},
            "compile_command": compile_command,
        },
        "_scan_results": scan_results or [],
    }


# ---- detect_ambiguous_basenames ---------------------------------------------


def test_ambiguous_basenames_flags_versioned_duplicates():
    entries = [
        {"path": "/proj/legacy/cpu.sv"},
        {"path": "/proj/current/cpu.sv"},
        {"path": "/proj/tb/top_tb.sv"},
    ]
    out = detect_ambiguous_basenames(entries)
    assert len(out) == 1
    assert out[0]["basename"] == "cpu.sv"
    assert set(out[0]["paths"]) == {"/proj/legacy/cpu.sv", "/proj/current/cpu.sv"}
    assert out[0]["picked"] in out[0]["paths"]


def test_ambiguous_basenames_empty_when_unique():
    entries = [
        {"path": "/proj/a.sv"},
        {"path": "/proj/b.sv"},
    ]
    assert detect_ambiguous_basenames(entries) == []


def test_ambiguous_basenames_handles_empty_input():
    assert detect_ambiguous_basenames([]) == []
    assert detect_ambiguous_basenames(None) == []


def test_ambiguous_basenames_groups_three_versions():
    entries = [
        {"path": "/p/x_v1.v"},
        {"path": "/q/x_v1.v"},
        {"path": "/r/x_v1.v"},
    ]
    out = detect_ambiguous_basenames(entries)
    assert len(out) == 1
    assert len(out[0]["paths"]) == 3


# ---- extract_tree_skeleton --------------------------------------------------


def _sample_tree():
    # top
    # ├─ u_cpu (cpu)
    # │   ├─ u_alu (alu)
    # │   └─ u_regfile (regfile)
    # │       └─ u_cell (mem_cell)
    # └─ u_bus (bus)
    return {
        "top": {
            "u_cpu": {
                "type": "module",
                "class": "cpu",
                "source_file": "/p/cpu.sv",
                "source_line": 10,
                "children": {
                    "u_alu": {
                        "type": "module", "class": "alu",
                        "source_file": "/p/alu.sv", "source_line": 3,
                    },
                    "u_regfile": {
                        "type": "module", "class": "regfile",
                        "source_file": "/p/regfile.sv", "source_line": 7,
                        "children": {
                            "u_cell": {
                                "type": "module", "class": "mem_cell",
                                "source_file": "/p/mem_cell.sv", "source_line": 1,
                            }
                        }
                    },
                },
            },
            "u_bus": {
                "type": "module", "class": "bus",
                "source_file": "/p/bus.sv", "source_line": 5,
            },
        }
    }


def test_skeleton_root_at_depth_2():
    skel = extract_tree_skeleton(_sample_tree(), "top", depth=2)
    assert skel["inst"] == "top"
    assert skel["module"] == "top"
    assert skel["child_count"] == 2
    assert not skel["truncated"]
    insts = {c["inst"] for c in skel["children"]}
    assert insts == {"u_cpu", "u_bus"}

    cpu = next(c for c in skel["children"] if c["inst"] == "u_cpu")
    assert cpu["module"] == "cpu"
    assert cpu["source_file"] == "/p/cpu.sv"
    assert cpu["source_line"] == 10
    assert cpu["child_count"] == 2
    grand_insts = {g["inst"] for g in cpu["children"]}
    assert grand_insts == {"u_alu", "u_regfile"}


def test_skeleton_marks_truncation_below_max_depth():
    skel = extract_tree_skeleton(_sample_tree(), "top", depth=2)
    cpu = next(c for c in skel["children"] if c["inst"] == "u_cpu")
    regfile = next(g for g in cpu["children"] if g["inst"] == "u_regfile")
    # regfile has 1 child (u_cell) that should NOT be included at depth=2
    # because we've used up the depth budget; instead truncated=True.
    assert regfile["child_count"] == 1
    assert regfile["truncated"] is True
    assert regfile["children"] == []


def test_skeleton_depth_zero_returns_root_only():
    skel = extract_tree_skeleton(_sample_tree(), "top", depth=0)
    assert skel["inst"] == "top"
    assert skel["child_count"] == 2
    assert skel["truncated"] is True
    assert skel["children"] == []


def test_skeleton_empty_inputs():
    assert extract_tree_skeleton({}, "top") == {}
    assert extract_tree_skeleton(_sample_tree(), "") == {}
    assert extract_tree_skeleton(_sample_tree(), "missing_module") == {}


def test_skeleton_handles_leaf_node():
    tree = {"top": {"u_leaf": {"type": "module", "class": "leaf"}}}
    skel = extract_tree_skeleton(tree, "top", depth=2)
    leaf = skel["children"][0]
    assert leaf["child_count"] == 0
    assert leaf["truncated"] is False


# ---- compute_stats ----------------------------------------------------------


def test_stats_basic_counts():
    full = _make_full_result(
        component_tree=_sample_tree(),
        files_user=[
            {"path": "/p/cpu.sv"}, {"path": "/p/alu.sv"},
            {"path": "/p/regfile.sv"}, {"path": "/p/mem_cell.sv"},
            {"path": "/p/bus.sv"},
        ],
        interfaces=[{"name": "if_a"}, {"name": "if_b"}],
        class_hierarchy=["foo -> bar"],
    )
    stats = compute_stats(full)
    assert stats["file_count"] == 5
    assert stats["instance_count"] == 5   # u_cpu, u_alu, u_regfile, u_cell, u_bus
    assert stats["module_count"] == 5     # cpu, alu, regfile, mem_cell, bus
    assert stats["tree_depth"] == 4       # levels: top, u_cpu, u_regfile, u_cell
    assert stats["class_count"] == 1
    assert stats["interface_count"] == 2
    assert stats["uvm_file_count"] == 0


def test_stats_uvm_detection_via_import():
    scans = [
        _scan("/p/test.sv", source_text="import uvm_pkg::*;\nclass t; endclass\n"),
        _scan("/p/plain.sv", source_text="module m; endmodule\n"),
    ]
    full = _make_full_result(scan_results=scans)
    assert compute_stats(full)["uvm_file_count"] == 1


def test_stats_uvm_detection_via_extends():
    scans = [
        _scan("/p/test.sv", source_text="class my_test extends uvm_test;\nendclass\n",
              class_extends={"my_test": "uvm_test"}),
        _scan("/p/plain.sv", source_text=""),
    ]
    full = _make_full_result(scan_results=scans)
    assert compute_stats(full)["uvm_file_count"] == 1


def test_stats_handles_empty_full_result():
    stats = compute_stats({"_scan_results": []})
    assert stats == {
        "file_count": 0,
        "module_count": 0,
        "instance_count": 0,
        "tree_depth": 0,
        "class_count": 0,
        "interface_count": 0,
        "uvm_file_count": 0,
    }


# ---- build_slim_payload -----------------------------------------------------


def test_slim_payload_shape():
    full = _make_full_result(
        component_tree=_sample_tree(),
        files_user=[
            {"path": "/proj/legacy/cpu.sv"},
            {"path": "/proj/current/cpu.sv"},
            {"path": "/proj/tb/top_tb.sv"},
        ],
        interfaces=[{"name": "if_a", "src": "", "bound_in": ""}],
        compile_command="vcs -full64 -f files.f",
    )
    slim = build_slim_payload(full, "tbh_deadbeef", kdb_hint={"missing": True})

    assert slim["hierarchy_handle"] == "tbh_deadbeef"
    assert slim["project"]["top_module"] == "top"
    assert slim["compile_command"] == "vcs -full64 -f files.f"
    assert slim["kdb_hint"] == {"missing": True}
    assert slim["handle_tools"] == HANDLE_TOOL_NAMES

    # ambiguous_basenames carries the multi-version warning
    assert len(slim["ambiguous_basenames"]) == 1
    assert slim["ambiguous_basenames"][0]["basename"] == "cpu.sv"

    # tree skeleton present, no full component_tree leak
    assert slim["tree_skeleton"]["inst"] == "top"
    assert "component_tree" not in slim
    assert "files" not in slim
    assert "compile_result" not in slim
    assert "_scan_results" not in slim
    assert "class_hierarchy" not in slim


def test_slim_payload_kdb_hint_defaults_to_none():
    slim = build_slim_payload(_make_full_result(), "tbh_aaaaaaaa")
    assert slim["kdb_hint"] is None


def test_slim_payload_does_not_mutate_input():
    full = _make_full_result(component_tree=_sample_tree())
    original_project = dict(full["project"])
    original_interfaces = list(full["interfaces"])
    build_slim_payload(full, "tbh_aaaaaaaa")
    assert full["project"] == original_project
    assert full["interfaces"] == original_interfaces
