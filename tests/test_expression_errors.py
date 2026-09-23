"""Public failures must let a client repair a request without guessing values."""
import json

import pytest

import server
from tests.test_expression_observe import wave


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_session():
    server.reset_session_state()
    yield
    server.reset_session_state()


async def point(path, spec, at=5):
    result = await server.call_tool("get_signal_at_time", {
        "wave_path": path, "signal_path": spec, "time_ps": at})
    return json.loads(result[0].text)


@pytest.mark.anyio
async def test_missing_type_error_can_be_repaired_with_declared_types(tmp_path):
    path = wave(tmp_path).file_path
    spec = {"expr": "a[i]", "bindings": {"a": "tb.data", "i": "tb.index"}}
    missing = await point(path, spec)
    assert missing["error_code"] == "expression_type_unresolved"
    assert missing["operand"] == "a"
    assert missing["recovery"]["action"] == "provide_types"
    assert "wave_bits" in missing["recovery"]["message"]
    fixed = await point(path, {**spec, "types": {
        "a": {"width": 8, "packed": [[7, 0]]}, "i": {"width": 3, "packed": [[2, 0]]}}})
    assert fixed["value"]["dec"] == 1
    assert fixed["expressions"][0]["coverage_status"] == "complete"


@pytest.mark.anyio
async def test_missing_binding_recovery_uses_actual_search_result(tmp_path):
    path = wave(tmp_path).file_path
    spec = {"expr": "a", "typing": "wave_bits", "bindings": {"a": "tb.typo"}}
    missing = await point(path, spec)
    assert missing["error_code"] == "expression_signal_unresolved"
    assert missing["operand"] == "a"
    assert missing["recovery"]["action"] == "resolve_binding"
    search = await server.call_tool("search_signals", {"wave_path": path, "keyword": "data"})
    found = json.loads(search[0].text)["results"][0]
    actual = found if isinstance(found, str) else found["path"]
    fixed = await point(path, {**spec, "bindings": {"a": actual}})
    assert fixed["value"]["dec"] == 8


@pytest.mark.anyio
@pytest.mark.parametrize("spec,code,action", [
    ({"expr": "a @ i"}, "expression_syntax_invalid", "correct_expression"),
    ({"expr": "a["}, "expression_syntax_invalid", "correct_expression"),
    ({"expr": "f(a)"}, "expression_unsupported", "rewrite_expression"),
    ({"expr": "$onehot(a,i)"}, "expression_operand_invalid", "correct_operand"),
    ({"expr": "a[0+:i]"}, "expression_constant_required", "provide_constant"),
    ({"expr": "W", "constants": {"W": "runtime_signal"}}, "expression_constant_required", "provide_constant"),
    ({"expr": "a", "types": {"a": {"width": 7}}}, "expression_shape_invalid", "correct_type_mapping"),
    ({"expr": "a", "types": {"a": {"width": 0}}}, "expression_input_invalid", "correct_input"),
    ({"expr": "(" * 40 + "a" + ")" * 40}, "expression_limit_exceeded", "reduce_request"),
])
async def test_public_error_categories(tmp_path, spec, code, action):
    path = wave(tmp_path).file_path
    result = await point(path, {"typing": "wave_bits", "bindings": {"a": "tb.data", "i": "tb.index"}, **spec})
    assert result["error_code"] == code, result
    assert result["recovery"]["action"] == action
    assert "value" not in result
    if spec["expr"] == "a @ i":
        assert result["position"] == 2


@pytest.mark.anyio
async def test_wrong_width_clock_returns_expression_recovery(tmp_path):
    path = wave(tmp_path).file_path
    result = await server.call_tool("get_signals_by_cycle", {"wave_path": path,
        "clock_path": {"expr": "tb.data", "typing": "wave_bits"}, "signal_paths": ["tb.index"]})
    body = json.loads(result[0].text)
    assert body["error_code"] == "expression_control_width_invalid"
    assert body["recovery"]["action"] == "correct_control_width"


@pytest.mark.anyio
async def test_partial_memory_and_observed_unknown_are_distinct(tmp_path):
    path = wave(tmp_path).file_path
    missing = await point(path, {"expr": "mem[i]", "bindings": {
        "i": "tb.index", "mem": {"elements": [{"indices": [2], "signal": "tb.data"}]}},
        "types": {"i": {"width": 3}, "mem": {"width": 8, "unpacked": [[0, 7]]}}})
    assert "error" not in missing
    assert missing["expressions"][0]["coverage_status"] == "partial"
    assert "array_element_not_dumped" in missing["expressions"][0]["gaps"]
    unknown = await point(path, {"expr": "a[i]", "typing": "wave_bits",
        "bindings": {"a": "tb.data", "i": "tb.index"}}, at=35)
    assert "error" not in unknown
    assert unknown["value"]["bin"] == "x"
    assert unknown["expressions"][0]["coverage_status"] == "complete"


def test_unrelated_errors_do_not_gain_expression_recovery():
    result = server._format_error(ValueError("expression_signal_unresolved: not from the expression engine"))
    assert result.error_code is None
