"""Bounded discovery control and text identity; no real KDB/NPI acceptance.

Cache-key parity uses actual local sources/filelists and the legacy extractor
as a compatibility oracle. Candidate directories are synthetic artifact facts,
not a claim that a vendor runtime could load them.
"""
from __future__ import annotations

import builtins
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import threading

import pytest

import config
from src import compile_input_capture as capture_module
from src import kdb_builder, structural_scanner, tb_hierarchy_builder, verdi_backend, verdi_npi_backend
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted, CaptureLimits, CompileInputCapture, read_compile_text
from src.compile_log_parser import parse_compile_log
from src.verdi_backend import bounded_kdb_discovery, probe_verdi_backend


@pytest.fixture(autouse=True)
def pure_discovery_only(monkeypatch, tmp_path):
    counts = {"native": 0, "build": 0, "hierarchy": 0, "scan": 0, "process": 0}
    def forbidden(category):
        def reject(*args, **kwargs):
            counts[category] += 1
            pytest.fail(f"discovery attempted {category}")
        return reject
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", forbidden("native"))
    monkeypatch.setattr(kdb_builder, "build_kdb", forbidden("build"))
    monkeypatch.setattr(kdb_builder, "_run_phase", forbidden("build"))
    monkeypatch.setattr(tb_hierarchy_builder, "build_hierarchy", forbidden("hierarchy"))
    monkeypatch.setattr(structural_scanner, "scan_structural_risks", forbidden("scan"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process"))
    monkeypatch.setattr(subprocess, "run", forbidden("process"))
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "private_cache")
    monkeypatch.setattr(config, "NPI_ALLOW_DEGRADED_KDB", True)
    yield counts
    assert not any(counts.values())


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _inputs(root):
    top = _write(root / "top.sv", "module top; leaf u(); endmodule\n")
    leaf = _write(root / "leaf.sv", "module leaf; endmodule\n")
    nested = _write(root / "nested.f", f"{leaf}\n")
    outer = _write(root / "outer.f", f"+define+MODE=1\n{top}\n-f {nested}\n")
    log = _write(root / "comp.log", "Chronologic VCS simulator\n"
                 f"Command: vcs -sverilog -f {outer} -top top\n"
                 f"Parsing design file '{top}'\nParsing design file '{leaf}'\n"
                 "Top Level Modules:\n       top\n")
    metadata = parse_compile_log(str(log), simulator="vcs")
    return metadata, log, (top, leaf, outer, nested)


def _minimal(root, *, simulator="vcs", source=None):
    log = _write(root / "comp.log", "synthetic probe anchor\n")
    return {"simulator": simulator, "top_modules": ["top"], "compile_command": "",
            "compile_cwd": str(root),
            "files": {"user": [] if source is None else [{"path": str(source)}]}}, log


def _extract(metadata, capture=None):
    return kdb_builder._extract_build_inputs(
        metadata, top_hint=None,
        **({"source_reader": capture.read_bytes} if capture is not None else {}),
    )


def _cache(metadata):
    inputs = _extract(metadata)
    assert "error" not in inputs
    path = config.TRACEWEAVE_CACHE_ROOT / config.KDB_CACHE_SUBDIR / inputs["hash"] / "kdb.elab++"
    path.mkdir(parents=True)
    return path, inputs


def test_real_nested_filelists_keep_legacy_cache_key_and_reuse_raw_capture(tmp_path):
    metadata, log, paths = _inputs(tmp_path)
    candidate, legacy = _cache(metadata)
    original = deepcopy(metadata)
    with CompileInputCapture() as capture:
        with bounded_kdb_discovery(capture):
            bounded = _extract(metadata, capture)
            assert bounded == legacy
            before_reads = capture.read_bytes_count
            status = probe_verdi_backend(metadata, str(log))
            assert capture.read_bytes_count == before_reads
            assert status["kdb_path"] == str(candidate)
            assert status["kdb_flow"] == "traceweave_cached"
            records = {r.path: r for r in capture.records}
            assert set(records) == {str(p.resolve()) for p in paths}
            assert sum(r.size for r in records.values()) == sum(p.stat().st_size for p in paths)
            assert capture.validate()
    assert metadata == original
    assert capture.body_count == capture.resident_bytes == 0


@pytest.mark.parametrize("dimension", ["bytes", "files", "references"])
@pytest.mark.parametrize("exact", [True, False])
def test_cache_discovery_honors_cumulative_capture_budget(tmp_path, dimension, exact):
    metadata, log, paths = _inputs(tmp_path)
    candidate, _ = _cache(metadata)
    # Four file identities plus the compile cwd's path alias; directories
    # consume reference capacity without becoming captured file bodies.
    expected = {"bytes": sum(p.stat().st_size for p in paths), "files": 4, "references": 5}[dimension]
    keyword = {"bytes": "max_bytes", "files": "max_files", "references": "max_references"}[dimension]
    code = {"bytes": "byte_limit", "files": "file_limit", "references": "reference_limit"}[dimension]
    capture = CompileInputCapture(CaptureLimits(**{keyword: expected if exact else expected - 1}))
    if exact:
        with capture, bounded_kdb_discovery(capture):
            assert probe_verdi_backend(metadata, str(log))["kdb_path"] == str(candidate)
            assert len(capture.records) == 4
    else:
        with pytest.raises(CaptureAborted) as raised:
            with capture, bounded_kdb_discovery(capture):
                probe_verdi_backend(metadata, str(log))
        assert raised.value.code == code
    assert capture.body_count == capture.resident_bytes == 0


@pytest.mark.parametrize("failure", ["cancel", "timeout", "input_changed"])
def test_cache_extractor_abort_is_never_downgraded_to_cache_miss(tmp_path, monkeypatch, failure):
    # A present cache namespace admits extraction; an absent one is a cheap miss.
    (config.TRACEWEAVE_CACHE_ROOT / config.KDB_CACHE_SUBDIR).mkdir(parents=True)
    metadata, log, _ = _inputs(tmp_path)
    capture = CompileInputCapture()
    error = OperationCancelled() if failure == "cancel" else CaptureAborted(failure)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(capture, "read_bytes", fail)
    with pytest.raises(type(error)) as raised:
        with capture, bounded_kdb_discovery(capture):
            probe_verdi_backend(metadata, str(log))
    assert raised.value is error
    assert capture.body_count == capture.resident_bytes == 0


def test_same_size_source_edit_with_restored_mtime_aborts_cached_body_reuse(tmp_path):
    metadata, log, (source, *_) = _inputs(tmp_path)
    _cache(metadata)
    with CompileInputCapture() as capture:
        with bounded_kdb_discovery(capture):
            assert probe_verdi_backend(metadata, str(log))["kdb_path"]
            before = source.stat()
            source.write_bytes(source.read_bytes().replace(b"leaf u", b"leaf v"))
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            with pytest.raises(CaptureAborted) as raised:
                probe_verdi_backend(metadata, str(log))
            assert raised.value.code == "input_changed"
    assert capture.body_count == capture.resident_bytes == 0


def test_extractor_post_reader_stat_change_is_abort_for_bounded_reader_only(tmp_path):
    source = _write(tmp_path / "top.sv", "module top; endmodule\n")
    metadata, _ = _minimal(tmp_path, source=source)
    with CompileInputCapture() as capture:
        def changing_reader(path):
            raw = capture.read_bytes(path)
            source.write_bytes(raw + b"// changed after capture read\n")
            return raw
        with pytest.raises(CaptureAborted) as raised:
            kdb_builder._extract_build_inputs(metadata, top_hint=None, source_reader=changing_reader)
        assert raised.value.code == "input_changed"


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
def test_direct_clean_candidate_checks_cancel_and_timeout_before_return(tmp_path, monkeypatch, failure):
    metadata, log = _minimal(tmp_path)
    candidate = tmp_path / "simv.daidir" / "kdb.elab++"
    candidate.mkdir(parents=True)
    now = [100.0]
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: now[0])
    event = threading.Event()
    token = push_cancel_event(event)
    try:
        error_type = OperationCancelled if failure == "cancel" else CaptureAborted
        with pytest.raises(error_type) as raised:
            with CompileInputCapture(CaptureLimits(timeout_sec=1)) as capture, bounded_kdb_discovery(capture):
                if failure == "cancel":
                    event.set()
                else:
                    now[0] = 102.0
                probe_verdi_backend(metadata, str(log))
                pytest.fail("direct candidate returned after cancellation/deadline")
        if failure == "timeout":
            assert raised.value.code == "timeout"
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize("size", [64 * 1024, 64 * 1024 + 1])
def test_setup_has_independent_64k_precheck(tmp_path, size):
    metadata, log = _minimal(tmp_path)
    setup = _write(tmp_path / "synopsys_sim.setup", "#" * size)
    with CompileInputCapture() as capture:
        if size > 64 * 1024:
            with pytest.raises(CaptureAborted) as raised:
                with bounded_kdb_discovery(capture):
                    probe_verdi_backend(metadata, str(log))
            assert raised.value.code == "kdb_setup_byte_limit"
            assert not capture.records
        else:
            with bounded_kdb_discovery(capture):
                assert probe_verdi_backend(metadata, str(log))["kdb_path"] is None
                assert any(r.path == str(setup) and r.size == size for r in capture.records)


@pytest.mark.parametrize("role", ["setup", "source", "filelist"])
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_special_inputs_abort_without_a_blocking_open(tmp_path, monkeypatch, role, kind):
    (config.TRACEWEAVE_CACHE_ROOT / config.KDB_CACHE_SUBDIR).mkdir(parents=True)
    name = {"setup": "synopsys_sim.setup", "source": "special.sv", "filelist": "special.f"}[role]
    path = tmp_path / name
    path.mkdir() if kind == "directory" else os.mkfifo(path)
    metadata, log = _minimal(tmp_path, source=path if role == "source" else None)
    if role == "filelist":
        metadata["compile_command"] = f"vcs -f {path}"
        source = _write(tmp_path / "top.sv", "module top; endmodule\n")
        metadata["files"]["user"] = [{"path": str(source)}]
    original_open = builtins.open
    def no_blocking_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.path.abspath(file) == str(path):
            pytest.fail("special input reached legacy blocking open")
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", no_blocking_open)
    with pytest.raises(CaptureAborted) as raised:
        with CompileInputCapture() as capture, bounded_kdb_discovery(capture):
            probe_verdi_backend(metadata, str(log))
    assert raised.value.code == "input_not_regular"
    assert capture.body_count == capture.resident_bytes == 0


def test_directory_entry_budget_is_cumulative_and_stops_at_first_excess(tmp_path, monkeypatch):
    metadata, log = _minimal(tmp_path, simulator="xcelium")
    for index in range(10):
        (tmp_path / f"entry_{index}").mkdir()
    original = os.scandir
    visited = []
    class ObservedScan:
        def __init__(self, path):
            self.iterator = original(path)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.iterator.close()
        def __iter__(self):
            return self
        def __next__(self):
            entry = next(self.iterator)
            visited.append(entry.name)
            return entry
    def no_listdir(*args, **kwargs):
        pytest.fail("bounded discovery attempted unbounded listdir")
    with monkeypatch.context() as patch:
        patch.setattr(verdi_backend.os, "scandir", ObservedScan)
        patch.setattr(verdi_backend.os, "listdir", no_listdir)
        with pytest.raises(CaptureAborted) as raised:
            with CompileInputCapture() as capture, bounded_kdb_discovery(capture, max_entries=3):
                probe_verdi_backend(metadata, str(log))
        assert raised.value.code == "kdb_discovery_entry_limit"
        assert len(visited) == 4


def test_directory_cap_covers_multiple_directories_and_exact_boundary(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    _write(first / "a", "a")
    _write(first / "b", "b")
    _write(second / "c", "c")
    with CompileInputCapture() as capture, bounded_kdb_discovery(capture, max_entries=3):
        assert set(verdi_backend._directory_entries(str(first))) == {"a", "b"}
        assert verdi_backend._directory_entries(str(second)) == ["c"]
        with pytest.raises(CaptureAborted) as raised:
            verdi_backend._directory_entries(str(second))
        assert raised.value.code == "kdb_discovery_entry_limit"


@pytest.mark.parametrize("layout", ["source_only", "clean", "degraded", "prefer_clean", "vcs_three_step"])
def test_bounded_candidates_preserve_legacy_selection_without_vendor_log_reads(tmp_path, monkeypatch, layout):
    metadata, log = _minimal(tmp_path, simulator="vcs" if layout == "vcs_three_step" else "xcelium")
    source_only = tmp_path / "rtl.lib++"
    source_only.mkdir()
    selected = source_only
    if layout in {"clean", "degraded", "prefer_clean"}:
        selected = tmp_path / "kdb.elab++"
        selected.mkdir()
    if layout in {"degraded", "prefer_clean"}:
        _write(selected / ".hasElabcomError", "vendor.log\n")
        _write(selected / "vendor.log", "Total 7 error(s)\n")
    if layout == "prefer_clean":
        selected = tmp_path / "clean" / "kdb.elab++"
        selected.mkdir(parents=True)
    if layout == "vcs_three_step":
        _write(tmp_path / "synopsys_sim.setup", "WORK : .\n")
    legacy = probe_verdi_backend(metadata, str(log))
    assert legacy["kdb_path"] == str(selected)
    def no_diagnostic_read(*args, **kwargs):
        pytest.fail("bounded discovery read optional vendor marker/log text")
    monkeypatch.setattr(verdi_backend, "_read_bounded_text", no_diagnostic_read)
    with CompileInputCapture() as capture, bounded_kdb_discovery(capture):
        bounded = probe_verdi_backend(metadata, str(log))
        for field in ("kdb_path", "kdb_flow", "kdb_validation_status", "simulator", "backend"):
            assert bounded[field] == legacy[field]
        assert bounded["kdb_error_count"] is bounded["kdb_error_log"] is None
    # A directory selection remains a candidate observation, never an NPI load.


def test_nested_exception_scope_restores_reader_and_outer_directory_budget(tmp_path):
    a, b, c = (_write(tmp_path / name, name) for name in ("a.f", "b.f", "c.f"))
    directory = tmp_path / "single"
    _write(directory / "entry", "body")
    class StopInner(Exception):
        pass
    with CompileInputCapture() as outer, CompileInputCapture() as inner:
        with bounded_kdb_discovery(outer, max_entries=1):
            assert read_compile_text(a) == "a.f"
            assert verdi_backend._directory_entries(str(directory)) == ["entry"]
            with pytest.raises(StopInner):
                with bounded_kdb_discovery(inner, max_entries=1):
                    assert read_compile_text(b) == "b.f"
                    assert verdi_backend._directory_entries(str(directory)) == ["entry"]
                    raise StopInner()
            assert read_compile_text(a) == "a.f"
            with pytest.raises(CaptureAborted) as raised:
                verdi_backend._directory_entries(str(directory))
            assert raised.value.code == "kdb_discovery_entry_limit"
        assert read_compile_text(c) == "c.f"
        assert [r.path for r in outer.records] == [str(a)]
        assert [r.path for r in inner.records] == [str(b)]
    assert outer.body_count == inner.body_count == 0
    # Legacy remains usable after a bounded exception and does not read a closed capture.
    assert verdi_backend._directory_entries(str(directory)) == ["entry"]


def test_fresh_worker_threads_have_independent_contextvars_and_budgets(tmp_path):
    barrier = threading.Barrier(2)
    paths = [_write(tmp_path / f"input_{i}.f", str(i)) for i in range(2)]
    directory = tmp_path / "single"
    _write(directory / "entry", "body")
    def worker(index):
        with CompileInputCapture() as capture, bounded_kdb_discovery(capture, max_entries=1):
            barrier.wait(timeout=5)
            text = read_compile_text(paths[index])
            entries = verdi_backend._directory_entries(str(directory))
            records = tuple(r.path for r in capture.records)
        return text, entries, records, capture.body_count
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, i) for i in range(2)]
        results = [future.result(timeout=10) for future in futures]
    for index, result in enumerate(results):
        assert result == (str(index), ["entry"], (str(paths[index]),), 0)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "4", None])
def test_invalid_directory_limit_rejected_without_rebinding_capture(tmp_path, limit):
    path = _write(tmp_path / "keep.f", "body")
    with CompileInputCapture() as outer, CompileInputCapture() as rejected:
        with bounded_kdb_discovery(outer):
            with pytest.raises(ValueError):
                with bounded_kdb_discovery(rejected, max_entries=limit):
                    pytest.fail("invalid limit admitted")
            assert read_compile_text(path) == "body"
            assert len(outer.records) == 1 and not rejected.records
