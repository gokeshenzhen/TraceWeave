"""Handle store for build_tb_hierarchy full payloads.

The MCP `build_tb_hierarchy` tool returns a slim payload to the LLM (project
summary, tree skeleton, ambiguous basenames, etc.) while the full hierarchy
data — files, complete component_tree, class_hierarchy, raw compile_result —
remains accessible only via a content-addressed handle. Subsequent handle
tools (`get_tb_subtree`, `lookup_tb_files`, ...) resolve the handle to fetch
the slice they need.

Design points:
- Handle format: ``tbh_<sha8>`` derived from absolute compile_log path,
  simulator, and compile_log mtime. Recompiling the design changes mtime and
  therefore the handle, so stale handles cannot silently point at fresh data.
- Storage: in-process dict, not persisted. Server restart invalidates every
  handle. This matches the lifetime of `_result_cache` in server.py.
- Single active handle per session is the common case; the store supports
  multiple but does not provide an enumeration API by design.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any


HANDLE_PREFIX = "tbh_"
_HANDLE_SHA_LEN = 8


def compute_handle(compile_log: str, simulator: str) -> str:
    """Derive a content-addressed handle for a compile_log+simulator pair.

    The mtime is folded into the digest so that recompiling — which rewrites
    the log — produces a fresh handle and invalidates the previous one
    automatically. If the file is missing we fall back to mtime=0; the
    resulting handle is still stable for the (path, simulator) pair, but a
    later successful build will replace it.
    """
    abs_path = os.path.abspath(compile_log) if compile_log else ""
    try:
        mtime_ns = os.stat(abs_path).st_mtime_ns if abs_path else 0
    except OSError:
        mtime_ns = 0
    material = f"{abs_path}|{simulator or ''}|{mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:_HANDLE_SHA_LEN]
    return f"{HANDLE_PREFIX}{digest}"


class HandleStore:
    """In-process registry mapping handle strings to full hierarchy payloads.

    The store does not own the payload's lifetime — `server._result_cache`
    is the authoritative cache. The store keeps a reference alongside so that
    handle tools can resolve without re-running build_tb_hierarchy.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def register(self, handle: str, full_result: dict[str, Any]) -> None:
        if not handle.startswith(HANDLE_PREFIX):
            raise ValueError(f"invalid handle format: {handle!r}")
        self._entries[handle] = full_result

    def resolve(self, handle: str) -> dict[str, Any] | None:
        return self._entries.get(handle)

    def invalidate(self) -> None:
        """Drop every registered handle. Called when build_tb_hierarchy is
        re-run or its downstream cache is cleared."""
        self._entries.clear()

    def __contains__(self, handle: str) -> bool:
        return handle in self._entries

    def __len__(self) -> int:
        return len(self._entries)
