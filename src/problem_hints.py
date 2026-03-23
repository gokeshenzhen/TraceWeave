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
_HEX_X_RE = re.compile(r"(?:[0-9a-fA-F][Xx]|[Xx][0-9a-fA-F]|^[Xx]+$)")
_HEX_Z_RE = re.compile(r"(?:[0-9a-fA-F][Zz]|[Zz][0-9a-fA-F]|^[Zz]+$)")
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
    """Check whether expected/actual contains hex-adjacent X unknown bits."""
    for field in ("expected", "actual"):
        value = event.get(field)
        if value is not None and _HEX_X_RE.search(str(value)):
            return True
    return False


def _has_z_in_hex_value(event: dict[str, Any]) -> bool:
    """Check whether expected/actual contains hex-adjacent Z high-impedance bits."""
    for field in ("expected", "actual"):
        value = event.get(field)
        if value is not None and _HEX_Z_RE.search(str(value)):
            return True
    return False
