import ctypes
import threading
from bisect import bisect_left
import pytest
from src import fsdb_parser as f
from src import fsdb_count_index as idx
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from tests.test_fsdb_capped_transitions import _PagedLib, _fake_parser, _int
from tests.fsdb_count_oracles import public_records, SAME_TICK_100FS


class IndexedLib(_PagedLib):
    _traceweave_has_event_count_index_v1 = True

    def __init__(self, *args, page_cap=None, **kw):
        super().__init__(*args, **kw)
        self.page_cap = page_cap
        self.index_calls = 0

    def fsdb_event_count_index_v1(
        self, handle, cursor, limit, receipt, receipt_size, boundary, boundary_size
    ):
        self.index_calls += 1
        self.calls.append("index_count")
        before = self.cursors[_int(cursor)][0]
        rc = self._read(cursor, min(_int(limit), self.page_cap or 65536), receipt)
        after = self.cursors[_int(cursor)][0]
        selected = self.rows[before:after]
        observed = [(t * self.scale_fs + 999) // 1000 for t, _ in selected]
        if receipt._obj.has_next:
            observed.append((self.rows[after][0] * self.scale_fs + 999) // 1000)
        boundary._obj.valid = 0
        if observed:
            first = observed[0]
            for time in observed[1:]:
                if time > first:
                    boundary._obj.valid = 1
                    boundary._obj.boundary_ps = time
                    boundary._obj.events_before_boundary = sum(
                        (t * self.scale_fs + 999) // 1000 < time for t, _ in selected
                    )
                    break
        if self.after_count:
            self.after_count()
        return self.count_rc or rc


def parser(rows=None, scale=1000, page_cap=None):
    lib = IndexedLib(
        rows if rows is not None else [(i * 10, str(i % 2)) for i in range(200000)],
        scale_fs=scale,
        page_cap=page_cap,
    )
    p = _fake_parser(lib)
    identity = ("fixture", 1, 2, 3, 4, 5)
    p._file_identity = identity
    p._stat_identity = lambda: identity
    return p, lib


def query(p, start=0, end=-1, cap=2):
    return p.get_transitions_capped("top.data", start, end, max_transitions=cap)


def check(value, rows, scale, start, end, cap):
    expected = public_records(rows, scale, start, end, cap)
    assert value["transition_count"] == expected["count"]
    assert [(x["time_ps"], x["value"]["bin"]) for x in value["transitions"]] == [
        (x["time_ps"], x["value"]) for x in expected["rows"]
    ]
    predecessor = value["predecessor"]
    expected_pred = expected["predecessor"]
    assert (
        None
        if predecessor is None
        else (predecessor["time_ps"], predecessor["value"]["bin"])
    ) == (
        None
        if expected_pred is None
        else (expected_pred["time_ps"], expected_pred["value"])
    )
    assert (
        not value["transition_count_is_lower_bound"]
        and value["truncated"] == expected["truncated"]
    )


def test_build_same_window_and_large_contained_window():
    p, lib = parser()
    try:
        a = query(p)
        check(a, lib.rows, 1000, 0, -1, 2)
        assert p._transition_index_stats["builds"] == 1
        n = lib.index_calls
        b = query(p)
        assert a == b and lib.index_calls == n
        c = query(p, 450003, 1500007)
        check(c, lib.rows, 1000, 450003, 1500007, 2)
        assert (
            p._transition_index_stats["hits"] == 1
            and p._transition_index_stats["interior_records"] > 0
        )
        assert (
            p._transition_index_stats["edge_queries"] > 0
            and not p._transition_group_active
            and not lib.cursors
        )
    finally:
        p.close()


@pytest.mark.parametrize("scale", [100, 1000])
def test_same_public_time_and_raw_tick_groups_across_pages(scale):
    raw = [(t, v) for t, v in SAME_TICK_100FS] * 1
    # Extend enough public groups to permit an actual nonempty interior.
    raw += [(40 + i, "1" if i % 2 else "0") for i in range(100)]
    p, lib = parser(raw, scale, page_cap=1)
    try:
        query(p, cap=1)
        for start, end, cap in [
            (1, 2, 1),
            (1, 3, 2),
            (2, 8, 1),
            (3, 12, 2),
            (4, 10, 1),
            (5, 11, 1000),
        ]:
            value = query(p, start, end, cap)
            check(value, raw, scale, start, end, cap)
        assert p._transition_index_stats["hits"] > 0
    finally:
        p.close()


def test_uncovered_and_expanded_windows_scan_instead_of_joining_partial_coverage():
    p, lib = parser()
    try:
        query(p, 1000, 80000)
        before = lib.index_calls
        hits = p._transition_index_stats["hits"]
        value = query(p, 70000, 150000)
        check(value, lib.rows, 1000, 70000, 150000, 2)
        assert lib.index_calls > before and p._transition_index_stats["hits"] == hits
        value = query(p, 0, -1)
        check(value, lib.rows, 1000, 0, -1, 2)
    finally:
        p.close()


def test_index_entry_and_byte_eviction_and_capacity_fallback(monkeypatch):
    monkeypatch.setattr(idx, "MAX_ENTRIES", 1)
    p, lib = parser()
    try:
        query(p, 0, 80000)
        query(p, 100000, 190000)
        assert len(p._transition_indexes) == 1
        assert p._transition_index_bytes <= idx.MAX_BYTES
    finally:
        p.close()
    monkeypatch.setattr(idx, "MAX_BYTES", 1)
    p, lib = parser()
    try:
        value = query(p)
        check(value, lib.rows, 1000, 0, -1, 2)
        assert not p._transition_indexes and p._transition_index_bytes == 0
    finally:
        p.close()


def test_coarsening_preserves_counts_and_bounds(monkeypatch):
    monkeypatch.setattr(idx, "MAX_POINTS", 4)
    p, lib = parser(page_cap=700)
    try:
        query(p)
        assert p._transition_index_stats["coarsenings"] > 0
        for index, _ in p._transition_indexes.values():
            assert len(index.times) <= 4
        value = query(p, 30003, 180007)
        check(value, lib.rows, 1000, 30003, 180007, 2)
    finally:
        p.close()


@pytest.mark.parametrize(
    "fault", ["cancel", "native_error", "partial", "cleanup_error", "identity"]
)
def test_failed_build_never_publishes_index(fault, monkeypatch):
    p, lib = parser()
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    if fault == "cancel":
        lib.after_count = cancel.set
    if fault == "native_error":
        lib.count_rc = -3
    if fault == "partial":
        original = lib.fsdb_event_count_index_v1

        def partial(*a):
            rc = original(*a)
            a[3]._obj.truncated = 1
            return rc

        monkeypatch.setattr(lib, "fsdb_event_count_index_v1", partial)
    if fault in ["cleanup_error", "identity"]:
        original = lib.fsdb_end_transition_group

        def end(*a):
            rc = original(*a)
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
        assert not p._transition_indexes and not p._transition_count_cache
        assert not p._transition_group_active and not lib.cursors
    finally:
        cancel.clear()
        pop_cancel_event(token)
        p.close()


def test_optional_native_abi_absence_preserves_count_cache_and_scans():
    p, lib = parser()
    lib._traceweave_has_event_count_index_v1 = False
    try:
        a = query(p)
        n = lib.calls.count("count")
        assert n > 0 and lib.index_calls == 0
        assert query(p) == a and lib.calls.count("count") == n
        assert not p._transition_indexes
    finally:
        p.close()


def test_close_and_identity_reset_index_state():
    p, lib = parser()
    try:
        query(p)
        assert p._transition_indexes
        p._stat_identity = lambda: ("replacement", 1, 2, 3, 4, 6)
        query(p, 450003, 1500007)
        assert p._transition_index_stats["hits"] == 0
        p.close()
        assert not p._transition_indexes and p._transition_index_bytes == 0
        assert not any(p._transition_index_stats.values())
    finally:
        p.close()


def test_builder_boundaries_zero_and_uint64_end():
    b = idx.CountIndexBuilder(0, max_points=4)
    b.offer(1, 2)
    b.offer(5, 6)
    index = b.finish((1 << 64) - 1, 10)
    assert index.plan(0, (1 << 64) - 1) == (0, 1 << 64, 10)
    assert index.plan(-1, 2) is None
    zero = idx.CountIndexBuilder(10).finish(20, 0)
    assert zero.plan(12, 18) == (12, 19, 0)
    b = idx.CountIndexBuilder(0)
    b.offer(2, 3)
    b.offer(1, 4)
    assert b.finish(10, 9) is None


@pytest.mark.parametrize("phase", ["prefix", "edge_count", "final_cleanup"])
def test_extra_index_hit_cancellation(phase, monkeypatch):
    p, lib = parser()
    query(p)
    event = threading.Event()
    token = push_cancel_event(event)
    if phase == "prefix":
        original = lib.fsdb_event_page_v1

        def page(*a):
            rc = original(*a)
            event.set()
            return rc

        monkeypatch.setattr(lib, "fsdb_event_page_v1", page)
    elif phase == "edge_count":
        lib.after_count = event.set
    else:
        original = lib.fsdb_end_transition_group
        ends = 0

        def end(*a):
            nonlocal ends
            rc = original(*a)
            ends += 1
            if ends == 3:
                event.set()
            return rc

        monkeypatch.setattr(lib, "fsdb_end_transition_group", end)
    try:
        with pytest.raises(OperationCancelled):
            query(p, 450003, 1500007)
        assert ("top.data", 450003, 1500007) not in p._transition_count_cache
        assert not lib.cursors and not p._transition_group_active
    finally:
        event.clear()
        pop_cancel_event(token)
        p.close()


def test_extra_partial_index_edge_never_seeds_exact(monkeypatch):
    p, lib = parser()
    query(p)
    original = lib.fsdb_event_count_v1

    def partial(*a):
        rc = original(*a)
        a[-2]._obj.truncated = 1
        return rc

    monkeypatch.setattr(lib, "fsdb_event_count_v1", partial)
    try:
        value = query(p, 450003, 1500007)
        assert value["transition_count_is_lower_bound"]
        assert ("top.data", 450003, 1500007) not in p._transition_count_cache
        assert p._transition_index_stats["builds"] == 1
    finally:
        p.close()


def test_extra_byte_budget_evicts_one_index(monkeypatch):
    p, lib = parser()
    try:
        query(p, 0, 80000)
        first = next(iter(p._transition_indexes.values()))[1]
        monkeypatch.setattr(idx, "MAX_BYTES", first + 256)
        query(p, 100000, 180000)
        assert len(p._transition_indexes) == 1
        assert p._transition_index_bytes <= idx.MAX_BYTES
    finally:
        p.close()


def test_compat_prefix_complete_left_edge_avoids_its_rescan():
    p, lib = parser()
    try:
        query(p)
        before = p._transition_index_stats["edge_queries"]
        value = query(p, 450003, 1500007, 5000)
        check(value, lib.rows, 1000, 450003, 1500007, 5000)
        assert p._transition_index_stats["hits"] == 1
        assert (
            p._transition_index_stats["edge_queries"] - before == 1
        )  # only right edge
    finally:
        p.close()


def test_compat_prefix_partial_same_ps_before_left_requires_scan():
    raw = [(i * 10, str(i % 2)) for i in range(200000)]
    position = next(i for i, row in enumerate(raw) if row[0] == 450010)
    raw[position : position + 1] = [
        (450010, "0"),
        (450010, "1"),
        (450010, "x"),
        (450010, "z"),
        (450010, "0"),
    ]
    p, lib = parser(raw)
    try:
        query(p)
        before = p._transition_index_stats["edge_queries"]
        value = query(p, 450003, 1500007, 2)
        check(value, raw, 1000, 450003, 1500007, 2)
        assert p._transition_index_stats["edge_queries"] - before == 2
    finally:
        p.close()


def test_compat_sparse_empty_covered_window_and_predecessor():
    raw = [(0, "0"), (100000, "1")]
    p, lib = parser(raw)
    try:
        query(p, cap=1)
        for start, end in [(1000, 90000), (2000, 80000), (100001, 100010)]:
            value = query(p, start, end, 1)
            check(value, raw, 1000, start, end, 1)
            assert value["transition_count"] == 0 and value["predecessor"] is not None
    finally:
        p.close()
