"""verify_condition — verification primitives over existing waveforms.

This is the entry point for the auto-debug DSL. The v2 MVP intentionally
bypasses the Lark grammar and exposes two primitives that LLMs cannot do on
their own:

- ``diff_first_divergence`` — find the first time two signals disagree
- ``period`` — detect a signal's dominant period inside a window and flag
  the first beat that deviates from it
- ``diff_value_distribution`` — sample one signal across two groups of
  time points and surface which values / bits discriminate the groups
  (multi-actual differential; no golden reference required)

These ship as dedicated MCP tools that **auto-register a cursor** at the
located instant, so downstream calls can reference ``@cur_*`` instead of
copying ps integers. Grammar/composition comes later, only when callers
need to compose multiple checks in one expression.
"""

from __future__ import annotations

from typing import Any, Callable

from .cancellation import CANCEL_CHECK_STRIDE, check_cancelled
from .cursor_store import CursorRef, CursorStore
from .cycle_query import sample_signals_on_edges


# ---------------------------------------------------------------------------
# diff_first_divergence
# ---------------------------------------------------------------------------


def diff_first_divergence(
    *,
    get_parser: Callable[[str], Any],
    wave_path_a: str,
    signal_a: str,
    wave_path_b: str,
    signal_b: str,
    start_ps: int = 0,
    end_ps: int = -1,
    cursor_store: CursorStore | None = None,
    cursor_name: str | None = None,
    cursor_note: str | None = None,
) -> dict[str, Any]:
    """Find the first time ``signal_a`` and ``signal_b`` hold unequal values.

    Both waveforms are loaded through ``get_parser`` so the server's
    parser cache is reused (no extra reparse cost on repeat calls). The
    two paths MAY be identical — that's the within-run self-diff case
    (e.g. expected vs actual signals in the same FSDB).

    Algorithm: walk the union of the two signals' transition timelines
    chronologically. Seed both running values from
    ``get_value_at_time(signal, start_ps)`` so the comparison begins from
    the actual pre-window state, not from "unknown". At every event time
    apply all coincident transitions before comparing — if the values
    differ then, that time is the first divergence.

    When divergence is found and ``cursor_store`` is provided, register
    an anchor at the divergence time. Auto-generated names are
    deterministic on ``(wave_path_a, signal_a, wave_path_b, signal_b)``
    so repeated calls converge on the same name.
    """
    parser_a = get_parser(wave_path_a)
    parser_b = parser_a if wave_path_a == wave_path_b else get_parser(wave_path_b)

    tr_a = parser_a.get_transitions(signal_a, start_ps, end_ps)
    tr_b = parser_b.get_transitions(signal_b, start_ps, end_ps)

    # Resolve the effective window. Some backends (FSDB) echo end_ps=-1
    # ("to end of sim") straight back instead of resolving it to the real
    # end time; clamp such values against the actual transition extents so
    # the window doesn't collapse. (VCD resolves -1 itself, which is why
    # VCD-only unit tests did not surface this.)
    eff_start = max(_resolve_start(tr_a, start_ps), _resolve_start(tr_b, start_ps))
    eff_end = min(_resolve_end(tr_a), _resolve_end(tr_b))

    result: dict[str, Any] = {
        "diverged": False,
        "wave_path_a": wave_path_a,
        "wave_path_b": wave_path_b,
        "signal_a": signal_a,
        "signal_b": signal_b,
        "start_ps": eff_start,
        "end_ps": eff_end,
        "first_divergence_time_ps": None,
        "value_a": None,
        "value_b": None,
        "cursor": None,
        "transitions_compared": 0,
        "missing_a": False,
        "missing_b": False,
        "note": None,
    }

    if eff_end < eff_start:
        result["note"] = "empty effective window (end_ps < start_ps after clamping)"
        return result

    events_a = _collect_transitions(tr_a, eff_start, eff_end)
    events_b = _collect_transitions(tr_b, eff_start, eff_end)

    # Seed values at the start of the effective window from the parser's
    # last-known value, so a signal that diverges from the very first
    # sample (no transitions inside the window) still surfaces.
    val_a = _value_at(parser_a, signal_a, eff_start)
    val_b = _value_at(parser_b, signal_b, eff_start)

    # If both signals already hold concrete, comparable values at
    # eff_start and they differ, the divergence point IS eff_start.
    if _is_known(val_a) and _is_known(val_b) and val_a != val_b:
        result["diverged"] = True
        result["first_divergence_time_ps"] = eff_start
        result["value_a"] = val_a
        result["value_b"] = val_b
        result["transitions_compared"] = 0
        _attach_cursor(result, cursor_store, eff_start, val_a, val_b,
                       wave_path_a, signal_a, wave_path_b, signal_b,
                       cursor_name, cursor_note)
        return result

    div = _walk_first_divergence(events_a, events_b, val_a, val_b)
    result["transitions_compared"] = div["events_seen"]

    if div["time_ps"] is None:
        result["note"] = "signals agree across the window"
        return result

    result["diverged"] = True
    result["first_divergence_time_ps"] = div["time_ps"]
    result["value_a"] = div["value_a"]
    result["value_b"] = div["value_b"]
    _attach_cursor(result, cursor_store, div["time_ps"], div["value_a"], div["value_b"],
                   wave_path_a, signal_a, wave_path_b, signal_b,
                   cursor_name, cursor_note)
    return result


# ---------------------------------------------------------------------------
# period
# ---------------------------------------------------------------------------

DEFAULT_PERIOD_TOLERANCE_FRAC = 0.05
_MIN_EDGES_FOR_PERIOD = 3


def period(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    signal: str,
    start_ps: int = 0,
    end_ps: int = -1,
    edge: str = "posedge",
    tolerance_frac: float = DEFAULT_PERIOD_TOLERANCE_FRAC,
    cursor_store: CursorStore | None = None,
    cursor_name: str | None = None,
    cursor_note: str | None = None,
) -> dict[str, Any]:
    """Estimate a signal's dominant period and flag the first off-beat.

    Periodicity is exactly the kind of long-sequence numeric regularity an
    LLM cannot eyeball reliably from a transition dump. The dominant period
    is the *median* of consecutive edge-to-edge intervals (robust to a few
    glitches); ``jitter_ps`` is the largest deviation from it. The first
    interval whose deviation exceeds ``tolerance_frac * period`` marks an
    off-beat — its end edge is registered as a cursor so the caller can
    jump straight to where the rhythm broke (e.g. a stalled clock, a
    dropped burst beat, a backpressure bubble).

    ``edge``:
        - ``"posedge"`` — count rising edges (value becomes ``1``)
        - ``"negedge"`` — count falling edges (value becomes ``0``)
        - ``"any"`` — count every transition (use for multi-bit / strobe
          signals where rise/fall is ill-defined)

    Returns ``period_ps=None`` with a ``reason`` when there are fewer than
    three usable edges — an honest "cannot tell", not a guess.
    """
    parser = get_parser(wave_path)
    tr = parser.get_transitions(signal, start_ps, end_ps)
    eff_start = int(tr.get("start_ps", start_ps))
    eff_end = int(tr.get("end_ps", end_ps))

    result: dict[str, Any] = {
        "wave_path": wave_path,
        "signal": signal,
        "edge": edge,
        "start_ps": eff_start,
        "end_ps": eff_end,
        "period_ps": None,
        "edges_used": 0,
        "jitter_ps": 0,
        "off_beat_count": 0,
        "first_off_beat_time_ps": None,
        "cursor": None,
        "reason": None,
    }

    edge_times = _edge_times(tr.get("transitions") or (), edge)
    result["edges_used"] = len(edge_times)

    if len(edge_times) < _MIN_EDGES_FOR_PERIOD:
        result["reason"] = (
            f"not enough {edge} edges to estimate a period "
            f"({len(edge_times)} found, need >= {_MIN_EDGES_FOR_PERIOD})"
        )
        return result

    deltas = [b - a for a, b in zip(edge_times, edge_times[1:])]
    dominant = _median(deltas)
    if dominant <= 0:
        result["reason"] = "degenerate edge intervals (period <= 0)"
        return result

    tolerance = max(1, round(dominant * tolerance_frac))
    result["period_ps"] = dominant

    max_dev = 0
    off_beats = 0
    first_off_beat: int | None = None
    for idx, delta in enumerate(deltas):
        dev = abs(delta - dominant)
        if dev > max_dev:
            max_dev = dev
        if dev > tolerance:
            off_beats += 1
            if first_off_beat is None:
                # The edge that *ends* the deviating interval is where the
                # rhythm visibly broke.
                first_off_beat = edge_times[idx + 1]
    result["jitter_ps"] = max_dev
    result["off_beat_count"] = off_beats
    result["first_off_beat_time_ps"] = first_off_beat

    if first_off_beat is not None and cursor_store is not None:
        _attach_period_cursor(
            result, cursor_store, first_off_beat, dominant,
            wave_path, signal, edge, cursor_name, cursor_note,
        )
    return result


def _edge_times(transitions, edge: str) -> list[int]:
    """Select edge times from a transition list according to ``edge``.

    For ``posedge`` / ``negedge`` only 1-bit logic values matter: a posedge
    is a transition *to* ``1``, a negedge a transition *to* ``0``. Multi-bit
    or X/Z values are ignored under those modes. ``any`` keeps every
    transition time, deduplicated.
    """
    want: str | None
    if edge == "posedge":
        want = "1"
    elif edge == "negedge":
        want = "0"
    elif edge == "any":
        want = None
    else:
        raise ValueError(f"unknown edge mode: {edge!r}")

    times: list[int] = []
    for index, item in enumerate(transitions):
        if not index % CANCEL_CHECK_STRIDE:
            check_cancelled()
        t = int(item["time_ps"])
        if want is None:
            times.append(t)
            continue
        val = _stringify(item.get("value"))
        if val == want:
            times.append(t)
    times.sort()
    # Deduplicate identical timestamps (coincident records) so they don't
    # produce phantom zero-length intervals.
    deduped: list[int] = []
    for t in times:
        if not deduped or deduped[-1] != t:
            deduped.append(t)
    return deduped


def _median(values: list[int]) -> int:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    # Even count: average the two central values, rounded to ps.
    return round((s[mid - 1] + s[mid]) / 2)


def _attach_period_cursor(
    result: dict[str, Any],
    cursor_store: CursorStore,
    time_ps: int,
    period_ps: int,
    wave_path: str,
    signal: str,
    edge: str,
    cursor_name: str | None,
    cursor_note: str | None,
) -> None:
    note = cursor_note or f"first off-beat of {signal} ({edge}, period~{period_ps}ps)"
    metadata = {
        "source": "period",
        "signal": signal,
        "edge": edge,
        "period_ps": period_ps,
        "wave_path": wave_path,
    }
    if cursor_name:
        ref = cursor_store.set(cursor_name, time_ps, note=note, metadata=metadata)
    else:
        ref = cursor_store.auto_set(
            time_ps,
            prefix="beat",
            note=note,
            metadata=metadata,
            seed=f"{wave_path}|{signal}|{edge}",
        )
    result["cursor"] = ref.as_dict()


# ---------------------------------------------------------------------------
# diff_value_distribution
# ---------------------------------------------------------------------------

DEFAULT_DIST_TOP_N = 16
DEFAULT_MIN_BIT_DELTA = 0.5


def diff_value_distribution(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    signal: str,
    group_a_times: list[int],
    group_b_times: list[int] | None = None,
    top_n: int = DEFAULT_DIST_TOP_N,
    min_bit_delta: float = DEFAULT_MIN_BIT_DELTA,
) -> dict[str, Any]:
    """Compare a signal's value distribution across two groups of samples.

    This is the *multi-actual differential* primitive: instead of comparing
    one actual against a golden reference (which may not exist, or would
    leak the answer if sourced from a passing run), it samples ONE signal
    at two sets of time points drawn from the SAME failing run — typically
    a ``group_a`` of failing-vector times (from the scoreboard log) and a
    ``group_b`` of passing-vector times — and asks *what is statistically
    different about the signal between the two groups*.

    The headline output is ``bit_diff``: for each bit position, the
    fraction of group-A samples where the bit is 1 vs the same for group B.
    A bit that is (say) 1 in 95% of failing samples but 50% of passing
    samples is a strong discriminator — for a datapath bug like a wrong
    S-box table entry it points straight at the implicated input bits,
    collapsing the search space without ever computing the golden value.

    ``X``/``Z`` fractions are tracked per bit separately, so X-propagation
    bugs surface as an X-enriched group rather than being silently dropped.

    When ``group_b_times`` is omitted the call degenerates to a single-group
    value histogram (no diff, no bit_diff).

    All times are picoseconds (resolve cursors / unit literals before
    calling). Returns frequencies as floats in [0, 1].
    """
    parser = get_parser(wave_path)

    result: dict[str, Any] = {
        "wave_path": wave_path,
        "signal": signal,
        "width": 0,
        "group_a": _empty_group(),
        "group_b": None,
        "value_enrichment": [],
        "bit_diff": [],
        "discriminative_bits": [],
        "note": None,
    }

    samples_a = [_value_at(parser, signal, t) for t in group_a_times]
    stats_a = _group_stats(samples_a)
    result["group_a"] = _group_summary(stats_a, top_n)

    # Guard against the silent-failure trap: a wrong path (e.g. a vector
    # signal queried without its [hi:lo] range) makes every sample
    # unreadable, which would otherwise look like a constant signal.
    if group_a_times and result["group_a"]["unreadable"] == stats_a["n"]:
        result["note"] = (
            "all group_a samples are unreadable ('?') — check the signal path "
            "(vector signals may need a [hi:lo] range; confirm with search_signals)"
        )
        result["width"] = stats_a["width"]
        return result

    if not group_b_times:
        if not group_a_times:
            result["note"] = "group_a_times is empty"
        else:
            result["note"] = "single-group histogram (no group_b_times given)"
        result["width"] = stats_a["width"]
        return result

    samples_b = [_value_at(parser, signal, t) for t in group_b_times]
    stats_b = _group_stats(samples_b)
    result["group_b"] = _group_summary(stats_b, top_n)
    result["width"] = max(stats_a["width"], stats_b["width"])

    if stats_a["n"] == 0 or stats_b["n"] == 0:
        result["note"] = "one group is empty; cannot compute a differential"
        return result

    result["value_enrichment"] = _value_enrichment(stats_a, stats_b, top_n)
    bit_diff = _bit_diff(stats_a, stats_b, result["width"])
    result["bit_diff"] = bit_diff
    result["discriminative_bits"] = [
        b["bit"] for b in bit_diff
        if b["delta"] is not None and abs(b["delta"]) >= min_bit_delta
    ]
    return result


def _empty_group() -> dict[str, Any]:
    return {"n_samples": 0, "distinct": 0, "values": []}


def _group_stats(samples: list[str]) -> dict[str, Any]:
    """Accumulate value counts and per-bit 0/1/x tallies for a sample set."""
    value_counts: dict[str, int] = {}
    width = 0
    for s in samples:
        value_counts[s] = value_counts.get(s, 0) + 1
        bits = _bits_no_prefix(s)
        if len(bits) > width:
            width = len(bits)

    # Per-bit tallies, LSB = index 0. Missing high bits count as 0.
    ones = [0] * width
    zeros = [0] * width
    xcount = [0] * width
    for s in samples:
        bits = _bits_no_prefix(s)
        n = len(bits)
        for i in range(width):
            ch = bits[n - 1 - i] if i < n else "0"
            if ch == "1":
                ones[i] += 1
            elif ch == "0":
                zeros[i] += 1
            else:  # x / z / u / ?
                xcount[i] += 1
    return {
        "n": len(samples),
        "width": width,
        "value_counts": value_counts,
        "ones": ones,
        "zeros": zeros,
        "xcount": xcount,
    }


def _group_summary(stats: dict[str, Any], top_n: int) -> dict[str, Any]:
    counts = stats["value_counts"]
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return {
        "n_samples": stats["n"],
        "distinct": len(counts),
        "unreadable": counts.get("?", 0),
        "top_values": [{"value": v, "count": c} for v, c in top],
    }


def _value_enrichment(
    stats_a: dict[str, Any], stats_b: dict[str, Any], top_n: int
) -> list[dict[str, Any]]:
    na, nb = stats_a["n"], stats_b["n"]
    all_values = set(stats_a["value_counts"]) | set(stats_b["value_counts"])
    rows: list[dict[str, Any]] = []
    for v in all_values:
        ca = stats_a["value_counts"].get(v, 0)
        cb = stats_b["value_counts"].get(v, 0)
        fa = ca / na if na else 0.0
        fb = cb / nb if nb else 0.0
        rows.append({
            "value": v,
            "count_a": ca,
            "count_b": cb,
            "freq_a": round(fa, 4),
            "freq_b": round(fb, 4),
            "delta": round(fa - fb, 4),
        })
    rows.sort(key=lambda r: (-abs(r["delta"]), r["value"]))
    return rows[:top_n]


def _bit_diff(
    stats_a: dict[str, Any], stats_b: dict[str, Any], width: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(width):
        p1a = _p1(stats_a, i)
        p1b = _p1(stats_b, i)
        delta = None if (p1a is None or p1b is None) else round(p1a - p1b, 4)
        rows.append({
            "bit": i,
            "p1_a": None if p1a is None else round(p1a, 4),
            "p1_b": None if p1b is None else round(p1b, 4),
            "delta": delta,
            "x_frac_a": round(_xfrac(stats_a, i), 4),
            "x_frac_b": round(_xfrac(stats_b, i), 4),
        })
    rows.sort(key=lambda r: (-(abs(r["delta"]) if r["delta"] is not None else -1.0), r["bit"]))
    return rows


def _p1(stats: dict[str, Any], i: int) -> float | None:
    if i < len(stats["ones"]):
        ones = stats["ones"][i]
        zeros = stats["zeros"][i]
    else:
        # Bit position beyond this group's observed width: the high bit is
        # an implicit 0 in every sample (zero-extension), so it's a known 0.
        ones = 0
        zeros = stats["n"]
    known = ones + zeros
    return ones / known if known else None


def _xfrac(stats: dict[str, Any], i: int) -> float:
    n = stats["n"]
    if not n or i >= len(stats["xcount"]):
        return 0.0
    return stats["xcount"][i] / n


def _bits_no_prefix(value: str) -> str:
    s = value.strip()
    if s[:1] in ("b", "B"):
        s = s[1:]
    return s


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _max_transition_time(tr: dict[str, Any]) -> int | None:
    times = [int(it["time_ps"]) for it in (tr.get("transitions") or ())]
    return max(times) if times else None


def _resolve_start(tr: dict[str, Any], req_start: int) -> int:
    s = tr.get("start_ps", req_start)
    s = int(s) if s is not None else req_start
    return max(s, 0)


def _resolve_end(tr: dict[str, Any]) -> int:
    """End of the comparison window for one signal.

    Treat a missing / negative ``end_ps`` (the -1 "to end of sim" sentinel
    that some backends echo back) as the last transition time, so the
    window covers all available data instead of collapsing.
    """
    e = tr.get("end_ps")
    e = int(e) if e is not None else -1
    if e is None or e < 0:
        max_t = _max_transition_time(tr)
        return max_t if max_t is not None else 0
    return e


def _collect_transitions(
    tr: dict[str, Any], start_ps: int, end_ps: int
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for item in tr.get("transitions") or ():
        t = int(item["time_ps"])
        if t < start_ps or (end_ps >= 0 and t > end_ps):
            continue
        out.append((t, _stringify(item.get("value"))))
    out.sort(key=lambda x: x[0])
    return out


def _value_at(parser: Any, signal_path: str, time_ps: int) -> str:
    """Return the signal's value at ``time_ps`` as a canonical string."""
    try:
        sample = parser.get_value_at_time(signal_path, time_ps)
    except Exception:
        return "?"
    return _stringify(sample.get("value"))


def _stringify(value: Any) -> str:
    """Reduce an enriched value dict (or scalar) to a comparable string.

    Both parsers emit ``{"bin": ..., "hex": ..., "dec": ...}`` via
    ``_enrich_value``; ``bin`` is the canonical lossless form for X/Z.
    Falls back to ``str(value)`` for unexpected shapes, and returns
    ``"?"`` for None.
    """
    if value is None:
        return "?"
    if isinstance(value, dict):
        for key in ("bin", "raw", "hex", "dec"):
            if key in value and value[key] is not None:
                return str(value[key])
        return str(value)
    return str(value)


def _is_known(value: str) -> bool:
    if not value or value == "?":
        return False
    return not any(c in value for c in "xXzZuU?")


def _walk_first_divergence(
    events_a: list[tuple[int, str]],
    events_b: list[tuple[int, str]],
    val_a: str,
    val_b: str,
) -> dict[str, Any]:
    ia = ib = 0
    na, nb = len(events_a), len(events_b)
    events_seen = 0
    while ia < na or ib < nb:
        if not events_seen % CANCEL_CHECK_STRIDE:
            check_cancelled()
        ta = events_a[ia][0] if ia < na else None
        tb = events_b[ib][0] if ib < nb else None
        if ta is None:
            t = tb
        elif tb is None:
            t = ta
        else:
            t = ta if ta <= tb else tb
        # Apply every coincident transition before comparing.
        if ia < na and events_a[ia][0] == t:
            val_a = events_a[ia][1]
            ia += 1
        if ib < nb and events_b[ib][0] == t:
            val_b = events_b[ib][1]
            ib += 1
        events_seen += 1
        if _is_known(val_a) and _is_known(val_b) and val_a != val_b:
            return {
                "time_ps": t,
                "value_a": val_a,
                "value_b": val_b,
                "events_seen": events_seen,
            }
    return {
        "time_ps": None,
        "value_a": None,
        "value_b": None,
        "events_seen": events_seen,
    }


def _attach_cursor(
    result: dict[str, Any],
    cursor_store: CursorStore | None,
    time_ps: int,
    value_a: str,
    value_b: str,
    wave_path_a: str,
    signal_a: str,
    wave_path_b: str,
    signal_b: str,
    cursor_name: str | None,
    cursor_note: str | None,
) -> None:
    if cursor_store is None:
        return
    note = cursor_note or f"first divergence of {signal_a} vs {signal_b}"
    metadata = {
        "source": "diff_first_divergence",
        "signal_a": signal_a,
        "signal_b": signal_b,
        "value_a": value_a,
        "value_b": value_b,
        "wave_path_a": wave_path_a,
        "wave_path_b": wave_path_b,
    }
    if cursor_name:
        ref: CursorRef = cursor_store.set(
            cursor_name, time_ps, note=note, metadata=metadata
        )
    else:
        ref = cursor_store.auto_set(
            time_ps,
            prefix="div",
            note=note,
            metadata=metadata,
            seed=f"{wave_path_a}|{signal_a}|{wave_path_b}|{signal_b}",
        )
    result["cursor"] = ref.as_dict()


# ---------------------------------------------------------------------------
# inspect_handshake
# ---------------------------------------------------------------------------

DEFAULT_MAX_WAIT_CYCLES = 16
DEFAULT_MAX_HANDSHAKE_FINDINGS = 20


def inspect_handshake(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    clock: str,
    ready: str,
    valid: str | None = None,
    valid_htrans: str | None = None,
    htrans_rule: str = "active",
    payload: list[str] | None = None,
    hwrite: str | None = None,
    write_data: str | None = None,
    edge: str = "posedge",
    start_ps: int = 0,
    end_ps: int = -1,
    max_wait_cycles: int = DEFAULT_MAX_WAIT_CYCLES,
    check_payload_hold: bool = True,
    check_valid_hold: bool = True,
    active_high: bool = True,
    max_findings: int = DEFAULT_MAX_HANDSHAKE_FINDINGS,
    cursor_store: CursorStore | None = None,
    cursor_name: str | None = None,
    cursor_note: str | None = None,
) -> dict[str, Any]:
    """Classify a valid/ready handshake cycle-by-cycle and surface protocol
    facts an LLM cannot get from a transition dump or a scoreboard log.

    Protocol-agnostic: works for any clocked ``valid``/``ready`` pair — AXI
    ``*valid``/``*ready``, an AHB pair (``ready``=hready, ``valid``=a 1-bit
    "htrans != IDLE" signal), a generic valid-ready stream, or a credit
    interface. At every sampled clock ``edge`` it classifies:

    - ``valid && ready``  -> transfer
    - ``valid && !ready`` -> **stall** (consecutive stalls are accumulated;
      a run longer than ``max_wait_cycles`` becomes a ``long_stall`` finding)
    - ``ready && !valid``  -> ready_without_valid (backpressure imbalance)

    When ``payload`` signals are given and ``check_payload_hold`` is set, the
    payload values are latched at the start of each stall; any change while the
    transfer is still stalled is a ``payload_hold_violation`` (e.g. AHB
    ``htrans``/``haddr`` must hold steady while ``hready`` is low). This is the
    highest-value, hardest-to-eyeball check and exactly the class of bug that
    leaves no value pattern in scoreboard logs.

    When ``check_valid_hold`` is set (default), a complementary **wait-state
    hold** rule is enforced on the valid/transfer itself: once a beat is stalled
    (valid high, ready low at one edge), the transfer MUST persist until it is
    accepted. If at the next edge ``valid`` is known-low — the master dropped the
    transfer before ready arrived — it is a ``premature_valid_deassertion``
    finding. This catches the AHB master that does not wait for ``HREADY``
    (``HTRANS`` driven back to IDLE the cycle after a wait state), a bug
    ``payload_hold`` structurally cannot see: with a 1-cycle stall the payload
    never gets a chance to change, and ``HTRANS`` (the derived valid) is not a
    payload signal. Unlike payload-hold this needs no ``payload`` argument — it
    watches the valid source itself. An x/z valid at the next edge is treated as
    unknown (no violation), not a deassertion.

    For an AHB (``valid_htrans``) interface, whose ``payload`` is address-phase
    control only, a third check runs: at any edge where the derived valid is
    known-asserted, a control/address payload signal that is x/z is a
    ``x_while_valid`` finding — an active transfer carrying an unknown control
    field. It stays OFF for a literal-``valid`` interface because that payload may
    be data lanes that are legally x on disabled byte strobes (a false positive);
    see ``coverage.x_while_valid_checked``.

    Passing ``hwrite`` + ``write_data`` (AHB HWDATA) enables a **write data-phase
    hold** check: HWDATA must stay stable from the cycle after a write address is
    accepted through the cycle its data is accepted (HREADY high), so a change
    during a data-phase wait state is a ``write_data_hold_violation``. This is a
    separate pass from payload-hold because the data phase trails the htrans-derived
    valid by one cycle — which is also why HWDATA is not a payload signal.

    ``active_high=False`` inverts the valid/ready polarity. Samples where valid
    or ready is x/z are counted in ``unknown_sample_cycles`` and do not extend a
    stall (an honest break rather than guessing across unknown). A cursor is
    auto-registered at the first finding, prioritising
    payload_hold_violation > long_stall > longest stall, so downstream calls can
    jump straight to where the protocol broke. Reads existing waveforms only —
    does NOT rerun simulation.
    """
    payload_in = list(payload or [])
    parser = get_parser(wave_path)

    # Determine the valid source. AHB has no literal valid signal — it is
    # "htrans != IDLE" — so a derived valid can be requested via valid_htrans.
    # Exactly one of valid / valid_htrans must be given. The rule is explicit
    # and echoed (valid_source) so a derived valid is never silent.
    use_htrans = valid_htrans is not None
    if use_htrans and valid is not None:
        return _handshake_input_error(
            wave_path, clock, ready, edge, start_ps, end_ps, active_high,
            "provide either valid OR valid_htrans, not both",
        )
    if not use_htrans and valid is None:
        return _handshake_input_error(
            wave_path, clock, ready, edge, start_ps, end_ps, active_high,
            "provide a valid signal, or valid_htrans for an AHB-style interface",
        )
    if use_htrans and htrans_rule not in ("active", "non_idle"):
        return _handshake_input_error(
            wave_path, clock, ready, edge, start_ps, end_ps, active_high,
            f"htrans_rule must be 'active' or 'non_idle', got {htrans_rule!r}",
        )

    # Resolve paths first, auto-correcting bus names that need an explicit
    # [msb:lsb] suffix (common on FSDB: 'top.addr' -> 'top.addr[7:0]'). This
    # removes the most common footgun where a payload signal silently fails to
    # resolve and the hold check then looks "clean".
    clock = _resolve_signal_path(parser, clock)
    ready = _resolve_signal_path(parser, ready)
    valid_signal = _resolve_signal_path(parser, valid_htrans if use_htrans else valid)
    valid_source = f"htrans:{htrans_rule}" if use_htrans else "signal"
    payload = [_resolve_signal_path(parser, p) for p in payload_in]

    # AHB write data-phase hold (G3): HWDATA must stay stable while HREADY is low
    # during a *write* data phase. This is a different window than the address-phase
    # payload-hold (the data phase trails the htrans-derived valid by one cycle), so
    # it needs HWRITE (to qualify writes) and the HWDATA bus, supplied separately.
    want_write_data_hold = use_htrans and hwrite is not None and write_data is not None
    hwrite_sig = _resolve_signal_path(parser, hwrite) if want_write_data_hold else None
    wdata_sig = _resolve_signal_path(parser, write_data) if want_write_data_hold else None
    extra_signals = [s for s in (hwrite_sig, wdata_sig) if s]

    sampled = sample_signals_on_edges(
        parser, clock, [valid_signal, ready, *payload, *extra_signals],
        start_ps=start_ps, end_ps=end_ps, edge=edge,
    )
    signal_errors = sampled.get("signal_errors", {})
    transition_signals_truncated = list(
        sampled.get("transition_signals_truncated", [])
    )
    transition_data_truncated = bool(transition_signals_truncated)
    payload_unresolved = [p for p in payload if p in signal_errors]
    resolved_payload = [p for p in payload if p not in signal_errors]
    warnings: list[str] = []
    coverage: dict[str, Any] = {
        "clock_sampled": False,
        "valid_ready_resolved": False,
        "stall_checked": False,
        "backpressure_checked": False,
        "payload_hold_requested": bool(check_payload_hold and payload),
        "payload_hold_checked": False,
        "payload_hold_partially_checked": False,
        "payload_signals_requested": len(payload),
        "payload_signals_checked": 0,
        "payload_signals_unresolved": len(payload_unresolved),
        "valid_hold_requested": bool(check_valid_hold),
        "valid_hold_checked": False,
        # x-while-valid: only meaningful when the payload is control (address-phase)
        # signals, which is guaranteed for AHB (valid_htrans) bundles. Off for a
        # literal-valid interface because its payload may be data lanes that are
        # legally x on disabled byte strobes — checking those would false-positive.
        "x_while_valid_checked": False,
        # AHB write data-phase hold: needs hwrite + write_data; data-phase window.
        "write_data_hold_requested": bool(want_write_data_hold),
        "write_data_hold_checked": False,
        # Native transition reads are bounded. If any returned only a prefix,
        # every check below is partial even though it still ran on that prefix.
        "transition_data_truncated": transition_data_truncated,
        "transition_signals_truncated": len(transition_signals_truncated),
    }

    result: dict[str, Any] = {
        "wave_path": wave_path,
        "clock": clock,
        "valid": valid_signal,
        "valid_source": valid_source,
        "ready": ready,
        "payload": payload,
        "edge": edge,
        "start_ps": int(start_ps),
        "end_ps": int(end_ps),
        "active_high": active_high,
        "sample_count": len(sampled.get("samples", [])),
        "transfer_count": 0,
        "stall_count": 0,
        "max_stall_cycles": 0,
        "max_stall_begin_ps": None,
        "ended_in_stall": False,
        "final_stall_cycles": 0,
        "ready_without_valid_cycles": 0,
        "payload_hold_violations": 0,
        "payload_hold_checked": False,
        "valid_deassert_violations": 0,
        # x_while_valid: a control/address payload signal was x/z at an edge where
        # valid was known-asserted — a definite protocol violation (the bus carries
        # an active transfer with an unknown address/control field). Only checked on
        # AHB (control-only payload); see coverage.x_while_valid_checked.
        "x_while_valid_violations": 0,
        # write_data_hold: HWDATA changed during a write data-phase wait state
        # (HREADY low) — the master must hold write data until accepted. AHB-only;
        # see coverage.write_data_hold_checked.
        "write_data_hold_violations": 0,
        "payload_unresolved": payload_unresolved,
        "transition_data_truncated": transition_data_truncated,
        "transition_signals_truncated": transition_signals_truncated,
        "coverage": coverage,
        # protocol_semantics: an AHB-only "receipt" stating which of this result's
        # own metrics are faithful vs suppressed, so the surface reads as all-true-
        # positive and a reader cannot dismiss a real finding as a generic
        # valid/ready-vs-AHB semantic mismatch. None for a literal-valid interface.
        "protocol_semantics": None,
        "unknown_sample_cycles": 0,
        "findings": [],
        "violating_signal": None,
        "attribution": _empty_attribution(),
        "next_actions": [],
        "cursor": None,
        "reason": None,
        "warnings": warnings,
        "signal_errors": signal_errors,
    }

    if transition_data_truncated:
        warnings.append(
            "TRANSITION DATA TRUNCATED: the native buffer returned only a prefix "
            f"for {len(transition_signals_truncated)} sampled signal(s). Findings "
            "and zero counts cover only that prefix; this is not complete coverage."
        )

    # A missing valid/ready signal makes the whole check meaningless.
    valid_role = "valid_htrans" if use_htrans else "valid"
    for role, sig in ((valid_role, valid_signal), ("ready", ready)):
        if sig in signal_errors:
            result["reason"] = f"{role} signal not found: {signal_errors[sig]}"
            return result
    coverage["valid_ready_resolved"] = True

    # The payload-hold check only runs on payload signals that actually
    # resolved. Be LOUD when some did not: a payload_hold_violations of 0 must
    # never be read as "payload stable" if the signal was simply not found.
    do_hold_check = check_payload_hold and bool(resolved_payload)
    payload_hold_complete = do_hold_check and not payload_unresolved
    coverage["payload_signals_checked"] = len(resolved_payload) if check_payload_hold else 0
    coverage["payload_hold_partially_checked"] = do_hold_check and bool(payload_unresolved)
    if check_payload_hold and payload_unresolved:
        warnings.append(
            "payload-hold check did NOT run for unresolved signal(s): "
            + ", ".join(payload_unresolved)
            + " — a payload_hold_violations of 0 does NOT mean these were stable. "
            "FSDB bus signals usually need an explicit bit range "
            "(e.g. 'top.addr[7:0]'); pass the full path or a basename that "
            "resolves to exactly one bus."
        )

    samples = sampled.get("samples", [])
    if not samples:
        result["reason"] = (
            f"no {edge} edges of clock {clock!r} in the window — cannot sample a handshake"
        )
        return result
    coverage["clock_sampled"] = True
    coverage["stall_checked"] = True
    coverage["backpressure_checked"] = True
    coverage["payload_hold_checked"] = payload_hold_complete
    result["payload_hold_checked"] = payload_hold_complete
    coverage["valid_hold_checked"] = bool(check_valid_hold)
    # x-while-valid is only zero-FP when the payload is control (address-phase)
    # signals — guaranteed for AHB (htrans-derived valid), where the bundle carries
    # HADDR + control and never HWDATA. For a literal-valid interface the payload
    # may be data lanes (legally x on disabled strobes), so the check stays off.
    do_x_while_valid = use_htrans and bool(resolved_payload)
    coverage["x_while_valid_checked"] = do_x_while_valid
    x_while_valid_active: set[str] = set()  # debounce: one finding per x episode

    wd_hold_unresolved = [s for s in extra_signals if s in signal_errors]
    do_write_data_hold = want_write_data_hold and not wd_hold_unresolved
    coverage["write_data_hold_checked"] = do_write_data_hold
    if want_write_data_hold and wd_hold_unresolved:
        warnings.append(
            "write-data-hold check did NOT run for unresolved signal(s): "
            + ", ".join(wd_hold_unresolved)
            + " — a write_data_hold_violations of 0 does NOT mean HWDATA was held. "
            "HWDATA usually needs an explicit bit range (e.g. 'top.hwdata[31:0]')."
        )

    if use_htrans:
        result["protocol_semantics"] = {
            "protocol": "ahb",
            "valid_hold": (
                "faithful: the AHB address phase must hold HTRANS while HREADY is "
                "low, so premature_valid_deassertion is a real violation (the witness "
                "accepted_before_deassert=False proves the beat was never accepted)"
            ),
            "ready_without_valid": (
                "not_a_violation: HREADY high while HTRANS idle is an idle bus, not "
                "backpressure — the count is retained as a fact, not flagged"
            ),
            "payload_hold": (
                "address-phase control only (HADDR/HWRITE/HSIZE/HBURST/HPROT); "
                "HWDATA/HRDATA are excluded — they are data-phase, trailing the "
                "derived valid by one cycle"
            ),
            "x_while_valid": (
                "checked on the control payload" if do_x_while_valid
                else "not checked: no control payload resolved"
            ),
            "write_data_hold": (
                "checked: HWDATA must hold through a write data-phase wait state"
                if do_write_data_hold
                else "not checked: pass hwrite + write_data to enable"
            ),
        }

    findings: list[dict[str, Any]] = []
    in_stall = False
    stall_begin: int | None = None
    stall_cycles = 0
    stall_payload: dict[str, Any] = {}
    stall_valid_repr: str | None = None

    def _close_stall(end_ps_val: int | None) -> None:
        nonlocal in_stall, stall_cycles, stall_begin
        if not in_stall:
            return
        if stall_cycles > result["max_stall_cycles"]:
            result["max_stall_cycles"] = stall_cycles
            result["max_stall_begin_ps"] = stall_begin
        if stall_cycles > max_wait_cycles and len(findings) < max_findings:
            findings.append({
                "type": "long_stall",
                "severity": "warning",
                "begin_ps": stall_begin,
                "end_ps": end_ps_val,
                "cycles": stall_cycles,
            })
        in_stall = False
        stall_cycles = 0
        stall_begin = None

    for cycle_index, s in enumerate(samples):
        if not cycle_index % CANCEL_CHECK_STRIDE:
            check_cancelled()
        sig = s["signals"]
        if use_htrans:
            v = _ahb_valid_truth(sig.get(valid_signal), htrans_rule)
        else:
            v = _hs_truth(sig.get(valid_signal), active_high)
        r = _hs_truth(sig.get(ready), active_high)

        # Wait-state hold: if the previous edge left us stalled (valid high,
        # ready low) the beat must persist until accepted. A known-low valid
        # here means the master dropped the transfer before ready arrived — a
        # premature deassertion (e.g. AHB htrans -> IDLE without waiting for
        # HREADY). Evaluated BEFORE this edge is reclassified, and only on a
        # known False (an x/z valid is "unknown", never a deassertion).
        if in_stall and check_valid_hold and v is False:
            result["valid_deassert_violations"] += 1
            if len(findings) < max_findings:
                findings.append({
                    "type": "premature_valid_deassertion",
                    "severity": "error",
                    "time_ps": s["time_ps"],
                    "signal": valid_signal,
                    "from_value": stall_valid_repr,
                    "to_value": _hs_repr(sig.get(valid_signal)),
                    "stall_begin_ps": stall_begin,
                    "stall_cycles": stall_cycles,
                    # The dropped beat was stalled (ready/HREADY low) the whole time it
                    # was asserted — it was NEVER accepted. This is the witness that
                    # forecloses the "it's just AHB pipeline overlap" reading: an
                    # accepted beat would have seen ready high before valid dropped.
                    "accepted_before_deassert": False,
                })

        # x-while-valid: with a known-asserted valid the address/control payload
        # must be fully driven. An x/z here is a definite violation (active transfer
        # carrying an unknown control field). Debounced so a persistent x reports
        # once per episode, not every cycle; reset when valid drops so a fresh beat
        # re-reports. Only the control payload (AHB) reaches here — see do_x_while_valid.
        if do_x_while_valid and v is True:
            for p in resolved_payload:
                if not _hs_known(sig.get(p)):
                    if p not in x_while_valid_active:
                        x_while_valid_active.add(p)
                        result["x_while_valid_violations"] += 1
                        if len(findings) < max_findings:
                            findings.append({
                                "type": "x_while_valid",
                                "severity": "error",
                                "time_ps": s["time_ps"],
                                "signal": p,
                                "from_value": _hs_repr(sig.get(valid_signal)),
                                "to_value": _hs_repr(sig.get(p)),
                            })
                else:
                    x_while_valid_active.discard(p)
        elif do_x_while_valid:
            x_while_valid_active.clear()

        if v is None or r is None:
            result["unknown_sample_cycles"] += 1
            _close_stall(s["time_ps"])
            continue

        if v and r:
            result["transfer_count"] += 1
            _close_stall(s["time_ps"])
        elif r and not v:
            result["ready_without_valid_cycles"] += 1
            _close_stall(s["time_ps"])
        elif v and not r:
            result["stall_count"] += 1
            if not in_stall:
                in_stall = True
                stall_begin = s["time_ps"]
                stall_cycles = 1
                stall_payload = {p: sig.get(p) for p in resolved_payload}
                stall_valid_repr = _hs_repr(sig.get(valid_signal))
            else:
                stall_cycles += 1
                if do_hold_check:
                    for p in resolved_payload:
                        cur = sig.get(p)
                        prev = stall_payload.get(p)
                        if _hs_known(cur) and _hs_known(prev) and cur != prev:
                            result["payload_hold_violations"] += 1
                            if len(findings) < max_findings:
                                findings.append({
                                    "type": "payload_hold_violation",
                                    "severity": "error",
                                    "time_ps": s["time_ps"],
                                    "signal": p,
                                    "from_value": _hs_repr(prev),
                                    "to_value": _hs_repr(cur),
                                    "stall_begin_ps": stall_begin,
                                })
                            # Re-latch so only the first change of each beat is
                            # reported, not every cycle of an ongoing mismatch.
                            stall_payload[p] = cur
        else:  # not v and not r — idle
            _close_stall(s["time_ps"])

    # Flush a stall still open at the window edge. A stall that is still open
    # when the window ends is the deadlock signature: valid asserted, ready
    # never came, sampled right up to end-of-trace. Surface it as a fact
    # (ended_in_stall) — NOT as a "deadlock" verdict, since the window may have
    # simply been cut short. The caller (e.g. the anomaly sweep) decides.
    if in_stall:
        result["ended_in_stall"] = True
        result["final_stall_cycles"] = stall_cycles
        _close_stall(samples[-1]["time_ps"])

    # AHB write data-phase hold pass (G3): a separate sweep over the same samples,
    # because the data phase trails the address-phase htrans valid by one cycle.
    if do_write_data_hold:
        # Independent finding budget: a noisy premature-deassertion interface must
        # not starve write-data-hold of its witnesses (and thus its higher-priority
        # attribution/cursor). The sweep echoes only counts, so the larger findings
        # list matters only to a direct inspect_handshake caller.
        wd_count, wd_findings = _ahb_write_data_hold(
            samples, valid_signal, ready, hwrite_sig, wdata_sig, active_high,
            max_findings,
        )
        result["write_data_hold_violations"] = wd_count
        findings.extend(wd_findings)

    result["findings"] = findings
    _attach_handshake_attribution(result, findings, ready)
    _attach_handshake_cursor(
        result, cursor_store, findings, wave_path, valid_signal, ready, edge,
        cursor_name, cursor_note,
    )
    return result


def _ahb_write_data_hold(
    samples: list[dict[str, Any]],
    htrans_sig: str,
    ready_sig: str,
    hwrite_sig: str,
    wdata_sig: str,
    active_high: bool,
    max_findings: int,
) -> tuple[int, list[dict[str, Any]]]:
    """AHB write data-phase hold: HWDATA must stay stable from the cycle after a
    write address is accepted through the cycle its data is accepted (HREADY high).
    Faithful to AHB's folded HREADY + one-cycle data-phase offset, so it is a
    *separate* pass from the address-phase payload-hold (which is keyed on the
    htrans-derived valid and would mis-window HWDATA).

    A write data phase is "outstanding" once a NONSEQ/SEQ write address is accepted
    (htrans active && HWRITE && HREADY) and persists across wait states until the
    next HREADY high. While it is outstanding the HWDATA latched at the start of the
    phase must not change; a known change is a ``write_data_hold_violation``. Reads
    are not tracked (HRDATA is slave-driven). x/z on HWDATA or unknown ready breaks
    continuity rather than guessing — never a false positive."""
    findings: list[dict[str, Any]] = []
    count = 0
    outstanding_write: bool | None = None  # None=no live data phase; True=write
    phase_open = False
    latched: Any = None
    for s in samples:
        sig = s["signals"]
        htr = _ahb_valid_truth(sig.get(htrans_sig), "active")   # NONSEQ/SEQ
        hr = _hs_truth(sig.get(ready_sig), active_high)
        hw = _hs_truth(sig.get(hwrite_sig), active_high)        # True = write
        wd = sig.get(wdata_sig)

        if outstanding_write is True:
            if not phase_open:
                phase_open = True
                latched = wd
            elif _hs_known(latched) and _hs_known(wd) and wd != latched:
                count += 1
                if len(findings) < max_findings:
                    findings.append({
                        "type": "write_data_hold_violation",
                        "severity": "error",
                        "time_ps": s["time_ps"],
                        "signal": wdata_sig,
                        "from_value": _hs_repr(latched),
                        "to_value": _hs_repr(wd),
                    })
                latched = wd  # report only the first change of each data phase
        else:
            phase_open = False
            latched = None

        # Advance the outstanding data phase at the end of the cycle. HREADY high
        # completes the current phase and (if an address is accepted now) opens the
        # next; an unknown ready clears state rather than guessing across it.
        if hr is True:
            phase_open = False
            latched = None
            outstanding_write = (hw is True) if htr is True else None
        elif hr is None:
            outstanding_write = None
            phase_open = False
            latched = None
        # hr is False: phase persists unchanged
    return count, findings


def _empty_attribution() -> dict[str, Any]:
    return {
        "violating_side": None,
        "exonerated_side": None,
        "basis": None,
        "note": None,
    }


def _producer_gloss(use_htrans: bool) -> str:
    """Master/slave gloss for the valid-driver, honest about channel direction.

    The violating side is always whoever drives valid (the producer of *this*
    channel). That is the master on AXI AW/AR/W but the SLAVE on R/B, so we do
    NOT hardcode 'master' for a generic valid — explain_signal_driver resolves
    the actual instance. AHB is the exception: HTRANS is always master-driven."""
    if use_htrans:
        return ("valid here is HTRANS, which AHB drives from the master — this is "
                "a master-side protocol violation")
    return ("the violating side is whoever drives valid (the channel's producer): "
            "the master on AXI AW/AR/W, but the slave on R/B — confirm via the "
            "driver lookup rather than assuming master")


def _attach_handshake_attribution(result: dict[str, Any], findings: list[dict[str, Any]],
                                  ready: str) -> None:
    """Surface the signal carrying the primary finding + a forward-link to driver
    lookup, and — for the one-sided violation classes — a structured ``attribution``
    block that names the responsible side.

    The boundary: the *trace* holds values, not ownership, so we never read
    master/slave off the waveform. But two violation classes are one-sided by
    PROTOCOL, not by trace:

    - ``payload_hold_violation`` and ``premature_valid_deassertion`` are both
      breaches of the valid-driver's obligation (payload travels with valid; only
      the producer can mutate payload mid-stall or drop valid before acceptance).
      So ``violating_side='valid_driver'`` / ``exonerated_side='ready_driver'`` —
      the responder/consumer (ready/HREADY side, the slave on a request channel)
      cannot cause either, so the caller should NOT start in the slave
      driver/monitor. ``explain_signal_driver`` on valid lands on the actual
      producing instance — but when the producer is a UVM/TB driver (procedural
      drive via virtual interface + clocking block), NPI's RTL register fan-in
      cannot see it and may land on a nearby DUT register that is actually a LOAD
      of the net; the driver tool reports ``driver_status='testbench_driven'`` in
      that case rather than naming the load as the driver.
    - A plain stall (valid && !ready) is genuinely TWO-sided (legitimate slave
      back-pressure vs the master over-asserting valid), so no side is attributed
      — ``violating_side`` stays None and the link targets ``ready``."""
    use_htrans = str(result.get("valid_source", "")).startswith("htrans")
    gloss = _producer_gloss(use_htrans)

    xwv = next((f for f in findings if f.get("type") == "x_while_valid"), None)
    if xwv and xwv.get("signal"):
        sig = xwv["signal"]
        result["violating_signal"] = sig
        result["attribution"] = {
            "violating_side": "valid_driver",
            "exonerated_side": "ready_driver",
            "basis": "protocol_known_value_obligation",
            "note": (f"x/z on a control field while valid is asserted: {gloss}. The "
                     "address/control bus is driven by the valid-producer, so an "
                     "unknown value there is a producer-side defect; the ready/responder "
                     "cannot drive it — do not start in the slave driver/monitor."),
        }
        result["next_actions"] = [{
            "tool": "explain_signal_driver",
            "reason": ("attribute this x-while-valid: the control field carrying x/z is "
                       f"driven by the valid-producer — {gloss}. Trace its driver to the "
                       "source of the unknown (uninitialised reg, mis-wired input, or "
                       "multi-driver X)."),
            "signal_path": sig,
        }]
        return

    hold = next((f for f in findings if f.get("type") == "payload_hold_violation"), None)
    if hold and hold.get("signal"):
        sig = hold["signal"]
        result["violating_signal"] = sig
        result["attribution"] = {
            "violating_side": "valid_driver",
            "exonerated_side": "ready_driver",
            "basis": "protocol_payload_hold_obligation",
            "note": (f"payload-hold violation: payload travels with valid, so {gloss}. "
                     "The ready/responder (slave on a request channel) cannot cause "
                     "it — do not start in the slave driver/monitor."),
        }
        result["next_actions"] = [{
            "tool": "explain_signal_driver",
            "reason": ("attribute this payload-hold violation: the held signal moves "
                       "with valid (same producer), so a change during a stall points "
                       f"at the valid-driver's logic — {gloss}. Confirm the driving "
                       "instance; the ready/slave side cannot cause this."),
            "signal_path": sig,
        }]
        return
    wdh = next((f for f in findings if f.get("type") == "write_data_hold_violation"), None)
    if wdh and wdh.get("signal"):
        sig = wdh["signal"]
        result["violating_signal"] = sig
        result["attribution"] = {
            "violating_side": "valid_driver",
            "exonerated_side": "ready_driver",
            "basis": "protocol_write_data_hold_obligation",
            "note": ("write data-phase hold violation: HWDATA changed while HREADY was "
                     "low (data not yet accepted). AHB write data is driven by the "
                     "master, so this is a master-side defect; the slave (HREADY side) "
                     "cannot cause it — do not start in the slave driver/monitor."),
        }
        result["next_actions"] = [{
            "tool": "explain_signal_driver",
            "reason": ("attribute this write-data-hold violation: HWDATA is master-driven, "
                       "so a change during a data-phase wait state points at the master "
                       "BFM/driver not holding write data until HREADY. Confirm the "
                       "driving instance; the slave side cannot cause this."),
            "signal_path": sig,
        }]
        return
    deassert = next((f for f in findings if f.get("type") == "premature_valid_deassertion"), None)
    if deassert and deassert.get("signal"):
        sig = deassert["signal"]
        result["violating_signal"] = sig
        result["attribution"] = {
            "violating_side": "valid_driver",
            "exonerated_side": "ready_driver",
            "basis": "protocol_valid_hold_obligation",
            "note": (f"premature valid deassertion: {gloss}. Holding valid until "
                     "acceptance is the valid-driver's obligation; the ready/responder "
                     "(slave on a request channel) physically cannot make valid drop "
                     "— do not start in the slave driver/monitor."),
        }
        result["next_actions"] = [{
            "tool": "explain_signal_driver",
            "reason": ("attribute this premature valid deassertion: valid (or AHB "
                       "htrans) went inactive while the beat was still stalled "
                       f"(ready/HREADY low) — {gloss}, and holding valid until "
                       "acceptance is the valid-driver's obligation. Confirm the "
                       "driving instance actually waits for ready/HREADY; the "
                       "ready/slave side cannot cause this. NOTE: the producer of an "
                       "AHB master's htrans is the UVM master driver (procedural drive "
                       "via virtual interface + clocking block), which NPI's RTL "
                       "register fan-in cannot see. If explain_signal_driver lands on a "
                       "DUT/RTL register (e.g. an interconnect/matrix), treat that as "
                       "NPI walking the net to a nearby LOAD register, NOT a real "
                       "mis-wire pointing at that module — the tool reports "
                       "driver_status='testbench_driven' (cross_check.conflict) when it "
                       "detects this; cross-check with find_signal_loads if unsure."),
            "signal_path": sig,
        }]
        return
    stalled = result["ended_in_stall"] or any(f.get("type") == "long_stall" for f in findings)
    if stalled:
        result["attribution"] = {
            "violating_side": None,
            "exonerated_side": None,
            "basis": "two_sided_stall",
            "note": ("a stall is valid && !ready — genuinely two-sided (legitimate "
                     "slave back-pressure vs the master over-asserting valid). The "
                     "trace cannot attribute a side; resolve who drives ready vs "
                     "valid to decide."),
        }
        result["next_actions"] = [{
            "tool": "explain_signal_driver",
            "reason": ("a stall is valid && !ready — resolve who drives ready "
                       "(slave back-pressure) vs the master's valid to attribute "
                       "the stall; the trace shows the values, not who drove them."),
            "signal_path": ready,
        }]


def _ahb_valid_truth(value: Any, rule: str) -> bool | None:
    """Derive an AHB 'valid' from htrans. ``active`` (default) = NONSEQ/SEQ
    (htrans[1]==1); ``non_idle`` = htrans != IDLE (any non-zero). x/z -> None."""
    if not _hs_known(value):
        return None
    dec = value.get("dec")
    if dec is None:
        bin_str = value.get("bin")
        if bin_str is None:
            return None
        try:
            dec = int(bin_str, 2)
        except ValueError:
            return None
    if rule == "non_idle":
        return dec != 0
    return ((dec >> 1) & 1) == 1


def _handshake_input_error(
    wave_path: str, clock: str, ready: str, edge: str,
    start_ps: int, end_ps: int, active_high: bool, message: str,
) -> dict[str, Any]:
    """Minimal, schema-complete result carrying an input-validation reason."""
    return {
        "wave_path": wave_path, "clock": clock, "valid": "", "valid_source": "none",
        "ready": ready, "payload": [], "edge": edge,
        "start_ps": int(start_ps), "end_ps": int(end_ps), "active_high": active_high,
        "sample_count": 0, "transfer_count": 0, "stall_count": 0,
        "max_stall_cycles": 0, "max_stall_begin_ps": None,
        "ended_in_stall": False, "final_stall_cycles": 0,
        "ready_without_valid_cycles": 0, "payload_hold_violations": 0,
        "payload_hold_checked": False, "valid_deassert_violations": 0,
        "payload_unresolved": [],
        "transition_data_truncated": False,
        "transition_signals_truncated": [],
        "coverage": _empty_handshake_coverage(),
        "unknown_sample_cycles": 0, "findings": [],
        "violating_signal": None, "attribution": _empty_attribution(),
        "next_actions": [], "cursor": None,
        "reason": message, "warnings": [], "signal_errors": {},
    }


def _empty_handshake_coverage() -> dict[str, Any]:
    return {
        "clock_sampled": False,
        "valid_ready_resolved": False,
        "stall_checked": False,
        "backpressure_checked": False,
        "payload_hold_requested": False,
        "payload_hold_checked": False,
        "payload_hold_partially_checked": False,
        "payload_signals_requested": 0,
        "payload_signals_checked": 0,
        "payload_signals_unresolved": 0,
        "valid_hold_requested": False,
        "valid_hold_checked": False,
        "x_while_valid_checked": False,
        "write_data_hold_requested": False,
        "write_data_hold_checked": False,
        "transition_data_truncated": False,
        "transition_signals_truncated": 0,
    }


def _resolve_signal_path(parser: Any, path: str) -> str:
    """Return a path the parser can read, auto-correcting a bus name that needs
    an explicit ``[msb:lsb]`` suffix.

    Tries the path as-is first (cheap width probe). On failure, and only when the
    path has no bit-range, searches by basename and accepts the match when
    exactly one signal's path is ``<path>[...]``. Best-effort: any failure leaves
    the path unchanged so the caller still surfaces it via ``signal_errors``.
    """
    if not path:
        return path
    try:
        parser.get_signal_width(path)
        return path
    except Exception:
        pass
    if "[" in path or not hasattr(parser, "search_signals"):
        return path
    basename = path.rsplit(".", 1)[-1]
    try:
        results = parser.search_signals(basename).get("results", [])
    except Exception:
        return path
    candidates = [
        r.get("path", "") for r in results
        if isinstance(r, dict) and r.get("path", "").startswith(path + "[")
    ]
    return candidates[0] if len(candidates) == 1 else path


def _hs_truth(value: Any, active_high: bool) -> bool | None:
    """Tri-state truth of a 1-bit handshake signal: True / False / None (x/z)."""
    if not _hs_known(value):
        return None
    dec = value.get("dec")
    if dec is None:
        return None
    asserted = dec != 0
    return asserted if active_high else not asserted


def _hs_known(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    bin_str = value.get("bin")
    if bin_str is not None:
        return _is_known(bin_str)
    return value.get("dec") is not None or value.get("hex") is not None


def _hs_repr(value: Any) -> str | None:
    """Compact display string for a sampled value. Uses the parser's native
    hex form (already ``0x``-prefixed) when present, else binary, else decimal."""
    if not isinstance(value, dict):
        return None
    if value.get("hex") is not None:
        return str(value["hex"])
    if value.get("bin") is not None:
        return f"0b{value['bin']}"
    if value.get("dec") is not None:
        return str(value["dec"])
    return None


def _attach_handshake_cursor(
    result: dict[str, Any],
    cursor_store: CursorStore | None,
    findings: list[dict[str, Any]],
    wave_path: str,
    valid: str,
    ready: str,
    edge: str,
    cursor_name: str | None,
    cursor_note: str | None,
) -> None:
    if cursor_store is None:
        return
    # Anchor priority: x-while-valid > payload hold violation > write data hold
    # violation > premature valid deassertion > long stall > longest stall.
    anchor_time: int | None = None
    anchor_desc = ""
    for f in findings:
        if f["type"] == "x_while_valid":
            anchor_time = f["time_ps"]
            anchor_desc = f"x/z on {f['signal']} while valid asserted"
            break
    if anchor_time is None:
        for f in findings:
            if f["type"] == "payload_hold_violation":
                anchor_time = f["time_ps"]
                anchor_desc = f"payload hold violation on {f['signal']}"
                break
    if anchor_time is None:
        for f in findings:
            if f["type"] == "write_data_hold_violation":
                anchor_time = f["time_ps"]
                anchor_desc = f"write data hold violation on {f['signal']}"
                break
    if anchor_time is None:
        for f in findings:
            if f["type"] == "premature_valid_deassertion":
                anchor_time = f["time_ps"]
                anchor_desc = f"premature valid deassertion on {f['signal']}"
                break
    if anchor_time is None:
        for f in findings:
            if f["type"] == "long_stall":
                anchor_time = f["begin_ps"]
                anchor_desc = f"long stall ({f['cycles']} cycles)"
                break
    if anchor_time is None and result["max_stall_begin_ps"] is not None and result["max_stall_cycles"] > 0:
        anchor_time = result["max_stall_begin_ps"]
        anchor_desc = f"longest stall ({result['max_stall_cycles']} cycles)"
    if anchor_time is None:
        return

    note = cursor_note or f"{anchor_desc} on {valid}/{ready}"
    metadata = {
        "source": "inspect_handshake",
        "valid": valid,
        "ready": ready,
        "edge": edge,
        "wave_path": wave_path,
    }
    if cursor_name:
        ref = cursor_store.set(cursor_name, anchor_time, note=note, metadata=metadata)
    else:
        ref = cursor_store.auto_set(
            anchor_time,
            prefix="hs",
            note=note,
            metadata=metadata,
            seed=f"{wave_path}|{valid}|{ready}|{edge}",
        )
    result["cursor"] = ref.as_dict()
