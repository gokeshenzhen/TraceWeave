"""
log_parser.py
支持：
  - VCS assertion fail
  - Xcelium assertion fail
  - UVM_ERROR / UVM_FATAL（VCS + Xcelium 格式相同）
  - custom_patterns.yaml 中定义的自定义格式
UVM_WARNING 不解析（config.py 中 UVM_PARSE_LEVELS 控制）
"""

import re
from pathlib import Path
from typing import List, Dict, Any
import yaml

from config import CUSTOM_PATTERNS_FILE, UVM_PARSE_LEVELS


# ═══════════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════════

class SimError:
    def __init__(self):
        self.error_type: str  = ""      # ASSERTION_FAIL / UVM_ERROR / UVM_FATAL / CUSTOM
        self.severity: str    = "ERROR" # ERROR / FATAL
        self.simulator: str   = ""      # vcs / xcelium / unknown
        self.sva_file: str    = ""
        self.line_num: int    = 0
        self.start_time_ps: int = 0
        self.fail_time_ps: int  = 0
        self.assertion_name: str = ""
        self.offending_expr: str = ""
        self.sva_code: str    = ""      # Xcelium 附带的 SVA 代码片段
        self.uvm_tag: str     = ""      # UVM reporter tag，如 [TOP]
        self.message: str     = ""      # 完整消息文本
        self.pattern_name: str = ""     # 自定义格式的 name

    def to_dict(self) -> dict:
        return {
            "error_type":      self.error_type,
            "severity":        self.severity,
            "simulator":       self.simulator,
            "sva_file":        self.sva_file,
            "line_number":     self.line_num,
            "start_time_ps":   self.start_time_ps,
            "fail_time_ps":    self.fail_time_ps,
            "fail_time_ns":    round(self.fail_time_ps / 1000, 3),
            "assertion_name":  self.assertion_name,
            "offending_expr":  self.offending_expr,
            "sva_code":        self.sva_code,
            "uvm_tag":         self.uvm_tag,
            "message":         self.message,
            "pattern_name":    self.pattern_name,
        }


# ═══════════════════════════════════════════════════════════════════
# 内置正则
# ═══════════════════════════════════════════════════════════════════

# ── VCS assertion fail ──────────────────────────────────────────────
# "path/file.sv", 66: top_tb.hier.ASSERT_NAME: started at 290000ps failed at 310000ps
_VCS_ASSERT_RE = re.compile(
    r'"([^"]+)",\s*(\d+):\s+'
    r'([\w.]+):\s+'
    r'started at (\d+)(ps|ns|us|fs)\s+'
    r'failed at (\d+)(ps|ns|us|fs)',
    re.IGNORECASE
)
_VCS_OFFEND_RE = re.compile(r'^\s+Offending\s+\'(.+)\'')

# ── Xcelium assertion fail ──────────────────────────────────────────
# xmsim: *E,ASRTST (file.sv,66): (time 270 NS) Assertion hier.NAME has failed (2 cycles, starting 250 NS)
_XCE_ASSERT_RE = re.compile(
    r'xmsim:\s+\*E,ASRTST\s+\(([^,]+),(\d+)\):\s+'
    r'\(time\s+([\d.]+)\s+(PS|NS|US|FS)\)\s+'
    r'Assertion\s+([\w.]+)\s+has failed'
    r'(?:\s+\(\d+\s+cycles?,\s+starting\s+([\d.]+)\s+(PS|NS|US|FS)\))?',
    re.IGNORECASE
)

# ── UVM_ERROR / UVM_FATAL ───────────────────────────────────────────
# UVM_ERROR /path/file.sv(129) @ 1429.000 ns: reporter [TAG] message text
# UVM_FATAL /path/file.sv(129) @ 1429.000 ns: reporter [TAG] message text
_UVM_RE = re.compile(
    r'(UVM_ERROR|UVM_FATAL)\s+'
    r'([^\s(]+)\((\d+)\)\s+'
    r'@\s+([\d.]+)\s*(ps|ns|us|fs)?:\s+'
    r'(\w+)\s+'           # reporter id（不含方括号时）或者直接跟 [TAG]
    r'(?:\[([^\]]+)\]\s+)?'  # [TAG]（可选）
    r'(.*)',
    re.IGNORECASE
)


# ═══════════════════════════════════════════════════════════════════
# 时间换算
# ═══════════════════════════════════════════════════════════════════

def _to_ps(value: float, unit: str) -> int:
    unit = unit.upper() if unit else "PS"
    mult = {"FS": 0.001, "PS": 1, "NS": 1000, "US": 1_000_000,
            "MS": 1_000_000_000, "S": 1_000_000_000_000}
    return int(value * mult.get(unit, 1))


# ═══════════════════════════════════════════════════════════════════
# 主解析器
# ═══════════════════════════════════════════════════════════════════

class SimLogParser:
    def __init__(self, log_path: str, simulator: str = "auto"):
        self.log_path  = log_path
        self.simulator = simulator.lower()
        self._custom_patterns = self._load_custom_patterns()

    # ── Public ──────────────────────────────────────────────────────

    def parse(self) -> dict:
        path = Path(self.log_path)
        if not path.exists():
            raise FileNotFoundError(f"Log 文件不存在: {self.log_path}")

        with open(self.log_path, "r", errors="replace") as f:
            lines = f.readlines()

        sim_type = self.simulator
        if sim_type == "auto":
            sim_type = self._detect_simulator(lines)

        errors: List[SimError] = []
        errors += self._parse_vcs_assertions(lines)
        errors += self._parse_xce_assertions(lines)
        errors += self._parse_uvm(lines, sim_type)
        errors += self._parse_custom(lines)

        # 去重（相同 type + assertion_name + fail_time_ps）
        seen = set()
        unique_errors = []
        for e in errors:
            key = (e.error_type, e.assertion_name, e.fail_time_ps, e.message[:50])
            if key not in seen:
                seen.add(key)
                unique_errors.append(e)

        unique_errors.sort(key=lambda x: x.fail_time_ps)

        # 统计摘要
        counts: Dict[str, int] = {}
        for e in unique_errors:
            label = e.assertion_name or e.error_type
            counts[label] = counts.get(label, 0) + 1

        fatal_count = sum(1 for e in unique_errors if e.severity == "FATAL")
        error_count = sum(1 for e in unique_errors if e.severity == "ERROR")

        return {
            "log_file":          self.log_path,
            "simulator":         sim_type,
            "total_errors":      len(unique_errors),
            "fatal_count":       fatal_count,
            "error_count":       error_count,
            "unique_error_types": len(counts),
            "error_summary": [
                {"label": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda x: -x[1])
            ],
            "errors": [e.to_dict() for e in unique_errors],
        }

    # ── VCS assertion ────────────────────────────────────────────────

    def _parse_vcs_assertions(self, lines: List[str]) -> List[SimError]:
        results = []
        for i, line in enumerate(lines):
            m = _VCS_ASSERT_RE.search(line)
            if not m:
                continue
            e = SimError()
            e.error_type      = "ASSERTION_FAIL"
            e.severity        = "ERROR"
            e.simulator       = "vcs"
            e.sva_file        = m.group(1)
            e.line_num        = int(m.group(2))
            e.assertion_name  = m.group(3).split(".")[-1]
            e.start_time_ps   = _to_ps(float(m.group(4)), m.group(5))
            e.fail_time_ps    = _to_ps(float(m.group(6)), m.group(7))
            e.message         = line.strip()
            # 下一行可能是 Offending
            if i + 1 < len(lines):
                om = _VCS_OFFEND_RE.match(lines[i + 1])
                if om:
                    e.offending_expr = om.group(1)
            results.append(e)
        return results

    # ── Xcelium assertion ────────────────────────────────────────────

    def _parse_xce_assertions(self, lines: List[str]) -> List[SimError]:
        results = []
        for i, line in enumerate(lines):
            m = _XCE_ASSERT_RE.search(line)
            if not m:
                continue
            e = SimError()
            e.error_type      = "ASSERTION_FAIL"
            e.severity        = "ERROR"
            e.simulator       = "xcelium"
            e.sva_file        = m.group(1)
            e.line_num        = int(m.group(2))
            e.fail_time_ps    = _to_ps(float(m.group(3)), m.group(4))
            e.assertion_name  = m.group(5).split(".")[-1]
            e.start_time_ps   = _to_ps(float(m.group(6)), m.group(7)) if m.group(6) else e.fail_time_ps
            e.message         = line.strip()
            # 收集随后的 SVA 代码行
            sva_lines = []
            for j in range(i + 1, min(i + 5, len(lines))):
                nl = lines[j].rstrip()
                if nl.startswith("xmsim:") or not nl.strip():
                    break
                sva_lines.append(nl.strip())
            e.sva_code = " ".join(sva_lines)
            results.append(e)
        return results

    # ── UVM_ERROR / UVM_FATAL ────────────────────────────────────────

    def _parse_uvm(self, lines: List[str], sim_type: str) -> List[SimError]:
        results = []
        for line in lines:
            m = _UVM_RE.search(line)
            if not m:
                continue
            level = m.group(1).upper()
            if level not in UVM_PARSE_LEVELS:
                continue
            e = SimError()
            e.error_type   = level                    # UVM_ERROR / UVM_FATAL
            e.severity     = "FATAL" if level == "UVM_FATAL" else "ERROR"
            e.simulator    = sim_type
            e.sva_file     = m.group(2)
            e.line_num     = int(m.group(3))
            time_val       = float(m.group(4))
            time_unit      = m.group(5) or "ns"       # 默认 ns
            e.fail_time_ps = _to_ps(time_val, time_unit)
            e.start_time_ps = e.fail_time_ps
            e.uvm_tag      = m.group(7) or ""
            e.message      = m.group(8).strip() if m.group(8) else ""
            e.assertion_name = f"{level}_{e.uvm_tag}" if e.uvm_tag else level
            results.append(e)
        return results

    # ── 自定义格式 ───────────────────────────────────────────────────

    def _parse_custom(self, lines: List[str]) -> List[SimError]:
        if not self._custom_patterns:
            return []
        results = []
        for pattern in self._custom_patterns:
            try:
                compiled = re.compile(pattern["regex"])
            except re.error as ex:
                print(f"[WARN] custom_patterns.yaml 正则编译失败 ({pattern.get('name')}): {ex}")
                continue
            for line in lines:
                m = compiled.search(line)
                if not m:
                    continue
                gd = m.groupdict()
                e = SimError()
                e.error_type    = "CUSTOM"
                e.severity      = pattern.get("severity", "ERROR").upper()
                e.pattern_name  = pattern.get("name", "custom")
                e.sva_file      = gd.get("file", "")
                e.line_num      = int(gd["line"]) if "line" in gd and gd["line"] else 0
                e.message       = gd.get("message", line.strip())
                time_val        = float(gd["time"]) if "time" in gd and gd["time"] else 0
                time_unit       = gd.get("time_unit", "ns")
                e.fail_time_ps  = _to_ps(time_val, time_unit)
                e.start_time_ps = e.fail_time_ps
                e.assertion_name = e.pattern_name
                results.append(e)
        return results

    # ── 自动检测仿真器 ───────────────────────────────────────────────

    def _detect_simulator(self, lines: List[str]) -> str:
        header = "".join(lines[:300])
        if "xmsim:" in header or "xrun" in header.lower():
            return "xcelium"
        if "vcs" in header.lower() and ("simv" in header.lower() or "VCS" in header):
            return "vcs"
        if "xmsim: *E,ASRTST" in header:
            return "xcelium"
        if "started at" in header and "failed at" in header:
            return "vcs"
        return "unknown"

    # ── 加载 custom_patterns.yaml ────────────────────────────────────

    def _load_custom_patterns(self) -> list:
        try:
            with open(CUSTOM_PATTERNS_FILE, "r") as f:
                data = yaml.safe_load(f)
            return data.get("patterns") or []
        except FileNotFoundError:
            return []
        except Exception as ex:
            print(f"[WARN] 加载 custom_patterns.yaml 失败: {ex}")
            return []
