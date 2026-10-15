"""Independent cache behavior oracle using the existing raw C-ABI fake."""

import threading
import pytest
from src import fsdb_parser as f
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from tests.test_fsdb_capped_transitions import _PagedLib, _fake_parser


def parser(rows=None, scale_fs=1000):
    lib = _PagedLib(rows, scale_fs=scale_fs)
    p = _fake_parser(lib)
    identity = ("fixture", 1, 2, 3, 4, 5)
    p._file_identity = identity
    p._stat_identity = lambda: identity
    return p, lib


def query(p, start=0, end=-1, cap=2):
    return p.get_transitions_capped("top.data", start, end, max_transitions=cap)


def test_exact_hit_cap_independence_and_window_miss():
    p, lib = parser()
    try:
        a = query(p)
        n = lib.calls.count("count")
        assert n > 0
        b = query(p)
        assert a == b and lib.calls.count("count") == n
        c = query(p, cap=4)
        assert c["transition_count"] == 20 and c["transitions"][:2] == a["transitions"]
        assert lib.calls.count("count") == n
        d = query(p, start=5, end=15)
        assert d["transition_count"] == 11 and d["predecessor"]["time_ps"] == 4
        assert lib.calls.count("count") > n and len(p._transition_count_cache) == 2
        a["transitions"].clear()
        assert len(query(p)["transitions"]) == 2
    finally:
        p.close()


def test_lru_entry_and_accounted_byte_limits(monkeypatch):
    monkeypatch.setattr(f, "_TRANSITION_COUNT_MAX_ENTRIES", 2)
    p, lib = parser()
    try:
        query(p, 0, 10)
        query(p, 1, 11)
        query(p, 0, 10)
        query(p, 2, 12)
        assert len(p._transition_count_cache) == 2
        before = lib.calls.count("count")
        query(p, 1, 11)
        assert lib.calls.count("count") > before
        assert p._transition_count_cache_bytes <= f._TRANSITION_COUNT_MAX_BYTES
    finally:
        p.close()
    monkeypatch.setattr(f, "_TRANSITION_COUNT_MAX_BYTES", 1)
    p, lib = parser()
    try:
        query(p)
        query(p)
        assert (
            len(p._transition_count_cache) == 0 and p._transition_count_cache_bytes == 0
        )
    finally:
        p.close()


def test_close_reopen_and_file_identity_invalidate():
    p, lib = parser()
    try:
        query(p)
        assert p._transition_count_cache
        p.close()
        assert not p._transition_count_cache and p._transition_count_cache_bytes == 0
        query(p)
        n = lib.calls.count("count")
        p._stat_identity = lambda: ("replaced", 5, 4, 3, 2, 1)
        query(p)
        assert lib.calls.count("count") > n
    finally:
        p.close()


@pytest.mark.parametrize(
    "fault", ["cancel", "partial", "native_error", "cleanup_error", "identity"]
)
def test_incomplete_cancel_or_failed_cleanup_never_seed(fault, monkeypatch):
    p, lib = parser()
    event = threading.Event()
    token = push_cancel_event(event)
    if fault == "cancel":
        lib.after_count = event.set
    if fault == "native_error":
        lib.count_rc = -3
    if fault == "partial":
        original = lib.fsdb_event_count_v1

        def partial(*args):
            rc = original(*args)
            args[-2]._obj.truncated = 1
            return rc

        monkeypatch.setattr(lib, "fsdb_event_count_v1", partial)
    if fault in ("cleanup_error", "identity"):
        original = lib.fsdb_end_transition_group

        def end(*args):
            rc = original(*args)
            if fault == "identity":
                p._stat_identity = lambda: ("changed",)
            return -6 if fault == "cleanup_error" else rc

        monkeypatch.setattr(lib, "fsdb_end_transition_group", end)
    try:
        if fault == "partial":
            assert query(p)["transition_count_is_lower_bound"]
        else:
            with pytest.raises(
                OperationCancelled if fault == "cancel" else RuntimeError
            ):
                query(p)
        assert not p._transition_count_cache and p._transition_count_cache_bytes == 0
        assert not p._transition_group_active and not lib.cursors
    finally:
        event.clear()
        pop_cancel_event(token)
        p.close()


@pytest.mark.parametrize(
    "missing", ["event_count_v1", "event_pages_v1", "lazy_transition_group"]
)
def test_legacy_fallback_never_seeds(missing):
    p, lib = parser()
    setattr(lib, "_traceweave_has_" + missing, False)
    try:
        a = query(p)
        b = query(p)
        assert a == b and not p._transition_count_cache
        assert lib.calls.count("legacy") == 2 and "count" not in lib.calls
    finally:
        p.close()


def test_same_tick_subps_closed_window_and_predecessor_hits():
    raw = [
        (0, "0"),
        (1, "1"),
        (2, "x"),
        (9, "z"),
        (10, "0"),
        (10, "1"),
        (10, "x"),
        (10, "0"),
        (11, "1"),
        (20, "z"),
        (29, "0"),
        (30, "1"),
    ]
    p, lib = parser(raw, 100)
    try:
        for start, end in [(0, 0), (0, 1), (1, 1), (1, 2), (2, 3), (4, 5)]:
            a = query(p, start, end, 1)
            n = lib.calls.count("count")
            b = query(p, start, end, 2)
            values = [
                ((tick + 9) // 10, v)
                for tick, v in raw
                if start <= (tick + 9) // 10 <= end and tick <= end * 10
            ]
            # Physical end and public ceil timestamps must both obey old behavior.
            assert a["transition_count"] == b["transition_count"] == len(values)
            assert b["transitions"][:1] == a["transitions"]
            assert a["predecessor"] == b["predecessor"]
            assert lib.calls.count("count") == n
    finally:
        p.close()


@pytest.mark.parametrize("phase", ["prefix", "group_end"])
def test_cache_extra_hit_cancellation(phase, monkeypatch):
    p, lib = parser()
    query(p)
    event = threading.Event()
    token = push_cancel_event(event)
    name = "fsdb_event_page_v1" if phase == "prefix" else "fsdb_end_transition_group"
    original = getattr(lib, name)

    def cancel_after(*args):
        rc = original(*args)
        event.set()
        return rc

    monkeypatch.setattr(lib, name, cancel_after)
    try:
        with pytest.raises(OperationCancelled):
            query(p)
        assert not lib.cursors and not p._transition_group_active
    finally:
        event.clear()
        pop_cancel_event(token)
        p.close()


@pytest.mark.parametrize("field", [2, 3, 4, 5], ids=["inode", "size", "mtime", "ctime"])
def test_cache_extra_identity_fields_force_rescan(field):
    p, lib = parser()
    try:
        query(p)
        n = lib.calls.count("count")
        identity = list(p._file_identity)
        identity[field] += 100
        p._stat_identity = lambda: tuple(identity)
        query(p)
        assert lib.calls.count("count") > n
    finally:
        p.close()


def test_cache_extra_byte_budget_eviction(monkeypatch):
    p, lib = parser()
    try:
        query(p, 0, 10)
        size = next(iter(p._transition_count_cache.values()))[1]
        monkeypatch.setattr(f, "_TRANSITION_COUNT_MAX_BYTES", size + 16)
        query(p, 1, 11)
        assert len(p._transition_count_cache) == 1
        assert p._transition_count_cache_bytes <= size + 16
        before = lib.calls.count("count")
        query(p, 0, 10)
        assert lib.calls.count("count") > before and len(p._transition_count_cache) == 1
    finally:
        p.close()


def test_cache_extra_cap_larger_than_cached_total():
    p, lib = parser()
    try:
        query(p)
        n = lib.calls.count("count")
        a = query(p, cap=100)
        assert (
            a["transition_count"] == 20
            and len(a["transitions"]) == 20
            and not a["truncated"]
        )
        assert [x["time_ps"] for x in a["transitions"]] == list(range(20))
        assert lib.calls.count("count") == n
    finally:
        p.close()
