"""Real-reader optional count-index ABI, checked against independent raw rows."""

import ctypes as C
import pytest
from src.fsdb_parser import (
    _NativeEventPageV1 as Page,
    _NativeCountBoundaryV1 as Boundary,
)
from tests.fsdb_count_oracles import assert_native_boundary
from tests.test_fsdb_cursor_reclaim import _parser, reclaim_libraries, same_tick_fsdb  # noqa: F401

pytestmark = pytest.mark.eda_integration


@pytest.mark.parametrize(
    "start,end", [(0, 4), (1, 1), (1, 2), (2, 3), (4, 5), (0, -1), (0, 2**64 - 2)]
)
@pytest.mark.parametrize("cap", [1, 2, 3, 8, 65536])
def test_index_count_matches_old_count_and_raw_detail(
    reclaim_libraries, same_tick_fsdb, monkeypatch, start, end, cap
):
    with _parser(reclaim_libraries("default"), same_tick_fsdb, monkeypatch) as parser:
        assert parser._lib._traceweave_has_event_count_index_v1
        with parser._event_transition_group(["top.data"], start, end):
            cursors = []
            try:
                for _ in range(3):
                    cursor = C.c_uint64()
                    assert (
                        parser._lib.fsdb_event_open_v1(
                            parser._handle,
                            b"top.data",
                            start,
                            2**64 - 1 if end < 0 else end,
                            C.byref(cursor),
                        )
                        == 0
                    )
                    cursors.append(cursor)
                while True:
                    indexed, old, detail = Page(), Page(), Page()
                    boundary, buffer = Boundary(), C.create_string_buffer(65536)
                    assert (
                        parser._lib.fsdb_event_count_index_v1(
                            parser._handle,
                            cursors[0],
                            cap,
                            C.byref(indexed),
                            C.sizeof(indexed),
                            C.byref(boundary),
                            C.sizeof(boundary),
                        )
                        == 0
                    )
                    assert (
                        parser._lib.fsdb_event_count_v1(
                            parser._handle, cursors[1], cap, C.byref(old), C.sizeof(old)
                        )
                        == 0
                    )
                    assert (
                        parser._lib.fsdb_event_page_v1(
                            parser._handle,
                            cursors[2],
                            min(cap, 4096),
                            buffer,
                            len(buffer),
                            C.byref(detail),
                            C.sizeof(detail),
                        )
                        == 0
                    )

                    def fact(page):
                        return {
                            name: getattr(page, name)
                            for name, *_ in Page._fields_
                            if name != "output_bytes"
                        }

                    assert fact(indexed) == fact(old) == fact(detail)
                    ticks = [
                        int(row.split("\t")[2])
                        for row in buffer.value.decode().splitlines()
                    ]
                    assert (
                        indexed.events == len(ticks)
                        and indexed.output_bytes == old.output_bytes == 0
                    )
                    assert_native_boundary(
                        ticks,
                        parser._scale_fs,
                        boundary.boundary_ps,
                        boundary.events_before_boundary,
                        boundary.valid,
                        indexed.next_tick if indexed.has_next else None,
                    )
                    if indexed.complete:
                        break
                eof, boundary = Page(), Boundary()
                assert (
                    parser._lib.fsdb_event_count_index_v1(
                        parser._handle,
                        cursors[0],
                        1,
                        C.byref(eof),
                        C.sizeof(eof),
                        C.byref(boundary),
                        C.sizeof(boundary),
                    )
                    == 0
                )
                assert eof.complete and eof.events == 0 and not boundary.valid
            finally:
                for cursor in cursors:
                    assert parser._lib.fsdb_event_close_v1(parser._handle, cursor) == 0


def test_invalid_index_arguments_preserve_cursor(
    reclaim_libraries, same_tick_fsdb, monkeypatch
):
    with _parser(reclaim_libraries("default"), same_tick_fsdb, monkeypatch) as parser:
        with parser._event_transition_group(["top.data"], 0, 4):
            with parser._event_pages("top.data", 0, 4, max_events=1) as reader:
                page, boundary = Page(), Boundary()
                for cap, size in [
                    (0, C.sizeof(boundary)),
                    (65537, C.sizeof(boundary)),
                    (1, 0),
                ]:
                    assert (
                        parser._lib.fsdb_event_count_index_v1(
                            parser._handle,
                            reader.cursor,
                            cap,
                            C.byref(page),
                            C.sizeof(page),
                            C.byref(boundary),
                            size,
                        )
                        == -1
                    )
                reader.index_counts = True
                first = reader.count_page(1)
                assert first.events == 1 and first.boundary_ps == 1
