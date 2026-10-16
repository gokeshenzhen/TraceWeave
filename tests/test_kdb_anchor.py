"""Bounded KDB candidate/generation facts, using local files and no EDA.

Map-byte hashes and directory fixtures are the independent oracle. A complete
observation of this five-file boundary is neither full KDB integrity evidence
nor proof of NPI loadability or association with the current source files.
"""
from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from src import kdb_anchor
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.kdb_anchor import capture_kdb_anchor


MAP_NAMES = {"_libFileMap", "_nstnElabDB", "_smElabDBMap", "_stnElabDB", ".hasElabcomError"}
BASE_GAPS = {"kdb_identity_bounded", "kdb_source_association_unverified"}


def _directory(root, contents=None):
    root.mkdir(parents=True)
    for name, raw in (contents or {}).items():
        (root / name).write_bytes(raw)
    return root


def _elaborated(root):
    return _directory(root, {"_libFileMap": b"library_map\n", "_stnElabDB": b"elaboration_map\n"})


def _records(anchor):
    return {record.name: record for record in anchor.map_records}


def _observe_map_reads(monkeypatch, path, after_read):
    original = kdb_anchor.os.read
    stat = path.stat()

    def observed(fd, size):
        opened = os.fstat(fd)
        data = original(fd, size)
        if (opened.st_dev, opened.st_ino) == (stat.st_dev, stat.st_ino):
            after_read(size, data)
        return data

    monkeypatch.setattr(kdb_anchor.os, "read", observed)


@pytest.mark.parametrize("contents,status", [
    ({}, "empty"),
    ({"unrelated.payload": b"not a map"}, "unrecognized"),
    ({"_libFileMap": b""}, "unrecognized"),
    ({"_stnElabDB": b"db_without_library_map"}, "unrecognized"),
    ({"_libFileMap": b"lib"}, "source_only_candidate"),
    ({"_libFileMap": b"lib", "_stnElabDB": b""}, "source_only_candidate"),
    ({"_libFileMap": b"lib", "_smElabDBMap": b"sm"}, "source_only_candidate"),
    ({"_libFileMap": b"lib", "_stnElabDB": b"db"}, "elaborated_candidate"),
    ({"_libFileMap": b"lib", "_nstnElabDB": b"db"}, "elaborated_candidate"),
    ({".hasElabcomError": b""}, "elaboration_error"),
    ({"_libFileMap": b"lib", "_stnElabDB": b"db", ".hasElabcomError": b"private compiler diagnostic"}, "elaboration_error"),
])
def test_candidate_status_truth_table_does_not_claim_loadability(tmp_path, contents, status):
    root = _directory(tmp_path / "kdb", contents)
    anchor = capture_kdb_anchor(str(root))
    assert anchor.artifact_status == status
    assert anchor.receipt()["load_status"] == "not_checked"
    assert anchor.receipt()["source_association"] == "unverified"
    assert BASE_GAPS <= set(anchor.gaps)


def test_missing_root_is_reported_as_missing_not_as_loadable(tmp_path):
    anchor = capture_kdb_anchor(str(tmp_path / "absent"))
    assert anchor.artifact_status == "missing"
    assert anchor.map_records == ()
    assert anchor.receipt()["load_status"] == "not_checked"


def test_regular_file_instead_of_directory_is_partial_unrecognized(tmp_path):
    root = tmp_path / "not_a_directory"
    root.write_bytes(b"not a KDB directory")
    anchor = capture_kdb_anchor(str(root))
    assert anchor.artifact_status == "unrecognized"
    assert not anchor.identity_complete
    assert "kdb_not_directory" in anchor.gaps


@pytest.mark.parametrize("failure", ["stat", "scandir"])
def test_unreadable_root_is_unknown_and_partial_not_empty(tmp_path, monkeypatch, failure):
    root = _directory(tmp_path / "kdb")
    original_stat, original_scandir = os.stat, os.scandir

    def stat(path, *args, **kwargs):
        if failure == "stat" and os.path.abspath(path) == str(root):
            raise PermissionError(errno.EACCES, "private root stat error", str(path))
        return original_stat(path, *args, **kwargs)

    def scandir(path):
        if failure == "scandir" and os.path.abspath(path) == str(root):
            raise PermissionError(errno.EACCES, "private root read error", str(path))
        return original_scandir(path)

    monkeypatch.setattr(kdb_anchor.os, "stat", stat)
    monkeypatch.setattr(kdb_anchor.os, "scandir", scandir)
    anchor = capture_kdb_anchor(str(root))
    assert anchor.artifact_status == "unrecognized"
    assert not anchor.identity_complete
    assert "kdb_root_unreadable" in anchor.gaps
    assert str(root) not in json.dumps(anchor.receipt())


def test_all_five_map_records_are_immutable_exact_raw_byte_facts(tmp_path):
    raw = {
        "_libFileMap": b"lib\r\n\xff\x00",
        "_nstnElabDB": b"nstn\n",
        "_smElabDBMap": b"sm\rmap",
        "_stnElabDB": b"stn\x00map",
        ".hasElabcomError": b"",
    }
    root = _directory(tmp_path / "kdb", raw)
    first, second = capture_kdb_anchor(str(root)), capture_kdb_anchor(str(root))
    assert first.identity_complete
    assert len(first.identity_sha256) == 64
    assert first.identity_sha256 == second.identity_sha256
    assert set(_records(first)) == MAP_NAMES
    for name, data in raw.items():
        record = _records(first)[name]
        assert record.size == len(data)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
        assert record.canonical_path == str((root / name).resolve())
        assert not hasattr(record, "text") and not hasattr(record, "raw_bytes")
    with pytest.raises(FrozenInstanceError):
        first.artifact_status = "loadable"
    with pytest.raises(FrozenInstanceError):
        first.map_records[0].sha256 = "0" * 64


def test_receipt_exposes_only_bounded_identity_and_unverified_association(tmp_path):
    root = _elaborated(tmp_path / "PRIVATE_ARTIFACT_PATH")
    anchor = capture_kdb_anchor(str(root))
    receipt = anchor.receipt()
    assert set(receipt) == {
        "artifact_status", "identity_sha256", "identity_basis", "identity_status",
        "load_status", "source_association", "gaps",
    }
    assert receipt["artifact_status"] == "elaborated_candidate"
    assert receipt["identity_sha256"] == anchor.identity_sha256
    assert receipt["identity_basis"] == "bounded_elaboration_maps"
    assert receipt["identity_status"] == "complete"
    assert receipt["load_status"] == "not_checked"
    assert receipt["source_association"] == "unverified"
    assert BASE_GAPS <= set(receipt["gaps"])
    assert str(tmp_path) not in json.dumps(receipt)
    assert "PRIVATE_ARTIFACT_PATH" not in json.dumps(receipt)
    receipt["gaps"].clear()
    assert BASE_GAPS <= set(anchor.gaps)


@pytest.mark.parametrize("name", sorted(MAP_NAMES))
def test_each_fixed_map_content_change_invalidates_generation(tmp_path, name):
    contents = {item: b"old" for item in MAP_NAMES}
    root = _directory(tmp_path / "kdb", contents)
    anchor = capture_kdb_anchor(str(root))
    path = root / name
    before = path.stat()
    path.write_bytes(b"new")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert anchor.current() is False
    assert capture_kdb_anchor(str(root)).identity_sha256 != anchor.identity_sha256


@pytest.mark.parametrize("change", ["add", "remove", "atomic_replace"])
def test_map_inventory_and_file_replacement_invalidate_generation(tmp_path, change):
    root = _elaborated(tmp_path / "kdb")
    anchor = capture_kdb_anchor(str(root))
    if change == "add":
        (root / "_smElabDBMap").write_bytes(b"new map")
    elif change == "remove":
        (root / "_stnElabDB").unlink()
    else:
        replacement = root / "replacement.tmp"
        replacement.write_bytes((root / "_stnElabDB").read_bytes())
        replacement.replace(root / "_stnElabDB")
    assert anchor.current() is False


def test_raw_hash_detects_same_size_change_even_with_fully_coarsened_stat(tmp_path, monkeypatch):
    root = _elaborated(tmp_path / "kdb")
    target = root / "_libFileMap"
    anchor = capture_kdb_anchor(str(root))
    old = target.stat()
    target.write_bytes(b"X" * old.st_size)
    os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    original_stat, original_fstat = os.stat, os.fstat

    def coarse(result):
        return old if (result.st_dev, result.st_ino) == (old.st_dev, old.st_ino) else result

    monkeypatch.setattr(kdb_anchor.os, "stat", lambda path, *args, **kwargs: coarse(original_stat(path, *args, **kwargs)))
    monkeypatch.setattr(kdb_anchor.os, "fstat", lambda fd: coarse(original_fstat(fd)))
    assert anchor.current() is False


def test_root_alias_preserves_original_spelling_and_retarget_invalidates(tmp_path):
    first, second = _elaborated(tmp_path / "first"), _elaborated(tmp_path / "second")
    alias = tmp_path / "chosen_kdb"
    alias.symlink_to(first, target_is_directory=True)
    anchor = capture_kdb_anchor(str(alias))
    assert anchor.path == str(alias.absolute())
    assert anchor.canonical_path == str(first.resolve())
    assert anchor.current()
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    assert anchor.current() is False


def test_map_symlink_retarget_invalidates_even_when_target_bytes_are_equal(tmp_path):
    root = _directory(tmp_path / "kdb", {"_stnElabDB": b"db"})
    first, second = tmp_path / "map_first", tmp_path / "map_second"
    first.write_bytes(b"equal")
    second.write_bytes(b"equal")
    alias = root / "_libFileMap"
    alias.symlink_to(first)
    anchor = capture_kdb_anchor(str(root))
    alias.unlink()
    alias.symlink_to(second)
    assert anchor.current() is False


def test_replacing_root_directory_invalidates_even_with_identical_map_bytes(tmp_path):
    root = _elaborated(tmp_path / "kdb")
    anchor = capture_kdb_anchor(str(root))
    root.rename(tmp_path / "old_generation")
    _elaborated(root)
    assert anchor.current() is False


def test_npi_lock_creation_and_removal_do_not_change_generation(tmp_path):
    root = _elaborated(tmp_path / "kdb")
    anchor = capture_kdb_anchor(str(root))
    lock = root / "temporary_npi.lock"
    lock.write_bytes(b"lock ownership")
    changed = root.stat()
    os.utime(root, ns=(changed.st_atime_ns, changed.st_mtime_ns + 1_000_000_000))
    assert anchor.current()
    locked = capture_kdb_anchor(str(root))
    assert locked.root_identity == anchor.root_identity
    assert locked.identity_sha256 == anchor.identity_sha256
    lock.unlink()
    assert anchor.current()


def test_unmapped_payload_change_is_explicitly_outside_identity_boundary(tmp_path):
    root = _elaborated(tmp_path / "kdb")
    payload = root / "opaque_database.payload"
    payload.write_bytes(b"old database payload")
    anchor = capture_kdb_anchor(str(root))
    payload.write_bytes(b"different database payload")
    assert anchor.current(), "this boundary hashes fixed maps, not arbitrary database payloads"
    assert BASE_GAPS <= set(anchor.receipt()["gaps"])
    assert anchor.receipt()["identity_basis"] == "bounded_elaboration_maps"
    assert anchor.receipt()["load_status"] == "not_checked"


@pytest.mark.parametrize("limit,complete", [(7, False), (8, True), (9, True)])
def test_raw_map_byte_budget_is_cumulative_and_inclusive(tmp_path, limit, complete):
    root = _directory(tmp_path / "kdb", {"_libFileMap": b"1234", "_stnElabDB": b"5678"})
    anchor = capture_kdb_anchor(str(root), max_bytes=limit)
    assert anchor.identity_complete is complete
    assert anchor.max_bytes == limit
    assert len(anchor.map_records) <= 5
    assert sum(record.size for record in anchor.map_records if record.sha256 is not None) <= limit
    if not complete:
        assert "kdb_map_byte_limit" in anchor.gaps
        assert anchor.receipt()["identity_status"] == "partial"
    assert anchor.receipt()["load_status"] == "not_checked"


def test_oversized_map_is_not_opened_or_hashed(tmp_path, monkeypatch):
    root = _directory(tmp_path / "kdb", {"_libFileMap": b"x" * 17})
    original_open = os.open

    def guarded(path, *args, **kwargs):
        if os.path.abspath(path) == str(root / "_libFileMap"):
            pytest.fail("oversized map bypassed its preflight budget")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(kdb_anchor.os, "open", guarded)
    anchor = capture_kdb_anchor(str(root), max_bytes=16)
    assert not anchor.identity_complete
    assert "kdb_map_byte_limit" in anchor.gaps


def test_unreadable_map_is_partial_and_receipt_drops_exception_text(tmp_path, monkeypatch):
    root = _elaborated(tmp_path / "kdb")
    original_open = os.open

    def denied(path, *args, **kwargs):
        if os.path.abspath(path) == str(root / "_libFileMap"):
            raise PermissionError(errno.EACCES, "PRIVATE_DIAGNOSTIC_LICENSE_TEXT", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(kdb_anchor.os, "open", denied)
    anchor = capture_kdb_anchor(str(root))
    assert not anchor.identity_complete
    assert "kdb_map_unreadable" in anchor.gaps
    assert "PRIVATE_DIAGNOSTIC_LICENSE_TEXT" not in json.dumps(anchor.receipt())
    assert str(tmp_path) not in json.dumps(anchor.receipt())


@pytest.mark.parametrize("failure", ["stat_denied", "not_regular", "read_denied", "byte_limit"])
def test_error_marker_attribution_requires_regular_file_stat_evidence(tmp_path, monkeypatch, failure):
    root = _elaborated(tmp_path / "kdb")
    marker = root / ".hasElabcomError"
    if failure == "not_regular":
        marker.mkdir()
    else:
        marker.write_bytes(b"private compiler diagnostic" * 4)
    original_stat, original_open = os.stat, os.open

    def stat(path, *args, **kwargs):
        if failure == "stat_denied" and os.path.abspath(path) == str(marker):
            raise PermissionError(errno.EACCES, "private marker stat error", str(path))
        return original_stat(path, *args, **kwargs)

    def open_(path, *args, **kwargs):
        if failure == "read_denied" and os.path.abspath(path) == str(marker):
            raise PermissionError(errno.EACCES, "private marker read error", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(kdb_anchor.os, "stat", stat)
    monkeypatch.setattr(kdb_anchor.os, "open", open_)
    anchor = capture_kdb_anchor(str(root), max_bytes=32 if failure == "byte_limit" else 16 * 1024 * 1024)
    assert not anchor.identity_complete
    assert anchor.artifact_status == ("unrecognized" if failure in {"stat_denied", "not_regular"} else "elaboration_error")
    assert anchor.receipt()["load_status"] == "not_checked"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_map_is_partial_without_blocking(tmp_path, monkeypatch, kind):
    root = _directory(tmp_path / "kdb", {"_libFileMap": b"lib"})
    target = root / "_stnElabDB"
    if kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    original_open = os.open

    def bounded_open(path, flags, *args, **kwargs):
        if os.path.abspath(path) == str(target):
            assert flags & os.O_NONBLOCK, "a FIFO open must not defeat the timeout"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(kdb_anchor.os, "open", bounded_open)
    anchor = capture_kdb_anchor(str(root))
    assert not anchor.identity_complete
    assert "kdb_map_not_regular" in anchor.gaps


class _FirstEntryOnly:
    def __init__(self, iterator):
        self.iterator = iterator
        self.reads = 0

    def __enter__(self):
        self.iterator.__enter__()
        return self

    def __exit__(self, *args):
        return self.iterator.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        self.reads += 1
        assert self.reads == 1, "classification enumerated more than the first directory entry"
        return next(self.iterator)


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_unknown_root_checks_only_first_entry_and_never_walks(tmp_path, monkeypatch, empty):
    root = _directory(tmp_path / "kdb")
    if not empty:
        nested = _elaborated(root / "nested_not_this_kdb")
        (root / "unrelated.payload").write_bytes(b"do not read this payload")
        assert nested.is_dir()
    original_scandir, original_open = os.scandir, os.open

    def bounded_scandir(path):
        assert os.path.abspath(path) == str(root), "classification recursed into another directory"
        return _FirstEntryOnly(original_scandir(path))

    def fixed_file_open(path, *args, **kwargs):
        assert Path(path).parent == root and Path(path).name in MAP_NAMES
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(kdb_anchor.os, "scandir", bounded_scandir)
    monkeypatch.setattr(kdb_anchor.os, "open", fixed_file_open)
    monkeypatch.setattr(kdb_anchor.os, "walk", lambda *args, **kwargs: pytest.fail("KDB classification used a full walk"))
    anchor = capture_kdb_anchor(str(root))
    assert anchor.artifact_status == ("empty" if empty else "unrecognized")


def test_growth_during_read_aborts_instead_of_hashing_an_unbounded_file(tmp_path, monkeypatch):
    initial = b"x" * (1024 * 1024 + 3)
    root = _directory(tmp_path / "kdb", {"_libFileMap": initial})
    target = root / "_libFileMap"
    reads = []

    def grow(size, data):
        reads.append((size, len(data)))
        if data:
            with target.open("ab") as stream:
                stream.write(b"x")

    _observe_map_reads(monkeypatch, target, grow)
    with pytest.raises(CaptureAborted) as raised:
        capture_kdb_anchor(str(root))
    assert raised.value.code == "input_changed"
    assert all(0 < size <= 1024 * 1024 for size, _ in reads)
    assert sum(length for _, length in reads) <= len(initial) + 1


def test_change_between_read_and_post_stat_is_not_a_partial_success(tmp_path, monkeypatch):
    root = _elaborated(tmp_path / "kdb")
    target = root / "_libFileMap"
    changed = False

    def mutate(size, data):
        nonlocal changed
        if data and not changed:
            changed = True
            target.write_bytes(b"X" * len(data))

    _observe_map_reads(monkeypatch, target, mutate)
    with pytest.raises(CaptureAborted) as raised:
        capture_kdb_anchor(str(root))
    assert raised.value.code == "input_changed"


def test_second_raw_observation_catches_an_earlier_map_changing_during_later_read(tmp_path, monkeypatch):
    root = _directory(tmp_path / "kdb", {"_libFileMap": b"AAAA", "_stnElabDB": b"BBBB"})
    first, later = root / "_libFileMap", root / "_stnElabDB"
    old = first.stat()
    original_stat, original_fstat = os.stat, os.fstat
    changed = False

    def mutate_earlier_map(size, data):
        nonlocal changed
        if data and not changed:
            changed = True
            first.write_bytes(b"CCCC")

    _observe_map_reads(monkeypatch, later, mutate_earlier_map)

    def coarse(result):
        return old if (result.st_dev, result.st_ino) == (old.st_dev, old.st_ino) else result

    monkeypatch.setattr(kdb_anchor.os, "stat", lambda path, *args, **kwargs: coarse(original_stat(path, *args, **kwargs)))
    monkeypatch.setattr(kdb_anchor.os, "fstat", lambda fd: coarse(original_fstat(fd)))
    with pytest.raises(CaptureAborted) as raised:
        capture_kdb_anchor(str(root))
    assert changed and raised.value.code == "input_changed"


@pytest.mark.parametrize("operation", ["capture", "current"])
def test_precancelled_capture_and_current_preserve_operation_cancelled(tmp_path, operation):
    root = _elaborated(tmp_path / "kdb")
    anchor = capture_kdb_anchor(str(root))
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            capture_kdb_anchor(str(root)) if operation == "capture" else anchor.current()
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
@pytest.mark.parametrize("operation", ["capture", "current"])
def test_mid_read_cancel_or_timeout_is_not_downgraded_to_a_gap(tmp_path, monkeypatch, failure, operation):
    root = _elaborated(tmp_path / "kdb")
    anchor = capture_kdb_anchor(str(root))
    event = threading.Event()
    now = [100.0]
    monkeypatch.setattr(kdb_anchor.time, "monotonic", lambda: now[0])

    def stop(size, data):
        if data:
            if failure == "cancel":
                event.set()
            else:
                now[0] = 102.0

    _observe_map_reads(monkeypatch, root / "_libFileMap", stop)
    token = push_cancel_event(event)
    try:
        expected = OperationCancelled if failure == "cancel" else CaptureAborted
        with pytest.raises(expected) as raised:
            if operation == "capture":
                capture_kdb_anchor(str(root), timeout_sec=1)
            else:
                anchor.current(timeout_sec=1)
        if failure == "timeout":
            assert raised.value.code == "timeout"
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize("timeout", [None, "5", True, 0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_io(tmp_path, monkeypatch, timeout):
    target = tmp_path / "kdb"
    original_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.path.abspath(path) == str(target):
            pytest.fail("invalid timeout performed file I/O")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(kdb_anchor.os, "stat", stat)
    with pytest.raises(ValueError):
        capture_kdb_anchor(str(target), timeout_sec=timeout)


def test_candidate_capture_invokes_no_frontend_or_vendor_process(tmp_path, monkeypatch):
    root = _elaborated(tmp_path / "kdb")
    original_import = builtins.__import__

    def forbidden(*args, **kwargs):
        pytest.fail("a filesystem KDB anchor started EDA/frontend work")

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pyslang", "pynpi"}:
            forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    anchor = capture_kdb_anchor(str(root))
    assert anchor.artifact_status == "elaborated_candidate"
    assert anchor.current()
