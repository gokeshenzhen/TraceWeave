"""Enum metadata must not contaminate successful digital value reads."""
import pytest

pylibfst = pytest.importorskip("pylibfst", reason="install the optional [fst] extra")
from src.fst_parser import FSTParser


def test_many_enum_tables_keep_value_traversal_independent(tmp_path):
    lib, ffi = pylibfst.lib, pylibfst.ffi
    path = tmp_path / "enums.fst"
    writer = lib.fstWriterCreate(str(path).encode(), 1)
    assert writer != ffi.NULL
    try:
        lib.fstWriterSetTimescale(writer, -12)
        for table in range(40):
            count = 215 if table == 39 else 2
            labels = [ffi.new("char[]", f"CSR_{i}".encode()) for i in range(count)]
            values = [ffi.new("char[]", f"{i:012b}".encode()) for i in range(count)]
            lib.fstWriterCreateEnumTable(writer, f"enum_{table}".encode(), count, 12,
                                        ffi.new("char*[]", labels), ffi.new("char*[]", values))
        lib.fstWriterSetScope(writer, lib.FST_ST_VCD_MODULE, b"top", ffi.NULL)
        handle = lib.fstWriterCreateVar(writer, lib.FST_VT_VCD_WIRE,
                                       lib.FST_VD_OUTPUT, 1, b"a", 0)
        lib.fstWriterSetUpscope(writer)
        for tick, value in [(0, b"0"), (10, b"1"), (20, b"0")]:
            lib.fstWriterEmitTimeChange(writer, tick)
            lib.fstWriterEmitValueChange(writer, handle, value)
        lib.fstWriterEmitTimeChange(writer, 30)
    finally:
        lib.fstWriterClose(writer)

    parser = FSTParser(str(path))
    assert parser.get_summary()["total_signals"] == 1
    result = parser.get_transitions("top.a", 11, 25)
    assert result["predecessor"]["time_ps"] == 10
    assert [(row["time_ps"], row["value"]["bin"]) for row in result["transitions"]] == [(20, "0")]
    assert result["fst_reading"]["coverage_status"] == "complete"
    assert parser.get_value_at_time("top.a", 10)["value"]["bin"] == "1"


def test_attribute_limit_precedes_native_metadata_processing(tmp_path):
    from src.fst_hierarchy import records
    # Attribute records do not become signals, but must fit ProcessHier's
    # buffer before any native call. Scope/variable limits are independent.
    safe = bytes([252, 0, 7]) + b"e" * 65536 + b"\0\1"
    assert list(records(safe)) == []
    with pytest.raises(ValueError, match="fst_metadata_attribute_limit"):
        list(records(bytes([252, 0, 7]) + b"e" * 65537 + b"\0\1"))


def test_duplicate_hierarchy_is_rejected_before_native_open(tmp_path):
    import struct
    from fst_fixture import write_fst
    from src.fst_runtime import FstError
    path = write_fst(tmp_path / "duplicate.fst", [(0, "a", "0"), (10, "a", "1")])
    raw = path.read_bytes()
    pos = 0
    while pos < len(raw):
        size = struct.unpack(">Q", raw[pos + 1:pos + 9])[0] + 1
        if raw[pos] in (4, 6, 7):
            path.write_bytes(raw + raw[pos:pos + size])
            break
        pos += size
    with pytest.raises(FstError, match="ambiguous embedded hierarchy"):
        FSTParser(str(path)).get_summary()
