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


@pytest.fixture(scope="module")
def native_lifecycle_probe(tmp_path_factory):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    executable = tmp_path_factory.mktemp("point_read") / "lifecycle"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(ROOT), str(ROOT / "tests/native/fsdb_point_read_test.cpp"),
                    "-o", str(executable)], check=True, capture_output=True, text=True)
    return executable


@pytest.mark.parametrize("scenario", ["success", "resident", "bounds", "disabled", "before", "after",
                                      "set", "unsupported", "add", "load", "create", "throw_load", "throw_body",
                                      "restore", "unload"])
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


@pytest.mark.eda_integration
def test_real_reader_restores_an_existing_view(tmp_path, monkeypatch):
    """Expose test-only bounds access; production keeps its native ABI."""
    wave = os.environ.get("TW_POINT_READ_FSDB")
    verdi = os.environ.get("VERDI_HOME")
    if not wave or not verdi or not shutil.which("g++"):
        pytest.skip("requires TW_POINT_READ_FSDB, VERDI_HOME and g++")
    from src import fsdb_parser
    sdk = Path(verdi) / "share/FsdbReader"
    source = (ROOT / "fsdb_wrapper.cpp").read_text() + r'''
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
'''
    cpp, library = tmp_path / "probe.cpp", tmp_path / "probe.so"
    cpp.write_text(source)
    subprocess.run(["g++", "-shared", "-fPIC", "-std=c++11", "-I" + str(ROOT),
                    "-I" + str(sdk), str(cpp), "-o", str(library),
                    "-L" + str(sdk / "linux64"), "-lnffr", "-lnsys", "-lz",
                    "-Wl,-rpath," + str(sdk / "linux64")], check=True, capture_output=True)
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
