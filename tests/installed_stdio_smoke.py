"""Exercise an installed TraceWeave console script through the MCP protocol."""

from __future__ import annotations

import os
import importlib.util
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    for generic_name in ("server", "config", "src"):
        assert importlib.util.find_spec(generic_name) is None
    assert importlib.util.find_spec("traceweave_mcp._runtime.server") is not None
    from traceweave_mcp._runtime.src.readback_probe import PROBE_VCD, check_readback

    executable = Path(sys.executable).with_name("traceweave-mcp")
    doctor = subprocess.run(
        [executable, "--doctor", "--json"],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    doctor_report = json.loads(doctor.stdout)
    assert doctor.stderr == ""
    assert doctor_report["installation_profile"] == "portable"
    assert doctor_report["base_runtime"]["ready"] is True
    assert doctor_report["fsdb"]["wrapper_present"] is False
    assert doctor_report["fsdb"]["status"] == "wrapper_missing"

    with tempfile.TemporaryDirectory(prefix="traceweave-installed-readback-") as temporary:
        work_dir = Path(temporary)
        wave = work_dir / "probe.vcd"
        wave.write_text(PROBE_VCD)
        params = StdioServerParameters(
            command=os.fspath(executable), args=[], cwd=Path.cwd(),
            env={**os.environ, "TRACEWEAVE_TELEMETRY": "0",
                 "TRACEWEAVE_CACHE_DIR": str(work_dir / "cache")},
        )
        with anyio.fail_after(30):
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    readback = await check_readback(session, str(wave), initialized)
    assert readback["status"] == "passed", readback

    tool_names = {tool.name for tool in listed.tools}
    package_version = version("traceweave-mcp")
    assert initialized.serverInfo.name == "traceweave"
    assert initialized.serverInfo.version == package_version
    assert {
        "get_sim_paths",
        "get_formal_paths",
        "parse_sim_log",
        "get_waveform_summary",
    } <= tool_names
    print(
        f"TraceWeave {package_version}: MCP initialize/list_tools/readback passed "
        f"with {len(tool_names)} tools"
    )


if __name__ == "__main__":
    anyio.run(main)
