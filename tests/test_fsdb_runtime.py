"""
test_fsdb_runtime.py
覆盖：FSDB runtime 缺失时给出清晰报错，提示用户回退到 VCD。
"""

from pathlib import Path

import pytest

from src import fsdb_parser


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
