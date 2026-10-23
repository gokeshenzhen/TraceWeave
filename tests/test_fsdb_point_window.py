"""Portable lifecycle faults plus opt-in same-trace FSDB integration."""

import os
import ctypes
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from src.fsdb_parser import FSDBParser

ROOT = Path(__file__).resolve().parents[1]
NATIVE_FSDB = ROOT / "native" / "fsdb"


@pytest.fixture(scope="module")
def native_lifecycle_probe(tmp_path_factory):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    executable = tmp_path_factory.mktemp("point_read") / "lifecycle"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(NATIVE_FSDB), str(ROOT / "tests/native/fsdb_point_read_test.cpp"),
                    "-o", str(executable)], check=True, capture_output=True, text=True)
    return executable


@pytest.mark.parametrize("scenario", ["success", "resident", "bounds", "disabled", "before", "after",
                                      "set", "unsupported", "add", "load", "create", "throw_load", "throw_body",
                                      "restore", "unload", "throw_add", "throw_create", "throw_free",
                                      "throw_unload", "throw_restore", "reset_initial", "reset_cleanup",
                                      "throw_reset_initial", "throw_reset_cleanup",
                                      "selection_dedup", "selection_transfer"])
def test_native_point_resource_lifecycle(native_lifecycle_probe, scenario):
    subprocess.run([str(native_lifecycle_probe), scenario], check=True, timeout=10)


@pytest.mark.eda_integration
def test_multiblock_points_preserve_following_reads():
    """Set TW_POINT_READ_FSDB to the 1M-step benchmark's converted FSDB.

    Generate using benchmark_waveform_reads.py, then convert with local
    vcd2fsdb and FSDB_ENV_WRITER_MEM_LIMIT=4 to exercise multiple flushes.
    """
    wave = os.environ.get("TW_POINT_READ_FSDB")
    if not wave:
        pytest.skip("set TW_POINT_READ_FSDB to a generated 1M-step FSDB")
    parser = FSDBParser(wave)
    duration = 10_000_000
    rng = random.Random(716)
    times = [0, 10, duration // 2, duration, duration + 100]
    times += [rng.randrange(duration) for _ in range(7)]
    try:
        before = parser.get_transitions("top.data", 0, 30)
        after = parser.get_transitions("top.data", duration - 30, duration)
        around = parser.get_signals_around_time(["top.data", "top.sparse"], 333, 30, 5)
        for timestamp in times:
            for leaf in ("data", "wide", "sparse"):
                step = min(timestamp // 10, 1_000_000)
                if leaf == "wide":
                    step = step // 1000 * 1000
                if leaf == "sparse":
                    step = 500_000 if step >= 500_000 else 0
                expected = f"{step * 2654435761 & 0xffffffff:032b}"
                if leaf == "wide":
                    expected *= 32
                if timestamp > duration:
                    expected = "x"  # Preserve the legacy FFR out-of-file seek.
                assert parser.get_value_at_time("top." + leaf, timestamp)["value"]["bin"] == expected, (leaf, timestamp)
            assert parser.get_summary()["simulation_duration_ps"] == duration
            assert parser.get_transitions("top.data", 0, 30) == before
            assert parser.get_transitions("top.data", duration - 30, duration) == after
        assert parser.get_signals_around_time(["top.data", "top.sparse"], 333, 30, 5) == around
    finally:
        parser.close()


@pytest.fixture(scope="module")
def native_reader_probe(tmp_path_factory):
    """Use real FFR selection/residency observations without changing public ABI."""
    verdi = os.environ.get("VERDI_HOME")
    if not verdi or not shutil.which("g++"):
        pytest.skip("requires VERDI_HOME and g++")
    tmp_path = tmp_path_factory.mktemp("reader_probe")
    sdk = Path(verdi) / "share/FsdbReader"
    source = (NATIVE_FSDB / "fsdb_wrapper.cpp").read_text(encoding="utf-8")
    finish_marker = "    bool Finish() {\n        FreeHandle();"
    assert source.count(finish_marker) == 1
    source = "static unsigned long long test_peak_loaded_records = 0;\n" + source.replace(
        finish_marker,
        """    bool Finish() {
        unsigned long long loaded = 0;
        for (const auto &entry : ctx_->id_to_sig)
            loaded += ctx_->obj->ffrGetVCCount(entry.first);
        if (loaded > test_peak_loaded_records) test_peak_loaded_records = loaded;
        FreeHandle();""",
    )
    source += r'''
extern "C" unsigned long long test_take_peak_records() {
    unsigned long long result = test_peak_loaded_records;
    test_peak_loaded_records = 0;
    return result;
}
extern "C" int test_view(void *p, unsigned long long start, unsigned long long end) {
    FsdbCtx *ctx = (FsdbCtx*)p;
    fsdbTag64 a = _ToTag(ctx, start), b = _ToTag(ctx, end);
    return ctx->obj->ffrResetViewWindow((fsdbXTag*)&a, (fsdbXTag*)&b);
}
extern "C" int test_bounds(void *p, unsigned long long *out) {
    FsdbCtx *ctx = (FsdbCtx*)p;
    fsdbTag64 a, b;
    if (ctx->obj->ffrGetMinFsdbTag64(&a) != FSDB_RC_SUCCESS ||
        ctx->obj->ffrGetMaxFsdbTag64(&b) != FSDB_RC_SUCCESS) return -1;
    out[0] = _TagToPs(ctx, a); out[1] = _TagToPs(ctx, b);
    int count = 0;
    for (ffrSessionInfo *s = ctx->obj->ffrGetSessionListInfo(); s; s = s->next) ++count;
    return count;
}
extern "C" int test_session_boundaries(void *p, unsigned long long *out, int cap) {
    FsdbCtx *ctx = (FsdbCtx*)p;
    int n = 0;
    for (ffrSessionInfo *s = ctx->obj->ffrGetSessionListInfo(); s && n + 2 <= cap; s = s->next) {
        out[n++] = _TagToPs(ctx, *(fsdbTag64*)&s->start_xtag);
        out[n++] = _TagToPs(ctx, *(fsdbTag64*)&s->close_xtag);
    }
    return n;
}
extern "C" int test_signal_state(void *p, const char *path, unsigned long long *out) {
    FsdbCtx *ctx = (FsdbCtx*)p;
    auto found = ctx->path_to_sig.find(path);
    if (found == ctx->path_to_sig.end()) return -1;
    fsdbVarIdcode id = found->second.idcode;
    out[0] = ctx->obj->ffrIsInSignalList(id) ? 1 : 0;
    out[1] = ctx->obj->ffrGetVCCount(id);
    return 0;
}
'''
    cpp, library = tmp_path / "probe.cpp", tmp_path / "probe.so"
    cpp.write_text(source, encoding="utf-8")
    subprocess.run(["g++", "-shared", "-fPIC", "-std=c++11", "-I" + str(NATIVE_FSDB),
                    "-I" + str(sdk), str(cpp), "-o", str(library),
                    "-L" + str(sdk / "linux64"), "-lnffr", "-lnsys", "-lz",
                    "-Wl,-rpath," + str(sdk / "linux64")], check=True, capture_output=True)
    return library


@pytest.mark.eda_integration
def test_real_reader_restores_an_existing_view(native_reader_probe, monkeypatch):
    """Point reads preserve an existing view and session-boundary values."""
    wave = os.environ["TW_POINT_READ_FSDB"]
    from src import fsdb_parser
    library = native_reader_probe
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(library))
    parser = FSDBParser(wave)
    try:
        parser._open()
        library = parser._lib
        library.test_view.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64]
        library.test_bounds.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
        library.test_session_boundaries.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_int]
        bounds = (ctypes.c_uint64 * 2)()
        assert library.test_bounds(parser._handle, bounds) > 1
        assert list(bounds) == [0, 10_000_000]
        assert library.test_view(parser._handle, 1_000_000, 9_000_000) == 0
        for timestamp in (8_000_003, 2_000_003, 7_000_003):
            assert parser.get_value_at_time("top.data", timestamp)["value"]["dec"] == (timestamp // 10 * 2654435761 & 0xffffffff)
            assert library.test_bounds(parser._handle, bounds) > 1
            assert list(bounds) == [1_000_000, 9_000_000]
        assert library.test_view(parser._handle, 0, 10_000_000) == 0
        boundaries = (ctypes.c_uint64 * 64)()
        count = library.test_session_boundaries(parser._handle, boundaries, 64)
        assert 2 < count < 64
        times = {t + offset for t in list(boundaries)[:count] for offset in (-1, 0, 1)
                 if 0 <= t + offset <= 10_000_000}
        for timestamp in sorted(times):
            for leaf in ("data", "sparse"):
                step = timestamp // 10
                if leaf == "sparse":
                    step = 500_000 if step >= 500_000 else 0
                assert parser.get_value_at_time("top." + leaf, timestamp)["value"]["dec"] == (step * 2654435761 & 0xffffffff)
        assert library.test_bounds(parser._handle, bounds) > 1
        assert list(bounds) == [0, 10_000_000]
    finally:
        parser.close()


@pytest.mark.eda_integration
@pytest.mark.parametrize("first_read", ["point", "transitions", "around", "group"])
@pytest.mark.parametrize("lazy_group", [False, True], ids=["eager", "lazy"])
def test_interleaved_reads_release_selection_and_residency(native_reader_probe, monkeypatch, first_read, lazy_group):
    """Dense A must not become a hidden dependency of subsequent sparse B reads."""
    from src import fsdb_parser
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(os.environ["TW_POINT_READ_FSDB"])
    fresh = FSDBParser(parser.file_path)
    dense, sparse = "top.data", "top.sparse"

    def group(paths):
        return parser._event_transition_group(paths, 0, -1) if lazy_group else parser.transition_group(paths)

    def state(path):
        out = (ctypes.c_uint64 * 2)()
        assert parser._lib.test_signal_state(parser._handle, path.encode(), out) == 0
        return tuple(out)

    def clean():
        assert state(dense) == (0, 0)
        assert state(sparse) == (0, 0)

    try:
        expected_transition = fresh.get_transitions(sparse, 3_000_000, 3_000_050)
        expected_point = fresh.get_value_at_time(sparse, 3_000_033)
        expected_around = fresh.get_signals_around_time([sparse], 3_000_033, 30, 5)
        fresh.close()
        parser._open()
        parser._lib.test_signal_state.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                  ctypes.POINTER(ctypes.c_uint64)]
        if first_read == "point":
            parser.get_value_at_time(dense, 7_000_033)
        elif first_read == "transitions":
            parser.get_transitions(dense, 7_000_000, 7_000_050)
        elif first_read == "around":
            parser.get_signals_around_time([dense], 7_000_033, 30, 5)
        else:
            with group([dense]) as active:
                assert active
                parser.get_transitions(dense, 7_000_000, 7_000_050)
                assert state(dense)[0] == 1 and state(dense)[1] > 0
        clean()
        assert parser.get_transitions(sparse, 3_000_000, 3_000_050) == expected_transition
        clean()
        assert parser.get_value_at_time(sparse, 3_000_033) == expected_point
        clean()
        assert parser.get_signals_around_time([sparse], 3_000_033, 30, 5) == expected_around
        clean()
        with group([sparse, sparse]) as active:
            assert active
            initial_state = state(sparse)
            assert initial_state[0] == 1
            assert initial_state[1] == 0 if lazy_group else initial_state[1] > 0
            assert state(dense) == (0, 0)
            # The lazy group's untouched signal has no in-core values before
            # this point seek; a premature HasIncoreVC check would return X.
            assert parser.get_value_at_time(sparse, 3_000_033) == expected_point
            group_state = state(sparse)
            assert group_state[0] == 1 and group_state[1] > 0
            assert parser.get_transitions(sparse, 3_000_000, 3_000_050) == expected_transition
            assert parser.get_signals_around_time([sparse], 3_000_033, 30, 5) == expected_around
            assert state(sparse) == group_state
            for outside_read in (
                lambda: parser.get_value_at_time(dense, 3_000_033),
                lambda: parser.get_transitions(dense, 3_000_000, 3_000_050),
                lambda: parser.get_signals_around_time([sparse, dense], 3_000_033, 30, 5),
            ):
                with pytest.raises(RuntimeError, match="rc=-5"):
                    outside_read()
                assert state(sparse) == group_state
                assert state(dense) == (0, 0)
            with parser._event_pages(sparse, 3_000_000, 3_000_050) as reader:
                page = reader.read_page()
                assert page.complete
                assert page.predecessor.value == expected_transition["predecessor"]["value"]["bin"]
            assert state(sparse) == group_state
            assert state(dense) == (0, 0)
        clean()
        # An exceptional group body must still release selection and residency.
        with pytest.raises(RuntimeError, match="injected group interruption"):
            with group([dense, sparse]) as active:
                assert active
                with parser._event_pages(dense, 100, 200, max_events=2) as reader:
                    reader.read_page()
                    raise RuntimeError("injected group interruption")
        clean()
        assert parser.get_transitions(sparse, 3_000_000, 3_000_050) == expected_transition
        clean()
    finally:
        parser.close()
        fresh.close()


def _million_bits(leaf, time_ps):
    step = time_ps // 10
    if leaf == "wide":
        step = step // 1000 * 1000
    elif leaf == "sparse":
        step = 500_000 if step >= 500_000 else 0
    bits = f"{step * 2654435761 & 0xffffffff:032b}"
    return bits * 32 if leaf == "wide" else bits


def _million_rows(leaf, start, end):
    if leaf == "sparse":
        times = [t for t in (0, 5_000_000) if start <= t <= end]
    else:
        stride = 10_000 if leaf == "wide" else 10
        times = range(max(0, (start + stride - 1) // stride * stride), min(end, 10_000_000) + 1, stride)
    return [(t, _million_bits(leaf, t)) for t in times]


def _rows(response):
    return [(row["time_ps"], row["value"]["bin"]) for row in response]


@pytest.mark.eda_integration
def test_lazy_windows_match_generator_across_sessions_in_both_directions(native_reader_probe, monkeypatch):
    """Full values, counts and true predecessor are checked against the generator."""
    from src import fsdb_parser
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(os.environ["TW_POINT_READ_FSDB"])
    try:
        parser._open()
        parser._lib.test_session_boundaries.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_int]
        boundaries = (ctypes.c_uint64 * 64)()
        count = parser._lib.test_session_boundaries(parser._handle, boundaries, 64)
        assert 2 < count < 64
        rng = random.Random(130013)
        windows = [(0, 0), (0, 13), (1, 9), (10, 10), (9_999_993, 10_000_050), (10_000_001, 10_000_030)]
        windows += [(max(0, t - 13), min(10_000_050, t + 17)) for t in list(boundaries)[:count]]
        windows += [(t, t + rng.randrange(1, 201)) for t in (rng.randrange(10_000_000) for _ in range(20))]
        for start, end in windows + list(reversed(windows)):
            for leaf in ("data", "wide", "sparse"):
                result = parser.get_transitions("top." + leaf, start, end)
                expected = _million_rows(leaf, start, end)
                assert _rows(result["transitions"]) == expected, (leaf, start, end)
                assert result["transition_count"] == len(expected)
                assert not result["truncated"] and not result["transition_count_is_lower_bound"]
                if start:
                    if leaf == "sparse":
                        previous_time = 5_000_000 if start > 5_000_000 else 0
                    else:
                        stride = 10_000 if leaf == "wide" else 10
                        previous_time = min(10_000_000, (start - 1) // stride * stride)
                    assert _rows([result["predecessor"]]) == [(previous_time, _million_bits(leaf, previous_time))]
                else:
                    assert result["predecessor"] is None
    finally:
        parser.close()


@pytest.mark.eda_integration
def test_lazy_around_keeps_full_requested_history_and_center(native_reader_probe, monkeypatch):
    from src import fsdb_parser
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(os.environ["TW_POINT_READ_FSDB"])
    rng = random.Random(316)
    centers = [0, 3, 10, 5_000_000, 10_000_000]
    centers += [rng.randrange(1, 10_000_000) for _ in range(15)]
    try:
        for center in centers + list(reversed(centers)):
            window, extra = 23, 7
            start, end = max(0, center - window), center + window
            result = parser.get_signals_around_time(["top." + leaf for leaf in ("data", "wide", "sparse")], center, window, extra)
            assert not result["truncated"]
            for leaf in ("data", "wide", "sparse"):
                signal = result["signals"]["top." + leaf]
                assert signal["value_at_center"]["bin"] == _million_bits(leaf, center)
                assert _rows(signal["transitions_in_window"]) == _million_rows(leaf, start, end)
                stride = 10_000 if leaf == "wide" else 10
                history_start = 0 if leaf == "sparse" else max(0, start - (extra + 1) * stride)
                assert _rows(signal["pre_window_transitions"]) == _million_rows(leaf, history_start, start - 1)[-extra:]
        # The last event remains historical evidence beyond EOF; the center
        # observation keeps the legacy FFR unavailable value outside the file.
        signal = parser.get_signals_around_time(["top.data"], 10_000_100, 10, 3)["signals"]["top.data"]
        assert signal["value_at_center"]["bin"] == "?"
        assert signal["transitions_in_window"] == []
        assert _rows(signal["pre_window_transitions"]) == _million_rows("data", 9_999_980, 10_000_000)
    finally:
        parser.close()


@pytest.fixture(scope="module")
def same_tick_fsdb(tmp_path_factory):
    """Shared, independently validated 100fs sequence fixture."""
    return Path(os.environ["TW_SAME_TICK_FSDB"])


@pytest.mark.eda_integration
@pytest.mark.parametrize("lazy_group", [False, True], ids=["eager", "lazy"])
def test_lazy_public_windows_preserve_same_tick_and_subps_order(native_reader_probe, same_tick_fsdb, monkeypatch, lazy_group):
    from src import fsdb_parser
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(str(same_tick_fsdb))
    # Public transitions compare integer ceil timestamps; native pages instead
    # use physical femtoseconds. Several physical times intentionally share 1ps.
    raw = [(0, "0"), (1, "1"), (2, "x"), (9, "z"),
           (10, "0"), (10, "1"), (10, "x"), (10, "0"),
           (11, "1"), (20, "z"), (29, "0"), (30, "1")]
    public = [((tick + 9) // 10, value) for tick, value in raw]
    try:
        for start, end in [(0, 4), (1, 1), (2, 2), (3, 3), (2, 4), (4, 4), (0, 0)]:
            result = parser.get_transitions("top.data", start, end)
            expected = [row for row in public if start <= row[0] <= end]
            assert _rows(result["transitions"]) == expected, (start, end)
            assert result["transition_count"] == len(expected)
            earlier = [row for row in public if row[0] < start]
            assert (_rows([result["predecessor"]]) if result["predecessor"] else []) == earlier[-1:]
            around = parser.get_signals_around_time(["top.data", "top.sparse"], start, 0, 100)
            signal = around["signals"]["top.data"]
            assert _rows(signal["transitions_in_window"]) == [row for row in public if row[0] == start]
            assert _rows(signal["pre_window_transitions"]) == earlier
            center_value = [value for tick, value in raw if tick <= start * 10][-1]
            assert signal["value_at_center"]["bin"] == center_value
            assert not around["truncated"]
        # Independently verify the converter really stored every same-tick
        # event. Native pages use physical fs and preserve sequence across caps.
        for start, end in ((0, 4), (1, 2)):
            for cap in (1, 2, 3):
                group = (parser._event_transition_group(["top.data"], start, end)
                         if lazy_group else parser.transition_group(["top.data"]))
                with group as active:
                    assert active
                    with parser._event_pages("top.data", start, end, max_events=cap) as reader:
                        events, predecessor = [], None
                        while True:
                            page = reader.read_page()
                            events.extend((event.time_fs, event.value) for event in page.events)
                            if page.predecessor is not None:
                                assert predecessor is None
                                predecessor = (page.predecessor.time_fs, page.predecessor.value)
                            assert not page.truncated
                            if page.complete:
                                break
                assert events == [(tick * 100, value) for tick, value in raw if start * 10 <= tick <= end * 10]
                earlier = [(tick * 100, value) for tick, value in raw if tick < start * 10]
                assert predecessor == (earlier[-1] if earlier else None)
    finally:
        parser.close()


@pytest.mark.eda_integration
def test_lazy_narrow_read_loads_only_a_fraction_of_multisession_trace(native_reader_probe, monkeypatch):
    """A fixture-specific resident-record bound, not a general byte/RSS claim."""
    from src import fsdb_parser
    wave = os.environ.get("TW_LAZY_LARGE_FSDB")
    if not wave:
        pytest.skip("set TW_LAZY_LARGE_FSDB to activity_10000000.fsdb")
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(wave)
    try:
        parser._open()
        parser._lib.test_take_peak_records.restype = ctypes.c_uint64
        parser._lib.test_take_peak_records()
        result = parser.get_transitions("top.data", 90_000_003, 90_000_203)
        expected_times = range(90_000_010, 90_000_204, 10)
        assert _rows(result["transitions"]) == [(t, f"{t // 10 * 2654435761 & 0xffffffff:032b}") for t in expected_times]
        assert result["predecessor"]["time_ps"] == 90_000_000
        loaded = parser._lib.test_take_peak_records()
        # This converted trace has ~107K events/session and 10M total events.
        assert 20 <= loaded < 1_000_000
        result = parser.get_signals_around_time(["top.data"], 90_000_033, 100, 5)
        assert len(result["signals"]["top.data"]["transitions_in_window"]) == 20
        assert 20 <= parser._lib.test_take_peak_records() < 1_000_000
    finally:
        parser.close()


@pytest.mark.eda_integration
def test_lazy_event_group_cancellation_releases_loaded_and_untouched_signals(native_reader_probe, monkeypatch):
    import threading
    from src import fsdb_parser
    from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event

    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(os.environ["TW_POINT_READ_FSDB"])
    event = threading.Event()
    token = push_cancel_event(event)
    paths = ["top.data", "top.sparse", "top.wide"]
    try:
        parser._open()
        parser._lib.test_signal_state.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                  ctypes.POINTER(ctypes.c_uint64)]
        original = parser._lib.fsdb_event_page_v1

        def cancel_after_first_page(*args):
            rc = original(*args)
            event.set()
            return rc

        with monkeypatch.context() as native_patch:
            native_patch.setattr(parser._lib, "fsdb_event_page_v1", cancel_after_first_page)
            with pytest.raises(OperationCancelled):
                with parser._event_transition_group(paths, 9_000_003, 9_000_203) as active:
                    assert active
                    for path in paths[1:]:
                        state = (ctypes.c_uint64 * 2)()
                        assert parser._lib.test_signal_state(parser._handle, path.encode(), state) == 0
                        assert tuple(state) == (1, 0)
                    with parser._event_pages(paths[0], 9_000_003, 9_000_203, max_events=2) as reader:
                        reader.read_page()
        assert not parser._transition_group_active
        for path in paths:
            state = (ctypes.c_uint64 * 2)()
            assert parser._lib.test_signal_state(parser._handle, path.encode(), state) == 0
            assert tuple(state) == (0, 0)
        event.clear()
        result = parser.get_transitions("top.data", 9_000_003, 9_000_023)
        assert _rows(result["transitions"]) == _million_rows("data", 9_000_003, 9_000_023)
    finally:
        pop_cancel_event(token)
        parser.close()


@pytest.mark.eda_integration
def test_lazy_multisignal_event_group_avoids_full_history_residency(native_reader_probe, monkeypatch):
    """The actual event_readers entry point shares an owned, unloaded group."""
    from src import fsdb_parser
    from src.waveform_batch import event_readers

    wave = os.environ.get("TW_LAZY_LARGE_FSDB")
    if not wave:
        pytest.skip("set TW_LAZY_LARGE_FSDB to activity_10000000.fsdb")
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(native_reader_probe))
    parser = FSDBParser(wave)
    start, end = 90_000_003, 90_000_203
    paths = ["top.data", "top.sparse", "top.wide"]
    try:
        parser._open()
        parser._lib.test_signal_state.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                  ctypes.POINTER(ctypes.c_uint64)]
        with event_readers([(parser, path) for path in paths], start, end, max_events=2) as readers:
            for index, reader in enumerate(readers):
                rows, previous = [], None
                while True:
                    page = reader.read_page()
                    rows.extend((row.time_ps, row.value) for row in page.events)
                    if page.predecessor is not None:
                        assert previous is None
                        previous = (page.predecessor.time_ps, page.predecessor.value)
                    assert not page.truncated
                    if page.complete:
                        break
                if index == 0:
                    expected_times = range(90_000_010, 90_000_204, 10)
                    assert rows == [(t, f"{t // 10 * 2654435761 & 0xffffffff:032b}") for t in expected_times]
                    assert previous == (90_000_000, f"{9_000_000 * 2654435761 & 0xffffffff:032b}")
                elif index == 1:
                    assert rows == []
                    assert previous == (50_000_000, f"{5_000_000 * 2654435761 & 0xffffffff:032b}")
                else:
                    assert rows == []
                    assert previous == (90_000_000, f"{9_000_000 * 2654435761 & 0xffffffff:032b}" * 32)
            resident_records = 0
            for path in paths:
                state = (ctypes.c_uint64 * 2)()
                assert parser._lib.test_signal_state(parser._handle, path.encode(), state) == 0
                assert state[0] == 1
                resident_records += state[1]
            assert 20 <= resident_records < 1_000_000
        for path in paths:
            state = (ctypes.c_uint64 * 2)()
            assert parser._lib.test_signal_state(parser._handle, path.encode(), state) == 0
            assert tuple(state) == (0, 0)
    finally:
        parser.close()
