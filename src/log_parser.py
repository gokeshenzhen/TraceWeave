"""
log_parser.py
支持两阶段仿真 log 分析：
  1. parse(): 返回分组摘要
  2. parse_failure_events(): 返回标准化 failure_event 列表
  3. get_error_context(): 按需提取指定报错附近的原始文本
"""

from __future__ import annotations

import hashlib
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


def _extract_structured_fields(line: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*([^\s,]+)", line):
        fields[match.group(1)] = match.group(2)
    return fields


@dataclass
class ParsedError:
    group_signature: str
    severity: str
    time_ps: int
    line_num: int
    message: str
    source_file: str | None = None
    source_line: int | None = None
    instance_path: str | None = None
    structured_fields: dict[str, Any] | None = None


class SimLogParser:
    def __init__(self, log_path: str, simulator: str):
        self.log_path = log_path
        self.simulator = simulator.lower()
        if self.simulator not in {"vcs", "xcelium"}:
            raise ValueError("simulator 必须为 'vcs' 或 'xcelium'")
        self._custom_patterns = self._load_custom_patterns()

    def parse(self, max_groups: int = DEFAULT_MAX_GROUPS) -> dict[str, Any]:
        return _build_summary(self.parse_failure_events(), self.log_path, self.simulator, max_groups)

    def parse_failure_events(self) -> list[dict[str, Any]]:
        path = Path(self.log_path)
        if not path.exists():
            raise FileNotFoundError(f"Log 文件不存在: {self.log_path}")

        events: list[dict[str, Any]] = []
        with path.open("r", errors="replace") as handle:
            for line_num, raw_line in enumerate(handle, 1):
                line = raw_line.rstrip("\n")
                line_lower = line.lower()
                error = self._try_match(line, line_lower, line_num)
                if error is None:
                    continue

                event_index = len(events) + 1
                event = {
                    "event_id": self._make_event_id(event_index, error),
                    "group_signature": error.group_signature,
                    "severity": error.severity,
                    "log_path": self.log_path,
                    "line": error.line_num,
                    "time_ps": error.time_ps,
                    "source_file": error.source_file,
                    "source_line": error.source_line,
                    "instance_path": error.instance_path,
                    "message_text": error.message,
                    "structured_fields": dict(error.structured_fields or {}),
                }
                events.append(event)
        return events

    def diff_against(self, new_log_path: str) -> dict[str, Any]:
        base_events = self.parse_failure_events()
        new_events = SimLogParser(new_log_path, self.simulator).parse_failure_events()
        return diff_failure_events(base_events, new_events)

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
                group_signature=f"ERROR: {line.strip()[:80]}",
                severity="ERROR",
                time_ps=_extract_time_ps(line),
                line_num=line_num,
                message=line.strip(),
                structured_fields=_extract_structured_fields(line),
            )

        return None

    def _match_vcs_assertion(self, line: str, line_num: int) -> ParsedError | None:
        match = _VCS_ASSERT_RE.search(line)
        if not match:
            return None
        assertion_name = match.group(3).split(".")[-1]
        fail_time_ps = _to_ps(float(match.group(6)), match.group(7))
        return ParsedError(
            group_signature=f"ASSERTION_FAIL: {assertion_name}",
            severity="ERROR",
            time_ps=fail_time_ps,
            line_num=line_num,
            message=line.strip(),
            source_file=match.group(1),
            source_line=int(match.group(2)),
            instance_path=match.group(3),
            structured_fields={
                "assertion_name": assertion_name,
                "start_time_ps": _to_ps(float(match.group(4)), match.group(5)),
                "fail_time_ps": fail_time_ps,
            },
        )

    def _match_xcelium_assertion(self, line: str, line_num: int) -> ParsedError | None:
        match = _XCE_ASSERT_RE.search(line)
        if not match:
            return None
        assertion_name = match.group(5).split(".")[-1]
        fail_time_ps = _to_ps(float(match.group(3)), match.group(4))
        return ParsedError(
            group_signature=f"ASSERTION_FAIL: {assertion_name}",
            severity="ERROR",
            time_ps=fail_time_ps,
            line_num=line_num,
            message=line.strip(),
            source_file=match.group(1),
            source_line=int(match.group(2)),
            instance_path=match.group(5),
            structured_fields={
                "assertion_name": assertion_name,
                "start_time_ps": (
                    _to_ps(float(match.group(6)), match.group(7)) if match.group(6) else None
                ),
                "fail_time_ps": fail_time_ps,
            },
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
            group_signature=signature,
            severity=severity,
            time_ps=time_ps,
            line_num=line_num,
            message=(match.group(8) or "").strip(),
            source_file=match.group(2),
            source_line=int(match.group(3)),
            instance_path=match.group(6),
            structured_fields={
                "reporter": match.group(6),
                "tag": tag or None,
            },
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
            structured_fields = {
                key: value for key, value in groups.items()
                if key not in {"message", "time", "time_unit", "source_file", "source_line", "instance_path"}
            }
            return ParsedError(
                group_signature=f"CUSTOM: {pattern.get('name', 'custom')}",
                severity=severity,
                time_ps=time_ps,
                line_num=line_num,
                message=groups.get("message", line.strip()),
                source_file=groups.get("source_file"),
                source_line=int(groups["source_line"]) if groups.get("source_line") else None,
                instance_path=groups.get("instance_path"),
                structured_fields=structured_fields,
            )
        return None

    def _make_event_id(self, event_index: int, error: ParsedError) -> str:
        raw = "|".join(
            [
                self.log_path,
                str(error.line_num),
                str(error.time_ps),
                error.group_signature,
                error.message,
            ]
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"failure-{event_index:06d}-{digest}"

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


def _build_summary(
    events: list[dict[str, Any]],
    log_path: str,
    simulator: str,
    max_groups: int,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for event in events:
        signature = event["group_signature"]
        group = groups.get(signature)
        if group is None:
            groups[signature] = {
                "signature": signature,
                "severity": event["severity"],
                "count": 1,
                "first_line": event["line"],
                "first_time_ps": event["time_ps"],
                "last_time_ps": event["time_ps"],
                "sample_event_id": event["event_id"],
                "sample_message": event["message_text"][:160],
                "source_file": event["source_file"],
                "source_line": event["source_line"],
                "instance_path": event["instance_path"],
            }
            continue

        group["count"] += 1
        if event["line"] < group["first_line"]:
            group["first_line"] = event["line"]
        if group["first_time_ps"] == 0 or (
            event["time_ps"] and event["time_ps"] < group["first_time_ps"]
        ):
            group["first_time_ps"] = event["time_ps"]
        if event["time_ps"] > group["last_time_ps"]:
            group["last_time_ps"] = event["time_ps"]

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

    fatal_count = sum(1 for event in events if event["severity"] == "FATAL")
    total_errors = len(events)
    first_error_line = events[0]["line"] if events else 0
    return {
        "log_file": log_path,
        "simulator": simulator,
        "total_errors": total_errors,
        "fatal_count": fatal_count,
        "error_count": total_errors - fatal_count,
        "unique_types": total_groups,
        "total_groups": total_groups,
        "truncated": truncated,
        "max_groups": max_groups,
        "first_error_line": first_error_line,
        "groups": group_list,
    }


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


def diff_failure_events(base_events: list[dict[str, Any]], new_events: list[dict[str, Any]]) -> dict[str, Any]:
    matched_base: set[int] = set()
    matched_new: set[int] = set()
    persistent_events: list[dict[str, Any]] = []

    for new_idx, new_event in enumerate(new_events):
        best_idx = _find_best_event_match(new_event, base_events, matched_base)
        if best_idx is None:
            continue
        matched_base.add(best_idx)
        matched_new.add(new_idx)
        base_event = base_events[best_idx]
        persistent_events.append(
            {
                "base_event": base_event,
                "new_event": new_event,
                "time_shift_ps": _time_shift_value(base_event, new_event),
                "group_changed": base_event["group_signature"] != new_event["group_signature"],
            }
        )

    resolved_events = [event for idx, event in enumerate(base_events) if idx not in matched_base]
    introduced_events = [event for idx, event in enumerate(new_events) if idx not in matched_new]
    changed_events = [
        item for item in persistent_events
        if item["group_changed"] or ((item["time_shift_ps"] or 0) > 0)
    ]

    comparison_notes = []
    if len(base_events) != len(new_events):
        comparison_notes.append(
            f"Total failure events changed from {len(base_events)} to {len(new_events)}."
        )
    if changed_events:
        comparison_notes.append(
            f"{len(changed_events)} persistent events changed timing or grouping."
        )

    return {
        "base_summary": _event_summary(base_events),
        "new_summary": _event_summary(new_events),
        "resolved_events": resolved_events,
        "persistent_events": persistent_events,
        "new_events": introduced_events,
        "comparison_notes": comparison_notes,
    }


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, int] = {}
    for event in events:
        groups[event["group_signature"]] = groups.get(event["group_signature"], 0) + 1
    return {
        "total_events": len(events),
        "unique_groups": len(groups),
        "groups": groups,
    }


def _find_best_event_match(
    target_event: dict[str, Any],
    candidates: list[dict[str, Any]],
    used_indexes: set[int],
) -> int | None:
    best_idx = None
    best_score = 0
    for idx, candidate in enumerate(candidates):
        if idx in used_indexes:
            continue
        score = _match_score(candidate, target_event)
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx if best_score >= 4 else None


def _match_score(base_event: dict[str, Any], new_event: dict[str, Any]) -> int:
    score = 0
    if base_event["group_signature"] == new_event["group_signature"]:
        score += 4
    if base_event.get("source_file") and base_event.get("source_file") == new_event.get("source_file"):
        score += 2
    if base_event.get("source_line") and base_event.get("source_line") == new_event.get("source_line"):
        score += 2
    if base_event.get("instance_path") and base_event.get("instance_path") == new_event.get("instance_path"):
        score += 2
    if _message_fingerprint(base_event["message_text"]) == _message_fingerprint(new_event["message_text"]):
        score += 2
    if _message_tokens(base_event["message_text"]) & _message_tokens(new_event["message_text"]):
        score += 1
    if not _time_shifted(base_event, new_event):
        score += 1
    return score


def _message_fingerprint(message: str) -> str:
    normalized = re.sub(r"\d+", "#", message.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _message_tokens(message: str) -> set[str]:
    return {
        token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", message.lower())
        if token not in {"error", "fatal", "expected", "got", "reporter"}
    }


def _time_shifted(base_event: dict[str, Any], new_event: dict[str, Any]) -> bool:
    shift = _time_shift_value(base_event, new_event)
    return shift is not None and shift > 0


def _time_shift_value(base_event: dict[str, Any], new_event: dict[str, Any]) -> int | None:
    base_time = base_event.get("time_ps") or 0
    new_time = new_event.get("time_ps") or 0
    if base_time <= 0 or new_time <= 0:
        return None
    return abs(new_time - base_time)
