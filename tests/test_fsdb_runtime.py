"""
test_fsdb_runtime.py
覆盖：FSDB runtime 缺失时给出清晰报错，提示用户回退到 VCD。
"""

from pathlib import Path
import ctypes

import pytest

from src import fsdb_parser, operation_metrics


def test_load_wrapper_fails_cleanly_without_fsdb_runtime(monkeypatch):
    monkeypatch.setattr(
        fsdb_parser,
        "get_fsdb_runtime_info",
        lambda: {
            "enabled": False,
            "source": None,
            "lib_dir": None,
            "missing_libs": ["libnsys.so", "libnffr.so"],
            "message": "FSDB runtime unavailable: provide VERDI_HOME or local runtime",
        },
    )
    monkeypatch.setattr(fsdb_parser.os.path, "exists", lambda path: True if path == str(Path(fsdb_parser._WRAPPER_SO).resolve()) else True)

    with pytest.raises(RuntimeError, match="FSDB parsing unavailable"):
        fsdb_parser._load_wrapper()


def test_get_signal_width_prefers_exact_path_match(monkeypatch):
    parser = fsdb_parser.FSDBParser.__new__(fsdb_parser.FSDBParser)
    parser._handle = None
    parser._lib = None

    calls: list[str] = []

    def fake_search(keyword: str, max_results: int = 0):
        calls.append(keyword)
        return {
            "results": [
                {"path": "top_tb.other.clk", "width": 8},
                {"path": "top_tb.dut.clk", "width": 1},
            ]
        }

    monkeypatch.setattr(parser, "search_signals", fake_search)

    assert parser.get_signal_width("top_tb.dut.clk") == 1
    assert calls == ["top_tb.dut.clk"]


def test_get_signal_width_uses_suffix_fallback_when_exact_search_misses(monkeypatch):
    parser = fsdb_parser.FSDBParser.__new__(fsdb_parser.FSDBParser)
    parser._handle = None
    parser._lib = None

    calls: list[str] = []
    responses = {
        "top_tb.dut.clk": {
            "results": [
                {"path": "top_tb.other.clk", "width": 8},
            ]
        },
        "clk": {
            "results": [
                {"path": "top_tb.iface.clk", "width": 2},
                {"path": "top_tb.dut.clk", "width": 1},
            ]
        },
    }

    def fake_search(keyword: str, max_results: int = 0):
        calls.append(keyword)
        return responses[keyword]

    monkeypatch.setattr(parser, "search_signals", fake_search)

    assert parser.get_signal_width("top_tb.dut.clk") == 1
    assert calls == ["top_tb.dut.clk", "clk"]


def test_get_signal_width_raises_keyerror_when_signal_missing(monkeypatch):
    parser = fsdb_parser.FSDBParser.__new__(fsdb_parser.FSDBParser)
    parser._handle = None
    parser._lib = None
    monkeypatch.setattr(parser, "search_signals", lambda keyword, max_results=0: {"results": []})

    with pytest.raises(KeyError, match="Signal not found"):
        parser.get_signal_width("top_tb.missing.clk")


def test_transition_parser_detects_native_truncation_receipt():
    text = "0\t0\n10\t1\n@TRUNCATED\n"

    assert fsdb_parser._buffer_was_truncated(text) is True
    assert len(fsdb_parser._parse_trans_buf(text)) == 2
    assert fsdb_parser._buffer_was_truncated("0\t0\n10\t1\n") is False


class _FakeGroupLib:
    _traceweave_has_transition_group = True

    def __init__(self):
        self.calls: list[object] = []

    def fsdb_begin_transition_group(self, handle, paths, count, profile_ptr):
        decoded = [paths[index].decode() for index in range(count)]
        self.calls.append(("begin", decoded))
        profile = profile_ptr._obj
        profile.signal_count = count
        profile.load_ns = 2_000_000
        return count

    def fsdb_get_loaded_transitions(
        self, handle, path, start, end, buf, size, profile_ptr
    ):
        decoded = path.decode()
        self.calls.append(("loaded", decoded))
        buf.value = b"0\t0\n10\t1\n"
        profile = profile_ptr._obj
        profile.transition_count = 2
        profile.output_bytes = len(buf.value)
        profile.traverse_format_ns = 3_000_000
        return 2

    def fsdb_get_transitions_profiled(
        self, handle, path, start, end, buf, size, profile_ptr
    ):
        self.calls.append(("profiled", path.decode()))
        buf.value = b"0\t0\n"
        return 1

    def fsdb_end_transition_group(self, handle, profile_ptr):
        self.calls.append("end")
        profile_ptr._obj.unload_ns = 1_000_000
        return 0

    def fsdb_close(self, handle):
        self.calls.append("close")


def _fake_group_parser(lib=None):
    parser = fsdb_parser.FSDBParser("/fake/test.fsdb")
    parser._lib = lib or _FakeGroupLib()
    parser._handle = ctypes.c_void_p(1)
    parser._scale_fs = 1000
    parser._scale_unit = "1ps"
    return parser


def test_transition_group_uses_loaded_reads_and_records_profile():
    parser = _fake_group_parser()
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value("_sweep_active", True)
    try:
        with parser.transition_group(["top.clk", "top.valid"]) as active:
            assert active is True
            result = parser.get_transitions("top.clk", 0, 10)
    finally:
        operation_metrics.pop(token)

    assert result["transition_count"] == 2
    assert parser._lib.calls == [
        ("begin", ["top.clk", "top.valid"]),
        ("loaded", "top.clk"),
        "end",
    ]
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_native_group_count"] == 1
    assert snapshot["sweep_native_group_signal_max"] == 2
    assert snapshot["sweep_native_profiled_read_count"] == 1
    assert snapshot["sweep_native_load_total_ms"] == 2.0
    assert snapshot["sweep_native_traverse_format_total_ms"] == 3.0
    assert snapshot["sweep_native_unload_total_ms"] == 1.0


def test_transition_group_unloads_when_body_raises():
    parser = _fake_group_parser()

    with pytest.raises(RuntimeError, match="stop"):
        with parser.transition_group(["top.clk"]):
            raise RuntimeError("stop")

    assert parser._transition_group_active is False
    assert parser._lib.calls[-1] == "end"


def test_transition_group_old_wrapper_falls_back_without_native_calls():
    class OldLib:
        _traceweave_has_transition_group = False

        def fsdb_close(self, handle):
            pass

    parser = _fake_group_parser(OldLib())
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value("_sweep_active", True)
    try:
        with parser.transition_group(["top.clk"]) as active:
            assert active is False
    finally:
        operation_metrics.pop(token)

    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_native_group_fallback_count"] == 1
    assert snapshot["sweep_native_group_unsupported_count"] == 1


def test_transition_group_limit_falls_back_before_begin(monkeypatch):
    parser = _fake_group_parser()
    monkeypatch.setenv("TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS", "1")

    with parser.transition_group(["top.clk", "top.valid"]) as active:
        assert active is False

    assert parser._lib.calls == []


def test_transition_group_begin_error_falls_back_to_profiled_read():
    class BeginErrorLib(_FakeGroupLib):
        def fsdb_begin_transition_group(self, handle, paths, count, profile_ptr):
            self.calls.append("begin_error")
            return -2

    parser = _fake_group_parser(BeginErrorLib())
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value("_sweep_active", True)
    try:
        with parser.transition_group(["top.clk"]) as active:
            assert active is False
            result = parser.get_transitions("top.clk")
    finally:
        operation_metrics.pop(token)

    assert result["transition_count"] == 1
    assert parser._lib.calls == ["begin_error", ("profiled", "top.clk")]
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot["sweep_native_group_begin_error_count"] == 1
    assert snapshot["sweep_native_group_fallback_count"] == 1
