import asyncio
import threading
import time

import pytest

from src import cancellation
from src.compile_source_index import CompileSourceIndex
from src.compile_source_runtime import (
    CompileSourceIndexRuntime,
    compile_source_index_key,
)


def test_key_covers_snapshot_simulator_and_ordered_sources(tmp_path):
    first = tmp_path / "first.sv"
    second = tmp_path / "second.sv"
    base = {
        "simulator": "vcs",
        "files": {"user": [{"path": str(first)}, {"path": str(second)}]},
    }

    key, paths = compile_source_index_key(
        compile_snapshot_sha256="a" * 64,
        compile_result=base,
    )
    same_key, same_paths = compile_source_index_key(
        compile_snapshot_sha256="a" * 64,
        compile_result=base,
    )
    reversed_key, _ = compile_source_index_key(
        compile_snapshot_sha256="a" * 64,
        compile_result={
            **base,
            "files": {"user": list(reversed(base["files"]["user"]))},
        },
    )
    simulator_key, _ = compile_source_index_key(
        compile_snapshot_sha256="a" * 64,
        compile_result={**base, "simulator": "xcelium"},
    )
    snapshot_key, _ = compile_source_index_key(
        compile_snapshot_sha256="b" * 64,
        compile_result=base,
    )

    assert key == same_key
    assert paths == same_paths
    assert key not in {reversed_key, simulator_key, snapshot_key}


@pytest.mark.anyio
async def test_concurrent_exact_acquires_share_one_build_and_release_text(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    runtime = CompileSourceIndexRuntime()
    started = threading.Event()
    unblock = threading.Event()
    real_preload = CompileSourceIndex.preload
    calls = 0

    def delayed_preload(self, paths):
        nonlocal calls
        calls += 1
        started.set()
        assert unblock.wait(timeout=5)
        return real_preload(self, paths)

    monkeypatch.setattr(CompileSourceIndex, "preload", delayed_preload)
    kwargs = {
        "key": "exact-key",
        "paths": [str(source)],
        "max_bytes": 4096,
        "max_files": 4,
    }
    first_task = asyncio.create_task(runtime.acquire(**kwargs))
    assert await asyncio.to_thread(started.wait, 5)
    second_task = asyncio.create_task(
        runtime.acquire(**kwargs, create_if_missing=False)
    )
    for _ in range(100):
        if runtime.metrics_snapshot()[
            "compile_source_runtime_coalesced_waiter_count"
        ]:
            break
        await asyncio.sleep(0)
    unblock.set()

    first, second = await asyncio.gather(first_task, second_task)
    assert calls == 1
    assert first.index is second.index
    assert {first.disposition, second.disposition} == {
        "miss_build",
        "coalesced",
    }
    assert first.index.metrics_snapshot()[
        "compile_source_index_entry_count"
    ] == 1

    await first.release()
    assert second.index.metrics_snapshot()[
        "compile_source_index_entry_count"
    ] == 1
    await second.release()
    assert second.index.metrics_snapshot()[
        "compile_source_index_entry_count"
    ] == 0
    metrics = runtime.metrics_snapshot()
    assert metrics["compile_source_runtime_build_count"] == 1
    assert metrics["compile_source_runtime_inflight_count"] == 0
    assert metrics["compile_source_runtime_active_session_count"] == 0


@pytest.mark.anyio
async def test_one_cancelled_waiter_preserves_shared_build(tmp_path, monkeypatch):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    runtime = CompileSourceIndexRuntime()
    started = threading.Event()
    unblock = threading.Event()
    real_preload = CompileSourceIndex.preload

    def delayed_preload(self, paths):
        started.set()
        assert unblock.wait(timeout=5)
        return real_preload(self, paths)

    monkeypatch.setattr(CompileSourceIndex, "preload", delayed_preload)
    kwargs = {
        "key": "shared-key",
        "paths": [str(source)],
        "max_bytes": 4096,
        "max_files": 4,
    }
    cancelled_task = asyncio.create_task(runtime.acquire(**kwargs))
    assert await asyncio.to_thread(started.wait, 5)
    survivor_task = asyncio.create_task(runtime.acquire(**kwargs))
    for _ in range(100):
        if runtime.metrics_snapshot()[
            "compile_source_runtime_coalesced_waiter_count"
        ]:
            break
        await asyncio.sleep(0)

    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    unblock.set()
    survivor = await survivor_task

    assert survivor.index.read_text(str(source)) == "module top; endmodule\n"
    assert runtime.metrics_snapshot()[
        "compile_source_runtime_cancelled_waiter_count"
    ] == 1
    await survivor.release()
    assert runtime.metrics_snapshot()[
        "compile_source_runtime_active_session_count"
    ] == 0


@pytest.mark.anyio
async def test_final_waiter_cancellation_stops_orphaned_preload(monkeypatch):
    runtime = CompileSourceIndexRuntime()
    started = threading.Event()
    observed = threading.Event()

    def cancellable_preload(self, _paths):
        started.set()
        try:
            while True:
                cancellation.check_cancelled()
                time.sleep(0.005)
        except cancellation.OperationCancelled:
            observed.set()
            raise

    monkeypatch.setattr(CompileSourceIndex, "preload", cancellable_preload)
    task = asyncio.create_task(
        runtime.acquire(
            key="orphan-key",
            paths=["/does/not/matter.sv"],
            max_bytes=4096,
            max_files=4,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(observed.wait, 5)
    for _ in range(100):
        metrics = runtime.metrics_snapshot()
        if not metrics["compile_source_runtime_inflight_count"]:
            break
        await asyncio.sleep(0.01)
    assert metrics["compile_source_runtime_inflight_count"] == 0
    assert metrics["compile_source_runtime_active_session_count"] == 0


@pytest.mark.anyio
async def test_different_limits_do_not_share_active_session(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    runtime = CompileSourceIndexRuntime()

    first = await runtime.acquire(
        key="same-content",
        paths=[str(source)],
        max_bytes=4096,
        max_files=4,
    )
    second = await runtime.acquire(
        key="same-content",
        paths=[str(source)],
        max_bytes=8192,
        max_files=4,
    )

    assert first.index is not second.index
    assert runtime.metrics_snapshot()["compile_source_runtime_build_count"] == 2
    await first.release()
    await second.release()


@pytest.mark.anyio
async def test_zero_holder_session_is_not_a_source_text_handoff(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    runtime = CompileSourceIndexRuntime()
    kwargs = {
        "key": "no-handoff",
        "paths": [str(source)],
        "max_bytes": 4096,
        "max_files": 4,
    }

    first = await runtime.acquire(**kwargs)
    await first.release()
    second = await runtime.acquire(**kwargs)

    assert second.disposition == "miss_build"
    assert runtime.metrics_snapshot()["compile_source_runtime_build_count"] == 2
    await second.release()


@pytest.mark.anyio
async def test_reuse_only_acquire_never_starts_a_full_preload(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    runtime = CompileSourceIndexRuntime()
    kwargs = {
        "key": "reuse-only",
        "paths": [str(source)],
        "max_bytes": 4096,
        "max_files": 4,
    }

    assert await runtime.acquire(**kwargs, create_if_missing=False) is None
    assert runtime.metrics_snapshot()["compile_source_runtime_build_count"] == 0

    owner = await runtime.acquire(**kwargs)
    assert owner is not None
    reuse = await runtime.acquire(**kwargs, create_if_missing=False)

    assert reuse is not None
    assert reuse.index is owner.index
    assert reuse.disposition == "hit_active_session"
    await owner.release()
    await reuse.release()


@pytest.mark.anyio
async def test_unexpected_build_failure_clears_flight_for_retry(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    runtime = CompileSourceIndexRuntime()
    real_preload = CompileSourceIndex.preload

    def broken_preload(self, paths):
        raise RuntimeError("injected implementation failure")

    monkeypatch.setattr(CompileSourceIndex, "preload", broken_preload)
    kwargs = {
        "key": "retry-key",
        "paths": [str(source)],
        "max_bytes": 4096,
        "max_files": 4,
    }
    with pytest.raises(RuntimeError, match="injected implementation failure"):
        await runtime.acquire(**kwargs)

    assert runtime.metrics_snapshot()["compile_source_runtime_inflight_count"] == 0
    monkeypatch.setattr(CompileSourceIndex, "preload", real_preload)
    lease = await runtime.acquire(**kwargs)

    assert lease.index.read_text(str(source)) == "module top; endmodule\n"
    assert runtime.metrics_snapshot()["compile_source_runtime_build_count"] == 2
    await lease.release()
