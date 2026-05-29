"""Cursor store for named time anchors (auto-debug v2, decision 5).

A *cursor* is a named time anchor in picoseconds, scoped to the current
TraceWeave process. Tools that locate a meaningful instant (first
divergence, stall start, error event, ...) register a cursor and return
its name; downstream tools can reference ``@<name>`` instead of copying
ps-precision integers across calls.

Lifetime mirrors ``HandleStore`` (see ``src/hierarchy_handles.py``):
in-process only, no persistence, server restart drops everything. There
is **no "active cursor"** — references must be explicit by name. xwave's
``cursor use`` REPL convenience is intentionally omitted: an AI agent is
better served by explicit names than by hidden state.

This module is the storage primitive. TimeSpec arithmetic
(``@name ± cycle(clk)``) lives in the expression grammar and resolves a
TimeSpec to a ps integer with a CursorRef as one input.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any


CURSOR_AUTO_PREFIX_DEFAULT = "cur"
_AUTO_SHA_LEN = 8
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


@dataclass(frozen=True)
class CursorRef:
    """Immutable handle for a named time anchor.

    ``metadata`` is a free-form dict that callers may use to record where
    a cursor came from (tool name, source signals, observed values).
    Downstream tools can read it to enrich UI / LLM context, but the
    field carries no semantic meaning to the store itself.
    """

    name: str
    time_ps: int
    note: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "time_ps": self.time_ps,
            "note": self.note,
            "metadata": dict(self.metadata),
        }


class CursorStore:
    """In-process registry of named time anchors.

    Thread-safe to match the rest of the server which dispatches tool
    calls from asyncio handlers. Names are case-sensitive and must match
    ``^[A-Za-z_][A-Za-z0-9_\\-]*$``.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CursorRef] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def set(
        self,
        name: str,
        time_ps: int,
        *,
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = True,
    ) -> CursorRef:
        _validate_name(name)
        if time_ps < 0:
            raise ValueError(f"cursor time_ps must be non-negative: {time_ps}")
        ref = CursorRef(
            name=name,
            time_ps=int(time_ps),
            note=note,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            if not overwrite and name in self._entries:
                raise ValueError(f"cursor already exists: {name!r}")
            self._entries[name] = ref
        return ref

    def auto_set(
        self,
        time_ps: int,
        *,
        prefix: str = CURSOR_AUTO_PREFIX_DEFAULT,
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
        seed: str | None = None,
    ) -> CursorRef:
        """Create a cursor with a deterministic, collision-free name.

        The base name is ``<prefix>_<sha8>`` where the digest folds in
        ``seed`` (or ``note`` if no seed) so two calls about the same
        underlying event reuse the same name. On collision with an
        existing entry holding a different time, a numeric suffix is
        appended.
        """
        _validate_name(prefix)
        material = f"{prefix}|{seed if seed is not None else (note or '')}".encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:_AUTO_SHA_LEN]
        base = f"{prefix}_{digest}"

        with self._lock:
            candidate = base
            suffix = 1
            while candidate in self._entries and self._entries[candidate].time_ps != int(time_ps):
                candidate = f"{base}_{suffix}"
                suffix += 1
            ref = CursorRef(
                name=candidate,
                time_ps=int(time_ps),
                note=note,
                metadata=dict(metadata or {}),
            )
            self._entries[candidate] = ref
        return ref

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def get(self, name: str) -> CursorRef | None:
        with self._lock:
            return self._entries.get(name)

    def resolve_time(self, name: str) -> int:
        ref = self.get(name)
        if ref is None:
            raise KeyError(f"unknown cursor: {name!r}")
        return ref.time_ps

    def list(self) -> list[CursorRef]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda r: (r.time_ps, r.name))

    # ------------------------------------------------------------------
    # delete / clear
    # ------------------------------------------------------------------

    def delete(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------------
    # dunder
    # ------------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        with self._lock:
            return name in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("cursor name must be a non-empty string")
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"cursor name must match {_NAME_PATTERN.pattern!r}, got {name!r}"
        )
