"""Exercise registered SDK validation through real MCP client/server sessions."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import server
from src.schemas import CycleInputErrorResult, KeywordLimitErrorResult


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


@pytest.mark.anyio
async def test_cycle_rejection_reports_all_conflicts_before_wave_work(monkeypatch):
    dispatch = Mock(side_effect=AssertionError("invalid cycle request reached dispatch"))
    recorded = Mock()
    monkeypatch.setattr(server, "_dispatch", dispatch)
    monkeypatch.setattr(server.usage_telemetry, "record_call", recorded)
    async with create_connected_server_and_client_session(server.app) as session:
        result = await session.call_tool("get_signals_by_cycle", {
            "wave_path": "/private/project/wave.fsdb", "clock_path": "private_clock",
            "signal_paths": ["private_signal"], "start_time_ps": 1000,
            "start_cycle": 0, "end_time_ps": 2000, "num_cycles": 2,
            "sample_offset_ps": -1,
        })
    assert result.isError is True
    payload = json.loads(result.content[0].text)
    error = CycleInputErrorResult.model_validate(payload)
    assert error.error_code == "invalid_cycle_arguments"
    assert len(error.issues) == 3
    assert "start_time_ps" in error.issues[0] and "start_cycle" in error.issues[0]
    assert "end_time_ps" in error.issues[1] and "num_cycles" in error.issues[1]
    assert "sample_offset_ps" in error.issues[2]
    assert error.sampling_executed is False
    assert "pre-edge" in error.recovery and "get_signals_around_time" in error.recovery
    assert "private" not in result.model_dump_json()
    assert result.structuredContent == payload
    dispatch.assert_not_called()
    recorded.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("conflict", [
    {"start_time_ps": 0, "start_cycle": 0},
    {"end_time_ps": 2000, "num_cycles": 2},
    {"sample_offset_ps": -1},
])
async def test_cycle_recovery_preserves_pre_and_post_edge_values(conflict):
    base = {"wave_path": WAVE, "clock_path": "top_tb.clk", "signal_paths": ["top_tb.data"]}
    async with create_connected_server_and_client_session(server.app) as session:
        rejected = await session.call_tool("get_signals_by_cycle", {**base, **conflict})
        assert rejected.isError is True
        assert len(json.loads(rejected.content[0].text)["issues"]) == 1
        # The client chooses its intended sampling phase; the server never silently
        # coerces a negative offset or drops one of the conflicting bounds.
        after = await session.call_tool("get_signals_by_cycle", {**base, "num_cycles": 2})
        before = await session.call_tool("get_signals_around_time", {
            "wave_path": WAVE, "signal_paths": ["top_tb.data"],
            "center_time_ps": 499, "window_ps": 0, "extra_transitions": 0,
            "return_mode": "values_only",
        })
    assert after.isError is False and before.isError is False
    cycles = json.loads(after.content[0].text)["cycles"]
    assert [(r["time_ps"], r["signals"]["top_tb.data"]["dec"]) for r in cycles] == [(500, 1), (1500, 2)]
    assert json.loads(before.content[0].text)["signals"]["top_tb.data"]["value_at_center"]["dec"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", [
    {"sample_offset_ps": "-1"}, {"sample_offset_ps": True}, {"sample_offset_ps": []},
    {"num_cycles": -1}, {"edge": "unknown"}, {"unsupported_parameter": 1},
])
async def test_unrelated_cycle_errors_keep_sdk_validation(monkeypatch, invalid):
    dispatch = Mock(side_effect=AssertionError("invalid input reached dispatch"))
    monkeypatch.setattr(server, "_dispatch", dispatch)
    async with create_connected_server_and_client_session(server.app) as session:
        result = await session.call_tool("get_signals_by_cycle", {
            "wave_path": WAVE, "clock_path": "top_tb.clk",
            "signal_paths": ["top_tb.data"], **invalid,
        })
    assert result.isError is True
    assert result.content[0].text.startswith("Input validation error:")
    dispatch.assert_not_called()
