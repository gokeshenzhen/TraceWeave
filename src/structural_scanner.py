"""
structural_scanner.py
对编译文件列表中的 RTL/TB 源码做 Scope 1 正则静态结构风险扫描。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import os
import re
from typing import Iterable

from .compile_log_parser import parse_compile_log


SUPPORTED_SCAN_SCOPE = "scope1"
ALL_CATEGORIES = [
    "slice_overlap",
    "narrow_condition_injection",
    "multi_drive",
    "incomplete_case",
    "magic_condition",
]

_MODULE_BLOCK_RE = re.compile(r"^\s*module\s+(\w+)\b(.*?)(?=^\s*endmodule\b)", re.IGNORECASE | re.MULTILINE | re.DOTALL)
_INST_PORT_SLICE_RE = re.compile(r"\.(\w+)\s*\(\s*(\w+)\s*\[(\d+):(\d+)\]\s*\)")
_INSTANCE_BLOCK_RE = re.compile(r"(?P<module>\w+)\s+(?P<inst>\w+)\s*\((?P<body>.*?)\);", re.DOTALL)
_ASSIGN_SLICE_RE = re.compile(r"assign\s+(\w+)\s*\[(\d+):(\d+)\]\s*=", re.IGNORECASE)
_ASSIGN_FULL_RE = re.compile(r"\bassign\s+(\w+)\s*=", re.IGNORECASE)
_ASSIGN_WITH_SLICE_RE = re.compile(r"\bassign\s+\w+\s*\[", re.IGNORECASE)
_CASE_START_RE = re.compile(r"\b(case[zx]?)\s*\(", re.IGNORECASE)
_CASE_OR_ENDCASE_RE = re.compile(r"\b(case[zx]?|endcase)\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bdefault\s*:", re.IGNORECASE)
_FULL_CASE_COMMENT_RE = re.compile(r"synopsys\s+full_case", re.IGNORECASE)
_MAGIC_COMPARE_RE = re.compile(
    r"(?P<lhs>[A-Za-z_]\w*(?:\s*\[[^\]]+\])?)\s*(?P<op>==|!=)\s*(?P<lit>\d+'[bBhHdDoO][0-9a-fA-F_xXzZ]+)"
)
_PARAM_LINE_RE = re.compile(r"\b(localparam|parameter)\b", re.IGNORECASE)
_ASSIGN_STATEMENT_RE = re.compile(r"\bassign\b", re.IGNORECASE)
_PROCEDURAL_BLOCK_RE = re.compile(r"\b(always(?:_comb|_ff|_latch)?|initial)\b", re.IGNORECASE)
_BLOCK_TOKEN_RE = re.compile(
    r"\b(always(?:_comb|_ff|_latch)?|initial|begin|end|case[zx]?|endcase|fork|join(?:_any|_none)?)\b",
    re.IGNORECASE,
)
_CASE_ITEM_LINE_RE = re.compile(r"^\s*\d+'[bBhHdDoO][0-9a-fA-F_xXzZ]+\s*.*:")
_DEFAULT_LINE_RE = re.compile(r"^\s*default\s*:", re.IGNORECASE)
_ZERO_LITERAL_RE = re.compile(r"(\d+)'b(0+)", re.IGNORECASE)
_MODULE_HEADER_RE = re.compile(
    r"^\s*module\s+(?P<name>\w+)(?:\s*#\s*\(.*?\))?\s*\((?P<ports>.*?)\)\s*;",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_PORT_NAME_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
_CONDITION_OP_RE = re.compile(r"(==|!=|<=|>=|&&|\|\||[<>!])")


@dataclass(frozen=True)
class _Risk:
    type: str
    file: str
    line: int
    module: str | None
    risk_level: str
    detail: str
    evidence: list[str]


@dataclass(frozen=True)
class _SliceUse:
    target: str
    lo: int
    hi: int
    line: int
    evidence: str


def scan_structural_risks(
    compile_log: str,
    simulator: str,
    scan_scope: str = SUPPORTED_SCAN_SCOPE,
    categories: list[str] | None = None,
) -> dict:
    if scan_scope != SUPPORTED_SCAN_SCOPE:
        raise ValueError(f"scan_scope only supports {SUPPORTED_SCAN_SCOPE}")

    categories_scanned = _normalize_categories(categories)
    compile_result = parse_compile_log(compile_log, simulator)
    file_entries = compile_result.get("files", {}).get("user", [])
    module_port_dirs = _build_module_port_directions(file_entries)

    risks: list[_Risk] = []
    skipped_files: list[str] = []
    files_scanned = 0

    for entry in file_entries:
        path = entry["path"]
        if not os.path.exists(path):
            skipped_files.append(path)
            continue
        if not _should_scan_file(path):
            continue
        files_scanned += 1
        risks.extend(_scan_file(path, categories_scanned, module_port_dirs))

    ordered_risks = sorted(risks, key=lambda item: (item.file, item.line, item.type, item.detail))
    return {
        "scan_scope": scan_scope,
        "files_scanned": files_scanned,
        "total_risks": len(ordered_risks),
        "risks": [risk.__dict__ for risk in ordered_risks],
        "categories_scanned": categories_scanned,
        "skipped_files": skipped_files,
    }


def _normalize_categories(categories: list[str] | None) -> list[str]:
    if categories is None:
        return list(ALL_CATEGORIES)
    normalized: list[str] = []
    unknown: list[str] = []
    for item in categories:
        if item in ALL_CATEGORIES and item not in normalized:
            normalized.append(item)
        elif item not in ALL_CATEGORIES:
            unknown.append(item)
    if unknown:
        raise ValueError(f"Unknown categories: {', '.join(sorted(unknown))}")
    return normalized


def _should_scan_file(path: str) -> bool:
    return path.lower().endswith((".sv", ".svh", ".v", ".vh"))


def _scan_file(
    path: str,
    categories: list[str],
    module_port_dirs: dict[str, dict[str, str]],
) -> list[_Risk]:
    with open(path, "r", errors="replace") as handle:
        raw_text = handle.read()
    text = _strip_comments_keep_lines(raw_text)
    source_lines = raw_text.splitlines()

    risks: list[_Risk] = []
    for module_name, module_text, module_start_line in _iter_modules(text):
        if "slice_overlap" in categories:
            risks.extend(_scan_slice_overlap(path, module_name, module_text, module_start_line, module_port_dirs))
        if "multi_drive" in categories:
            risks.extend(_scan_multi_drive(path, module_name, module_text, module_start_line))
        if "incomplete_case" in categories:
            risks.extend(_scan_incomplete_case(path, module_name, module_text, module_start_line, source_lines))
    if "narrow_condition_injection" in categories:
        risks.extend(_scan_narrow_condition_injection(path, text))
    if "magic_condition" in categories:
        risks.extend(_scan_magic_condition(path, text))
    return risks


def _strip_comments_keep_lines(text: str) -> str:
    def replace_block(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    text = re.sub(r"/\*.*?\*/", replace_block, text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _iter_modules(text: str) -> Iterable[tuple[str, str, int]]:
    for match in _MODULE_BLOCK_RE.finditer(text):
        module_name = match.group(1)
        module_text = match.group(2)
        start_line = text.count("\n", 0, match.start()) + 1
        yield module_name, module_text, start_line


def _line_number(text: str, pos: int, base_line: int = 1) -> int:
    return base_line + text.count("\n", 0, pos)


def _normalize_slice(a: str, b: str) -> tuple[int, int]:
    lo = min(int(a), int(b))
    hi = max(int(a), int(b))
    return lo, hi


def _scan_slice_overlap(
    path: str,
    module_name: str,
    module_text: str,
    module_start_line: int,
    module_port_dirs: dict[str, dict[str, str]],
) -> list[_Risk]:
    slices_by_target: dict[str, list[_SliceUse]] = defaultdict(list)
    output_slices_by_target: dict[str, list[_SliceUse]] = defaultdict(list)
    for inst_match in _INSTANCE_BLOCK_RE.finditer(module_text):
        instance_module = inst_match.group("module")
        body = inst_match.group("body")
        port_dirs = module_port_dirs.get(instance_module, {})
        for match in _INST_PORT_SLICE_RE.finditer(body):
            port_name, target, lhs, rhs = match.groups()
            lo, hi = _normalize_slice(lhs, rhs)
            line = _line_number(module_text, inst_match.start("body") + match.start(), module_start_line)
            snippet = match.group(0).strip()
            use = _SliceUse(target, lo, hi, line, f"port {port_name}: {snippet}")
            if port_dirs.get(port_name) == "output":
                output_slices_by_target[target].append(use)
            else:
                slices_by_target[target].append(use)
    for match in _ASSIGN_SLICE_RE.finditer(module_text):
        target, lhs, rhs = match.groups()
        lo, hi = _normalize_slice(lhs, rhs)
        line = _line_number(module_text, match.start(), module_start_line)
        snippet = match.group(0).strip()
        slices_by_target[target].append(_SliceUse(target, lo, hi, line, f"assign: {snippet}"))

    risks: list[_Risk] = []
    for target, uses in slices_by_target.items():
        if len(uses) < 2:
            continue
        ordered = sorted(uses, key=lambda item: (item.lo, item.hi, item.line))
        findings: list[str] = []
        for prev, curr in zip(ordered, ordered[1:]):
            if curr.lo <= prev.hi:
                overlap_lo = curr.lo
                overlap_hi = min(prev.hi, curr.hi)
                if overlap_lo == overlap_hi:
                    findings.append(f"overlap at bit {overlap_lo}")
                else:
                    findings.append(f"overlap at bits {overlap_lo}:{overlap_hi}")
            if curr.lo > prev.hi + 1:
                gap_lo = prev.hi + 1
                gap_hi = curr.lo - 1
                if gap_lo == gap_hi:
                    findings.append(f"gap at bit {gap_lo}")
                else:
                    findings.append(f"gap at bits {gap_lo}:{gap_hi}")
        if findings:
            line = ordered[0].line
            detail = f"Target {target} has slice coverage issues: {'; '.join(findings)}"
            evidence = [item.evidence for item in ordered]
            evidence.append("-> " + ", ".join(findings))
            risks.append(_Risk("slice_overlap", path, line, module_name, "high", detail, evidence))
    for target, uses in output_slices_by_target.items():
        if len(uses) < 2:
            continue
        ordered = sorted(uses, key=lambda item: (item.lo, item.hi, item.line))
        findings: list[str] = []
        for prev, curr in zip(ordered, ordered[1:]):
            if curr.lo <= prev.hi:
                overlap_lo = curr.lo
                overlap_hi = min(prev.hi, curr.hi)
                if overlap_lo == overlap_hi:
                    findings.append(f"overlap at bit {overlap_lo}")
                else:
                    findings.append(f"overlap at bits {overlap_lo}:{overlap_hi}")
            if curr.lo > prev.hi + 1:
                gap_lo = prev.hi + 1
                gap_hi = curr.lo - 1
                if gap_lo == gap_hi:
                    findings.append(f"gap at bit {gap_lo}")
                else:
                    findings.append(f"gap at bits {gap_lo}:{gap_hi}")
        if findings:
            line = ordered[0].line
            detail = f"Target {target} has slice coverage issues: {'; '.join(findings)}"
            evidence = [item.evidence for item in ordered]
            evidence.append("-> " + ", ".join(findings))
            risks.append(_Risk("slice_overlap", path, line, module_name, "high", detail, evidence))
    return risks


def _scan_multi_drive(path: str, module_name: str, module_text: str, module_start_line: int) -> list[_Risk]:
    assigns: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for match in _ASSIGN_FULL_RE.finditer(module_text):
        statement = match.group(0)
        if _ASSIGN_WITH_SLICE_RE.match(statement):
            continue
        signal = match.group(1)
        line = _line_number(module_text, match.start(), module_start_line)
        assigns[signal].append((line, statement.strip()))

    risks: list[_Risk] = []
    for signal, uses in assigns.items():
        if len(uses) < 2:
            continue
        line = uses[0][0]
        evidence = [f"line {entry_line}: {snippet}" for entry_line, snippet in uses]
        detail = f"Signal {signal} is driven by {len(uses)} continuous assignments"
        risks.append(_Risk("multi_drive", path, line, module_name, "high", detail, evidence))
    return risks


def _scan_incomplete_case(
    path: str,
    module_name: str,
    module_text: str,
    module_start_line: int,
    source_lines: list[str],
) -> list[_Risk]:
    risks: list[_Risk] = []
    for match in _CASE_START_RE.finditer(module_text):
        start = match.start()
        end = _find_matching_endcase(module_text, start)
        if end is None:
            continue
        case_body = module_text[start:end]
        if _DEFAULT_RE.search(case_body):
            continue
        line = _line_number(module_text, start, module_start_line)
        if _has_full_case_pragma(source_lines, line):
            continue
        detail = f"{match.group(1).lower()} statement has no default branch"
        risks.append(
            _Risk(
                "incomplete_case",
                path,
                line,
                module_name,
                "medium",
                detail,
                [match.group(0).strip(), "missing default:"],
            )
        )
    return _dedupe_case_risks(risks)


def _find_matching_endcase(text: str, case_start: int) -> int | None:
    depth = 0
    for match in _CASE_OR_ENDCASE_RE.finditer(text, case_start):
        token = match.group(1).lower()
        if token.startswith("case"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.start()
    return None


def _dedupe_case_risks(risks: list[_Risk]) -> list[_Risk]:
    seen: set[tuple[str, int, str]] = set()
    ordered: list[_Risk] = []
    for risk in risks:
        key = (risk.file, risk.line, risk.type)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(risk)
    return ordered


def _scan_narrow_condition_injection(path: str, text: str) -> list[_Risk]:
    risks: list[_Risk] = []
    for match in _ZERO_LITERAL_RE.finditer(text):
        if _line_has_param(text, match.start()):
            continue
        brace_text, brace_start = _extract_enclosing_braces(text, match.start())
        if brace_text is None or not _is_assignment_context(text, brace_start):
            continue
        analysis = _analyze_narrow_injection(brace_text)
        if analysis is None:
            continue
        if _is_plain_zero_extend_assignment(text, brace_start, brace_text):
            continue
        zero_width, total_width = analysis
        line = _line_number(text, brace_start)
        detail = f"Concatenation injects a narrow condition with {zero_width} zero-fill bits"
        evidence = [brace_text.strip()]
        if total_width is not None:
            evidence.append(f"zero_fill_width={zero_width}, total_width={total_width}")
        else:
            evidence.append(f"zero_fill_width={zero_width}, total_width=unknown")
        risks.append(_Risk("narrow_condition_injection", path, line, None, "high", detail, evidence))
    return _dedupe_risks(risks)


def _extract_enclosing_braces(text: str, pos: int) -> tuple[str | None, int | None]:
    start = None
    depth = 0
    for i in range(pos, -1, -1):
        char = text[i]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None, None

    end = None
    depth = 0
    for i in range(start, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None or not (start <= pos <= end):
        return None, None
    return text[start:end + 1], start


def _line_has_param(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    return bool(_PARAM_LINE_RE.search(text[line_start:line_end]))


def _is_assignment_context(text: str, brace_start: int | None) -> bool:
    if brace_start is None:
        return False

    stmt_start, stmt_end = _find_statement_bounds(text, brace_start)
    statement = text[stmt_start:stmt_end]
    if _PARAM_LINE_RE.search(statement):
        return False

    prefix = text[stmt_start:brace_start]
    if _ASSIGN_STATEMENT_RE.search(prefix):
        return True
    assign_op_pos = _find_top_level_assignment_pos(statement)
    if assign_op_pos is None:
        return False
    assign_abs_pos = stmt_start + assign_op_pos
    if assign_abs_pos >= brace_start:
        return False
    return _is_within_procedural_block(text, assign_abs_pos)


def _find_statement_bounds(text: str, pos: int) -> tuple[int, int]:
    stmt_start = text.rfind(";", 0, pos) + 1
    stmt_end = text.find(";", pos)
    if stmt_end == -1:
        stmt_end = len(text)
    return stmt_start, stmt_end


def _find_top_level_assignment_pos(statement: str) -> int | None:
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    i = 0
    while i < len(statement):
        char = statement[i]
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren = max(0, depth_paren - 1)
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace = max(0, depth_brace - 1)
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif depth_paren == depth_brace == depth_bracket == 0:
            next_char = statement[i + 1] if i + 1 < len(statement) else ""
            prev_char = statement[i - 1] if i > 0 else ""
            if char == "<" and next_char == "=":
                return i
            if char == "=" and next_char != "=" and prev_char not in {"!", "<", ">", "="}:
                return i
        i += 1
    return None


def _is_within_procedural_block(text: str, pos: int) -> bool:
    depth = 0
    matches = list(_BLOCK_TOKEN_RE.finditer(text, 0, pos))
    for match in reversed(matches):
        token = match.group(1).lower()
        if token in {"end", "endcase", "join", "join_any", "join_none"}:
            depth += 1
            continue
        if token in {"begin", "case", "casez", "casex", "fork"}:
            if depth > 0:
                depth -= 1
            continue
        if depth == 0 and _PROCEDURAL_BLOCK_RE.fullmatch(token):
            return True
    return False


def _analyze_narrow_injection(brace_text: str) -> tuple[int, int | None] | None:
    inner = brace_text[1:-1].strip()
    parts = _split_top_level_commas(inner)
    if len(parts) != 2:
        return None

    left_zero = _parse_zero_literal(parts[0])
    right_zero = _parse_zero_literal(parts[1])
    if left_zero is None and right_zero is None:
        return None

    zero_width = left_zero or right_zero
    other_part = parts[1] if left_zero is not None else parts[0]
    if _looks_like_all_zero(other_part):
        return None

    other_width = _estimate_expr_width(other_part)
    total_width = zero_width + other_width if other_width is not None else None
    if total_width is None:
        return zero_width, None
    if zero_width / total_width >= 0.75:
        return zero_width, total_width
    return None


def _parse_zero_literal(part: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)'b(0+)\s*", part, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _looks_like_all_zero(part: str) -> bool:
    return bool(re.fullmatch(r"\s*\d+'[bBhHdDoO][0xXzZ_]+\s*", part))


def _estimate_expr_width(expr: str) -> int | None:
    literal_match = re.fullmatch(r"\s*(\d+)'[bBhHdDoO][0-9a-fA-F_xXzZ]+\s*", expr)
    if literal_match:
        return int(literal_match.group(1))
    return None


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    current: list[str] = []
    for char in text:
        if char == "," and depth_paren == depth_brace == depth_bracket == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren = max(0, depth_paren - 1)
        elif char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace = max(0, depth_brace - 1)
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket = max(0, depth_bracket - 1)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _scan_magic_condition(path: str, text: str) -> list[_Risk]:
    risks: list[_Risk] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _PARAM_LINE_RE.search(line):
            continue
        if _CASE_ITEM_LINE_RE.match(line) or _DEFAULT_LINE_RE.match(line):
            continue
        for match in _MAGIC_COMPARE_RE.finditer(line):
            literal = match.group("lit")
            if _is_allowed_literal(literal):
                continue
            detail = f"Condition compares against magic literal {literal}"
            evidence = [line.strip()]
            risks.append(_Risk("magic_condition", path, line_no, None, "low", detail, evidence))
    return _dedupe_risks(risks)


def _is_allowed_literal(literal: str) -> bool:
    match = re.fullmatch(r"(\d+)'([bBhHdDoO])([0-9a-fA-F_xXzZ]+)", literal)
    if not match:
        return False
    _width, base, digits = match.groups()
    normalized = digits.replace("_", "").lower()
    if normalized in {"0", "1"}:
        return True
    if base.lower() == "b" and set(normalized) == {"1"}:
        return True
    if base.lower() == "h" and set(normalized) == {"f"}:
        return True
    return False


def _build_module_port_directions(file_entries: list[dict]) -> dict[str, dict[str, str]]:
    module_port_dirs: dict[str, dict[str, str]] = {}
    for entry in file_entries:
        path = entry["path"]
        if not os.path.exists(path):
            continue
        with open(path, "r", errors="replace") as handle:
            raw_text = handle.read()
        for match in _MODULE_HEADER_RE.finditer(raw_text):
            module_name = match.group("name")
            ports_blob = match.group("ports")
            directions = module_port_dirs.setdefault(module_name, {})
            for part in _split_top_level_commas(ports_blob):
                lower = part.lower()
                direction = None
                if "input" in lower:
                    direction = "input"
                elif "output" in lower:
                    direction = "output"
                elif "inout" in lower:
                    direction = "inout"
                if direction is None:
                    continue
                for port_name in _extract_declared_names(part):
                    directions.setdefault(port_name, direction)
    return module_port_dirs


def _extract_declared_names(port_decl: str) -> list[str]:
    scrubbed = re.sub(r"\[[^\]]+\]", " ", port_decl)
    scrubbed = re.sub(
        r"\b(input|output|inout|wire|reg|logic|signed|unsigned|var)\b",
        " ",
        scrubbed,
        flags=re.IGNORECASE,
    )
    return [
        match.group(1)
        for match in _PORT_NAME_RE.finditer(scrubbed)
        if match.group(1) not in {"input", "output", "inout"}
    ]


def _has_full_case_pragma(source_lines: list[str], case_line: int) -> bool:
    candidate_lines: list[str] = []
    if 0 < case_line <= len(source_lines):
        candidate_lines.append(source_lines[case_line - 1])
    if 0 <= case_line < len(source_lines):
        candidate_lines.append(source_lines[case_line])
    return any(_FULL_CASE_COMMENT_RE.search(line) for line in candidate_lines)


def _is_plain_zero_extend_assignment(text: str, brace_start: int | None, brace_text: str) -> bool:
    if brace_start is None:
        return False
    stmt_start, stmt_end = _find_statement_bounds(text, brace_start)
    statement = text[stmt_start:stmt_end]
    assign_op_pos = _find_top_level_assignment_pos(statement)
    if assign_op_pos is None:
        return False
    assign_len = 2 if statement[assign_op_pos:assign_op_pos + 2] == "<=" else 1
    rhs = statement[assign_op_pos + assign_len:].strip()
    if rhs != brace_text.strip():
        return False
    other_part = _extract_narrow_injection_other_part(brace_text)
    return other_part is not None and not _CONDITION_OP_RE.search(other_part)


def _extract_narrow_injection_other_part(brace_text: str) -> str | None:
    inner = brace_text[1:-1].strip()
    parts = _split_top_level_commas(inner)
    if len(parts) != 2:
        return None
    left_zero = _parse_zero_literal(parts[0])
    right_zero = _parse_zero_literal(parts[1])
    if left_zero is None and right_zero is None:
        return None
    return parts[1] if left_zero is not None else parts[0]


def _dedupe_risks(risks: list[_Risk]) -> list[_Risk]:
    seen: set[tuple[str, int, str, str]] = set()
    ordered: list[_Risk] = []
    for risk in risks:
        key = (risk.file, risk.line, risk.type, risk.detail)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(risk)
    return ordered
