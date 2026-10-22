import asyncio
import threading
import time

import pytest

import server
from src import design_context_runtime as contexts
from src.cancellation import check_cancelled
from src.native_query import RequestBudget, read_native_inputs
from src.source_fallback_runtime import SourceFallbackRuntime
from tests.native_first_helpers import project


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    _, log, _ = project(tmp_path, candidate=False)
    inputs = read_native_inputs({"compile_log": str(log)})
    store, runtime = contexts.DesignContextStore(), SourceFallbackRuntime()
    calls, entered, release, stopped = [], threading.Event(), threading.Event(), threading.Event()
    original = contexts.prepare_design_context
    def prepare(*a, **kw):
        calls.append(1)
        entered.set()
        try:
            while not release.wait(.002):
                check_cancelled()
            check_cancelled()
            return original(*a, **kw)
        finally:
            stopped.set()
    monkeypatch.setattr(contexts, "prepare_design_context", prepare)
    async def resolve(timeout=2, selected=inputs):
        return await runtime.resolve(selected, store, RequestBudget(timeout),
            server._run_in_cancellable_thread, lambda s: None)
    yield inputs, store, runtime, calls, entered, release, stopped, resolve
    release.set()
    runtime.close()


@pytest.mark.anyio
async def test_two_waiters_share_one_capture_and_one_publication(setup_runtime):
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    tasks = [asyncio.create_task(resolve()) for _ in range(2)]
    await eventually(lambda: entered.is_set() and next(iter(runtime.flights.values())).waiters == 2)
    release.set()
    first, second = await asyncio.gather(*tasks)
    assert first[0] == second[0]
    assert {first[1], second[1]} == {"prepared", "reused"}
    assert first[2] + second[2] == 1
    assert len(calls) == len(store.summaries()) == len(runtime.aliases) == 1
    assert not runtime.flights and stopped.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("short_first", [False, True])
async def test_waiters_cannot_change_producer_deadline(setup_runtime, short_first):
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    producer = asyncio.create_task(resolve(.1 if short_first else 2))
    await eventually(entered.is_set)
    waiter = asyncio.create_task(resolve(2 if short_first else .03))
    await eventually(lambda: next(iter(runtime.flights.values())).waiters == 2)
    if short_first:
        results = await asyncio.gather(producer, waiter, return_exceptions=True)
        assert all(isinstance(r, TimeoutError) for r in results)
        assert not store.summaries() and not runtime.aliases
    else:
        with pytest.raises(TimeoutError):
            await waiter
        release.set()
        assert (await producer)[1] == "prepared"
        assert len(store.summaries()) == 1
    await eventually(stopped.is_set)
    assert not runtime.flights


@pytest.mark.anyio
async def test_one_cancelled_waiter_preserves_other_work(setup_runtime):
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    first = asyncio.create_task(resolve())
    await eventually(entered.is_set)
    second = asyncio.create_task(resolve())
    await eventually(lambda: next(iter(runtime.flights.values())).waiters == 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not stopped.is_set()
    release.set()
    result = await second
    assert result[1] == "reused" and len(calls) == 1
    assert len(store.summaries()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["capture", "admission", "publication", "reset"])
async def test_final_waiter_cancellation_or_reset_never_publishes(setup_runtime, monkeypatch, stage):
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    gate, leave = threading.Event(), threading.Event()
    if stage == "admission":
        await runtime.admission.acquire()
    if stage == "publication":
        original = store.prepare_registration
        def registration(*a, **kw):
            ticket = original(*a, **kw)
            gate.set()
            try:
                while not leave.wait(.002):
                    check_cancelled()
                check_cancelled()
                return ticket
            finally:
                stopped.set()
        monkeypatch.setattr(store, "prepare_registration", registration)
        release.set()
    task = asyncio.create_task(resolve())
    await eventually(lambda: bool(runtime.flights))
    if stage == "publication":
        await eventually(gate.is_set)
    elif stage != "admission":
        await eventually(entered.is_set)
    if stage == "reset":
        runtime.close()
    else:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if stage == "admission":
        runtime.admission.release()
        assert not calls
    else:
        await eventually(stopped.is_set)
    leave.set()
    assert not runtime.flights and not runtime.aliases and not store.summaries()


@pytest.mark.anyio
async def test_busy_store_failure_preserves_unrelated_entries_and_cleans_flight(setup_runtime):
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    store.max_entries = 1
    release.set()
    first = await resolve()
    runtime.aliases.clear()
    with store.pin(first[0]):
        with pytest.raises(contexts.ContextUnavailable, match="context_capacity"):
            await resolve()
        assert len(store.summaries()) == 1
    assert not runtime.flights and not runtime.aliases


@pytest.mark.anyio
async def test_four_live_flights_and_one_capture_admission(setup_runtime):
    from dataclasses import replace
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    tasks = [asyncio.create_task(resolve(selected=replace(inputs,
        compile_metadata_identity_sha256=str(i) * 64))) for i in range(4)]
    await eventually(lambda: len(runtime.flights) == 4 and entered.is_set())
    assert len(calls) == 1
    with pytest.raises(contexts.ContextUnavailable, match="source_fallback_busy"):
        await resolve(selected=replace(inputs, compile_metadata_identity_sha256="f" * 64))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await eventually(stopped.is_set)
    assert not runtime.flights and not store.summaries()


@pytest.mark.anyio
async def test_incomplete_identity_has_only_live_sharing(setup_runtime):
    from dataclasses import replace
    inputs, store, runtime, calls, entered, release, stopped, resolve = setup_runtime
    inputs = replace(inputs, identity_complete=False)
    release.set()
    first, second = await asyncio.gather(resolve(selected=inputs), resolve(selected=inputs))
    assert first[0] == second[0] and not runtime.aliases
    third = await resolve(selected=inputs)
    assert third[0] != first[0] and len(calls) == 2
