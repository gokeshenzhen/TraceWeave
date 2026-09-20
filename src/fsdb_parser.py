"""
fsdb_parser.py
Read FSDB waveforms through libfsdb_wrapper.so (C++ wrapper).
The public API matches vcd_parser.py.
"""

import ctypes
import os
import struct
import sys
from collections import OrderedDict
from contextlib import contextmanager
from types import MappingProxyType
from typing import Iterator

from config import (
    DEFAULT_EXTRA_TRANSITIONS,
    FSDB_REQUIRED_LIBS,
    LOCAL_FSDB_RUNTIME_DIR,
    get_fsdb_runtime_info,
    SIGNAL_SEARCH_MAX_RESULTS,
)
from . import operation_metrics
from .clock_edge_cache import ClockCacheToken, discard_clock_edges
from .cancellation import OperationCancelled, check_cancelled
from .scope_metadata import (PAGE_ITEMS, PAGE_BYTES, PAGE_SCAN, ScopeCursor,
                             ScopeIdentityChanged, normalize_scope, page_limits)

# Wrapper shared object lives next to this file.
_WRAPPER_SO = os.path.join(os.path.dirname(__file__), "..", "libfsdb_wrapper.so")

# fsdbVarDir (fsdbShr.h)
_FSDB_VAR_DIR = {
    0: "implicit", 1: "input", 2: "output", 3: "inout",
    4: "buffer", 5: "linkage",
}

# fsdbVarType (fsdbShr.h) — only the values commonly seen in HDL FSDBs.
_FSDB_VAR_TYPE = {
    0: "event", 1: "integer", 2: "parameter", 3: "real", 4: "reg",
    5: "supply0", 6: "supply1", 7: "time",
    8: "tri", 9: "triand", 10: "trior", 11: "trireg",
    12: "tri0", 13: "tri1", 14: "wand", 15: "wire", 16: "wor",
    17: "memory",
}


class _NativeTransitionProfileV1(ctypes.Structure):
    _fields_ = [
        ("lookup_ns", ctypes.c_uint64),
        ("add_signal_ns", ctypes.c_uint64),
        ("load_ns", ctypes.c_uint64),
        ("create_handle_ns", ctypes.c_uint64),
        ("seek_ns", ctypes.c_uint64),
        ("traverse_format_ns", ctypes.c_uint64),
        ("free_handle_ns", ctypes.c_uint64),
        ("unload_ns", ctypes.c_uint64),
        ("transition_count", ctypes.c_uint64),
        ("output_bytes", ctypes.c_uint64),
        ("truncated", ctypes.c_int),
    ]


class _NativeTransitionGroupProfileV1(ctypes.Structure):
    _fields_ = [
        ("lookup_ns", ctypes.c_uint64),
        ("add_signal_ns", ctypes.c_uint64),
        ("load_ns", ctypes.c_uint64),
        ("unload_ns", ctypes.c_uint64),
        ("signal_count", ctypes.c_uint64),
    ]


class _NativeSignalMetadataV1(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint), ("direction", ctypes.c_uint),
                ("var_type", ctypes.c_uint)]


class _NativeScopePageV1(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in
                ("returned", "visited", "output_bytes", "complete", "stop_reason")]


def _profile_dict(profile: ctypes.Structure) -> dict[str, int]:
    return {name: int(getattr(profile, name)) for name, _ in profile._fields_}


def _load_wrapper():
    so_path = os.path.abspath(_WRAPPER_SO)
    if not os.path.exists(so_path):
        raise RuntimeError(
            f"libfsdb_wrapper.so not found: {so_path}\n"
            "Run `bash scripts/build_wrapper.sh` from the TraceWeave repo root."
        )
    runtime_info = get_fsdb_runtime_info()
    if not runtime_info["enabled"]:
        raise RuntimeError(
            "FSDB parsing unavailable: "
            f"{runtime_info['message']}。\n"
            "If a VCD waveform is available, prefer it in follow-up workflow steps."
        )
    _ensure_wrapper_runtime_dir(runtime_info)
    # Load Verdi runtime dependencies first.
    for libz in ("libz.so.1", "libz.so"):
        try:
            ctypes.CDLL(libz, ctypes.RTLD_GLOBAL)
            break
        except OSError:
            pass
    for lib in ("libnsys.so", "libnffr.so"):
        lib_path = os.path.join(runtime_info["lib_dir"], lib)
        ctypes.CDLL(lib_path, ctypes.RTLD_GLOBAL)
    lib = ctypes.CDLL(so_path)
    _setup(lib)
    return lib


def _ensure_wrapper_runtime_dir(runtime_info: dict):
    if runtime_info["source"] != "verdi_home":
        return
    runtime_dir = LOCAL_FSDB_RUNTIME_DIR
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source_dir = runtime_info["lib_dir"]
    for lib in FSDB_REQUIRED_LIBS:
        target = runtime_dir / lib
        if target.exists():
            continue
        os.symlink(os.path.join(source_dir, lib), target)


def _setup(lib):
    # void* fsdb_open(const char*)
    lib.fsdb_open.restype  = ctypes.c_void_p
    lib.fsdb_open.argtypes = [ctypes.c_char_p]

    # void fsdb_close(void*)
    lib.fsdb_close.restype  = None
    lib.fsdb_close.argtypes = [ctypes.c_void_p]

    # int fsdb_search_signals(void*, const char*, char*, int)
    lib.fsdb_search_signals.restype  = ctypes.c_int
    lib.fsdb_search_signals.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_char_p, ctypes.c_int]

    # Optional, explicitly versioned metadata ABI; old wrappers keep search.
    lib._traceweave_has_metadata_v1 = False
    try:
        lib.fsdb_metadata_version.restype = ctypes.c_int
        lib.fsdb_metadata_version.argtypes = []
        if lib.fsdb_metadata_version() == 1:
            lib.fsdb_get_signal_metadata_v1.restype = ctypes.c_int
            lib.fsdb_get_signal_metadata_v1.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p,
                ctypes.POINTER(_NativeSignalMetadataV1), ctypes.c_uint,
            ]
            lib.fsdb_get_summary_paths_v1.restype = ctypes.c_int
            lib.fsdb_get_summary_paths_v1.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
            ]
            lib._traceweave_has_metadata_v1 = True
    except AttributeError:
        pass

    lib._traceweave_has_scope_page_v1 = False
    try:
        lib.fsdb_scope_page_version.restype = ctypes.c_int
        lib.fsdb_scope_page_version.argtypes = []
        if lib.fsdb_scope_page_version() == 1:
            lib.fsdb_scope_page_v1.restype = ctypes.c_int
            lib.fsdb_scope_page_v1.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint,
                ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(_NativeScopePageV1), ctypes.c_uint,
            ]
            lib._traceweave_has_scope_page_v1 = True
    except AttributeError:
        pass

    # int fsdb_get_value_at_time(void*, const char*, uint64, char*, int)
    lib.fsdb_get_value_at_time.restype  = ctypes.c_int
    lib.fsdb_get_value_at_time.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_uint64,
                                            ctypes.c_char_p, ctypes.c_int]

    # int fsdb_get_transitions(void*, const char*, uint64, uint64, char*, int)
    lib.fsdb_get_transitions.restype  = ctypes.c_int
    lib.fsdb_get_transitions.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_uint64, ctypes.c_uint64,
                                          ctypes.c_char_p, ctypes.c_int]

    # Optional P2 group-load/profiling ABI. Older wrappers remain usable: the
    # parser falls back to fsdb_get_transitions and records an aggregate
    # unsupported fallback receipt during a full sweep.
    try:
        lib.fsdb_get_transitions_profiled.restype = ctypes.c_int
        lib.fsdb_get_transitions_profiled.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p,
            ctypes.c_uint64, ctypes.c_uint64,
            ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(_NativeTransitionProfileV1),
        ]
        lib.fsdb_begin_transition_group.restype = ctypes.c_int
        lib.fsdb_begin_transition_group.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
            ctypes.POINTER(_NativeTransitionGroupProfileV1),
        ]
        lib.fsdb_get_loaded_transitions.restype = ctypes.c_int
        lib.fsdb_get_loaded_transitions.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p,
            ctypes.c_uint64, ctypes.c_uint64,
            ctypes.c_char_p, ctypes.c_int,
            ctypes.POINTER(_NativeTransitionProfileV1),
        ]
        lib.fsdb_end_transition_group.restype = ctypes.c_int
        lib.fsdb_end_transition_group.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_NativeTransitionGroupProfileV1),
        ]
        lib._traceweave_has_transition_group = True
    except AttributeError:
        lib._traceweave_has_transition_group = False

    # int fsdb_get_multi_signals_around_time(
    #     void*, const char**, int, uint64, uint64, int, char*, int)
    lib.fsdb_get_multi_signals_around_time.restype = ctypes.c_int
    lib.fsdb_get_multi_signals_around_time.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
        ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int,
        ctypes.c_char_p, ctypes.c_int,
    ]

    # int fsdb_batch_window_transitions(
    #     void*, const char**, int, uint64, uint64, char*, int)
    lib.fsdb_batch_window_transitions.restype = ctypes.c_int
    lib.fsdb_batch_window_transitions.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
        ctypes.c_uint64, ctypes.c_uint64,
        ctypes.c_char_p, ctypes.c_int,
    ]

    # unsigned long long fsdb_get_end_time(void*)
    lib.fsdb_get_end_time.restype  = ctypes.c_uint64
    lib.fsdb_get_end_time.argtypes = [ctypes.c_void_p]

    # int fsdb_get_signal_count(void*)
    lib.fsdb_get_signal_count.restype  = ctypes.c_int
    lib.fsdb_get_signal_count.argtypes = [ctypes.c_void_p]

    # unsigned long long fsdb_get_scale_info(void*, char*, int)
    # An old .so without this symbol also lacks tick↔ps scale conversion: it
    # silently treats 1 FSDB tick as 1 ps and misaligns every timestamp on any
    # non-1ps-scale FSDB. Refuse to run against it instead of returning
    # plausible-but-wrong times.
    try:
        lib.fsdb_get_scale_info.restype  = ctypes.c_uint64
        lib.fsdb_get_scale_info.argtypes = [ctypes.c_void_p,
                                            ctypes.c_char_p, ctypes.c_int]
    except AttributeError:
        raise RuntimeError(
            "libfsdb_wrapper.so is outdated: missing fsdb_get_scale_info "
            "(FSDB time-scale support). Rebuild it with `bash scripts/build_wrapper.sh` "
            "and reconnect the MCP server."
        )


_BUF_SIZE = 64 * 1024 * 1024
# A group keeps every selected signal resident in the native FFR layer until
# the context exits. Keep the default deliberately conservative for multi-GB
# waves; larger groups can be opted into after inspecting the RSS telemetry.
_DEFAULT_TRANSITION_GROUP_MAX_SIGNALS = 16
# Per-parser positive metadata LRU. No full-design Python path table or misses.
_METADATA_MAX_ENTRIES = 1024
_METADATA_MAX_BYTES = 256 * 1024
_SUMMARY_PATH_BYTES = 64 * 1024
_SUMMARY_TOP_LIMIT = 256


def _transition_group_limit() -> int:
    try:
        requested = int(os.environ.get(
            "TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS",
            str(_DEFAULT_TRANSITION_GROUP_MAX_SIGNALS),
        ))
    except ValueError:
        return _DEFAULT_TRANSITION_GROUP_MAX_SIGNALS
    return min(256, max(1, requested))


class FSDBParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._clock_cache_token = ClockCacheToken()
        self._lib    = None
        self._handle = None
        self._buf    = None
        self._scale_unit = None   # raw header string, e.g. "100fs"; "unknown" if unreadable
        self._scale_fs   = None   # fs per FSDB tick; 0 = unknown
        self._transition_group_active = False
        self._file_identity = None
        self._header = None
        self._summary_paths = None
        self._metadata_cache = OrderedDict()
        self._metadata_bytes = 0
        self._scope_epoch = object()

    def _stat_identity(self):
        stat = os.stat(self.file_path)
        return (os.path.realpath(self.file_path), stat.st_dev, stat.st_ino,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _check_metadata_identity(self, expected):
        check_cancelled()
        if expected is None:
            return
        try:
            unchanged = expected == self._file_identity == self._stat_identity()
        except OSError:
            unchanged = False
        if not unchanged:
            if not self._transition_group_active:
                self.close()
            raise RuntimeError("FSDB changed during metadata query")

    def _open(self):
        check_cancelled()
        try:
            identity = self._stat_identity()
        except OSError:
            if not self._transition_group_active:
                self.close()
            raise
        if self._handle:
            if identity == self._file_identity:
                return
            if self._transition_group_active:
                raise RuntimeError("FSDB changed during active transition group")
            self.close()
        if self._lib is None:
            self._lib = _load_wrapper()
        handle = self._lib.fsdb_open(self.file_path.encode())
        if not handle:
            raise RuntimeError(f"Unable to open FSDB: {self.file_path}")
        self._handle = handle
        unit_buf = ctypes.create_string_buffer(64)
        self._scale_fs = int(self._lib.fsdb_get_scale_info(handle, unit_buf, 64))
        self._scale_unit = unit_buf.value.decode() or "unknown"
        self._file_identity = identity
        try:
            if self._stat_identity() != identity:
                raise RuntimeError("FSDB changed while opening")
            check_cancelled()
        except BaseException:
            self.close()
            raise

    @property
    def transition_group_limit(self) -> int:
        """Maximum resident signal count used by the bounded sweep scheduler."""
        return _transition_group_limit()

    def _scale_unknown_error(self) -> RuntimeError:
        return RuntimeError(
            f"FSDB time scale is unreadable (header scale unit: {self._scale_unit!r}) "
            f"for {self.file_path}; refusing the time-based query because timestamps "
            "cannot be converted between ps and FSDB ticks. Silently assuming 1 tick "
            "== 1 ps would return misaligned values."
        )

    def close(self):
        self._scope_epoch = object()
        discard_clock_edges(self)
        self.__dict__.pop("_cached_clock_info", None)
        self.__dict__.pop("_cached_clock_detect_reason", None)
        self._header = None
        self._summary_paths = None
        self._file_identity = None
        self._metadata_cache = OrderedDict()
        self._metadata_bytes = 0
        if self._handle and self._lib:
            if getattr(self, "_transition_group_active", False):
                profile = _NativeTransitionGroupProfileV1()
                self._lib.fsdb_end_transition_group(
                    self._handle, ctypes.byref(profile)
                )
                self._transition_group_active = False
            self._lib.fsdb_close(self._handle)
            self._handle = None

    def __del__(self):
        self.close()

    def _get_buf(self):
        """Return the shared 64 MB buffer, created lazily."""
        if self._buf is None:
            self._buf = ctypes.create_string_buffer(_BUF_SIZE)
        return self._buf

    def get_value_at_time(self, signal_path: str, time_ps: int) -> dict:
        self._open()
        # Use the shared 64 MB buffer (not a fixed 1024-byte one): a wide bus
        # such as 1024-bit AXI wdata renders to >1023 chars and would otherwise
        # be silently truncated by a smaller native output capacity.
        buf = self._get_buf()
        rc  = self._lib.fsdb_get_value_at_time(
            self._handle, signal_path.encode(),
            ctypes.c_uint64(time_ps), buf, _BUF_SIZE
        )
        if rc == -2:
            raise KeyError(
                f"Signal not found: '{signal_path}'. Use search_signals to confirm the full path first."
            )
        if rc == -4:
            raise self._scale_unknown_error()
        if rc == -6:
            # Do not let a failed native view restoration poison later reads.
            # A future call will open a fresh handle; close also drops indexes.
            self.close()
            raise RuntimeError("FSDB point-read cleanup failed; reader closed for recovery")
        if rc < 0:
            raise RuntimeError(f"fsdb_get_value_at_time failed, rc={rc}")
        return {
            "signal":  signal_path,
            "time_ps": time_ps,
            "time_ns": time_ps / 1000,
            "value":   _enrich_value(buf.value.decode()),
        }

    def get_transitions(self, signal_path: str,
                        start_ps: int = 0, end_ps: int = -1) -> dict:
        self._open()
        buf = self._get_buf()
        end = ctypes.c_uint64(0xFFFFFFFFFFFFFFFF if end_ps == -1 else end_ps)
        profile = None
        if getattr(self._lib, "_traceweave_has_transition_group", False):
            profile = _NativeTransitionProfileV1()
            native_fn = (
                self._lib.fsdb_get_loaded_transitions
                if self._transition_group_active
                else self._lib.fsdb_get_transitions_profiled
            )
            rc = native_fn(
                self._handle, signal_path.encode(),
                ctypes.c_uint64(start_ps), end,
                buf, _BUF_SIZE, ctypes.byref(profile),
            )
            operation_metrics.record_sweep_native_transition(
                _profile_dict(profile),
                standalone_load=not self._transition_group_active,
            )
        else:
            rc = self._lib.fsdb_get_transitions(
                self._handle, signal_path.encode(),
                ctypes.c_uint64(start_ps), end,
                buf, _BUF_SIZE
            )
        if rc == -2:
            raise KeyError(f"Signal not found: '{signal_path}'")
        if rc == -4:
            raise self._scale_unknown_error()
        if rc < 0:
            raise RuntimeError(f"fsdb_get_transitions failed, rc={rc}")
        text = buf.value.decode()
        raw_transitions = _parse_trans_buf(text)
        predecessor = _parse_predecessor_buf(text)
        if predecessor is None:
            # Compatibility with an older wrapper: ffrGotoXTag leaked its
            # at-or-before result as the first ordinary transition. Separate
            # that state here so the public transition list is strict even
            # before the local native wrapper is rebuilt.
            legacy_predecessors = [
                item for item in raw_transitions if item["time_ps"] < start_ps
            ]
            if legacy_predecessors:
                predecessor = max(
                    legacy_predecessors, key=lambda item: item["time_ps"]
                )
        transitions = [
            item
            for item in raw_transitions
            if item["time_ps"] >= start_ps
            and (end_ps == -1 or item["time_ps"] <= end_ps)
        ]
        native_truncated = _buffer_was_truncated(text)
        return {
            "signal":           signal_path,
            "start_ps":         start_ps,
            "end_ps":           end_ps,
            "transition_count": len(transitions),
            "transitions":      transitions,
            # Explicit pre-state for internal edge extraction. It is never
            # mixed into the strict [start_ps, end_ps] transition list.
            "predecessor":      predecessor,
            # Native output is bounded by the shared buffer. A true value means
            # this is only a prefix and must never support a clean/full verdict.
            "truncated":        native_truncated,
            "transition_count_is_lower_bound": native_truncated,
        }

    @contextmanager
    def transition_group(self, signal_paths: list[str]) -> Iterator[bool]:
        """Load a bounded signal group once while preserving per-signal output.

        Yields whether the optimized native path is active. Unsupported/large/
        failed groups fall back before touching FFR group state, so callers can
        continue using ordinary ``get_transitions`` unchanged.
        """
        self._open()
        paths = list(dict.fromkeys(str(path) for path in signal_paths if path))
        if not paths:
            yield False
            return
        if not getattr(self._lib, "_traceweave_has_transition_group", False):
            operation_metrics.record_sweep_native_group_fallback(
                "unsupported", signal_count=len(paths)
            )
            yield False
            return
        if len(paths) > _transition_group_limit():
            operation_metrics.record_sweep_native_group_fallback(
                "oversized", signal_count=len(paths)
            )
            yield False
            return

        # Duration's rare legacy recovery may load signals. Prepare it before
        # beginning a resident group, never from inside an active native view.
        if getattr(self._lib, "_traceweave_has_metadata_v1", False):
            self.get_header()

        encoded = [path.encode() for path in paths]
        c_paths = (ctypes.c_char_p * len(encoded))(*encoded)
        begin_profile = _NativeTransitionGroupProfileV1()
        rc = self._lib.fsdb_begin_transition_group(
            self._handle, c_paths, len(encoded), ctypes.byref(begin_profile)
        )
        if rc < 0:
            operation_metrics.record_sweep_native_group_fallback(
                "begin_error", signal_count=len(paths)
            )
            yield False
            return

        self._transition_group_active = True
        operation_metrics.record_sweep_native_group_begin(
            _profile_dict(begin_profile)
        )
        operation_metrics.record_sweep_rss(phase="sample")
        try:
            yield True
        finally:
            # Capture the group's loaded high-water before FFR unload, then the
            # caller samples again after the context to observe retention.
            operation_metrics.record_sweep_rss(phase="sample")
            end_profile = _NativeTransitionGroupProfileV1()
            self._lib.fsdb_end_transition_group(
                self._handle, ctypes.byref(end_profile)
            )
            self._transition_group_active = False
            operation_metrics.record_sweep_native_group_end(
                _profile_dict(end_profile)
            )

    def get_signals_around_time(self, signal_paths: list,
                                center_ps: int, window_ps: int = 500,
                                extra_transitions: int = DEFAULT_EXTRA_TRANSITIONS) -> dict:
        self._open()
        if not signal_paths:
            return {
                "center_time_ps": center_ps,
                "center_time_ns": center_ps / 1000,
                "window_ps": window_ps,
                "extra_transitions": extra_transitions,
                "signals": {},
                "truncated": False,
            }

        buf = self._get_buf()
        encoded_paths = [path.encode() for path in signal_paths]
        c_paths = (ctypes.c_char_p * len(encoded_paths))(*encoded_paths)

        rc = self._lib.fsdb_get_multi_signals_around_time(
            self._handle,
            c_paths,
            len(signal_paths),
            ctypes.c_uint64(center_ps),
            ctypes.c_uint64(window_ps),
            ctypes.c_int(extra_transitions),
            buf,
            _BUF_SIZE,
        )
        if rc == -4:
            raise self._scale_unknown_error()
        if rc < 0:
            raise RuntimeError(f"fsdb_get_multi_signals_around_time failed, rc={rc}")
        return _parse_multi_signal_buf(
            buf.value.decode(),
            center_ps=center_ps,
            window_ps=window_ps,
            extra_transitions=extra_transitions,
        )

    def _get_summary_paths(self):
        if self._summary_paths is not None:
            return self._summary_paths
        identity = self._file_identity
        if getattr(self._lib, "_traceweave_has_metadata_v1", False):
            listings = []
            for kind, limit in ((0, 20), (1, _SUMMARY_TOP_LIMIT)):
                check_cancelled()
                buf = ctypes.create_string_buffer(_SUMMARY_PATH_BYTES)
                truncated = ctypes.c_int()
                count = self._lib.fsdb_get_summary_paths_v1(
                    self._handle, kind, limit, buf, len(buf), ctypes.byref(truncated)
                )
                check_cancelled()
                if count < 0:
                    raise RuntimeError(f"fsdb_get_summary_paths_v1 failed, rc={count}")
                paths = tuple(s.decode() for s in buf.raw.split(b"\0", count)[:count])
                listings.append((paths, not bool(truncated.value)))
            result = (listings[0][0], listings[1][0], listings[1][1])
        else:
            search = self.search_signals("", max_results=20)
            check_cancelled()
            samples = tuple(item["path"] for item in search.get("results", [])[:20])
            tops = tuple(sorted({p.split(".")[0] for p in samples if "." in p}))
            # A legacy sample does not prove the complete set of top scopes.
            result = (samples, tops, False)
        self._check_metadata_identity(identity)
        self._summary_paths = result
        return result

    def get_header(self) -> dict:
        """Scalar metadata for comparison; no signal listing on a normal file.

        Returned dictionaries are copies of immutable, file-bound cached facts.
        The zero-duration compatibility recovery alone may read bounded samples.
        All calls retain the caller's existing process-global FSDB lock contract.
        """
        self._open()
        if self._header is not None:
            return dict(self._header)
        identity = self._file_identity
        end_ps = int(self._lib.fsdb_get_end_time(self._handle))
        count  = self._lib.fsdb_get_signal_count(self._handle)
        try:
            if end_ps <= 0 and self._scale_fs != 0:
                if self._transition_group_active:
                    raise RuntimeError("FSDB duration recovery requires an unloaded group")
                for signal_path in self._get_summary_paths()[0][:8]:
                    check_cancelled()
                    transitions = self.get_transitions(signal_path, 0, -1).get("transitions", [])
                    if transitions:
                        end_ps = max(end_ps, transitions[-1]["time_ps"])
        except OperationCancelled:
            raise
        except Exception:
            pass
        summary = {
            "file":                   self.file_path,
            "format":                 "FSDB",
            # Self-check fields: which time scale the tool read from the FSDB
            # header and the conversion factor it derived. Anyone can verify at
            # a glance that timestamps are interpreted in the right unit.
            "scale_unit":             self._scale_unit,
            "scale_fs_per_tick":      self._scale_fs,
            "simulation_duration_ps": end_ps,
            "simulation_duration_ns": end_ps / 1000,
            "total_signals":          count,
            "metadata_query_mode": (
                "native_v1" if getattr(self._lib, "_traceweave_has_metadata_v1", False)
                else "legacy_search"
            ),
        }
        if not self._scale_fs:
            summary["scale_warning"] = (
                "FSDB time scale is unreadable; duration is unavailable and all "
                "time-based queries on this waveform will be refused rather than "
                "silently assuming 1 tick == 1 ps."
            )
        self._check_metadata_identity(identity)
        self._header = MappingProxyType(summary)
        return dict(self._header)

    def get_summary(self) -> dict:
        summary = self.get_header()
        identity = self._file_identity
        try:
            samples, tops, complete = self._get_summary_paths()
        except OperationCancelled:
            raise
        except Exception:
            samples, tops, complete = (), (), False
        self._check_metadata_identity(identity)
        summary.update(sample_signals=list(samples), top_modules=list(tops),
                       top_modules_complete=complete,
                       sample_signals_order=("lexical" if summary["metadata_query_mode"] == "native_v1"
                                             else "legacy_ranked"))
        return summary

    def enumerate_scope_page(self, scope: str | None = None, *, cursor=None,
                             direct: bool = False, max_items: int = PAGE_ITEMS,
                             max_bytes: int = PAGE_BYTES, max_visited: int = PAGE_SCAN) -> dict:
        """Read one bounded lexical subtree page under the caller's FSDB lock.

        Old wrappers explicitly decline this optional ABI; discovery may use its
        legacy search path. A stale cursor never restarts silently on a new file.
        """
        check_cancelled()
        scope = normalize_scope(scope)
        if "\0" in scope:
            raise ValueError("scope contains NUL")
        limits = page_limits(max_items, max_bytes, max_visited)
        if cursor is not None:
            if not isinstance(cursor, ScopeCursor):
                raise ValueError("invalid scope cursor")
            try:
                cursor.validate(self._scope_epoch, self._stat_identity(), scope, direct)
            except OSError as exc:
                raise ScopeIdentityChanged("waveform disappeared during scope enumeration") from exc
        self._open()
        if not getattr(self._lib, "_traceweave_has_scope_page_v1", False):
            raise NotImplementedError("native scope paging unavailable")
        identity = self._file_identity
        max_items, max_bytes, max_visited = limits
        buf = ctypes.create_string_buffer(max_bytes)
        continuation = ctypes.create_string_buffer(PAGE_BYTES)
        receipt = _NativeScopePageV1()
        rc = self._lib.fsdb_scope_page_v1(
            self._handle, scope.encode(), cursor.next_path.encode() if cursor else b"",
            int(direct), max_items, max_visited, buf, max_bytes, continuation, PAGE_BYTES,
            ctypes.byref(receipt), ctypes.sizeof(receipt))
        check_cancelled()
        try:
            self._check_metadata_identity(identity)
        except RuntimeError as exc:
            raise ScopeIdentityChanged(str(exc)) from exc
        if rc != 0:
            raise RuntimeError(f"fsdb_scope_page_v1 failed, rc={rc}")
        if receipt.output_bytes > max_bytes or receipt.returned > max_items:
            raise RuntimeError("invalid native scope page")
        data = buf.raw[:receipt.output_bytes]
        rows, pos = [], 0
        for _ in range(receipt.returned):
            check_cancelled()
            size, width, direction, vartype, parent_bytes = struct.unpack_from("=IIIII", data, pos)
            pos += 20
            raw = data[pos:pos + size]
            if len(raw) != size or parent_bytes > size:
                raise RuntimeError("invalid native scope record")
            path = raw.decode()
            parent = raw[:parent_bytes].decode()
            rows.append({"path": path, "name": raw[parent_bytes + bool(parent_bytes):].decode(),
                         "scope": parent, "width": width,
                         "direction": _FSDB_VAR_DIR.get(direction, "unknown"), "direction_code": direction,
                         "var_type": _FSDB_VAR_TYPE.get(vartype, "unknown"), "var_type_code": vartype})
            pos += size
        if pos != len(data):
            raise RuntimeError("invalid native scope page length")
        next_path = continuation.value.decode()
        return {"results": rows, "complete": bool(receipt.complete),
                "cursor": ScopeCursor(self._scope_epoch, identity, scope, direct, next_path)
                          if next_path and not receipt.complete else None,
                "visited": int(receipt.visited), "bytes_returned": int(receipt.output_bytes),
                "mode": "native_scope_v1",
                "stop_reason": {0: None, 1: "page_items", 2: "page_bytes", 3: "page_scan",
                                4: "path_limit"}.get(receipt.stop_reason, "native_error")}

    def search_signals(self, keyword: str,
                       max_results: int = SIGNAL_SEARCH_MAX_RESULTS) -> dict:
        self._open()
        buf = self._get_buf()
        count = self._lib.fsdb_search_signals(
            self._handle, keyword.encode(), buf, _BUF_SIZE
        )
        results = []
        for line in buf.value.decode().splitlines():
            if "\t" not in line:
                continue
            parts = line.split("\t")
            item = {
                "path":  parts[0],
                "name":  parts[0].split(".")[-1],
                "width": int(parts[1]) if len(parts) > 1 else 0,
                "direction": None,
                "var_type":  None,
            }
            # Direction and var_type are appended by wrapper (>=v2). Older .so
            # builds only emit 2 columns; in that case the fields stay None so
            # the response shape stays stable across wrapper versions.
            if len(parts) >= 4:
                try:
                    dir_code = int(parts[2])
                    type_code = int(parts[3])
                    item["direction"] = _FSDB_VAR_DIR.get(dir_code, "unknown")
                    item["direction_code"] = dir_code
                    item["var_type"] = _FSDB_VAR_TYPE.get(type_code, "unknown")
                    item["var_type_code"] = type_code
                except ValueError:
                    pass
            results.append(item)
        results.sort(key=lambda item: (-_signal_rank(item["path"], keyword.lower()), item["path"]))
        # Older wrappers stop writing when their output buffer fills. Even
        # without an explicit native truncation flag, a nearly full buffer is
        # insufficient evidence of a complete search.
        native_truncated = len(buf.value) >= _BUF_SIZE - 512
        results = results[:max_results]
        return {
            "keyword":       keyword,
            "total_matched": count,
            "truncated": native_truncated or count > len(results),
            "results":       results,
            "hint": "Use the full path from the path field as the signal_path argument for tools such as get_signal_at_time.",
        }

    def get_signal_width(self, signal_path: str) -> int:
        self._open()
        identity = self._file_identity
        cached = self._metadata_cache.get(signal_path)
        if cached is not None:
            self._metadata_cache.move_to_end(signal_path)
            return cached[0][0]
        if getattr(self._lib, "_traceweave_has_metadata_v1", False):
            metadata = _NativeSignalMetadataV1()
            rc = self._lib.fsdb_get_signal_metadata_v1(
                self._handle, signal_path.encode(), ctypes.byref(metadata), ctypes.sizeof(metadata)
            )
            check_cancelled()
            if rc == 0:
                values = (int(metadata.width), int(metadata.direction), int(metadata.var_type))
                self._check_metadata_identity(identity)
                self._cache_metadata(signal_path, values)
                return values[0]
            if rc != -2:
                raise RuntimeError(f"fsdb_get_signal_metadata_v1 failed, rc={rc}")
        # Preserve suffix/legacy resolution; exact native hits never get here.
        exact = self.search_signals(signal_path, max_results=32)
        check_cancelled()
        for item in exact.get("results", []):
            if item.get("path") == signal_path:
                return self._cache_width(signal_path, item, identity)

        leaf = signal_path.split(".")[-1]
        fallback = self.search_signals(leaf, max_results=64)
        check_cancelled()
        for item in fallback.get("results", []):
            path = item.get("path")
            if path == signal_path or path.endswith("." + signal_path):
                return self._cache_width(signal_path, item, identity)

        self._check_metadata_identity(identity)
        raise KeyError(f"Signal not found: '{signal_path}'")

    def get_signal_declaration(self, signal_path: str) -> dict:
        """Projection requires an exact dump key, never legacy suffix fallback."""
        from .waveform_selection import declaration_range
        check_cancelled()
        self._open()
        identity = self._file_identity
        width = None
        if getattr(self._lib, "_traceweave_has_metadata_v1", False):
            metadata = _NativeSignalMetadataV1()
            rc = self._lib.fsdb_get_signal_metadata_v1(
                self._handle, signal_path.encode(), ctypes.byref(metadata), ctypes.sizeof(metadata)
            )
            check_cancelled()
            if rc == 0:
                width = int(metadata.width)
            elif rc != -2:
                raise RuntimeError(f"fsdb_get_signal_metadata_v1 failed, rc={rc}")
        else:
            rows = self.search_signals(signal_path, max_results=64).get("results", [])
            exact = [r for r in rows if r["path"] == signal_path]
            if len(exact) == 1:
                width = int(exact[0]["width"])
        if width is None:
            raise KeyError(f"Exact dump declaration not found: {signal_path}")
        self._check_metadata_identity(identity)
        declared = declaration_range(signal_path, width)
        return {"path": signal_path, "width": width,
                "declared_range": {"left": declared.left, "right": declared.right},
                "identity": identity}

    def _cache_width(self, path, item, identity):
        width = int(item["width"])
        self._check_metadata_identity(identity)
        self._cache_metadata(path, (width, item.get("direction_code"), item.get("var_type_code")))
        return width

    def _cache_metadata(self, path, values):
        # Include tuple, scalars and conservative LRU bookkeeping in the budget.
        size = sys.getsizeof(path) + sys.getsizeof(values) + sum(map(sys.getsizeof, values)) + 128
        if size > _METADATA_MAX_BYTES or _METADATA_MAX_ENTRIES < 1:
            return
        while self._metadata_cache and (len(self._metadata_cache) >= _METADATA_MAX_ENTRIES or
                                       self._metadata_bytes + size > _METADATA_MAX_BYTES):
            _, (_, old_size) = self._metadata_cache.popitem(last=False)
            self._metadata_bytes -= old_size
        self._metadata_cache[path] = (values, size)
        self._metadata_bytes += size


# ── Utility ───────────────────────────────────────────────────────────

def _parse_trans_buf(text: str) -> list:
    result = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        parts = line.split("\t", 1)
        try:
            t_ps = int(parts[0])
            val  = parts[1] if len(parts) > 1 else "?"
            result.append({
                "time_ps": t_ps,
                "time_ns": t_ps / 1000,
                "value":   _enrich_value(val),
            })
        except ValueError:
            pass
    return result


def _parse_predecessor_buf(text: str) -> dict | None:
    for line in text.splitlines():
        if not line.startswith("@PREDECESSOR\t"):
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            return None
        try:
            t_ps = int(parts[1])
        except ValueError:
            return None
        return {
            "time_ps": t_ps,
            "time_ns": t_ps / 1000,
            "value": _enrich_value(parts[2]),
        }
    return None


def _buffer_was_truncated(text: str) -> bool:
    """Return whether native output contains its standalone truncation receipt."""
    return text.startswith("@TRUNCATED\n") or "\n@TRUNCATED\n" in text


def _parse_multi_signal_buf(
    text: str,
    center_ps: int,
    window_ps: int,
    extra_transitions: int,
) -> dict:
    result = {
        "center_time_ps": center_ps,
        "center_time_ns": center_ps / 1000,
        "window_ps": window_ps,
        "extra_transitions": extra_transitions,
        "signals": {},
        "truncated": False,
    }
    current_path = None
    current_section = None

    for line in text.splitlines():
        if not line:
            continue
        if line == "@TRUNCATED":
            result["truncated"] = True
            continue
        if line.startswith("@ERROR\t"):
            _, path, reason = (line.split("\t", 2) + [""])[:3]
            result["signals"][path] = {"error": reason or "unknown_error"}
            current_path = None
            current_section = None
            continue
        if line.startswith("@SIGNAL\t"):
            parts = line.split("\t")
            path = parts[1]
            width = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            result["signals"][path] = {
                "bit_size": width,
                "value_at_center": None,
                "transitions_in_window": [],
                "pre_window_transitions": [],
            }
            current_path = path
            current_section = None
            continue
        if current_path is None:
            continue
        if line.startswith("#VALUE_AT_CENTER\t"):
            value = line.split("\t", 1)[1] if "\t" in line else "?"
            result["signals"][current_path]["value_at_center"] = _enrich_value(value)
            current_section = None
            continue
        if line == "#WINDOW_TRANSITIONS":
            current_section = "transitions_in_window"
            continue
        if line == "#PRE_WINDOW_TRANSITIONS":
            current_section = "pre_window_transitions"
            continue
        if current_section and "\t" in line:
            time_str, value = line.split("\t", 1)
            try:
                time_ps = int(time_str)
            except ValueError:
                continue
            result["signals"][current_path][current_section].append({
                "time_ps": time_ps,
                "time_ns": time_ps / 1000,
                "value": _enrich_value(value),
            })

    start_ps = max(0, center_ps - window_ps)
    end_ps = center_ps + window_ps
    for signal in result["signals"].values():
        if not isinstance(signal, dict) or "error" in signal:
            continue
        signal["transitions_in_window"] = sorted(
            (
                item
                for item in signal["transitions_in_window"]
                if start_ps <= item["time_ps"] <= end_ps
            ),
            key=lambda item: item["time_ps"],
        )
        pre_window = sorted(
            (
                item
                for item in signal["pre_window_transitions"]
                if item["time_ps"] < start_ps
            ),
            key=lambda item: item["time_ps"],
        )
        signal["pre_window_transitions"] = (
            pre_window[-extra_transitions:] if extra_transitions > 0 else []
        )

    return result


def _enrich_value(binary_str: str) -> dict:
    result = {"bin": binary_str}
    normalized = binary_str.strip()
    if not normalized or any(c in normalized for c in "xXzZu?"):
        result["hex"] = None
        result["dec"] = None
        return result
    try:
        val = int(normalized, 2)
    except ValueError:
        result["hex"] = None
        result["dec"] = None
        return result
    width = len(normalized)
    hex_width = max(1, (width + 3) // 4)
    result["hex"] = f"0x{val:0{hex_width}x}"
    result["dec"] = val
    return result


def _signal_rank(path: str, keyword: str) -> int:
    lower = path.lower()
    score = 0
    if path.split(".")[-1].lower() == keyword:
        score += 8
    elif lower.endswith(f".{keyword}"):
        score += 6
    elif keyword in lower:
        score += 3
    if any(token in lower for token in ("dut", "core", "rtl", "design")):
        score += 4
    if any(token in lower for token in ("assert", "checker", "scoreboard", "uvm", "monitor")):
        score -= 3
    return score
