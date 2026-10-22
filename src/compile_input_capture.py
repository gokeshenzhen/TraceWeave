"""Bounded, call-local raw input capture for independent compile preparation.

The optional ContextVar reader binds existing log/filelist parsers to the bytes
being fingerprinted. It does not change their normal streaming I/O. No HDL
frontend, hierarchy, license, or source-body cache outlives this capture.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
import stat
import time

from .cancellation import check_cancelled
from .compile_session_snapshot import (
    CompileSessionSnapshot, FileContentSnapshot, SourceContentRead, _decode_source, _marker_codes,
)
from .hdl_suffixes import is_protected_systemverilog_path


class CaptureAborted(RuntimeError):
    """A failed capture must not become an ordinary parser warning."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CaptureLimits:
    max_files: int = 32768
    max_bytes: int = 128 * 1024 * 1024
    timeout_sec: float = 30.0
    max_references: int = 65536

    def __post_init__(self):
        for value in (self.max_files, self.max_bytes, self.max_references):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("capture limits must be positive integers")
        if (isinstance(self.timeout_sec, bool)
                or not math.isfinite(self.timeout_sec) or self.timeout_sec <= 0):
            raise ValueError("capture timeout must be finite and positive")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _availability(path: str) -> tuple:
    try:
        return (os.path.realpath(path), *_identity(os.stat(path)))
    except OSError as exc:
        return (os.path.realpath(path), exc.errno)


class CompileInputCapture:
    """Own bounded input bodies until close; retain only immutable facts after.

    Limits count unique canonical files and raw bytes, including logs and
    filelists. Validation rehashes at most the captured size plus one byte per
    file. A timeout is cooperative between filesystem reads. Unavailable paths
    remain explicit negative observations, never proof of a complete closure.
    """

    def __init__(self, limits: CaptureLimits | None = None):
        self.limits = limits or CaptureLimits()
        self._deadline = time.monotonic() + self.limits.timeout_sec
        self._bodies: dict[str, bytes] = {}
        self._records: dict[str, FileContentSnapshot] = {}
        self._aliases: dict[str, str] = {}
        self._unavailable: dict[str, tuple] = {}
        self._total_bytes = 0
        self._read_bytes = 0
        self._path_read_bytes = {}
        self._closed = False

    def __enter__(self):
        self.checkpoint()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._bodies.clear()
        self._closed = True

    @property
    def resident_bytes(self) -> int:
        return sum(map(len, self._bodies.values()))

    @property
    def body_count(self) -> int:
        return len(self._bodies)

    @property
    def read_bytes_count(self) -> int:
        """Actual bytes read, including publication-time validation."""
        return self._read_bytes

    @property
    def records(self) -> tuple[FileContentSnapshot, ...]:
        return tuple(self._records.values())

    @property
    def aliases(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._aliases.items())

    @property
    def unavailable(self) -> tuple[str, ...]:
        return tuple(self._unavailable)

    @property
    def unavailable_observations(self) -> tuple[tuple[str, tuple], ...]:
        return tuple(self._unavailable.items())

    def checkpoint(self) -> None:
        check_cancelled()
        if self._closed:
            raise RuntimeError("compile input capture is closed")
        if time.monotonic() >= self._deadline:
            raise CaptureAborted("timeout")

    def _read(self, path: str, byte_limit: int, *, digest_only: bool = False):
        """Read regular files only; a FIFO cannot defeat cooperative deadlines."""
        self.checkpoint()
        before = os.stat(path)
        if not stat.S_ISREG(before.st_mode):
            raise CaptureAborted("input_not_regular")
        if before.st_size > byte_limit:
            raise CaptureAborted("byte_limit")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise CaptureAborted("input_not_regular")
            if _identity(before) != _identity(opened):
                raise CaptureAborted("input_changed")
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        chunks = []
        digest = hashlib.sha256()
        consumed = 0
        with stream:
            while True:
                self.checkpoint()
                chunk = stream.read(min(1024 * 1024, byte_limit - consumed + 1))
                self._read_bytes += len(chunk)
                if chunk:
                    self._path_read_bytes[path] = self._path_read_bytes.get(path, 0) + len(chunk)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > byte_limit:
                    raise CaptureAborted("byte_limit")
                if digest_only:
                    digest.update(chunk)
                else:
                    chunks.append(chunk)
            after = os.fstat(stream.fileno())
        self.checkpoint()
        try:
            final = os.stat(path)
        except OSError as exc:
            raise CaptureAborted("input_changed") from exc
        if (_identity(before) != _identity(after)
                or _identity(after) != _identity(final)
                or consumed != after.st_size):
            raise CaptureAborted("input_changed")
        return (digest.hexdigest() if digest_only else b"".join(chunks)), after

    def observe_path(self, path: str | os.PathLike) -> str:
        """Keep spelling-to-target evidence before a parser normalizes a path."""
        self.checkpoint()
        spelling = os.path.abspath(path)
        canonical = os.path.realpath(spelling)
        prior = self._aliases.get(spelling)
        if prior is not None and prior != canonical:
            raise CaptureAborted("input_changed")
        if prior is None and len(self._aliases) >= self.limits.max_references:
            raise CaptureAborted("reference_limit")
        self._aliases[spelling] = canonical
        return canonical

    def observe_metadata_path(self, path):
        # Full source preparation retains its historical replay/negative-input
        # rules; only the native metadata reader opts into this parser hook.
        self.checkpoint()

    def read_bytes(self, path: str | os.PathLike) -> bytes:
        spelling = os.path.abspath(path)
        canonical = self.observe_path(spelling)
        if canonical in self._records:
            if not self._records[canonical].current():
                raise CaptureAborted("input_changed")
            return self._bodies[canonical]
        # Unavailable attempts consume the file budget too.
        if (spelling not in self._unavailable
                and len(self._records) + len(self._unavailable) >= self.limits.max_files):
            raise CaptureAborted("file_limit")
        try:
            data, info = self._read(canonical, self.limits.max_bytes - self._total_bytes)
        except OSError:
            self._unavailable[spelling] = _availability(spelling)
            raise
        if os.path.realpath(spelling) != canonical:
            raise CaptureAborted("input_changed")
        if spelling in self._unavailable:
            # A missing/unreadable file that becomes readable is a new input.
            raise CaptureAborted("input_changed")
        markers = set(_marker_codes(data))
        if is_protected_systemverilog_path(canonical):
            markers.add("protected_region")
        self._records[canonical] = FileContentSnapshot(
            canonical, *_identity(info), hashlib.sha256(data).hexdigest(),
            tuple(sorted(markers)),
        )
        self._bodies[canonical] = data
        self._total_bytes += len(data)
        return data

    def read_text(self, path: str | os.PathLike, *, encoding: str | None = None) -> str:
        raw = self.read_bytes(path)
        with io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, errors="replace") as stream:
            return stream.read()

    def read_source_content(self, path: str | os.PathLike) -> SourceContentRead:
        """Expose the historical source-reader shape over the captured bytes."""
        raw = self.read_bytes(path)
        canonical = self.observe_path(path)
        return SourceContentRead(_decode_source(raw), self._records[canonical])

    def validate(self) -> bool:
        self.checkpoint()
        for spelling, canonical in self._aliases.items():
            self.checkpoint()
            if os.path.realpath(spelling) != canonical:
                return False
        for spelling, observed in self._unavailable.items():
            self.checkpoint()
            if _availability(spelling) != observed:
                return False
        for record in self._records.values():
            self.checkpoint()
            if not record.current():
                return False
            try:
                digest, _ = self._read(record.path, record.size, digest_only=True)
            except OSError:
                return False
            except CaptureAborted as exc:
                if exc.code in {"input_changed", "byte_limit", "input_not_regular"}:
                    return False
                raise
            if digest != record.sha256 or not record.current():
                return False
        # Later reads must not conceal an earlier file/alias changing while
        # the rest of a large capture is being verified.
        for spelling, canonical in self._aliases.items():
            self.checkpoint()
            if os.path.realpath(spelling) != canonical:
                return False
        for record in self._records.values():
            self.checkpoint()
            if not record.current():
                return False
        for spelling, observed in self._unavailable.items():
            self.checkpoint()
            if _availability(spelling) != observed:
                return False
        return True

    def snapshot(self) -> CompileSessionSnapshot:
        if not self.validate():
            raise CaptureAborted("input_changed")
        records = tuple(sorted(self._records.values(), key=lambda r: r.path))
        issues = ("compile_input_unavailable",) if self._unavailable else ()
        if not records:
            issues += ("compile_content_snapshot_empty",)
        fingerprint = hashlib.sha256(json.dumps(
            [record.identity_record() for record in records],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        self.checkpoint()
        return CompileSessionSnapshot(records, not issues, issues, fingerprint)


_capture: ContextVar[CompileInputCapture | None] = ContextVar("compile_input_capture", default=None)


def resolve_compile_path(path: str | os.PathLike) -> str:
    capture = _capture.get()
    return capture.observe_path(path) if capture is not None else os.path.realpath(path)


def observe_compile_metadata_path(path):
    capture = _capture.get()
    if capture is not None:
        capture.observe_metadata_path(path)


def validate_captured_inputs(
    snapshot: CompileSessionSnapshot,
    aliases: tuple[tuple[str, str], ...],
    unavailable: tuple[tuple[str, tuple], ...] = (),
    *, timeout_sec: float = 30.0,
) -> bool:
    """Revalidate private snapshot facts without retaining source bodies.

    This validates observations even for an incomplete snapshot; it does not
    upgrade missing-input or dependency coverage into a complete identity.
    """
    with CompileInputCapture(CaptureLimits(timeout_sec=timeout_sec)) as capture:
        capture._records = {r.path: r for r in snapshot.records}
        capture._aliases = dict(aliases)
        capture._unavailable = dict(unavailable)
        return capture.validate()


@contextmanager
def bind_compile_input_capture(capture: CompileInputCapture):
    capture.checkpoint()
    token = _capture.set(capture)
    try:
        yield capture
    finally:
        _capture.reset(token)


def open_compile_text(path: str | os.PathLike, *, encoding: str | None = None):
    capture = _capture.get()
    if capture is None:
        return open(path, "r", encoding=encoding, errors="replace")
    return io.TextIOWrapper(io.BytesIO(capture.read_bytes(path)),
                            encoding=encoding, errors="replace")


def read_compile_text(path: str | os.PathLike, *, encoding: str | None = None) -> str:
    with open_compile_text(path, encoding=encoding) as stream:
        return stream.read()


def sample_compile_bytes(path: str | os.PathLike, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 2:
        raise ValueError("sample byte limit must be at least two")
    capture = _capture.get()
    if capture is not None:
        data = capture.read_bytes(path)
        if len(data) <= max_bytes:
            return data
        return data[:max_bytes // 2] + b"\n" + data[-(max_bytes // 2):]
    with open(path, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size <= max_bytes:
            return stream.read(max_bytes)
        head = stream.read(max_bytes // 2)
        stream.seek(size - max_bytes // 2)
        return head + b"\n" + stream.read(max_bytes // 2)
