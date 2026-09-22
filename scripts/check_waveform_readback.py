#!/usr/bin/env python3
"""Check MCP catalog and readback; leave AI-client exposure explicitly unknown."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.readback_probe import PROBE_VCD, check_readback, client_probe_requests


def source_receipt():
    receipt = {"server_sha256": hashlib.sha256((ROOT / "server.py").read_bytes()).hexdigest()}
    try:
        receipt["commit"] = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
        receipt["dirty"] = bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain"], text=True,
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        receipt["commit"] = None
        receipt["dirty"] = None
    return receipt


async def run_check(work_dir, command):
    wave = work_dir / "readback_probe.vcd"
    if wave.exists():
        if wave.read_text() != PROBE_VCD:
            raise FileExistsError("Refusing to overwrite a different readback fixture")
    else:
        wave.write_text(PROBE_VCD, encoding="utf-8")
    env = {**os.environ, "TRACEWEAVE_TELEMETRY": "0", "TRACEWEAVE_NPI_EXECUTION": "local",
           "TRACEWEAVE_CACHE_DIR": str(work_dir / "cache")}
    before = source_receipt() if not command else None
    argv = command or [sys.executable, str(ROOT / "server.py")]
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env, cwd=work_dir)
    with anyio.fail_after(30):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                report = await check_readback(session, str(wave), initialized)
    report["launch_source"] = before
    report["launch_receipt_unchanged"] = before == source_receipt() if before else None
    report["fixture"] = str(wave)
    report["ai_client_probe_requests"] = client_probe_requests(str(wave))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="Keep the neutral fixture for a separate AI-client smoke session")
    parser.add_argument("--server-command", nargs=argparse.REMAINDER, help="Optional stdio server command and args; source commit is then unknown")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="traceweave-readback-") as temporary:
        work_dir = (args.work_dir or Path(temporary)).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            report = anyio.run(run_check, work_dir, args.server_command)
        except Exception as exc:
            report = {
                "status": "failed", "failure_reason": "probe_setup_or_transport_failed",
                "exception_type": type(exc).__name__,
                "server_catalog": {"status": "unknown"},
                "ai_client": {"version": None, "catalog_visibility": "unknown", "readback_calls": "unknown"},
                "model_adoption": "not_measured", "calls": [],
            }
        report["fixture_retained"] = args.work_dir is not None
        print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
