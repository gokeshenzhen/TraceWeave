"""
vcd_parser.py
纯 Python VCD 解析器：不依赖 Verdi 库，仅用于处理轻量级的 VCD 文本波形。
它通过将整个 VCD 加载到内存并建立信号与跳变的映射关系来实现查询。
接口设计与 FSDBParser 保持完全一致，实现多协议透明切换。
"""

import re
from pathlib import Path

class VCDParser:
    def __init__(self, file_path: str):
        """初始化解析器，此时不解析数据"""
        self.file_path = file_path
        self._parsed          = False
        self._timescale_ps    = 1            # 时间精度转换比例
        self._signals: dict   = {}          # 内部符号 (symbol) -> {path, width}
        self._path_to_sym: dict = {}        # 完整路径 -> 内部符号
        self._transitions: dict = {}        # 内部符号 -> [(时刻, 值)]
        self._end_time_ps     = 0            # 仿真结束时间
        self._top_modules: list = []        # 顶层模块清单

    # ── Public API ────────────────────────────────────────────────

    def get_value_at_time(self, signal_path: str, time_ps: int) -> dict:
        """查询 VCD 中某信号在指定时刻的状态值"""
        self._ensure_parsed()
        sym   = self._resolve(signal_path)
        trans = self._transitions.get(sym, [])
        value = _value_at(trans, time_ps)
        return {
            "signal":  signal_path,
            "time_ps": time_ps,
            "time_ns": time_ps / 1000,
            "value":   value,
        }

    def get_transitions(self, signal_path: str,
                        start_ps: int = 0, end_ps: int = -1) -> dict:
        """提取 VCD 信号在某段时间内的跳变历史"""
        self._ensure_parsed()
        sym   = self._resolve(signal_path)
        trans = self._transitions.get(sym, [])
        if end_ps == -1:
            end_ps = self._end_time_ps
        # 过滤出时间范围内的记录
        filtered = [(t, v) for t, v in trans if start_ps <= t <= end_ps]
        return {
            "signal":           signal_path,
            "start_ps":         start_ps,
            "end_ps":           end_ps,
            "transition_count": len(filtered),
            "transitions": [{"time_ps": t, "time_ns": t / 1000, "value": v}
                            for t, v in filtered],
        }

    def get_signals_around_time(self, signal_paths: list,
                                center_ps: int, window_ps: int = 500) -> dict:
        """获取多个信号在特定时刻的上下文快照"""
        self._ensure_parsed()
        start_ps = max(0, center_ps - window_ps)
        end_ps   = center_ps + window_ps
        result   = {}
        for path in signal_paths:
            try:
                sym   = self._resolve(path)
                trans = self._transitions.get(sym, [])
                filtered = [(t, v) for t, v in trans if start_ps <= t <= end_ps]
                result[path] = {
                    "value_at_center":       _value_at(trans, center_ps),
                    "transitions_in_window": [{"time_ps": t, "time_ns": t / 1000, "value": v}
                                              for t, v in filtered],
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
        """返回 VCD 文件概况"""
        self._ensure_parsed()
        return {
            "file":                   self.file_path,
            "format":                 "VCD",
            "timescale_ps":           self._timescale_ps,
            "simulation_duration_ps": self._end_time_ps,
            "simulation_duration_ns": self._end_time_ps / 1000,
            "total_signals":          len(self._signals),
            "top_modules":            self._top_modules,
            "sample_signals":         list(self._path_to_sym.keys())[:20],
        }

    def search_signals(self, keyword: str, max_results: int = 100) -> dict:
        """在 VCD 已加载的索引中搜索信号"""
        self._ensure_parsed()
        kw = keyword.lower()
        matched = [
            {"path": p, "name": p.split(".")[-1],
             "width": self._signals[s]["width"]}
            for p, s in self._path_to_sym.items()
            if kw in p.lower()
        ][:max_results]
        return {
            "keyword":        keyword,
            "total_matched":  len(matched),
            "results":        matched,
        }

    # ── Internal Logic ─────────────────────────────────────────────

    def _ensure_parsed(self):
        """确保文件已解析，采用懒加载模式"""
        if not self._parsed:
            self._parse()
            self._parsed = True

    def _resolve(self, signal_path: str) -> str:
        """根据全名或简名解析出 VCD 内部的符号 ID"""
        if signal_path in self._path_to_sym:
            return self._path_to_sym[signal_path]
        # 支持后缀匹配
        for full, sym in self._path_to_sym.items():
            if full.endswith("." + signal_path) or full == signal_path:
                return sym
        sample = list(self._path_to_sym.keys())[:5]
        raise KeyError(f"信号未找到: '{signal_path}'。示例路径: {sample}")

    def _parse(self):
        """执行 VCD 文件的全量解析循环"""
        if not Path(self.file_path).exists():
            raise FileNotFoundError(f"VCD 文件不存在: {self.file_path}")
        
        # 为了兼容性，读取整个文件（对于超大 VCD 建议换 FSDB）
        with open(self.file_path, "r", errors="replace") as f:
            content = f.read()

        # 解析时间单位 (timescale)
        ts = re.search(r'\$timescale\s+(.*?)\s*\$end', content, re.DOTALL)
        if ts:
            self._timescale_ps = _parse_timescale(ts.group(1).strip())

        scope_stack   = []
        current_ps    = 0
        tokens        = content.split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            # 处理层级进入
            if tok == "$scope":
                scope_name = tokens[i + 2] if i + 2 < len(tokens) else "unknown"
                scope_stack.append(scope_name)
                if len(scope_stack) == 1 and scope_name not in self._top_modules:
                    self._top_modules.append(scope_name)
                i += 4
            # 处理层级退出
            elif tok == "$upscope":
                if scope_stack:
                    scope_stack.pop()
                i += 2
            # 处理信号定义
            elif tok == "$var":
                width  = int(tokens[i + 2]) if tokens[i + 2].isdigit() else 1
                symbol = tokens[i + 3]
                name   = tokens[i + 4]
                full   = ".".join(scope_stack + [name])
                self._signals[symbol]     = {"path": full, "width": width}
                self._path_to_sym[full]   = symbol
                self._transitions[symbol] = []
                i += 6
            # 处理仿真时刻变更 (#100)
            elif tok.startswith("#"):
                try:
                    current_ps = int(tok[1:]) * self._timescale_ps
                    self._end_time_ps = max(self._end_time_ps, current_ps)
                except ValueError:
                    pass
                i += 1
            # 处理多位二进制值 (b1010 !)
            elif tok.startswith(("b", "B")):
                val = tok
                if i + 1 < len(tokens):
                    sym = tokens[i + 1]
                    if sym in self._transitions:
                        self._transitions[sym].append((current_ps, val))
                i += 2
            # 处理单比特值 (0!)
            elif len(tok) >= 2 and tok[0] in "01xXzZ":
                val = tok[0]
                sym = tok[1:]
                if sym in self._transitions:
                    self._transitions[sym].append((current_ps, val))
                i += 1
            else:
                i += 1

# ── 工具函数 ───────────────────────────────────────────────────

def _value_at(transitions: list, time_ps: int):
    """从跳变历史列表中检索特定时刻的值（最近的一次变更）"""
    if not transitions:
        return None
    val = transitions[0][1]
    for t, v in transitions:
        if t <= time_ps:
            val = v
        else:
            break
    return val

def _parse_timescale(ts_str: str) -> int:
    """将 VCD 的 timescale 字符串（如 '1ns'）转化为 ps 整数"""
    ts_str = ts_str.strip().replace(" ", "")
    units  = {"fs": 0.001, "ps": 1, "ns": 1000, "us": 1_000_000,
               "ms": 1_000_000_000, "s": 1_000_000_000_000}
    for unit, mult in units.items():
        if ts_str.endswith(unit):
            try:
                # 提取数字部分并计算
                return int(float(ts_str[:-len(unit)]) * mult)
            except ValueError:
                pass
    return 1
