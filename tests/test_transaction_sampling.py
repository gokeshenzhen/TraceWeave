"""Independent accepted-edge tables, resource limits and native cleanup."""
from dataclasses import replace
from pathlib import Path
import threading

import anyio
import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.cycle_query import iter_sample_rows
from src.fsdb_parser import FSDBParser
from src.schemas import TxnReconstructResult
from src.transaction_sampling import (TransactionLimits, TransactionBudget,
    TransactionBudgetExceeded, bounded_parser, sample_identity)
from src.txn_reconstruct import reconstruct_transactions
from src.vcd_parser import VCDParser


WIDTHS = dict(clk=1, rv=1, rr=1, rid=8, cv=1, cr=1, cid=8,
              last=1, length=8, dv=1, dr=1, dl=1, data=8, rst=1)


def trace(tmp_path, rows, *, scale="1ps"):
    path = tmp_path / "transactions.vcd"
    text = [f"$timescale {scale} $end", "$scope module tb $end"]
    for key, width in WIDTHS.items():
        text.append(f"$var wire {width} {key} {key} $end")
    text += ["$upscope $end", "$enddefinitions $end"]
    for i, row in enumerate(rows):
        text += [f"#{10*i}", "0clk"]
        for key, width in WIDTHS.items():
            if key == "clk":
                continue
            value = row.get(key, 1 if key == "rst" else 0)
            bits = value * width if isinstance(value, str) else format(value, f"0{width}b")
            text.append(f"b{bits} {key}")
        text += [f"#{10*i+5}", "1clk"]
    text += [f"#{10*len(rows)}", "0clk"]
    path.write_text("\n".join(text) + "\n")
    return VCDParser(str(path))


def request(i=0, **kw):
    return dict(rv=1, rr=1, rid=i, **kw)


def response(i=0, **kw):
    return dict(cv=1, cr=1, cid=i, **kw)


def run(p, *, fifo=False, **kw):
    args = dict(get_parser=lambda _: p, wave_path=p.file_path, clock="tb.clk",
                req_valid="tb.rv", req_ready="tb.rr", cmp_valid="tb.cv", cmp_ready="tb.cr")
    if not fifo:
        args.update(req_id="tb.rid", cmp_id="tb.cid")
    result = reconstruct_transactions(**args, **kw)
    TxnReconstructResult.model_validate(result)
    return result


def test_many_ids_high_outstanding_and_small_display(tmp_path):
    count = 2048
    p = trace(tmp_path, [request(i % 64) for i in range(count)] +
              [response(63 - i % 64) for i in range(count)])
    r = run(p, max_transactions=1, timeout_cycles=count)
    assert (r["request_count"], r["completion_count"], r["matched_count"]) == (count, count, count)
    assert r["max_outstanding"] == count and r["max_outstanding_per_id"] == 32
    assert r["max_outstanding_id"] == 0 and r["reorder_count"] == 32*63
    assert r["outstanding_at_end"] == 0 and r["slow_count"] == count//2
    assert r["latency"] == dict(min_cycles=count-63, max_cycles=count+63,
                                median_cycles=count, mean_cycles=float(count))
    assert r["coverage_status"] == "complete"
    assert len(r["transactions"]) == 1 and r["transactions_truncated"]


def test_long_idle_and_fifo_are_counted_through_last_edge(tmp_path):
    rows = [request(), request()] + [{}] * 8192 + [response(), response()]
    r = run(trace(tmp_path, rows), fifo=True, max_transactions=1)
    assert r["matched_count"] == 2 and r["reorder_count"] == 0
    assert r["transactions"][0]["id"] is None
    assert r["latency"]["mean_cycles"] == 8194
    assert r["analysis"]["analyzed_samples"] == len(rows)


def test_late_burst_anomaly_and_tail_counts_do_not_depend_on_cap(tmp_path):
    rows = [request(length=1), response(last=0), response(last=1),
            request(1, length=3), response(1, last=1), request(2)]
    p = trace(tmp_path, rows)
    results = [run(p, req_len="tb.length", cmp_last="tb.last", max_transactions=n,
                   timeout_cycles=1) for n in (1, 64)]
    for r in results:
        assert r["matched_count"] == 2 and r["beat_count_mismatch_count"] == 1
        assert r["slow_count"] == 1 and r["unmatched_request_count"] == 1
        assert r["unmatched_requests"] == [dict(id=2, request_time_ps=55)]
        assert r["analysis"]["analyzed_samples"] == 6
    assert [x["beat_count"] for x in results[1]["transactions"]] == [2, 1]


def test_early_data_reset_and_completion_before_last(tmp_path):
    rows = [dict(dv=1, dr=1, data=17), dict(dv=1, dr=1, dl=1, data=34),
            request(3, length=1), response(3), request(4), dict(rst=0), response(4),
            request(5, length=1), dict(dv=1, dr=1, data=51), response(5),
            dict(dv=1, dr=1, dl=1, data=68)]
    r = run(trace(tmp_path, rows), req_len="tb.length", data_valid="tb.dv",
            data_ready="tb.dr", data_last="tb.dl", data_fields=["tb.data"],
            reset="tb.rst", capture_beats=True)
    assert r["matched_count"] == 2 and r["reset_clears"] == 1
    assert r["unmatched_completion_count"] == 1 and r["orphan_data_beats"] == 1
    a, b = r["transactions"]
    assert a["data_complete"] and a["beat_count"] == 2
    assert [x["fields"]["tb.data"] for x in a["data_beats"]] == ["0x11", "0x22"]
    assert not b["data_complete"] and b["beat_count_mismatch"]


def test_late_last_is_counted_but_missing_last_remains_pending(tmp_path):
    rows = [request(1, length=0), response(1, last=0), response(1, last=1),
            request(2, length=1), response(2, last=0), response(2, last=0)]
    r = run(trace(tmp_path, rows), req_len="tb.length", cmp_last="tb.last", max_transactions=1)
    assert r["request_count"] == 2 and r["matched_count"] == 1
    assert r["transactions"][0]["beat_count"] == 2
    assert r["beat_count_mismatch_count"] == 1 and r["outstanding_at_end"] == 1
    assert r["unmatched_requests"] == [dict(id=2, request_time_ps=35)]


def test_unknown_id_control_last_len_and_reset_are_not_exclusion(tmp_path):
    rows = [request("x"), response("z"), request(2, length="x"),
            response(2, last="x"), response(2, last=1),
            request(3), dict(rst="x"), response(3, last=1), dict(rv="x", rr=1)]
    r = run(trace(tmp_path, rows), req_len="tb.length", cmp_last="tb.last", reset="tb.rst")
    assert r["unknown_id_beats"] == 2 and r["unknown_control_cycles"] == 3
    assert r["matched_count"] == 1 and r["transactions"][0]["expected_beats"] is None
    assert r["beat_count_mismatch_count"] == 0
    assert r["unknown_history_clears"] == 1 and r["reset_clears"] == 0
    assert r["coverage_status"] == "partial"


def test_window_carry_in_and_missing_predecessor(tmp_path):
    p = trace(tmp_path, [request(7), {}, response(7), request(8)])
    r = run(p, start_ps=10)
    assert r["matched_count"] == 0 and r["unmatched_completion_count"] == 1
    assert r["outstanding_at_end"] == 1
    # No initial transition must remain unknown; no fallback to a later value.
    q = bounded_parser(p, TransactionBudget())
    assert q.get_value_at_time("tb.rv", 0)["value"] is None


@pytest.mark.parametrize("limit,reason", [(dict(pending=3), "pending_budget"),
                                        (dict(state_bytes=6000), "state_byte_budget")])
def test_required_state_budget_stops_without_erasing_frontier(tmp_path, limit, reason):
    p = trace(tmp_path, [request(i) for i in range(12)] + [response(i) for i in range(12)])
    r = run(p, max_transactions=1, _limits=replace(TransactionLimits(), **limit))
    assert r["coverage_status"] == "partial" and r["analysis"]["stop_reason"] == reason
    assert 0 < r["request_count"] < 12 and r["matched_count"] == 0
    assert r["outstanding_at_end"] == r["request_count"]
    assert r["unmatched_requests"][0]["request_time_ps"] == 5
    assert r["analysis"]["analyzed_samples"] == r["request_count"]


def test_early_data_budget_stops_and_keeps_orphans(tmp_path):
    p = trace(tmp_path, [dict(dv=1, dr=1, dl=1) for _ in range(10)] + [request()])
    r = run(p, data_valid="tb.dv", data_ready="tb.dr", data_last="tb.dl",
            _limits=replace(TransactionLimits(), early_data=3))
    assert r["analysis"]["stop_reason"] == "early_data_budget"
    assert r["orphan_data_beats"] == 3 and r["request_count"] == 0
    assert r["analysis"]["analyzed_samples"] == 3


@pytest.mark.parametrize("limit,reason", [(dict(events=5), "event_budget"),
    (dict(decoded_bytes=512), "decoded_byte_budget"), (dict(timeout_sec=0), "timeout"),
    (dict(samples=3), "sample_budget"), (dict(sample_cells=18), "sample_budget"),
    (dict(sample_cells=1), "sample_budget")])
def test_sampling_limits_never_claim_complete(tmp_path, limit, reason):
    p = trace(tmp_path, [request(), response()] * 12)
    r = run(p, _limits=replace(TransactionLimits(), **limit))
    assert r["coverage_status"] == "partial"
    assert r["analysis"]["stop_reason"] == reason
    assert r["matched_count"] < 12
    assert r["analysis"].get("analyzed_samples", 0) < 24


def test_late_read_budget_cannot_count_an_unread_opposite_channel(tmp_path):
    p = trace(tmp_path, [request(), response()] * 12)
    # Clock uses 49 events, then some alphabetically earlier completion fields.
    # Request controls have not been read when this shared budget expires.
    r = run(p, _limits=replace(TransactionLimits(), events=65))
    assert r["coverage_status"] == "partial" and r["analysis"]["stop_reason"] == "event_budget"
    assert r["request_count"] == r["completion_count"] == 0
    assert r["analysis"].get("analyzed_samples", 0) == 0


def test_legacy_lower_bound_marker_is_not_a_complete_stream(tmp_path, monkeypatch):
    p = trace(tmp_path, [request(), response()] * 4)
    monkeypatch.setattr(p, "_supports_event_pages", lambda: False)
    original = p.get_transitions
    def limited(path, *args, **kwargs):
        r = original(path, *args, **kwargs)
        if path == "tb.rv":
            r.update(transitions=r["transitions"][:2], transition_count_is_lower_bound=True)
        return r
    monkeypatch.setattr(p, "get_transitions", limited)
    r = run(p)
    assert r["coverage_status"] == "partial" and "transition_data_truncated" in r["gaps"]
    assert r["request_count"] == 1


def test_result_bytes_bound_projection_but_preserve_analysis(tmp_path):
    p = trace(tmp_path, [request(), response()] * 20)
    r = run(p, _limits=replace(TransactionLimits(), result_bytes=1))
    assert r["matched_count"] == 20 and r["latency"]["max_cycles"] == 1
    assert r["coverage_status"] == "complete"
    assert r["analysis"]["display_status"] == "partial"
    assert r["analysis"]["display_stop_reason"] == "result_byte_budget"
    assert r["analysis"]["result_bytes"] == 0 and r["transactions_truncated"]


def test_frozen_facts_cursor_and_larger_projection_have_honest_boundary(tmp_path, monkeypatch):
    from src import txn_reconstruct as txn
    from src.cursor_store import CursorStore
    p = trace(tmp_path, [request(), response()] * 3 + [request(7)])
    captured = []
    project = txn._project
    def save(facts, *args):
        captured.append(facts)
        return project(facts, *args)
    monkeypatch.setattr(txn, "_project", save)
    a = run(p, max_transactions=1, cursor_store=CursorStore(), cursor_name="one")
    b = run(p, max_transactions=3, cursor_store=CursorStore(), cursor_name="two")
    assert a["matched_count"] == b["matched_count"] == 3
    assert a["cursor"]["time_ps"] == b["cursor"]["time_ps"] == 65
    with pytest.raises(TypeError):
        captured[0].summary["matched_count"] = 0
    with pytest.raises(TypeError):
        captured[0].summary["transactions"][0]["id"] = 17
    later = project(captured[0], 3, None)
    assert later["matched_count"] == 3 and len(later["transactions"]) == 1
    assert later["analysis"]["display_stop_reason"] == "retained_fact_prefix"


def test_untyped_samples_cannot_cross_semantics(tmp_path):
    with pytest.raises(ValueError, match="source-bound"):
        run(trace(tmp_path, [request(), response()]), _sampled={"samples": []})


def test_tlul_reuse_rejects_clock_roles_file_and_reopen_changes(tmp_path, monkeypatch):
    from src import tlul
    from tests.test_tlul import fixture, run as tlrun
    p, fields = fixture(tmp_path, packed=True)
    captured = []
    original = tlul.reconstruct_transactions
    def save(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(tlul, "reconstruct_transactions", save)
    tlrun(p, fields)
    args = captured[0]
    for change in (dict(start_ps=1), dict(edge="negedge"), dict(reset_active_low=False),
                   dict(req_id=args["cmp_id"])):
        with pytest.raises(ValueError, match="incompatible transaction samples"):
            original(**{**args, **change})
    owner = args["get_parser"]("")
    p._scope_epoch = object()
    with pytest.raises(ValueError, match="incompatible transaction samples"):
        original(**args)
    Path(p.file_path).write_text(Path(p.file_path).read_text()+"\n")
    with pytest.raises(RuntimeError, match="changed"):
        sample_identity(owner, p.file_path, "tb.clk", [], 0, -1, "posedge")


def test_tlul_cannot_rebind_old_columns_after_a_consumer_reopens_parser(tmp_path, monkeypatch):
    from src import tlul
    from tests.test_tlul import fixture, run as tlrun
    p, fields = fixture(tmp_path, packed=True)
    original = tlul.inspect_handshake
    def reopen(**kwargs):
        result = original(**kwargs)
        p._scope_epoch = object()
        return result
    monkeypatch.setattr(tlul, "inspect_handshake", reopen)
    with pytest.raises(ValueError, match="incompatible transaction samples"):
        tlrun(p, fields)


def test_value_reuse_has_bounded_request_local_lifetime(tmp_path):
    p = trace(tmp_path, [request(), response()] * 1024)
    a, b = run(p), run(p)
    for r in (a, b):
        assert r["analysis"]["value_reuse_hits"] > 1024
        assert 0 < r["analysis"]["value_table_peak_entries"] <= 256
        assert 0 < r["analysis"]["value_table_peak_bytes"] <= 262144
    assert a["analysis"]["events_read"] == b["analysis"]["events_read"]
    assert a["analysis"]["value_reuse_misses"] == b["analysis"]["value_reuse_misses"]


def test_native_budget_cancel_timeout_and_failure_close_groups(monkeypatch):
    p = FSDBParser(str(Path(__file__).parent / "fixtures/scale_100fs.fsdb"))
    path = "scale_100fs_tb.addr[31:0]"
    try:
        p._open()
        assert p._supports_event_pages()
        original = p._lib.fsdb_event_page_v1
        opened, closed = [], []
        open_cursor, close_cursor = p._lib.fsdb_event_open_v1, p._lib.fsdb_event_close_v1
        def record_open(*args):
            opened.append(1)
            return open_cursor(*args)
        def record_close(*args):
            closed.append(1)
            return close_cursor(*args)
        monkeypatch.setattr(p._lib, "fsdb_event_open_v1", record_open)
        monkeypatch.setattr(p._lib, "fsdb_event_close_v1", record_close)
        for mode in ("budget", "cancel", "timeout", "error"):
            event = threading.Event()
            token = push_cancel_event(event)
            budget = TransactionBudget(replace(TransactionLimits(), events=1 if mode == "budget" else 1000))
            def page(*args):
                result = original(*args)
                if mode == "cancel":
                    event.set()
                elif mode == "timeout":
                    budget.started -= 60
                elif mode == "error":
                    raise RuntimeError("injected read failure")
                return result
            monkeypatch.setattr(p._lib, "fsdb_event_page_v1", page)
            try:
                if mode == "budget":
                    assert bounded_parser(p, budget).get_transitions(path)["truncated"]
                else:
                    cls = OperationCancelled if mode == "cancel" else TransactionBudgetExceeded if mode == "timeout" else RuntimeError
                    with pytest.raises(cls):
                        bounded_parser(p, budget).get_transitions(path)
            finally:
                pop_cancel_event(token)
                monkeypatch.setattr(p._lib, "fsdb_event_page_v1", original)
            assert not p._transition_group_active
            assert len(opened) == len(closed)
            assert p.get_value_at_time(path, 100100)["value"]["dec"] == 0xbbbb0000
    finally:
        p.close()


@pytest.mark.parametrize("scale", ["100fs", "1ns"])
@pytest.mark.parametrize("legacy", [False, True])
def test_native_scale_selection_and_legacy_transaction_semantics(scale, legacy, monkeypatch):
    p = FSDBParser(str(Path(__file__).parent / "fixtures" / f"scale_{scale}.fsdb"))
    root = f"scale_{scale}_tb"
    try:
        if legacy:
            monkeypatch.setattr(p, "_supports_event_pages", lambda: False)
        # Address bit 31 is already high before the 105 ns edge. Using the
        # rising clock itself as active-high valid tested post-edge values.
        valid = {"path": f"{root}.addr[31:0]", "bits": [31]}
        result = reconstruct_transactions(get_parser=lambda _: p, wave_path=p.file_path,
            clock=f"{root}.clk", req_valid=valid, req_ready=valid,
            cmp_valid=valid, cmp_ready=valid,
            req_id={"path": f"{root}.addr[31:0]", "bits": [31, 29, 30, 28]},
            cmp_id={"path": f"{root}.addr[31:0]", "bits": [31, 29, 30, 28]},
            start_ps=100000, end_ps=109000)
        TxnReconstructResult.model_validate(result)
        if legacy and scale == "100fs":
            assert result["coverage_status"] == "partial"
            assert result["matched_count"] == 0
            assert "legacy_sub_ps_order_unavailable" in result["gaps"]
            assert result["analysis"]["legacy_reads"] > 0
            assert result["analysis"]["events_read"] > 0
            assert not p._transition_group_active
            return
        assert result["coverage_status"] == "complete"
        assert result["matched_count"] == 1
        txn = result["transactions"][0]
        assert txn["latency_cycles"] == 0 and txn["request_time_ps"] == 105000
        # C=1100 -> ordered [31,29,30,28]=1010. B=1011 -> 1101.
        assert txn["id"] == (10 if scale == "100fs" else 13)
        assert bool(result["analysis"]["legacy_reads"]) == legacy
        assert not p._transition_group_active
    finally:
        p.close()


def test_compact_iterator_borrows_values_without_per_edge_copy():
    zero = dict(bin="0", dec=0, hex="0x0")
    sampled = dict(edge_times=[5, 15], signal_columns={"v": [zero, zero]})
    rows = iter_sample_rows(sampled)
    _, first = next(rows)
    assert first.get("v") is zero
    _, second = next(rows)
    assert first is second and second.get("v") is zero


@pytest.mark.anyio
async def test_cancelled_transaction_worker_releases_native_group(monkeypatch):
    import server
    p = FSDBParser(str(Path(__file__).parent / "fixtures/scale_100fs.fsdb"))
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    try:
        p._open()
        original_page, original_end = p._lib.fsdb_event_page_v1, p._lib.fsdb_end_transition_group
        def page(*args):
            result = original_page(*args)
            started.set()
            assert release.wait(3), "test did not release native read"
            return result
        def end(*args):
            result = original_end(*args)
            closed.set()
            return result
        monkeypatch.setattr(p._lib, "fsdb_event_page_v1", page)
        monkeypatch.setattr(p._lib, "fsdb_end_transition_group", end)
        monkeypatch.setattr(server, "_get_parser", lambda _: p)
        args = dict(wave_path=p.file_path, clock="scale_100fs_tb.clk",
                    req_valid="scale_100fs_tb.clk", req_ready="scale_100fs_tb.clk",
                    cmp_valid="scale_100fs_tb.clk", cmp_ready="scale_100fs_tb.clk",
                    active_high=False)  # Clock is low strictly before posedge.
        async with anyio.create_task_group() as tasks:
            async def call():
                await server._dispatch("reconstruct_transactions", args)
            tasks.start_soon(call)
            assert await anyio.to_thread.run_sync(lambda: started.wait(2))
            assert p._transition_group_active
            with anyio.fail_after(1):
                await server._dispatch("cursor_list", {})
            tasks.cancel_scope.cancel()
            release.set()
        assert await anyio.to_thread.run_sync(lambda: closed.wait(2))
        monkeypatch.setattr(p._lib, "fsdb_event_page_v1", original_page)
        # The next public transaction call must run normally after cancellation.
        r = await server._dispatch("reconstruct_transactions", args)
        assert r.matched_count == 11
        assert not p._transition_group_active
    finally:
        release.set()
        p.close()
