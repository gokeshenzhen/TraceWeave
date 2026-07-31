from __future__ import annotations

import re
from typing import Any

from .schemas import ProblemHints


_HEURISTIC_X_PATTERNS = (
    re.compile(r"\bxprop\b", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])x(?![A-Za-z0-9_])", re.IGNORECASE),
    re.compile(r"\bx-state\b", re.IGNORECASE),
    re.compile(r"\bunknown value\b", re.IGNORECASE),
)
_HEURISTIC_Z_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])z(?![A-Za-z0-9_])", re.IGNORECASE),
    re.compile(r"\bhigh[- ]?z\b", re.IGNORECASE),
    re.compile(r"\btri[- ]?state\b", re.IGNORECASE),
    re.compile(r"\bhigh impedance\b", re.IGNORECASE),
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNKNOWN_ONLY_RE = re.compile(r"^[xz?]+$", re.IGNORECASE)
_SV_LITERAL_RE = re.compile(r"^(?:\d+)?'[bdho][0-9a-fxz?_]+$", re.IGNORECASE)
_HEX_PREFIX_RE = re.compile(r"^0x[0-9a-fxz?_]+$", re.IGNORECASE)
_HEX_UNKNOWN_RE = re.compile(r"^[0-9a-f]*[xz][0-9a-fxz]*$", re.IGNORECASE)
_VALUE_LIKE_STRUCTURED_KEYS = frozenset(
    {"value", "expected", "actual", "observed", "got", "exp", "act"}
)
_ERROR_PATTERN_PRIORITY = (
    "mismatch",
    "timeout",
    "deadlock",
    "protocol",
    "tb_error",
    "unknown",
)


def compute_problem_hints(summary: dict[str, Any], events: list[dict[str, Any]]) -> ProblemHints:
    first_time = None
    groups = summary.get("groups", [])
    if groups:
        first_time = groups[0].get("first_time_ps")
    return _build_problem_hints(events, first_time)


def compute_problem_hints_from_events(events: list[dict[str, Any]]) -> ProblemHints:
    first_error_time_ps = None
    for event in events:
        time_ps = event.get("time_ps")
        if time_ps is not None and time_ps >= 0:
            if first_error_time_ps is None or time_ps < first_error_time_ps:
                first_error_time_ps = time_ps
    return _build_problem_hints(events, first_error_time_ps)


def problem_hints_from_event(
    event: dict[str, Any] | None,
    first_error_time_ps: int | None,
) -> ProblemHints:
    return _build_problem_hints([event] if event else [], first_error_time_ps)


def _build_problem_hints(
    events: list[dict[str, Any]],
    first_error_time_ps: int | None,
) -> ProblemHints:
    # These flags are intentionally heuristic symptom hints for LLM consumers,
    # not parser-guaranteed structured facts.
    has_x = False
    has_z = False
    mechanisms: set[str] = set()

    for event in events:
        mechanism = event.get("failure_mechanism")
        if mechanism:
            mechanisms.add(mechanism)
        payload = _event_text(event)
        if mechanism == "xprop" or _matches_any(payload, _HEURISTIC_X_PATTERNS):
            has_x = True
        if not has_x and _has_x_in_hex_value(event):
            has_x = True
        if _matches_any(payload, _HEURISTIC_Z_PATTERNS):
            has_z = True
        if not has_z and _has_z_in_hex_value(event):
            has_z = True

    return ProblemHints(
        has_x=has_x,
        has_z=has_z,
        first_error_time_ps=first_error_time_ps,
        error_pattern=_select_error_pattern(mechanisms, has_x, has_z),
    )


def _select_error_pattern(mechanisms: set[str], has_x: bool, has_z: bool) -> str | None:
    if has_z:
        return "zprop"
    if has_x or "xprop" in mechanisms:
        return "xprop"
    for pattern in _ERROR_PATTERN_PRIORITY:
        if pattern in mechanisms:
            return pattern
    return None


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def event_has_x_or_z(event: dict[str, Any]) -> tuple[bool, bool]:
    payload = _event_text(event)
    mechanism = event.get("failure_mechanism")

    has_x = (
        mechanism == "xprop"
        or _matches_any(payload, _HEURISTIC_X_PATTERNS)
        or _has_x_in_hex_value(event)
    )
    has_z = (
        _matches_any(payload, _HEURISTIC_Z_PATTERNS)
        or _has_z_in_hex_value(event)
    )
    return has_x, has_z


def event_has_raw_x_or_z_evidence(event: dict[str, Any]) -> tuple[bool, bool]:
    payload = _event_text(event)
    has_x = _matches_any(payload, _HEURISTIC_X_PATTERNS) or _has_x_in_hex_value(event)
    has_z = _matches_any(payload, _HEURISTIC_Z_PATTERNS) or _has_z_in_hex_value(event)
    return has_x, has_z


def compute_xprop_priority_for_group(
    group_events: list[dict[str, Any]],
    global_has_x: bool,
    global_has_z: bool,
) -> str | None:
    if not global_has_x and not global_has_z:
        return None
    for event in group_events:
        has_x, has_z = event_has_raw_x_or_z_evidence(event)
        if has_x or has_z:
            return "high"
    return "normal"


def _event_text(event: dict[str, Any]) -> str:
    payloads = [event.get("message_text"), event.get("group_signature"), event.get("instance_path")]
    structured_fields = event.get("structured_fields") or {}
    payloads.extend(str(value) for value in structured_fields.values() if value is not None)
    payloads.extend(
        str(event.get(field))
        for field in ("expected", "actual", "transaction_hint")
        if event.get(field) is not None
    )
    return " ".join(str(text) for text in payloads if text)


def _has_x_in_hex_value(event: dict[str, Any]) -> bool:
    """Check whether a known value-bearing field contains X unknown bits."""
    for value in _iter_value_payloads(event):
        if _value_payload_contains_unknown(value, {"x"}):
            return True
    return False


def _has_z_in_hex_value(event: dict[str, Any]) -> bool:
    """Check whether a known value-bearing field contains Z high-impedance bits."""
    for value in _iter_value_payloads(event):
        if _value_payload_contains_unknown(value, {"z"}):
            return True
    return False


def _iter_value_payloads(event: dict[str, Any]):
    """Yield only fields whose keys mechanically identify value payloads.

    Arbitrary structured values are deliberately excluded: identifiers such as
    ``signal=x_axis`` must not turn a log into an X-propagation symptom. Keys are
    matched case-insensitively because simulator/user log formats are not
    consistent about capitalization.
    """
    for field in ("expected", "actual"):
        value = event.get(field)
        if value is not None:
            yield value

    structured_fields = event.get("structured_fields") or {}
    if not isinstance(structured_fields, dict):
        return
    for key, value in structured_fields.items():
        if (
            value is not None
            and str(key).lower() in _VALUE_LIKE_STRUCTURED_KEYS
        ):
            yield value


def _value_payload_contains_unknown(value: Any, unknown_chars: set[str]) -> bool:
    if value is None:
        return False
    normalized = str(value).strip().lower()
    if "=" in normalized:
        normalized = normalized.split("=", 1)[1].strip()
    if not normalized:
        return False
    if _UNKNOWN_ONLY_RE.fullmatch(normalized):
        return any(char in unknown_chars for char in normalized)
    if _IDENTIFIER_RE.fullmatch(normalized):
        return False
    if _SV_LITERAL_RE.fullmatch(normalized):
        literal_body = normalized.split("'", 1)[1][1:]
        return any(char in unknown_chars for char in literal_body)
    if _HEX_PREFIX_RE.fullmatch(normalized):
        return any(char in unknown_chars for char in normalized[2:])
    if _HEX_UNKNOWN_RE.fullmatch(normalized):
        return any(char in unknown_chars for char in normalized)
    return False
