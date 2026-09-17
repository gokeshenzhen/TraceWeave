"""Time-aware observation of one normalized driver step under the wave lock."""
from __future__ import annotations

from .cancellation import check_cancelled
from .divergence_compare import bit_value, known, read_stream
from .dynamic_evidence import Expr, evaluate


def clock_edges(stream, edge: str, *, start: int, end: int, before=False):
    gaps = []
    if stream.error:
        return [], [stream.error]
    if stream.truncated:
        gaps.append("transition_data_truncated")
    if stream.scale_fs is not None and stream.scale_fs < 1000:
        gaps.append("sampling_order_unresolved")
    if stream.width != 1:
        gaps.append("clock_alignment_unproven")
    prev = bit_value(stream.predecessor.get("value"), 1) if stream.predecessor else None
    edges = []
    previous_time = None
    for event in stream.transitions:
        check_cancelled()
        t = int(event["time_ps"])
        if t > end or before and t == end:
            break
        value = bit_value(event.get("value"), 1)
        if t == previous_time:
            gaps.append("sampling_order_unresolved")
        if t >= start:
            if known(prev) and known(value):
                if (prev, value) == (("0", "1") if edge == "posedge" else ("1", "0")):
                    edges.append(t)
            elif prev != value:
                # Initial assignment at the start is not a confirmed edge.
                if previous_time is not None or stream.predecessor:
                    gaps.append("clock_alignment_unproven")
        prev, previous_time = value, t
    return edges, list(dict.fromkeys(gaps))


class ObservationReader:
    """Per-node cache; no transition arrays escape this object."""
    def __init__(self, get_parser, wave, start, end, consume=None):
        self.get_parser, self.wave, self.start, self.end = get_parser, wave, start, end
        self.streams = {}
        self.consume = consume

    def stream(self, signal):
        check_cancelled()
        if signal not in self.streams:
            stream = read_stream(self.get_parser, self.wave, signal, self.start, self.end)
            if self.consume:
                self.consume(stream)
            self.streams[signal] = stream
        return self.streams[signal]

    def sample(self, expr: Expr, time: int, phase="after") -> dict:
        stream = self.stream(expr.signal)
        gaps = []
        event = stream.predecessor
        same_time = []
        for index, current in enumerate(stream.transitions):
            if index % 1024 == 0:
                check_cancelled()
            t = int(current["time_ps"])
            if t > time:
                break
            if t == time:
                same_time.append(current)
            if t < time or phase == "after":
                event = current
        if stream.error:
            gaps.append("signal_not_dumped" if stream.missing else stream.error)
        if stream.truncated and (stream.safe_before < time or phase == "after" and stream.safe_before == time):
            gaps.append("transition_data_truncated")
            event = None
        if stream.scale_fs is not None and stream.scale_fs < 1000:
            gaps.append("sampling_order_unresolved")
        declared = expr.declared_bits
        bits = bit_value(event.get("value"), stream.width) if event else None
        if not declared or stream.width != len(declared) or any(b not in declared for b in expr.bits):
            gaps.append("dynamic_bit_mapping_unavailable")
            bits = None
        elif bits is not None:
            bits = "".join(bits[declared.index(b)] for b in expr.bits)
        if phase == "before" and same_time:
            # Even integer-ps records cannot order active/TB/NBA regions.
            previous = bit_value(event.get("value"), stream.width) if event else None
            if any(bit_value(e.get("value"), stream.width) != previous for e in same_time):
                gaps.append("sampling_order_unresolved")
        if time < self.start or time > stream.end:
            gaps.append("history_window_exhausted")
            bits = None
        return dict(value=bits, time_ps=time, phase=phase,
                    predecessor_time_ps=int(event["time_ps"]) if event else None,
                    gaps=list(dict.fromkeys(gaps)))


def observe_step(step: dict, *, get_parser, wave: str, time: int, history_start: int,
                 phase="after", consume=None) -> dict:
    reader = ObservationReader(get_parser, wave, history_start, time, consume)
    result = dict(observation_time_ps=time, observation_phase=phase, sampling_time_ps=time,
                  sampling_phase=phase, trigger_time_ps=None, dependencies=[], branches=[],
                  value=None, complete=False, gaps=list(step.get("gaps", ())), boundary=step["boundary"])
    if step["boundary"] in {"input", "unsupported"} or not step.get("complete"):
        return result
    sample_time, sample_phase = time, phase
    if step["boundary"] == "sequential":
        clock = Expr.from_dict(step["clock"]["expression"])
        edges, gaps = clock_edges(reader.stream(clock.signal), step["clock"]["edge"],
                                  start=history_start, end=time, before=phase == "before")
        result["gaps"].extend(gaps)
        if not edges:
            result["gaps"].append("history_window_exhausted")
            return result
        sample_time, sample_phase = edges[-1], "before"
        result.update(trigger_time_ps=sample_time, sampling_time_ps=sample_time, sampling_phase=sample_phase)
    def sample(expr):
        return reader.sample(expr, sample_time, sample_phase)
    selected = []
    unresolved = False
    for branch in sorted(step["branches"], key=lambda b: b["order"]):
        guard = evaluate(Expr.from_dict(branch["guard"]), sample, "control")
        result["dependencies"].extend(guard.dependencies)
        result["gaps"].extend(guard.gaps)
        result["branches"].append({"id": branch["id"], "state": guard.truth, "value": guard.value})
        if guard.truth == "true":
            selected = [branch]  # Last assignment in this proven single process wins.
        elif guard.truth in {"unknown", "unsupported"}:
            unresolved = True
            selected.append(branch)
    if unresolved:
        result["gaps"].append("guard_unresolved")
    if not selected and not unresolved:
        if step["boundary"] == "sequential":
            # No assignment executes: the predecessor Q is the data source.
            bits = tuple(step["bits"])
            expr = Expr("signal", len(bits), signal=step["signal"].split("[")[0], bits=bits,
                        declared_bits=tuple(step.get("declared_bits", bits)))
            value = evaluate(expr, sample)
            result["action"] = "hold"
            result["dependencies"].extend(value.dependencies)
            result["gaps"].extend(value.gaps)
            result["value"] = value.value
        else:
            result["gaps"].append("temporal_context_unavailable")
    else:
        for branch in selected:
            value = evaluate(Expr.from_dict(branch["value"]), sample)
            result["dependencies"].extend(value.dependencies)
            result["gaps"].extend(value.gaps)
            result["branches"].extend(value.branches)
            if not unresolved:
                result["value"] = value.value
    result["gaps"] = list(dict.fromkeys(result["gaps"]))
    result["complete"] = not result["gaps"] and known(result["value"])
    return result
