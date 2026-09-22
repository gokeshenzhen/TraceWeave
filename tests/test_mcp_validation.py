"""Exercise registered SDK validation through real MCP client/server sessions."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import server
from src.schemas import KeywordLimitErrorResult


WAVE = str(Path(__file__).parent / "fixtures" / "cycle_test.vcd")


@pytest.mark.anyio
async def test_keyword_limit_is_recoverable_before_any_search(monkeypatch):
    wave_work = Mock(side_effect=AssertionError("search must not execute"))
    recorded = Mock()
    monkeypatch.setattr(server, "_run_in_wave_thread", wave_work)
    monkeypatch.setattr(server.usage_telemetry, "record_call", recorded)
    async with create_connected_server_and_client_session(server.app) as session:
        listed = await session.list_tools()
        tool = next(t for t in listed.tools if t.name == "search_signals")
        assert tool.inputSchema["properties"]["keyword"]["anyOf"][1]["maxItems"] == 16
        result = await session.call_tool("search_signals", {
            "wave_path": "/private/project/wave.fsdb",
            "keyword": ["private_signal"] * 17,
        })

    assert result.isError is True
    payload = json.loads(result.content[0].text)
    error = KeywordLimitErrorResult.model_validate(payload)
    assert error.parameter == "keyword"
    assert error.provided_count == 17
    assert error.max_count == 16
    assert error.search_executed is False
    assert "16" in error.recovery and "Split" in error.recovery
    assert result.structuredContent == payload
    assert "private" not in result.model_dump_json()
    wave_work.assert_not_called()
    recorded.assert_not_called()  # pre-handler failures are explicitly outside telemetry


@pytest.mark.anyio
@pytest.mark.parametrize("keyword", ["data", ["data"], ["data"] * 16])
async def test_valid_searches_still_use_the_sdk_and_reader(keyword):
    async with create_connected_server_and_client_session(server.app) as session:
        result = await session.call_tool("search_signals", {
            "wave_path": WAVE, "keyword": keyword,
        })
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    entries = payload["batch"] if isinstance(keyword, list) else [payload]
    assert len(entries) == (len(keyword) if isinstance(keyword, list) else 1)
    assert all(e["results"][0]["path"] == "top_tb.data" for e in entries)


@pytest.mark.anyio
@pytest.mark.parametrize("name,arguments", [
    ("search_signals", {"wave_path": WAVE, "keyword": []}),
    ("search_signals", {"wave_path": WAVE, "keyword": [1]}),
    ("search_signals", {"wave_path": WAVE, "keyword": ["data", None]}),
    ("search_signals", {"wave_path": WAVE, "keyword": {"name": "data"}}),
    ("search_signals", {"keyword": "data"}),
    ("search_signals", {"wave_path": 123, "keyword": "data"}),
    ("search_signals", {"wave_path": WAVE, "keyword": "data", "max_results": "many"}),
    ("get_signal_at_time", {"wave_path": WAVE, "signal_path": "top_tb.data", "time_ps": []}),
])
async def test_other_invalid_inputs_keep_sdk_validation(monkeypatch, name, arguments):
    dispatch = Mock(side_effect=AssertionError("invalid input reached dispatch"))
    monkeypatch.setattr(server, "_dispatch", dispatch)
    async with create_connected_server_and_client_session(server.app) as session:
        result = await session.call_tool(name, arguments)
    assert result.isError is True
    assert result.content[0].text.startswith("Input validation error:")
    dispatch.assert_not_called()
