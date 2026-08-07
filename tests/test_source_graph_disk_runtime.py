from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
import threading
import time

import pytest

from src.source_graph_contract import compute_source_graph_artifact_key
from src.source_graph_disk_cache import (
    DiskCacheCancelled,
    DiskLookupResult,
    DiskValidationOutcome,
    SOURCE_GRAPH_DISK_CACHE_IR,
    SourceGraphDiskCache,
)
from src.source_graph_runtime import (
    CacheDisposition,
    CacheTier,
    PrepareStatus,
    SourceGraphRuntime,
    WorkerBuildResult,
)
from tests.test_source_graph_runtime import (
    ControlledWorker,
    ImmediateWorker,
    _request,
    _scope,
)


class ForbiddenWorker:
    def __init__(self) -> None:
        self.count = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.count += 1
        raise AssertionError("verified disk hit must skip the frontend worker")


class CountingDiskCache(SourceGraphDiskCache):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lookup_calls = 0
        self.publish_calls = 0

    def lookup(self, *args, **kwargs):
        self.lookup_calls += 1
        return super().lookup(*args, **kwargs)

    def publish(self, *args, **kwargs):
        self.publish_calls += 1
        return super().publish(*args, **kwargs)


class BlockingDiskCache:
    max_entries = 1
    max_bytes = 1024

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.publish_calls = 0

    def lookup(self, identity, *, cancelled):
        del identity
        self.entered.set()
        while not cancelled():
            time.sleep(0.002)
        raise DiskCacheCancelled("cancelled blocking lookup")

    def publish(self, *args, **kwargs):
        del args, kwargs
        self.publish_calls += 1
        raise AssertionError("cancelled lookup must not publish")


class BlockingPublishDiskCache:
    max_entries = 1
    max_bytes = 1024

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.completed = False

    def lookup(self, identity, *, cancelled):
        del identity, cancelled
        return DiskLookupResult(DiskValidationOutcome.NOT_FOUND)

    def publish(self, *args, cancelled, **kwargs):
        del args, kwargs
        self.entered.set()
        while not cancelled():
            time.sleep(0.002)
        raise DiskCacheCancelled("cancelled blocking publish")


class FailedPublishDiskCache:
    max_entries = 1
    max_bytes = 1024

    def lookup(self, identity, *, cancelled):
        del identity, cancelled
        return DiskLookupResult(DiskValidationOutcome.NOT_FOUND)

    def publish(self, *args, **kwargs):
        del args, kwargs
        raise OSError("private disk failure must not become a public fallback")


def _process_prepare(root: str, forbidden: bool, queue) -> None:
    async def run() -> None:
        request = _request(label="fresh_process")
        worker = ForbiddenWorker() if forbidden else ImmediateWorker()
        runtime = SourceGraphRuntime(
            worker,
            disk_cache=SourceGraphDiskCache(Path(root)),
        )
        outcome = await runtime.prepare(request)
        queue.put(
            {
                "status": outcome.status.value,
                "tier": outcome.metrics.cache_tier.value,
                "actual_build_count": outcome.metrics.actual_build_count,
                "frontend_launch_count": outcome.metrics.frontend_launch_count,
                "worker_count": worker.count,
                "ir_fingerprint": outcome.entry.ir_fingerprint_sha256,
                "coverage": outcome.entry.coverage_status.value,
            }
        )

    asyncio.run(run())


def test_two_fresh_processes_cold_publish_then_exact_disk_hit_without_rebuild(
    tmp_path,
):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    root = str(tmp_path / "cache")

    first = context.Process(target=_process_prepare, args=(root, False, queue))
    first.start()
    cold = queue.get(timeout=20)
    first.join(timeout=20)
    assert first.exitcode == 0

    second = context.Process(target=_process_prepare, args=(root, True, queue))
    second.start()
    disk = queue.get(timeout=20)
    second.join(timeout=20)
    assert second.exitcode == 0
    queue.close()

    assert cold == {
        "status": "ready",
        "tier": "build",
        "actual_build_count": 1,
        "frontend_launch_count": 1,
        "worker_count": 1,
        "ir_fingerprint": cold["ir_fingerprint"],
        "coverage": "complete",
    }
    assert disk == {
        "status": "ready",
        "tier": "disk",
        "actual_build_count": 0,
        "frontend_launch_count": 0,
        "worker_count": 0,
        "ir_fingerprint": cold["ir_fingerprint"],
        "coverage": cold["coverage"],
    }


@pytest.mark.anyio
async def test_memory_precedes_disk_and_disk_hit_populates_independent_memory_entry(
    tmp_path,
):
    request = _request(label="tier_order")
    first_worker = ImmediateWorker()
    first_store = CountingDiskCache(tmp_path / "cache")
    first_runtime = SourceGraphRuntime(first_worker, disk_cache=first_store)
    cold = await first_runtime.prepare(request)

    second_worker = ForbiddenWorker()
    second_store = CountingDiskCache(tmp_path / "cache")
    second_runtime = SourceGraphRuntime(second_worker, disk_cache=second_store)
    disk = await second_runtime.prepare(request)
    lookup_calls_after_disk = second_store.lookup_calls
    memory = await second_runtime.prepare(request)

    assert cold.metrics.cache_tier is CacheTier.BUILD
    assert cold.metrics.disk_validation_outcome == "not_found"
    assert cold.metrics.actual_build_count == 1
    assert cold.metrics.disk_miss_count == 1
    assert cold.metrics.disk_bytes_written > 0
    assert disk.metrics.cache_tier is CacheTier.DISK
    assert disk.metrics.cache_disposition is CacheDisposition.MISS
    assert disk.metrics.disk_validation_outcome == "hit"
    assert disk.metrics.actual_build_count == 0
    assert disk.metrics.frontend_launch_count == 0
    assert disk.metrics.disk_hit_count == 1
    assert disk.metrics.disk_build_skip_count == 1
    assert disk.metrics.disk_bytes_read > 0
    assert disk.entry.ir_fingerprint_sha256 == cold.entry.ir_fingerprint_sha256
    assert disk.entry.artifact_scope_receipt == cold.entry.artifact_scope_receipt
    assert disk.entry.query_engine is not cold.entry.query_engine
    assert second_worker.count == 0
    assert memory.metrics.cache_tier is CacheTier.MEMORY
    assert memory.metrics.disk_validation_outcome == "not_checked"
    assert memory.metrics.disk_lookup_wall_ms == 0
    assert second_store.lookup_calls == lookup_calls_after_disk
    assert memory.entry is disk.entry


@pytest.mark.anyio
async def test_disabled_disk_cache_preserves_memory_only_behavior_without_io(tmp_path):
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(label="disabled_disk")

    cold = await runtime.prepare(request)
    memory = await runtime.prepare(request)

    assert cold.metrics.cache_tier is CacheTier.BUILD
    assert cold.metrics.disk_validation_outcome == "disabled"
    assert cold.metrics.disk_lookup_wall_ms == 0
    assert cold.metrics.disk_publish_wall_ms == 0
    assert memory.metrics.cache_tier is CacheTier.MEMORY
    assert worker.count == 1
    assert not (tmp_path / "cache").exists()


@pytest.mark.anyio
async def test_corrupt_entry_is_a_cold_rebuild_then_repaired_for_next_process(tmp_path):
    request = _request(label="corrupt_runtime")
    root = tmp_path / "cache"
    first = SourceGraphRuntime(ImmediateWorker(), disk_cache=SourceGraphDiskCache(root))
    cold = await first.prepare(request)
    digest = compute_source_graph_artifact_key(request.artifact_identity).digest
    ir_path = SourceGraphDiskCache(root).entry_path(digest) / SOURCE_GRAPH_DISK_CACHE_IR
    ir_path.write_bytes(ir_path.read_bytes()[:100])

    rebuild_worker = ImmediateWorker()
    rebuild = await SourceGraphRuntime(
        rebuild_worker, disk_cache=SourceGraphDiskCache(root)
    ).prepare(request)
    final_worker = ForbiddenWorker()
    final = await SourceGraphRuntime(
        final_worker, disk_cache=SourceGraphDiskCache(root)
    ).prepare(request)

    assert rebuild.status is PrepareStatus.READY
    assert rebuild.metrics.cache_tier is CacheTier.BUILD
    assert rebuild.metrics.disk_validation_outcome == "ir_size_mismatch"
    assert rebuild.metrics.disk_corrupt_count == 1
    assert rebuild.metrics.actual_build_count == 1
    assert rebuild_worker.count == 1
    assert final.metrics.cache_tier is CacheTier.DISK
    assert final.metrics.actual_build_count == 0
    assert final.entry.ir_fingerprint_sha256 == cold.entry.ir_fingerprint_sha256
    assert final_worker.count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    (
        PrepareStatus.BUILD_FAILED,
        PrepareStatus.WORKER_CRASH,
        PrepareStatus.TIMED_OUT,
    ),
)
async def test_failed_crashed_and_timed_out_builds_never_publish(tmp_path, status):
    class FailedWorker:
        async def run(self, request, *, timeout_seconds, cancel_event):
            del request, timeout_seconds, cancel_event
            return WorkerBuildResult.failed(
                status,
                code="fixture_failure",
                stage="worker_process",
            )

    root = tmp_path / status.value
    request = _request(label=status.value)
    outcome = await SourceGraphRuntime(
        FailedWorker(), disk_cache=SourceGraphDiskCache(root)
    ).prepare(request)

    assert outcome.status is status
    assert not SourceGraphDiskCache(root).namespace_root.exists()


@pytest.mark.anyio
async def test_cancelled_sole_disk_lookup_stays_off_loop_and_never_builds_or_publishes():
    disk = BlockingDiskCache()
    worker = ForbiddenWorker()
    runtime = SourceGraphRuntime(worker, disk_cache=disk)
    task = asyncio.create_task(runtime.prepare(_request(label="cancel_disk_lookup")))
    assert await asyncio.to_thread(disk.entered.wait, 2)

    light_completed = False

    async def light_call():
        nonlocal light_completed
        await asyncio.sleep(0)
        light_completed = True

    await asyncio.wait_for(light_call(), timeout=0.2)
    task.cancel()
    outcome = await task
    await runtime.wait_idle()

    assert light_completed is True
    assert outcome.status is PrepareStatus.CANCELLED
    assert worker.count == 0
    assert disk.publish_calls == 0
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_cancelled_sole_waiter_during_disk_publish_never_publishes_or_caches():
    disk = BlockingPublishDiskCache()
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker, disk_cache=disk)
    task = asyncio.create_task(runtime.prepare(_request(label="cancel_disk_publish")))
    assert await asyncio.to_thread(disk.entered.wait, 2)

    task.cancel()
    outcome = await task
    await runtime.wait_idle()

    assert outcome.status is PrepareStatus.CANCELLED
    assert worker.count == 1
    assert disk.completed is False
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_disk_publish_failure_keeps_valid_build_in_memory_without_fallback():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker, disk_cache=FailedPublishDiskCache())
    request = _request(label="failed_disk_publish")

    cold = await runtime.prepare(request)
    memory = await runtime.prepare(request)

    assert cold.status is PrepareStatus.READY
    assert cold.metrics.cache_tier is CacheTier.BUILD
    assert cold.metrics.disk_validation_outcome == "not_found"
    assert cold.metrics.actual_build_count == 1
    assert memory.status is PrepareStatus.READY
    assert memory.metrics.cache_tier is CacheTier.MEMORY
    assert worker.count == 1
    assert runtime.stats_snapshot()["disk_publish_failure_count"] == 1


@pytest.mark.anyio
async def test_one_cancelled_waiter_does_not_break_other_waiter_or_disk_publish(
    tmp_path,
):
    worker = ControlledWorker()
    store = SourceGraphDiskCache(tmp_path / "cache")
    runtime = SourceGraphRuntime(worker, disk_cache=store)
    request = _request(label="shared_waiter")
    first_cancel = asyncio.Event()
    first = asyncio.create_task(runtime.prepare(request, cancel_event=first_cancel))
    second = asyncio.create_task(runtime.prepare(request))
    await worker.started.wait()
    first_cancel.set()
    cancelled = await first
    worker.release.set()
    ready = await second

    assert cancelled.status is PrepareStatus.CANCELLED
    assert ready.status is PrepareStatus.READY
    assert worker.count == 1
    assert store.lookup(request.artifact_identity).hit


@pytest.mark.anyio
async def test_smaller_disk_artifact_does_not_serve_sibling_scope(tmp_path):
    root = tmp_path / "cache"
    small_scope = _scope(
        signal_path="sg_top.u_bridge.q",
        ancestors=("sg_top", "sg_top.u_bridge"),
        cone_paths=("sg_top", "sg_top.u_bridge"),
        boundary_paths=("sg_top", "sg_top.u_bridge"),
    )
    sibling_scope = _scope(
        signal_path="sg_top.u_producer.q",
        ancestors=("sg_top", "sg_top.u_producer"),
        cone_paths=("sg_top", "sg_top.u_producer"),
        boundary_paths=("sg_top", "sg_top.u_producer"),
    )
    small = _request(label="same_design", scope=small_scope)
    sibling = _request(label="same_design", scope=sibling_scope)
    await SourceGraphRuntime(
        ImmediateWorker(), disk_cache=SourceGraphDiskCache(root)
    ).prepare(small)
    sibling_worker = ImmediateWorker()

    outcome = await SourceGraphRuntime(
        sibling_worker, disk_cache=SourceGraphDiskCache(root)
    ).prepare(sibling)

    assert outcome.metrics.cache_tier is CacheTier.BUILD
    assert outcome.metrics.disk_validation_outcome == "not_found"
    assert outcome.metrics.actual_build_count == 1
    assert sibling_worker.count == 1


@pytest.mark.anyio
async def test_disk_eviction_does_not_invalidate_loaded_memory_holder(tmp_path):
    root = tmp_path / "cache"
    first_request = _request(label="memory_holder_a")
    second_request = _request(label="memory_holder_b")
    first_store = CountingDiskCache(root, max_entries=1)
    first_worker = ImmediateWorker()
    first_runtime = SourceGraphRuntime(first_worker, disk_cache=first_store)
    await first_runtime.prepare(first_request)
    first_lookup_calls = first_store.lookup_calls

    second = await SourceGraphRuntime(
        ImmediateWorker(),
        disk_cache=SourceGraphDiskCache(root, max_entries=1),
    ).prepare(second_request)
    held = await first_runtime.prepare(first_request)

    assert second.metrics.disk_eviction_count == 1
    assert held.metrics.cache_tier is CacheTier.MEMORY
    assert first_store.lookup_calls == first_lookup_calls
    assert first_worker.count == 1
