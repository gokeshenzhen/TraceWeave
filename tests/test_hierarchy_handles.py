"""Tests for src/hierarchy_handles.py."""

from __future__ import annotations

import os
import time

import pytest

from src.hierarchy_handles import (
    HANDLE_PREFIX,
    HandleStore,
    compute_handle,
)


@pytest.fixture
def compile_log(tmp_path):
    p = tmp_path / "compile.log"
    p.write_text("dummy compile log\n", encoding="utf-8")
    return str(p)


def test_handle_format(compile_log):
    h = compute_handle(compile_log, "vcs")
    assert h.startswith(HANDLE_PREFIX)
    # tbh_ + 8 hex chars
    assert len(h) == len(HANDLE_PREFIX) + 8
    assert all(c in "0123456789abcdef" for c in h[len(HANDLE_PREFIX):])


def test_handle_stable_for_same_inputs(compile_log):
    assert compute_handle(compile_log, "vcs") == compute_handle(compile_log, "vcs")


def test_handle_differs_by_simulator(compile_log):
    assert compute_handle(compile_log, "vcs") != compute_handle(compile_log, "xcelium")


def test_handle_differs_by_path(tmp_path):
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("x", encoding="utf-8")
    b.write_text("x", encoding="utf-8")
    assert compute_handle(str(a), "vcs") != compute_handle(str(b), "vcs")


def test_handle_changes_with_mtime(compile_log):
    h1 = compute_handle(compile_log, "vcs")
    # Bump mtime forward 2 seconds so the change is visible regardless of FS
    # timestamp resolution.
    future = time.time() + 2
    os.utime(compile_log, (future, future))
    h2 = compute_handle(compile_log, "vcs")
    assert h1 != h2


def test_handle_missing_file_returns_stable_value():
    h1 = compute_handle("/no/such/file.log", "vcs")
    h2 = compute_handle("/no/such/file.log", "vcs")
    assert h1 == h2
    assert h1.startswith(HANDLE_PREFIX)


def test_handle_relative_path_normalized(tmp_path, monkeypatch):
    p = tmp_path / "compile.log"
    p.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    h_rel = compute_handle("compile.log", "vcs")
    h_abs = compute_handle(str(p), "vcs")
    assert h_rel == h_abs


def test_handle_empty_inputs():
    # Should not crash; returns a deterministic value.
    h = compute_handle("", "")
    assert h.startswith(HANDLE_PREFIX)


def test_store_register_and_resolve():
    store = HandleStore()
    payload = {"project": {"top_module": "top"}, "files": {}}
    store.register("tbh_abcd1234", payload)
    assert store.resolve("tbh_abcd1234") is payload
    assert "tbh_abcd1234" in store
    assert len(store) == 1


def test_store_resolve_missing_returns_none():
    store = HandleStore()
    assert store.resolve("tbh_deadbeef") is None
    assert "tbh_deadbeef" not in store


def test_store_invalidate_clears_all():
    store = HandleStore()
    store.register("tbh_aaaaaaaa", {"a": 1})
    store.register("tbh_bbbbbbbb", {"b": 2})
    assert len(store) == 2
    store.invalidate()
    assert len(store) == 0
    assert store.resolve("tbh_aaaaaaaa") is None


def test_store_register_rejects_invalid_format():
    store = HandleStore()
    with pytest.raises(ValueError):
        store.register("not_a_handle", {})


def test_store_register_overwrites_same_handle():
    store = HandleStore()
    store.register("tbh_aaaaaaaa", {"v": 1})
    store.register("tbh_aaaaaaaa", {"v": 2})
    assert store.resolve("tbh_aaaaaaaa") == {"v": 2}
    assert len(store) == 1
