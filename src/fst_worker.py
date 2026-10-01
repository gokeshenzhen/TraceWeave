"""Private Linux worker for pylibfst 0.2.1. Never imported by the MCP server.

All native allocation and decompression happens after process resource limits
are applied. Callback output is bounded and waits for a next-page request, so a
slow or stopped consumer cannot accumulate an unbounded Python event queue.
"""
from bisect import bisect_left
from collections import namedtuple

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
import time

MAX_DECLARATIONS = 32768
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_WIDTH = 65536
MAX_ACTIVITY = 4096
MAX_NAME_BYTES = 4096
MAX_SCOPE_DEPTH = 256
MAX_BLOCKS = 1048576
FRAME_BYTES = 1024 * 1024
METRICS = {}

# Only the worker owns these records. Scope strings and ancestor references
# are shared by all declarations in that parsed scope; public dictionaries
# (including range coordinates) are materialized only at the IPC boundary.
Scope = namedtuple("Scope", "path ancestors top")
Declaration = namedtuple("Declaration", (
    "path base name scope width handle is_alias direction var_type declared_range supported"))


def declaration(row):
    result = row._asdict()
    result.update(scope=row.scope.path, ancestors=row.scope.ancestors, top=row.scope.top,
                  declared_range=(dict(zip(("left", "right"), row.declared_range))
                                  if row.declared_range is not None else None))
    return result


def rss(field='VmRSS'):
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith(field + ':'):
            return int(line.split()[1])
    return None


def emit(value):
    value['metrics'] = {**METRICS, 'worker_peak_rss_kib': rss('VmHWM'), 'worker_rss_kib': rss()}
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
    opened = time.perf_counter()
    reader = lib.fstReaderOpen(path.encode())
    METRICS['native_open_ms'] = (time.perf_counter() - opened) * 1000
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
                started = time.perf_counter()
                METRICS['index_rss_before_kib'] = rss()
                index = build_index(lib, ffi, reader)
                ordered_paths = sorted(index)
                accounted = METRICS['index_accounted_bytes'] + sys.getsizeof(ordered_paths)
                if accounted > MAX_INDEX_BYTES:
                    raise ValueError('fst_metadata_memory_limit')
                METRICS.update(index_accounted_bytes=accounted,
                               index_sorted_path_bytes=sys.getsizeof(ordered_paths))
                METRICS.update(index_build_ms=(time.perf_counter() - started) * 1000,
                               index_rss_after_kib=rss())
            op = message["op"]
            if op == "metadata":
                emit({"kind": "metadata", "count": len(index),
                      "top_modules": sorted({row.scope.top for row in index.values() if row.scope.top}),
                      "sample_signals": ordered_paths[:20]})
            elif op == "declaration":
                emit({"kind": "declaration", "declaration": declaration(resolve(index, message["path"]))})
            elif op == "search":
                keyword = message["keyword"].lower()
                paths = [p for p in ordered_paths if keyword in p.lower()]
                emit({"kind": "search", "total_matched": len(paths),
                      "results": [public(index[p]) for p in paths[:message["limit"]]]})
            elif op == "scope":
                emit(scope_page(index, ordered_paths, message))
            elif op == "stream":
                row = resolve(index, message["path"])
                if not row.supported:
                    raise ValueError("fst_signal_type_unsupported")
                stream_events(pylibfst, reader, row, header, message, check, path)
                return
            elif op == "batch":
                spool_events(pylibfst, reader, index, header, message, check, path)
                return
            else:
                raise ValueError("fst_request_invalid")
    finally:
        lib.fstReaderClose(reader)


def public(row):
    return {**{k: getattr(row, k) for k in ("path", "name", "width", "direction", "var_type", "supported", "is_alias")},
            "scope": row.scope.path}


def scope_page(index, ordered_paths, message):
    scope, direct, after = message["scope"], message["direct"], message.get("after")
    # A real descendant must sort within [scope + '.', scope + '/'). This
    # narrows candidates only: escaped identifiers may contain dots, so parsed
    # ancestry still decides membership, exactly as in the original scan.
    lo = bisect_left(ordered_paths, scope + ".") if scope else 0
    hi = bisect_left(ordered_paths, scope + "/") if scope else len(ordered_paths)
    if after is not None:
        lo = bisect_left(ordered_paths, after, lo, hi)  # inclusive next_path
    rows, nbytes, visited, next_path, reason = [], 0, 0, None, None
    for i in range(lo, hi):
        p = ordered_paths[i]
        row = index[p]
        if scope and scope not in row.scope.ancestors:
            continue
        if len(rows) >= message["max_items"] or visited >= message["max_visited"]:
            next_path, reason = p, "page_items" if len(rows) >= message["max_items"] else "page_scan"
            break
        visited += 1
        if direct and row.scope.path != scope:
            continue
        item = public(row)
        size = len(json.dumps(item, ensure_ascii=True).encode())
        if size > message["max_bytes"] - nbytes:
            next_path, reason = p, "page_bytes"
            break
        rows.append(item)
        nbytes += size
    return {"kind": "scope", "results": rows, "bytes_returned": nbytes,
            "visited": visited, "next_path": next_path, "stop_reason": reason}


def resolve(index, path):
    if path in index:
        return index[path]
    matches = [r for p, r in index.items() if r.base == path]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("fst_signal_not_found" if not matches else "fst_signal_ambiguous")


def build_index(lib, ffi, reader):
    if lib.fstReaderGetVarCount(reader) > MAX_DECLARATIONS:
        raise ValueError("fst_metadata_entry_limit")
    scopes, index = [], {}
    root = Scope("", (), None)
    # Owned object sizes, not JSON expansion. Shared immutable enum/small-int
    # objects are deliberately overcharged. Reserve small build tables;
    # dict/list backing allocations are charged separately. One bounded
    # in-construction record and container resize can temporarily exceed this
    # accounting cap; the separate process address limit covers that peak.
    nbytes = 64 * 1024
    scope_count = 0

    def account(extra):
        nonlocal nbytes
        nbytes += extra
        used = nbytes + sys.getsizeof(index) + sys.getsizeof(scopes)
        if used > MAX_INDEX_BYTES:
            raise ValueError("fst_metadata_memory_limit")
        return used
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
            name = ffi.string(node.u.scope.name).decode("utf-8", "strict")
            parent = scopes[-1] if scopes else root
            path = parent.path + "." + name if scopes else name
            scope = Scope(path, parent.ancestors + (path,), parent.top if scopes else path)
            account(sys.getsizeof(scope) + sys.getsizeof(path) + sys.getsizeof(scope.ancestors))
            scopes.append(scope)
            scope_count += 1
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
            scope = scopes[-1] if scopes else root
            path = scope.path + "." + name if scope.path else name
            explicit = re.search(r"\[(-?\d+)(?::(-?\d+))?\]$", name)
            # An attached single index can name a dumped array element. Only
            # a separated range token (or an explicit colon range) supplies
            # packed coordinates; never alias mem[2] to an undumped mem.
            if explicit and ":" not in explicit[0] and match is None:
                explicit = None
            if name.startswith("\\") and not match:
                explicit = None
            coords = None
            base = path
            if explicit:
                left, right = int(explicit[1]), int(explicit[2] or explicit[1])
                if abs(left - right) + 1 != var.length:
                    raise ValueError("fst_declaration_range_unsupported")
                coords = (left, right)
                base = path[:len(path) - len(explicit[0])].rstrip()
            elif var.length == 1:
                coords = (0, 0)
            row = Declaration(path, base, name, scope, int(var.length), int(var.handle),
                bool(var.is_alias), directions.get(int(var.direction), "unknown"),
                types.get(int(var.typ), "unknown"), coords,
                int(var.typ) in types and int(var.typ) not in unsupported and var.length > 0)
            if path in index:
                raise ValueError("fst_declaration_conflict")
            if len(index) >= MAX_DECLARATIONS:
                raise ValueError("fst_metadata_entry_limit")
            owned = sys.getsizeof(row) + sys.getsizeof(path)
            owned += sys.getsizeof(name) if name is not path else 0
            owned += sys.getsizeof(base) if base is not path else 0
            owned += sum(sys.getsizeof(v) for v in (row.width, row.handle, row.is_alias,
                                                   row.direction, row.var_type, row.supported))
            if coords is not None:
                owned += sys.getsizeof(coords) + sum(sys.getsizeof(v) for v in coords)
            account(owned)
            index[path] = row
            account(0)
    if scopes or len(index) != lib.fstReaderGetVarCount(reader):
        raise ValueError("fst_corrupt: incomplete hierarchy")
    METRICS.update(index_declarations=len(index), index_scopes=scope_count,
                   index_accounted_bytes=account(0))
    return index


def window_scan_start(api, reader, path, header, start, handles, check):
    """Skip older blocks only after witnessing an actual pre-window event.

    A time-filtered libfst traversal emits a snapshot at the first loaded
    block's begin tick. That snapshot proves no change time. A callback
    strictly AFTER that tick and BEFORE the query is a genuine event; every
    selected storage handle must have one before we use the filtered scan.
    The final scan will overwrite its synthetic prefix with those actual
    events before emitting anything. Otherwise preserve the original scan.
    No event/window cache or retained block index is introduced.
    """
    METRICS.update(native_prefix_probe_iterations=0, native_prefix_probe_callbacks=0,
                   native_prefix_blocks_skipped=0)
    if not handles or start <= header['start_tick'] * header['scale_fs']:
        return 0
    # Recording interruptions need history beyond event timestamps. Keep
    # their established full-prefix treatment, even outside this window.
    if any(not active for _, active in header['activity']):
        return 0
    tick = start // header['scale_fs']
    if tick > header['end_tick']:
        return 0
    skipped = 0
    first_begin = None
    with open(path, 'rb') as stream:
        # preflight already validated these envelopes under the same file
        # identity. Seek over compressed payloads, retaining only two ticks.
        pos, size = 0, os.stat(path).st_size
        while pos < size:
            stream.seek(pos)
            raw = stream.read(9)
            if len(raw) != 9:
                raise ValueError('fst_corrupt: incomplete block header')
            kind, length = raw[0], struct.unpack('>Q', raw[1:])[0]
            if length < 8 or pos + length + 1 > size:
                raise ValueError('fst_corrupt: invalid block envelope')
            if kind in (1, 5, 8):
                begin, end = struct.unpack('>QQ', stream.read(16))
                if end >= tick:
                    first_begin = begin
                    break
                skipped += 1
            pos += length + 1
    if not skipped or first_begin is None or first_begin * header['scale_fs'] >= start:
        return 0
    witnessed, callbacks = set(), 0

    def probe(_, at, handle, pointer):
        nonlocal callbacks
        try:
            callbacks += 1
            if first_begin < at and at * header['scale_fs'] < start and handle in handles:
                witnessed.add(handle)
        except BaseException:
            # CFFI cannot propagate callback failures. Never continue with
            # potentially incomplete proof of the prefix.
            try:
                emit({'kind': 'error', 'reason': 'fst_callback_failed'})
            finally:
                os._exit(2)

    api.lib.fstReaderSetLimitTimeRange(reader, tick, tick)
    rc = api.fstReaderIterBlocks(reader, probe)
    check()
    if rc != 1 or api.lib.fstReaderGetFseekFailed(reader):
        raise ValueError('fst_decode_failed')
    METRICS.update(native_prefix_probe_iterations=1, native_prefix_probe_callbacks=callbacks)
    if witnessed != set(handles):
        return 0
    METRICS['native_prefix_blocks_skipped'] = skipped
    return tick


def spool_events(api, reader, index, header, message, check, wave_path):
    """One output traversal, optionally preceded by a bounded prefix probe.

    Spooling decouples native callback order from independent consumer cursors.
    Only the selected window and two prefix records are stored, never a VCD or
    a full-file Python event index. The parent reaps this worker before readers
    are opened, so logical cursors cannot exhaust native admission slots.
    """
    lib, ffi = api.lib, api.ffi
    paths = list(dict.fromkeys(message['paths']))
    if not 1 <= len(paths) <= 128:
        raise ValueError('fst_batch_signal_limit')
    cap = message.get('spool_bytes', 64 * 1024 * 1024)
    if not 128 <= cap <= 64 * 1024 * 1024:
        raise ValueError('fst_spool_limit_invalid')
    start, end = message['start_fs'], message['end_fs']
    scale = header['scale_fs']
    begin, finish = header['start_tick'] * scale, header['end_tick'] * scale
    end = finish if end == -1 else end
    if start < 0 or end < start or end > 2**63 - 1:
        raise ValueError('fst_time_range_unsupported')
    declarations, errors, states = {}, {}, {}
    record = struct.Struct('>qBI')
    total_bytes = callbacks = selected_events = 0
    started = time.perf_counter()

    def write(state, at, kind, value):
        nonlocal total_bytes, selected_events
        raw = value.encode('ascii')
        size = record.size + len(raw)
        if total_bytes + size > cap:
            raise ValueError('fst_spool_byte_limit: narrow window or reduce signals')
        state['file'].write(record.pack(at, kind, len(raw)))
        state['file'].write(raw)
        total_bytes += size
        selected_events += kind == 0

    def end_gap(state, at, inclusive=False):
        gap = state['gap']
        if gap is None:
            return
        lo, reason = gap
        lo, hi = max(start, lo), min(end, at)
        right = inclusive if hi == at else True
        if lo < hi or (lo == hi and right):
            write(state, lo, 3, json.dumps([hi, reason, right], separators=(',', ':')))
        state['gap'] = None

    def advance(state, at):
        activity = header['activity']
        while state['activity'] < len(activity) and activity[state['activity']][0] * scale <= at:
            tick, enabled = activity[state['activity']]
            state['activity'] += 1
            t = tick * scale
            if bool(enabled) == state['active']:
                continue
            end_gap(state, t)
            state['gap'] = (t, 'unrecorded_after_dump_resume' if enabled else 'dump_inactive')
            state['active'] = bool(enabled)

    try:
        lib.fstReaderClrFacProcessMaskAll(reader)
        for path in paths:
            try:
                row = resolve(index, path)
                if not row.supported:
                    raise ValueError('fst_signal_type_unsupported')
            except ValueError as exc:
                errors[path] = str(exc)
                continue
            declarations[path] = declaration(row)
            handle = row.handle
            if handle in states:
                continue
            state = dict(file=open(f'{handle}.events', 'wb'), width=row.width,
                         first=True, last=-1, initial=None, previous=None,
                         activity=0, active=True, gap=(begin, 'unrecorded'))
            states[handle] = state
            if start < begin:
                write(state, start, 3, json.dumps([min(end, begin), 'outside_recorded_range', end < begin]))
            lib.fstReaderSetFacProcessMask(reader, handle)
        scan_start = window_scan_start(api, reader, wave_path, header, start, states, check)
        lib.fstReaderSetLimitTimeRange(reader, scan_start, min(header['end_tick'], end // scale))

        def callback(_, tick, handle, pointer):
            nonlocal callbacks
            try:
                callbacks += 1
                state = states[int(handle)]
                at = int(tick) * scale
                if at < state['last']:
                    raise ValueError('fst_transition_order_invalid')
                state['last'] = at
                if at > end:
                    return
                advance(state, at)
                first = state['first'] and at == begin
                state['first'] = False
                if not state['active']:
                    return
                value = bytes(ffi.buffer(pointer, state['width'])).decode('ascii').lower()
                if any(c not in '01xz' for c in value):
                    raise ValueError('fst_digital_value_unsupported')
                end_gap(state, at)
                if first:
                    state['initial'] = (at, value)
                elif at < start:
                    state['previous'] = (at, value)
                else:
                    write(state, at, 0, value)
            except BaseException as exc:
                try:
                    emit({'kind': 'error', 'reason': str(exc) if isinstance(exc, ValueError) else 'fst_callback_failed'})
                finally:
                    os._exit(2)

        if states:
            rc = api.fstReaderIterBlocks(reader, callback)
            if rc != 1 or lib.fstReaderGetFseekFailed(reader):
                raise ValueError('fst_decode_failed')
        streams = {}
        for handle, state in states.items():
            advance(state, min(end, finish))
            end_gap(state, min(end, finish), inclusive=True)
            if end > finish:
                # fs is an integer lattice; invalidation starts strictly after
                # the last recorded instant, not at that still-valid instant.
                write(state, max(start, finish + 1), 3,
                      json.dumps([end, 'outside_recorded_range', True]))
            event_bytes = state['file'].tell()
            prefix = state['previous'] or state['initial']
            if prefix:
                write(state, prefix[0], 2 if state['previous'] else 1, prefix[1])
            streams[str(handle)] = {'event_bytes': event_bytes}
            state['file'].close()
        check()
        emit({'kind': 'batch', 'declarations': declarations, 'errors': errors, 'streams': streams,
              'batch_metrics': {'native_iterations': int(bool(states)) + METRICS['native_prefix_probe_iterations'],
                  'native_callbacks': callbacks + METRICS['native_prefix_probe_callbacks'],
                  'window_transitions': selected_events, 'spool_bytes': total_bytes,
                  'storage_streams': len(states), 'logical_signals': len(paths),
                  'scan_and_spool_ms': (time.perf_counter() - started) * 1000}})
    finally:
        for state in states.values():
            state['file'].close()


def stream_events(api, reader, row, header, message, check, path):
    lib, ffi = api.lib, api.ffi
    cap, byte_cap = message["max_events"], message["max_bytes"]
    if not 1 <= cap <= 4096 or not 128 <= byte_cap <= 1048576:
        raise ValueError("fst_page_limits_invalid")
    start, end = message["start_fs"], message["end_fs"]
    rows, used, previous, initial = [], 0, None, None
    first_page, first_event, finished = True, True, False
    last_tick = -1
    lib.fstReaderClrFacProcessMaskAll(reader)
    lib.fstReaderSetFacProcessMask(reader, row.handle)
    scan_start = window_scan_start(api, reader, path, header, start, {row.handle}, check)
    METRICS.update(native_iterations=1 + METRICS['native_prefix_probe_iterations'],
                   native_callbacks=METRICS['native_prefix_probe_callbacks'])
    lib.fstReaderSetLimitTimeRange(reader, scan_start, min(header["end_tick"], end // header["scale_fs"]))


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
        METRICS['native_callbacks'] += 1
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
            value = bytes(ffi.buffer(pointer, row.width)).decode("ascii").lower()
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
