"""Bounded on-disk KDB generation evidence, without loading a vendor API.

The elaboration maps identify a candidate generation. They do not prove that
NPI can load it or that its compiled sources match the files on disk. Unmapped
database payloads are outside this fingerprint; receipts preserve that gap.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import stat
import time

from .cancellation import check_cancelled
from .compile_input_capture import CaptureAborted


_MAP_NAMES = ("_libFileMap", "_nstnElabDB", "_smElabDBMap", "_stnElabDB", ".hasElabcomError")
_BASE_GAPS = ("kdb_identity_bounded", "kdb_source_association_unverified")


class KdbBindingChanged(RuntimeError):
    """A query must discard its result instead of switching design generations."""

    def __init__(self):
        super().__init__("kdb_identity_changed")


def validate_query_anchor(anchor: KdbAnchor) -> None:
    """Admit bounded candidate evidence, without claiming NPI loadability."""
    if (not isinstance(anchor, KdbAnchor) or not anchor.identity_complete
            or anchor.artifact_status not in {"elaborated_candidate", "elaboration_error"}
            or anchor.max_bytes > 16 * 1024 * 1024):
        raise ValueError("a complete bounded elaborated KDB candidate is required")


def _stat_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class KdbMapRecord:
    name: str
    canonical_path: str
    stat_identity: tuple[int, ...] | None
    sha256: str | None
    disposition: str

    @property
    def size(self):
        return self.stat_identity[3] if self.stat_identity is not None else 0


@dataclass(frozen=True)
class KdbAnchor:
    path: str
    canonical_path: str
    artifact_status: str
    identity_sha256: str
    identity_complete: bool
    gaps: tuple[str, ...]
    map_records: tuple[KdbMapRecord, ...]
    root_identity: tuple[int, ...] | None
    max_bytes: int

    def metadata_current(self) -> bool:
        """Recheck aliases/stat after a multi-design content-validation pass.

        This is not a replacement for current(), which hashes the maps. It
        catches ordinary changes to an earlier design while a later design is
        being hashed, without rereading the same maps for each waveform side.
        """
        check_cancelled()
        if os.path.realpath(self.path) != self.canonical_path:
            return False
        try:
            root = os.stat(self.path)
        except FileNotFoundError:
            return (self.artifact_status == "missing" and self.root_identity is None
                    and not self.map_records)
        except OSError:
            return False
        if (root.st_dev, root.st_ino, root.st_mode) != self.root_identity:
            return False
        for record in self.map_records:
            check_cancelled()
            path = os.path.join(self.path, record.name)
            if os.path.realpath(path) != record.canonical_path:
                return False
            try:
                identity = _stat_identity(os.stat(path))
            except FileNotFoundError:
                if record.disposition != "missing":
                    return False
                continue
            except OSError:
                return False
            if identity != record.stat_identity:
                return False
        return True

    def current(self, *, timeout_sec: float = 5.0) -> bool:
        try:
            observed = capture_kdb_anchor(self.path, max_bytes=self.max_bytes, timeout_sec=timeout_sec)
        except CaptureAborted as exc:
            if exc.code != "input_changed":
                raise
            return False
        return observed.identity_sha256 == self.identity_sha256

    def receipt(self) -> dict:
        return {
            "artifact_status": self.artifact_status,
            "identity_sha256": self.identity_sha256,
            "identity_basis": "bounded_elaboration_maps",
            "identity_status": "complete" if self.identity_complete else "partial",
            "load_status": "not_checked",
            "source_association": "unverified",
            "gaps": list(self.gaps),
        }

    def require_current(self) -> None:
        if not self.current():
            raise KdbBindingChanged()


def _read_map(path, name, remaining, checkpoint):
    checkpoint()
    canonical = os.path.realpath(path)
    try:
        before = os.stat(path)
    except FileNotFoundError:
        return KdbMapRecord(name, canonical, None, None, "missing")
    except OSError:
        return KdbMapRecord(name, canonical, None, None, "unreadable")
    identity = _stat_identity(before)
    if not stat.S_ISREG(before.st_mode):
        return KdbMapRecord(name, canonical, identity, None, "not_regular")
    if before.st_size > remaining:
        return KdbMapRecord(name, canonical, identity, None, "byte_limit")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if _stat_identity(os.fstat(fd)) != identity:
                raise CaptureAborted("input_changed")
            digest, read = hashlib.sha256(), 0
            while True:
                checkpoint()
                block = os.read(fd, min(1024 * 1024, before.st_size - read + 1))
                if not block:
                    break
                read += len(block)
                if read > before.st_size:
                    raise CaptureAborted("input_changed")
                digest.update(block)
            if read != before.st_size or _stat_identity(os.fstat(fd)) != identity:
                raise CaptureAborted("input_changed")
        finally:
            os.close(fd)
        if os.path.realpath(path) != canonical or _stat_identity(os.stat(path)) != identity:
            raise CaptureAborted("input_changed")
        return KdbMapRecord(name, canonical, identity, digest.hexdigest(), "present")
    except OSError:
        return KdbMapRecord(name, canonical, identity, None, "unreadable")


def _observe(path, max_bytes, checkpoint):
    checkpoint()
    canonical = os.path.realpath(path)
    gaps = set(_BASE_GAPS)
    try:
        root = os.stat(path)
    except FileNotFoundError:
        return canonical, None, (), "missing", True, tuple(sorted(gaps))
    except OSError:
        gaps.add("kdb_root_unreadable")
        return canonical, None, (), "unrecognized", False, tuple(sorted(gaps))
    # NPI lock creation changes directory mtime/size, not the design identity.
    root_identity = (root.st_dev, root.st_ino, root.st_mode)
    if not stat.S_ISDIR(root.st_mode):
        gaps.add("kdb_not_directory")
        return canonical, root_identity, (), "unrecognized", False, tuple(sorted(gaps))
    records, remaining = [], max_bytes
    for name in _MAP_NAMES:
        record = _read_map(os.path.join(path, name), name, remaining, checkpoint)
        records.append(record)
        if record.disposition == "present":
            remaining -= record.size
        elif record.disposition != "missing":
            gaps.add("kdb_map_" + record.disposition)
    by_name = {record.name: record for record in records}
    present = lambda name: by_name[name].disposition == "present" and by_name[name].size > 0
    marker = by_name[".hasElabcomError"]
    if marker.stat_identity is not None and stat.S_ISREG(marker.stat_identity[2]):
        status = "elaboration_error"
    elif marker.disposition != "missing":
        # An inaccessible path does not establish that an error marker exists.
        status = "unrecognized"
    elif present("_libFileMap") and (present("_stnElabDB") or present("_nstnElabDB")):
        status = "elaborated_candidate"
    elif present("_libFileMap"):
        status = "source_only_candidate"
    else:
        checkpoint()
        try:
            with os.scandir(path) as entries:
                status = "empty" if next(entries, None) is None else "unrecognized"
        except OSError:
            status = "unrecognized"
            gaps.add("kdb_root_unreadable")
    checkpoint()
    try:
        end = os.stat(path)
    except OSError as exc:
        raise CaptureAborted("input_changed") from exc
    if canonical != os.path.realpath(path) or root_identity != (end.st_dev, end.st_ino, end.st_mode):
        raise CaptureAborted("input_changed")
    complete = all(record.disposition in {"present", "missing"} for record in records)
    complete &= "kdb_root_unreadable" not in gaps
    return canonical, root_identity, tuple(records), status, complete, tuple(sorted(gaps))


def capture_kdb_anchor(path: str, *, max_bytes: int = 16 * 1024 * 1024, timeout_sec: float = 5.0) -> KdbAnchor:
    """Read at most five fixed maps twice, bounded by max_bytes per pass.

    Both passes must agree, including raw hashes, to handle coarse filesystem
    timestamps. Missing/partial observations are valid evidence of limitations,
    never proof of loadability or whole-database integrity.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("KDB map budget must be a positive integer")
    if (isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float))
            or not math.isfinite(timeout_sec) or timeout_sec <= 0):
        raise ValueError("KDB anchor timeout must be finite and positive")
    deadline = time.monotonic() + timeout_sec

    def checkpoint():
        check_cancelled()
        if time.monotonic() >= deadline:
            raise CaptureAborted("timeout")

    path = os.path.abspath(path)
    first = _observe(path, max_bytes, checkpoint)
    second = _observe(path, max_bytes, checkpoint)
    if first != second:
        raise CaptureAborted("input_changed")
    canonical, root, records, status, complete, gaps = first
    payload = {
        "version": 1, "path": path, "canonical": canonical, "root": root,
        "maps": [(r.name, r.canonical_path, r.stat_identity, r.sha256, r.disposition) for r in records],
        "status": status, "complete": complete, "gaps": gaps,
    }
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    checkpoint()
    return KdbAnchor(path, canonical, status, identity, complete, gaps, records, root, max_bytes)
