"""reconstruct_transactions — id-correlated request/response transaction layer (P2).

The most durable perception@scale primitive: walk a request handshake channel and
a completion handshake channel over the whole window, correlate accepted beats by
an id field, and emit per-transaction records (latency) plus aggregate facts
(outstanding curve, ordering). The LLM knows the protocol semantics; it cannot
apply them to millions of edges or hold thousands of outstanding txns in context —
that is exactly the split this tool serves.

One GENERIC core, not a tool per protocol: AXI read = AR channel -> R channel
(cmp_last on rlast), id=arid/rid; AXI write = AW -> B, id=awid/bid; any req/resp
with an id; CHI-like. AHB/APB (no id, phase-based) are intentionally out of scope
here — use verify_window / inspect_handshake.

Returns FACTS, never a verdict: a latency distribution (min/median/max/mean), not
an "outlier" label; unmatched requests/completions are surfaced loudly (the hang
signature), never swallowed. Reads existing waveforms only.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Any, Callable

from .cursor_store import CursorStore
from .cycle_query import sample_signals_on_edges
from .verify_condition import _hs_repr, _hs_truth, _resolve_signal_path

DEFAULT_MAX_TRANSACTIONS = 256
_MAX_UNMATCHED_SHOWN = 32


def reconstruct_transactions(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    clock: str,
    req_valid: str,
    req_ready: str,
    req_id: str,
    cmp_valid: str,
    cmp_ready: str,
    cmp_id: str,
    req_fields: list[str] | None = None,
    cmp_last: str | None = None,
    cmp_fields: list[str] | None = None,
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

    result: dict[str, Any] = _empty_result(wave_path, clock, edge, start_ps, end_ps)
    warnings: list[str] = result["warnings"]

    # Resolve every signal (auto-correct bus bit-range; loud on miss).
    clock_r = _resolve_signal_path(parser, clock)
    spec = {
        "req_valid": req_valid, "req_ready": req_ready, "req_id": req_id,
        "cmp_valid": cmp_valid, "cmp_ready": cmp_ready, "cmp_id": cmp_id,
    }
    if cmp_last is not None:
        spec["cmp_last"] = cmp_last
    resolved: dict[str, str] = {role: _resolve_signal_path(parser, p) for role, p in spec.items()}
    req_fields_r = [_resolve_signal_path(parser, p) for p in req_fields]
    cmp_fields_r = [_resolve_signal_path(parser, p) for p in cmp_fields]

    result["clock"] = clock_r

    # Deterministic existence check — do NOT rely on the sampler's signal_errors:
    # sample_signals_on_edges silently omits some unresolved signals (they just
    # never appear in the samples), which would otherwise make a typo'd control
    # signal look like an all-idle channel (0 txns, no reason). A missing control
    # signal makes the whole reconstruction meaningless, so be loud.
    missing_ctrl = sorted({p for p in ([clock_r] + list(resolved.values()))
                           if not _exists(parser, p)})
    if missing_ctrl:
        result["reason"] = (
            "control signal(s) not found: " + ", ".join(missing_ctrl)
            + " — FSDB buses usually need an explicit bit range (e.g. 'top.arid[3:0]')."
        )
        return result

    all_signals = sorted(set(resolved.values()) | set(req_fields_r) | set(cmp_fields_r))
    sampled = sample_signals_on_edges(
        parser, clock_r, all_signals, start_ps=start_ps, end_ps=end_ps, edge=edge,
    )
    # Coerce error values to str: the sampler may report non-string codes.
    signal_errors = {k: str(v) for k, v in sampled.get("signal_errors", {}).items()}
    result["signal_errors"] = signal_errors

    unresolved_fields = [p for p in (req_fields_r + cmp_fields_r) if not _exists(parser, p)]
    if unresolved_fields:
        warnings.append(
            "field signal(s) not captured (unresolved): " + ", ".join(sorted(set(unresolved_fields)))
        )

    samples = sampled.get("samples", [])
    if not samples:
        result["reason"] = f"no {edge} edges of clock {clock_r!r} in the window"
        return result

    has_last = "cmp_last" in resolved
    keep_req_fields = [p for p in req_fields_r if _exists(parser, p)]
    keep_cmp_fields = [p for p in cmp_fields_r if _exists(parser, p)]

    _walk(
        result, samples, resolved, active_high, has_last,
        keep_req_fields, keep_cmp_fields,
    )
    _finalize(result, max_transactions, timeout_cycles, warnings)
    _attach_cursor(result, cursor_store, wave_path, edge, cursor_name, cursor_note)
    return result


def _walk(result, samples, resolved, active_high, has_last,
          req_fields, cmp_fields) -> None:
    rv, rr, rid = resolved["req_valid"], resolved["req_ready"], resolved["req_id"]
    cv, cr, cid = resolved["cmp_valid"], resolved["cmp_ready"], resolved["cmp_id"]
    clast = resolved.get("cmp_last")

    pending: dict[int, list[dict]] = {}   # id -> FIFO of open requests
    pending_seqs: set[int] = set()        # all open request seq numbers
    transactions: list[dict] = result["transactions"]
    # private accumulators live only on the normal path (created here, popped in
    # _finalize); early-return paths never create them, so nothing leaks into the
    # schema-validated result (SchemaModel forbids extra fields).
    result["_unmatched_cmp"] = []
    result["_latencies"] = []
    unmatched_completions: list[dict] = result["_unmatched_cmp"]
    latencies: list[int] = result["_latencies"]

    seq = 0
    outstanding = 0
    max_out = 0
    max_out_time = None
    unknown_id_beats = 0
    req_count = 0
    cmp_count = 0
    reorder = 0
    per_id_out: dict[int, int] = {}       # id -> current outstanding
    per_id_peak: dict[int, int] = {}      # id -> peak outstanding
    beat_ctr: dict[int, int] = {}         # id -> accepted cmp beats since last completion

    for idx, s in enumerate(samples):
        sig = s["signals"]
        t = s["time_ps"]

        # --- request accepted ---
        if _ok(sig.get(rv), active_high) and _ok(sig.get(rr), active_high):
            rid_val = _dec(sig.get(rid))
            if rid_val is None:
                unknown_id_beats += 1
            else:
                req_count += 1
                outstanding += 1
                per_id_out[rid_val] = per_id_out.get(rid_val, 0) + 1
                per_id_peak[rid_val] = max(per_id_peak.get(rid_val, 0), per_id_out[rid_val])
                rec = {
                    "id": rid_val, "seq": seq, "idx": idx, "time_ps": t,
                    "outstanding_at_start": outstanding,
                    "req_fields": {f: _hs_repr(sig.get(f)) for f in req_fields},
                }
                pending.setdefault(rid_val, []).append(rec)
                pending_seqs.add(seq)
                seq += 1
                if outstanding > max_out:
                    max_out, max_out_time = outstanding, t

        # --- completion channel beat accepted ---
        if _ok(sig.get(cv), active_high) and _ok(sig.get(cr), active_high):
            cid_val = _dec(sig.get(cid))
            if cid_val is None:
                unknown_id_beats += 1
            else:
                beat_ctr[cid_val] = beat_ctr.get(cid_val, 0) + 1
                # A txn completes on cmp_last (multi-beat burst) or on every beat.
                is_completion = True if not has_last else _ok(sig.get(clast), active_high)
                if is_completion:
                    cmp_count += 1
                    n_beats = beat_ctr.pop(cid_val, 1)
                    queue = pending.get(cid_val)
                    if queue:
                        req = queue.pop(0)
                        pending_seqs.discard(req["seq"])
                        outstanding -= 1
                        per_id_out[cid_val] = max(0, per_id_out.get(cid_val, 1) - 1)
                        # reorder: an earlier-issued request is still open
                        if any(ps < req["seq"] for ps in pending_seqs):
                            reorder += 1
                        lat = idx - req["idx"]
                        latencies.append(lat)
                        transactions.append({
                            "id": cid_val,
                            "request_time_ps": req["time_ps"],
                            "completion_time_ps": t,
                            "latency_cycles": lat,
                            "latency_ps": t - req["time_ps"],
                            "beat_count": n_beats,
                            "outstanding_at_start": req["outstanding_at_start"],
                            "req_fields": req["req_fields"],
                            "cmp_fields": {f: _hs_repr(sig.get(f)) for f in cmp_fields},
                        })
                    else:
                        unmatched_completions.append({"id": cid_val, "completion_time_ps": t})

    # leftover open requests = unmatched (outstanding at end)
    unmatched_requests = [
        {"id": r["id"], "request_time_ps": r["time_ps"]}
        for q in pending.values() for r in q
    ]
    unmatched_requests.sort(key=lambda r: r["request_time_ps"])

    result.update({
        "request_count": req_count,
        "completion_count": cmp_count,
        "matched_count": len(transactions),
        "outstanding_at_end": len(unmatched_requests),
        "max_outstanding": max_out,
        "max_outstanding_time_ps": max_out_time,
        "max_outstanding_per_id": (max(per_id_peak.values()) if per_id_peak else 0),
        "max_outstanding_id": (max(per_id_peak, key=per_id_peak.get) if per_id_peak else None),
        "reorder_count": reorder,
        "unknown_id_beats": unknown_id_beats,
        "_unmatched_req": unmatched_requests,
    })


def _finalize(result, max_transactions, timeout_cycles, warnings) -> None:
    lats = result.pop("_latencies")
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

    um_req = result.pop("_unmatched_req")
    um_cmp = result.pop("_unmatched_cmp")
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
