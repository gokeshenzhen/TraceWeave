"""
structural_scanner.py
对编译文件列表中的 RTL/TB 源码做 Scope 1 正则静态结构风险扫描。
"""

from __future__ import annotations

from array import array
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
import os
import re
from typing import Iterable

from .cancellation import check_cancelled
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
# SVA assertion context. A literal comparison inside a property/sequence block
# (or an implication line) is a checker by construction — e.g. an AHB 1KB
# boundary assertion `(HTRANS==3) |-> (HADDR[10:0] != 11'b...)` — not suspect
# DUT/TB control logic. Same self-filtering rationale as the param/case/default
# skips above; protocol-agnostic (no signal-name matching). `\bproperty\b` does
# not match inside `endproperty` (no word boundary), so the two stay distinct.
_PROPERTY_START_RE = re.compile(r"\bproperty\b", re.IGNORECASE)
_PROPERTY_END_RE = re.compile(r"\bendproperty\b", re.IGNORECASE)
_SEQUENCE_START_RE = re.compile(r"\bsequence\b", re.IGNORECASE)
_SEQUENCE_END_RE = re.compile(r"\bendsequence\b", re.IGNORECASE)
_ASSERT_IMPLICATION_RE = re.compile(r"\|->|\|=>")
_ZERO_LITERAL_RE = re.compile(r"(\d+)'b(0+)", re.IGNORECASE)
_BRACE_RE = re.compile(r"[{}]")
_MAGIC_CANDIDATE_RE = re.compile(
    r"(?:==|!=)[^\r\n]*\d+'[bBhHdDoO]"
)
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


class _LineIndex:
    """Compact file-local offset-to-line mapping."""

    def __init__(self, text: str) -> None:
        self._newlines = array("Q")
        offset = text.find("\n")
        while offset >= 0:
            if len(self._newlines) % 65_536 == 0:
                check_cancelled()
            self._newlines.append(offset)
            offset = text.find("\n", offset + 1)
        check_cancelled()

    def line_number(self, offset: int) -> int:
        return bisect_left(self._newlines, offset) + 1


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
    check_cancelled()
    file_entries = compile_result.get("files", {}).get("user", [])
    eligible_entries = [entry for entry in file_entries if _should_scan_file(entry["path"])]
    module_port_dirs = (
        _build_module_port_directions(eligible_entries)
        if "slice_overlap" in categories_scanned
        else {}
    )

    risks: list[_Risk] = []
    skipped_files: list[str] = []
    files_scanned = 0

    for entry in eligible_entries:
        check_cancelled()
        path = entry["path"]
        if not os.path.exists(path):
            skipped_files.append(path)
            continue
        try:
            file_risks = _scan_file(path, categories_scanned, module_port_dirs)
        except OSError:
            skipped_files.append(path)
            continue
        files_scanned += 1
        risks.extend(file_risks)

    eligible_file_count = len(eligible_entries)
    parse_warnings = compile_result.get("parse_warnings") or []
    coverage_warnings: list[str] = []
    if eligible_file_count == 0:
        coverage_status = "zero_coverage"
        coverage_warnings.append(
            "ZERO COVERAGE: no supported Verilog/SystemVerilog source files were available to scan; "
            "total_risks=0 is not evidence of a clean design."
        )
    elif skipped_files or parse_warnings:
        coverage_status = "degraded"
        if skipped_files:
            coverage_warnings.append(
                "DEGRADED COVERAGE: scanned "
                f"{files_scanned} of {eligible_file_count} supported source files; "
                "total_risks covers only the scanned files."
            )
    else:
        coverage_status = "complete"
    if parse_warnings:
        warning_count = len(parse_warnings)
        suffix = "warning" if warning_count == 1 else "warnings"
        coverage_warnings.append(
            f"Compile-log parsing reported {warning_count} {suffix}; "
            "structural source coverage may be incomplete."
        )

    ordered_risks = sorted(risks, key=lambda item: (item.file, item.line, item.type, item.detail))
    return {
        "scan_scope": scan_scope,
        "eligible_file_count": eligible_file_count,
        "files_scanned": files_scanned,
        "coverage_status": coverage_status,
        "coverage_warnings": coverage_warnings,
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
    check_cancelled()
    with open(path, "r", errors="replace") as handle:
        raw_text = handle.read()
    check_cancelled()
    text = _strip_comments_keep_lines(raw_text)
    module_categories = {
        "slice_overlap",
        "multi_drive",
        "incomplete_case",
    }
    needs_line_index = bool(
        module_categories.intersection(categories)
        or "narrow_condition_injection" in categories
    )
    line_index = _LineIndex(text) if needs_line_index else None
    source_lines = (
        raw_text.splitlines() if "incomplete_case" in categories else []
    )

    risks: list[_Risk] = []
    if module_categories.intersection(categories):
        assert line_index is not None
        for (
            module_name,
            module_text,
            module_offset,
            module_line_bias,
        ) in _iter_modules(text, line_index):
            if "slice_overlap" in categories and "[" in module_text:
                risks.extend(
                    _scan_slice_overlap(
                        path,
                        module_name,
                        module_text,
                        module_offset,
                        module_line_bias,
                        line_index,
                        module_port_dirs,
                    )
                )
            if "multi_drive" in categories:
                risks.extend(
                    _scan_multi_drive(
                        path,
                        module_name,
                        module_text,
                        module_offset,
                        module_line_bias,
                        line_index,
                    )
                )
            if "incomplete_case" in categories:
                risks.extend(
                    _scan_incomplete_case(
                        path,
                        module_name,
                        module_text,
                        module_offset,
                        module_line_bias,
                        line_index,
                        source_lines,
                    )
                )
    if "narrow_condition_injection" in categories:
        assert line_index is not None
        risks.extend(_scan_narrow_condition_injection(path, text, line_index))
    if "magic_condition" in categories:
        risks.extend(_scan_magic_condition(path, text))
    return risks


def _strip_comments_keep_lines(text: str) -> str:
    def replace_block(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    text = re.sub(r"/\*.*?\*/", replace_block, text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _iter_modules(
    text: str,
    line_index: _LineIndex,
) -> Iterable[tuple[str, str, int, int]]:
    for match in _MODULE_BLOCK_RE.finditer(text):
        module_name = match.group(1)
        module_text = match.group(2)
        module_offset = match.start(2)
        # Preserve the historical line attribution. ``_MODULE_BLOCK_RE`` may
        # consume leading blank lines through its multiline ``^\s*`` prefix,
        # so the old relative counter used match.start() as its base even
        # though group 2 begins on the module declaration line.
        module_line_bias = (
            line_index.line_number(match.start())
            - line_index.line_number(module_offset)
        )
        yield module_name, module_text, module_offset, module_line_bias


def _normalize_slice(a: str, b: str) -> tuple[int, int]:
    lo = min(int(a), int(b))
    hi = max(int(a), int(b))
    return lo, hi


def _scan_slice_overlap(
    path: str,
    module_name: str,
    module_text: str,
    module_offset: int,
    module_line_bias: int,
    line_index: _LineIndex,
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
            line = (
                line_index.line_number(
                    module_offset + inst_match.start("body") + match.start()
                )
                + module_line_bias
            )
            snippet = match.group(0).strip()
            use = _SliceUse(target, lo, hi, line, f"port {port_name}: {snippet}")
            if port_dirs.get(port_name) == "output":
                output_slices_by_target[target].append(use)
            else:
                slices_by_target[target].append(use)
    for match in _ASSIGN_SLICE_RE.finditer(module_text):
        target, lhs, rhs = match.groups()
        lo, hi = _normalize_slice(lhs, rhs)
        line = (
            line_index.line_number(module_offset + match.start())
            + module_line_bias
        )
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


def _scan_multi_drive(
    path: str,
    module_name: str,
    module_text: str,
    module_offset: int,
    module_line_bias: int,
    line_index: _LineIndex,
) -> list[_Risk]:
    assigns: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for match in _ASSIGN_FULL_RE.finditer(module_text):
        statement = match.group(0)
        if _ASSIGN_WITH_SLICE_RE.match(statement):
            continue
        signal = match.group(1)
        line = (
            line_index.line_number(module_offset + match.start())
            + module_line_bias
        )
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
    module_offset: int,
    module_line_bias: int,
    line_index: _LineIndex,
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
        line = line_index.line_number(module_offset + start) + module_line_bias
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


def _scan_narrow_condition_injection(
    path: str,
    text: str,
    line_index: _LineIndex,
) -> list[_Risk]:
    risks: list[_Risk] = []
    zero_matches = list(_ZERO_LITERAL_RE.finditer(text))
    brace_spans = _index_enclosing_brace_spans(
        text,
        [match.start() for match in zero_matches],
    )
    for match_index, match in enumerate(zero_matches):
        if match_index % 4_096 == 0:
            check_cancelled()
        if _line_has_param(text, match.start()):
            continue
        span = brace_spans.get(match.start())
        if span is None:
            continue
        brace_start, brace_end = span
        brace_text = text[brace_start:brace_end]
        if not _is_assignment_context(text, brace_start):
            continue
        analysis = _analyze_narrow_injection(brace_text)
        if analysis is None:
            continue
        if _is_plain_zero_extend_assignment(text, brace_start, brace_text):
            continue
        zero_width, total_width = analysis
        line = line_index.line_number(brace_start)
        detail = f"Concatenation injects a narrow condition with {zero_width} zero-fill bits"
        evidence = [brace_text.strip()]
        if total_width is not None:
            evidence.append(f"zero_fill_width={zero_width}, total_width={total_width}")
        else:
            evidence.append(f"zero_fill_width={zero_width}, total_width=unknown")
        risks.append(_Risk("narrow_condition_injection", path, line, None, "high", detail, evidence))
    return _dedupe_risks(risks)


def _index_enclosing_brace_spans(
    text: str,
    positions: Iterable[int],
) -> dict[int, tuple[int, int]]:
    """Map targets to their innermost balanced ``{...}`` span in one pass.

    The old per-target backward/forward walk was quadratic when a large source
    contained many sized zero literals. A stack preserves the same purely
    lexical brace semantics while visiting each source character once.
    """

    targets = sorted(set(positions))
    if not targets:
        return {}
    result: dict[int, tuple[int, int]] = {}
    # Each frame retains only targets whose innermost open brace is that frame.
    stack: list[tuple[int, list[int]]] = []
    target_index = 0
    for event_index, match in enumerate(_BRACE_RE.finditer(text)):
        if event_index % 4_096 == 0:
            check_cancelled()
        brace_offset = match.start()
        while (
            target_index < len(targets)
            and targets[target_index] < brace_offset
        ):
            if stack:
                stack[-1][1].append(targets[target_index])
            target_index += 1
        char = match.group(0)
        if char == "{":
            stack.append((brace_offset, []))
        elif stack:
            start, enclosed_targets = stack.pop()
            end = brace_offset + 1
            for target in enclosed_targets:
                result[target] = (start, end)
        if target_index >= len(targets) and not stack:
            break
    check_cancelled()
    return result


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


def _assertion_line_numbers(text: str) -> set[int]:
    """Line numbers that sit inside an SVA assertion context.

    Tracks property/sequence block depth and flags any line carrying an
    implication operator. Heuristic and line-based, matching the rest of the
    scanner; `endproperty`/`endsequence` are not seen as block starts.
    """
    assertion_lines: set[int] = set()
    prop_depth = 0
    seq_depth = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line_no % 4_096 == 0:
            check_cancelled()
        starts_prop = bool(_PROPERTY_START_RE.search(line))
        ends_prop = bool(_PROPERTY_END_RE.search(line))
        starts_seq = bool(_SEQUENCE_START_RE.search(line))
        ends_seq = bool(_SEQUENCE_END_RE.search(line))
        in_block = prop_depth > 0 or seq_depth > 0
        if (
            in_block
            or starts_prop
            or ends_prop
            or starts_seq
            or ends_seq
            or _ASSERT_IMPLICATION_RE.search(line)
        ):
            assertion_lines.add(line_no)
        if starts_prop:
            prop_depth += 1
        if ends_prop:
            prop_depth = max(0, prop_depth - 1)
        if starts_seq:
            seq_depth += 1
        if ends_seq:
            seq_depth = max(0, seq_depth - 1)
    return assertion_lines


def _scan_magic_condition(path: str, text: str) -> list[_Risk]:
    risks: list[_Risk] = []
    assertion_lines = _assertion_line_numbers(text)
    line_no = 1
    line_start = 0
    count_cursor = 0
    previous_line_start = -1
    for candidate_index, candidate in enumerate(
        _MAGIC_CANDIDATE_RE.finditer(text)
    ):
        if candidate_index % 4_096 == 0:
            check_cancelled()
        candidate_start = candidate.start()
        newline_count = text.count("\n", count_cursor, candidate_start)
        if newline_count:
            line_no += newline_count
            line_start = text.rfind("\n", count_cursor, candidate_start) + 1
        count_cursor = candidate_start
        if line_start == previous_line_start:
            continue
        previous_line_start = line_start
        line_end = text.find("\n", candidate.end())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        if _PARAM_LINE_RE.search(line):
            continue
        if _CASE_ITEM_LINE_RE.match(line) or _DEFAULT_LINE_RE.match(line):
            continue
        if line_no in assertion_lines:
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
        check_cancelled()
        path = entry["path"]
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", errors="replace") as handle:
                raw_text = handle.read()
        except OSError:
            continue
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
