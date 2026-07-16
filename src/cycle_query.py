"""
cycle_query.py
按 clock 边沿对齐，返回多个信号的周期级采样结果。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from statistics import median
from typing import Any

from .cancellation import CANCEL_CHECK_STRIDE, check_cancelled


def get_signals_by_cycle(
    parser,
    clock_path: str,
    signal_paths: list[str],
    edge: str = "posedge",
    start_cycle: int = 0,
    num_cycles: int = 16,
    sample_offset_ps: int = 1,
    requested_num_cycles: int | None = None,
    capped: bool = False,
    start_time_ps: int | None = None,
    end_time_ps: int | None = None,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Sample ``signal_paths`` on ``num_cycles`` clock edges from ``start_cycle``.

    Two orthogonal locating axes; pick one per axis (the dispatch layer rejects
    mixing within an axis):

    * start axis: ``start_cycle`` (index) OR ``start_time_ps`` (ps) — the latter
      snaps to the first edge at/after the given time (``bisect_left``).
    * count axis: ``num_cycles`` OR ``end_time_ps`` (ps) — the latter counts every
      edge in ``[start, end_time_ps]`` (``bisect_right``), so the window is
      inclusive on both ends and a partial trailing period contributes a cycle
      iff it actually contains an edge. The count is always an exact edge count,
      never a fractional-cycle division. ``max_cycles`` caps a time-derived count
      (sets ``capped``); the slow path stays bounded.
    """
    if edge not in {"posedge", "negedge"}:
        raise ValueError(f"edge must be 'posedge' or 'negedge', got {edge!r}")
    if start_cycle < 0:
        raise ValueError("start_cycle must be >= 0")
    if num_cycles < 0:
        raise ValueError("num_cycles must be >= 0")
    if sample_offset_ps < 0:
        raise ValueError("sample_offset_ps must be >= 0")
    if start_time_ps is not None and start_time_ps < 0:
        raise ValueError("start_time_ps must be >= 0")
    if end_time_ps is not None and end_time_ps < 0:
        raise ValueError("end_time_ps must be >= 0")

    clock_result = parser.get_transitions(clock_path, start_ps=0, end_ps=-1)
    clock_transitions = clock_result.get("transitions", [])
    _validate_clock_width(parser, clock_path)
    edge_times = _extract_edge_times(clock_transitions, edge)

    resolved_from_time = start_time_ps is not None or end_time_ps is not None
    if start_time_ps is not None:
        start_cycle = bisect_left(edge_times, start_time_ps)
    if end_time_ps is not None:
        derived = max(0, bisect_right(edge_times, end_time_ps) - start_cycle)
        requested_num_cycles = derived
        if max_cycles is not None and derived > max_cycles:
            num_cycles = max_cycles
            capped = True
        else:
            num_cycles = derived

    target_edges = edge_times[start_cycle:start_cycle + num_cycles]
    truncated = len(target_edges) < num_cycles
    original_num_cycles = num_cycles if requested_num_cycles is None else requested_num_cycles

    result = {
        "clock_path": clock_path,
        "edge": edge,
        "sample_offset_ps": sample_offset_ps,
        "clock_period_ps": _compute_clock_period_ps(edge_times),
        "total_edges_found": len(edge_times),
        "start_cycle": start_cycle,
        "num_cycles_requested": original_num_cycles,
        "effective_num_cycles": num_cycles,
        "num_cycles_returned": len(target_edges),
        "capped": capped,
        "truncated": truncated,
        "resolved_from_time": resolved_from_time,
        "requested_start_time_ps": start_time_ps,
        "requested_end_time_ps": end_time_ps,
        "cycles": [],
        "signal_errors": {},
    }
    if not target_edges:
        return result

    per_cycle_signals, signal_errors = _sample_signals_at_edges(
        parser, signal_paths, target_edges, sample_offset_ps
    )
    result["signal_errors"] = signal_errors

    result["cycles"] = [
        {
            "cycle": start_cycle + index,
            "time_ps": edge_time,
            "time_ns": edge_time / 1000,
            "signals": signals,
        }
        for index, (edge_time, signals) in enumerate(zip(target_edges, per_cycle_signals))
    ]
    return result


def sample_signals_on_edges(
    parser,
    clock_path: str,
    signal_paths: list[str],
    start_ps: int = 0,
    end_ps: int = -1,
    edge: str = "posedge",
    sample_offset_ps: int = 1,
) -> dict[str, Any]:
    """Sample ``signal_paths`` on every ``clock_path`` edge inside a *time
    window* (as opposed to ``get_signals_by_cycle``, which slices by cycle
    index and caps the count).

    This is the shared clock-sampling substrate for window-scoped relational
    analysis (e.g. ``verify_condition.inspect_handshake``). Returns one entry
    per edge in chronological order, each carrying the edge time and the
    normalized ``{bin,hex,dec}`` value of each signal sampled at
    ``edge + sample_offset_ps``.
    """
    if edge not in {"posedge", "negedge"}:
        raise ValueError(f"edge must be 'posedge' or 'negedge', got {edge!r}")
    if sample_offset_ps < 0:
        raise ValueError("sample_offset_ps must be >= 0")

    clock_result = parser.get_transitions(clock_path, start_ps=start_ps, end_ps=end_ps)
    _validate_clock_width(parser, clock_path)
    edge_times = _extract_edge_times(clock_result.get("transitions", []), edge)

    per_edge_signals, signal_errors = _sample_signals_at_edges(
        parser, signal_paths, edge_times, sample_offset_ps
    )
    return {
        "clock_path": clock_path,
        "edge": edge,
        "sample_offset_ps": sample_offset_ps,
        "clock_period_ps": _compute_clock_period_ps(edge_times),
        "total_edges_found": len(edge_times),
        "samples": [
            {"time_ps": edge_time, "time_ns": edge_time / 1000, "signals": signals}
            for edge_time, signals in zip(edge_times, per_edge_signals)
        ],
        "signal_errors": signal_errors,
    }


def _sample_signals_at_edges(
    parser,
    signal_paths: list[str],
    target_edges: list[int],
    sample_offset_ps: int,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Sample each signal at ``edge + offset`` for the given edge times.

    Shared by ``get_signals_by_cycle`` and ``sample_signals_on_edges``. A
    missing signal is recorded in the returned error map rather than aborting
    the whole sample (multi-signal calls stay best-effort per signal); other
    backend errors propagate.
    """
    per_edge_signals: list[dict[str, Any]] = [dict() for _ in target_edges]
    signal_errors: dict[str, str] = {}
    if not target_edges:
        return per_edge_signals, signal_errors

    range_start = target_edges[0]
    range_end = target_edges[-1] + sample_offset_ps + 1
    sample_times = [edge_time + sample_offset_ps for edge_time in target_edges]

    for signal_path in signal_paths:
        check_cancelled()
        try:
            transitions_result = parser.get_transitions(
                signal_path,
                start_ps=range_start,
                end_ps=range_end,
            )
            transitions = transitions_result.get("transitions", [])
            sampled_values = _sample_signal_values(parser, signal_path, transitions, sample_times)
            for index, value in enumerate(sampled_values):
                per_edge_signals[index][signal_path] = value
        except KeyError as exc:
            signal_errors[signal_path] = str(exc)
    return per_edge_signals, signal_errors


def _validate_clock_width(parser, clock_path: str) -> None:
    width = parser.get_signal_width(clock_path)
    if width != 1:
        raise ValueError(f"clock signal must be 1-bit, got {width}-bit")


def _extract_edge_times(transitions: list[dict[str, Any]], edge: str) -> list[int]:
    edge_times: list[int] = []
    prev_val: int | None = None
    for index, transition in enumerate(transitions):
        if not index % CANCEL_CHECK_STRIDE:
            check_cancelled()
        value = transition.get("value") or {}
        cur_val = value.get("dec")
        if cur_val not in {0, 1}:
            prev_val = None
            continue
        if edge == "posedge" and prev_val == 0 and cur_val == 1:
            edge_times.append(transition["time_ps"])
        elif edge == "negedge" and prev_val == 1 and cur_val == 0:
            edge_times.append(transition["time_ps"])
        prev_val = cur_val
    return edge_times


def _compute_clock_period_ps(edge_times: list[int]) -> int | None:
    if len(edge_times) < 2:
        return None
    deltas = [curr - prev for prev, curr in zip(edge_times, edge_times[1:]) if curr >= prev]
    if not deltas:
        return None
    return int(median(deltas))


def _sample_signal_values(
    parser,
    signal_path: str,
    transitions: list[dict[str, Any]],
    sample_times: list[int],
) -> list[dict[str, Any]]:
    if not sample_times:
        return []

    transition_times = [transition["time_ps"] for transition in transitions]
    fallback_value = None
    sampled_values: list[dict[str, Any]] = []

    for sample_index, sample_time in enumerate(sample_times):
        if not sample_index % CANCEL_CHECK_STRIDE:
            check_cancelled()
        index = bisect_right(transition_times, sample_time) - 1
        if index >= 0:
            value = transitions[index].get("value")
        else:
            if fallback_value is None:
                fallback_result = parser.get_value_at_time(signal_path, sample_times[0])
                fallback_value = fallback_result.get("value")
            value = fallback_value
        sampled_values.append(_normalize_signal_value(value))
    return sampled_values


def _normalize_signal_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "bin": value.get("bin"),
            "hex": value.get("hex"),
            "dec": value.get("dec"),
        }
    return {"bin": None, "hex": None, "dec": None}


# ---------------------------------------------------------------------------
# Sub-cycle transient annotation for get_signals_around_time
# ---------------------------------------------------------------------------

def _value_key(v: Any) -> Any:
    """Comparable key for an enriched value dict ({bin,hex,dec}) or a raw value."""
    if isinstance(v, dict):
        return v.get("bin") or v.get("hex") or v.get("dec")
    return v


def annotate_center_transients(result: dict[str, Any]) -> dict[str, Any]:
    """Flag a ``value_at_center`` that is a sub-cycle transient — a combinational
    glitch at/just-after a clock edge that settles back within the same cycle.

    The sampled value is CORRECT for that exact ps, but a model reading it as the
    settled protocol value can misattribute: e.g. an interconnect owner-mux drives
    its data output to the idle value for ~1ns at each clock edge (sequential
    lock_owner updates on the edge while the combinational mux re-settles), so a
    point sample at the edge reads idle/garbage that the design never actually
    captures. Detect the unmistakable, zero-FP signature: the centre value is a
    brief excursion that RETURNS to the value held just before it (X -> glitch -> X),
    and add ``center_transient`` + ``center_settles_to`` + ``center_settle_ps`` so
    the reader treats the settled value as the protocol value. Mutates and returns
    ``result``. Best-effort: only fires when the window captured the settle."""
    center = result.get("center_time_ps")
    if center is None:
        return result
    flagged: list[str] = []
    for path, sig in (result.get("signals") or {}).items():
        if not isinstance(sig, dict) or sig.get("error"):
            continue
        vc = sig.get("value_at_center")
        trs = sig.get("transitions_in_window") or []
        pre = sig.get("pre_window_transitions") or []
        if vc is None or not trs:
            continue
        trs_sorted = sorted(trs, key=lambda t: t["time_ps"])
        after = [t for t in trs_sorted if t["time_ps"] > center]
        before = [t for t in trs_sorted if t["time_ps"] <= center]
        if not after:
            continue
        t_next = after[0]
        # value the signal held just BEFORE the centre value was entered
        if len(before) >= 2:
            v_prev = before[-2]["value"]
        elif pre:
            v_prev = sorted(pre, key=lambda t: t["time_ps"])[-1]["value"]
        else:
            continue  # cannot establish the pre-value -> stay conservative
        kc, kn, kp = _value_key(vc), _value_key(t_next["value"]), _value_key(v_prev)
        # dip-and-return: centre differs from a value that is the same before & after
        if kc != kn and kn == kp:
            sig["center_transient"] = True
            sig["center_settles_to"] = t_next["value"]
            sig["center_settle_ps"] = t_next["time_ps"]
            flagged.append(path)
    if flagged:
        result["transient_note"] = (
            "value_at_center is a SUB-CYCLE TRANSIENT (glitch) for: "
            + ", ".join(flagged)
            + " — it settles back to center_settles_to at center_settle_ps within the "
            "same cycle. Treat the SETTLED value as the protocol value; the edge "
            "sample is likely a combinational glitch (e.g. an interconnect mux "
            "re-settling at the clock edge), not what the design captures. Do not "
            "attribute a root cause to this edge value alone."
        )
    return result
