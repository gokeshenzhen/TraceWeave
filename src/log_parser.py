"""
log_parser.py
支持两阶段仿真 log 分析：
  1. parse(): 单遍流式扫描，返回分组摘要
  2. get_error_context(): 按需提取指定报错附近的原始文本
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config import (
    CUSTOM_PATTERNS_FILE,
    DEFAULT_LOG_CONTEXT_AFTER,
    DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_MAX_GROUPS,
    UVM_PARSE_LEVELS,
)


_VCS_ASSERT_RE = re.compile(
    r'"([^"]+)",\s*(\d+):\s+'
    r'([\w.]+):\s+'
    r'started at (\d+)(ps|ns|us|fs)\s+'
    r'failed at (\d+)(ps|ns|us|fs)',
    re.IGNORECASE,
)

_XCE_ASSERT_RE = re.compile(
    r"xmsim:\s+\*E,ASRTST\s+\(([^,]+),(\d+)\):\s+"
    r"\(time\s+([\d.]+)\s+(PS|NS|US|FS)\)\s+"
    r"Assertion\s+([\w.]+)\s+has failed"
    r"(?:\s+\(\d+\s+cycles?,\s+starting\s+([\d.]+)\s+(PS|NS|US|FS)\))?",
    re.IGNORECASE,
)

_UVM_RE = re.compile(
    r"(UVM_ERROR|UVM_FATAL)\s+"
    r"([^\s(]+)\((\d+)\)\s+"
    r"@\s+([\d.]+)\s*(ps|ns|us|fs)?:\s+"
    r"(\w+)\s+"
    r"(?:\[([^\]]+)\]\s+)?"
    r"(.*)",
    re.IGNORECASE,
)

_GENERIC_ERROR_RE = re.compile(r"\berror\b", re.IGNORECASE)


def _to_ps(value: float, unit: str | None) -> int:
    unit_upper = (unit or "PS").upper()
    mult = {
        "FS": 0.001,
        "PS": 1,
        "NS": 1000,
        "US": 1_000_000,
        "MS": 1_000_000_000,
        "S": 1_000_000_000_000,
    }
    return int(value * mult.get(unit_upper, 1))


def _extract_time_ps(line: str) -> int:
    patterns = (
        re.compile(r"@\s*([\d.]+)\s*(ps|ns|us|fs)\b", re.IGNORECASE),
        re.compile(r"\(time\s+([\d.]+)\s+(PS|NS|US|FS)\)", re.IGNORECASE),
        re.compile(r"\b(\d+)\s*(ps|ns|us|fs)\b", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            return _to_ps(float(match.group(1)), match.group(2))
    return 0


def _has_error_keyword(line_lower: str) -> bool:
    keywords = ("error", "fatal", "uvm_error", "uvm_fatal", "failed at", "*e,asrtst")
    return any(keyword in line_lower for keyword in keywords)


@dataclass
class ParsedError:
    signature: str
    severity: str
    time_ps: int
    line_num: int
    message: str


class SimLogParser:
    def __init__(self, log_path: str, simulator: str):
        self.log_path = log_path
        self.simulator = simulator.lower()
        if self.simulator not in {"vcs", "xcelium"}:
            raise ValueError("simulator 必须为 'vcs' 或 'xcelium'")
        self._custom_patterns = self._load_custom_patterns()

    def parse(self, max_groups: int = DEFAULT_MAX_GROUPS) -> dict[str, Any]:
        path = Path(self.log_path)
        if not path.exists():
            raise FileNotFoundError(f"Log 文件不存在: {self.log_path}")

        groups: dict[str, dict[str, Any]] = {}
        total_errors = 0
        fatal_count = 0
        error_count = 0
        first_error_line = 0

        with path.open("r", errors="replace") as handle:
            for line_num, raw_line in enumerate(handle, 1):
                line = raw_line.rstrip("\n")
                line_lower = line.lower()
                if not _has_error_keyword(line_lower):
                    continue

                error = self._try_match(line, line_lower, line_num)
                if error is None:
                    continue

                total_errors += 1
                if error.severity == "FATAL":
                    fatal_count += 1
                else:
                    error_count += 1

                if first_error_line == 0:
                    first_error_line = line_num

                group = groups.get(error.signature)
                if group is None:
                    groups[error.signature] = {
                        "signature": error.signature,
                        "severity": error.severity,
                        "count": 1,
                        "first_line": error.line_num,
                        "first_time_ps": error.time_ps,
                        "last_time_ps": error.time_ps,
                    }
                    continue

                group["count"] += 1
                if error.line_num < group["first_line"]:
                    group["first_line"] = error.line_num
                if group["first_time_ps"] == 0 or (
                    error.time_ps and error.time_ps < group["first_time_ps"]
                ):
                    group["first_time_ps"] = error.time_ps
                if error.time_ps > group["last_time_ps"]:
                    group["last_time_ps"] = error.time_ps

        group_list = sorted(
            groups.values(),
            key=lambda item: (
                item["first_time_ps"] if item["first_time_ps"] > 0 else float("inf"),
                item["first_line"],
                item["signature"],
            ),
        )
        total_groups = len(group_list)
        truncated = total_groups > max_groups
        if truncated:
            group_list = group_list[:max_groups]

        return {
            "log_file": self.log_path,
            "simulator": self.simulator,
            "total_errors": total_errors,
            "fatal_count": fatal_count,
            "error_count": error_count,
            "unique_types": total_groups,
            "total_groups": total_groups,
            "truncated": truncated,
            "max_groups": max_groups,
            "first_error_line": first_error_line,
            "groups": group_list,
        }

    def get_error_context(
        self,
        line: int,
        before: int = DEFAULT_LOG_CONTEXT_BEFORE,
        after: int = DEFAULT_LOG_CONTEXT_AFTER,
    ) -> dict[str, Any]:
        return get_error_context(self.log_path, line, before, after)

    def _try_match(self, line: str, line_lower: str, line_num: int) -> ParsedError | None:
        if self.simulator == "vcs":
            error = self._match_vcs_assertion(line, line_num)
            if error is not None:
                return error
        elif self.simulator == "xcelium":
            error = self._match_xcelium_assertion(line, line_num)
            if error is not None:
                return error

        error = self._match_uvm(line, line_num)
        if error is not None:
            return error

        error = self._match_custom(line, line_num)
        if error is not None:
            return error

        if _GENERIC_ERROR_RE.search(line_lower):
            return ParsedError(
                signature=f"ERROR: {line.strip()[:80]}",
                severity="ERROR",
                time_ps=_extract_time_ps(line),
                line_num=line_num,
                message=line.strip(),
            )

        return None

    def _match_vcs_assertion(self, line: str, line_num: int) -> ParsedError | None:
        match = _VCS_ASSERT_RE.search(line)
        if not match:
            return None
        assertion_name = match.group(3).split(".")[-1]
        fail_time_ps = _to_ps(float(match.group(6)), match.group(7))
        return ParsedError(
            signature=f"ASSERTION_FAIL: {assertion_name}",
            severity="ERROR",
            time_ps=fail_time_ps,
            line_num=line_num,
            message=line.strip(),
        )

    def _match_xcelium_assertion(self, line: str, line_num: int) -> ParsedError | None:
        match = _XCE_ASSERT_RE.search(line)
        if not match:
            return None
        assertion_name = match.group(5).split(".")[-1]
        fail_time_ps = _to_ps(float(match.group(3)), match.group(4))
        return ParsedError(
            signature=f"ASSERTION_FAIL: {assertion_name}",
            severity="ERROR",
            time_ps=fail_time_ps,
            line_num=line_num,
            message=line.strip(),
        )

    def _match_uvm(self, line: str, line_num: int) -> ParsedError | None:
        match = _UVM_RE.search(line)
        if not match:
            return None
        level = match.group(1).upper()
        if level not in UVM_PARSE_LEVELS:
            return None
        severity = "FATAL" if level == "UVM_FATAL" else "ERROR"
        tag = match.group(7) or ""
        signature = f"{level} [{tag}]" if tag else level
        time_ps = _to_ps(float(match.group(4)), match.group(5) or "ns")
        return ParsedError(
            signature=signature,
            severity=severity,
            time_ps=time_ps,
            line_num=line_num,
            message=(match.group(8) or "").strip(),
        )

    def _match_custom(self, line: str, line_num: int) -> ParsedError | None:
        for pattern in self._custom_patterns:
            compiled = pattern.get("compiled")
            if compiled is None:
                continue
            match = compiled.search(line)
            if not match:
                continue
            groups = match.groupdict()
            severity = pattern.get("severity", "ERROR").upper()
            time_ps = 0
            if groups.get("time"):
                time_ps = _to_ps(float(groups["time"]), groups.get("time_unit", "ns"))
            return ParsedError(
                signature=f"CUSTOM: {pattern.get('name', 'custom')}",
                severity=severity,
                time_ps=time_ps,
                line_num=line_num,
                message=groups.get("message", line.strip()),
            )
        return None

    def _load_custom_patterns(self) -> list[dict[str, Any]]:
        try:
            with open(CUSTOM_PATTERNS_FILE, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            patterns = data.get("patterns") or []
        except FileNotFoundError:
            return []
        except Exception as ex:
            print(f"[WARN] 加载 custom_patterns.yaml 失败: {ex}")
            return []

        for pattern in patterns:
            try:
                pattern["compiled"] = re.compile(pattern["regex"])
            except (KeyError, re.error) as ex:
                print(f"[WARN] custom_patterns.yaml 正则编译失败 ({pattern.get('name')}): {ex}")
                pattern["compiled"] = None
        return patterns


def get_error_context(
    log_path: str,
    line: int,
    before: int = DEFAULT_LOG_CONTEXT_BEFORE,
    after: int = DEFAULT_LOG_CONTEXT_AFTER,
) -> dict[str, Any]:
    if line <= 0:
        raise ValueError("line 必须大于 0")
    if before < 0 or after < 0:
        raise ValueError("before/after 不能为负数")

    path = Path(log_path)
    if not path.exists():
        raise FileNotFoundError(f"Log 文件不存在: {log_path}")

    prev_lines: deque[tuple[int, str]] = deque(maxlen=before)
    post_lines: list[tuple[int, str]] = []
    center_line = None

    with path.open("r", errors="replace") as handle:
        for line_num, raw_line in enumerate(handle, 1):
            text = raw_line.rstrip("\n")
            if line_num < line:
                prev_lines.append((line_num, text))
                continue
            if line_num == line:
                center_line = (line_num, text)
                continue
            if line_num <= line + after:
                post_lines.append((line_num, text))
                continue
            break

    if center_line is None:
        raise ValueError(f"line {line} 超出文件范围")

    selected = list(prev_lines) + [center_line] + post_lines
    return {
        "log_file": log_path,
        "center_line": line,
        "start_line": selected[0][0],
        "end_line": selected[-1][0],
        "context": "\n".join(text for _, text in selected),
    }
