"""Bounded immutable result cache and cancellation-safe exact single flight."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import json
import threading

from . import cancellation
from .design_identity import DesignIdentity, _digest

STRUCTURAL_RULE_VERSION = "1"


@dataclass
class _Flight:
    event: threading.Event
    task: asyncio.Task | None = None
    waiters: int = 0


class StructuralScanRuntime:
    """Event-loop-owned storage; workers never mutate session state.

    Serialized complete results have a strict byte bound. Each caller receives
    a fresh copy, before schema/output-budget processing. One cancelled caller
    cannot cancel shared work; the final waiter arms its independent token.
    """
    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=8):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self._flights: dict[str, _Flight] = {}

    async def run(self, identity: DesignIdentity, options: dict, build):
        key = _digest({"design": identity.digest, "rules": STRUCTURAL_RULE_VERSION, "options": options})
        reusable = identity.complete and await asyncio.to_thread(identity.current)
        if reusable and key in self._cache:
            data = self._cache[key]
            self._cache.move_to_end(key)
            return json.loads(data), "hit_memory"
        # Incomplete identities never coalesce or enter cross-request storage.
        flight_key = key if reusable else object()
        flight = self._flights.get(flight_key)
        disposition = "coalesced" if flight else "miss" if reusable else "bypass_incomplete_identity"
        if flight is None:
            flight = _Flight(threading.Event())
            self._flights[flight_key] = flight

            async def execute():
                token = cancellation.push_cancel_event(flight.event)
                try:
                    result = await build()
                    cancellation.check_cancelled()
                    current = reusable and await asyncio.to_thread(identity.current)
                    if reusable and not current:
                        result["coverage_status"] = "degraded"
                        result.setdefault("coverage_warnings", []).append("Sources changed during scanning; rebuild the compile context before interpreting this result.")
                    data = json.dumps(result, separators=(",", ":")).encode()
                    if current and not flight.event.is_set() and len(data) <= self.max_bytes and self.max_entries > 0:
                        old = self._cache.pop(key, None)
                        self._bytes -= len(old) if old is not None else 0
                        self._cache[key] = data
                        self._bytes += len(data)
                        while self._bytes > self.max_bytes or len(self._cache) > self.max_entries:
                            self._bytes -= len(self._cache.popitem(last=False)[1])
                    return data
                finally:
                    cancellation.pop_cancel_event(token)

            flight.task = asyncio.create_task(execute())
            # Retrieve orphaned failures after a final-waiter cancellation.
            flight.task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        flight.waiters += 1
        try:
            data = await asyncio.shield(flight.task)
            return json.loads(data), disposition
        finally:
            flight.waiters -= 1
            if not flight.waiters:
                self._flights.pop(flight_key, None)
                if not flight.task.done():
                    flight.event.set()
                    flight.task.cancel()

    def metrics(self):
        return {"result_cache_bytes": self._bytes, "result_cache_entries": len(self._cache)}
