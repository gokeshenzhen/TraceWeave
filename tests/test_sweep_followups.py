"""Coverage decisions from recorded MCP receipts and bounded synthetic controls."""

from copy import deepcopy
import json
from pathlib import Path

import jsonschema
import pytest

import server
from src.handshake_sweep import build_sweep_followup_actions
from src.schemas import HandshakeSweepResult, SuggestedToolCall


def receipt(**overrides):
    result = dict(
        wave_path="/tmp/run.vcd", scope="top", start_ps=0, end_ps=100,
        edge="negedge", max_interfaces=2, max_wait_cycles=3,
        coverage_status="truncated", truncated=True,
        discovered_count=4, interface_count=2, flagged_count=1,
        interfaces=[dict(scope="top.if0", kind="valid_ready", valid="top.if0.valid",
                         ready="top.if0.ready", flags=["ended_in_stall"])],
    )
    result.update(overrides)
    return result


@pytest.mark.anyio
@pytest.mark.parametrize("index", ["06", "09"])
async def test_recorded_three_gap_receipts_replace_global_cap_retry(index):
    fixture = Path(__file__).parent / "fixtures" / "sweep_coverage" / f"caliptra_{index}.json"
    raw = json.loads(fixture.read_text())["result"]
    before = deepcopy(raw)
    actions = build_sweep_followup_actions(raw)
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert actions and all(action["tool"] != "sweep_handshakes" for action in actions)
    for action in actions:
        assert action["reason_code"] == "discovery_incomplete"
        assert action["arguments"]["scope"] in [row["scope"] for row in raw["interfaces"]]
        assert action["arguments"]["wave_path"] == raw["wave_path"]
        assert "transition_truncated" in action["remaining_gaps"]
        assert "interface_cap" in action["remaining_gaps"]
        jsonschema.validate(action["arguments"], tools[action["tool"]].inputSchema)
        SuggestedToolCall.model_validate(action)
    assert raw == before  # positive facts and original coverage are untouched

    # Old cached receipts still contain the counterproductive cap-only action.
    routed = server._build_sweep_suggested_calls(raw["wave_path"], HandshakeSweepResult.model_validate(raw))
    assert routed == actions
    child = actions[0]["arguments"]["scope"]
    repeated = {**raw, "scope": child,
                "interfaces": [row for row in raw["interfaces"] if row["scope"] == child]}
    assert build_sweep_followup_actions(repeated) == []


def test_only_interface_cap_preserves_window_and_sampling_parameters():
    raw = receipt()
    actions = build_sweep_followup_actions(raw)
    assert len(actions) == 1
    assert actions[0]["reason_code"] == "interface_cap"
    assert actions[0]["arguments"] == dict(
        wave_path="/tmp/run.vcd", scope="top", start_time_ps=0, end_time_ps=100,
        edge="negedge", max_interfaces=4, max_wait_cycles=3,
    )
    assert actions[0]["remaining_gaps"] == []


def test_unknown_time_anchor_does_not_invent_a_window_or_enlarge_cap():
    raw = receipt(transition_truncated_count=2, end_ps=-1)
    assert build_sweep_followup_actions(raw) == []
    raw = {k: v for k, v in raw.items() if k not in {"max_interfaces", "max_wait_cycles"}}
    assert server._build_sweep_suggested_calls(raw["wave_path"], HandshakeSweepResult(**raw)) == []


def test_observed_stall_anchor_offers_a_bounded_target_without_repeating_prefix():
    raw = receipt(transition_truncated_count=1)
    raw["interfaces"][0].update(transition_data_truncated=True, max_stall_begin_ps=40)
    action, = build_sweep_followup_actions(raw)
    assert action["arguments"] == dict(
        wave_path="/tmp/run.vcd", scope="top.if0", start_time_ps=40, end_time_ps=100,
        edge="negedge", max_interfaces=2, max_wait_cycles=3,
    )
    assert "outside_selected_window" in action["remaining_gaps"]
    assert "interface_cap" in action["remaining_gaps"]
    assert build_sweep_followup_actions({**raw, "scope": "top.if0", "start_ps": 40}) == []


@pytest.mark.parametrize("change", [
    {"scope": "top.if0"},
    {"start_time_ps": 40, "end_time_ps": 60},
])
def test_truncated_routing_accepts_smaller_scope_or_window(change):
    result = HandshakeSweepResult(
        wave_path="/tmp/run.vcd", scope="top", start_ps=0, end_ps=100,
        coverage_status="truncated", truncated=True, discovered_count=4,
        interface_count=2, transition_truncated_count=2,
    )
    action = dict(tool="sweep_handshakes", arguments={
        "wave_path": "/tmp/run.vcd", "scope": "top", **change,
    })
    assert server._sweep_action_makes_progress(action, result.wave_path, result)
    assert not server._sweep_action_makes_progress(
        {**action, "arguments": {**action["arguments"], "wave_path": "/tmp/other.vcd"}},
        result.wave_path, result,
    )
    result.suggested_next_actions = [action]
    assert server._build_sweep_suggested_call(result.wave_path, result) == action
    assert result.coverage_status == "truncated"


def test_unknown_semantics_or_clock_does_not_generate_another_sweep():
    raw = receipt(coverage_status="degraded", truncated=False,
                  skipped=[dict(scope="top.if1", reason="no clock found in scope/ancestors")])
    assert build_sweep_followup_actions(raw) == []


def test_zero_coverage_with_incomplete_discovery_does_not_widen_scope():
    raw = receipt(coverage_status="zero_coverage", truncated=False, interfaces=[],
                  discovery={"valid_ready": {"status": "partial"}})
    assert build_sweep_followup_actions(raw) == []
    raw = {k: v for k, v in raw.items() if k not in {"max_interfaces", "max_wait_cycles"}}
    result = HandshakeSweepResult(**raw)
    result.suggested_next_actions = [dict(
        tool="sweep_handshakes", arguments={"wave_path": raw["wave_path"]},
    )]
    assert server._build_sweep_suggested_call(result.wave_path, result) is None
