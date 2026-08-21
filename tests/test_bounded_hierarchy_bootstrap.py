from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import threading

import pytest

from config import BoundedBootstrapConfig, SourceGraphExecutionConfig
import server
from src import cancellation
from src.bounded_hierarchy_bootstrap import build_bounded_connectivity_context
from src.compile_log_parser import parse_compile_log
from src.connectivity_ir import ConnectivityIR
from src.connectivity_query import ConnectivityQueryEngine, QueryStatus
from src.source_graph_adapter import AdapterStatus, build_source_graph_plan
from src.source_graph_contract import QueryOperation
from src.source_graph_runtime import IsolatedSourceGraphProcessRunner, PrepareStatus


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_PYTHON = ROOT / ".venv" / "bin" / "python"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _vcs_context(tmp_path: Path, sources: list[Path], *, top: str = "top"):
    compile_log = tmp_path / "comp.log"
    command_sources = " ".join(str(path) for path in sources)
    parse_lines = "".join(
        f"Parsing design file '{path}'\n" for path in sources
    )
    _write(
        compile_log,
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {command_sources} -top {top}\n"
        f"{parse_lines}"
        "Top Level Modules:\n"
        f"       {top}\n",
    )
    return compile_log, parse_compile_log(str(compile_log), "vcs")


def _config(**overrides) -> BoundedBootstrapConfig:
    values = {
        "timeout_sec": 5.0,
        "max_source_inputs": 16,
        "max_source_bytes": 4 * 1024 * 1024,
        "max_inventory_files": 128,
        "max_inventory_bytes": 8 * 1024 * 1024,
        "max_include_depth": 8,
        "max_hierarchy_depth": 16,
    }
    values.update(overrides)
    return BoundedBootstrapConfig(**values)


def _require_pinned_frontend() -> None:
    if not FRONTEND_PYTHON.is_file():
        pytest.skip("repository Source Graph frontend environment is unavailable")
    probe = subprocess.run(
        [
            str(FRONTEND_PYTHON),
            "-c",
            (
                "import importlib.metadata; import pyslang; "
                "print(importlib.metadata.version('pyslang'))"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "11.0.0":
        pytest.skip("pinned pyslang 11.0.0 frontend is unavailable")


def test_bootstrap_selects_only_proved_ancestor_source_closure(tmp_path):
    top = tmp_path / "top.sv"
    child = tmp_path / "child.sv"
    unrelated = tmp_path / "unrelated.sv"
    _write(top, "module top; child u_child(); endmodule\n")
    _write(
        child,
        "module child(input logic a, output logic y);\n"
        "  logic gate_en;\n"
        "  assign gate_en = a;\n"
        "  assign y = gate_en;\n"
        "endmodule\n",
    )
    _write(unrelated, "module unrelated; endmodule\n")
    compile_log, compile_result = _vcs_context(
        tmp_path, [top, child, unrelated]
    )

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="a" * 64,
        signal_path="top.u_child.gate_en",
        top_hint=None,
        config=_config(),
    )

    assert result.status == "ready"
    assert result.receipt["ancestor_chain_proved"] is True
    assert result.receipt["coverage_status"] == "inconclusive"
    assert result.receipt["metrics"]["selected_source_input_count"] == 2
    selected = [
        item["path"]
        for item in result.compile_result["compile_evidence"][
            "ordered_compilation_units"
        ]
    ]
    assert selected == [str(top.resolve()), str(child.resolve())]
    assert str(unrelated.resolve()) not in selected
    assert (
        result.hierarchy_result["component_tree"]["top"]["u_child"]["class"]
        == "child"
    )

    plan = build_source_graph_plan(
        compile_log=str(compile_log),
        compile_result=result.compile_result,
        hierarchy_result=result.hierarchy_result,
        hierarchy_snapshot_sha256="a" * 64,
        operation=QueryOperation.LOADS,
        signal_path="top.u_child.gate_en",
        top_hint=None,
        max_hops=1,
        frontend_version="11.0.0",
    )
    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(top.resolve()), str(child.resolve()))
    assert manifest.complete is False
    assert "bootstrap_compile_inputs_scoped" in plan.receipt.objective_exclusions
    assert plan.receipt.cross_request_reusable is False


def test_bootstrap_adapter_drops_unbounded_library_search_options(tmp_path):
    top = tmp_path / "top.sv"
    library = tmp_path / "large_library.v"
    library_dir = tmp_path / "lib"
    library_dir.mkdir()
    _write(top, "module top; logic value; endmodule\n")
    _write(library, "module library_cell; endmodule\n")
    compile_log = tmp_path / "comp.log"
    _write(
        compile_log,
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog -v {library} -y {library_dir} {top} -top top\n"
        f"Parsing design file '{top}'\n"
        "Top Level Modules:\n"
        "       top\n",
    )
    compile_result = parse_compile_log(str(compile_log), "vcs")
    bounded = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="7" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(),
    )
    assert bounded.status == "ready"

    plan = build_source_graph_plan(
        compile_log=str(compile_log),
        compile_result=bounded.compile_result,
        hierarchy_result=bounded.hierarchy_result,
        hierarchy_snapshot_sha256="7" * 64,
        operation=QueryOperation.LOADS,
        signal_path="top.value",
        top_hint=None,
        max_hops=1,
        frontend_version="11.0.0",
    )

    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    manifest = plan.request.identity.compile_inputs
    assert manifest.ordered_inputs == (str(top.resolve()),)
    assert "-v" not in manifest.ordered_options
    assert "-y" not in manifest.ordered_options
    assert "bootstrap_library_context_scoped" in (
        plan.receipt.objective_exclusions
    )


def test_bootstrap_blocks_ambiguous_module_definition(tmp_path):
    top = tmp_path / "top.sv"
    duplicate_a = tmp_path / "dup_a.sv"
    duplicate_b = tmp_path / "dup_b.sv"
    _write(top, "module top; dup u_dup(); endmodule\n")
    _write(duplicate_a, "module dup; endmodule\n")
    _write(duplicate_b, "module dup; endmodule\n")
    _, compile_result = _vcs_context(
        tmp_path, [top, duplicate_a, duplicate_b]
    )

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="b" * 64,
        signal_path="top.u_dup.value",
        top_hint=None,
        config=_config(),
    )

    assert result.status == "blocked"
    assert result.receipt["blocker"] == {
        "code": "bootstrap_definition_ambiguous",
        "stage": "definition_inventory",
    }


def test_bootstrap_blocks_external_compilation_unit_macro_context(tmp_path):
    macro_owner = tmp_path / "macro_owner.sv"
    top = tmp_path / "top.sv"
    _write(macro_owner, "`define FEATURE\nmodule helper; endmodule\n")
    _write(
        top,
        "module top;\n"
        "`ifdef FEATURE\n"
        "  logic selected;\n"
        "`endif\n"
        "endmodule\n",
    )
    _, compile_result = _vcs_context(tmp_path, [macro_owner, top])

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="c" * 64,
        signal_path="top.selected",
        top_hint=None,
        config=_config(),
    )

    assert result.status == "blocked"
    assert result.receipt["blocker"]["code"] == (
        "bootstrap_preprocessor_context_unproved"
    )


def test_bootstrap_source_byte_cap_is_hard(tmp_path):
    top = tmp_path / "top.sv"
    _write(top, "module top; logic value; endmodule\n")
    _, compile_result = _vcs_context(tmp_path, [top])

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="d" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(max_source_bytes=1),
    )

    assert result.status == "blocked"
    assert result.receipt["blocker"]["code"] == (
        "bootstrap_source_byte_limit_exceeded"
    )


def test_bootstrap_inventory_caps_are_hard(tmp_path):
    top = tmp_path / "top.sv"
    unrelated = tmp_path / "unrelated.sv"
    _write(top, "module top; logic value; endmodule\n")
    _write(unrelated, "module unrelated; endmodule\n")
    _, compile_result = _vcs_context(tmp_path, [top, unrelated])

    file_limited = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="e" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(max_inventory_files=1),
    )
    byte_limited = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="e" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(max_inventory_bytes=1),
    )

    assert file_limited.receipt["blocker"]["code"] == (
        "bootstrap_inventory_file_limit_exceeded"
    )
    assert byte_limited.receipt["blocker"]["code"] == (
        "bootstrap_inventory_byte_limit_exceeded"
    )


def test_bootstrap_source_count_and_hierarchy_depth_caps_are_hard(tmp_path):
    top = tmp_path / "top.sv"
    child = tmp_path / "child.sv"
    grand = tmp_path / "grand.sv"
    _write(top, "module top; child u_child(); endmodule\n")
    _write(child, "module child; grand u_grand(); endmodule\n")
    _write(grand, "module grand; logic value; endmodule\n")
    _, compile_result = _vcs_context(tmp_path, [top, child, grand])

    input_limited = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="f" * 64,
        signal_path="top.u_child.value",
        top_hint=None,
        config=_config(max_source_inputs=1),
    )
    depth_limited = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="f" * 64,
        signal_path="top.u_child.u_grand.value",
        top_hint=None,
        config=_config(max_hierarchy_depth=1),
    )

    assert input_limited.receipt["blocker"]["code"] == (
        "bootstrap_source_input_limit_exceeded"
    )
    assert depth_limited.receipt["blocker"]["code"] == (
        "bootstrap_hierarchy_depth_exceeded"
    )


def test_bootstrap_include_depth_cap_is_hard(tmp_path):
    top = tmp_path / "top.sv"
    first = tmp_path / "first.svh"
    second = tmp_path / "second.svh"
    _write(top, '`include "first.svh"\nmodule top; logic value; endmodule\n')
    _write(first, '`include "second.svh"\n')
    _write(second, "`define SECOND_PRESENT\n")
    _, compile_result = _vcs_context(tmp_path, [top])
    compile_result["include_tree"] = {
        str(top.resolve()): [str(first.resolve())],
        str(first.resolve()): [str(second.resolve())],
    }

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="1" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(max_include_depth=1),
    )

    assert result.receipt["blocker"]["code"] == (
        "bootstrap_include_depth_exceeded"
    )


def test_bootstrap_inventory_ignores_commented_definitions(tmp_path):
    top = tmp_path / "top.sv"
    commented = tmp_path / "commented.sv"
    _write(top, "module top; logic value; endmodule\n")
    _write(commented, "/* module top; endmodule */\nmodule other; endmodule\n")
    _, compile_result = _vcs_context(tmp_path, [top, commented])

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="2" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(),
    )

    assert result.status == "ready"
    assert result.receipt["metrics"]["selected_source_input_count"] == 1


def test_bootstrap_does_not_expand_the_simulator_uvm_library(tmp_path):
    top = tmp_path / "top.sv"
    _write(
        top,
        "import uvm_pkg::*;\n"
        "module top; logic source; logic value; assign value = source; endmodule\n",
    )
    _, compile_result = _vcs_context(tmp_path, [top])

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="4" * 64,
        signal_path="top.source",
        top_hint=None,
        config=_config(),
    )

    assert result.status == "ready"
    assert result.receipt["metrics"]["selected_source_input_count"] == 1
    assert "uvm_dynamic_connectivity" in result.receipt["objective_exclusions"]


@pytest.mark.anyio
async def test_bootstrap_uvm_exclusion_keeps_local_positive_fact(tmp_path):
    _require_pinned_frontend()
    top = tmp_path / "top.sv"
    _write(
        top,
        "import uvm_pkg::*;\n"
        "module top(input logic source, output logic value);\n"
        "  assign value = source;\n"
        "endmodule\n",
    )
    compile_log, compile_result = _vcs_context(tmp_path, [top])
    bounded = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="5" * 64,
        signal_path="top.source",
        top_hint=None,
        config=_config(),
    )
    assert bounded.status == "ready"
    plan = build_source_graph_plan(
        compile_log=str(compile_log),
        compile_result=bounded.compile_result,
        hierarchy_result=bounded.hierarchy_result,
        hierarchy_snapshot_sha256="5" * 64,
        operation=QueryOperation.LOADS,
        signal_path="top.source",
        top_hint=None,
        max_hops=1,
        frontend_version="11.0.0",
    )
    assert plan.status is AdapterStatus.READY
    assert plan.request is not None
    staging = tmp_path / "staging"
    staging.mkdir()
    worker = IsolatedSourceGraphProcessRunner(
        python_executable=FRONTEND_PYTHON,
        staging_directory=staging,
    )

    built = await worker.run(
        plan.request,
        timeout_seconds=10.0,
        cancel_event=asyncio.Event(),
    )

    assert built.status is PrepareStatus.READY
    assert built.ir_json_bytes is not None
    engine = ConnectivityQueryEngine(
        ConnectivityIR.from_json_bytes(built.ir_json_bytes)
    )
    assert engine.query_loads("top.source").status is QueryStatus.FOUND
    assert list(staging.iterdir()) == []


def test_bootstrap_observes_cooperative_cancellation(tmp_path):
    top = tmp_path / "top.sv"
    _write(top, "module top; logic value; endmodule\n")
    _, compile_result = _vcs_context(tmp_path, [top])
    event = threading.Event()
    event.set()
    token = cancellation.push_cancel_event(event)
    try:
        with pytest.raises(cancellation.OperationCancelled):
            build_bounded_connectivity_context(
                compile_result=compile_result,
                hierarchy_snapshot_sha256="3" * 64,
                signal_path="top.value",
                top_hint=None,
                config=_config(),
            )
    finally:
        cancellation.pop_cancel_event(token)


def test_bootstrap_timeout_is_a_structured_blocker(tmp_path):
    top = tmp_path / "top.sv"
    _write(top, "module top; logic value; endmodule\n")
    _, compile_result = _vcs_context(tmp_path, [top])

    result = build_bounded_connectivity_context(
        compile_result=compile_result,
        hierarchy_snapshot_sha256="6" * 64,
        signal_path="top.value",
        top_hint=None,
        config=_config(timeout_sec=0.0),
    )

    assert result.status == "blocked"
    assert result.receipt["blocker"] == {
        "code": "bootstrap_timeout",
        "stage": "target_scope",
    }


@pytest.mark.anyio
async def test_explicit_bootstrap_bypasses_prerequisite_but_not_static_fallback(
    monkeypatch, tmp_path
):
    server.reset_session_state()
    compile_log = tmp_path / "comp.log"
    _write(
        compile_log,
        "Chronologic VCS simulator\nTop Level Modules:\n       top\n",
    )
    args = {
        "signal_path": "top.value",
        "compile_log": str(compile_log),
        "simulator": "vcs",
    }
    assert server._check_prerequisites("find_signal_loads", args) is not None
    args["allow_bounded_bootstrap"] = True
    assert server._check_prerequisites("find_signal_loads", args) is None

    monkeypatch.setattr(
        server,
        "get_source_graph_execution_config",
        lambda: SourceGraphExecutionConfig(
            enabled=False,
            python_bin="python3",
            frontend_version="11.0.0",
            timeout_sec=5.0,
        ),
    )

    def forbidden_static(*_args, **_kwargs):
        raise AssertionError("bounded bootstrap must not start Legacy Static")

    monkeypatch.setattr(
        "src.connectivity_backend.StaticConnectivityBackend.find_loads",
        forbidden_static,
    )
    result = await server._dispatch("find_signal_loads", args)

    assert result.backend == "source_graph"
    assert result.loads == []
    assert result.claim_semantics.negative_claim_allowed is False
    assert result.backend_status.source_graph.fallback_used is False
    assert result.backend_status.source_graph.bootstrap_context["used"] is True
