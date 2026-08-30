"""Console entry point for the existing TraceWeave stdio server."""

from __future__ import annotations

import asyncio


def main() -> None:
    """Run the same stdio server exposed by the repository-local server.py."""

    try:
        from ._runtime.server import main as server_main
    except ModuleNotFoundError as exc:
        # A raw source checkout has no physical ``_runtime`` directory: that
        # private package is produced by setuptools' package-dir mapping.
        if exc.name != "traceweave_mcp._runtime":
            raise
        from server import main as server_main

    asyncio.run(server_main())
