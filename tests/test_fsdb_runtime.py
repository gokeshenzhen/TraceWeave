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

    with pytest.raises(RuntimeError, match="FSDB 解析不可用"):
        fsdb_parser._load_wrapper()
