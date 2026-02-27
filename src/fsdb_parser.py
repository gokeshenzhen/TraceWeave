"""
fsdb_parser.py
通过 libfsdb_wrapper.so（C++ wrapper）读取 FSDB 波形
接口与 vcd_parser.py 完全一致
"""

import ctypes
import os
from pathlib import Path
from config import FSDB_LIB_DIR, SIGNAL_SEARCH_MAX_RESULTS

# wrapper .so 与本文件同目录
_WRAPPER_SO = os.path.join(os.path.dirname(__file__), "..", "libfsdb_wrapper.so")


def _load_wrapper():
    so_path = os.path.abspath(_WRAPPER_SO)
    if not os.path.exists(so_path):
        raise RuntimeError(
            f"未找到 libfsdb_wrapper.so：{so_path}\n"
            f"请在 waveform_mcp/ 目录下执行：bash build_wrapper.sh"
        )
    # 先加载 Verdi 依赖库
    for libz in ("libz.so.1", "libz.so"):
        try:
            ctypes.CDLL(libz, ctypes.RTLD_GLOBAL)
            break
        except OSError:
            pass
    for lib in ("libnsys.so", "libnffr.so"):
        try:
            ctypes.CDLL(os.path.join(FSDB_LIB_DIR, lib), ctypes.RTLD_GLOBAL)
        except OSError:
            pass
    lib = ctypes.CDLL(so_path)
    _setup(lib)
    return lib


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

    # unsigned long long fsdb_get_end_time(void*)
    lib.fsdb_get_end_time.restype  = ctypes.c_uint64
    lib.fsdb_get_end_time.argtypes = [ctypes.c_void_p]

    # int fsdb_get_signal_count(void*)
    lib.fsdb_get_signal_count.restype  = ctypes.c_int
    lib.fsdb_get_signal_count.argtypes = [ctypes.c_void_p]


_BUF_SIZE = 64 * 1024 * 1024   # 64 MB 结果缓冲


class FSDBParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lib    = None
        self._handle = None

    # ── 生命周期 ─────────────────────────────────────────────────────

    def _open(self):
        if self._handle:
            return
        if self._lib is None:
            self._lib = _load_wrapper()
        handle = self._lib.fsdb_open(self.file_path.encode())
        if not handle:
            raise RuntimeError(f"无法打开 FSDB：{self.file_path}")
        self._handle = handle

    def close(self):
        if self._handle and self._lib:
            self._lib.fsdb_close(self._handle)
            self._handle = None

    def __del__(self):
        self.close()

    # ── Public API ────────────────────────────────────────────────────

    def get_value_at_time(self, signal_path: str, time_ps: int) -> dict:
        self._open()
        buf = ctypes.create_string_buffer(1024)
        rc  = self._lib.fsdb_get_value_at_time(
            self._handle, signal_path.encode(),
            ctypes.c_uint64(time_ps), buf, 1024
        )
        if rc == -2:
            raise KeyError(f"信号未找到：'{signal_path}'，请先用 search_signals 确认完整路径")
        if rc < 0:
            raise RuntimeError(f"fsdb_get_value_at_time 失败，rc={rc}")
        return {
            "signal":  signal_path,
            "time_ps": time_ps,
            "time_ns": time_ps / 1000,
            "value":   buf.value.decode(),
        }

    def get_transitions(self, signal_path: str,
                        start_ps: int = 0, end_ps: int = -1) -> dict:
        self._open()
        buf = ctypes.create_string_buffer(_BUF_SIZE)
        end = ctypes.c_uint64(0xFFFFFFFFFFFFFFFF if end_ps == -1 else end_ps)
        rc  = self._lib.fsdb_get_transitions(
            self._handle, signal_path.encode(),
            ctypes.c_uint64(start_ps), end,
            buf, _BUF_SIZE
        )
        if rc == -2:
            raise KeyError(f"信号未找到：'{signal_path}'")
        if rc < 0:
            raise RuntimeError(f"fsdb_get_transitions 失败，rc={rc}")
        transitions = _parse_trans_buf(buf.value.decode())
        return {
            "signal":           signal_path,
            "start_ps":         start_ps,
            "end_ps":           end_ps,
            "transition_count": len(transitions),
            "transitions":      transitions,
        }

    def get_signals_around_time(self, signal_paths: list,
                                center_ps: int, window_ps: int = 500) -> dict:
        self._open()
        start_ps = max(0, center_ps - window_ps)
        end_ps   = center_ps + window_ps
        result   = {}
        for path in signal_paths:
            try:
                # 先拿 center 时刻的值
                v_res = self.get_value_at_time(path, center_ps)
                # 再拿窗口内的跳变
                t_res = self.get_transitions(path, start_ps, end_ps)
                result[path] = {
                    "value_at_center":       v_res["value"],
                    "transitions_in_window": t_res["transitions"],
                }
            except Exception as e:
                result[path] = {"error": str(e)}
        return {
            "center_time_ps": center_ps,
            "center_time_ns": center_ps / 1000,
            "window_ps":      window_ps,
            "signals":        result,
        }

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
        buf = ctypes.create_string_buffer(_BUF_SIZE)
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
            if len(results) >= max_results:
                break
        return {
            "keyword":       keyword,
            "total_matched": count,
            "results":       results,
            "hint": "使用 path 字段中的完整路径作为 get_signal_at_time 等工具的 signal_path 参数",
        }


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
                "value":   val,
            })
        except ValueError:
            pass
    return result
