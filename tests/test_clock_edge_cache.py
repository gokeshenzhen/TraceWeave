"""Result equivalence, resource bounds and lifetime of full-clock reuse."""

from array import array
from concurrent.futures import ThreadPoolExecutor
import gc
import sys
import threading
from unittest.mock import patch
import weakref

import pytest

from src import cancellation, clock_edge_cache as storage, cycle_query
from src.clock_edge_cache import ClockCacheToken, ClockEdgeCache
from src.fsdb_parser import FSDBParser
from src.vcd_parser import VCDParser


@pytest.fixture
def cache(monkeypatch):
    instance = ClockEdgeCache(max_bytes=4096, entry_max_bytes=1024, max_entries=8)
    monkeypatch.setattr(storage, "cache", instance)
    return instance


@pytest.fixture
def wave(tmp_path):
    path = tmp_path / "clock.vcd"
    path.write_text(
        '$timescale 1ps $end\n$scope module top $end\n'
        '$var wire 1 ! clk $end\n$var wire 1 " data $end\n'
        '$var wire 2 # bus $end\n$upscope $end\n$enddefinitions $end\n'
        '#0\n0!\n0"\nb00 #\n#5\n1!\n0!\n1!\n1"\n'
        '#17\n0!\n#30\n1!\n#32\nx!\n#50\n1!\n0"\n'
        '#60\n0!\n#91\n1!\n'
    )
    return path


def query(parser, **kwargs):
    return cycle_query.get_signals_by_cycle(parser, "top.clk", [], **kwargs)


def test_cached_cycles_match_all_uncached_fields(cache, wave, monkeypatch):
    baseline, actual = VCDParser(str(wave)), VCDParser(str(wave))
    requests = [dict(num_cycles=3), dict(start_cycle=2, num_cycles=20),
                dict(start_time_ps=5, end_time_ps=60, max_cycles=2),
                dict(start_time_ps=100, num_cycles=0)]
    with patch.object(actual, "get_transitions", wraps=actual.get_transitions) as reads:
        for edge in ("posedge", "negedge", "posedge"):
            for offset in (0, 1):
                for request in requests:
                    args = dict(clock_path="top.clk", signal_paths=["top.data", "missing"],
                                edge=edge, sample_offset_ps=offset, **request)
                    with monkeypatch.context() as context:
                        context.setattr(storage, "cache", ClockEdgeCache(max_bytes=0))
                        expected = cycle_query.get_signals_by_cycle(baseline, **args)
                    assert cycle_query.get_signals_by_cycle(actual, **args) == expected
        assert sum(call.args[0] == "top.clk" for call in reads.call_args_list) == 3
    assert cache.snapshot()["entries"] == 1
    with pytest.raises(ValueError, match="1-bit"):
        cycle_query.get_signals_by_cycle(actual, "top.bus", [])


def test_process_byte_budget_lru_and_owner_collection():
    size = sys.getsizeof(array("Q", list(range(10))))
    cache = ClockEdgeCache(max_bytes=2 * size, entry_max_bytes=size, max_entries=8)
    a, b, c = ClockCacheToken(), ClockCacheToken(), ClockCacheToken()
    key = ("clk", "posedge")
    for token in (a, b):
        cache.put(token, key, list(range(10)), 1)
    assert cache.snapshot() == {"entries": 2, "bytes": 2 * size}
    assert cache.get(a, key) is not None  # a is now most recently used
    cache.put(c, key, list(range(10)), 1)
    assert cache.get(b, key) is None
    assert cache.get(a, key) is not None
    assert cache.snapshot()["bytes"] <= 2 * size
    owner = weakref.ref(a)
    del a
    gc.collect()
    assert owner() is None
    assert cache.snapshot() == {"entries": 1, "bytes": size}


def test_entry_count_limit_also_bounds_empty_indexes():
    cache = ClockEdgeCache(max_bytes=4096, entry_max_bytes=1024, max_entries=2)
    tokens = [ClockCacheToken() for _ in range(3)]
    for token in tokens:
        cache.put(token, ("clk", "posedge"), [], None)
    assert cache.snapshot()["entries"] == 2
    assert cache.get(tokens[0], ("clk", "posedge")) is None


@pytest.mark.parametrize("flag", ["truncated", "transition_count_is_lower_bound"])
def test_incomplete_clock_reads_are_never_cached(cache, wave, flag):
    parser = VCDParser(str(wave))
    original = parser.get_transitions

    def truncated(*args, **kwargs):
        return dict(original(*args, **kwargs), **{flag: True})

    with patch.object(parser, "get_transitions", side_effect=truncated) as reads:
        assert query(parser) == query(parser)
        assert reads.call_count == 2
    assert cache.snapshot()["entries"] == 0


def test_capacity_and_allocation_failure_are_bypasses(cache, wave, monkeypatch):
    parser = VCDParser(str(wave))
    cache.entry_max_bytes = 80
    with patch.object(parser, "get_transitions", wraps=parser.get_transitions) as reads:
        expected = query(parser)
        assert query(parser) == expected
        assert reads.call_count == 2
    assert cache.snapshot()["bytes"] == 0
    cache.entry_max_bytes = 1024
    with patch.object(storage, "array", side_effect=MemoryError):
        assert query(parser) == expected
    assert cache.snapshot()["bytes"] == 0
    cache.put(parser._clock_cache_token, ("clk", "posedge"), [-1, 2**65], None)
    assert cache.snapshot()["entries"] == 0
    assert query(parser) == expected
    assert cache.snapshot()["entries"] == 1


@pytest.mark.parametrize("warm", [False, True])
def test_cancelled_hit_or_miss_performs_no_clock_read(cache, wave, warm):
    parser = VCDParser(str(wave))
    if warm:
        query(parser)
    before = cache.snapshot()
    event = threading.Event()
    event.set()
    token = cancellation.push_cancel_event(event)
    try:
        with patch.object(parser, "get_transitions", side_effect=AssertionError("read after cancellation")):
            with pytest.raises(cancellation.OperationCancelled):
                query(parser)
    finally:
        cancellation.pop_cancel_event(token)
    assert cache.snapshot() == before


def test_cancelled_build_never_publishes_index(cache, wave):
    parser = VCDParser(str(wave))
    event = threading.Event()
    token = cancellation.push_cancel_event(event)
    original = cycle_query._compute_clock_period_ps

    def cancel_after_extract(edges):
        event.set()
        return original(edges)

    try:
        with patch.object(cycle_query, "_compute_clock_period_ps", side_effect=cancel_after_extract):
            with pytest.raises(cancellation.OperationCancelled):
                query(parser)
    finally:
        cancellation.pop_cancel_event(token)
    assert cache.snapshot()["entries"] == 0
    assert query(parser)["total_edges_found"] == 4


def test_fsdb_close_discards_index_even_without_an_open_handle(cache):
    parser = FSDBParser("/unused.fsdb")
    cache.put(parser._clock_cache_token, ("clk", "posedge"), [1, 2], 1)
    assert cache.snapshot()["entries"] == 1
    parser.close()
    assert cache.snapshot()["entries"] == 0


def test_server_replacement_discards_old_index_while_parser_is_referenced(cache, wave, monkeypatch):
    import server
    monkeypatch.setattr(server, "_parser_cache", {})
    first = server._get_parser(str(wave))
    assert query(first)["total_edges_found"] == 4
    wave.write_text(wave.read_text() + "#100\n0!\n#110\n1!\n")
    second = server._get_parser(str(wave))
    assert second is not first
    assert cache.snapshot()["entries"] == 0
    assert query(second)["total_edges_found"] == 5


def test_vcd_workers_share_bounded_cache_without_serializing_io(cache, wave):
    barrier = threading.Barrier(4)
    parsers = [VCDParser(str(wave)) for _ in range(4)]
    expected = query(VCDParser(str(wave)))

    def work(parser):
        original = parser.get_transitions

        def synchronized_read(*args, **kwargs):
            barrier.wait(timeout=5)
            return original(*args, **kwargs)

        with patch.object(parser, "get_transitions", side_effect=synchronized_read) as reads:
            assert query(parser) == expected
            assert query(parser) == expected
            assert reads.call_count == 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, parsers))
    assert cache.snapshot()["entries"] == 4
    assert cache.snapshot()["bytes"] <= cache.max_bytes
