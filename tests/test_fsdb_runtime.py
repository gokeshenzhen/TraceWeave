"""
test_fsdb_runtime.py
覆盖：FSDB runtime 缺失时给出清晰报错，提示用户回退到 VCD。
"""

from pathlib import Path
from contextlib import contextmanager
import ctypes
from types import SimpleNamespace

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
    parser = fsdb_parser.FSDBParser("/fake/test.fsdb")
    parser._open = lambda: None

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
    parser = fsdb_parser.FSDBParser("/fake/test.fsdb")
    parser._open = lambda: None

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
    parser = fsdb_parser.FSDBParser("/fake/test.fsdb")
    parser._open = lambda: None
    monkeypatch.setattr(parser, "search_signals", lambda keyword, max_results=0: {"results": []})

    with pytest.raises(KeyError, match="Signal not found"):
        parser.get_signal_width("top_tb.missing.clk")


def test_transition_parser_detects_native_truncation_receipt():
    text = "0\t0\n10\t1\n@TRUNCATED\n"

    assert fsdb_parser._buffer_was_truncated(text) is True
    assert len(fsdb_parser._parse_trans_buf(text)) == 2
    assert fsdb_parser._buffer_was_truncated("0\t0\n10\t1\n") is False


def test_multi_signal_parser_enforces_window_and_chronological_pre_history():
    text = """\
@SIGNAL\ttop.clk\t1
#VALUE_AT_CENTER\t1
#WINDOW_TRANSITIONS
5\t0
10\t1
20\t0
25\t1
#PRE_WINDOW_TRANSITIONS
9\t0
3\t1
"""

    result = fsdb_parser._parse_multi_signal_buf(
        text, center_ps=15, window_ps=5, extra_transitions=2
    )
    signal = result["signals"]["top.clk"]

    assert [item["time_ps"] for item in signal["transitions_in_window"]] == [10, 20]
    assert [item["time_ps"] for item in signal["pre_window_transitions"]] == [3, 9]


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
    parser._stat_identity = lambda: None
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


def test_transition_parser_separates_legacy_native_predecessor_from_window():
    parser = _fake_group_parser()

    with parser.transition_group(["top.clk"]):
        result = parser.get_transitions("top.clk", 5, 10)

    assert [item["time_ps"] for item in result["transitions"]] == [10]
    assert result["predecessor"]["time_ps"] == 0
    assert result["transition_count"] == 1


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


def test_point_cleanup_failure_closes_reader_before_next_query():
    class PointLib(_FakeGroupLib):
        fail = True

        def fsdb_get_value_at_time(self, handle, path, timestamp, buffer, capacity):
            if self.fail:
                self.fail = False
                return -6
            buffer.value = b"1"
            return 0

        def fsdb_open(self, path):
            self.calls.append("reopen")
            return ctypes.c_void_p(2)

        def fsdb_get_scale_info(self, handle, buffer, size):
            buffer.value = b"1ps"
            return 1000

    parser = _fake_group_parser(PointLib())
    parser._buf = ctypes.create_string_buffer(64)
    with pytest.raises(RuntimeError, match="reader closed for recovery"):
        parser.get_value_at_time("top.clk", 5)
    assert parser._handle is None
    assert parser._lib.calls == ["close"]
    assert parser.get_value_at_time("top.clk", 5)["value"]["bin"] == "1"
    assert parser._lib.calls == ["close", "reopen"]


@pytest.mark.parametrize("operation", ["transition", "around", "begin", "end", "duration", "batch"])
def test_cleanup_failure_invalidates_reader(operation):
    lib = _FakeGroupLib()
    parser = _fake_group_parser(lib)
    parser._buf = ctypes.create_string_buffer(64)
    if operation == "transition":
        lib.fsdb_get_transitions_profiled = lambda *args: -6
        call = lambda: parser.get_transitions("top.clk")
    elif operation == "around":
        lib.fsdb_get_multi_signals_around_time = lambda *args: -6
        call = lambda: parser.get_signals_around_time(["top.clk"], 5)
    elif operation == "duration":
        lib._traceweave_has_reader_status = True
        lib.fsdb_get_end_time = lambda *args: 0
        lib.fsdb_reader_status = lambda *args: -6
        call = parser.get_header
    elif operation == "batch":
        from src.waveform_batch import FSDBBatchReader
        lib.fsdb_batch_window_transitions = lambda *args: -6
        call = lambda: FSDBBatchReader(parser).values_in_window(["top.clk"], 0, 10)
    else:
        setattr(lib, "fsdb_" + operation + "_transition_group", lambda *args: -6)
        def call():
            with parser.transition_group(["top.clk"]):
                pass
    with pytest.raises(RuntimeError, match="reader closed for recovery"):
        call()
    assert parser._handle is None
    assert not parser._transition_group_active
    assert lib.calls[-1] == "close"


def test_group_cleanup_failure_preserves_cancellation():
    from src.cancellation import OperationCancelled
    parser = _fake_group_parser()
    parser._lib.fsdb_end_transition_group = lambda *args: -6
    with pytest.raises(OperationCancelled):
        with parser.transition_group(["top.clk"]):
            raise OperationCancelled()
    assert parser._handle is None


def test_failed_group_read_closes_once_and_skips_stale_finalizer():
    parser = _fake_group_parser()
    parser._lib.fsdb_get_loaded_transitions = lambda *args: -6
    with pytest.raises(RuntimeError, match="reader closed for recovery"):
        with parser.transition_group(["top.clk"]):
            parser.get_transitions("top.clk")
    assert parser._lib.calls == [("begin", ["top.clk"]), "end", "close"]


def test_nested_group_cannot_unload_outer_group():
    parser = _fake_group_parser()
    with parser.transition_group(["top.clk"]):
        with pytest.raises(RuntimeError, match="Nested"):
            with parser.transition_group(["top.clk"]):
                pass
        assert parser.get_transitions("top.clk")["transition_count"] == 2
    assert parser._lib.calls.count("end") == 1


@pytest.mark.parametrize("metric", ["record_sweep_native_group_begin", "record_sweep_rss"])
def test_group_releases_even_if_instrumentation_fails(monkeypatch, metric):
    parser = _fake_group_parser()
    def fail(*args, **kwargs):
        raise RuntimeError("instrumentation")
    monkeypatch.setattr(operation_metrics, metric, fail)
    with pytest.raises(RuntimeError, match="instrumentation"):
        with parser.transition_group(["top.clk"]):
            pass
    assert not parser._transition_group_active
    assert parser._lib.calls[-1] == "end"


class _FakeLazyGroupLib(_FakeGroupLib):
    _traceweave_has_lazy_transition_group = True

    def fsdb_begin_lazy_transition_group_v1(self, handle, paths, count, profile_ptr):
        self.calls.append(('lazy_begin', [paths[index].decode() for index in range(count)]))
        profile_ptr._obj.signal_count = count
        return count


@pytest.mark.parametrize('lazy', [False, True])
def test_event_group_selects_capability_and_reports_actual_loads(lazy):
    lib = _FakeLazyGroupLib() if lazy else _FakeGroupLib()
    parser = _fake_group_parser(lib)
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value('_sweep_active', True)
    try:
        with parser._event_transition_group(['top.clk', 'top.clk'], 50, 60) as active:
            assert active and parser._transition_group_active
    finally:
        operation_metrics.pop(token)
    assert lib.calls == [('lazy_begin' if lazy else 'begin', ['top.clk']), 'end']
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot['sweep_native_group_count'] == 1
    assert snapshot['sweep_native_group_load_call_count'] == (0 if lazy else 1)
    assert snapshot['sweep_native_group_load_total_ms'] == (0 if lazy else 2.0)


def test_eager_sweep_group_remains_eager_with_lazy_capability():
    parser = _fake_group_parser(_FakeLazyGroupLib())
    with parser.transition_group(['top.clk']) as active:
        assert active
    assert parser._lib.calls == [('begin', ['top.clk']), 'end']


@pytest.mark.parametrize('lazy', [False, True])
def test_standalone_seek_does_not_report_an_eager_load(lazy):
    parser = _fake_group_parser(_FakeLazyGroupLib() if lazy else _FakeGroupLib())
    metrics = operation_metrics.OperationMetrics()
    token = operation_metrics.push(metrics)
    operation_metrics.set_value('_sweep_active', True)
    try:
        result = parser.get_transitions('top.clk', 0, 10)
    finally:
        operation_metrics.pop(token)
    assert result['transition_count'] == 1
    assert parser._lib.calls == [('profiled', 'top.clk')]
    snapshot = operation_metrics.snapshot(metrics)
    assert snapshot['sweep_native_profiled_read_count'] == 1
    assert snapshot.get('sweep_native_standalone_load_call_count', 0) == (0 if lazy else 1)


@pytest.mark.parametrize('code', [-2, -6])
def test_lazy_event_group_begin_failure_does_not_load_eagerly(code):
    parser = _fake_group_parser(_FakeLazyGroupLib())
    parser._lib.fsdb_begin_lazy_transition_group_v1 = lambda *args: code
    if code == -6:
        with pytest.raises(RuntimeError, match='reader closed for recovery'):
            with parser._event_transition_group(['top.clk'], 50, 60):
                pytest.fail('poisoned group must not be published')
        assert parser._handle is None
        assert parser._lib.calls == ['close']
    else:
        with parser._event_transition_group(['top.clk'], 50, 60) as active:
            assert not active
        assert parser._lib.calls == []


@pytest.mark.parametrize('existing_group', [False, True])
def test_event_readers_share_window_group_and_close_cursors_first(monkeypatch, existing_group):
    from src.waveform_batch import event_readers
    parser = _fake_group_parser(_FakeLazyGroupLib())
    seen_windows = []
    group = parser._event_transition_group

    @contextmanager
    def event_group(paths, start, end):
        seen_windows.append((start, end))
        with group(paths, start, end) as active:
            yield active

    @contextmanager
    def pages(path, start, end, **limits):
        assert parser._transition_group_active
        parser._lib.calls.append(('open_cursor', path, start, end))
        try:
            yield SimpleNamespace(mode='native_event_pages_v1')
        finally:
            parser._lib.calls.append(('close_cursor', path))

    monkeypatch.setattr(parser, '_supports_event_pages', lambda: True)
    monkeypatch.setattr(parser, '_event_transition_group', event_group)
    monkeypatch.setattr(parser, '_event_pages', pages)

    def read():
        with event_readers([(parser, 'top.clk'), (parser, 'top.data')], 50, 60) as readers:
            assert [reader.mode for reader in readers] == ['native_event_pages_v1'] * 2

    if existing_group:
        with parser.transition_group(['top.clk', 'top.data']):
            read()
            assert parser._transition_group_active
    else:
        read()
    assert seen_windows == ([] if existing_group else [(50, 60)])
    assert parser._lib.calls == [
        ('begin' if existing_group else 'lazy_begin', ['top.clk', 'top.data']),
        ('open_cursor', 'top.clk', 50, 60), ('open_cursor', 'top.data', 50, 60),
        ('close_cursor', 'top.data'), ('close_cursor', 'top.clk'), 'end',
    ]


@pytest.mark.parametrize('reason', ['old_group_abi', 'oversized'])
def test_event_group_fallback_keeps_materialized_reads_usable(monkeypatch, reason):
    lib = _FakeLazyGroupLib()
    parser = _fake_group_parser(lib)
    paths = ['top.clk', 'top.valid']
    if reason == 'old_group_abi':
        lib._traceweave_has_transition_group = False
        def legacy_read(handle, path, start, end, buf, size):
            lib.calls.append(('legacy', path.decode()))
            buf.value = b'10\t1\n'
            return 1
        lib.fsdb_get_transitions = legacy_read
    else:
        monkeypatch.setenv('TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS', '1')
    with parser._event_transition_group(paths, 5, 10) as active:
        assert not active
        assert not parser._transition_group_active
        result = parser.get_transitions('top.clk', 0, 10)
    assert result['transition_count'] == 1
    assert not parser._transition_group_active
    assert all(not (isinstance(call, tuple) and call[0] in {'begin', 'lazy_begin'}) for call in lib.calls)
