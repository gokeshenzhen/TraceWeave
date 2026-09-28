"""Public FST discovery, schema, capability and projection contracts."""
import json
import os

import anyio
import pytest

pytest.importorskip("pylibfst", reason="install the optional [fst] extra")
from fst_fixture import write_fst
import server
from src.evidence_output import expand_compact_result
from src.fst_runtime import FST_BASIC_TOOLS, FST_SUPPORTED_TOOLS, FstProcess
from src.path_discovery import discover_sim_paths
from src.scope_metadata import ScopeIdentityChanged


@pytest.fixture(autouse=True)
def clean_state():
    server.reset_session_state()
    yield
    server.reset_session_state()
    for _, parser in server._parser_cache.values():
        server._dispose_cached_object(parser)
    server._parser_cache.clear()


def call(name, **args):
    return json.loads(anyio.run(server.call_tool, name, args)[0].text)


@pytest.fixture
def wave(tmp_path):
    return str(write_fst(tmp_path / "basic.fst", [
        (0, "bus [3:0]", "10xz"), (5, "bus [3:0]", "1100"),
        (10, "bus [3:0]", "0011"), (10, "bus [3:0]", "1111")],
        declarations=[("bus [3:0]", 4, "wire", "input", None),
                      ("alias [0:3]", 4, "wire", "output", "bus [3:0]")]))


def test_fst_only_and_mixed_discovery_preserve_file_order(tmp_path, wave):
    only = call("get_sim_paths", verif_root=str(tmp_path))
    assert only["wave_files"][0]["format"] == "fst"
    assert only["fst_runtime"]["enabled"]
    assert any("basic point/transition/window" in h for h in only["hints"])
    for suffix in ("fsdb", "vcd"):
        path = tmp_path / ("other." + suffix)
        path.write_bytes(b"fixture")
        os.utime(path, (1000, 1000))
    mixed = discover_sim_paths(str(tmp_path))
    assert [r["format"] for r in mixed["wave_files"]] == ["fst", "fsdb", "vcd"]
    explicit = discover_sim_paths(str(tmp_path), wave_file="other.vcd")
    assert [r["format"] for r in explicit["wave_files"]] == ["vcd"]


def test_public_summary_search_and_fixed_selection(wave):
    summary = call("get_waveform_summary", wave_path=wave)
    assert summary["format"] == "FST" and summary["total_signals"] == 2
    assert summary["fst_backend"]["storage_count"] == 1
    assert summary["fst_backend"]["supported_tools"] == list(FST_SUPPORTED_TOOLS)
    search = call("search_signals", wave_path=wave, keyword="bus")
    assert search["results"][0]["path"] == "top.bus[3:0]"
    selected = {"path": "top.alias", "bits": [3, 0]}
    point = call("get_signal_at_time", wave_path=wave, signal_path=selected, time_ps=0)
    assert point["value"]["bin"] == "z1" and point["value_status"] == "recorded"
    assert point["selections"][0]["declared_range"] == {"left": 0, "right": 3}


@pytest.mark.parametrize("tool,arguments", [
    ("get_waveform_summary", {}),
    ("get_signal_at_time", {"signal_path": "top.bus", "time_ps": 7}),
    ("get_signal_transitions", {"signal_path": "top.bus", "max_transitions": 1}),
    ("get_signals_around_time", {"signal_paths": [{"path": "top.bus", "bits": [0, 3]}],
                                 "center_time_ps": 7, "window_ps": 3}),
])
def test_compact_round_trip_preserves_recording_facts(wave, tool, arguments, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unvalidated FST clock/transient inference ran")
    monkeypatch.setattr(server, "_detect_wave_clock", forbidden)
    monkeypatch.setattr(server, "annotate_center_transients", forbidden)
    full = call(tool, wave_path=wave, **arguments)
    assert "error" not in full, full
    compact = call(tool, wave_path=wave, output_format="compact", **arguments)
    assert expand_compact_result(compact) == full


def test_display_cap_and_values_only_keep_coverage(wave):
    one = call("get_signal_transitions", wave_path=wave, signal_path="top.bus", max_transitions=1)
    all_rows = call("get_signal_transitions", wave_path=wave, signal_path="top.bus", max_transitions=20)
    assert one["truncated"] and not one["transition_count_is_lower_bound"]
    assert one["transition_count"] == all_rows["transition_count"] == 3
    assert one["fst_reading"] == all_rows["fst_reading"]
    result = call("get_signals_around_time", wave_path=wave, signal_paths=["top.bus"],
                  center_time_ps=7, window_ps=3, return_mode="values_only")
    entry = next(iter(result["signals"].values()))
    assert entry["value_at_center"]["bin"] == "1100"
    assert entry["fst_reading"]["coverage_status"] == "complete"
    assert entry["window_transition_count"] == 3 and "transitions_in_window" not in entry


def test_unvalidated_waveform_analyses_keep_an_explicit_gate(wave):
    catalog = anyio.run(server.list_tools)
    for tool in catalog:
        properties = tool.inputSchema.get("properties", {})
        key = next((k for k in ("wave_path", "wave_path_a", "wave_path_b") if k in properties), None)
        if key and tool.name not in FST_SUPPORTED_TOOLS:
            result = call(tool.name, **{key: wave})
            assert result["error_code"] == "fst_analysis_not_validated", tool.name
            assert result["analysis_status"] == "not_run"
    for key in ("wave_path_a", "wave_path_b"):
        assert call("diff_first_divergence", **{key: wave})["error_code"] == "fst_analysis_not_validated"
    result = call("get_signal_at_time", wave_path=wave, time_ps=0,
                  signal_path={"expr": "a", "bindings": {"a": "top.bus"}, "typing": "wave_bits"})
    assert result['value']['bin'] == '10xz'


def test_parser_cache_replacement_and_missing_file_errors(tmp_path, wave):
    first = server._get_parser(wave)
    cursor = first.enumerate_scope_page(max_items=1)["cursor"]
    replacement = write_fst(tmp_path / "next.fst", [(0, "a", "1")])
    os.replace(replacement, wave)
    second = server._get_parser(wave)
    assert second is not first and second.get_summary()["total_signals"] == 1
    with pytest.raises(ScopeIdentityChanged):
        second.enumerate_scope_page(cursor=cursor)
    missing = call("get_waveform_summary", wave_path=str(tmp_path / "missing.fst"))
    assert missing["error_code"] == "fst_file_not_found"


def test_missing_native_dependency_is_local_to_fst(wave, tmp_path, monkeypatch):
    original = FstProcess.command()
    monkeypatch.setattr(FstProcess, "command", staticmethod(lambda: [original[0], "-S", original[1]]))
    failed = call("get_waveform_summary", wave_path=wave)
    assert failed["error_code"] == "fst_dependency_missing"
    vcd = tmp_path / "a.vcd"
    vcd.write_text("$timescale 1ps $end\n$scope module top $end\n$var wire 1 ! a $end\n"
                   "$upscope $end\n$enddefinitions $end\n#0\n1!\n#30\n")
    assert call("get_signal_at_time", wave_path=str(vcd), signal_path="top.a", time_ps=5)["value"]["bin"] == "1"


def test_ambiguous_declaration_is_not_resolved_from_a_clipped_search(tmp_path):
    # A legacy fallback sees only one of the two candidates in its first
    # 100 matches. The exact FST declaration lookup must remain authoritative.
    declarations = [(f"aa_bus{i:03}", 1, "wire", "input", None) for i in range(99)]
    declarations += [("bus [0:0]", 1, "wire", "input", None), ("bus [1:1]", 1, "wire", "input", None)]
    wave = str(write_fst(tmp_path / "ambiguous.fst", [(0, name, "1") for name, *_ in declarations],
                         declarations=declarations))
    point = call("get_signal_at_time", wave_path=wave, signal_path="top.bus", time_ps=0)
    assert "fst_signal_ambiguous" in point.get("error", "")
    around = call("get_signals_around_time", wave_path=wave, signal_paths=["top.bus"], center_time_ps=0, window_ps=1)
    assert "fst_signal_ambiguous" in around["signals"]["top.bus"]["error"]
