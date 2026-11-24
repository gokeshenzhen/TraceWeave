import pytest

from src.verdi_rc import render_verdi_rc


def render(groups=None, *, error_time=0, first_deviation=None, duration_ps=200_000):
    return render_verdi_rc(
        {"Issue": ["tb.flag"]} if groups is None else groups,
        error_time=error_time, first_deviation=first_deviation,
        duration_ps=duration_ps, check_signal=lambda path: None,
    )


def test_functions_preserve_evidence_order_and_distinguish_full_instance_paths():
    a = "tb.g[0].u_fifo.data[7:0]"
    b = "tb.g[1].u_fifo.data[7:0]"
    checked = []
    view = render_verdi_rc(
        {"数据比较": [b, a, a], "Source timing": [a]},
        error_time="120ns", first_deviation=100_000,
        duration_ps=200_000, check_signal=checked.append,
    )
    assert checked == [b, a]
    assert view.splitlines() == [
        "Magic 271485", "Revision Verdi_V-2023.12-SP2", "",
        "windowTimeUnit 1ps", "zoom 90000 130000 1p", "cursor 120000", "marker 100000",
        "userMarker 120000 reported_error ID_RED5 long_dashed",
        "userMarker 100000 first_deviation ID_YELLOW8 long_dashed", "",
        'addGroup "数据比较"', 'addSubGroup "tb/g[1]/u_fifo"',
        "addSignal -h 15 /tb/g[1]/u_fifo/data[7:0]", 'endSubGroup "tb/g[1]/u_fifo"',
        'addSubGroup "tb/g[0]/u_fifo"', "addSignal -h 15 /tb/g[0]/u_fifo/data[7:0]",
        'endSubGroup "tb/g[0]/u_fifo"', "", 'addGroup "Source timing"',
        'addSubGroup "tb/g[0]/u_fifo"', "addSignal -h 15 /tb/g[0]/u_fifo/data[7:0]",
        'endSubGroup "tb/g[0]/u_fifo"',
    ]


def test_escaped_component_keeps_literal_dot_and_adds_nwave_delimiter():
    checked = []
    view = render_verdi_rc(
        {"D": [["tb", r"\g[0]", r"\a.b/x"]]},
        error_time=0, duration_ps=0, check_signal=checked.append,
    )
    assert checked == [r"tb.\g[0].\a.b/x"]
    assert r"addSignal -h 15 /tb/\g[0] /\a.b/x " in view
    assert "zoom " not in view


def test_sub_ps_times_and_coincident_markers_keep_both_meanings():
    view = render(error_time="0.12345ns", first_deviation="123450fs", duration_ps=124)
    assert "windowTimeUnit 1fs\nzoom 0 124000 1f\ncursor 123450\nmarker 123450\n" in view
    assert "userMarker 123450 reported_error" in view
    assert "userMarker 123450 first_deviation" in view


@pytest.mark.parametrize(("error", "deviation", "label"), [
    (None, 10, "first_deviation"), (10, None, "reported_error"),
])
def test_single_anchor_only_labels_the_supported_event(error, deviation, label):
    view = render(error_time=error, first_deviation=deviation)
    assert "cursor 10\nmarker 10\n" in view
    assert view.count("userMarker ") == 1
    assert f"userMarker 10 {label} " in view


@pytest.mark.parametrize("error", [None, False, -1, 1.5, "0.5fs", "12", "1ms"])
def test_unknown_or_unrepresentable_time_cannot_create_a_view(error):
    with pytest.raises(ValueError):
        render(error_time=error)


@pytest.mark.parametrize("groups", [
    {}, {"": ["tb.a"]}, {"G": []}, {"G": "tb.a"}, {"G": ["tb..a"]},
    {"G\nmarker 9": ["tb.a"]}, {"G": ["tb.a\nmarker 9"]},
    {"G": [r"tb.\ambiguous.dot"]}, {"G": [["tb", "a.b"]]},
])
def test_reject_malformed_groups_and_ambiguous_paths(groups):
    with pytest.raises(ValueError):
        render(groups)


def test_unresolved_declaration_stops_export():
    with pytest.raises(KeyError, match="tb.missing"):
        render_verdi_rc({"Compare": ["tb.missing"]}, error_time=1, duration_ps=2,
                        check_signal={}.__getitem__)


def test_slices_use_declared_coordinates_and_check_each_parent_once():
    checked = []
    def declaration(path):
        checked.append(path)
        left, right = (31, 0) if path.endswith("data[31:0]") else (1, 32)
        return {"declared_range": {"left": left, "right": right}}

    asc, desc = "tb.u.S[1:32]", "tb.u.data[31:0]"
    view = render_verdi_rc({"Port map": [
        {"path": asc, "slice": [6, 9]}, {"path": asc, "slice": [5, 8]},
        {"path": asc, "slice": [9, 9]}, {"path": desc, "slice": [7, 4]},
        {"path": ["tb", r"\literal[7]"], "slice": [3, 4]},
    ]}, error_time=0, duration_ps=1, check_signal=declaration)
    assert checked == [asc, desc, r"tb.\literal[7]"]
    assert [row for row in view.splitlines() if row.startswith("addSignal")] == [
        "addSignal -h 15 /tb/u/S[6:9]", "addSignal -h 15 /tb/u/S[5:8]",
        "addSignal -h 15 /tb/u/S[9]", "addSignal -h 15 /tb/u/data[7:4]",
        r"addSignal -h 15 /tb/\literal[7] [3:4]",
    ]


@pytest.mark.parametrize("bounds", [[0, 4], [3, 33], [9, 6], [True, 1], [4], "6:9"])
def test_invalid_slice_cannot_be_rendered_as_the_whole_bus(bounds):
    with pytest.raises(ValueError, match="slice"):
        render_verdi_rc({"G": [{"path": "tb.S[1:32]", "slice": bounds}]},
                        error_time=0, duration_ps=1,
                        check_signal=lambda path: {"declared_range": {"left": 1, "right": 32}})


def test_changed_rows_come_first_and_keep_their_color_by_instance_and_slice():
    checked = []
    def declaration(path):
        checked.append(path)
        return {"declared_range": {"left": 1, "right": 32}}

    bus, dout = "tb.u.S[1:32]", "tb.u.u1.dout[1:4]"
    old = {"path": bus, "slice": [6, 9]}
    view = render_verdi_rc({"Round": [old, bus, dout, "tb.u.u2.dout[1:4]"]},
                           changes={"crp.v:21 output slice": [old, {"path": bus, "slice": [5, 8]}, dout],
                                    "sbox2.v:26 case 17": [dout], "tb.sv:49 message": []},
                           error_time=0, duration_ps=1, check_signal=declaration)
    assert checked == [bus, dout, "tb.u.u2.dout[1:4]"]
    assert [row for row in view.splitlines() if row.startswith("addGroup")] == [
        'addGroup "RTL change - crp.v:21 output slice"', 'addGroup "RTL change - sbox2.v:26 case 17"',
        'addGroup "RTL change - tb.sv:49 message (no mapped waveform signal)"', 'addGroup "Round"']
    assert view.count("addSignal -c ID_YELLOW5 -h 15 /tb/u/S[6:9]") == 2
    assert view.count("addSignal -c ID_YELLOW5 -h 15 /tb/u/S[5:8]") == 1
    assert view.count("addSignal -c ID_YELLOW5 -h 15 /tb/u/u1/dout[1:4]") == 3
    assert "addSignal -h 15 /tb/u/S[1:32]" in view
    assert "addSignal -h 15 /tb/u/u2/dout[1:4]" in view


def test_evidence_without_edits_colors_earlier_occurrences_of_only_the_exact_slice():
    bus = "tb.g[0].S[1:32]"
    bit = {"path": bus, "slice": [9, 9]}
    other = {"path": "tb.g[1].S[1:32]", "slice": [9, 9]}
    view = render_verdi_rc({"Overview": [bit, other, bus], "Boundary": [{**bit, "role": "evidence"}]},
                           error_time=0, duration_ps=1,
                           check_signal=lambda path: {"declared_range": {"left": 1, "right": 32}})
    assert view.count("addSignal -c ID_CYAN5 -h 15 /tb/g[0]/S[9]\n") == 2
    assert "addSignal -h 15 /tb/g[1]/S[9]\n" in view
    assert "addSignal -h 15 /tb/g[0]/S[1:32]\n" in view
    assert "ID_YELLOW5" not in view


def test_roles_inside_edits_and_later_modified_signals_share_one_color_in_every_group():
    selector, output, context = "tb.u.addr", "tb.u.dout", "tb.u.clk"
    view = render_verdi_rc({"Result": [selector, {"path": output, "role": "modified"}, context]},
                           changes={"table.sv:26": [{"path": selector, "role": "evidence"},
                                                     {"path": output, "role": "evidence"},
                                                     {"path": context, "role": "context"}],
                                    "table.sv:32": [output]},
                           error_time=0, duration_ps=1, check_signal=lambda path: None)
    assert view.count("addSignal -c ID_CYAN5 -h 15 /tb/u/addr\n") == 2
    assert view.count("addSignal -c ID_YELLOW5 -h 15 /tb/u/dout\n") == 3
    assert view.count("addSignal -h 15 /tb/u/clk\n") == 2


@pytest.mark.parametrize("role", [None, "candidate", True, []])
def test_invalid_signal_roles_do_not_silently_lose_the_highlight(role):
    with pytest.raises(ValueError, match="role"):
        render({"Result": [{"path": "tb.u.q", "role": role}]})
