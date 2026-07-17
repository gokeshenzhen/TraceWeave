# test_server_concurrency.py
# 事件循环卸载(卸线程 + 每波形锁 + 协作取消)的回归测试。
#
# 缺陷形态:波形工具体曾同步跑在 async 派发协程里 —— 一个重调用饿死整个
# 事件循环(后到的轻调用排队到重调用结束),且客户端弃单后服务端继续算完。
# 这些测试钉住修复后的行为:
#   - 重波形调用在飞时,轻调用不排队
#   - 同一波形路径(以及所有 FSDB)上的调用保持串行,parser 不被并发访问
#   - 轻量 FSDB 查询可协作抢占仍持有全局锁的后台 sweep,但 FFR 调用不重叠
#   - 请求任务被取消后,worker 在下一个协作检查点停止计算
#   - 还在排队等锁时被取消的调用,从不触碰 parser

import threading
import time

import anyio
import pytest

import server
from src import cancellation
from src.cancellation import OperationCancelled


class FakeParser:
    """Wave-parser stand-in whose get_summary behavior is injectable."""

    def __init__(self, summary_fn):
        self._summary_fn = summary_fn

    def get_summary(self):
        return self._summary_fn()


def _summary_dict(path="/fake/wave.vcd"):
    return {
        "file": path,
        "format": "vcd",
        "simulation_duration_ps": 1000,
        "simulation_duration_ns": 1.0,
        "total_signals": 1,
    }


def _wait_event(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)


class TestWaveLocks:
    def test_fsdb_paths_share_one_global_lock(self):
        locks = server._wave_locks_for(["/a/x.fsdb", "/b/y.fsdb"])
        assert locks == [server._FSDB_WAVE_LOCK]

    def test_vcd_paths_get_per_path_locks_deduped(self):
        locks = server._wave_locks_for(["/a/x.vcd", "/a/x.vcd", "/b/y.vcd"])
        assert len(locks) == 2
        assert locks[0] is not locks[1]
        again = server._wave_locks_for(["/a/x.vcd"])
        assert again[0] in locks

    def test_mixed_paths_stable_order(self):
        first = server._wave_locks_for(["/b/y.vcd", "/a/x.fsdb"])
        second = server._wave_locks_for(["/a/x.fsdb", "/b/y.vcd"])
        assert first == second

    def test_interactive_priority_preempts_background_fsdb_holder(self):
        holder_event = threading.Event()
        waiter_event = threading.Event()
        server._set_active_fsdb(holder_event, server._WAVE_PRIORITY_BACKGROUND)
        try:
            server._preempt_lower_priority_fsdb(
                waiter_event, server._WAVE_PRIORITY_INTERACTIVE
            )
            assert holder_event.is_set()
        finally:
            server._clear_active_fsdb(holder_event)

    def test_equal_priority_does_not_preempt_fsdb_holder(self):
        holder_event = threading.Event()
        waiter_event = threading.Event()
        server._set_active_fsdb(holder_event, server._WAVE_PRIORITY_INTERACTIVE)
        try:
            server._preempt_lower_priority_fsdb(
                waiter_event, server._WAVE_PRIORITY_INTERACTIVE
            )
            assert not holder_event.is_set()
        finally:
            server._clear_active_fsdb(holder_event)


class TestCheckCancelled:
    def test_noop_without_armed_event(self):
        cancellation.check_cancelled()  # must not raise

    def test_raises_when_event_set(self):
        event = threading.Event()
        token = cancellation.push_cancel_event(event)
        try:
            cancellation.check_cancelled()  # armed but not set: no raise
            event.set()
            with pytest.raises(OperationCancelled):
                cancellation.check_cancelled()
        finally:
            cancellation.pop_cancel_event(token)


@pytest.mark.anyio
class TestEventLoopNotBlocked:
    async def test_sweep_dispatch_uses_background_priority(self, monkeypatch):
        class DispatchReached(Exception):
            pass

        async def capture_run(wave_paths, fn, *, priority):
            assert wave_paths == "/fake/wave.fsdb"
            assert priority == server._WAVE_PRIORITY_BACKGROUND
            raise DispatchReached

        monkeypatch.setattr(server, "_check_prerequisites", lambda name, args: None)
        monkeypatch.setattr(server, "_run_in_wave_thread", capture_run)

        with pytest.raises(DispatchReached):
            await server._dispatch(
                "sweep_handshakes", {"wave_path": "/fake/wave.fsdb"}
            )

    async def test_light_call_completes_while_heavy_wave_call_in_flight(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def slow_summary():
            started.set()
            release.wait(timeout=10)
            return _summary_dict()

        monkeypatch.setattr(server, "_get_parser", lambda p: FakeParser(slow_summary))

        light_elapsed = None
        try:
            async with anyio.create_task_group() as tg:
                async def heavy():
                    await server._dispatch(
                        "get_waveform_summary", {"wave_path": "/fake/heavy.vcd"}
                    )

                tg.start_soon(heavy)
                assert await anyio.to_thread.run_sync(_wait_event, started, 5)

                start = time.perf_counter()
                result = await server._dispatch("cursor_list", {})
                light_elapsed = time.perf_counter() - start
                assert result is not None
        finally:
            release.set()

        # Pre-fix, the light call could not even START until the heavy call
        # returned. Post-fix it runs while the heavy call sits in its worker.
        assert light_elapsed < 0.5

    async def test_same_path_calls_serialize_on_the_wave_lock(self, monkeypatch):
        active = 0
        max_active = 0
        guard = threading.Lock()

        def summary():
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with guard:
                active -= 1
            return _summary_dict()

        monkeypatch.setattr(server, "_get_parser", lambda p: FakeParser(summary))

        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(
                    server._dispatch,
                    "get_waveform_summary",
                    {"wave_path": "/fake/serialize.vcd"},
                )
        assert max_active == 1

    async def test_distinct_fsdb_paths_serialize_on_the_global_lock(self, monkeypatch):
        active = 0
        max_active = 0
        guard = threading.Lock()

        def summary():
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with guard:
                active -= 1
            return dict(_summary_dict(), format="fsdb")

        monkeypatch.setattr(server, "_get_parser", lambda p: FakeParser(summary))

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                server._dispatch, "get_waveform_summary", {"wave_path": "/fake/a.fsdb"}
            )
            tg.start_soon(
                server._dispatch, "get_waveform_summary", {"wave_path": "/fake/b.fsdb"}
            )
        assert max_active == 1


@pytest.mark.anyio
class TestCooperativeCancellation:
    async def test_cancelled_call_stops_at_next_checkpoint(self, monkeypatch):
        started = threading.Event()
        observed_cancel = threading.Event()
        ran_to_completion = threading.Event()

        def looping_summary():
            started.set()
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    cancellation.check_cancelled()
                    time.sleep(0.01)
            except OperationCancelled:
                observed_cancel.set()
                raise
            ran_to_completion.set()
            return _summary_dict()

        monkeypatch.setattr(server, "_get_parser", lambda p: FakeParser(looping_summary))

        async with anyio.create_task_group() as tg:
            async def call():
                await server._dispatch(
                    "get_waveform_summary", {"wave_path": "/fake/cancel.vcd"}
                )

            tg.start_soon(call)
            assert await anyio.to_thread.run_sync(_wait_event, started, 5)
            tg.cancel_scope.cancel()

        # The abandoned worker must observe the armed cancel event promptly
        # instead of computing for the full 5s deadline.
        assert await anyio.to_thread.run_sync(_wait_event, observed_cancel, 2)
        assert not ran_to_completion.is_set()

    async def test_cancelled_while_queued_never_touches_parser(self, monkeypatch):
        holder_started = threading.Event()
        holder_release = threading.Event()
        parser_calls: list[str] = []

        def holder_summary():
            holder_started.set()
            holder_release.wait(timeout=10)
            return _summary_dict()

        def fake_get_parser(path):
            parser_calls.append(path)
            return FakeParser(holder_summary)

        monkeypatch.setattr(server, "_get_parser", fake_get_parser)

        try:
            async with anyio.create_task_group() as tg:
                async def holder():
                    await server._dispatch(
                        "get_waveform_summary", {"wave_path": "/fake/queued.vcd"}
                    )

                tg.start_soon(holder)
                assert await anyio.to_thread.run_sync(_wait_event, holder_started, 5)

                # Second call on the same path queues on the wave lock; cancel
                # it while it is still waiting.
                with anyio.move_on_after(0.5):
                    await server._dispatch(
                        "get_waveform_summary", {"wave_path": "/fake/queued.vcd"}
                    )
                holder_release.set()
        finally:
            holder_release.set()

        # Only the lock holder ever reached the parser; the cancelled queued
        # call gave up at the lock-poll checkpoint.
        assert parser_calls == ["/fake/queued.vcd"]
