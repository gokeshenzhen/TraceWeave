"""reconstruct_transactions — id-correlated request/response transaction layer (P2).

The most durable perception@scale primitive: walk a request handshake channel and
a completion handshake channel over the whole window, correlate accepted beats by
an id field, and emit per-transaction records (latency) plus aggregate facts
(outstanding curve, ordering). The LLM knows the protocol semantics; it cannot
apply them to millions of edges or hold thousands of outstanding txns in context —
that is exactly the split this tool serves.

One GENERIC core, not a tool per protocol:
  - AXI READ : req=AR -> cmp=R (cmp_last=rlast), id=arid/rid.
  - AXI WRITE: req=AW -> cmp=B, id=awid/bid, PLUS an optional unindexed DATA channel
    (W: data_valid=wvalid, data_ready=wready, data_last=wlast, data_fields=[wdata,
    wstrb]). AXI's W channel carries no id; beats attach in order to the oldest
    request whose data is not yet complete (matching real interconnect behaviour).
  - any id'd req/resp, CHI-like.
  - AHB/APB (no id, phase-based) are intentionally out of scope — use
    verify_window / inspect_handshake.

Read vs write outstanding is by DESIGN two calls (one per direction); that keeps the
core generic instead of hard-coding AXI's five channels.

Faithful: a latency distribution (min/median/max/mean), not an "outlier" verdict;
unmatched requests/completions surfaced loudly (the hang signature); an asserted
``reset`` clears in-flight state so a transaction straddling reset is not reported
as a phantom hang. An optional ``req_len`` (AxLEN) compares each txn's observed
beat count to ``req_len+1`` and flags a mismatch (early/late LAST, dropped/extra
beat) per-txn + as an aggregate — x/z len is not checked, so never a false
positive. Reads existing waveforms only.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Any, Callable

from .cursor_store import CursorStore
from .cycle_query import sample_signals_on_edges
from .verify_condition import _hs_repr, _hs_truth, _resolve_signal_path

DEFAULT_MAX_TRANSACTIONS = 256
_MAX_UNMATCHED_SHOWN = 32
# Single internal bucket key for no-id (in-order FIFO) mode; txn id is reported
# as None to the caller, never this sentinel.
_FIFO_KEY = 0


def reconstruct_transactions(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    clock: str,
    req_valid: str,
    req_ready: str,
    req_id: str | None = None,
    cmp_valid: str,
    cmp_ready: str,
    cmp_id: str | None = None,
    req_fields: list[str] | None = None,
    req_len: str | None = None,
    cmp_last: str | None = None,
    cmp_fields: list[str] | None = None,
    data_valid: str | None = None,
    data_ready: str | None = None,
    data_last: str | None = None,
    data_fields: list[str] | None = None,
    reset: str | None = None,
    reset_active_low: bool = True,
    capture_beats: bool = False,
    edge: str = "posedge",
    start_ps: int = 0,
    end_ps: int = -1,
    active_high: bool = True,
    timeout_cycles: int | None = None,
    max_transactions: int = DEFAULT_MAX_TRANSACTIONS,
    cursor_store: CursorStore | None = None,
    cursor_name: str | None = None,
    cursor_note: str | None = None,
) -> dict[str, Any]:
    parser = get_parser(wave_path)
    req_fields = list(req_fields or [])
    cmp_fields = list(cmp_fields or [])
    data_fields = list(data_fields or [])

    result: dict[str, Any] = _empty_result(wave_path, clock, edge, start_ps, end_ps)
    warnings: list[str] = result["warnings"]

    # A data channel needs at least valid+ready to be usable.
    has_data = data_valid is not None and data_ready is not None
    if (data_valid is not None) ^ (data_ready is not None):
        result["reason"] = "data channel needs both data_valid and data_ready"
        return result

    # id correlation needs BOTH ids or NEITHER. With neither, requests and
    # completions are paired in pure FIFO issue order (single in-order stream:
    # AXI-Lite, APB, any req/resp without an id tag); txn id is reported as null.
    has_id = req_id is not None and cmp_id is not None
    if (req_id is not None) ^ (cmp_id is not None):
        result["reason"] = (
            "id correlation needs both req_id and cmp_id, or neither "
            "(neither = in-order FIFO pairing for an unindexed stream)"
        )
        return result

    # Resolve every signal (auto-correct bus bit-range; loud on miss).
    clock_r = _resolve_signal_path(parser, clock)
    spec = {
        "req_valid": req_valid, "req_ready": req_ready,
        "cmp_valid": cmp_valid, "cmp_ready": cmp_ready,
    }
    if has_id:
        spec["req_id"] = req_id
        spec["cmp_id"] = cmp_id
    if req_len is not None:
        # AxLEN (AWLEN/ARLEN): expected data/response beats = req_len + 1. Captured
        # at request, compared against the observed beat count at completion.
        spec["req_len"] = req_len
    if cmp_last is not None:
        spec["cmp_last"] = cmp_last
    if has_data:
        spec["data_valid"] = data_valid
        spec["data_ready"] = data_ready
        if data_last is not None:
            spec["data_last"] = data_last
    if reset is not None:
        spec["reset"] = reset
    resolved: dict[str, str] = {role: _resolve_signal_path(parser, p) for role, p in spec.items()}
    req_fields_r = [_resolve_signal_path(parser, p) for p in req_fields]
    cmp_fields_r = [_resolve_signal_path(parser, p) for p in cmp_fields]
    data_fields_r = [_resolve_signal_path(parser, p) for p in data_fields]

    result["clock"] = clock_r

    # Deterministic existence check — do NOT rely on the sampler's signal_errors:
    # sample_signals_on_edges silently omits some unresolved signals, which would
    # make a typo'd control signal look like an all-idle channel (0 txns, no
    # reason). A missing control signal makes the whole reconstruction
    # meaningless, so be loud.
    missing_ctrl = sorted({p for p in ([clock_r] + list(resolved.values()))
                           if not _exists(parser, p)})
    if missing_ctrl:
        result["reason"] = (
            "control signal(s) not found: " + ", ".join(missing_ctrl)
            + " — FSDB buses usually need an explicit bit range (e.g. 'top.arid[3:0]')."
        )
        return result

    keep_req_fields = [p for p in req_fields_r if _exists(parser, p)]
    keep_cmp_fields = [p for p in cmp_fields_r if _exists(parser, p)]
    keep_data_fields = [p for p in data_fields_r if _exists(parser, p)]
    unresolved_fields = [p for p in (req_fields_r + cmp_fields_r + data_fields_r)
                         if not _exists(parser, p)]
    if unresolved_fields:
        warnings.append(
            "field signal(s) not captured (unresolved): "
            + ", ".join(sorted(set(unresolved_fields)))
        )

    all_signals = sorted(
        set(resolved.values()) | set(keep_req_fields)
        | set(keep_cmp_fields) | set(keep_data_fields)
    )
    sampled = sample_signals_on_edges(
        parser, clock_r, all_signals, start_ps=start_ps, end_ps=end_ps, edge=edge,
    )
    signal_errors = {k: str(v) for k, v in sampled.get("signal_errors", {}).items()}
    result["signal_errors"] = signal_errors

    samples = sampled.get("samples", [])
    if not samples:
        result["reason"] = f"no {edge} edges of clock {clock_r!r} in the window"
        return result

    cfg = _WalkCfg(
        resolved=resolved,
        active_high=active_high,
        has_id=has_id,
        has_last="cmp_last" in resolved,
        has_data=has_data,
        has_data_last="data_last" in resolved,
        reset_active_low=reset_active_low,
        capture_beats=capture_beats,
        req_fields=keep_req_fields,
        cmp_fields=keep_cmp_fields,
        data_fields=keep_data_fields,
    )
    # _walk returns its private accumulators as locals — they are NEVER stored on
    # ``result`` so no early-return path can leak a private key into the schema.
    acc = _walk(result, samples, cfg)
    _finalize(result, acc, max_transactions, timeout_cycles, warnings)
    _attach_cursor(result, cursor_store, wave_path, edge, cursor_name, cursor_note)
    return result


class _WalkCfg:
    __slots__ = (
        "resolved", "active_high", "has_id", "has_last", "has_data", "has_data_last",
        "reset_active_low", "capture_beats", "req_fields", "cmp_fields", "data_fields",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])


def _walk(result, samples, cfg: _WalkCfg) -> dict[str, Any]:
    """Walk the sampled edges; fill aggregate facts onto ``result`` and return a
    dict of private accumulators (latencies, unmatched lists) for _finalize."""
    r = cfg.resolved
    rv, rr = r["req_valid"], r["req_ready"]
    cv, cr = r["cmp_valid"], r["cmp_ready"]
    rid, cid = r.get("req_id"), r.get("cmp_id")  # None in no-id (FIFO) mode
    len_sig = r.get("req_len")                    # AxLEN; None if not supplied
    clast = r.get("cmp_last")
    dv, dr_, dlast = r.get("data_valid"), r.get("data_ready"), r.get("data_last")
    rst = r.get("reset")

    pending: dict[int, list[dict]] = {}   # id -> FIFO of open requests
    pending_order: list[dict] = []        # all open requests, issue order
    transactions: list[dict] = result["transactions"]
    unmatched_completions: list[dict] = []
    latencies: list[int] = []
    data_buffer: list[dict] = []          # unindexed data beats awaiting a request

    seq = 0
    outstanding = 0
    max_out = 0
    max_out_time = None
    unknown_id_beats = 0
    req_count = 0
    cmp_count = 0
    reorder = 0
    reset_clears = 0
    orphan_data_beats = 0
    beat_count_mismatch = 0
    per_id_out: dict[int, int] = {}
    per_id_peak: dict[int, int] = {}
    beat_ctr: dict[int, int] = {}         # id -> cmp beats since last completion

    def _attach_data_beats():
        """Drain buffered data beats in order into the oldest data-incomplete req."""
        while data_buffer:
            target = next((p for p in pending_order if not p["data_complete"]), None)
            if target is None:
                break
            beat = data_buffer.pop(0)
            if not target["data_beats_meta"]:
                target["first_data_time"] = beat["time_ps"]
            target["data_beat_count"] += 1
            target["last_data_time"] = beat["time_ps"]
            if cfg.capture_beats:
                target["data_beats_meta"].append(beat)
            if beat["last"]:
                target["data_complete"] = True

    for idx, s in enumerate(samples):
        sig = s["signals"]
        t = s["time_ps"]

        # --- reset: clear all in-flight state (correctness) -------------------
        if rst is not None:
            if _hs_truth(sig.get(rst), active_high=not cfg.reset_active_low) is True:
                if pending_order or data_buffer:
                    reset_clears += 1
                pending.clear()
                pending_order.clear()
                data_buffer.clear()
                beat_ctr.clear()
                per_id_out.clear()
                outstanding = 0
                continue

        # --- request accepted -------------------------------------------------
        if _ok(sig.get(rv), cfg.active_high) and _ok(sig.get(rr), cfg.active_high):
            # In id mode the bucket key is the decoded id (x/z id = uncorrelatable);
            # in FIFO mode every accepted beat shares one bucket and reports id=None.
            rid_val = _dec(sig.get(rid)) if cfg.has_id else _FIFO_KEY
            if cfg.has_id and rid_val is None:
                unknown_id_beats += 1
            else:
                req_count += 1
                outstanding += 1
                per_id_out[rid_val] = per_id_out.get(rid_val, 0) + 1
                per_id_peak[rid_val] = max(per_id_peak.get(rid_val, 0), per_id_out[rid_val])
                rec = {
                    "id": rid_val if cfg.has_id else None,
                    "seq": seq, "idx": idx, "time_ps": t,
                    "outstanding_at_start": outstanding,
                    # AxLEN decoded at request; None on x/z (no check, never a FP).
                    "len": _dec(sig.get(len_sig)) if len_sig is not None else None,
                    "req_fields": {f: _hs_repr(sig.get(f)) for f in cfg.req_fields},
                    "data_complete": not cfg.has_data,
                    "data_beat_count": 0,
                    "data_beats_meta": [],
                    "first_data_time": None,
                    "last_data_time": None,
                }
                pending.setdefault(rid_val, []).append(rec)
                pending_order.append(rec)
                seq += 1
                if outstanding > max_out:
                    max_out, max_out_time = outstanding, t
                if cfg.has_data:
                    _attach_data_beats()

        # --- data channel beat accepted (write W; unindexed, in-order) --------
        if cfg.has_data and _ok(sig.get(dv), cfg.active_high) and _ok(sig.get(dr_), cfg.active_high):
            is_last = True if not cfg.has_data_last else _ok(sig.get(dlast), cfg.active_high)
            data_buffer.append({
                "time_ps": t, "last": is_last,
                "fields": {f: _hs_repr(sig.get(f)) for f in cfg.data_fields},
            })
            # Beats that can't attach yet (W before its AW) stay buffered and
            # attach when the AW arrives; only counted as orphans at end-of-window.
            _attach_data_beats()

        # --- completion channel beat accepted ---------------------------------
        if _ok(sig.get(cv), cfg.active_high) and _ok(sig.get(cr), cfg.active_high):
            cid_val = _dec(sig.get(cid)) if cfg.has_id else _FIFO_KEY
            if cfg.has_id and cid_val is None:
                unknown_id_beats += 1
            else:
                beat_ctr[cid_val] = beat_ctr.get(cid_val, 0) + 1
                is_completion = True if not cfg.has_last else _ok(sig.get(clast), cfg.active_high)
                if is_completion:
                    cmp_count += 1
                    n_cmp_beats = beat_ctr.pop(cid_val, 1)
                    queue = pending.get(cid_val)
                    if queue:
                        req = queue.pop(0)
                        try:
                            pending_order.remove(req)
                        except ValueError:
                            pass
                        outstanding -= 1
                        per_id_out[cid_val] = max(0, per_id_out.get(cid_val, 1) - 1)
                        if any(p["seq"] < req["seq"] for p in pending_order):
                            reorder += 1
                        lat = idx - req["idx"]
                        latencies.append(lat)
                        beats = req["data_beat_count"] if cfg.has_data else n_cmp_beats
                        # Beat-count vs AxLEN: a correct burst has exactly len+1
                        # beats. A mismatch is a real protocol error (early/late
                        # LAST, dropped/extra beat). x/z len -> expected None -> no
                        # check (never a false positive).
                        exp_beats = req["len"] + 1 if req["len"] is not None else None
                        is_mismatch = exp_beats is not None and beats != exp_beats
                        if is_mismatch:
                            beat_count_mismatch += 1
                        transactions.append({
                            "id": cid_val if cfg.has_id else None,
                            "request_time_ps": req["time_ps"],
                            "completion_time_ps": t,
                            "latency_cycles": lat,
                            "latency_ps": t - req["time_ps"],
                            "beat_count": beats,
                            "expected_beats": exp_beats,
                            "beat_count_mismatch": is_mismatch,
                            "outstanding_at_start": req["outstanding_at_start"],
                            "data_complete": req["data_complete"],
                            "req_fields": req["req_fields"],
                            "cmp_fields": {f: _hs_repr(sig.get(f)) for f in cfg.cmp_fields},
                            # always present (empty unless capture_beats)
                            "data_beats": req["data_beats_meta"] if cfg.capture_beats else [],
                        })
                    else:
                        unmatched_completions.append(
                            {"id": cid_val if cfg.has_id else None, "completion_time_ps": t}
                        )

    unmatched_requests = [
        {"id": rq["id"], "request_time_ps": rq["time_ps"]} for rq in pending_order
    ]
    unmatched_requests.sort(key=lambda x: x["request_time_ps"])
    # data beats still buffered at end never found a request to attach to.
    orphan_data_beats += len(data_buffer)

    result.update({
        "request_count": req_count,
        "completion_count": cmp_count,
        "matched_count": len(transactions),
        "outstanding_at_end": len(unmatched_requests),
        "max_outstanding": max_out,
        "max_outstanding_time_ps": max_out_time,
        # per-id stats are meaningless without ids; report 0/None in FIFO mode.
        "max_outstanding_per_id": (max(per_id_peak.values()) if (cfg.has_id and per_id_peak) else 0),
        "max_outstanding_id": (max(per_id_peak, key=per_id_peak.get) if (cfg.has_id and per_id_peak) else None),
        "reorder_count": reorder,
        "unknown_id_beats": unknown_id_beats,
        "reset_clears": reset_clears,
        "orphan_data_beats": orphan_data_beats,
        "beat_count_mismatch_count": beat_count_mismatch,
    })
    return {
        "latencies": latencies,
        "unmatched_requests": unmatched_requests,
        "unmatched_completions": unmatched_completions,
    }


def _finalize(result, acc, max_transactions, timeout_cycles, warnings) -> None:
    lats = acc["latencies"]
    if lats:
        result["latency"] = {
            "min_cycles": min(lats),
            "median_cycles": int(median(lats)),
            "max_cycles": max(lats),
            "mean_cycles": round(mean(lats), 2),
        }

    # timeout_cycles is a caller-supplied threshold, echoed back; slow_count is a
    # FACT (how many completed txns exceeded it), not an "outlier" verdict.
    if timeout_cycles is not None:
        result["timeout_cycles"] = int(timeout_cycles)
        result["slow_count"] = sum(1 for x in lats if x > timeout_cycles)
        if result["slow_count"]:
            warnings.append(
                f"{result['slow_count']} transaction(s) had latency > {timeout_cycles} cycles."
            )

    txns = result["transactions"]
    if len(txns) > max_transactions:
        result["transactions"] = txns[:max_transactions]
        result["transactions_truncated"] = True
        warnings.append(
            f"transactions list truncated to {max_transactions} of {len(txns)} "
            "(counts and latency stats are over ALL transactions); raise max_transactions."
        )

    um_req = acc["unmatched_requests"]
    um_cmp = acc["unmatched_completions"]
    result["unmatched_request_count"] = len(um_req)
    result["unmatched_completion_count"] = len(um_cmp)
    result["unmatched_requests"] = um_req[:_MAX_UNMATCHED_SHOWN]
    result["unmatched_completions"] = um_cmp[:_MAX_UNMATCHED_SHOWN]
    if um_req:
        warnings.append(
            f"{len(um_req)} request(s) never completed (outstanding at end of window) "
            "— the hang/deadlock signature."
        )
    if um_cmp:
        warnings.append(
            f"{len(um_cmp)} completion(s) had no matching open request "
            "(id mismatch, completion before request, or a window that starts mid-stream)."
        )
    if result["unknown_id_beats"]:
        warnings.append(
            f"{result['unknown_id_beats']} accepted beat(s) had an x/z id and could not be "
            "correlated."
        )
    if result["orphan_data_beats"]:
        warnings.append(
            f"{result['orphan_data_beats']} data beat(s) had no open request to attach to "
            "(data before address, or a window starting mid-burst)."
        )
    if result.get("beat_count_mismatch_count"):
        warnings.append(
            f"{result['beat_count_mismatch_count']} transaction(s) had a beat count != "
            "AxLEN+1 (early/late LAST, dropped or extra beat) — see each txn's "
            "beat_count vs expected_beats."
        )


def _attach_cursor(result, cursor_store, wave_path, edge, cursor_name, cursor_note) -> None:
    if cursor_store is None:
        return
    # Anchor priority: first stuck (never-completed) request > max-outstanding peak.
    anchor_time = None
    desc = ""
    if result["unmatched_requests"]:
        anchor_time = result["unmatched_requests"][0]["request_time_ps"]
        desc = f"first never-completed request id={result['unmatched_requests'][0]['id']}"
    elif result["max_outstanding"] > 0 and result["max_outstanding_time_ps"] is not None:
        anchor_time = result["max_outstanding_time_ps"]
        desc = f"peak outstanding={result['max_outstanding']}"
    if anchor_time is None:
        return
    note = cursor_note or f"reconstruct_transactions: {desc}"
    metadata = {"source": "reconstruct_transactions", "edge": edge, "wave_path": wave_path}
    if cursor_name:
        ref = cursor_store.set(cursor_name, int(anchor_time), note=note, metadata=metadata)
    else:
        ref = cursor_store.auto_set(
            int(anchor_time), prefix="txn", note=note, metadata=metadata,
            seed=f"txn|{wave_path}|{anchor_time}|{desc}",
        )
    result["cursor"] = ref.as_dict()


def _empty_result(wave_path, clock, edge, start_ps, end_ps) -> dict[str, Any]:
    return {
        "wave_path": wave_path, "clock": clock, "edge": edge,
        "start_ps": int(start_ps), "end_ps": int(end_ps),
        "request_count": 0, "completion_count": 0, "matched_count": 0,
        "outstanding_at_end": 0, "max_outstanding": 0, "max_outstanding_time_ps": None,
        "max_outstanding_per_id": 0, "max_outstanding_id": None,
        "reorder_count": 0, "unknown_id_beats": 0,
        "reset_clears": 0, "orphan_data_beats": 0,
        "beat_count_mismatch_count": 0,
        "timeout_cycles": None, "slow_count": 0,
        "latency": None,
        "transactions": [], "transactions_truncated": False,
        "unmatched_request_count": 0, "unmatched_completion_count": 0,
        "unmatched_requests": [], "unmatched_completions": [],
        "cursor": None, "reason": None, "warnings": [], "signal_errors": {},
    }


def _exists(parser: Any, path: str) -> bool:
    """Deterministic signal-existence probe (width read), independent of the
    sampler's signal_errors which silently omits some unresolved signals."""
    try:
        parser.get_signal_width(path)
        return True
    except Exception:
        return False


def _ok(value: Any, active_high: bool) -> bool:
    return _hs_truth(value, active_high) is True


def _dec(value: Any) -> int | None:
    return value.get("dec") if isinstance(value, dict) else None
