"""Bounded references to full source observations, captured only on fallback."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import time

from . import design_context_runtime
from .compile_input_capture import CaptureLimits


@dataclass
class _Flight:
    task: asyncio.Task | None = None
    waiters: int = 0
    waiter_tasks: set = field(default_factory=set)


class SourceFallbackRuntime:
    def __init__(self):
        self.aliases = OrderedDict()
        self.flights = {}
        self.admission = asyncio.Semaphore(1)
        self.closed = False

    def close(self):
        self.closed = True
        self.aliases.clear()
        for flight in self.flights.values():
            flight.task.cancel()

    async def resolve(self, inputs, store, budget, run_worker, require_owner):
        key = inputs.fallback_key
        if inputs.identity_complete and key in self.aliases:
            self.aliases.move_to_end(key)
            # Keep expired/stale references. The normal pin/current contract
            # reports them; neither error authorizes recapturing a new version.
            return self.aliases[key], "reused", 0, 0
        flight = self.flights.get(key)
        coalesced = flight is not None
        if flight is None:
            if self.closed:
                raise design_context_runtime.ContextUnavailable("design_context_generation_changed")
            if len(self.flights) >= 4:
                raise design_context_runtime.ContextUnavailable("source_fallback_busy")
            flight = _Flight()
            deadline = budget.deadline

            async def produce():
                async with asyncio.timeout_at(deadline):
                    async with self.admission:
                        def capture():
                            def remaining():
                                value = deadline - time.monotonic()
                                if value <= 0:
                                    raise TimeoutError
                                return value
                            inputs.require_current(timeout_sec=remaining())
                            context = design_context_runtime.prepare_design_context(
                                inputs.ordered_compile_logs[0],
                                supplementary_compile_logs=inputs.ordered_compile_logs[1:],
                                simulator=inputs.simulator, top_hint=inputs.selected_top,
                                backend_preference="source", limits=CaptureLimits(timeout_sec=remaining()))
                            inputs.require_current(timeout_sec=remaining())
                            return store.prepare_registration(context, timeout_sec=remaining())
                        ticket = await run_worker(capture)
                        await asyncio.sleep(0)
                        require_owner(store)
                        if self.closed or not flight.waiters or not any(
                                not task.done() and not task.cancelling() for task in flight.waiter_tasks):
                            raise asyncio.CancelledError
                        handle = store.publish(ticket)
                        if inputs.identity_complete:
                            self.aliases[key] = handle
                            self.aliases.move_to_end(key)
                            while len(self.aliases) > 8:
                                self.aliases.popitem(last=False)
                        return handle, dict(ticket.context.metrics)["read_bytes"]

            flight.task = asyncio.create_task(produce())
            self.flights[key] = flight
            flight.task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        flight.waiters += 1
        waiter = asyncio.current_task()
        flight.waiter_tasks.add(waiter)
        try:
            async with asyncio.timeout(budget.remaining()):
                handle, read_bytes = await asyncio.shield(flight.task)
            return handle, "reused" if coalesced else "prepared", 0 if coalesced else 1, 0 if coalesced else read_bytes
        finally:
            flight.waiters -= 1
            flight.waiter_tasks.discard(waiter)
            if not flight.waiters:
                self.flights.pop(key, None)
                if not flight.task.done():
                    flight.task.cancel()
