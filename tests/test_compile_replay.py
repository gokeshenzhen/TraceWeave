"""Independent compile-unit/include and cross-terminal replay expectations."""
from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_environment import resolve_compile_environment
from src.compile_log_parser import parse_compile_log
from src.hierarchy_handles import compute_snapshot_fingerprint
from src.kdb_builder import _extract_build_inputs, _vericom_cmd, build_kdb
from src.source_graph_adapter import build_source_graph_plan
from src.source_graph_contract import QueryOperation
from src.sv_preprocessor import SystemVerilogPreprocessor
from src.tb_hierarchy_builder import build_hierarchy


@pytest.fixture
def replay_case(tmp_path, monkeypatch):
    root = tmp_path / "design"
    for directory in ("tb", "rtl", "lists", "include"):
        (root / directory).mkdir(parents=True)
    sources = [root / "rtl/pkg.sv", root / "rtl/core.sv", root / "tb/top.sv"]
    sources[0].write_text("package design_pkg; parameter int W = 4; endpackage\n")
    sources[1].write_text(
        "module core #(parameter W=4)(input [W-1:0] d, output [W-1:0] q);\n"
        "assign q=d; endmodule\n"
    )
    sources[2].write_text(
        "module tb;\n"
        '`include "decls.svh"\n'
        "`ifdef INSTANTIATE_DUT\n"
        '`include "connections.sv"\n'
        "`endif\nendmodule\n"
    )
    decls = root / "include/decls.svh"
    fragment = root / "tb/connections.sv"
    decls.write_text("wire [3:0] d, q;\n")
    fragment.write_text("core #(.W(design_pkg::W)) u0(.d(d), .q(q));\n")
    outer, nested = root / "lists/design.f", root / "lists/rtl.f"
    outer.write_text(
        "$TW_REPLAY_ROOT/rtl/pkg.sv\n"
        "-F $TW_REPLAY_ROOT/lists/rtl.f\n"
        "$TW_REPLAY_ROOT/tb/top.sv\n"
    )
    nested.write_text("+define+INSTANTIATE_DUT\n+incdir+../include\n../rtl/core.sv\n")
    log = root / "tb/compile.log"
    log.write_text(
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog -f {outer} -top tb\n"
        + "".join(f"Parsing design file '{p}'\n" for p in sources)
        + f"Parsing included file '{decls}'\nBack to file '{sources[2]}'\n"
        + f"Parsing included file '{fragment}'\nBack to file '{sources[2]}'\n"
        + "Top Level Modules:\n       tb\n"
    )
    monkeypatch.delenv("TW_REPLAY_ROOT", raising=False)
    return dict(root=root, sources=sources, decls=decls, fragment=fragment,
                outer=outer, nested=nested, log=log)


@pytest.mark.parametrize("ambient", ["missing", "stale"])
def test_log_anchored_environment_preserves_included_dut_and_query_scope(
    replay_case, monkeypatch, ambient,
):
    c = replay_case
    if ambient == "stale":
        monkeypatch.setenv("TW_REPLAY_ROOT", "/unrelated/design")
    previous = os.environ.get("TW_REPLAY_ROOT")
    cr = parse_compile_log(str(c["log"]), "vcs")
    resolved = resolve_compile_environment(cr)
    assert resolved.complete
    assert resolved.bindings == {"TW_REPLAY_ROOT": str(c["root"])}
    hierarchy = build_hierarchy(cr, apply_source_overlay=False)
    assert hierarchy["component_tree"]["tb"]["u0"]["class"] == "core"
    plan = build_source_graph_plan(
        compile_log=str(c["log"]), compile_result=cr, hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(str(c["log"]), "vcs"),
        operation=QueryOperation.DRIVER, signal_path="tb.u0.q[2:1]",
        top_hint="tb", max_hops=8, frontend_version="11.0.0",
    )
    assert plan.request is not None
    assert plan.request.scope.hierarchy_ancestors == ("tb", "tb.u0")
    assert plan.request.scope.target.instance_path == "tb.u0"
    assert plan.request.identity.compile_inputs.complete
    assert plan.request.identity.compile_inputs.ordered_inputs == tuple(map(str, c["sources"]))
    assert os.environ.get("TW_REPLAY_ROOT") == previous


def test_kdb_compiles_ordered_units_and_keeps_include_fragment_in_parent(replay_case):
    c = replay_case
    cr = parse_compile_log(str(c["log"]), "vcs")
    assert str(c["fragment"]) in {f["path"] for f in cr["files"]["user"]}
    inputs = _extract_build_inputs(cr, top_hint=None)
    assert inputs["files"] == list(map(str, c["sources"]))
    assert inputs["incdirs"] == [str(c["root"] / "tb"), str(c["root"] / "include")]
    assert inputs["defines"] == ["INSTANTIATE_DUT=1"]
    command = _vericom_cmd("vericom", inputs)
    assert str(c["fragment"]) not in command
    assert str(c["decls"]) not in command
    assert command.index(str(c["sources"][0])) < command.index(str(c["sources"][2]))


@pytest.mark.parametrize("changed", ["fragment", "nested", "source"])
def test_kdb_dependency_contents_invalidate_cache_even_with_preserved_mtime(
    replay_case, changed,
):
    c = replay_case
    cr = parse_compile_log(str(c["log"]), "vcs")
    before = _extract_build_inputs(cr, top_hint=None)
    path = c["sources"][1] if changed == "source" else c[changed]
    old = path.stat()
    path.write_text(path.read_text() + "// changed dependency\n")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    after = _extract_build_inputs(cr, top_hint=None)
    assert before["hash"] != after["hash"]


def test_kdb_explicitly_compiled_header_remains_a_unit_and_changes_identity(replay_case):
    c = replay_case
    cr = parse_compile_log(str(c["log"]), "vcs")
    before = _extract_build_inputs(cr, top_hint=None)
    c["outer"].write_text(c["outer"].read_text() + str(c["fragment"]) + "\n")
    c["log"].write_text(c["log"].read_text() + f"Parsing design file '{c['fragment']}'\n")
    cr = parse_compile_log(str(c["log"]), "vcs")
    after = _extract_build_inputs(cr, top_hint=None)
    assert after["files"] == [*map(str, c["sources"]), str(c["fragment"])]
    assert after["hash"] != before["hash"]


def test_changed_filelist_order_cannot_authorize_inferred_hierarchy_or_kdb(replay_case):
    c = replay_case
    text = c["outer"].read_text().splitlines()
    c["outer"].write_text("\n".join(reversed(text)) + "\n")
    cr = parse_compile_log(str(c["log"]), "vcs")
    assert not resolve_compile_environment(cr).complete
    parsed = SystemVerilogPreprocessor(cr).preprocess(str(c["sources"][2]))
    assert "compile_options_incomplete" in parsed.issues
    assert "u0" not in parsed.trusted_hierarchy_text
    result = build_kdb(cr, cache_root=c["root"] / "cache")
    assert result["status"] == "failed" and result["phase"] == "precheck"
    assert "verified" in result["reason"]


def test_unresolved_option_variable_stops_replay_even_when_unit_order_matches(replay_case):
    c = replay_case
    c["nested"].write_text(c["nested"].read_text() + "+incdir+$TW_UNKNOWN_INCLUDE\n")
    cr = parse_compile_log(str(c["log"]), "vcs")
    assert "error" in _extract_build_inputs(cr, top_hint=None)
    assert "compile_options_incomplete" in SystemVerilogPreprocessor(cr).preprocess(
        str(c["sources"][2])
    ).issues


def test_explicit_uvm_package_does_not_inject_another_package(tmp_path):
    package = tmp_path / "uvm_pkg.sv"
    top = tmp_path / "top.sv"
    package.write_text("package uvm_pkg; endpackage\n")
    top.write_text("module tb; endmodule\n")
    cr = {"top_modules": ["tb"], "compile_command": f"vcs {package} {top}",
          "files": {"user": [{"path": str(p)} for p in (package, top)]}}
    inputs = _extract_build_inputs(cr, top_hint=None)
    assert "-ntb_opts" not in _vericom_cmd("vericom", inputs)
    assert inputs["files"] == [str(package), str(top)]


def test_replay_cancellation_does_not_build_or_claim_complete(replay_case):
    c = replay_case
    cr = parse_compile_log(str(c["log"]), "vcs")
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            _extract_build_inputs(cr, top_hint=None)
    finally:
        pop_cancel_event(token)
