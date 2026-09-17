"""Explicit, one-to-one correspondence; no basename or suffix heuristics."""

import re

_SELECT = re.compile(r"^(.*?)(?:\[(-?\d+)(?::(-?\d+))?\])?$")


def split_selection(path):
    match = _SELECT.fullmatch(path)
    if not match or not match[1] or path != path.strip():
        raise ValueError("signal_mapping_invalid")
    bits = None
    if match[2] is not None:
        left, right = int(match[2]), int(match[3] or match[2])
        if abs(left - right) >= 4096:
            raise ValueError("signal_mapping_width_limit")
        direction = -1 if left > right else 1
        bits = tuple(range(left, right + direction, direction))
    return match[1], bits


def selected_path(signal, bits):
    if not bits:
        return signal
    return signal + (f"[{bits[0]}]" if len(bits) == 1 else f"[{bits[0]}:{bits[-1]}]")


def contains(scope, path):
    return path == scope or path.startswith(scope + ".")


class Mapping:
    def __init__(self, signal_pairs, scope_pairs, root_a, root_b, same_design):
        self.pairs = {}
        reverse = {}
        for pair in [*signal_pairs, {"a": root_a, "b": root_b}]:
            a, b = pair["a"], pair["b"]
            split_selection(a)
            split_selection(b)
            if (
                a in self.pairs
                and self.pairs[a] != b
                or b in reverse
                and reverse[b] != a
            ):
                raise ValueError("mapping_ambiguous")
            self.pairs[a] = b
            reverse[b] = a
        self.scopes = list(scope_pairs)
        for i, pair in enumerate(self.scopes):
            for side in ("a", "b"):
                if not pair[side] or pair[side].strip(".") != pair[side]:
                    raise ValueError("mapping_invalid_scope")
                for previous in self.scopes[:i]:
                    if contains(previous[side], pair[side]) or contains(
                        pair[side], previous[side]
                    ):
                        raise ValueError("mapping_ambiguous")
        self.same_design = same_design

    def target(self, signal, bits):
        selected = selected_path(signal, bits)
        exact = self.pairs.get(selected, self.pairs.get(signal))
        if exact is not None:
            base, explicit = split_selection(exact)
            return base, explicit if explicit is not None else tuple(bits)
        for pair in self.scopes:
            if contains(pair["a"], signal):
                return pair["b"] + signal[len(pair["a"]) :], tuple(bits)
        return (signal, tuple(bits)) if self.same_design else None
