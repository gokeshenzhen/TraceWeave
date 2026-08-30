"""Console entry point for the existing TraceWeave stdio server."""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    """Run the same stdio server exposed by the repository-local server.py."""

    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--doctor":
        if arguments not in (["--doctor"], ["--doctor", "--json"]):
            print("usage: traceweave-mcp --doctor [--json]", file=sys.stderr)
            raise SystemExit(64)
        from .doctor import run_doctor

        raise SystemExit(run_doctor(json_output="--json" in arguments))

    try:
        from ._runtime.server import main as server_main
    except ModuleNotFoundError as exc:
        # A raw source checkout has no physical ``_runtime`` directory: that
        # private package is produced by setuptools' package-dir mapping.
        if exc.name != "traceweave_mcp._runtime":
            raise
        from server import main as server_main

    asyncio.run(server_main())
