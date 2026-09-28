"""Test-only FST writer. Event tables and expected values belong to each test."""
from pathlib import Path


def write_fst(path, rows, *, declarations=None, scale=-12, timezero=0,
              start=0, end=30, activity=(), flush=()):
    from pylibfst import lib, ffi
    path = Path(path)
    declarations = declarations or [("a", 1, "wire", "input", None)]
    writer = lib.fstWriterCreate(str(path).encode(), 1)
    if writer == ffi.NULL:
        raise RuntimeError("fixture writer could not open")
    handles = {}
    try:
        lib.fstWriterSetTimescale(writer, scale)
        lib.fstWriterSetTimezero(writer, timezero)
        lib.fstWriterSetVersion(writer, b"TraceWeave independent event-table fixture")
        lib.fstWriterSetScope(writer, lib.FST_ST_VCD_MODULE, b"top", ffi.NULL)
        for name, width, kind, direction, alias in declarations:
            vartype = getattr(lib, "FST_VT_" + ("SV_" if kind in {"bit", "logic"} else "VCD_") + kind.upper())
            handles[name] = lib.fstWriterCreateVar(writer, vartype,
                getattr(lib, "FST_VD_" + direction.upper()), width,
                name.encode(), handles[alias] if alias else 0)
        lib.fstWriterSetUpscope(writer)
        lib.fstWriterEmitTimeChange(writer, start)
        activity = dict(activity)
        by_time = {}
        for tick, name, value in rows:
            by_time.setdefault(tick, []).append((name, value))
        for tick in sorted(set(by_time) | set(activity) | set(flush)):
            lib.fstWriterEmitTimeChange(writer, tick)
            if tick in activity:
                lib.fstWriterEmitDumpActive(writer, activity[tick])
            for name, value in by_time.get(tick, []):
                lib.fstWriterEmitValueChange(writer, handles[name], value.encode())
            if tick in flush:
                lib.fstWriterFlushContext(writer)
        lib.fstWriterEmitTimeChange(writer, end)
    finally:
        lib.fstWriterClose(writer)
    return path
