"""
cycle_query.py
按 clock 边沿对齐，返回多个信号的周期级采样结果。
"""

from __future__ import annotations

from bisect import bisect_right
from statistics import median
from typing import Any


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
) -> dict[str, Any]:
    if edge not in {"posedge", "negedge"}:
        raise ValueError(f"edge must be 'posedge' or 'negedge', got {edge!r}")
    if start_cycle < 0:
        raise ValueError("start_cycle must be >= 0")
    if num_cycles < 0:
        raise ValueError("num_cycles must be >= 0")
    if sample_offset_ps < 0:
        raise ValueError("sample_offset_ps must be >= 0")

    clock_result = parser.get_transitions(clock_path, start_ps=0, end_ps=-1)
    clock_transitions = clock_result.get("transitions", [])
    _validate_clock_width(parser, clock_path)
    edge_times = _extract_edge_times(clock_transitions, edge)
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
        "cycles": [],
        "signal_errors": {},
    }
    if not target_edges:
        return result

    range_start = target_edges[0]
    range_end = target_edges[-1] + sample_offset_ps + 1
    sample_times = [edge_time + sample_offset_ps for edge_time in target_edges]
    per_cycle_signals = [dict() for _ in target_edges]

    for signal_path in signal_paths:
        try:
            transitions_result = parser.get_transitions(
                signal_path,
                start_ps=range_start,
                end_ps=range_end,
            )
            transitions = transitions_result.get("transitions", [])
            sampled_values = _sample_signal_values(parser, signal_path, transitions, sample_times)
            for index, value in enumerate(sampled_values):
                per_cycle_signals[index][signal_path] = value
        except KeyError as exc:
            result["signal_errors"][signal_path] = str(exc)

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


def _validate_clock_width(parser, clock_path: str) -> None:
    width = parser.get_signal_width(clock_path)
    if width != 1:
        raise ValueError(f"clock signal must be 1-bit, got {width}-bit")


def _extract_edge_times(transitions: list[dict[str, Any]], edge: str) -> list[int]:
    edge_times: list[int] = []
    prev_val: int | None = None
    for transition in transitions:
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

    for sample_time in sample_times:
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
