"""One cumulative, cancellation-aware budget across all graph attempts."""

import sys
import time

import anyio

from config import (
    DIVERGENCE_MAX_TRANSITIONS,
    DIVERGENCE_MAX_WAVE_CACHE_BYTES,
    DIVERGENCE_MAX_RESTARTS,
)
from .cancellation import check_cancelled


class BudgetExceeded(Exception):
    def __init__(self, limit):
        self.limit = limit
        super().__init__(limit)


class TraceRestart(Exception):
    def __init__(self, side, reason):
        self.side, self.reason = side, reason
        super().__init__(reason)


class Budget:
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.deadline = self.started + limits["timeout_sec"]
        self.nodes = self.queries = self.transitions = self.restarts = 0
        self.cache_bytes = self.cache_peak = 0
        self.restart_reasons = []

    def check(self):
        check_cancelled()
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("timeout_sec")

    @property
    def remaining(self):
        self.check()
        return self.deadline - time.monotonic()

    async def run(self, fn):
        self.check()
        try:
            with anyio.fail_after(self.remaining):
                result = await fn()
        except TimeoutError as exc:
            raise BudgetExceeded("timeout_sec") from exc
        self.check()
        return result

    def consume(self, stream):
        self.check()
        self.transitions += len(stream.transitions)
        if self.transitions > DIVERGENCE_MAX_TRANSITIONS:
            raise BudgetExceeded("max_transitions")
        # Conservative retained Python-object estimate, not just packed payload.
        size = sys.getsizeof(stream.transitions)
        for index, event in enumerate(stream.transitions):
            if index % 1024 == 0:
                self.check()
            value = event.get("value")
            size += (
                sys.getsizeof(event)
                + sys.getsizeof(value)
                + sys.getsizeof(event.get("time_ps"))
            )
            if isinstance(value, dict):
                size += sum(sys.getsizeof(v) for v in value.values())
        self.cache_bytes += size
        self.cache_peak = max(self.cache_peak, self.cache_bytes)
        if self.cache_bytes > DIVERGENCE_MAX_WAVE_CACHE_BYTES:
            raise BudgetExceeded("max_wave_cache_bytes")

    def node(self):
        self.check()
        if self.nodes >= self.limits["max_nodes"]:
            raise BudgetExceeded("max_nodes")
        self.nodes += 1

    def restart(self, reason):
        self.check()
        if self.restarts >= DIVERGENCE_MAX_RESTARTS:
            raise BudgetExceeded("max_restarts")
        self.restarts += 1
        self.restart_reasons.append(reason)

    def metrics(self):
        return dict(
            elapsed_ms=round((time.monotonic() - self.started) * 1000, 3),
            admitted_node_count=self.nodes,
            backend_query_count=self.queries,
            transition_count=self.transitions,
            wave_cache_peak_bytes=self.cache_peak,
            restart_count=self.restarts,
            effective_timeout_sec=self.limits["timeout_sec"],
        )
