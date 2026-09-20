"""Bounded projections of one exact dump declaration, in declared coordinates.

Strings retain their historical parser behavior. Structured selections never
search by suffix or combine independently dumped slices. Their ordered bits use
the same BitRange / SignalSelection contract as source connectivity.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps, cached_property
import hashlib
import json
import re

from .cancellation import check_cancelled
from .connectivity_ir import BitRange, SignalSelection
from .divergence_compare import bit_value

MAX_SELECTIONS = 128
MAX_BITS = 65536
MAX_VALUE_BITS = 4096  # Bound numeric output as well as the four-state vector.
MAX_PROJECTED_CELLS = 1_048_576
MAX_PROJECTED_BITS = 16 * 1024 * 1024
MAX_TRANSITIONS = 262144


def declaration_range(path: str, width: int) -> BitRange:
    """A trailing dump range is a declaration, never a requested projection."""
    match = re.search(r"\[(-?\d+)(?::(-?\d+))?\]$", path)
    # Escaped identifiers may contain brackets; only a separately delimited
    # range is syntax in that case.
    leaf = path.rsplit("\\", 1)
    if len(leaf) == 2 and not re.search(r"\s", leaf[1]):
        match = None
    if match:
        left = int(match[1])
        right = int(match[2]) if match[2] is not None else left
        declared = BitRange(left, right)
        if declared.width != width:
            raise ValueError("dump declaration range and width disagree")
        return declared
    return BitRange.from_width(width)


@dataclass(frozen=True)
class Projection:
    path: str
    declared: BitRange
    selection: SignalSelection

    @cached_property
    def key(self):
        bits = self.selection.bits
        if len(bits) == 1 or (abs(bits[1] - bits[0]) == 1 and
                all(b - a == bits[1] - bits[0] for a, b in zip(bits, bits[1:]))):
            suffix = f"{bits[0]}:{bits[-1]}"
        else:
            suffix = hashlib.sha256(json.dumps(bits).encode()).hexdigest()
        # A compact display key, not a fabricated hierarchical signal path.
        return self.path + "@bits(" + suffix + ")"

    def source_path(self):
        bits = self.selection.bits
        if "\\" in self.path:
            return self.path
        if len(bits) > 1 and not (abs(bits[1] - bits[0]) == 1 and
                all(b - a == bits[1] - bits[0] for a, b in zip(bits, bits[1:]))):
            return self.path
        base = re.sub(r"\[-?\d+(?::-?\d+)?\]$", "", self.path)
        return SignalSelection(symbol=base, bits=bits).path(include_bits=True)

    def value(self, value):
        check_cancelled()
        if value is None:
            return None
        raw = bit_value(value, self.declared.width)
        if raw is None:
            raise ValueError("selection requires binary dump data within the declared width")
        step = -1 if self.declared.left > self.declared.right else 1
        bits = "".join(raw[(bit - self.declared.left) * step] for bit in self.selection.bits)
        known = all(bit in "01" for bit in bits)
        number = int(bits, 2) if known else None
        return {"bin": bits, "hex": f"0x{number:0{max(1, (len(bits)+3)//4)}x}" if known else None,
                "dec": number}

    def receipt(self):
        return {"key": self.key, "path": self.path,
                "declared_range": {"left": self.declared.left, "right": self.declared.right},
                "bits": list(self.selection.bits), "width": self.selection.width}


class SelectionParser:
    """Request-local adapter. No global cache, native handles, or lock ownership."""

    def __init__(self, parser):
        self.parser = parser
        self.projections: dict[str, Projection] = {}
        self._declarations = {}

    def __getattr__(self, name):
        return getattr(self.parser, name)

    def bind(self, spec):
        if spec is None or isinstance(spec, str):
            return spec
        from .schemas import WaveformSelection
        spec = WaveformSelection.model_validate(spec)
        check_cancelled()
        if spec.path not in self._declarations:
            getter = getattr(self.parser, "get_signal_declaration", None)
            if getter is None:
                raise ValueError("exact dump declaration evidence unavailable")
            self._declarations[spec.path] = getter(spec.path)
        declaration = self._declarations[spec.path]
        path, width = declaration["path"], declaration["width"]
        self._declarations[path] = declaration
        if width > MAX_BITS:
            raise ValueError("dump width exceeds selection bit budget")
        declared = BitRange(**declaration["declared_range"])
        if declared.width != width:
            raise ValueError("dump declaration range and width disagree")
        if spec.bits is not None:
            bits = tuple(spec.bits)
        else:
            # lsb is a DECLARED index; width extends toward the declaration's
            # left bound. Thus [0:15], lsb=15, width=4 selects [12:15].
            direction = 1 if declared.left >= declared.right else -1
            bits = BitRange(spec.lsb + direction * (spec.width - 1), spec.lsb).indices
        if min(bits) < min(declared.left, declared.right) or max(bits) > max(declared.left, declared.right):
            raise ValueError("selection contains bits outside the dump declaration")
        selection = SignalSelection(symbol=path, bits=bits)
        projection = Projection(path, declared, selection)
        if projection.key in self.projections:
            return projection.key
        if len(self.projections) >= MAX_SELECTIONS:
            raise ValueError("selection count limit exceeded")
        if sum(p.selection.width for p in self.projections.values()) + selection.width > MAX_BITS:
            raise ValueError("total selection bit budget exceeded")
        self.projections[projection.key] = projection
        return projection.key

    def receipts(self):
        return [p.receipt() for p in self.projections.values()]

    def get_signal_width(self, path):
        check_cancelled()
        if path in self.projections:
            return self.projections[path].selection.width
        return self.parser.get_signal_width(path)

    def get_value_at_time(self, path, time_ps):
        check_cancelled()
        p = self.projections.get(path)
        if p is None:
            return self.parser.get_value_at_time(path, time_ps)
        self._validate(p)
        result = self.parser.get_value_at_time(p.path, time_ps)
        return {**result, "signal": path, "value": p.value(result.get("value"))}

    def _validate(self, projection):
        current = self.parser.get_signal_declaration(projection.path)
        if current != self._declarations.get(projection.path, current):
            raise ValueError("dump declaration changed during selection")

    def get_transitions(self, path, start_ps=0, end_ps=-1):
        check_cancelled()
        p = self.projections.get(path)
        if p is None:
            return self.parser.get_transitions(path, start_ps=start_ps, end_ps=end_ps)
        self._validate(p)
        result = self.parser.get_transitions(p.path, start_ps=start_ps, end_ps=end_ps)
        rows = result.get("transitions", [])
        if len(rows) > MAX_TRANSITIONS or len(rows) * p.selection.width > MAX_PROJECTED_BITS:
            raise ValueError("selection projection budget exceeded; narrow the time window")
        predecessor = result.get("predecessor")
        predecessor = ({**predecessor, "value": p.value(predecessor.get("value"))}
                       if predecessor else None)
        projected, previous = [], predecessor.get("value") if predecessor else None
        for row in rows:
            check_cancelled()
            value = p.value(row.get("value"))
            if (not projected and previous is None) or value != previous:
                projected.append({**row, "value": value})
            previous = value
        return {**result, "signal": path, "transitions": projected,
                "transition_count": len(projected), "predecessor": predecessor,
                "predecessor_kind": "declaration_anchor"}

    def sample_columns(self, paths, edges, offset, sample_times, session):
        """Read and sample each backing declaration once, then project fields."""
        from .cycle_query import _sample_signal_columns_at_edges
        groups = {}
        for path in dict.fromkeys(paths):
            projection = self.projections.get(path)
            base = projection.path if projection else path
            storage = ("selection", self._declarations[base].get("storage_key", base)) if projection else ("signal", base)
            if storage not in groups:
                groups[storage] = (base, [])
            groups[storage][1].append((path, projection))
        projected = [p for path in paths if (p := self.projections.get(path))]
        if (len(edges) * len(projected) > MAX_PROJECTED_CELLS or
                len(edges) * sum(p.selection.width for p in projected) > MAX_PROJECTED_BITS):
            raise ValueError("selection sample budget exceeded; narrow the time window")
        columns, errors, truncated = {}, {}, []
        for base, members in groups.values():
            check_cancelled()
            for _, p in members:
                if p:
                    self._validate(p)
            raw, failed, limited = _sample_signal_columns_at_edges(
                self.parser, [base], edges, offset, sample_times=sample_times,
                sampling_session=session,
                safe_prefix_only=True,
            )
            for path, projection in members:
                if base in failed:
                    errors[path] = failed[base]
                elif projection is None:
                    columns[path] = raw.get(base, [])
                else:
                    values, last_raw, last_value = [], object(), None
                    for value in raw.get(base, []):
                        check_cancelled()
                        if value is not last_raw:
                            last_value = projection.value(value)
                            last_raw = value
                        values.append(last_value)
                    columns[path] = values
                if base in limited:
                    truncated.append(path)
        return columns, errors, truncated

    def get_signals_around_time(self, paths, center_ps, window_ps=500, extra_transitions=5):
        bases = list(dict.fromkeys(self.projections[p].path if p in self.projections else p
                                  for p in paths))
        for path in paths:
            if path in self.projections:
                self._validate(self.projections[path])
        raw = self.parser.get_signals_around_time(bases, center_ps, window_ps, extra_transitions)
        signals = {}
        work = 0
        for path in paths:
            check_cancelled()
            projection = self.projections.get(path)
            entry = raw["signals"].get(projection.path if projection else path, {})
            if projection is None or "error" in entry:
                signals[path] = entry
                continue
            mapped = {**entry, "value_at_center": projection.value(entry.get("value_at_center"))}
            for name in ("transitions_in_window", "pre_window_transitions"):
                rows = entry.get(name, [])
                work += len(rows) * projection.selection.width
                if work > MAX_PROJECTED_BITS or len(rows) > MAX_TRANSITIONS:
                    raise ValueError("selection projection budget exceeded; narrow the time window")
                mapped[name] = [{**r, "value": projection.value(r.get("value"))} for r in rows]
            signals[path] = mapped
        return {**raw, "signals": signals}


def prepare_selections(parser, values):
    """Bind explicit selections; all-string callers retain their old parser."""
    def has_selection(v):
        return any(has_selection(x) for x in v) if isinstance(v, list) else isinstance(v, dict)
    if not any(has_selection(v) for v in values.values()):
        return parser, values
    adapter = parser if isinstance(parser, SelectionParser) else SelectionParser(parser)
    def bind(v):
        return [bind(x) for x in v] if isinstance(v, list) else adapter.bind(v)
    return adapter, {key: bind(value) for key, value in values.items()}


def attach_selections(result, parser):
    if isinstance(parser, SelectionParser):
        result["selections"] = parser.receipts()
        for action in result.get("next_actions", []):
            projection = parser.projections.get(action.get("signal_path"))
            if projection:
                action["signal_path"] = projection.source_path()
                action["signal_selection"] = {"path": projection.path, "bits": list(projection.selection.bits)}
    return result


def selection_inputs(*names):
    """Keep protocol engines on string keys while accepting structured inputs."""
    def decorate(function):
        @wraps(function)
        def call(**kwargs):
            parser = kwargs["get_parser"](kwargs["wave_path"])
            parser, bound = prepare_selections(parser, {n: kwargs[n] for n in names if n in kwargs})
            result = function(**{**kwargs, **bound, "get_parser": lambda _: parser})
            return attach_selections(result, parser)
        return call
    return decorate
