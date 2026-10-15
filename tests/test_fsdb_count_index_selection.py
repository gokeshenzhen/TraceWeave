from array import array
import pytest
from tests.test_fsdb_count_index import parser, query, check
from src.fsdb_count_index import CountIndex
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
import threading


def setup_index(total, left, right):
    p, lib = parser([(i * 10, str(i % 2)) for i in range(total)])
    index = CountIndex(
        0,
        (total - 1) * 10,
        total,
        True,
        array("Q", [0, left * 10, right * 10]),
        array("Q", [0, left, right]),
    )
    p._cache_transition_index("top.data", index)
    return p, lib


@pytest.mark.parametrize("interior", [1, 65536])
def test_small_interior_with_edge_keeps_cursor(interior):
    p, lib = setup_index(interior + 100, 10, 10 + interior)
    try:
        value = query(p, 3, (interior + 50) * 10 + 7, 2)
        check(value, lib.rows, 1000, 3, (interior + 50) * 10 + 7, 2)
        s = p._transition_index_stats
        assert (
            s["small_window_bypasses"] == 1
            and s["edge_queries"] == 0
            and s["hits"] == 0
        )
        before = lib.index_calls
        assert query(p, 3, (interior + 50) * 10 + 7, 2) == value
        assert lib.index_calls == before and not lib.cursors
    finally:
        p.close()
    assert p._transition_index_bytes == 0 and not any(
        p._transition_index_stats.values()
    )


def test_large_interior_still_uses_edges():
    p, lib = setup_index(65700, 10, 65547)
    try:
        value = query(p, 3, 656007, 2)
        check(value, lib.rows, 1000, 3, 656007, 2)
        assert (
            p._transition_index_stats["hits"] == 1
            and p._transition_index_stats["edge_queries"] == 2
        )
        assert p._transition_index_stats["small_window_bypasses"] == 0
    finally:
        p.close()


def test_small_exact_interior_without_edge_still_hits():
    p, lib = setup_index(100, 10, 50)
    try:
        value = query(p, 100, 499, 2)
        check(value, lib.rows, 1000, 100, 499, 2)
        assert (
            p._transition_index_stats["hits"] == 1
            and p._transition_index_stats["edge_queries"] == 0
        )
        assert (
            p._transition_index_stats["small_window_bypasses"] == 0
            and lib.index_calls == 0
        )
    finally:
        p.close()


def test_bypassed_scan_cancel_cleans_group_and_does_not_seed():
    p, lib = setup_index(200, 10, 100)
    event = threading.Event()
    lib.after_count = event.set
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            query(p, 3, 1507, 2)
        assert (
            not p._transition_group_active
            and not lib.cursors
            and not p._transition_count_cache
        )
    finally:
        pop_cancel_event(token)
        p.close()


def test_bypassed_scan_identity_change_cannot_publish():
    p, lib = setup_index(200, 10, 100)
    old = p._file_identity
    lib.after_count = lambda: setattr(p, "_stat_identity", lambda: ("changed",))
    try:
        with pytest.raises(RuntimeError, match="FSDB changed"):
            query(p, 3, 1507, 2)
        assert not p._transition_count_cache and len(p._transition_indexes) == 1
        assert not p._transition_group_active and not lib.cursors
    finally:
        p.close()
