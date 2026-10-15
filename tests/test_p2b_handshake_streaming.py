"""P2b regression oracles: block boundaries must not become protocol EOF.

The complete pre-edge sample table is the differential oracle. Named expected
facts below come from the input cycle table, independently of its partition.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from src.cursor_store import CursorStore
from src.cycle_query import sample_signals_on_edges
from src.vcd_parser import VCDParser
from src.verify_condition import inspect_handshake


def _wave(tmp_path, cycles, *, ahb=False):
    definitions = [(1, "!", "clk"), (1, "r", "ready"),
                   (2 if ahb else 1, "v", "htrans" if ahb else "valid"),
                   (8, "p", "payload"), (1, "w", "hwrite"), (8, "d", "hwdata")]
    lines = ["$timescale 1ps $end", "$scope module top $end"]
    lines += [f"$var wire {width} {code} {name} $end" for width, code, name in definitions]
    lines += ["$upscope $end", "$enddefinitions $end"]
    for index, cycle in enumerate(cycles):
        lines += [f"#{index * 10}", "0!"]
        for width, code, name in definitions[1:]:
            value = cycle.get(name, 0)
            bits = str(value) * width if value in ("x", "z") else f"{int(value):0{width}b}"
            lines.append(f"b{bits} {code}")
        lines += [f"#{index * 10 + 5}", "1!"]
    path = tmp_path / "consumer.vcd"
    path.write_text("\n".join(lines) + "\n")
    parser = VCDParser(str(path))
    args = dict(get_parser=lambda _: parser, wave_path=str(path), clock="top.clk",
                ready="top.ready", payload=["top.payload"], max_wait_cycles=2,
                _compact_sampling=True)
    if ahb:
        args.update(valid_htrans="top.htrans", hwrite="top.hwrite", write_data="top.hwdata")
    else:
        args.update(valid="top.valid")
    sampled = sample_signals_on_edges(parser, "top.clk", [
        "top.htrans" if ahb else "top.valid", "top.ready", "top.payload",
        *(["top.hwrite", "top.hwdata"] if ahb else [])],
        sample_phase="before", sample_offset_ps=0, compact=True)
    return args, sampled


def _blocks(sampled, size):
    total = len(sampled["edge_times"])
    for begin in range(0, total, size):
        stop = min(begin + size, total)
        block = {**sampled, "edge_times": sampled["edge_times"][begin:stop],
                 "signal_columns": {path: values[begin:stop]
                                    for path, values in sampled["signal_columns"].items()},
                 "inactive_cycles": {index - begin: state
                                     for index, state in sampled.get("inactive_cycles", {}).items()
                                     if begin <= index < stop}}
        block["total_edges_found"] = stop - begin
        yield block


def _compare(args, sampled, size):
    expected = inspect_handshake(**args, _sampled=deepcopy(sampled), cursor_store=CursorStore())
    consumer = inspect_handshake(**args, _defer_sampling=True, cursor_store=CursorStore())
    for block in _blocks(sampled, size):
        consumer.consume(block)
    actual = consumer.finish()
    assert actual == expected
    return actual


@pytest.mark.parametrize("size", [1, 2, 3, 4, 7, 1000])
@pytest.mark.parametrize("tail", [False, True])
def test_cross_block_stall_closes_once_at_acceptance_or_true_end(tmp_path, size, tail):
    cycles = [{"valid": 1, "ready": 1, "payload": 16}]
    cycles += [{"valid": 1, "ready": 0, "payload": 16}] * 7
    if not tail:
        cycles += [{"valid": 1, "ready": 1, "payload": 16}] * 2
    args, sampled = _wave(tmp_path, cycles)
    result = _compare(args, sampled, size)
    assert result["sample_count"] == len(cycles)
    assert result["stall_count"] == result["max_stall_cycles"] == 7
    assert result["max_stall_begin_ps"] == 15
    assert result["transfer_count"] == (1 if tail else 3)
    assert result["ended_in_stall"] is tail
    assert result["final_stall_cycles"] == (7 if tail else 0)
    stalls = [finding for finding in result["findings"] if finding["type"] == "long_stall"]
    assert len(stalls) == 1 and stalls[0]["cycles"] == 7
    assert stalls[0]["end_ps"] == (75 if tail else 85)
    assert result["cursor"]["time_ps"] == 15


@pytest.mark.parametrize("size", [1, 2, 3, 5, 1000])
def test_valid_payload_unknown_and_relatch_keep_whole_request_order(tmp_path, size):
    cycles = [
        dict(valid=1, ready=1, payload=16),
        dict(valid=1, ready=0, payload=16),
        dict(valid=1, ready=0, payload=17),
        dict(valid=1, ready=0, payload=17),
        dict(valid=1, ready=0, payload=18),
        dict(valid=0, ready=1, payload=18),
        dict(valid=1, ready=0, payload=19),
        dict(valid="x", ready=0, payload="x"),
        dict(valid="z", ready=1, payload=20),
        dict(valid=1, ready=0, payload=21),
        dict(valid=1, ready=1, payload=21),
    ]
    args, sampled = _wave(tmp_path, cycles)
    result = _compare(args, sampled, size)
    assert result["payload_hold_violations"] == 2
    assert result["valid_deassert_violations"] == 1
    assert result["unknown_sample_cycles"] == 2
    assert result["stall_count"] == 6 and result["max_stall_cycles"] == 4
    assert result["transfer_count"] == 2
    assert result["cursor"]["time_ps"] == 25


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
@pytest.mark.parametrize("cap", [0, 1, 2, 5])
def test_ahb_phase_x_episode_and_independent_global_finding_caps(tmp_path, size, cap):
    cycles = [dict(htrans=2, ready=1, hwrite=1, payload=16, hwdata=0)]
    cycles += [dict(htrans=2, ready=0, hwrite=1,
                    payload="x" if index in (2, 3, 4) else index,
                    hwdata=index) for index in range(1, 9)]
    cycles += [dict(htrans=0, ready=1, payload=0, hwdata=9)]
    args, sampled = _wave(tmp_path, cycles, ahb=True)
    args["max_findings"] = cap
    result = _compare(args, sampled, size)
    assert result["x_while_valid_violations"] == 1
    assert result["write_data_hold_violations"] == 8
    assert result["payload_hold_violations"] == 4
    assert result["valid_deassert_violations"] == 1
    assert result["stall_count"] == result["max_stall_cycles"] == 8
    general = [finding for finding in result["findings"] if finding["type"] != "write_data_hold_violation"]
    writes = [finding for finding in result["findings"] if finding["type"] == "write_data_hold_violation"]
    assert len(general) == min(cap, 7)
    assert len(writes) == min(cap, 8)
    assert result["findings"] == general + writes
    if writes:
        assert writes[0]["time_ps"] == 25 and writes[0]["from_value"] == "0x01"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 1000])
def test_inactive_cycle_breaks_state_with_local_block_index(tmp_path, size):
    cycles = [dict(htrans=2, ready=1, hwrite=1, payload=1, hwdata=1)]
    cycles += [dict(htrans=2, ready=0, hwrite=1, payload=1, hwdata=1)] * 3
    cycles += [dict(htrans=0, ready=1, hwrite=0, payload=0, hwdata=7)]
    cycles += [dict(htrans=2, ready=0, hwrite=1, payload=1, hwdata=2)] * 3
    args, sampled = _wave(tmp_path, cycles, ahb=True)
    sampled["inactive_cycles"] = {4: "reset"}
    result = _compare(args, sampled, size)
    assert result["valid_deassert_violations"] == 0
    assert result["write_data_hold_violations"] == 0
    assert result["stall_count"] == 6 and result["max_stall_cycles"] == 3
    assert result["ended_in_stall"] and result["final_stall_cycles"] == 3


def test_deferred_empty_stream_preserves_empty_complete_result(tmp_path):
    args, sampled = _wave(tmp_path, [dict(valid=1, ready=1)])
    sampled["edge_times"] = []
    sampled["signal_columns"] = {path: [] for path in sampled["signal_columns"]}
    sampled["total_edges_found"] = 0
    # Empty input must be delivered at real EOF, not treated as a fresh block.
    expected = inspect_handshake(**args, _sampled=sampled)
    consumer = inspect_handshake(**args, _defer_sampling=True)
    actual = consumer.finish(sampling_status=sampled)
    assert actual == expected
    assert actual["sample_count"] == 0 and actual["reason"]
    assert not actual["coverage"]["clock_sampled"]


@pytest.mark.parametrize("size", [1, 2, 5])
def test_final_prefix_status_is_added_without_recounting_consumed_cycles(tmp_path, size):
    args, sampled = _wave(tmp_path, [dict(valid=1, ready=0, payload=1)] * 6)
    consumer = inspect_handshake(**args, _defer_sampling=True)
    for block in _blocks(sampled, size):
        consumer.consume(block)
    final = {**sampled, "transition_data_truncated": True,
             "transition_signals_truncated": ["top.clk"],
             "sampling_gaps": ["clock_unknown"]}
    expected = inspect_handshake(**args, _sampled=final)
    actual = consumer.finish(sampling_status=final)
    assert actual == expected
    assert actual["sample_count"] == actual["stall_count"] == 6
    assert actual["transition_data_truncated"]
    assert actual["coverage"]["sampling_gaps"] == ["clock_unknown"]
    assert actual["warnings"].count("clock_unknown") == 1


def _edge_wave(tmp_path, body, *, scale="1ps"):
    path = tmp_path / "edges.vcd"
    path.write_text(f"$timescale {scale} $end\n$scope module top $end\n"
                    "$var wire 1 ! clk $end\n$var wire 1 v valid $end\n"
                    "$var wire 1 r ready $end\n$var wire 8 p payload $end\n"
                    "$upscope $end\n$enddefinitions $end\n" + body + "\n")
    return VCDParser(str(path))


def _sample_rows(sampled):
    return [(at, {path: column[index] for path, column in sampled["signal_columns"].items()})
            for index, at in enumerate(sampled["edge_times"])]


def _stream_samples(parser, *, page_size=1, block_size=1, start=0, end=-1, paths=None, edge="posedge"):
    from src.fsdb_sweep_stream import StreamLimits, iter_edge_blocks
    paths = paths or ["top.valid", "top.ready", "top.payload"]
    limits = StreamLimits(page_events=page_size, page_bytes=128,
                          block_edges=block_size, block_bytes=4096)
    rows = []
    with iter_edge_blocks(parser, "top.clk", paths, start, end, edge, limits=limits) as stream:
        for block in stream:
            assert len(block["edge_times"]) <= block_size
            rows.extend(_sample_rows(block))
        status = deepcopy(stream.status)
    return rows, status


@pytest.mark.parametrize("page_size", [1, 2, 3, 7])
@pytest.mark.parametrize("block_size", [1, 2, 5])
@pytest.mark.parametrize("window", [(0, -1), (5, 25), (6, 14), (26, 99)])
def test_stream_exact_preedge_closed_windows_and_empty_gaps(tmp_path, page_size, block_size, window):
    parser = _edge_wave(tmp_path,
        "#0\n0!\n0v\n1r\nb00000001 p\n"
        "#4\n1v\n#5\n1!\n0v\nb00000010 p\n"
        "#10\n0!\n#14\n1v\n#15\n1!\n0v\n"
        "#20\n0!\n#24\n1v\n#25\n1!\n0v\n#30\n0!")
    start, end = window
    paths = ["top.valid", "top.ready", "top.payload"]
    expected = sample_signals_on_edges(parser, "top.clk", paths, start_ps=start,
        end_ps=end, sample_phase="before", sample_offset_ps=0, compact=True)
    rows, status = _stream_samples(parser, page_size=page_size, block_size=block_size,
                                   start=start, end=end)
    assert rows == _sample_rows(expected)
    assert status["signal_errors"] == expected["signal_errors"]
    assert status["sampling_gaps"] == expected["sampling_gaps"]
    assert status["transition_signals_truncated"] == expected["transition_signals_truncated"]
    if start == 5:
        assert [at for at, _ in rows] == [5, 15, 25]
        assert [signals["top.valid"]["dec"] for _, signals in rows] == [1, 1, 1]


@pytest.mark.parametrize("page_size", [1, 2, 3, 7])
@pytest.mark.parametrize("block_size", [1, 2, 5])
def test_stream_subps_and_same_tick_groups_preserve_physical_order(tmp_path, page_size, block_size):
    many_changes = "\n".join(f"{index % 2}v" for index in range(1101))
    parser = _edge_wave(tmp_path,
        "#0\n0!\n0v\n1r\nb00000001 p\n#1\n1v\n#5\n1!\n" + many_changes +
        "\n0v\n#7\n0!\n#8\n1!\n1v\n#10\n0!", scale="100fs")
    expected = sample_signals_on_edges(parser, "top.clk", ["top.valid", "top.ready", "top.payload"],
        sample_phase="before", sample_offset_ps=0, compact=True)
    rows, status = _stream_samples(parser, page_size=page_size, block_size=block_size)
    assert rows == _sample_rows(expected)
    assert [at for at, _ in rows] == [1, 1]
    assert [signals["top.valid"]["dec"] for _, signals in rows] == [1, 0]
    assert not status["transition_signals_truncated"]


@pytest.mark.parametrize("page_size", [1, 2, 3])
@pytest.mark.parametrize("bad_clock,gap", [("x!", "clock_unknown"),
    ("z!", "clock_unknown"), ("1!\n0!\n1!", "clock_event_order_unresolved")])
def test_stream_clock_gap_drops_entire_ambiguous_group(tmp_path, page_size, bad_clock, gap):
    parser = _edge_wave(tmp_path, "#0\n0!\n1v\n1r\nb00000001 p\n#5\n1!\n"
                        "#10\n0!\n#15\n" + bad_clock + "\n#20\n0!\n#25\n1!")
    expected = sample_signals_on_edges(parser, "top.clk", ["top.valid", "top.ready", "top.payload"],
        sample_phase="before", sample_offset_ps=0, compact=True)
    rows, status = _stream_samples(parser, page_size=page_size)
    assert rows == _sample_rows(expected)
    assert [at for at, _ in rows] == [5]
    assert status["sampling_gaps"] == [gap]
    assert status["transition_signals_truncated"] == ["top.clk"]


@pytest.mark.parametrize("page_size", [1, 2, 3])
def test_stream_sparse_large_time_and_missing_signal_are_not_idle(tmp_path, page_size):
    parser = _edge_wave(tmp_path,
        "#0\n0!\n1v\n1r\nb00000001 p\n#5\n1!\n#10\n0!\n"
        "#1000000000000005\n1!\n#1000000000000010\n0!")
    paths = ["top.valid", "top.ready", "top.missing"]
    expected = sample_signals_on_edges(parser, "top.clk", paths, sample_phase="before",
                                       sample_offset_ps=0, compact=True)
    rows, status = _stream_samples(parser, page_size=page_size, paths=paths)
    assert rows == _sample_rows(expected)
    assert len(rows) == 2 and rows[-1][0] == 1000000000000005
    assert "top.missing" in status["signal_errors"]
    assert not status["transition_signals_truncated"]


def test_consumer_releases_every_consumed_column_and_edge_vector(tmp_path):
    import gc
    import weakref

    class TrackedList(list):
        pass

    args, sampled = _wave(tmp_path, [dict(valid=1, ready=0, payload=1)] * 19)
    consumer = inspect_handshake(**args, _defer_sampling=True)
    references = []
    for index in range(32):
        block = {**sampled, "edge_times": TrackedList(at + index * 190 for at in sampled["edge_times"]),
                 "signal_columns": {path: TrackedList(values) for path, values in sampled["signal_columns"].items()}}
        references.append(weakref.ref(block["edge_times"]))
        references.extend(weakref.ref(values) for values in block["signal_columns"].values())
        consumer.consume(block)
        del block
        gc.collect()
        assert all(reference() is None for reference in references)
    result = consumer.finish()
    assert result["sample_count"] == result["stall_count"] == 32 * 19
    assert result["max_stall_cycles"] == 32 * 19


@pytest.mark.parametrize("cancel", [False, True])
def test_stream_interruption_closes_readers_group_and_allows_reuse(tmp_path, monkeypatch, cancel):
    from contextlib import contextmanager
    import threading

    from src.cancellation import OperationCancelled, check_cancelled, pop_cancel_event, push_cancel_event

    parser = _edge_wave(tmp_path, "#0\n0!\n1v\n1r\nb00000001 p\n#5\n1!\n"
                        "#10\n0!\n#15\n1!\n#20\n0!\n#25\n1!\n#30\n0!")
    original = parser._event_pages
    state = {"readers": 0, "groups": 0, "pages": 0, "inject": True}
    event = threading.Event()

    @contextmanager
    def group(paths, start, end):
        assert state["groups"] == 0
        state["groups"] += 1
        parser._transition_group_active = True
        try:
            yield True
        finally:
            parser._transition_group_active = False
            state["groups"] -= 1

    @contextmanager
    def pages(*args, **kwargs):
        with original(*args, **kwargs) as reader:
            state["readers"] += 1
            read = reader.read_page

            def interrupted():
                page = read()
                state["pages"] += 1
                if state["inject"] and state["pages"] == 5:
                    if cancel:
                        event.set()
                        check_cancelled()
                    raise RuntimeError("injected page failure")
                return page

            reader.read_page = interrupted
            try:
                yield reader
            finally:
                state["readers"] -= 1

    monkeypatch.setattr(parser, "_event_transition_group", group, raising=False)
    monkeypatch.setattr(parser, "_event_pages", pages)
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled if cancel else RuntimeError,
                           match=None if cancel else "injected page failure"):
            _stream_samples(parser, page_size=1, block_size=1)
        assert state["readers"] == state["groups"] == 0
        assert not parser._transition_group_active
        event.clear()
        state["inject"] = False
        rows, status = _stream_samples(parser, page_size=1, block_size=1)
        assert [at for at, _ in rows] == [5, 15, 25]
        assert not status["transition_signals_truncated"]
        assert state["readers"] == state["groups"] == 0
        assert parser.get_value_at_time("top.valid", 5)["value"]["dec"] == 1
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize("kind,expected_reason", [("events", "sweep_event_budget"),
    ("bytes", "sweep_byte_budget"), ("time", "sweep_time_budget")])
def test_stream_total_budget_stops_honestly_and_is_not_reset_by_blocks(tmp_path, kind, expected_reason):
    from src.fsdb_sweep_stream import StreamLimits, SweepBudget, iter_edge_blocks

    args, sampled = _wave(tmp_path, [dict(valid=1, ready=1, payload=1)] * 40)
    parser = args["get_parser"](args["wave_path"])
    options = dict(max_events=10000, max_bytes=10000000, timeout_sec=30)
    if kind == "events":
        options["max_events"] = 18
    elif kind == "bytes":
        options["max_bytes"] = 18 * 516
    else:
        options["timeout_sec"] = 0
    budget = SweepBudget(**options)
    paths = ["top.valid", "top.ready", "top.payload"]
    with iter_edge_blocks(parser, "top.clk", paths, 0, -1,
                          limits=StreamLimits(page_events=2, page_bytes=128, block_edges=1), budget=budget) as stream:
        rows = [row for block in stream for row in _sample_rows(block)]
        assert rows == _sample_rows(sampled)[:len(rows)]
        assert len(rows) < 40
        assert stream.status["transition_signals_truncated"] == ["top.clk"]
        assert stream.status["sampling_gaps"] == [expected_reason]
    # A request-wide exhausted budget cannot grant a fresh allowance to a pack.
    with iter_edge_blocks(parser, "top.clk", paths, 0, -1, budget=budget) as second:
        assert list(second) == []
        assert second.status["sampling_gaps"] == [expected_reason]


def test_stream_block_byte_cap_limits_edges_independently_of_edge_count(tmp_path):
    from src.fsdb_sweep_stream import StreamLimits, iter_edge_blocks

    args, sampled = _wave(tmp_path, [dict(valid=1, ready=1, payload=1)] * 23)
    parser = args["get_parser"](args["wave_path"])
    paths = ["top.valid", "top.ready", "top.payload"]
    limits = StreamLimits(page_events=3, page_bytes=128, block_edges=100, block_bytes=2048)
    with iter_edge_blocks(parser, "top.clk", paths, 0, -1, limits=limits) as stream:
        blocks = list(stream)
        assert len(blocks) == 23 and all(len(block["edge_times"]) == 1 for block in blocks)
        assert [row for block in blocks for row in _sample_rows(block)] == _sample_rows(sampled)
        assert not stream.status["transition_signals_truncated"]


def test_stream_unrepresentable_sample_row_is_partial_with_no_fake_complete(tmp_path):
    from src.fsdb_sweep_stream import StreamLimits, iter_edge_blocks

    parser = _edge_wave(tmp_path, "#0\n0!\n1v\n1r\nb00000001 p\n#5\n1!\n#10\n0!")
    # Even three ordinary columns cannot fit a 1024-byte logical sample row.
    with iter_edge_blocks(parser, "top.clk", ["top.valid", "top.ready", "top.payload"], 0, -1,
            limits=StreamLimits(block_edges=100, block_bytes=1024)) as stream:
        assert list(stream) == []
        assert stream.status["sampling_gaps"] == ["sweep_sample_row_budget"]
        assert stream.status["transition_signals_truncated"] == ["top.clk"]


@pytest.mark.parametrize("filename,top", [("scale_100fs.fsdb", "scale_100fs_tb"),
    ("scale_1ns.fsdb", "scale_1ns_tb")])
@pytest.mark.parametrize("page_size,block_size", [(1, 1), (2, 3), (7, 2)])
@pytest.mark.parametrize("start,end", [(0, -1), (5000, 15000), (100000, 110000)])
def test_native_stream_matches_complete_raw_time_oracle_and_reuses_parser(
        require_fsdb_runtime, filename, top, page_size, block_size, start, end):
    from pathlib import Path
    from src.fsdb_parser import FSDBParser
    from src.fsdb_sweep_stream import StreamLimits, iter_edge_blocks

    parser = FSDBParser(str(Path(__file__).parent / "fixtures" / filename))
    clock, address = f"{top}.clk", f"{top}.addr[31:0]"
    try:
        expected = sample_signals_on_edges(parser, clock, [clock, address], start_ps=start,
            end_ps=end, sample_phase="before", sample_offset_ps=0, compact=True)
        with iter_edge_blocks(parser, clock, [clock, address], start, end,
            limits=StreamLimits(page_events=page_size, page_bytes=256, block_edges=block_size)) as stream:
            rows = [row for block in stream for row in _sample_rows(block)]
            assert rows == _sample_rows(expected)
            assert not stream.status["transition_signals_truncated"]
            assert stream.sample_count == len(rows)
        times = [5000 + index * 10000 for index in range(11)]
        assert [at for at, _ in rows] == [at for at in times if at >= start and (end == -1 or at <= end)]
        assert all(signals[clock]["dec"] == 0 for _, signals in rows)
        assert not parser._transition_group_active
        assert parser.get_value_at_time(address, 100000)["value"]["dec"] == 0xAAAA0000
    finally:
        parser.close()


@pytest.fixture
def native_protocol(require_fsdb_runtime):
    import os
    from src.fsdb_parser import FSDBParser
    path = os.environ.get("TW_P2B_PROTOCOL_FSDB")
    if not path:
        pytest.skip("set TW_P2B_PROTOCOL_FSDB to the existing small protocol benchmark fixture")
    parser = FSDBParser(path)
    try:
        yield parser
    finally:
        parser.close()


@pytest.mark.parametrize("page_size,block_size", [(1, 1), (2, 3), (7, 2), (1024, 2048)])
@pytest.mark.parametrize("group_cap", [4, 16])
def test_native_sweep_stream_shared_clocks_multiple_packs_match_legacy(
        native_protocol, monkeypatch, page_size, block_size, group_cap):
    from contextlib import contextmanager
    import src.fsdb_sweep_stream as streaming
    from src.handshake_sweep import sweep_handshake_anomalies

    parser = native_protocol
    parser._open()
    monkeypatch.setenv("TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS", str(group_cap))
    args = dict(get_parser=lambda _: parser, wave_path=parser.file_path,
                max_wait_cycles=4, cursor_store=CursorStore())
    actual_limits = streaming.StreamLimits
    monkeypatch.setattr(streaming, "StreamLimits", lambda: actual_limits(
        page_events=page_size, page_bytes=262144, block_edges=block_size))
    real_group = parser._event_transition_group
    real_blocks = streaming.iter_edge_blocks
    real_open = parser._lib.fsdb_event_open_v1
    opened = []
    native_opened = []
    state = {"group": 0}

    def open_cursor(handle, path, *args):
        native_opened.append((state["group"], path.decode()))
        return real_open(handle, path, *args)

    @contextmanager
    def groups(*args, **kwargs):
        state["group"] += 1
        with real_group(*args, **kwargs) as active:
            yield active

    @contextmanager
    def blocks(parser, clock, *args, **kwargs):
        opened.append((state["group"], clock))
        with real_blocks(parser, clock, *args, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(parser, "_event_transition_group", groups)
    monkeypatch.setattr(streaming, "iter_edge_blocks", blocks)
    monkeypatch.setattr(parser._lib, "fsdb_event_open_v1", open_cursor)
    actual = sweep_handshake_anomalies(**args)
    assert opened and len(opened) == len(set(opened))
    assert all(native_opened.count(clock_request) == 1 for clock_request in opened)
    assert not parser._transition_group_active
    monkeypatch.setattr(streaming, "supports_streaming", lambda _: False)
    expected = sweep_handshake_anomalies(**{**args, "cursor_store": CursorStore()})
    assert actual == expected
    assert actual["coverage_status"] == "complete"
    assert actual["interface_count"] == 10


@pytest.mark.parametrize("missing", ["_traceweave_has_event_pages_v1", "_traceweave_has_transition_group"])
def test_native_old_abi_falls_back_to_legacy_sweep(native_protocol, monkeypatch, missing):
    import src.fsdb_sweep_stream as streaming
    from src.handshake_sweep import sweep_handshake_anomalies

    parser = native_protocol
    parser._open()
    args = dict(get_parser=lambda _: parser, wave_path=parser.file_path, max_wait_cycles=4)
    with monkeypatch.context() as patch:
        patch.setattr(streaming, "supports_streaming", lambda _: False)
        expected = sweep_handshake_anomalies(**args)
    monkeypatch.setattr(parser._lib, missing, False)
    monkeypatch.setattr(streaming, "stream_fsdb_pack", lambda *a, **k: pytest.fail("old ABI entered streaming"))
    actual = sweep_handshake_anomalies(**args)
    assert actual == expected
    assert not parser._transition_group_active


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_native_stream_keeps_global_ffr_lock_until_cancel_cleanup(native_protocol, monkeypatch):
    from contextlib import contextmanager
    import threading

    import anyio
    import server
    import src.fsdb_sweep_stream as streaming
    from src.cancellation import OperationCancelled
    from src.handshake_sweep import sweep_handshake_anomalies

    parser = native_protocol
    parser._open()
    entered, release, interactive_entered, cancelled = (threading.Event() for _ in range(4))
    state = {"stream": False, "injected": False, "native_boundary": False}
    native_page = parser._lib.fsdb_event_page_v1
    real_blocks = streaming.iter_edge_blocks

    @contextmanager
    def blocks(*args, **kwargs):
        state["stream"] = True
        try:
            with real_blocks(*args, **kwargs) as stream:
                yield stream
        finally:
            state["stream"] = False

    def pause_after_native(*args):
        result = native_page(*args)
        if state["stream"] and not state["injected"]:
            state["injected"] = state["native_boundary"] = True
            entered.set()
            try:
                assert release.wait(5)
                assert not interactive_entered.is_set()
            finally:
                state["native_boundary"] = False
        return result

    monkeypatch.setattr(streaming, "iter_edge_blocks", blocks)
    monkeypatch.setattr(parser._lib, "fsdb_event_page_v1", pause_after_native)

    def sweep():
        return sweep_handshake_anomalies(get_parser=lambda _: parser, wave_path=parser.file_path)

    async def background():
        try:
            await server._run_in_wave_thread(parser.file_path, sweep,
                                            priority=server._WAVE_PRIORITY_BACKGROUND)
        except OperationCancelled:
            cancelled.set()

    def point_query():
        assert not state["native_boundary"] and not parser._transition_group_active
        parser.get_summary()
        interactive_entered.set()

    async def interactive():
        # Distinct FSDB paths must still serialize on the process-global lock.
        await server._run_in_wave_thread("/another/local/trace.fsdb", point_query)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(background)
            assert await anyio.to_thread.run_sync(entered.wait, 5)
            group.start_soon(interactive)
            with anyio.fail_after(3):
                while not server._FSDB_ACTIVE or not server._FSDB_ACTIVE[0].is_set():
                    await anyio.sleep(0.01)
            assert not interactive_entered.is_set()
            release.set()
    finally:
        release.set()
    assert cancelled.is_set() and interactive_entered.is_set()
    assert not parser._transition_group_active
    recovered = await server._run_in_wave_thread(parser.file_path, sweep)
    assert recovered["coverage_status"] == "complete"
    assert recovered["interface_count"] == 10


@pytest.mark.parametrize("fixture_var,start,end,expected_samples", [
    ("TW_P2B_COMPLETE_FSDB", 0, -1, 294912),
    ("TW_P2B_LARGE_FSDB", 1280, 3840, 1152)])
@pytest.mark.parametrize("block_size", [31, 2048])
def test_native_complete_full_and_large_window_equal_legacy(
        require_fsdb_runtime, monkeypatch, fixture_var, start, end, expected_samples, block_size):
    import os
    import src.fsdb_sweep_stream as streaming
    from src.fsdb_parser import FSDBParser
    from src.handshake_sweep import sweep_handshake_anomalies

    path = os.environ.get(fixture_var)
    if not path:
        pytest.skip(f"set {fixture_var} to the existing protocol benchmark fixture")
    parser = FSDBParser(path)
    limits_class = streaming.StreamLimits
    monkeypatch.setattr(streaming, "StreamLimits", lambda: limits_class(
        page_events=127, block_edges=block_size))
    args = dict(get_parser=lambda _: parser, wave_path=path, start_ps=start, end_ps=end,
                max_wait_cycles=4, cursor_store=CursorStore())
    try:
        actual = sweep_handshake_anomalies(**args)
        monkeypatch.setattr(streaming, "supports_streaming", lambda _: False)
        expected = sweep_handshake_anomalies(**{**args, "cursor_store": CursorStore()})
        assert actual == expected
        assert actual["coverage_status"] == "complete"
        assert sum(row["sample_count"] for row in actual["interfaces"]) == expected_samples
        assert not parser._transition_group_active
    finally:
        parser.close()


@pytest.mark.parametrize("kind", ["unfinished_tick", "late_recording_gap"])
@pytest.mark.parametrize("block_size", [1, 2, 5])
def test_stream_payload_page_boundary_preserves_proven_prefix(tmp_path, monkeypatch, kind, block_size):
    from contextlib import contextmanager
    from src.event_pages import Event, EventPage, RecordingGap

    parser = _edge_wave(tmp_path,
        "#0\n0!\n1v\n1r\nb00000000 p\n#5\n1!\n#10\n0!\n#15\n1!\n#20\n0!\n#25\n1!\n#30\n0!")
    original = parser._event_pages

    @contextmanager
    def pages(path, start=0, end=-1, **kwargs):
        if path != "top.payload":
            with original(path, start, end, **kwargs) as reader:
                yield reader
            return

        class Reader:
            width = 8
            start_fs = start * 1000
            end_fs = 30000
            index = 0

            def read_page(self):
                index = self.index
                self.index += 1
                if kind == "unfinished_tick":
                    assert index == 0
                    return EventPage((Event(0, 0, "00000000"), Event(15000, 15, "00000001")),
                                     next_time=15000, truncated=True)
                if index == 0:
                    return EventPage((Event(0, 0, "00000000"),), next_time=10000)
                assert index == 1
                return EventPage((Event(10000, 10, None, kind="recording_gap"),), complete=True,
                    recording_gaps=(RecordingGap(10000, 30000, "dump_inactive"),))

        yield Reader()

    monkeypatch.setattr(parser, "_event_pages", pages)
    rows, status = _stream_samples(parser, block_size=block_size)
    assert [at for at, _ in rows] == [5, 15, 25]
    assert rows[0][1]["top.payload"]["dec"] == 0
    assert all(signals["top.payload"] is None for _, signals in rows[1:])
    assert status["transition_signals_truncated"] == ["top.payload"]
    assert not status["sampling_gaps"]  # Signal uncertainty must not invent a clock gap.
