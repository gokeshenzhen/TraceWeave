"""Tests for src/handle_tools.py — pure function layer.

Server-side wiring (HandleStore lookup, MCP dispatch) is covered by
test_server.py; this module exercises the implementations directly against
constructed full_result fixtures.
"""

from __future__ import annotations

import pytest

from src import handle_tools as ht


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _scan(path, source_text="", **extra):
    base = {
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "type": extra.pop("type", "module"),
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


def _full(**overrides):
    base = {
        "project": {"top_module": "top", "simulator": "vcs"},
        "component_tree": {},
        "files": {},
        "class_hierarchy": [],
        "interfaces": [],
        "compile_result": {},
        "_scan_results": [],
    }
    base.update(overrides)
    return base


def _sample_tree():
    # top -> u_cpu(cpu) -> u_alu(alu), u_regfile(regfile) -> u_cell(mem_cell)
    # top -> u_bus(bus)
    return {
        "top": {
            "u_cpu": {
                "type": "module", "class": "cpu",
                "source_file": "/p/cpu.sv", "source_line": 10,
                "children": {
                    "u_alu": {"type": "module", "class": "alu",
                              "source_file": "/p/alu.sv", "source_line": 3},
                    "u_regfile": {
                        "type": "module", "class": "regfile",
                        "source_file": "/p/regfile.sv", "source_line": 7,
                        "children": {
                            "u_cell": {"type": "module", "class": "mem_cell",
                                       "source_file": "/p/mem_cell.sv", "source_line": 1},
                        },
                    },
                },
            },
            "u_bus": {"type": "module", "class": "bus",
                      "source_file": "/p/bus.sv", "source_line": 5},
        }
    }


# ---------------------------------------------------------------------------
# get_tb_subtree
# ---------------------------------------------------------------------------


def test_subtree_root_empty_path():
    full = _full(component_tree=_sample_tree())
    out = ht.get_tb_subtree(full, "tbh_x", root="", depth=1)
    assert out["handle"] == "tbh_x"
    assert out["root"] == ""
    assert out["node"]["inst"] == "top"
    assert out["node"]["child_count"] == 2
    insts = {c["inst"] for c in out["node"]["children"]}
    assert insts == {"u_cpu", "u_bus"}
    # depth=1 means grandchildren not included
    cpu = next(c for c in out["node"]["children"] if c["inst"] == "u_cpu")
    assert cpu["child_count"] == 2
    assert cpu["truncated"] is True
    assert cpu["children"] == []
    assert out["total_descendants"] == 5  # u_cpu, u_alu, u_regfile, u_cell, u_bus


def test_subtree_dotted_path():
    full = _full(component_tree=_sample_tree())
    out = ht.get_tb_subtree(full, "tbh_x", root="top.u_cpu.u_regfile", depth=-1)
    assert out["node"]["inst"] == "u_regfile"
    assert out["node"]["module"] == "regfile"
    assert out["node"]["child_count"] == 1
    assert out["node"]["children"][0]["inst"] == "u_cell"


def test_subtree_missing_path():
    full = _full(component_tree=_sample_tree())
    out = ht.get_tb_subtree(full, "tbh_x", root="top.nope", depth=1)
    assert out["error"] == "instance_not_found"
    assert out["current_handle"] == "tbh_x"


def test_subtree_depth_zero_root_only():
    full = _full(component_tree=_sample_tree())
    out = ht.get_tb_subtree(full, "tbh_x", root="", depth=0)
    assert out["node"]["inst"] == "top"
    assert out["node"]["truncated"] is True
    assert out["node"]["children"] == []


def test_subtree_max_nodes_truncates():
    full = _full(component_tree=_sample_tree())
    out = ht.get_tb_subtree(full, "tbh_x", root="", depth=-1, max_nodes=2)
    # we should bail out as soon as 2 children expanded
    assert out["truncated"] is True


def test_subtree_empty_tree():
    out = ht.get_tb_subtree(_full(), "tbh_x", root="")
    assert out["error"] == "instance_not_found"


# ---------------------------------------------------------------------------
# lookup_tb_files
# ---------------------------------------------------------------------------


def _files_full():
    return _full(_scan_results=[
        _scan("/proj/rtl/cpu.sv", modules=["cpu"]),
        _scan("/proj/rtl/alu.sv", modules=["alu"]),
        _scan("/proj/tb/top_tb.sv",
              source_text="import uvm_pkg::*;\nmodule top_tb; endmodule\n",
              modules=["top_tb"]),
        _scan("/proj/tb/my_test.sv",
              source_text="class my_test extends uvm_test;\nendclass\n",
              type="class",
              classes=["my_test"],
              class_extends={"my_test": "uvm_test"}),
    ])


def test_lookup_basename_match():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", basename="cpu.sv")
    assert out["total"] == 1
    assert out["matches"][0]["path"] == "/proj/rtl/cpu.sv"
    assert out["matches"][0]["modules"] == ["cpu"]


def test_lookup_path_contains():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", path_contains="/tb/")
    assert out["total"] == 2
    paths = {m["path"] for m in out["matches"]}
    assert paths == {"/proj/tb/top_tb.sv", "/proj/tb/my_test.sv"}


def test_lookup_contains_uvm_true():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", contains_uvm=True)
    assert out["total"] == 2
    assert all(m["has_uvm_import"] for m in out["matches"])


def test_lookup_contains_uvm_false():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", contains_uvm=False)
    assert out["total"] == 2
    assert all(not m["has_uvm_import"] for m in out["matches"])


def test_lookup_has_module():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", has_module="alu")
    assert out["total"] == 1
    assert out["matches"][0]["path"] == "/proj/rtl/alu.sv"


def test_lookup_file_type():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", file_type="class")
    assert out["total"] == 1
    assert out["matches"][0]["classes"] == ["my_test"]


def test_lookup_no_filter_errors():
    out = ht.lookup_tb_files(_files_full(), "tbh_x")
    assert out["error"] == "filter_required"
    assert out["current_handle"] == "tbh_x"


def test_lookup_respects_limit():
    out = ht.lookup_tb_files(_files_full(), "tbh_x", path_contains="/proj/", limit=2)
    assert out["total"] == 4
    assert out["truncated"] is True
    assert len(out["matches"]) == 2


# ---------------------------------------------------------------------------
# find_tb_instance
# ---------------------------------------------------------------------------


def test_find_instance_by_exact_path():
    full = _full(component_tree=_sample_tree())
    out = ht.find_tb_instance(full, "tbh_x", path="top.u_cpu.u_regfile.u_cell")
    assert out["total"] == 1
    hit = out["hits"][0]
    assert hit["module"] == "mem_cell"
    assert hit["parent"] == "top.u_cpu.u_regfile"
    assert hit["source_file"] == "/p/mem_cell.sv"


def test_find_instance_by_module_name():
    full = _full(component_tree=_sample_tree())
    out = ht.find_tb_instance(full, "tbh_x", module="alu")
    assert out["total"] == 1
    assert out["hits"][0]["path"] == "top.u_cpu.u_alu"


def test_find_instance_no_filter():
    out = ht.find_tb_instance(_full(component_tree=_sample_tree()), "tbh_x")
    assert out["error"] == "filter_required"


def test_find_instance_both_filters_rejected():
    out = ht.find_tb_instance(_full(component_tree=_sample_tree()),
                              "tbh_x", path="top.u_cpu", module="cpu")
    assert out["error"] == "mutually_exclusive_filters"


def test_find_instance_module_multiple_hits():
    # Add a second alu instance under bus
    tree = _sample_tree()
    tree["top"]["u_bus"]["children"] = {
        "u_alu2": {"type": "module", "class": "alu",
                   "source_file": "/p/alu.sv", "source_line": 99}
    }
    out = ht.find_tb_instance(_full(component_tree=tree), "tbh_x", module="alu")
    assert out["total"] == 2
    paths = {h["path"] for h in out["hits"]}
    assert paths == {"top.u_cpu.u_alu", "top.u_bus.u_alu2"}


# ---------------------------------------------------------------------------
# get_tb_file_detail
# ---------------------------------------------------------------------------


def test_file_detail_happy_path():
    full = _full(_scan_results=[
        _scan("/proj/rtl/cpu.sv",
              modules=["cpu"], classes=[], interfaces=[],
              source_text="module cpu; endmodule\n"),
    ])
    out = ht.get_tb_file_detail(full, "tbh_x", path="/proj/rtl/cpu.sv")
    assert out["path"] == "/proj/rtl/cpu.sv"
    assert out["file_type"] == "module"
    assert any(s["name"] == "cpu" and s["kind"] == "module" for s in out["symbols"])


def test_file_detail_unknown_path_suggests_basename_matches():
    full = _full(_scan_results=[
        _scan("/proj/current/cpu.sv", modules=["cpu"]),
        _scan("/proj/legacy/cpu.sv", modules=["cpu"]),
    ])
    out = ht.get_tb_file_detail(full, "tbh_x", path="/proj/tb/cpu.sv")
    assert out["error"] == "file_not_in_compile_set"
    assert set(out["did_you_mean"]) == {"/proj/current/cpu.sv", "/proj/legacy/cpu.sv"}


def test_file_detail_unknown_path_no_basename_match():
    full = _full(_scan_results=[_scan("/proj/cpu.sv", modules=["cpu"])])
    out = ht.get_tb_file_detail(full, "tbh_x", path="/proj/foo.sv")
    assert out["error"] == "file_not_in_compile_set"
    assert out["did_you_mean"] == []


# ---------------------------------------------------------------------------
# get_tb_class_hierarchy
# ---------------------------------------------------------------------------


def test_class_hierarchy_builds_tree_from_extends():
    full = _full(_scan_results=[
        _scan("/p/base.sv", type="class", classes=["base_test"],
              class_extends={"base_test": "uvm_test"}),
        _scan("/p/case0.sv", type="class", classes=["my_case0"],
              class_extends={"my_case0": "base_test"}),
    ])
    out = ht.get_tb_class_hierarchy(full, "tbh_x")
    # uvm_test is a root because no scan defines it nor declares it as a child
    roots_by_name = {r["name"]: r for r in out["roots"]}
    assert "uvm_test" in roots_by_name
    base = roots_by_name["uvm_test"]["children"][0]
    assert base["name"] == "base_test"
    assert base["source_file"] == "/p/base.sv"
    assert base["children"][0]["name"] == "my_case0"
    assert out["total"] >= 3


def test_class_hierarchy_specific_root():
    full = _full(_scan_results=[
        _scan("/p/base.sv", type="class", classes=["base_test"],
              class_extends={"base_test": "uvm_test"}),
        _scan("/p/case0.sv", type="class", classes=["my_case0"],
              class_extends={"my_case0": "base_test"}),
    ])
    out = ht.get_tb_class_hierarchy(full, "tbh_x", root_class="base_test")
    assert len(out["roots"]) == 1
    assert out["roots"][0]["name"] == "base_test"
    assert out["roots"][0]["children"][0]["name"] == "my_case0"


def test_class_hierarchy_unknown_root():
    full = _full(_scan_results=[_scan("/p/a.sv", classes=["foo"])])
    out = ht.get_tb_class_hierarchy(full, "tbh_x", root_class="nope")
    assert out["roots"] == []


def test_class_hierarchy_empty_returns_empty():
    out = ht.get_tb_class_hierarchy(_full(), "tbh_x")
    assert out["roots"] == []
    assert out["total"] == 0


# ---------------------------------------------------------------------------
# dump_tb_section
# ---------------------------------------------------------------------------


def test_dump_compile_result():
    full = _full(compile_result={"simulator": "vcs", "files": {"user": []}})
    out = ht.dump_tb_section(full, "tbh_x", section="compile_result")
    assert out["section"] == "compile_result"
    assert out["data"]["simulator"] == "vcs"


def test_dump_include_tree_subkey():
    full = _full(compile_result={
        "simulator": "vcs",
        "include_tree": {"/p/a.sv": ["/p/b.sv"]},
    })
    out = ht.dump_tb_section(full, "tbh_x", section="include_tree")
    assert out["data"] == {"/p/a.sv": ["/p/b.sv"]}


def test_dump_unknown_section():
    out = ht.dump_tb_section(_full(), "tbh_x", section="nonsense")
    assert out["error"] == "unknown_section"


def test_dump_warns_on_large_section():
    # Construct a large section to trigger the warning.
    huge = {"k": "x" * 100_000}
    full = _full(compile_result=huge)
    out = ht.dump_tb_section(full, "tbh_x", section="compile_result")
    assert "large" in out["warning"]


def test_dump_section_missing_field_returns_empty_default():
    out = ht.dump_tb_section(_full(), "tbh_x", section="files_full")
    assert out["data"] in ({}, [])
