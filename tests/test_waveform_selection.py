"""Independent declaration-coordinate and four-state projection oracles."""
from collections import Counter
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.cycle_query import sample_signals_on_edges
from src.fsdb_parser import FSDBParser, get_fsdb_runtime_info
from src.schemas import WaveformSelection
from src.vcd_parser import VCDParser
from src.waveform_selection import SelectionParser, prepare_selections


def dump(tmp_path):
    path = tmp_path / "selections.vcd"
    path.write_text("""$timescale 1ps $end
$scope module tb $end
$var wire 1 c clk $end
$var wire 8 a bus [15:8] $end
$var wire 8 a ascending [0:7] $end
$var wire 8 a negative [-2:-9] $end
$var wire 1 b bit [3] $end
$var wire 4 d pieces [7:4] $end
$var wire 4 e pieces [3:0] $end
$scope module gen[2] $end
$var wire 4 f data [3:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0c
b0101xz10 a
1b
b1010 d
b0110 e
b1100 f
#5
1c
#10
0c
b1101xz10 a
#15
1c
#20
0c
bx a
#25
1c
#30
0c
bz a
#35
1c
#40
0c
""")
    return VCDParser(str(path))


@pytest.mark.parametrize("path,lsb", [("tb.bus[15:8]", 8), ("tb.ascending[0:7]", 7),
                                     ("tb.negative[-2:-9]", -9), ("tb.bus", 8)])
def test_declared_indices_preserve_order_and_unknowns(tmp_path, path, lsb):
    p = SelectionParser(dump(tmp_path))
    key = p.bind({"path": path, "lsb": lsb, "width": 4})
    assert p.get_value_at_time(key, 5)["value"] == {"bin": "xz10", "hex": None, "dec": None}
    assert p.get_value_at_time(key, 25)["value"]["bin"] == "xxxx"
    assert p.get_value_at_time(key, 35)["value"]["bin"] == "zzzz"


def test_ordered_bits_aliases_independent_fragments_and_generated_path(tmp_path):
    p = SelectionParser(dump(tmp_path))
    key = p.bind({"path": "tb.bus[15:8]", "bits": [8, 15, 12]})
    assert p.get_value_at_time(key, 5)["value"]["bin"] == "001"
    key = p.bind({"path": "tb.gen[2].data[3:0]", "lsb": 2, "width": 2})
    assert p.get_value_at_time(key, 5)["value"]["dec"] == 3
    key = p.bind({"path": "tb.bit[3]", "bits": [3]})
    assert p.get_value_at_time(key, 5)["value"]["bin"] == "1"
    for path in ("tb.pieces", "tb.pieces[7:0]", "bus[15:8]", "tb.bit"):
        with pytest.raises(KeyError):
            p.bind({"path": path, "lsb": 0, "width": 1})


@pytest.mark.parametrize("spec", [
    {"bits": []}, {"bits": [8, 8]}, {"bits": [True]}, {"lsb": 8, "width": 0},
    {"lsb": 8, "width": 65537}, {"lsb": 8}, {"lsb": 8, "width": 1, "bits": [8]},
    {"lsb": True, "width": 1},
    {"lsb": 0, "width": 4097},
])
def test_invalid_selection_shapes(spec):
    with pytest.raises(ValueError):
        WaveformSelection.model_validate({"path": "tb.bus[15:8]", **spec})


@pytest.mark.parametrize("spec", [{"lsb": 7, "width": 1}, {"lsb": 14, "width": 3}, {"bits": [-1]}])
def test_outside_declared_range_rejected(tmp_path, spec):
    with pytest.raises(ValueError, match="outside"):
        SelectionParser(dump(tmp_path)).bind({"path": "tb.bus[15:8]", **spec})


def test_projection_transitions_keep_closed_window_predecessor_and_filter_other_bits(tmp_path):
    p = SelectionParser(dump(tmp_path))
    key = p.bind({"path": "tb.bus[15:8]", "lsb": 8, "width": 4})
    r = p.get_transitions(key, 5, 30)
    assert r["predecessor"]["time_ps"] == 0
    assert [(x["time_ps"], x["value"]["bin"]) for x in r["transitions"]] == [(20, "xxxx"), (30, "zzzz")]


def test_sampling_reads_one_bus_once_and_preserves_four_states(tmp_path):
    base = dump(tmp_path)
    counts = Counter()
    original = base.get_transitions
    def counted(path, *a, **kw):
        counts[path] += 1
        return original(path, *a, **kw)
    base.get_transitions = counted
    p, keys = prepare_selections(base, {"lo": {"path": "tb.bus[15:8]", "lsb": 8, "width": 4},
                                       "hi": {"path": "tb.bus[15:8]", "lsb": 12, "width": 4}})
    r = sample_signals_on_edges(p, "tb.clk", list(keys.values()), compact=True)
    assert counts == {"tb.clk": 1, "tb.bus[15:8]": 1}
    assert [v["bin"] for v in r["signal_columns"][keys["hi"]]] == ["0101", "1101", "xxxx", "zzzz"]
    assert [v["bin"] for v in r["signal_columns"][keys["lo"]]] == ["xz10", "xz10", "xxxx", "zzzz"]


def test_alias_coordinates_share_storage_read_and_duplicate_bind_budget(tmp_path, monkeypatch):
    import src.waveform_selection as selection
    base = dump(tmp_path)
    reads = []
    original = base.get_transitions
    def measured(path, *a, **kw):
        reads.append(path)
        return original(path, *a, **kw)
    base.get_transitions = measured
    adapter = SelectionParser(base)
    low = adapter.bind({"path": "tb.bus", "lsb": 8, "width": 4})
    other = adapter.bind({"path": "tb.ascending[0:7]", "bits": [7, 6, 5, 4]})
    monkeypatch.setattr(selection, "MAX_SELECTIONS", 2)
    assert adapter.bind({"path": "tb.bus[15:8]", "bits": [11, 10, 9, 8]}) == low
    with pytest.raises(ValueError, match="count limit"):
        adapter.bind({"path": "tb.bus", "bits": [15]})
    r = sample_signals_on_edges(adapter, "tb.clk", [low, other], compact=True)
    assert reads == ["tb.clk", "tb.bus[15:8]"]
    assert r["signal_columns"][low][0]["bin"] == "xz10"
    assert r["signal_columns"][other][0]["bin"] == "01zx"


def test_escaped_identifier_brackets_are_not_a_range(tmp_path):
    path = tmp_path / "escaped.vcd"
    path.write_text("$scope module tb $end\n$var wire 4 a \\bus[9] [0:3] $end\n"
                    "$upscope $end\n$enddefinitions $end\n#0\nb10xz a\n")
    parser = VCDParser(str(path))
    declaration = next(iter(parser.search_signals("bus")["results"]))["path"]
    adapter = SelectionParser(parser)
    selected = adapter.bind({"path": declaration, "lsb": 3, "width": 2})
    assert adapter.get_value_at_time(selected, 0)["value"]["bin"] == "xz"


def test_truncated_selection_sampling_does_not_extend_the_prefix(tmp_path):
    from src.cycle_query import get_signals_by_cycle
    base = dump(tmp_path)
    original = base.get_transitions
    def limited(path, *a, **kw):
        r = original(path, *a, **kw)
        if path == "tb.bus[15:8]":
            r.update(transitions=r["transitions"][:1], truncated=True)
        return r
    base.get_transitions = limited
    adapter = SelectionParser(base)
    key = adapter.bind({"path": "tb.bus", "bits": [15]})
    r = get_signals_by_cycle(adapter, "tb.clk", [key], num_cycles=4)
    assert r["transition_data_truncated"]
    assert r["transition_signals_truncated"] == [key]
    assert r["cycles"][0]["signals"][key]["dec"] == 0
    assert all(row["signals"][key]["dec"] is None for row in r["cycles"][1:])


def test_projection_limits_cancellation_and_changed_declaration(tmp_path, monkeypatch):
    import src.waveform_selection as selection
    base = dump(tmp_path)
    p = SelectionParser(base)
    key = p.bind({"path": "tb.bus[15:8]", "lsb": 8, "width": 4})
    monkeypatch.setattr(selection, "MAX_PROJECTED_BITS", 1)
    with pytest.raises(ValueError, match="budget"):
        sample_signals_on_edges(p, "tb.clk", [key])
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            p.get_value_at_time(key, 5)
    finally:
        pop_cancel_event(token)
    # Also change byte count; some filesystems coarsen same-tick timestamps.
    Path(base.file_path).write_text(Path(base.file_path).read_text().replace("[15:8]", "[107:100]"))
    with pytest.raises(RuntimeError, match="changed"):
        p.get_value_at_time(key, 5)


def test_legacy_native_declaration_fallback_is_bounded_and_exact():
    p = FSDBParser.__new__(FSDBParser)
    p._handle = None
    p._lib = SimpleNamespace(_traceweave_has_metadata_v1=False)
    p._file_identity = ("test",)
    p._open = lambda: None
    p._check_metadata_identity = lambda identity: None
    calls = []
    def search(path, max_results):
        calls.append((path, max_results))
        return {"results": [{"path": "top.bus[7:4]", "width": 4}]}
    p.search_signals = search
    assert p.get_signal_declaration("top.bus[7:4]")["declared_range"] == {"left": 7, "right": 4}
    with pytest.raises(KeyError):
        p.get_signal_declaration("bus[7:4]")
    assert calls == [("top.bus[7:4]", 64), ("bus[7:4]", 64)]


def test_cancellation_during_projection_propagates(tmp_path, monkeypatch):
    import src.waveform_selection as selection
    adapter = SelectionParser(dump(tmp_path))
    key = adapter.bind({"path": "tb.bus", "bits": [15, 14]})
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        if count == 4:
            raise OperationCancelled()
    monkeypatch.setattr(selection, "check_cancelled", cancelled)
    with pytest.raises(OperationCancelled):
        adapter.get_transitions(key, 0, 40)


def test_truncated_clock_selection_cannot_enter_complete_edge_cache(tmp_path):
    from src.cycle_query import get_signals_by_cycle
    base = dump(tmp_path)
    original = base.get_transitions
    def limited(path, *a, **kw):
        r = original(path, *a, **kw)
        if path == "tb.clk":
            r.update(transitions=r["transitions"][:3], truncated=True)
        return r
    base.get_transitions = limited
    adapter = SelectionParser(base)
    key = adapter.bind({"path": "tb.bus", "bits": [15]})
    with pytest.raises(ValueError, match="incomplete clock"):
        get_signals_by_cycle(adapter, "tb.clk", [key], num_cycles=4)
    r = sample_signals_on_edges(adapter, "tb.clk", [key])
    assert r["transition_data_truncated"] and len(r["samples"]) == 1


@pytest.mark.skipif(not get_fsdb_runtime_info().get("enabled"), reason="FSDB runtime unavailable")
@pytest.mark.parametrize("scale,times,expected", [
    ("100fs", [100000, 100100, 100101], [0xAA, 0xBB, 0xCC]),
    ("1ns", [100000, 100999, 101000], [0xAA, 0xAA, 0xBB]),
])
def test_native_projection_matches_fixture_stimulus(scale, times, expected):
    path = Path(__file__).parent / "fixtures" / f"scale_{scale}.fsdb"
    base = FSDBParser(str(path))
    try:
        p = SelectionParser(base)
        key = p.bind({"path": f"scale_{scale}_tb.addr[31:0]", "lsb": 24, "width": 8})
        assert [p.get_value_at_time(key, t)["value"]["dec"] for t in times] == expected
    finally:
        base.close()
