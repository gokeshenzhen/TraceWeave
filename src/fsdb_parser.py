"""
fsdb_parser.py
Read FSDB waveforms through libfsdb_wrapper.so (C++ wrapper).
The public API matches vcd_parser.py.
"""

import ctypes
import os
from config import (
    DEFAULT_EXTRA_TRANSITIONS,
    FSDB_REQUIRED_LIBS,
    LOCAL_FSDB_RUNTIME_DIR,
    get_fsdb_runtime_info,
    SIGNAL_SEARCH_MAX_RESULTS,
)

# Wrapper shared object lives next to this file.
_WRAPPER_SO = os.path.join(os.path.dirname(__file__), "..", "libfsdb_wrapper.so")


def _load_wrapper():
    so_path = os.path.abspath(_WRAPPER_SO)
    if not os.path.exists(so_path):
        raise RuntimeError(
            f"libfsdb_wrapper.so not found: {so_path}\n"
            "Run `bash build_wrapper.sh` from the TraceWeave repo root."
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

    # int fsdb_get_multi_signals_around_time(
    #     void*, const char**, int, uint64, uint64, int, char*, int)
    lib.fsdb_get_multi_signals_around_time.restype = ctypes.c_int
    lib.fsdb_get_multi_signals_around_time.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p), ctypes.c_int,
        ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int,
        ctypes.c_char_p, ctypes.c_int,
    ]

    # unsigned long long fsdb_get_end_time(void*)
    lib.fsdb_get_end_time.restype  = ctypes.c_uint64
    lib.fsdb_get_end_time.argtypes = [ctypes.c_void_p]

    # int fsdb_get_signal_count(void*)
    lib.fsdb_get_signal_count.restype  = ctypes.c_int
    lib.fsdb_get_signal_count.argtypes = [ctypes.c_void_p]


_BUF_SIZE = 64 * 1024 * 1024


class FSDBParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lib    = None
        self._handle = None
        self._buf    = None

    def _open(self):
        if self._handle:
            return
        if self._lib is None:
            self._lib = _load_wrapper()
        handle = self._lib.fsdb_open(self.file_path.encode())
        if not handle:
            raise RuntimeError(f"Unable to open FSDB: {self.file_path}")
        self._handle = handle

    def close(self):
        if self._handle and self._lib:
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
        buf = ctypes.create_string_buffer(1024)
        rc  = self._lib.fsdb_get_value_at_time(
            self._handle, signal_path.encode(),
            ctypes.c_uint64(time_ps), buf, 1024
        )
        if rc == -2:
            raise KeyError(
                f"Signal not found: '{signal_path}'. Use search_signals to confirm the full path first."
            )
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
        rc  = self._lib.fsdb_get_transitions(
            self._handle, signal_path.encode(),
            ctypes.c_uint64(start_ps), end,
            buf, _BUF_SIZE
        )
        if rc == -2:
            raise KeyError(f"Signal not found: '{signal_path}'")
        if rc < 0:
            raise RuntimeError(f"fsdb_get_transitions failed, rc={rc}")
        transitions = _parse_trans_buf(buf.value.decode())
        return {
            "signal":           signal_path,
            "start_ps":         start_ps,
            "end_ps":           end_ps,
            "transition_count": len(transitions),
            "transitions":      transitions,
        }

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
        if rc < 0:
            raise RuntimeError(f"fsdb_get_multi_signals_around_time failed, rc={rc}")
        return _parse_multi_signal_buf(
            buf.value.decode(),
            center_ps=center_ps,
            window_ps=window_ps,
            extra_transitions=extra_transitions,
        )

    def get_summary(self) -> dict:
        self._open()
        end_ps = int(self._lib.fsdb_get_end_time(self._handle))
        count  = self._lib.fsdb_get_signal_count(self._handle)
        return {
            "file":                   self.file_path,
            "format":                 "FSDB",
            "simulation_duration_ps": end_ps,
            "simulation_duration_ns": end_ps / 1000,
            "total_signals":          count,
        }

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
            results.append({
                "path":  parts[0],
                "name":  parts[0].split(".")[-1],
                "width": int(parts[1]) if len(parts) > 1 else 0,
            })
        results.sort(key=lambda item: (-_signal_rank(item["path"], keyword.lower()), item["path"]))
        results = results[:max_results]
        return {
            "keyword":       keyword,
            "total_matched": count,
            "results":       results,
            "hint": "Use the full path from the path field as the signal_path argument for tools such as get_signal_at_time.",
        }

    def get_signal_width(self, signal_path: str) -> int:
        exact = self.search_signals(signal_path, max_results=32)
        for item in exact.get("results", []):
            if item.get("path") == signal_path:
                return int(item["width"])

        leaf = signal_path.split(".")[-1]
        fallback = self.search_signals(leaf, max_results=64)
        for item in fallback.get("results", []):
            path = item.get("path")
            if path == signal_path or path.endswith("." + signal_path):
                return int(item["width"])

        raise KeyError(f"Signal not found: '{signal_path}'")


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
