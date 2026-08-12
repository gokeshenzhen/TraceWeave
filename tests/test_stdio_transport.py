"""Regression coverage for the MCP/native-library stdout boundary."""

from pathlib import Path
import subprocess
import sys


def test_stdio_transport_isolates_native_and_python_stdout():
    script = """
import asyncio
import os
import server

async def exercise():
    protocol = server._prepare_stdio_transport()
    os.write(1, b"native-noise\\n")
    print("python-noise")
    await protocol.write("protocol-json\\n")
    await protocol.flush()
    await protocol.aclose()

asyncio.run(exercise())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "protocol-json\n"
    assert "native-noise" in completed.stderr
    assert "python-noise" in completed.stderr
