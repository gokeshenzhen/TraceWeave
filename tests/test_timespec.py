"""Unit tests for src.timespec.

TimeSpec is the widened time input contract for auto-debug v2: integers
(ps), cursor references, and unit literals. These tests pin the
resolution rules and the backward-compatibility guarantee that plain
integers pass through untouched.
"""

from __future__ import annotations

import pytest

from src.cursor_store import CursorStore
from src.timespec import TimeSpecError, resolve_timespec


# ----------------------------------------------------------------------
# integer passthrough (backward compatibility)
# ----------------------------------------------------------------------


def test_int_passthrough():
    assert resolve_timespec(0) == 0
    assert resolve_timespec(12_340) == 12_340


def test_negative_int_rejected_without_sentinel():
    with pytest.raises(TimeSpecError):
        resolve_timespec(-1)


def test_negative_one_allowed_as_sentinel():
    assert resolve_timespec(-1, allow_sentinel=True) == -1


def test_other_negative_rejected_even_with_sentinel():
    with pytest.raises(TimeSpecError):
        resolve_timespec(-5, allow_sentinel=True)


def test_bool_rejected():
    with pytest.raises(TimeSpecError):
        resolve_timespec(True)


def test_integral_float_accepted():
    assert resolve_timespec(1000.0) == 1000


def test_fractional_float_rejected():
    with pytest.raises(TimeSpecError):
        resolve_timespec(12.5)


# ----------------------------------------------------------------------
# unit literals
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0ps", 0),
        ("12340ps", 12_340),
        ("12.34ns", 12_340),
        ("5ns", 5_000),
        ("1us", 1_000_000),
        ("2ms", 2_000_000_000),
        ("1500fs", 2),  # 1500fs = 1.5ps → rounds to 2
        ("12.34 ns", 12_340),  # whitespace tolerated
        ("5NS", 5_000),  # case-insensitive unit
    ],
)
def test_unit_literals(text, expected):
    assert resolve_timespec(text) == expected


def test_bare_numeric_string_is_ps():
    assert resolve_timespec("12340") == 12_340


def test_garbage_string_rejected():
    for bad in ("", "abc", "12.3.4ns", "ns", "@", "12 ns ns"):
        with pytest.raises(TimeSpecError):
            resolve_timespec(bad)


# ----------------------------------------------------------------------
# cursor references
# ----------------------------------------------------------------------


def test_cursor_reference_resolves():
    store = CursorStore()
    store.set("div_point", 42_000)
    assert resolve_timespec("@div_point", store) == 42_000


def test_cursor_reference_without_store_raises():
    with pytest.raises(TimeSpecError):
        resolve_timespec("@div_point", None)


def test_unknown_cursor_raises_with_known_list():
    store = CursorStore()
    store.set("known_one", 1)
    with pytest.raises(TimeSpecError) as exc:
        resolve_timespec("@nope", store)
    assert "known_one" in str(exc.value)


def test_cursor_with_dash_and_underscore():
    store = CursorStore()
    store.set("div_3a7c-x", 7)
    assert resolve_timespec("@div_3a7c-x", store) == 7
