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
