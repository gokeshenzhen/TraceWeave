"""Opt-in real FFR regressions for bounded lazy cursors and count-only pages.

TW_POINT_READ_FSDB enables the local SDK fixtures. TW_LAZY_LARGE_FSDB names
the unchanged 10M-step, multi-session waveform from benchmark_waveform_reads.
Temporary instrumented wrappers observe FFR calls; no production test ABI or
simulator run is needed.
"""

from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import pytest

from src import fsdb_parser
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.fsdb_parser import FSDBParser, _NativeEventPageV1
from tests.test_fsdb_point_window import same_tick_fsdb  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native/fsdb"
pytestmark = pytest.mark.eda_integration

RAW_EVENTS = [(0, "0"), (1, "1"), (2, "x"), (9, "z"),
              (10, "0"), (10, "1"), (10, "x"), (10, "0"),
              (11, "1"), (20, "z"), (29, "0"), (30, "1")]


@pytest.fixture(scope="module")
def reclaim_libraries(tmp_path_factory):
    verdi = os.environ.get("VERDI_HOME")
    compiler = shutil.which("g++")
    if not verdi or not compiler:
        pytest.skip("requires VERDI_HOME and g++")
    sdk = Path(verdi) / "share/FsdbReader"
    directory = tmp_path_factory.mktemp("cursor_reclaim")
    libraries = {}

    def build(mode):
        if mode in libraries:
            return libraries[mode]
        source = (NATIVE / "fsdb_wrapper.cpp").read_text(encoding="utf-8")
        # Observe immediately before every explicit per-ID unload, including
        # reclamation. Page-boundary sampling alone could miss a resident peak.
        marker = "static bool _CloseEventCursors(FsdbCtx *ctx) {"
        assert source.count(marker) == 1
        unload = "ctx->obj->ffrUnloadSignals(id)"
        assert source.count(unload) == 1
        source = source.replace(unload, "test_observe_unload(ctx, id)")
        reclaim = "    if (!FsdbReclaimEventCursors(ctx->obj, sig->idcode, siblings, ctx->cleanup_failed))"
        assert source.count(reclaim) == 1
        source = source.replace(reclaim,
            "    test_observe_records(ctx);\n    ++test_unloads;\n" + reclaim)
        source = source.replace(marker, r'''
static unsigned long long test_peak_records = 0, test_unloads = 0;
static void test_observe_records(FsdbCtx *ctx) {
    unsigned long long loaded = 0;
    for (const auto &entry : ctx->id_to_sig)
        loaded += ctx->obj->ffrGetVCCount(entry.first);
    if (loaded > test_peak_records) test_peak_records = loaded;
}
static fsdbRC test_observe_unload(FsdbCtx *ctx, fsdbVarIdcode id) {
    test_observe_records(ctx);
    ++test_unloads;
    return ctx->obj->ffrUnloadSignals(id);
}
''' + marker)
        # Count-only traversal must not fetch values, including wide vectors.
        source, replacements = re.subn(
            r"\b(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*->ffrGetVC\([^)]*\)",
            lambda match: "(++test_get_vc_calls, " + match.group() + ")", source)
        assert replacements >= 1
        source = "static unsigned long long test_get_vc_calls = 0;\n" + source
        source += r'''
extern "C" void test_probe_reset() {
    test_peak_records = test_unloads = test_get_vc_calls = 0;
}
extern "C" void test_probe_snapshot(void *p, unsigned long long *out) {
    FsdbCtx *ctx = (FsdbCtx*)p;
    test_observe_records(ctx);
    out[0] = test_peak_records;
    out[1] = test_unloads;
    out[2] = test_get_vc_calls;
    out[3] = ctx->event_cursors.size();
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
// Inject only the optional metadata policy. FFR event data, traversal and
// Restore remain real, including the fixture's same-tick/sub-ps events.
struct TestSessionReader {
    ffrSessionInfo rows[3] = {};
    int mode;
    explicit TestSessionReader(int m) : mode(m) {
        rows[0].next = &rows[1];
        rows[0].close_xtag.hltag.L = 1;
        rows[1].start_xtag.hltag.L = 1;
        rows[1].next = &rows[2];
        rows[1].close_xtag.hltag.L = 10;
        rows[2].start_xtag.hltag.L = 10;
        rows[2].close_xtag.hltag.L = 40;
        if (mode == 2) rows[1].start_xtag.hltag.L = 0;
        if (mode == 3) { rows[0].next = nullptr; rows[0].close_xtag.hltag.L = 40; }
    }
    fsdbFileType ffrGetFileType() { return FSDB_FT_VERILOG; }
    fsdbRC ffrGetMinFsdbTag64(fsdbTag64 *tag) { *tag = {}; return FSDB_RC_SUCCESS; }
    fsdbRC ffrGetMaxFsdbTag64(fsdbTag64 *tag) { *tag = {}; tag->L = 40; return FSDB_RC_SUCCESS; }
    ffrSessionInfo *ffrGetSessionListInfo() {
        if (mode == 4) throw 1;
        return mode == 1 ? nullptr : rows;
    }
};
extern "C" int test_session_plan(void *p, int mode) {
    FsdbCtx *ctx = (FsdbCtx*)p;
    ctx->event_session_plan = FsdbSessionReclaimPlan{};
    TestSessionReader reader(mode);
    if (mode == 5) ctx->path_to_sig["top.alias"] = ctx->path_to_sig.at("top.data");
    return ctx->event_session_plan.Load(&reader) ? 1 : 0;
}
'''
        flags = []
        if mode.startswith("records"):
            flags = ["-DTRACEWEAVE_FSDB_EVENT_RECLAIM_RECORDS=" + mode[7:]]
        elif mode == "bytes1":
            flags = ["-DTRACEWEAVE_FSDB_EVENT_RECLAIM_RECORDS=1000000000",
                     "-DTRACEWEAVE_FSDB_EVENT_RECLAIM_BYTES=1"]
        else:
            assert mode == "default"
        cpp, library = directory / (mode + ".cpp"), directory / (mode + ".so")
        cpp.write_text(source, encoding="utf-8")
        subprocess.run([compiler, "-shared", "-fPIC", "-std=c++11", *flags,
                        "-I" + str(NATIVE), "-I" + str(sdk), str(cpp), "-o", str(library),
                        "-L" + str(sdk / "linux64"), "-lnffr", "-lnsys", "-lz",
                        "-Wl,-rpath," + str(sdk / "linux64")],
                       check=True, capture_output=True, text=True, timeout=120)
        libraries[mode] = library
        return library

    return build


@contextmanager
def _parser(library, wave, monkeypatch):
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(library))
    parser = FSDBParser(str(wave))
    try:
        parser._open()
        lib = parser._lib
        # Deliberately private binding: Python production adopts this optional
        # ABI separately; these tests exercise native count/page interleaving.
        lib.fsdb_event_count_v1.restype = ctypes.c_int
        lib.fsdb_event_count_v1.argtypes = [ctypes.c_void_p, ctypes.c_uint64,
            ctypes.c_uint, ctypes.POINTER(_NativeEventPageV1), ctypes.c_uint]
        lib.test_probe_reset.argtypes, lib.test_probe_reset.restype = [], None
        lib.test_probe_snapshot.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
        lib.test_probe_snapshot.restype = None
        lib.test_signal_state.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.POINTER(ctypes.c_uint64)]
        lib.test_signal_state.restype = ctypes.c_int
        lib.test_probe_reset()
        yield parser
    finally:
        parser.close()


@pytest.fixture(params=["records1", "records2", "bytes1"])
def forced_parser(request, reclaim_libraries, same_tick_fsdb, monkeypatch):
    with _parser(reclaim_libraries(request.param), same_tick_fsdb, monkeypatch) as parser:
        yield parser


@pytest.fixture(scope="module")
def predecessor_fsdb(tmp_path_factory, same_tick_fsdb):
    # Reuse the existing fixture's SDK opt-in checks. Leave its source/FSDB
    # untouched; this second trace leaves a whole-ps gap after a glitch group.
    directory = tmp_path_factory.mktemp("reclaim_predecessor")
    vcd, fsdb = directory / "predecessor.vcd", directory / "predecessor.fsdb"
    vcd.write_text(
        '$timescale 100fs $end\n$scope module top $end\n'
        '$var wire 1 ! data $end\n$var wire 1 @ sparse $end\n'
        '$upscope $end\n$enddefinitions $end\n'
        '#0\n0!\n0@\n#10\n1!\nx!\n0!\nz!\n'
        '#30\n1!\n#40\n1@\n#50\n')
    converter = Path(os.environ["VERDI_HOME"]) / "bin/vcd2fsdb"
    subprocess.run([str(converter), str(vcd), "-seq_num", "-o", str(fsdb)],
                   cwd=directory, env={**os.environ, "FSDB_ENV_MAX_GLITCH_NUM": "0"},
                   check=True, capture_output=True, text=True, timeout=30)
    return fsdb


def _snapshot(parser):
    out = (ctypes.c_uint64 * 4)()
    parser._lib.test_probe_snapshot(parser._handle, out)
    return dict(zip(("peak_records", "unloads", "get_vc", "cursors"), out))


def _assert_clean(parser, paths=("top.data", "top.sparse")):
    assert not parser._transition_group_active
    assert _snapshot(parser)["cursors"] == 0
    for path in paths:
        out = (ctypes.c_uint64 * 2)()
        assert parser._lib.test_signal_state(parser._handle, path.encode(), out) == 0
        assert tuple(out) == (0, 0), path


class _Cursor:
    def __init__(self, parser, path="top.data", start=0, end=4):
        self.parser, self.cursor = parser, ctypes.c_uint64()
        assert parser._lib.fsdb_event_open_v1(parser._handle, path.encode(), start,
            0xffffffffffffffff if end == -1 else end, ctypes.byref(self.cursor)) == 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        assert self.parser._lib.fsdb_event_close_v1(self.parser._handle, self.cursor) == 0

    def read(self, limit, *, count=False):
        receipt = _NativeEventPageV1()
        lib, handle = self.parser._lib, self.parser._handle
        if count:
            assert lib.fsdb_event_count_v1(handle, self.cursor, limit,
                ctypes.byref(receipt), ctypes.sizeof(receipt)) == 0
            assert receipt.output_bytes == 0
            rows = None
        else:
            buffer = ctypes.create_string_buffer(65536)
            assert lib.fsdb_event_page_v1(handle, self.cursor, limit, buffer, len(buffer),
                ctypes.byref(receipt), ctypes.sizeof(receipt)) == 0
            rows = [(kind, int(ps), int(tick), value) for kind, ps, tick, value in
                    (line.split("\t") for line in buffer.value.decode().splitlines())]
            assert receipt.events == len(rows)
        assert not receipt.truncated
        assert bool(receipt.has_next) != bool(receipt.complete)
        return receipt, rows


def _expected(raw=RAW_EVENTS, *, start=0, end=4):
    before = [row for row in raw if row[0] < start * 10]
    rows = before[-1:] + [row for row in raw if start * 10 <= row[0] <= end * 10]
    return [("P" if tick < start * 10 else "T", (tick + 9) // 10, tick, value)
            for tick, value in rows]


def _take(cursor, remaining, limit, *, count=False):
    receipt, rows = cursor.read(limit, count=count)
    expected, remaining = remaining[:limit], remaining[limit:]
    assert receipt.events == len(expected)
    if rows is not None:
        assert rows == expected
    assert bool(receipt.complete) == (not remaining)
    if remaining:
        assert receipt.next_tick == remaining[0][2]
    return remaining


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_reclaimed_pages_preserve_all_same_tick_and_raw_subps_events(forced_parser, cap):
    parser = forced_parser
    with parser._event_transition_group(["top.data"], 0, 4) as active:
        assert active
        with _Cursor(parser) as cursor:
            remaining = _expected()
            while remaining:
                remaining = _take(cursor, remaining, cap)
            _take(cursor, [], cap)
            assert _snapshot(parser)["unloads"] > 0
    _assert_clean(parser)


def test_count_and_value_pages_share_one_exact_resume_position(forced_parser):
    parser = forced_parser
    with parser._event_transition_group(["top.data"], 0, 4) as active:
        assert active
        with _Cursor(parser) as cursor:
            remaining = _expected()
            for count, cap in [(False, 2), (True, 3), (False, 1),
                               (True, 2), (False, 3), (True, 65536)]:
                before = _snapshot(parser)["get_vc"]
                remaining = _take(cursor, remaining, cap, count=count)
                if count:
                    assert _snapshot(parser)["get_vc"] == before
            assert remaining == []
            _take(cursor, [], 1)
            _take(cursor, [], 65536, count=True)
    _assert_clean(parser)


def test_reclaim_restores_duplicate_cursors_at_distinct_same_tick_ordinals(forced_parser):
    parser = forced_parser
    with parser._event_transition_group(["top.data"], 0, 4) as active:
        assert active
        with _Cursor(parser) as first, _Cursor(parser) as second:
            a, b = _expected(), _expected()
            a = _take(first, a, 5)  # Next: tick 10, ordinal 1, value 1.
            b = _take(second, b, 7)  # Next: tick 10, ordinal 3, value 0.
            a = _take(first, a, 1)
            b = _take(second, b, 1, count=True)
            while a or b:
                if a:
                    a = _take(first, a, 1)
                if b:
                    b = _take(second, b, 2)
            _take(first, [], 1)
            _take(second, [], 1, count=True)
            assert _snapshot(parser)["cursors"] == 2
            assert _snapshot(parser)["unloads"] > 0
    _assert_clean(parser)


@pytest.mark.parametrize("mode", ["records1", "records2", "bytes1"])
def test_unread_predecessor_restores_last_event_at_tick_after_sibling_reclaim(
        reclaim_libraries, predecessor_fsdb, monkeypatch, mode):
    raw = [(0, "0"), (10, "1"), (10, "x"), (10, "0"), (10, "z"), (30, "1")]
    with _parser(reclaim_libraries(mode), predecessor_fsdb, monkeypatch) as parser:
        with parser._event_transition_group(["top.data", "top.sparse"], 0, 5) as active:
            assert active
            with _Cursor(parser, end=5) as first, \
                    _Cursor(parser, start=2, end=5) as previous:
                remaining = _expected(raw, end=5)
                # Previous has never advanced: GotoXTag landed at the LAST
                # event of tick 10. First forces its handle to be recreated.
                for _ in range(3):
                    remaining = _take(first, remaining, 1)
                assert _snapshot(parser)["unloads"] > 0
                tail = _take(previous, _expected(raw, start=2, end=5), 1)
                assert tail == [("T", 3, 30, "1")]
                _take(previous, tail, 1, count=True)
                while remaining:
                    remaining = _take(first, remaining, 1)
        _assert_clean(parser)


@pytest.mark.parametrize("path,start,end", [("top.sparse", 1, 1),
    ("top.sparse", 1, 4), ("top.data", 4, 4), ("top.data", 5, 6), ("top.data", 1, 2)])
def test_sparse_predecessor_empty_window_and_eof_survive_reclaim(forced_parser, path, start, end):
    parser = forced_parser
    raw = [(0, "0"), (20, "1")] if path == "top.sparse" else RAW_EVENTS
    with parser._event_transition_group([path], start, end) as active:
        assert active
        with _Cursor(parser, path, start, end) as cursor:
            remaining = _expected(raw, start=start, end=end)
            remaining = _take(cursor, remaining, 1)
            while remaining:
                remaining = _take(cursor, remaining, 1, count=True)
            _take(cursor, [], 1)
            _take(cursor, [], 1, count=True)
    _assert_clean(parser)


def test_invalid_count_arguments_do_not_advance_cursor(forced_parser):
    parser = forced_parser
    with parser._event_transition_group(["top.data"], 0, 4) as active:
        assert active
        with _Cursor(parser) as cursor:
            receipt = _NativeEventPageV1()
            for limit, size in [(0, ctypes.sizeof(receipt)),
                                (65537, ctypes.sizeof(receipt)), (1, 0)]:
                assert parser._lib.fsdb_event_count_v1(parser._handle, cursor.cursor, limit,
                    ctypes.byref(receipt), size) == -1
            _take(cursor, _expected(), 1)
        assert parser._lib.fsdb_event_count_v1(parser._handle, cursor.cursor, 1,
            ctypes.byref(receipt), ctypes.sizeof(receipt)) == -5
    _assert_clean(parser)


def test_cancellation_after_reclaim_releases_sibling_and_untouched_signals(forced_parser, monkeypatch):
    parser = forced_parser
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    try:
        with pytest.raises(OperationCancelled):
            with parser._event_transition_group(["top.data", "top.sparse"], 0, 4) as active:
                assert active
                with parser._event_pages("top.data", 0, 4, max_events=1) as first, \
                        parser._event_pages("top.data", 0, 4, max_events=1) as second:
                    for _ in range(3):
                        first.read_page()
                    second.read_page()
                    assert _snapshot(parser)["unloads"] > 0
                    original = parser._lib.fsdb_event_page_v1

                    def cancel_on_return(*args):
                        rc = original(*args)
                        cancel.set()
                        return rc

                    with monkeypatch.context() as patch:
                        patch.setattr(parser._lib, "fsdb_event_page_v1", cancel_on_return)
                        first.read_page()
        _assert_clean(parser)
        cancel.clear()
        with parser._event_transition_group(["top.data"], 0, 4) as active:
            assert active
            with _Cursor(parser) as cursor:
                _take(cursor, _expected(), 12)
        _assert_clean(parser)
    finally:
        pop_cancel_event(token)


def test_full_ten_million_count_is_exact_without_value_reads_and_bounds_residency(
        reclaim_libraries, monkeypatch, record_property):
    wave = os.environ.get("TW_LAZY_LARGE_FSDB")
    if not wave:
        pytest.skip("set TW_LAZY_LARGE_FSDB to activity_10000000.fsdb")
    with _parser(reclaim_libraries("default"), wave, monkeypatch) as parser:
        with parser._event_transition_group(["top.data"], 0, -1) as active:
            assert active
            with _Cursor(parser, end=-1) as cursor:
                total, pages = 0, 0
                started = time.perf_counter()
                while True:
                    receipt, _ = cursor.read(65536, count=True)
                    total += receipt.events
                    pages += 1
                    if receipt.complete:
                        break
                    assert receipt.next_tick == total * 10
                elapsed = time.perf_counter() - started
                stats = _snapshot(parser)
                assert total == 10_000_001
                assert stats["get_vc"] == 0
                assert stats["unloads"] >= 30
                # This fixture has ~107K records per flush session. FFR can
                # load a whole session: this is not a general byte/RSS bound.
                assert 65536 <= stats["peak_records"] < 1_000_000
                _take(cursor, [], 65536, count=True)
                for name, value in {**stats, "records": total, "pages": pages,
                                    "elapsed_sec": elapsed}.items():
                    record_property(name, value)
        _assert_clean(parser, ("top.data", "top.sparse", "top.wide", "top.clk"))
