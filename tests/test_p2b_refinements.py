"""Independent cursor, page-accounting and consumer-isolation refinement oracles."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src import operation_metrics
from src.cancellation import OperationCancelled
from src.event_pages import Event, EventPage, GroupCursor, RecordingGap
import src.fsdb_sweep_stream as streaming


class _GenericCursor(streaming._Cursor):
    """Bypass the private single-record fast path with the general algorithm."""
    def take_group(self, at):
        return GroupCursor.take_group(self, at)


class _Reader:
    def __init__(self, pages, *, start_fs=0, width=1):
        self.remaining = iter(pages)
        self.start_fs = start_fs
        self.width = width
        self.end_fs = 100000
        self.reads = 0

    def read_page(self):
        self.reads += 1
        try:
            return next(self.remaining)
        except StopIteration:
            raise RuntimeError("reader attempted to advance beyond supplied pages") from None


class _CheckpointBudget(streaming.SweepBudget):
    def __init__(self, cancel_at=None):
        super().__init__()
        self.checkpoints = 0
        self.cancel_at = cancel_at

    def check(self):
        self.checkpoints += 1
        if self.checkpoints == self.cancel_at:
            raise OperationCancelled("injected checkpoint cancellation")
        return super().check()


def _owner(budget):
    return streaming.EdgeBlockStream(None, "clk", [], 0, -1, "posedge",
                                     streaming.StreamLimits(), budget, ExitStack())


def _event(at, value, kind="transition"):
    return Event(at, (at + 999) // 1000, value, kind=kind)


def _pages(case):
    first = _event(0, "0")
    if case == "single":
        return [EventPage((first, _event(1000, "1"), _event(2000, "0")), complete=True)], 0
    if case == "same_value":
        return [EventPage((first, _event(1000, "1"), _event(1000, "1"), _event(2000, "0")), complete=True)], 0
    if case == "same_tick_toggle":
        return [EventPage((first, _event(1000, "1"), _event(1000, "0"), _event(1000, "1")), complete=True)], 0
    if case == "cross_page":
        return [EventPage((first,), next_time=1000), EventPage((_event(1000, "1"),), next_time=1000),
                EventPage((_event(1000, "0"), _event(1000, "1")), next_time=2000),
                EventPage((_event(2000, "0"),), complete=True)], 0
    if case == "initial_in_window":
        return [EventPage((_event(1000, "1"),), initial_state=_event(0, "0", "initial_state"), complete=True)], 0
    if case == "initial_before_window":
        return [EventPage((_event(1000, "1"),), initial_state=_event(0, "0", "initial_state"), complete=True)], 500
    if case == "initial_same_tick":
        return [EventPage((first, _event(1000, "1")), initial_state=_event(0, "1", "initial_state"), complete=True)], 0
    if case == "predecessor":
        return [EventPage((first, _event(1000, "1")), predecessor=_event(-1000, "1"), complete=True)], 0
    if case == "replayed_anchor":
        return [EventPage((first,), next_time=1000),
                EventPage((_event(1000, "1"),), predecessor=first, complete=True)], 0
    if case == "descending_inside":
        return [EventPage((first, _event(2000, "1"), _event(1000, "0")), complete=True)], 0
    if case == "descending_next_page":
        return [EventPage((first, _event(2000, "1")), next_time=1000),
                EventPage((_event(1000, "0"),), complete=True)], 0
    if case == "unknown":
        return [EventPage((first, _event(1000, "x"), _event(2000, "z"), _event(3000, "1")), complete=True)], 0
    if case == "recording_gap":
        return [EventPage((first,), next_time=1000),
                EventPage((_event(1000, None, "recording_gap"), _event(2000, "1")), complete=True,
                    recording_gaps=(RecordingGap(1000, 2000, "dump_inactive"),))], 0
    if case == "incomplete_group":
        return [EventPage((first, _event(1000, "1")), next_time=1000, truncated=True)], 0
    if case == "empty_continuation":
        return [EventPage((first,), next_time=1000), EventPage((), next_time=1000)], 0
    if case == "empty_complete":
        return [EventPage((), complete=True)], 0
    raise AssertionError(case)


def _trace(cls, pages, start_fs, *, cancel_at=None):
    budget = _CheckpointBudget(cancel_at)
    owner = _owner(budget)
    reader = _Reader(pages, start_fs=start_fs)
    rows = []
    failure = None
    cursor = None
    try:
        cursor = cls(reader, budget, owner)
        while cursor.peek() is not None:
            at = cursor.peek()
            value = cursor.take_group(at)
            rows.append((at, value, cursor.group_changed, frozenset(cursor.group_kinds),
                         cursor.compared, cursor.last_time, cursor.peek()))
    except (OperationCancelled, ValueError, RuntimeError, streaming.IncompleteGroup) as exc:
        failure = (type(exc).__name__, str(exc))
    finally:
        owner.close()
    return dict(rows=rows, failure=failure, page_reads=reader.reads,
                checkpoints=budget.checkpoints, charged_events=budget.events,
                charged_bytes=budget.nbytes)


CASES = ["single", "same_value", "same_tick_toggle", "cross_page", "initial_in_window",
         "initial_before_window", "initial_same_tick", "predecessor", "replayed_anchor",
         "descending_inside", "descending_next_page", "unknown", "recording_gap",
         "incomplete_group", "empty_continuation", "empty_complete"]


@pytest.mark.parametrize("case", CASES)
def test_private_cursor_matches_general_group_cursor_state_and_order(case):
    pages, start_fs = _pages(case)
    assert _trace(streaming._Cursor, pages, start_fs) == _trace(_GenericCursor, pages, start_fs)


@pytest.mark.parametrize("case", ["single", "same_tick_toggle", "cross_page", "initial_same_tick", "recording_gap"])
@pytest.mark.parametrize("cancel_at", range(1, 13))
def test_private_cursor_preserves_each_general_cursor_cancellation_checkpoint(case, cancel_at):
    pages, start_fs = _pages(case)
    assert _trace(streaming._Cursor, pages, start_fs, cancel_at=cancel_at) == _trace(
        _GenericCursor, pages, start_fs, cancel_at=cancel_at)


def _cost(page):
    # Deliberately independent of the production page_records/account helpers.
    rows = [*page.events]
    if page.predecessor is not None:
        rows.append(page.predecessor)
    if page.initial_state is not None:
        rows.append(page.initial_state)
    return len(rows), sum(512 + 4 * len(event.value or "") for event in rows)


@pytest.mark.parametrize("anchors", ["none", "initial", "predecessor", "both"])
@pytest.mark.parametrize("values", [(), ("",), ("0", "1"), (None, "x" * 256, "z" * 1024)])
def test_page_charging_counts_every_typed_anchor_and_value_once(anchors, values):
    page = EventPage(tuple(_event(index * 1000, value) for index, value in enumerate(values)),
                     initial_state=_event(-2000, "1" * 17, "initial_state") if anchors in ("initial", "both") else None,
                     predecessor=_event(-1000, "0" * 9) if anchors in ("predecessor", "both") else None,
                     complete=True)
    budget = streaming.SweepBudget()
    result = budget.account(page)
    expected = _cost(page)
    assert (budget.events, budget.nbytes) == expected
    # opt1 returns these precomputed facts; independent variants may return None.
    if result is not None:
        assert result == expected


def test_live_page_peaks_replace_resident_pages_instead_of_accumulating_history():
    pages_a = [EventPage((_event(0, "0"), _event(1000, "1")), predecessor=_event(-1000, "0"), next_time=2000),
               EventPage((_event(2000, "1" * 256),), complete=True)]
    pages_b = [EventPage((_event(0, "1"),), initial_state=_event(-1000, "0", "initial_state"), next_time=3000),
               EventPage(tuple(_event(3000 + i, "0" * 32) for i in range(4)), complete=True)]
    budget = streaming.SweepBudget()
    owner = _owner(budget)
    metrics = operation_metrics.OperationMetrics(values={"_sweep_active": True})
    token = operation_metrics.push(metrics)
    try:
        a = streaming._Cursor(_Reader(pages_a), budget, owner)
        b = streaming._Cursor(_Reader(pages_b), budget, owner)
        # Admit A0, B0, replace A0 by A1, then replace B0 by B1.
        a.take_group(0); a.take_group(1000); a.take_group(2000)
        b.take_group(0); b.take_group(3000)
        current = operation_metrics.snapshot(metrics)
        costs_a = [_cost(page) for page in pages_a]
        costs_b = [_cost(page) for page in pages_b]
        resident = [costs_a[0], tuple(x+y for x,y in zip(costs_a[0],costs_b[0])),
                    tuple(x+y for x,y in zip(costs_a[1],costs_b[0])),
                    tuple(x+y for x,y in zip(costs_a[1],costs_b[1]))]
        assert current["sweep_stream_live_page_records_peak"] == max(row[0] for row in resident)
        assert current["sweep_stream_live_page_bytes_peak"] == max(row[1] for row in resident)
        assert current["sweep_stream_active_cursors_peak"] == 2
        assert budget.events == sum(_cost(page)[0] for page in [*pages_a,*pages_b])
        assert budget.nbytes == sum(_cost(page)[1] for page in [*pages_a,*pages_b])
    finally:
        owner.close()
        operation_metrics.pop(token)


def _protocol_parser(tmp_path):
    from src.vcd_parser import VCDParser

    path = tmp_path / "refinement.vcd"
    definitions = [(1,"!","clk"),(1,"r","ready"),(1,"a","valid0"),
                   (1,"b","valid1"),(1,"c","valid2"),(8,"d","payload0"),
                   (8,"e","payload1"),(8,"f","shared")]
    lines = ["$timescale 1ps $end", "$scope module top $end"]
    lines += [f"$var wire {width} {code} {name} $end" for width,code,name in definitions]
    lines += ["$upscope $end", "$enddefinitions $end"]
    for cycle in range(12):
        lines += [f"#{cycle*10}", "0!", f"{int(cycle not in range(1,8))}r", "1a", "1b", "1c",
                  f"b{int(cycle>=4):08b} d", "b00000001 e", "b00000010 f",
                  f"#{cycle*10+5}", "1!"]
    path.write_text("\n".join(lines)+"\n")
    return VCDParser(str(path))


@pytest.mark.parametrize("problem", ["clean", "payload_missing", "valid_missing", "payload_truncated", "clock_truncated"])
@pytest.mark.parametrize("block_edges", [1, 3, 20])
def test_prebound_pack_keeps_each_consumers_status_and_protocol_state_isolated(
        tmp_path, monkeypatch, problem, block_edges):
    import gc
    import weakref
    from src.cycle_query import sample_signals_on_edges
    from src.verify_condition import inspect_handshake

    parser = _protocol_parser(tmp_path)
    members = [dict(clock="top.clk", ready="top.ready", valid=f"top.valid{index}",
        payload=["top."+payload], inspect_kwargs={"valid":f"top.valid{index}"})
        for index,payload in enumerate(["payload0","payload1","shared"])]
    paths = ["top.ready", "top.valid0", "top.valid1", "top.valid2", "top.payload0", "top.payload1", "top.shared"]
    sampled = sample_signals_on_edges(parser,"top.clk",paths,compact=True,
        sample_phase="before",sample_offset_ps=0)
    errors = {"top.payload0":"missing payload"} if problem=="payload_missing" else (
             {"top.valid0":"missing valid"} if problem=="valid_missing" else {})
    limited = ["top.payload0"] if problem=="payload_truncated" else (
              ["top.clk"] if problem=="clock_truncated" else [])
    final_status = dict(signal_errors=errors, transition_signals_truncated=limited,
                        sampling_gaps=["clock_unknown"] if problem=="clock_truncated" else [])
    if problem=="payload_truncated":
        sampled["signal_columns"]["top.payload0"][5:] = [None]*7
    references = []

    class TrackedDict(dict):
        pass

    class TrackedList(list):
        pass

    class Blocks:
        status = final_status

        def __iter__(self):
            for start in range(0,len(sampled["edge_times"]),block_edges):
                end = start+block_edges
                block = TrackedDict(edge_times=TrackedList(sampled["edge_times"][start:end]),
                    signal_columns={path:TrackedList(values[start:end])
                                    for path,values in sampled["signal_columns"].items()},**final_status)
                references.append(weakref.ref(block))
                references.append(weakref.ref(block["edge_times"]))
                references.extend(weakref.ref(column) for column in block["signal_columns"].values())
                yield block

    @contextmanager
    def blocks(*args,**kwargs):
        yield Blocks()

    monkeypatch.setattr(streaming,"iter_edge_blocks",blocks)
    cache = {path:path for path in ["top.clk",*paths]}
    actual = streaming.stream_fsdb_pack(parser,[(member,None) for member in members],cache,
        wave_path=parser.file_path,start_ps=0,end_ps=-1,edge="posedge",max_wait_cycles=2,
        budget=streaming.SweepBudget())
    for index,member in enumerate(members):
        own = {member["valid"],member["ready"],*member["payload"]}
        expected_samples = {**sampled,**final_status,
            "signal_errors":{path:error for path,error in errors.items() if path in own},
            "transition_signals_truncated":[path for path in limited if path=="top.clk" or path in own]}
        expected = inspect_handshake(get_parser=lambda _:parser,wave_path=parser.file_path,
            clock=member["clock"],ready=member["ready"],valid=member["valid"],payload=member["payload"],
            max_wait_cycles=2,_compact_sampling=True,_sampled=expected_samples)
        assert actual[id(member)][0] == expected
        if index and problem in ("payload_missing","valid_missing","payload_truncated"):
            assert actual[id(member)][0]["signal_errors"] == {}
            assert not actual[id(member)][0]["transition_data_truncated"]
            assert actual[id(member)][0]["reason"] is None
    gc.collect()
    assert all(reference() is None for reference in references)


@pytest.mark.parametrize("kind", ["events", "bytes"])
@pytest.mark.parametrize("stop_after", [1, 2, 3])
def test_anchor_page_admission_preserves_budget_stop_boundary(kind,stop_after):
    pages = [EventPage((_event(0,"0"),),predecessor=_event(-1000,"1"),next_time=1000),
             EventPage((_event(1000,"x"*8),_event(2000,None)),next_time=3000),
             EventPage((_event(3000,"1"*64),),complete=True)]
    prefix = [_cost(page) for page in pages[:stop_after]]
    budget = streaming.SweepBudget(max_events=sum(row[0] for row in prefix) if kind=="events" else 100000,
        max_bytes=sum(row[1] for row in prefix) if kind=="bytes" else 10000000)
    for index,page in enumerate(pages):
        if index==stop_after:
            with pytest.raises(streaming._WorkStop,match="sweep_event_budget" if kind=="events" else "sweep_byte_budget"):
                budget.check()
            break
        budget.check()
        budget.account(page)
    with pytest.raises(streaming._WorkStop,match="sweep_event_budget" if kind=="events" else "sweep_byte_budget"):
        budget.check()
    assert (budget.events,budget.nbytes)==(sum(row[0] for row in prefix),sum(row[1] for row in prefix))


@pytest.mark.parametrize("block_edges", [1, 3, 2048])
def test_budget_stop_mid_row_never_publishes_partially_sampled_columns(tmp_path,monkeypatch,block_edges):
    from src.cycle_query import sample_signals_on_edges

    parser = _protocol_parser(tmp_path)
    paths = ["top.valid0","top.ready","top.payload1"]
    expected = sample_signals_on_edges(parser,"top.clk",paths,compact=True,
        sample_phase="before",sample_offset_ps=0)
    budget = streaming.SweepBudget()
    original = parser._event_pages
    reads = {}

    @contextmanager
    def pages(path,*args,**kwargs):
        with original(path,*args,**kwargs) as reader:
            read = reader.read_page

            def read_page():
                page = read()
                reads[path] = reads.get(path,0)+1
                if path=="top.payload1" and reads[path]==2:
                    budget.reason = "sweep_event_budget"
                return page

            reader.read_page = read_page
            yield reader

    monkeypatch.setattr(parser,"_event_pages",pages)
    with streaming.iter_edge_blocks(parser,"top.clk",paths,0,-1,
            limits=streaming.StreamLimits(page_events=1,block_edges=block_edges),budget=budget) as stream:
        blocks = list(stream)
        assert sum(len(block["edge_times"]) for block in blocks)==1
        assert blocks[0]["edge_times"]==expected["edge_times"][:1]
        for path,column in blocks[0]["signal_columns"].items():
            assert column==expected["signal_columns"][path][:1]
        assert stream.status["sampling_gaps"]==["sweep_event_budget"]
        assert stream.status["transition_signals_truncated"]==["top.clk"]
