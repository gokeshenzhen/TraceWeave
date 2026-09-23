"""Client-visible expression schemas and the actual MCP transport contract."""
import json
from pathlib import Path
import sys

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

import server
from src.expression_examples import expression_examples
from scripts.run_expression_examples import run_examples
from tests.test_expression_observe import wave


@pytest.fixture
def anyio_backend():
    return "asyncio"


def check_local_refs(schema):
    def visit(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                assert ref.startswith("#/")
                target = schema
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                assert isinstance(target, dict)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)


@pytest.mark.anyio
async def test_all_tool_refs_resolve_and_definition_budget():
    tools = await server.list_tools()
    for tool in tools:
        Draft202012Validator.check_schema(tool.inputSchema)
        check_local_refs(tool.inputSchema)
    data = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
    # 207,896-byte baseline, including all tools. Reserve room for guidance
    # and examples while retaining at least a 25% reduction on the wire.
    assert size <= 155_922, size


@pytest.mark.anyio
async def test_published_examples_match_catalog_and_run_over_stdio():
    catalog = {t.name: t for t in await server.list_tools()}
    for example in expression_examples():
        schema = catalog[example["tool"]].inputSchema
        assert schema["examples"] == [example["arguments"]]
        Draft202012Validator(schema).validate(example["arguments"])
    report = await run_examples()
    assert report["status"] == "passed"
    assert len(report["calls"]) == 3


@pytest.mark.anyio
async def test_signal_inputs_keep_legacy_and_recursive_type_constraints():
    tools = {t.name: t for t in await server.list_tools()}
    validator = Draft202012Validator(tools["get_signal_at_time"].inputSchema)
    valid = [
        "tb.data",
        {"path": "tb.data", "lsb": 0, "width": 8},
        {"path": "tb.data", "bits": [7, 3]},
        {"expr": "a[i]", "typing": "wave_bits", "bindings": {"a": "tb.data", "i": "tb.index"}},
        {"expr": "rows[i].payload.flag", "bindings": {
            "rows": {"elements": [{"indices": [0], "signal": "tb.rows[0]"}]}, "i": "tb.index"},
         "types": {"i": {"width": 3}, "rows": {"width": 8, "unpacked": [[0, 7]],
            "members": [{"name": "payload", "lsb": 0, "type": {"width": 8,
                "members": [{"name": "flag", "lsb": 0, "type": {"width": 1}}]}}]}}},
    ]
    for signal in valid:
        validator.validate({"wave_path": "a.vcd", "signal_path": signal, "time_ps": 0})
    invalid = [
        {"path": "tb.data", "lsb": 0, "width": 0},
        {"path": "tb.data", "bits": [False]},
        {"expr": "a", "typing": "guess"},
        {"expr": "a", "types": {"a": {"width": 4097}}},
        {"expr": "a", "types": {"a": {"width": 8, "packed": [[7]]}}},
        {"expr": "a", "bindings": {"a": {"elements": [{"indices": [], "signal": "tb.a"}]}}},
    ]
    for signal in invalid:
        assert not validator.is_valid({"wave_path": "a.vcd", "signal_path": signal, "time_ps": 0}), signal


@pytest.mark.anyio
async def test_expression_list_and_call_over_stdio(tmp_path):
    source = wave(tmp_path)
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / "server.py")], cwd=str(root))
    with anyio.fail_after(30):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listing = await client.list_tools()
                tool = next(t for t in listing.tools if t.name == "get_signal_at_time")
                args = {"wave_path": source.file_path, "time_ps": 5, "signal_path": {
                    "expr": "a[i] + e", "typing": "wave_bits",
                    "bindings": {"a": "tb.data", "i": "tb.index", "e": "tb.expected"}}}
                Draft202012Validator(tool.inputSchema).validate(args)
                result = await client.call_tool(tool.name, args)
                assert not result.isError
                payload = json.loads(result.content[0].text)
                assert payload["value"]["dec"] == 6
                assert payload["expressions"][0]["coverage_status"] == "complete"
