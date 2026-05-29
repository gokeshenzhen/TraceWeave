"""verify_condition — verification primitives over existing waveforms.

This is the entry point for the auto-debug DSL described in
``docs/auto-debug-decisions-v2.md``. The v2 MVP intentionally bypasses the
Lark grammar and exposes two primitives that LLMs cannot do on their own:

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

from .cursor_store import CursorRef, CursorStore


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
    for item in transitions:
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
