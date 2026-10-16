"""Historical source records are a bounded observation set, not HDL closure.

H is specified by each fixture independently of dependency discovery. C is a
real prepared context and can contain logs/support files or omit source bodies
after a source budget. No hierarchy, semantic frontend or vendor API is run.
"""
from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
import subprocess
import threading

import pytest

from src import compile_input_capture as capture_module, sv_preprocessor, tb_hierarchy_builder
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted, CaptureLimits, CompileInputCapture
from src.design_context_runtime import prepare_design_context
from src.hierarchy_recovery import RecoveryBlocked
from src.legacy_context_observations import verify_legacy_source_observations


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _log(path, source, *, options="", recorded_include=None):
    return _write(path, "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {options} {source} -top top\n"
        f"Parsing design file '{source}'\n" +
        (f"Parsing included file '{recorded_include}'\nBack to file '{source}'\n" if recorded_include else "") +
        "Top Level Modules:\n       top\n")


def _historical_sha(paths, *, markers=None):
    """Handwritten legacy record protocol; paths are the explicit fixture H.

    Deliberately includes old stat fields. Does not call a source collector,
    snapshot builder, context fingerprint or the verifier under test.
    """
    markers = markers or {}
    records = []
    for path in sorted({str(path.resolve()) for path in paths}):
        with open(path, "rb") as stream:
            raw = stream.read()
        stat = os.stat(path)
        records.append(dict(path=path, device=stat.st_dev, inode=stat.st_ino,
            bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns,
            sha256=hashlib.sha256(raw).hexdigest(), objective_exclusions=list(markers.get(path, ()))))
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _records(context):
    return {record.path: record for record in context.snapshot.records}


def _base(root, *, partial=False, symlink=False):
    source = _write(root / "rtl/top.sv", '`include "defs.svh"\nmodule top; endmodule\n')
    header = _write(root / "include/defs.svh", "wire selected_header;\n")
    reference = root / "rtl/defs.svh"
    if symlink:
        reference.symlink_to(header)
    log = _log(root / "compile.log", source, options=f"+incdir+{header.parent}")
    context = prepare_design_context(str(log), limits=CaptureLimits(max_files=1) if partial else None)
    return context, source, header, reference, log


@pytest.fixture(autouse=True)
def forbid_unrelated_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("historical observations entered hierarchy/frontend/native work")
    for name in ("build_hierarchy", "build_component_tree", "_scan_user_files", "scan_sv_file", "scan_preprocessed_sv"):
        monkeypatch.setattr(tb_hierarchy_builder, name, forbidden)
    monkeypatch.setattr(sv_preprocessor.SystemVerilogPreprocessor, "preprocess", forbidden)
    monkeypatch.setattr(sv_preprocessor, "_expand_hierarchy_macro_line", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pyslang", "pynpi"}:
            forbidden()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)


@pytest.fixture
def captures(monkeypatch):
    observed = []
    original = CompileInputCapture.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        observed.append(self)
    monkeypatch.setattr(CompileInputCapture, "__init__", init)
    yield observed
    assert observed
    assert all(capture.body_count == 0 and capture.resident_bytes == 0 for capture in observed)


def test_handwritten_h_excludes_log_and_differs_from_full_capture_c(tmp_path, captures):
    context, source, header, _, log = _base(tmp_path)
    expected = _historical_sha([source, header])
    assert set(_records(context)) == {str(log), str(source), str(header)}
    assert expected != context.snapshot.content_fingerprint_sha256
    before = (context.identity_sha256, context.snapshot, context.aliases, context.gaps, context.metrics)
    verified = verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    repeated = verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert verified.current()
    assert verified.identity_sha256 == repeated.identity_sha256 != context.identity_sha256
    assert set(_records(verified)) >= {str(log), str(source), str(header)}
    assert verified.compile_result() == context.compile_result()
    assert (context.identity_sha256, context.snapshot, context.aliases, context.gaps, context.metrics) == before
    assert context.path_observations == ()
    with pytest.raises(FrozenInstanceError):
        verified.identity_sha256 = "0" * 64


def test_partial_c_gains_extra_h_records_and_retains_capture_gaps(tmp_path, captures):
    context, source, header, _, log = _base(tmp_path, partial=True)
    assert set(_records(context)) == {str(log)}
    expected = _historical_sha([source, header])
    verified = verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert set(_records(verified)) >= {str(log), str(source), str(header)}
    assert set(context.gaps) <= set(verified.gaps)
    assert not verified.include_resolution_complete
    assert verified.identity_sha256 != context.identity_sha256
    assert set(_records(context)) == {str(log)}
    metrics = dict(verified.metrics)
    assert metrics["legacy_source_files_read"] == 2
    assert metrics["legacy_source_bytes_read"] == 2 * (source.stat().st_size + header.stat().st_size)
    assert metrics["legacy_dependency_file_visits"] == 2
    assert metrics["legacy_dependency_work_chars"] == len(source.read_text()) + len(header.read_text())
    assert metrics["legacy_verification_ms"] >= 0
    assert verified.current()
    header.write_text("wire changed_header;\n")
    assert context.current() and not verified.current()


def test_extra_h_alias_retarget_is_checked_after_verifier_returns(tmp_path, captures):
    context, source, header, reference, _ = _base(tmp_path, partial=True, symlink=True)
    verified = verify_legacy_source_observations(context, _historical_sha([source, header]), limits=CaptureLimits())
    assert verified.current()
    assert (str(reference), str(header)) in verified.aliases
    replacement = _write(tmp_path / "other/defs.svh", header.read_text())
    reference.unlink()
    reference.symlink_to(replacement)
    assert context.current() and not verified.current()


def test_new_previously_absent_include_shadow_invalidates_merged_observation(tmp_path, captures):
    context, source, header, missing, _ = _base(tmp_path, partial=True)
    verified = verify_legacy_source_observations(context, _historical_sha([source, header]), limits=CaptureLimits())
    assert verified.current()
    assert str(missing) in dict(verified.path_observations)
    assert str(header) in dict(verified.path_observations)
    missing.write_text(header.read_text())
    assert context.current() and not verified.current()


def test_old_exact_include_evidence_wins_over_search_shadow_for_h(tmp_path, captures):
    source = _write(tmp_path / "rtl/top.sv", '`include "defs.svh"\nmodule top; endmodule\n')
    shadow = _write(source.parent / "defs.svh", "wire current_shadow;\n")
    historical = _write(tmp_path / "recorded/defs.svh", "wire logged_header;\n")
    log = _log(tmp_path / "compile.log", source, recorded_include=historical)
    context = prepare_design_context(str(log))
    expected = _historical_sha([source, historical])
    assert {str(source), str(shadow), str(historical), str(log)} <= set(_records(context))
    verified = verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert verified.current()
    assert "include_evidence_mismatch" in verified.gaps
    assert not verified.include_resolution_complete
    with pytest.raises(RecoveryBlocked) as raised:
        verify_legacy_source_observations(context, _historical_sha([source, shadow]), limits=CaptureLimits())
    assert raised.value.code == "compile_context_changed"


@pytest.mark.parametrize("change", ["raw", "same_size_restored_mtime", "touch", "replace_inode", "missing"])
def test_old_h_content_and_stat_identity_are_both_required(tmp_path, captures, change):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    previous = header.stat()
    if change == "raw":
        header.write_text("wire different_and_larger_header;\n")
    elif change == "same_size_restored_mtime":
        header.write_bytes(header.read_bytes().replace(b"selected", b"modified"))
        os.utime(header, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "touch":
        os.utime(header, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    elif change == "replace_inode":
        temporary = _write(header.parent / "replacement.svh", header.read_text())
        os.replace(temporary, header)
        os.utime(header, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    else:
        header.unlink()
    with pytest.raises(RecoveryBlocked) as raised:
        verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert raised.value.code == "compile_context_changed"


@pytest.mark.parametrize("omitted", ["root", "include", "none_but_extra_log"])
def test_hash_of_wrong_record_subset_cannot_validate_history(tmp_path, captures, omitted):
    context, source, header, _, log = _base(tmp_path)
    paths = {"root": [header], "include": [source], "none_but_extra_log": [source, header, log]}[omitted]
    with pytest.raises(RecoveryBlocked) as raised:
        verify_legacy_source_observations(context, _historical_sha(paths), limits=CaptureLimits())
    assert raised.value.code == "compile_context_changed"


@pytest.mark.parametrize("partial", ["macro", "conditional"])
def test_matching_h_with_partial_preprocessing_retains_gaps(tmp_path, captures, partial):
    source = _write(tmp_path / "top.sv", (
        "`UNMODELED_HIERARCHY_MACRO()\nmodule top; endmodule\n" if partial == "macro" else
        "`ifdef ENABLE\nmodule top; endmodule\n"))
    log = _log(tmp_path / "compile.log", source, options="+define+ENABLE")
    context = prepare_design_context(str(log))
    assert not context.include_resolution_complete and context.gaps
    verified = verify_legacy_source_observations(context, _historical_sha([source]), limits=CaptureLimits())
    assert verified.current()
    assert set(context.gaps) <= set(verified.gaps)
    assert not verified.include_resolution_complete
    assert ("dependency_macro_unexpanded" if partial == "macro" else "conditional_unbalanced") in verified.gaps


def test_protected_user_source_is_raw_observed_without_plaintext_expansion(tmp_path, captures):
    source = tmp_path / "encrypted.svp"
    source.write_bytes(b"\xff\xfeopaque encrypted source\n")
    log = _log(tmp_path / "compile.log", source)
    context = prepare_design_context(str(log))
    expected = _historical_sha([source], markers={str(source): ("protected_region",)})
    verified = verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert verified.current()
    assert "protected_region" in verified.gaps
    assert not verified.include_resolution_complete
    assert _records(verified)[str(source)].sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


def test_inactive_include_is_not_part_of_h_and_stays_outside_observation(tmp_path, captures):
    source = _write(tmp_path / "top.sv", '`ifdef NEVER_ENABLED\n`include "inactive.svh"\n`endif\nmodule top; endmodule\n')
    inactive = _write(tmp_path / "inactive.svh", "wire not_read;\n")
    log = _log(tmp_path / "compile.log", source)
    context = prepare_design_context(str(log))
    verified = verify_legacy_source_observations(context, _historical_sha([source]), limits=CaptureLimits())
    assert str(inactive) not in _records(verified)
    inactive.write_text("wire still_not_read;\n")
    assert verified.current()


@pytest.mark.parametrize("limit,code", [("files", "file_limit"), ("bytes", "byte_limit"), ("references", "reference_limit")])
def test_h_capture_limits_propagate_without_returning_partial_verified_context(tmp_path, captures, limit, code):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    limits = {"files": CaptureLimits(max_files=1), "bytes": CaptureLimits(max_bytes=source.stat().st_size + header.stat().st_size - 1),
              "references": CaptureLimits(max_references=1)}[limit]
    with pytest.raises(CaptureAborted) as raised:
        verify_legacy_source_observations(context, expected, limits=limits)
    assert raised.value.code == code


@pytest.mark.parametrize("limit,code", [("files", "file_limit"), ("bytes", "byte_limit")])
def test_limits_apply_to_merged_c_union_h_not_only_the_new_h_reads(tmp_path, captures, limit, code):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    limits = (CaptureLimits(max_files=2) if limit == "files" else
              CaptureLimits(max_bytes=source.stat().st_size + header.stat().st_size))
    # H itself fits exactly, but C adds the compile log to the retained union.
    with pytest.raises(CaptureAborted) as raised:
        verify_legacy_source_observations(context, expected, limits=limits)
    assert raised.value.code == code


def test_timeout_during_h_read_propagates_and_releases_bodies(tmp_path, monkeypatch, captures):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    now = [100.0]
    original = CompileInputCapture.read_source_content
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: now[0])
    def expired(self, path):
        result = original(self, path)
        now[0] += 2
        return result
    monkeypatch.setattr(CompileInputCapture, "read_source_content", expired)
    with pytest.raises(CaptureAborted) as raised:
        verify_legacy_source_observations(context, expected, limits=CaptureLimits(timeout_sec=1))
    assert raised.value.code == "timeout"


def test_typed_input_change_from_reader_is_not_reclassified(tmp_path, monkeypatch, captures):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    def changed(self, path):
        raise CaptureAborted("input_changed")
    monkeypatch.setattr(CompileInputCapture, "read_source_content", changed)
    with pytest.raises(CaptureAborted) as raised:
        verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert raised.value.code == "input_changed"


def test_cancellation_is_not_demoted_to_history_mismatch(tmp_path, captures):
    context, source, header, _, _ = _base(tmp_path)
    expected = _historical_sha([source, header])
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    finally:
        pop_cancel_event(token)


def test_missing_h_root_is_a_typed_mismatch_not_an_empty_success(tmp_path, captures):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    source.unlink()
    with pytest.raises(RecoveryBlocked) as raised:
        verify_legacy_source_observations(context, expected, limits=CaptureLimits())
    assert raised.value.code == "compile_context_changed"


def test_fifo_h_input_is_rejected_without_blocking_reader(tmp_path, monkeypatch, captures):
    context, source, header, _, _ = _base(tmp_path, partial=True)
    expected = _historical_sha([source, header])
    header.unlink()
    os.mkfifo(header)
    original_open = capture_module.os.open
    def nonblocking_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(header):
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(capture_module.os, "open", nonblocking_open)
    with pytest.raises((CaptureAborted, RecoveryBlocked)) as raised:
        verify_legacy_source_observations(context, expected, limits=CaptureLimits(timeout_sec=0.2))
    assert raised.value.code in {"input_not_regular", "compile_context_changed"}
