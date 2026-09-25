"""Scope paging boundaries, lifecycle, bounded work and cancellation oracles."""
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from src import fsdb_parser as fs, handshake_suggest as suggest
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.scope_metadata import ScopeIdentityChanged
from src.vcd_parser import VCDParser


def vcd(tmp_path, scopes=None):
    lines = ["$timescale 1ps $end", "$scope module tb $end", "$var wire 1 ! clk $end"]
    for number, (scope, names) in enumerate(scopes or [("a", ["valid_i", "ready_o", "data_i"]),
                                                    ("a0", ["valid", "ready"]), ("A", ["valid", "ready"])]):
        lines.append(f"$scope module {scope} $end")
        for index, name in enumerate(names):
            lines.append(f"$var wire {8 if 'data' in name else 1} s{number}_{index} {name} $end")
        lines.append("$upscope $end")
    lines += ["$upscope $end", "$enddefinitions $end", "#0", "0!", "#5", "1!", "#10", "0!"]
    path = tmp_path / "scope.vcd"
    path.write_text("\n".join(lines))
    return VCDParser(str(path))


def all_pages(parser, scope=None, **kwargs):
    cursor, rows, pages = None, [], []
    for _ in range(100):
        page = parser.enumerate_scope_page(scope, cursor=cursor, **kwargs)
        rows += page["results"]
        pages.append(page)
        if page["complete"]:
            return rows, pages
        assert page["cursor"] is not None and page["cursor"] != cursor
        cursor = page["cursor"]
    pytest.fail("pagination did not terminate")


def test_vcd_subtree_pages_are_exact_case_sensitive_and_stable(tmp_path):
    parser = vcd(tmp_path)
    rows, pages = all_pages(parser, "tb.a", max_items=1)
    assert [r["path"] for r in rows] == ["tb.a.data_i", "tb.a.ready_o", "tb.a.valid_i"]
    assert len(pages) == 3 and sum(p["visited"] for p in pages) == 3
    assert rows[0]["scope"] == "tb.a" and rows[0]["width"] == 8
    assert parser.enumerate_scope_page("tb.A")["results"][0]["path"] == "tb.A.ready"
    assert parser.enumerate_scope_page("tb.absent")["complete"]
    assert [r["path"] for r in parser.enumerate_scope_page("tb", direct=True)["results"]] == ["tb.clk"]


def test_vcd_escaped_names_and_generated_indices(tmp_path):
    parser = vcd(tmp_path, [(r"\a.b", [r"\odd.name", "valid[0]", "ready[0]"]), ("gen[2]", ["valid", "ready"])])
    rows, _ = all_pages(parser, r"tb.\a.b", max_items=1)
    assert len(rows) == 3 and rows[0]["name"] == r"\odd.name"
    assert not parser.enumerate_scope_page(r"tb.\a")["results"]
    assert len(parser.enumerate_scope_page("tb.gen[2]")["results"]) == 2


def test_escaped_scope_ending_in_dot_is_not_normalized_away(tmp_path):
    parser = vcd(tmp_path, [(r"\tail.", ["valid", "ready"])])
    assert len(parser.enumerate_scope_page(r"tb.\tail.")["results"]) == 2


def test_vcd_separate_bits_slices_and_aliases_keep_declaration_identity(tmp_path):
    path = tmp_path / "bits.vcd"
    path.write_text("""$timescale 1ps $end
$scope module tb $end
$var wire 1 a valid [0] $end
$var wire 1 b valid [1] $end
$var wire 1 c ready [0] $end
$var wire 1 d ready [1] $end
$var wire 4 e data [7:4] $end
$var wire 4 f data [3:0] $end
$var wire 1 a alias $end
$upscope $end
$enddefinitions $end
""")
    rows, _ = all_pages(VCDParser(str(path)), max_items=1)
    assert {r["path"] for r in rows} == {"tb.alias", "tb.valid[0]", "tb.valid[1]", "tb.ready[0]", "tb.ready[1]", "tb.data[7:4]", "tb.data[3:0]"}


@pytest.mark.parametrize("change", ["write", "replace", "remove", "symlink"])
def test_vcd_stale_page_is_rejected(tmp_path, change):
    parser = vcd(tmp_path)
    path = Path(parser.file_path)
    cursor = parser.enumerate_scope_page(max_items=1)["cursor"]
    if change == "write":
        path.write_text(path.read_text() + "\n#20")
    elif change == "remove":
        path.unlink()
    else:
        other = tmp_path / "other.vcd"
        shutil.copyfile(path, other)
        if change == "replace":
            other.replace(path)
        else:
            path.unlink()
            path.symlink_to(other)
    with pytest.raises(ScopeIdentityChanged):
        parser.enumerate_scope_page(cursor=cursor)


def test_vcd_byte_scan_item_limits_and_cancel(tmp_path):
    parser = vcd(tmp_path)
    page = parser.enumerate_scope_page("tb.a", max_bytes=1)
    assert page["results"] == [] and page["stop_reason"] == "page_bytes" and not page["complete"]
    assert all_pages(parser, "tb.a", max_bytes=40)[0]
    page = parser.enumerate_scope_page(max_visited=1)
    assert len(page["results"]) == page["visited"] == 1 and not page["complete"]
    with pytest.raises(ValueError):
        parser.enumerate_scope_page("tb.a", cursor=page["cursor"])
    with pytest.raises(ValueError):
        parser.enumerate_scope_page(max_items=0)
    event = threading.Event(); event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            parser.enumerate_scope_page(cursor=page["cursor"])
    finally:
        pop_cancel_event(token)


def test_native_scope_oracle(tmp_path):
    verdi = os.environ.get("VERDI_HOME")
    if not verdi or not shutil.which("g++"):
        pytest.skip("requires local Verdi reader SDK and g++")
    root = Path(__file__).resolve().parents[1]
    sdk = Path(verdi) / "share/FsdbReader"
    exe = tmp_path / "scope"
    subprocess.run(["g++", "-std=c++11", "-I" + str(root), "-I" + str(sdk),
                    str(root / "tests/native/fsdb_scope_page_test.cpp"), "-o", str(exe),
                    "-L" + str(sdk / "linux64"), "-lnffr", "-lnsys", "-lz", "-ldl",
                    "-Wl,-rpath," + str(sdk / "linux64")], check=True, capture_output=True)
    subprocess.run([str(exe)], check=True, timeout=10)


@pytest.mark.parametrize("fixture", ["scale_100fs.fsdb", "scale_1ns.fsdb", "wide_bus.fsdb"])
def test_real_fsdb_pages_match_metadata_and_legacy_search(fixture, require_fsdb_runtime):
    parser = fs.FSDBParser(str(Path(__file__).parent / "fixtures" / fixture))
    try:
        expected = parser.search_signals("", max_results=10000)
        assert not expected["truncated"]
        rows, pages = all_pages(parser, max_items=2)
        fields = ("path", "width", "direction", "var_type")
        canonical = lambda values: sorted(tuple(r[k] for k in fields) for r in values)
        assert canonical(rows) == canonical(expected["results"])
        assert all(p["mode"] == "native_scope_v1" for p in pages)
        assert parser._metadata_cache == {}  # paging does not populate an unbounded cache
    finally:
        parser.close()


@pytest.mark.parametrize("change", ["close", "replace", "write", "remove", "symlink"])
def test_real_fsdb_stale_cursor_rejected_without_continuing(tmp_path, change, require_fsdb_runtime):
    path = tmp_path / "wave.fsdb"
    shutil.copyfile(Path(__file__).parent / "fixtures/scale_100fs.fsdb", path)
    parser = fs.FSDBParser(str(path))
    try:
        page = parser.enumerate_scope_page(max_items=1)
        assert page["cursor"] is not None
        if change == "close":
            parser.close()
        elif change == "remove":
            path.unlink()
        elif change == "write":
            with path.open("ab") as stream:
                stream.write(b"\0")
        else:
            other = tmp_path / "replacement.fsdb"
            shutil.copyfile(Path(__file__).parent / "fixtures/scale_1ns.fsdb", other)
            if change == "replace":
                other.replace(path)
            else:
                path.unlink(); path.symlink_to(other)
        with pytest.raises(ScopeIdentityChanged):
            parser.enumerate_scope_page(cursor=page["cursor"])
    finally:
        parser.close()


@pytest.mark.parametrize("budget,limit,reason", [("DISCOVERY_SIGNAL_LIMIT", 2, "signal_limit"),
                                                ("DISCOVERY_BYTE_LIMIT", 1, "byte_limit"),
                                                ("DISCOVERY_SCAN_LIMIT", 1, "scan_limit"),
                                                ("DISCOVERY_MEMORY_LIMIT", 1, "memory_limit")])
def test_discovery_caps_are_partial_with_lower_bounds(tmp_path, monkeypatch, budget, limit, reason):
    parser = vcd(tmp_path)
    monkeypatch.setattr(suggest, budget, limit)
    result = suggest.suggest_handshakes(get_parser=lambda _: parser, wave_path=parser.file_path)
    receipt = result["discovery"]
    assert receipt["status"] == "partial" and reason in receipt["reasons"]
    assert receipt["scope_total"] is None
    assert receipt["scope_total_lower_bound"] == receipt["scope_signals_returned"]


def test_scan_limit_does_not_count_escaped_prefix_as_scope_member(tmp_path, monkeypatch):
    parser = vcd(tmp_path, [(r"\a.b", ["valid", "ready"])])
    monkeypatch.setattr(suggest, "DISCOVERY_SCAN_LIMIT", 1)
    rows, receipt = suggest._gather_scope(parser, r"tb.\a")
    assert rows == []
    assert receipt["status"] == "partial" and receipt["scope_total"] is None
    assert receipt["scope_total_lower_bound"] == 0


def test_pair_can_span_pages_and_ancestor_clock_is_independent(tmp_path, monkeypatch):
    parser = vcd(tmp_path)
    monkeypatch.setattr(suggest, "PAGE_ITEMS", 1)
    result = suggest.suggest_handshakes(get_parser=lambda _: parser, wave_path=parser.file_path, scope="tb.a")
    assert result["candidate_count"] == 1
    b = result["candidates"][0]
    assert b["clock"] == "tb.clk" and b["payload"] == ["tb.a.data_i"]
    r = result["discovery"]
    assert r["status"] == "complete" and r["scope_total"] == 3
    assert r["scope_total_lower_bound"] == 3 and r["ancestor_signals_returned"] == 1
    assert r["pages_read"] >= 4


def test_snapshot_is_read_only_and_rejects_identity_change(tmp_path):
    parser = vcd(tmp_path)
    snapshot = suggest.DiscoverySnapshot()
    rows, receipt = snapshot.get(parser, parser.file_path, None)
    with pytest.raises(TypeError):
        rows[0]["path"] = "bad"
    receipt["reasons"].append("injected")
    assert snapshot.get(parser, parser.file_path, None)[1]["reasons"] == []
    Path(parser.file_path).write_text("changed")
    with pytest.raises(ScopeIdentityChanged):
        snapshot.get(parser, parser.file_path, None)


def test_discovery_cancellation_between_pages_has_no_fallback(tmp_path, monkeypatch):
    parser = vcd(tmp_path)
    real = parser.enumerate_scope_page
    event = threading.Event()
    calls = []
    def cancel(*args, **kwargs):
        calls.append(1)
        result = real(*args, **kwargs)
        event.set()
        return result
    monkeypatch.setattr(parser, "enumerate_scope_page", cancel)
    monkeypatch.setattr(parser, "search_signals", lambda *a, **k: pytest.fail("cancel fallback"))
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            suggest.suggest_handshakes(get_parser=lambda _: parser, wave_path=parser.file_path)
    finally:
        pop_cancel_event(token)
    assert len(calls) == 1


def test_identity_change_between_pages_discards_the_prefix(tmp_path, monkeypatch):
    parser = vcd(tmp_path)
    original = parser.enumerate_scope_page
    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        Path(parser.file_path).write_text("changed")
        return result
    monkeypatch.setattr(parser, "enumerate_scope_page", change)
    monkeypatch.setattr(suggest, "PAGE_ITEMS", 1)
    rows, receipt = suggest._gather_scope(parser, None)
    assert rows == [] and receipt["signals_returned"] == 0
    assert receipt["status"] == "partial" and receipt["reasons"] == ["index_identity_changed"]
    assert receipt["scope_total"] is None and receipt["scope_total_lower_bound"] == 0


def test_native_cancel_and_change_during_page_are_not_published(tmp_path, monkeypatch, require_fsdb_runtime):
    path = tmp_path / "wave.fsdb"
    shutil.copyfile(Path(__file__).parent / "fixtures/scale_100fs.fsdb", path)
    parser = fs.FSDBParser(str(path))
    event = threading.Event()
    try:
        parser._open()
        original = parser._lib.fsdb_scope_page_v1
        def cancel(*args):
            result = original(*args)
            event.set()
            return result
        monkeypatch.setattr(parser._lib, "fsdb_scope_page_v1", cancel)
        token = push_cancel_event(event)
        try:
            with pytest.raises(OperationCancelled):
                parser.enumerate_scope_page()
        finally:
            pop_cancel_event(token)
        def change(*args):
            result = original(*args)
            with path.open("ab") as stream:
                stream.write(b"\0")
            return result
        monkeypatch.setattr(parser._lib, "fsdb_scope_page_v1", change)
        with pytest.raises(ScopeIdentityChanged):
            parser.enumerate_scope_page()
        assert parser._handle is None
    finally:
        parser.close()


def test_scope_change_inside_active_group_preserves_cleanup(tmp_path, require_fsdb_runtime):
    path = tmp_path / "wave.fsdb"
    shutil.copyfile(Path(__file__).parent / "fixtures/scale_100fs.fsdb", path)
    parser = fs.FSDBParser(str(path))
    try:
        page = parser.enumerate_scope_page(max_items=1)
        signal = page["results"][0]["path"]
        with parser.transition_group([signal]):
            with path.open("ab") as stream:
                stream.write(b"\0")
            with pytest.raises(ScopeIdentityChanged):
                parser.enumerate_scope_page(cursor=page["cursor"])
            assert parser._transition_group_active
        assert not parser._transition_group_active
    finally:
        parser.close()
