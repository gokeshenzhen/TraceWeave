"""
fsdb_parser.py
FSDB 波形解析器：通过 ctypes 调用 C++ 编写的动态库 (libfsdb_wrapper.so) 来读取信号。
它底层使用了 Verdi 的原生库 (libnffr.so)，因此支持 GB 级别的大波形快速随机访问。
"""

import ctypes
import os
from pathlib import Path
from config import FSDB_LIB_DIR, SIGNAL_SEARCH_MAX_RESULTS

# 指向与当前脚本配套的 C++ 封装库路径
_WRAPPER_SO = os.path.join(os.path.dirname(__file__), "..", "libfsdb_wrapper.so")

def _load_wrapper():
    """加载 C++ 封装库及其所需的 Verdi 依赖环境"""
    so_path = os.path.abspath(_WRAPPER_SO)
    if not os.path.exists(so_path):
        raise RuntimeError(
            f"未找到 libfsdb_wrapper.so：{so_path}\n"
            f"请在 waveform_mcp/ 目录下执行：bash build_wrapper.sh"
        )
    
    # 步骤 A：预加载 zlib，防止 Linux 4.18 环境下的符号缺失问题
    for libz in ("libz.so.1", "libz.so"):
        try:
            ctypes.CDLL(libz, ctypes.RTLD_GLOBAL)
            break
        except OSError:
            pass
            
    # 步骤 B：按顺序加载 Verdi 的核心解析库，必须开启 RTLD_GLOBAL
    for lib in ("libnsys.so", "libnffr.so"):
        try:
            ctypes.CDLL(os.path.join(FSDB_LIB_DIR, lib), ctypes.RTLD_GLOBAL)
        except OSError:
            pass
            
    # 步骤 C：加载我们的 C++ Wrapper 并定义函数签名
    lib = ctypes.CDLL(so_path)
    _setup(lib)
    return lib

def _setup(lib):
    """定义 C++ 函数的参数类型和返回类型，确保 Python 能正确传递数据"""
    # 打开 FSDB 文件句柄
    lib.fsdb_open.restype  = ctypes.c_void_p
    lib.fsdb_open.argtypes = [ctypes.c_char_p]

    # 关闭句柄
    lib.fsdb_close.restype  = None
    lib.fsdb_close.argtypes = [ctypes.c_void_p]

    # 信号搜索函数
    lib.fsdb_search_signals.restype  = ctypes.c_int
    lib.fsdb_search_signals.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_char_p, ctypes.c_int]

    # 获取特定时刻信号值
    lib.fsdb_get_value_at_time.restype  = ctypes.c_int
    lib.fsdb_get_value_at_time.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_uint64,
                                            ctypes.c_char_p, ctypes.c_int]

    # 获取时间段内的所有跳变
    lib.fsdb_get_transitions.restype  = ctypes.c_int
    lib.fsdb_get_transitions.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_uint64, ctypes.c_uint64,
                                          ctypes.c_char_p, ctypes.c_int]

    # 获取仿真结束时间
    lib.fsdb_get_end_time.restype  = ctypes.c_uint64
    lib.fsdb_get_end_time.argtypes = [ctypes.c_void_p]

    # 获取波形总信号数
    lib.fsdb_get_signal_count.restype  = ctypes.c_int
    lib.fsdb_get_signal_count.argtypes = [ctypes.c_void_p]

# 结果缓冲区大小（64MB），用于接收 C++ 返回的大批量跳变数据
_BUF_SIZE = 64 * 1024 * 1024

class FSDBParser:
    def __init__(self, file_path: str):
        """初始化解析器，此时不真正加载库，仅记录路径"""
        self.file_path = file_path
        self._lib    = None
        self._handle = None

    def _open(self):
        """内部方法：延迟打开文件，确保在需要时才占用系统资源"""
        if self._handle:
            return
        if self._lib is None:
            self._lib = _load_wrapper()
        handle = self._lib.fsdb_open(self.file_path.encode())
        if not handle:
            raise RuntimeError(f"无法打开 FSDB：{self.file_path}")
        self._handle = handle

    def close(self):
        """释放 C++ 句柄"""
        if self._handle and self._lib:
            self._lib.fsdb_close(self._handle)
            self._handle = None

    def __del__(self):
        """析构函数，防止内存泄漏"""
        self.close()

    def get_value_at_time(self, signal_path: str, time_ps: int) -> dict:
        """查询某个信号在指定时刻的值（ps 精度）"""
        self._open()
        buf = ctypes.create_string_buffer(1024)
        rc  = self._lib.fsdb_get_value_at_time(
            self._handle, signal_path.encode(),
            ctypes.c_uint64(time_ps), buf, 1024
        )
        if rc == -2:
            raise KeyError(f"信号未找到：'{signal_path}'")
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
        """获取信号在指定时间段内的所有状态切换记录"""
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
        
        # 将 C++ 返回的 tab 分隔字符串解析为 Python 列表
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
        """批量获取多个信号在特定时刻前后的“快照”"""
        self._open()
        start_ps = max(0, center_ps - window_ps)
        end_ps   = center_ps + window_ps
        result   = {}
        for path in signal_paths:
            try:
                # 获取中心点的值
                v_res = self.get_value_at_time(path, center_ps)
                # 获取窗口内的跳变过程
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
        """获取波形文件的元数据（时长、信号总数等）"""
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
        """在波形中模糊搜索信号名"""
        self._open()
        buf = ctypes.create_string_buffer(_BUF_SIZE)
        count = self._lib.fsdb_search_signals(
            self._handle, keyword.encode(), buf, _BUF_SIZE
        )
        results = []
        # C++ wrapper 返回的每一行格式为: path\twidth
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

def _parse_trans_buf(text: str) -> list:
    """工具函数：解析 C++ 返回的跳变数据流"""
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
