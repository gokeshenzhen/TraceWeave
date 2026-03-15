"""
vcd_parser.py
纯 Python VCD 解析器，不依赖任何外部库
接口与 FSDBParser 完全一致
"""

import re
from bisect import bisect_right
from pathlib import Path


class VCDParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._parsed          = False
        self._timescale_ps    = 1
        self._signals: dict   = {}          # symbol → {path, width}
        self._path_to_sym: dict = {}        # full_path → symbol
        self._transitions: dict = {}        # symbol → [(time_ps, value)]
        self._end_time_ps     = 0
        self._top_modules: list = []

    # ── Public API ────────────────────────────────────────────────

    def get_value_at_time(self, signal_path: str, time_ps: int) -> dict:
        self._ensure_parsed()
        sym   = self._resolve(signal_path)
        trans = self._transitions.get(sym, [])
        value = _value_at(trans, time_ps)
        return {
            "signal":  signal_path,
            "time_ps": time_ps,
            "time_ns": time_ps / 1000,
            "value":   _enrich_value(value),
        }

    def get_transitions(self, signal_path: str,
                        start_ps: int = 0, end_ps: int = -1) -> dict:
        self._ensure_parsed()
        sym   = self._resolve(signal_path)
        trans = self._transitions.get(sym, [])
        if end_ps == -1:
            end_ps = self._end_time_ps
        filtered = [(t, v) for t, v in trans if start_ps <= t <= end_ps]
        return {
            "signal":           signal_path,
            "start_ps":         start_ps,
            "end_ps":           end_ps,
            "transition_count": len(filtered),
            "transitions": [{"time_ps": t, "time_ns": t / 1000, "value": _enrich_value(v)}
                            for t, v in filtered],
        }

    def get_signals_around_time(self, signal_paths: list,
                                center_ps: int, window_ps: int = 500,
                                extra_transitions: int = 5) -> dict:
        self._ensure_parsed()
        start_ps = max(0, center_ps - window_ps)
        end_ps   = center_ps + window_ps
        result   = {}
        for path in signal_paths:
            try:
                sym   = self._resolve(path)
                trans = self._transitions.get(sym, [])
                filtered = [(t, v) for t, v in trans if start_ps <= t <= end_ps]
                pre_window = [(t, v) for t, v in trans if t < start_ps][-extra_transitions:]
                result[path] = {
                    "value_at_center":       _enrich_value(_value_at(trans, center_ps)),
                    "transitions_in_window": [{"time_ps": t, "time_ns": t / 1000, "value": _enrich_value(v)}
                                              for t, v in filtered],
                    "pre_window_transitions": [{"time_ps": t, "time_ns": t / 1000, "value": _enrich_value(v)}
                                               for t, v in pre_window],
                }
            except Exception as e:
                result[path] = {"error": str(e)}
        return {
            "center_time_ps": center_ps,
            "center_time_ns": center_ps / 1000,
            "window_ps":      window_ps,
            "extra_transitions": extra_transitions,
            "signals":        result,
            "truncated":      False,
        }

    def get_summary(self) -> dict:
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
        """VCD 版的信号搜索（已有完整索引，直接查）"""
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

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_parsed(self):
        if not self._parsed:
            self._parse()
            self._parsed = True

    def _resolve(self, signal_path: str) -> str:
        if signal_path in self._path_to_sym:
            return self._path_to_sym[signal_path]
        for full, sym in self._path_to_sym.items():
            if full.endswith("." + signal_path) or full == signal_path:
                return sym
        sample = list(self._path_to_sym.keys())[:5]
        raise KeyError(f"信号未找到: '{signal_path}'。示例路径: {sample}")

    def _parse(self):
        if not Path(self.file_path).exists():
            raise FileNotFoundError(f"VCD 文件不存在: {self.file_path}")
        with open(self.file_path, "r", errors="replace") as f:
            content = f.read()

        # timescale
        ts = re.search(r'\$timescale\s+(.*?)\s*\$end', content, re.DOTALL)
        if ts:
            self._timescale_ps = _parse_timescale(ts.group(1).strip())

        scope_stack   = []
        current_ps    = 0
        tokens        = content.split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "$scope":
                scope_name = tokens[i + 2] if i + 2 < len(tokens) else "unknown"
                scope_stack.append(scope_name)
                if len(scope_stack) == 1 and scope_name not in self._top_modules:
                    self._top_modules.append(scope_name)
                i += 4
            elif tok == "$upscope":
                if scope_stack:
                    scope_stack.pop()
                i += 2
            elif tok == "$var":
                # $var wire 8 # data [7:0] $end
                width  = int(tokens[i + 2]) if tokens[i + 2].isdigit() else 1
                symbol = tokens[i + 3]
                name   = tokens[i + 4]
                full   = ".".join(scope_stack + [name])
                self._signals[symbol]     = {"path": full, "width": width}
                self._path_to_sym[full]   = symbol
                self._transitions[symbol] = []
                i += 6
            elif tok.startswith("#"):
                try:
                    current_ps = int(tok[1:]) * self._timescale_ps
                    self._end_time_ps = max(self._end_time_ps, current_ps)
                except ValueError:
                    pass
                i += 1
            elif tok.startswith(("b", "B")):
                val = tok
                if i + 1 < len(tokens):
                    sym = tokens[i + 1]
                    if sym in self._transitions:
                        self._transitions[sym].append((current_ps, val))
                i += 2
            elif len(tok) >= 2 and tok[0] in "01xXzZ":
                val = tok[0]
                sym = tok[1:]
                if sym in self._transitions:
                    self._transitions[sym].append((current_ps, val))
                i += 1
            else:
                i += 1


# ── Utility ────────────────────────────────────────────────────────

def _value_at(transitions: list, time_ps: int):
    """二分查找 transitions 中 time_ps 时刻的值，O(log n)"""
    if not transitions:
        return None
    # transitions 是 (time_ps, value) 的有序列表
    times = [t for t, _ in transitions]
    idx = bisect_right(times, time_ps) - 1
    if idx < 0:
        return None
    return transitions[idx][1]


def _enrich_value(binary_str: str | None) -> dict | None:
    if binary_str is None:
        return None
    result = {"bin": binary_str}
    normalized = binary_str.strip()
    if not normalized or any(c in normalized for c in "xXzZu?"):
        result["hex"] = None
        result["dec"] = None
        return result
    if normalized.startswith(("b", "B")):
        normalized = normalized[1:]
        result["bin"] = normalized
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


def _parse_timescale(ts_str: str) -> int:
    ts_str = ts_str.strip().replace(" ", "")
    units  = {"fs": 0.001, "ps": 1, "ns": 1000, "us": 1_000_000,
               "ms": 1_000_000_000, "s": 1_000_000_000_000}
    for unit, mult in units.items():
        if ts_str.endswith(unit):
            try:
                return int(float(ts_str[:-len(unit)]) * mult)
            except ValueError:
                pass
    return 1
