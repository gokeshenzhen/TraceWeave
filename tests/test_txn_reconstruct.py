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
