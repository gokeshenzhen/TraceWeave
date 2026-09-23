"""
cycle_query.py
按 clock 边沿对齐，返回多个信号的周期级采样结果。
"""

from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from statistics import median
from typing import Any

from .cancellation import CANCEL_CHECK_STRIDE, check_cancelled
from . import clock_edge_cache, operation_metrics


class SignalColumnView:
    """Borrow one row of columns without copying the signal dictionary."""
    __slots__ = ("columns", "index")

    def __init__(self, columns):
        self.columns, self.index = columns, 0

    def get(self, path, default=None):
        column = self.columns.get(path)
        return column[self.index] if column is not None and self.index < len(column) else default


def iter_sample_rows(sampled):
    """Yield borrowed (time, values) pairs; consumers must not retain the view."""
    if "signal_columns" in sampled:
        view = SignalColumnView(sampled["signal_columns"])
        for index, at in enumerate(sampled["edge_times"]):
            if not index % CANCEL_CHECK_STRIDE:
                check_cancelled()
            view.index = index
            yield at, view
    else:
        for row in sampled.get("samples", ()):
            yield row["time_ps"], row["signals"]


class EdgeSamplingSession:
    """Bounded reuse state for a group of inspections on one shared clock.

    A full sweep creates one session per clock group and consumes that group
    before moving to the next. The high-activity clock transition list and its
    extracted edge/sample-time vectors therefore exist only once per group.

    Signal transition results are cached only when the sweep says a signal is
    used by more than one interface. A small reference count evicts each cached
    result immediately after its last consumer, so unique payload buses never
    accumulate and shared buses have a bounded lifetime.
    """

    def __init__(
        self,
        *,
        clock_path: str,
        start_ps: int,
        end_ps: int,
        edge: str,
        sample_offset_ps: int,
        signal_use_counts: dict[str, int] | None = None,
        sample_phase: str = "after",
    ):
        self.clock_path = clock_path
        self.start_ps = int(start_ps)
        self.end_ps = int(end_ps)
        self.edge = edge
        self.sample_offset_ps = int(sample_offset_ps)
        self.sample_phase = sample_phase
        self._signal_uses_remaining = dict(signal_use_counts or {})
        self._signal_transition_cache: dict[
            tuple[str, int, int], dict[str, Any]
        ] = {}
        self._cached_transition_count = 0
        self._bound_clock_path: str | None = None
        self._clock_result: dict[str, Any] | None = None
        self._edge_times: list[int] | None = None
        self._sample_times: list[int] | None = None
        self._clock_period_ps: int | None = None

    def _check_compatible(
        self,
        clock_path: str,
        start_ps: int,
        end_ps: int,
        edge: str,
        sample_offset_ps: int,
        sample_phase: str = "after",
    ) -> None:
        actual_window = (
            int(start_ps), int(end_ps), edge, int(sample_offset_ps), sample_phase
        )
        expected_window = (
            self.start_ps, self.end_ps, self.edge, self.sample_offset_ps, self.sample_phase
        )
        if actual_window != expected_window:
            raise ValueError(
                "EdgeSamplingSession reused with incompatible clock/window/edge"
            )
        # Discovery can return an unsuffixed FSDB path while inspect_handshake
        # resolves it to the native ``name[msb:lsb]`` spelling. Bind the first
        # resolved path and require every later consumer in this raw-clock group
        # to resolve identically.
        if self._bound_clock_path is None:
            self._bound_clock_path = clock_path
        elif clock_path != self._bound_clock_path:
            raise ValueError(
                "EdgeSamplingSession reused with an incompatible resolved clock"
            )

    def bind_signal_alias(self, original_path: str, resolved_path: str) -> None:
        """Move a discovery-path refcount to its resolved parser spelling."""
        if original_path == resolved_path:
            return
        remaining = self._signal_uses_remaining.pop(original_path, 0)
        if remaining:
            self._signal_uses_remaining[resolved_path] = (
                int(self._signal_uses_remaining.get(resolved_path, 0)) + remaining
            )

    def clear(self) -> None:
        """Release all cached transition/edge vectors after the clock unit ends."""
        self._signal_transition_cache.clear()
        self._signal_uses_remaining.clear()
        self._cached_transition_count = 0
        self._clock_result = None
        self._edge_times = None
        self._sample_times = None
        self._clock_period_ps = None

    def clock_context(
        self,
        parser: Any,
        *,
        clock_path: str,
        start_ps: int,
        end_ps: int,
        edge: str,
        sample_offset_ps: int,
        sample_phase: str = "after",
    ) -> tuple[dict[str, Any], list[int], list[int], int | None]:
        self._check_compatible(
            clock_path, start_ps, end_ps, edge, sample_offset_ps, sample_phase
        )
        check_cancelled()
        if self._clock_result is not None:
            operation_metrics.record_sweep_reuse_hit("clock")
            assert self._edge_times is not None
            assert self._sample_times is not None
            return (
                self._clock_result,
                self._edge_times,
                self._sample_times,
                self._clock_period_ps,
            )

        self._clock_result = _read_sweep_transition_result(
            parser, clock_path, start_ps, end_ps, kind="clock", sample_phase=sample_phase
        )
        if sample_phase == "before":
            self._clock_result = _before_clock_prefix(self._clock_result)
        _validate_clock_width(parser, clock_path)
        edge_extract_started = time.perf_counter()
        try:
            self._edge_times = _extract_edge_times(
                self._clock_result.get("transitions", []),
                edge,
                predecessor=self._clock_result.get("predecessor"),
                raw_time=sample_phase == "before",
            )
        finally:
            operation_metrics.add_sweep_cpu_timing(
                "edge_extract",
                (time.perf_counter() - edge_extract_started) * 1000.0,
            )
        self._sample_times = [
            edge_time + sample_offset_ps for edge_time in self._edge_times
        ]
        self._clock_period_ps = _compute_clock_period_ps(self._edge_times)
        if sample_phase == "before" and self._clock_period_ps is not None:
            self._clock_period_ps //= 1000
        return (
            self._clock_result,
            self._edge_times,
            self._sample_times,
            self._clock_period_ps,
        )

    def signal_transitions(
        self,
        parser: Any,
        signal_path: str,
        start_ps: int,
        end_ps: int,
    ) -> dict[str, Any]:
        check_cancelled()
        key = (signal_path, int(start_ps), int(end_ps))
        remaining = int(self._signal_uses_remaining.get(signal_path, 1))
        try:
            cached = self._signal_transition_cache.get(key)
            if cached is not None:
                operation_metrics.record_sweep_reuse_hit("signal")
                return cached
            result = _read_sweep_transition_result(
                parser, signal_path, start_ps, end_ps, kind="signal", sample_phase=self.sample_phase
            )
            if remaining > 1:
                self._signal_transition_cache[key] = result
                self._cached_transition_count += len(result.get("transitions", []))
                operation_metrics.record_sweep_cache_peak(
                    len(self._signal_transition_cache),
                    self._cached_transition_count,
                )
            return result
        finally:
            if signal_path in self._signal_uses_remaining:
                remaining -= 1
                if remaining <= 0:
                    self._signal_uses_remaining.pop(signal_path, None)
                    evicted = self._signal_transition_cache.pop(key, None)
                    if evicted is not None:
                        self._cached_transition_count -= len(
                            evicted.get("transitions", [])
                        )
                else:
                    self._signal_uses_remaining[signal_path] = remaining


def _read_sweep_transition_result(
    parser: Any,
    signal_path: str,
    start_ps: int,
    end_ps: int,
    *,
    kind: str,
    sample_phase: str = "after",
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        if sample_phase == "before":
            return _read_before_transitions(parser, signal_path, start_ps, end_ps)
        return parser.get_transitions(
            signal_path, start_ps=start_ps, end_ps=end_ps
        )
    finally:
        operation_metrics.record_sweep_transition_read(
            kind, (time.perf_counter() - started) * 1000.0
        )


def _time_fs(row):
    return row.get("time_fs", row["time_ps"] * 1000)


def _clock_value(value):
    # Signed one-bit expressions represent high as dec=-1; edge polarity is
    # determined by the physical bit, independent of numeric interpretation.
    value = value or {}
    bits = value.get('bin')
    return int(bits) if bits in ('0','1') else value.get('dec')


def _before_clock_prefix(result):
    """No protocol acceptance can be ordered through an unknown/glitch clock."""
    rows = result.get("transitions", [])
    previous = result.get("predecessor")
    ambiguous = result.get('ambiguous_times_fs',[])
    if ambiguous:
        stop = min(ambiguous)
        return {**result,'transitions':[r for r in rows if _time_fs(r)<stop],
                'truncated':True,'safe_before_fs':stop,
                'sampling_gaps':[*result.get('sampling_gaps',[]),'clock_event_order_unresolved']}
    for index, row in enumerate(rows):
        if not index % CANCEL_CHECK_STRIDE:
            check_cancelled()
        value = _clock_value(row.get('value'))
        gap = "clock_unknown" if value not in (0, 1) else None
        if previous and _time_fs(row) == _time_fs(previous) and row.get("value") != previous.get("value"):
            gap = "clock_event_order_unresolved"
        if gap:
            # Drop the entire first ambiguous timestamp, including an edge that
            # appeared earlier in the same group. No later prefix is complete.
            stop = index
            while stop and _time_fs(rows[stop - 1]) == _time_fs(row):
                stop -= 1
            return {**result, "transitions": rows[:stop], "truncated": True,
                    "safe_before_fs": _time_fs(row),
                    "sampling_gaps": [*result.get("sampling_gaps", []), gap]}
        previous = row
    return result


def _read_before_transitions(parser, path, start, end):
    """Read exact physical event times without changing the public ps API.

    The transaction adapter already enforces its request budget. Other protocol
    readers retain a bounded prefix, and reuse any active native group. No point
    query may replace a missing predecessor inside that group.
    """
    reader = getattr(type(parser), "_sampling_transitions", None)
    if reader is not None:
        return reader(parser, path, start, end)
    from .waveform_batch import event_readers, EventPagingUnavailable
    from .vcd_parser import _enrich_value
    result = {"transitions": [], "predecessor": None, "truncated": False}
    try:
        with event_readers([(parser, path)], start, end) as readers:
            count = size = 0
            while True:
                check_cancelled()
                page = readers[0].read_page()
                events = ((page.predecessor,) if page.predecessor else ()) + page.events
                for event in events:
                    check_cancelled()
                    count += 1
                    size += 512 + 4 * len(event.value or "")
                    if count > 1_000_000 or size > 64 * 1024 * 1024:
                        result["truncated"] = True
                        break
                    row = {"time_ps": event.time_ps, "time_fs": event.time_fs,
                           "value": _enrich_value(event.value)}
                    if event is page.predecessor:
                        result["predecessor"] = row
                    else:
                        result["transitions"].append(row)
                if result["truncated"] or page.truncated or page.complete:
                    result["truncated"] |= page.truncated
                    return result
    except EventPagingUnavailable:
        result = parser.get_transitions(path, start_ps=start, end_ps=end)
        header = getattr(parser, "get_header", lambda: {})()
        scale = header.get("scale_fs_per_tick")
        if isinstance(scale, int) and scale < 1000:
            # A rounded legacy timestamp cannot order sub-ps clock/data events.
            return {"transitions": [], "predecessor": None, "truncated": True,
                    "sampling_gaps": ["legacy_sub_ps_order_unavailable"]}
        return result


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

    edge_times, clock_period = _full_clock_edges(parser, clock_path, edge)

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
        "clock_period_ps": clock_period,
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

    per_cycle_signals, signal_errors, limited = _sample_signals_at_edges(
        parser, signal_paths, target_edges, sample_offset_ps
    )
    result["signal_errors"] = signal_errors
    result["transition_data_truncated"] = bool(limited)
    result["transition_signals_truncated"] = limited

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
    sampling_session: EdgeSamplingSession | None = None,
    compact: bool = False,
    max_edges: int | None = None,
    safe_prefix_only: bool = False,
    sample_phase: str = "after",
) -> dict[str, Any]:
    """Sample ``signal_paths`` on every ``clock_path`` edge inside a *time
    window* (as opposed to ``get_signals_by_cycle``, which slices by cycle
    index and caps the count).

    This is the shared clock-sampling substrate for window-scoped relational
    analysis (e.g. ``verify_condition.inspect_handshake``). Returns one entry
    per edge in chronological order, each carrying the edge time and the
    normalized ``{bin,hex,dec}`` value of each signal sampled at
    ``edge + sample_offset_ps``. Protocol consumers use ``sample_phase='before'``
    with zero offset: the last value strictly before the physical edge, excluding
    every event at that timestamp. Internal before-phase vectors use femtoseconds;
    returned times keep the public ceiling-to-picoseconds convention.
    """
    if edge not in {"posedge", "negedge"}:
        raise ValueError(f"edge must be 'posedge' or 'negedge', got {edge!r}")
    if sample_offset_ps < 0:
        raise ValueError("sample_offset_ps must be >= 0")
    if sample_phase not in {"before", "after"} or (sample_phase == "before" and sample_offset_ps):
        raise ValueError("before sampling requires zero offset; phase must be before or after")
    before = sample_phase == "before"
    safe_prefix_only |= before

    check_cancelled()
    from .waveform_selection import SelectionParser
    if isinstance(parser, SelectionParser):
        safe_prefix_only = True
    if sampling_session is not None:
        clock_result, edge_times, sample_times, clock_period_ps = (
            sampling_session.clock_context(
                parser,
                clock_path=clock_path,
                start_ps=start_ps,
                end_ps=end_ps,
                edge=edge,
                sample_offset_ps=sample_offset_ps,
                sample_phase=sample_phase,
            )
        )
    else:
        clock_result = _read_sweep_transition_result(
            parser, clock_path, start_ps, end_ps, kind="clock", sample_phase=sample_phase
        )
        if before:
            clock_result = _before_clock_prefix(clock_result)
        _validate_clock_width(parser, clock_path)
        edge_extract_started = time.perf_counter()
        try:
            edge_times = _extract_edge_times(
                clock_result.get("transitions", []),
                edge,
                predecessor=clock_result.get("predecessor"),
                raw_time=before,
            )
        finally:
            operation_metrics.add_sweep_cpu_timing(
                "edge_extract",
                (time.perf_counter() - edge_extract_started) * 1000.0,
            )
        sample_times = [edge_time + sample_offset_ps for edge_time in edge_times]
        clock_period_ps = _compute_clock_period_ps(edge_times)
        if before and clock_period_ps is not None:
            clock_period_ps //= 1000

    if safe_prefix_only and clock_result.get("truncated"):
        rows = clock_result.get("transitions", [])
        safe_before = (_time_fs(rows[-1]) if before else rows[-1]["time_ps"]) if rows else -1
        if before:
            safe_before = clock_result.get("safe_before_fs", safe_before)
        keep = bisect_left(sample_times, safe_before)
        edge_times, sample_times = edge_times[:keep], sample_times[:keep]
    sample_limit_reached = max_edges is not None and len(edge_times) > max_edges
    if sample_limit_reached:
        edge_times = edge_times[:max_edges]
        sample_times = sample_times[:max_edges]

    if compact and sampling_session is None:
        # The edge vector owns everything needed below. A request-local compact
        # consumer need not keep all enriched clock transitions while sampling
        # payload columns. Shared-clock sessions retain their own context.
        clock_result = {"truncated": bool(clock_result.get("truncated")),
                        "sampling_gaps": clock_result.get("sampling_gaps", [])}

    if compact:
        signal_columns, signal_errors, signal_transition_truncations = (
            _sample_signal_columns_at_edges(
                parser,
                signal_paths,
                edge_times,
                sample_offset_ps,
                sample_times=sample_times,
                sampling_session=sampling_session,
                safe_prefix_only=safe_prefix_only,
                sample_phase=sample_phase,
            )
        )
        per_edge_signals: list[dict[str, Any]] = []
    else:
        per_edge_signals, signal_errors, signal_transition_truncations = (
            _sample_signals_at_edges(
                parser,
                signal_paths,
                edge_times,
                sample_offset_ps,
                sample_times=sample_times,
                sampling_session=sampling_session,
                safe_prefix_only=safe_prefix_only,
                sample_phase=sample_phase,
            )
        )
        signal_columns = {}
    operation_metrics.record_sweep_sampling_shape(
        len(edge_times), len(dict.fromkeys(signal_paths))
    )
    transition_signals_truncated = []
    if clock_result.get("truncated"):
        transition_signals_truncated.append(clock_path)
    for signal_path in signal_transition_truncations:
        if signal_path not in transition_signals_truncated:
            transition_signals_truncated.append(signal_path)
    public_edges = [(at + 999) // 1000 for at in edge_times] if before else edge_times
    result = {
        "clock_path": clock_path,
        "edge": edge,
        "sample_offset_ps": sample_offset_ps,
        "sample_phase": sample_phase,
        "sampling_gaps": clock_result.get("sampling_gaps", []),
        "clock_period_ps": clock_period_ps,
        "total_edges_found": len(edge_times),
        "samples": [
            {"time_ps": edge_time, "time_ns": edge_time / 1000, "signals": signals}
            for edge_time, signals in zip(public_edges, per_edge_signals)
        ],
        "signal_errors": signal_errors,
        "transition_data_truncated": bool(transition_signals_truncated),
        "transition_signals_truncated": transition_signals_truncated,
    }
    if compact:
        # Internal full-sweep representation: one time vector plus one value
        # column per signal. Values remain references to the parser's enriched
        # transition values, avoiding one normalized dict and one row-dict
        # insertion per sampled signal per edge. Public tool results are built
        # by inspect_handshake and remain unchanged.
        result["edge_times"] = public_edges
        result["signal_columns"] = signal_columns
    if max_edges is not None:
        result["sample_limit_reached"] = sample_limit_reached
    return result


def _sample_signal_columns_at_edges(
    parser,
    signal_paths: list[str],
    target_edges: list[int],
    sample_offset_ps: int,
    *,
    sample_times: list[int] | None = None,
    sampling_session: EdgeSamplingSession | None = None,
    safe_prefix_only: bool = False,
    sample_phase: str = "after",
) -> tuple[dict[str, list[Any]], dict[str, str], list[str]]:
    """Compact sweep-only counterpart of :func:`_sample_signals_at_edges`."""
    columns: dict[str, list[Any]] = {}
    signal_errors: dict[str, str] = {}
    transition_signals_truncated: list[str] = []
    if not target_edges:
        return columns, signal_errors, transition_signals_truncated

    from .waveform_selection import SelectionParser
    if isinstance(parser, SelectionParser):
        return parser.sample_columns(signal_paths, target_edges, sample_offset_ps,
                                     sample_times, sampling_session, sample_phase=sample_phase)

    before = sample_phase == "before"
    range_start = target_edges[0] // 1000 if before else target_edges[0]
    range_end = (target_edges[-1] + 999) // 1000 if before else target_edges[-1] + sample_offset_ps + 1
    if sample_times is None:
        sample_times = [edge_time + sample_offset_ps for edge_time in target_edges]

    for signal_path in dict.fromkeys(signal_paths):
        check_cancelled()
        try:
            if sampling_session is not None:
                transitions_result = sampling_session.signal_transitions(
                    parser, signal_path, range_start, range_end
                )
            else:
                transitions_result = _read_sweep_transition_result(
                    parser, signal_path, range_start, range_end, kind="signal", sample_phase=sample_phase
                )
            if transitions_result.get("truncated"):
                transition_signals_truncated.append(signal_path)
            columns[signal_path] = _lookup_signal_values(
                parser,
                signal_path,
                transitions_result.get("transitions", []),
                sample_times,
                predecessor=transitions_result.get("predecessor"),
                before=before,
            )
            if safe_prefix_only and transitions_result.get("truncated"):
                rows = transitions_result.get("transitions", [])
                safe_before = (_time_fs(rows[-1]) if before else rows[-1]["time_ps"]) if rows else -1
                first_unknown = bisect_left(sample_times, safe_before)
                columns[signal_path][first_unknown:] = [None] * (len(sample_times) - first_unknown)
        except KeyError as exc:
            signal_errors[signal_path] = str(exc)
    return columns, signal_errors, transition_signals_truncated


def _sample_signals_at_edges(
    parser,
    signal_paths: list[str],
    target_edges: list[int],
    sample_offset_ps: int,
    *,
    sample_times: list[int] | None = None,
    sampling_session: EdgeSamplingSession | None = None,
    safe_prefix_only: bool = False,
    sample_phase: str = "after",
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Sample each signal at ``edge + offset`` for the given edge times.

    Shared by ``get_signals_by_cycle`` and ``sample_signals_on_edges``. A
    missing signal is recorded in the returned error map rather than aborting
    the whole sample (multi-signal calls stay best-effort per signal); other
    backend errors propagate.
    """
    per_edge_signals: list[dict[str, Any]] = [dict() for _ in target_edges]
    signal_errors: dict[str, str] = {}
    transition_signals_truncated: list[str] = []
    if not target_edges:
        return per_edge_signals, signal_errors, transition_signals_truncated

    from .waveform_selection import SelectionParser
    if isinstance(parser, SelectionParser) or safe_prefix_only or sample_phase == "before":
        columns, errors, truncated = _sample_signal_columns_at_edges(
            parser, signal_paths, target_edges, sample_offset_ps,
            sample_times=sample_times, sampling_session=sampling_session,
            safe_prefix_only=safe_prefix_only, sample_phase=sample_phase)
        for path, values in columns.items():
            for index, value in enumerate(values):
                if not index % CANCEL_CHECK_STRIDE:
                    check_cancelled()
                per_edge_signals[index][path] = _normalize_signal_value(value)
        return per_edge_signals, errors, truncated

    range_start = target_edges[0]
    range_end = target_edges[-1] + sample_offset_ps + 1
    if sample_times is None:
        sample_times = [edge_time + sample_offset_ps for edge_time in target_edges]

    # AHB may surface HWRITE both as address payload and as the write-data
    # qualifier. Sampling it twice is pure duplicate work and the per-edge dict
    # would overwrite the first value with the same second value anyway.
    for signal_path in dict.fromkeys(signal_paths):
        check_cancelled()
        try:
            if sampling_session is not None:
                transitions_result = sampling_session.signal_transitions(
                    parser, signal_path, range_start, range_end
                )
            else:
                transitions_result = _read_sweep_transition_result(
                    parser,
                    signal_path,
                    range_start,
                    range_end,
                    kind="signal",
                )
            if transitions_result.get("truncated"):
                transition_signals_truncated.append(signal_path)
            transitions = transitions_result.get("transitions", [])
            sampled_values = _sample_signal_values(
                parser,
                signal_path,
                transitions,
                sample_times,
                predecessor=transitions_result.get("predecessor"),
            )
            for index, value in enumerate(sampled_values):
                per_edge_signals[index][signal_path] = value
        except KeyError as exc:
            signal_errors[signal_path] = str(exc)
    return per_edge_signals, signal_errors, transition_signals_truncated


def _full_clock_edges(parser, clock_path, edge):
    """Retain global cycle numbering and period while reusing complete reads.

    Only snapshot-owning parsers opt in; arbitrary mutable parser adapters keep
    their previous uncached behavior. Native prefixes never enter the cache.
    """
    check_cancelled()
    token = getattr(parser, "_clock_cache_token", None)
    key = (clock_path, edge)
    cached = clock_edge_cache.cache.get(token, key)
    if cached is not None:
        check_cancelled()
        return cached
    result = parser.get_transitions(clock_path, start_ps=0, end_ps=-1)
    if getattr(parser,'_time_group_ambiguities',{}).get(clock_path):
        raise ValueError('derived clock has unresolved intra-time-group edges')
    check_cancelled()
    from .waveform_selection import SelectionParser
    if isinstance(parser, SelectionParser) and (result.get("truncated") or result.get("transition_count_is_lower_bound")):
        raise ValueError("incomplete clock transitions for selection; use a narrower time-window inspection")
    _validate_clock_width(parser, clock_path)
    edges = _extract_edge_times(result.get("transitions", []), edge)
    period = _compute_clock_period_ps(edges)
    check_cancelled()
    if not (result.get("truncated") or result.get("transition_count_is_lower_bound")):
        clock_edge_cache.cache.put(token, key, edges, period)
    return edges, period


def _validate_clock_width(parser, clock_path: str) -> None:
    width = parser.get_signal_width(clock_path)
    if width != 1:
        raise ValueError(f"clock signal must be 1-bit, got {width}-bit")


def _extract_edge_times(
    transitions: list[dict[str, Any]],
    edge: str,
    predecessor: dict[str, Any] | None = None,
    raw_time: bool = False,
) -> list[int]:
    edge_times: list[int] = []
    predecessor_value = (predecessor or {}).get("value") or {}
    prev_val: int | None = _clock_value(predecessor_value)
    if prev_val not in {0, 1}:
        prev_val = None
    for index, transition in enumerate(transitions):
        if not index % CANCEL_CHECK_STRIDE:
            check_cancelled()
        value = transition.get("value") or {}
        cur_val = _clock_value(value)
        if cur_val not in {0, 1}:
            prev_val = None
            continue
        if edge == "posedge" and prev_val == 0 and cur_val == 1:
            edge_times.append(_time_fs(transition) if raw_time else transition["time_ps"])
        elif edge == "negedge" and prev_val == 1 and cur_val == 0:
            edge_times.append(_time_fs(transition) if raw_time else transition["time_ps"])
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
    predecessor: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw_values = _lookup_signal_values(
        parser,
        signal_path,
        transitions,
        sample_times,
        predecessor=predecessor,
    )
    sampled_values: list[dict[str, Any]] = []
    materialize_started = time.perf_counter()
    try:
        for value_index, value in enumerate(raw_values):
            if not value_index % CANCEL_CHECK_STRIDE:
                check_cancelled()
            sampled_values.append(_normalize_signal_value(value))
    finally:
        materialize_ms = (time.perf_counter() - materialize_started) * 1000.0
        operation_metrics.add_sweep_execution_timing(
            "sample_materialize", materialize_ms
        )
        operation_metrics.add_sweep_cpu_timing(
            "value_sample", materialize_ms
        )
    return sampled_values


def _lookup_signal_values(
    parser,
    signal_path: str,
    transitions: list[dict[str, Any]],
    sample_times: list[int],
    predecessor: dict[str, Any] | None = None,
    before: bool = False,
) -> list[Any]:
    """Return parser value references at monotonic sample times in linear time."""
    if not sample_times:
        return []
    transition_times = [_time_fs(row) if before else row["time_ps"] for row in transitions]
    fallback_value = (predecessor or {}).get("value")
    fallback_queried = before or fallback_value is not None
    raw_values: list[Any] = []
    transition_index = -1
    next_transition = 0
    previous_sample_time: int | None = None

    lookup_started = time.perf_counter()
    try:
        for sample_index, sample_time in enumerate(sample_times):
            if not sample_index % CANCEL_CHECK_STRIDE:
                check_cancelled()
            if (
                previous_sample_time is not None
                and sample_time < previous_sample_time
            ):
                # The production edge sampler is monotonic. Preserve the old
                # private helper behavior for an unexpected decreasing input,
                # then continue linearly from the restored cursor.
                transition_index = (bisect_left if before else bisect_right)(
                    transition_times, sample_time
                ) - 1
                next_transition = transition_index + 1
            else:
                while (
                    next_transition < len(transition_times)
                    and (transition_times[next_transition] < sample_time if before
                         else transition_times[next_transition] <= sample_time)
                ):
                    transition_index = next_transition
                    next_transition += 1
            previous_sample_time = sample_time
            if transition_index >= 0:
                value = transitions[transition_index].get("value")
            else:
                if not fallback_queried:
                    fallback_result = parser.get_value_at_time(
                        signal_path, sample_times[0]
                    )
                    fallback_value = fallback_result.get("value")
                    fallback_queried = True
                value = fallback_value
            raw_values.append(value)
    finally:
        lookup_ms = (time.perf_counter() - lookup_started) * 1000.0
        operation_metrics.add_sweep_execution_timing(
            "sample_lookup", lookup_ms
        )
        operation_metrics.add_sweep_cpu_timing("value_sample", lookup_ms)

    return raw_values


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
