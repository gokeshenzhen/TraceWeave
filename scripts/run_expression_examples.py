#!/usr/bin/env python3
"""Run the published examples over MCP stdio without a simulator or network."""
import json
import os
from pathlib import Path
import sys

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.expression_examples import check_example, expression_examples


async def run_examples():
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")],
        cwd=str(ROOT), env={**os.environ, "TRACEWEAVE_TELEMETRY": "0"})
    report = {"status": "passed", "model_adoption": "not_measured", "calls": []}
    with anyio.fail_after(30):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                catalog = {t.name: t for t in (await client.list_tools()).tools}
                for example in expression_examples(str(ROOT / "examples/expressions/demo.vcd")):
                    schema = catalog[example["tool"]].inputSchema
                    Draft202012Validator(schema).validate(example["arguments"])
                    result = await client.call_tool(example["tool"], example["arguments"])
                    assert not result.isError, result
                    payload = json.loads(result.content[0].text)
                    report["calls"].append({"tool": example["tool"], "arguments": example["arguments"],
                        "checked": check_example(example, payload), "status": "passed"})
    return report


if __name__ == "__main__":
    print(json.dumps(anyio.run(run_examples), indent=2))
