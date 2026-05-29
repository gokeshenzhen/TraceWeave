"""TimeSpec resolution for auto-debug v2 (decision 5, workflow glue).

A *TimeSpec* is the value a time-taking tool accepts. It widens the old
``time_ps: int`` contract to also accept:

- a raw integer (picoseconds) — unchanged, fully backward compatible
- a cursor reference ``"@<name>"`` — resolved through the CursorStore
- a unit-suffixed literal ``"12340ps"`` / ``"12.34ns"`` / ``"5us"`` — converted to ps

What this module deliberately does NOT do yet: arithmetic
(``@name + 3*cycle(clk)``). That belongs to the Lark grammar slated for a
later milestone (see docs/auto-debug-decisions-v2.md, decision 4). Keeping
the resolver arithmetic-free here means every time input across the server
gains cursor + unit support without pulling in the parser.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cursor_store import CursorStore


# Unit → picoseconds. fs is sub-ps; we round to the nearest ps.
_UNIT_TO_PS: dict[str, float] = {
    "fs": 0.001,
    "ps": 1.0,
    "ns": 1_000.0,
    "us": 1_000_000.0,
    "ms": 1_000_000_000.0,
    "s": 1_000_000_000_000.0,
}

_CURSOR_REF = re.compile(r"^@([A-Za-z_][A-Za-z0-9_\-]*)$")
_UNIT_LITERAL = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(fs|ps|ns|us|ms|s)$", re.IGNORECASE)


class TimeSpecError(ValueError):
    """Raised when a TimeSpec string cannot be resolved to a ps integer."""


def resolve_timespec(
    spec: object,
    cursor_store: "CursorStore | None" = None,
    *,
    allow_sentinel: bool = False,
) -> int:
    """Resolve a TimeSpec to picoseconds.

    Args:
        spec: int (ps), ``"@cursor"``, or a unit literal like ``"12.34ns"``.
        cursor_store: required to resolve ``@cursor`` references.
        allow_sentinel: when True, the value ``-1`` passes through
            untouched. Several tools use ``end_time_ps=-1`` to mean
            "end of simulation"; callers that support that sentinel set
            this flag so it is not mistaken for a negative time.

    Raises:
        TimeSpecError: on unparsable strings, unknown cursors, or
            negative resolved times (when not the allowed sentinel).
    """
    if isinstance(spec, bool):
        # bool is an int subclass; reject to avoid True→1ps surprises.
        raise TimeSpecError(f"invalid TimeSpec: {spec!r}")

    if isinstance(spec, int):
        return _check_nonneg(spec, allow_sentinel)

    if isinstance(spec, float):
        if spec != int(spec):
            raise TimeSpecError(
                f"fractional ps not allowed: {spec!r}; use a unit literal like '{spec}ns'"
            )
        return _check_nonneg(int(spec), allow_sentinel)

    if isinstance(spec, str):
        return _resolve_str(spec.strip(), cursor_store, allow_sentinel)

    raise TimeSpecError(f"unsupported TimeSpec type: {type(spec).__name__}")


def _resolve_str(
    text: str,
    cursor_store: "CursorStore | None",
    allow_sentinel: bool,
) -> int:
    if not text:
        raise TimeSpecError("empty TimeSpec string")

    cursor_match = _CURSOR_REF.match(text)
    if cursor_match:
        if cursor_store is None:
            raise TimeSpecError(
                f"cursor reference {text!r} given but no cursor store available"
            )
        name = cursor_match.group(1)
        ref = cursor_store.get(name)
        if ref is None:
            known = ", ".join(c.name for c in cursor_store.list()) or "(none)"
            raise TimeSpecError(
                f"unknown cursor {name!r}; known cursors: {known}"
            )
        return ref.time_ps

    unit_match = _UNIT_LITERAL.match(text)
    if unit_match:
        value = float(unit_match.group(1))
        unit = unit_match.group(2).lower()
        ps = value * _UNIT_TO_PS[unit]
        return _check_nonneg(round(ps), allow_sentinel)

    # Bare numeric string, e.g. "12340" → ps.
    try:
        return _check_nonneg(int(text), allow_sentinel)
    except ValueError:
        pass

    raise TimeSpecError(
        f"cannot parse TimeSpec {text!r}; expected an integer (ps), "
        f"'@cursor', or a unit literal like '12.34ns'"
    )


def _check_nonneg(value: int, allow_sentinel: bool) -> int:
    if value < 0:
        if allow_sentinel and value == -1:
            return value
        raise TimeSpecError(f"resolved time is negative: {value}")
    return value
