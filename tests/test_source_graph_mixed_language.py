from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import pytest

from src.compile_log_parser import merge_compile_results, parse_compile_log
from src.connectivity_ir import ConnectivityIR
from src.connectivity_query import ConnectivityQueryEngine, QueryStatus
from src.hierarchy_handles import compute_snapshot_fingerprint
from src.source_graph_adapter import AdapterStatus, build_source_graph_plan
from src.source_graph_contract import QueryOperation
from src.source_graph_runtime import IsolatedSourceGraphProcessRunner, PrepareStatus
from src.tb_hierarchy_builder import build_hierarchy


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_PYTHON = ROOT / ".venv" / "bin" / "python"


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


@pytest.mark.anyio
async def test_frontend_error_keeps_mixed_language_positive_facts_usable(tmp_path):
    _require_pinned_frontend()
    sv = tmp_path / "top.sv"
    vhdl = tmp_path / "leaf.vhd"
    compile_log = tmp_path / "sv_compile.log"
    vhdl_log = tmp_path / "vhdl_compile.log"
    elaborate_log = tmp_path / "elaborate.log"
    sv.write_text(
        """module sv_top(input logic a, output logic local_y);
  logic from_vhdl;
  vhdl_leaf u_vhdl (.i(a), .o(from_vhdl));
  assign local_y = a;
endmodule
""",
        encoding="utf-8",
    )
    vhdl.write_text(
        """entity vhdl_leaf is
  port (i : in bit; o : out bit);
end entity vhdl_leaf;
architecture rtl of vhdl_leaf is begin o <= i; end architecture rtl;
""",
        encoding="utf-8",
    )
    compile_log.write_text(
        "Chronologic VCS simulator\n"
        f"Command: vlogan -sverilog {sv}\n"
        f"Parsing design file '{sv}'\n",
        encoding="utf-8",
    )
    vhdl_log.write_text(
        "Chronologic VCS simulator\n"
        f"Command: vhdlan -work WORK {vhdl}\n"
        f"Parsing design file '{vhdl}'\n",
        encoding="utf-8",
    )
    elaborate_log.write_text(
        "Chronologic VCS simulator\n"
        "Command: vcs -top sv_top\n"
        "Top Level Modules:\n"
        "       sv_top\n",
        encoding="utf-8",
    )
    primary = parse_compile_log(str(compile_log), "vcs")
    merged = merge_compile_results(
        primary,
        [
            parse_compile_log(str(vhdl_log), "vcs"),
            parse_compile_log(str(elaborate_log), "vcs"),
        ],
        primary_log=str(compile_log),
        supplementary_logs=[str(vhdl_log), str(elaborate_log)],
    )
    hierarchy = build_hierarchy(merged, compile_log_path=str(compile_log))
    snapshot = compute_snapshot_fingerprint(
        str(compile_log),
        "vcs",
        supplementary_compile_logs=(str(vhdl_log), str(elaborate_log)),
    )
    plan = build_source_graph_plan(
        compile_log=str(compile_log),
        compile_result=merged,
        hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=snapshot,
        operation=QueryOperation.DRIVER,
        signal_path="sv_top.local_y",
        top_hint="sv_top",
        max_hops=8,
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

    result = await worker.run(
        plan.request,
        timeout_seconds=10.0,
        cancel_event=asyncio.Event(),
    )

    assert result.status is PrepareStatus.READY
    assert result.ir_json_bytes is not None
    ir = ConnectivityIR.from_json_bytes(result.ir_json_bytes)
    gap_codes = {gap.code for gap in ir.coverage.gaps}
    assert "frontend_diagnostic:UnknownModule" in gap_codes
    assert "opaque_vhdl_boundary" in gap_codes
    engine = ConnectivityQueryEngine(ir)
    assert engine.query_driver("sv_top.local_y").status is QueryStatus.FOUND
    assert engine.query_loads("sv_top.a").status is QueryStatus.FOUND
    assert engine.query_driver("sv_top.from_vhdl").status is QueryStatus.INCONCLUSIVE
    assert list(staging.iterdir()) == []
