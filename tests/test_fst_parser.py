"""Independent expected observations for the first-round digital FST backend."""
import pytest

pytest.importorskip("pylibfst", reason="install the optional [fst] extra")
from fst_fixture import write_fst
from src.fst_parser import FSTParser
from src.fst_runtime import FstError
from src.scope_metadata import ScopeIdentityChanged
from src.waveform_selection import SelectionParser


@pytest.mark.parametrize("scale,multiplier", [(-9, 1000000), (-12, 1000), (-13, 100)])
def test_time_four_states_and_strict_physical_window(tmp_path, scale, multiplier):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(0, "a", "0"), (10, "a", "1"),
         (11, "a", "x"), (12, "a", "z"), (20, "a", "0")], scale=scale))
    result = p.get_transitions("top.a")
    assert [r["time_fs"] for r in result["transitions"]] == [t * multiplier for t in (10, 11, 12, 20)]
    assert [r["value"]["bin"] for r in result["transitions"]] == ["1", "x", "z", "0"]
    assert result["initial_state"]["event_kind"] == "initial_state"
    assert result["initial_state"]["value"]["bin"] == "0"
    assert result["predecessor"] is None
    assert result["fst_reading"]["coverage_status"] == "complete"
    if scale == -13:
        narrow = p.get_transitions("top.a", 2, 2)
        assert [r["time_fs"] for r in narrow["transitions"]] == [2000]
        assert narrow["predecessor"]["time_fs"] == 1200
        assert narrow["predecessor"]["time_ps"] == 2
        assert p.get_value_at_time("top.a", 1)["value"]["bin"] == "1"


def test_nonzero_start_missing_prefix_and_outside_end(tmp_path):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(5, "a", "1"), (10, "a", "x")], start=5, end=15))
    for time in (0, 16):
        result = p.get_value_at_time("top.a", time)
        assert result["value"] is None and result["value_status"] == "outside_recorded_range"
        assert result["fst_reading"]["coverage_status"] == "partial"
    assert p.get_value_at_time("top.a", 5)["value"]["bin"] == "1"
    assert p.get_value_at_time("top.a", 10)["value"]["bin"] == "x"
    result = p.get_transitions("top.a", 6, 9)
    assert result["predecessor"] is None and result["transitions"] == []
    assert result["initial_state"]["time_fs"] == 5000


def test_dump_blackout_and_resume_do_not_extend_stale_values(tmp_path):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(0, "a", "1"), (25, "a", "0")],
                           activity=[(10, 0), (20, 1)]))
    expected = {9: ("recorded", "1"), 10: ("dump_inactive", None), 19: ("dump_inactive", None),
                20: ("unrecorded_after_dump_resume", None), 24: ("unrecorded_after_dump_resume", None),
                25: ("recorded", "0")}
    for at, (status, bits) in expected.items():
        result = p.get_value_at_time("top.a", at)
        assert result["value_status"] == status
        assert (result["value"]["bin"] if result["value"] else None) == bits
        assert (result["fst_reading"]["coverage_status"] == "complete") == (status == "recorded")
    result = p.get_transitions("top.a")
    assert {g["reason"] for g in result["fst_reading"]["gaps"]} == {"dump_inactive", "unrecorded_after_dump_resume"}


def test_declared_coordinates_aliases_missing_elements_and_scope_pages(tmp_path):
    declarations = [("down [7:4]", 4, "wire", "input", None),
                    ("up [-2:1]", 4, "wire", "output", "down [7:4]"),
                    ("\\escaped.name [3:0]", 4, "wire", "implicit", None),
                    ("mem[2]", 1, "wire", "implicit", None)]
    p = FSTParser(write_fst(tmp_path / "a.fst", [(0, "down [7:4]", "10xz"),
        (0, "\\escaped.name [3:0]", "0110"), (0, "mem[2]", "1")], declarations=declarations,
        scopes_by_name={"mem[2]": ["top", "genblk[0]"]}))
    down, up = p.get_signal_declaration("top.down"), p.get_signal_declaration("top.up[-2:1]")
    assert down["storage_key"] == up["storage_key"]
    assert down["path"] == "top.down[7:4]" and up["declared_range"] == {"left": -2, "right": 1}
    selection = SelectionParser(p)
    key = selection.bind({"path": "top.down[7:4]", "bits": [4, 7, 5]})
    assert selection.get_value_at_time(key, 0)["value"]["bin"] == "z1x"
    assert p.get_value_at_time("top.\\escaped.name [3:0]", 0)["value"]["bin"] == "0110"
    with pytest.raises(KeyError, match="not_found"):
        p.get_value_at_time("top.genblk[0].mem[3]", 0)
    found, cursor = [], None
    while True:
        page = p.enumerate_scope_page("top", cursor=cursor, max_items=1)
        found.extend(r["path"] for r in page["results"])
        cursor = page["cursor"]
        if page["complete"]:
            break
    assert found == sorted(set(found)) and len(found) == 4
    assert len(p.enumerate_scope_page("top", direct=True)["results"]) == 3
    assert p.enumerate_scope_page("top.genblk[0]", direct=True)["results"][0]["name"] == "mem[2]"
    assert p.search_signals("DOWN")["total_matched"] == 1


def test_escaped_scope_dots_are_not_hierarchy_separators(tmp_path):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(0, "a", "1")],
                           scopes_by_name={"a": ["top", "\\with.dot"]}))
    assert not p.enumerate_scope_page("top.\\with")["results"]
    assert len(p.enumerate_scope_page("top.\\with.dot")["results"]) == 1


def test_metadata_and_old_cursors_invalidate_on_close_or_replacement(tmp_path):
    path = write_fst(tmp_path / "a.fst", [(0, "a", "1")],
        declarations=[("a", 1, "wire", "input", None), ("b", 1, "wire", "output", "a")])
    p = FSTParser(path)
    page = p.enumerate_scope_page(max_items=1)
    p.close()
    assert p.get_summary()["format"] == "FST"
    with pytest.raises(ScopeIdentityChanged):
        p.enumerate_scope_page(cursor=page["cursor"], max_items=1)
    path.write_bytes(path.read_bytes() + b"\0")
    with pytest.raises(ScopeIdentityChanged):
        p.get_summary()


def test_around_history_and_alias_reads_are_bounded_and_shared(tmp_path, monkeypatch):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(t, "a", str(t % 2)) for t in range(30)],
        declarations=[("a", 1, "wire", "input", None), ("b", 1, "wire", "output", "a")]))
    original, calls = p._read, []
    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(p, "_read", counted)
    result = p.get_signals_around_time(["top.a", "top.b"], 20, 2, 3)
    assert len(calls) == 1
    for row in result["signals"].values():
        assert [e["time_ps"] for e in row["pre_window_transitions"]] == [15, 16, 17]
        assert [e["time_ps"] for e in row["transitions_in_window"]] == [18, 19, 20, 21, 22]
        assert row["value_at_center"]["bin"] == "0"
    assert p.get_signals_around_time(["top.a"], 20, 2, 0)["signals"]["top.a"]["pre_window_transitions"] == []


def test_read_budget_is_partial_and_never_returns_a_guessed_point(tmp_path, monkeypatch):
    from src import fst_parser
    p = FSTParser(write_fst(tmp_path / "a.fst", [(t, "a", str(t % 2)) for t in range(30)]))
    monkeypatch.setattr(fst_parser, "MAX_RESULT_EVENTS", 2)
    result = p.get_transitions("top.a")
    assert result["truncated"] and result["transition_count_is_lower_bound"]
    assert [r["time_ps"] for r in result["transitions"]] == [1, 2]
    assert result["fst_reading"]["coverage_status"] == "partial"
    around = p.get_signals_around_time(["top.a"], 20, 20)
    assert around["signals"]["top.a"]["value_at_center"] is None
    assert around["signals"]["top.a"]["value_status"] == "read_incomplete"


def test_summary_reports_real_loaded_component_and_capability_boundary(tmp_path):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(0, "a", "1")]))
    result = p.get_summary()
    assert result["fst_backend"]["version"] == "0.2.1"
    assert len(result["fst_backend"]["native_sha256"]) == 64
    assert result["fst_backend"]["analysis_status"] == "not_run"
    assert not p._supports_event_pages()


def test_missing_coordinates_are_not_guessed_for_structured_selection(tmp_path):
    p = FSTParser(write_fst(tmp_path / "a.fst", [(0, "bus", "10")],
        declarations=[("bus", 2, "wire", "input", None)]))
    assert p.get_value_at_time("top.bus", 0)["value"]["bin"] == "10"
    with pytest.raises(FstError, match="declared_range_unknown"):
        p.get_signal_declaration("top.bus")


def test_fixed_selection_projects_initial_state_and_true_transitions(tmp_path):
    p = SelectionParser(FSTParser(write_fst(tmp_path / "a.fst",
        [(0, "bus [3:0]", "10xz"), (5, "bus [3:0]", "11xz"), (10, "bus [3:0]", "00z1")],
        declarations=[("bus [3:0]", 4, "wire", "input", None)])))
    key = p.bind({"path": "top.bus", "bits": [0, 3]})
    result = p.get_transitions(key)
    assert result["initial_state"]["value"]["bin"] == "z1"
    assert result["initial_state"]["event_kind"] == "initial_state"
    assert [(r["time_ps"], r["value"]["bin"]) for r in result["transitions"]] == [(10, "10")]
    assert result["predecessor"] is None
    around = p.get_signals_around_time([key], 0, 2)["signals"][key]
    assert around["initial_state"]["value"]["bin"] == "z1"
    assert around["value_at_center"]["bin"] == "z1"


def test_array_element_indices_are_not_guessed_as_packed_coordinates(tmp_path):
    declarations = [("flag[2]", 1, "wire", "input", None),
                    ("mem[2]", 8, "wire", "input", None),
                    ("mem[4] [7:0]", 8, "wire", "input", None),
                    ("bit_at_four [4]", 1, "wire", "input", None)]
    parser = FSTParser(write_fst(tmp_path / "arrays.fst", [(0, n, "1" * w) for n, w, *_ in declarations],
                                 declarations=declarations))
    assert parser.get_summary()["total_signals"] == 4
    assert parser.get_value_at_time("top.mem[2]", 0)["value"]["bin"] == "11111111"
    with pytest.raises(FstError, match="declared_range_unknown"):
        parser.get_signal_declaration("top.mem[2]")
    assert parser.get_signal_declaration("top.flag[2]")["declared_range"] == {"left": 0, "right": 0}
    assert parser.get_signal_declaration("top.mem[4]")["declared_range"] == {"left": 7, "right": 0}
    assert parser.get_signal_declaration("top.bit_at_four")["declared_range"] == {"left": 4, "right": 4}
    for missing in ("top.flag", "top.mem", "top.mem[3]"):
        with pytest.raises(KeyError, match="not_found"):
            parser.get_value_at_time(missing, 0)
