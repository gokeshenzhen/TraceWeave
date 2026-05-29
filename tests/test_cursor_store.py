"""Unit tests for src.cursor_store.

Cursors are the auto-debug v2 time-anchor primitive (decision 5). The
store is intentionally simple — these tests pin the contract the rest of
the auto-debug pipeline depends on.
"""

from __future__ import annotations

import pytest

from src.cursor_store import CursorStore


def test_set_and_get_roundtrip():
    store = CursorStore()
    ref = store.set("stall_start", 12_340, note="rready never deasserts")
    assert ref.name == "stall_start"
    assert ref.time_ps == 12_340
    assert ref.note == "rready never deasserts"
    assert store.get("stall_start") == ref
    assert "stall_start" in store
    assert len(store) == 1


def test_set_overwrites_by_default():
    store = CursorStore()
    store.set("anchor", 100)
    store.set("anchor", 200, note="updated")
    ref = store.get("anchor")
    assert ref.time_ps == 200
    assert ref.note == "updated"


def test_set_overwrite_false_raises_on_collision():
    store = CursorStore()
    store.set("anchor", 100)
    with pytest.raises(ValueError):
        store.set("anchor", 200, overwrite=False)


def test_set_rejects_invalid_name():
    store = CursorStore()
    for bad in ("", "1foo", "foo bar", "foo.bar", "foo/bar"):
        with pytest.raises(ValueError):
            store.set(bad, 0)


def test_set_rejects_negative_time():
    store = CursorStore()
    with pytest.raises(ValueError):
        store.set("foo", -1)


def test_auto_set_is_deterministic_on_seed():
    store = CursorStore()
    ref1 = store.auto_set(1_000, prefix="div", seed="sigA|sigB")
    ref2 = store.auto_set(1_000, prefix="div", seed="sigA|sigB")
    # Same seed AND same time → same name AND single entry.
    assert ref1.name == ref2.name
    assert len(store) == 1


def test_auto_set_disambiguates_on_time_collision():
    store = CursorStore()
    ref1 = store.auto_set(1_000, prefix="div", seed="x")
    ref2 = store.auto_set(2_000, prefix="div", seed="x")
    # Same seed, different time → must produce distinct name.
    assert ref1.name != ref2.name
    assert ref1.time_ps == 1_000
    assert ref2.time_ps == 2_000


def test_list_orders_by_time_then_name():
    store = CursorStore()
    store.set("c", 30)
    store.set("a", 10)
    store.set("b", 20)
    names = [ref.name for ref in store.list()]
    assert names == ["a", "b", "c"]


def test_delete_returns_existed_flag():
    store = CursorStore()
    store.set("foo", 1)
    assert store.delete("foo") is True
    assert store.delete("foo") is False
    assert "foo" not in store


def test_resolve_time_raises_on_unknown():
    store = CursorStore()
    with pytest.raises(KeyError):
        store.resolve_time("nope")


def test_clear_empties_the_store():
    store = CursorStore()
    store.set("a", 1)
    store.set("b", 2)
    store.clear()
    assert len(store) == 0
    assert store.list() == []


def test_metadata_is_isolated_per_call():
    store = CursorStore()
    meta = {"key": "value"}
    ref = store.set("x", 1, metadata=meta)
    meta["key"] = "mutated"
    # Stored metadata must not reflect the post-call mutation.
    assert store.get("x").metadata["key"] == "value"
    # And the returned dict on as_dict is a copy too.
    d = ref.as_dict()
    d["metadata"]["key"] = "again"
    assert store.get("x").metadata["key"] == "value"
