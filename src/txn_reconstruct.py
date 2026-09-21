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

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from types import MappingProxyType
import time
from typing import Any, Callable

from .cancellation import CANCEL_CHECK_STRIDE, OperationCancelled, check_cancelled
from .cursor_store import CursorStore
from .cycle_query import sample_signals_on_edges, iter_sample_rows
from .transaction_sampling import (TransactionBudget, TransactionBudgetExceeded,
    TransactionSampleInput, bounded_parser, role_identity, sample_limit, limit_to_budget_prefix)
from .verify_condition import _hs_repr, _hs_truth, _resolve_signal_path
from .waveform_selection import selection_inputs

DEFAULT_MAX_TRANSACTIONS = 256
_MAX_UNMATCHED_SHOWN = 32
# Single internal bucket key for no-id (in-order FIFO) mode; txn id is reported
# as None to the caller, never this sentinel.
_FIFO_KEY = 0


@selection_inputs("clock", "req_valid", "req_ready", "req_id", "cmp_valid", "cmp_ready", "cmp_id",
                  "req_fields", "req_len", "cmp_last", "cmp_fields", "data_valid",
                  "data_ready", "data_last", "data_fields", "reset")
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
    _sampled: TransactionSampleInput | None = None,
    _limits=None,
) -> dict[str, Any]:
    parser = get_parser(wave_path)
    req_fields = list(req_fields or [])
    cmp_fields = list(cmp_fields or [])
    data_fields = list(data_fields or [])

    result: dict[str, Any] = _empty_result(wave_path, clock, edge, start_ps, end_ps)
    warnings: list[str] = result["warnings"]
    if not 1 <= max_transactions <= 65536:
        raise ValueError("max_transactions must be between 1 and 65536")
    if start_ps < 0 or (end_ps != -1 and end_ps < start_ps):
        raise ValueError("invalid time window")
    budget = _sampled.budget if isinstance(_sampled, TransactionSampleInput) else TransactionBudget(_limits)

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
    started = time.perf_counter()
    if len(all_signals) > budget.limits.signals:
        result.update(coverage_status="partial", reason="signal_budget", gaps=["signal_budget"])
        result["analysis"] = _receipt(budget, stop_reason="signal_budget")
        return result
    if _sampled is not None:
        if not isinstance(_sampled, TransactionSampleInput):
            raise ValueError("transaction samples require a source-bound TransactionSampleInput")
        try:
            _sampled.validate(parser, wave_path, clock_r, all_signals, start_ps, end_ps, edge,
                role_identity(resolved, keep_req_fields, keep_cmp_fields, keep_data_fields,
                              active_high, reset_active_low))
        except TransactionBudgetExceeded as exc:
            result.update(coverage_status="partial", reason=str(exc))
            result["analysis"] = _receipt(budget, stop_reason=str(exc))
            return result
        sampled = _sampled.sampled
    else:
        try:
            sampled = sample_signals_on_edges(
                bounded_parser(parser, budget), clock_r, all_signals, start_ps=start_ps,
                end_ps=end_ps, edge=edge, compact=True, safe_prefix_only=True,
                max_edges=sample_limit(parser, all_signals, budget),
            )
            sampled = limit_to_budget_prefix(sampled, budget)
        except TransactionBudgetExceeded as exc:
            result.update(coverage_status="partial", reason=str(exc))
            result["analysis"] = _receipt(budget, stop_reason=str(exc))
            return result
    sample_ms = _sampled.sampling_ms if _sampled else (time.perf_counter() - started) * 1000
    signal_errors = {k: str(v) for k, v in sampled.get("signal_errors", {}).items()}
    result["signal_errors"] = signal_errors

    sample_count = len(sampled.get("edge_times", sampled.get("samples", [])))
    if not sample_count:
        result["reason"] = f"no {edge} edges of clock {clock_r!r} in the window"
        stopped = budget.stop_reason or ("sample_budget" if sampled.get("sample_limit_reached") else
                                         "transition_data_truncated" if sampled.get("transition_data_truncated") else None)
        if stopped:
            result.update(coverage_status="partial", reason=stopped, gaps=[stopped])
        result["analysis"] = _receipt(budget, stop_reason=stopped,
                                       sample_ms=sample_ms)
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
        budget=budget,
        max_transactions=max_transactions,
        boundaries=_sampled.boundaries if _sampled else None,
        uncertain=_sampled.uncertain if _sampled else None,
    )
    # _walk returns its private accumulators as locals — they are NEVER stored on
    # ``result`` so no early-return path can leak a private key into the schema.
    started = time.perf_counter()
    acc = _walk(result, iter_sample_rows(sampled), cfg)
    walk_ms = (time.perf_counter() - started) * 1000
    gaps = []
    if unresolved_fields or signal_errors:
        gaps.append("signal_read_incomplete")
    if sampled.get("transition_data_truncated"):
        gaps.append("transition_data_truncated")
    if sampled.get("sample_limit_reached"):
        gaps.append("sample_budget")
    if result["unknown_id_beats"] or result["unknown_control_cycles"]:
        gaps.append("unknown_control_or_id")
    if budget.stop_reason:
        gaps.append(budget.stop_reason)
    result.update(coverage_status="partial" if gaps else "complete", gaps=list(dict.fromkeys(gaps)))
    result["analysis"] = _receipt(budget, available_samples=sample_count,
        analyzed_samples=acc["analyzed_samples"], last_time_ps=acc["last_time_ps"],
        stop_reason=budget.stop_reason or ("sample_budget" if sampled.get("sample_limit_reached") else
            "transition_data_truncated" if sampled.get("transition_data_truncated") else None),
        sample_ms=sample_ms, walk_ms=walk_ms, **acc["resources"])
    facts = TransactionFacts(_freeze(result), _freeze(acc))
    result = _project(facts, max_transactions, timeout_cycles)
    _attach_cursor(result, cursor_store, wave_path, edge, cursor_name, cursor_note)
    return result


class _WalkCfg:
    __slots__ = (
        "resolved", "active_high", "has_id", "has_last", "has_data", "has_data_last",
        "reset_active_low", "capture_beats", "req_fields", "cmp_fields", "data_fields",
        "budget", "max_transactions", "boundaries", "uncertain",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])


def _walk(result, samples, cfg: _WalkCfg) -> dict[str, Any]:
    """One generic correlator over borrowed rows; retention never gates counting."""
    r, budget = cfg.resolved, cfg.budget
    pending, order, data_targets = {}, OrderedDict(), OrderedDict()
    early = deque()
    per_id_peak, per_id_out, beat_ctr = {}, {}, {}
    latencies = Counter()
    transactions, unmatched = result["transactions"], []
    req_count = cmp_count = matched = unmatched_count = reorder = 0
    unknown_id = unknown_control = reset_clears = history_clears = mismatches = 0
    max_out = peak_pending = peak_early = state_peak = result_bytes = 0
    max_out_time = None
    seq = analyzed = state_bytes = id_bytes = beat_id_bytes = 0
    last_time = None
    display_stop = None

    def fields(sig, names):
        return {f: _hs_repr(sig.get(f)) for f in names}

    def field_size(sig, names):
        return sum(256 + 4 * (len(f) + len(_hs_repr(sig.get(f)) or "")) for f in names)

    def attach():
        nonlocal state_bytes
        while early and data_targets:
            budget.check()
            target = next(iter(data_targets.values()))
            beat, size = early.popleft()
            target["data_beat_count"] += 1
            if cfg.capture_beats:
                target["data_beats_meta"].append(beat)
                target["bytes"] += size
            else:
                state_bytes -= size
            if beat["last"]:
                target["data_complete"] = True
                data_targets.pop(target["seq"])

    try:
        for idx, (t, sig) in enumerate(samples):
            budget.check()
            boundary = bool(cfg.boundaries[idx]) if cfg.boundaries is not None else False
            uncertain = bool(cfg.uncertain[idx]) if cfg.uncertain is not None else False
            if cfg.boundaries is None and r.get("reset") is not None:
                rst = _hs_truth(sig.get(r["reset"]), active_high=not cfg.reset_active_low)
                boundary, uncertain = rst is not False, rst is None
            if boundary:
                if order or early:
                    if uncertain:
                        history_clears += 1
                    else:
                        reset_clears += 1
                unknown_control += int(uncertain)
                pending.clear(); order.clear(); early.clear(); data_targets.clear()
                beat_ctr.clear(); per_id_out.clear()
                state_bytes = beat_id_bytes = 0
                analyzed, last_time = idx + 1, t
                continue

            req_ok = _ok(sig.get(r["req_valid"]), cfg.active_high) and _ok(sig.get(r["req_ready"]), cfg.active_high)
            cmp_ok = _ok(sig.get(r["cmp_valid"]), cfg.active_high) and _ok(sig.get(r["cmp_ready"]), cfg.active_high)
            data_ok = cfg.has_data and _ok(sig.get(r["data_valid"]), cfg.active_high) and _ok(sig.get(r["data_ready"]), cfg.active_high)
            rid = _dec(sig.get(r.get("req_id"))) if cfg.has_id else _FIFO_KEY
            cid = _dec(sig.get(r.get("cmp_id"))) if cfg.has_id else _FIFO_KEY
            req_ok_known = req_ok and rid is not None
            req_size = 1024 + rid.__sizeof__() + field_size(sig, cfg.req_fields) if req_ok_known else 0
            data_size = 512 + (field_size(sig, cfg.data_fields) if cfg.capture_beats else 0) if data_ok else 0
            new_id_bytes = (256 + 4 * rid.__sizeof__()) if req_ok_known and rid not in per_id_peak else 0
            new_beat_bytes = (256 + cid.__sizeof__()) if cmp_ok and cid is not None and cid not in beat_ctr else 0
            # Preflight a whole sampled edge before changing correlation state.
            reserve = (state_bytes + req_size + data_size + id_bytes + new_id_bytes +
                       beat_id_bytes + new_beat_bytes + 256 * (len(latencies) + 3))
            reason = ("pending_budget" if req_ok_known and len(order) >= budget.limits.pending else
                      "early_data_budget" if data_ok and not data_targets and not req_ok_known and len(early) >= budget.limits.early_data else
                      "state_byte_budget" if reserve > budget.limits.state_bytes else None)
            if reason:
                budget.stop_reason = reason
                break
            state_peak = max(state_peak, reserve)
            analyzed, last_time = idx + 1, t
            for prefix in ("req", "cmp", "data") if cfg.has_data else ("req", "cmp"):
                valid = _hs_truth(sig.get(r[prefix + "_valid"]), cfg.active_high)
                ready = _hs_truth(sig.get(r[prefix + "_ready"]), cfg.active_high)
                last_path = r.get(prefix + "_last")
                if valid is None or (valid is True and (ready is None or
                        (ready and last_path and _hs_truth(sig.get(last_path), cfg.active_high) is None))):
                    unknown_control += 1
                    break
            if req_ok:
                if rid is None:
                    unknown_id += 1
                else:
                    req_count += 1
                    id_bytes += new_id_bytes
                    per_id_out[rid] = per_id_out.get(rid, 0) + 1
                    per_id_peak[rid] = max(per_id_peak.get(rid, 0), per_id_out[rid])
                    rec = dict(id=rid if cfg.has_id else None, seq=seq, idx=idx, time_ps=t,
                        outstanding_at_start=len(order)+1,
                        len=_dec(sig.get(r.get("req_len"))), req_fields=fields(sig, cfg.req_fields),
                        data_complete=not cfg.has_data, data_beat_count=0, data_beats_meta=[], bytes=req_size)
                    pending.setdefault(rid, deque()).append(rec)
                    order[seq] = rec
                    if cfg.has_data:
                        data_targets[seq] = rec
                    seq += 1
                    state_bytes += req_size
                    peak_pending = max(peak_pending, len(order))
                    if len(order) > max_out:
                        max_out, max_out_time = len(order), t
                    attach()
            if data_ok:
                beat = dict(time_ps=t, last=not cfg.has_data_last or _ok(sig.get(r["data_last"]), cfg.active_high),
                            fields=fields(sig, cfg.data_fields) if cfg.capture_beats else {})
                early.append((beat, data_size))
                state_bytes += data_size
                attach()
                peak_early = max(peak_early, len(early))
            if cmp_ok:
                if cid is None:
                    unknown_id += 1
                else:
                    beat_id_bytes += new_beat_bytes
                    beat_ctr[cid] = beat_ctr.get(cid, 0) + 1
                    if cfg.has_last and not _ok(sig.get(r["cmp_last"]), cfg.active_high):
                        continue
                    cmp_count += 1
                    nbeats = beat_ctr.pop(cid)
                    beat_id_bytes -= 256 + cid.__sizeof__()
                    queue = pending.get(cid)
                    if not queue:
                        unmatched_count += 1
                        if len(unmatched) < _MAX_UNMATCHED_SHOWN:
                            unmatched.append(dict(id=cid if cfg.has_id else None, completion_time_ps=t))
                        continue
                    req = queue.popleft()
                    if not queue:
                        del pending[cid]
                    order.pop(req["seq"])
                    data_targets.pop(req["seq"], None)
                    state_bytes -= req["bytes"]
                    per_id_out[cid] -= 1
                    if not per_id_out[cid]:
                        del per_id_out[cid]
                    reorder += int(bool(order) and next(iter(order)) < req["seq"])
                    lat = idx - req["idx"]
                    latencies[lat] += 1
                    matched += 1
                    beats = req["data_beat_count"] if cfg.has_data else nbeats
                    expected = req["len"] + 1 if req["len"] is not None else None
                    mismatch = expected is not None and beats != expected
                    mismatches += int(mismatch)
                    # Optional presentation storage is independent of required
                    # state and all aggregates. An exhausted projection reports
                    # its own partial prefix; later anomalies are still counted.
                    if len(transactions) < cfg.max_transactions and display_stop is None:
                        size = req["bytes"] + 1024 + field_size(sig, cfg.cmp_fields)
                        if result_bytes + size > budget.limits.result_bytes:
                            display_stop = "result_byte_budget"
                        else:
                            transactions.append(dict(id=cid if cfg.has_id else None,
                                request_time_ps=req["time_ps"], completion_time_ps=t,
                                latency_cycles=lat, latency_ps=t-req["time_ps"], beat_count=beats,
                                expected_beats=expected, beat_count_mismatch=mismatch,
                                outstanding_at_start=req["outstanding_at_start"], data_complete=req["data_complete"],
                                req_fields=req["req_fields"], cmp_fields=fields(sig, cfg.cmp_fields),
                                data_beats=req["data_beats_meta"]))
                            result_bytes += size
    except TransactionBudgetExceeded:
        pass

    result.update(request_count=req_count, completion_count=cmp_count, matched_count=matched,
        outstanding_at_end=len(order), max_outstanding=max_out, max_outstanding_time_ps=max_out_time,
        max_outstanding_per_id=max(per_id_peak.values()) if cfg.has_id and per_id_peak else 0,
        max_outstanding_id=max(per_id_peak, key=per_id_peak.get) if cfg.has_id and per_id_peak else None,
        reorder_count=reorder, unknown_id_beats=unknown_id, unknown_control_cycles=unknown_control,
        reset_clears=reset_clears, unknown_history_clears=history_clears,
        orphan_data_beats=len(early), beat_count_mismatch_count=mismatches,
        unmatched_request_count=len(order), unmatched_completion_count=unmatched_count)
    # Only the bounded endpoint evidence survives. Order already is issue order.
    unmatched_req = []
    for req in order.values():
        if len(unmatched_req) == _MAX_UNMATCHED_SHOWN:
            break
        unmatched_req.append(dict(id=req["id"], request_time_ps=req["time_ps"]))
    return dict(latencies=latencies, unmatched_requests=unmatched_req,
        unmatched_completions=unmatched, analyzed_samples=analyzed, last_time_ps=last_time,
        resources=dict(peak_pending=peak_pending, peak_early_data=peak_early,
            state_peak_bytes=state_peak, result_bytes=result_bytes,
            display_status="partial" if display_stop else "complete", display_stop_reason=display_stop))


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class TransactionFacts:
    summary: object
    accumulators: object


def _project(facts, max_transactions, timeout_cycles):
    started = time.perf_counter()
    result, acc = _thaw(facts.summary), _thaw(facts.accumulators)
    _finalize(result, acc, max_transactions, timeout_cycles, result["warnings"])
    result["analysis"]["projection_ms"] = (time.perf_counter() - started) * 1000
    return result


def _receipt(budget, **kw):
    return dict(events_read=budget.events_read, events_admitted=budget.events, decoded_bytes=budget.decoded_bytes,
        value_bytes_read=budget.value_bytes_read,
        value_reuse_hits=budget.value_reuse_hits, value_reuse_misses=budget.value_reuse_misses,
        value_table_peak_entries=budget.value_table_peak_entries, value_table_peak_bytes=budget.value_table_peak_bytes,
        read_output_bytes=budget.output_bytes, read_pages=budget.pages,
        read_calls=budget.read_calls, legacy_reads=budget.legacy_reads, **kw)


def _finalize(result, acc, max_transactions, timeout_cycles, warnings) -> None:
    lats = acc["latencies"]
    if lats:
        n = sum(lats.values())
        middle, seen = [], 0
        for value, count in sorted(lats.items()):
            check_cancelled()
            middle.extend(value for target in ((n-1)//2, n//2) if seen <= target < seen+count)
            seen += count
        result["latency"] = {
            "min_cycles": min(lats),
            "median_cycles": sum(middle) // 2,
            "max_cycles": max(lats),
            "mean_cycles": round(sum(x*c for x, c in lats.items()) / n, 2),
        }

    # timeout_cycles is a caller-supplied threshold, echoed back; slow_count is a
    # FACT (how many completed txns exceeded it), not an "outlier" verdict.
    if timeout_cycles is not None:
        result["timeout_cycles"] = int(timeout_cycles)
        result["slow_count"] = sum(count for x, count in lats.items() if x > timeout_cycles)
        if result["slow_count"]:
            warnings.append(
                f"{result['slow_count']} transaction(s) had latency > {timeout_cycles} cycles."
            )

    txns = result["transactions"]
    if result["matched_count"] > max_transactions or len(txns) < result["matched_count"]:
        result["transactions"] = txns[:max_transactions]
        result["transactions_truncated"] = True
        warnings.append(
            f"transactions list truncated to {min(len(txns), max_transactions)} of {result['matched_count']} "
            "(counts and latency stats cover all analyzed transactions); raise max_transactions "
            "or narrow the window when the result byte budget is exhausted."
        )
        projection = result["analysis"]
        projection["display_status"] = "partial"
        projection["display_stop_reason"] = projection.get("display_stop_reason") or (
            "retained_fact_prefix" if len(txns) < min(max_transactions, result["matched_count"])
            else "max_transactions")

    um_req = acc["unmatched_requests"]
    um_cmp = acc["unmatched_completions"]
    result["unmatched_requests"] = um_req[:_MAX_UNMATCHED_SHOWN]
    result["unmatched_completions"] = um_cmp[:_MAX_UNMATCHED_SHOWN]
    if um_req:
        warnings.append(
            f"{result['unmatched_request_count']} request(s) remain pending at the analysis boundary; "
            "the window or partial prefix does not prove a hang."
        )
    if um_cmp:
        warnings.append(
            f"{result['unmatched_completion_count']} completion(s) had no matching open request "
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
    if result.get("coverage_status") == "partial":
        warnings.append("Partial analysis: missing, unknown or budget-limited evidence cannot exclude a defect; "
                        "inspect gaps and analysis.stop_reason.")


def _attach_cursor(result, cursor_store, wave_path, edge, cursor_name, cursor_note) -> None:
    if cursor_store is None:
        return
    # Anchor priority: first stuck (never-completed) request > max-outstanding peak.
    anchor_time = None
    desc = ""
    if result["unmatched_requests"]:
        anchor_time = result["unmatched_requests"][0]["request_time_ps"]
        desc = f"first pending request at analysis boundary id={result['unmatched_requests'][0]['id']}"
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
        "coverage_status": "zero_coverage", "gaps": [], "analysis": {},
        "unknown_control_cycles": 0,
    }


def _exists(parser: Any, path: str) -> bool:
    """Deterministic signal-existence probe (width read), independent of the
    sampler's signal_errors which silently omits some unresolved signals."""
    try:
        parser.get_signal_width(path)
        return True
    except OperationCancelled:
        raise
    except Exception:
        return False


def _ok(value: Any, active_high: bool) -> bool:
    return _hs_truth(value, active_high) is True


def _dec(value: Any) -> int | None:
    return value.get("dec") if isinstance(value, dict) else None
