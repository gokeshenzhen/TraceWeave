"""Cooperative cancellation for worker-thread tool bodies.

Waveform tool bodies run in worker threads (``server._run_in_wave_thread``)
so a heavy scan cannot starve the asyncio event loop. Python cannot preempt
a running thread, so cancellation is cooperative: the server arms a
``threading.Event`` per call and sets it when the client abandons the
request; long scan loops call :func:`check_cancelled` at checkpoints and
bail out with :class:`OperationCancelled`.

The event travels via a ``ContextVar`` set inside the worker thread, so
library code (``cycle_query``, ``handshake_sweep``, ``verify_condition``)
needs no signature changes. Outside a worker thread the checkpoint is a
no-op, which keeps every existing synchronous test path unaffected.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token

# Checkpoint stride for inner scan loops: cheap enough to keep cancellation
# latency sub-second while adding no measurable per-iteration cost.
CANCEL_CHECK_STRIDE = 4096

_cancel_event: ContextVar[threading.Event | None] = ContextVar(
    "traceweave_cancel_event", default=None
)


class OperationCancelled(Exception):
    """The in-flight tool call was abandoned by the client; stop computing."""


def push_cancel_event(event: threading.Event) -> Token:
    return _cancel_event.set(event)


def pop_cancel_event(token: Token) -> None:
    _cancel_event.reset(token)


def check_cancelled() -> None:
    """Checkpoint: raise :class:`OperationCancelled` if this call was abandoned.

    One ContextVar read plus one Event read when armed; a no-op when the
    caller is not running under ``_run_in_wave_thread``.
    """
    event = _cancel_event.get()
    if event is not None and event.is_set():
        raise OperationCancelled("tool call cancelled by client")
