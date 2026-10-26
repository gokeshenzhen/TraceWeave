"""Workflow opt-out controls, including fresh-server MCP startup and readback."""

import json
import os
from pathlib import Path
import subprocess
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

import server
from src import divergence_context


FLAG = "TRACEWEAVE_STRUCTURAL_SCAN_IN_WORKFLOW"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_session():
    server.reset_session_state()
    yield
    server.reset_session_state()


@pytest.mark.parametrize("value,enabled", [
    (None, True), ("", True), ("1", True), ("0", False),
    ("false", False), (" OFF ", False), ("no", False), ("invalid", True),
])
def test_startup_flag_defaults_to_enabled(value, enabled):
    env = {**os.environ}
    env.pop(FLAG, None)
    if value is not None:
        env[FLAG] = value
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.STRUCTURAL_SCAN_IN_WORKFLOW)"],
        cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=10,
    )
    assert result.stdout.strip() == str(enabled)


@pytest.mark.parametrize("prepared", [False, True])
def test_opt_out_removes_scan_relays_but_keeps_context_recovery(monkeypatch, prepared):
    monkeypatch.setattr(divergence_context, "STRUCTURAL_SCAN_IN_WORKFLOW", False)
    context = dict(
        compile_log="/exact/primary.log", supplementary_compile_logs=["/exact/extra.log"],
        simulator="vcs", top_hint="top", hierarchy_handle="tbh_historical",
        snapshot_sha256="a" * 64, source_snapshot_sha256="b" * 64,
        compile_log_identity_sha256="c" * 64,
    )
    if prepared:
        context["design_handle"] = "dc_expired"
    action = divergence_context.driver_action(
        result=dict(wave_path_a="/waves/a.vcd", signal_a="top.q", first_divergence_time_ps=42),
        side="a", context=context, reason="handle_expired",
    )
    assert [row["tool"] for row in action["prerequisite_calls"]] == (
        ["prepare_design_context"] if prepared else []
    )
    assert action["status"] == ("needs_prerequisite" if prepared else "ready")


@pytest.mark.anyio
@pytest.mark.parametrize("sweep_complete", [False, True])
async def test_opt_out_keeps_handshakes_optional_with_explicit_coverage(
    tmp_path, monkeypatch, sweep_complete
):
    monkeypatch.setattr(server, "STRUCTURAL_SCAN_IN_WORKFLOW", False)
    log = tmp_path / "run.log"
    log.write_text("UVM_ERROR /tmp/top.sv(2) @ 10 ns: reporter [SCB] compare failed\n")
    wave = str(tmp_path / "wave.vcd")
    server._session_state["get_sim_paths"] = dict(
        compile_log="/exact/compile.log", simulator="vcs", verif_root=str(tmp_path)
    )
    if sweep_complete:
        server._result_cache["sweep_handshakes"] = server.schemas.HandshakeSweepResult(
            wave_path=wave, discovered_count=1, interface_count=1, flagged_count=0,
            coverage_status="complete",
        )
        server._result_provenance["sweep_handshakes"] = {"wave_path": wave}

    class Analyzer:
        def __init__(self, **kwargs):
            pass

        def recommend_debug_next_steps(self, **kwargs):
            assert kwargs["structural_risks"] is None
            return dict(
                suspected_failure_class="data-path corruption",
                primary_failure_target={"time_ps": 10000},
            )

    monkeypatch.setattr(server, "WaveformAnalyzer", Analyzer)
    monkeypatch.setattr(server, "_get_parser", lambda path: object())
    result = await server._dispatch_inner("recommend_failure_debug_next_steps", dict(
        log_path=str(log), wave_path=wave, simulator="vcs",
    ))
    assert result["workflow_incomplete"] is False
    assert result["degraded_reason"] is None
    assert result["required_next_call"] is None
    assert result["runtime_protocol_coverage"]["coverage_status"] == (
        "complete" if sweep_complete else "not_run"
    )
    assert any(FLAG + "=0" in why and "unchecked" in why for why in result["why"])


@pytest.mark.anyio
@pytest.mark.parametrize("workflow_enabled", [True, False])
async def test_fresh_stdio_workflow_policy_and_explicit_scan(tmp_path, workflow_enabled):
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [3:0] mode, output logic y);\n"
        "  always_comb if (mode == 4'h2) y = 1'b1;\nendmodule\n"
    )
    compile_log = tmp_path / "compile.log"
    compile_log.write_text(
        f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top top\n"
        f"Parsing design file '{source}'\nTop Level Modules:\n       top\n"
    )
    log = tmp_path / "run.log"
    log.write_text("UVM_ERROR /tmp/top.sv(2) @ 10 ns: reporter [SCB] compare failed\n")
    wave = tmp_path / "wave.vcd"
    wave.write_text(
        "$timescale 1ps $end\n$scope module top $end\n$var wire 1 ! y $end\n"
        "$upscope $end\n$enddefinitions $end\n#0\n0!\n#10\n1!\n"
    )
    env = {**os.environ, "TRACEWEAVE_CACHE_DIR": str(tmp_path / "cache"),
           "TRACEWEAVE_TELEMETRY": "0", "TRACEWEAVE_NPI_EXECUTION": "local"}
    env.pop(FLAG, None)
    if not workflow_enabled:
        env[FLAG] = "0"
    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT), env=env,
    )
    with anyio.fail_after(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                if workflow_enabled:
                    assert "2. Run scan_structural_risks" in initialized.instructions
                else:
                    assert FLAG + "=0" in initialized.instructions
                    assert "Do not call or resume scan_structural_risks" in initialized.instructions
                    assert "2. Run scan_structural_risks" not in initialized.instructions
                assert "scan_structural_risks" in {tool.name for tool in (await client.list_tools()).tools}

                async def call(name, **args):
                    response = await client.call_tool(name, args)
                    assert not response.isError, response
                    value = json.loads(response.content[0].text)
                    assert value.get("error") is not True, value
                    return value

                paths = await call("get_sim_paths", verif_root=str(tmp_path),
                                   compile_log=str(compile_log), sim_log=str(log), wave_file=str(wave))
                if workflow_enabled:
                    assert paths["next_required_step"]["tool"] == "scan_structural_risks"
                else:
                    assert paths.get("next_required_step") is None
                    assert any(FLAG + "=0" in hint for hint in paths["hints"])
                parsed = await call("parse_sim_log", log_path=str(log), simulator="vcs")
                assert parsed["runtime_total_errors"] == 1
                snapshot = await call("get_diagnostic_snapshot")
                missing = {step["tool"] for step in snapshot.get("missing_steps") or []}
                assert ("scan_structural_risks" in missing) is workflow_enabled
                assert "sweep_handshakes" not in missing
                assert snapshot["protocol_health"]["summary"]["coverage_status"] == "not_run"
                assert "optional batch investigation" in initialized.instructions
                if not workflow_enabled:
                    assert snapshot["structural_scan"]["available"] is False
                    assert snapshot["structural_scan"]["summary"]["status"] == "skipped_by_policy"
                    assert snapshot["structural_scan"]["summary"]["coverage_status"] == "not_run"

                scan = await call("scan_structural_risks", compile_log=str(compile_log),
                                  simulator="vcs", analysis_mode="fast", categories=["magic_condition"])
                assert scan["files_scanned"] == 1
                assert scan["total_risks"] == 1
                assert scan["analysis_mode"] == "fast"
                snapshot = await call("get_diagnostic_snapshot")
                assert snapshot["structural_scan"]["available"] is True
                assert snapshot["structural_scan"]["summary"]["total_risks"] == 1
                if not workflow_enabled:
                    assert snapshot["structural_scan"]["summary"]["workflow_enabled"] is False
                    assert "status" not in snapshot["structural_scan"]["summary"]
