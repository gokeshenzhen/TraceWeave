"""FST queries over request-scoped, isolated libfst readers.

No waveform or metadata index is retained between requests. Generic pages use
bounded private spools; public analyses are enabled individually after validation.
"""
from collections import deque
from contextlib import contextmanager
import threading

from .cancellation import check_cancelled
from .clock_edge_cache import ClockCacheToken
from .fst_runtime import FstError, FstProcess, FST_BASIC_TOOLS
from .scope_metadata import ScopeCursor, ScopeIdentityChanged, file_identity, normalize_scope, page_limits

MAX_RESULT_EVENTS = 65536
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_SIGNALS = 128
MAX_HISTORY = 128


def _value(bits):
    if bits is None:
        return None
    known = all(c in "01" for c in bits)
    number = int(bits, 2) if known and len(bits) <= 4096 else None
    return {"bin": bits, "hex": hex(number) if number is not None else None, "dec": number}


def _event(row, scale, *, kind="transition"):
    if row is None:
        return None
    fs = row[0] * scale
    ps = (fs + 999) // 1000
    return {"time_fs": fs, "time_ps": ps, "time_ns": ps / 1000,
            "value": _value(row[1]), "event_kind": kind}


def _status(at, last, header):
    scale = header["scale_fs"]
    if not header["start_tick"] * scale <= at <= header["end_tick"] * scale:
        return "outside_recorded_range"
    active, resumed = True, None
    for tick, enabled in header["activity"]:
        if tick * scale > at:
            break
        if enabled and not active:
            resumed = tick * scale
        active = bool(enabled)
    if not active:
        return "dump_inactive"
    if last is None:
        return "unrecorded"
    if resumed is not None and last[0] * scale < resumed:
        return "unrecorded_after_dump_resume"
    return "recorded"


def _gaps(header, start, end, observations):
    """Closed query bounds, with explicit open ends for recording intervals."""
    scale = header["scale_fs"]
    begin, finish = header["start_tick"] * scale, header["end_tick"] * scale
    gaps = []
    def add(a, b, reason, *, start_inclusive=True, end_inclusive=False):
        lo, hi = max(start, a), min(end, b)
        left = start_inclusive if lo == a else True
        right = end_inclusive if hi == b else True
        if lo < hi or (lo == hi and left and right):
            gaps.append({"start_fs": lo, "end_fs": hi, "start_inclusive": left,
                         "end_inclusive": right, "reason": reason})
    if start < begin:
        add(start, begin, "outside_recorded_range")
    if end > finish:
        add(finish, end, "outside_recorded_range", start_inclusive=False, end_inclusive=True)
    off = None
    times = sorted(row[0] * scale for row in observations if row is not None)
    if not times or times[0] > max(begin, start):
        add(begin, times[0] if times else finish, "unrecorded", end_inclusive=not times)
    for tick, active in header["activity"]:
        at = tick * scale
        if not active and off is None:
            off = at
        elif active and off is not None:
            add(off, at, "dump_inactive")
            resumed_value = next((t for t in times if t >= at), None)
            add(at, resumed_value if resumed_value is not None else finish,
                "unrecorded_after_dump_resume", end_inclusive=resumed_value is None)
            off = None
    if off is not None:
        add(off, finish, "dump_inactive", end_inclusive=True)
    return gaps


def _receipt(header, start, end, observations=(), *, truncated=False, metadata=False):
    gaps = [] if metadata else _gaps(header, start, end, observations)
    if truncated:
        gaps.append({"start_fs": start, "end_fs": end, "start_inclusive": True,
                     "end_inclusive": True, "reason": "read_limit"})
    return {"mode": "isolated_libfst_v1", "coverage_status": "partial" if gaps else "complete",
            "recorded_start_fs": header["start_tick"] * header["scale_fs"],
            "recorded_end_fs": header["end_tick"] * header["scale_fs"],
            "gaps": gaps, "time_labels": "ceil_ps", "native_memory_limit_bytes": 512 * 1024 * 1024}


class FSTParser:
    _basic_queries_only = True

    def __init__(self, file_path):
        self.file_path = str(file_path)
        self._scope_epoch = object()
        self._file_identity = None
        self._clock_cache_token = ClockCacheToken()
        self._lock = threading.RLock()
        self._active_batch = None

    def _supports_event_pages(self):
        return True

    def _event_batch(self, paths, start, end, *, deadline=None, spool_bytes=64 * 1024 * 1024):
        from .fst_event_pages import fst_batch
        return fst_batch(self, list(dict.fromkeys(paths)), start, end, deadline=deadline, spool_bytes=spool_bytes)

    @contextmanager
    def _event_pages(self, path, start=0, end=-1, *, max_events=1024, max_bytes=262144):
        from .fst_event_pages import FstEventReader
        if self._active_batch is None:
            with self._event_batch([path], start, end):
                with self._event_pages(path, start, end, max_events=max_events, max_bytes=max_bytes) as reader:
                    yield reader
            return
        reader = FstEventReader(self._active_batch, path, start, end, max_events, max_bytes)
        try:
            yield reader
        finally:
            reader.close()

    @contextmanager
    def _session(self):
        while not self._lock.acquire(timeout=0.05):
            check_cancelled()
        try:
            check_cancelled()
            identity = file_identity(self.file_path)
            if self._file_identity is not None and self._file_identity != identity:
                raise ScopeIdentityChanged("FST changed; close or obtain a new parser")
            self._file_identity = identity
            with FstProcess(self.file_path) as session:
                yield session
                session.check()
        except (FileNotFoundError, PermissionError) as exc:
            code = "fst_file_not_found" if isinstance(exc, FileNotFoundError) else "fst_permission_denied"
            raise FstError(f"{code}: {self.file_path}") from exc
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            self._scope_epoch = object()
            self._file_identity = None
            self._clock_cache_token = ClockCacheToken()

    @staticmethod
    def _declaration(session, path):
        try:
            return session.ask({"op": "declaration", "path": path})["declaration"]
        except FstError as exc:
            if str(exc) in {"fst_signal_not_found", "fst_signal_ambiguous"}:
                raise KeyError(str(exc) + ": " + path) from exc
            raise

    def get_signal_declaration(self, signal_path):
        with self._session() as session:
            row = self._declaration(session, signal_path)
            if not row["supported"]:
                raise FstError("fst_signal_type_unsupported")
            if row["declared_range"] is None:
                raise FstError("fst_declared_range_unknown: supply a dump with explicit coordinates")
            return {"path": row["path"], "width": row["width"], "declared_range": row["declared_range"],
                    "identity": self._file_identity, "storage_key": row["handle"]}

    def get_signal_width(self, signal_path):
        with self._session() as session:
            row = self._declaration(session, signal_path)
            if not row["supported"]:
                raise FstError("fst_signal_type_unsupported")
            return row["width"]

    def get_summary(self):
        with self._session() as session:
            header = session.header
            metadata = session.ask({"op": "metadata"})
            scale = header["scale_fs"]
            units = ((10**15, "s"), (10**12, "ms"), (10**9, "us"), (10**6, "ns"), (1000, "ps"), (1, "fs"))
            divisor, unit = next((n, u) for n, u in units if scale >= n and scale % n == 0)
            end = (header["end_tick"] * scale + 999) // 1000
            return {"file": self.file_path, "format": "FST", "timescale_ps": scale / 1000,
                    "scale_unit": f"{scale // divisor}{unit}", "scale_fs_per_tick": scale,
                    "simulation_duration_ps": end, "simulation_duration_ns": end / 1000,
                    "total_signals": metadata["count"], "top_modules": metadata["top_modules"],
                    "sample_signals": metadata["sample_signals"], "metadata_query_mode": "fst_isolated_v1",
                    "sample_signals_order": "lexical", "top_modules_complete": True,
                    "fst_backend": {**header["backend"], "storage_count": header["storage_count"],
                        "supported_tools": list(FST_BASIC_TOOLS), "analysis_status": "not_run",
                        "observation_scope": "recorded_digital_values_only"},
                    "fst_reading": _receipt(header, 0, end * 1000, metadata=True)}

    get_header = get_summary

    def search_signals(self, keyword, max_results=100):
        if not isinstance(keyword, str) or len(keyword) > 4096 or not 1 <= max_results <= 1024:
            raise ValueError("FST search requires a keyword up to 4096 characters and 1..1024 results")
        with self._session() as session:
            result = session.ask({"op": "search", "keyword": keyword, "limit": max_results})
            return {"keyword": keyword, "total_matched": result["total_matched"],
                    "truncated": result["total_matched"] > len(result["results"]),
                    "results": result["results"],
                    "hint": "FST declarations retain aliases; type/direction unknowns are not inferred"}

    def enumerate_scope_page(self, scope=None, *, cursor=None, direct=False,
                             max_items=1024, max_bytes=1048576, max_visited=4096):
        scope = normalize_scope(scope)
        max_items, max_bytes, max_visited = page_limits(max_items, max_bytes, max_visited)
        with self._session() as session:
            if cursor is not None:
                if not isinstance(cursor, ScopeCursor):
                    raise ValueError("invalid scope cursor")
                cursor.validate(self._scope_epoch, self._file_identity, scope, direct)
            result = session.ask({"op": "scope", "scope": scope, "direct": direct,
                "after": cursor.next_path if cursor else None, "max_items": max_items,
                "max_bytes": min(max_bytes, 512 * 1024), "max_visited": max_visited})
            next_path = result["next_path"]
            return {"results": result["results"], "complete": next_path is None,
                    "cursor": (ScopeCursor(self._scope_epoch, self._file_identity, scope, direct, next_path)
                               if next_path is not None else None),
                    "visited": result["visited"], "bytes_returned": result["bytes_returned"],
                    "mode": "fst_scope_v1", "stop_reason": result["stop_reason"]}

    @staticmethod
    def _bounds(start, end, header):
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("FST times must be integer picoseconds")
        if end == -1:
            end = (header["end_tick"] * header["scale_fs"] + 999) // 1000
        if start < 0 or end < start or end * 1000 > 2**63 - 1:
            raise ValueError("FST time window is outside the supported range")
        return start, end

    def _read(self, path, start, end, *, center=None, history=0, budget=None):
        with self._session() as session:
            header = session.header
            declaration = self._declaration(session, path)
            if not declaration["supported"]:
                raise FstError("fst_signal_type_unsupported")
            exact_end = header["end_tick"] * header["scale_fs"] if end == -1 else end * 1000
            start, end = self._bounds(start, end, header)
            scale = header["scale_fs"]
            rows, before = [], deque(maxlen=history)
            initial = previous = last_at_center = None
            truncated = False
            budget = budget if budget is not None else {"events": MAX_RESULT_EVENTS, "bytes": MAX_RESULT_BYTES}
            auxiliary_bytes = (history + 3) * (declaration["width"] * 4 + 512)
            if budget["bytes"] < auxiliary_bytes:
                raise FstError("fst_result_memory_limit: reduce signal count, width or history")
            budget["bytes"] -= auxiliary_bytes
            # Around-time history uses a forward scan with a bounded ring;
            # ordinary windows keep only the native stream's strict predecessor.
            page = session.ask({"op": "stream", "path": declaration["path"],
                "start_fs": 0 if history else start * 1000, "end_fs": exact_end,
                "max_events": 1024, "max_bytes": 256 * 1024})
            while True:
                check_cancelled()
                if page["initial_state"] is not None:
                    initial = page["initial_state"]
                if page["predecessor"] is not None:
                    previous = page["predecessor"]
                for row in (page["initial_state"], page["predecessor"]):
                    if row is not None and center is not None and row[0] * scale <= center * 1000:
                        last_at_center = row
                for row in page["events"]:
                    at = row[0] * scale
                    if center is not None and at <= center * 1000:
                        last_at_center = row
                    if at < start * 1000:
                        previous = row
                        before.append(row)
                        continue
                    cost = len(row[1]) * 4 + 512
                    if budget["events"] <= 0 or budget["bytes"] < cost:
                        truncated = True
                        break
                    budget["events"] -= 1
                    budget["bytes"] -= cost
                    rows.append(row)
                if truncated or page["truncated"]:
                    truncated = True
                    break
                if page["complete"]:
                    break
                page = session.ask({"op": "next"})
            observations = [initial, previous, *rows]
            receipt = _receipt(header, start * 1000, exact_end, observations, truncated=truncated)
            value_status = _status(center * 1000, last_at_center, header) if center is not None else None
            if truncated and center is not None:
                # A retained prefix never establishes a point value beyond it.
                value_status = "read_incomplete"
            return {"header": header, "declaration": declaration, "start": start, "end": end,
                    "rows": rows, "initial": initial, "previous": previous, "history": list(before),
                    "point": last_at_center if value_status == "recorded" else None,
                    "value_status": value_status, "truncated": truncated, "receipt": receipt}

    def get_value_at_time(self, signal_path, time_ps):
        result = self._read(signal_path, time_ps, time_ps, center=time_ps)
        row = result["point"]
        return {"signal": signal_path, "time_ps": time_ps, "time_ns": time_ps / 1000,
                "value": _value(row[1]) if row is not None else None,
                "value_status": result["value_status"], "fst_reading": result["receipt"]}

    def get_transitions(self, signal_path, start_ps=0, end_ps=-1):
        result = self._read(signal_path, start_ps, end_ps)
        scale = result["header"]["scale_fs"]
        return {"signal": signal_path, "start_ps": result["start"], "end_ps": result["end"],
                "transitions": [_event(row, scale) for row in result["rows"]],
                "transition_count": len(result["rows"]),
                "predecessor": _event(result["previous"], scale),
                "initial_state": _event(result["initial"], scale, kind="initial_state"),
                "truncated": result["truncated"], "transition_count_is_lower_bound": result["truncated"],
                "fst_reading": result["receipt"]}

    def get_signals_around_time(self, signal_paths, center_ps, window_ps=500, extra_transitions=5):
        if not 0 <= extra_transitions <= MAX_HISTORY or not 0 <= window_ps or len(signal_paths) > MAX_SIGNALS:
            raise ValueError("FST around-time limits: 128 signals, 0..128 history records, nonnegative window")
        signals, cache = {}, {}
        costs = {}
        truncated = False
        budget = {"events": MAX_RESULT_EVENTS, "bytes": MAX_RESULT_BYTES}
        for path in signal_paths:
            check_cancelled()
            try:
                with self._session() as session:
                    declaration = self._declaration(session, path)
                key = declaration["handle"]
                if key not in cache:
                    before_bytes = budget["bytes"]
                    result = self._read(path, max(0, center_ps - window_ps), center_ps + window_ps,
                                        center=center_ps, history=extra_transitions, budget=budget)
                    scale = result["header"]["scale_fs"]
                    point = result["point"]
                    cache[key] = {"value_at_center": _value(point[1]) if point is not None else None,
                        "value_status": result["value_status"],
                        "transitions_in_window": [_event(row, scale) for row in result["rows"]],
                        "pre_window_transitions": [_event(row, scale) for row in result["history"]],
                        "initial_state": _event(result["initial"], scale, kind="initial_state"),
                        "fst_reading": result["receipt"]}
                    truncated |= result["truncated"]
                    costs[key] = before_bytes - budget["bytes"]
                else:
                    if budget["bytes"] < costs[key]:
                        raise FstError("fst_result_memory_limit: reduce alias output count")
                    budget["bytes"] -= costs[key]
                signals[path] = cache[key]
            except KeyError as exc:
                signals[path] = {"error": str(exc)}
        return {"center_time_ps": center_ps, "center_time_ns": center_ps / 1000,
                "window_ps": window_ps, "extra_transitions": extra_transitions,
                "signals": signals, "truncated": truncated}
