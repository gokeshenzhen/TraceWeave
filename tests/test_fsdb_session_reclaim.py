"""Session-timing policy and its real-reader wiring, without public test ABI."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from tests.test_fsdb_cursor_reclaim import (
    _Cursor, _assert_clean, _expected, _parser, _snapshot, _take,
    reclaim_libraries, same_tick_fsdb,  # noqa: F401
)


@pytest.fixture(scope="module")
def session_policy_probe(tmp_path_factory):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("session_policy") / "probe"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(root / "native/fsdb"),
                    str(root / "tests/native/fsdb_session_reclaim_test.cpp"),
                    "-o", str(binary)], check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("mode", ["two_session", "valid", "zero_prefix", "carry", "missing", "single",
    "single_zero", "gap", "overlap", "reverse", "first", "last", "beyond",
    "zero_middle", "bounds", "throws", "type", "cycle", "limit"])
def test_session_metadata_and_raw_boundaries(session_policy_probe, mode):
    subprocess.run([str(session_policy_probe), mode], check=True, timeout=10)


def _plan(parser, mode=0):
    function = parser._lib.test_session_plan
    function.argtypes, function.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_int
    return function(parser._handle, mode)


@pytest.mark.eda_integration
@pytest.mark.parametrize("count", [False, True])
def test_defer_preserves_raw_ordinals_then_reclaims_across_close(
        reclaim_libraries, same_tick_fsdb, monkeypatch, count):
    with _parser(reclaim_libraries("records1"), same_tick_fsdb, monkeypatch) as parser:
        with parser._event_transition_group(["top.data"], 0, 4):
            assert _plan(parser) == 1
            with _Cursor(parser) as cursor:
                remaining = _expected()
                # The first two metadata sessions end at raw 1 and raw 10.
                # All ordinals at their second closed endpoint remain deferred.
                for _ in range(7):
                    remaining = _take(cursor, remaining, 1, count=count)
                    assert _snapshot(parser)["unloads"] == 0
                remaining = _take(cursor, remaining, 1, count=count)
                assert _snapshot(parser)["unloads"] == 1  # Next raw tag is 11.
                while remaining:
                    remaining = _take(cursor, remaining, 1, count=not count)
                assert _snapshot(parser)["unloads"] == 2  # EOF uses old cleanup.
        _assert_clean(parser)


@pytest.mark.eda_integration
def test_cancellation_while_reclaim_is_deferred_releases_group(
        reclaim_libraries, same_tick_fsdb, monkeypatch):
    with _parser(reclaim_libraries("records1"), same_tick_fsdb, monkeypatch) as parser:
        cancel = threading.Event()
        token = push_cancel_event(cancel)
        try:
            with pytest.raises(OperationCancelled):
                with parser._event_transition_group(["top.data"], 0, 4):
                    assert _plan(parser) == 1
                    with parser._event_pages("top.data", 0, 4, max_events=1) as cursor:
                        cursor.read_page()
                        assert _snapshot(parser)["unloads"] == 0
                        cancel.set()
                        cursor.read_page()
            cancel.clear()
            _assert_clean(parser)
        finally:
            pop_cancel_event(token)


@pytest.mark.eda_integration
def test_real_multisession_wide_counter_count_and_values(
        reclaim_libraries, monkeypatch):
    wave = os.environ.get("TW_SESSION_RECLAIM_FSDB")
    if not wave:
        pytest.skip("set TW_SESSION_RECLAIM_FSDB to the fixed wide_256_reclaim.fsdb")
    path = "top.data[255:0]"
    with _parser(reclaim_libraries("default"), wave, monkeypatch) as parser:
        with parser._event_transition_group([path], 0, -1):
            with _Cursor(parser, path, end=-1) as cursor:
                total, checked_after_boundary = 0, False
                while total < 600001:
                    values = total == 0 or (total > 500733 and not checked_after_boundary)
                    before = _snapshot(parser)["get_vc"]
                    receipt, rows = cursor.read(8 if values else 65536, count=not values)
                    if values:
                        for offset, (kind, ps, tick, value) in enumerate(rows):
                            index = total + offset
                            expected = "x" * 256 if index == 5 else "z" * 256 if index == 6 else (
                                f"{index * 2654435761 & 0xffffffff:064b}" * 4)
                            assert (kind, ps, tick, value) == ("T", index * 10, index * 10, expected)
                        checked_after_boundary |= total > 500733
                    else:
                        assert _snapshot(parser)["get_vc"] == before
                    total += receipt.events
                    if total < 600001:
                        assert receipt.next_tick == total * 10
                    # The old byte threshold is crossed inside the first
                    # physical session; only crossing its close may reclaim.
                    if 262144 <= total <= 500733:
                        assert _snapshot(parser)["unloads"] == 0
                assert receipt.complete and total == 600001 and checked_after_boundary
                assert _snapshot(parser)["unloads"] == 1
        _assert_clean(parser, (path, "top.clk", "top.sparse"))


@pytest.mark.eda_integration
@pytest.mark.parametrize("mode", [1, 2, 3, 4])
def test_missing_invalid_and_single_session_metadata_keep_old_budget(
        reclaim_libraries, same_tick_fsdb, monkeypatch, mode):
    with _parser(reclaim_libraries("records1"), same_tick_fsdb, monkeypatch) as parser:
        with parser._event_transition_group(["top.data"], 0, 4):
            assert _plan(parser, mode) == 0
            with _Cursor(parser) as cursor:
                _take(cursor, _expected(), 1)
                assert _snapshot(parser)["unloads"] == 1
        _assert_clean(parser)


@pytest.mark.eda_integration
@pytest.mark.parametrize("unsafe", ["alias", "second_cursor", "multiple_ids"])
def test_unsafe_ownership_uses_old_budget_even_with_valid_metadata(
        reclaim_libraries, same_tick_fsdb, monkeypatch, unsafe):
    with _parser(reclaim_libraries("records1"), same_tick_fsdb, monkeypatch) as parser:
        paths = ["top.data", "top.sparse"] if unsafe == "multiple_ids" else ["top.data"]
        with parser._event_transition_group(paths, 0, 4):
            assert _plan(parser, 5 if unsafe == "alias" else 0) == 1
            with _Cursor(parser) as first:
                if unsafe == "second_cursor":
                    with _Cursor(parser):
                        pass  # The group's conservative mode survives close.
                _take(first, _expected(), 1)
                assert _snapshot(parser)["unloads"] == 1
        _assert_clean(parser)
