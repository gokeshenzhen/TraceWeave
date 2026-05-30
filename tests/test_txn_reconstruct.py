"""Tests for reconstruct_transactions (P2 id-correlated transaction layer)."""

from __future__ import annotations

import itertools
from pathlib import Path

from src.cursor_store import CursorStore
from src.txn_reconstruct import reconstruct_transactions
from src.vcd_parser import VCDParser
import src.schemas as schemas

_seq = itertools.count()

# (id_char, name, width)
_AXI_RD = [("!", "clk", 1), ("a", "arvalid", 1), ("b", "arready", 1), ("c", "arid", 2),
           ("d", "rvalid", 1), ("e", "rready", 1), ("f", "rid", 2), ("g", "rlast", 1)]


def _build(cycles: list[dict], sig: list[tuple]) -> str:
    hdr = ["$timescale 1ps $end", "$scope module top $end"]
    hdr += [f"$var wire {w} {i} {n} $end" for i, n, w in sig]
    hdr += ["$upscope $end", "$enddefinitions $end"]
    nid = {n: i for i, n, w in sig}
    wid = {n: w for i, n, w in sig}
    body: list[str] = []
    for k, c in enumerate(cycles):
        body.append(f"#{10 * k}")
        body.append(f"0{nid['clk']}")
        for _i, n, _w in sig:
            if n == "clk":
                continue
            v = c.get(n, 0)
            if wid[n] == 1:
                body.append(f"{int(v)}{nid[n]}")
            else:
                body.append(f"b{int(v):0{wid[n]}b} {nid[n]}")
        body.append(f"#{10 * k + 5}")
        body.append(f"1{nid['clk']}")
    return "\n".join(hdr + body) + "\n"


def _write(tmp_path: Path, cycles, sig) -> str:
    p = tmp_path / f"txn{next(_seq)}.vcd"
    p.write_text(_build(cycles, sig))
    return str(p)


def _rd(tmp_path, cycles, *, store=None, **kw):
    wave = _write(tmp_path, cycles, _AXI_RD)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.arvalid", req_ready="top.arready", req_id="top.arid",
        cmp_valid="top.rvalid", cmp_ready="top.rready", cmp_id="top.rid",
        cmp_last="top.rlast", cursor_store=store, **kw,
    )
    schemas.TxnReconstructResult.model_validate(r)
    return r


# AR id1@c1, AR id2@c2, R id2 burst(2 beats)@c4-5 (out of order), R id1@c7, AR id3@c8 (never)
_OOO = [
    {}, {"arvalid": 1, "arready": 1, "arid": 1}, {"arvalid": 1, "arready": 1, "arid": 2}, {},
    {"rvalid": 1, "rready": 1, "rid": 2, "rlast": 0},
    {"rvalid": 1, "rready": 1, "rid": 2, "rlast": 1},
    {}, {"rvalid": 1, "rready": 1, "rid": 1, "rlast": 1},
    {"arvalid": 1, "arready": 1, "arid": 3}, {},
]


def test_axi_read_ooo_burst_and_hang(tmp_path):
    store = CursorStore()
    r = _rd(tmp_path, _OOO, store=store)
    assert (r["request_count"], r["completion_count"], r["matched_count"]) == (3, 2, 2)
    assert r["outstanding_at_end"] == 1          # id3 never completes
    assert r["max_outstanding"] == 2
    assert r["reorder_count"] == 1               # id2 completed before id1
    assert [tx["beat_count"] for tx in r["transactions"]] == [2, 1]
    assert r["unmatched_requests"][0]["id"] == 3
    assert r["cursor"] is not None               # anchored at the stuck request
    assert len(store.list()) == 1


def test_latency_and_outstanding_at_start(tmp_path):
    r = _rd(tmp_path, _OOO)
    by_id = {tx["id"]: tx for tx in r["transactions"]}
    assert by_id[2]["latency_cycles"] == 3       # AR@c2 -> Rlast@c5
    assert by_id[1]["latency_cycles"] == 6       # AR@c1 -> Rlast@c7
    assert by_id[2]["outstanding_at_start"] == 2  # both id1,id2 in flight
    assert r["latency"]["max_cycles"] == 6


def test_per_id_outstanding_peak(tmp_path):
    # same id issued twice back-to-back before any completion
    cyc = [{}, {"arvalid": 1, "arready": 1, "arid": 1},
           {"arvalid": 1, "arready": 1, "arid": 1}, {},
           {"rvalid": 1, "rready": 1, "rid": 1, "rlast": 1},
           {"rvalid": 1, "rready": 1, "rid": 1, "rlast": 1}, {}]
    r = _rd(tmp_path, cyc)
    assert r["max_outstanding_per_id"] == 2
    assert r["max_outstanding_id"] == 1


def test_timeout_slow_count(tmp_path):
    r = _rd(tmp_path, _OOO, timeout_cycles=4)
    assert r["timeout_cycles"] == 4
    assert r["slow_count"] == 1                  # id1 latency 6 > 4; id2 latency 3
    assert any("latency >" in w for w in r["warnings"])


def test_cmp_fields_captured(tmp_path):
    sig = _AXI_RD + [("h", "rresp", 2)]
    cyc = [{}, {"arvalid": 1, "arready": 1, "arid": 1},
           {"rvalid": 1, "rready": 1, "rid": 1, "rlast": 1, "rresp": 2}, {}]
    wave = _write(tmp_path, cyc, sig)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.arvalid", req_ready="top.arready", req_id="top.arid",
        cmp_valid="top.rvalid", cmp_ready="top.rready", cmp_id="top.rid",
        cmp_last="top.rlast", cmp_fields=["top.rresp"],
    )
    schemas.TxnReconstructResult.model_validate(r)
    assert r["transactions"][0]["cmp_fields"]["top.rresp"] in ("0x2", "0b10", "2")


def test_axi_write_style_no_cmp_last(tmp_path):
    # AW + B (single-beat completion, no last). reuse arid/rid as awid/bid.
    cyc = [{}, {"arvalid": 1, "arready": 1, "arid": 1}, {},
           {"rvalid": 1, "rready": 1, "rid": 1}, {}]
    wave = _write(tmp_path, cyc, _AXI_RD)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.arvalid", req_ready="top.arready", req_id="top.arid",
        cmp_valid="top.rvalid", cmp_ready="top.rready", cmp_id="top.rid",
    )  # no cmp_last -> every completion beat is a txn
    schemas.TxnReconstructResult.model_validate(r)
    assert r["matched_count"] == 1
    assert r["transactions"][0]["beat_count"] == 1


def test_unmatched_completion(tmp_path):
    # R for id1 with no preceding AR
    cyc = [{}, {"rvalid": 1, "rready": 1, "rid": 1, "rlast": 1}, {}]
    r = _rd(tmp_path, cyc)
    assert r["completion_count"] == 1
    assert r["matched_count"] == 0
    assert r["unmatched_completion_count"] == 1
    assert any("no matching open request" in w for w in r["warnings"])


def test_missing_control_signal_is_loud(tmp_path):
    wave = _write(tmp_path, _OOO, _AXI_RD)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.arvalid", req_ready="top.arready", req_id="top.nope",
        cmp_valid="top.rvalid", cmp_ready="top.rready", cmp_id="top.rid",
    )
    schemas.TxnReconstructResult.model_validate(r)
    assert r["reason"] and "not found" in r["reason"]


# --- AXI WRITE channel (AW + W data + B), reset, per-beat capture ----------

_AXI_WR = [("!", "clk", 1), ("R", "rst_n", 1),
           ("a", "awvalid", 1), ("b", "awready", 1), ("c", "awid", 2),
           ("d", "wvalid", 1), ("e", "wready", 1), ("f", "wlast", 1),
           ("g", "wdata", 8), ("h", "wstrb", 2),
           ("i", "bvalid", 1), ("j", "bready", 1), ("k", "bid", 2)]


def _wr(tmp_path, cycles, *, store=None, **kw):
    wave = _write(tmp_path, cycles, _AXI_WR)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.awvalid", req_ready="top.awready", req_id="top.awid",
        cmp_valid="top.bvalid", cmp_ready="top.bready", cmp_id="top.bid",
        data_valid="top.wvalid", data_ready="top.wready", data_last="top.wlast",
        data_fields=["top.wdata", "top.wstrb"],
        reset="top.rst_n", reset_active_low=True, cursor_store=store, **kw,
    )
    schemas.TxnReconstructResult.model_validate(r)
    return r


def test_write_data_channel_reconstructs_burst(tmp_path):
    cyc = [
        {"rst_n": 1, "awvalid": 1, "awready": 1, "awid": 1},
        {"rst_n": 1, "wvalid": 1, "wready": 1, "wlast": 0, "wdata": 0x11, "wstrb": 3},
        {"rst_n": 1, "wvalid": 1, "wready": 1, "wlast": 1, "wdata": 0x22, "wstrb": 3},
        {"rst_n": 1, "bvalid": 1, "bready": 1, "bid": 1},
    ]
    r = _wr(tmp_path, cyc, capture_beats=True)
    assert r["matched_count"] == 1
    tx = r["transactions"][0]
    assert tx["beat_count"] == 2
    assert tx["data_complete"] is True
    assert [b["fields"]["top.wdata"] for b in tx["data_beats"]] == ["0x11", "0x22"]


def test_capture_beats_off_by_default(tmp_path):
    cyc = [
        {"rst_n": 1, "awvalid": 1, "awready": 1, "awid": 1},
        {"rst_n": 1, "wvalid": 1, "wready": 1, "wlast": 1, "wdata": 0x33, "wstrb": 3},
        {"rst_n": 1, "bvalid": 1, "bready": 1, "bid": 1},
    ]
    r = _wr(tmp_path, cyc)  # capture_beats not set
    assert r["transactions"][0]["beat_count"] == 1
    assert r["transactions"][0]["data_beats"] == []


def test_reset_clears_inflight_no_phantom_hang(tmp_path):
    cyc = [
        {"rst_n": 1, "awvalid": 1, "awready": 1, "awid": 1},  # AW id1, never completes
        {"rst_n": 1, "wvalid": 1, "wready": 1, "wlast": 1, "wdata": 0x44, "wstrb": 3},
        {"rst_n": 0},  # reset -> wipe in-flight id1
        {"rst_n": 1},
    ]
    r = _wr(tmp_path, cyc)
    assert r["reset_clears"] == 1
    assert r["outstanding_at_end"] == 0   # id1 cleared by reset, not a phantom hang
    assert r["matched_count"] == 0


def test_write_data_before_address_attaches_in_order(tmp_path):
    # W beats can arrive before AW; they buffer and attach when AW arrives.
    cyc = [
        {"rst_n": 1, "wvalid": 1, "wready": 1, "wlast": 1, "wdata": 0x55, "wstrb": 3},
        {"rst_n": 1, "awvalid": 1, "awready": 1, "awid": 2},
        {"rst_n": 1, "bvalid": 1, "bready": 1, "bid": 2},
    ]
    r = _wr(tmp_path, cyc, capture_beats=True)
    assert r["matched_count"] == 1
    tx = r["transactions"][0]
    assert tx["beat_count"] == 1
    assert tx["data_beats"][0]["fields"]["top.wdata"] == "0x55"


def test_data_channel_needs_both_valid_and_ready(tmp_path):
    wave = _write(tmp_path, [{"rst_n": 1}], _AXI_WR)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.awvalid", req_ready="top.awready", req_id="top.awid",
        cmp_valid="top.bvalid", cmp_ready="top.bready", cmp_id="top.bid",
        data_valid="top.wvalid",  # data_ready missing
    )
    schemas.TxnReconstructResult.model_validate(r)
    assert r["reason"] and "data_valid and data_ready" in r["reason"]


# --- no-id (in-order FIFO) mode: AXI-Lite / APB / unindexed streams ---------

def _noid(tmp_path, cycles, sig, *, store=None, **kw):
    wave = _write(tmp_path, cycles, sig)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.av", req_ready="top.ar",
        cmp_valid="top.bv", cmp_ready="top.br",
        cursor_store=store, **kw,  # NO req_id / cmp_id
    )
    schemas.TxnReconstructResult.model_validate(r)
    return r


_LITE = [("!", "clk", 1), ("p", "av", 1), ("q", "ar", 1), ("x", "bv", 1), ("y", "br", 1)]


def test_no_id_fifo_pairs_in_order(tmp_path):
    # 3 requests, 3 completions, paired by issue order; id reported as null.
    cyc = [
        {"av": 1, "ar": 1},                 # req0
        {"av": 1, "ar": 1},                 # req1
        {"bv": 1, "br": 1},                 # cmp -> req0
        {"av": 1, "ar": 1},                 # req2
        {"bv": 1, "br": 1},                 # cmp -> req1
        {"bv": 1, "br": 1},                 # cmp -> req2
    ]
    r = _noid(tmp_path, cyc, _LITE)
    assert r["request_count"] == 3
    assert r["matched_count"] == 3
    assert r["outstanding_at_end"] == 0
    assert all(tx["id"] is None for tx in r["transactions"])
    # FIFO: completions pair to requests in issue order, so request times are
    # non-decreasing down the (completion-ordered) transaction list.
    req_times = [tx["request_time_ps"] for tx in r["transactions"]]
    assert req_times == sorted(req_times)
    assert len(set(req_times)) == 3
    assert r["unknown_id_beats"] == 0
    assert r["max_outstanding_per_id"] == 0   # meaningless without ids
    assert r["max_outstanding_id"] is None


def test_no_id_hang_reports_null_id(tmp_path):
    cyc = [{"av": 1, "ar": 1}, {"av": 1, "ar": 1}, {"bv": 1, "br": 1}]  # 2 req, 1 cmp
    r = _noid(tmp_path, cyc, _LITE)
    assert r["matched_count"] == 1
    assert r["outstanding_at_end"] == 1
    assert r["unmatched_requests"][0]["id"] is None


def test_one_sided_id_is_loud(tmp_path):
    wave = _write(tmp_path, [{"av": 1}], _LITE)
    r = reconstruct_transactions(
        get_parser=lambda _w: VCDParser(wave), wave_path=wave, clock="top.clk",
        req_valid="top.av", req_ready="top.ar", req_id="top.av",  # only req_id
        cmp_valid="top.bv", cmp_ready="top.br",
    )
    schemas.TxnReconstructResult.model_validate(r)
    assert r["reason"] and "both req_id and cmp_id, or neither" in r["reason"]
