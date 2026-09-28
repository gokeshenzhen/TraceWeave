"""Private Linux worker for pylibfst 0.2.1. Never imported by the MCP server.

All native allocation and decompression happens after process resource limits
are applied. Callback output is bounded and waits for a next-page request, so a
slow or stopped consumer cannot accumulate an unbounded Python event queue.
"""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import resource
import struct
import sys

MAX_DECLARATIONS = 32768
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_WIDTH = 65536
MAX_ACTIVITY = 4096
MAX_NAME_BYTES = 4096
MAX_SCOPE_DEPTH = 256
MAX_BLOCKS = 1048576
FRAME_BYTES = 1024 * 1024


def emit(value):
    value["metrics"] = {"worker_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(raw) > FRAME_BYTES:
        raise ValueError("fst_worker_frame_limit")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def request():
    raw = sys.stdin.buffer.readline(65537)
    if not raw:
        raise EOFError()
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("fst_request_limit")
    return json.loads(raw)


def identity(path):
    s = os.stat(path)
    return [os.path.realpath(path), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def preflight(path, memory):
    """Validate the block envelope before trusting native length allocations."""
    if os.path.exists(path + ".hier"):
        raise ValueError("fst_external_hierarchy_unsupported")
    size = os.stat(path).st_size
    blocks, waves, hierarchy = 0, 0, False
    with open(path, "rb") as stream:
        pos = 0
        while pos < size:
            stream.seek(pos)
            raw = stream.read(9)
            if len(raw) != 9:
                raise ValueError("fst_corrupt: incomplete block header")
            kind, length = raw[0], struct.unpack(">Q", raw[1:])[0]
            if pos == 0 and kind == 254:
                raise ValueError("fst_gzip_container_unsupported")
            if pos == 0 and kind != 0:
                raise ValueError("fst_format_mismatch")
            if length < 8 or pos + length + 1 > size or kind not in {0, 1, 2, 3, 4, 5, 6, 7, 8}:
                raise ValueError("fst_corrupt: invalid or unfinished block")
            if kind in {1, 5, 8}:
                if length < 64:
                    raise ValueError("fst_corrupt: short value block")
                begin, end, traversal_bytes = struct.unpack(">QQQ", stream.read(24))
                if end < begin:
                    raise ValueError("fst_corrupt: unordered value block")
                stream.seek(pos + 1 + length - 24)
                unpacked, packed, count = struct.unpack(">QQQ", stream.read(24))
                if packed > length or unpacked + packed + count * 16 + traversal_bytes > memory // 2:
                    raise ValueError("fst_native_block_memory_limit")
                waves += 1
            hierarchy |= kind in {4, 6, 7}
            pos += 1 + length
            blocks += 1
            if blocks > MAX_BLOCKS:
                raise ValueError("fst_block_count_limit")
    if not waves or not hierarchy:
        raise ValueError("fst_corrupt_or_incomplete: embedded hierarchy and value blocks required")
    return waves


def main():
    config = request()
    memory = config["memory_bytes"]
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
    cpu = max(1, math.ceil(config["timeout_sec"]))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    path = config["path"]
    def check():
        if identity(path) != config["identity"]:
            raise ValueError("waveform_changed")
    check()
    waves = preflight(path, memory)
    try:
        import pylibfst
        from pylibfst import lib, ffi
    except ImportError as exc:
        raise ValueError("fst_dependency_missing: install traceweave-mcp[fst] in the server interpreter") from exc
    if importlib.metadata.version("pylibfst") != "0.2.1":
        raise ValueError("fst_dependency_version_unsupported: requires pylibfst 0.2.1")
    # CFFI's loaded extension is the evidence source, not the Python facade.
    import _libfstapi
    native = Path(_libfstapi.__file__)
    digest = hashlib.sha256(native.read_bytes()).hexdigest()
    reader = lib.fstReaderOpen(path.encode())
    if reader == ffi.NULL:
        raise ValueError("fst_corrupt: native reader could not open")
    try:
        exponent = int(lib.fstReaderGetTimescale(reader))
        if not -15 <= exponent <= 0:
            raise ValueError("fst_time_precision_unsupported")
        if lib.fstReaderGetTimezero(reader):
            raise ValueError("fst_timezero_unsupported: nonzero timezero is not yet validated")
        scale = 10 ** (exponent + 15)
        begin, end = int(lib.fstReaderGetStartTime(reader)), int(lib.fstReaderGetEndTime(reader))
        if begin > end or end * scale > 2**63 - 1:
            raise ValueError("fst_time_range_unsupported")
        if lib.fstReaderGetValueChangeSectionCount(reader) != waves:
            raise ValueError("fst_corrupt_or_incomplete: value block count disagrees")
        count = int(lib.fstReaderGetNumberDumpActivityChanges(reader))
        if count > MAX_ACTIVITY:
            raise ValueError("fst_dump_activity_limit")
        activity = [(int(lib.fstReaderGetDumpActivityChangeTime(reader, i)),
                     int(lib.fstReaderGetDumpActivityChangeValue(reader, i))) for i in range(count)]
        if any(t < begin or t > end or v not in (0, 1) for t, v in activity) or activity != sorted(activity, key=lambda x: x[0]):
            raise ValueError("fst_corrupt: invalid dump activity")
        header = {"start_tick": begin, "end_tick": end, "scale_fs": scale,
                  "timescale_exponent": exponent, "activity": activity,
                  "declaration_count": int(lib.fstReaderGetVarCount(reader)),
                  "storage_count": int(lib.fstReaderGetMaxHandle(reader)),
                  "backend": {"dependency": "pylibfst", "version": "0.2.1",
                              "native_path": str(native), "native_sha256": digest}}
        check()
        emit({"kind": "ready", **header})
        index = None
        while True:
            message = request()
            check()
            if index is None:
                index = build_index(lib, ffi, reader)
            op = message["op"]
            if op == "metadata":
                emit({"kind": "metadata", "count": len(index),
                      "top_modules": sorted({row["top"] for row in index.values() if row["top"]}),
                      "sample_signals": sorted(index)[:20]})
            elif op == "declaration":
                emit({"kind": "declaration", "declaration": resolve(index, message["path"])})
            elif op == "search":
                keyword = message["keyword"].lower()
                paths = [p for p in sorted(index) if keyword in p.lower()]
                emit({"kind": "search", "total_matched": len(paths),
                      "results": [public(index[p]) for p in paths[:message["limit"]]]})
            elif op == "scope":
                scope, direct, after = message["scope"], message["direct"], message.get("after")
                rows, nbytes, visited, next_path, reason = [], 0, 0, None, None
                for p in sorted(index):
                    if after is not None and p < after:
                        continue
                    row = index[p]
                    if scope and scope not in row["ancestors"]:
                        continue
                    if len(rows) >= message["max_items"] or visited >= message["max_visited"]:
                        next_path, reason = p, "page_items" if len(rows) >= message["max_items"] else "page_scan"
                        break
                    visited += 1
                    if direct and row["scope"] != scope:
                        continue
                    item = public(row)
                    size = len(json.dumps(item, ensure_ascii=True).encode())
                    if size > message["max_bytes"] - nbytes:
                        next_path, reason = p, "page_bytes"
                        break
                    rows.append(item)
                    nbytes += size
                emit({"kind": "scope", "results": rows, "bytes_returned": nbytes,
                      "visited": visited, "next_path": next_path, "stop_reason": reason})
            elif op == "stream":
                row = resolve(index, message["path"])
                if not row["supported"]:
                    raise ValueError("fst_signal_type_unsupported")
                stream_events(pylibfst, reader, row, header, message, check)
                return
            else:
                raise ValueError("fst_request_invalid")
    finally:
        lib.fstReaderClose(reader)


def public(row):
    return {k: row[k] for k in ("path", "name", "scope", "width", "direction", "var_type", "supported", "is_alias")}


def resolve(index, path):
    if path in index:
        return index[path]
    matches = [r for p, r in index.items() if r["base"] == path]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("fst_signal_not_found" if not matches else "fst_signal_ambiguous")


def build_index(lib, ffi, reader):
    if lib.fstReaderGetVarCount(reader) > MAX_DECLARATIONS:
        raise ValueError("fst_metadata_entry_limit")
    scopes, index, nbytes = [], {}, 0
    types = {int(getattr(lib, name)): name.removeprefix("FST_VT_").lower().removeprefix("vcd_")
             for name in dir(lib) if name.startswith("FST_VT_") and name not in {"FST_VT_MIN", "FST_VT_MAX"}}
    unsupported = {0, 3, 4, 18, 19, 20, 21, 29}
    directions = {1: "input", 2: "output", 3: "inout", 4: "buffer", 5: "linkage"}
    while True:
        node = lib.fstReaderIterateHier(reader)
        if node == ffi.NULL:
            break
        if node.htyp == lib.FST_HT_SCOPE:
            if node.u.scope.name_length > MAX_NAME_BYTES or len(scopes) >= MAX_SCOPE_DEPTH:
                raise ValueError("fst_scope_limit")
            scopes.append(ffi.string(node.u.scope.name).decode("utf-8", "strict"))
        elif node.htyp == lib.FST_HT_UPSCOPE:
            if not scopes:
                raise ValueError("fst_corrupt: unbalanced scopes")
            scopes.pop()
        elif node.htyp == lib.FST_HT_VAR:
            var = node.u.var
            if var.name_length > MAX_NAME_BYTES or var.length > MAX_WIDTH:
                raise ValueError("fst_signal_width_or_name_limit")
            name = ffi.string(var.name).decode("utf-8", "strict")
            match = re.search(r"\s+(\[-?\d+(?::-?\d+)?\])$", name)
            if match and not name.startswith("\\"):
                name = name[:match.start()] + match[1]
            scope = ".".join(scopes)
            path = scope + "." + name if scope else name
            explicit = re.search(r"\[(-?\d+)(?::(-?\d+))?\]$", name)
            if name.startswith("\\") and not match:
                explicit = None
            coords = None
            base = path
            if explicit:
                left, right = int(explicit[1]), int(explicit[2] or explicit[1])
                if abs(left - right) + 1 != var.length:
                    raise ValueError("fst_declaration_range_unsupported")
                coords = {"left": left, "right": right}
                base = path[:len(path) - len(explicit[0])].rstrip()
            elif var.length == 1:
                coords = {"left": 0, "right": 0}
            ancestors = [".".join(scopes[:i]) for i in range(1, len(scopes) + 1)]
            row = {"path": path, "base": base, "name": name, "scope": scope, "ancestors": ancestors,
                   "top": scopes[0] if scopes else None, "width": int(var.length), "handle": int(var.handle),
                   "is_alias": bool(var.is_alias), "direction": directions.get(int(var.direction), "unknown"),
                   "var_type": types.get(int(var.typ), "unknown"), "declared_range": coords,
                   "supported": int(var.typ) in types and int(var.typ) not in unsupported and var.length > 0}
            if path in index:
                raise ValueError("fst_declaration_conflict")
            # Conservative accounting includes Python dictionaries and strings,
            # in addition to the separate hard native process address limit.
            nbytes += 1024 + 4 * len(json.dumps(row, ensure_ascii=True).encode())
            if nbytes > MAX_INDEX_BYTES or len(index) >= MAX_DECLARATIONS:
                raise ValueError("fst_metadata_memory_limit")
            index[path] = row
    if scopes or len(index) != lib.fstReaderGetVarCount(reader):
        raise ValueError("fst_corrupt: incomplete hierarchy")
    return index


def stream_events(api, reader, row, header, message, check):
    lib, ffi = api.lib, api.ffi
    cap, byte_cap = message["max_events"], message["max_bytes"]
    if not 1 <= cap <= 4096 or not 128 <= byte_cap <= 1048576:
        raise ValueError("fst_page_limits_invalid")
    start, end = message["start_fs"], message["end_fs"]
    rows, used, previous, initial = [], 0, None, None
    first_page, first_event, finished = True, True, False
    last_tick = -1
    lib.fstReaderClrFacProcessMaskAll(reader)
    lib.fstReaderSetFacProcessMask(reader, row["handle"])
    # Scan from the recorded beginning to retain a genuine predecessor time.
    # Starting at the requested window could invent a transition from a block
    # snapshot. Range filtering is only a block-load hint, never clipping.
    lib.fstReaderSetLimitTimeRange(reader, 0, min(header["end_tick"], end // header["scale_fs"]))

    def prefixes():
        predecessor = previous if first_page else None
        anchor = initial if first_page and previous is None else None
        return predecessor, anchor

    def page(next_tick=None, complete=False, truncated=False):
        nonlocal rows, used, first_page
        check()
        predecessor, anchor = prefixes()
        prefix_bytes = sum(len(e[1]) + 64 for e in (predecessor, anchor) if e)
        if prefix_bytes + used > byte_cap:
            next_tick = (predecessor or anchor or rows[0])[0]
            predecessor, anchor, rows, used, prefix_bytes = None, None, [], 0, 0
            truncated, complete = True, False
        emit({"kind": "page", "events": rows, "predecessor": predecessor,
              "initial_state": anchor, "next_tick": next_tick,
              "complete": complete, "truncated": truncated, "record_bytes": used + prefix_bytes})
        rows, used, first_page = [], 0, False
        if not complete and not truncated:
            if request().get("op") != "next":
                raise ValueError("fst_request_invalid")

    def callback(_, tick, handle, pointer):
        nonlocal used, previous, initial, first_event, finished, last_tick
        if finished:
            return
        try:
            if tick < last_tick:
                raise ValueError("fst_transition_order_invalid")
            last_tick = tick
            at = tick * header["scale_fs"]
            if at > end:
                page(complete=True)
                finished = True
                return
            value = bytes(ffi.buffer(pointer, row["width"])).decode("ascii").lower()
            if any(c not in "01xz" for c in value):
                raise ValueError("fst_digital_value_unsupported")
            event = [int(tick), value]
            if first_event and tick == header["start_tick"]:
                initial = event
                first_event = False
                return
            first_event = False
            if at < start:
                previous = event
                return
            size = len(value) + 64
            prefix_bytes = sum(len(e[1]) + 64 for e in prefixes() if e)
            if size > byte_cap or prefix_bytes > byte_cap:
                page(next_tick=int(tick), truncated=True)
                finished = True
                return
            if len(rows) + bool(prefix_bytes) >= cap or used + size + prefix_bytes > byte_cap:
                page(next_tick=int(tick))
            rows.append(event)
            used += size
        except BaseException as exc:
            # Exceptions may not cross the CFFI callback boundary. Publish a
            # fixed error and exit this isolated process instead of swallowing
            # it and letting the parent trust a partial stream.
            try:
                emit({"kind": "error", "reason": str(exc) if isinstance(exc, ValueError) else "fst_callback_failed"})
            finally:
                os._exit(2)

    rc = api.fstReaderIterBlocks(reader, callback)
    if rc != 1 or lib.fstReaderGetFseekFailed(reader):
        raise ValueError("fst_decode_failed")
    if not finished:
        prefix_bytes = sum(len(e[1]) + 64 for e in prefixes() if e)
        if used + prefix_bytes > byte_cap:
            page(truncated=True)
        else:
            page(complete=True)


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        pass
    except BaseException as exc:
        reason = ("file_not_found" if isinstance(exc, FileNotFoundError) else
                  "permission_denied" if isinstance(exc, PermissionError) else
                  "fst_worker_memory_limit" if isinstance(exc, MemoryError) else
                  str(exc) if isinstance(exc, ValueError) else "fst_worker_failed")
        emit({"kind": "error", "reason": reason})
        sys.exit(1)
