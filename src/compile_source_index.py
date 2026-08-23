"""Bounded transient source content shared by compile-session consumers.

The index owns decoded source only while active callers hold a lease. Exact
raw-byte snapshot facts survive independently in hierarchy results; source text
is cleared when the final lease releases. Capacity bypass is an optimization
miss, never a functional blocker.
"""

from __future__ import annotations

from collections.abc import Iterable
import os
import sys
import threading

from .cancellation import check_cancelled
from .compile_session_snapshot import SourceContentRead, read_source_content


DEFAULT_COMPILE_SOURCE_INDEX_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_COMPILE_SOURCE_INDEX_MAX_FILES = 32_768


class CompileSourceIndex:
    """A byte/file-bounded source-text tier with exact snapshot evidence."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_COMPILE_SOURCE_INDEX_MAX_BYTES,
        max_files: int = DEFAULT_COMPILE_SOURCE_INDEX_MAX_FILES,
    ) -> None:
        self._max_bytes = max(0, int(max_bytes))
        self._max_files = max(0, int(max_files))
        self._entries: dict[str, SourceContentRead] = {}
        self._lock = threading.Lock()
        self._cache_bytes = 0
        self._cache_peak_bytes = 0
        self._planned_file_count = 0
        self._planned_bytes = 0
        self._physical_read_count = 0
        self._physical_read_bytes = 0
        self._cache_hit_count = 0
        self._cache_miss_count = 0
        self._capacity_bypass_count = 0
        self._closed = False
        self._preload_status = "not_started"

    @staticmethod
    def _canonical_paths(paths: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(os.path.realpath(path) for path in paths))

    def preload(self, paths: Iterable[str]) -> None:
        """Populate all planned paths only when the whole set fits."""

        with self._lock:
            if self._preload_status != "not_started":
                return
            self._preload_status = "planning"
        canonical_paths = self._canonical_paths(paths)
        planned_bytes = 0
        readable_paths: list[tuple[str, int]] = []
        for index, path in enumerate(canonical_paths):
            if index % 256 == 0:
                check_cancelled()
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            planned_bytes += size
            readable_paths.append((path, size))
        with self._lock:
            self._planned_file_count = len(canonical_paths)
            self._planned_bytes = planned_bytes
            over_capacity = (
                not self._max_bytes
                or not self._max_files
                or len(readable_paths) > self._max_files
                or planned_bytes > self._max_bytes
            )
            if over_capacity:
                self._preload_status = "bypass_capacity"
                self._capacity_bypass_count += len(readable_paths)
                return
        pending: list[tuple[str, SourceContentRead, int]] = []
        pending_bytes = 0
        for path, _size in readable_paths:
            check_cancelled()
            content = read_source_content(path)
            record = content.snapshot
            source_bytes = record.size if record is not None else len(content.text)
            entry_bytes = self._entry_resident_bytes(path, content)
            with self._lock:
                self._physical_read_count += 1
                self._physical_read_bytes += source_bytes
            pending.append((path, content, entry_bytes))
            pending_bytes += entry_bytes
            if pending_bytes > self._max_bytes:
                # Decoded Python strings can occupy more memory than their raw
                # source bytes.  Do not publish a prefix when the measured
                # resident payload crosses the configured bound.
                with self._lock:
                    self._preload_status = "bypass_capacity"
                    self._capacity_bypass_count += len(readable_paths)
                return
        with self._lock:
            if self._closed:
                self._preload_status = "closed"
                return
            self._entries = {
                path: content for path, content, _entry_bytes in pending
            }
            self._cache_bytes = pending_bytes
            self._cache_peak_bytes = max(self._cache_peak_bytes, pending_bytes)
            self._preload_status = "ready"

    @staticmethod
    def _entry_resident_bytes(
        canonical: str,
        content: SourceContentRead,
    ) -> int:
        """Conservative size of objects retained solely by one cache entry."""

        total = (
            sys.getsizeof(canonical)
            + sys.getsizeof(content)
            + sys.getsizeof(content.text)
            + sys.getsizeof(content.issue_codes)
        )
        total += sum(sys.getsizeof(code) for code in content.issue_codes)
        snapshot = content.snapshot
        if snapshot is not None:
            total += (
                sys.getsizeof(snapshot)
                + sys.getsizeof(snapshot.path)
                + sys.getsizeof(snapshot.sha256)
                + sys.getsizeof(snapshot.objective_exclusions)
            )
            total += sum(
                sys.getsizeof(value)
                for value in (
                    snapshot.device,
                    snapshot.inode,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.ctime_ns,
                )
            )
            total += sum(
                sys.getsizeof(code) for code in snapshot.objective_exclusions
            )
        # Reserve dictionary/table growth and references.  This keeps the
        # configured limit conservative without depending on CPython's current
        # hash-table allocation strategy.
        return total + 128

    def read(self, path: str) -> SourceContentRead:
        canonical = os.path.realpath(path)
        check_cancelled()
        with self._lock:
            cached = self._entries.get(canonical)
            if cached is not None:
                self._cache_hit_count += 1
                return cached
            self._cache_miss_count += 1
        # Serialize a rare lazy miss so concurrent include consumers cannot
        # publish duplicate physical reads. Preloaded compile inputs take the
        # short hit path above.
        with self._lock:
            cached = self._entries.get(canonical)
            if cached is not None:
                self._cache_hit_count += 1
                return cached
            # Reading under this private lock is intentional and bounded to a
            # single source file; it keeps same-path lazy misses single-flight.
            content = read_source_content(canonical)
            record = content.snapshot
            source_bytes = record.size if record is not None else len(content.text)
            entry_bytes = self._entry_resident_bytes(canonical, content)
            self._physical_read_count += 1
            self._physical_read_bytes += source_bytes
            can_store = (
                not self._closed
                and self._preload_status != "bypass_capacity"
                and self._max_bytes > 0
                and self._max_files > 0
                and len(self._entries) < self._max_files
                and self._cache_bytes + entry_bytes <= self._max_bytes
            )
            if can_store:
                self._entries[canonical] = content
                self._cache_bytes += entry_bytes
                self._cache_peak_bytes = max(
                    self._cache_peak_bytes,
                    self._cache_bytes,
                )
            else:
                self._capacity_bypass_count += 1
            return content

    def read_text(self, path: str) -> str:
        return self.read(path).text

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()
            self._cache_bytes = 0

    def metrics_snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "compile_source_index_status": self._preload_status,
                "compile_source_index_planned_file_count": (
                    self._planned_file_count
                ),
                "compile_source_index_planned_bytes": self._planned_bytes,
                "compile_source_index_physical_read_count": (
                    self._physical_read_count
                ),
                "compile_source_index_physical_read_bytes": (
                    self._physical_read_bytes
                ),
                "compile_source_index_cache_hit_count": self._cache_hit_count,
                "compile_source_index_cache_miss_count": self._cache_miss_count,
                "compile_source_index_capacity_bypass_count": (
                    self._capacity_bypass_count
                ),
                "compile_source_index_entry_count": len(self._entries),
                "compile_source_index_cache_bytes": self._cache_bytes,
                "compile_source_index_cache_peak_bytes": self._cache_peak_bytes,
                "compile_source_index_max_bytes": self._max_bytes,
                "compile_source_index_max_files": self._max_files,
            }
