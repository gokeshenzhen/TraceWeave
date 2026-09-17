"""Explicit aligned-clock comparison, with no event-mode fallback."""

from .divergence_compare import read_stream, bit_value, known, _seed
from .dynamic_observe import clock_edges
from .cancellation import check_cancelled


def compare_clock(*, get_parser, side_a, side_b, start, end, policy, consume):
    sides = (side_a, side_b)
    clocks = [
        read_stream(get_parser, s["wave_path"], policy[f"clock_{label}"], start, end)
        for label, s in zip(("a", "b"), sides)
    ]
    for stream in clocks:
        consume(stream)
    ends = [s.end for s in clocks if s.end >= 0]
    effective_end = min(ends) if ends else start - 1
    if end >= 0:
        effective_end = min(end, effective_end)
    result = dict(
        diverged=False,
        wave_path_a=side_a["wave_path"],
        wave_path_b=side_b["wave_path"],
        signal_a=side_a["signal_path"],
        signal_b=side_b["signal_path"],
        start_ps=start,
        end_ps=effective_end,
        requested_start_ps=start,
        requested_end_ps=end,
        first_divergence_time_ps=None,
        value_a=None,
        value_b=None,
        comparison_status="inconclusive",
        coverage_status="none",
        earliest_difference_proven=False,
        coverage_gaps=[],
        transitions_compared=0,
        mode="clock",
        policy=policy,
        samples_compared=0,
        cursor=None,
    )

    def gap(reason, side="both"):
        row = {"reason": reason, "side": side, "start_ps": start}
        if row not in result["coverage_gaps"]:
            result["coverage_gaps"].append(row)

    edge_sets = []
    for stream in clocks:
        edges, gaps = clock_edges(
            stream, policy["edge"], start=start, end=effective_end
        )
        edge_sets.append(edges)
        for g in gaps:
            gap(g)
    if not edge_sets[0] or edge_sets[0] != edge_sets[1] or result["coverage_gaps"]:
        gap("clock_alignment_unproven")
        return result
    streams = []
    for side, s in zip(("a", "b"), sides):
        stream = read_stream(
            get_parser, s["wave_path"], s["signal_path"], start, effective_end
        )
        consume(stream)
        streams.append(stream)
        if stream.error:
            gap(stream.error, side)
        if stream.scale_fs is None or stream.scale_fs < 1000:
            gap("time_precision_loss", side)
        if stream.width is None:
            gap("signal_width_unavailable", side)
        if stream.truncated:
            gap("transition_data_truncated", side)
        if stream.end < effective_end:
            gap("outside_recorded_range", side)
    if streams[0].width != streams[1].width:
        gap("width_mismatch")
        return result
    result.update(width_a=streams[0].width, width_b=streams[1].width)
    values = [_seed(s, side["signal_path"], start) for s, side in zip(streams, sides)]
    indices = [0, 0]
    for edge in edge_sets[0]:
        check_cancelled()
        sample = edge + policy["sample_offset_ps"]
        if sample > effective_end:
            continue
        if any(
            s.error
            or s.end < sample
            or s.safe_before is not None
            and s.safe_before <= sample
            for s in streams
        ):
            break
        for i, stream in enumerate(streams):
            while (
                indices[i] < len(stream.transitions)
                and stream.transitions[indices[i]]["time_ps"] <= sample
            ):
                check_cancelled()
                values[i] = bit_value(
                    stream.transitions[indices[i]]["value"], stream.width
                )
                indices[i] += 1
                result["transitions_compared"] += 1
        result["samples_compared"] += 1
        if not all(known(v) for v in values):
            gap("value_unknown")
            continue
        if values[0] != values[1]:
            result.update(
                diverged=True,
                first_divergence_time_ps=sample,
                value_a=values[0],
                value_b=values[1],
                comparison_status="different",
                earliest_difference_proven=not result["coverage_gaps"],
            )
            break
    requested_end = end if end >= 0 else max(ends, default=effective_end)
    if requested_end > effective_end:
        gap("outside_recorded_range")
    if not result["samples_compared"]:
        gap("clock_alignment_unproven")
    elif not result["diverged"] and not result["coverage_gaps"]:
        result["comparison_status"] = "equal"
    result["coverage_status"] = (
        "partial" if result["coverage_gaps"] or result["diverged"] else "complete"
    )
    return result
