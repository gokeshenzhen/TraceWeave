"""Coverage-aware, cursor-free comparison of two recorded digital signals.

This module has no connectivity or session dependencies. A difference is a
positive observation; equality requires complete known-value coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Callable
from collections.abc import Mapping

from .cancellation import OperationCancelled, check_cancelled


def file_identity(path: str) -> tuple | None:
    try:
        stat = os.stat(path)
        return (os.path.realpath(path), stat.st_dev, stat.st_ino,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return None


def bit_value(value: Any, width: int | None) -> str | None:
    """Normalize a digital value without changing its declared width."""
    if isinstance(value, Mapping):
        value = value.get("bin", value.get("raw"))
    if value is None:
        return None
    bits = str(value).strip().lower()
    if bits.startswith("0b"):
        bits = bits[2:]
    elif bits.startswith("b"):
        bits = bits[1:]
    if not bits or any(c not in "01xz" for c in bits):
        return None
    if width is not None:
        if len(bits) > width:
            return None
        bits = bits.rjust(width, bits[0] if bits[0] in "xz" else "0")
    return bits


def known(bits: str | None) -> bool:
    return bool(bits) and not any(c in bits for c in "xz")


@dataclass
class SignalStream:
    parser: Any = None
    transitions: list[dict] = field(default_factory=list)
    predecessor: dict | None = None
    width: int | None = None
    end: int = -1
    scale_fs: int | None = None
    range_known: bool = False
    truncated: bool = False
    safe_before: int | None = None
    missing: bool = False
    error: str | None = None


def read_stream(get_parser: Callable, wave: str, signal: str,
                start: int, end: int) -> SignalStream:
    stream = SignalStream()
    try:
        check_cancelled()
        stream.parser = get_parser(wave)
        raw = stream.parser.get_transitions(signal, start, end)
        check_cancelled()
        stream.transitions = raw.get("transitions") or []
        stream.predecessor = raw.get("predecessor")
        stream.truncated = bool(raw.get("truncated"))
        if stream.truncated:
            stream.safe_before = (
                int(stream.transitions[-1]["time_ps"])
                if stream.transitions else start
            )
        if hasattr(stream.parser, "get_signal_width"):
            stream.width = int(stream.parser.get_signal_width(signal))
        header = getattr(stream.parser, "get_header", None)
        if header is None:
            header = getattr(stream.parser, "get_summary", None)
        if header is not None:
            summary = header()
            stream.end = int(summary.get("simulation_duration_ps", -1))
            stream.range_known = stream.end >= 0
            stream.scale_fs = summary.get("scale_fs_per_tick")
            if stream.scale_fs is None and summary.get("timescale_ps") is not None:
                stream.scale_fs = int(summary["timescale_ps"] * 1000)
        if stream.end < 0:
            stream.end = int(raw.get("end_ps", -1))
            if stream.end < 0:
                stream.end = max((int(t["time_ps"]) for t in stream.transitions), default=0)
        # Nonstandard/library parsers can still provide positive observations;
        # their missing declaration/range metadata never proves equality.
        if stream.width is not None and stream.width < 1:
            stream.width = None
    except OperationCancelled:
        raise
    except KeyError:
        stream.missing = True
        stream.error = "signal_not_found"
    except Exception:
        stream.error = "read_failed"
    return stream


def _seed(stream: SignalStream, signal: str, start: int) -> str | None:
    if stream.error:
        return None
    value = stream.predecessor.get("value") if stream.predecessor else None
    # Fold all original records at the start, including repeated timestamps.
    for index, event in enumerate(stream.transitions):
        if index % 1024 == 0:
            check_cancelled()
        if int(event["time_ps"]) > start:
            break
        value = event.get("value")
    if value is None:
        try:
            check_cancelled()
            value = stream.parser.get_value_at_time(signal, start).get("value")
        except OperationCancelled:
            raise
        except KeyError:
            stream.error = "signal_not_found"
            stream.missing = True
        except Exception:
            stream.error = "read_failed"
    return bit_value(value, stream.width)


def _compare_legacy(*, get_parser: Callable, wave_path_a: str, signal_a: str,
                    wave_path_b: str, signal_b: str, start_ps: int = 0,
                    end_ps: int = -1, consume: Callable | None = None) -> dict:
    if start_ps < 0 or end_ps < -1 or (end_ps >= 0 and end_ps < start_ps):
        raise ValueError("comparison window requires 0 <= start <= end, or end=-1")
    identities = {p: file_identity(p) for p in (wave_path_a, wave_path_b)}
    streams = []
    for p, s in ((wave_path_a, signal_a), (wave_path_b, signal_b)):
        stream = read_stream(get_parser, p, s, start_ps, end_ps)
        if consume:
            consume(stream)
        streams.append(stream)
    available_ends = [s.end for s in streams if not s.error and s.end >= 0]
    effective_end = min(available_ends) if available_ends else start_ps - 1
    if end_ps >= 0:
        effective_end = min(effective_end, end_ps)
    result = dict(
        diverged=False, wave_path_a=wave_path_a, wave_path_b=wave_path_b,
        signal_a=signal_a, signal_b=signal_b, start_ps=start_ps, end_ps=effective_end,
        requested_start_ps=start_ps, requested_end_ps=end_ps,
        first_divergence_time_ps=None, value_a=None, value_b=None, cursor=None,
        transitions_compared=0, missing_a=streams[0].missing, missing_b=streams[1].missing,
        note=None, comparison_status="inconclusive", coverage_status="none",
        earliest_difference_proven=False, coverage_gaps=[], next_actions=[],
        width_a=streams[0].width, width_b=streams[1].width,
        excluded_intervals=[],
        reading={"mode_a": "legacy_materialized", "mode_b": "legacy_materialized",
                 "events_read": sum(len(s.transitions) + bool(s.predecessor) for s in streams),
                 "pages_read": 0, "native_read_calls": None, "record_bytes_read": None,
                 "byte_basis": "unavailable"},
    )
    gaps = result["coverage_gaps"]

    def gap(code: str, side: str = "both", at: int | None = None):
        item = {"reason": code, "side": side, "start_ps": start_ps if at is None else at}
        previous = next((g for g in gaps if g["reason"] == code and g["side"] == side), None)
        if previous is None:
            gaps.append(item)
        else:
            previous["end_ps"] = max(previous.get("end_ps", previous["start_ps"]), item["start_ps"])

    for side, stream in zip(("a", "b"), streams):
        if stream.error:
            gap(stream.error, side)
        if stream.width is None and not stream.error:
            gap("signal_width_unavailable", side)
        if not stream.range_known and not stream.error:
            gap("recorded_range_unavailable", side)
        if stream.scale_fs is not None and stream.scale_fs < 1000:
            gap("time_precision_loss", side)
        if stream.truncated:
            gap("transition_data_truncated", side, stream.safe_before)
        requested_end = end_ps if end_ps >= 0 else max(available_ends, default=effective_end)
        if stream.end >= 0 and requested_end > stream.end:
            result["excluded_intervals"].append(
                {"side": side, "start_ps": stream.end, "end_ps": requested_end,
                 "reason": "outside_recorded_range"})
            gap("outside_recorded_range", side, stream.end)
    if effective_end < start_ps:
        gap("empty_window")
        result["note"] = "empty effective comparison window"
        return result
    if any(stream.error for stream in streams):
        return result
    if all(s.width is not None for s in streams) and streams[0].width != streams[1].width:
        gap("width_mismatch")
        return result

    values = [_seed(stream, signal, start_ps) for stream, signal in
              zip(streams, (signal_a, signal_b))]
    indices = [0, 0]
    time_ps = start_ps
    examined = 0
    invalid_order = False
    while time_ps <= effective_end:
        check_cancelled()
        if any(s.safe_before is not None and time_ps >= s.safe_before for s in streams):
            break
        for side_index, stream in enumerate(streams):
            events = stream.transitions
            index = indices[side_index]
            while index < len(events) and int(events[index]["time_ps"]) <= time_ps:
                if index % 1024 == 0:
                    check_cancelled()
                if index and int(events[index]["time_ps"]) < int(events[index - 1]["time_ps"]):
                    invalid_order = True
                    break
                values[side_index] = bit_value(events[index].get("value"), stream.width)
                index += 1
                result["transitions_compared"] += 1
            indices[side_index] = index
        if invalid_order:
            gap("transition_order_invalid", at=time_ps)
            break
        examined += 1
        for side, stream, value in zip(("a", "b"), streams, values):
            if stream.error:
                gap(stream.error, side, time_ps)
            elif value is None:
                gap("value_unavailable", side, time_ps)
            elif not known(value):
                gap("value_unknown", side, time_ps)
        if all(known(value) for value in values) and values[0] != values[1]:
            result.update(diverged=True, first_divergence_time_ps=time_ps,
                          value_a=values[0], value_b=values[1], comparison_status="different")
            result["earliest_difference_proven"] = not any(
                g["start_ps"] <= time_ps for g in gaps
            )
            break
        upcoming = [int(s.transitions[i]["time_ps"]) for s, i in zip(streams, indices)
                    if i < len(s.transitions)]
        if not upcoming:
            break
        time_ps = min(upcoming)

    for path, identity in identities.items():
        if identity is not None and identity != file_identity(path):
            gap("waveform_changed")
            result.update(diverged=False, first_divergence_time_ps=None,
                          value_a=None, value_b=None, earliest_difference_proven=False,
                          comparison_status="inconclusive")
    result["missing_a"], result["missing_b"] = (s.missing for s in streams)
    if not result["diverged"] and examined and not gaps:
        result["comparison_status"] = "equal"
        result["note"] = "known values agree across the complete comparison window"
    result["coverage_status"] = "none" if not examined else ("partial" if gaps else "complete")
    if result["diverged"] and result["first_divergence_time_ps"] < effective_end:
        # A first-difference search intentionally does not inspect the suffix.
        result["coverage_status"] = "partial"
        gaps.append({"reason": "stopped_at_difference", "side": "both",
                     "start_ps": result["first_divergence_time_ps"], "end_ps": effective_end})
    return result


def compare_signals(*, get_parser: Callable, wave_path_a: str, signal_a: str,
                    wave_path_b: str, signal_b: str, start_ps: int = 0,
                    end_ps: int = -1, consume: Callable | None = None) -> dict:
    """One explicitly mapped pair. Never infer a global earliest across pairs."""
    from .waveform_batch import event_readers, EventPagingUnavailable
    if start_ps < 0 or end_ps < -1 or (end_ps >= 0 and end_ps < start_ps):
        raise ValueError('comparison window requires 0 <= start <= end, or end=-1')
    args = dict(get_parser=get_parser, wave_path_a=wave_path_a, signal_a=signal_a,
                wave_path_b=wave_path_b, signal_b=signal_b, start_ps=start_ps,
                end_ps=end_ps, consume=consume)
    identities = {p: file_identity(p) for p in (wave_path_a, wave_path_b)}
    failed_side = 'both'
    try:
        parsers = {p: get_parser(p) for p in identities}
        requests = [(parsers[p], signal) for p, signal in
                    ((wave_path_a, signal_a), (wave_path_b, signal_b))]
        # Header is prepared before loading a group: legacy duration recovery
        # may load/unload signals and must never invalidate a resident cursor.
        for side, (parser, signal) in zip(('a', 'b'), requests):
            failed_side = side
            if not getattr(parser, '_supports_event_pages', lambda: False)():
                raise EventPagingUnavailable()
            parser.get_header()
            parser.get_signal_width(signal)
        failed_side = 'both'
        with event_readers(requests, start_ps, end_ps) as readers:
            result = _compare_pages(readers, args)
    except EventPagingUnavailable:
        result = _compare_legacy(**args)
    except OperationCancelled:
        raise
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        # A failed iterator is not permission to replay the request against a
        # different file generation or hide an incomplete time group.
        result = dict(diverged=False, wave_path_a=wave_path_a, wave_path_b=wave_path_b,
            signal_a=signal_a, signal_b=signal_b, start_ps=start_ps, end_ps=end_ps,
            first_divergence_time_ps=None, first_divergence_time_fs=None, value_a=None, value_b=None,
            cursor=None, comparison_status='inconclusive', coverage_status='none',
            earliest_difference_proven=False, transitions_compared=0,
            missing_a=isinstance(exc, KeyError) and failed_side == 'a',
            missing_b=isinstance(exc, KeyError) and failed_side == 'b',
            coverage_gaps=[dict(reason='signal_not_found' if isinstance(exc, KeyError)
                else 'read_failed', side=failed_side, start_ps=start_ps)], next_actions=[],
            reading=dict(mode_a='event_pages_failed', mode_b='event_pages_failed'))
    if any(identity is not None and identity != file_identity(path)
           for path, identity in identities.items()):
        result.update(diverged=False, comparison_status='inconclusive', coverage_status='partial',
                      earliest_difference_proven=False, first_divergence_time_ps=None,
                      first_divergence_time_fs=None, value_a=None, value_b=None)
        result['coverage_gaps'].append(dict(reason='waveform_changed', side='both', start_ps=start_ps))
    return result


def _compare_pages(readers, args):
    from .event_pages import GroupCursor, IncompleteGroup
    start, end = args['start_ps'], args['end_ps']
    ends = [r.end_fs for r in readers if r.end_fs is not None]
    stop = min(ends) if ends else start*1000 - 1
    if end >= 0:
        stop = min(stop, end*1000)
    public_end = (stop + 999)//1000
    result = dict(diverged=False, wave_path_a=args['wave_path_a'], wave_path_b=args['wave_path_b'],
        signal_a=args['signal_a'], signal_b=args['signal_b'], start_ps=start, end_ps=public_end,
        requested_start_ps=start, requested_end_ps=end, first_divergence_time_ps=None,
        first_divergence_time_fs=None, value_a=None, value_b=None, cursor=None,
        transitions_compared=0, missing_a=False, missing_b=False, note=None,
        comparison_status='inconclusive', coverage_status='none', earliest_difference_proven=False,
        coverage_gaps=[], next_actions=[], width_a=readers[0].width, width_b=readers[1].width,
        excluded_intervals=[], reading=dict(mode_a=readers[0].mode, mode_b=readers[1].mode,
            events_read=0, pages_read=0, native_read_calls=0, record_bytes_read=0,
            byte_basis='native_abi' if all(r.native for r in readers) else 'record_estimate'))
    gaps = result['coverage_gaps']
    def gap(reason, side='both', at=None):
        at = start*1000 if at is None else at
        previous = next((g for g in gaps if g['reason'] == reason and g['side'] == side), None)
        if previous is None:
            gaps.append(dict(reason=reason, side=side, start_ps=(at+999)//1000, start_fs=at))
        else:
            previous['end_ps'] = (at+999)//1000
    requested_stop = end*1000 if end >= 0 else max(ends, default=stop)
    for side, reader in zip(('a', 'b'), readers):
        if reader.end_fs is None:
            gap('recorded_range_unavailable', side)
        elif requested_stop > reader.end_fs:
            gap('outside_recorded_range', side, reader.end_fs)
            result['excluded_intervals'].append(dict(side=side, start_ps=(reader.end_fs+999)//1000,
                end_ps=(requested_stop+999)//1000, reason='outside_recorded_range'))
    if stop < start*1000:
        gap('empty_window')
        result['note'] = 'empty effective comparison window'
        return result
    if readers[0].width != readers[1].width:
        gap('width_mismatch')
        return result
    consume = args['consume']
    owner = getattr(consume, '__self__', None)
    checkpoint = getattr(owner, 'check', check_cancelled)
    def account(page):
        if consume is None:
            return
        if hasattr(owner, 'consume_page'):
            owner.consume_page(page)
        else:
            consume(SignalStream(transitions=[dict(time_ps=e.time_ps, value=e.value) for e in page.events]))
    cursors = [GroupCursor(reader, checkpoint, account) for reader in readers]
    values = [bit_value(c.predecessor.value, r.width) if c.predecessor else None
              for c, r in zip(cursors, readers)]
    at, examined = start*1000, False
    try:
        while at <= stop:
            checkpoint()
            for i, (cursor, reader) in enumerate(zip(cursors, readers)):
                if cursor.peek() is not None and cursor.peek() < at:
                    # An oversized predecessor can be the unread first row.
                    # Its physical time is correctly before the window; the
                    # missing evidence is a capacity limit, not bad ordering.
                    raise IncompleteGroup('transition_data_truncated' if cursor.page.truncated
                                          else 'transition_order_invalid')
                if cursor.peek() == at:
                    values[i] = bit_value(cursor.take_group(at), reader.width)
            examined = True
            for side, value in zip(('a', 'b'), values):
                if value is None:
                    gap('value_unavailable', side, at)
                elif not known(value):
                    gap('value_unknown', side, at)
            if all(known(v) for v in values) and values[0] != values[1]:
                result.update(diverged=True, comparison_status='different', value_a=values[0], value_b=values[1],
                    first_divergence_time_ps=(at+999)//1000, first_divergence_time_fs=at,
                    earliest_difference_proven=not any(g['start_fs'] <= at for g in gaps))
                if at % 1000:
                    result['note'] = 'exact difference is at first_divergence_time_fs; the rounded ps cursor may observe a later value'
                break
            upcoming = [c.peek() for c in cursors if c.peek() is not None]
            if not upcoming:
                break
            at = min(upcoming)
    except IncompleteGroup as exc:
        gap(str(exc), at=at)
    result['transitions_compared'] = sum(c.compared for c in cursors)
    result['reading'].update(events_read=sum(c.events for c in cursors), pages_read=sum(c.pages for c in cursors),
        record_bytes_read=sum(c.nbytes for c in cursors),
        native_read_calls=sum(c.pages for c in cursors if c.reader.native))
    if not result['diverged'] and examined and not gaps:
        result.update(comparison_status='equal', note='known values agree across the complete comparison window')
    result['coverage_status'] = 'none' if not examined else ('partial' if gaps else 'complete')
    if result['diverged'] and at < stop:
        result['coverage_status'] = 'partial'
        gaps.append(dict(reason='stopped_at_difference', side='both', start_ps=(at+999)//1000, end_ps=public_end))
    return result
