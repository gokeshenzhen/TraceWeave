"""Lazy bounded disk storage for exact Source Graph artifacts.

The store persists only the canonical :class:`ConnectivityIR` JSON and a
small, versioned manifest.  It deliberately has no query-result, query-engine,
waveform, frontend-AST, or hierarchy-enumeration state.  Construction is
side-effect free; filesystem work begins only on an exact lookup, publication,
or explicit maintenance call.

Entries are addressed directly by the accepted ``SourceGraphArtifactIdentity``
digest.  Publication happens through a private sibling temporary directory and
one atomic directory rename, so readers never observe a partially written
canonical entry.  Capacity maintenance is confined to publication and explicit
inspection, and deletes only validated entry directories inside this module's
private namespace.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Iterator
import uuid

from .connectivity_ir import CONNECTIVITY_IR_VERSION, ConnectivityIR, CoverageStatus
from .source_graph_contract import (
    SOURCE_GRAPH_ARTIFACT_IDENTITY_VERSION,
    SOURCE_GRAPH_BUILD_CONTRACT_VERSION,
    SOURCE_GRAPH_PROJECTOR_NAME,
    SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactIdentity,
    SourceGraphArtifactKey,
    SourceGraphArtifactScopeReceipt,
    compute_source_graph_artifact_key,
)


SOURCE_GRAPH_DISK_CACHE_FORMAT = "traceweave_source_graph_connectivity_ir"
SOURCE_GRAPH_DISK_CACHE_FORMAT_VERSION = "1.0"
SOURCE_GRAPH_DISK_CACHE_NAMESPACE = ("source_graph", "disk-v1")
SOURCE_GRAPH_DISK_CACHE_MANIFEST = "manifest.json"
SOURCE_GRAPH_DISK_CACHE_IR = "connectivity_ir.json"
DEFAULT_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES = 8
DEFAULT_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES = 512 * 1024 * 1024
MAX_SOURCE_GRAPH_DISK_MANIFEST_BYTES = 16 * 1024 * 1024
_IO_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHARD_RE = re.compile(r"^[0-9a-f]{2}$")
_PRIVATE_FILE_MASK = stat.S_IRWXG | stat.S_IRWXO
_PRIVATE_DIRECTORY_MODE = stat.S_IRWXU
_PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
_EXPECTED_ENTRY_NAMES = frozenset(
    {SOURCE_GRAPH_DISK_CACHE_MANIFEST, SOURCE_GRAPH_DISK_CACHE_IR}
)


class DiskCacheCancelled(RuntimeError):
    """Raised when cooperative cancellation is observed during disk work."""


class DiskValidationOutcome(str, Enum):
    HIT = "hit"
    NOT_FOUND = "not_found"
    IDENTITY_NOT_REUSABLE = "identity_not_reusable"
    UNSAFE_NAMESPACE = "unsafe_namespace"
    UNSAFE_ENTRY = "unsafe_entry"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_TOO_LARGE = "manifest_too_large"
    MANIFEST_INVALID = "manifest_invalid"
    UNKNOWN_FORMAT = "unknown_format"
    INCOMPLETE_ENTRY = "incomplete_entry"
    ARTIFACT_KEY_MISMATCH = "artifact_key_mismatch"
    ARTIFACT_IDENTITY_MISMATCH = "artifact_identity_mismatch"
    BUILD_SEMANTICS_MISMATCH = "build_semantics_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    VERSION_MISMATCH = "version_mismatch"
    COVERAGE_RECEIPT_MISMATCH = "coverage_receipt_mismatch"
    IR_MISSING = "ir_missing"
    IR_TOO_LARGE = "ir_too_large"
    IR_SIZE_MISMATCH = "ir_size_mismatch"
    IR_DIGEST_MISMATCH = "ir_digest_mismatch"
    IR_SCHEMA_MISMATCH = "ir_schema_mismatch"
    IR_IDENTITY_MISMATCH = "ir_identity_mismatch"
    IO_ERROR = "io_error"


class DiskPublishOutcome(str, Enum):
    PUBLISHED = "published"
    ALREADY_PRESENT = "already_present"
    BYPASS_IDENTITY = "bypass_identity"
    BYPASS_CAPACITY = "bypass_capacity"
    INVALID_ARTIFACT = "invalid_artifact"
    UNSAFE_NAMESPACE = "unsafe_namespace"
    UNSAFE_EXISTING_ENTRY = "unsafe_existing_entry"
    IO_ERROR = "io_error"


@dataclass(frozen=True)
class DiskArtifact:
    identity: SourceGraphArtifactIdentity
    artifact_key: SourceGraphArtifactKey
    scope_receipt: SourceGraphArtifactScopeReceipt
    ir_json_bytes: bytes
    ir_fingerprint_sha256: str
    ir_bytes: int
    entry_bytes: int


@dataclass(frozen=True)
class DiskLookupResult:
    outcome: DiskValidationOutcome
    artifact: DiskArtifact | None = None
    bytes_read: int = 0

    @property
    def hit(self) -> bool:
        return self.outcome is DiskValidationOutcome.HIT and self.artifact is not None

    @property
    def corrupt(self) -> bool:
        return self.outcome not in {
            DiskValidationOutcome.HIT,
            DiskValidationOutcome.NOT_FOUND,
            DiskValidationOutcome.IDENTITY_NOT_REUSABLE,
        }


@dataclass(frozen=True)
class DiskPublishResult:
    outcome: DiskPublishOutcome
    entry_bytes: int = 0
    disk_entry_count: int = 0
    disk_bytes: int = 0
    eviction_count: int = 0
    unsafe_entry_count: int = 0

    @property
    def published(self) -> bool:
        return self.outcome in {
            DiskPublishOutcome.PUBLISHED,
            DiskPublishOutcome.ALREADY_PRESENT,
        }


@dataclass(frozen=True)
class DiskCacheSnapshot:
    entry_count: int
    disk_bytes: int
    unsafe_entry_count: int


@dataclass(frozen=True)
class _OwnedEntry:
    digest: str
    path: Path
    entry_bytes: int


class _UnsafeFilesystem(RuntimeError):
    pass


class _ValidationMiss(RuntimeError):
    def __init__(self, outcome: DiskValidationOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload, object_pairs_hook=_strict_object)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    if _canonical_json_bytes(value) != payload:
        raise ValueError("JSON is not canonical")
    return value


def _validate_digest(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("artifact digest must be lowercase SHA-256 hex")
    return value


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise DiskCacheCancelled("source graph disk cache operation cancelled")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _owned_by_current_user(info: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or info.st_uid == os.getuid()


def _is_private_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not info.st_mode & _PRIVATE_FILE_MASK
        and _owned_by_current_user(info)
    )


def _is_private_regular_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not info.st_mode & _PRIVATE_FILE_MASK
        and _owned_by_current_user(info)
    )


def _normalized_gap_label(value: str) -> str:
    label = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not label or not label[0].isalpha():
        label = "gap_" + label
    return label[:120]


def _expected_gap_codes(ir: ConnectivityIR) -> tuple[str, ...]:
    gap_codes = {_normalized_gap_label(gap.code) for gap in ir.coverage.gaps}
    if ir.coverage.status is not CoverageStatus.COMPLETE and not gap_codes:
        gap_codes.add("coverage_incomplete_without_detailed_gap")
    return tuple(sorted(gap_codes))


class SourceGraphDiskCache:
    """Direct-addressed, content-validated Source Graph disk artifact store."""

    def __init__(
        self,
        cache_root: str | Path,
        *,
        max_entries: int = DEFAULT_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES,
        max_bytes: int = DEFAULT_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES,
    ) -> None:
        raw_root = os.fspath(cache_root)
        if not raw_root or "\x00" in raw_root:
            raise ValueError("disk cache root must be a non-empty path")
        root = Path(raw_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("disk cache root must be absolute and traversal-free")
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries < 1
        ):
            raise ValueError("disk cache max_entries must be a positive integer")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
        ):
            raise ValueError("disk cache max_bytes must be a positive integer")
        self._cache_root = root
        self._namespace_root = root.joinpath(*SOURCE_GRAPH_DISK_CACHE_NAMESPACE)
        self._entries_root = self._namespace_root / "entries"
        self._temp_root = self._namespace_root / "tmp"
        self._marker_path = self._namespace_root / "namespace.json"
        self._lock_path = self._namespace_root / "maintenance.lock"
        self.max_entries = max_entries
        self.max_bytes = max_bytes

    @property
    def namespace_root(self) -> Path:
        return self._namespace_root

    def entry_path(self, digest: str) -> Path:
        digest = _validate_digest(digest)
        return self._entries_root / digest[:2] / digest

    def lookup(
        self,
        identity: SourceGraphArtifactIdentity,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> DiskLookupResult:
        """Read and fully validate one exact artifact without enumerating peers."""

        _check_cancelled(cancelled)
        artifact_key = compute_source_graph_artifact_key(identity)
        if not artifact_key.cross_request_reusable:
            return DiskLookupResult(DiskValidationOutcome.IDENTITY_NOT_REUSABLE)
        namespace_state = self._validate_existing_namespace()
        if namespace_state is DiskValidationOutcome.NOT_FOUND:
            return DiskLookupResult(DiskValidationOutcome.NOT_FOUND)
        if namespace_state is not None:
            return DiskLookupResult(namespace_state)
        return self._lookup_entry(identity, artifact_key, cancelled=cancelled)

    def publish(
        self,
        identity: SourceGraphArtifactIdentity,
        ir_json_bytes: bytes,
        scope_receipt: SourceGraphArtifactScopeReceipt,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> DiskPublishResult:
        """Validate, privately stage, atomically publish, then enforce bounds."""

        _check_cancelled(cancelled)
        artifact_key = compute_source_graph_artifact_key(identity)
        if not artifact_key.cross_request_reusable:
            return DiskPublishResult(DiskPublishOutcome.BYPASS_IDENTITY)
        try:
            ir, fingerprint = self._validate_publish_payload(
                identity, ir_json_bytes, scope_receipt
            )
            manifest = self._manifest(
                identity,
                artifact_key,
                ir,
                ir_json_bytes,
                fingerprint,
                scope_receipt,
            )
            manifest_bytes = _canonical_json_bytes(manifest)
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            _ValidationMiss,
        ):
            return DiskPublishResult(DiskPublishOutcome.INVALID_ARTIFACT)

        entry_bytes = len(ir_json_bytes) + len(manifest_bytes)
        if entry_bytes > self.max_bytes:
            return DiskPublishResult(
                DiskPublishOutcome.BYPASS_CAPACITY,
                entry_bytes=entry_bytes,
            )

        try:
            self._ensure_namespace()
        except (OSError, _UnsafeFilesystem):
            return DiskPublishResult(DiskPublishOutcome.UNSAFE_NAMESPACE)

        temp_path: Path | None = None
        try:
            _check_cancelled(cancelled)
            temp_path = Path(
                tempfile.mkdtemp(
                    prefix=f"{artifact_key.digest}.", dir=str(self._temp_root)
                )
            )
            os.chmod(temp_path, _PRIVATE_DIRECTORY_MODE)
            self._write_private_file(
                temp_path / SOURCE_GRAPH_DISK_CACHE_IR,
                ir_json_bytes,
                cancelled=cancelled,
            )
            self._write_private_file(
                temp_path / SOURCE_GRAPH_DISK_CACHE_MANIFEST,
                manifest_bytes,
                cancelled=cancelled,
            )
            self._fsync_directory(temp_path)
            _check_cancelled(cancelled)

            with self._maintenance_lock(cancelled):
                existing = self._lookup_entry(
                    identity, artifact_key, cancelled=cancelled
                )
                if existing.hit:
                    snapshot = self._scan_owned_entries(cancelled=cancelled)
                    self._remove_temp_directory(temp_path)
                    temp_path = None
                    return DiskPublishResult(
                        DiskPublishOutcome.ALREADY_PRESENT,
                        entry_bytes=existing.artifact.entry_bytes,
                        disk_entry_count=len(snapshot[0]),
                        disk_bytes=sum(item.entry_bytes for item in snapshot[0]),
                        unsafe_entry_count=snapshot[1],
                    )
                final_path = self.entry_path(artifact_key.digest)
                if _lstat(final_path) is not None and not self._remove_exact_entry(
                    final_path
                ):
                    return DiskPublishResult(
                        DiskPublishOutcome.UNSAFE_EXISTING_ENTRY,
                        entry_bytes=entry_bytes,
                    )

                owned_entries, unsafe_count = self._scan_owned_entries(
                    cancelled=cancelled
                )
                eviction_count = 0
                current_bytes = sum(item.entry_bytes for item in owned_entries)
                while (
                    len(owned_entries) + 1 > self.max_entries
                    or current_bytes + entry_bytes > self.max_bytes
                ):
                    _check_cancelled(cancelled)
                    if not owned_entries:
                        return DiskPublishResult(
                            DiskPublishOutcome.BYPASS_CAPACITY,
                            entry_bytes=entry_bytes,
                            unsafe_entry_count=unsafe_count,
                        )
                    victim = min(owned_entries, key=lambda item: item.digest)
                    if not self._remove_exact_entry(victim.path):
                        return DiskPublishResult(
                            DiskPublishOutcome.UNSAFE_EXISTING_ENTRY,
                            entry_bytes=entry_bytes,
                            unsafe_entry_count=unsafe_count + 1,
                        )
                    owned_entries.remove(victim)
                    current_bytes -= victim.entry_bytes
                    eviction_count += 1

                _check_cancelled(cancelled)
                shard = final_path.parent
                self._ensure_private_directory(shard)
                os.rename(temp_path, final_path)
                temp_path = None
                self._fsync_directory(shard)
                return DiskPublishResult(
                    DiskPublishOutcome.PUBLISHED,
                    entry_bytes=entry_bytes,
                    disk_entry_count=len(owned_entries) + 1,
                    disk_bytes=current_bytes + entry_bytes,
                    eviction_count=eviction_count,
                    unsafe_entry_count=unsafe_count,
                )
        except DiskCacheCancelled:
            raise
        except (OSError, _UnsafeFilesystem):
            return DiskPublishResult(
                DiskPublishOutcome.IO_ERROR,
                entry_bytes=entry_bytes,
            )
        finally:
            if temp_path is not None:
                self._remove_temp_directory(temp_path)

    def maintenance_snapshot(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> DiskCacheSnapshot:
        """Explicitly enumerate validated entries for evidence or maintenance."""

        _check_cancelled(cancelled)
        state = self._validate_existing_namespace()
        if state is DiskValidationOutcome.NOT_FOUND:
            return DiskCacheSnapshot(0, 0, 0)
        if state is not None:
            return DiskCacheSnapshot(0, 0, 1)
        with self._maintenance_lock(cancelled):
            entries, unsafe_count = self._scan_owned_entries(cancelled=cancelled)
        return DiskCacheSnapshot(
            entry_count=len(entries),
            disk_bytes=sum(entry.entry_bytes for entry in entries),
            unsafe_entry_count=unsafe_count,
        )

    def _validate_existing_namespace(self) -> DiskValidationOutcome | None:
        try:
            self._ensure_no_symlink_components(self._namespace_root)
        except _UnsafeFilesystem:
            return DiskValidationOutcome.UNSAFE_NAMESPACE
        root_info = _lstat(self._namespace_root)
        if root_info is None:
            parent_info = _lstat(self._namespace_root.parent)
            if parent_info is not None and not _is_private_directory(parent_info):
                return DiskValidationOutcome.UNSAFE_NAMESPACE
            return DiskValidationOutcome.NOT_FOUND
        if not _is_private_directory(root_info):
            return DiskValidationOutcome.UNSAFE_NAMESPACE
        for path in (self._entries_root, self._temp_root):
            info = _lstat(path)
            if info is None or not _is_private_directory(info):
                return DiskValidationOutcome.UNSAFE_NAMESPACE
        try:
            marker = self._read_private_file(
                self._marker_path,
                max_bytes=4096,
                cancelled=None,
                missing=DiskValidationOutcome.UNSAFE_NAMESPACE,
                unsafe=DiskValidationOutcome.UNSAFE_NAMESPACE,
            )
            marker_value = _strict_json_object(marker)
        except (OSError, ValueError, _ValidationMiss):
            return DiskValidationOutcome.UNSAFE_NAMESPACE
        if marker_value != self._namespace_marker():
            return DiskValidationOutcome.UNSAFE_NAMESPACE
        return None

    def _ensure_namespace(self) -> None:
        self._ensure_no_symlink_components(self._cache_root)
        if _lstat(self._cache_root) is None:
            try:
                self._cache_root.mkdir(parents=True, mode=_PRIVATE_DIRECTORY_MODE)
            except FileExistsError:
                pass
        cache_info = _lstat(self._cache_root)
        if cache_info is None or not stat.S_ISDIR(cache_info.st_mode):
            raise _UnsafeFilesystem("cache root is not a directory")
        if stat.S_ISLNK(cache_info.st_mode):
            raise _UnsafeFilesystem("cache root is a symlink")

        source_graph_root = self._namespace_root.parent
        self._ensure_private_directory(source_graph_root)
        namespace_created = _lstat(self._namespace_root) is None
        self._ensure_private_directory(self._namespace_root)
        self._ensure_private_directory(self._entries_root)
        self._ensure_private_directory(self._temp_root)
        if namespace_created or _lstat(self._marker_path) is None:
            marker_temp = self._namespace_root / f".namespace.{uuid.uuid4().hex}.tmp"
            try:
                self._write_private_file(
                    marker_temp,
                    _canonical_json_bytes(self._namespace_marker()),
                    cancelled=None,
                )
                os.replace(marker_temp, self._marker_path)
            finally:
                try:
                    marker_temp.unlink()
                except FileNotFoundError:
                    pass
            self._fsync_directory(self._namespace_root)
        state = self._validate_existing_namespace()
        if state is not None:
            raise _UnsafeFilesystem(state.value)

    @staticmethod
    def _namespace_marker() -> dict[str, Any]:
        return {
            "format": SOURCE_GRAPH_DISK_CACHE_FORMAT,
            "namespace_version": SOURCE_GRAPH_DISK_CACHE_FORMAT_VERSION,
        }

    @staticmethod
    def _ensure_no_symlink_components(path: Path) -> None:
        existing: list[Path] = []
        current = path
        while True:
            info = _lstat(current)
            if info is not None:
                existing.append(current)
            if current.parent == current:
                break
            current = current.parent
        for component in reversed(existing):
            info = _lstat(component)
            if info is not None and stat.S_ISLNK(info.st_mode):
                raise _UnsafeFilesystem("cache path contains a symlink")

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        info = _lstat(path)
        if info is None:
            try:
                path.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
                os.chmod(path, _PRIVATE_DIRECTORY_MODE)
            except FileExistsError:
                pass
            info = _lstat(path)
        if info is None or not _is_private_directory(info):
            raise _UnsafeFilesystem("cache directory is unsafe")

    @staticmethod
    def _write_private_file(
        path: Path,
        payload: bytes,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, _PRIVATE_FILE_MODE)
        try:
            os.fchmod(fd, _PRIVATE_FILE_MODE)
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                _check_cancelled(cancelled)
                written = os.write(fd, view[offset : offset + _IO_CHUNK_BYTES])
                if written < 1:
                    raise OSError("short disk cache write")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _read_private_file(
        path: Path,
        *,
        max_bytes: int,
        cancelled: Callable[[], bool] | None,
        missing: DiskValidationOutcome,
        unsafe: DiskValidationOutcome,
    ) -> bytes:
        initial_info = _lstat(path)
        if initial_info is None:
            raise _ValidationMiss(missing)
        if not _is_private_regular_file(initial_info):
            raise _ValidationMiss(unsafe)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise _ValidationMiss(missing) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _ValidationMiss(unsafe) from exc
            raise
        try:
            info = os.fstat(fd)
            if not _is_private_regular_file(info):
                raise _ValidationMiss(unsafe)
            if info.st_size > max_bytes:
                raise _ValidationMiss(
                    DiskValidationOutcome.MANIFEST_TOO_LARGE
                    if path.name == SOURCE_GRAPH_DISK_CACHE_MANIFEST
                    else DiskValidationOutcome.IR_TOO_LARGE
                )
            chunks: list[bytes] = []
            remaining = info.st_size
            while remaining:
                _check_cancelled(cancelled)
                chunk = os.read(fd, min(_IO_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) != info.st_size:
                raise _ValidationMiss(
                    DiskValidationOutcome.MANIFEST_INVALID
                    if path.name == SOURCE_GRAPH_DISK_CACHE_MANIFEST
                    else DiskValidationOutcome.IR_SIZE_MISMATCH
                )
            return payload
        finally:
            os.close(fd)

    def _lookup_entry(
        self,
        identity: SourceGraphArtifactIdentity,
        artifact_key: SourceGraphArtifactKey,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> DiskLookupResult:
        entry_path = self.entry_path(artifact_key.digest)
        entry_info = _lstat(entry_path)
        if entry_info is None:
            return DiskLookupResult(DiskValidationOutcome.NOT_FOUND)
        if not _is_private_directory(entry_info):
            return DiskLookupResult(DiskValidationOutcome.UNSAFE_ENTRY)
        try:
            names = {item.name for item in entry_path.iterdir()}
        except OSError:
            return DiskLookupResult(DiskValidationOutcome.IO_ERROR)
        if names != _EXPECTED_ENTRY_NAMES:
            return DiskLookupResult(DiskValidationOutcome.UNSAFE_ENTRY)

        try:
            manifest_bytes = self._read_private_file(
                entry_path / SOURCE_GRAPH_DISK_CACHE_MANIFEST,
                max_bytes=MAX_SOURCE_GRAPH_DISK_MANIFEST_BYTES,
                cancelled=cancelled,
                missing=DiskValidationOutcome.MANIFEST_MISSING,
                unsafe=DiskValidationOutcome.UNSAFE_ENTRY,
            )
            manifest = _strict_json_object(manifest_bytes)
            self._validate_manifest(manifest, identity, artifact_key)
            ir_meta = manifest["ir"]
            assert isinstance(ir_meta, Mapping)
            declared_size = int(ir_meta["size_bytes"])
            if declared_size > self.max_bytes:
                raise _ValidationMiss(DiskValidationOutcome.IR_TOO_LARGE)
            ir_bytes = self._read_private_file(
                entry_path / SOURCE_GRAPH_DISK_CACHE_IR,
                max_bytes=self.max_bytes,
                cancelled=cancelled,
                missing=DiskValidationOutcome.IR_MISSING,
                unsafe=DiskValidationOutcome.UNSAFE_ENTRY,
            )
            if len(ir_bytes) != declared_size:
                raise _ValidationMiss(DiskValidationOutcome.IR_SIZE_MISMATCH)
            digest = hashlib.sha256(ir_bytes).hexdigest()
            if digest != ir_meta["sha256"]:
                raise _ValidationMiss(DiskValidationOutcome.IR_DIGEST_MISMATCH)
            try:
                ir = ConnectivityIR.from_json_bytes(ir_bytes)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise _ValidationMiss(DiskValidationOutcome.IR_SCHEMA_MISMATCH) from exc
            if ir.to_json_bytes() != ir_bytes:
                raise _ValidationMiss(DiskValidationOutcome.IR_SCHEMA_MISMATCH)
            receipt = SourceGraphArtifactScopeReceipt.from_dict(
                manifest["scope_receipt"]
            )
            self._validate_ir_and_receipt(ir, identity, receipt)
            self._validate_coverage_manifest(
                ir,
                identity,
                receipt,
                manifest["coverage"],
            )
        except DiskCacheCancelled:
            raise
        except _ValidationMiss as miss:
            return DiskLookupResult(miss.outcome)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return DiskLookupResult(DiskValidationOutcome.MANIFEST_INVALID)
        except OSError:
            return DiskLookupResult(DiskValidationOutcome.IO_ERROR)

        entry_bytes = len(manifest_bytes) + len(ir_bytes)
        if entry_bytes > self.max_bytes:
            return DiskLookupResult(DiskValidationOutcome.IR_TOO_LARGE)
        return DiskLookupResult(
            DiskValidationOutcome.HIT,
            artifact=DiskArtifact(
                identity=identity,
                artifact_key=artifact_key,
                scope_receipt=receipt,
                ir_json_bytes=ir_bytes,
                ir_fingerprint_sha256=digest,
                ir_bytes=len(ir_bytes),
                entry_bytes=entry_bytes,
            ),
            bytes_read=entry_bytes,
        )

    @staticmethod
    def _validate_publish_payload(
        identity: SourceGraphArtifactIdentity,
        ir_json_bytes: bytes,
        scope_receipt: SourceGraphArtifactScopeReceipt,
    ) -> tuple[ConnectivityIR, str]:
        if not isinstance(ir_json_bytes, bytes):
            raise ValueError("disk cache IR payload must be bytes")
        ir = ConnectivityIR.from_json_bytes(ir_json_bytes)
        if ir.to_json_bytes() != ir_json_bytes:
            raise ValueError("disk cache IR payload must be canonical")
        SourceGraphDiskCache._validate_ir_and_receipt(ir, identity, scope_receipt)
        return ir, hashlib.sha256(ir_json_bytes).hexdigest()

    @staticmethod
    def _validate_ir_and_receipt(
        ir: ConnectivityIR,
        identity: SourceGraphArtifactIdentity,
        scope_receipt: SourceGraphArtifactScopeReceipt,
    ) -> None:
        if scope_receipt.scope != identity.scope:
            raise _ValidationMiss(DiskValidationOutcome.SCOPE_MISMATCH)
        if scope_receipt.coverage_status is not ir.coverage.status:
            raise _ValidationMiss(DiskValidationOutcome.COVERAGE_RECEIPT_MISMATCH)
        if scope_receipt.gap_codes != _expected_gap_codes(ir):
            raise _ValidationMiss(DiskValidationOutcome.COVERAGE_RECEIPT_MISMATCH)
        source = identity.source
        if (
            ir.frontend_name != source.frontend_name
            or ir.frontend_version != source.frontend_version
            or ir.ir_version != source.ir_schema_version
            or identity.scope.top not in ir.top_instances
        ):
            raise _ValidationMiss(DiskValidationOutcome.IR_IDENTITY_MISMATCH)

    @staticmethod
    def _validate_coverage_manifest(
        ir: ConnectivityIR,
        identity: SourceGraphArtifactIdentity,
        receipt: SourceGraphArtifactScopeReceipt,
        coverage: Mapping[str, Any],
    ) -> None:
        expected = {
            "status": ir.coverage.status.value,
            "gap_codes": list(receipt.gap_codes),
            "objective_exclusions": list(
                identity.scope.coverage_boundary.objective_exclusions
            ),
            "files_total": ir.coverage.files_total,
            "files_projected": ir.coverage.files_projected,
            "diagnostic_count": ir.coverage.diagnostic_count,
            "blocking_diagnostic_count": ir.coverage.blocking_diagnostic_count,
        }
        if dict(coverage) != expected:
            raise _ValidationMiss(DiskValidationOutcome.COVERAGE_RECEIPT_MISMATCH)

    @staticmethod
    def _manifest(
        identity: SourceGraphArtifactIdentity,
        artifact_key: SourceGraphArtifactKey,
        ir: ConnectivityIR,
        ir_json_bytes: bytes,
        fingerprint: str,
        scope_receipt: SourceGraphArtifactScopeReceipt,
    ) -> dict[str, Any]:
        source = identity.source
        scope = identity.scope
        return {
            "format": SOURCE_GRAPH_DISK_CACHE_FORMAT,
            "format_version": SOURCE_GRAPH_DISK_CACHE_FORMAT_VERSION,
            "completed": True,
            "artifact": {
                "digest": artifact_key.digest,
                "build_semantics_digest": artifact_key.build_semantics_digest,
                "scope_digest": artifact_key.scope_digest,
                "identity": identity.to_dict(),
            },
            "snapshots": {
                "compile_snapshot_sha256": identity.compile_snapshot_sha256,
                "compile_inputs_sha256": source.compile_inputs.fingerprint,
                "hierarchy_snapshot_sha256": scope.hierarchy_snapshot_sha256,
            },
            "versions": {
                "artifact_identity": identity.identity_version,
                "build_contract": identity.build_contract_version,
                "adapter": identity.adapter_version,
                "worker_protocol": identity.worker_protocol_version,
                "frontend_name": source.frontend_name,
                "frontend": source.frontend_version,
                "projector_name": source.projector_name,
                "projector": source.projector_version,
                "projector_schema": source.projector_schema_version,
                "ir_schema": source.ir_schema_version,
            },
            "scope_receipt": scope_receipt.to_dict(),
            "coverage": {
                "status": ir.coverage.status.value,
                "gap_codes": list(scope_receipt.gap_codes),
                "objective_exclusions": list(
                    scope.coverage_boundary.objective_exclusions
                ),
                "files_total": ir.coverage.files_total,
                "files_projected": ir.coverage.files_projected,
                "diagnostic_count": ir.coverage.diagnostic_count,
                "blocking_diagnostic_count": ir.coverage.blocking_diagnostic_count,
            },
            "ir": {
                "file": SOURCE_GRAPH_DISK_CACHE_IR,
                "sha256": fingerprint,
                "size_bytes": len(ir_json_bytes),
                "schema_version": CONNECTIVITY_IR_VERSION,
            },
        }

    @staticmethod
    def _validate_manifest(
        manifest: Mapping[str, Any],
        expected_identity: SourceGraphArtifactIdentity,
        expected_key: SourceGraphArtifactKey,
    ) -> None:
        expected_top_keys = {
            "format",
            "format_version",
            "completed",
            "artifact",
            "snapshots",
            "versions",
            "scope_receipt",
            "coverage",
            "ir",
        }
        if set(manifest) != expected_top_keys:
            raise _ValidationMiss(DiskValidationOutcome.MANIFEST_INVALID)
        if (
            manifest.get("format") != SOURCE_GRAPH_DISK_CACHE_FORMAT
            or manifest.get("format_version") != SOURCE_GRAPH_DISK_CACHE_FORMAT_VERSION
        ):
            raise _ValidationMiss(DiskValidationOutcome.UNKNOWN_FORMAT)
        if manifest.get("completed") is not True:
            raise _ValidationMiss(DiskValidationOutcome.INCOMPLETE_ENTRY)

        artifact = manifest.get("artifact")
        snapshots = manifest.get("snapshots")
        versions = manifest.get("versions")
        coverage = manifest.get("coverage")
        ir_meta = manifest.get("ir")
        receipt_value = manifest.get("scope_receipt")
        if not all(
            isinstance(item, Mapping)
            for item in (
                artifact,
                snapshots,
                versions,
                coverage,
                ir_meta,
                receipt_value,
            )
        ):
            raise _ValidationMiss(DiskValidationOutcome.MANIFEST_INVALID)
        assert isinstance(artifact, Mapping)
        identity_value = artifact.get("identity")
        if not isinstance(identity_value, Mapping):
            raise _ValidationMiss(DiskValidationOutcome.MANIFEST_INVALID)
        stored_identity = SourceGraphArtifactIdentity.from_dict(identity_value)
        if stored_identity != expected_identity:
            raise _ValidationMiss(DiskValidationOutcome.ARTIFACT_IDENTITY_MISMATCH)
        stored_key = compute_source_graph_artifact_key(stored_identity)
        if artifact.get("digest") != expected_key.digest:
            raise _ValidationMiss(DiskValidationOutcome.ARTIFACT_KEY_MISMATCH)
        if (
            artifact.get("build_semantics_digest")
            != expected_key.build_semantics_digest
            or stored_key.build_semantics_digest != expected_key.build_semantics_digest
        ):
            raise _ValidationMiss(DiskValidationOutcome.BUILD_SEMANTICS_MISMATCH)
        if (
            artifact.get("scope_digest") != expected_key.scope_digest
            or stored_key.scope_digest != expected_key.scope_digest
        ):
            raise _ValidationMiss(DiskValidationOutcome.SCOPE_MISMATCH)
        if stored_key.digest != expected_key.digest:
            raise _ValidationMiss(DiskValidationOutcome.ARTIFACT_KEY_MISMATCH)

        assert isinstance(snapshots, Mapping)
        expected_snapshots = {
            "compile_snapshot_sha256": expected_identity.compile_snapshot_sha256,
            "compile_inputs_sha256": (
                expected_identity.source.compile_inputs.fingerprint
            ),
            "hierarchy_snapshot_sha256": (
                expected_identity.scope.hierarchy_snapshot_sha256
            ),
        }
        if dict(snapshots) != expected_snapshots:
            raise _ValidationMiss(DiskValidationOutcome.SNAPSHOT_MISMATCH)

        source = expected_identity.source
        expected_versions = {
            "artifact_identity": SOURCE_GRAPH_ARTIFACT_IDENTITY_VERSION,
            "build_contract": SOURCE_GRAPH_BUILD_CONTRACT_VERSION,
            "adapter": expected_identity.adapter_version,
            "worker_protocol": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
            "frontend_name": source.frontend_name,
            "frontend": source.frontend_version,
            "projector_name": SOURCE_GRAPH_PROJECTOR_NAME,
            "projector": SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION,
            "projector_schema": SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION,
            "ir_schema": CONNECTIVITY_IR_VERSION,
        }
        if dict(versions) != expected_versions:
            raise _ValidationMiss(DiskValidationOutcome.VERSION_MISMATCH)

        receipt = SourceGraphArtifactScopeReceipt.from_dict(receipt_value)
        if receipt.scope != expected_identity.scope:
            raise _ValidationMiss(DiskValidationOutcome.SCOPE_MISMATCH)
        expected_coverage = {
            "status": receipt.coverage_status.value,
            "gap_codes": list(receipt.gap_codes),
            "objective_exclusions": list(
                expected_identity.scope.coverage_boundary.objective_exclusions
            ),
        }
        for name, value in expected_coverage.items():
            if coverage.get(name) != value:
                raise _ValidationMiss(DiskValidationOutcome.COVERAGE_RECEIPT_MISMATCH)
        if set(coverage) != {
            "status",
            "gap_codes",
            "objective_exclusions",
            "files_total",
            "files_projected",
            "diagnostic_count",
            "blocking_diagnostic_count",
        }:
            raise _ValidationMiss(DiskValidationOutcome.MANIFEST_INVALID)
        if (
            ir_meta.get("file") != SOURCE_GRAPH_DISK_CACHE_IR
            or ir_meta.get("schema_version") != CONNECTIVITY_IR_VERSION
            or not isinstance(ir_meta.get("size_bytes"), int)
            or isinstance(ir_meta.get("size_bytes"), bool)
            or int(ir_meta["size_bytes"]) < 1
            or not isinstance(ir_meta.get("sha256"), str)
            or not _SHA256_RE.fullmatch(str(ir_meta["sha256"]))
            or set(ir_meta) != {"file", "sha256", "size_bytes", "schema_version"}
        ):
            raise _ValidationMiss(DiskValidationOutcome.MANIFEST_INVALID)

    def _scan_owned_entries(
        self,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[list[_OwnedEntry], int]:
        entries: list[_OwnedEntry] = []
        unsafe_count = 0
        try:
            shards = sorted(self._entries_root.iterdir(), key=lambda item: item.name)
        except FileNotFoundError:
            return entries, unsafe_count
        for shard in shards:
            _check_cancelled(cancelled)
            shard_info = _lstat(shard)
            if (
                not _SHARD_RE.fullmatch(shard.name)
                or shard_info is None
                or not _is_private_directory(shard_info)
            ):
                unsafe_count += 1
                continue
            try:
                candidates = sorted(shard.iterdir(), key=lambda item: item.name)
            except OSError:
                unsafe_count += 1
                continue
            for candidate in candidates:
                _check_cancelled(cancelled)
                owned = self._inspect_owned_entry(candidate, shard.name)
                if owned is None:
                    unsafe_count += 1
                else:
                    entries.append(owned)
        return entries, unsafe_count

    def _inspect_owned_entry(self, path: Path, shard: str) -> _OwnedEntry | None:
        info = _lstat(path)
        if (
            not _SHA256_RE.fullmatch(path.name)
            or path.name[:2] != shard
            or info is None
            or not _is_private_directory(info)
        ):
            return None
        try:
            names = {item.name for item in path.iterdir()}
        except OSError:
            return None
        if names != _EXPECTED_ENTRY_NAMES:
            return None
        manifest_path = path / SOURCE_GRAPH_DISK_CACHE_MANIFEST
        ir_path = path / SOURCE_GRAPH_DISK_CACHE_IR
        manifest_info = _lstat(manifest_path)
        ir_info = _lstat(ir_path)
        if (
            manifest_info is None
            or ir_info is None
            or not _is_private_regular_file(manifest_info)
            or not _is_private_regular_file(ir_info)
            or manifest_info.st_size > MAX_SOURCE_GRAPH_DISK_MANIFEST_BYTES
        ):
            return None
        try:
            manifest_bytes = self._read_private_file(
                manifest_path,
                max_bytes=MAX_SOURCE_GRAPH_DISK_MANIFEST_BYTES,
                cancelled=None,
                missing=DiskValidationOutcome.MANIFEST_MISSING,
                unsafe=DiskValidationOutcome.UNSAFE_ENTRY,
            )
            manifest = _strict_json_object(manifest_bytes)
            artifact = manifest.get("artifact")
            ir_meta = manifest.get("ir")
            if (
                manifest.get("format") != SOURCE_GRAPH_DISK_CACHE_FORMAT
                or manifest.get("format_version")
                != SOURCE_GRAPH_DISK_CACHE_FORMAT_VERSION
                or manifest.get("completed") is not True
                or not isinstance(artifact, Mapping)
                or artifact.get("digest") != path.name
                or not isinstance(ir_meta, Mapping)
                or ir_meta.get("file") != SOURCE_GRAPH_DISK_CACHE_IR
                or ir_meta.get("size_bytes") != ir_info.st_size
            ):
                return None
        except (OSError, ValueError, _ValidationMiss):
            return None
        return _OwnedEntry(
            digest=path.name,
            path=path,
            entry_bytes=manifest_info.st_size + ir_info.st_size,
        )

    def _remove_exact_entry(self, path: Path) -> bool:
        info = _lstat(path)
        if info is None:
            return True
        if not _is_private_directory(info):
            return False
        try:
            children = list(path.iterdir())
        except OSError:
            return False
        if {child.name for child in children} - _EXPECTED_ENTRY_NAMES:
            return False
        for name in _EXPECTED_ENTRY_NAMES:
            child = path / name
            child_info = _lstat(child)
            if child_info is None:
                continue
            if not _owned_by_current_user(child_info):
                return False
            if not (
                stat.S_ISREG(child_info.st_mode)
                or stat.S_ISLNK(child_info.st_mode)
                or stat.S_ISFIFO(child_info.st_mode)
            ):
                return False
            try:
                child.unlink()
            except OSError:
                return False
        try:
            path.rmdir()
        except OSError:
            return False
        return True

    def _remove_temp_directory(self, path: Path) -> bool:
        try:
            if path.parent != self._temp_root:
                return False
            prefix = path.name.split(".", 1)[0]
            if not _SHA256_RE.fullmatch(prefix):
                return False
            info = _lstat(path)
            if info is None:
                return True
            if not _is_private_directory(info):
                return False
            children = list(path.iterdir())
            if {child.name for child in children} - _EXPECTED_ENTRY_NAMES:
                return False
            for child in children:
                child_info = _lstat(child)
                if child_info is None or not _owned_by_current_user(child_info):
                    return False
                if not stat.S_ISREG(child_info.st_mode):
                    return False
                child.unlink()
            path.rmdir()
            return True
        except OSError:
            return False

    @contextmanager
    def _maintenance_lock(self, cancelled: Callable[[], bool] | None) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self._lock_path, flags, _PRIVATE_FILE_MODE)
        try:
            os.fchmod(fd, _PRIVATE_FILE_MODE)
            if not _is_private_regular_file(os.fstat(fd)):
                raise _UnsafeFilesystem("maintenance lock is unsafe")
            while True:
                _check_cancelled(cancelled)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
