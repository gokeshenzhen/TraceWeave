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
    MAX_LOG_FILE_SIZE_FOR_MULTILINE,
    MAX_UVM_CONTINUATION_LINES,
    UVM_PARSE_LEVELS,
)
from .problem_hints import compute_problem_hints_from_events, event_has_x_or_z


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
    r"([\w.]+)\s+"
    r"(?:\[([^\]]+)\]\s+)?"
    r"(.*)",
    re.IGNORECASE,
)

_GENERIC_ERROR_RE = re.compile(r"\berror\b", re.IGNORECASE)
_UVM_TABLE_SEPARATOR_RE = re.compile(r"^\s*-{5,}\s*$")
_UVM_TABLE_HEADER_RE = re.compile(r"^\s*Name\s+Type\s+Size\s+Value\s*$")
_TIME_PATTERNS = (
    (re.compile(r"@\s*(?P<value>[\d.]+)\s*(?P<unit>ps|ns|us|ms|s|fs)\b", re.IGNORECASE), "exact", None),
    (re.compile(r"\(time\s+(?P<value>[\d.]+)\s+(?P<unit>PS|NS|US|MS|S|FS)\)", re.IGNORECASE), "exact", None),
    (re.compile(r"\[(?P<value>[\d.]+)\s*(?P<unit>ps|ns|us|ms|s|fs)\]", re.IGNORECASE), "exact", None),
    (re.compile(r"\btime\s*=\s*(?P<value>[\d.]+)\s*(?P<unit>ps|ns|us|ms|s|fs)\b", re.IGNORECASE), "exact", None),
    (re.compile(r"@\s*(?P<value>[\d.]+)\b", re.IGNORECASE), "exact", "ps"),
    (re.compile(r"\btime\s*=\s*(?P<value>[\d.]+)\b", re.IGNORECASE), "inferred", "ticks"),
)

SCHEMA_VERSION = "2.0"
CONTRACT_VERSION = "1.3"
FAILURE_EVENTS_SCHEMA_VERSION = "1.0"
_PHASE_BUCKETS = 4

_NON_RUNTIME_DIAGNOSTIC_TOKENS = (
    "xmvlog",
    "xmelab",
    "vlogan",
    "vhdlan",
    "parsing design file",
    "parsing included file",
    "recompiling module",
    "recompiling interface",
    "top level modules",
    "compiling source file",
    "elaborating the design",
    "*e,cu",
    "*e,vlog",
    "*e,syntax",
    "error-[",
)


@dataclass(frozen=True)
class TimeParseResult:
    raw_time: str | None
    raw_time_unit: str | None
    time_ps: int | None
    time_parse_status: str


def _normalize_time_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    normalized = unit.lower()
    if normalized == "fs":
        return "fs"
    if normalized in {"ps", "ns", "us", "ms", "s"}:
        return normalized
    if normalized in {"tick", "ticks"}:
        return "ticks"
    return "unknown"


def _to_ps(value: float, unit: str | None) -> int | None:
    unit_upper = (unit or "PS").upper()
    mult = {
        "FS": 0.001,
        "PS": 1,
        "NS": 1000,
        "US": 1_000_000,
        "MS": 1_000_000_000,
        "S": 1_000_000_000_000,
    }
    if unit_upper not in mult:
        return None
    return int(value * mult[unit_upper])


def _extract_time_info(line: str) -> TimeParseResult:
    for pattern, status, default_unit in _TIME_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        raw_time = match.group("value")
        unit = match.groupdict().get("unit") or default_unit
        normalized_unit = _normalize_time_unit(unit)
        if normalized_unit == "ticks":
            return TimeParseResult(
                raw_time=raw_time,
                raw_time_unit="ticks",
                time_ps=int(float(raw_time)),
                time_parse_status="inferred",
            )
        time_ps = _to_ps(float(raw_time), normalized_unit)
        if time_ps is not None:
            return TimeParseResult(
                raw_time=raw_time,
                raw_time_unit=normalized_unit,
                time_ps=time_ps,
                time_parse_status=status,
            )
    return TimeParseResult(
        raw_time=None,
        raw_time_unit=None,
        time_ps=None,
        time_parse_status="missing",
    )


def _has_error_keyword(line_lower: str) -> bool:
    keywords = ("error", "fatal", "uvm_error", "uvm_fatal", "failed at", "*e,asrtst")
    return any(keyword in line_lower for keyword in keywords)


def _is_non_runtime_diagnostic(line_lower: str) -> bool:
    if any(token in line_lower for token in _NON_RUNTIME_DIAGNOSTIC_TOKENS):
        return True
    return line_lower.startswith("file: ")


def _is_non_runtime_candidate(error: "ParsedError", line_lower: str) -> bool:
    if not _is_non_runtime_diagnostic(line_lower):
        return False
    signature = error.group_signature.lower()
    message = error.message.lower()
    if signature.startswith("assertion_fail:"):
        return False
    if "uvm_error" in signature or "uvm_fatal" in signature:
        return False
    if "*e,asrtst" in message:
        return False
    return True


def _extract_structured_fields(line: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*([^\s,]+)", line):
        fields[match.group(1)] = match.group(2)
    return fields


@dataclass
class ParsedError:
    group_signature: str
    severity: str
    time_ps: int | None
    line_num: int
    message: str
    raw_time: str | None = None
    raw_time_unit: str | None = None
    time_parse_status: str = "missing"
    source_file: str | None = None
    source_line: int | None = None
    instance_path: str | None = None
    structured_fields: dict[str, Any] | None = None
    continuation_text: str | None = None


class SimLogParser:
    def __init__(self, log_path: str, simulator: str):
        self.log_path = log_path
        self.simulator = simulator.lower()
        if self.simulator not in {"vcs", "xcelium"}:
            raise ValueError("simulator must be 'vcs' or 'xcelium'")
        self._custom_patterns = self._load_custom_patterns()

    def parse(self, max_groups: int = DEFAULT_MAX_GROUPS) -> dict[str, Any]:
        return _build_summary(self.parse_failure_events(), self.log_path, self.simulator, max_groups)

    def parse_failure_events(self) -> list[dict[str, Any]]:
        path = Path(self.log_path)
        if not path.exists():
            raise FileNotFoundError(f"Log file does not exist: {self.log_path}")

        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = 0
        multiline_enabled = file_size <= MAX_LOG_FILE_SIZE_FOR_MULTILINE

        with path.open("r", errors="replace") as handle:
            all_lines = handle.readlines()

        events: list[dict[str, Any]] = []
        i = 0
        while i < len(all_lines):
            line = all_lines[i].rstrip("\n")
            line_num = i + 1
            line_lower = line.lower()
            error = self._try_match(line, line_lower, line_num)
            if error is None:
                i += 1
                continue

            if multiline_enabled and error.group_signature.startswith(("UVM_ERROR", "UVM_FATAL")):
                continuation_lines, lines_consumed = self._collect_continuation(all_lines, i + 1)
                if continuation_lines:
                    error.continuation_text = "\n".join(continuation_lines)
                    expected, actual = _extract_uvm_table_diff(error.continuation_text)
                    if expected is not None or actual is not None:
                        if error.structured_fields is None:
                            error.structured_fields = {}
                        if expected is not None:
                            error.structured_fields["expected"] = expected
                        if actual is not None:
                            error.structured_fields["actual"] = actual
                i += lines_consumed

            event_index = len(events) + 1
            event = {
                "event_id": self._make_event_id(event_index, error),
                "group_signature": error.group_signature,
                "severity": error.severity,
                "log_path": self.log_path,
                "line": error.line_num,
                "time_ps": error.time_ps,
                "raw_time": error.raw_time,
                "raw_time_unit": error.raw_time_unit,
                "time_parse_status": error.time_parse_status,
                "source_file": error.source_file,
                "source_line": error.source_line,
                "instance_path": error.instance_path,
                "message_text": error.message,
                "structured_fields": dict(error.structured_fields or {}),
            }
            event = _enrich_runtime_event(event)
            events.append(event)
            i += 1
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
                return self._filter_runtime_candidate(error, line_lower)
        elif self.simulator == "xcelium":
            error = self._match_xcelium_assertion(line, line_num)
            if error is not None:
                return self._filter_runtime_candidate(error, line_lower)

        error = self._match_uvm(line, line_num)
        if error is not None:
            return self._filter_runtime_candidate(error, line_lower)

        error = self._match_custom(line, line_num)
        if error is not None:
            return self._filter_runtime_candidate(error, line_lower)

        if _GENERIC_ERROR_RE.search(line_lower):
            time_info = _extract_time_info(line)
            error = ParsedError(
                group_signature=f"ERROR: {line.strip()[:80]}",
                severity="ERROR",
                time_ps=time_info.time_ps,
                line_num=line_num,
                message=line.strip(),
                raw_time=time_info.raw_time,
                raw_time_unit=time_info.raw_time_unit,
                time_parse_status=time_info.time_parse_status,
                structured_fields=_extract_structured_fields(line),
            )
            return self._filter_runtime_candidate(error, line_lower)

        return None

    def _filter_runtime_candidate(self, error: ParsedError, line_lower: str) -> ParsedError | None:
        if _is_non_runtime_candidate(error, line_lower):
            return None
        return error

    def _match_vcs_assertion(self, line: str, line_num: int) -> ParsedError | None:
        match = _VCS_ASSERT_RE.search(line)
        if not match:
            return None
        assertion_name = match.group(3).split(".")[-1]
        fail_unit = _normalize_time_unit(match.group(7))
        fail_time_ps = _to_ps(float(match.group(6)), fail_unit)
        return ParsedError(
            group_signature=f"ASSERTION_FAIL: {assertion_name}",
            severity="ERROR",
            time_ps=fail_time_ps,
            line_num=line_num,
            message=line.strip(),
            raw_time=match.group(6),
            raw_time_unit=fail_unit,
            time_parse_status="exact",
            source_file=match.group(1),
            source_line=int(match.group(2)),
            instance_path=match.group(3),
            structured_fields={
                "assertion_name": assertion_name,
                "start_time_ps": _to_ps(float(match.group(4)), _normalize_time_unit(match.group(5))),
                "fail_time_ps": fail_time_ps,
            },
        )

    def _match_xcelium_assertion(self, line: str, line_num: int) -> ParsedError | None:
        match = _XCE_ASSERT_RE.search(line)
        if not match:
            return None
        assertion_name = match.group(5).split(".")[-1]
        fail_unit = _normalize_time_unit(match.group(4))
        fail_time_ps = _to_ps(float(match.group(3)), fail_unit)
        return ParsedError(
            group_signature=f"ASSERTION_FAIL: {assertion_name}",
            severity="ERROR",
            time_ps=fail_time_ps,
            line_num=line_num,
            message=line.strip(),
            raw_time=match.group(3),
            raw_time_unit=fail_unit,
            time_parse_status="exact",
            source_file=match.group(1),
            source_line=int(match.group(2)),
            instance_path=match.group(5),
            structured_fields={
                "assertion_name": assertion_name,
                "start_time_ps": (
                    _to_ps(float(match.group(6)), _normalize_time_unit(match.group(7))) if match.group(6) else None
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
        raw_unit = _normalize_time_unit(match.group(5) or "ns")
        time_ps = _to_ps(float(match.group(4)), raw_unit)
        return ParsedError(
            group_signature=signature,
            severity=severity,
            time_ps=time_ps,
            line_num=line_num,
            message=(match.group(8) or "").strip(),
            raw_time=match.group(4),
            raw_time_unit=raw_unit,
            time_parse_status="exact",
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
            raw_time = groups.get("time")
            if raw_time:
                raw_time_unit = _normalize_time_unit(groups.get("time_unit", "ns"))
                time_ps = _to_ps(float(raw_time), raw_time_unit)
                time_parse_status = "exact"
            else:
                time_info = _extract_time_info(line)
                raw_time = time_info.raw_time
                raw_time_unit = time_info.raw_time_unit
                time_ps = time_info.time_ps
                time_parse_status = time_info.time_parse_status
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
                raw_time=raw_time,
                raw_time_unit=raw_time_unit,
                time_parse_status=time_parse_status,
                source_file=groups.get("source_file"),
                source_line=int(groups["source_line"]) if groups.get("source_line") else None,
                instance_path=groups.get("instance_path"),
                structured_fields=structured_fields,
            )
        return None

    def _collect_continuation(self, all_lines: list[str], start_idx: int) -> tuple[list[str], int]:
        continuation: list[str] = []
        consecutive_empty = 0
        idx = start_idx

        while idx < len(all_lines) and len(continuation) < MAX_UVM_CONTINUATION_LINES:
            raw = all_lines[idx].rstrip("\n")

            if re.match(r"\s*UVM_(ERROR|FATAL|WARNING)\s", raw):
                break
            if _VCS_ASSERT_RE.search(raw):
                break
            if _XCE_ASSERT_RE.search(raw):
                break

            if raw.strip() == "":
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
                continuation.append(raw)
                idx += 1
                continue

            consecutive_empty = 0

            if not raw[0:1].isspace() and not raw.strip().startswith("---"):
                break

            continuation.append(raw)
            idx += 1

        while continuation and continuation[-1].strip() == "":
            continuation.pop()

        return continuation, idx - start_idx

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
            print(f"[WARN] Failed to load custom_patterns.yaml: {ex}")
            return []

        for pattern in patterns:
            try:
                pattern["compiled"] = re.compile(pattern["regex"])
            except (KeyError, re.error) as ex:
                print(f"[WARN] Failed to compile regex in custom_patterns.yaml ({pattern.get('name')}): {ex}")
                pattern["compiled"] = None
        return patterns


def _get_parser_capabilities() -> list[str]:
    return [
        "mixed_log_detection",
        "assertion_parsing",
        "uvm_parsing",
        "custom_pattern_parsing",
        "expected_actual_extraction",
        "transaction_hint_extraction",
        "uvm_table_multiline_extraction",
    ]


def _enrich_runtime_event(event: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(event)
    structured_fields = enriched.get("structured_fields") or {}
    message = enriched.get("message_text") or ""
    provenance_hints: dict[str, str] = {}
    expected, actual, expected_actual_provenance = _extract_expected_actual(message, structured_fields)
    transaction_hint, transaction_hint_provenance = _extract_transaction_hint(message, structured_fields)
    enriched["log_phase"] = "runtime"
    provenance_hints["log_phase"] = "derived"
    if enriched.get("time_ps") is not None:
        provenance_hints["time_ps"] = "observed"
    if enriched.get("source_file") is not None:
        provenance_hints["source_file"] = "observed"
    if enriched.get("source_line") is not None:
        provenance_hints["source_line"] = "observed"
    if enriched.get("instance_path") is not None:
        provenance_hints["instance_path"] = "observed"

    failure_source, failure_source_provenance = _classify_failure_source(enriched)
    failure_mechanism, failure_mechanism_provenance = _classify_failure_mechanism(enriched, expected, actual)
    enriched["failure_source"] = failure_source
    enriched["failure_mechanism"] = failure_mechanism
    enriched["transaction_hint"] = transaction_hint
    enriched["expected"] = expected
    enriched["actual"] = actual
    provenance_hints["failure_source"] = failure_source_provenance
    provenance_hints["failure_mechanism"] = failure_mechanism_provenance
    provenance_hints.update(expected_actual_provenance)
    if transaction_hint is not None and transaction_hint_provenance is not None:
        provenance_hints["transaction_hint"] = transaction_hint_provenance
    enriched["missing_fields"] = _compute_missing_fields(enriched)
    enriched["field_provenance"] = _compute_field_provenance(enriched, provenance_hints)
    return enriched


def _classify_failure_source(event: dict[str, Any]) -> tuple[str, str]:
    signature = (event.get("group_signature") or "").lower()
    message = (event.get("message_text") or "").lower()
    instance_path = (event.get("instance_path") or "").lower()
    source_file = (event.get("source_file") or "").lower()
    text = " ".join((signature, message, instance_path, source_file))

    if signature.startswith("assertion_fail:"):
        return "assertion", "derived"
    if any(token in text for token in ("scoreboard", "compare", "mismatch")):
        return "scoreboard", "derived"
    if any(token in text for token in ("checker", "monitor", "reporter", "uvm")):
        return "checker", "derived"
    if any(token in text for token in ("xmsim", "simulator", "vcs")):
        return "simulator", "derived"
    if message or signature.startswith("custom:") or signature.startswith("error:"):
        return "user_log", "derived"
    return "unknown", "heuristic"


def _classify_failure_mechanism(
    event: dict[str, Any],
    expected: str | None,
    actual: str | None,
) -> tuple[str, str]:
    text = " ".join(
        [
            event.get("group_signature") or "",
            event.get("message_text") or "",
            event.get("instance_path") or "",
        ]
    ).lower()
    if expected is not None and actual is not None:
        return "mismatch", "derived"
    if any(token in text for token in ("timeout", "timed out", "watchdog")):
        return "timeout", "derived"
    if any(token in text for token in ("xprop", " x ", " z ", "unknown value", "x-state")):
        return "xprop", "derived"
    if any(token in text for token in ("deadlock", "hang", "stuck", "stall")):
        return "deadlock", "derived"
    if any(token in text for token in ("assert", "protocol", "ready", "valid", "handshake")):
        return "protocol", "heuristic"
    if any(token in text for token in ("uvm_fatal", "tb", "reporter", "checker")):
        return "tb_error", "heuristic"
    return "unknown", "heuristic"


def _extract_expected_actual(
    message: str,
    structured_fields: dict[str, Any],
) -> tuple[str | None, str | None, dict[str, str]]:
    expected_keys = ("expected", "exp", "golden", "ref", "reference")
    actual_keys = ("actual", "act", "got", "observed")
    expected = _clean_extracted_value(_first_present(structured_fields, expected_keys))
    actual = _clean_extracted_value(_first_present(structured_fields, actual_keys))
    provenance: dict[str, str] = {}
    if expected is not None:
        provenance["expected"] = "observed"
    if actual is not None:
        provenance["actual"] = "observed"

    patterns = (
        re.compile(
            r"\bexpected\s*[:=]?\s*(?P<expected>\S+)\s+(?:but\s+)?(?:got|actual)\s*[:=]?\s*(?P<actual>\S+)",
            re.IGNORECASE,
        ),
        re.compile(r"\bexp(?:ected)?\s*[:=]\s*(?P<expected>[^,;]+?)\s*,\s*(?:act(?:ual)?|got)\s*[:=]\s*(?P<actual>[^,;]+)", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(message)
        if match:
            if expected is None:
                expected = _clean_extracted_value(match.group("expected"))
                if expected is not None:
                    provenance["expected"] = "observed"
            if actual is None:
                actual = _clean_extracted_value(match.group("actual"))
                if actual is not None:
                    provenance["actual"] = "observed"
            if expected is not None and actual is not None:
                break
    return expected, actual, provenance


def _extract_uvm_table_diff(text: str) -> tuple[str | None, str | None]:
    lines = text.split("\n")
    expect_start = None
    actual_start = None

    for index, line in enumerate(lines):
        lower = line.lower().strip()
        if "expect" in lower and any(token in lower for token in ("pkt", "packet", "trans", "item")):
            expect_start = index
        elif "actual" in lower and any(token in lower for token in ("pkt", "packet", "trans", "item")):
            actual_start = index

    if expect_start is None or actual_start is None:
        return None, None

    expect_fields = _parse_uvm_table_fields(lines[expect_start:actual_start])
    actual_fields = _parse_uvm_table_fields(lines[actual_start:])
    if not expect_fields or not actual_fields:
        return None, None

    for field_name, expect_value in expect_fields.items():
        actual_value = actual_fields.get(field_name)
        if actual_value is not None and actual_value != expect_value:
            return f"{field_name}={expect_value}", f"{field_name}={actual_value}"

    for field_name, actual_value in actual_fields.items():
        if field_name not in expect_fields:
            return None, f"{field_name}={actual_value}"

    return None, None


def _parse_uvm_table_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_table = False
    header_seen = False
    data_seen = False

    for line in lines:
        stripped = line.strip()

        if _UVM_TABLE_SEPARATOR_RE.match(line):
            if in_table and header_seen and data_seen:
                break
            in_table = True
            continue

        if not in_table:
            continue

        if _UVM_TABLE_HEADER_RE.match(line):
            header_seen = True
            continue

        if not header_seen:
            continue

        parts = stripped.split()
        if len(parts) < 4:
            continue

        field_name = parts[0]
        value = parts[-1]
        if value == "-" or value.startswith("@"):
            continue
        if field_name.startswith("[") and field_name.endswith("]"):
            continue

        data_seen = True
        fields[field_name] = value

    return fields


def _extract_transaction_hint(
    message: str,
    structured_fields: dict[str, Any],
) -> tuple[str | None, str | None]:
    hint_keys = ("transaction", "transaction_id", "txn", "txn_id", "seq", "seq_id", "opcode", "op")
    hint = _first_present(structured_fields, hint_keys)
    if hint is not None:
        return str(hint), "observed"

    patterns = (
        re.compile(r"\b(?:txn|transaction|seq|sequence|op|opcode)(?:_id)?\s*[:=]\s*([A-Za-z0-9_.-]+)", re.IGNORECASE),
        re.compile(r"\btx(?:n)?#([A-Za-z0-9_.-]+)", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(message)
        if match:
            return match.group(1), "observed"
    return None, None


def _compute_missing_fields(event: dict[str, Any]) -> list[str]:
    relevant: list[str] = ["time_ps", "failure_source", "failure_mechanism"]
    failure_source = event.get("failure_source")
    message_text = (event.get("message_text") or "").lower()
    group_signature = (event.get("group_signature") or "").lower()

    if failure_source in {"assertion", "scoreboard", "checker"} or group_signature.startswith("assertion_fail:"):
        relevant.extend(["source_file", "source_line", "instance_path"])
    elif event.get("source_file") is not None or event.get("source_line") is not None:
        relevant.extend(["source_file", "source_line"])
    elif event.get("instance_path") is not None:
        relevant.append("instance_path")

    if _comparison_fields_relevant(event):
        relevant.extend(["expected", "actual"])
    if _transaction_hint_relevant(event):
        relevant.append("transaction_hint")

    deduped = list(dict.fromkeys(relevant))
    return [field for field in deduped if event.get(field) is None]


def _comparison_fields_relevant(event: dict[str, Any]) -> bool:
    mechanism = event.get("failure_mechanism")
    text = (event.get("message_text") or "").lower()
    return mechanism == "mismatch" or bool(
        re.search(r"\b(expected|actual|got|compare|mismatch)\b", text)
    )


def _transaction_hint_relevant(event: dict[str, Any]) -> bool:
    text = " ".join(
        [
            event.get("group_signature") or "",
            event.get("message_text") or "",
        ]
    ).lower()
    return any(token in text for token in ("txn", "transaction", "opcode", "seq"))


def _compute_field_provenance(event: dict[str, Any], provenance_hints: dict[str, str]) -> dict[str, str]:
    semantic_fields = (
        "log_phase",
        "time_ps",
        "source_file",
        "source_line",
        "instance_path",
        "failure_source",
        "failure_mechanism",
        "semantic_phase",
        "transaction_hint",
        "expected",
        "actual",
    )
    missing = set(event.get("missing_fields", []))
    provenance: dict[str, str] = {}
    for field in semantic_fields:
        if field in missing:
            continue
        if event.get(field) is None:
            continue
        provenance[field] = provenance_hints.get(field, "derived")
    return provenance


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key)
        if value is not None:
            return value
    return None


def _stringify_optional(value: Any) -> str | None:
    return None if value is None else str(value)


def _clean_extracted_value(value: Any) -> str | None:
    text = _stringify_optional(value)
    if text is None:
        return None
    return text.strip().rstrip(",;")


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
        if group["first_time_ps"] is None or (
            event["time_ps"] is not None and event["time_ps"] < group["first_time_ps"]
        ):
            group["first_time_ps"] = event["time_ps"]
        if group["last_time_ps"] is None or (
            event["time_ps"] is not None and event["time_ps"] > group["last_time_ps"]
        ):
            group["last_time_ps"] = event["time_ps"]

    group_list = sorted(
        groups.values(),
        key=lambda item: (
            item["first_time_ps"] if item["first_time_ps"] is not None else float("inf"),
            item["first_line"],
            item["signature"],
        ),
    )
    total_groups = len(group_list)
    truncated = total_groups > max_groups
    sampling_strategy = "time_order"
    if truncated:
        group_list = _sample_groups_phase_stratified(group_list, max_groups)
        sampling_strategy = "phase_stratified"
    for group_index, item in enumerate(group_list):
        item["group_index"] = group_index

    fatal_count = sum(1 for event in events if event["severity"] == "FATAL")
    total_errors = len(events)
    first_error_line = events[0]["line"] if events else 0
    return {
        "log_file": log_path,
        "simulator": simulator,
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "failure_events_schema_version": FAILURE_EVENTS_SCHEMA_VERSION,
        "parser_capabilities": _get_parser_capabilities(),
        "runtime_total_errors": total_errors,
        "runtime_fatal_count": fatal_count,
        "runtime_error_count": total_errors - fatal_count,
        "unique_types": total_groups,
        "total_groups": total_groups,
        "truncated": truncated,
        "max_groups": max_groups,
        "first_error_line": first_error_line,
        "groups": group_list,
        "sampling_strategy": sampling_strategy,
        **_find_previous_log_hints(log_path),
    }


def _sample_groups_phase_stratified(
    groups: list[dict[str, Any]],
    max_groups: int,
) -> list[dict[str, Any]]:
    if len(groups) <= max_groups:
        return groups
    if max_groups <= 0:
        return []

    phase_buckets = _bucket_groups_by_time_phase(groups)
    quota_per_phase = max(1, max_groups // max(1, len(phase_buckets)))
    selected_ids: set[int] = set()
    selected_signatures: set[str] = set()
    selected: list[dict[str, Any]] = []

    for phase_groups in phase_buckets:
        phase_count = 0
        for group in phase_groups:
            if len(selected) >= max_groups or phase_count >= quota_per_phase:
                break
            gid = id(group)
            signature = group["signature"]
            if gid in selected_ids or signature in selected_signatures:
                continue
            selected.append(group)
            selected_ids.add(gid)
            selected_signatures.add(signature)
            phase_count += 1

    phase_index = 0
    while len(selected) < max_groups and phase_buckets:
        phase_groups = phase_buckets[phase_index % len(phase_buckets)]
        added = False
        for group in phase_groups:
            gid = id(group)
            if gid in selected_ids:
                continue
            selected.append(group)
            selected_ids.add(gid)
            selected_signatures.add(group["signature"])
            added = True
            break
        if not added:
            if all(id(group) in selected_ids for group in phase_groups):
                if all(all(id(group) in selected_ids for group in bucket) for bucket in phase_buckets):
                    break
        phase_index += 1

    return sorted(
        selected[:max_groups],
        key=lambda item: (
            item["first_time_ps"] if item["first_time_ps"] is not None else float("inf"),
            item["first_line"],
            item["signature"],
        ),
    )


def _bucket_groups_by_time_phase(groups: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    known_times = [group["first_time_ps"] for group in groups if group.get("first_time_ps") is not None]
    if not known_times:
        return [groups]
    start = min(known_times)
    end = max(group.get("last_time_ps") or group.get("first_time_ps") or start for group in groups)
    if end <= start:
        return [groups]

    span = end - start
    phase_width = max(1, span // _PHASE_BUCKETS)
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(_PHASE_BUCKETS)]
    for group in groups:
        time_ps = group.get("first_time_ps")
        if time_ps is None:
            buckets[-1].append(group)
            continue
        phase = min(_PHASE_BUCKETS - 1, max(0, (time_ps - start) // phase_width))
        buckets[int(phase)].append(group)
    return [bucket for bucket in buckets if bucket]


def _find_previous_log_hints(log_path: str) -> dict[str, Any]:
    current = Path(log_path)
    try:
        current_stat = current.stat()
        siblings = sorted(
            (
                path for path in current.parent.glob("*.log")
                if path.resolve() != current.resolve()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        current_stat = None
        siblings = []

    candidates: list[str] = []
    for path in siblings:
        try:
            if current_stat is not None and path.stat().st_mtime >= current_stat.st_mtime:
                continue
        except OSError:
            continue
        candidates.append(str(path.resolve()))
        if len(candidates) >= 3:
            break
    return {
        "previous_log_detected": bool(candidates),
        "candidate_previous_logs": candidates,
        "suggested_followup_tool": "diff_sim_failure_results" if candidates else None,
    }


def get_error_context(
    log_path: str,
    line: int,
    before: int = DEFAULT_LOG_CONTEXT_BEFORE,
    after: int = DEFAULT_LOG_CONTEXT_AFTER,
) -> dict[str, Any]:
    if line <= 0:
        raise ValueError("line must be greater than 0")
    if before < 0 or after < 0:
        raise ValueError("before/after must be non-negative")

    path = Path(log_path)
    if not path.exists():
        raise FileNotFoundError(f"Log file does not exist: {log_path}")

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
        raise ValueError(f"line {line} is outside the file range")

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
    base_hints = compute_problem_hints_from_events(base_events)
    new_hints = compute_problem_hints_from_events(new_events)

    for new_idx, new_event in enumerate(new_events):
        best_idx = _find_best_event_match(new_event, base_events, matched_base)
        if best_idx is None:
            continue
        matched_base.add(best_idx)
        matched_new.add(new_idx)
        base_event = base_events[best_idx]
        persistent_events.append(_analyze_persistent_event(base_event, new_event))

    resolved_events = [event for idx, event in enumerate(base_events) if idx not in matched_base]
    introduced_events = [event for idx, event in enumerate(new_events) if idx not in matched_new]
    hints_comparison = _build_hints_comparison(base_hints, new_hints)
    changed_events = [
        item for item in persistent_events
        if item["group_changed"] or ((item["time_shift_ps"] or 0) != 0)
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
    if hints_comparison["x_resolved"]:
        comparison_notes.append("X propagation no longer present in new run.")
    if hints_comparison["x_introduced"]:
        comparison_notes.append("X propagation appeared in new run.")
    if hints_comparison["z_resolved"]:
        comparison_notes.append("Z (high-impedance) no longer present in new run.")
    if hints_comparison["z_introduced"]:
        comparison_notes.append("Z (high-impedance) appeared in new run.")
    if hints_comparison["first_error_time_shift_ps"] is not None:
        shift = hints_comparison["first_error_time_shift_ps"]
        if shift != 0:
            direction = "later" if shift > 0 else "earlier"
            comparison_notes.append(
                f"First failure time shifted {abs(shift)} ps {direction}."
            )

    mechanism_changed_count = sum(1 for item in persistent_events if item["mechanism_changed"])
    if mechanism_changed_count > 0:
        comparison_notes.append(
            f"{mechanism_changed_count} persistent events changed failure mechanism."
        )

    x_to_deterministic_count = sum(1 for item in persistent_events if item["x_to_deterministic"])
    if x_to_deterministic_count > 0:
        comparison_notes.append(
            f"{x_to_deterministic_count} persistent events transitioned from X/Z to deterministic values."
        )

    return {
        "base_summary": _event_summary(base_events),
        "new_summary": _event_summary(new_events),
        "problem_hints_comparison": hints_comparison,
        "resolved_events": resolved_events,
        "persistent_events": persistent_events,
        "new_events": introduced_events,
        "comparison_notes": comparison_notes,
        "convergence_summary": _build_convergence_summary(
            resolved_events,
            persistent_events,
            introduced_events,
            hints_comparison,
        ),
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


def _build_hints_comparison(base_hints, new_hints) -> dict[str, Any]:
    x_resolved = base_hints.has_x and not new_hints.has_x
    z_resolved = base_hints.has_z and not new_hints.has_z
    x_introduced = not base_hints.has_x and new_hints.has_x
    z_introduced = not base_hints.has_z and new_hints.has_z

    pattern_changed = base_hints.error_pattern != new_hints.error_pattern
    pattern_transition = None
    if pattern_changed and base_hints.error_pattern and new_hints.error_pattern:
        pattern_transition = f"{base_hints.error_pattern} → {new_hints.error_pattern}"

    time_shift = None
    direction = None
    base_time = base_hints.first_error_time_ps
    new_time = new_hints.first_error_time_ps
    if base_time is not None and new_time is not None:
        time_shift = new_time - base_time
        if time_shift > 0:
            direction = "later"
        elif time_shift < 0:
            direction = "earlier"
        else:
            direction = "unchanged"

    return {
        "base": base_hints.model_dump(),
        "new": new_hints.model_dump(),
        "x_resolved": x_resolved,
        "z_resolved": z_resolved,
        "x_introduced": x_introduced,
        "z_introduced": z_introduced,
        "error_pattern_changed": pattern_changed,
        "error_pattern_transition": pattern_transition,
        "first_error_time_shift_ps": time_shift,
        "first_error_time_direction": direction,
    }


def _analyze_persistent_event(base_event: dict[str, Any], new_event: dict[str, Any]) -> dict[str, Any]:
    time_shift_ps = _time_shift_value(base_event, new_event)
    time_direction = None
    if time_shift_ps is not None and time_shift_ps != 0:
        time_direction = "later" if time_shift_ps > 0 else "earlier"

    group_changed = base_event["group_signature"] != new_event["group_signature"]
    base_mechanism = base_event.get("failure_mechanism")
    new_mechanism = new_event.get("failure_mechanism")
    mechanism_changed = base_mechanism != new_mechanism
    mechanism_transition = None
    if mechanism_changed and base_mechanism and new_mechanism:
        mechanism_transition = f"{base_mechanism} → {new_mechanism}"

    return {
        "base_event": base_event,
        "new_event": new_event,
        "time_shift_ps": time_shift_ps,
        "time_direction": time_direction,
        "group_changed": group_changed,
        "mechanism_changed": mechanism_changed,
        "mechanism_transition": mechanism_transition,
        "x_to_deterministic": _detect_x_to_deterministic(base_event, new_event),
        "value_changed": _detect_value_changed(base_event, new_event),
    }


def _detect_x_to_deterministic(base_event: dict[str, Any], new_event: dict[str, Any]) -> bool:
    base_x, base_z = event_has_x_or_z(base_event)
    new_x, new_z = event_has_x_or_z(new_event)
    return (base_x or base_z) and not (new_x or new_z)


def _detect_value_changed(base_event: dict[str, Any], new_event: dict[str, Any]) -> bool:
    for field in ("expected", "actual"):
        if base_event.get(field) != new_event.get(field):
            return True
    return False


def _build_convergence_summary(
    resolved_events: list[dict[str, Any]],
    persistent_events: list[dict[str, Any]],
    introduced_events: list[dict[str, Any]],
    hints_comparison: dict[str, Any],
) -> str | None:
    parts = []

    if len(resolved_events) > 0 and len(introduced_events) == 0:
        parts.append(f"{len(resolved_events)} failures resolved, no new failures")
    elif len(resolved_events) > 0 and len(introduced_events) > 0:
        parts.append(f"{len(resolved_events)} resolved, {len(introduced_events)} new")
    elif len(introduced_events) > 0:
        parts.append(f"{len(introduced_events)} new failures introduced")

    if hints_comparison["x_resolved"]:
        parts.append("X propagation resolved")
    if hints_comparison["x_introduced"]:
        parts.append("X propagation introduced")

    if hints_comparison["error_pattern_transition"]:
        parts.append(f"error pattern: {hints_comparison['error_pattern_transition']}")

    if hints_comparison["first_error_time_direction"] == "later":
        parts.append("first failure shifted later")
    elif hints_comparison["first_error_time_direction"] == "earlier":
        parts.append("first failure shifted earlier")

    return "; ".join(parts) if parts else None


def _time_shifted(base_event: dict[str, Any], new_event: dict[str, Any]) -> bool:
    shift = _time_shift_value(base_event, new_event)
    return shift is not None and shift != 0


def _time_shift_value(base_event: dict[str, Any], new_event: dict[str, Any]) -> int | None:
    base_time = base_event.get("time_ps")
    new_time = new_event.get("time_ps")
    if base_time is None or new_time is None:
        return None
    return new_time - base_time
