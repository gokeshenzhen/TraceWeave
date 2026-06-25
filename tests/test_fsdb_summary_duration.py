"""
test_fsdb_summary_duration.py
针对 FSDB 时长（simulation_duration_ps）推导逻辑的单元测试。

不依赖真实 FSDB / 编译好的 libfsdb_wrapper.so —— 通过 mock 掉 native 层
(fsdb_get_end_time / fsdb_get_signal_count) 与 search_signals / get_transitions，
锁定 get_summary 的 Python 侧时长选择/兜底行为。

背景：原 native 实现 fsdb_get_end_time 从“最大 idcode 的单个信号”推导整个
FSDB 的结束时间，该信号是任意挑选的，若它是 static/记账信号就会让一个有效的
FSDB 报出 0（或系统性偏小）。native 侧已改为优先调用文件级全局 API
ffrGetMaxFsdbTag64；这里覆盖的是 native 拿不到时间（返回 0）时 Python 侧仍能
诚实兜底、且 native 给出有效值时不画蛇添足的行为。
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.fsdb_parser import FSDBParser


def _make_parser(native_end_ps, sample_signals, transitions_map):
    """构造一个不触碰 native 层的 FSDBParser。

    native_end_ps      : fsdb_get_end_time 的返回值（模拟 native 全局/兜底结果）
    sample_signals     : search_signals("") 返回的信号路径列表
    transitions_map    : {signal_path: [{"time_ps": int}, ...]} 模拟每个信号的跳变
    """
    p = FSDBParser("/fake/case.fsdb")
    p._open = lambda: None          # 跳过 native open
    p._handle = object()            # 非空即可

    class _Lib:
        @staticmethod
        def fsdb_get_end_time(_handle):
            return native_end_ps

        @staticmethod
        def fsdb_get_signal_count(_handle):
            return len(sample_signals)

        @staticmethod
        def fsdb_close(_handle):
            return None

    p._lib = _Lib()
    p.search_signals = lambda kw, max_results=20: {
        "results": [{"path": s} for s in sample_signals]
    }

    calls = {"get_transitions": []}

    def _get_transitions(signal_path, a, b):
        calls["get_transitions"].append(signal_path)
        return {"transitions": transitions_map.get(signal_path, [])}

    p.get_transitions = _get_transitions
    p._calls = calls
    return p


# ── native 全局 API 给出有效时间：直接采用，不走兜底 ──────────────────
def test_native_valid_duration_is_used_directly():
    p = _make_parser(
        native_end_ps=693048788,
        sample_signals=["tb.clk", "tb.rst"],
        transitions_map={"tb.clk": [{"time_ps": 10}]},
    )
    s = p.get_summary()
    assert s["simulation_duration_ps"] == 693048788
    assert s["simulation_duration_ns"] == 693048.788
    # native 已给出有效值 → 不应再去逐信号采样跳变
    assert p._calls["get_transitions"] == []


# ── native 返回 0，但有信号在后期有跳变：兜底应恢复出非零时长 ─────────
def test_zero_native_recovers_from_late_transition():
    p = _make_parser(
        native_end_ps=0,
        sample_signals=["tb.cfg_static", "tb.some_clk"],
        transitions_map={
            "tb.cfg_static": [{"time_ps": 0}],
            "tb.some_clk":   [{"time_ps": 100}, {"time_ps": 692386623}],
        },
    )
    s = p.get_summary()
    assert s["simulation_duration_ps"] == 692386623
    assert s["simulation_duration_ns"] == 692386.623


# ── native 返回 0 且所有采样信号都 static：当前如实返回 0（已知局限）──
def test_all_static_signals_yield_zero():
    p = _make_parser(
        native_end_ps=0,
        sample_signals=["tb.cfg_a", "tb.cfg_b"],
        transitions_map={
            "tb.cfg_a": [{"time_ps": 0}],
            "tb.cfg_b": [],
        },
    )
    s = p.get_summary()
    # 文档化当前行为：无任何后期跳变可恢复时，时长为 0。
    # （native 全局 API 修复后，这种情况在真实有效 FSDB 上已基本不会出现。）
    assert s["simulation_duration_ps"] == 0


# ── 兜底取所有采样信号里的最大末次跳变 ────────────────────────────────
def test_zero_native_takes_max_across_sampled_signals():
    p = _make_parser(
        native_end_ps=0,
        sample_signals=["tb.a", "tb.b", "tb.c"],
        transitions_map={
            "tb.a": [{"time_ps": 500}],
            "tb.b": [{"time_ps": 9000}],
            "tb.c": [{"time_ps": 3000}],
        },
    )
    s = p.get_summary()
    assert s["simulation_duration_ps"] == 9000
