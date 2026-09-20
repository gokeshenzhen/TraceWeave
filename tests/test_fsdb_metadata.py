"""Metadata oracles, bounded caching, identity and optional ABI compatibility."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from src import fsdb_parser as fs
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.verify_condition import _resolve_signal_path
from src.txn_reconstruct import _exists


class MetadataLib:
    _traceweave_has_metadata_v1 = True
    _traceweave_has_transition_group = True

    def __init__(self):
        self.calls = []
        self.width = 8
        self.scale = 1000
        self.samples = ["a.s" + str(i) for i in range(20)]
        self.tops = ["a", "z"]

    def fsdb_open(self, path):
        self.calls.append("open")
        self.width = int(Path(path.decode()).read_text())
        return 1

    def fsdb_close(self, handle):
        self.calls.append("close")

    def fsdb_get_scale_info(self, handle, buf, size):
        buf.value = b"1ps" if self.scale else b"unknown"
        return self.scale

    def fsdb_get_signal_metadata_v1(self, handle, path, out, size):
        self.calls.append("exact")
        if b"missing" in path:
            return -2
        out._obj.width, out._obj.direction, out._obj.var_type = self.width, 2, 15
        return 0

    def fsdb_get_end_time(self, handle):
        self.calls.append("end")
        return 100 * self.width if self.scale else 0

    def fsdb_get_signal_count(self, handle):
        self.calls.append("count")
        return 553185

    def fsdb_get_summary_paths_v1(self, handle, kind, limit, buf, size, truncated):
        self.calls.append("sample" if kind == 0 else "tops")
        items = self.samples if kind == 0 else self.tops
        truncated._obj.value = len(items) > limit
        data = b"".join(s.encode() + b"\0" for s in items[:limit])
        ctypes.memmove(buf, data, len(data))
        return len(items[:limit])

    def fsdb_begin_transition_group(self, *args):
        self.calls.append("begin")
        return 0

    def fsdb_end_transition_group(self, *args):
        self.calls.append("end_group")
        return 0


@pytest.fixture
def parser(tmp_path):
    path = tmp_path / "wave.fsdb"
    path.write_text("8")
    p = fs.FSDBParser(str(path))
    p._lib = MetadataLib()
    yield p
    p.close()


@pytest.mark.parametrize("path", ["top.left.same", "top.right.same", "top.gen[3].bus[7:0]",
                                 "top.\\escaped.with.dot ", "top.a[0:7]", "top.alias"])
def test_exact_metadata_never_searches(parser, path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("exact metadata must not search")
    monkeypatch.setattr(parser, "search_signals", forbidden)
    assert parser.get_signal_width(path) == parser.get_signal_width(path) == 8
    assert parser._lib.calls.count("exact") == 1
    assert parser._metadata_cache[path][0] == (8, 2, 15)
    assert parser._buf is None  # no 64 MiB transition buffer for metadata


def test_header_does_not_list_and_summary_is_bounded_immutable(parser):
    header = parser.get_header()
    assert parser._lib.calls == ["open", "end", "count"]
    header["simulation_duration_ps"] = 1
    assert parser.get_header()["simulation_duration_ps"] == 800
    summary = parser.get_summary()
    assert summary["top_modules"] == ["a", "z"]  # z is absent from the sample
    assert summary["top_modules_complete"]
    assert summary["sample_signals_order"] == "lexical"
    summary["sample_signals"].clear()
    summary["top_modules"].clear()
    assert len(parser.get_summary()["sample_signals"]) == 20
    assert parser.get_summary()["top_modules"] == ["a", "z"]
    assert parser._lib.calls == ["open", "end", "count", "sample", "tops"]
    assert parser._buf is None


def test_top_cap_never_claims_complete(parser, monkeypatch):
    monkeypatch.setattr(fs, "_SUMMARY_TOP_LIMIT", 1)
    s = parser.get_summary()
    assert s["top_modules"] == ["a"] and s["top_modules_complete"] is False


def test_metadata_cache_entry_lru_and_byte_caps(parser, monkeypatch):
    monkeypatch.setattr(fs, "_METADATA_MAX_ENTRIES", 2)
    for name in ("a", "b", "a", "c"):
        parser.get_signal_width(name)
    assert list(parser._metadata_cache) == ["a", "c"]
    parser.get_signal_width("b")
    assert parser._lib.calls.count("exact") == 4
    monkeypatch.setattr(fs, "_METADATA_MAX_BYTES", 1)
    parser.close()
    parser.get_signal_width("oversize")
    parser.get_signal_width("oversize")
    assert not parser._metadata_cache and parser._metadata_bytes == 0


def test_metadata_byte_budget_evicts_before_entry_cap(parser, monkeypatch):
    parser.get_signal_width("a")
    monkeypatch.setattr(fs, "_METADATA_MAX_BYTES", parser._metadata_bytes + 1)
    parser.get_signal_width("b")
    assert list(parser._metadata_cache) == ["b"]
    assert parser._metadata_bytes <= fs._METADATA_MAX_BYTES


def test_legacy_wrapper_fallback_and_positive_cache(parser, monkeypatch):
    parser._lib._traceweave_has_metadata_v1 = False
    searches = []
    def search(keyword, **kw):
        searches.append(keyword)
        return {"results": [{"path": "top.s", "width": 3}]}
    monkeypatch.setattr(parser, "search_signals", search)
    assert parser.get_signal_width("s") == parser.get_signal_width("s") == 3
    assert searches == ["s", "s"]  # preserved exact then suffix search
    a, b = parser.get_summary(), parser.get_summary()
    assert a == b and a["metadata_query_mode"] == "legacy_search"
    assert not a["top_modules_complete"]
    assert searches.count("") == 1
    assert "exact" not in parser._lib.calls


def test_missing_lookup_is_not_negatively_cached(parser, monkeypatch):
    monkeypatch.setattr(parser, "search_signals", lambda *a, **k: {"results": []})
    for _ in range(2):
        with pytest.raises(KeyError):
            parser.get_signal_width("missing")
    assert parser._lib.calls.count("exact") == 2
    assert not parser._metadata_cache


@pytest.mark.parametrize("change", ["replace", "write", "symlink"])
def test_file_identity_invalidates_all_metadata(parser, tmp_path, change):
    assert parser.get_signal_width("s") == 8
    assert parser.get_summary()["simulation_duration_ps"] == 800
    parser._cached_clock_info = ("old.clock", 5)
    path = Path(parser.file_path)
    old = path.stat()
    if change == "write":
        path.write_text("4")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1))
    else:
        replacement = tmp_path / "replacement"
        replacement.write_text("4")
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        if change == "replace":
            replacement.replace(path)
        else:
            path.unlink()
            path.symlink_to(replacement)
    assert parser.get_signal_width("s") == 4
    assert not hasattr(parser, "_cached_clock_info")
    assert parser.get_summary()["simulation_duration_ps"] == 400
    assert parser._lib.calls.count("open") == 2
    assert parser._lib.calls.count("sample") == 2
    parser.close()
    assert not parser._metadata_cache and parser._summary_paths is None and parser._header is None
    assert parser.get_signal_width("s") == 4


def test_removed_file_does_not_return_cached_header(parser):
    parser.get_header()
    Path(parser.file_path).unlink()
    with pytest.raises(FileNotFoundError):
        parser.get_header()
    assert parser._header is None and parser._handle is None


def test_change_during_native_lookup_does_not_publish_cache(parser, monkeypatch):
    original = parser._lib.fsdb_get_signal_metadata_v1
    def changing(*args):
        result = original(*args)
        path = Path(parser.file_path)
        old = path.stat()
        path.write_text("4")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1))
        return result
    monkeypatch.setattr(parser._lib, "fsdb_get_signal_metadata_v1", changing)
    with pytest.raises(RuntimeError, match="changed during metadata"):
        parser.get_signal_width("s")
    assert not parser._metadata_cache and parser._handle is None


def test_summary_does_not_hide_file_change_as_listing_failure(parser, monkeypatch):
    original = parser._lib.fsdb_get_summary_paths_v1
    def changing(*args):
        result = original(*args)
        path = Path(parser.file_path)
        old = path.stat()
        path.write_text("4")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1))
        return result
    monkeypatch.setattr(parser._lib, "fsdb_get_summary_paths_v1", changing)
    with pytest.raises(RuntimeError, match="changed during metadata"):
        parser.get_summary()
    assert parser._summary_paths is None and parser._header is None


def test_changed_file_in_group_unloads_once_then_reopens(parser):
    with parser.transition_group(["s"]):
        path = Path(parser.file_path)
        old = path.stat()
        path.write_text("4")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1))
        with pytest.raises(RuntimeError, match="active transition group"):
            parser.get_header()
        assert "close" not in parser._lib.calls
    assert parser._lib.calls.count("end_group") == 1
    assert parser.get_signal_width("s") == 4


def test_unknown_scale_stays_unknown_and_never_reads_values(parser, monkeypatch):
    parser._lib.scale = 0
    monkeypatch.setattr(parser, "get_transitions", lambda *a: pytest.fail("unknown scale recovery"))
    h = parser.get_header()
    assert h["scale_fs_per_tick"] == 0 and h["simulation_duration_ps"] == 0
    assert "scale_warning" in h
    assert "sample" not in parser._lib.calls


def test_cancelled_duration_recovery_does_not_cache_header(parser, monkeypatch):
    monkeypatch.setattr(parser._lib, "fsdb_get_end_time", lambda handle: 0)
    def cancelled(*args):
        raise OperationCancelled("cancelled")
    monkeypatch.setattr(parser, "get_transitions", cancelled)
    with pytest.raises(OperationCancelled):
        parser.get_header()
    assert parser._header is None


@pytest.mark.parametrize("method", ["header", "summary", "width", "resolve", "exists"])
def test_cancelled_cache_hit_propagates(parser, method):
    parser.get_summary()
    parser.get_signal_width("s")
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            {"header": parser.get_header, "summary": parser.get_summary,
             "width": lambda: parser.get_signal_width("s"),
             "exists": lambda: _exists(parser, "s"),
             "resolve": lambda: _resolve_signal_path(parser, "s")}[method]()
    finally:
        pop_cancel_event(token)


def test_cancel_during_native_lookup_does_not_cache_or_fallback(parser, monkeypatch):
    event = threading.Event()
    original = parser._lib.fsdb_get_signal_metadata_v1
    def cancel(*args):
        result = original(*args)
        event.set()
        return result
    monkeypatch.setattr(parser._lib, "fsdb_get_signal_metadata_v1", cancel)
    monkeypatch.setattr(parser, "search_signals", lambda *a, **k: pytest.fail("cancel fallback"))
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            parser.get_signal_width("s")
        assert not parser._metadata_cache
    finally:
        pop_cancel_event(token)


def test_native_metadata_oracles(tmp_path):
    """Run the actual C ABI against synthetic map entries without simulation."""
    verdi = os.environ.get("VERDI_HOME")
    if not verdi or not shutil.which("g++"):
        pytest.skip("requires the local Verdi reader SDK and g++")
    root = Path(__file__).resolve().parents[1]
    sdk = Path(verdi) / "share/FsdbReader"
    exe = tmp_path / "metadata"
    subprocess.run(["g++", "-std=c++11", "-I" + str(root), "-I" + str(sdk),
                    str(root / "tests/native/fsdb_metadata_test.cpp"), "-o", str(exe),
                    "-L" + str(sdk / "linux64"), "-lnffr", "-lnsys", "-lz", "-ldl",
                    "-Wl,-rpath," + str(sdk / "linux64")], check=True, capture_output=True)
    subprocess.run([str(exe)], check=True, timeout=10)


@pytest.mark.parametrize("version,missing,expected", [(None, False, False), (2, False, False),
                                                      (1, True, False), (1, False, True)])
def test_optional_abi_probe(version, missing, expected):
    class Function:
        def __call__(self):
            return version
    class Library:
        def __init__(self):
            self.functions = {}
        def __getattr__(self, name):
            if (name == "fsdb_metadata_version" and version is None or
                    name == "fsdb_get_summary_paths_v1" and missing):
                raise AttributeError(name)
            return self.functions.setdefault(name, Function())
    lib = Library()
    fs._setup(lib)
    assert lib._traceweave_has_metadata_v1 is expected


@pytest.mark.parametrize("fixture", ["scale_100fs.fsdb", "scale_1ns.fsdb", "wide_bus.fsdb"])
def test_real_metadata_matches_search(fixture):
    path = Path(__file__).parent / "fixtures" / fixture
    p = fs.FSDBParser(str(path))
    try:
        try:
            p._open()
        except RuntimeError as exc:
            pytest.skip(str(exc))
        assert p._lib._traceweave_has_metadata_v1, "rebuild this checkout's wrapper"
        records = p.search_signals("", max_results=10000)
        assert not records["truncated"]
        for item in records["results"]:
            assert p.get_signal_width(item["path"]) == item["width"]
            assert p._metadata_cache[item["path"]][0] == (
                item["width"], item["direction_code"], item["var_type_code"])
    finally:
        p.close()


def test_real_fsdb_replacement_reopens_native_header_and_index(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    wave, replacement = tmp_path / "wave.fsdb", tmp_path / "replacement.fsdb"
    shutil.copyfile(fixtures / "scale_100fs.fsdb", wave)
    p = fs.FSDBParser(str(wave))
    try:
        try:
            old = p.get_summary()
        except RuntimeError as exc:
            pytest.skip(str(exc))
        assert old["scale_fs_per_tick"] == 100
        path = old["sample_signals"][0]
        p.get_signal_width(path)
        assert p._metadata_cache
        shutil.copyfile(fixtures / "scale_1ns.fsdb", replacement)
        replacement.replace(wave)
        new = p.get_summary()
        assert new["scale_fs_per_tick"] == 1_000_000
        assert new["scale_unit"] == "1ns"
        assert not p._metadata_cache
        assert new["sample_signals"] != old["sample_signals"]
        assert p.get_signal_width(new["sample_signals"][0]) > 0
    finally:
        p.close()
