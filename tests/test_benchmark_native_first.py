"""Harness qualification without starting a server, SDK or frontend."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from scripts.benchmark_native_first import decode_response, python_command, server_directory


def response(text, *, error=False, structured=None):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           isError=error, structuredContent=structured)


def test_python_command_preserves_virtual_environment_symlink(tmp_path):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    assert python_command(executable) == str(executable.absolute())
    assert python_command(executable) != str(executable.resolve())


def test_server_directory_is_independent_of_code_archive(tmp_path):
    log = tmp_path / "project" / "compile.log"
    assert server_directory({"compile_log": str(log)}) == str(log.parent)
    assert server_directory({"compile_log": str(log), "server_cwd": str(tmp_path)}) == str(tmp_path)


def test_json_facts_are_preserved():
    assert decode_response(response('{"value":"x","coverage":"partial"}')) == {"value": "x", "coverage": "partial"}


def test_non_json_error_is_retained_as_failed_evidence():
    raw = decode_response(response("Error executing tool: original failure", error=True))
    assert raw["error_code"] == "non_json_mcp_response" and raw["transport_is_error"] is True
    assert raw["raw_mcp_content"][0]["text"] == "Error executing tool: original failure"


def test_structured_only_response_is_supported():
    assert decode_response(SimpleNamespace(content=[], structuredContent={"count": 0}, isError=False)) == {"count": 0}


@pytest.mark.anyio
@pytest.mark.parametrize('variant', ['baseline', 'candidate'])
async def test_slow_evidence_journal_is_excluded_from_sequence_latency(tmp_path, monkeypatch, variant):
    import json
    from scripts import benchmark_native_first as benchmark
    workload = tmp_path / 'workload.json'
    workload.write_text(json.dumps({'compile_log': '/unused/compile.log', 'baseline_native_first': True, 'queries': [
        {'tool': 'find_signal_loads', 'arguments': {'signal_path': 'top.a'}}] * 7}))
    class Clock:
        now = 0.0
    clock = Clock()
    class Session:
        async def call_tool(self, tool, arguments):
            assert tool == 'find_signal_loads' and 'design_handle' not in arguments
            clock.now += .001
            return response('{"ok":true}')
    original = Path.write_text
    def slow_write(path, *args, **kwargs):
        clock.now += .1
        return original(path, *args, **kwargs)
    monkeypatch.setattr(benchmark.time, 'perf_counter', lambda: clock.now)
    monkeypatch.setattr(Path, 'write_text', slow_write)
    result = await benchmark.perform(SimpleNamespace(workload=str(workload),
        output_dir=str(tmp_path / 'out.json'), variant=variant, warmup=False, repeats=2), Session())
    for sample in result['samples']:
        assert sample['wall_ms'] == pytest.approx(7)
        assert sample['observer_journal_ms'] == pytest.approx(700)
        assert sample['observed_wall_ms'] == pytest.approx(707)
        assert sum(q['wall_ms'] for q in sample['queries']) == pytest.approx(7)
