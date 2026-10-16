"""Independent tests for bounded compile-input capture and parser I/O hooks.

These tests use only local text fixtures.  Expected digests come from fixture
bytes, and filelist order comes from their written contents; no HDL frontend or
licensed executable is an oracle for this capture-only layer.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import locale
import os
import threading

import pytest

from src import compile_input_capture as capture_module
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_environment import resolve_compile_environment
from src.compile_input_capture import (
    CaptureAborted,
    CaptureLimits,
    CompileInputCapture,
    bind_compile_input_capture,
    open_compile_text,
    read_compile_text,
    sample_compile_bytes,
)
from src.compile_log_parser import detect_simulator, parse_compile_log
from src.compile_session_snapshot import FileContentSnapshot
from src.sv_preprocessor import extract_preprocessor_options


def _records(capture):
    return {record.path: record for record in capture.snapshot().records}


def _decoded(raw):
    return raw.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace").replace(
        "\r\n", "\n"
    ).replace("\r", "\n")


class _ReadObserver:
    """Transparent fd stream wrapper with a deterministic post-read barrier."""

    def __init__(self, stream, after_read):
        self._stream = stream
        self._after_read = after_read

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        return self._stream.__exit__(*args)

    def read(self, size=-1):
        result = self._stream.read(size)
        self._after_read(size, result)
        return result

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _observe_reads(monkeypatch, path, callback):
    original = capture_module.os.fdopen
    inode = path.stat().st_ino
    device = path.stat().st_dev

    def fdopen(fd, *args, **kwargs):
        stat = os.fstat(fd)
        stream = original(fd, *args, **kwargs)
        if (stat.st_dev, stat.st_ino) == (device, inode):
            return _ReadObserver(stream, callback)
        return stream

    monkeypatch.setattr(capture_module.os, "fdopen", fdopen)


def test_raw_bytes_text_and_snapshot_are_one_immutable_observation(tmp_path):
    path = tmp_path / "input.f"
    raw = b"first\r\nsecond\rthird\xff\n"
    path.write_bytes(raw)
    with CompileInputCapture() as capture:
        assert capture.read_bytes(str(path)) == raw
        assert capture.read_text(str(path)) == _decoded(raw)
        snapshot = capture.snapshot()
        assert snapshot.complete
        assert snapshot.file_count == 1
        assert snapshot.total_bytes == len(raw)
        record = snapshot.records[0]
        assert record.path == str(path.resolve())
        assert record.sha256 == hashlib.sha256(raw).hexdigest()
        assert record.size == len(raw)
        with pytest.raises(FrozenInstanceError):
            record.sha256 = "0" * 64
        assert not hasattr(record, "text") and not hasattr(record, "source_text")
        assert capture.validate()


def test_repeat_reads_and_symlink_alias_consume_one_file_budget(tmp_path, monkeypatch):
    path = tmp_path / "input.f"
    path.write_bytes(b"four")
    alias = tmp_path / "alias.f"
    alias.symlink_to(path)
    reads = []
    _observe_reads(monkeypatch, path, lambda size, data: reads.append((size, data)))
    with CompileInputCapture(limits=CaptureLimits(max_files=1, max_bytes=4)) as capture:
        assert capture.read_bytes(str(path)) == b"four"
        physical_reads = len(reads)
        assert capture.read_text(str(path)) == "four"
        assert capture.read_bytes(str(alias)) == b"four"
        assert len(reads) == physical_reads
        assert capture.snapshot().file_count == 1
        assert capture.body_count == 1


def test_reference_budget_counts_distinct_spellings_without_counting_repeats(tmp_path):
    path = tmp_path / "input.f"
    path.write_bytes(b"body")
    first_alias, second_alias = tmp_path / "first_alias.f", tmp_path / "second_alias.f"
    first_alias.symlink_to(path)
    second_alias.symlink_to(path)
    with CompileInputCapture(limits=CaptureLimits(max_files=1, max_references=2)) as capture:
        assert capture.read_bytes(str(path)) == b"body"
        assert capture.read_bytes(str(first_alias)) == b"body"
        assert capture.read_bytes(str(first_alias)) == b"body"
        assert capture.body_count == 1
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(second_alias))
        assert raised.value.code == "reference_limit"


@pytest.mark.parametrize("size", [3, 4, 5])
def test_exact_byte_budget_boundaries(tmp_path, size):
    path = tmp_path / "bytes.f"
    path.write_bytes(b"x" * size)
    with CompileInputCapture(limits=CaptureLimits(max_bytes=4)) as capture:
        if size > 4:
            with pytest.raises(CaptureAborted) as raised:
                capture.read_bytes(str(path))
            assert raised.value.code == "byte_limit"
        else:
            assert capture.read_bytes(str(path)) == b"x" * size
            assert capture.snapshot().total_bytes == size


def test_byte_budget_is_cumulative_across_files(tmp_path):
    first, second = tmp_path / "first.f", tmp_path / "second.f"
    first.write_bytes(b"123")
    second.write_bytes(b"45")
    with CompileInputCapture(limits=CaptureLimits(max_bytes=4)) as capture:
        assert capture.read_bytes(str(first)) == b"123"
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(second))
        assert raised.value.code == "byte_limit"


def test_file_budget_rejects_first_file_beyond_limit(tmp_path):
    paths = [tmp_path / f"file_{index}.f" for index in range(3)]
    for path in paths:
        path.write_bytes(b"")
    with CompileInputCapture(limits=CaptureLimits(max_files=2)) as capture:
        for path in paths[:2]:
            assert capture.read_bytes(str(path)) == b""
        assert capture.snapshot().file_count == 2
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(paths[2]))
        assert raised.value.code == "file_limit"


def test_reads_are_chunked_and_growth_during_read_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "growing.f"
    path.write_bytes(b"a" * (1024 * 1024 + 7))
    changed = False
    requests = []

    def grow_after_read(size, data):
        nonlocal changed
        requests.append(size)
        if data and not changed:
            changed = True
            with path.open("ab") as stream:
                stream.write(b"grew")

    _observe_reads(monkeypatch, path, grow_after_read)
    with CompileInputCapture() as capture:
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(path))
        assert raised.value.code == "input_changed"
        assert changed
        assert requests and all(0 < size <= 1024 * 1024 for size in requests)


def test_same_length_mutation_during_read_cannot_publish_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "changed.f"
    path.write_bytes(b"before")
    changed = False

    def mutate(size, data):
        nonlocal changed
        if data and not changed:
            changed = True
            path.write_bytes(b"after!")

    _observe_reads(monkeypatch, path, mutate)
    with CompileInputCapture() as capture:
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(path))
            # Some local filesystems coarsen both mtime and ctime to a tick.
            # Stat can reject immediately; raw-byte validation must reject
            # before publication even when that first check cannot see it.
            capture.snapshot()
        assert raised.value.code == "input_changed"


def test_validate_rehashes_same_size_content_with_restored_mtime(tmp_path, monkeypatch):
    path = tmp_path / "stable_metadata.f"
    path.write_bytes(b"before")
    with CompileInputCapture() as capture:
        capture.read_bytes(str(path))
        old = path.stat()
        path.write_bytes(b"after!")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        # Simulate a filesystem whose complete stat evidence is coarsened,
        # including checks made directly on the opened descriptor.
        original_stat, original_fstat = os.stat, os.fstat

        def coarsened_stat(name, *args, **kwargs):
            result = original_stat(name, *args, **kwargs)
            return old if (result.st_dev, result.st_ino) == (old.st_dev, old.st_ino) else result

        def coarsened_fstat(fd):
            result = original_fstat(fd)
            return old if (result.st_dev, result.st_ino) == (old.st_dev, old.st_ino) else result

        monkeypatch.setattr(capture_module.os, "stat", coarsened_stat)
        monkeypatch.setattr(capture_module.os, "fstat", coarsened_fstat)
        assert capture.records[0].current()
        assert capture.validate() is False


def test_validate_rejects_symlink_retarget_when_original_target_is_unchanged(tmp_path):
    first, second, alias = tmp_path / "first.f", tmp_path / "second.f", tmp_path / "link.f"
    first.write_bytes(b"equal")
    second.write_bytes(b"equal")
    alias.symlink_to(first)
    with CompileInputCapture() as capture:
        capture.read_bytes(str(alias))
        record = capture.snapshot().records[0]
        alias.unlink()
        alias.symlink_to(second)
        assert record.current(), "the original target itself did not change"
        assert capture.validate() is False


def test_validate_is_bounded_if_file_grows_after_capture(tmp_path, monkeypatch):
    path = tmp_path / "growth_after_capture.f"
    path.write_bytes(b"four")
    with CompileInputCapture() as capture:
        capture.read_bytes(str(path))
        path.write_bytes(b"x" * (2 * 1024 * 1024))
        monkeypatch.setattr(FileContentSnapshot, "current", lambda self: True)
        requests = []
        _observe_reads(monkeypatch, path, lambda size, data: requests.append(size))
        assert capture.validate() is False
        # A size/stat precheck may reject without reading. If rehashing occurs,
        # at most the old four bytes plus one lookahead byte may be requested.
        assert all(0 < size <= 5 for size in requests)
        assert sum(requests) <= 5


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_inputs_abort_without_reading_or_blocking(tmp_path, kind):
    path = tmp_path / kind
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with CompileInputCapture() as capture:
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(path))
        assert raised.value.code == "input_not_regular"


def test_timeout_also_applies_to_reused_body(tmp_path, monkeypatch):
    path = tmp_path / "time.f"
    path.write_bytes(b"captured")
    now = [100.0]
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: now[0])
    with CompileInputCapture(limits=CaptureLimits(timeout_sec=1)) as capture:
        capture.read_bytes(str(path))
        now[0] = 102.0
        with pytest.raises(CaptureAborted) as raised:
            capture.read_bytes(str(path))
        assert raised.value.code == "timeout"


@pytest.mark.parametrize("operation", ["read", "validate"])
def test_precancelled_operation_propagates_original_cancel(tmp_path, operation):
    path = tmp_path / "cancel.f"
    path.write_bytes(b"body")
    with CompileInputCapture() as capture:
        capture.read_bytes(str(path))
        event = threading.Event()
        event.set()
        token = push_cancel_event(event)
        try:
            with pytest.raises(OperationCancelled):
                if operation == "read":
                    capture.read_bytes(str(path))
                else:
                    capture.validate()
        finally:
            pop_cancel_event(token)


def test_cancellation_during_chunk_read_is_not_replaced_by_capture_error(tmp_path, monkeypatch):
    path = tmp_path / "cancel_during_read.f"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    event = threading.Event()
    _observe_reads(monkeypatch, path, lambda size, data: event.set())
    token = push_cancel_event(event)
    try:
        with CompileInputCapture() as capture:
            with pytest.raises(OperationCancelled):
                capture.read_bytes(str(path))
    finally:
        pop_cancel_event(token)


@pytest.mark.parametrize("exceptional", [False, True])
def test_exit_releases_bodies_and_closed_reads_fail(tmp_path, exceptional):
    path = tmp_path / "retained.f"
    path.write_bytes(b"capture-private-body" * 4096)
    capture = CompileInputCapture()
    snapshot = None
    try:
        with capture:
            capture.read_bytes(str(path))
            snapshot = capture.snapshot()
            assert capture.body_count == 1 and capture.resident_bytes > 0
            if exceptional:
                raise LookupError("test body failed")
    except LookupError:
        assert exceptional
    assert snapshot is not None and snapshot.records[0].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert capture.body_count == 0 and capture.resident_bytes == 0
    capture.close()  # idempotent cleanup
    with pytest.raises(RuntimeError):
        capture.read_bytes(str(path))
    with pytest.raises(RuntimeError):
        capture.read_text(str(path))


def test_nested_binding_restores_outer_capture_after_exception(tmp_path):
    paths = [tmp_path / name for name in ("outer.f", "inner.f", "restored.f", "unbound.f")]
    for path in paths:
        path.write_text(path.name)
    with CompileInputCapture() as outer, CompileInputCapture() as inner:
        with bind_compile_input_capture(outer):
            assert read_compile_text(str(paths[0])) == "outer.f"
            with pytest.raises(LookupError):
                with bind_compile_input_capture(inner):
                    with open_compile_text(str(paths[1])) as stream:
                        assert list(stream) == ["inner.f"]
                    raise LookupError("leave inner binding")
            assert read_compile_text(str(paths[2])) == "restored.f"
        assert read_compile_text(str(paths[3])) == "unbound.f"
        assert set(_records(outer)) == {str(p.resolve()) for p in (paths[0], paths[2])}
        assert set(_records(inner)) == {str(paths[1].resolve())}


def test_capture_binding_is_isolated_between_threads(tmp_path):
    paths = [tmp_path / f"thread_{index}.f" for index in range(3)]
    for path in paths:
        path.write_text(path.name)
    with CompileInputCapture() as parent:
        with bind_compile_input_capture(parent), ThreadPoolExecutor(max_workers=2) as pool:
            read_compile_text(str(paths[0]))

            def own_capture():
                with CompileInputCapture() as child:
                    with bind_compile_input_capture(child):
                        read_compile_text(str(paths[1]))
                    return set(_records(child))

            own = pool.submit(own_capture)
            unbound = pool.submit(read_compile_text, str(paths[2]))
            assert own.result(timeout=5) == {str(paths[1].resolve())}
            assert unbound.result(timeout=5) == paths[2].name
        assert set(_records(parent)) == {str(paths[0].resolve())}


@pytest.mark.parametrize("raw,max_bytes", [(b"abc", 4), (b"abcdefgh", 4), (b"abcdefgh", 5)])
def test_bound_sampling_preserves_sample_shape_but_captures_full_identity(tmp_path, raw, max_bytes):
    path = tmp_path / "sample.log"
    path.write_bytes(raw)
    expected = raw if len(raw) <= max_bytes else raw[: max_bytes // 2] + b"\n" + raw[-(max_bytes // 2) :]
    assert sample_compile_bytes(str(path), max_bytes) == expected
    with CompileInputCapture() as capture, bind_compile_input_capture(capture):
        assert sample_compile_bytes(str(path), max_bytes) == expected
        record = capture.snapshot().records[0]
        assert record.size == len(raw)
        assert record.sha256 == hashlib.sha256(raw).hexdigest()


def test_small_sample_cannot_bypass_full_capture_byte_limit(tmp_path):
    path = tmp_path / "larger_than_capture.log"
    path.write_bytes(b"x" * 32)
    with CompileInputCapture(limits=CaptureLimits(max_bytes=16)) as capture:
        with bind_compile_input_capture(capture):
            with pytest.raises(CaptureAborted) as raised:
                sample_compile_bytes(str(path), 4)
            assert raised.value.code == "byte_limit"


def _parser_case(tmp_path, simulator):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    lists = tmp_path / "lists"
    lists.mkdir()
    outer, nested = tmp_path / "design.f", lists / "nested.f"
    outer.write_text("-F lists/nested.f\n")
    nested.write_text("+define+FROM_NESTED=17\n../top.sv\n")
    log = tmp_path / f"{simulator}.log"
    if simulator == "vcs":
        log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog -f {outer} -top top\n")
    elif simulator == "xcelium":
        # Wrapper commands must replay the filelist. A native indented xrun
        # transcript already contains its expansion and deliberately avoids
        # refeeding -f (covered separately below).
        log.write_text(
            f"cd {tmp_path} && xrun -f {outer} -top top\n"
            f"file: {source}\n  module worklib.top:v\n"
        )
    else:
        log.write_text(f"make: Entering directory '{tmp_path}'\nverilator --cc -f design.f --top-module top\n")
    return source, log, outer, nested


@pytest.mark.parametrize("simulator", ["vcs", "xcelium", "verilator"])
def test_real_text_parsers_and_nested_filelists_share_capture(tmp_path, simulator):
    source, log, outer, nested = _parser_case(tmp_path, simulator)
    with CompileInputCapture() as capture, bind_compile_input_capture(capture):
        assert detect_simulator(str(log)) == simulator
        result = parse_compile_log(str(log), simulator)
        assert result["simulator"] == simulator
        assert "top" in result["top_modules"]
        assert [item["path"] for item in result["files"]["user"]] == [str(source.resolve())]
        options = extract_preprocessor_options(result, str(source))
        assert options.complete
        assert options.macro_dict()["FROM_NESTED"] == "17"
        expected_paths = {str(p.resolve()) for p in (log, outer, nested)}
        assert set(_records(capture)) == expected_paths
        for path in (log, outer, nested):
            assert _records(capture)[str(path.resolve())].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert capture.validate()


@pytest.mark.parametrize("simulator", ["vcs", "xcelium", "verilator"])
def test_parser_log_budget_failure_is_not_a_parse_warning(tmp_path, simulator):
    _, log, _, _ = _parser_case(tmp_path, simulator)
    with CompileInputCapture(limits=CaptureLimits(max_bytes=log.stat().st_size - 1)) as capture:
        with bind_compile_input_capture(capture):
            with pytest.raises(CaptureAborted) as raised:
                parse_compile_log(str(log), simulator)
            assert raised.value.code == "byte_limit"


@pytest.mark.parametrize("simulator", ["vcs", "xcelium", "verilator"])
def test_nested_filelist_budget_failure_is_not_downgraded(tmp_path, simulator):
    source, log, _, _ = _parser_case(tmp_path, simulator)
    with CompileInputCapture(limits=CaptureLimits(max_files=2)) as capture:
        with bind_compile_input_capture(capture):
            with pytest.raises(CaptureAborted) as raised:
                result = parse_compile_log(str(log), simulator)
                extract_preprocessor_options(result, str(source))
            assert raised.value.code == "file_limit"


def _environment_context(tmp_path):
    source, _, outer, nested = _parser_case(tmp_path, "vcs")
    return source, outer, nested, {
        "simulator": "vcs",
        "compile_cwd": str(tmp_path),
        "compile_command": f"vcs -f {outer} -top top",
        "compile_evidence": {
            "schema_version": 1,
            "unit_order_source": "simulator_log",
            "ordered_compilation_units": [{"path": str(source), "role": "project"}],
        },
    }


def test_compile_environment_inference_captures_its_nested_documents(tmp_path):
    _, outer, nested, context = _environment_context(tmp_path)
    with CompileInputCapture() as capture, bind_compile_input_capture(capture):
        result = resolve_compile_environment(context)
        assert result.complete
        assert set(_records(capture)) == {str(p.resolve()) for p in (outer, nested)}
        assert capture.validate()


def test_compile_environment_does_not_swallow_capture_abort(tmp_path):
    _, _, _, context = _environment_context(tmp_path)
    with CompileInputCapture(limits=CaptureLimits(max_files=1)) as capture:
        with bind_compile_input_capture(capture):
            with pytest.raises(CaptureAborted) as raised:
                resolve_compile_environment(context)
            assert raised.value.code == "file_limit"


def test_native_xcelium_expansion_does_not_replay_filelist(tmp_path):
    source, log, outer, nested = _parser_case(tmp_path, "xcelium")
    log.write_text(
        f"xrun\n  -f {outer}\n    -F {nested}\n"
        f"      +define+FROM_NATIVE=23\n      {source}\n"
        f"  -top top\nfile: {source}\n  module worklib.top:v\n"
    )
    with CompileInputCapture() as capture, bind_compile_input_capture(capture):
        result = parse_compile_log(str(log), "xcelium")
        options = extract_preprocessor_options(result, str(source))
        assert options.complete
        assert options.macro_dict()["FROM_NATIVE"] == "23"
        assert "FROM_NESTED" not in options.macro_dict()
        assert set(_records(capture)) == {str(log.resolve())}
        # Preparation must separately capture these recorded support files;
        # parser I/O alone cannot prove the full compile-input closure.
        assert {item["path"] for item in result["compile_evidence"]["filelists"]} == {
            str(outer.resolve()), str(nested.resolve()),
        }
