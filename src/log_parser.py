"""
log_parser.py
仿真日志解析器：负责从冗长的仿真输出中精准抓取 Assertion 失败和 UVM 错误。
支持的格式：
  - VCS 官方格式的 Assertion Fail
  - Xcelium 官方格式的 Assertion Fail
  - UVM 标准打印 (ERROR/FATAL)
  - 通过 custom_patterns.yaml 定义的任何自定义打印
"""

import re
from pathlib import Path
from typing import List, Dict, Any
import yaml

from config import CUSTOM_PATTERNS_FILE, UVM_PARSE_LEVELS

# ── 数据结构定义 ─────────────────────────────────────────────────────

class SimError:
    """统一存储各种来源的仿真报错"""
    def __init__(self):
        self.error_type: str  = ""      # ASSERTION_FAIL / UVM_ERROR / UVM_FATAL / CUSTOM
        self.severity: str    = "ERROR" # ERROR / FATAL
        self.simulator: str   = ""      # vcs / xcelium / unknown
        self.sva_file: str    = ""      # 报错发生的源文件
        self.line_num: int    = 0       # 行号
        self.start_time_ps: int = 0     # (SVA) 断言启动时间
        self.fail_time_ps: int  = 0     # 实际报错时间（关键：用于对齐波形）
        self.assertion_name: str = ""   # 断言名或标签名
        self.offending_expr: str = ""   # (VCS) 违例的表达式片段
        self.sva_code: str    = ""      # (Xcelium) SVA 源码片段
        self.uvm_tag: str     = ""      # UVM 组件 ID
        self.message: str     = ""      # 报错的完整文本描述
        self.pattern_name: str = ""     # 如果是自定义匹配，记录规则名称

    def to_dict(self) -> dict:
        """转化为 JSON 友好的字典格式供 AI 读取"""
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

# ── 正则表达式规则库 ───────────────────────────────────────────────

# 匹配 VCS 风格的断言失败
_VCS_ASSERT_RE = re.compile(
    r'"([^"]+)",\s*(\d+):\s+'          # 文件名, 行号
    r'([\w.]+):\s+'                    # 断言全名
    r'started at (\d+)(ps|ns|us|fs)\s+'# 启动时刻
    r'failed at (\d+)(ps|ns|us|fs)',   # 失败时刻
    re.IGNORECASE
)
# 匹配 VCS 的 Offending 表达式补充行
_VCS_OFFEND_RE = re.compile(r'^\s+Offending\s+\'(.+)\'')

# 匹配 Xcelium (xmsim) 风格的断言失败
_XCE_ASSERT_RE = re.compile(
    r'xmsim:\s+\*E,ASRTST\s+\(([^,]+),(\d+)\):\s+' # 文件, 行
    r'\(time\s+([\d.]+)\s+(PS|NS|US|FS)\)\s+'      # 失败时刻
    r'Assertion\s+([\w.]+)\s+has failed'           # 断言名
    r'(?:\s+\(\d+\s+cycles?,\s+starting\s+([\d.]+)\s+(PS|NS|US|FS)\))?', # 可选的启动时刻
    re.IGNORECASE
)

# 匹配 UVM 标准打印格式
_UVM_RE = re.compile(
    r'(UVM_ERROR|UVM_FATAL)\s+'          # 级别
    r'([^\s(]+)\((\d+)\)\s+'             # 文件(行号)
    r'@\s+([\d.]+)\s*(ps|ns|us|fs)?:\s+' # 时刻
    r'(\w+)\s+'                          # reporter
    r'(?:\[([^\]]+)\]\s+)?'              # [TAG] (可选)
    r'(.*)',                             # 消息体
    re.IGNORECASE
)

# ── 解析主逻辑 ────────────────────────────────────────────────────

def _to_ps(value: float, unit: str) -> int:
    """统一时间单位为 ps"""
    unit = unit.upper() if unit else "PS"
    mult = {"FS": 0.001, "PS": 1, "NS": 1000, "US": 1_000_000,
            "MS": 1_000_000_000, "S": 1_000_000_000_000}
    return int(value * mult.get(unit, 1))

class SimLogParser:
    def __init__(self, log_path: str, simulator: str = "auto"):
        self.log_path  = log_path
        self.simulator = simulator.lower()
        self._custom_patterns = self._load_custom_patterns()

    def parse(self) -> dict:
        """执行全量解析并返回去重后的报错摘要"""
        path = Path(self.log_path)
        if not path.exists():
            raise FileNotFoundError(f"Log 文件不存在: {self.log_path}")

        with open(self.log_path, "r", errors="replace") as f:
            lines = f.readlines()

        # 确定仿真器类型（用于后续逻辑微调）
        sim_type = self.simulator
        if sim_type == "auto":
            sim_type = self._detect_simulator(lines)

        errors: List[SimError] = []
        # 分别运行各路解析器
        errors += self._parse_vcs_assertions(lines)
        errors += self._parse_xce_assertions(lines)
        errors += self._parse_uvm(lines, sim_type)
        errors += self._parse_custom(lines)

        # 步骤：去重处理。防止同一行报错被多个正则重复抓取
        seen = set()
        unique_errors = []
        for e in errors:
            # 使用关键字段生成指纹
            key = (e.error_type, e.assertion_name, e.fail_time_ps, e.message[:50])
            if key not in seen:
                seen.add(key)
                unique_errors.append(e)

        # 按时间先后排序，符合人类 debug 习惯
        unique_errors.sort(key=lambda x: x.fail_time_ps)

        # 统计各类型报错的数量
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

    def _parse_vcs_assertions(self, lines: List[str]) -> List[SimError]:
        """解析 VCS 的断言输出及其 Offending 补充信息"""
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
            # 尝试抓取下一行的表达式细节
            if i + 1 < len(lines):
                om = _VCS_OFFEND_RE.match(lines[i + 1])
                if om:
                    e.offending_expr = om.group(1)
            results.append(e)
        return results

    def _parse_xce_assertions(self, lines: List[str]) -> List[SimError]:
        """解析 Xcelium 的断言输出及其附带的多行 SVA 代码"""
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
            # Xcelium 通常会把出错的代码行打印在后面几行
            sva_lines = []
            for j in range(i + 1, min(i + 5, len(lines))):
                nl = lines[j].rstrip()
                if nl.startswith("xmsim:") or not nl.strip():
                    break
                sva_lines.append(nl.strip())
            e.sva_code = " ".join(sva_lines)
            results.append(e)
        return results

    def _parse_uvm(self, lines: List[str], sim_type: str) -> List[SimError]:
        """解析标准的 UVM_ERROR 和 UVM_FATAL"""
        results = []
        for line in lines:
            m = _UVM_RE.search(line)
            if not m:
                continue
            level = m.group(1).upper()
            # 过滤掉不需要关注的级别（如 WARNING）
            if level not in UVM_PARSE_LEVELS:
                continue
            e = SimError()
            e.error_type   = level
            e.severity     = "FATAL" if level == "UVM_FATAL" else "ERROR"
            e.simulator    = sim_type
            e.sva_file     = m.group(2)
            e.line_num     = int(m.group(3))
            time_val       = float(m.group(4))
            time_unit      = m.group(5) or "ns"
            e.fail_time_ps = _to_ps(time_val, time_unit)
            e.start_time_ps = e.fail_time_ps
            e.uvm_tag      = m.group(7) or ""
            e.message      = m.group(8).strip() if m.group(8) else ""
            e.assertion_name = f"{level}_{e.uvm_tag}" if e.uvm_tag else level
            results.append(e)
        return results

    def _parse_custom(self, lines: List[str]) -> List[SimError]:
        """解析用户在 custom_patterns.yaml 中定义的个性化打印"""
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

    def _detect_simulator(self, lines: List[str]) -> str:
        """扫描日志前 300 行，自动识别是哪个公司的仿真器"""
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

    def _load_custom_patterns(self) -> list:
        """加载自定义正则匹配配置文件"""
        try:
            with open(CUSTOM_PATTERNS_FILE, "r") as f:
                data = yaml.safe_load(f)
            return data.get("patterns") or []
        except FileNotFoundError:
            return []
        except Exception as ex:
            print(f"[WARN] 加载 custom_patterns.yaml 失败: {ex}")
            return []
