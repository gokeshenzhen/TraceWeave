"""Small independent public count/cache oracle for a chosen normal wrapper."""

import hashlib
import os
from pathlib import Path
import pytest
from src import fsdb_parser


@pytest.mark.eda_integration
def test_normal_wrapper_cross_scale_count_cache_and_windows(monkeypatch, tmp_path):
    selected = os.environ.get("TW_COUNT_INDEX_WRAPPER")
    if not selected:
        pytest.skip("set TW_COUNT_INDEX_WRAPPER to a normally built wrapper")
    library = Path(selected).resolve()
    monkeypatch.setattr(fsdb_parser, "_WRAPPER_SO", str(library))
    fixtures = Path(__file__).parent / "fixtures"
    cases = [
        (
            "scale_100fs",
            [(0, 0), (100000, 0xAAAA0000), (100100, 0xBBBB0000), (100101, 0xCCCC0000)],
            [(0, 110000), (0, 110000), (100050, 110000), (100050, 100101)],
        ),
        (
            "scale_1ns",
            [(0, 0), (100000, 0xAAAA0000), (101000, 0xBBBB0000)],
            [(0, 200000), (0, 200000), (99999, 100999)],
        ),
    ]
    receipts = []
    for name, raw, windows in cases:
        parser = fsdb_parser.FSDBParser(str(fixtures / (name + ".fsdb")))
        try:
            for start, end in windows:
                value = parser.get_transitions_capped(
                    name + "_tb.addr[31:0]", start, end, max_transitions=1
                )
                inside = [(t, v) for t, v in raw if start <= t <= end]
                before = [(t, v) for t, v in raw if t < start]
                assert value["transition_count"] == len(inside)
                assert not value["transition_count_is_lower_bound"]
                assert [
                    (r["time_ps"], int(r["value"]["bin"], 2))
                    for r in value["transitions"]
                ] == inside[:1]
                pred = value["predecessor"]
                assert (
                    None
                    if pred is None
                    else (pred["time_ps"], int(pred["value"]["bin"], 2))
                ) == (before[-1] if before else None)
                receipts.append(value)
        finally:
            parser.close()
        assert not parser._transition_indexes
        assert parser._transition_index_bytes == 0
        assert not parser._transition_count_cache
    maps = Path("/proc/self/maps").read_text()
    assert str(library) in maps
    import json

    evidence = dict(
        responses=receipts,
        module=fsdb_parser.__file__,
        library=str(library),
        sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
        maps=maps,
        after_close_empty=True,
    )
    output = os.environ.get("TW_COUNT_INDEX_SMOKE_RECEIPT")
    (Path(output) if output else tmp_path / "receipt.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
