"""Immutable content facts captured while hierarchy sources are already open.

The snapshot is private process-session state.  It stores only bounded digest,
stat, size, and fixed-label marker facts; source text is never retained.  A
Source Graph manifest may reuse a record only while its full stat identity is
unchanged, preserving the hierarchy rebuild boundary without a second content
read.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import locale
import os
import re
from typing import Callable

from .cancellation import check_cancelled


SOURCE_CONTENT_MARKERS: tuple[tuple[re.Pattern[bytes], str], ...] = (
    (re.compile(rb"\b(?:import|export)\s*\"DPI", re.IGNORECASE), "dpi_runtime"),
    (
        re.compile(rb"\b(?:force|release)\b", re.IGNORECASE),
        "procedural_force_release",
    ),
    (re.compile(rb"\bbind\b", re.IGNORECASE), "bind_semantics"),
    (
        re.compile(rb"(?:`pragma\s+protect|`protect|`protected)", re.IGNORECASE),
        "protected_region",
    ),
    (
        re.compile(rb"\b(?:uvm_pkg|uvm_[a-z0-9_]+)\b", re.IGNORECASE),
        "uvm_dynamic_connectivity",
    ),
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _stat_identity(path: str) -> tuple[int, int, int, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _decode_source(data: bytes) -> str:
    encoding = locale.getpreferredencoding(False) or "utf-8"
    text = data.decode(encoding, errors="replace")
    # Match TextIOWrapper's default universal-newline behavior used by the
    # previous text-mode source loader.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _marker_codes(data: bytes) -> tuple[str, ...]:
    return tuple(
        code for pattern, code in SOURCE_CONTENT_MARKERS if pattern.search(data)
    )


@dataclass(frozen=True)
class FileContentSnapshot:
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    objective_exclusions: tuple[str, ...] = ()

    def current(self) -> bool:
        return _stat_identity(self.path) == (
            self.device,
            self.inode,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        )

    def identity_record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "bytes": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "sha256": self.sha256,
            "objective_exclusions": list(self.objective_exclusions),
        }


@dataclass(frozen=True)
class CompileSessionSnapshot:
    records: tuple[FileContentSnapshot, ...]
    complete: bool
    issue_codes: tuple[str, ...]
    content_fingerprint_sha256: str

    @property
    def file_count(self) -> int:
        return len(self.records)

    @property
    def total_bytes(self) -> int:
        return sum(record.size for record in self.records)

    def record_map(self) -> dict[str, FileContentSnapshot]:
        return {record.path: record for record in self.records}

    def current(self) -> bool:
        for index, record in enumerate(self.records):
            if index % 256 == 0:
                check_cancelled()
            if not record.current():
                return False
        return self.complete


@dataclass(frozen=True)
class SourceContentRead:
    """One decoded source body plus facts derived from its exact raw bytes."""

    text: str
    snapshot: FileContentSnapshot | None
    issue_codes: tuple[str, ...] = ()


def read_source_content(path: str) -> SourceContentRead:
    """Read one source with before/after identity and exact-byte evidence."""

    canonical = os.path.realpath(path)
    check_cancelled()
    before = _stat_identity(canonical)
    with open(canonical, "rb") as stream:
        data = stream.read()
    check_cancelled()
    after = _stat_identity(canonical)
    issues: set[str] = set()
    if before is None or after is None or before != after:
        issues.add("compile_content_changed_during_scan")
    stable = after or before
    snapshot = None
    if stable is not None:
        snapshot = FileContentSnapshot(
            path=canonical,
            device=stable[0],
            inode=stable[1],
            size=stable[2],
            mtime_ns=stable[3],
            ctime_ns=stable[4],
            sha256=hashlib.sha256(data).hexdigest(),
            objective_exclusions=_marker_codes(data),
        )
    return SourceContentRead(
        text=_decode_source(data),
        snapshot=snapshot,
        issue_codes=tuple(sorted(issues)),
    )


class CompileSessionSnapshotBuilder:
    """Capture exact raw-byte facts through the hierarchy source loader."""

    def __init__(
        self,
        *,
        indexed_reader: Callable[[str], SourceContentRead] | None = None,
    ) -> None:
        self._records: dict[str, FileContentSnapshot] = {}
        self._issues: set[str] = set()
        self._indexed_reader = indexed_reader

    def mark_issue(self, code: str) -> None:
        self._issues.add(code)

    def read_text(self, path: str) -> str:
        content = (
            self._indexed_reader(path)
            if self._indexed_reader is not None
            else read_source_content(path)
        )
        self._issues.update(content.issue_codes)
        record = content.snapshot
        if record is not None:
            previous = self._records.get(record.path)
            if previous is not None and previous != record:
                self._issues.add("compile_content_changed_during_scan")
            else:
                self._records[record.path] = record
        return content.text

    def finish(self) -> CompileSessionSnapshot:
        records = tuple(sorted(self._records.values(), key=lambda item: item.path))
        if not records:
            self._issues.add("compile_content_snapshot_empty")
        for index, record in enumerate(records):
            if index % 256 == 0:
                check_cancelled()
            if not record.current():
                self._issues.add("compile_content_changed_during_scan")
                break
        issues = tuple(sorted(self._issues))
        fingerprint = hashlib.sha256(
            _canonical_json([record.identity_record() for record in records])
        ).hexdigest()
        return CompileSessionSnapshot(
            records=records,
            complete=not issues,
            issue_codes=issues,
            content_fingerprint_sha256=fingerprint,
        )
